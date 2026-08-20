import importlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import dotenv
import pytest

import config
from modules.database import Database
from modules.growth_discovery import (
    GrowthDiscovery,
    passes_candidate_filters,
    score_growth_candidate,
)
from modules.twitter_client import TwitterClient


NOW = datetime(2026, 8, 10, tzinfo=timezone.utc)


def profile(user_id="100", username="owner", **overrides):
    value = {
        "id": user_id,
        "username": username,
        "description": "Owner of an independent strength and conditioning studio",
        "followers_count": 1800,
        "following_count": 650,
        "protected": False,
        "spam_signals": [],
    }
    value.update(overrides)
    return value


def post(post_id="900", created_at="2026-08-09T10:00:00+00:00", **overrides):
    value = {
        "id": post_id,
        "text": "Testing a new class timetable for our members",
        "created_at": created_at,
        "lang": "en",
        "is_original": True,
    }
    value.update(overrides)
    return value


class FakeX:
    def __init__(self):
        self.followers = []
        self.search_results = {}
        self.network_results = []
        self.latest_posts = {}
        self.latest_errors = set()
        self.calls = []
        self.latest_calls = []
        self.write_calls = []

    @property
    def search_and_network_query_count(self):
        return sum(call[0] in {"search", "network"} for call in self.calls)

    @property
    def new_profile_evaluation_count(self):
        return len(self.latest_calls)

    def get_followers_profiles(self):
        self.calls.append(("followers",))
        return list(self.followers)

    def search_recent_authors(self, query):
        self.calls.append(("search", query))
        response = self.search_results.get(query, self.search_results.get("*", []))
        if isinstance(response, BaseException):
            raise response
        return list(response)

    def get_network_candidates(self, seed_accounts):
        self.calls.append(("network", tuple(seed_accounts)))
        if isinstance(self.network_results, BaseException):
            raise self.network_results
        return list(self.network_results)

    def get_latest_original_post(self, user_id):
        self.latest_calls.append(user_id)
        if user_id in self.latest_errors:
            raise RuntimeError("read failed")
        return self.latest_posts.get(user_id)


def discovery(tmp_path, fake_x, **overrides):
    options = {
        "score_threshold": 75,
        "query_budget": 3,
        "new_profile_budget": 25,
        "profile_cache_days": 7,
        "digest_limit": 5,
        "seed_accounts": ("seed_one", "seed_two"),
        "topic_queries": ("topic-one", "topic-two"),
    }
    options.update(overrides)
    return GrowthDiscovery(fake_x, Database(str(tmp_path / "growth.db")), **options)


def test_relevant_gym_owner_has_exact_approved_score_components():
    result = score_growth_candidate(profile(), post(), NOW)
    assert result == {
        "role_bio": 30,
        "recent_topic_fit": 25,
        "activity": 15,
        "market": 15,
        "account_quality": 10,
        "affinity": 0,
        "total": 95,
        "audience_segment": "primary",
        "reasons": [
            "primary_operator_role",
            "multiple_operating_topics",
            "active_within_7_days",
            "english_market",
            "plausible_public_metrics",
        ],
        "activity_at": "2026-08-09T10:00:00+00:00",
    }


def test_score_caps_each_component_and_total_at_approved_maximums():
    rich_profile = profile(
        description=(
            "Owner founder manager coach trainer studio gym box pilates yoga "
            "fitness tech FlexDropin class booking drop-in"
        ),
        followers_count=5000,
        following_count=700,
    )
    rich_post = post(
        text=(
            "Class schedule retention member no-show occupancy booking drop-in "
            "class booking FlexDropin"
        ),
    )
    result = score_growth_candidate(rich_profile, rich_post, NOW)
    assert {key: result[key] for key in (
        "role_bio", "recent_topic_fit", "activity", "market",
        "account_quality", "affinity", "total",
    )} == {
        "role_bio": 30,
        "recent_topic_fit": 25,
        "activity": 15,
        "market": 15,
        "account_quality": 10,
        "affinity": 5,
        "total": 100,
    }


@pytest.mark.parametrize(
    ("candidate_profile", "latest_post", "reason"),
    [
        (profile(protected=True), post(), "protected_profile"),
        (
            profile(),
            post(created_at="2026-06-01T10:00:00+00:00"),
            "no_original_post_within_30_days",
        ),
        (profile(), post(is_original=False), "no_original_post_within_30_days"),
        (
            profile(description=""),
            post(text=""),
            "insufficient_bio_post_context",
        ),
        (
            profile(spam_signals=["follow_farming"]),
            post(),
            "spam_or_follow_farming_signals",
        ),
    ],
)
def test_hard_filters_reject_disallowed_profiles(candidate_profile, latest_post, reason):
    assert passes_candidate_filters(candidate_profile, latest_post, NOW) == (
        False,
        reason,
    )


def test_hard_filter_accepts_sufficient_post_context_when_bio_is_empty():
    accepted, reason = passes_candidate_filters(
        profile(description=""),
        post(text="Pilates class booking and occupancy planning"),
        NOW,
    )
    assert (accepted, reason) == (True, "accepted")


def test_discovery_caps_queries_and_new_profiles_even_on_errors_and_duplicates(tmp_path):
    fake_x = FakeX()
    fake_x.followers = [profile(str(index), f"f{index}") for index in range(1, 27)]
    fake_x.search_results["*"] = [
        profile("1", "duplicate"),
        profile("27", "search_owner"),
        profile(True, "bool_id"),
        profile(28, "integer_id"),
    ]
    fake_x.network_results = [profile("27", "duplicate_again")]
    fake_x.latest_posts = {
        str(index): post(str(800 + index)) for index in range(1, 28)
    }
    fake_x.latest_errors.add("1")

    growth = discovery(tmp_path, fake_x)
    growth.run(NOW)

    assert fake_x.search_and_network_query_count == 3
    assert fake_x.new_profile_evaluation_count == 25
    assert fake_x.latest_calls == [str(index) for index in range(1, 26)]
    assert fake_x.write_calls == []


def test_new_followers_are_evaluated_before_search_candidates(tmp_path):
    fake_x = FakeX()
    fake_x.followers = [profile("follower", "new_follower")]
    fake_x.search_results["*"] = [profile("searched", "searched_owner")]
    fake_x.latest_posts = {
        "follower": post("901"),
        "searched": post("902"),
    }

    growth = discovery(tmp_path, fake_x, new_profile_budget=1)
    result = growth.run(NOW)

    assert fake_x.latest_calls == ["follower"]
    assert [candidate["user_id"] for candidate in result] == ["follower"]


def test_followers_deferred_by_daily_cap_are_considered_next_day(tmp_path):
    fake_x = FakeX()
    fake_x.followers = [
        profile(str(index), f"follower_{index}") for index in range(1, 27)
    ]
    fake_x.latest_posts = {
        str(index): post(str(930 + index)) for index in range(1, 27)
    }
    growth = discovery(tmp_path, fake_x)

    growth.run(NOW)
    assert fake_x.latest_calls == [str(index) for index in range(1, 26)]

    growth.run(NOW + timedelta(days=1))
    assert fake_x.latest_calls[-1] == "26"
    assert len(fake_x.latest_calls) == 26


def test_cached_follower_keeps_snapshot_relevance_without_new_evaluation(tmp_path):
    fake_x = FakeX()
    fake_x.followers = [profile("cached_follower", "cached_follower")]
    fake_x.latest_posts["cached_follower"] = post("960")
    growth = discovery(tmp_path, fake_x)

    growth.run(NOW)
    assert fake_x.latest_calls == ["cached_follower"]
    growth.run(NOW)
    assert fake_x.latest_calls == ["cached_follower"]

    with growth.db._conn() as conn:
        row = conn.execute(
            "SELECT relevant, source FROM follower_snapshots "
            "WHERE observed_on = ? AND user_id = ?",
            ("2026-08-10", "cached_follower"),
        ).fetchone()
    assert row["relevant"] == 1
    assert row["source"].startswith("candidate:")


def test_fresh_cache_costs_no_fetch_or_evaluation_but_expiry_uses_explicit_clock(tmp_path):
    fake_x = FakeX()
    fake_x.search_results["*"] = [profile("cached", "cached_owner")]
    fake_x.latest_posts["cached"] = post("903")
    growth = discovery(tmp_path, fake_x)

    assert [row["user_id"] for row in growth.run(NOW)] == ["cached"]
    assert fake_x.latest_calls == ["cached"]

    fake_x.latest_posts["cached"] = post(
        "904", created_at="2026-08-15T10:00:00+00:00",
    )
    assert [row["user_id"] for row in growth.run(NOW + timedelta(days=6))] == [
        "cached"
    ]
    assert fake_x.latest_calls == ["cached"]

    growth.run(NOW + timedelta(days=8))
    assert fake_x.latest_calls == ["cached", "cached"]


def test_active_sqlite_suppression_skips_evaluation_and_digest(tmp_path):
    fake_x = FakeX()
    growth = discovery(tmp_path, fake_x)
    candidate_id = growth.db.upsert_growth_candidate({
        "user_id": "suppressed",
        "username": "suppressed_owner",
        "profile": profile("suppressed", "suppressed_owner"),
        "latest_post": post("905"),
        "score": 95,
        "score_data": {"total": 95},
        "discovery_source": "topic_search",
        "profile_expires_at": (NOW + timedelta(days=7)).isoformat(),
        "last_evaluated_at": NOW.isoformat(),
    })
    with growth.db._conn() as conn:
        conn.execute(
            "UPDATE growth_candidates SET decision = 'discarded', "
            "suppressed_until = ? WHERE id = ?",
            ((NOW + timedelta(days=30)).isoformat(), candidate_id),
        )
    fake_x.search_results["*"] = [profile("suppressed", "suppressed_owner")]
    fake_x.latest_posts["suppressed"] = post("906")

    assert growth.run(NOW) == []
    assert fake_x.latest_calls == []


def test_low_score_and_hard_filtered_candidates_are_stored_only_for_audit(tmp_path):
    fake_x = FakeX()
    fake_x.followers = [
        profile("low", "low_score", description="Fitness member"),
        profile("inactive", "inactive_owner"),
    ]
    fake_x.latest_posts = {
        "low": post("907", text="A class update"),
        "inactive": post(
            "908", created_at="2026-06-01T10:00:00+00:00",
        ),
    }
    growth = discovery(tmp_path, fake_x)

    assert growth.run(NOW) == []

    low = growth.db.get_growth_candidate("low")
    inactive = growth.db.get_growth_candidate("inactive")
    assert low["score"] == 65
    assert low["score_data"]["hard_filter_passed"] is True
    assert inactive["score"] == 80
    assert inactive["score_data"]["hard_filter_passed"] is False
    assert inactive["score_data"]["filter_reason"] == (
        "no_original_post_within_30_days"
    )


def test_sqlite_follower_snapshot_records_final_relevance_for_same_observation(tmp_path):
    db = Database(str(tmp_path / "growth.db"))
    follower = profile("snapshot", "snapshot_owner")

    assert db.save_follower_snapshot(
        "2026-08-10", follower, relevant=False, source="x_followers"
    )
    assert db.save_follower_snapshot(
        "2026-08-10", follower, relevant=True, source="candidate:1"
    )

    with db._conn() as conn:
        row = conn.execute(
            "SELECT relevant, source FROM follower_snapshots "
            "WHERE observed_on = ? AND user_id = ?",
            ("2026-08-10", "snapshot"),
        ).fetchone()
    assert dict(row) == {"relevant": 1, "source": "candidate:1"}


def test_sqlite_digest_sorts_by_score_then_latest_activity_and_limits_five(tmp_path):
    db = Database(str(tmp_path / "growth.db"))
    for index in range(7):
        score = 90 if index < 2 else 80
        activity = NOW - timedelta(hours=index)
        db.upsert_growth_candidate({
            "user_id": str(index),
            "username": f"owner_{index}",
            "profile": profile(str(index), f"owner_{index}"),
            "latest_post": post(str(910 + index), activity.isoformat()),
            "score": score,
            "score_data": {
                "total": score,
                "audience_segment": "primary",
                "reasons": ["primary_operator_role"],
                "activity_at": activity.isoformat(),
                "hard_filter_passed": True,
                "filter_reason": "accepted",
            },
            "discovery_source": "topic_search",
            "profile_expires_at": (NOW + timedelta(days=7)).isoformat(),
            "last_evaluated_at": NOW.isoformat(),
        })

    rows = db.get_digest_candidates(limit=5, now=NOW, threshold=75)

    assert [row["user_id"] for row in rows] == ["0", "1", "2", "3", "4"]
    assert len(rows) == 5
    assert rows[0]["direct_url"] == "https://x.com/owner_0/status/910"
    assert rows[0]["audience_segment"] == "primary"


def test_source_order_rotates_between_daily_runs_in_sqlite(tmp_path):
    fake_x = FakeX()
    growth = discovery(tmp_path, fake_x)

    growth.run(NOW)
    first = [call[0] for call in fake_x.calls if call[0] != "followers"]
    fake_x.calls.clear()
    growth.run(NOW + timedelta(days=1))
    second = [call[0] for call in fake_x.calls if call[0] != "followers"]

    assert first == ["search", "search", "network"]
    assert second == ["network", "search", "search"]


def test_invalid_or_non_string_user_ids_are_not_coerced_or_evaluated(tmp_path):
    fake_x = FakeX()
    fake_x.followers = [
        profile(True, "boolean"),
        profile(123, "integer"),
        profile("123", "exact_string"),
        profile("123", "duplicate_string"),
    ]
    fake_x.latest_posts["123"] = post("920")

    result = discovery(tmp_path, fake_x).run(NOW)

    assert fake_x.latest_calls == ["123"]
    assert [row["username"] for row in result] == ["exact_string"]


def test_candidate_without_safe_direct_x_url_is_not_evaluated(tmp_path):
    fake_x = FakeX()
    fake_x.followers = [profile("unsafe", "owner/redirect")]
    fake_x.latest_posts["unsafe"] = post("921")

    assert discovery(tmp_path, fake_x).run(NOW) == []
    assert fake_x.latest_calls == []


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GROWTH_SCORE_THRESHOLD", "0"),
        ("GROWTH_QUERY_BUDGET", "-1"),
        ("GROWTH_NEW_PROFILE_BUDGET", "invalid"),
        ("GROWTH_PROFILE_CACHE_DAYS", "0"),
        ("GROWTH_DIGEST_LIMIT", "-5"),
    ],
)
def test_growth_numeric_config_fails_closed(monkeypatch, name, value):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda: False)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        importlib.reload(config)
    monkeypatch.delenv(name)
    importlib.reload(config)


def test_growth_query_budget_is_capped_and_seed_accounts_are_trimmed(monkeypatch):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda: False)
    monkeypatch.setenv("GROWTH_QUERY_BUDGET", "99")
    monkeypatch.setenv("GROWTH_SEED_ACCOUNTS", " @first,second, ,@third ")
    reloaded = importlib.reload(config)
    assert reloaded.GROWTH_QUERY_BUDGET == 3
    assert reloaded.GROWTH_SEED_ACCOUNTS == ("first", "second", "third")
    monkeypatch.delenv("GROWTH_QUERY_BUDGET")
    monkeypatch.delenv("GROWTH_SEED_ACCOUNTS")
    importlib.reload(config)


def test_twitter_read_methods_request_complete_profile_and_original_post_fields():
    calls = []
    user = SimpleNamespace(
        id=100,
        username="owner",
        description="Gym owner",
        protected=False,
        location="London",
        created_at=NOW,
        public_metrics={"followers_count": 50, "following_count": 20},
    )
    tweet = SimpleNamespace(
        id=200,
        text="Class booking update",
        author_id=100,
        created_at=NOW,
        lang="en",
    )

    class ReadClient:
        def get_me(self):
            return SimpleNamespace(data=SimpleNamespace(id=999))

        def get_users_followers(self, **kwargs):
            calls.append(("followers", kwargs))
            return SimpleNamespace(data=[user])

        def search_recent_tweets(self, **kwargs):
            calls.append(("search", kwargs))
            return SimpleNamespace(data=[tweet], includes={"users": [user]})

        def get_users_tweets(self, **kwargs):
            calls.append(("latest", kwargs))
            return SimpleNamespace(data=[tweet])

    client = TwitterClient.__new__(TwitterClient)
    client._client = ReadClient()

    followers = client.get_followers_profiles()
    authors = client.search_recent_authors("gym owner")
    network = client.get_network_candidates(("seed_one", "seed_two"))
    latest = client.get_latest_original_post("100")

    expected_user_fields = {
        "username", "description", "protected", "location", "created_at",
        "public_metrics",
    }
    assert followers[0]["id"] == "100"
    assert authors[0]["id"] == "100"
    assert network[0]["id"] == "100"
    assert latest == {
        "id": "200",
        "text": "Class booking update",
        "created_at": "2026-08-10T00:00:00+00:00",
        "lang": "en",
        "is_original": True,
    }
    for kind, kwargs in calls:
        if kind in {"followers", "search"}:
            assert set(kwargs["user_fields"]) == expected_user_fields
    latest_kwargs = next(kwargs for kind, kwargs in calls if kind == "latest")
    assert set(latest_kwargs["exclude"]) == {"retweets", "replies"}
    assert set(latest_kwargs["tweet_fields"]) >= {"created_at", "lang"}
