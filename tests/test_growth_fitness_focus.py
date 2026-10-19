from datetime import datetime, timedelta, timezone

import pytest

from modules.database import Database
from modules.growth_candidate_schema import (
    GROWTH_RELEVANCE_POLICY,
    audience_segment,
    evaluate_growth_candidate_filters,
    has_noise_signals,
)
from modules.growth_digest import (
    MAX_FOUNDER_ACCOUNTS,
    GrowthDigestService,
    score_growth_post,
)
from modules.growth_discovery import score_growth_candidate


NOW = datetime(2026, 10, 19, 9, tzinfo=timezone.utc)
RECENT = (NOW - timedelta(hours=5)).isoformat()


def _profile(user_id, username, description, **overrides):
    value = {
        "id": user_id,
        "username": username,
        "description": description,
        "followers_count": 1500,
        "following_count": 600,
        "protected": False,
        "spam_signals": [],
    }
    value.update(overrides)
    return value


def _latest(post_id="900", text="Morning workout done, legs are toast"):
    return {
        "id": post_id,
        "text": text,
        "created_at": RECENT,
        "lang": "en",
        "is_original": True,
    }


@pytest.mark.parametrize(
    "bio",
    [
        "Personal trainer and fitness creator",
        "Running coach | marathon addict",
        "Yoga teacher, studio owner in Austin",
        "Boutique fitness studio. Classes daily",
        "CrossFit athlete and coach",
    ],
)
def test_fitness_bios_are_the_fitness_segment(bio):
    assert audience_segment(_profile("1", "coach", bio)) == "fitness"


def test_founders_stay_a_secondary_segment_and_off_target_bios_fail():
    assert audience_segment(_profile("2", "maker", "Indie hacker building a SaaS")) == "peer"
    assert audience_segment(_profile("3", "dad", "Dad of two, coffee lover")) is None
    passed, reason = evaluate_growth_candidate_filters(
        _profile("3", "dad", "Dad of two, coffee lover"), _latest(), NOW,
    )
    assert (passed, reason) == (False, "no_audience_context")


def test_fitness_bio_wins_over_founder_when_both_match():
    assert audience_segment(
        _profile("4", "both", "Founder of a gym app and weekend runner")
    ) == "fitness"


@pytest.mark.parametrize(
    "text",
    [
        "Coach and lifter, use code FIT10 for 20% off",
        "Gym content. Sarms and steroids talk",
        "Crypto gym bro, trading signals",
        "Fitness affiliate, discount code in bio",
    ],
)
def test_fitness_promo_and_supplement_noise_is_rejected(text):
    assert has_noise_signals(text)
    passed, reason = evaluate_growth_candidate_filters(
        _profile("5", "noisy", text), _latest(), NOW,
    )
    assert (passed, reason) == (False, "noise_signals")


def test_fitness_creator_outscores_an_equally_active_founder():
    latest = _latest(text="Workout class today, great training session")
    fitness = score_growth_candidate(
        _profile("6", "coach", "Personal trainer"), latest, NOW,
    )
    founder = score_growth_candidate(
        _profile("7", "maker", "Indie hacker building a SaaS"), latest, NOW,
    )
    assert fitness["audience_segment"] == "fitness"
    assert fitness["relevance_policy"] == GROWTH_RELEVANCE_POLICY == "fitness_first_v1"
    assert "fitness_bio" in fitness["reasons"]
    assert "multiple_fitness_topics" in fitness["reasons"]
    assert fitness["total"] > founder["total"]


def _search_post(post_id, text, author_id="9"):
    return {
        "id": post_id,
        "text": text,
        "author_id": author_id,
        "author_username": f"user{author_id}",
        "created_at": RECENT,
        "lang": "en",
        "public_metrics": {
            "like_count": 3, "retweet_count": 0, "reply_count": 1,
            "quote_count": 0, "impression_count": 100,
        },
        "author_public_metrics": {
            "followers_count": 800, "following_count": 300,
            "tweet_count": 2000, "listed_count": 3,
        },
    }


def test_fitness_posts_score_with_fitness_reason_codes():
    scored = score_growth_post(
        _search_post(
            "100",
            "Travelling next week, any drop-in gym or class worth a visit?",
        ),
        NOW,
    )
    assert scored is not None
    assert "access_pain" in scored["reason_codes"]
    assert "class_or_gym_talk" in scored["reason_codes"]
    assert score_growth_post(_search_post("101", "Nice weather."), NOW) is None


class _Discovery:
    def __init__(self, rows):
        self.rows = rows

    def run(self, now):
        return self.rows


class _X:
    def search_relevant_posts(self, query, limit=25):
        return []


def _candidate(user_id, segment):
    return {
        "user_id": user_id,
        "username": f"acct{user_id}",
        "profile": {
            "followers_count": 900, "following_count": 300,
            "tweet_count": 500, "listed_count": 2,
        },
        "latest_post": {"id": f"5{user_id}", "created_at": RECENT},
        "score": 90,
        "audience_segment": segment,
        "reasons": ["fitness_bio" if segment == "fitness" else "founder_bio"],
    }


def test_digest_caps_founder_accounts_and_keeps_fitness_ones(tmp_path):
    database = Database(str(tmp_path / "fitness-focus.db"))
    candidates = [_candidate(str(i), "peer") for i in range(1, 5)] + [
        _candidate("20", "fitness"),
        _candidate("21", "fitness"),
    ]
    digest = GrowthDigestService(
        _X(), database, discovery=_Discovery(candidates),
    ).build(NOW)

    segments = [row["payload"]["segment"] for row in digest["accounts"]]
    assert segments.count("peer") == MAX_FOUNDER_ACCOUNTS == 2
    assert segments.count("fitness") == 2
