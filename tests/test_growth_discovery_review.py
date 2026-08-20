import json
import multiprocessing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from modules.database import Database
from modules.growth_discovery import GrowthDiscovery, passes_candidate_filters
from modules.twitter_client import TwitterClient


NOW = datetime(2026, 8, 10, 10, tzinfo=timezone.utc)


def review_profile(user_id="100", username="owner", **overrides):
    value = {
        "id": user_id,
        "username": username,
        "description": "Independent gym owner",
        "followers_count": 1800,
        "following_count": 650,
        "protected": False,
        "spam_signals": [],
    }
    value.update(overrides)
    return value


def review_post(post_id="900", created_at=None, **overrides):
    value = {
        "id": post_id,
        "text": "Class schedule and member booking update",
        "created_at": created_at or (NOW - timedelta(days=1)).isoformat(),
        "lang": "en",
        "is_original": True,
    }
    value.update(overrides)
    return value


def accepted_score(total=95, activity_at=None):
    return {
        "total": total,
        "audience_segment": "primary",
        "reasons": ["primary_operator_role"],
        "activity_at": activity_at or (NOW - timedelta(days=1)).isoformat(),
        "hard_filter_passed": True,
        "filter_reason": "accepted",
    }


class CoordinatedDatabase(Database):
    """Test-only probe that exposes the inherited get/set race deterministically."""

    def __init__(self, db_path, query_barrier, profile_barrier):
        self._query_barrier = query_barrier
        self._profile_barrier = profile_barrier
        super().__init__(db_path)

    def get_state(self, key, default=None):
        value = super().get_state(key, default)
        if key.startswith("growth_queries:"):
            self._query_barrier.wait(timeout=10)
        elif key.startswith("growth_profile_evaluations:"):
            self._profile_barrier.wait(timeout=10)
        return value


class ProcessProbeX:
    def __init__(self, worker_index):
        self.worker_index = worker_index
        self.query_calls = []
        self.latest_calls = []

    def get_followers_profiles(self):
        return [
            review_profile(
                f"{self.worker_index}-{index}",
                f"w{self.worker_index}_{index:02d}",
            )
            for index in range(30)
        ]

    def search_recent_authors(self, query):
        self.query_calls.append(("search", query))
        raise RuntimeError("planned query failure")

    def get_network_candidates(self, seeds):
        self.query_calls.append(("network", tuple(seeds)))
        raise RuntimeError("planned network failure")

    def get_latest_original_post(self, user_id):
        self.latest_calls.append(user_id)
        return review_post(f"9{self.worker_index}{len(self.latest_calls):02d}")


def _run_budget_probe(
    db_path,
    worker_index,
    query_barrier,
    profile_barrier,
    start_event,
    result_queue,
):
    try:
        db = CoordinatedDatabase(db_path, query_barrier, profile_barrier)
        fake_x = ProcessProbeX(worker_index)
        growth = GrowthDiscovery(
            fake_x,
            db,
            query_budget=3,
            new_profile_budget=25,
            seed_accounts=("seed_one",),
            topic_queries=("topic-one", "topic-two"),
        )
        start_event.wait(timeout=10)
        growth.run(NOW)
        result_queue.put({
            "queries": list(fake_x.query_calls),
            "latest": list(fake_x.latest_calls),
        })
    except BaseException as error:
        result_queue.put({"error": f"{type(error).__name__}: {error}"})


def test_sqlite_budget_claims_are_atomic_across_processes_errors_and_restart(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    db_path = str(tmp_path / "growth.db")
    Database(db_path)
    query_barrier = ctx.Barrier(2)
    profile_barrier = ctx.Barrier(2)
    start_event = ctx.Event()
    result_queue = ctx.Queue()
    processes = [
        ctx.Process(
            target=_run_budget_probe,
            args=(
                db_path,
                index,
                query_barrier,
                profile_barrier,
                start_event,
                result_queue,
            ),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    results = [result_queue.get(timeout=5) for _ in processes]
    assert all("error" not in result for result in results), results

    queries = [call for result in results for call in result["queries"]]
    latest = [user_id for result in results for user_id in result["latest"]]
    assert len(queries) == 3
    assert sorted(call[0] for call in queries) == ["network", "search", "search"]
    assert len(latest) == 25

    restarted_x = ProcessProbeX(3)
    restarted = GrowthDiscovery(
        restarted_x,
        Database(db_path),
        query_budget=3,
        new_profile_budget=25,
        seed_accounts=("seed_one",),
        topic_queries=("topic-one", "topic-two"),
    )
    restarted.run(NOW)
    assert restarted_x.query_calls == []
    assert restarted_x.latest_calls == []


@pytest.mark.parametrize("protected", [None, 1, "true", [], {}])
def test_hard_filter_requires_exact_public_protected_flag(protected):
    candidate = review_profile()
    if protected is None:
        candidate.pop("protected")
    else:
        candidate["protected"] = protected
    assert passes_candidate_filters(candidate, review_post(), NOW) == (
        False,
        "protected_profile",
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"description": 1},
        {"spam_signals": "none"},
        {"spam_signals": [1]},
        {"following_count": "650"},
    ],
)
def test_hard_filter_rejects_malformed_profile_records(overrides):
    assert passes_candidate_filters(
        review_profile(**overrides), review_post(), NOW,
    ) == (False, "malformed_candidate_record")


def test_digest_excludes_legacy_and_isolates_malformed_score_rows(tmp_path):
    db = Database(str(tmp_path / "growth.db"))
    common = {
        "latest_post": review_post(),
        "score": 95,
        "discovery_source": "topic_search",
        "profile_expires_at": (NOW + timedelta(days=7)).isoformat(),
        "last_evaluated_at": NOW.isoformat(),
    }
    db.upsert_growth_candidate({
        **common,
        "user_id": "legacy",
        "username": "legacy",
        "profile": review_profile("legacy", "legacy"),
        "score_data": {"total": 95},
    })
    malformed_id = db.upsert_growth_candidate({
        **common,
        "user_id": "malformed",
        "username": "malformed",
        "profile": review_profile("malformed", "malformed"),
        "score_data": {"total": 95, "hard_filter_passed": True},
    })
    db.upsert_growth_candidate({
        **common,
        "user_id": "valid",
        "username": "valid",
        "profile": review_profile("valid", "valid"),
        "score_data": accepted_score(),
    })
    with db._conn() as conn:
        conn.execute(
            "UPDATE growth_candidates SET score_json = '[]' WHERE id = ?",
            (malformed_id,),
        )

    assert [row["user_id"] for row in db.get_digest_candidates(now=NOW)] == [
        "valid"
    ]


def test_digest_isolates_malformed_sqlite_score_type_before_sorting(tmp_path):
    db = Database(str(tmp_path / "growth.db"))
    common = {
        "latest_post": review_post(),
        "score": 95,
        "score_data": accepted_score(),
        "discovery_source": "topic_search",
        "profile_expires_at": (NOW + timedelta(days=7)).isoformat(),
        "last_evaluated_at": NOW.isoformat(),
    }
    malformed_id = db.upsert_growth_candidate({
        **common,
        "user_id": "malformed_score",
        "username": "malformed_score",
        "profile": review_profile("malformed_score", "malformed_score"),
    })
    db.upsert_growth_candidate({
        **common,
        "user_id": "valid_score",
        "username": "valid_score",
        "profile": review_profile("valid_score", "valid_score"),
    })
    with db._conn() as conn:
        conn.execute(
            "UPDATE growth_candidates SET score = 'not-an-integer' WHERE id = ?",
            (malformed_id,),
        )

    assert [row["user_id"] for row in db.get_digest_candidates(now=NOW)] == [
        "valid_score"
    ]


class OrderedFollowerX:
    def __init__(self, followers):
        self.followers = followers
        self.latest_calls = []
        self.network_seeds = []

    def get_followers_profiles(self):
        return list(self.followers)

    def search_recent_authors(self, _query):
        return []

    def get_network_candidates(self, seeds):
        self.network_seeds.append(tuple(seeds))
        return []

    def get_latest_original_post(self, user_id):
        self.latest_calls.append(user_id)
        return review_post(f"8{len(self.latest_calls)}")


class MixedLatestPostX(OrderedFollowerX):
    def get_latest_original_post(self, user_id):
        self.latest_calls.append(user_id)
        if user_id == "unsafe_post":
            return review_post("810", unsafe_extra=object())
        return review_post("811")


def test_non_json_safe_latest_post_isolated_without_aborting_later_candidate(
    tmp_path,
):
    fake_x = MixedLatestPostX([
        review_profile("unsafe_post", "unsafe_post"),
        review_profile("safe_post", "safe_post"),
    ])
    growth = GrowthDiscovery(
        fake_x,
        Database(str(tmp_path / "growth.db")),
        query_budget=3,
        new_profile_budget=2,
        seed_accounts=("valid_seed",),
        topic_queries=("one", "two"),
    )

    result = growth.run(NOW)

    assert fake_x.latest_calls == ["unsafe_post", "safe_post"]
    assert [candidate["user_id"] for candidate in result] == ["safe_post"]
    assert growth.db.get_growth_candidate("unsafe_post") is not None


def test_new_then_deferred_followers_precede_expired_refresh_on_day_eight(tmp_path):
    db = Database(str(tmp_path / "growth.db"))
    day_one = NOW - timedelta(days=7)
    expired = review_profile("expired", "expired")
    deferred = review_profile("deferred", "deferred")
    new = review_profile("new", "new_owner")
    for candidate in (expired, deferred):
        db.save_follower_snapshot(
            day_one.date().isoformat(), candidate, False, "x_followers"
        )
    db.upsert_growth_candidate({
        "user_id": "expired",
        "username": "expired",
        "profile": expired,
        "latest_post": review_post("801", day_one.isoformat()),
        "score": 95,
        "score_data": {"total": 95, "hard_filter_passed": True},
        "discovery_source": "new_follower",
        "last_evaluated_at": day_one.isoformat(),
        "profile_expires_at": NOW.isoformat(),
    })
    fake_x = OrderedFollowerX([expired, deferred, new])
    growth = GrowthDiscovery(
        fake_x,
        db,
        query_budget=3,
        new_profile_budget=2,
        seed_accounts=("valid_seed",),
        topic_queries=("one", "two"),
    )

    growth.run(NOW)

    assert fake_x.latest_calls == ["new", "deferred"]


def complete_user(user_id, username, **overrides):
    value = {
        "id": user_id,
        "username": username,
        "description": "Gym owner",
        "protected": False,
        "location": "London",
        "created_at": NOW,
        "public_metrics": {
            "followers_count": 100,
            "following_count": 50,
            "tweet_count": 10,
            "listed_count": 1,
        },
    }
    value.update(overrides)
    return SimpleNamespace(**value)


def twitter_client_with_backend(backend):
    client = TwitterClient.__new__(TwitterClient)
    client._client = backend
    return client


def test_follower_reads_paginate_all_pages_and_deduplicate_exact_ids():
    first = complete_user(1, "first")
    duplicate = complete_user(1, "duplicate")
    second = complete_user(2, "second")

    class Backend:
        def __init__(self):
            self.tokens = []

        def get_me(self):
            return SimpleNamespace(data=SimpleNamespace(id=999))

        def get_users_followers(self, **kwargs):
            token = kwargs.get("pagination_token")
            self.tokens.append(token)
            if token is None:
                return SimpleNamespace(
                    data=[first, duplicate], meta={"next_token": "next"},
                )
            return SimpleNamespace(data=[duplicate, second], meta={})

    backend = Backend()
    client = twitter_client_with_backend(backend)

    assert [row["id"] for row in client.get_followers_profiles()] == ["1", "2"]
    assert backend.tokens == [None, "next"]


def test_follower_pagination_stops_on_repeated_token_and_keeps_safe_partial_rows():
    valid = complete_user(1, "first")

    class Backend:
        def __init__(self):
            self.calls = 0

        def get_me(self):
            return SimpleNamespace(data=SimpleNamespace(id=999))

        def get_users_followers(self, **_kwargs):
            self.calls += 1
            return SimpleNamespace(data=[valid], meta={"next_token": "repeat"})

    backend = Backend()
    rows = twitter_client_with_backend(backend).get_followers_profiles()
    assert [row["id"] for row in rows] == ["1"]
    assert backend.calls == 2


def test_follower_pagination_keeps_first_page_when_later_page_errors():
    valid = complete_user(1, "first")

    class Backend:
        def __init__(self):
            self.calls = 0

        def get_me(self):
            return SimpleNamespace(data=SimpleNamespace(id=999))

        def get_users_followers(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(
                    data=[valid], meta={"next_token": "next"},
                )
            raise RuntimeError("planned second-page failure")

    backend = Backend()
    rows = twitter_client_with_backend(backend).get_followers_profiles()
    assert [row["id"] for row in rows] == ["1"]
    assert backend.calls == 2


def test_follower_pagination_keeps_valid_rows_when_later_metadata_raises():
    first = complete_user(1, "first")
    second = complete_user(2, "second")

    class ExplodingMetadataResponse:
        data = [second]

        @property
        def meta(self):
            raise RuntimeError("malformed response metadata")

    class Backend:
        def __init__(self):
            self.calls = 0

        def get_me(self):
            return SimpleNamespace(data=SimpleNamespace(id=999))

        def get_users_followers(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(
                    data=[first], meta={"next_token": "next"},
                )
            return ExplodingMetadataResponse()

    rows = twitter_client_with_backend(Backend()).get_followers_profiles()
    assert [row["id"] for row in rows] == ["1", "2"]


def test_malformed_follower_records_are_isolated_and_json_safe():
    invalid_metrics = [
        "metrics",
        [1],
        7,
        {"followers_count": object(), "following_count": 2},
    ]
    users = [
        complete_user(index + 10, f"bad{index}", public_metrics=metrics)
        for index, metrics in enumerate(invalid_metrics)
    ]
    missing_protected = vars(complete_user(20, "missing"))
    missing_protected.pop("protected")
    users.extend([
        SimpleNamespace(**missing_protected),
        complete_user(21, "valid"),
    ])

    class Backend:
        def get_me(self):
            return SimpleNamespace(data=SimpleNamespace(id=999))

        def get_users_followers(self, **_kwargs):
            return SimpleNamespace(data=users, meta={})

    rows = twitter_client_with_backend(Backend()).get_followers_profiles()
    assert [row["id"] for row in rows] == ["21"]
    assert json.loads(json.dumps(rows)) == rows


def test_twitter_client_hides_general_purpose_backends_but_post_tweet_still_works(
    monkeypatch,
):
    class Backend:
        def create_tweet(self, **_params):
            return SimpleNamespace(data={"id": "123"})

    class Auth:
        def set_access_token(self, *_args):
            return None

    backend = Backend()
    monkeypatch.setattr("modules.twitter_client.tweepy.Client", lambda **_kw: backend)
    monkeypatch.setattr("modules.twitter_client.tweepy.OAuthHandler", lambda *_a: Auth())
    monkeypatch.setattr(
        "modules.twitter_client.tweepy.API", lambda *_a, **_kw: SimpleNamespace(),
    )

    client = TwitterClient()

    assert not hasattr(client, "client")
    assert not hasattr(client, "api")
    assert client.post_tweet("approved").data == {"id": "123"}


def test_cache_and_digest_fail_closed_after_clock_rollback_and_future_activity(tmp_path):
    path = str(tmp_path / "growth.db")
    db = Database(path)
    future = NOW + timedelta(days=1)
    common = {
        "score": 95,
        "discovery_source": "topic_search",
        "profile_expires_at": (future + timedelta(days=7)).isoformat(),
    }
    db.upsert_growth_candidate({
        **common,
        "user_id": "future_eval",
        "username": "future_eval",
        "profile": review_profile("future_eval", "future_eval"),
        "latest_post": review_post("701"),
        "score_data": accepted_score(),
        "last_evaluated_at": future.isoformat(),
    })
    db.upsert_growth_candidate({
        **common,
        "user_id": "future_post",
        "username": "future_post",
        "profile": review_profile("future_post", "future_post"),
        "latest_post": review_post("702", future.isoformat()),
        "score_data": accepted_score(activity_at=future.isoformat()),
        "last_evaluated_at": NOW.isoformat(),
    })
    db.upsert_growth_candidate({
        **common,
        "user_id": "valid_clock",
        "username": "valid_clock",
        "profile": review_profile("valid_clock", "valid_clock"),
        "latest_post": review_post("703"),
        "score_data": accepted_score(),
        "last_evaluated_at": NOW.isoformat(),
    })

    restarted = Database(path)
    assert restarted.get_cached_growth_candidate("future_eval", NOW) is None
    assert [row["user_id"] for row in restarted.get_digest_candidates(now=NOW)] == [
        "valid_clock"
    ]


def test_digest_ties_are_deterministic_by_exact_user_id_across_restart(tmp_path):
    path = str(tmp_path / "growth.db")
    db = Database(path)
    for user_id in ("b", "a"):
        db.upsert_growth_candidate({
            "user_id": user_id,
            "username": user_id,
            "profile": review_profile(user_id, user_id),
            "latest_post": review_post("600"),
            "score": 90,
            "score_data": accepted_score(total=90),
            "discovery_source": "topic_search",
            "profile_expires_at": (NOW + timedelta(days=7)).isoformat(),
            "last_evaluated_at": NOW.isoformat(),
        })
    assert [row["user_id"] for row in db.get_digest_candidates(now=NOW)] == [
        "a", "b"
    ]
    assert [
        row["user_id"] for row in Database(path).get_digest_candidates(now=NOW)
    ] == ["a", "b"]


def test_budget_day_uses_configured_business_timezone_and_seeds_are_validated(tmp_path):
    at_rome_next_day = datetime(2026, 8, 10, 23, 30, tzinfo=timezone.utc)
    fake_x = OrderedFollowerX([])
    growth = GrowthDiscovery(
        fake_x,
        Database(str(tmp_path / "growth.db")),
        query_budget=3,
        new_profile_budget=1,
        seed_accounts=("valid_seed", "bad/name", "@prefixed", True),
        topic_queries=("one", "two"),
    )
    growth.run(at_rome_next_day)

    assert fake_x.network_seeds == [("valid_seed",)]
    assert growth.db.get_state("growth_queries:2026-08-11") == "3"
