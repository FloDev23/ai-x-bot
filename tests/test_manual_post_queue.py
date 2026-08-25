import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone

from modules.database import Database
from modules.draft_pipeline import DraftPipeline
from modules.fact_guard import FactCheckResult
from modules.media_processor import MediaProcessor
from modules.review_translation import ReviewTranslator


NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
STATE_KEY = "telegram_session:42"
TOKEN = "manualSessionToken_1234567890"
ENGLISH = "One useful operator note beats ten rushed posts."
ITALIAN = "Una nota utile per gli operatori vale più di dieci post affrettati."


class NeverGenerate:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def forbidden(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            raise AssertionError("manual copy must not invoke AI generation")

        return forbidden


class ApprovingGuard:
    def __init__(self, approved=True, reasons=None):
        self.approved = approved
        self.reasons = reasons or []
        self.calls = []

    def check(self, text, sources):
        self.calls.append((text, list(sources)))
        return FactCheckResult(self.approved, self.reasons)


class FixedScorer:
    def __init__(self, total=75):
        self.total = total
        self.calls = []

    def score_draft(self, text, *, sources, recent_texts):
        self.calls.append((text, list(sources), list(recent_texts)))
        return {"total": self.total}


def manual_pipeline(db, *, guard=None, scorer=None):
    generator = NeverGenerate()
    pipeline = DraftPipeline(
        db,
        planner=object(),
        generator=generator,
        fact_guard=guard or ApprovingGuard(),
        scorer=scorer or FixedScorer(),
        now_fn=lambda: NOW,
        review_translator=ReviewTranslator(generator),
    )
    return pipeline, generator


def seed_session_and_source(db, *, token=TOKEN, source_type="founder_note"):
    state = json.dumps({
        "v": 1,
        "kind": "manual_post",
        "step": "translation_text",
        "token": token,
    }, sort_keys=True, separators=(",", ":"))
    db.set_state(STATE_KEY, state)
    source_id = db.add_content_source(
        source_type,
        "I learned that useful operator notes deserve careful review.",
        metadata={"publishable": True} if source_type == "founder_note" else {},
        verified_by="floriano",
    )
    return state, source_id


def create_manual(pipeline, state, source_id, **overrides):
    values = {
        "text": ENGLISH,
        "category": "founder_journey",
        "source_ids": [source_id],
        "media_id": None,
        "translation_it": ITALIAN,
        "state_key": STATE_KEY,
        "expected_state_value": state,
        "session_token": TOKEN,
    }
    values.update(overrides)
    return pipeline.create_manual_from_telegram_session(**values)


def available_image(db, root):
    media_root = root / "manual-media"
    media_root.mkdir(mode=0o700)
    staged = media_root / "staged.jpg"
    content = b"\xff\xd8\xff\xe0manual-post-media"
    staged.write_bytes(content)
    return MediaProcessor(db).process_new_file(
        str(staged),
        "manual.jpg",
        "image/jpeg",
        len(content),
        "An operator preparing a flexible class.",
    )


def test_manual_copy_is_exact_and_bypasses_generation_but_uses_canonical_gates(
    tmp_path,
):
    db = Database(str(tmp_path / "manual-exact.db"))
    state, source_id = seed_session_and_source(db)
    guard = ApprovingGuard()
    scorer = FixedScorer(75)
    pipeline, generator = manual_pipeline(db, guard=guard, scorer=scorer)

    draft, outcome = create_manual(pipeline, state, source_id)

    assert outcome == "created"
    assert draft["text"] == ENGLISH
    assert draft["category"] == "founder_journey"
    assert draft["source_ids"] == [source_id]
    assert draft["score_data"]["total"] == 75
    assert generator.calls == []
    assert [call[0] for call in guard.calls] == [ENGLISH]
    assert [call[0] for call in scorer.calls] == [ENGLISH]
    queued = db.get_queue_draft(draft["id"])
    assert queued["translation_status"] == "ready"
    assert queued["translation_it"] == ITALIAN
    assert db.get_state(STATE_KEY) is None


def test_manual_copy_without_translation_enters_translation_pending(tmp_path):
    db = Database(str(tmp_path / "manual-pending-translation.db"))
    state, source_id = seed_session_and_source(db)
    pipeline, _generator = manual_pipeline(db)

    draft, outcome = create_manual(
        pipeline, state, source_id, translation_it=None,
    )

    assert outcome == "created"
    queued = db.get_queue_draft(draft["id"])
    assert queued["translation_status"] == "pending"
    assert queued["translation_it"] is None


def test_manual_copy_rejects_overlength_before_any_generator_call(tmp_path):
    db = Database(str(tmp_path / "manual-overlength.db"))
    state, source_id = seed_session_and_source(db)
    pipeline, generator = manual_pipeline(db)

    draft, outcome = create_manual(
        pipeline, state, source_id, text="x" * 281,
    )

    assert draft is None
    assert outcome == "rejected"
    assert generator.calls == []
    assert db.get_state(STATE_KEY) == state
    assert db.list_post_drafts() == []


def test_manual_copy_rejects_category_source_mismatch(tmp_path):
    db = Database(str(tmp_path / "manual-category-mismatch.db"))
    state, source_id = seed_session_and_source(db)
    pipeline, _generator = manual_pipeline(db)

    draft, outcome = create_manual(
        pipeline, state, source_id, category="product_proof",
    )

    assert (draft, outcome) == (None, "rejected")
    assert db.get_state(STATE_KEY) == state


def test_manual_copy_score_74_rejects_and_exact_75_accepts(tmp_path):
    low_db = Database(str(tmp_path / "manual-score-74.db"))
    low_state, low_source = seed_session_and_source(low_db)
    low_pipeline, _generator = manual_pipeline(low_db, scorer=FixedScorer(74))
    assert create_manual(low_pipeline, low_state, low_source) == (None, "rejected")
    assert low_db.get_state(STATE_KEY) == low_state

    pass_db = Database(str(tmp_path / "manual-score-75.db"))
    pass_state, pass_source = seed_session_and_source(pass_db)
    pass_pipeline, _generator = manual_pipeline(pass_db, scorer=FixedScorer(75))
    draft, outcome = create_manual(pass_pipeline, pass_state, pass_source)
    assert outcome == "created"
    assert draft["score_data"]["total"] == 75


def test_manual_session_replay_is_exact_and_payload_change_is_rejected(tmp_path):
    db = Database(str(tmp_path / "manual-replay.db"))
    state, source_id = seed_session_and_source(db)
    pipeline, _generator = manual_pipeline(db)

    first, first_outcome = create_manual(pipeline, state, source_id)
    replay, replay_outcome = create_manual(pipeline, state, source_id)
    changed, changed_outcome = create_manual(
        pipeline, state, source_id, text="Different operator copy.",
    )

    assert first_outcome == "created"
    assert replay_outcome == "already_applied"
    assert replay["id"] == first["id"]
    assert (changed, changed_outcome) == (None, "rejected")
    assert len(db.list_post_drafts()) == 1


def test_manual_draft_and_session_consume_rollback_together(tmp_path):
    db = Database(str(tmp_path / "manual-rollback.db"))
    state, source_id = seed_session_and_source(db)
    with db._conn() as conn:
        conn.execute("""
            CREATE TRIGGER fail_manual_queue BEFORE INSERT ON editorial_queue
            BEGIN SELECT RAISE(ABORT, 'forced queue failure'); END
        """)
    pipeline, _generator = manual_pipeline(db)

    try:
        create_manual(pipeline, state, source_id)
    except Exception:
        pass

    assert db.get_state(STATE_KEY) == state
    assert db.list_post_drafts() == []


def test_manual_media_reservation_and_trace_commit_with_draft(tmp_path):
    db = Database(str(tmp_path / "manual-media.db"))
    state, source_id = seed_session_and_source(db)
    media = available_image(db, tmp_path)
    pipeline, _generator = manual_pipeline(db)

    draft, outcome = create_manual(
        pipeline, state, source_id, media_id=media["id"],
    )

    assert outcome == "created"
    assert draft["media_id"] == media["id"]
    context_source = db.get_media_context_source(media["id"])
    assert draft["source_ids"] == [source_id, context_source["id"]]
    reserved = db.get_media_by_id(media["id"])
    assert reserved["lifecycle_state"] == "reserved"
    assert reserved["reserved_by_draft_id"] == draft["id"]
    assert db.get_state(STATE_KEY) is None


def test_manual_media_rollback_preserves_session_and_availability(tmp_path):
    db = Database(str(tmp_path / "manual-media-rollback.db"))
    state, source_id = seed_session_and_source(db)
    media = available_image(db, tmp_path)
    with db._conn() as conn:
        conn.execute("""
            CREATE TRIGGER fail_manual_media_queue BEFORE INSERT ON editorial_queue
            BEGIN SELECT RAISE(ABORT, 'forced media queue failure'); END
        """)
    pipeline, _generator = manual_pipeline(db)

    try:
        create_manual(pipeline, state, source_id, media_id=media["id"])
    except Exception:
        pass

    assert db.get_state(STATE_KEY) == state
    assert db.list_post_drafts() == []
    available = db.get_media_by_id(media["id"])
    assert available["lifecycle_state"] == "available"
    assert available["reserved_by_draft_id"] is None


def test_manual_fact_rejection_preserves_session(tmp_path):
    db = Database(str(tmp_path / "manual-fact-reject.db"))
    state, source_id = seed_session_and_source(db)
    pipeline, _generator = manual_pipeline(
        db, guard=ApprovingGuard(False, ["unsupported_number"]),
    )

    assert create_manual(pipeline, state, source_id) == (None, "rejected")
    assert db.get_state(STATE_KEY) == state
    assert db.list_post_drafts() == []


def test_manual_revoked_source_fails_closed(tmp_path):
    db = Database(str(tmp_path / "manual-revoked.db"))
    state, source_id = seed_session_and_source(db)
    with db._conn() as conn:
        conn.execute(
            "UPDATE content_sources SET trust_state='revoked' WHERE id=?",
            (source_id,),
        )
    pipeline, _generator = manual_pipeline(db)

    draft, outcome = create_manual(pipeline, state, source_id)

    assert (draft, outcome) == (None, "no_eligible_source")
    assert db.get_state(STATE_KEY) == state


def test_manual_url_must_be_safe_and_match_a_selected_source(tmp_path):
    for suffix, url in (
        ("http", "http://flexdropin.com/features"),
        ("credentials", "https://user:pass@flexdropin.com/features"),
        ("unselected", "https://example.com/report"),
    ):
        db = Database(str(tmp_path / f"manual-url-{suffix}.db"))
        state, source_id = seed_session_and_source(db)
        pipeline, _generator = manual_pipeline(db)

        draft, outcome = create_manual(
            pipeline,
            state,
            source_id,
            text=f"Read the supporting operator note: {url}",
            translation_it=None,
        )

        assert (draft, outcome) == (None, "rejected")
        assert db.get_state(STATE_KEY) == state


def test_manual_safe_selected_source_url_is_preserved_exactly(tmp_path):
    db = Database(str(tmp_path / "manual-safe-url.db"))
    state = json.dumps({
        "v": 1,
        "kind": "manual_post",
        "step": "translation_text",
        "token": TOKEN,
    }, sort_keys=True, separators=(",", ":"))
    db.set_state(STATE_KEY, state)
    source_id = db.add_content_source(
        "product_fact",
        "FlexDropin explains its flexible booking workflow.",
        url="https://flexdropin.com/features",
        verified_by="floriano",
    )
    pipeline, _generator = manual_pipeline(db)
    exact = "See the flexible booking workflow: https://flexdropin.com/features"

    draft, outcome = create_manual(
        pipeline,
        state,
        source_id,
        text=exact,
        category="product_proof",
        translation_it=None,
    )

    assert outcome == "created"
    assert draft["text"] == exact


def test_manual_invalid_operator_translation_preserves_session(tmp_path):
    db = Database(str(tmp_path / "manual-invalid-translation.db"))
    state, source_id = seed_session_and_source(db)
    pipeline, _generator = manual_pipeline(db)

    draft, outcome = create_manual(
        pipeline,
        state,
        source_id,
        text="Exactly 10 useful operator notes.",
        translation_it="Esattamente 11 note utili per gli operatori.",
    )

    assert (draft, outcome) == (None, "rejected")
    assert db.get_state(STATE_KEY) == state


def test_two_workers_create_one_exact_manual_draft(tmp_path):
    path = str(tmp_path / "manual-race.db")
    db = Database(path)
    state, source_id = seed_session_and_source(db)
    barrier = threading.Barrier(2)
    outcomes = []

    def worker():
        pipeline, _generator = manual_pipeline(Database(path))
        barrier.wait(timeout=5)
        draft, outcome = create_manual(pipeline, state, source_id)
        outcomes.append((draft["id"] if draft else None, outcome))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert {outcome for _draft_id, outcome in outcomes} == {
        "created", "already_applied",
    }
    assert len({draft_id for draft_id, _outcome in outcomes}) == 1
    assert len(Database(path).list_post_drafts()) == 1


def test_review_translator_validates_operator_copy_without_provider_call():
    generator = NeverGenerate()
    translator = ReviewTranslator(generator)

    result = translator.validate(ENGLISH, ITALIAN)

    assert result is not None
    assert result.text_it == ITALIAN
    assert generator.calls == []


def test_manual_hard_crash_before_commit_preserves_session_and_no_draft(tmp_path):
    path = str(tmp_path / "manual-hard-crash.db")
    db = Database(path)
    state, source_id = seed_session_and_source(db)
    script = r'''
import os
import sqlite3
import sys
from datetime import datetime, timezone
import modules.database as database_module

db = database_module.Database(sys.argv[1])
real_connect = sqlite3.connect

class CrashCursor:
    def __init__(self, cursor):
        self._cursor = cursor
    def execute(self, sql, parameters=()):
        result = self._cursor.execute(sql, parameters)
        if "INSERT INTO editorial_queue (" in sql:
            os._exit(97)
        return self
    def __iter__(self):
        return iter(self._cursor)
    def __getattr__(self, name):
        return getattr(self._cursor, name)

class CrashConnection:
    def __init__(self, connection):
        object.__setattr__(self, "_connection", connection)
    def __setattr__(self, name, value):
        setattr(self._connection, name, value)
    def cursor(self):
        return CrashCursor(self._connection.cursor())
    def execute(self, sql, parameters=()):
        return CrashCursor(self._connection.cursor()).execute(sql, parameters)
    def __getattr__(self, name):
        return getattr(self._connection, name)

database_module.sqlite3.connect = lambda path: CrashConnection(real_connect(path))
db.create_manual_queue_draft_consuming_state_atomic(
    text=sys.argv[4],
    category="founder_journey",
    source_ids=[int(sys.argv[3])],
    score_data={"total": 75},
    intended_slot="2026-08-24T12:00:00.114616+00:00",
    media_id=None,
    translation_it=sys.argv[5],
    state_key=sys.argv[2],
    expected_state_value=sys.argv[6],
    session_token=sys.argv[7],
    validation_time=datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc),
)
'''

    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            path,
            STATE_KEY,
            str(source_id),
            ENGLISH,
            ITALIAN,
            state,
            TOKEN,
        ],
        cwd=os.getcwd(),
        check=False,
    )

    assert crashed.returncode == 97
    reopened = Database(path)
    assert reopened.get_state(STATE_KEY) == state
    assert reopened.list_post_drafts() == []
