import json
import multiprocessing
from datetime import date, datetime, timedelta, timezone

import pytest

from modules.analytics import PerformanceAnalyzer
from modules.database import Database
from modules.telegram_controller import TelegramController


NOW = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)
REPORT_KEYS = {
    "followers_total",
    "new_followers",
    "new_relevant_followers",
    "relevant_follower_rate",
    "candidate_count",
    "decision_counts",
    "follow_back_rate_by_source",
    "median_impressions",
    "post_count",
    "content_by_category",
    "query_budget_used",
    "profiles_evaluated",
    "factual_blocks",
    "attribution_label",
}


def follower_profile(user_id="100", username="owner", **overrides):
    profile = {
        "id": user_id,
        "username": username,
        "description": "Owner of an independent strength and conditioning studio",
        "followers_count": 1800,
        "following_count": 650,
        "protected": False,
        "spam_signals": [],
    }
    profile.update(overrides)
    return profile


def latest_post(post_id="900", created_at=None, **overrides):
    post = {
        "id": post_id,
        "text": "Testing a new class timetable for our members",
        "created_at": created_at or (NOW - timedelta(days=1)).isoformat(),
        "lang": "en",
        "is_original": True,
    }
    post.update(overrides)
    return post


def accepted_score(total=95, activity_at=None):
    return {
        "role_bio": 30,
        "recent_topic_fit": 25,
        "activity": 15,
        "market": 15,
        "account_quality": 10,
        "affinity": 0,
        "total": total,
        "audience_segment": "primary",
        "reasons": [
            "primary_operator_role",
            "multiple_operating_topics",
            "active_within_7_days",
            "english_market",
            "plausible_public_metrics",
        ],
        "activity_at": activity_at or (NOW - timedelta(days=1)).isoformat(),
        "hard_filter_passed": True,
        "filter_reason": "accepted",
    }


class FollowerX:
    def __init__(self, followers=None):
        self.followers = list(followers or [])
        self.follower_reads = 0
        self.write_calls = []

    def get_followers_profiles(self):
        self.follower_reads += 1
        return list(self.followers)


def analyzer_for(tmp_path, followers=None, name="analytics.db"):
    database = Database(str(tmp_path / name))
    x_client = FollowerX(followers)
    return PerformanceAnalyzer(x_client, database), x_client, database


def add_candidate(
    database,
    profile,
    *,
    source="topic_search:owners",
    first_seen_at=NOW,
    last_evaluated_at=NOW,
):
    activity_at = last_evaluated_at - timedelta(days=1)
    post = latest_post(
        post_id=str(900 + int(profile["id"])) if profile["id"].isdigit() else "900",
        created_at=activity_at.isoformat(),
    )
    return database.upsert_growth_candidate({
        "user_id": profile["id"],
        "username": profile["username"],
        "profile": profile,
        "latest_post": post,
        "score": 95,
        "score_data": accepted_score(activity_at=activity_at.isoformat()),
        "discovery_source": source,
        "first_seen_at": first_seen_at.isoformat(),
        "last_evaluated_at": last_evaluated_at.isoformat(),
        "profile_expires_at": (last_evaluated_at + timedelta(days=7)).isoformat(),
    })


def _capture_worker(db_path, start_event, result_queue):
    try:
        database = Database(db_path)
        analyzer = PerformanceAnalyzer(
            FollowerX([follower_profile("700", "concurrentown")]),
            database,
        )
        start_event.wait(timeout=10)
        result_queue.put(analyzer.capture_follower_snapshot(NOW))
    except BaseException as error:
        result_queue.put({"error": f"{type(error).__name__}: {error}"})


def _insert_post(database, tweet_id, category, created_at, impressions=None):
    with database._conn() as conn:
        conn.execute(
            """
            INSERT INTO posted_tweets (
                tweet_id, text, category, topic, has_link, score_total,
                agent_used, created_at
            ) VALUES (?, 'Published copy', ?, '', 0, 90, 'test', ?)
            """,
            (tweet_id, category, created_at.isoformat()),
        )
        if impressions is not None:
            conn.execute(
                """
                INSERT INTO tweet_metrics (
                    tweet_id, impressions, likes, retweets, replies,
                    bookmarks, checked_at
                ) VALUES (?, ?, 0, 0, 0, 0, ?)
                """,
                (tweet_id, impressions, created_at.isoformat()),
            )


def test_snapshot_detects_new_relevant_follower_from_canonical_cache(tmp_path):
    profile = follower_profile("101", "relevant_owner")
    analyzer, fake_x, database = analyzer_for(tmp_path, [profile])
    add_candidate(database, profile, source="network")

    summary = analyzer.capture_follower_snapshot(NOW)

    assert summary == {
        "followers_total": 1,
        "new_total": 1,
        "new_relevant": 1,
        "source_counts": {"network": 1},
        "follow_backs_by_source": {},
    }
    assert fake_x.follower_reads == 1
    assert fake_x.write_calls == []


def test_snapshot_rerun_upserts_relevance_without_counting_twice(tmp_path):
    profile = follower_profile("102", "same_day_owner")
    analyzer, fake_x, database = analyzer_for(tmp_path, [profile])

    first = analyzer.capture_follower_snapshot(NOW)
    add_candidate(database, profile)
    second = analyzer.capture_follower_snapshot(NOW + timedelta(hours=2))

    assert first["new_total"] == 1
    assert first["new_relevant"] == 0
    assert second["followers_total"] == 1
    assert second["new_total"] == 0
    assert second["new_relevant"] == 0
    with database._conn() as conn:
        row = conn.execute(
            "SELECT relevant, attribution_source FROM follower_snapshots"
        ).fetchone()
    assert dict(row) == {"relevant": 1, "attribution_source": "topic_search:owners"}
    assert fake_x.follower_reads == 2


def test_removed_then_refollowed_user_is_never_new_again_across_gap(tmp_path):
    profile = follower_profile("103", "returning_owner")
    analyzer, fake_x, database = analyzer_for(tmp_path, [profile])
    add_candidate(database, profile)

    assert analyzer.capture_follower_snapshot(NOW)["new_total"] == 1
    fake_x.followers = []
    assert analyzer.capture_follower_snapshot(NOW + timedelta(days=1))["new_total"] == 0
    fake_x.followers = [profile]
    returned = analyzer.capture_follower_snapshot(NOW + timedelta(days=8))

    assert returned["followers_total"] == 1
    assert returned["new_total"] == 0
    assert returned["new_relevant"] == 0
    assert database.get_known_follower_ids() == {"103"}


def test_empty_daily_snapshot_reports_zero_current_followers(tmp_path):
    profile = follower_profile("116", "departed_owner")
    analyzer, fake_x, _database = analyzer_for(tmp_path, [profile])
    analyzer.capture_follower_snapshot(NOW)
    fake_x.followers = []

    summary = analyzer.capture_follower_snapshot(NOW + timedelta(days=1))
    report = analyzer.build_weekly_report(date(2026, 8, 11))

    assert summary["followers_total"] == 0
    assert report["followers_total"] == 0


def test_backdated_capture_never_relabels_an_id_seen_in_any_snapshot(tmp_path):
    profile = follower_profile("113", "replayed_owner")
    analyzer, _fake_x, database = analyzer_for(tmp_path, [profile])
    assert analyzer.capture_follower_snapshot(NOW + timedelta(days=2))["new_total"] == 1

    replay = analyzer.capture_follower_snapshot(NOW)

    assert replay["followers_total"] == 1
    assert replay["new_total"] == 0
    with database._conn() as conn:
        rows = conn.execute(
            "SELECT observed_on, is_new FROM follower_snapshots ORDER BY observed_on"
        ).fetchall()
    assert [dict(row) for row in rows] == [
        {"observed_on": "2026-08-10", "is_new": 0},
        {"observed_on": "2026-08-12", "is_new": 1},
    ]


def test_snapshot_claim_is_atomic_across_processes_and_restart(tmp_path):
    db_path = str(tmp_path / "concurrent.db")
    Database(db_path)
    ctx = multiprocessing.get_context("spawn")
    start_event = ctx.Event()
    result_queue = ctx.Queue()
    processes = [
        ctx.Process(
            target=_capture_worker,
            args=(db_path, start_event, result_queue),
        )
        for _index in range(2)
    ]
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    results = [result_queue.get(timeout=5) for _process in processes]

    assert all("error" not in result for result in results), results
    assert sorted(result["new_total"] for result in results) == [0, 1]
    with Database(db_path)._conn() as conn:
        rows = conn.execute("SELECT user_id, is_new FROM follower_snapshots").fetchall()
    assert [dict(row) for row in rows] == [{"user_id": "700", "is_new": 1}]
    restarted = PerformanceAnalyzer(
        FollowerX([follower_profile("700", "concurrentown")]),
        Database(db_path),
    )
    assert restarted.capture_follower_snapshot(NOW)["new_total"] == 0


def test_malformed_followers_are_isolated_and_exact_ids_are_not_coerced(tmp_path):
    followers = [
        follower_profile(True, "bool_id"),
        follower_profile(123, "integer_id"),
        follower_profile("104", "valid_owner"),
        follower_profile("bad", "bad_metrics", followers_count=True),
        {"id": "missing_fields"},
    ]
    analyzer, fake_x, database = analyzer_for(tmp_path, followers)

    summary = analyzer.capture_follower_snapshot(NOW)

    assert summary["followers_total"] == 1
    assert summary["new_total"] == 1
    assert fake_x.follower_reads == 1
    assert database.get_known_follower_ids() == {"104"}


@pytest.mark.parametrize("latest_value", [None, {}, [], "raw", {"id": True}])
def test_missing_or_malformed_cached_latest_post_is_never_relevant(
    tmp_path, latest_value,
):
    profile = follower_profile("114", "closed_owner")
    analyzer, _fake_x, database = analyzer_for(
        tmp_path, [profile], name=f"latest-{type(latest_value).__name__}.db",
    )
    candidate_id = add_candidate(database, profile)
    with database._conn() as conn:
        conn.execute(
            "UPDATE growth_candidates SET latest_post_json = ? WHERE id = ?",
            (
                None
                if latest_value is None
                else json.dumps(latest_value),
                candidate_id,
            ),
        )

    summary = analyzer.capture_follower_snapshot(NOW)

    assert summary["new_total"] == 1
    assert summary["new_relevant"] == 0


def test_suppressed_cached_candidate_is_not_a_relevant_follower(tmp_path):
    profile = follower_profile("118", "suppressedown")
    analyzer, _fake_x, database = analyzer_for(tmp_path, [profile])
    candidate_id = add_candidate(database, profile)
    assert database.mark_candidate_decision(
        candidate_id,
        "rejected",
        "not_relevant",
        decided_at=NOW - timedelta(hours=1),
    )

    summary = analyzer.capture_follower_snapshot(NOW)

    assert summary["new_total"] == 1
    assert summary["new_relevant"] == 0


@pytest.mark.parametrize("decision", ["saved", "rejected", "automatic"])
def test_only_manual_follow_decisions_can_convert(tmp_path, decision):
    profile = follower_profile("105", f"owner_{decision}")
    analyzer, _fake_x, database = analyzer_for(
        tmp_path, [profile], name=f"{decision}.db",
    )
    candidate_id = add_candidate(database, profile, source="network")
    if decision == "automatic":
        with database._conn() as conn:
            conn.execute(
                "UPDATE growth_candidates SET decision = ? WHERE id = ?",
                (decision, candidate_id),
            )
    else:
        assert database.mark_candidate_decision(
            candidate_id, decision, decided_at=NOW - timedelta(hours=1),
        )

    summary = analyzer.capture_follower_snapshot(NOW)

    assert summary["follow_backs_by_source"] == {}
    assert database.get_growth_candidate("105")["followed_back_at"] is None


def test_manual_follow_back_is_recorded_once_with_discovery_source(tmp_path):
    profile = follower_profile("106", "manual_owner")
    analyzer, fake_x, database = analyzer_for(tmp_path, [profile])
    candidate_id = add_candidate(database, profile, source="network")
    assert database.mark_candidate_decision(
        candidate_id,
        "followed_manually",
        decided_at=NOW - timedelta(hours=1),
    )

    first = analyzer.capture_follower_snapshot(NOW)
    first_conversion = database.get_growth_candidate("106")["followed_back_at"]
    fake_x.followers = []
    analyzer.capture_follower_snapshot(NOW + timedelta(days=1))
    fake_x.followers = [profile]
    second = analyzer.capture_follower_snapshot(NOW + timedelta(days=2))

    assert first["follow_backs_by_source"] == {"network": 1}
    assert first_conversion == NOW.isoformat()
    assert second["follow_backs_by_source"] == {}
    assert database.get_growth_candidate("106")["followed_back_at"] == first_conversion


def test_follower_seen_before_manual_action_is_not_a_conversion(tmp_path):
    profile = follower_profile("117", "early_owner")
    analyzer, _fake_x, database = analyzer_for(tmp_path, [profile])
    candidate_id = add_candidate(database, profile, source="network")
    assert database.mark_candidate_decision(
        candidate_id,
        "followed_manually",
        decided_at=NOW,
    )

    summary = analyzer.capture_follower_snapshot(NOW - timedelta(hours=1))

    assert summary["new_total"] == 1
    assert summary["follow_backs_by_source"] == {}
    assert database.get_growth_candidate("117")["followed_back_at"] is None


def test_capture_rejects_naive_or_non_datetime_clocks_without_reading_x(tmp_path):
    analyzer, fake_x, _database = analyzer_for(tmp_path)

    for invalid in (datetime(2026, 8, 10), date(2026, 8, 10), True, 1):
        with pytest.raises(ValueError, match="timezone-aware"):
            analyzer.capture_follower_snapshot(invalid)

    assert fake_x.follower_reads == 0


def test_candidate_decision_does_not_coerce_false_clock_to_now(tmp_path):
    profile = follower_profile("115", "clock_owner")
    _analyzer, _fake_x, database = analyzer_for(tmp_path)
    candidate_id = add_candidate(database, profile)

    with pytest.raises(ValueError, match="timezone-aware"):
        database.mark_candidate_decision(
            candidate_id, "followed_manually", decided_at=False,
        )

    assert database.get_growth_candidate("115")["decision"] == "new"


def test_weekly_report_empty_has_exact_stable_schema(tmp_path):
    analyzer, _fake_x, _database = analyzer_for(tmp_path)

    report = analyzer.build_weekly_report(date(2026, 8, 10))

    assert set(report) == REPORT_KEYS
    assert report == {
        "followers_total": 0,
        "new_followers": 0,
        "new_relevant_followers": 0,
        "relevant_follower_rate": 0.0,
        "candidate_count": 0,
        "decision_counts": {
            "saved": 0,
            "followed_manually": 0,
            "discarded": 0,
            "rejected": 0,
        },
        "follow_back_rate_by_source": {},
        "median_impressions": 0.0,
        "post_count": 0,
        "content_by_category": {},
        "query_budget_used": 0,
        "profiles_evaluated": 0,
        "factual_blocks": {
            "period": {"start_date": "2026-08-04", "end_date": "2026-08-10"},
            "new_follower_sources": {},
            "manual_follows_by_source": {},
            "follow_backs_by_source": {},
        },
        "attribution_label": "correlation",
    }


def test_weekly_report_uses_local_end_date_inclusive_boundaries_and_sources(tmp_path):
    analyzer, fake_x, database = analyzer_for(tmp_path)
    first = follower_profile("107", "boundary_owner")
    last = follower_profile("108", "last_owner")
    outside = follower_profile("109", "outside_owner")
    for profile in (first, last, outside):
        add_candidate(
            database,
            profile,
            source="network",
            first_seen_at=NOW - timedelta(days=20),
        )

    fake_x.followers = [outside]
    analyzer.capture_follower_snapshot(datetime(2026, 8, 3, 12, tzinfo=timezone.utc))
    fake_x.followers = [first]
    analyzer.capture_follower_snapshot(datetime(2026, 8, 4, 12, tzinfo=timezone.utc))
    fake_x.followers = [last]
    analyzer.capture_follower_snapshot(datetime(2026, 8, 10, 21, 30, tzinfo=timezone.utc))

    # 22:30 UTC is already 11 August in Europe/Rome, so the seven-day
    # operating window is 5..11 August and excludes the 4 August follower.
    report = analyzer.build_weekly_report(
        datetime(2026, 8, 10, 22, 30, tzinfo=timezone.utc),
    )

    assert report["followers_total"] == 1
    assert report["new_followers"] == 1
    assert report["new_relevant_followers"] == 1
    assert report["relevant_follower_rate"] == 1.0
    assert report["factual_blocks"]["period"] == {
        "start_date": "2026-08-05",
        "end_date": "2026-08-11",
    }
    assert report["factual_blocks"]["new_follower_sources"] == {"network": 1}


@pytest.mark.parametrize(
    ("impressions", "expected"),
    [([10, 30, 20], 20), ([10, 40, 20, 30], 25.0)],
)
def test_weekly_report_median_is_deterministic_for_odd_and_even_samples(
    tmp_path, impressions, expected,
):
    analyzer, _fake_x, database = analyzer_for(tmp_path)
    for index, value in enumerate(impressions):
        _insert_post(
            database,
            str(800 + index),
            "gym_strategy" if index % 2 == 0 else "founder_story",
            NOW - timedelta(days=index),
            value,
        )

    report = analyzer.build_weekly_report(date(2026, 8, 10))

    assert report["median_impressions"] == expected
    assert report["post_count"] == len(impressions)
    assert report["content_by_category"] == {
        "founder_story": len(impressions) // 2,
        "gym_strategy": (len(impressions) + 1) // 2,
    }


def test_weekly_report_counts_period_decisions_conversions_and_budgets(tmp_path):
    analyzer, _fake_x, database = analyzer_for(tmp_path)
    converted = follower_profile("110", "converted_owner")
    pending = follower_profile("111", "pending_owner")
    saved = follower_profile("112", "saved_owner")
    converted_id = add_candidate(database, converted, source="network")
    pending_id = add_candidate(database, pending, source="network")
    saved_id = add_candidate(database, saved, source="topic_search:owners")
    for candidate_id, decision in (
        (converted_id, "followed_manually"),
        (pending_id, "followed_manually"),
        (saved_id, "saved"),
    ):
        assert database.mark_candidate_decision(
            candidate_id, decision, decided_at=NOW - timedelta(hours=1),
        )
    capture = PerformanceAnalyzer(FollowerX([converted]), database)
    capture.capture_follower_snapshot(NOW)
    database.set_state("growth_queries:2026-08-10", "3")
    database.set_state("growth_profile_evaluations:2026-08-10", "7")
    database.set_state("growth_queries:2026-08-09", "2")
    database.set_state("growth_profile_evaluations:2026-08-09", "5")

    report = analyzer.build_weekly_report(date(2026, 8, 10))

    assert report["candidate_count"] == 3
    assert report["decision_counts"] == {
        "saved": 1,
        "followed_manually": 2,
        "discarded": 0,
        "rejected": 0,
    }
    assert report["follow_back_rate_by_source"] == {
        "network": 0.5,
        "topic_search:owners": 0.0,
    }
    assert report["query_budget_used"] == 5
    assert report["profiles_evaluated"] == 12
    assert report["factual_blocks"]["manual_follows_by_source"] == {"network": 2}
    assert report["factual_blocks"]["follow_backs_by_source"] == {"network": 1}


class ReportTelegramApi:
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append((str(chat_id), text, kwargs))
        return {"message_id": len(self.messages)}


class StaticAnalytics:
    def __init__(self, report):
        self.report = report
        self.end_dates = []

    def build_weekly_report(self, end_date):
        self.end_dates.append(end_date)
        return self.report


def _report_controller(database, telegram, analytics):
    return TelegramController(
        telegram,
        database,
        notifier=None,
        authorized_chat_id="42",
        analytics=analytics,
        dry_run=True,
        now_fn=lambda: NOW,
    )


def test_stats_growth_and_weekly_push_share_the_weekly_formatter(tmp_path):
    analyzer, _fake_x, database = analyzer_for(tmp_path)
    report = analyzer.build_weekly_report(date(2026, 8, 10))
    telegram = ReportTelegramApi()
    controller = _report_controller(database, telegram, StaticAnalytics(report))

    assert controller._stats("42") == "stats"
    stats_text = telegram.messages[-1][1]
    assert controller.push_weekly_report(NOW) == "weekly_report_sent"
    push_text = telegram.messages[-1][1]
    assert controller._growth("42") == "growth_empty"
    growth_text = telegram.messages[-2][1]

    assert stats_text == push_text == growth_text
    assert stats_text == controller.format_weekly_report(report)
    assert telegram.messages[-1][1] == "Nessun candidato growth disponibile."
    assert all(message[2]["parse_mode"] is None for message in telegram.messages)


def test_reweighting_is_disabled_for_first_30_days_then_uses_existing_policy(tmp_path):
    analyzer, _fake_x, database = analyzer_for(tmp_path)
    first_post = NOW - timedelta(days=29)
    _insert_post(database, "501", "strong", first_post, 100)
    with database._conn() as conn:
        conn.execute(
            "UPDATE tweet_metrics SET likes = 10 WHERE tweet_id = '501'"
        )

    assert analyzer.recompute_category_weights(now=NOW) == {}
    assert database.get_all_category_weights() == {}

    weights = analyzer.recompute_category_weights(now=first_post + timedelta(days=30))

    assert weights == {"strong": 1.0}
    assert database.get_all_category_weights() == {"strong": 1.0}


def test_weekly_report_rejects_naive_datetime_end_date(tmp_path):
    analyzer, _fake_x, _database = analyzer_for(tmp_path)

    with pytest.raises(ValueError, match="timezone-aware"):
        analyzer.build_weekly_report(datetime(2026, 8, 10))
