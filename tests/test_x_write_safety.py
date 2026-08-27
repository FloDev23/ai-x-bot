import importlib
import inspect
import ast
from pathlib import Path

import pytest

import config
import dotenv
from modules.twitter_client import TwitterClient
from tests.fakes import FakeXClient


_PROHIBITED_X_CALLS = {
    "like_tweet", "favorite_tweet", "follow_user", "unfollow_user",
    "create_friendship", "destroy_friendship", "reply_to_tweet",
    "retweet", "repost", "send_dm", "send_direct_message",
    "bookmark_tweet", "engage", "write",
}
_APPROVED_X_CALLS = {
    "post_tweet": {"modules/publisher.py"},
    "create_tweet": {"modules/twitter_client.py"},
    "_upload_media": {"modules/twitter_client.py"},
    "media_upload": {"modules/twitter_client.py"},
}
_GENERIC_X_BOUNDARIES = {
    "x", "x_client", "twitter", "twitter_client", "client", "_client",
    "api", "_api",
}


def _attribute_parts(node):
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        parts = _attribute_parts(node.value)
        return [*parts, node.attr] if parts is not None else None
    return None


def _is_generic_x_boundary(node, receiver_aliases=frozenset()):
    parts = _attribute_parts(node)
    return bool(
        parts
        and (
            parts[-1] in receiver_aliases
            or parts[-1].lower() in _GENERIC_X_BOUNDARIES
        )
    )


def _getattr_capability(node, receiver_aliases=frozenset()):
    if (
        not isinstance(node, ast.Call)
        or not isinstance(node.func, ast.Name)
        or node.func.id != "getattr"
        or len(node.args) < 2
        or not _is_generic_x_boundary(node.args[0], receiver_aliases)
    ):
        return None
    method = node.args[1]
    if isinstance(method, ast.Constant) and type(method.value) is str:
        return method.value, True, False
    return "<dynamic>", True, True


def _capability_reference(node, aliases, receiver_aliases=frozenset()):
    if isinstance(node, ast.Attribute):
        return (
            node.attr,
            _is_generic_x_boundary(node.value, receiver_aliases),
            False,
        )
    dynamic = _getattr_capability(node, receiver_aliases)
    if dynamic is not None:
        return dynamic
    if isinstance(node, ast.Name):
        return aliases.get(node.id)
    return None


def _scan_x_calls(source, relative):
    """Find invoked X writes, including aliased and dynamic generic calls."""
    tree = ast.parse(source, filename=relative)
    receiver_aliases = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not _is_generic_x_boundary(value, receiver_aliases):
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in receiver_aliases:
                    receiver_aliases.add(target.id)
                    changed = True
    aliases = {}
    for node in ast.walk(tree):
        value = None
        targets = []
        if isinstance(node, ast.Assign):
            value = node.value
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            value = node.value
            targets = [node.target]
        capability = _capability_reference(value, aliases, receiver_aliases)
        if capability is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                aliases[target.id] = capability

    found = {name: set() for name in _APPROVED_X_CALLS}
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dynamic_getattr = _getattr_capability(node, receiver_aliases)
        if dynamic_getattr is not None and dynamic_getattr[2]:
            violations.append((relative, node.lineno, "dynamic_getattr"))
        capability = _capability_reference(node.func, aliases, receiver_aliases)
        if capability is None:
            continue
        method, generic_x_boundary, dynamic = capability
        if dynamic:
            violations.append((relative, node.lineno, "dynamic_call"))
        elif method in _PROHIBITED_X_CALLS and (
            method != "write" or generic_x_boundary
        ):
            violations.append((relative, node.lineno, method))
        if method in _APPROVED_X_CALLS:
            found[method].add(relative)
    return violations, found


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
        "write",
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
    found = {name: set() for name in _APPROVED_X_CALLS}
    violations = []
    for path in production_files:
        relative = path.relative_to(root).as_posix()
        file_violations, file_found = _scan_x_calls(
            path.read_text(encoding="utf-8"), relative
        )
        violations.extend(file_violations)
        for method, locations in file_found.items():
            found[method].update(locations)
    assert violations == []
    assert found == _APPROVED_X_CALLS


def test_x_write_static_scan_rejects_generic_and_dynamic_mutations():
    mutations = {
        "direct": "client.write('target')",
        "aliased": "operation = client.write\noperation('target')",
        "dynamic": "operation = getattr(x_client, method_name)\noperation('target')",
        "receiver_alias": "x_alias = client\nx_alias.write('target')",
        "dynamic_receiver_alias": (
            "x_alias = client\n"
            "operation = getattr(x_alias, method_name)\n"
            "operation('target')"
        ),
    }

    for name, source in mutations.items():
        violations, _found = _scan_x_calls(source, f"mutation-{name}.py")
        assert violations, name

    safe_source = "destination_file.write(b'data')\nos.write(fd, b'data')"
    violations, _found = _scan_x_calls(safe_source, "safe-files.py")
    assert violations == []
