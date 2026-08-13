import json
import inspect
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.database import Database
from modules.draft_pipeline import DraftPipeline
from modules.fact_guard import FactCheckResult
from modules.telegram_api import TelegramApi
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
        self.callback_answers = []

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append((str(chat_id), text, kwargs))
        return {"message_id": len(self.messages)}

    def answer_callback(self, callback_id, **kwargs):
        self.callback_answers.append((callback_id, kwargs))
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

    def postpone(self, draft_id, slot):
        self.calls.append(("postpone", draft_id, slot))
        return True

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
    candidate_id = db.upsert_growth_candidate({
        "user_id": "900",
        "username": "real_owner",
        "profile": {
            "username": "real_owner",
            "description": "Independent gym owner",
            "followers_count": 1200,
        },
        "latest_post": {"id": "12345", "text": "A useful studio update"},
        "score": 91,
        "score_data": {"relevance": 91},
        "discovery_source": "search",
        "profile_expires_at": "2030-08-20T00:00:00+00:00",
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
        "score_data": {"total": 90},
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
