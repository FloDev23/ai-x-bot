"""Tests for X thread posting support."""
import json
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import List, Optional
from unittest.mock import MagicMock

import pytest

from modules.database import Database, PostDraftPublicationClaim
from modules.publisher import Publisher
from modules.twitter_client import (
    TwitterClient,
    XPublicationPaused,
    XPublicationRejected,
    XPublicationUnknown,
)


SLOT = datetime.fromisoformat("2030-03-01T10:00:00+01:00")

THREAD_TWEETS = [
    "Tweet 1: opening hook — la palestra è cara.",
    "Tweet 2: sviluppo del problema.",
    "Tweet 3: la soluzione FlexDropin.",
]


# ─── Helpers ────────────────────────────────────────────────────────────────


def _make_twitter_client_with_stub(stub_tweepy_client):
    """Return a TwitterClient whose internal _client is replaced by stub."""
    client = object.__new__(TwitterClient)
    client._client = stub_tweepy_client
    client._api = None
    return client


class StubTweepyClient:
    """Records create_tweet calls and returns fake responses."""

    def __init__(self, base_id=1000):
        self._base_id = base_id
        self.calls: List[dict] = []

    def create_tweet(self, **kwargs):
        self.calls.append(dict(kwargs))
        tweet_id = str(self._base_id + len(self.calls) - 1)
        return SimpleNamespace(data={"id": tweet_id})


class PausingTweepyClient:
    """Always raises on create_tweet (simulates a gate-pause scenario)."""

    def create_tweet(self, **kwargs):
        raise AssertionError("create_tweet must not be called after gate refuses")


class ThreadRecordingXClient:
    """Records post_thread calls for integration tests."""

    def __init__(self, tweet_ids=None):
        self._tweet_ids = tweet_ids or ["2001", "2002", "2003"]
        self.thread_calls: List[List[str]] = []
        self.post_calls: List[str] = []

    def post_tweet(self, text, *args, **kwargs):
        self.post_calls.append(text)
        return SimpleNamespace(data={"id": "9001"})

    def post_thread(self, tweets, **kwargs):
        before_write = kwargs.get("before_write")
        if before_write is not None:
            allowed = before_write()
            if allowed is not True:
                raise XPublicationPaused("publication_paused")
        self.thread_calls.append(list(tweets))
        return list(self._tweet_ids[: len(tweets)])


def _approved_thread_draft(db: Database, *, slot, key, tweets: List[str]):
    """Create and approve a thread draft, returning its draft_id."""
    db.set_state("paused", "false")
    source_id = db.add_content_source("evergreen_idea", "Thread source.")
    draft_id = db.create_post_draft(
        text=tweets[0],
        category="gym_strategy",
        source_ids=[source_id],
        score_data={"total": 75, "authority": "operator"},
        intended_slot=slot.isoformat(),
        publication_key=key,
    )
    tweets_json = json.dumps(tweets, ensure_ascii=False, separators=(",", ":"))
    with db._conn() as conn:
        conn.execute(
            "UPDATE post_drafts SET thread_tweets_json = ?, origin = 'manual_operator'"
            " WHERE id = ?",
            (tweets_json, draft_id),
        )
    assert db.transition_post_draft(
        draft_id,
        ["pending_approval"],
        "approved",
        approved_at=(slot - timedelta(minutes=30)).isoformat(),
        approved_by="floriano",
    )
    return draft_id


# ─── TwitterClient.post_thread unit tests ───────────────────────────────────


def test_post_thread_posts_all_tweets_and_returns_ids():
    stub = StubTweepyClient(base_id=100)
    client = _make_twitter_client_with_stub(stub)

    ids = client.post_thread(THREAD_TWEETS)

    assert ids == ["100", "101", "102"]
    assert len(stub.calls) == 3
    assert stub.calls[0]["text"] == THREAD_TWEETS[0]
    assert stub.calls[1]["text"] == THREAD_TWEETS[1]
    assert stub.calls[2]["text"] == THREAD_TWEETS[2]


def test_post_thread_reports_each_confirmed_id_with_its_parent():
    stub = StubTweepyClient(base_id=110)
    client = _make_twitter_client_with_stub(stub)
    checkpoints = []

    ids = client.post_thread(
        THREAD_TWEETS,
        on_tweet_posted=lambda index, tweet_id, parent_id: checkpoints.append(
            (index, tweet_id, parent_id)
        ),
    )

    assert ids == ["110", "111", "112"]
    assert checkpoints == [
        (0, "110", None),
        (1, "111", "110"),
        (2, "112", "111"),
    ]


def test_post_thread_links_replies_via_in_reply_to_tweet_id():
    stub = StubTweepyClient(base_id=200)
    client = _make_twitter_client_with_stub(stub)

    client.post_thread(THREAD_TWEETS)

    assert "in_reply_to_tweet_id" not in stub.calls[0]
    assert stub.calls[1]["in_reply_to_tweet_id"] == "200"
    assert stub.calls[2]["in_reply_to_tweet_id"] == "201"


def test_post_thread_checks_before_write_exactly_once():
    stub = StubTweepyClient(base_id=300)
    client = _make_twitter_client_with_stub(stub)
    gate_calls = []

    def gate():
        gate_calls.append(1)
        return True

    client.post_thread(THREAD_TWEETS, before_write=gate)

    assert gate_calls == [1]


def test_post_thread_raises_paused_when_gate_returns_false():
    stub = PausingTweepyClient()
    client = _make_twitter_client_with_stub(stub)

    with pytest.raises(XPublicationPaused):
        client.post_thread(THREAD_TWEETS, before_write=lambda: False)


def test_post_thread_raises_paused_when_gate_raises():
    stub = PausingTweepyClient()
    client = _make_twitter_client_with_stub(stub)

    def crashing_gate():
        raise RuntimeError("gate exploded")

    with pytest.raises(XPublicationPaused):
        client.post_thread(THREAD_TWEETS, before_write=crashing_gate)


def test_post_thread_rejects_single_tweet_list():
    stub = StubTweepyClient()
    client = _make_twitter_client_with_stub(stub)

    with pytest.raises(XPublicationRejected):
        client.post_thread(["Only one tweet."])


def test_post_thread_rejects_empty_list():
    stub = StubTweepyClient()
    client = _make_twitter_client_with_stub(stub)

    with pytest.raises(XPublicationRejected):
        client.post_thread([])


# ─── Database thread_tweets decode ──────────────────────────────────────────


def test_get_post_draft_decodes_thread_tweets(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    source_id = db.add_content_source("evergreen_idea", "Source.")
    draft_id = db.create_post_draft(
        text=THREAD_TWEETS[0],
        category="gym_strategy",
        source_ids=[source_id],
        score_data={"total": 75},
        intended_slot=SLOT.isoformat(),
        publication_key="test-thread-decode",
    )
    tweets_json = json.dumps(THREAD_TWEETS, separators=(",", ":"))
    with db._conn() as conn:
        conn.execute(
            "UPDATE post_drafts SET thread_tweets_json = ? WHERE id = ?",
            (tweets_json, draft_id),
        )

    draft = db.get_post_draft(draft_id)

    assert draft["thread_tweets"] == THREAD_TWEETS


def test_get_post_draft_thread_tweets_is_none_for_single_post(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    source_id = db.add_content_source("evergreen_idea", "Source.")
    draft_id = db.create_post_draft(
        text="A single tweet.",
        category="gym_strategy",
        source_ids=[source_id],
        score_data={"total": 75},
        intended_slot=SLOT.isoformat(),
        publication_key="test-single-post",
    )

    draft = db.get_post_draft(draft_id)

    assert draft["thread_tweets"] is None


def test_thread_publication_checkpoints_are_sequential_idempotent_and_persistent(
    tmp_path,
):
    path = str(tmp_path / "thread-checkpoints.db")
    db = Database(path)
    draft_id = _approved_thread_draft(
        db, slot=SLOT, key="thread-checkpoints", tweets=THREAD_TWEETS,
    )
    draft = db.get_post_draft(draft_id)
    claimed = db.claim_post_draft_for_publication(draft_id, draft["revision"])
    assert claimed is not None
    _claimed_draft, claim = claimed

    assert db.record_thread_publication_part(claim, 0, "7101", None) is True
    assert db.record_thread_publication_part(claim, 0, "7101", None) is True
    assert db.record_thread_publication_part(claim, 2, "7103", "7102") is False
    assert db.record_thread_publication_part(claim, 1, "7102", "wrong") is False
    assert db.record_thread_publication_part(claim, 1, "7102", "7101") is True

    reopened = Database(path)
    assert reopened.get_thread_publication_parts(draft_id) == [
        {
            "part_index": 0,
            "tweet_id": "7101",
            "reply_to_tweet_id": None,
        },
        {
            "part_index": 1,
            "tweet_id": "7102",
            "reply_to_tweet_id": "7101",
        },
    ]


# ─── Publisher integration tests ────────────────────────────────────────────


def test_thread_draft_is_published_via_post_thread(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    x = ThreadRecordingXClient(tweet_ids=["3001", "3002", "3003"])
    draft_id = _approved_thread_draft(db, slot=SLOT, key="thread-pub-1", tweets=THREAD_TWEETS)

    result = Publisher(db, x, dry_run=False).publish(draft_id, SLOT)

    assert result.status == "published"
    assert result.tweet_id == "3001"
    assert x.thread_calls == [THREAD_TWEETS]
    assert x.post_calls == []


def test_thread_draft_stores_root_tweet_id(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    x = ThreadRecordingXClient(tweet_ids=["4001", "4002", "4003"])
    draft_id = _approved_thread_draft(db, slot=SLOT, key="thread-pub-2", tweets=THREAD_TWEETS)

    Publisher(db, x, dry_run=False).publish(draft_id, SLOT)

    stored = db.get_post_draft(draft_id)
    assert stored["status"] == "published"
    assert stored["published_tweet_id"] == "4001"


def test_partial_thread_failure_keeps_confirmed_part_ids_for_reconciliation(tmp_path):
    class PartialThreadXClient(ThreadRecordingXClient):
        def post_thread(self, tweets, **kwargs):
            checkpoint = kwargs["on_tweet_posted"]
            checkpoint(0, "7201", None)
            checkpoint(1, "7202", "7201")
            raise XPublicationUnknown("third_part_outcome_unknown")

    db = Database(str(tmp_path / "partial-thread.db"))
    draft_id = _approved_thread_draft(
        db, slot=SLOT, key="thread-partial", tweets=THREAD_TWEETS,
    )

    result = Publisher(db, PartialThreadXClient(), dry_run=False).publish(
        draft_id, SLOT,
    )

    assert result.status == "publication_unknown"
    assert db.get_post_draft(draft_id)["status"] == "publication_unknown"
    assert db.get_thread_publication_parts(draft_id) == [
        {
            "part_index": 0,
            "tweet_id": "7201",
            "reply_to_tweet_id": None,
        },
        {
            "part_index": 1,
            "tweet_id": "7202",
            "reply_to_tweet_id": "7201",
        },
    ]


def test_thread_draft_paused_gate_restores_draft(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    db.set_state("paused", "true")
    source_id = db.add_content_source("evergreen_idea", "Source.")
    draft_id = db.create_post_draft(
        text=THREAD_TWEETS[0],
        category="gym_strategy",
        source_ids=[source_id],
        score_data={"total": 75},
        intended_slot=SLOT.isoformat(),
        publication_key="thread-paused",
    )
    tweets_json = json.dumps(THREAD_TWEETS, separators=(",", ":"))
    with db._conn() as conn:
        conn.execute(
            "UPDATE post_drafts SET thread_tweets_json = ?, origin = 'manual_operator'"
            " WHERE id = ?",
            (tweets_json, draft_id),
        )
    db.transition_post_draft(
        draft_id,
        ["pending_approval"],
        "approved",
        approved_at=(SLOT - timedelta(minutes=30)).isoformat(),
        approved_by="floriano",
    )
    x = ThreadRecordingXClient()

    result = Publisher(db, x, dry_run=False).publish(draft_id, SLOT)

    assert result.status == "paused"
    assert db.get_post_draft(draft_id)["status"] == "approved"
    assert x.thread_calls == []


def test_single_post_draft_still_uses_post_tweet_not_post_thread(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    x = ThreadRecordingXClient()
    source_id = db.add_content_source("evergreen_idea", "Source.")
    draft_id = db.create_post_draft(
        text="Just a single tweet.",
        category="gym_strategy",
        source_ids=[source_id],
        score_data={"total": 75},
        intended_slot=SLOT.isoformat(),
        publication_key="single-post-no-thread",
    )
    db.set_state("paused", "false")
    db.transition_post_draft(
        draft_id,
        ["pending_approval"],
        "approved",
        approved_at=(SLOT - timedelta(minutes=30)).isoformat(),
        approved_by="floriano",
    )

    result = Publisher(db, x, dry_run=False).publish(draft_id, SLOT)

    assert result.status == "published"
    assert x.thread_calls == []
    assert x.post_calls == ["Just a single tweet."]
