from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from modules.adaptive_timing import TimingSample
from modules.publication_cadence import PublicationCadencePolicy


PLAN_DAY = date(2026, 9, 7)  # Monday
NEW_YORK = ZoneInfo("America/New_York")


def _policy():
    return PublicationCadencePolicy(
        audience_timezone="America/New_York",
        third_days_per_week=3,
        learning_min_posts=30,
    )


def _sample(weekday, *, engagements, index=0, plan_day=PLAN_DAY):
    days_back = 7 + ((plan_day.weekday() - weekday) % 7) + index * 7
    scheduled = datetime.combine(
        plan_day - timedelta(days=days_back),
        time(14, 0),
        tzinfo=NEW_YORK,
    )
    return TimingSample(
        scheduled_for=scheduled,
        measured_at=scheduled + timedelta(hours=25),
        impressions=100,
        engagements=engagements,
    )


def _samples_by_weekday(engagements_by_weekday):
    samples = []
    for weekday in range(7):
        for index in range(5):
            samples.append(_sample(
                weekday,
                engagements=engagements_by_weekday[weekday],
                index=index,
            ))
    return samples


def test_cold_start_week_is_two_three_two_three_two_three_two():
    counts = [
        _policy().choose(PLAN_DAY + timedelta(days=offset), []).post_count
        for offset in range(7)
    ]

    assert counts == [2, 3, 2, 3, 2, 3, 2]


def test_cold_start_decision_is_explicit_and_restart_stable():
    first = _policy().choose(date(2026, 9, 8), [])
    second = _policy().choose(date(2026, 9, 8), [])

    assert first == second
    assert first.third_post_weekdays == (1, 3, 5)
    assert first.reason == "cold_start"


def test_after_thirty_samples_exactly_three_best_weekdays_are_selected():
    samples = _samples_by_weekday({0: 1, 1: 8, 2: 2, 3: 9, 4: 3, 5: 7, 6: 1})

    decision = _policy().choose(PLAN_DAY, samples)

    assert decision.third_post_weekdays == (1, 3, 5)
    assert decision.reason == "performance_weighted"
    assert decision.post_count == 2
    assert _policy().choose(date(2026, 9, 8), samples).post_count == 3


def test_learning_stays_cold_with_twenty_nine_mature_samples():
    samples = _samples_by_weekday({0: 10, 1: 1, 2: 9, 3: 1, 4: 8, 5: 1, 6: 7})[:29]

    decision = _policy().choose(PLAN_DAY, samples)

    assert decision.third_post_weekdays == (1, 3, 5)
    assert decision.reason == "cold_start"


def test_malformed_immature_and_future_samples_do_not_unlock_learning():
    scheduled = datetime(2026, 9, 3, 14, 0, tzinfo=NEW_YORK)
    invalid = [
        TimingSample(scheduled, scheduled + timedelta(hours=23), 100, 10),
        TimingSample(scheduled, datetime(2026, 9, 8, tzinfo=NEW_YORK), 100, 10),
        TimingSample(scheduled.replace(tzinfo=None), scheduled + timedelta(hours=25), 100, 10),
        TimingSample(scheduled, scheduled + timedelta(hours=25), True, 1),
        TimingSample(scheduled, scheduled + timedelta(hours=25), -1, 0),
        TimingSample(scheduled, scheduled + timedelta(hours=25), 10, 11),
    ]

    decision = _policy().choose(PLAN_DAY, invalid * 10)

    assert decision.reason == "cold_start"


def test_ties_are_deterministic_and_choose_three_unique_weekdays():
    decision = _policy().choose(
        PLAN_DAY,
        _samples_by_weekday({weekday: 4 for weekday in range(7)}),
    )

    assert decision.third_post_weekdays == (1, 3, 5)
    assert len(set(decision.third_post_weekdays)) == 3


@pytest.mark.parametrize(
    "kwargs",
    (
        {"audience_timezone": "UTC+5", "third_days_per_week": 3, "learning_min_posts": 30},
        {"audience_timezone": "America/New_York", "third_days_per_week": True, "learning_min_posts": 30},
        {"audience_timezone": "America/New_York", "third_days_per_week": 2, "learning_min_posts": 30},
        {"audience_timezone": "America/New_York", "third_days_per_week": 3, "learning_min_posts": 0},
    ),
)
def test_policy_configuration_fails_closed(kwargs):
    with pytest.raises(ValueError):
        PublicationCadencePolicy(**kwargs)


@pytest.mark.parametrize("local_day", (None, datetime(2026, 9, 7)))
def test_policy_requires_an_exact_date(local_day):
    with pytest.raises(ValueError):
        _policy().choose(local_day, [])
