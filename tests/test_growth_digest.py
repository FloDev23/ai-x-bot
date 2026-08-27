import json
import multiprocessing
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from modules.twitter_client import TwitterClient
from modules.twitter_client import RelevantPostsRead
from modules.database import Database
from modules.growth_digest import GrowthDigestService, score_growth_post


NOW = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)


def _client(backend):
    client = TwitterClient.__new__(TwitterClient)
    client._client = backend
    return client


def _author(user_id="101", username="gymowner", **overrides):
    values = {
        "id": user_id,
        "username": username,
        "protected": False,
        "public_metrics": {
            "followers_count": 1200,
            "following_count": 300,
            "tweet_count": 800,
            "listed_count": 12,
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _post(post_id="9001", author_id="101", **overrides):
    values = {
        "id": post_id,
        "text": "Gym owners can fill empty class capacity with drop-in bookings.",
        "author_id": author_id,
        "created_at": datetime.now(timezone.utc) - timedelta(hours=2),
        "lang": "en",
        "public_metrics": {
            "like_count": 20,
            "retweet_count": 4,
            "reply_count": 2,
            "quote_count": 1,
            "impression_count": 5000,
        },
        "referenced_tweets": [],
        "entities": {
            "urls": [{"expanded_url": "https://example.com/gym-capacity"}],
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_relevant_post_read_requests_exact_fields_and_returns_closed_projection():
    class Backend:
        def __init__(self):
            self.calls = []

        def search_recent_tweets(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                data=[_post()], includes={"users": [_author()]}, meta={}
            )

    backend = Backend()
    rows = _client(backend).search_relevant_posts("gym owner", limit=25)

    assert backend.calls == [{
        "query": "gym owner",
        "max_results": 25,
        "tweet_fields": [
            "id", "text", "author_id", "created_at", "lang",
            "public_metrics", "referenced_tweets", "entities",
        ],
        "expansions": ["author_id"],
        "user_fields": ["id", "username", "protected", "public_metrics"],
    }]
    assert rows == [{
        "id": "9001",
        "text": "Gym owners can fill empty class capacity with drop-in bookings.",
        "author_id": "101",
        "author_username": "gymowner",
        "created_at": rows[0]["created_at"],
        "lang": "en",
        "public_metrics": {
            "like_count": 20,
            "retweet_count": 4,
            "reply_count": 2,
            "quote_count": 1,
            "impression_count": 5000,
        },
        "author_public_metrics": {
            "followers_count": 1200,
            "following_count": 300,
            "tweet_count": 800,
            "listed_count": 12,
        },
    }]
    assert json.loads(json.dumps(rows, allow_nan=False)) == rows


def test_relevant_post_read_isolates_malformed_and_unsafe_records():
    now = datetime.now(timezone.utc)
    invalid = [
        _post(True),
        _post("０１"),
        _post("0"),
        _post("2", author_id="missing"),
        _post("3", author_id="102"),
        _post("4", referenced_tweets=[SimpleNamespace(type="retweeted")]),
        _post("5", referenced_tweets=[SimpleNamespace(type="replied_to")]),
        _post("6", created_at=now + timedelta(hours=1)),
        _post("7", created_at=now - timedelta(days=31)),
        _post("8", text="x" * 1001),
        _post("9", public_metrics={"like_count": True}),
        _post("12", public_metrics={"like_count": 1}),
        _post("10", entities={"urls": [{"expanded_url": "http://bad.test"}]}),
        _post(
            "13",
            text="Gym owners should read http://bad.test before filling classes.",
            entities=None,
        ),
    ]

    class Backend:
        def search_recent_tweets(self, **_kwargs):
            return SimpleNamespace(
                data=invalid + [_post("11")],
                includes={
                    "users": [
                        _author("101"),
                        _author("102", "private", protected=True),
                    ]
                },
                meta={},
            )

    assert [row["id"] for row in _client(Backend()).search_relevant_posts("x")] == [
        "11"
    ]


def test_relevant_post_read_paginates_deduplicates_and_stops_repeated_token():
    class Backend:
        def __init__(self):
            self.tokens = []

        def search_recent_tweets(self, **kwargs):
            token = kwargs.get("next_token")
            self.tokens.append(token)
            if token is None:
                return SimpleNamespace(
                    data=[_post("20")],
                    includes={"users": [_author()]},
                    meta={"next_token": "repeat"},
                )
            return SimpleNamespace(
                data=[_post("20"), _post("21")],
                includes={"users": [_author()]},
                meta={"next_token": "repeat"},
            )

    backend = Backend()
    rows = _client(backend).search_relevant_posts("capacity", limit=30)

    assert [row["id"] for row in rows] == ["20", "21"]
    assert backend.tokens == [None, "repeat"]


def test_relevant_post_read_keeps_safe_partial_page_and_redacts_exception(caplog):
    class Backend:
        def __init__(self):
            self.calls = 0

        def search_recent_tweets(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(
                    data=[_post("30")],
                    includes={"users": [_author()]},
                    meta={"next_token": "next"},
                )
            raise RuntimeError("Bearer secret-payload")

    rows = _client(Backend()).search_relevant_posts("pilates", limit=30)

    assert [row["id"] for row in rows] == ["30"]
    assert "secret-payload" not in caplog.text
    assert "error_type=RuntimeError" in caplog.text


def test_relevant_post_read_treats_a_valid_empty_page_as_complete():
    class Backend:
        def search_recent_tweets(self, **_kwargs):
            return SimpleNamespace(data=[], includes={}, meta={"result_count": 0})

    result = _client(Backend()).read_relevant_posts("gym capacity")

    assert result == RelevantPostsRead((), True)


def test_relevant_post_read_isolates_an_author_whose_properties_raise():
    class ExplodingAuthor:
        id = "999"

        @property
        def username(self):
            raise RuntimeError("Bearer author-secret")

    class Backend:
        def search_recent_tweets(self, **_kwargs):
            return SimpleNamespace(
                data=[_post("41")],
                includes={"users": [ExplodingAuthor(), _author()]},
                meta={},
            )

    result = _client(Backend()).read_relevant_posts("gym owner")

    assert [row["id"] for row in result.posts] == ["41"]
    assert result.complete is True


def _account_row(object_id="101", username="gymowner"):
    reasons = ["primary_operator_role", "active_within_7_days"]
    return {
        "object_id": object_id,
        "username": username,
        "payload": {
            "user_id": object_id,
            "username": username,
            "public_metrics": {
                "followers_count": 1200,
                "following_count": 300,
                "tweet_count": 800,
                "listed_count": 12,
            },
            "latest_activity_id": "8001",
            "latest_activity_at": (NOW - timedelta(hours=2)).isoformat(),
            "segment": "primary",
            "reason_codes": reasons,
        },
        "score": 90,
        "reason_codes": reasons,
        "cooldown_until": (NOW + timedelta(days=30)).isoformat(),
    }


def _post_row(object_id="9001", username="gymowner"):
    reasons = ["gym_owner", "empty_capacity", "drop_in", "recent"]
    return {
        "object_id": object_id,
        "username": username,
        "payload": {
            "id": object_id,
            "author_id": "101",
            "author_username": username,
            "excerpt": "Gym owners can fill empty class capacity with drop-ins.",
            "created_at": (NOW - timedelta(hours=2)).isoformat(),
            "public_metrics": {
                "like_count": 20,
                "retweet_count": 4,
                "reply_count": 2,
                "quote_count": 1,
                "impression_count": 5000,
            },
            "reason_codes": reasons,
        },
        "score": 95,
        "reason_codes": reasons,
        "cooldown_until": (NOW + timedelta(days=30)).isoformat(),
    }


def test_growth_schema_and_atomic_persist_are_restart_safe(tmp_path):
    path = str(tmp_path / "digest.db")
    database = Database(path)

    digest, outcome = database.persist_growth_digest_atomic(
        observed_on="2026-08-26",
        account_rows=[_account_row()],
        post_rows=[_post_row()],
        reevaluate_rows=[],
        completed_at=NOW.isoformat(),
    )
    replay, replay_outcome = Database(path).persist_growth_digest_atomic(
        observed_on="2026-08-26",
        account_rows=[],
        post_rows=[],
        reevaluate_rows=[],
        completed_at=(NOW + timedelta(hours=1)).isoformat(),
    )

    assert outcome == "created"
    assert replay_outcome == "existing"
    assert replay == digest
    assert [row["object_id"] for row in digest["accounts"]] == ["101"]
    assert [row["object_id"] for row in digest["posts"]] == ["9001"]
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM growth_digest_runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM growth_suggestions").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM growth_read_claims").fetchone()[0] == 0


def test_growth_persist_rejects_open_or_secret_bearing_payloads(tmp_path):
    database = Database(str(tmp_path / "closed.db"))
    hostile = _post_row()
    hostile["payload"]["raw_response"] = {"authorization": "Bearer secret"}

    digest, outcome = database.persist_growth_digest_atomic(
        observed_on="2026-08-26",
        account_rows=[],
        post_rows=[hostile],
        reevaluate_rows=[],
        completed_at=NOW.isoformat(),
    )

    assert outcome == "invalid"
    assert digest == {}
    with database._conn() as connection:
        assert connection.execute("SELECT COUNT(*) FROM growth_digest_runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM growth_suggestions").fetchone()[0] == 0


def test_growth_persist_rolls_back_all_rows_and_retries_after_write_error(tmp_path):
    path = str(tmp_path / "rollback.db")
    database = Database(path)
    with database._conn() as connection:
        connection.execute("""
            CREATE TRIGGER fail_post BEFORE INSERT ON growth_suggestions
            WHEN NEW.kind = 'post' BEGIN SELECT RAISE(ABORT, 'planned'); END
        """)
    with pytest.raises(sqlite3.IntegrityError):
        database.persist_growth_digest_atomic(
            observed_on="2026-08-26",
            account_rows=[_account_row()],
            post_rows=[_post_row()],
            reevaluate_rows=[],
            completed_at=NOW.isoformat(),
        )
    with database._conn() as connection:
        assert connection.execute("SELECT COUNT(*) FROM growth_suggestions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM growth_digest_runs").fetchone()[0] == 0
        connection.execute("DROP TRIGGER fail_post")

    digest, outcome = Database(path).persist_growth_digest_atomic(
        observed_on="2026-08-26",
        account_rows=[_account_row()],
        post_rows=[_post_row()],
        reevaluate_rows=[],
        completed_at=NOW.isoformat(),
    )
    assert outcome == "created"
    assert len(digest["accounts"]) == len(digest["posts"]) == 1


def test_growth_persist_has_one_cross_connection_winner(tmp_path):
    path = str(tmp_path / "race.db")
    Database(path)

    def persist():
        return Database(path).persist_growth_digest_atomic(
            observed_on="2026-08-26",
            account_rows=[_account_row()],
            post_rows=[_post_row()],
            reevaluate_rows=[],
            completed_at=NOW.isoformat(),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: persist(), range(2)))

    assert sorted(outcome for _digest, outcome in results) == ["created", "existing"]
    assert results[0][0] == results[1][0]


def test_read_claim_budget_completion_and_stale_recovery_survive_restart(tmp_path):
    path = str(tmp_path / "claims.db")
    database = Database(path)
    expires = NOW + timedelta(minutes=5)

    assert database.claim_growth_read_query(
        "2026-08-26", "gym", NOW, expires, budget=2
    ) == "claimed"
    assert Database(path).claim_growth_read_query(
        "2026-08-26", "gym", NOW + timedelta(minutes=1), expires, budget=2
    ) == "busy"
    assert database.complete_growth_read_query("2026-08-26", "gym", NOW) is True
    assert Database(path).claim_growth_read_query(
        "2026-08-26", "gym", NOW + timedelta(minutes=6),
        NOW + timedelta(minutes=11), budget=2
    ) == "completed"
    assert database.claim_growth_read_query(
        "2026-08-26", "pilates", NOW, expires, budget=2
    ) == "claimed"
    assert database.claim_growth_read_query(
        "2026-08-26", "martial", NOW, expires, budget=2
    ) == "budget_exhausted"

    stale_day = "2026-08-27"
    assert database.claim_growth_read_query(
        stale_day, "gym", NOW, expires, budget=2
    ) == "claimed"
    assert Database(path).claim_growth_read_query(
        stale_day,
        "gym",
        NOW + timedelta(minutes=6),
        NOW + timedelta(minutes=11),
        budget=2,
    ) == "claimed"


def test_reevaluation_requires_complete_absent_snapshot_after_fourteen_days(tmp_path):
    database = Database(str(tmp_path / "reevaluate.db"))
    candidate_id = database.upsert_growth_candidate({
        "user_id": "301",
        "username": "oldowner",
        "profile": {
            "id": "301", "user_id": "301", "username": "oldowner",
            "description": "gym owner", "protected": False, "location": None,
            "created_at": "2020-01-01T00:00:00+00:00",
            "followers_count": 500, "following_count": 100,
            "tweet_count": 100, "listed_count": 2, "spam_signals": [],
        },
        "latest_post": {
            "id": "7001", "text": "gym class booking occupancy",
            "created_at": (NOW - timedelta(days=1)).isoformat(),
            "lang": "en", "is_original": True,
        },
        "score": 90,
        "score_data": {
            "total": 90, "audience_segment": "primary",
            "reasons": ["primary_operator_role"],
            "activity_at": (NOW - timedelta(days=1)).isoformat(),
            "hard_filter_passed": True, "filter_reason": "accepted",
        },
        "discovery_source": "topic_search",
        "last_evaluated_at": NOW.isoformat(),
        "profile_expires_at": (NOW + timedelta(days=7)).isoformat(),
    })
    assert database.mark_candidate_decision(
        candidate_id, "followed_manually", decided_at=NOW - timedelta(days=15)
    )
    assert database.get_growth_reevaluation_candidates(NOW, limit=5) == []

    with database._conn() as connection:
        connection.execute(
            "INSERT INTO follower_snapshot_runs "
            "(observed_on, followers_total, captured_at, completed, summary_json) "
            "VALUES (?, ?, ?, 0, '{}')",
            ("2026-08-26", 0, NOW.isoformat()),
        )
    assert database.get_growth_reevaluation_candidates(NOW, limit=5) == []
    with database._conn() as connection:
        connection.execute(
            "UPDATE follower_snapshot_runs SET completed = 1 WHERE observed_on = ?",
            ("2026-08-26",),
        )
    rows = database.get_growth_reevaluation_candidates(NOW, limit=5)
    assert [row["user_id"] for row in rows] == ["301"]
    assert "description" not in json.dumps(rows)


def test_reevaluation_accepts_older_complete_snapshot_after_accounts_own_boundary(
    tmp_path,
):
    database = Database(str(tmp_path / "old-complete.db"))
    candidate_id = database.upsert_growth_candidate({
        "user_id": "401",
        "username": "longpending",
        "profile": {
            "id": "401", "user_id": "401", "username": "longpending",
            "description": "gym owner", "protected": False, "location": None,
            "created_at": "2020-01-01T00:00:00+00:00",
            "followers_count": 500, "following_count": 100,
            "tweet_count": 100, "listed_count": 2, "spam_signals": [],
        },
        "latest_post": {
            "id": "7401", "text": "gym class booking occupancy",
            "created_at": (NOW - timedelta(days=1)).isoformat(),
            "lang": "en", "is_original": True,
        },
        "score": 90,
        "score_data": {
            "total": 90, "audience_segment": "primary",
            "reasons": ["primary_operator_role"],
            "activity_at": (NOW - timedelta(days=1)).isoformat(),
            "hard_filter_passed": True, "filter_reason": "accepted",
        },
        "discovery_source": "topic_search",
        "last_evaluated_at": NOW.isoformat(),
        "profile_expires_at": (NOW + timedelta(days=7)).isoformat(),
    })
    followed_at = NOW - timedelta(days=40)
    assert database.mark_candidate_decision(
        candidate_id, "followed_manually", decided_at=followed_at
    )
    snapshot_at = NOW - timedelta(days=20)
    with database._conn() as connection:
        connection.execute(
            "INSERT INTO follower_snapshot_runs "
            "(observed_on, followers_total, captured_at, completed, summary_json) "
            "VALUES (?, 0, ?, 1, '{}')",
            (snapshot_at.date().isoformat(), snapshot_at.isoformat()),
        )

    assert [
        row["user_id"]
        for row in database.get_growth_reevaluation_candidates(NOW, limit=5)
    ] == ["401"]


def _candidate(user_id="101", username="gymowner", score=90):
    reasons = ["primary_operator_role", "active_within_7_days"]
    return {
        "user_id": user_id,
        "username": username,
        "profile": {
            "id": user_id,
            "user_id": user_id,
            "username": username,
            "description": "private body must never persist",
            "followers_count": 1200,
            "following_count": 300,
            "tweet_count": 800,
            "listed_count": 12,
        },
        "latest_post": {
            "id": "8001",
            "text": "source body must never persist",
            "created_at": (NOW - timedelta(hours=2)).isoformat(),
        },
        "score": score,
        "audience_segment": "primary",
        "reasons": reasons,
        "activity_at": (NOW - timedelta(hours=2)).isoformat(),
    }


def _normalized_post(post_id="9001", text=None, **overrides):
    values = {
        "id": post_id,
        "text": text or (
            "Gym owners can fill empty class capacity with drop-in bookings."
        ),
        "author_id": "101",
        "author_username": "gymowner",
        "created_at": (NOW - timedelta(hours=2)).isoformat(),
        "lang": "en",
        "public_metrics": {
            "like_count": 20,
            "retweet_count": 4,
            "reply_count": 2,
            "quote_count": 1,
            "impression_count": 5000,
        },
        "author_public_metrics": {
            "followers_count": 1200,
            "following_count": 300,
            "tweet_count": 800,
            "listed_count": 12,
        },
    }
    values.update(overrides)
    return values


class DigestDiscovery:
    def __init__(self, rows=None):
        self.rows = [_candidate()] if rows is None else rows
        self.calls = 0

    def run(self, _now):
        self.calls += 1
        return list(self.rows)


class DigestX:
    def __init__(self, pages=None, complete=True):
        self.pages = pages or {}
        self.complete = complete
        self.queries = []
        self.engagement_writes = []

    def read_relevant_posts(self, query, limit=25):
        self.queries.append((query, limit))
        rows = self.pages.get(len(self.queries) - 1, [])
        return RelevantPostsRead(tuple(rows), self.complete)


def test_service_builds_closed_ranked_daily_digest_with_fixed_read_budget(tmp_path):
    database = Database(str(tmp_path / "service.db"))
    x_client = DigestX({
        0: [
            _normalized_post("9002", "Pilates studios can fill empty class spots."),
            _normalized_post("9001"),
        ],
        1: [
            _normalized_post("9001"),
            _normalized_post("9003", "A generic fitness motivation quote."),
        ],
    })
    discovery = DigestDiscovery()

    digest = GrowthDigestService(
        x_client, database, discovery=discovery, post_query_budget=2
    ).build(NOW)

    assert digest["observed_on"] == "2026-08-26"
    assert digest["outcome"] == "created"
    assert [row["object_id"] for row in digest["accounts"]] == ["101"]
    assert [row["object_id"] for row in digest["posts"]] == ["9001", "9002"]
    assert digest["posts"][0]["score"] == 89
    assert len(x_client.queries) == 2
    assert all(limit == 25 for _query, limit in x_client.queries)
    assert discovery.calls == 1
    serialized = json.dumps(digest, allow_nan=False)
    assert "private body" not in serialized
    assert "source body" not in serialized
    assert "generic fitness motivation" not in serialized
    assert x_client.engagement_writes == []


def test_fitness_business_operations_are_relevant_without_a_second_topic():
    scored = score_growth_post(
        _normalized_post(
            "9050", "Fitness business revenue operations need better systems."
        ),
        NOW,
    )

    assert scored is not None
    assert "fitness_operations" in scored["reason_codes"]


def test_service_replays_exact_persisted_rows_before_any_x_read(tmp_path):
    path = str(tmp_path / "replay.db")
    first_x = DigestX({0: [_normalized_post()], 1: []})
    first_discovery = DigestDiscovery()
    first = GrowthDigestService(
        first_x, Database(path), discovery=first_discovery
    ).build(NOW)
    replay_x = DigestX({0: [_normalized_post("9999")], 1: []})
    replay_discovery = DigestDiscovery([_candidate("202", "otherowner")])

    replay = GrowthDigestService(
        replay_x, Database(path), discovery=replay_discovery
    ).build(NOW + timedelta(hours=1))

    assert replay["outcome"] == "existing"
    assert replay["accounts"] == first["accounts"]
    assert replay["posts"] == first["posts"]
    assert replay_x.queries == []
    assert replay_discovery.calls == 0


def test_service_incomplete_read_commits_nothing_and_no_cooldown(tmp_path):
    path = str(tmp_path / "incomplete.db")
    x_client = DigestX({0: [_normalized_post()]}, complete=False)
    digest = GrowthDigestService(
        x_client, Database(path), discovery=DigestDiscovery()
    ).build(NOW)

    assert digest == {
        "observed_on": "2026-08-26", "accounts": [], "posts": [],
        "reevaluate": [], "outcome": "incomplete",
    }
    restarted = Database(path)
    assert restarted.get_growth_digest("2026-08-26") is None
    assert restarted.growth_object_in_cooldown("post", "9001", NOW) is False


def test_service_post_cooldown_survives_restart_and_clock_rollback(tmp_path):
    path = str(tmp_path / "cooldown.db")
    first = DigestX({0: [_normalized_post()], 1: []})
    GrowthDigestService(first, Database(path), discovery=DigestDiscovery([])).build(NOW)

    tomorrow = NOW + timedelta(days=1)
    second = DigestX({0: [_normalized_post()], 1: []})
    digest = GrowthDigestService(
        second, Database(path), discovery=DigestDiscovery([])
    ).build(tomorrow)
    assert digest["posts"] == []

    rollback = NOW - timedelta(days=1)
    third = DigestX({0: [_normalized_post()], 1: []})
    rolled_back = GrowthDigestService(
        third, Database(path), discovery=DigestDiscovery([])
    ).build(rollback)
    assert rolled_back["posts"] == []


def test_service_isolates_future_account_activity_without_aborting_digest(tmp_path):
    future = _candidate()
    future["latest_post"]["created_at"] = (NOW + timedelta(hours=1)).isoformat()
    future["activity_at"] = future["latest_post"]["created_at"]

    digest = GrowthDigestService(
        DigestX(), Database(str(tmp_path / "future-account.db")),
        discovery=DigestDiscovery([future]),
    ).build(NOW)

    assert digest["outcome"] == "created"
    assert digest["accounts"] == []


@pytest.mark.parametrize(
    ("instant", "observed_on"),
    [
        (datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc), "2026-03-29"),
        (datetime(2026, 3, 29, 22, 30, tzinfo=timezone.utc), "2026-03-30"),
        (datetime(2026, 10, 25, 22, 30, tzinfo=timezone.utc), "2026-10-25"),
    ],
)
def test_service_uses_rome_date_across_dst_boundaries(tmp_path, instant, observed_on):
    digest = GrowthDigestService(
        DigestX(), Database(str(tmp_path / f"{observed_on}.db")),
        discovery=DigestDiscovery([]),
    ).build(instant)
    assert digest["observed_on"] == observed_on


def test_service_two_threads_return_one_exact_committed_digest(tmp_path):
    path = str(tmp_path / "service-race.db")
    Database(path)
    x_client = DigestX({0: [_normalized_post()], 1: []})
    discovery = DigestDiscovery()

    def build():
        return GrowthDigestService(
            x_client, Database(path), discovery=discovery
        ).build(NOW)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: build(), range(2)))

    assert results[0]["accounts"] == results[1]["accounts"]
    assert results[0]["posts"] == results[1]["posts"]
    assert sorted(result["outcome"] for result in results) == ["created", "existing"]
    assert len(x_client.queries) == 2
    with Database(path)._conn() as connection:
        assert connection.execute("SELECT COUNT(*) FROM growth_digest_runs").fetchone()[0] == 1


def test_service_fails_closed_on_malformed_completed_run_without_reads(tmp_path):
    path = str(tmp_path / "malformed-run.db")
    database = Database(path)
    with database._conn() as connection:
        connection.execute(
            "INSERT INTO growth_digest_runs VALUES (?, ?, ?)",
            ("2026-08-26", NOW.isoformat(), "{malformed"),
        )
    x_client = DigestX({0: [_normalized_post()]})
    discovery = DigestDiscovery()

    digest = GrowthDigestService(x_client, Database(path), discovery=discovery).build(NOW)

    assert digest["outcome"] == "invalid_persisted"
    assert digest["accounts"] == digest["posts"] == []
    assert x_client.queries == []
    assert discovery.calls == 0


def test_service_fails_closed_on_open_persisted_payload_without_leaking_it(tmp_path):
    path = str(tmp_path / "malformed-payload.db")
    database = Database(path)
    database.persist_growth_digest_atomic(
        observed_on="2026-08-26",
        account_rows=[],
        post_rows=[_post_row()],
        reevaluate_rows=[],
        completed_at=NOW.isoformat(),
    )
    with database._conn() as connection:
        payload = json.loads(connection.execute(
            "SELECT payload_json FROM growth_suggestions"
        ).fetchone()[0])
        payload["token"] = "secret"
        connection.execute(
            "UPDATE growth_suggestions SET payload_json = ?",
            (json.dumps(payload),),
        )
    x_client = DigestX()
    discovery = DigestDiscovery()

    digest = GrowthDigestService(x_client, Database(path), discovery=discovery).build(NOW)

    assert digest == {
        "observed_on": "2026-08-26", "accounts": [], "posts": [],
        "reevaluate": [], "outcome": "invalid_persisted",
    }
    assert "secret" not in json.dumps(digest)
    assert x_client.queries == []
    assert discovery.calls == 0


def _process_build_digest(path, barrier, output):
    barrier.wait()
    result = GrowthDigestService(
        DigestX({0: [_normalized_post()], 1: []}),
        Database(path),
        discovery=DigestDiscovery(),
    ).build(NOW)
    output.put(result)


def _process_claim_then_crash(path):
    database = Database(path)
    database.claim_growth_digest_build(
        "2026-08-26", NOW, NOW + timedelta(seconds=1)
    )
    os._exit(23)


def _process_commit_then_crash(path):
    Database(path).persist_growth_digest_atomic(
        observed_on="2026-08-26",
        account_rows=[_account_row()],
        post_rows=[_post_row()],
        reevaluate_rows=[],
        completed_at=NOW.isoformat(),
    )
    os._exit(29)


def test_two_processes_share_one_completed_digest_and_exact_rows(tmp_path):
    path = str(tmp_path / "process-race.db")
    Database(path)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    output = context.Queue()
    processes = [
        context.Process(target=_process_build_digest, args=(path, barrier, output))
        for _index in range(2)
    ]
    for process in processes:
        process.start()
    results = [output.get(timeout=10) for _process in processes]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert results[0]["accounts"] == results[1]["accounts"]
    assert results[0]["posts"] == results[1]["posts"]
    assert sorted(result["outcome"] for result in results) == ["created", "existing"]
    with Database(path)._conn() as connection:
        assert connection.execute("SELECT COUNT(*) FROM growth_digest_runs").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM growth_read_claims "
            "WHERE substr(query_key, 1, 2) != '__' AND state = 'completed'"
        ).fetchone()[0] == 2


def test_hard_crash_before_commit_recovers_only_after_lease_expiry(tmp_path):
    path = str(tmp_path / "precommit-crash.db")
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_process_claim_then_crash, args=(path,))
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 23

    before_expiry = GrowthDigestService(
        DigestX(), Database(path), discovery=DigestDiscovery([]), wait_attempts=0
    ).build(NOW + timedelta(milliseconds=500))
    recovered = GrowthDigestService(
        DigestX(), Database(path), discovery=DigestDiscovery([])
    ).build(NOW + timedelta(seconds=2))

    assert before_expiry["outcome"] == "incomplete"
    assert recovered["outcome"] == "created"


def test_hard_crash_after_commit_replays_without_any_read(tmp_path):
    path = str(tmp_path / "postcommit-crash.db")
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_process_commit_then_crash, args=(path,))
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 29
    x_client = DigestX({0: [_normalized_post("9999")]})
    discovery = DigestDiscovery([_candidate("202", "otherowner")])

    replay = GrowthDigestService(
        x_client, Database(path), discovery=discovery
    ).build(NOW + timedelta(hours=1))

    assert replay["outcome"] == "existing"
    assert [row["object_id"] for row in replay["accounts"]] == ["101"]
    assert [row["object_id"] for row in replay["posts"]] == ["9001"]
    assert x_client.queries == []
    assert discovery.calls == 0
