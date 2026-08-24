import json
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import config
import main
from main import FlexDropinGrowthAgent
from modules.database import Database
from tests.fakes import (
    FakeEditorialScorer,
    FakeGroundedGenerator,
    FakeNewsFetcher,
    FakeScheduler,
    FakeTelegramApi,
    FakeXClient,
    callback_update,
    photo_update,
)


ROME = ZoneInfo("Europe/Rome")
NOW = datetime(2026, 8, 11, 10, 0, tzinfo=ROME)


class NoopLeadFinder:
    def __init__(self):
        self.calls = 0

    def find_opportunities(self, **_kwargs):
        self.calls += 1
        return []


class FalseyProxy:
    """Delegate a boundary protocol while remaining false in boolean context."""

    def __init__(self, target):
        self.target = target

    def __bool__(self):
        return False

    def __getattr__(self, name):
        return getattr(self.target, name)


class CandidateGenerator(FakeGroundedGenerator):
    def __init__(self, texts):
        super().__init__()
        self.texts = tuple(texts)

    def generate_grounded_tweet(
        self, _category, _sources, _include_link, candidate_index=None
    ):
        self.candidate_indices.append(candidate_index)
        return {"text": self.texts[candidate_index]}


class CandidateScorer:
    def __init__(self, scores):
        self.scores = dict(scores)

    def score_draft(self, text, sources=None, recent_texts=None):
        del sources, recent_texts
        return {"total": self.scores[text]}


class BarrierDraftDatabase(Database):
    def __init__(self, path, barrier):
        self.barrier = barrier
        super().__init__(path)

    def create_or_get_post_draft(self, **values):
        self.barrier.wait(timeout=5)
        return super().create_or_get_post_draft(**values)


def _candidate_agent(tmp_path, texts, scores):
    generator = CandidateGenerator(texts)
    dependencies = dependency_bundle(
        tmp_path,
        generator=generator,
        scorer=CandidateScorer(scores),
    )
    agent = FlexDropinGrowthAgent(dependencies)
    agent.db.add_content_source(
        "founder_note",
        texts[1],
        metadata={"publishable": True},
        verified_by="floriano",
    )
    return agent, dependencies, generator


def dependency_bundle(tmp_path, **overrides):
    media_root = tmp_path / "media"
    media_root.mkdir(mode=0o700, parents=True)
    db = Database(str(tmp_path / "agent.db"))
    db.set_state("paused", "false")
    dependencies = {
        "db": db,
        "telegram_api": FakeTelegramApi(media_root),
        "x_client": FakeXClient(),
        "generator": FakeGroundedGenerator(),
        "scorer": FakeEditorialScorer(),
        "news_fetcher": FakeNewsFetcher(),
        "scheduler": FakeScheduler(),
        "lead_finder": NoopLeadFinder(),
        "lead_cycle_times": ("10:00", "16:00"),
        "clock": lambda: NOW,
        "authorized_chat_id": "42",
        "media_library_dir": str(media_root),
        "timezone_name": "Europe/Rome",
        "content_slots": ("14:00", "20:00"),
        "draft_lead_minutes": 120,
        "dry_run": True,
        "lead_discovery_enabled": False,
    }
    dependencies.update(overrides)
    return dependencies


@pytest.fixture
def agent_and_fakes(tmp_path, monkeypatch):
    dependencies = dependency_bundle(tmp_path)
    monkeypatch.setattr(
        main,
        "validate_config",
        lambda: (_ for _ in ()).throw(AssertionError("validation must be skipped")),
    )
    agent = FlexDropinGrowthAgent(dependencies=dependencies)
    return agent, dependencies


def test_source_to_approval_to_dry_run_without_external_writes(agent_and_fakes):
    agent, fakes = agent_and_fakes
    source_id = agent.db.add_content_source(
        "founder_note",
        "I decided to reduce posting frequency so every post earns attention.",
        metadata={"publishable": True},
        verified_by="floriano",
    )
    assert source_id > 0

    draft = agent.create_draft_cycle("14:00", now=NOW)
    assert draft["status"] == "pending_approval"
    assert any(
        "Bozza #" in text
        for _chat_id, text, _kwargs in fakes["telegram_api"].messages
    )
    assert agent.telegram_controller.process_update(
        callback_update(301, "draft:approve:" + str(draft["id"])),
    ) == "processed"

    result = agent.publish_cycle("14:00", now=NOW.replace(hour=14))
    assert result.status == "dry_run"
    assert fakes["x_client"].posts == []
    assert fakes["x_client"].engagement_writes == []


def test_candidate_tournament_sends_one_winner_card_without_x_write(tmp_path):
    winner_texts = (
        "I keep the first candidate out of the approval queue.",
        "I send only the strongest candidate for approval.",
        "I keep the third candidate out of the approval queue.",
    )
    winner_agent, winner_dependencies, winner_generator = _candidate_agent(
        tmp_path,
        winner_texts,
        {
            winner_texts[0]: 79,
            winner_texts[1]: 94,
            winner_texts[2]: 83,
        },
    )

    draft = winner_agent.create_draft_cycle("14:00", now=NOW)

    winner_messages = winner_dependencies["telegram_api"].messages
    assert draft["text"] == winner_texts[1]
    assert draft["score_data"] == {"total": 94}
    assert winner_generator.candidate_indices == [0, 1, 2]
    assert len(winner_messages) == 1
    assert winner_texts[1] in winner_messages[0][1]
    assert winner_dependencies["x_client"].posts == []


def test_concurrent_agents_and_replay_send_one_card_for_shared_sqlite_draft(
    tmp_path,
):
    shared_path = str(tmp_path / "shared-agent.db")
    setup = Database(shared_path)
    setup.set_state("paused", "false")
    text = "I send one shared SQLite winner for approval."
    setup.add_content_source(
        "founder_note",
        text,
        metadata={"publishable": True},
        verified_by="floriano",
    )
    barrier = threading.Barrier(2)
    media_root = tmp_path / "shared-media"
    media_root.mkdir(mode=0o700)
    telegram = FakeTelegramApi(media_root)
    agents = []
    dependencies = []
    for index in range(2):
        database = BarrierDraftDatabase(shared_path, barrier)
        bundle = dependency_bundle(
            tmp_path / f"agent-{index}",
            db=database,
            telegram_api=telegram,
            generator=CandidateGenerator((text, text, text)),
        )
        dependencies.append(bundle)
        agents.append(FlexDropinGrowthAgent(bundle))

    results = []
    errors = []

    def worker(agent):
        try:
            results.append(agent.create_draft_cycle("14:00", now=NOW))
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=worker, args=(agent,), daemon=True)
        for agent in agents
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not [thread for thread in threads if thread.is_alive()]
    assert errors == []
    assert len(results) == 2
    assert all(result is not None for result in results)
    assert len({result["id"] for result in results}) == 1
    assert len(telegram.messages) == 1
    assert text in telegram.messages[0][1]
    assert agents[0].create_draft_cycle("14:00", now=NOW)["id"] == results[0]["id"]
    assert len(telegram.messages) == 1
    assert all(bundle["x_client"].posts == [] for bundle in dependencies)
    with setup._conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM post_drafts"
        ).fetchone()[0] == 1


def test_candidate_tournament_accepts_exact_threshold_and_sends_one_card(
    tmp_path,
):
    texts = (
        "I leave the 73-point candidate out of the approval queue.",
        "I send the exact-threshold candidate for approval.",
        "I leave the 74-point candidate out of the approval queue.",
    )
    agent, dependencies, generator = _candidate_agent(
        tmp_path,
        texts,
        {
            texts[0]: 73,
            texts[1]: 75,
            texts[2]: 74,
        },
    )

    draft = agent.create_draft_cycle("14:00", now=NOW)

    assert draft["text"] == texts[1]
    assert draft["score_data"] == {"total": 75}
    assert draft["status"] == "pending_approval"
    assert generator.candidate_indices == [0, 1, 2]
    assert len(dependencies["telegram_api"].messages) == 1
    assert texts[1] in dependencies["telegram_api"].messages[0][1]
    assert dependencies["x_client"].posts == []


def test_candidate_tournament_below_threshold_has_no_side_effects(tmp_path):
    low_texts = (
        "I leave low candidate one unpersisted.",
        "I leave low candidate two unpersisted.",
        "I leave low candidate three unpersisted.",
    )
    low_agent, low_dependencies, low_generator = _candidate_agent(
        tmp_path,
        low_texts,
        {
            low_texts[0]: 72,
            low_texts[1]: 74,
            low_texts[2]: 71,
        },
    )

    assert low_agent.create_draft_cycle("14:00", now=NOW) is None

    assert low_generator.candidate_indices == [0, 1, 2]
    assert low_dependencies["telegram_api"].messages == []
    assert low_dependencies["x_client"].posts == []
    assert low_agent.db.list_post_drafts() == []
    with low_agent.db._conn() as conn:
        evaluations = conn.execute(
            "SELECT outcome, details_json FROM draft_evaluations"
        ).fetchall()
        error_count = conn.execute(
            "SELECT COUNT(*) FROM error_events"
        ).fetchone()[0]
    assert [
        (row["outcome"], json.loads(row["details_json"])["scores"]["total"])
        for row in evaluations
    ] == [("rejected_score", 74)]
    assert error_count == 0


def test_scheduled_publish_uses_one_effective_clock_read_for_grace_expiry(
    tmp_path,
    monkeypatch,
):
    late = NOW.replace(hour=14, minute=10)

    class CountingClock:
        def __init__(self):
            self.calls = 0
            self.value = NOW

        def __call__(self):
            self.calls += 1
            return self.value

    clock = CountingClock()
    monkeypatch.setattr(main, "PUBLISH_GRACE_SECONDS", 300)
    agent = FlexDropinGrowthAgent(dependency_bundle(tmp_path, clock=clock))
    source_id = agent.db.add_content_source(
        "founder_note",
        "I decided to reduce posting frequency so every post earns attention.",
        metadata={"publishable": True},
        verified_by="floriano",
    )
    assert source_id > 0
    draft = agent.create_draft_cycle("14:00", now=NOW)
    assert agent.telegram_controller.process_update(
        callback_update(303, "draft:approve:" + str(draft["id"])),
    ) == "processed"
    clock.value = late
    clock.calls = 0

    result = agent.publish_cycle("14:00")

    assert result.status == "expired"
    assert clock.calls == 1
    assert agent.db.get_post_draft(draft["id"])["status"] == "expired"


def test_media_upload_does_not_create_draft(agent_and_fakes):
    agent, fakes = agent_and_fakes
    before = len(agent.db.list_post_drafts())

    assert agent.telegram_controller.process_update(
        photo_update(302, "Future studio content"),
    ) == "processed"

    assert len(agent.db.list_post_drafts()) == before
    assert len(agent.db.get_available_media()) == 1
    assert fakes["x_client"].posts == []
    assert fakes["x_client"].engagement_writes == []


def test_register_jobs_uses_rome_timezone_and_only_safe_jobs(agent_and_fakes):
    agent, fakes = agent_and_fakes

    jobs = agent.register_jobs()

    assert {job.id for job in jobs} == {
        "verified_news_refresh",
        "draft_14:00",
        "draft_20:00",
        "publish_14:00",
        "publish_20:00",
        "growth_discovery",
        "follower_snapshot",
        "performance_metrics",
        "weekly_growth_report",
    }
    assert all(job.trigger.timezone.key == "Europe/Rome" for job in jobs)
    schedule = {
        job.id: (
            str(job.trigger.fields[4]),
            str(job.trigger.fields[5]),
            str(job.trigger.fields[6]),
        )
        for job in jobs
    }
    assert schedule == {
        "verified_news_refresh": ("*", "10", "30"),
        "draft_14:00": ("*", "12", "0"),
        "draft_20:00": ("*", "18", "0"),
        "publish_14:00": ("*", "14", "0"),
        "publish_20:00": ("*", "20", "0"),
        "growth_discovery": ("*", "11", "0"),
        "follower_snapshot": ("*", "23", "15"),
        "performance_metrics": ("*", "23", "30"),
        "weekly_growth_report": ("mon", "9", "0"),
    }
    assert not any(job.id.startswith(
        ("engagement_", "follow_", "unfollow_", "human_", "build_")
    ) for job in jobs)
    assert fakes["scheduler"].get_jobs() == jobs


def test_status_renders_scheduler_next_run_contract(agent_and_fakes):
    agent, fakes = agent_and_fakes
    jobs = agent.register_jobs()
    jobs[0].next_run_time = datetime(2026, 8, 11, 10, 30, tzinfo=ROME)

    assert agent.telegram_controller.process_update({
        "update_id": 304,
        "message": {"chat": {"id": 42}, "text": "/status"},
    }) == "processed"

    rendered = fakes["telegram_api"].messages[-1][1]
    assert "- Verified news refresh: 2026-08-11T10:30:00+02:00" in rendered


def test_lead_jobs_are_registered_only_when_explicitly_enabled(tmp_path):
    disabled = FlexDropinGrowthAgent(dependency_bundle(tmp_path / "off"))
    assert not any(job.id.startswith("lead_discovery_") for job in disabled.register_jobs())

    enabled = FlexDropinGrowthAgent(dependency_bundle(
        tmp_path / "on", lead_discovery_enabled=True,
    ))
    lead_jobs = [
        job for job in enabled.register_jobs()
        if job.id.startswith("lead_discovery_")
    ]
    assert {job.id for job in lead_jobs} == {
        "lead_discovery_10:00",
        "lead_discovery_16:00",
    }


def test_polling_thread_is_named_daemon_and_shutdown_uses_shared_event(tmp_path):
    entered = threading.Event()

    class BlockingController:
        def __init__(self):
            self.stop_event = None

        def run_forever(self, stop_event):
            self.stop_event = stop_event
            entered.set()
            stop_event.wait(5)

    dependencies = dependency_bundle(tmp_path)
    controller = BlockingController()
    dependencies["telegram_controller"] = controller
    agent = FlexDropinGrowthAgent(dependencies)

    agent.start(block=False)
    assert entered.wait(1)
    assert agent.telegram_thread.name == "flexdropin-telegram-polling"
    assert agent.telegram_thread.daemon is True
    assert controller.stop_event is agent.stop_event

    agent.shutdown()
    assert agent.stop_event.is_set()
    assert not agent.telegram_thread.is_alive()
    assert dependencies["scheduler"].shutdown_calls == [True]


def _set_base_required_environment(monkeypatch):
    for name in (
        "TWITTER_API_KEY",
        "TWITTER_API_SECRET",
        "TWITTER_ACCESS_TOKEN",
        "TWITTER_ACCESS_TOKEN_SECRET",
        "TWITTER_BEARER_TOKEN",
        "GROQ_API_KEY",
    ):
        monkeypatch.setenv(name, "configured")
    monkeypatch.setenv(
        "TELEGRAM_BOT_TOKEN",
        "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
    )
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
    monkeypatch.delenv("NEWSAPI_KEY", raising=False)


def test_validate_config_requires_telegram_and_keeps_approval_mandatory(monkeypatch):
    _set_base_required_environment(monkeypatch)
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    monkeypatch.setattr(config, "NEWS_TRUSTED_DOMAINS", set())
    monkeypatch.setattr(config, "APPROVAL_REQUIRED", True)
    with pytest.raises(ValueError, match="TELEGRAM_CHAT_ID"):
        config.validate_config()

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
    monkeypatch.setattr(config, "APPROVAL_REQUIRED", False)
    with pytest.raises(ValueError, match="APPROVAL_REQUIRED"):
        config.validate_config()


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("TELEGRAM_BOT_TOKEN", "   "),
        ("TELEGRAM_BOT_TOKEN", "not-a-telegram-token"),
        ("TELEGRAM_CHAT_ID", "   "),
        ("TELEGRAM_CHAT_ID", "chat-name"),
    ),
)
def test_validate_config_rejects_malformed_telegram_identity(
    monkeypatch,
    name,
    value,
):
    _set_base_required_environment(monkeypatch)
    monkeypatch.setattr(config, "APPROVAL_REQUIRED", True)
    monkeypatch.setattr(config, "NEWS_TRUSTED_DOMAINS", set())
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        config.validate_config()


def test_validate_config_requires_news_key_only_for_enabled_trusted_news(monkeypatch):
    _set_base_required_environment(monkeypatch)
    monkeypatch.setattr(config, "APPROVAL_REQUIRED", True)
    monkeypatch.setattr(config, "NEWS_TRUSTED_DOMAINS", set())
    config.validate_config()

    monkeypatch.setattr(config, "NEWS_TRUSTED_DOMAINS", {"industry.example"})
    with pytest.raises(ValueError, match="NEWSAPI_KEY"):
        config.validate_config()


def test_partial_dependency_injection_fails_without_real_client_fallback(
    tmp_path,
    monkeypatch,
):
    dependencies = dependency_bundle(tmp_path)
    del dependencies["x_client"]
    constructed = []

    def forbidden_x_client():
        constructed.append(True)
        raise AssertionError("real X client constructed")

    monkeypatch.setattr(main, "TwitterClient", forbidden_x_client)

    with pytest.raises(ValueError, match="x_client"):
        FlexDropinGrowthAgent(dependencies)
    assert constructed == []


@pytest.mark.parametrize(
    ("dependency_name", "agent_attribute"),
    (
        ("db", "db"),
        ("telegram_api", "telegram_api"),
        ("news_fetcher", "news_fetcher"),
        ("generator", "ai_generator"),
        ("x_client", "twitter_client"),
        ("scorer", "scorer"),
        ("lead_finder", "lead_finder"),
        ("scheduler", "scheduler"),
    ),
)
def test_falsey_required_boundary_is_used_without_production_fallback(
    tmp_path,
    monkeypatch,
    dependency_name,
    agent_attribute,
):
    dependencies = dependency_bundle(tmp_path)
    injected = FalseyProxy(dependencies[dependency_name])
    dependencies[dependency_name] = injected

    def forbidden_boundary(*_args, **_kwargs):
        raise AssertionError("production boundary constructed")

    for factory_name in (
        "Database",
        "TelegramApi",
        "NewsFetcher",
        "AIGenerator",
        "TwitterClient",
        "TweetScorer",
        "LeadFinder",
        "BackgroundScheduler",
    ):
        monkeypatch.setattr(main, factory_name, forbidden_boundary)

    agent = FlexDropinGrowthAgent(dependencies)

    assert getattr(agent, agent_attribute) is injected


@pytest.mark.parametrize(
    ("dependency_name", "agent_attribute"),
    (
        ("notifier", "notifier"),
        ("planner", "content_planner"),
        ("source_ingestor", "source_ingestor"),
        ("fact_guard", "fact_guard"),
        ("draft_pipeline", "draft_pipeline"),
        ("media_processor", "media_processor"),
        ("media_matcher", "media_matcher"),
        ("publisher", "publisher"),
        ("analytics", "analytics"),
        ("growth_discovery", "growth_discovery"),
        ("telegram_controller", "telegram_controller"),
    ),
)
def test_falsey_component_override_is_preserved(
    tmp_path,
    dependency_name,
    agent_attribute,
):
    dependencies = dependency_bundle(tmp_path)
    baseline = FlexDropinGrowthAgent(dependencies)
    injected = FalseyProxy(getattr(baseline, agent_attribute))
    dependencies[dependency_name] = injected

    agent = FlexDropinGrowthAgent(dependencies)

    assert getattr(agent, agent_attribute) is injected
