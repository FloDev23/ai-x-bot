import json
from datetime import datetime, timezone

import pytest

from modules.database import Database
from modules.growth_replies import GrowthReplyService, validate_reply_draft
from modules.telegram_controller import TelegramController
from tests.fakes import FakeTelegramApi, callback_update


NOW = datetime(2026, 10, 5, 7, 0, tzinfo=timezone.utc)
OBSERVED_ON = "2026-10-05"
GOOD = {
    "post_it": "Giorno 2 da solo founder: 0 utenti. Come trovate i primi 10?",
    "reply_en": "What worked for me was showing up in person where my users already are. Which channel are you trying first?",
    "reply_it": "A me ha funzionato presentarmi di persona dove sono già i miei utenti. Quale canale provi per primo?",
}


class FakeGenerator:
    def __init__(self, drafts=None):
        self.drafts = list(drafts or [])
        self.calls = []

    def draft_founder_reply(self, post_text):
        self.calls.append(post_text)
        return self.drafts.pop(0) if self.drafts else dict(GOOD)


class NoopNotifier:
    def notify_error(self, operation, error):
        pass


def seed_posts(db, sources):
    with db._conn() as conn:
        for rank, source in enumerate(sources):
            object_id = str(9000 + rank)
            reasons = [source, "recent"]
            payload = {
                "id": object_id,
                "author_id": str(500 + rank),
                "author_username": f"founder_{rank}",
                "excerpt": f"Day {rank} building in public. How do you get users?",
                "created_at": "2026-10-05T05:00:00+00:00",
                "public_metrics": {
                    "like_count": 3, "retweet_count": 0, "reply_count": 1,
                    "quote_count": 0, "impression_count": 90,
                },
                "reason_codes": reasons,
            }
            conn.execute(
                """
                INSERT INTO growth_suggestions (
                    observed_on, kind, object_id, username, payload_json,
                    score, reason_codes_json, suggested_at, cooldown_until,
                    rank_position
                ) VALUES (?, 'post', ?, ?, ?, 80, ?, ?, ?, ?)
                """,
                (
                    OBSERVED_ON, object_id, f"founder_{rank}",
                    json.dumps(payload), json.dumps(reasons),
                    "2026-10-05T07:00:00+00:00", "2026-11-04T07:00:00+00:00",
                    rank,
                ),
            )
        conn.execute(
            "INSERT INTO growth_digest_runs VALUES (?, ?, ?)",
            (
                OBSERVED_ON, "2026-10-05T07:00:00+00:00",
                json.dumps({
                    "observed_on": OBSERVED_ON,
                    "counts": {"account": 0, "post": len(sources), "reevaluate": 0},
                }),
            ),
        )
    return db.get_growth_digest(OBSERVED_ON)


@pytest.mark.parametrize(
    "change",
    [
        {"reply_en": "Check https://example.com for how we did it, it worked well."},
        {"reply_en": "We learned this the hard way #buildinpublic, keep going."},
        {"reply_en": "Ask @levelsio, he covered exactly this in a thread."},
        {"reply_en": "FlexDropin had the same problem before our first users."},
        {"reply_en": "Nice!"},
        {"reply_en": "The onboarding felt clear—what made you pause on pricing?"},
        {"reply_en": "x" * 241},
        {"reply_it": ""},
        {"post_it": None},
    ],
)
def test_reply_validation_rejects_promo_links_tags_and_bad_lengths(change):
    assert validate_reply_draft({**GOOD, **change}) is None


def test_reply_validation_normalizes_whitespace_and_quotes():
    draft = validate_reply_draft({**GOOD, "reply_en": '  "Ship it,\n then ask."  '})

    assert draft["reply_en"] == "Ship it, then ask."


def test_build_prefers_founder_conversations_and_respects_daily_limit(tmp_path):
    db = Database(str(tmp_path / "replies.db"))
    seed_posts(db, [
        "followed_account", "suggested_account", "founder_conversation",
        "founder_conversation", "suggested_account", "founder_conversation",
    ])
    generator = FakeGenerator()

    drafts = GrowthReplyService(generator, db, daily_limit=4).build(OBSERVED_ON, NOW)

    assert [draft["tweet_id"] for draft in drafts] == ["9002", "9003", "9005", "9001"]
    assert drafts[0]["post_it"] == GOOD["post_it"]
    assert drafts[0]["status"] == "ready"
    assert len(generator.calls) == 4

    again = GrowthReplyService(generator, db, daily_limit=4).build(OBSERVED_ON, NOW)

    assert again == drafts
    assert len(generator.calls) == 4


def test_build_skips_invalid_generation_and_tries_next_post(tmp_path):
    db = Database(str(tmp_path / "invalid.db"))
    seed_posts(db, ["founder_conversation", "founder_conversation"])
    generator = FakeGenerator([{**GOOD, "reply_en": "Great post!"}, None])

    assert GrowthReplyService(generator, db).build(OBSERVED_ON, NOW) == []

    generator.drafts = []
    drafts = GrowthReplyService(generator, db).build(OBSERVED_ON, NOW)
    assert [draft["tweet_id"] for draft in drafts] == ["9000", "9001"]


def test_regenerate_replaces_text_until_generation_limit(tmp_path):
    db = Database(str(tmp_path / "regenerate.db"))
    seed_posts(db, ["founder_conversation"])
    replacement = {**GOOD, "reply_en": "Have you tried asking ten users what they would pay for?"}
    service = GrowthReplyService(FakeGenerator([dict(GOOD), replacement, dict(GOOD)]), db)
    draft = service.build(OBSERVED_ON, NOW)[0]

    updated, outcome = service.regenerate(draft["id"], draft["revision"], NOW)
    assert outcome == "updated"
    assert updated["reply_en"] == replacement["reply_en"]
    assert service.regenerate(draft["id"], draft["revision"], NOW)[1] == "rejected"

    updated, outcome = service.regenerate(updated["id"], updated["revision"], NOW)
    assert (outcome, updated["generation_count"]) == ("updated", 3)
    assert service.regenerate(updated["id"], updated["revision"], NOW)[1] == "rejected"


def _controller(tmp_path, db, service):
    telegram = FakeTelegramApi(tmp_path / "media")
    controller = TelegramController(
        telegram, db, NoopNotifier(), "42",
        growth_replies=service, now_fn=lambda: NOW,
    )
    return controller, telegram


def _buttons(message):
    return [
        button
        for row in message[2]["reply_markup"]["inline_keyboard"]
        for button in row
    ]


def test_digest_summary_links_reply_cards_with_italian_and_copy(tmp_path):
    db = Database(str(tmp_path / "telegram.db"))
    digest = seed_posts(db, ["founder_conversation", "founder_conversation"])
    service = GrowthReplyService(FakeGenerator(), db)
    service.build(OBSERVED_ON, NOW)
    controller, telegram = _controller(tmp_path, db, service)

    assert controller.push_growth_digest(
        {**digest, "outcome": "created"}, explicit=False,
    ) == "growth_digest"
    summary = telegram.messages[-1]
    assert "Risposte da scrivere: 2" in summary[1]
    replies_button = next(b for b in _buttons(summary) if b["text"] == "Risposte")

    controller.process_update(callback_update(1, replies_button["callback_data"]))
    card = telegram.messages[-1]
    assert "Risposta a @founder_0" in card[1]
    assert "🇮🇹 " + GOOD["post_it"] in card[1]
    assert GOOD["reply_en"] in card[1]
    assert "🇮🇹 " + GOOD["reply_it"] in card[1]
    buttons = {button["text"]: button for button in _buttons(card)}
    assert buttons["Copia risposta"]["copy_text"] == {"text": GOOD["reply_en"]}
    assert buttons["Apri post"]["url"] == "https://x.com/founder_0/status/9000"
    assert {"Pubblicata", "Salta", "Rigenera", "Successiva"} <= set(buttons)

    controller.process_update(callback_update(2, buttons["Pubblicata"]["callback_data"]))
    assert "Stato: pubblicata" in telegram.messages[-1][1]
    assert db.list_growth_reply_drafts(OBSERVED_ON)[0]["status"] == "posted_manually"
    assert "Pubblicata" not in {b["text"] for b in _buttons(telegram.messages[-1])}

    controller.process_update(callback_update(3, buttons["Pubblicata"]["callback_data"]))
    assert db.list_growth_reply_drafts(OBSERVED_ON)[0]["revision"] == 1


def test_regenerate_button_sends_updated_card(tmp_path):
    db = Database(str(tmp_path / "regen-telegram.db"))
    seed_posts(db, ["founder_conversation"])
    replacement = {**GOOD, "reply_en": "How are you deciding which feature to cut first?"}
    service = GrowthReplyService(FakeGenerator([dict(GOOD), replacement]), db)
    draft = service.build(OBSERVED_ON, NOW)[0]
    controller, telegram = _controller(tmp_path, db, service)

    controller.process_update(callback_update(
        1, f"gra:g:{draft['id']}:{draft['revision']}",
    ))

    assert replacement["reply_en"] in telegram.messages[-1][1]
