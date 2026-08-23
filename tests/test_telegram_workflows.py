import json
import inspect
import os
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from modules.database import Database
from modules.draft_pipeline import DraftPipeline
from modules.fact_guard import FactCheckResult
from modules.media_processor import MediaProcessor
from modules.media_store import media_store_lock
from modules.telegram_api import TelegramApi, telegram_media_metadata
from modules.telegram_controller import TelegramController


FUTURE_SLOT = "2030-08-15T12:00:00+00:00"


class NeverPlanner:
    def plan(self, _slot):
        raise AssertionError("the Telegram workflow must not create a draft")


class CopyGenerator:
    def __init__(self, rewritten=None):
        self.rewritten = rewritten
        self.rewrite_calls = []

    def rewrite_to_limit(self, text, sources, limit):
        self.rewrite_calls.append((text, sources, limit))
        return self.rewritten


class Guard:
    def __init__(self, approved=True):
        self.approved = approved
        self.calls = []

    def check(self, text, sources):
        self.calls.append((text, sources))
        return FactCheckResult(self.approved, [] if self.approved else ["malformed_claim"])


class Scorer:
    def __init__(self, total=90):
        self.total = total
        self.calls = []

    def score_draft(self, text):
        self.calls.append(text)
        return {"clarity": 18, "total": self.total}


class WorkflowTelegramApi:
    def __init__(self, media_library_dir):
        self.media_library_dir = Path(media_library_dir)
        self.messages = []
        self.media_messages = []
        self.callback_answers = []
        self.events = []

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append((str(chat_id), text, kwargs))
        self.events.append(("message", str(chat_id), text, kwargs))
        return {"message_id": len(self.messages)}

    def send_media(self, chat_id, media, media_type, **kwargs):
        if isinstance(media, (str, Path)):
            raise AssertionError("draft previews must use a verified open stream")
        content = media.read()
        self.media_messages.append((str(chat_id), content, media_type, kwargs))
        self.events.append(("media", str(chat_id), content, media_type, kwargs))
        return {"message_id": len(self.events)}

    def answer_callback(self, callback_id, **kwargs):
        self.callback_answers.append((callback_id, kwargs))
        self.events.append(("callback", callback_id, kwargs))
        return True


class Notifier:
    def __init__(self):
        self.errors = []

    def notify_error(self, context, error):
        self.errors.append((context, type(error).__name__))


class StubPipeline:
    def __init__(self, db):
        self.db = db
        self.calls = []

    def approve(self, draft_id, approved_by):
        self.calls.append(("approve", draft_id, approved_by))
        return self.db.transition_post_draft(
            draft_id, ["pending_approval"], "approved", approved_by=approved_by,
        )

    def regenerate(self, draft_id):
        self.calls.append(("regen", draft_id))
        return self.db.get_post_draft(draft_id)

    def edit(self, draft_id, text):
        self.calls.append(("edit", draft_id, text))
        return self.db.get_post_draft(draft_id)

    def edit_from_telegram_session(
        self, draft_id, text, *, state_key, expected_state_value, session_token,
    ):
        del session_token
        if not self.db.compare_and_clear_state(state_key, expected_state_value):
            return None, "session_conflict"
        return self.edit(draft_id, text), "created"

    def postpone(self, draft_id, slot):
        self.calls.append(("postpone", draft_id, slot))
        return True

    def postpone_from_telegram_session(
        self, draft_id, slot, *, state_key, expected_state_value,
    ):
        if not self.db.compare_and_clear_state(state_key, expected_state_value):
            return "session_conflict"
        self.postpone(draft_id, slot)
        return "postponed"

    def discard(self, draft_id, reason):
        self.calls.append(("discard", draft_id, reason))
        return self.db.transition_post_draft(
            draft_id, ["pending_approval"], "discarded", error=reason,
        )


class StubMatcher:
    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def attach_best(self, draft_id):
        self.calls.append(draft_id)
        return self.result


class StubAnalytics:
    def weekly_report(self):
        return {
            "followers_total": 120,
            "new_followers": 4,
            "attribution_label": "correlation",
        }


def message_update(update_id, text, chat_id=42):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text},
    }


def callback_update(update_id, data, chat_id=42):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "message": {"chat": {"id": chat_id}},
            "data": data,
        },
    }


def workflow_controller(
    db,
    telegram,
    *,
    pipeline=None,
    matcher=None,
    analytics=None,
    now=None,
    scheduler_status=None,
    trusted_domains=None,
):
    return TelegramController(
        telegram_api=telegram,
        db=db,
        notifier=Notifier(),
        authorized_chat_id="42",
        draft_pipeline=pipeline,
        media_matcher=matcher,
        analytics=analytics,
        dry_run=True,
        now_fn=lambda: now or datetime(2029, 8, 15, tzinfo=timezone.utc),
        scheduler_status=scheduler_status,
        news_trusted_domains=(
            {"news.example"} if trusted_domains is None else trusted_domains
        ),
    )


def add_pending_draft(db, *, slot=FUTURE_SLOT, text="Old pending copy"):
    source_id = db.add_content_source(
        "founder_note",
        "I learned that flexible access helps independent studios.",
        verified_by="floriano",
    )
    draft_id = db.create_post_draft(
        text=text,
        category="founder_story",
        source_ids=[source_id],
        score_data={"total": 88},
        intended_slot=slot,
        publication_key=f"telegram-test:{slot}:{text}",
    )
    return source_id, draft_id


_PREVIEW_JPEG = b"\xff\xd8\xff\xe0" + b"telegram-preview"
_PREVIEW_MP4 = (
    b"\x00\x00\x00\x14ftypisom\x00\x00\x00\x00mp42" + b"telegram-preview"
)
_PREVIEW_DOCUMENT_FILENAME = telegram_media_metadata({
    "document": {
        "file_id": "document-file-id",
        "file_unique_id": "document-unique-id",
        "file_name": "preview.jpg",
        "mime_type": "image/jpeg",
        "file_size": len(_PREVIEW_JPEG),
    },
})["message_filename"]


def attach_verified_preview(
    db,
    tmp_path,
    draft_id,
    *,
    content=_PREVIEW_JPEG,
    filename="photo-preview.jpg",
    mime_type="image/jpeg",
):
    media_root = tmp_path / f"media-{draft_id}"
    media_root.mkdir(mode=0o700, parents=True)
    staged = media_root / ("staged" + Path(filename).suffix)
    staged.write_bytes(content)
    record = MediaProcessor(db).process_new_file(
        str(staged),
        filename,
        mime_type,
        len(content),
        "Real studio",
    )
    assert db.attach_media_to_draft(record["id"], draft_id)
    return record


def test_session_compare_clear_survives_restart_and_has_one_thread_winner(tmp_path):
    path = str(tmp_path / "sessions.db")
    first = Database(path)
    key = "telegram_session:42"
    value = json.dumps({
        "version": 1,
        "token": "session-token",
        "kind": "draft_edit",
        "step": "text",
        "payload": {"draft_id": 7},
        "expires_at": "2030-08-15T12:30:00+00:00",
    }, sort_keys=True, separators=(",", ":"))
    first.set_state(key, value)

    restarted = [Database(path), Database(path)]
    barrier = threading.Barrier(2)
    outcomes = []

    def consume(index):
        barrier.wait()
        outcomes.append(restarted[index].compare_and_clear_state(key, value))

    threads = [threading.Thread(target=consume, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == [False, True]
    assert Database(path).get_state(key) is None


def _session_json(kind, step, payload):
    return json.dumps({
        "version": 1,
        "token": "atomic-session-token",
        "kind": kind,
        "step": step,
        "payload": payload,
        "expires_at": "2030-08-15T12:30:00+00:00",
    }, sort_keys=True, separators=(",", ":"))


def test_terminal_source_failure_rolls_back_exact_session_and_insert(tmp_path):
    db = Database(str(tmp_path / "source-atomic.db"))
    key = "telegram_session:42"
    raw = _session_json(
        "source_intake",
        "news_source",
        {
            "text": "Grounded report",
            "url": "https://news.example/report",
            "published_at": "2029-08-14",
        },
    )
    db.set_state(key, raw)
    with db._conn() as conn:
        conn.execute("""
            CREATE TRIGGER fail_terminal_source
            BEFORE INSERT ON content_sources
            BEGIN SELECT RAISE(ABORT, 'injected source failure'); END
        """)

    try:
        db.add_content_source_consuming_state_atomic(
            state_key=key,
            expected_state_value=raw,
            source_type="verified_news",
            text="Grounded report",
            url="https://news.example/report",
            metadata={
                "title": "Grounded report",
                "summary": "Grounded report",
                "published_at": "2029-08-14",
                "source_name": "News Example",
            },
            trust_state="verified",
            verified_by="floriano",
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("injected SQLite failure did not propagate")

    assert Database(db.db_path).get_state(key) == raw
    assert Database(db.db_path).get_eligible_sources("verified_news") == []


def test_non_news_terminal_failure_rolls_back_exact_session_and_insert(tmp_path):
    db = Database(str(tmp_path / "non-news-atomic.db"))
    key = "telegram_session:42"
    raw = _session_json(
        "source_intake", "classification", {"text": "Founder fact"},
    )
    db.set_state(key, raw)
    with db._conn() as conn:
        conn.execute("""
            CREATE TRIGGER fail_terminal_non_news
            BEFORE INSERT ON content_sources
            BEGIN SELECT RAISE(ABORT, 'injected non-news failure'); END
        """)

    try:
        db.add_content_source_consuming_state_atomic(
            state_key=key,
            expected_state_value=raw,
            source_type="founder_note",
            text="Founder fact",
            trust_state="verified",
            verified_by="floriano",
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("injected SQLite failure did not propagate")

    assert Database(db.db_path).get_state(key) == raw
    assert Database(db.db_path).get_eligible_sources("founder_note") == []


def test_postpone_terminal_failure_rolls_back_session_slot_and_revision(tmp_path):
    db = Database(str(tmp_path / "postpone-atomic.db"))
    _source_id, draft_id = add_pending_draft(db)
    prior = db.get_post_draft(draft_id)
    key = "telegram_session:42"
    raw = _session_json("draft_postpone", "slot", {"draft_id": draft_id})
    db.set_state(key, raw)
    with db._conn() as conn:
        conn.execute("""
            CREATE TRIGGER fail_terminal_postpone
            BEFORE UPDATE OF intended_slot ON post_drafts
            BEGIN SELECT RAISE(ABORT, 'injected postpone failure'); END
        """)

    try:
        db.postpone_post_draft_consuming_state_atomic(
            state_key=key,
            expected_state_value=raw,
            draft_id=draft_id,
            expected_revision=prior["revision"],
            expected_statuses=["pending_approval"],
            new_slot="2030-08-17T12:00:00+00:00",
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("injected SQLite failure did not propagate")

    recovered = Database(db.db_path)
    assert recovered.get_state(key) == raw
    current = recovered.get_post_draft(draft_id)
    assert current["intended_slot"] == prior["intended_slot"]
    assert current["revision"] == prior["revision"]


def test_edit_terminal_failure_rolls_back_session_and_replacement(tmp_path):
    db = Database(str(tmp_path / "edit-atomic.db"))
    source_id, draft_id = add_pending_draft(db)
    prior = db.get_post_draft(draft_id)
    key = "telegram_session:42"
    raw = _session_json("draft_edit", "text", {"draft_id": draft_id})
    db.set_state(key, raw)
    with db._conn() as conn:
        conn.execute("""
            CREATE TRIGGER fail_terminal_edit
            BEFORE INSERT ON post_drafts
            WHEN NEW.publication_key = 'telegram-edit:atomic-session-token'
            BEGIN SELECT RAISE(ABORT, 'injected edit failure'); END
        """)

    try:
        db.replace_post_draft_consuming_state_atomic(
            state_key=key,
            expected_state_value=raw,
            prior_draft_id=draft_id,
            expected_revision=prior["revision"],
            expected_slot=prior["intended_slot"],
            expected_category=prior["category"],
            expected_source_ids=[source_id],
            text="Edited copy",
            score_data={"total": 90},
            publication_key="telegram-edit:atomic-session-token",
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("injected SQLite failure did not propagate")

    recovered = Database(db.db_path)
    assert recovered.get_state(key) == raw
    assert recovered.get_post_draft(draft_id)["status"] == "pending_approval"
    assert len(recovered.list_post_drafts()) == 1


def test_concurrent_terminal_source_session_has_one_business_winner(tmp_path):
    path = str(tmp_path / "atomic-winner.db")
    key = "telegram_session:42"
    raw = _session_json(
        "source_intake", "classification", {"text": "One winner"},
    )
    Database(path).set_state(key, raw)
    barrier = threading.Barrier(2)
    outcomes = []

    def save_source():
        local = Database(path)
        barrier.wait()
        outcomes.append(local.add_content_source_consuming_state_atomic(
            state_key=key,
            expected_state_value=raw,
            source_type="founder_note",
            text="One winner",
            trust_state="verified",
            verified_by="floriano",
        )[1])

    threads = [threading.Thread(target=save_source) for _index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["created", "session_conflict"]
    assert len(Database(path).get_eligible_sources("founder_note")) == 1
    assert Database(path).get_state(key) is None


def test_terminal_source_hard_crash_rolls_back_then_replays_once(tmp_path):
    path = str(tmp_path / "source-hard-crash.db")
    key = "telegram_session:42"
    raw = _session_json(
        "source_intake", "classification", {"text": "Crash safe insight"},
    )
    Database(path).set_state(key, raw)
    script = """
import os
import sys
from modules.database import Database

db = Database(sys.argv[1])
original = db._insert_content_source_in_conn
def insert_then_crash(conn, **values):
    original(conn, **values)
    os._exit(91)
db._insert_content_source_in_conn = insert_then_crash
db.add_content_source_consuming_state_atomic(
    state_key=sys.argv[2],
    expected_state_value=sys.argv[3],
    source_type='founder_note',
    text='Crash safe insight',
    trust_state='verified',
    verified_by='floriano',
)
"""

    crashed = subprocess.run(
        [sys.executable, "-c", script, path, key, raw],
        cwd=str(Path(__file__).resolve().parents[1]),
        check=False,
    )

    assert crashed.returncode == 91
    recovered = Database(path)
    assert recovered.get_state(key) == raw
    assert recovered.get_eligible_sources("founder_note") == []
    source_id, outcome = recovered.add_content_source_consuming_state_atomic(
        state_key=key,
        expected_state_value=raw,
        source_type="founder_note",
        text="Crash safe insight",
        trust_state="verified",
        verified_by="floriano",
    )
    assert type(source_id) is int
    assert outcome == "created"
    assert recovered.get_state(key) is None
    assert len(recovered.get_eligible_sources("founder_note")) == 1


def test_applied_commit_ambiguity_keeps_one_effect_and_consumed_session(tmp_path):
    class CommitAmbiguityDatabase(Database):
        _raise_after_commit = False

        @contextmanager
        def _conn(self):
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
                if self._raise_after_commit:
                    self._raise_after_commit = False
                    raise sqlite3.OperationalError("injected commit ambiguity")
            finally:
                conn.close()

        def _insert_content_source_in_conn(self, conn, **values):
            source_id = super()._insert_content_source_in_conn(conn, **values)
            self._raise_after_commit = True
            return source_id

    path = str(tmp_path / "source-ambiguous-commit.db")
    db = CommitAmbiguityDatabase(path)
    key = "telegram_session:42"
    raw = _session_json(
        "source_intake", "classification", {"text": "One committed insight"},
    )
    db.set_state(key, raw)
    telegram = WorkflowTelegramApi(tmp_path)
    update = callback_update(109, "input:source:founder_note")

    assert workflow_controller(db, telegram).process_update(update) == "failed"

    recovered = Database(path)
    assert recovered.get_state(key) is None
    assert len(recovered.get_eligible_sources("founder_note")) == 1
    assert workflow_controller(recovered, telegram).process_update(update) == "duplicate"
    assert workflow_controller(recovered, telegram).process_update(
        callback_update(1090, "input:source:founder_note")
    ) == "processed"
    assert len(Database(path).get_eligible_sources("founder_note")) == 1


def test_edit_atomic_retry_returns_same_replacement_without_second_effect(tmp_path):
    db = Database(str(tmp_path / "edit-atomic-retry.db"))
    source_id, draft_id = add_pending_draft(db)
    prior = db.get_post_draft(draft_id)
    key = "telegram_session:42"
    raw = _session_json("draft_edit", "text", {"draft_id": draft_id})
    db.set_state(key, raw)
    values = {
        "state_key": key,
        "expected_state_value": raw,
        "prior_draft_id": draft_id,
        "expected_revision": prior["revision"],
        "expected_slot": prior["intended_slot"],
        "expected_category": prior["category"],
        "expected_source_ids": [source_id],
        "text": "Idempotent edited copy",
        "score_data": {"total": 90},
        "publication_key": "telegram-edit:atomic-session-token",
    }

    created, first_outcome = db.replace_post_draft_consuming_state_atomic(**values)
    replayed, replay_outcome = db.replace_post_draft_consuming_state_atomic(**values)

    assert first_outcome == "created"
    assert replay_outcome == "already_applied"
    assert replayed["id"] == created["id"]
    assert len(db.list_post_drafts()) == 2
    assert db.get_post_draft(draft_id)["status"] == "superseded"


def test_edit_atomic_retry_with_different_copy_is_session_conflict(tmp_path):
    db = Database(str(tmp_path / "edit-atomic-different-retry.db"))
    source_id, draft_id = add_pending_draft(db)
    prior = db.get_post_draft(draft_id)
    key = "telegram_session:42"
    raw = _session_json("draft_edit", "text", {"draft_id": draft_id})
    db.set_state(key, raw)
    values = {
        "state_key": key,
        "expected_state_value": raw,
        "prior_draft_id": draft_id,
        "expected_revision": prior["revision"],
        "expected_slot": prior["intended_slot"],
        "expected_category": prior["category"],
        "expected_source_ids": [source_id],
        "text": "Winning edited copy",
        "score_data": {"clarity": 18, "total": 90},
        "publication_key": "telegram-edit:atomic-session-token",
    }
    created, first_outcome = db.replace_post_draft_consuming_state_atomic(**values)

    replayed, replay_outcome = db.replace_post_draft_consuming_state_atomic(
        **{
            **values,
            "text": "Different losing copy",
            "score_data": {"clarity": 19, "total": 91},
        }
    )

    assert first_outcome == "created"
    assert type(created["id"]) is int
    assert replayed is None
    assert replay_outcome == "session_conflict"
    assert len(db.list_post_drafts()) == 2


def test_news_operational_error_keeps_input_for_restart_after_same_update_duplicate(
    tmp_path,
):
    class OperationalSourceDatabase(Database):
        def _insert_content_source_in_conn(self, conn, **values):
            del values
            conn.execute("SELECT * FROM injected_missing_table")

    path = str(tmp_path / "news-restart.db")
    failing = OperationalSourceDatabase(path)
    key = "telegram_session:42"
    raw = _session_json(
        "source_intake",
        "news_source",
        {
            "text": "Restart-safe report",
            "url": "https://news.example/restart-report",
            "published_at": "2029-08-14",
        },
    )
    failing.set_state(key, raw)
    telegram = WorkflowTelegramApi(tmp_path)
    update = message_update(110, "News Example")

    assert workflow_controller(failing, telegram).process_update(update) == "failed"
    assert Database(path).get_state(key) == raw
    assert workflow_controller(Database(path), telegram).process_update(update) == (
        "duplicate"
    )
    assert Database(path).get_state(key) == raw

    restarted = workflow_controller(Database(path), telegram)
    assert restarted.process_update(message_update(111, "News Example")) == "processed"
    assert Database(path).get_state(key) is None
    sources = Database(path).get_eligible_sources("verified_news")
    assert len(sources) == 1
    assert sources[0]["url"] == "https://news.example/restart-report"


def test_non_news_callback_failure_keeps_exact_session_and_no_source(tmp_path):
    db = Database(str(tmp_path / "non-news-controller.db"))
    key = "telegram_session:42"
    raw = _session_json(
        "source_intake", "classification", {"text": "Founder insight"},
    )
    db.set_state(key, raw)
    with db._conn() as conn:
        conn.execute("""
            CREATE TRIGGER fail_controller_source
            BEFORE INSERT ON content_sources
            BEGIN SELECT RAISE(ABORT, 'injected source failure'); END
        """)
    telegram = WorkflowTelegramApi(tmp_path)

    assert workflow_controller(db, telegram).process_update(
        callback_update(112, "input:source:founder_note")
    ) == "failed"

    assert Database(db.db_path).get_state(key) == raw
    assert Database(db.db_path).get_eligible_sources("founder_note") == []


def test_postpone_failure_keeps_exact_session_and_draft_snapshot(tmp_path):
    db = Database(str(tmp_path / "postpone-controller.db"))
    _source_id, draft_id = add_pending_draft(db)
    prior = db.get_post_draft(draft_id)
    key = "telegram_session:42"
    raw = _session_json("draft_postpone", "slot", {"draft_id": draft_id})
    db.set_state(key, raw)
    with db._conn() as conn:
        conn.execute("""
            CREATE TRIGGER fail_controller_postpone
            BEFORE UPDATE OF intended_slot ON post_drafts
            BEGIN SELECT RAISE(ABORT, 'injected postpone failure'); END
        """)
    pipeline = DraftPipeline(
        db, NeverPlanner(), CopyGenerator(), Guard(), Scorer(),
        now_fn=lambda: datetime(2029, 8, 15, tzinfo=timezone.utc),
    )
    telegram = WorkflowTelegramApi(tmp_path)

    assert workflow_controller(db, telegram, pipeline=pipeline).process_update(
        message_update(113, "2030-08-17T12:00:00+00:00")
    ) == "failed"

    recovered = Database(db.db_path)
    assert recovered.get_state(key) == raw
    current = recovered.get_post_draft(draft_id)
    assert current["intended_slot"] == prior["intended_slot"]
    assert current["revision"] == prior["revision"]


def test_edit_failure_keeps_exact_session_and_draft_snapshot(tmp_path):
    db = Database(str(tmp_path / "edit-controller.db"))
    _source_id, draft_id = add_pending_draft(db)
    key = "telegram_session:42"
    raw = _session_json("draft_edit", "text", {"draft_id": draft_id})
    db.set_state(key, raw)
    with db._conn() as conn:
        conn.execute("""
            CREATE TRIGGER fail_controller_edit
            BEFORE INSERT ON post_drafts
            BEGIN SELECT RAISE(ABORT, 'injected edit failure'); END
        """)
    pipeline = DraftPipeline(
        db, NeverPlanner(), CopyGenerator(), Guard(), Scorer(),
        now_fn=lambda: datetime(2029, 8, 15, tzinfo=timezone.utc),
    )
    telegram = WorkflowTelegramApi(tmp_path)

    assert workflow_controller(db, telegram, pipeline=pipeline).process_update(
        message_update(114, "A grounded edited insight")
    ) == "failed"

    recovered = Database(db.db_path)
    assert recovered.get_state(key) == raw
    assert recovered.get_post_draft(draft_id)["status"] == "pending_approval"
    assert len(recovered.list_post_drafts()) == 1


def test_edit_rewrites_then_runs_fact_score_and_novelty_gates(tmp_path):
    db = Database(str(tmp_path / "edit.db"))
    _source_id, draft_id = add_pending_draft(db)
    rewritten = "Studios can offer flexible access without losing their identity."
    generator = CopyGenerator(rewritten)
    guard = Guard()
    scorer = Scorer(75)
    pipeline = DraftPipeline(
        db,
        NeverPlanner(),
        generator,
        guard,
        scorer,
        now_fn=lambda: datetime(2029, 8, 15, tzinfo=timezone.utc),
    )

    replacement = pipeline.edit(draft_id, "x" * 281)

    assert replacement["text"] == rewritten
    assert replacement["status"] == "pending_approval"
    assert db.get_post_draft(draft_id)["status"] == "superseded"
    assert generator.rewrite_calls[0][2] == 280
    assert guard.calls[0][0] == rewritten
    assert scorer.calls == [rewritten]


def test_edit_rejects_copy_that_fails_a_pipeline_gate(tmp_path):
    db = Database(str(tmp_path / "edit-rejected.db"))
    _source_id, draft_id = add_pending_draft(db)
    pipeline = DraftPipeline(
        db,
        NeverPlanner(),
        CopyGenerator(),
        Guard(approved=False),
        Scorer(100),
        now_fn=lambda: datetime(2029, 8, 15, tzinfo=timezone.utc),
    )

    assert pipeline.edit(draft_id, "A factual-looking but unsupported edit") is None
    assert db.get_post_draft(draft_id)["status"] == "pending_approval"


def test_edit_novelty_window_uses_pipeline_clock(tmp_path):
    db = Database(str(tmp_path / "edit-clock.db"))
    source_id, draft_id = add_pending_draft(db)
    prior_text = "A grounded idea that was last used more than thirty days ago."
    prior_id = db.create_post_draft(
        prior_text,
        "proof",
        [source_id],
        {"total": 90},
        "2030-08-16T12:00:00+00:00",
        "old-logical-draft",
    )
    with db._conn() as conn:
        conn.execute(
            "UPDATE post_drafts SET created_at = ? WHERE id = ?",
            ("2029-07-14T00:00:00+00:00", prior_id),
        )
    pipeline = DraftPipeline(
        db,
        NeverPlanner(),
        CopyGenerator(),
        Guard(),
        Scorer(90),
        now_fn=lambda: datetime(2029, 8, 15, tzinfo=timezone.utc),
    )

    replacement = pipeline.edit(draft_id, prior_text)

    assert replacement is not None
    assert replacement["text"] == prior_text


def test_recent_content_window_replays_deterministically_with_explicit_clock(
    tmp_path,
):
    db = Database(str(tmp_path / "novelty-replay.db"))
    source_id = db.add_content_source(
        "founder_note", "Grounded source", verified_by="floriano",
    )
    outside_id = db.create_post_draft(
        "outside window", "proof", [source_id], {"total": 90},
        "2030-08-16T12:00:00+00:00", "novelty-outside",
    )
    inside_id = db.create_post_draft(
        "inside window", "proof", [source_id], {"total": 90},
        "2030-08-17T12:00:00+00:00", "novelty-inside",
    )
    future_id = db.create_post_draft(
        "future relative to replay", "proof", [source_id], {"total": 90},
        "2030-08-18T12:00:00+00:00", "novelty-future",
    )
    with db._conn() as conn:
        conn.executemany(
            "UPDATE post_drafts SET created_at = ? WHERE id = ?",
            [
                ("2029-07-14T00:00:00+00:00", outside_id),
                ("2029-07-17T00:00:00+00:00", inside_id),
                ("2029-08-16T00:00:00+00:00", future_id),
            ],
        )
    replay_clock = datetime(2029, 8, 15, tzinfo=timezone.utc)

    first = db.get_recent_content_texts(days=30, now=replay_clock)
    second = db.get_recent_content_texts(days=30, now=replay_clock)

    assert first == second == ["inside window"]


def test_late_approval_expires_exact_pending_revision(tmp_path):
    db = Database(str(tmp_path / "late.db"))
    _source_id, draft_id = add_pending_draft(
        db,
        slot="2026-08-10T12:00:00+00:00",
    )
    pipeline = DraftPipeline(
        db,
        NeverPlanner(),
        CopyGenerator(),
        Guard(),
        Scorer(),
        now_fn=lambda: datetime(2026, 8, 10, 12, 0, 1, tzinfo=timezone.utc),
    )

    assert pipeline.approve(draft_id, "floriano") is False
    assert db.get_post_draft(draft_id)["status"] == "expired"


def test_late_approval_callback_only_expires_and_offers_reschedule(tmp_path):
    db = Database(str(tmp_path / "late-callback.db"))
    _source_id, draft_id = add_pending_draft(
        db,
        slot="2026-08-10T12:00:00+00:00",
    )
    pipeline = DraftPipeline(
        db,
        NeverPlanner(),
        CopyGenerator(),
        Guard(),
        Scorer(),
        now_fn=lambda: datetime(2026, 8, 10, 12, 0, 1, tzinfo=timezone.utc),
    )
    pipeline.publish_calls = []
    telegram = WorkflowTelegramApi(tmp_path)
    controller = workflow_controller(db, telegram, pipeline=pipeline)

    assert controller.process_update(
        callback_update(19, f"draft:approve:{draft_id}")
    ) == "processed"

    assert db.get_post_draft(draft_id)["status"] == "expired"
    assert pipeline.publish_calls == []
    assert "riprogramma" in telegram.messages[-1][1].lower()
    buttons = telegram.messages[-1][2]["reply_markup"]["inline_keyboard"]
    assert buttons[0][0]["callback_data"] == f"draft:postpone:{draft_id}"


def test_pause_resume_status_and_help_are_persistent_and_concise(tmp_path):
    path = str(tmp_path / "commands.db")
    db = Database(path)
    telegram = WorkflowTelegramApi(tmp_path)
    controller = workflow_controller(
        db,
        telegram,
        scheduler_status=lambda: [
            {"name": "bozza 14:00", "next_run": "2030-08-15T10:00:00+00:00"}
        ],
    )

    assert controller.process_update(message_update(20, "/pause")) == "processed"
    assert db.get_state("paused") == "true"
    restarted = workflow_controller(Database(path), telegram)
    assert restarted.process_update(message_update(21, "/status")) == "processed"
    assert "dry-run: attivo" in telegram.messages[-1][1].lower()
    assert "pausa: attiva" in telegram.messages[-1][1].lower()
    assert controller.process_update(message_update(22, "/resume")) == "processed"
    assert db.get_state("paused") == "false"
    assert controller.process_update(message_update(23, "/help")) == "processed"
    help_text = telegram.messages[-1][1]
    for command in (
        "/status", "/posts", "/growth", "/stats", "/ideas",
        "/pause", "/resume", "/errors", "/help",
    ):
        assert command in help_text
    assert all(len(message[1]) <= 4096 for message in telegram.messages)


def test_status_reports_missing_or_noncanonical_pause_state_as_active(tmp_path):
    db = Database(str(tmp_path / "fail-closed-status.db"))
    telegram = WorkflowTelegramApi(tmp_path)
    controller = workflow_controller(db, telegram)

    assert controller.process_update(message_update(24, "/status")) == "processed"
    assert "pausa: attiva" in telegram.messages[-1][1].lower()

    db.set_state("paused", "FALSE")
    assert controller.process_update(message_update(25, "/status")) == "processed"
    assert "pausa: attiva" in telegram.messages[-1][1].lower()

    db.set_state("paused", "false")
    assert controller.process_update(message_update(26, "/status")) == "processed"
    assert "pausa: disattiva" in telegram.messages[-1][1].lower()


def test_posts_renders_complete_safe_draft_card_and_latest_published(tmp_path):
    db = Database(str(tmp_path / "posts.db"))
    source_id, draft_id = add_pending_draft(
        db,
        text="Complete <draft> & copy",
    )
    media_id = db.add_media(
        "studio.jpg", "/private/studio.jpg", "image",
        ai_description="Real <studio>", ai_tags="pilates,rome",
    )
    assert db.transition_post_draft(
        draft_id, ["pending_approval"], "pending_approval", media_id=media_id,
    )
    published_id = db.create_post_draft(
        "Already published", "proof", [source_id], {"total": 91},
        "2030-08-16T12:00:00+00:00", "telegram-published",
    )
    assert db.transition_post_draft(published_id, ["pending_approval"], "published")
    telegram = WorkflowTelegramApi(tmp_path)
    controller = workflow_controller(db, telegram, pipeline=StubPipeline(db))

    assert controller.process_update(message_update(30, "/posts")) == "processed"

    rendered = "\n".join(item[1] for item in telegram.messages)
    assert "Complete <draft> & copy" in rendered
    assert "Already published" in rendered
    assert "founder_story" in rendered
    assert FUTURE_SLOT in rendered
    assert "total: 88" in rendered
    assert "founder_note" in rendered
    assert "studio.jpg" in rendered
    card_kwargs = next(item[2] for item in telegram.messages if "Complete <draft>" in item[1])
    assert card_kwargs["parse_mode"] is None
    callback_data = [
        button["callback_data"]
        for row in card_kwargs["reply_markup"]["inline_keyboard"]
        for button in row
    ]
    assert set(callback_data) == {
        f"draft:approve:{draft_id}",
        f"draft:regen:{draft_id}",
        f"draft:edit:{draft_id}",
        f"draft:media:{draft_id}",
        f"draft:textonly:{draft_id}",
        f"draft:postpone:{draft_id}",
        f"draft:discard:{draft_id}",
    }


def test_posts_sends_verified_media_stream_before_separate_full_draft_card(tmp_path):
    db = Database(str(tmp_path / "posts-preview.db"))
    _source_id, draft_id = add_pending_draft(
        db,
        text="Complete preview draft <literal> & safe",
    )
    attach_verified_preview(db, tmp_path, draft_id)
    telegram = WorkflowTelegramApi(tmp_path)
    controller = workflow_controller(db, telegram, pipeline=StubPipeline(db))

    assert controller.process_update(message_update(301, "/posts")) == "processed"

    assert telegram.media_messages == [(
        "42",
        _PREVIEW_JPEG,
        "photo",
        {"caption": f"Anteprima media bozza #{draft_id}"},
    )]
    media_event_index = next(
        index for index, event in enumerate(telegram.events)
        if event[0] == "media"
    )
    card_event_index = next(
        index for index, event in enumerate(telegram.events)
        if event[0] == "message" and "Complete preview draft" in event[2]
    )
    assert media_event_index < card_event_index
    card = telegram.events[card_event_index]
    assert "Complete preview draft <literal> & safe" in card[2]
    assert card[3]["reply_markup"]["inline_keyboard"]


def test_posts_dispatches_verified_video_and_document_previews(tmp_path):
    for update_id, filename, mime_type, content, telegram_type in (
        (305, "video-preview.mp4", "video/mp4", _PREVIEW_MP4, "video"),
        (
            306,
            _PREVIEW_DOCUMENT_FILENAME,
            "image/jpeg",
            _PREVIEW_JPEG,
            "document",
        ),
    ):
        case_root = tmp_path / telegram_type
        case_root.mkdir()
        db = Database(str(case_root / "posts-preview.db"))
        _source_id, draft_id = add_pending_draft(
            db,
            text=f"Complete {telegram_type} preview post",
        )
        attach_verified_preview(
            db,
            case_root,
            draft_id,
            content=content,
            filename=filename,
            mime_type=mime_type,
        )
        telegram = WorkflowTelegramApi(case_root)
        controller = workflow_controller(db, telegram, pipeline=StubPipeline(db))

        assert controller.process_update(
            message_update(update_id, "/posts")
        ) == "processed"

        assert len(telegram.media_messages) == 1
        assert telegram.media_messages[0][1:3] == (content, telegram_type)


def test_posts_media_type_mime_mismatch_fails_closed(tmp_path):
    db = Database(str(tmp_path / "posts-mismatch-preview.db"))
    _source_id, draft_id = add_pending_draft(
        db,
        text="Complete post despite mismatched preview metadata",
    )
    record = attach_verified_preview(db, tmp_path, draft_id)
    with db._conn() as conn:
        conn.execute(
            "UPDATE media_library SET media_type = 'video' WHERE id = ?",
            (record["id"],),
        )
    telegram = WorkflowTelegramApi(tmp_path)
    controller = workflow_controller(db, telegram, pipeline=StubPipeline(db))

    assert controller.process_update(message_update(308, "/posts")) == "processed"

    assert telegram.media_messages == []
    assert any(
        "Complete post despite mismatched preview metadata" in message[1]
        for message in telegram.messages
    )


def test_published_preview_requires_exact_tweet_media_binding(tmp_path):
    db = Database(str(tmp_path / "posts-published-binding.db"))
    _source_id, draft_id = add_pending_draft(
        db,
        text="Published post with mismatched used media",
    )
    record = attach_verified_preview(db, tmp_path, draft_id)
    with db._conn() as conn:
        conn.execute(
            "UPDATE post_drafts SET status = 'published', "
            "published_tweet_id = 'tweet-a' WHERE id = ?",
            (draft_id,),
        )
        conn.execute(
            "UPDATE media_library SET lifecycle_state = 'used', used = 1, "
            "reserved_by_draft_id = NULL, used_in_tweet_id = 'tweet-other' "
            "WHERE id = ?",
            (record["id"],),
        )
    telegram = WorkflowTelegramApi(tmp_path)
    controller = workflow_controller(db, telegram, pipeline=StubPipeline(db))

    assert controller.process_update(message_update(309, "/posts")) == "processed"

    assert telegram.media_messages == []
    assert any(
        "Published post with mismatched used media" in message[1]
        for message in telegram.messages
    )


def test_preview_revalidates_draft_media_binding_under_open_media_lease(tmp_path):
    class DetachAfterMediaReadDatabase(Database):
        detach_draft_id = None

        def get_media_by_id(self, media_id):
            record = super().get_media_by_id(media_id)
            if self.detach_draft_id is not None:
                draft_id = self.detach_draft_id
                self.detach_draft_id = None
                assert self.detach_media_from_draft(draft_id)
            return record

    path = str(tmp_path / "preview-binding-race.db")
    db = DetachAfterMediaReadDatabase(path)
    _source_id, draft_id = add_pending_draft(
        db,
        text="Post whose preview binding changed",
    )
    attach_verified_preview(db, tmp_path, draft_id)
    db.detach_draft_id = draft_id
    telegram = WorkflowTelegramApi(tmp_path)
    controller = workflow_controller(db, telegram, pipeline=StubPipeline(db))

    assert controller.process_update(message_update(310, "/posts")) == "processed"

    assert telegram.media_messages == []
    assert Database(path).get_post_draft(draft_id)["media_id"] is None
    assert any(
        "Post whose preview binding changed" in message[1]
        for message in telegram.messages
    )


def test_preview_send_linearizes_before_same_draft_transition_and_frees_sqlite(
    tmp_path,
    monkeypatch,
):
    from modules import database as database_module
    from modules.media_store import media_store_lock as real_media_store_lock

    class BlockingPreviewTelegramApi(WorkflowTelegramApi):
        def __init__(self, media_library_dir):
            super().__init__(media_library_dir)
            self.send_started = threading.Event()
            self.allow_send_return = threading.Event()

        def send_media(self, chat_id, media, media_type, **kwargs):
            self.events.append(("media_send_started",))
            self.send_started.set()
            if not self.allow_send_return.wait(timeout=2):
                raise AssertionError("preview send was never released")
            result = super().send_media(chat_id, media, media_type, **kwargs)
            self.events.append(("media_send_finished",))
            return result

    path = str(tmp_path / "preview-linearization.db")
    db = Database(path)
    _source_id, draft_id = add_pending_draft(
        db,
        text="Preview remains valid until its send returns",
    )
    attach_verified_preview(db, tmp_path, draft_id)
    telegram = BlockingPreviewTelegramApi(tmp_path)
    controller = workflow_controller(db, telegram, pipeline=StubPipeline(db))
    controller_result = []
    transition_result = []
    transition_started = threading.Event()
    transition_attempted_root = threading.Event()
    transition_finished = threading.Event()
    unrelated_writer_finished = threading.Event()

    @contextmanager
    def observed_media_store_lock(directory):
        transition_attempted_root.set()
        with real_media_store_lock(directory) as lease:
            yield lease

    monkeypatch.setattr(
        database_module,
        "media_store_lock",
        observed_media_store_lock,
    )

    def render_posts():
        controller_result.append(
            controller.process_update(message_update(311, "/posts"))
        )

    def discard_draft():
        transition_started.set()
        transition_result.append(db.transition_post_draft(
            draft_id,
            ["pending_approval"],
            "discarded",
            error="user_discarded",
        ))
        telegram.events.append(("draft_transition_finished",))
        transition_finished.set()

    def write_unrelated_state():
        Database(path).set_state("unrelated_writer", "complete")
        unrelated_writer_finished.set()

    controller_thread = threading.Thread(target=render_posts)
    transition_thread = threading.Thread(target=discard_draft)
    writer_thread = threading.Thread(target=write_unrelated_state)
    try:
        controller_thread.start()
        assert telegram.send_started.wait(timeout=2)
        transition_thread.start()
        assert transition_started.wait(timeout=1)
        assert transition_attempted_root.wait(timeout=1)
        writer_thread.start()
        writer_completed_during_send = unrelated_writer_finished.wait(timeout=1)
        transition_completed_during_send = transition_finished.is_set()
    finally:
        telegram.allow_send_return.set()
        controller_thread.join(timeout=2)
        transition_thread.join(timeout=2)
        if writer_thread.ident is not None:
            writer_thread.join(timeout=2)

    assert writer_completed_during_send is True
    assert transition_completed_during_send is False
    assert not controller_thread.is_alive()
    assert not transition_thread.is_alive()
    assert not writer_thread.is_alive()
    assert controller_result == ["processed"]
    assert transition_result == [True]
    assert len(telegram.media_messages) == 1
    assert db.get_post_draft(draft_id)["status"] == "discarded"
    event_names = [event[0] for event in telegram.events]
    assert event_names.index("media_send_finished") < event_names.index(
        "draft_transition_finished"
    )


@pytest.mark.parametrize(
    "mutation_name",
    [
        "transition",
        "approve",
        "postpone",
        "postpone_session",
        "claim",
        "restore_claim",
        "mark_unknown",
        "replace",
        "replace_session",
        "detach",
        "finalize",
        "fail_publication",
    ],
)
def test_existing_draft_mutations_wait_for_bound_media_root(
    tmp_path,
    monkeypatch,
    mutation_name,
):
    from modules import database as database_module
    from modules.media_store import media_store_lock as real_media_store_lock

    case_root = tmp_path / mutation_name
    case_root.mkdir()
    db = Database(str(case_root / "binding.db"))
    source_id, draft_id = add_pending_draft(
        db,
        text=f"Bound mutation {mutation_name}",
    )
    record = attach_verified_preview(db, case_root, draft_id)
    draft = db.get_post_draft(draft_id)
    state_key = "telegram_session:42"
    state_value = _session_json(
        "draft_postpone" if mutation_name == "postpone_session" else "draft_edit",
        "slot" if mutation_name == "postpone_session" else "text",
        {"draft_id": draft_id},
    )
    claim = None
    if mutation_name in {"claim", "restore_claim", "mark_unknown", "finalize", "fail_publication"}:
        with db._conn() as conn:
            conn.execute(
                "UPDATE post_drafts SET status = 'approved' WHERE id = ?",
                (draft_id,),
            )
        draft = db.get_post_draft(draft_id)
    if mutation_name in {"restore_claim", "mark_unknown", "finalize", "fail_publication"}:
        _claimed_draft, claim = db.claim_post_draft_for_publication(
            draft_id, draft["revision"],
        )
        draft = db.get_post_draft(draft_id)
    if mutation_name in {"postpone_session", "replace_session"}:
        db.set_state(state_key, state_value)

    def mutate():
        current = db.get_post_draft(draft_id)
        if mutation_name == "transition":
            return db.transition_post_draft(
                draft_id, ["pending_approval"], "discarded",
            )
        if mutation_name == "approve":
            return db.approve_post_draft_atomic(
                draft_id,
                current["revision"],
                current["intended_slot"],
                "floriano",
                lambda: datetime(2029, 8, 15, tzinfo=timezone.utc),
            )
        if mutation_name == "postpone":
            return db.postpone_post_draft_atomic(
                draft_id,
                current["revision"],
                ["pending_approval"],
                "2030-08-17T12:00:00+00:00",
            )
        if mutation_name == "postpone_session":
            return db.postpone_post_draft_consuming_state_atomic(
                state_key=state_key,
                expected_state_value=state_value,
                draft_id=draft_id,
                expected_revision=current["revision"],
                expected_statuses=["pending_approval"],
                new_slot="2030-08-17T12:00:00+00:00",
            )
        if mutation_name == "claim":
            return db.claim_post_draft_for_publication(
                draft_id, current["revision"],
            )
        if mutation_name == "restore_claim":
            return db.restore_post_draft_publication_claim(claim)
        if mutation_name == "mark_unknown":
            return db.mark_post_draft_publication_unknown(claim, "TimeoutError")
        if mutation_name in {"replace", "replace_session"}:
            values = {
                "prior_draft_id": draft_id,
                "expected_revision": current["revision"],
                "expected_slot": current["intended_slot"],
                "expected_category": current["category"],
                "expected_source_ids": current["source_ids"],
                "text": "Replacement copy",
                "score_data": {"total": 90},
                "publication_key": f"round2:{mutation_name}",
            }
            if mutation_name == "replace_session":
                return db.replace_post_draft_consuming_state_atomic(
                    state_key=state_key,
                    expected_state_value=state_value,
                    **values,
                )
            return db.replace_post_draft_atomic(**values)
        if mutation_name == "detach":
            return db.detach_media_from_draft(draft_id)
        if mutation_name == "finalize":
            return db.finalize_post_draft_publication(
                claim, "tweet-round2", db.get_media_by_id(record["id"]),
            )
        if mutation_name == "fail_publication":
            return db.fail_post_draft_publication(claim, "RuntimeError")
        raise AssertionError("unhandled mutation")

    root_attempted = threading.Event()
    mutation_finished = threading.Event()
    mutation_errors = []

    @contextmanager
    def observed_media_store_lock(directory):
        if Path(directory).resolve() == Path(record["filepath"]).parent.resolve():
            root_attempted.set()
        with real_media_store_lock(directory) as lease:
            yield lease

    monkeypatch.setattr(
        database_module,
        "media_store_lock",
        observed_media_store_lock,
    )

    def run_mutation():
        try:
            mutate()
        except BaseException as error:
            mutation_errors.append(error)
        finally:
            mutation_finished.set()

    thread = threading.Thread(target=run_mutation)
    with media_store_lock(Path(record["filepath"]).parent):
        thread.start()
        attempted_bound_root = root_attempted.wait(timeout=1)
        finished_while_root_held = mutation_finished.is_set()
    thread.join(timeout=2)

    assert attempted_bound_root is True
    assert finished_while_root_held is False
    assert not thread.is_alive()
    assert mutation_errors == []


def test_transition_that_wins_root_first_invalidates_preview_without_deadlock(
    tmp_path,
    monkeypatch,
):
    from modules import database as database_module
    from modules import media_store as media_store_module
    from modules.media_store import media_store_lock as real_media_store_lock

    path = str(tmp_path / "transition-wins-root.db")
    db = Database(path)
    _source_id, draft_id = add_pending_draft(
        db,
        text="Transition wins the bound root",
    )
    record = attach_verified_preview(db, tmp_path, draft_id)
    telegram = WorkflowTelegramApi(tmp_path)
    controller = workflow_controller(db, telegram, pipeline=StubPipeline(db))
    mutation_has_root = threading.Event()
    preview_attempted_root = threading.Event()
    allow_mutation_db = threading.Event()
    mutation_finished = threading.Event()
    controller_result = []
    mutation_result = []
    mutation_thread_id = []
    controller_thread_id = []

    @contextmanager
    def controlled_media_store_lock(directory):
        caller = threading.get_ident()
        if controller_thread_id and caller == controller_thread_id[0]:
            preview_attempted_root.set()
        with real_media_store_lock(directory) as lease:
            if mutation_thread_id and caller == mutation_thread_id[0]:
                mutation_has_root.set()
                if not allow_mutation_db.wait(timeout=2):
                    raise AssertionError("mutation DB phase was never released")
            yield lease

    monkeypatch.setattr(
        database_module,
        "media_store_lock",
        controlled_media_store_lock,
    )
    monkeypatch.setattr(
        media_store_module,
        "media_store_lock",
        controlled_media_store_lock,
    )

    def mutate():
        mutation_thread_id.append(threading.get_ident())
        mutation_result.append(db.transition_post_draft(
            draft_id, ["pending_approval"], "discarded",
        ))
        mutation_finished.set()

    def render_posts():
        controller_thread_id.append(threading.get_ident())
        controller_result.append(
            controller.process_update(message_update(312, "/posts"))
        )

    mutation_thread = threading.Thread(target=mutate)
    controller_thread = threading.Thread(target=render_posts)
    mutation_thread.start()
    acquired_before_db = mutation_has_root.wait(timeout=1)
    if acquired_before_db:
        controller_thread.start()
        preview_waited_for_root = preview_attempted_root.wait(timeout=1)
    else:
        preview_waited_for_root = False
    allow_mutation_db.set()
    mutation_thread.join(timeout=2)
    if controller_thread.ident is not None:
        controller_thread.join(timeout=2)

    assert acquired_before_db is True
    assert preview_waited_for_root is True
    assert not mutation_thread.is_alive()
    assert not controller_thread.is_alive()
    assert mutation_result == [True]
    assert controller_result == ["processed"]
    assert mutation_finished.is_set()
    assert db.get_post_draft(draft_id)["status"] == "discarded"
    assert telegram.media_messages == []


def test_waiting_on_bound_root_does_not_hold_sqlite_or_block_unrelated_root(
    tmp_path,
    monkeypatch,
):
    from modules import database as database_module
    from modules.media_store import media_store_lock as real_media_store_lock

    path = str(tmp_path / "unrelated-roots.db")
    db = Database(path)
    _source_a, draft_a = add_pending_draft(db, text="Root A draft")
    _source_b, draft_b = add_pending_draft(
        db,
        slot="2030-08-16T12:00:00+00:00",
        text="Root B draft",
    )
    media_a = attach_verified_preview(db, tmp_path / "a", draft_a)
    media_b = attach_verified_preview(db, tmp_path / "b", draft_b)
    root_a = Path(media_a["filepath"]).parent.resolve()
    root_b = Path(media_b["filepath"]).parent.resolve()
    attempted_a = threading.Event()
    attempted_b = threading.Event()
    finished_a = threading.Event()
    finished_b = threading.Event()
    results = {}

    @contextmanager
    def observed_media_store_lock(directory):
        resolved = Path(directory).resolve()
        if resolved == root_a:
            attempted_a.set()
        if resolved == root_b:
            attempted_b.set()
        with real_media_store_lock(directory) as lease:
            yield lease

    monkeypatch.setattr(
        database_module,
        "media_store_lock",
        observed_media_store_lock,
    )

    def transition_a():
        results["a"] = db.transition_post_draft(
            draft_a, ["pending_approval"], "discarded",
        )
        finished_a.set()

    def transition_b():
        results["b"] = Database(path).transition_post_draft(
            draft_b, ["pending_approval"], "approved",
        )
        finished_b.set()

    thread_a = threading.Thread(target=transition_a)
    thread_b = threading.Thread(target=transition_b)
    with real_media_store_lock(root_a):
        thread_a.start()
        assert attempted_a.wait(timeout=1)
        assert not finished_a.is_set()
        thread_b.start()
        b_attempted_own_root = attempted_b.wait(timeout=1)
        b_finished_while_a_waited = finished_b.wait(timeout=1)
    thread_a.join(timeout=2)
    thread_b.join(timeout=2)

    assert b_attempted_own_root is True
    assert b_finished_while_a_waited is True
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert results == {"a": True, "b": True}
    assert Database(path).get_post_draft(draft_a)["status"] == "discarded"
    assert Database(path).get_post_draft(draft_b)["status"] == "approved"


def test_draft_binding_retargets_and_retries_roots_in_sorted_order(
    tmp_path,
    monkeypatch,
):
    from modules import database as database_module
    from modules.media_store import media_store_lock as real_media_store_lock

    path = str(tmp_path / "retarget.db")
    db = Database(path)
    _source_id, draft_id = add_pending_draft(db, text="Retargeted binding")
    record_a = attach_verified_preview(db, tmp_path / "a-root", draft_id)
    root_b = tmp_path / "z-root"
    root_b.mkdir(mode=0o700)
    staged_b = root_b / "staged.jpg"
    staged_b.write_bytes(_PREVIEW_JPEG)
    record_b = MediaProcessor(db).process_new_file(
        str(staged_b),
        "second.jpg",
        "image/jpeg",
        len(_PREVIEW_JPEG),
        "Second media",
    )
    root_a = Path(record_a["filepath"]).parent.resolve()
    resolved_b = Path(record_b["filepath"]).parent.resolve()
    assert str(root_a) < str(resolved_b)
    attempted_a = threading.Event()
    attempted_b = threading.Event()
    attempts = []
    finished = threading.Event()
    results = []
    errors = []

    @contextmanager
    def observed_media_store_lock(directory):
        resolved = Path(directory).resolve()
        attempts.append(resolved)
        if resolved == root_a:
            attempted_a.set()
        if resolved == resolved_b:
            attempted_b.set()
        with real_media_store_lock(directory) as lease:
            yield lease

    monkeypatch.setattr(
        database_module,
        "media_store_lock",
        observed_media_store_lock,
    )
    prior = db.get_post_draft(draft_id)

    def approve_after_discovery():
        try:
            results.append(db.approve_post_draft_atomic(
                draft_id,
                prior["revision"],
                prior["intended_slot"],
                "floriano",
                lambda: datetime(2029, 8, 15, tzinfo=timezone.utc),
            ))
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=approve_after_discovery)
    guard_a = real_media_store_lock(root_a)
    guard_b = real_media_store_lock(resolved_b)
    a_held = False
    b_held = False
    try:
        guard_a.__enter__()
        a_held = True
        thread.start()
        assert attempted_a.wait(timeout=1)
        with db._conn() as conn:
            conn.execute(
                "UPDATE post_drafts SET media_id = ? WHERE id = ?",
                (record_b["id"], draft_id),
            )
        guard_b.__enter__()
        b_held = True
        guard_a.__exit__(None, None, None)
        a_held = False
        retried_to_b = attempted_b.wait(timeout=1)
        finished_while_b_held = finished.is_set()
    finally:
        if a_held:
            guard_a.__exit__(None, None, None)
        if b_held:
            guard_b.__exit__(None, None, None)
        thread.join(timeout=2)

    assert retried_to_b is True
    assert finished_while_b_held is False
    assert not thread.is_alive()
    assert errors == []
    assert results == [True]
    assert attempts[-2:] == [root_a, resolved_b]
    current = db.get_post_draft(draft_id)
    assert current["status"] == "approved"
    assert current["media_id"] == record_b["id"]


def test_draft_binding_lock_closes_each_discovered_root_descriptor(
    tmp_path,
    monkeypatch,
):
    from modules import database as database_module
    from modules.media_store import media_store_lock as real_media_store_lock

    db = Database(str(tmp_path / "closed-root-fd.db"))
    _source_id, draft_id = add_pending_draft(db, text="Close root descriptor")
    attach_verified_preview(db, tmp_path, draft_id)
    closed_descriptors = []

    @contextmanager
    def checked_media_store_lock(directory):
        root_fd = None
        with real_media_store_lock(directory) as lease:
            root_fd = lease[1]
            yield lease
        with pytest.raises(OSError):
            os.fstat(root_fd)
        closed_descriptors.append(root_fd)

    monkeypatch.setattr(
        database_module,
        "media_store_lock",
        checked_media_store_lock,
    )

    assert db.transition_post_draft(
        draft_id, ["pending_approval"], "discarded",
    )
    assert len(closed_descriptors) == 1


def test_draft_callback_sends_preview_then_card_then_answers_once(tmp_path):
    db = Database(str(tmp_path / "callback-preview.db"))
    _source_id, draft_id = add_pending_draft(
        db,
        text="Complete callback preview post",
    )
    attach_verified_preview(db, tmp_path, draft_id)
    telegram = WorkflowTelegramApi(tmp_path)
    controller = workflow_controller(db, telegram, pipeline=StubPipeline(db))

    assert controller.process_update(
        callback_update(307, f"draft:regen:{draft_id}")
    ) == "processed"

    preview_index = next(
        index for index, event in enumerate(telegram.events)
        if event[0] == "media"
    )
    card_index = next(
        index for index, event in enumerate(telegram.events)
        if event[0] == "message" and "Complete callback preview post" in event[2]
    )
    callback_indexes = [
        index for index, event in enumerate(telegram.events)
        if event[0] == "callback"
    ]
    assert preview_index < card_index < callback_indexes[0]
    assert callback_indexes == [len(telegram.events) - 1]


def test_posts_tampered_media_fails_closed_and_keeps_safe_full_card(tmp_path):
    db = Database(str(tmp_path / "posts-tampered-preview.db"))
    _source_id, draft_id = add_pending_draft(
        db,
        text="Keep this complete post after preview failure",
    )
    record = attach_verified_preview(db, tmp_path, draft_id)
    tampered = b"\xff\xd8\xff\xe0" + b"x" * (len(_PREVIEW_JPEG) - 4)
    assert len(tampered) == len(_PREVIEW_JPEG)
    Path(record["filepath"]).write_bytes(tampered)
    telegram = WorkflowTelegramApi(tmp_path)
    controller = workflow_controller(db, telegram, pipeline=StubPipeline(db))

    assert controller.process_update(message_update(302, "/posts")) == "processed"

    assert telegram.media_messages == []
    rendered = "\n".join(message[1] for message in telegram.messages)
    assert "Keep this complete post after preview failure" in rendered
    assert record["filepath"] not in rendered


def test_posts_missing_or_stale_media_fails_closed_without_raw_path(tmp_path):
    for update_id, failure in ((303, "missing"), (304, "stale")):
        case_root = tmp_path / failure
        case_root.mkdir()
        db = Database(str(case_root / "posts-preview.db"))
        _source_id, draft_id = add_pending_draft(
            db,
            text=f"Complete {failure} preview post",
        )
        record = attach_verified_preview(db, case_root, draft_id)
        media_path = Path(record["filepath"])
        if failure == "missing":
            media_path.unlink()
        else:
            original = media_path.read_bytes()
            media_path.rename(media_path.with_suffix(".old"))
            media_path.write_bytes(original)
        telegram = WorkflowTelegramApi(case_root)
        controller = workflow_controller(db, telegram, pipeline=StubPipeline(db))

        assert controller.process_update(
            message_update(update_id, "/posts")
        ) == "processed"

        assert telegram.media_messages == []
        rendered = "\n".join(message[1] for message in telegram.messages)
        assert f"Complete {failure} preview post" in rendered
        assert record["filepath"] not in rendered


def test_draft_card_preserves_complete_text_when_metadata_exceeds_message_limit(
    tmp_path,
):
    db = Database(str(tmp_path / "large-card.db"))
    source_ids = [
        db.add_content_source(
            "founder_note",
            f"Grounded source {index}",
            metadata={"title": f"Source {index} " + "x" * 100},
            verified_by="floriano",
        )
        for index in range(50)
    ]
    complete_text = "Final complete draft text: " + "z" * 250
    draft_id = db.create_post_draft(
        complete_text,
        "founder_story",
        source_ids,
        {"clarity": 20, "total": 90},
        FUTURE_SLOT,
        "telegram-large-card",
    )
    telegram = WorkflowTelegramApi(tmp_path)
    controller = workflow_controller(db, telegram, pipeline=StubPipeline(db))

    assert controller.process_update(message_update(31, "/posts")) == "processed"

    card = next(
        message for message in telegram.messages
        if message[2].get("reply_markup") is not None
    )
    assert complete_text in card[1]
    assert len(card[1]) <= 4096
    assert card[2]["reply_markup"]["inline_keyboard"]


def test_errors_uses_sanitized_rows_and_stats_uses_read_only_analytics(tmp_path):
    db = Database(str(tmp_path / "reads.db"))
    db.log_error("worker", "RuntimeError", "safe <detail> only")
    telegram = WorkflowTelegramApi(tmp_path)
    controller = workflow_controller(db, telegram, analytics=StubAnalytics())

    assert controller.process_update(message_update(40, "/errors")) == "processed"
    assert "safe <detail> only" in telegram.messages[-1][1]
    assert telegram.messages[-1][2]["parse_mode"] is None
    assert controller.process_update(message_update(41, "/stats")) == "processed"
    assert "followers_total: 120" in telegram.messages[-1][1]
    assert "correlation" in telegram.messages[-1][1]


def test_plain_text_source_flow_survives_restart_and_consumes_once(tmp_path):
    path = str(tmp_path / "source-session.db")
    db = Database(path)
    telegram = WorkflowTelegramApi(tmp_path)
    first = workflow_controller(db, telegram)

    assert first.process_update(message_update(50, "/ideas")) == "processed"
    assert first.process_update(
        message_update(51, "Founder learned: <keep this literal> & improve.")
    ) == "processed"
    selection = telegram.messages[-1][2]["reply_markup"]
    labels = [button["text"] for row in selection["inline_keyboard"] for button in row]
    assert labels == [
        "Founder note", "Product fact", "Evergreen idea", "Verified news",
    ]

    restarted = workflow_controller(Database(path), telegram)
    assert restarted.process_update(
        callback_update(52, "input:source:founder_note")
    ) == "processed"
    sources = Database(path).get_eligible_sources("founder_note")
    assert [source["text"] for source in sources] == [
        "Founder learned: <keep this literal> & improve."
    ]
    assert Database(path).get_state("telegram_session:42") is None
    assert restarted.process_update(
        callback_update(53, "input:source:founder_note")
    ) == "processed"
    assert len(Database(path).get_eligible_sources("founder_note")) == 1


def test_manual_news_collects_complete_allowlisted_metadata_across_restarts(tmp_path):
    path = str(tmp_path / "news-session.db")
    telegram = WorkflowTelegramApi(tmp_path)
    update_id = 60

    def run(update):
        nonlocal update_id
        controller = workflow_controller(Database(path), telegram)
        result = controller.process_update(update)
        update_id += 1
        return result

    assert run(message_update(update_id, "/ideas")) == "processed"
    assert run(message_update(update_id, "Studios report a measurable change.")) == "processed"
    assert run(callback_update(update_id, "input:source:verified_news")) == "processed"
    assert run(message_update(update_id, "https://reports.news.example/2029/change")) == "processed"
    assert run(message_update(update_id, "2029-08-14")) == "processed"
    assert run(message_update(update_id, "News Example")) == "processed"

    sources = Database(path).get_eligible_sources("verified_news")
    assert len(sources) == 1
    assert sources[0]["url"] == "https://reports.news.example/2029/change"
    assert sources[0]["trust_state"] == "verified"
    assert sources[0]["verified_by"] == "floriano"
    assert sources[0]["metadata"] == {
        "title": "Studios report a measurable change.",
        "summary": "Studios report a measurable change.",
        "published_at": "2029-08-14",
        "source_name": "News Example",
    }


def test_malformed_or_expired_session_fails_closed_without_saving_input(tmp_path):
    path = str(tmp_path / "bad-session.db")
    db = Database(path)
    telegram = WorkflowTelegramApi(tmp_path)
    key = "telegram_session:42"
    db.set_state(key, "{private malformed")
    controller = workflow_controller(db, telegram)

    assert controller.process_update(message_update(70, "must not be stored")) == "processed"
    assert db.get_state(key) is None
    assert db.get_eligible_sources() == []

    db.set_state(key, json.dumps({
        "version": 1,
        "token": "expired-token",
        "kind": "source_intake",
        "step": "text",
        "payload": {},
        "expires_at": "2020-01-01T00:00:00+00:00",
    }))
    assert controller.process_update(message_update(71, "also ignored")) == "processed"
    assert db.get_state(key) is None
    assert db.get_eligible_sources() == []


def test_semantically_tampered_news_session_fails_closed_after_restart(tmp_path):
    path = str(tmp_path / "tampered-session.db")
    db = Database(path)
    telegram = WorkflowTelegramApi(tmp_path)
    db.set_state("telegram_session:42", json.dumps({
        "version": 1,
        "token": "tampered-session-token",
        "kind": "source_intake",
        "step": "news_source",
        "payload": {
            "text": "Claim that must not become verified.",
            "url": "https://attacker.example/report",
            "published_at": "2029-08-14",
        },
        "expires_at": "2029-08-15T00:30:00+00:00",
    }, sort_keys=True, separators=(",", ":")))
    restarted = workflow_controller(
        Database(path),
        telegram,
        now=datetime(2029, 8, 15, tzinfo=timezone.utc),
    )

    assert restarted.process_update(message_update(72, "Attacker News")) == "processed"

    assert Database(path).get_state("telegram_session:42") is None
    assert Database(path).get_eligible_sources("verified_news") == []
    assert "sessione non valida" in telegram.messages[-1][1].lower()
    assert "priv" not in telegram.messages[-1][1].lower()

def test_draft_callbacks_delegate_only_to_pipeline_and_manual_media_matcher(tmp_path):
    db = Database(str(tmp_path / "draft-callbacks.db"))
    _source_id, draft_id = add_pending_draft(db)
    telegram = WorkflowTelegramApi(tmp_path)
    pipeline = StubPipeline(db)
    matcher = StubMatcher({"id": 44})
    controller = workflow_controller(db, telegram, pipeline=pipeline, matcher=matcher)

    assert controller.process_update(
        callback_update(80, f"draft:regen:{draft_id}")
    ) == "processed"
    assert controller.process_update(
        callback_update(81, f"draft:media:{draft_id}")
    ) == "processed"
    assert controller.process_update(
        callback_update(82, f"draft:edit:{draft_id}")
    ) == "processed"
    restarted = workflow_controller(Database(db.db_path), telegram, pipeline=pipeline)
    assert restarted.process_update(message_update(83, "Edited copy")) == "processed"
    assert controller.process_update(
        callback_update(84, f"draft:postpone:{draft_id}")
    ) == "processed"
    assert restarted.process_update(
        message_update(85, "2030-08-17T12:00:00+00:00")
    ) == "processed"

    assert ("regen", draft_id) in pipeline.calls
    assert matcher.calls == [draft_id]
    assert ("edit", draft_id, "Edited copy") in pipeline.calls
    assert (
        "postpone", draft_id, "2030-08-17T12:00:00+00:00"
    ) in pipeline.calls
    assert "publisher" not in inspect.signature(TelegramController).parameters


def test_growth_cards_and_decisions_are_manual_only_with_reason_suppression(tmp_path):
    db = Database(str(tmp_path / "growth.db"))
    evaluated_at = datetime.now(timezone.utc)
    activity_at = evaluated_at - timedelta(days=1)
    candidate_id = db.upsert_growth_candidate({
        "user_id": "900",
        "username": "real_owner",
        "profile": {
            "id": "900",
            "user_id": "900",
            "username": "real_owner",
            "description": "Independent gym owner",
            "followers_count": 1200,
            "following_count": 500,
            "protected": False,
            "spam_signals": [],
        },
        "latest_post": {
            "id": "12345",
            "text": "A useful studio update",
            "created_at": activity_at.isoformat(),
            "lang": "en",
            "is_original": True,
        },
        "score": 91,
        "score_data": {
            "total": 91,
            "audience_segment": "primary",
            "reasons": ["primary_operator_role"],
            "activity_at": activity_at.isoformat(),
            "hard_filter_passed": True,
            "filter_reason": "accepted",
        },
        "discovery_source": "search",
        "last_evaluated_at": evaluated_at.isoformat(),
        "profile_expires_at": (evaluated_at + timedelta(days=7)).isoformat(),
    })
    telegram = WorkflowTelegramApi(tmp_path)
    controller = workflow_controller(db, telegram)

    assert controller.process_update(message_update(90, "/growth")) == "processed"
    card = telegram.messages[-1]
    buttons = [button for row in card[2]["reply_markup"]["inline_keyboard"] for button in row]
    assert {button.get("callback_data") for button in buttons if "callback_data" in button} == {
        f"growth:save:{candidate_id}",
        f"growth:followed:{candidate_id}",
        f"growth:discard:{candidate_id}",
    }
    assert next(button["url"] for button in buttons if button["text"] == "Open on X") == (
        "https://x.com/real_owner/status/12345"
    )

    assert controller.process_update(
        callback_update(91, f"growth:discard:{candidate_id}")
    ) == "processed"
    reason_buttons = telegram.messages[-1][2]["reply_markup"]["inline_keyboard"]
    reason_callback = reason_buttons[0][0]["callback_data"]
    assert reason_callback == f"growth:reason:{candidate_id}:not_relevant"
    assert controller.process_update(callback_update(92, reason_callback)) == "processed"
    with db._conn() as conn:
        row = conn.execute(
            "SELECT decision, rejection_reason, suppressed_until "
            "FROM growth_candidates WHERE id = ?", (candidate_id,),
        ).fetchone()
    assert row["decision"] == "discarded"
    assert row["rejection_reason"] == "not_relevant"
    suppression = datetime.fromisoformat(row["suppressed_until"])
    assert timedelta(days=29) < suppression - datetime.now(timezone.utc) <= timedelta(days=30)

    followed_id = db.upsert_growth_candidate({
        "user_id": "901",
        "username": "second_owner",
        "profile": {"username": "second_owner"},
        "latest_post": {},
        "score": 90,
        "score_data": {"total": 90, "hard_filter_passed": True},
        "discovery_source": "network",
    })
    assert controller.process_update(
        callback_update(93, f"growth:followed:{followed_id}")
    ) == "processed"
    with db._conn() as conn:
        decision = conn.execute(
            "SELECT decision FROM growth_candidates WHERE id = ?", (followed_id,),
        ).fetchone()["decision"]
    assert decision == "followed_manually"


class NoRequests:
    def __init__(self):
        self.posts = []

    def post(self, *_args, **_kwargs):
        self.posts.append((_args, _kwargs))
        raise AssertionError("invalid Telegram payload must fail before network")


def test_telegram_api_enforces_message_callback_and_caption_limits(tmp_path):
    requests = NoRequests()
    api = TelegramApi("123456:secret", tmp_path, requests_client=requests)

    for call in (
        lambda: api.send_message("42", "x" * 4097),
        lambda: api.send_message("42", "ok", reply_markup={
            "inline_keyboard": [[{"text": "bad", "callback_data": "x" * 65}]]
        }),
        lambda: api.answer_callback("cb", text="x" * 201),
        lambda: api.send_media(
            "42", tmp_path / "image.jpg", "photo", caption="x" * 1025,
        ),
    ):
        try:
            call()
        except ValueError:
            pass
        else:
            raise AssertionError("Telegram limit was not enforced")
    assert requests.posts == []


def test_oversized_inbound_text_does_not_reach_draft_pipeline(tmp_path):
    db = Database(str(tmp_path / "oversized-text.db"))
    _source_id, draft_id = add_pending_draft(db)
    telegram = WorkflowTelegramApi(tmp_path)
    pipeline = StubPipeline(db)
    controller = workflow_controller(db, telegram, pipeline=pipeline)

    assert controller.process_update(
        callback_update(95, f"draft:edit:{draft_id}")
    ) == "processed"
    assert controller.process_update(message_update(96, "x" * 4097)) == "processed"

    assert not any(call[0] == "edit" for call in pipeline.calls)
    assert "troppo lungo" in telegram.messages[-1][1].lower()


class UploadTelegramApi(WorkflowTelegramApi):
    def __init__(self, media_library_dir, get_file_result=None):
        super().__init__(media_library_dir)
        self.get_file_result = get_file_result or {
            "file_id": "photo-file",
            "file_unique_id": "photo-unique",
            "file_size": 6,
            "file_path": "photos/remote.jpg",
        }
        self.get_file_calls = []
        self.downloads = []

    def get_file(self, file_id):
        self.get_file_calls.append(file_id)
        return dict(self.get_file_result)

    def download_file(
        self,
        file_path,
        destination,
        *,
        message_filename,
        mime_type,
        expected_size,
    ):
        destination = Path(destination)
        self.downloads.append({
            "file_path": file_path,
            "destination": destination,
            "message_filename": message_filename,
            "mime_type": mime_type,
            "expected_size": expected_size,
        })
        destination.write_bytes(b"jpeg!!")
        return destination


class UploadProcessor:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.uploads = []

    def process_new_file(self, filepath, filename, mime_type, file_size, user_context):
        self.uploads.append({
            "filepath": filepath,
            "filename": filename,
            "mime_type": mime_type,
            "file_size": file_size,
            "user_context": user_context,
        })
        if self.fail:
            raise RuntimeError("private processing details")
        return {
            "id": 44,
            "lifecycle_state": "available",
            "ai_description": "Real Pilates studio",
            "ai_tags": "pilates,rome",
            "user_context": user_context,
        }


class NoCreatePipeline:
    def create_for_slot(self, *_args, **_kwargs):
        raise AssertionError("Telegram upload must not create a draft")


def photo_update(update_id, caption="Real Pilates studio in Rome"):
    message = {
        "chat": {"id": 42},
        "photo": [{
            "file_id": "photo-file",
            "file_unique_id": "photo-unique",
            "width": 1200,
            "height": 800,
            "file_size": 6,
        }],
    }
    if caption is not None:
        message["caption"] = caption
    return {"update_id": update_id, "message": message}


def test_media_upload_uses_canonical_contract_and_only_enters_library(tmp_path):
    root = tmp_path / "media"
    root.mkdir(mode=0o700)
    db = Database(str(tmp_path / "upload.db"))
    telegram = UploadTelegramApi(root)
    processor = UploadProcessor()
    controller = TelegramController(
        telegram,
        db,
        Notifier(),
        "42",
        draft_pipeline=NoCreatePipeline(),
        media_processor=processor,
        dry_run=True,
    )

    assert controller.process_update(photo_update(100)) == "processed"

    assert telegram.get_file_calls == ["photo-file"]
    assert len(telegram.downloads) == 1
    download = telegram.downloads[0]
    assert download["message_filename"] == (
        "telegram-photo-"
        "caa64f9084c54478aa1df672a4bb5adc8ae4d8962056b1bc6d8b9e40dc61130e.jpg"
    )
    assert download["mime_type"] == "image/jpeg"
    assert download["expected_size"] == 6
    assert download["destination"].is_absolute()
    assert download["destination"].parent == root
    assert download["destination"].name.startswith(".telegram-download-")
    assert processor.uploads[0]["filename"] == download["message_filename"]
    assert processor.uploads[0]["user_context"] == "Real Pilates studio in Rome"
    assert db.list_post_drafts() == []
    assert not download["destination"].exists()
    reply = telegram.messages[-1][1]
    for expected in (
        "Libreria #44", "available", "Real Pilates studio", "pilates,rome",
        "Real Pilates studio in Rome",
    ):
        assert expected in reply


def test_media_processor_failure_cleans_download_and_replies_without_details(tmp_path):
    root = tmp_path / "media"
    root.mkdir(mode=0o700)
    db = Database(str(tmp_path / "upload-fail.db"))
    telegram = UploadTelegramApi(root)
    processor = UploadProcessor(fail=True)
    controller = TelegramController(
        telegram, db, Notifier(), "42", media_processor=processor,
    )

    assert controller.process_update(photo_update(101)) == "processed"

    destination = telegram.downloads[0]["destination"]
    assert not destination.exists()
    assert "private" not in telegram.messages[-1][1].lower()
    assert "non riuscito" in telegram.messages[-1][1].lower()
    assert db.list_post_drafts() == []


def test_upload_rejects_download_result_outside_explicit_destination(tmp_path):
    root = tmp_path / "media"
    root.mkdir(mode=0o700)
    outside = tmp_path / "must-not-be-processed-or-deleted.jpg"
    outside.write_bytes(b"private")
    db = Database(str(tmp_path / "upload-return.db"))
    telegram = UploadTelegramApi(root)
    original_download = telegram.download_file

    def wrong_result(*args, **kwargs):
        original_download(*args, **kwargs)
        return outside

    telegram.download_file = wrong_result
    processor = UploadProcessor()
    controller = TelegramController(
        telegram, db, Notifier(), "42", media_processor=processor,
    )

    assert controller.process_update(photo_update(107)) == "processed"

    assert processor.uploads == []
    assert outside.read_bytes() == b"private"
    assert not telegram.downloads[0]["destination"].exists()
    assert "non riuscito" in telegram.messages[-1][1].lower()


def test_get_file_optional_metadata_mismatch_fails_before_download(tmp_path):
    root = tmp_path / "media"
    root.mkdir(mode=0o700)
    db = Database(str(tmp_path / "upload-mismatch.db"))
    telegram = UploadTelegramApi(root, {
        "file_id": "different-file",
        "file_unique_id": "photo-unique",
        "file_size": 6,
        "file_path": "photos/remote.jpg",
    })
    processor = UploadProcessor()
    controller = TelegramController(
        telegram, db, Notifier(), "42", media_processor=processor,
    )

    assert controller.process_update(photo_update(102)) == "processed"

    assert telegram.downloads == []
    assert processor.uploads == []
    assert "non valido" in telegram.messages[-1][1].lower()


def test_get_file_unique_identity_mismatch_fails_before_download(tmp_path):
    root = tmp_path / "media"
    root.mkdir(mode=0o700)
    db = Database(str(tmp_path / "upload-identity.db"))
    telegram = UploadTelegramApi(root, {
        "file_id": "photo-file",
        "file_unique_id": "different-unique-id",
        "file_size": 6,
        "file_path": "photos/remote.jpg",
    })
    processor = UploadProcessor()
    controller = TelegramController(
        telegram, db, Notifier(), "42", media_processor=processor,
    )

    assert controller.process_update(photo_update(105)) == "processed"

    assert telegram.downloads == []
    assert processor.uploads == []
    assert "non valido" in telegram.messages[-1][1].lower()


def test_malformed_optional_caption_fails_before_get_file_with_safe_reply(tmp_path):
    root = tmp_path / "media"
    root.mkdir(mode=0o700)
    db = Database(str(tmp_path / "upload-caption.db"))
    telegram = UploadTelegramApi(root)
    controller = TelegramController(
        telegram, db, Notifier(), "42", media_processor=UploadProcessor(),
    )
    update = photo_update(103)
    update["message"]["caption"] = {"private": "payload"}

    assert controller.process_update(update) == "processed"

    assert telegram.get_file_calls == []
    assert "private" not in telegram.messages[-1][1].lower()
    assert "non valido" in telegram.messages[-1][1].lower()


def test_oversized_caption_fails_before_get_file(tmp_path):
    root = tmp_path / "media"
    root.mkdir(mode=0o700)
    db = Database(str(tmp_path / "upload-long-caption.db"))
    telegram = UploadTelegramApi(root)
    controller = TelegramController(
        telegram, db, Notifier(), "42", media_processor=UploadProcessor(),
    )

    assert controller.process_update(photo_update(106, "x" * 1025)) == "processed"

    assert telegram.get_file_calls == []
    assert "non valido" in telegram.messages[-1][1].lower()


def test_textonly_releases_reserved_media_and_trace_source(tmp_path):
    db = Database(str(tmp_path / "textonly.db"))
    source_id, draft_id = add_pending_draft(db)
    media_id = db.add_media("studio.jpg", "/tmp/studio.jpg", "image")
    media_source_id = db.add_content_source(
        "media_context",
        "Real studio",
        metadata={"media_id": media_id},
    )
    assert db.attach_media_to_draft(media_id, draft_id)
    telegram = WorkflowTelegramApi(tmp_path)
    controller = workflow_controller(db, telegram, pipeline=StubPipeline(db))

    assert controller.process_update(
        callback_update(104, f"draft:textonly:{draft_id}")
    ) == "processed"

    draft = db.get_post_draft(draft_id)
    assert draft["media_id"] is None
    assert draft["source_ids"] == [source_id]
    assert media_source_id not in draft["source_ids"]
    media = db.get_media_by_id(media_id)
    assert media["lifecycle_state"] == "available"
    assert media["reserved_by_draft_id"] is None
