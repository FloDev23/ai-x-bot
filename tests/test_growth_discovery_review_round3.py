import json
from datetime import timedelta

import pytest

from modules.database import Database
from modules.growth_discovery import GrowthDiscovery
from tests.test_growth_discovery_review import NOW, review_post, review_profile
from tests.test_growth_discovery_review_round2 import _persist_complete_candidate


class NoDiscoveryX:
    def get_followers_profiles(self):
        return []

    def search_recent_authors(self, _query):
        return []

    def get_network_candidates(self, _seeds):
        return []

    def get_latest_original_post(self, _user_id):
        raise AssertionError("a stored candidate must not cause an X read")


def _candidate_row(database, user_id="cache-user"):
    with database._conn() as conn:
        return conn.execute(
            "SELECT * FROM growth_candidates WHERE user_id = ?", (user_id,),
        ).fetchone()


def _raw_json(database, column, user_id="cache-user"):
    row = _candidate_row(database, user_id)
    return json.loads(row[column])


def _set_column(database, column, value, user_id="cache-user"):
    with database._conn() as conn:
        conn.execute(
            f"UPDATE growth_candidates SET {column} = ? WHERE user_id = ?",
            (value, user_id),
        )


def test_corrupt_total_is_rejected_identically_by_cache_digest_and_full_run(
    tmp_path,
):
    database = Database(str(tmp_path / "growth.db"))
    _persist_complete_candidate(database, "corrupt-user", "corrupt_owner")
    score_data = _raw_json(database, "score_json", "corrupt-user")
    score_data["total"] = "95"
    _set_column(
        database, "score_json", json.dumps(score_data), "corrupt-user",
    )

    assert database.get_cached_growth_candidate("corrupt-user", NOW) is None
    assert database.get_digest_candidates(now=NOW) == []

    discovery = GrowthDiscovery(
        NoDiscoveryX(),
        database,
        query_budget=3,
        new_profile_budget=25,
        seed_accounts=("valid_seed",),
        topic_queries=("one", "two"),
    )
    assert discovery.run(NOW) == []


def _mutate_conflicting_alias(database):
    profile = _raw_json(database, "profile_json")
    profile["id"] = "other-user"
    profile["user_id"] = "cache-user"
    _set_column(database, "profile_json", json.dumps(profile))


def _mutate_protected(database):
    profile = _raw_json(database, "profile_json")
    profile["protected"] = True
    _set_column(database, "profile_json", json.dumps(profile))


def _mutate_spam(database):
    profile = _raw_json(database, "profile_json")
    profile["spam_signals"] = ["duplicate_content"]
    _set_column(database, "profile_json", json.dumps(profile))


def _mutate_non_original(database):
    latest = _raw_json(database, "latest_post_json")
    latest["is_original"] = False
    _set_column(database, "latest_post_json", json.dumps(latest))


def _mutate_old_activity(database):
    old = (NOW - timedelta(days=30, microseconds=1)).isoformat()
    latest = _raw_json(database, "latest_post_json")
    latest["created_at"] = old
    score_data = _raw_json(database, "score_json")
    score_data["activity_at"] = old
    _set_column(database, "latest_post_json", json.dumps(latest))
    _set_column(database, "score_json", json.dumps(score_data))


def _mutate_naive_activity(database):
    naive = (NOW - timedelta(days=1)).replace(tzinfo=None).isoformat()
    latest = _raw_json(database, "latest_post_json")
    latest["created_at"] = naive
    score_data = _raw_json(database, "score_json")
    score_data["activity_at"] = naive
    _set_column(database, "latest_post_json", json.dumps(latest))
    _set_column(database, "score_json", json.dumps(score_data))


def _mutate_hard_filter(database):
    score_data = _raw_json(database, "score_json")
    score_data["hard_filter_passed"] = False
    _set_column(database, "score_json", json.dumps(score_data))


def _mutate_filter_reason(database):
    score_data = _raw_json(database, "score_json")
    score_data["filter_reason"] = "protected_profile"
    _set_column(database, "score_json", json.dumps(score_data))


def _mutate_missing_latest_text(database):
    latest = _raw_json(database, "latest_post_json")
    del latest["text"]
    _set_column(database, "latest_post_json", json.dumps(latest))


def _mutate_noncanonical_profile(database):
    profile = _raw_json(database, "profile_json")
    profile["unsafe"] = float("nan")
    _set_column(database, "profile_json", json.dumps(profile))


@pytest.mark.parametrize(
    "mutate",
    [
        _mutate_conflicting_alias,
        _mutate_protected,
        _mutate_spam,
        _mutate_non_original,
        _mutate_old_activity,
        _mutate_naive_activity,
        _mutate_hard_filter,
        _mutate_filter_reason,
        _mutate_missing_latest_text,
        _mutate_noncanonical_profile,
    ],
)
def test_cache_and_digest_share_fail_closed_candidate_validation(
    tmp_path,
    mutate,
):
    database = Database(str(tmp_path / "growth.db"))
    _persist_complete_candidate(database)
    mutate(database)

    assert database.get_cached_growth_candidate("cache-user", NOW) is None
    assert database.get_digest_candidates(now=NOW) == []


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("profile_json", "[]"),
        ("latest_post_json", "[]"),
        ("score_json", "[]"),
        ("profile_json", "{not-json"),
        ("latest_post_json", "{not-json"),
        ("score_json", "{not-json"),
    ],
)
def test_legacy_malformed_json_is_isolated_identically(column, value, tmp_path):
    database = Database(str(tmp_path / "growth.db"))
    _persist_complete_candidate(database)
    _set_column(database, column, value)

    assert database.get_cached_growth_candidate("cache-user", NOW) is None
    assert database.get_digest_candidates(now=NOW) == []


def test_excessively_nested_legacy_json_is_isolated_without_crashing(tmp_path):
    database = Database(str(tmp_path / "growth.db"))
    _persist_complete_candidate(database)
    nested = "[" * 1100 + "0" + "]" * 1100
    _set_column(database, "profile_json", nested)

    assert database.get_growth_candidate("cache-user") is None
    assert database.get_cached_growth_candidate("cache-user", NOW) is None
    assert database.get_digest_candidates(now=NOW) == []


def test_cache_and_digest_share_the_exact_thirty_day_activity_boundary(tmp_path):
    database = Database(str(tmp_path / "growth.db"))
    _persist_complete_candidate(database)
    boundary = NOW - timedelta(days=30)
    latest = _raw_json(database, "latest_post_json")
    latest["created_at"] = boundary.isoformat()
    score_data = _raw_json(database, "score_json")
    score_data["activity_at"] = boundary.isoformat()
    _set_column(database, "latest_post_json", json.dumps(latest))
    _set_column(database, "score_json", json.dumps(score_data))

    assert database.get_cached_growth_candidate("cache-user", NOW) is not None
    assert [
        row["user_id"] for row in database.get_digest_candidates(now=NOW)
    ] == ["cache-user"]

    later = NOW + timedelta(microseconds=1)
    assert database.get_cached_growth_candidate("cache-user", later) is None
    assert database.get_digest_candidates(now=later) == []


def test_valid_canonical_candidate_is_accepted_identically(tmp_path):
    database = Database(str(tmp_path / "growth.db"))
    _persist_complete_candidate(database)

    cached = database.get_cached_growth_candidate("cache-user", NOW)
    digest = database.get_digest_candidates(now=NOW)

    assert cached is not None
    assert [row["id"] for row in digest] == [cached["id"]]
