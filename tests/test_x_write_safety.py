import importlib
import inspect
import ast
from pathlib import Path

import pytest

import config
import dotenv
from modules.twitter_client import TwitterClient
from tests.fakes import FakeXClient


def _set_valid_runtime_environment(monkeypatch):
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
    monkeypatch.setenv("APPROVAL_REQUIRED", "true")
    monkeypatch.setenv("NEWS_TRUSTED_DOMAINS", "")


def test_twitter_client_exposes_only_approved_post_write():
    prohibited = {
        "like_tweet",
        "follow_user",
        "unfollow_user",
        "reply_to_tweet",
        "retweet",
        "send_dm",
    }
    assert prohibited.isdisjoint(set(dir(TwitterClient)))
    assert hasattr(TwitterClient, "post_tweet")
    assert not hasattr(TwitterClient, "upload_media")
    assert "reply_to" not in inspect.signature(TwitterClient.post_tweet).parameters


def test_rollout_defaults_are_safe(monkeypatch):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda: False)
    for name in (
        "DRY_RUN",
        "APPROVAL_REQUIRED",
        "BOT_TIMEZONE",
        "CONTENT_SLOTS",
        "DRAFT_SCORE_THRESHOLD",
        "MAX_LINKS_PER_WEEK",
    ):
        monkeypatch.delenv(name, raising=False)
    reloaded = importlib.reload(config)
    assert reloaded.DRY_RUN is True
    assert reloaded.APPROVAL_REQUIRED is True
    assert reloaded.BOT_TIMEZONE == "Europe/Rome"
    assert reloaded.CONTENT_SLOTS == ["14:00", "20:00"]
    assert reloaded.DRAFT_SCORE_THRESHOLD == 70
    assert reloaded.MAX_LINKS_PER_WEEK == 1


@pytest.mark.parametrize("raw_value", ["", "typo", "yes", "0", " true ", "TRUE"])
def test_invalid_dry_run_value_falls_back_safe_and_validation_rejects(
    monkeypatch,
    raw_value,
):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda: False)
    _set_valid_runtime_environment(monkeypatch)
    monkeypatch.setenv("DRY_RUN", raw_value)

    reloaded = importlib.reload(config)

    assert reloaded.DRY_RUN is True
    with pytest.raises(ValueError, match="DRY_RUN"):
        reloaded.validate_config()

    monkeypatch.setenv("DRY_RUN", "true")
    importlib.reload(config)


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [("true", True), ("false", False)],
)
def test_dry_run_accepts_only_canonical_boolean_values(
    monkeypatch,
    raw_value,
    expected,
):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda: False)
    _set_valid_runtime_environment(monkeypatch)
    monkeypatch.setenv("DRY_RUN", raw_value)

    reloaded = importlib.reload(config)

    assert reloaded.DRY_RUN is expected
    reloaded.validate_config()

    monkeypatch.setenv("DRY_RUN", "true")
    importlib.reload(config)


def test_invalid_approval_required_value_stays_enabled_but_is_rejected(
    monkeypatch,
):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda: False)
    _set_valid_runtime_environment(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("APPROVAL_REQUIRED", "TRUE")

    reloaded = importlib.reload(config)

    assert reloaded.APPROVAL_REQUIRED is True
    with pytest.raises(ValueError, match="APPROVAL_REQUIRED"):
        reloaded.validate_config()

    monkeypatch.setenv("APPROVAL_REQUIRED", "true")
    importlib.reload(config)


def test_validation_rechecks_boolean_environment_after_import(monkeypatch):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda: False)
    _set_valid_runtime_environment(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "false")
    reloaded = importlib.reload(config)
    assert reloaded.DRY_RUN is False

    monkeypatch.setenv("DRY_RUN", "typo-after-import")

    with pytest.raises(ValueError, match="DRY_RUN"):
        reloaded.validate_config()

    monkeypatch.setenv("DRY_RUN", "true")
    importlib.reload(config)


def test_character_contains_no_invented_bug_example():
    character_text = open("character.json", encoding="utf-8").read().lower()
    prohibited = ("absurd bugs", "stripe webhook", "bugs fixed", "rough day")
    assert all(term not in character_text for term in prohibited)


@pytest.mark.parametrize(
    "module_name",
    [
        "modules.editorial_feed",
        "modules.source_ingestion",
        "modules.source_refresh",
        "modules.content_planner",
    ],
)
def test_source_refresh_modules_have_no_x_write_capability(module_name):
    source = inspect.getsource(importlib.import_module(module_name)).lower()
    prohibited = (
        "post_tweet",
        "create_tweet",
        "like_tweet",
        "follow_user",
        "unfollow_user",
        "reply_to_tweet",
        "retweet",
        "send_dm",
        "upload_media",
        "media_upload",
    )
    assert all(capability not in source for capability in prohibited)


@pytest.mark.parametrize(
    "method_name",
    [
        "like_tweet", "favorite_tweet", "follow_user", "unfollow_user",
        "create_friendship", "destroy_friendship", "reply_to_tweet",
        "retweet", "repost", "send_dm", "send_direct_message",
        "bookmark_tweet", "write", "engage",
    ],
)
def test_runtime_x_fake_raises_if_any_engagement_write_is_touched(method_name):
    fake = FakeXClient()
    with pytest.raises(AssertionError, match="read-only X boundary"):
        getattr(fake, method_name)("target")
    assert fake.engagement_writes == []


def test_production_ast_allows_only_the_approved_x_publication_boundary():
    root = Path(__file__).resolve().parents[1]
    production_files = [root / "main.py", *sorted((root / "modules").glob("*.py"))]
    prohibited = {
        "like_tweet", "favorite_tweet", "follow_user", "unfollow_user",
        "create_friendship", "destroy_friendship", "reply_to_tweet",
        "retweet", "repost", "send_dm", "send_direct_message",
        "bookmark_tweet", "engage",
    }
    approved_locations = {
        "post_tweet": {"modules/publisher.py"},
        "create_tweet": {"modules/twitter_client.py"},
        "_upload_media": {"modules/twitter_client.py"},
        "media_upload": {"modules/twitter_client.py"},
    }
    found = {name: set() for name in approved_locations}
    violations = []
    for path in production_files:
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if node.attr in prohibited:
                violations.append((relative, node.lineno, node.attr))
            if node.attr in approved_locations:
                found[node.attr].add(relative)
    assert violations == []
    assert found == approved_locations
