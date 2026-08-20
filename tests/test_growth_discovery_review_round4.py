from datetime import timedelta

import pytest

from modules.database import Database
from modules.growth_discovery import GrowthDiscovery, passes_candidate_filters
from tests.test_growth_discovery_review import NOW, review_post, review_profile


class EmptyDiscoveryX:
    def get_followers_profiles(self):
        return []

    def search_recent_authors(self, _query):
        return []

    def get_network_candidates(self, _seeds):
        return []

    def get_latest_original_post(self, _user_id):
        raise AssertionError("a stored candidate must not cause an X read")


class OneFollowerX(EmptyDiscoveryX):
    def __init__(self, candidate_profile, latest_post):
        self.candidate_profile = candidate_profile
        self.latest_post = latest_post
        self.latest_calls = []

    def get_followers_profiles(self):
        return [self.candidate_profile]

    def get_latest_original_post(self, user_id):
        self.latest_calls.append(user_id)
        return self.latest_post


def _discovery(x_client, database):
    return GrowthDiscovery(
        x_client,
        database,
        query_budget=3,
        new_profile_budget=25,
        seed_accounts=("valid_seed",),
        topic_queries=("one", "two"),
    )


_MISSING = object()


def _with_changes(value, changes):
    for name, changed in changes.items():
        if changed is _MISSING:
            value.pop(name, None)
        else:
            value[name] = changed
    return value


FILTER_CASES = [
    (
        "canonical",
        {},
        {},
        (True, "accepted"),
    ),
    (
        "post-context-only",
        {"description": " \t "},
        {},
        (True, "accepted"),
    ),
    (
        "bio-context-only",
        {},
        {"text": "\n\t"},
        (True, "accepted"),
    ),
    (
        "whitespace-only-context",
        {"description": " \t "},
        {"text": "\n\t"},
        (False, "insufficient_bio_post_context"),
    ),
    (
        "empty-language",
        {},
        {"lang": ""},
        (True, "accepted"),
    ),
    (
        "whitespace-language",
        {},
        {"lang": " \t"},
        (True, "accepted"),
    ),
    (
        "non-string-language",
        {},
        {"lang": None},
        (False, "malformed_candidate_record"),
    ),
    (
        "non-string-bio",
        {"description": 1},
        {},
        (False, "malformed_candidate_record"),
    ),
    (
        "non-string-post-text",
        {},
        {"text": True},
        (False, "malformed_candidate_record"),
    ),
    (
        "protected",
        {"protected": True},
        {},
        (False, "protected_profile"),
    ),
    (
        "missing-protected",
        {"protected": _MISSING},
        {},
        (False, "protected_profile"),
    ),
    (
        "non-boolean-protected",
        {"protected": 0},
        {},
        (False, "protected_profile"),
    ),
    (
        "stored-spam",
        {"spam_signals": ["duplicate_content"]},
        {},
        (False, "spam_or_follow_farming_signals"),
    ),
    (
        "non-string-spam-signal",
        {"spam_signals": [1]},
        {},
        (False, "malformed_candidate_record"),
    ),
    (
        "non-list-spam-signals",
        {"spam_signals": "none"},
        {},
        (False, "malformed_candidate_record"),
    ),
    (
        "follow-farming",
        {"follow_farming": True},
        {},
        (False, "spam_or_follow_farming_signals"),
    ),
    (
        "non-boolean-follow-farming",
        {"follow_farming": 0},
        {},
        (False, "malformed_candidate_record"),
    ),
    (
        "non-original",
        {},
        {"is_original": False},
        (False, "no_original_post_within_30_days"),
    ),
    (
        "non-boolean-original",
        {},
        {"is_original": 1},
        (False, "no_original_post_within_30_days"),
    ),
    (
        "exact-thirty-day-boundary",
        {},
        {"created_at": (NOW - timedelta(days=30)).isoformat()},
        (True, "accepted"),
    ),
    (
        "older-than-thirty-days",
        {},
        {
            "created_at": (
                NOW - timedelta(days=30, microseconds=1)
            ).isoformat(),
        },
        (False, "no_original_post_within_30_days"),
    ),
    (
        "future-activity",
        {},
        {"created_at": (NOW + timedelta(microseconds=1)).isoformat()},
        (False, "no_original_post_within_30_days"),
    ),
    (
        "naive-activity",
        {},
        {
            "created_at": (
                NOW - timedelta(days=1)
            ).replace(tzinfo=None).isoformat(),
        },
        (False, "no_original_post_within_30_days"),
    ),
    (
        "aware-offset-activity",
        {},
        {"created_at": "2026-08-10T12:00:00+02:00"},
        (True, "accepted"),
    ),
]


@pytest.mark.parametrize(
    ("_case", "profile_changes", "post_changes", "expected_filter"),
    FILTER_CASES,
    ids=[case[0] for case in FILTER_CASES],
)
def test_hard_filter_cache_and_digest_share_canonical_eligibility_matrix(
    tmp_path,
    _case,
    profile_changes,
    post_changes,
    expected_filter,
):
    database = Database(str(tmp_path / "growth.db"))
    candidate_profile = review_profile("matrix-user", "matrix_owner")
    candidate_profile["user_id"] = "matrix-user"
    candidate_profile = _with_changes(candidate_profile, profile_changes)
    latest_post = _with_changes(
        review_post("842", (NOW - timedelta(days=1)).isoformat()),
        post_changes,
    )
    activity_at = latest_post.get("created_at")
    database.upsert_growth_candidate({
        "user_id": "matrix-user",
        "username": "matrix_owner",
        "profile": candidate_profile,
        "latest_post": latest_post,
        "score": 85,
        "score_data": {
            "total": 85,
            "audience_segment": "primary",
            "reasons": ["primary_operator_role"],
            "activity_at": activity_at,
            "hard_filter_passed": True,
            "filter_reason": "accepted",
        },
        "discovery_source": "topic_search",
        "last_evaluated_at": NOW.isoformat(),
        "profile_expires_at": (NOW + timedelta(days=7)).isoformat(),
    })

    filter_result = passes_candidate_filters(
        candidate_profile,
        latest_post,
        NOW,
    )
    cached = database.get_cached_growth_candidate("matrix-user", NOW)
    digest = database.get_digest_candidates(now=NOW)

    assert filter_result == expected_filter
    expected_eligible = expected_filter == (True, "accepted")
    assert (cached is not None) is expected_eligible
    assert bool(digest) is expected_eligible
    if cached is not None:
        assert [row["id"] for row in digest] == [cached["id"]]


def test_whitespace_only_context_is_excluded_from_cache_digest_and_full_run(
    tmp_path,
):
    database = Database(str(tmp_path / "growth.db"))
    activity_at = NOW - timedelta(days=1)
    candidate_profile = review_profile(
        "whitespace-user",
        "space_owner",
        description="   ",
    )
    latest_post = review_post(
        "840",
        activity_at.isoformat(),
        text="\t",
    )
    database.upsert_growth_candidate({
        "user_id": "whitespace-user",
        "username": "space_owner",
        "profile": candidate_profile,
        "latest_post": latest_post,
        "score": 85,
        "score_data": {
            "total": 85,
            "audience_segment": "primary",
            "reasons": ["primary_operator_role"],
            "activity_at": activity_at.isoformat(),
            "hard_filter_passed": True,
            "filter_reason": "accepted",
        },
        "discovery_source": "topic_search",
        "last_evaluated_at": NOW.isoformat(),
        "profile_expires_at": (NOW + timedelta(days=7)).isoformat(),
    })

    assert passes_candidate_filters(candidate_profile, latest_post, NOW) == (
        False,
        "insufficient_bio_post_context",
    )
    assert database.get_cached_growth_candidate("whitespace-user", NOW) is None
    assert database.get_digest_candidates(now=NOW) == []
    assert _discovery(EmptyDiscoveryX(), database).run(NOW) == []


def test_empty_language_from_collector_remains_eligible_end_to_end(tmp_path):
    database = Database(str(tmp_path / "growth.db"))
    activity_at = NOW - timedelta(days=1)
    candidate_profile = review_profile(
        "empty-lang-user",
        "empty_lang",
        description="Owner of a FlexDropin gym",
    )
    latest_post = review_post(
        "841",
        activity_at.isoformat(),
        text="Class schedule, member booking, and drop-in occupancy",
        lang="",
    )
    x_client = OneFollowerX(candidate_profile, latest_post)

    rows = _discovery(x_client, database).run(NOW)

    assert x_client.latest_calls == ["empty-lang-user"]
    assert passes_candidate_filters(candidate_profile, latest_post, NOW) == (
        True,
        "accepted",
    )
    assert [row["user_id"] for row in rows] == ["empty-lang-user"]
    assert rows[0]["score"] == 85
    assert database.get_cached_growth_candidate("empty-lang-user", NOW) is not None
    assert [
        row["user_id"] for row in database.get_digest_candidates(now=NOW)
    ] == ["empty-lang-user"]


def test_collector_rejects_conflicting_profile_aliases_before_paid_read(tmp_path):
    database = Database(str(tmp_path / "growth.db"))
    candidate_profile = review_profile("other-user", "alias_owner")
    candidate_profile["user_id"] = "canonical-user"
    x_client = OneFollowerX(candidate_profile, review_post("843"))

    assert _discovery(x_client, database).run(NOW) == []
    assert x_client.latest_calls == []
    assert database.get_growth_candidate("canonical-user") is None
    assert database.get_state(
        "growth_profile_evaluations:2026-08-10",
        "0",
    ) == "0"


@pytest.mark.parametrize(
    "post_changes",
    [
        {"id": "not-a-tweet-id"},
        {"tweet_id": "999"},
        {
            "created_at": (
                NOW - timedelta(days=1)
            ).replace(tzinfo=None).isoformat(),
        },
    ],
    ids=("non-digit-id", "conflicting-tweet-alias", "naive-clock"),
)
def test_collector_persists_noncanonical_latest_post_as_failed_audit_only(
    tmp_path,
    post_changes,
):
    database = Database(str(tmp_path / "growth.db"))
    candidate_profile = review_profile("bad-post-user", "bad_post_owner")
    latest_post = _with_changes(review_post("844"), post_changes)
    x_client = OneFollowerX(candidate_profile, latest_post)

    assert _discovery(x_client, database).run(NOW) == []

    assert x_client.latest_calls == ["bad-post-user"]
    audit = database.get_growth_candidate("bad-post-user")
    assert audit["latest_post"] is None
    assert audit["score_data"]["hard_filter_passed"] is False
    assert audit["score_data"]["filter_reason"] == (
        "no_original_post_within_30_days"
    )
    assert database.get_cached_growth_candidate("bad-post-user", NOW) is None
    assert database.get_digest_candidates(now=NOW) == []


def test_collector_isolates_excessively_nested_profile_json(tmp_path):
    database = Database(str(tmp_path / "growth.db"))
    candidate_profile = review_profile("nested-user", "nested_owner")
    nested = {}
    cursor = nested
    for _level in range(1100):
        child = {}
        cursor["child"] = child
        cursor = child
    candidate_profile["nested"] = nested
    x_client = OneFollowerX(candidate_profile, review_post("845"))

    assert _discovery(x_client, database).run(NOW) == []
    assert x_client.latest_calls == []
    assert database.get_growth_candidate("nested-user") is None
