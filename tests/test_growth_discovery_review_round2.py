import json
import multiprocessing
import os
from datetime import timedelta

import pytest

from modules.database import Database
from modules.growth_discovery import GrowthDiscovery
from tests.test_growth_discovery_review import NOW, review_post, review_profile


SHARED_USER_ID = "shared-user"


class SharedCacheBarrierDatabase(Database):
    """Synchronize both cache reads that precede the paid-read claim."""

    def __init__(self, db_path, cache_barrier):
        self._cache_barrier = cache_barrier
        self._shared_cache_reads = 0
        super().__init__(db_path)

    def get_cached_growth_candidate(self, user_id, now):
        candidate = super().get_cached_growth_candidate(user_id, now)
        if user_id == SHARED_USER_ID and self._shared_cache_reads < 2:
            self._shared_cache_reads += 1
            self._cache_barrier.wait(timeout=30)
        return candidate


class ConcurrentProfileX:
    def __init__(self, worker_index):
        self.worker_index = worker_index
        self.latest_calls = []

    def get_followers_profiles(self):
        return [
            review_profile(SHARED_USER_ID, "shared_owner"),
            review_profile(f"unique-{self.worker_index}", f"worker_{self.worker_index}"),
        ]

    def search_recent_authors(self, _query):
        return []

    def get_network_candidates(self, _seeds):
        return []

    def get_latest_original_post(self, user_id):
        self.latest_calls.append(user_id)
        return review_post(f"8{self.worker_index + 1:03d}")


def _run_concurrent_profile_probe(
    db_path,
    worker_index,
    cache_barrier,
    start_event,
    result_queue,
):
    try:
        database = SharedCacheBarrierDatabase(db_path, cache_barrier)
        x_client = ConcurrentProfileX(worker_index)
        discovery = GrowthDiscovery(
            x_client,
            database,
            query_budget=3,
            new_profile_budget=25,
            seed_accounts=("valid_seed",),
            topic_queries=("one", "two"),
        )
        start_event.wait(timeout=30)
        discovery.run(NOW)
        result_queue.put({"latest": x_client.latest_calls})
    except BaseException as error:
        result_queue.put({"error": f"{type(error).__name__}: {error}"})


def test_profile_claim_is_atomic_per_business_day_and_user_across_25_processes(
    tmp_path,
):
    ctx = multiprocessing.get_context("spawn")
    db_path = str(tmp_path / "growth.db")
    Database(db_path)
    cache_barrier = ctx.Barrier(25)
    start_event = ctx.Event()
    result_queue = ctx.Queue()
    processes = [
        ctx.Process(
            target=_run_concurrent_profile_probe,
            args=(
                db_path,
                worker_index,
                cache_barrier,
                start_event,
                result_queue,
            ),
        )
        for worker_index in range(25)
    ]
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(timeout=45)
        assert process.exitcode == 0
    results = [result_queue.get(timeout=5) for _process in processes]
    assert all("error" not in result for result in results), results

    latest_calls = [
        user_id for result in results for user_id in result["latest"]
    ]
    assert len(latest_calls) == 25
    assert latest_calls.count(SHARED_USER_ID) == 1
    assert len({user_id for user_id in latest_calls if user_id != SHARED_USER_ID}) == 24
    database = Database(db_path)
    assert database.get_state("growth_profile_evaluations:2026-08-10") == "25"
    with database._conn() as conn:
        claim_count = conn.execute(
            "SELECT COUNT(*) AS count FROM growth_profile_claims "
            "WHERE observed_on = ?",
            ("2026-08-10",),
        ).fetchone()["count"]
    assert claim_count == 25


def test_profile_claim_outcomes_are_restart_safe_exact_and_reset_next_day(tmp_path):
    path = str(tmp_path / "growth.db")
    database = Database(path)

    assert database.claim_growth_profile_evaluation(
        "2026-08-10", "exact-user", 25,
    ) == "claimed"
    assert Database(path).claim_growth_profile_evaluation(
        "2026-08-10", "exact-user", 25,
    ) == "already_claimed"
    assert Database(path).claim_growth_profile_evaluation(
        "2026-08-11", "exact-user", 25,
    ) == "claimed"
    assert database.claim_growth_profile_evaluation(
        "2026-08-10", 1, 25,
    ) == "budget_exhausted"
    assert database.claim_growth_profile_evaluation(
        "2026-08-10", True, 25,
    ) == "budget_exhausted"
    assert database.get_state("growth_profile_evaluations:2026-08-10") == "1"
    assert database.get_state("growth_profile_evaluations:2026-08-11") == "1"


def _crash_around_profile_claim(db_path, crash_before_commit):
    database = Database(db_path)
    if crash_before_commit:
        database._now_iso = lambda: os._exit(17)
    outcome = database.claim_growth_profile_evaluation(
        "2026-08-10", "crash-user", 25,
    )
    os._exit(18 if outcome == "claimed" else 19)


def test_profile_claim_transaction_rolls_back_before_commit_and_survives_after(
    tmp_path,
):
    ctx = multiprocessing.get_context("spawn")
    before_path = str(tmp_path / "before.db")
    Database(before_path)
    before = ctx.Process(
        target=_crash_around_profile_claim, args=(before_path, True),
    )
    before.start()
    before.join(timeout=20)
    assert before.exitcode == 17
    before_db = Database(before_path)
    assert before_db.get_state("growth_profile_evaluations:2026-08-10", "0") == "0"
    assert before_db.claim_growth_profile_evaluation(
        "2026-08-10", "crash-user", 25,
    ) == "claimed"

    after_path = str(tmp_path / "after.db")
    Database(after_path)
    after = ctx.Process(
        target=_crash_around_profile_claim, args=(after_path, False),
    )
    after.start()
    after.join(timeout=20)
    assert after.exitcode == 18
    after_db = Database(after_path)
    assert after_db.get_state("growth_profile_evaluations:2026-08-10") == "1"
    assert after_db.claim_growth_profile_evaluation(
        "2026-08-10", "crash-user", 25,
    ) == "already_claimed"


class SingleFollowerX:
    def __init__(self, user_id="cache-user", username="cache_owner", error=False):
        self.profile = review_profile(user_id, username)
        self.error = error
        self.latest_calls = []

    def get_followers_profiles(self):
        return [self.profile]

    def search_recent_authors(self, _query):
        return []

    def get_network_candidates(self, _seeds):
        return []

    def get_latest_original_post(self, user_id):
        self.latest_calls.append(user_id)
        if self.error:
            raise RuntimeError("planned latest-post failure")
        return review_post("811")


def _persist_complete_candidate(database, user_id="cache-user", username="cache_owner"):
    database.upsert_growth_candidate({
        "user_id": user_id,
        "username": username,
        "profile": review_profile(user_id, username),
        "latest_post": review_post("810"),
        "score": 95,
        "score_data": {
            "total": 95,
            "audience_segment": "primary",
            "reasons": ["primary_operator_role"],
            "activity_at": (NOW - timedelta(days=1)).isoformat(),
            "hard_filter_passed": True,
            "filter_reason": "accepted",
        },
        "discovery_source": "topic_search",
        "last_evaluated_at": NOW.isoformat(),
        "profile_expires_at": (NOW + timedelta(days=7)).isoformat(),
    })


@pytest.mark.parametrize(
    ("column", "malformed_value"),
    [
        ("score", "not-an-integer"),
        ("username", "bad/name"),
        (
            "score_json",
            json.dumps({
                "total": "95",
                "audience_segment": "primary",
                "reasons": ["primary_operator_role"],
                "activity_at": (NOW - timedelta(days=1)).isoformat(),
                "hard_filter_passed": True,
                "filter_reason": "accepted",
            }),
        ),
        ("latest_post_json", json.dumps({"is_original": True})),
        (
            "profile_json",
            json.dumps(review_profile("different-user", "cache_owner")),
        ),
    ],
)
def test_malformed_cache_fields_force_full_run_reevaluation(
    tmp_path,
    column,
    malformed_value,
):
    database = Database(str(tmp_path / "growth.db"))
    _persist_complete_candidate(database)
    with database._conn() as conn:
        conn.execute(
            f"UPDATE growth_candidates SET {column} = ? WHERE user_id = ?",
            (malformed_value, "cache-user"),
        )
    x_client = SingleFollowerX()
    discovery = GrowthDiscovery(
        x_client,
        database,
        query_budget=3,
        new_profile_budget=25,
        seed_accounts=("valid_seed",),
        topic_queries=("one", "two"),
    )

    discovery.run(NOW)

    assert x_client.latest_calls == ["cache-user"]
    refreshed = database.get_growth_candidate("cache-user")
    assert refreshed["score"] == 95
    assert refreshed["username"] == "cache_owner"
    assert refreshed["score_data"]["total"] == 95


def test_latest_post_error_consumes_persistent_user_claim_and_one_slot(tmp_path):
    database = Database(str(tmp_path / "growth.db"))
    x_client = SingleFollowerX("error-user", "error_owner", error=True)
    discovery = GrowthDiscovery(
        x_client,
        database,
        query_budget=3,
        new_profile_budget=25,
        seed_accounts=("valid_seed",),
        topic_queries=("one", "two"),
    )

    discovery.run(NOW)
    with database._conn() as conn:
        conn.execute(
            "DELETE FROM growth_candidates WHERE user_id = ?", ("error-user",),
        )
    discovery.run(NOW)

    assert x_client.latest_calls == ["error-user"]
    assert database.get_state("growth_profile_evaluations:2026-08-10") == "1"


def test_suppression_is_independent_of_candidate_decoder_and_fail_closed(tmp_path):
    database = Database(str(tmp_path / "growth.db"))
    _persist_complete_candidate(database, "suppressed-user", "suppress_owner")
    future = (NOW + timedelta(days=10)).isoformat()
    with database._conn() as conn:
        conn.execute(
            "UPDATE growth_candidates SET score_json = '[]', decision = 'discarded', "
            "suppressed_until = ? WHERE user_id = ?",
            (future, "suppressed-user"),
        )
    x_client = SingleFollowerX("suppressed-user", "suppress_owner")
    discovery = GrowthDiscovery(
        x_client,
        database,
        query_budget=3,
        new_profile_budget=25,
        seed_accounts=("valid_seed",),
        topic_queries=("one", "two"),
    )

    discovery.run(NOW)

    assert x_client.latest_calls == []
    with database._conn() as conn:
        stored = conn.execute(
            "SELECT suppressed_until FROM growth_candidates WHERE user_id = ?",
            ("suppressed-user",),
        ).fetchone()["suppressed_until"]
    assert stored == future


def test_suppression_timestamp_policy_is_future_true_expired_false_malformed_true(
    tmp_path,
):
    database = Database(str(tmp_path / "growth.db"))
    _persist_complete_candidate(database, "policy-user", "policy_owner")

    with database._conn() as conn:
        conn.execute(
            "UPDATE growth_candidates SET suppressed_until = ? WHERE user_id = ?",
            ((NOW + timedelta(seconds=1)).isoformat(), "policy-user"),
        )
    assert database.is_growth_candidate_suppressed("policy-user", NOW) is True

    with database._conn() as conn:
        conn.execute(
            "UPDATE growth_candidates SET suppressed_until = ? WHERE user_id = ?",
            ((NOW - timedelta(seconds=1)).isoformat(), "policy-user"),
        )
    assert database.is_growth_candidate_suppressed("policy-user", NOW) is False

    with database._conn() as conn:
        conn.execute(
            "UPDATE growth_candidates SET suppressed_until = ? WHERE user_id = ?",
            ("not-a-timestamp", "policy-user"),
        )
    assert database.is_growth_candidate_suppressed("policy-user", NOW) is True
    with database._conn() as conn:
        stored = conn.execute(
            "SELECT suppressed_until FROM growth_candidates WHERE user_id = ?",
            ("policy-user",),
        ).fetchone()["suppressed_until"]
    assert stored == "not-a-timestamp"
