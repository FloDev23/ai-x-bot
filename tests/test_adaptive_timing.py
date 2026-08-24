import json
import os
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest


_ADAPTIVE_ENV_NAMES = (
    "POSTS_PER_DAY",
    "THIRD_POST_DAYS_PER_WEEK",
    "APPROVED_QUEUE_TARGET",
    "PENDING_REVIEW_LIMIT",
    "DRAFT_GENERATION_DAILY_CAP",
    "AUDIENCE_TIMEZONE",
    "MORNING_WINDOW",
    "MIDDAY_WINDOW",
    "EVENING_WINDOW",
    "MIN_POST_GAP_HOURS",
    "ADAPTIVE_TIMING_MIN_POSTS",
    "ADAPTIVE_WEEKDAY_MIN_POSTS",
    "THIRD_POST_TIMING_MIN_POSTS",
    "PUBLICATION_PLAN_GRACE_MINUTES",
)


def _config_process(overrides=None):
    environment = dict(os.environ)
    for name in _ADAPTIVE_ENV_NAMES:
        environment.pop(name, None)
    environment.update({
        "TWITTER_API_KEY": "configured",
        "TWITTER_API_SECRET": "configured",
        "TWITTER_ACCESS_TOKEN": "configured",
        "TWITTER_ACCESS_TOKEN_SECRET": "configured",
        "TWITTER_BEARER_TOKEN": "configured",
        "GROQ_API_KEY": "configured",
        "TELEGRAM_BOT_TOKEN": (
            "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
        ),
        "TELEGRAM_CHAT_ID": "-1001234567890",
        "NEWS_TRUSTED_DOMAINS": "",
        "DRY_RUN": "true",
        "APPROVAL_REQUIRED": "true",
    })
    environment.update(overrides or {})
    script = """
import json
import config
config.validate_config()
print(json.dumps({
    "posts": config.POSTS_PER_DAY,
    "third_days": config.THIRD_POST_DAYS_PER_WEEK,
    "queue": config.APPROVED_QUEUE_TARGET,
    "pending": config.PENDING_REVIEW_LIMIT,
    "generation_cap": config.DRAFT_GENERATION_DAILY_CAP,
    "timezone": config.AUDIENCE_TIMEZONE,
    "morning": config.MORNING_WINDOW,
    "midday": config.MIDDAY_WINDOW,
    "evening": config.EVENING_WINDOW,
    "gap": config.MIN_POST_GAP_HOURS,
    "timing_min": config.ADAPTIVE_TIMING_MIN_POSTS,
    "weekday_min": config.ADAPTIVE_WEEKDAY_MIN_POSTS,
    "third_timing_min": config.THIRD_POST_TIMING_MIN_POSTS,
    "grace": config.PUBLICATION_PLAN_GRACE_MINUTES,
}, sort_keys=True))
"""
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_queue_configuration_defaults_are_safe_and_complete():
    result = _config_process()

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "evening": "18:00-20:30",
        "gap": 4,
        "generation_cap": 5,
        "grace": 90,
        "midday": "13:00-15:30",
        "morning": "08:30-10:30",
        "pending": 5,
        "posts": 2,
        "queue": 14,
        "third_days": 3,
        "third_timing_min": 30,
        "timing_min": 30,
        "timezone": "America/New_York",
        "weekday_min": 90,
    }


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("POSTS_PER_DAY", "1"),
        ("POSTS_PER_DAY", "3"),
        ("POSTS_PER_DAY", "two"),
        ("POSTS_PER_DAY", "true"),
        ("POSTS_PER_DAY", "-2"),
        ("THIRD_POST_DAYS_PER_WEEK", "2"),
        ("THIRD_POST_DAYS_PER_WEEK", "true"),
        ("APPROVED_QUEUE_TARGET", "13"),
        ("APPROVED_QUEUE_TARGET", "0"),
        ("PENDING_REVIEW_LIMIT", "15"),
        ("PENDING_REVIEW_LIMIT", "false"),
        ("DRAFT_GENERATION_DAILY_CAP", "4"),
        ("DRAFT_GENERATION_DAILY_CAP", "999999999999999999999"),
        ("AUDIENCE_TIMEZONE", "UTC+5"),
        ("MORNING_WINDOW", "10:30-08:30"),
        ("MORNING_WINDOW", "08:30/11:30"),
        ("MIDDAY_WINDOW", "10:00-09:00"),
        ("EVENING_WINDOW", "14:00-16:00"),
        ("MIN_POST_GAP_HOURS", "7"),
        ("ADAPTIVE_TIMING_MIN_POSTS", "0"),
        ("ADAPTIVE_WEEKDAY_MIN_POSTS", "29"),
        ("THIRD_POST_TIMING_MIN_POSTS", "0"),
        ("PUBLICATION_PLAN_GRACE_MINUTES", "0"),
    ),
)
def test_queue_configuration_rejects_unsafe_values(name, value):
    result = _config_process({name: value})

    assert result.returncode != 0
    assert name in result.stderr
    assert "configured" not in result.stderr


def _policy():
    from modules.adaptive_timing import AdaptiveTimingPolicy

    return AdaptiveTimingPolicy(
        audience_timezone="America/New_York",
        morning_window="08:30-11:30",
        evening_window="16:30-20:30",
        minimum_gap_hours=6,
        timing_min_posts=30,
        weekday_min_posts=90,
    )


def _three_position_policy():
    from modules.adaptive_timing import AdaptiveTimingPolicy

    return AdaptiveTimingPolicy(
        audience_timezone="America/New_York",
        morning_window="08:30-10:30",
        midday_window="13:00-15:30",
        evening_window="18:00-20:30",
        minimum_gap_hours=4,
        timing_min_posts=30,
        weekday_min_posts=90,
    )


def _assert_inside_approved_windows(decision):
    morning = decision.times[0].timetz().replace(tzinfo=None)
    evening = decision.times[1].timetz().replace(tzinfo=None)
    assert time(8, 30) <= morning <= time(11, 30)
    assert time(16, 30) <= evening <= time(20, 30)
    assert decision.times[1] - decision.times[0] >= timedelta(hours=6)
    assert decision.bucket_ids[0].startswith("morning:")
    assert decision.bucket_ids[1].startswith("evening:")


def test_time_window_parser_is_exact_and_bounded():
    from modules.adaptive_timing import TimeWindow

    parsed = TimeWindow.parse("08:30-11:30")
    assert parsed.start == time(8, 30)
    assert parsed.end == time(11, 30)

    for invalid in (
        "8:30-11:30",
        "08:30/11:30",
        "08:30-08:30",
        "11:30-08:30",
        "24:00-25:00",
        " 08:30-11:30",
    ):
        with pytest.raises(ValueError):
            TimeWindow.parse(invalid)


def test_cold_start_is_stable_inside_two_windows():
    policy = _policy()

    first = policy.choose(date(2026, 8, 24), "install-1", [])
    second = policy.choose(date(2026, 8, 24), "install-1", [])

    assert first == second
    assert first.reason == "cold_start"
    assert len(first.times) == 2
    assert all(value.tzinfo == ZoneInfo("America/New_York") for value in first.times)
    _assert_inside_approved_windows(first)


def test_three_post_day_uses_all_three_windows_with_four_hour_gaps():
    policy = _three_position_policy()

    first = policy.choose(
        date(2026, 8, 25), "install-three", [], post_count=3,
    )
    second = policy.choose(
        date(2026, 8, 25), "install-three", [], post_count=3,
    )

    assert first == second
    assert len(first.times) == 3
    assert len(first.bucket_ids) == 3
    local_times = [value.timetz().replace(tzinfo=None) for value in first.times]
    assert time(8, 30) <= local_times[0] <= time(10, 30)
    assert time(13, 0) <= local_times[1] <= time(15, 30)
    assert time(18, 0) <= local_times[2] <= time(20, 30)
    assert first.times[1] - first.times[0] >= timedelta(hours=4)
    assert first.times[2] - first.times[1] >= timedelta(hours=4)
    assert first.bucket_ids[0].startswith("morning:")
    assert first.bucket_ids[1].startswith("midday:")
    assert first.bucket_ids[2].startswith("evening:")


@pytest.mark.parametrize("post_count", (True, False, 0, 1, 4, "3", 3.0, None))
def test_timing_rejects_invalid_post_count(post_count):
    with pytest.raises(ValueError):
        _three_position_policy().choose(
            date(2026, 8, 25), "install-invalid-count", [],
            post_count=post_count,
        )


@pytest.mark.parametrize("local_day", (date(2026, 3, 8), date(2026, 11, 1)))
def test_three_post_windows_preserve_local_times_across_dst(local_day):
    decision = _three_position_policy().choose(
        local_day, "install-three-dst", [], post_count=3,
    )

    assert len(decision.times) == 3
    assert all(value.date() == local_day for value in decision.times)
    assert all(value.tzinfo == ZoneInfo("America/New_York") for value in decision.times)


def test_cold_start_changes_deterministically_across_dates():
    policy = _policy()

    choices = {
        policy.choose(date(2026, 8, 24) + timedelta(days=offset), "install-1", []).times
        for offset in range(4)
    }

    assert len(choices) > 1


@pytest.mark.parametrize("local_day", (date(2026, 3, 8), date(2026, 11, 1)))
def test_cold_start_preserves_local_windows_across_dst(local_day):
    decision = _policy().choose(local_day, "install-dst", [])

    assert all(value.date() == local_day for value in decision.times)
    assert all(value.tzinfo == ZoneInfo("America/New_York") for value in decision.times)
    _assert_inside_approved_windows(decision)


def _mature_samples(count, *, plan_day=date(2026, 8, 24)):
    from modules.adaptive_timing import TimingSample

    zone = ZoneInfo("America/New_York")
    samples = []
    for index in range(count):
        scheduled = datetime.combine(
            plan_day - timedelta(days=index + 3),
            time(9 if index % 2 == 0 else 18, 5 + index % 70),
            tzinfo=zone,
        )
        samples.append(TimingSample(
            scheduled_for=scheduled,
            measured_at=scheduled + timedelta(hours=25),
            impressions=1000 + index,
            engagements=100 if index % 2 == 0 else 25,
        ))
    return samples


def test_learning_stays_cold_before_minimum_mature_sample_count():
    decision = _policy().choose(
        date(2026, 8, 24), "install-learning", _mature_samples(29),
    )

    assert decision.reason == "cold_start"
    _assert_inside_approved_windows(decision)


def test_learning_uses_performance_after_thirty_mature_samples():
    decision = _policy().choose(
        date(2026, 8, 24), "install-learning", _mature_samples(30),
    )

    assert decision.reason == "performance_weighted"
    _assert_inside_approved_windows(decision)


def test_immature_future_and_malformed_samples_fail_closed():
    from modules.adaptive_timing import TimingSample

    zone = ZoneInfo("America/New_York")
    plan_day = date(2026, 8, 24)
    valid_shape = datetime(2026, 8, 20, 9, 0, tzinfo=zone)
    invalid_samples = [
        TimingSample(valid_shape, valid_shape + timedelta(hours=23), 100, 10),
        TimingSample(valid_shape, datetime(2026, 8, 25, tzinfo=zone), 100, 10),
        TimingSample(valid_shape, valid_shape + timedelta(hours=25), -1, 0),
        TimingSample(valid_shape, valid_shape + timedelta(hours=25), 10, 11),
        TimingSample(valid_shape.replace(tzinfo=None), valid_shape + timedelta(hours=25), 10, 1),
        TimingSample(valid_shape, valid_shape + timedelta(hours=25), True, 1),
    ]

    decision = _policy().choose(plan_day, "install-invalid", invalid_samples * 10)

    assert decision.reason == "cold_start"
    _assert_inside_approved_windows(decision)


def test_policy_rejects_invalid_identity_date_and_overlapping_windows():
    from modules.adaptive_timing import AdaptiveTimingPolicy

    with pytest.raises(ValueError):
        _policy().choose(datetime(2026, 8, 24, tzinfo=timezone.utc), "install", [])
    with pytest.raises(ValueError):
        _policy().choose(date(2026, 8, 24), "", [])
    with pytest.raises(ValueError):
        AdaptiveTimingPolicy(
            audience_timezone="America/New_York",
            morning_window="08:30-17:00",
            evening_window="16:30-20:30",
            minimum_gap_hours=1,
            timing_min_posts=30,
            weekday_min_posts=90,
        )
