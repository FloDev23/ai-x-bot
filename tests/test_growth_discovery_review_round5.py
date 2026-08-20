import json
from datetime import datetime, timedelta, timezone

import tweepy

from modules.database import Database
from modules.growth_discovery import GrowthDiscovery, passes_candidate_filters
from modules.twitter_client import TwitterClient


NOW = datetime(2026, 8, 10, 10, tzinfo=timezone.utc)


def _tweepy_user(user_id, username, description):
    return tweepy.User({
        "id": str(user_id),
        "name": username,
        "username": username,
        "description": description,
        "protected": False,
        "location": "Rome",
        "created_at": "2026-01-01T10:00:00.000Z",
        "public_metrics": {
            "followers_count": 1800,
            "following_count": 650,
            "tweet_count": 100,
            "listed_count": 5,
        },
    })


def _tweepy_tweet(tweet_id, text, lang):
    return tweepy.Tweet({
        "id": str(tweet_id),
        "text": text,
        "edit_history_tweet_ids": [str(tweet_id)],
        "created_at": (NOW - timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        ),
        "lang": lang,
        "public_metrics": {
            "like_count": 0,
            "reply_count": 0,
            "retweet_count": 0,
            "quote_count": 0,
        },
    })


def _response(data, *, meta=None, includes=None):
    return tweepy.Response(data, includes or {}, [], meta or {})


class TweepyGrowthBackend:
    def __init__(self, follower_pages, latest_tweets):
        self.follower_pages = follower_pages
        self.latest_tweets = latest_tweets
        self.follower_tokens = []
        self.latest_calls = []
        self.search_calls = []

    def get_me(self):
        return _response(_tweepy_user(999, "self_account", "Bot account"))

    def get_users_followers(self, **kwargs):
        token = kwargs.get("pagination_token")
        self.follower_tokens.append(token)
        result = self.follower_pages[token]
        if isinstance(result, BaseException):
            raise result
        return result

    def get_users_tweets(self, **kwargs):
        user_id = str(kwargs["id"])
        self.latest_calls.append(user_id)
        tweet = self.latest_tweets.get(user_id)
        return _response([] if tweet is None else [tweet])

    def search_recent_tweets(self, **kwargs):
        self.search_calls.append(kwargs["query"])
        return _response([])


def _twitter_client(backend):
    client = TwitterClient.__new__(TwitterClient)
    client._client = backend
    return client


def _discovery(client, database, *, score_threshold=75):
    return GrowthDiscovery(
        client,
        database,
        score_threshold=score_threshold,
        query_budget=1,
        new_profile_budget=25,
        profile_cache_days=7,
        digest_limit=5,
        seed_accounts=(),
        topic_queries=("topic-one", "topic-two"),
    )


def test_none_description_from_tweepy_is_eligible_end_to_end(tmp_path):
    user = _tweepy_user(101, "none_bio", None)
    tweet = _tweepy_tweet(
        901,
        "Class schedule, member booking, occupancy, and drop-in update",
        "en",
    )
    backend = TweepyGrowthBackend(
        {None: _response([user])},
        {"101": tweet},
    )
    database = Database(str(tmp_path / "growth.db"))

    rows = _discovery(
        _twitter_client(backend),
        database,
        score_threshold=70,
    ).run(NOW)

    assert backend.latest_calls == ["101"]
    assert [row["user_id"] for row in rows] == ["101"]
    audit = database.get_growth_candidate("101")
    assert audit["profile"]["description"] == ""
    assert audit["latest_post"]["text"].strip()
    assert audit["score"] == 70
    assert audit["score_data"]["hard_filter_passed"] is True
    assert audit["score_data"]["filter_reason"] == "accepted"
    assert passes_candidate_filters(audit["profile"], audit["latest_post"], NOW) == (
        True,
        "accepted",
    )
    assert database.get_cached_growth_candidate("101", NOW) is not None
    assert [
        row["user_id"]
        for row in database.get_digest_candidates(now=NOW, threshold=70)
    ] == ["101"]


def test_none_lang_from_tweepy_is_eligible_without_market_points(tmp_path):
    user = _tweepy_user(
        102,
        "none_lang",
        "Owner of an independent FlexDropin gym",
    )
    tweet = _tweepy_tweet(
        902,
        "Class schedule, member booking, and occupancy update",
        None,
    )
    backend = TweepyGrowthBackend(
        {None: _response([user])},
        {"102": tweet},
    )
    database = Database(str(tmp_path / "growth.db"))

    rows = _discovery(_twitter_client(backend), database).run(NOW)

    assert backend.latest_calls == ["102"]
    assert [row["user_id"] for row in rows] == ["102"]
    audit = database.get_growth_candidate("102")
    assert audit["latest_post"]["lang"] == ""
    assert audit["score"] == 85
    assert audit["score_data"]["market"] == 0
    assert "english_market" not in audit["score_data"]["reasons"]
    assert audit["score_data"]["hard_filter_passed"] is True
    assert database.get_cached_growth_candidate("102", NOW) is not None
    assert [
        row["user_id"] for row in database.get_digest_candidates(now=NOW)
    ] == ["102"]


def test_follower_pagination_normalizes_only_none_description_and_keeps_partial_rows():
    backend = TweepyGrowthBackend(
        {
            None: _response(
                [
                    _tweepy_user(201, "none_desc", None),
                    _tweepy_user(202, "bool_desc", False),
                ],
                meta={"next_token": "second-page"},
            ),
            "second-page": _response([
                _tweepy_user(203, "int_desc", 7),
                _tweepy_user(204, "list_desc", ["Gym owner"]),
                _tweepy_user(205, "valid_desc", "Gym owner"),
                _tweepy_user(206, "empty_desc", ""),
            ]),
        },
        {},
    )

    rows = _twitter_client(backend).get_followers_profiles()

    assert backend.follower_tokens == [None, "second-page"]
    assert [row["id"] for row in rows] == ["201", "205", "206"]
    assert [row["description"] for row in rows] == ["", "Gym owner", ""]
    assert json.loads(json.dumps(rows)) == rows


def test_follower_snapshot_boundary_marks_page_one_outage_incomplete():
    backend = TweepyGrowthBackend({None: RuntimeError("outage")}, {})

    result = _twitter_client(backend).read_followers_profiles()

    assert result.complete is False
    assert result.profiles == ()
    assert backend.follower_tokens == [None]


def test_follower_snapshot_boundary_accepts_complete_empty_page():
    backend = TweepyGrowthBackend({None: _response([])}, {})

    result = _twitter_client(backend).read_followers_profiles()

    assert result.complete is True
    assert result.profiles == ()
    assert backend.follower_tokens == [None]


def test_follower_snapshot_boundary_isolates_malformed_record_without_partial_run():
    backend = TweepyGrowthBackend(
        {None: _response([object(), _tweepy_user(207, "valid_owner", "Gym owner")])},
        {},
    )

    result = _twitter_client(backend).read_followers_profiles()

    assert result.complete is True
    assert [profile["id"] for profile in result.profiles] == ["207"]
    assert backend.follower_tokens == [None]


def test_latest_post_normalizes_only_none_lang_and_isolates_wrong_types():
    languages = {
        "301": None,
        "302": False,
        "303": 7,
        "304": ["en"],
        "305": "en",
        "306": "",
    }
    backend = TweepyGrowthBackend(
        {None: _response([])},
        {
            user_id: _tweepy_tweet(
                1000 + int(user_id),
                "Class booking update",
                lang,
            )
            for user_id, lang in languages.items()
        },
    )
    client = _twitter_client(backend)

    results = {
        user_id: client.get_latest_original_post(user_id)
        for user_id in languages
    }

    assert backend.latest_calls == list(languages)
    assert results["301"]["lang"] == ""
    assert results["302"] is None
    assert results["303"] is None
    assert results["304"] is None
    assert results["305"]["lang"] == "en"
    assert results["306"]["lang"] == ""
