from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from modules.database import Database
from modules.reply_copilot import ReplyCopilotService
from modules.telegram_controller import TelegramController
from tests.fakes import FakeTelegramApi, callback_update
from tests.test_reply_copilot import (
    NOW,
    QueueReplyGenerator,
    SAFE_REPLY,
    _seed_custom_growth_posts,
    _seed_growth_posts,
)


class NoopNotifier:
    def __init__(self):
        self.errors = []

    def notify_error(self, operation, error):
        self.errors.append((operation, type(error).__name__))


def _message_update(update_id, text, chat_id=42):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text},
    }


def _buttons(message):
    markup = message[2].get("reply_markup") or {}
    return [
        button
        for row in markup.get("inline_keyboard", [])
        for button in row
    ]


def _controller(tmp_path, *, count=2, responses=None, database=None):
    db = database or Database(str(tmp_path / "reply-telegram.db"))
    if count:
        _seed_growth_posts(db, count)
    generator = QueueReplyGenerator(responses)
    service = ReplyCopilotService(db, generator)
    telegram = FakeTelegramApi(tmp_path / "media")
    notifier = NoopNotifier()
    controller = TelegramController(
        telegram,
        db,
        notifier,
        "42",
        reply_copilot=service,
        now_fn=lambda: NOW,
    )
    return controller, db, telegram, service, generator, notifier


def test_replies_command_builds_manual_card_with_copy_and_web_intent(tmp_path):
    controller, database, telegram, _service, generator, _notifier = _controller(
        tmp_path, count=1,
    )

    assert controller.process_update(_message_update(1, "/replies")) == "processed"

    assert len(generator.calls) == 1
    assert len(telegram.messages) == 2
    assert telegram.messages[0][1].splitlines() == [
        "Reply Copilot — risposte manuali",
        "Pronte: 1",
        "Fallite: 0",
    ]
    card = telegram.messages[1]
    assert "Post di @gymowner0" in card[1]
    assert "Segmento: operator" in card[1]
    assert "Rilevanza: 95" in card[1]
    assert SAFE_REPLY in card[1]
    assert f"Caratteri: {len(SAFE_REPLY)}/256" in card[1]
    assert "Pubblicazione manuale: il bot non risponde su X" in card[1]

    buttons = {button["text"]: button for button in _buttons(card)}
    assert buttons["Copia risposta"] == {
        "text": "Copia risposta",
        "copy_text": {"text": SAFE_REPLY},
    }
    assert "callback_data" not in buttons["Copia risposta"]
    intent = urlparse(buttons["Rispondi su X"]["url"])
    assert parse_qs(intent.query) == {
        "in_reply_to": ["8100"],
        "text": [SAFE_REPLY],
    }
    assert "callback_data" not in buttons["Rispondi su X"]
    row = database.list_reply_suggestions("2026-09-03")[0]
    assert row["status"] == "ready"


def test_promotional_following_card_explains_source_and_has_no_regenerate(tmp_path):
    database = Database(str(tmp_path / "reply-promotional-card.db"))
    _seed_custom_growth_posts(database, [{
        "reasons": ["gym_owner", "empty_capacity", "followed_account"],
        "excerpt": "Our gym has empty spots in tomorrow's class.",
        "score": 99,
    }])
    controller, _db, telegram, _service, generator, _notifier = _controller(
        tmp_path, count=0, database=database,
    )

    assert controller.process_update(_message_update(20, "/replies")) == "processed"

    assert generator.calls == []
    card = telegram.messages[1]
    assert "Fonte: account seguito da @FlexDropin" in card[1]
    assert "Tipo: promozionale contestuale" in card[1]
    buttons = {button["text"]: button for button in _buttons(card)}
    assert "Rigenera" not in buttons
    intent = urlparse(buttons["Rispondi su X"]["url"])
    assert "FlexDropin" in parse_qs(intent.query)["text"][0]


def test_replies_command_without_persisted_digest_never_generates(tmp_path):
    controller, _db, telegram, _service, generator, _notifier = _controller(
        tmp_path, count=0,
    )

    assert controller.process_update(_message_update(2, "/replies")) == "processed"

    assert generator.calls == []
    assert len(telegram.messages) == 1
    assert telegram.messages[0][1] == (
        "Nessun candidato disponibile: prima serve un Growth Digest persistito."
    )


def test_scheduled_reply_summary_is_only_sent_for_new_ready_rows(tmp_path):
    controller, _db, telegram, service, _generator, _notifier = _controller(
        tmp_path, count=1,
    )
    created = service.build("2026-09-03", now=NOW)

    assert controller.push_reply_digest(created, explicit=False) == "reply_digest"
    assert len(telegram.messages) == 2
    existing = service.build("2026-09-03", now=NOW + timedelta(minutes=1))
    assert controller.push_reply_digest(existing, explicit=False) == (
        "reply_digest_silent"
    )
    assert len(telegram.messages) == 2


def test_reply_navigation_and_regeneration_are_bound_to_view_and_revision(tmp_path):
    controller, database, telegram, _service, generator, _notifier = _controller(
        tmp_path,
        count=3,
        responses=[SAFE_REPLY, SAFE_REPLY, SAFE_REPLY,
                   "Compare attendance windows before changing the timetable again."],
    )
    controller.process_update(_message_update(10, "/replies"))
    first_card = telegram.messages[-1]
    next_button = next(
        button for button in _buttons(first_card) if button["text"] == "Successiva"
    )

    assert controller.process_update(
        callback_update(11, next_button["callback_data"])
    ) == "processed"
    second_card = telegram.messages[-1]
    assert "@gymowner1" in second_card[1]
    regenerate = next(
        button for button in _buttons(second_card) if button["text"] == "Rigenera"
    )
    before = database.list_reply_suggestions("2026-09-03")[1]

    assert controller.process_update(
        callback_update(12, regenerate["callback_data"])
    ) == "processed"
    after = database.get_reply_suggestion(before["id"])
    assert after["generation_count"] == 2
    assert after["revision"] == before["revision"] + 2
    assert after["reply_text"] == (
        "Compare attendance windows before changing the timetable again."
    )
    assert len(generator.calls) == 4

    assert controller.process_update(
        callback_update(13, regenerate["callback_data"])
    ) == "processed"
    replayed = database.get_reply_suggestion(before["id"])
    assert replayed == after
    assert "non valida o scaduta" in telegram.messages[-1][1].lower()


def test_reply_manual_actions_are_local_idempotent_and_foreign_chat_is_blocked(
    tmp_path,
):
    controller, database, telegram, _service, _generator, _notifier = _controller(
        tmp_path, count=2,
    )
    controller.process_update(_message_update(20, "/replies"))
    first = database.list_reply_suggestions("2026-09-03")[0]
    publish = next(
        button for button in _buttons(telegram.messages[-1])
        if button["text"] == "Segna come pubblicata"
    )

    assert controller.process_update(
        callback_update(21, publish["callback_data"], chat_id=999)
    ) == "unauthorized"
    assert database.get_reply_suggestion(first["id"])["status"] == "ready"

    assert controller.process_update(
        callback_update(22, publish["callback_data"])
    ) == "processed"
    published = database.get_reply_suggestion(first["id"])
    assert published["status"] == "published_manually"
    assert controller.process_update(
        callback_update(23, publish["callback_data"])
    ) == "processed"
    assert database.get_reply_suggestion(first["id"]) == published

    controller.process_update(_message_update(24, "/replies"))
    dismiss = next(
        button for button in _buttons(telegram.messages[-1])
        if button["text"] == "Ignora"
    )
    controller.process_update(callback_update(25, dismiss["callback_data"]))
    statuses = {
        row["status"] for row in database.list_reply_suggestions("2026-09-03")
    }
    assert statuses == {"published_manually", "dismissed"}


def test_reply_callback_rejects_forged_or_expired_view(tmp_path):
    controller, database, telegram, _service, _generator, _notifier = _controller(
        tmp_path, count=1,
    )
    controller.process_update(_message_update(30, "/replies"))
    row = database.list_reply_suggestions("2026-09-03")[0]
    action = next(
        button["callback_data"] for button in _buttons(telegram.messages[-1])
        if button["text"] == "Ignora"
    )
    parts = action.split(":")
    forged = ":".join(parts[:-2] + [str(row["id"] + 999), parts[-1]])

    controller.process_update(callback_update(31, forged))
    assert database.get_reply_suggestion(row["id"])["status"] == "ready"
    assert "non valida o scaduta" in telegram.messages[-1][1].lower()

    with database._conn() as connection:
        connection.execute(
            "UPDATE telegram_views SET expires_at = ? WHERE token = ?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), parts[2]),
        )
    controller.process_update(callback_update(32, action))
    assert database.get_reply_suggestion(row["id"])["status"] == "ready"
    assert "non valida o scaduta" in telegram.messages[-1][1].lower()


def test_status_and_help_describe_reply_copilot_without_exposing_copy(tmp_path):
    controller, _database, telegram, service, _generator, _notifier = _controller(
        tmp_path, count=1,
    )
    service.build("2026-09-03", now=NOW)

    controller.process_update(_message_update(40, "/status"))
    status = telegram.messages[-1][1]
    assert "Reply Copilot — oggi" in status
    assert "pronte: 1" in status
    assert SAFE_REPLY not in status

    controller.process_update(_message_update(41, "/help"))
    assert "/replies — risposte X da pubblicare manualmente" in (
        telegram.messages[-1][1]
    )
