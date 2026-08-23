import json
import multiprocessing
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

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

    def read_followers_profiles(self):
        self.follower_reads += 1
        return SimpleNamespace(profiles=tuple(self.followers), complete=True)


class IncompleteFollowerX:
    def __init__(self, followers):
        self.followers = list(followers)
        self.follower_reads = 0
        self.legacy_reads = 0
        self.write_calls = []

    def read_followers_profiles(self):
        self.follower_reads += 1
        return SimpleNamespace(profiles=tuple(self.followers), complete=False)

    def get_followers_profiles(self):
        self.legacy_reads += 1
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


def _seed_legacy_follower_run(db_path, profile, captured_at=NOW):
    database = Database(db_path)
    observed_on = captured_at.date().isoformat()
    with database._conn() as conn:
        conn.execute("DROP TABLE follower_snapshot_runs")
        conn.execute("""
            CREATE TABLE follower_snapshot_runs (
                observed_on TEXT PRIMARY KEY,
                followers_total INTEGER NOT NULL,
                captured_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO follower_snapshot_runs VALUES (?, ?, ?)",
            (observed_on, 999, captured_at.isoformat()),
        )
        conn.execute("""
            INSERT INTO follower_snapshots (
                observed_on, user_id, username, relevant, source,
                attribution_source, profile_json, first_seen_at,
                is_new, captured_at
            ) VALUES (?, ?, ?, 1, 'legacy', 'legacy', ?, ?, 1, ?)
        """, (
            observed_on, profile["id"], profile["username"],
            json.dumps(profile), captured_at.isoformat(), captured_at.isoformat(),
        ))
    return observed_on


class _HardCrashDatabase(Database):
    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.create_function("task11_hard_crash", 0, lambda: os._exit(73))
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def _hard_crash_capture_worker(db_path):
    database = _HardCrashDatabase(db_path)
    PerformanceAnalyzer(
        FollowerX([
            follower_profile("501", "first_owner"),
            follower_profile("502", "second_owner"),
        ]),
        database,
    ).capture_follower_snapshot(NOW)


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


def test_incomplete_follower_read_writes_nothing(tmp_path):
    profile = follower_profile("120", "partial_owner")
    database = Database(str(tmp_path / "incomplete.db"))
    fake_x = IncompleteFollowerX([profile])
    analyzer = PerformanceAnalyzer(fake_x, database)

    summary = analyzer.capture_follower_snapshot(NOW)

    assert summary == {
        "followers_total": 0,
        "new_total": 0,
        "new_relevant": 0,
        "source_counts": {},
        "follow_backs_by_source": {},
    }
    assert fake_x.follower_reads == 1
    assert fake_x.legacy_reads == 0
    with database._conn() as conn:
        snapshot_count = conn.execute(
            "SELECT COUNT(*) AS count FROM follower_snapshots"
        ).fetchone()["count"]
        run_count = conn.execute(
            "SELECT COUNT(*) AS count FROM follower_snapshot_runs"
        ).fetchone()["count"]
    assert (snapshot_count, run_count) == (0, 0)


def test_hard_crash_rolls_back_rows_conversion_and_run_then_retry_matches_report(
    tmp_path,
):
    db_path = str(tmp_path / "hard-crash.db")
    database = Database(db_path)
    first_profile = follower_profile("501", "first_owner")
    candidate_id = add_candidate(database, first_profile, source="network")
    assert database.mark_candidate_decision(
        candidate_id,
        "followed_manually",
        decided_at=NOW - timedelta(hours=1),
    )
    with database._conn() as conn:
        conn.execute("""
            CREATE TRIGGER task11_crash_on_second_follower
            BEFORE INSERT ON follower_snapshots
            WHEN NEW.user_id = '502'
            BEGIN
                SELECT task11_hard_crash();
            END
        """)

    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(target=_hard_crash_capture_worker, args=(db_path,))
    process.start()
    process.join(timeout=20)

    assert process.exitcode == 73
    with Database(db_path)._conn() as conn:
        snapshots = conn.execute(
            "SELECT user_id FROM follower_snapshots ORDER BY user_id"
        ).fetchall()
        runs = conn.execute(
            "SELECT observed_on FROM follower_snapshot_runs"
        ).fetchall()
        conn.execute("DROP TRIGGER task11_crash_on_second_follower")
    assert snapshots == []
    assert runs == []
    assert Database(db_path).get_growth_candidate("501")["followed_back_at"] is None
    crashed_report = PerformanceAnalyzer(
        FollowerX([]), Database(db_path)
    ).build_weekly_report(date(2026, 8, 10))
    assert crashed_report["new_followers"] == 0
    assert crashed_report["followers_total"] == 0

    retry = PerformanceAnalyzer(
        FollowerX([
            first_profile,
            follower_profile("502", "second_owner"),
        ]),
        Database(db_path),
    ).capture_follower_snapshot(NOW)
    retry_report = PerformanceAnalyzer(
        FollowerX([]), Database(db_path)
    ).build_weekly_report(date(2026, 8, 10))

    assert retry["followers_total"] == 2
    assert retry["new_total"] == retry_report["new_followers"] == 2
    assert retry["new_relevant"] == retry_report["new_relevant_followers"] == 1
    assert retry["source_counts"] == retry_report["factual_blocks"][
        "new_follower_sources"
    ]


def test_retry_rebuilds_unmarked_legacy_rows_without_admitting_orphans(tmp_path):
    db_path = str(tmp_path / "unmarked-legacy.db")
    database = Database(db_path)
    observed_on = NOW.date().isoformat()
    current = follower_profile("701", "current_owner")
    orphan = follower_profile("702", "orphan_owner")
    task10 = follower_profile("703", "task10_owner")
    task10_first_seen = (NOW - timedelta(hours=2)).isoformat()
    for profile in (current, orphan):
        result = database.capture_follower_observation(
            observed_on, NOW, profile, relevant=False,
        )
        assert result["is_new"] is True
    assert database.save_follower_snapshot(
        observed_on, task10, relevant=False, source="task10",
    )
    with database._conn() as conn:
        conn.execute(
            "UPDATE follower_snapshots SET first_seen_at = ? "
            "WHERE user_id = ?",
            (task10_first_seen, task10["id"]),
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM follower_snapshot_runs"
        ).fetchone()[0] == 0

    analyzer = PerformanceAnalyzer(FollowerX([current]), database)
    summary = analyzer.capture_follower_snapshot(NOW + timedelta(hours=1))
    replay = analyzer.capture_follower_snapshot(NOW + timedelta(hours=1))
    report = analyzer.build_weekly_report(date(2026, 8, 10))

    assert summary == replay
    assert summary["followers_total"] == 1
    assert summary["new_total"] == report["new_followers"] == 1
    assert report["followers_total"] == 1
    with database._conn() as conn:
        rows = conn.execute("""
            SELECT user_id, first_seen_at, captured_at
            FROM follower_snapshots
            WHERE observed_on = ?
            ORDER BY user_id
        """, (observed_on,)).fetchall()
    assert [row["user_id"] for row in rows] == ["701", "703"]
    assert rows[0]["first_seen_at"] == NOW.isoformat()
    assert rows[1]["first_seen_at"] == task10_first_seen
    assert rows[1]["captured_at"] is None


def test_concurrent_unmarked_legacy_repair_has_one_completed_transition(tmp_path):
    db_path = str(tmp_path / "unmarked-concurrent.db")
    database = Database(db_path)
    profile = follower_profile("700", "concurrentown")
    result = database.capture_follower_observation(
        NOW.date().isoformat(), NOW, profile, relevant=False,
    )
    assert result["is_new"] is True
    with database._conn() as conn:
        conn.execute("CREATE TABLE repair_audit (id INTEGER PRIMARY KEY)")
        conn.execute("""
            CREATE TRIGGER count_unmarked_completed_repair
            AFTER INSERT ON follower_snapshot_runs
            WHEN NEW.completed = 1
            BEGIN INSERT INTO repair_audit (id) VALUES (NULL); END
        """)
    ctx = multiprocessing.get_context("spawn")
    start_event = ctx.Event()
    result_queue = ctx.Queue()
    processes = [
        ctx.Process(target=_capture_worker, args=(db_path, start_event, result_queue))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    results = [result_queue.get(timeout=5) for _process in processes]

    assert results[0] == results[1]
    assert results[0]["new_total"] == 1
    with Database(db_path)._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM repair_audit").fetchone()[0] == 1
        marker = conn.execute(
            "SELECT completed, followers_total FROM follower_snapshot_runs"
        ).fetchone()
    assert dict(marker) == {"completed": 1, "followers_total": 1}


def test_hard_crash_during_unmarked_repair_restores_orphans_until_retry(tmp_path):
    db_path = str(tmp_path / "unmarked-crash.db")
    database = Database(db_path)
    for profile in (
        follower_profile("501", "first_owner"),
        follower_profile("599", "legacy_orphan"),
    ):
        result = database.capture_follower_observation(
            NOW.date().isoformat(), NOW, profile, relevant=False,
        )
        assert result["is_new"] is True
    with database._conn() as conn:
        conn.execute("""
            CREATE TRIGGER task11_crash_on_second_follower
            BEFORE INSERT ON follower_snapshots
            WHEN NEW.user_id = '502'
            BEGIN SELECT task11_hard_crash(); END
        """)
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(target=_hard_crash_capture_worker, args=(db_path,))
    process.start()
    process.join(timeout=20)

    assert process.exitcode == 73
    restarted = Database(db_path)
    with restarted._conn() as conn:
        rows = conn.execute(
            "SELECT user_id FROM follower_snapshots ORDER BY user_id"
        ).fetchall()
        markers = conn.execute(
            "SELECT observed_on FROM follower_snapshot_runs"
        ).fetchall()
        conn.execute("DROP TRIGGER task11_crash_on_second_follower")
    assert [row["user_id"] for row in rows] == ["501", "599"]
    assert markers == []
    assert PerformanceAnalyzer(
        FollowerX([]), restarted,
    ).build_weekly_report(date(2026, 8, 10))["new_followers"] == 0

    retry_analyzer = PerformanceAnalyzer(FollowerX([
        follower_profile("501", "first_owner"),
        follower_profile("502", "second_owner"),
    ]), restarted)
    summary = retry_analyzer.capture_follower_snapshot(NOW)
    report = retry_analyzer.build_weekly_report(date(2026, 8, 10))
    with restarted._conn() as conn:
        repaired_rows = conn.execute(
            "SELECT user_id FROM follower_snapshots ORDER BY user_id"
        ).fetchall()

    assert summary["new_total"] == report["new_followers"] == 2
    assert [row["user_id"] for row in repaired_rows] == ["501", "502"]


def test_legacy_run_migration_is_nonfinal_until_atomic_repair_and_replay(tmp_path):
    db_path = str(tmp_path / "legacy-run.db")
    legacy = follower_profile("710", "legacy_owner")
    observed_on = _seed_legacy_follower_run(db_path, legacy)

    migrated = Database(db_path)
    before = PerformanceAnalyzer(FollowerX([]), migrated).build_weekly_report(
        date.fromisoformat(observed_on),
    )
    with migrated._conn() as conn:
        marker = conn.execute(
            "SELECT * FROM follower_snapshot_runs WHERE observed_on = ?",
            (observed_on,),
        ).fetchone()
    assert marker["completed"] == 0
    assert marker["summary_json"] == "{}"
    assert before["followers_total"] == 0
    assert before["new_followers"] == 0

    repaired = PerformanceAnalyzer(FollowerX([legacy]), migrated)
    summary = repaired.capture_follower_snapshot(NOW)
    restarted = PerformanceAnalyzer(FollowerX([legacy]), Database(db_path))
    replay = restarted.capture_follower_snapshot(NOW)
    report = restarted.build_weekly_report(date.fromisoformat(observed_on))

    assert summary == replay == {
        "followers_total": 1,
        "new_total": 1,
        "new_relevant": 0,
        "source_counts": {"unattributed": 1},
        "follow_backs_by_source": {},
    }
    assert report["followers_total"] == 1
    assert report["new_followers"] == 1
    with migrated._conn() as conn:
        marker = conn.execute(
            "SELECT completed, followers_total, summary_json "
            "FROM follower_snapshot_runs WHERE observed_on = ?",
            (observed_on,),
        ).fetchone()
        rows = conn.execute(
            "SELECT user_id FROM follower_snapshots WHERE observed_on = ?",
            (observed_on,),
        ).fetchall()
    assert marker["completed"] == 1
    assert marker["followers_total"] == 1
    assert json.loads(marker["summary_json"]) == summary
    assert [row["user_id"] for row in rows] == ["710"]


def test_concurrent_legacy_repair_has_one_completed_transition(tmp_path):
    db_path = str(tmp_path / "legacy-concurrent.db")
    profile = follower_profile("700", "concurrentown")
    _seed_legacy_follower_run(db_path, profile)
    migrated = Database(db_path)
    with migrated._conn() as conn:
        conn.execute("CREATE TABLE repair_audit (id INTEGER PRIMARY KEY)")
        conn.execute("""
            CREATE TRIGGER count_completed_repair
            AFTER UPDATE OF completed ON follower_snapshot_runs
            WHEN OLD.completed = 0 AND NEW.completed = 1
            BEGIN INSERT INTO repair_audit (id) VALUES (NULL); END
        """)
    ctx = multiprocessing.get_context("spawn")
    start_event = ctx.Event()
    result_queue = ctx.Queue()
    processes = [
        ctx.Process(target=_capture_worker, args=(db_path, start_event, result_queue))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    results = [result_queue.get(timeout=5) for _process in processes]

    assert results[0] == results[1]
    assert results[0]["new_total"] == 1
    with Database(db_path)._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM repair_audit").fetchone()[0] == 1
        assert conn.execute(
            "SELECT completed FROM follower_snapshot_runs"
        ).fetchone()[0] == 1


def test_hard_crash_during_legacy_repair_keeps_marker_nonfinal_and_report_empty(
    tmp_path,
):
    db_path = str(tmp_path / "legacy-crash.db")
    legacy = follower_profile("710", "legacy_owner")
    observed_on = _seed_legacy_follower_run(db_path, legacy)
    database = Database(db_path)
    with database._conn() as conn:
        conn.execute("""
            CREATE TRIGGER task11_crash_on_second_follower
            BEFORE INSERT ON follower_snapshots
            WHEN NEW.user_id = '502'
            BEGIN SELECT task11_hard_crash(); END
        """)
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(target=_hard_crash_capture_worker, args=(db_path,))
    process.start()
    process.join(timeout=20)

    assert process.exitcode == 73
    restarted = Database(db_path)
    report = PerformanceAnalyzer(FollowerX([]), restarted).build_weekly_report(
        date.fromisoformat(observed_on),
    )
    with restarted._conn() as conn:
        marker = conn.execute(
            "SELECT completed, followers_total FROM follower_snapshot_runs"
        ).fetchone()
        rows = conn.execute(
            "SELECT user_id FROM follower_snapshots ORDER BY user_id"
        ).fetchall()
    assert dict(marker) == {"completed": 0, "followers_total": 999}
    assert [row["user_id"] for row in rows] == ["710"]
    assert report["followers_total"] == 0
    assert report["new_followers"] == 0


def test_legacy_repair_preserves_first_seen_before_manual_decision(tmp_path):
    db_path = str(tmp_path / "legacy-first-seen.db")
    profile = follower_profile("711", "legacy_first")
    observed_on = _seed_legacy_follower_run(db_path, profile)
    database = Database(db_path)
    candidate_id = add_candidate(database, profile, source="network")
    assert database.mark_candidate_decision(
        candidate_id,
        "followed_manually",
        decided_at=NOW + timedelta(hours=1),
    )

    summary = PerformanceAnalyzer(
        FollowerX([profile]), database,
    ).capture_follower_snapshot(NOW + timedelta(hours=2))

    candidate = database.get_growth_candidate(profile["id"])
    assert candidate["followed_back_at"] is None
    assert summary["follow_backs_by_source"] == {}
    with database._conn() as conn:
        snapshot = conn.execute(
            "SELECT first_seen_at FROM follower_snapshots "
            "WHERE observed_on = ? AND user_id = ?",
            (observed_on, profile["id"]),
        ).fetchone()
    assert snapshot["first_seen_at"] == NOW.isoformat()


@pytest.mark.parametrize(
    ("first_seen", "expected_conversion"),
    [
        (NOW - timedelta(hours=2), False),
        (NOW - timedelta(hours=1), False),
        (NOW - timedelta(minutes=30), True),
        ("malformed", False),
    ],
)
def test_task10_preobservation_uses_preserved_first_seen_for_strict_attribution(
    tmp_path, first_seen, expected_conversion,
):
    profile = follower_profile("121", "preobserved")
    database = Database(str(tmp_path / f"preobserved-{expected_conversion}.db"))
    candidate_id = add_candidate(database, profile, source="network")
    assert database.mark_candidate_decision(
        candidate_id,
        "followed_manually",
        decided_at=NOW - timedelta(hours=1),
    )
    observed_on = NOW.date().isoformat()
    assert database.save_follower_snapshot(
        observed_on, profile, relevant=True, source="task10",
    )
    stored_first_seen = (
        first_seen.isoformat() if isinstance(first_seen, datetime) else first_seen
    )
    with database._conn() as conn:
        conn.execute(
            "UPDATE follower_snapshots SET first_seen_at = ? WHERE user_id = ?",
            (stored_first_seen, profile["id"]),
        )

    summary = PerformanceAnalyzer(
        FollowerX([profile]), database,
    ).capture_follower_snapshot(NOW)

    candidate = database.get_growth_candidate(profile["id"])
    assert (candidate["followed_back_at"] is not None) is expected_conversion
    assert summary["follow_backs_by_source"] == (
        {"network": 1} if expected_conversion else {}
    )
    with database._conn() as conn:
        snapshot = conn.execute(
            "SELECT first_seen_at FROM follower_snapshots WHERE user_id = ?",
            (profile["id"],),
        ).fetchone()
    assert snapshot["first_seen_at"] == stored_first_seen


@pytest.mark.parametrize(
    ("user_id", "first_seen_at", "decision_at", "expected_conversion"),
    [
        ("720", "2026-08-10T12:30:00+02:00", "2026-08-10T10:00:00+00:00", True),
        ("721", "2026-08-10T12:00:00+02:00", "2026-08-10T10:00:00+00:00", False),
        ("722", "2026-08-10T10:30:00", "2026-08-10T10:00:00+00:00", False),
        ("723", "malformed", "2026-08-10T10:00:00+00:00", False),
        ("724", "2026-08-10T10:30:00+00:00", "2026-08-10T10:00:00", False),
        ("725", "2026-08-10T10:30:00+00:00", "malformed", False),
        ("726", "2026-08-10T10:30:00+00:00", "2026-08-10T11:00:00+00:00", False),
    ],
)
def test_followback_requires_strict_aware_iso_order_revalidated_in_transaction(
    tmp_path, user_id, first_seen_at, decision_at, expected_conversion,
):
    profile = follower_profile(user_id, f"strict{user_id}")
    database = Database(str(tmp_path / f"strict-{user_id}.db"))
    candidate_id = add_candidate(database, profile, source="network")
    assert database.mark_candidate_decision(
        candidate_id, "followed_manually", decided_at=NOW - timedelta(hours=1),
    )
    assert database.save_follower_snapshot(
        NOW.date().isoformat(), profile, relevant=True, source="task10",
    )
    with database._conn() as conn:
        conn.execute(
            "UPDATE follower_snapshots SET first_seen_at = ? WHERE user_id = ?",
            (first_seen_at, profile["id"]),
        )
        conn.execute(
            "UPDATE growth_candidates SET decision_at = ? WHERE id = ?",
            (decision_at, candidate_id),
        )

    summary = PerformanceAnalyzer(FollowerX([profile]), database).capture_follower_snapshot(
        NOW,
    )

    candidate = database.get_growth_candidate(profile["id"])
    assert (candidate["followed_back_at"] is not None) is expected_conversion
    assert summary["follow_backs_by_source"] == (
        {"network": 1} if expected_conversion else {}
    )


def test_followback_reloads_timestamp_values_changed_inside_capture_transaction(
    tmp_path,
):
    profile = follower_profile("799", "trigger_owner")
    database = Database(str(tmp_path / "timestamp-trigger.db"))
    candidate_id = add_candidate(database, profile, source="network")
    assert database.mark_candidate_decision(
        candidate_id, "followed_manually", decided_at=NOW - timedelta(hours=1),
    )
    with database._conn() as conn:
        conn.execute("""
            CREATE TRIGGER make_decision_timestamp_naive
            AFTER INSERT ON follower_snapshots
            WHEN NEW.user_id = '799'
            BEGIN
                UPDATE growth_candidates
                SET decision_at = '2026-08-10T09:00:00'
                WHERE user_id = NEW.user_id;
            END
        """)

    summary = PerformanceAnalyzer(FollowerX([profile]), database).capture_follower_snapshot(
        NOW,
    )

    assert database.get_growth_candidate(profile["id"])["followed_back_at"] is None
    assert summary["follow_backs_by_source"] == {}


def test_same_day_older_and_equal_captures_are_idempotent_and_monotonic(tmp_path):
    newer_profile = follower_profile("122", "newer_owner")
    stale_profile = follower_profile("123", "stale_owner")
    equal_profile = follower_profile("124", "equal_owner")
    analyzer, fake_x, database = analyzer_for(tmp_path, [newer_profile])
    newest_time = NOW + timedelta(hours=3)

    newest = analyzer.capture_follower_snapshot(newest_time)
    fake_x.followers = [stale_profile]
    stale = analyzer.capture_follower_snapshot(NOW)
    fake_x.followers = [equal_profile]
    equal = analyzer.capture_follower_snapshot(newest_time)

    assert stale == equal == newest
    with database._conn() as conn:
        rows = conn.execute(
            "SELECT user_id, captured_at FROM follower_snapshots ORDER BY user_id"
        ).fetchall()
        run = conn.execute(
            "SELECT followers_total, captured_at FROM follower_snapshot_runs"
        ).fetchone()
    assert [dict(row) for row in rows] == [{
        "user_id": "122",
        "captured_at": newest_time.isoformat(),
    }]
    assert dict(run) == {
        "followers_total": 1,
        "captured_at": newest_time.isoformat(),
    }


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
    assert [result["new_total"] for result in results] == [1, 1]
    with Database(db_path)._conn() as conn:
        rows = conn.execute("SELECT user_id, is_new FROM follower_snapshots").fetchall()
    assert [dict(row) for row in rows] == [{"user_id": "700", "is_new": 1}]
    restarted = PerformanceAnalyzer(
        FollowerX([follower_profile("700", "concurrentown")]),
        Database(db_path),
    )
    assert restarted.capture_follower_snapshot(NOW)["new_total"] == 1


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


def test_only_exact_nonempty_string_tweet_ids_feed_reports_and_metrics(tmp_path):
    analyzer, _fake_x, database = analyzer_for(tmp_path)
    created_at = NOW - timedelta(days=1)
    invalid_ids = [None, "", "   ", sqlite3.Binary(b"ghost")]
    for index, tweet_id in enumerate(invalid_ids):
        _insert_post(
            database,
            tweet_id,
            f"ghost_{index}",
            created_at - timedelta(minutes=index),
            900 + index,
        )
    _insert_post(database, "confirmed-1", "confirmed", created_at, 20)

    report = analyzer.build_weekly_report(date(2026, 8, 10))

    assert report["post_count"] == 1
    assert report["content_by_category"] == {"confirmed": 1}
    assert report["median_impressions"] == 20
    assert database.get_category_performance(days=30, end_at=NOW) == {
        "confirmed": {"impressions": 20, "engagement": 0, "posts": 1},
    }
    assert database.get_recent_tweet_ids() == ["confirmed-1"]


def test_ghost_post_cannot_open_the_thirty_day_reweighting_gate(tmp_path):
    analyzer, _fake_x, database = analyzer_for(tmp_path)
    _insert_post(database, "   ", "ghost", NOW - timedelta(days=45), 999)
    valid_created_at = NOW - timedelta(days=2)
    _insert_post(database, "confirmed-2", "strong", valid_created_at, 100)
    with database._conn() as conn:
        conn.execute(
            "UPDATE tweet_metrics SET likes = 10 WHERE tweet_id = 'confirmed-2'"
        )

    assert database.get_first_posted_at() == valid_created_at.isoformat()
    assert analyzer.recompute_category_weights(now=NOW) == {}
    assert database.get_all_category_weights() == {}


def test_weekly_report_skips_pathological_counter_without_crashing(tmp_path):
    analyzer, _fake_x, database = analyzer_for(tmp_path)
    database.set_state("growth_queries:2026-08-10", "9" * 5000)
    database.set_state("growth_profile_evaluations:2026-08-10", "7" * 5000)

    report = analyzer.build_weekly_report(date(2026, 8, 10))

    assert report["query_budget_used"] == 0
    assert report["profiles_evaluated"] == 0


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
