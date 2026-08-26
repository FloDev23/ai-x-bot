"""Restart-safe optional source intake for Telegram manual posts."""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from modules.database import Database
from modules.telegram_controller import TelegramController


class Telegram:
    def __init__(self, media_library_dir):
        self.media_library_dir = Path(media_library_dir)
        self.messages = []
        self.callback_answers = []

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append((str(chat_id), text, kwargs))
        return {"message_id": len(self.messages)}

    def answer_callback(self, callback_id, **kwargs):
        self.callback_answers.append((callback_id, kwargs))
        return True


class Notifier:
    def notify_error(self, *_args):
        return None


class Pipeline:
    """Small real persistence boundary; it deliberately revalidates sources."""

    def __init__(self, db):
        self.db = db
        self.calls = []

    def create_manual_from_telegram_session(self, **kwargs):
        self.calls.append(kwargs)
        return self.db.create_manual_approved_draft_consuming_state_atomic(
            text=kwargs["text"],
            category=kwargs["category"],
            source_ids=kwargs["source_ids"],
            intended_slot="2029-08-15T12:00:00+00:00",
            media_id=kwargs["media_id"],
            state_key=kwargs["state_key"],
            expected_state_value=kwargs["expected_state_value"],
            session_token=kwargs["session_token"],
            operator="telegram_operator",
            now=datetime(2029, 8, 15, tzinfo=timezone.utc),
        )


def message(update_id, text, chat_id=42):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text},
    }


def callback(update_id, data, chat_id=42):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "message": {"chat": {"id": chat_id}},
            "data": data,
        },
    }


def controller(db, telegram):
    return TelegramController(
        telegram_api=telegram,
        db=db,
        notifier=Notifier(),
        authorized_chat_id="42",
        draft_pipeline=Pipeline(db),
        dry_run=True,
        now_fn=lambda: datetime(2029, 8, 15, tzinfo=timezone.utc),
        news_trusted_domains={"news.example"},
    )


def session(db):
    return json.loads(db.get_state("telegram_session:42"))


def start_at_sources(db, telegram, *, category="founder_journey"):
    bot = controller(db, telegram)
    assert bot.process_update(message(1, "/newpost")) == "processed"
    assert bot.process_update(message(2, "Exact English source intake copy.")) == "processed"
    assert bot.process_update(callback(3, f"manual:category:{category}")) == "processed"
    return bot


def button_texts(telegram):
    markup = telegram.messages[-1][2]["reply_markup"]
    return [button["text"] for row in markup["inline_keyboard"] for button in row]


def test_source_choice_has_existing_add_none_and_cancel_then_none_reaches_media(tmp_path):
    db = Database(str(tmp_path / "choice.db"))
    telegram = Telegram(tmp_path)
    bot = start_at_sources(db, telegram)

    assert button_texts(telegram) == [
        "Scegli fonti esistenti", "Aggiungi una fonte", "Nessuna fonte", "Annulla",
    ]
    assert bot.process_update(callback(4, "manual:sources:none")) == "processed"
    assert session(db)["step"] == "media"
    assert session(db)["payload"]["source_ids"] == []


def test_existing_sources_paginate_select_exact_ids_without_rendering_bodies(tmp_path):
    db = Database(str(tmp_path / "existing.db"))
    expected = []
    for index in range(12):
        expected.append(db.add_content_source(
            "founder_note",
            f"PRIVATE_SOURCE_BODY_{index}",
            metadata={
                "publishable": True,
                "title": f"Lesson {index} " + "x" * 80,
            },
            verified_by="floriano",
        ))
    telegram = Telegram(tmp_path)
    bot = start_at_sources(db, telegram)
    source_bodies = {f"PRIVATE_SOURCE_BODY_{index}" for index in range(12)}

    assert bot.process_update(callback(4, "manual:sources:existing")) == "processed"
    rendered = "\n".join(text for _chat, text, _kwargs in telegram.messages)
    page_zero_labels = button_texts(telegram)
    assert all(body not in rendered for body in source_bodies)
    assert all(
        body not in label
        for body in source_bodies
        for label in page_zero_labels
    )
    assert any("· Verificata" in label for label in page_zero_labels)
    assert any(f"#{expected[-1]} founder_note" in label for label in page_zero_labels)
    assert bot.process_update(callback(5, "manual:sources:page:1")) == "processed"
    page_one_labels = button_texts(telegram)
    page_one_rendered = "\n".join(
        text for _chat, text, _kwargs in telegram.messages
    )
    assert all(body not in page_one_rendered for body in source_bodies)
    assert all(
        body not in label
        for body in source_bodies
        for label in page_one_labels
    )
    assert any(f"#{expected[0]} founder_note" in label for label in page_one_labels)
    for update_id, source_id in enumerate(expected[-3:], start=6):
        assert bot.process_update(callback(update_id, f"manual:source:{source_id}")) == "processed"
    assert session(db)["payload"]["source_ids"] == expected[-3:]
    assert bot.process_update(callback(9, "manual:sources_done")) == "processed"
    assert session(db)["step"] == "media"


def test_child_news_source_survives_restarts_and_atomically_resumes_parent(tmp_path):
    path = str(tmp_path / "child-news.db")
    telegram = Telegram(tmp_path)
    bot = start_at_sources(Database(path), telegram, category="fitness_business_insight")
    parent_token = session(Database(path))["token"]

    assert bot.process_update(callback(4, "manual:sources:add")) == "processed"
    initial_child = session(Database(path))
    assert initial_child["step"] == "source_child_text"
    assert initial_child["token"] == parent_token
    for update_id, update in enumerate((
        message(5, "Studios report a measurable change."),
        callback(6, "manual:child:source:verified_news"),
        message(7, "https://reports.news.example/2029/change"),
        message(8, "2029-08-14"),
        message(9, "News Example"),
    ), start=5):
        del update_id
        bot = controller(Database(path), telegram)
        assert bot.process_update(update) == "processed"
        assert session(Database(path))["token"] == parent_token

    resumed = session(Database(path))
    assert resumed["step"] == "sources"
    assert resumed["token"] == parent_token
    assert "child" not in resumed["payload"]
    source_ids = resumed["payload"]["source_ids"]
    assert len(source_ids) == 1
    source = Database(path).get_content_source(source_ids[0])
    assert source["metadata"] == {
        "title": "Studios report a measurable change.",
        "summary": "Studios report a measurable change.",
        "published_at": "2029-08-14",
        "source_name": "News Example",
    }


def test_child_cancel_preserves_parent_and_replayed_finish_creates_one_source(tmp_path):
    db = Database(str(tmp_path / "child-replay.db"))
    telegram = Telegram(tmp_path)
    bot = start_at_sources(db, telegram)
    parent_token = session(db)["token"]
    assert bot.process_update(callback(4, "manual:sources:add")) == "processed"
    child_session = session(db)
    child = child_session["payload"]["child"]["token"]
    assert child_session["token"] == parent_token
    assert bot.process_update(callback(5, "manual:sources:child_cancel")) == "processed"
    assert session(db)["step"] == "sources"
    assert session(db)["token"] == parent_token
    assert session(db)["payload"]["source_ids"] == []

    assert bot.process_update(callback(6, "manual:sources:add")) == "processed"
    assert bot.process_update(message(7, "A founder lesson saved exactly once.")) == "processed"
    assert bot.process_update(callback(8, "manual:child:source:founder_note")) == "processed"
    assert len(db.get_eligible_sources("founder_note")) == 1
    assert bot.process_update(callback(9, "manual:child:source:founder_note")) == "processed"
    assert len(db.get_eligible_sources("founder_note")) == 1
    assert child != parent_token
    assert session(db)["token"] == parent_token


def test_stale_source_keeps_parent_for_reselection_and_unauthorized_gets_no_state(tmp_path):
    db = Database(str(tmp_path / "stale.db"))
    source_id = db.add_content_source(
        "founder_note", "A source that will become stale.",
        metadata={"publishable": True}, verified_by="floriano",
    )
    valid_source_id = db.add_content_source(
        "founder_note", "A source that remains eligible.",
        metadata={"publishable": True}, verified_by="floriano",
    )
    telegram = Telegram(tmp_path)
    bot = start_at_sources(db, telegram)
    assert bot.process_update(callback(4, "manual:sources:existing")) == "processed"
    assert bot.process_update(callback(5, f"manual:source:{source_id}")) == "processed"
    assert bot.process_update(callback(6, f"manual:source:{valid_source_id}")) == "processed"
    assert bot.process_update(callback(7, "manual:sources_done")) == "processed"
    with db._conn() as conn:
        conn.execute("DELETE FROM content_sources WHERE id = ?", (source_id,))
    assert bot.process_update(callback(8, "manual:media:none")) == "processed"
    recovered = session(db)
    assert recovered["step"] == "sources"
    assert recovered["payload"]["source_ids"] == [valid_source_id]
    assert "fonti selezionate non sono più disponibili" in telegram.messages[-1][1].lower()

    assert bot.process_update(message(9, "/newpost", chat_id=999)) == "unauthorized"
    assert db.get_state("telegram_session:999") is None
    assert all(chat_id != "999" for chat_id, _text, _kwargs in telegram.messages)


def test_child_operation_replay_is_exact_and_concurrent_writers_create_one_source(tmp_path):
    path = str(tmp_path / "child-operation.db")
    db = Database(path)
    telegram = Telegram(tmp_path)
    bot = start_at_sources(db, telegram)
    assert bot.process_update(callback(4, "manual:sources:add")) == "processed"
    assert bot.process_update(message(5, "A founder note for atomic insertion.")) == "processed"
    expected = db.get_state("telegram_session:42")
    child = session(db)["payload"]["child"]
    resumed = bot._session_value(
        "manual_post", "sources",
        {
            "text": "Exact English source intake copy.",
            "category": "founder_journey",
            "source_ids": [],
        },
        token=session(db)["token"],
    )
    values = dict(
        state_key="telegram_session:42",
        expected_state_value=expected,
        resumed_state_value=resumed,
        child_token=child["token"],
        source_type="founder_note",
        text="A founder note for atomic insertion.",
        url=None,
        metadata={"publishable": True},
        trust_state="verified",
        verified_by="floriano",
    )
    barrier = threading.Barrier(2)
    outcomes = []

    def save_once():
        barrier.wait()
        outcomes.append(Database(path).add_content_source_and_resume_manual_state_atomic(**values))

    threads = [threading.Thread(target=save_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcome for _source_id, outcome in outcomes) == [
        "already_applied", "created",
    ]
    source_ids = {source_id for source_id, _outcome in outcomes}
    assert len(source_ids) == 1
    assert len(db.get_eligible_sources("founder_note")) == 1
    assert session(db)["payload"]["source_ids"] == [source_ids.pop()]
    replay_id, replay_outcome = db.add_content_source_and_resume_manual_state_atomic(**values)
    assert replay_outcome == "already_applied"
    assert len(db.get_eligible_sources("founder_note")) == 1
    changed_id, changed_outcome = db.add_content_source_and_resume_manual_state_atomic(
        **{**values, "text": "Changed replay must fail closed."},
    )
    assert changed_id is None
    assert changed_outcome == "child_replay_mismatch"


def test_child_operation_rolls_back_source_and_parent_state_on_insert_failure(tmp_path):
    db = Database(str(tmp_path / "child-rollback.db"))
    telegram = Telegram(tmp_path)
    bot = start_at_sources(db, telegram)
    assert bot.process_update(callback(4, "manual:sources:add")) == "processed"
    assert bot.process_update(message(5, "A note that must roll back.")) == "processed"
    expected = db.get_state("telegram_session:42")
    current = session(db)
    values = dict(
        state_key="telegram_session:42",
        expected_state_value=expected,
        resumed_state_value=bot._session_value(
            "manual_post", "sources",
            {
                "text": "Exact English source intake copy.",
                "category": "founder_journey",
                "source_ids": [],
            },
            token=current["token"],
        ),
        child_token=current["payload"]["child"]["token"],
        source_type="founder_note",
        text="A note that must roll back.",
        url=None,
        metadata={"publishable": True},
        trust_state="verified",
        verified_by="floriano",
    )
    with db._conn() as conn:
        conn.execute("""
            CREATE TRIGGER fail_child_source BEFORE INSERT ON content_sources
            BEGIN SELECT RAISE(ABORT, 'injected child source failure'); END
        """)
    try:
        db.add_content_source_and_resume_manual_state_atomic(**values)
    except sqlite3.DatabaseError:
        pass
    else:
        raise AssertionError("injected source write must fail")

    assert db.get_state("telegram_session:42") == expected
    assert db.get_eligible_sources("founder_note") == []
    with db._conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM telegram_child_operations",
        ).fetchone()[0] == 0


def test_child_operation_rejects_malformed_resume_without_orphan_source(tmp_path):
    db = Database(str(tmp_path / "child-malformed-resume.db"))
    telegram = Telegram(tmp_path)
    bot = start_at_sources(db, telegram)
    assert bot.process_update(callback(4, "manual:sources:add")) == "processed"
    assert bot.process_update(message(5, "A source that cannot be orphaned.")) == "processed"
    expected = db.get_state("telegram_session:42")
    current = session(db)
    malformed_resume = json.dumps({
        "version": 1,
        "token": current["token"],
        "kind": "manual_post",
        "step": "sources",
        "payload": {
            "text": "Exact English source intake copy.",
            "category": "founder_journey",
            "source_ids": ["not-an-id"],
        },
        "expires_at": current["expires_at"],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    source_id, outcome = db.add_content_source_and_resume_manual_state_atomic(
        state_key="telegram_session:42",
        expected_state_value=expected,
        resumed_state_value=malformed_resume,
        child_token=current["payload"]["child"]["token"],
        source_type="founder_note",
        text="A source that cannot be orphaned.",
        url=None,
        metadata={"publishable": True},
        trust_state="verified",
        verified_by="floriano",
    )

    assert source_id is None
    assert outcome == "rejected"
    assert db.get_state("telegram_session:42") == expected
    assert db.get_eligible_sources("founder_note") == []
    with db._conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM telegram_child_operations",
        ).fetchone()[0] == 0
