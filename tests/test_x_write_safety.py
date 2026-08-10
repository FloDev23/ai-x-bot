import importlib
import inspect

import config
from modules.twitter_client import TwitterClient


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
    assert "reply_to" not in inspect.signature(TwitterClient.post_tweet).parameters


def test_rollout_defaults_are_safe(monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("CONTENT_SLOTS", raising=False)
    reloaded = importlib.reload(config)
    assert reloaded.DRY_RUN is True
    assert reloaded.APPROVAL_REQUIRED is True
    assert reloaded.BOT_TIMEZONE == "Europe/Rome"
    assert reloaded.CONTENT_SLOTS == ["14:00", "20:00"]
    assert reloaded.MAX_LINKS_PER_WEEK == 1


def test_character_contains_no_invented_bug_example():
    character_text = open("character.json", encoding="utf-8").read().lower()
    prohibited = ("absurd bugs", "stripe webhook", "bugs fixed", "rough day")
    assert all(term not in character_text for term in prohibited)
