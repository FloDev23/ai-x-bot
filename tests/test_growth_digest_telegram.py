import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from modules.database import Database
from modules.telegram_controller import TelegramController
from tests.fakes import FakeTelegramApi, callback_update


ROME = ZoneInfo("Europe/Rome")
NOW = datetime(2026, 8, 26, 9, 0, tzinfo=ROME)


class NoopNotifier:
    def __init__(self):
        self.errors = []

    def notify_error(self, operation, error):
        self.errors.append((operation, type(error).__name__))


class FixedDigest:
    def __init__(self, digest):
        self.digest = digest
        self.calls = []

    def build(self, now):
        self.calls.append(now)
        return self.digest


def _message_update(update_id, text, chat_id=42):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text},
    }


def _payload(kind, object_id, username):
    reasons = (
        ["gym_owner", "recent"]
        if kind == "post"
        else ["primary_operator_role", "active_within_7_days"]
    )
    if kind == "post":
        return {
            "id": object_id,
            "author_id": "200",
            "author_username": username,
            "excerpt": "A gym owner explains how to fill empty class capacity.",
            "created_at": "2026-08-26T06:00:00+00:00",
            "public_metrics": {
                "like_count": 12,
                "retweet_count": 2,
                "reply_count": 3,
                "quote_count": 1,
                "impression_count": 500,
            },
            "reason_codes": reasons,
        }, reasons
    return {
        "user_id": object_id,
        "username": username,
        "public_metrics": {
            "followers_count": 1200,
            "following_count": 300,
            "tweet_count": 450,
            "listed_count": 4,
        },
        "latest_activity_id": "9001",
        "latest_activity_at": "2026-08-26T06:00:00+00:00",
        "segment": "primary",
        "reason_codes": reasons,
    }, reasons


def _seed_digest(db, *, kinds=("account", "post", "reevaluate")):
    identities = {
        "account": ("101", "studio_owner"),
        "post": ("7001", "gym_writer"),
        "reevaluate": ("303", "old_contact"),
    }
    with db._conn() as conn:
        for rank, kind in enumerate(kinds):
            object_id, username = identities[kind]
            payload, reasons = _payload(kind, object_id, username)
            conn.execute(
                """
                INSERT INTO growth_suggestions (
                    observed_on, kind, object_id, username, payload_json,
                    score, reason_codes_json, suggested_at, cooldown_until,
                    rank_position
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-08-26", kind, object_id, username,
                    json.dumps(payload), 88, json.dumps(reasons),
                    "2026-08-26T07:00:00+00:00",
                    "2026-09-25T07:00:00+00:00", 0,
                ),
            )
        counts = {name: int(name in kinds) for name in identities}
        conn.execute(
            "INSERT INTO growth_digest_runs VALUES (?, ?, ?)",
            (
                "2026-08-26", "2026-08-26T07:00:00+00:00",
                json.dumps({"observed_on": "2026-08-26", "counts": counts}),
            ),
        )
    return db.get_growth_digest("2026-08-26")


def _controller(tmp_path, digest, *, db=None):
    database = db or Database(str(tmp_path / "digest.db"))
    telegram = FakeTelegramApi(tmp_path / "media")
    service = FixedDigest(digest)
    controller = TelegramController(
        telegram, database, NoopNotifier(), "42",
        growth_digest=service,
        now_fn=lambda: NOW,
    )
    return controller, database, telegram, service


def _buttons(message):
    markup = message[2].get("reply_markup") or {}
    return [
        button
        for row in markup.get("inline_keyboard", [])
        for button in row
    ]


def test_manual_command_and_scheduled_push_share_one_compact_formatter(tmp_path):
    db = Database(str(tmp_path / "digest.db"))
    digest = _seed_digest(db)
    controller, _db, telegram, service = _controller(tmp_path, digest, db=db)

    assert controller.process_update(_message_update(1, "/growth")) == "processed"

    assert service.calls == [NOW]
    assert len(telegram.messages) == 1
    assert "Account: 1" in telegram.messages[0][1]
    assert "Post: 1" in telegram.messages[0][1]
    assert "Da rivalutare: 1" in telegram.messages[0][1]
    assert [button["text"] for button in _buttons(telegram.messages[0])] == [
        "Account", "Post", "Da rivalutare",
    ]
    assert len(telegram.messages[0][1]) <= 4096
    assert all(
        len(button["callback_data"].encode("utf-8")) <= 64
        for button in _buttons(telegram.messages[0])
    )
    assert controller.push_growth_digest(digest, explicit=True) == "growth_digest"
    assert telegram.messages[1][1:] == telegram.messages[0][1:]


def test_scheduled_empty_or_replayed_digest_is_silent_but_manual_empty_is_explicit(
    tmp_path,
):
    empty_created = {
        "observed_on": "2026-08-26", "accounts": [], "posts": [],
        "reevaluate": [], "outcome": "created",
    }
    controller, _db, telegram, _service = _controller(tmp_path, empty_created)

    assert controller.push_growth_digest(empty_created, explicit=False) == (
        "growth_digest_silent"
    )
    assert telegram.messages == []
    assert controller.push_growth_digest(
        {**empty_created, "outcome": "existing"}, explicit=False,
    ) == "growth_digest_silent"
    assert telegram.messages == []

    assert controller.process_update(_message_update(2, "/growth")) == "processed"
    assert telegram.messages[-1][1] == "Nessun nuovo suggerimento."


def test_category_navigation_reaches_every_persisted_suggestion(tmp_path):
    db = Database(str(tmp_path / "digest.db"))
    _seed_digest(db, kinds=("account",))
    second_payload, second_reasons = _payload("account", "102", "second_owner")
    with db._conn() as conn:
        conn.execute(
            """
            INSERT INTO growth_suggestions (
                observed_on, kind, object_id, username, payload_json, score,
                reason_codes_json, suggested_at, cooldown_until, rank_position
            ) VALUES (?, 'account', ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                "2026-08-26", "102", "second_owner",
                json.dumps(second_payload), 87, json.dumps(second_reasons),
                "2026-08-26T07:00:00+00:00",
                "2026-09-25T07:00:00+00:00",
            ),
        )
        conn.execute(
            "UPDATE growth_digest_runs SET summary_json = ?",
            (json.dumps({
                "observed_on": "2026-08-26",
                "counts": {"account": 2, "post": 0, "reevaluate": 0},
            }),),
        )
    digest = db.get_growth_digest("2026-08-26")
    controller, _db, telegram, _service = _controller(tmp_path, digest, db=db)
    controller.push_growth_digest(digest, explicit=True)

    controller.process_update(callback_update(
        5, _buttons(telegram.messages[-1])[0]["callback_data"],
    ))
    next_button = next(
        button for button in _buttons(telegram.messages[-1])
        if button["text"] == "Successivo"
    )
    controller.process_update(callback_update(6, next_button["callback_data"]))

    assert "@second_owner" in telegram.messages[-1][1]
    assert _buttons(telegram.messages[-1])[0]["url"] == (
        "https://x.com/second_owner"
    )


def test_account_detail_uses_public_url_and_local_idempotent_follow_ack(tmp_path):
    db = Database(str(tmp_path / "digest.db"))
    db.upsert_growth_candidate({
        "user_id": "101",
        "username": "studio_owner",
        "profile": {},
        "latest_post": None,
        "score": 88,
        "score_data": {},
        "discovery_source": "topic_search",
    })
    digest = _seed_digest(db, kinds=("account",))
    controller, _db, telegram, _service = _controller(tmp_path, digest, db=db)
    controller.push_growth_digest(digest, explicit=True)
    navigation = _buttons(telegram.messages[-1])[0]

    assert controller.process_update(callback_update(10, navigation["callback_data"])) == (
        "processed"
    )
    detail = telegram.messages[-1]
    assert "@studio_owner" in detail[1]
    assert "follower: 1200" in detail[1]
    assert "primary_operator_role" in detail[1]
    buttons = _buttons(detail)
    assert buttons[0] == {
        "text": "Apri account su X", "url": "https://x.com/studio_owner",
    }
    assert buttons[1]["text"] == "Segnala come seguito"
    assert "follow" not in buttons[1]["callback_data"].lower()

    acknowledgement = buttons[1]["callback_data"]
    assert controller.process_update(callback_update(11, acknowledgement)) == "processed"
    assert "nessuna azione" in telegram.messages[-1][1].lower()
    assert "X" in telegram.messages[-1][1]
    with db._conn() as conn:
        saved = conn.execute(
            "SELECT decision, revision FROM growth_suggestions"
        ).fetchone()
        candidate = conn.execute(
            "SELECT decision, manual_followed_at FROM growth_candidates "
            "WHERE user_id = '101'"
        ).fetchone()
    assert tuple(saved) == ("followed_manually", 1)
    assert candidate["decision"] == "followed_manually"
    assert candidate["manual_followed_at"] == NOW.astimezone(timezone.utc).isoformat()

    restarted = TelegramController(
        FakeTelegramApi(tmp_path / "restart-media"), Database(db.db_path),
        NoopNotifier(), "42", growth_digest=FixedDigest(digest), now_fn=lambda: NOW,
    )
    assert restarted.process_update(callback_update(12, acknowledgement)) == "processed"
    assert "nessuna azione" in restarted.telegram_api.messages[-1][1].lower()
    with db._conn() as conn:
        saved = conn.execute(
            "SELECT decision, revision FROM growth_suggestions"
        ).fetchone()
    assert tuple(saved) == ("followed_manually", 1)


def test_post_and_reevaluation_details_never_offer_x_write_callbacks(tmp_path):
    db = Database(str(tmp_path / "digest.db"))
    digest = _seed_digest(db, kinds=("post", "reevaluate"))
    controller, _db, telegram, _service = _controller(tmp_path, digest, db=db)
    controller.push_growth_digest(digest, explicit=True)
    navigation = {button["text"]: button for button in _buttons(telegram.messages[-1])}

    controller.process_update(callback_update(20, navigation["Post"]["callback_data"]))
    post_buttons = _buttons(telegram.messages[-1])
    assert post_buttons == [{
        "text": "Apri post su X",
        "url": "https://x.com/gym_writer/status/7001",
    }]
    assert "like" not in json.dumps(post_buttons).lower()

    controller.process_update(
        callback_update(21, navigation["Da rivalutare"]["callback_data"])
    )
    reevaluate_buttons = _buttons(telegram.messages[-1])
    assert reevaluate_buttons[0] == {
        "text": "Apri account su X", "url": "https://x.com/old_contact",
    }
    assert [button["text"] for button in reevaluate_buttons[1:]] == [
        "Segna ancora pertinente", "Ignora suggerimento",
    ]
    assert "unfollow" not in json.dumps(reevaluate_buttons).lower()

    controller.process_update(
        callback_update(22, reevaluate_buttons[1]["callback_data"])
    )
    with db._conn() as conn:
        decision = conn.execute(
            "SELECT decision FROM growth_suggestions WHERE kind = 'reevaluate'"
        ).fetchone()[0]
    assert decision == "still_relevant"


def test_reevaluation_dismiss_is_local_and_wrong_revision_fails_closed(tmp_path):
    db = Database(str(tmp_path / "digest.db"))
    digest = _seed_digest(db, kinds=("reevaluate",))
    controller, _db, telegram, _service = _controller(tmp_path, digest, db=db)
    controller.push_growth_digest(digest, explicit=True)
    navigation = _buttons(telegram.messages[-1])[0]["callback_data"]
    controller.process_update(callback_update(23, navigation))
    dismiss = _buttons(telegram.messages[-1])[2]["callback_data"]

    assert controller.process_update(callback_update(24, dismiss)) == "processed"
    with db._conn() as conn:
        assert conn.execute(
            "SELECT decision FROM growth_suggestions"
        ).fetchone()[0] == "dismissed"

    prior_messages = len(telegram.messages)
    assert controller.process_update(callback_update(25, "gd:r:1:0")) == "processed"
    assert len(telegram.messages) == prior_messages + 1
    assert "non valido o scaduto" in telegram.messages[-1][1]
    assert not _buttons(telegram.messages[-1])


def test_follow_ack_has_one_sqlite_winner_and_rolls_back_on_error(tmp_path):
    path = str(tmp_path / "digest.db")
    db = Database(path)
    digest = _seed_digest(db, kinds=("account",))
    suggestion = digest["accounts"][0]

    def acknowledge():
        return Database(path).mark_growth_suggestion_decision(
            suggestion["id"], suggestion["revision"], "followed_manually",
            decided_at=NOW,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(lambda _index: acknowledge(), range(2)))
    assert outcomes == ["duplicate", "updated"]
    with db._conn() as conn:
        assert tuple(conn.execute(
            "SELECT decision, revision FROM growth_suggestions"
        ).fetchone()) == ("followed_manually", 1)

    failing_path = str(tmp_path / "failing.db")
    failing = Database(failing_path)
    failing_digest = _seed_digest(failing, kinds=("account",))
    failing_suggestion = failing_digest["accounts"][0]
    with failing._conn() as conn:
        conn.execute("""
            CREATE TRIGGER fail_growth_ack BEFORE UPDATE ON growth_suggestions
            BEGIN SELECT RAISE(ABORT, 'forced failure'); END
        """)
    try:
        failing.mark_growth_suggestion_decision(
            failing_suggestion["id"], failing_suggestion["revision"],
            "followed_manually", decided_at=NOW,
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("forced SQLite failure must escape")
    with failing._conn() as conn:
        assert tuple(conn.execute(
            "SELECT decision, revision FROM growth_suggestions"
        ).fetchone()) == ("new", 0)


def test_unauthorized_growth_command_and_callback_reveal_nothing(tmp_path):
    db = Database(str(tmp_path / "digest.db"))
    digest = _seed_digest(db, kinds=("account",))
    controller, _db, telegram, service = _controller(tmp_path, digest, db=db)

    assert controller.process_update(_message_update(30, "/growth", 999)) == (
        "unauthorized"
    )
    assert controller.process_update(callback_update(31, "gd:a:1:0", 999)) == (
        "unauthorized"
    )
    assert service.calls == []
    assert telegram.messages == []
    assert telegram.callback_answers == []
