import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from modules.adaptive_timing import AdaptiveTimingPolicy, TimingSample
from modules.analytics import PerformanceAnalyzer
from modules.database import Database
from modules.media_processor import MediaProcessor
from modules.publisher import Publisher
from modules.twitter_client import XPublicationRejected


NOW = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)


def _publication_plan_claim_process(
    db_path,
    plan_id,
    revision,
    due_iso,
    barrier,
    outcomes,
):
    database = Database(db_path)
    barrier.wait(timeout=10)
    claimed = database.claim_due_publication_plan(
        plan_id,
        revision,
        datetime.fromisoformat(due_iso),
        90,
    )
    outcomes.put(claimed is not None)


def _database_constructor_process(db_path, barrier, outcomes):
    try:
        barrier.wait(timeout=10)
        Database(db_path)
        outcomes.put("ok")
    except Exception as exc:
        outcomes.put(type(exc).__name__)


def _policy():
    return AdaptiveTimingPolicy(
        audience_timezone="America/New_York",
        morning_window="08:30-11:30",
        evening_window="16:30-20:30",
        minimum_gap_hours=6,
        timing_min_posts=30,
        weekday_min_posts=90,
    )


def _three_timing_decision():
    from modules.adaptive_timing import DailyTimingDecision

    zone = ZoneInfo("America/New_York")
    return DailyTimingDecision(
        times=(
            datetime(2026, 8, 25, 9, 15, tzinfo=zone),
            datetime(2026, 8, 25, 14, 0, tzinfo=zone),
            datetime(2026, 8, 25, 19, 0, tzinfo=zone),
        ),
        bucket_ids=("morning:0", "midday:0", "evening:0"),
        reason="cold_start",
    )


def _replace_publication_plans_with_legacy_schema(path):
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX IF EXISTS uq_publication_plans_active_draft")
        conn.execute("DROP TABLE publication_plans")
        conn.execute("""
            CREATE TABLE publication_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                local_date TEXT NOT NULL,
                position INTEGER NOT NULL CHECK (position IN (1, 2)),
                scheduled_for TEXT NOT NULL UNIQUE,
                draft_id INTEGER REFERENCES post_drafts(id),
                draft_revision INTEGER,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'open', 'planned', 'publishing', 'published',
                        'simulated', 'skipped', 'unknown'
                    )
                ),
                selection_reason_json TEXT NOT NULL DEFAULT '{}',
                claim_token TEXT,
                published_tweet_id TEXT,
                revision INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(local_date, position)
            )
        """)
        for position, hour in ((1, 9), (2, 19)):
            timestamp = datetime(
                2026, 8, 24, hour, 0, tzinfo=timezone.utc,
            ).isoformat()
            conn.execute("""
                INSERT INTO publication_plans (
                    local_date, position, scheduled_for, status,
                    selection_reason_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'open', ?, ?, ?)
            """, (
                "2026-08-24",
                position,
                timestamp,
                json.dumps({
                    "timing_bucket": "morning:0" if position == 1 else "evening:0",
                    "timing_reason": "cold_start",
                }),
                timestamp,
                timestamp,
            ))


def _insert_post_and_metrics(
    db,
    *,
    tweet_id="1001",
    posted_at=NOW - timedelta(hours=48),
    measured_at=NOW - timedelta(hours=24),
    impressions=100,
    likes=5,
    retweets=2,
    replies=1,
    bookmarks=1,
):
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO posted_tweets "
            "(tweet_id,text,category,has_link,created_at) VALUES (?,?,?,?,?)",
            (tweet_id, "private tweet body", "gym_strategy", 0, posted_at.isoformat()),
        )
        conn.execute(
            "INSERT OR REPLACE INTO tweet_metrics "
            "(tweet_id,impressions,likes,retweets,replies,bookmarks,checked_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                tweet_id,
                impressions,
                likes,
                retweets,
                replies,
                bookmarks,
                measured_at.isoformat(),
            ),
        )


def test_timing_samples_project_only_mature_allowlisted_metrics(tmp_path):
    db = Database(str(tmp_path / "timing.db"))
    _insert_post_and_metrics(db)

    samples = db.get_publication_timing_samples(NOW, min_age_hours=24)

    assert samples == [TimingSample(
        scheduled_for=NOW - timedelta(hours=48),
        measured_at=NOW - timedelta(hours=24),
        impressions=100,
        engagements=9,
    )]
    assert not hasattr(samples[0], "text")


def test_timing_samples_reject_boundary_corruption_and_duplicates(tmp_path):
    db = Database(str(tmp_path / "timing-invalid.db"))
    cases = (
        ("", NOW - timedelta(hours=48), NOW - timedelta(hours=24), 100, 1),
        ("01", NOW - timedelta(hours=48), NOW - timedelta(hours=24), 100, 1),
        ("1002", NOW - timedelta(hours=47), NOW - timedelta(hours=24), 1, 2),
        ("1003", NOW - timedelta(hours=48), NOW + timedelta(seconds=1), 100, 1),
        ("1004", NOW - timedelta(hours=48), NOW - timedelta(hours=25), -1, 0),
    )
    for tweet_id, posted, measured, impressions, likes in cases:
        _insert_post_and_metrics(
            db,
            tweet_id=tweet_id,
            posted_at=posted,
            measured_at=measured,
            impressions=impressions,
            likes=likes,
            retweets=0,
            replies=0,
            bookmarks=0,
        )
    _insert_post_and_metrics(db, tweet_id="2000")
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO posted_tweets "
            "(tweet_id,text,category,has_link,created_at) VALUES (?,?,?,?,?)",
            (
                "2000",
                "duplicate",
                "proof",
                0,
                (NOW - timedelta(hours=48)).isoformat(),
            ),
        )

    assert db.get_publication_timing_samples(NOW, min_age_hours=24) == []
    assert db.get_publication_timing_samples(NOW, min_age_hours=True) == []


def test_performance_analyzer_exposes_same_timing_samples(tmp_path):
    db = Database(str(tmp_path / "analyzer-timing.db"))
    _insert_post_and_metrics(db)
    analyzer = PerformanceAnalyzer(object(), db)

    assert analyzer.timing_samples(NOW) == db.get_publication_timing_samples(NOW)


def test_saved_owned_metrics_use_aware_clock_and_reject_bool_values(tmp_path):
    db = Database(str(tmp_path / "saved-metrics.db"))
    current = datetime.now(timezone.utc)
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO posted_tweets "
            "(tweet_id,text,category,has_link,created_at) VALUES (?,?,?,?,?)",
            (
                "3000",
                "private body",
                "proof",
                0,
                (current - timedelta(hours=48)).isoformat(),
            ),
        )

    assert db.save_tweet_metrics("3000", 100, 5, 2, 1, 1) is True
    assert db.save_tweet_metrics("3001", True, 0, 0, 0, 0) is False
    samples = db.get_publication_timing_samples(
        current + timedelta(seconds=1),
    )
    assert len(samples) == 1
    assert samples[0].engagements == 9


def _approved_queue_draft(
    db,
    *,
    text,
    category,
    score,
    expiry=None,
    media_id=None,
):
    source_id = db.add_content_source(
        "evergreen_idea",
        f"Grounded source for {category}",
    )
    draft_id = db.create_post_draft(
        text,
        category,
        [source_id],
        {"total": score},
        (NOW + timedelta(days=30, microseconds=draft_id_seed(text))).isoformat(),
        f"adaptive:{text}",
    )
    queued = db.ensure_editorial_queue(draft_id)
    if media_id is not None:
        assert db.attach_media_to_draft(media_id, draft_id)
        queued = db.get_queue_draft(draft_id)
    assert db.save_review_translation(
        draft_id,
        queued["revision"],
        f"Traduzione italiana privata {draft_id}",
    )
    ready = db.get_queue_draft(draft_id)
    assert db.approve_queued_draft_atomic(
        draft_id,
        ready["revision"],
        ready["queue_revision"],
        "floriano",
        NOW.isoformat(),
    )
    if expiry is not None:
        with db._conn() as conn:
            conn.execute(
                "UPDATE content_sources SET expires_at = ? WHERE id = ?",
                (expiry.isoformat(), source_id),
            )
    return draft_id


def draft_id_seed(text):
    return sum(text.encode("utf-8")) % 1_000_000


def _planner(db, *, now=NOW, dry_run=True, installation_id="install-1"):
    from modules.publication_queue import PublicationPlanner

    return PublicationPlanner(
        db=db,
        timing_policy=_policy(),
        timing_sample_provider=lambda current: db.get_publication_timing_samples(
            current
        ),
        now_fn=lambda: now,
        audience_timezone="America/New_York",
        installation_id_provider=lambda: installation_id,
        source_expiry_safety_margin=timedelta(hours=2),
        max_links_per_week=1,
        dry_run=dry_run,
    )


def test_ensure_day_is_restart_stable_and_concurrent(tmp_path):
    path = str(tmp_path / "plans.db")
    Database(path)
    barrier = threading.Barrier(2)
    results = []

    def worker():
        planner = _planner(Database(path))
        barrier.wait(timeout=5)
        results.append(planner.ensure_day(NOW))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert len(results) == 2
    assert [[row["id"] for row in result] for result in results][0] == [
        row["id"] for row in results[1]
    ]
    assert len(results[0]) == 2
    assert all(row["status"] == "open" for row in results[0])
    assert _planner(Database(path)).ensure_day(NOW) == results[0]


def test_legacy_two_position_database_migrates_and_preserves_existing_plans(tmp_path):
    path = str(tmp_path / "legacy-two-position.db")
    Database(path)
    _replace_publication_plans_with_legacy_schema(path)

    migrated = Database(path)
    existing = migrated.list_publication_positions()

    assert [row["position"] for row in existing] == [1, 2]
    with migrated._conn() as conn:
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='publication_plans'"
        ).fetchone()["sql"]
        assert "position IN (1, 2, 3)" in schema
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_legacy_publication_plan_migration_is_constructor_safe_and_exact(tmp_path):
    path = str(tmp_path / "legacy-concurrent.db")
    Database(path)
    _replace_publication_plans_with_legacy_schema(path)
    with sqlite3.connect(path) as conn:
        before = conn.execute(
            "SELECT * FROM publication_plans ORDER BY id"
        ).fetchall()

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(12)
    outcomes = context.Queue()
    processes = [
        context.Process(
            target=_database_constructor_process,
            args=(path, barrier, outcomes),
        )
        for _ in range(12)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)

    assert not any(process.is_alive() for process in processes)
    assert [process.exitcode for process in processes] == [0] * 12
    assert sorted(outcomes.get(timeout=2) for _ in processes) == ["ok"] * 12
    with sqlite3.connect(path) as conn:
        after = conn.execute(
            "SELECT * FROM publication_plans ORDER BY id"
        ).fetchall()
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='uq_publication_plans_active_draft'"
        ).fetchall()
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert after == before
    assert indexes == [("uq_publication_plans_active_draft",)]


def test_legacy_publication_plan_migration_rolls_back_on_hard_crash(tmp_path):
    path = str(tmp_path / "legacy-crash.db")
    Database(path)
    _replace_publication_plans_with_legacy_schema(path)
    script = r'''
import os
import sqlite3
import sys
import modules.database as database_module

real_connect = sqlite3.connect

class CrashCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, parameters=()):
        result = self._cursor.execute(sql, parameters)
        if "INSERT INTO publication_plans_v3" in sql:
            os._exit(97)
        return self

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def __iter__(self):
        return iter(self._cursor)

class CrashConnection:
    def __init__(self, connection):
        object.__setattr__(self, "_connection", connection)

    def __setattr__(self, name, value):
        setattr(self._connection, name, value)

    def cursor(self):
        return CrashCursor(self._connection.cursor())

    def __getattr__(self, name):
        return getattr(self._connection, name)

database_module.sqlite3.connect = lambda path: CrashConnection(real_connect(path))
database_module.Database(sys.argv[1])
'''
    crashed = subprocess.run(
        [sys.executable, "-c", script, path],
        cwd=os.getcwd(),
        check=False,
    )
    assert crashed.returncode == 97
    with sqlite3.connect(path) as conn:
        schema_after_crash = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='publication_plans'"
        ).fetchone()[0]
        rows_after_crash = conn.execute(
            "SELECT position FROM publication_plans ORDER BY position"
        ).fetchall()
    assert "position IN (1, 2)" in schema_after_crash
    assert rows_after_crash == [(1,), (2,)]

    recovered = Database(path)
    assert [row["position"] for row in recovered.list_publication_positions()] == [1, 2]
    with recovered._conn() as conn:
        recovered_schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='publication_plans'"
        ).fetchone()["sql"]
        assert "position IN (1, 2, 3)" in recovered_schema
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_new_database_persists_three_publication_positions(tmp_path):
    db = Database(str(tmp_path / "three-position.db"))
    now = datetime(2026, 8, 25, 4, 0, tzinfo=timezone.utc)

    positions = db.create_or_get_publication_positions(
        date(2026, 8, 25), _three_timing_decision(), now,
    )

    assert [row["position"] for row in positions] == [1, 2, 3]
    assert [row["selection_reason"]["timing_bucket"] for row in positions] == [
        "morning:0", "midday:0", "evening:0",
    ]


def test_ensure_day_reuses_persisted_installation_id_without_provider(tmp_path):
    db = Database(str(tmp_path / "installation-restart.db"))
    first = _planner(db).ensure_day(NOW)

    def unavailable_provider():
        raise RuntimeError("provider raw failure")

    from modules.publication_queue import PublicationPlanner

    restarted = PublicationPlanner(
        db=Database(str(tmp_path / "installation-restart.db")),
        timing_policy=_policy(),
        timing_sample_provider=lambda _current: [],
        now_fn=lambda: NOW,
        audience_timezone="America/New_York",
        installation_id_provider=unavailable_provider,
        source_expiry_safety_margin=timedelta(hours=2),
        max_links_per_week=1,
        dry_run=True,
    )

    assert restarted.ensure_day(NOW) == first


def test_reconcile_prefers_valid_expiring_source_then_category_diversity(tmp_path):
    db = Database(str(tmp_path / "selection.db"))
    urgent_id = _approved_queue_draft(
        db,
        text="Urgent grounded operator insight",
        category="gym_strategy",
        score=80,
        expiry=NOW + timedelta(days=2),
    )
    proof_id = _approved_queue_draft(
        db,
        text="High quality proof without expiring source",
        category="proof",
        score=90,
    )
    duplicate_category_id = _approved_queue_draft(
        db,
        text="Same category alternative",
        category="gym_strategy",
        score=90,
    )
    planner = _planner(db)

    plans = planner.reconcile(NOW)

    assert [plan["draft_id"] for plan in plans] == [urgent_id, proof_id]
    assert duplicate_category_id not in {plan["draft_id"] for plan in plans}
    for plan in plans:
        assert set(plan["selection_reason"]).issubset({
            "source_urgency",
            "score",
            "category_diversity",
            "format_diversity",
            "approval_age",
            "timing_reason",
            "timing_bucket",
        })
        encoded = json.dumps(plan["selection_reason"])
        assert "grounded" not in encoded.lower()
        assert "traduzione" not in encoded.lower()


def test_reconcile_ignores_old_legacy_naive_link_rows_at_atomic_boundary(tmp_path):
    db = Database(str(tmp_path / "legacy-naive-link-quota.db"))
    linked_id = _approved_queue_draft(
        db,
        text="Read https://flexdropin.com/blog/operator-guide",
        category="proof",
        score=90,
    )
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO posted_tweets "
            "(tweet_id,text,category,has_link,created_at) VALUES (?,?,?,?,?)",
            (
                "3001",
                "Old linked post",
                "proof",
                1,
                "2026-08-01T12:00:00",
            ),
        )

    plans = _planner(db).reconcile(NOW)

    assert plans[0]["draft_id"] == linked_id
    assert plans[0]["status"] == "planned"


def test_reconcile_blocks_source_expiring_before_safety_margin(tmp_path):
    db = Database(str(tmp_path / "expiry-block.db"))
    blocked_id = _approved_queue_draft(
        db,
        text="Soon expired source",
        category="gym_strategy",
        score=99,
        expiry=NOW + timedelta(hours=3),
    )
    valid_id = _approved_queue_draft(
        db,
        text="Stable source",
        category="proof",
        score=80,
    )

    plans = _planner(db).reconcile(NOW)

    assert blocked_id not in {plan.get("draft_id") for plan in plans}
    assert valid_id in {plan.get("draft_id") for plan in plans}


def test_reconcile_skips_invalid_media_winner_and_uses_safe_candidate(tmp_path):
    db = Database(str(tmp_path / "invalid-media-selection.db"))
    invalid_id = _approved_queue_draft(
        db,
        text="Invalid media high score",
        category="gym_strategy",
        score=99,
    )
    assert db.transition_post_draft(
        invalid_id,
        ["approved"],
        "approved",
        media_id=999_999,
    )
    valid_id = _approved_queue_draft(
        db,
        text="Valid text fallback",
        category="proof",
        score=80,
    )

    plans = _planner(db).reconcile(NOW)

    assigned = {plan.get("draft_id") for plan in plans}
    assert invalid_id not in assigned
    assert valid_id in assigned


def test_reconcile_filters_revoked_malformed_stale_not_before_and_link_quota(
    tmp_path,
):
    db = Database(str(tmp_path / "selection-gates.db"))
    revoked_id = _approved_queue_draft(
        db, text="Revoked source", category="gym_strategy", score=99,
    )
    revoked = db.get_queue_draft(revoked_id)
    with db._conn() as conn:
        conn.execute(
            "UPDATE content_sources SET trust_state = 'revoked' WHERE id = ?",
            (revoked["source_ids"][0],),
        )
    malformed_id = _approved_queue_draft(
        db, text="Malformed score", category="proof", score=98,
    )
    stale_id = _approved_queue_draft(
        db, text="Stale translation", category="shareable_insight", score=97,
    )
    future_id = _approved_queue_draft(
        db, text="Future not before", category="product_education", score=96,
    )
    link_id = _approved_queue_draft(
        db,
        text="Read https://flexdropin.com/blog/operator-guide",
        category="proof",
        score=95,
    )
    valid_id = _approved_queue_draft(
        db, text="Safe eligible fallback", category="founder_story", score=80,
    )
    with db._conn() as conn:
        conn.execute(
            "UPDATE post_drafts SET score_json = ? WHERE id = ?",
            ('{"total":"98"}', malformed_id),
        )
        conn.execute(
            "UPDATE editorial_queue SET translation_status = 'invalidated' "
            "WHERE draft_id = ?",
            (stale_id,),
        )
        conn.execute(
            "UPDATE editorial_queue SET not_before = ? WHERE draft_id = ?",
            ((NOW + timedelta(days=1)).isoformat(), future_id),
        )
        conn.execute(
            "INSERT INTO posted_tweets "
            "(tweet_id,text,category,has_link,created_at) VALUES (?,?,?,?,?)",
            (
                "4000",
                "Recent linked post",
                "proof",
                1,
                (NOW - timedelta(days=1)).isoformat(),
            ),
        )

    plans = _planner(db).reconcile(NOW)

    assigned = {plan.get("draft_id") for plan in plans}
    assert valid_id in assigned
    assert not assigned.intersection({
        revoked_id, malformed_id, stale_id, future_id, link_id,
    })


def test_concurrent_reconcile_assigns_two_distinct_stable_drafts(tmp_path):
    path = str(tmp_path / "reconcile-race.db")
    seed = Database(path)
    for index, category in enumerate((
        "gym_strategy", "proof", "shareable_insight", "founder_story",
    )):
        _approved_queue_draft(
            seed,
            text=f"Concurrent candidate {index}",
            category=category,
            score=90 - index,
        )
    _planner(seed).ensure_day(NOW)
    barrier = threading.Barrier(2)
    errors = []
    results = []

    def worker():
        try:
            planner = _planner(Database(path))
            barrier.wait(timeout=5)
            results.append(planner.reconcile(NOW))
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert not any(thread.is_alive() for thread in threads)
    assert len(results) == 2
    assert all(
        len(result) == 2 and all(plan["status"] == "planned" for plan in result)
        for result in results
    )
    final = Database(path).list_publication_positions(
        NOW.astimezone(_planner(seed).audience_zone).date()
    )
    assert len(final) == 2
    assert all(plan["status"] == "planned" for plan in final)
    assert len({plan["draft_id"] for plan in final}) == 2


def test_concurrent_reconcile_cannot_exceed_one_weekly_link_plan(tmp_path):
    class BarrierAssignDatabase(Database):
        barrier = None

        def __init__(self, path):
            super().__init__(path)
            self.waited = False

        def assign_publication_plan_atomic(self, *args, **kwargs):
            if not self.waited:
                self.waited = True
                self.barrier.wait(timeout=5)
            return super().assign_publication_plan_atomic(*args, **kwargs)

    path = str(tmp_path / "link-plan-race.db")
    seed = Database(path)
    for index in range(2):
        _approved_queue_draft(
            seed,
            text=f"Link candidate {index} https://flexdropin.com/blog/{index}",
            category="proof",
            score=90 - index,
        )
    _planner(seed).ensure_day(NOW)
    BarrierAssignDatabase.barrier = threading.Barrier(2)
    errors = []

    def worker():
        try:
            _planner(BarrierAssignDatabase(path)).reconcile(NOW)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert not any(thread.is_alive() for thread in threads)
    final = Database(path).list_publication_positions(
        NOW.astimezone(_planner(seed).audience_zone).date()
    )
    assert sum(plan["status"] == "planned" for plan in final) == 1
    assert sum(plan["status"] == "open" for plan in final) == 1


def test_dry_run_simulation_keeps_approved_drafts_and_media_reservations(tmp_path):
    db = Database(str(tmp_path / "simulation.db"))
    db.set_state("paused", "false")
    media_root = tmp_path / "simulation-media"
    media_root.mkdir(mode=0o700)
    staged = media_root / "staged.jpg"
    content = b"\xff\xd8\xff\xe0adaptive-simulation"
    staged.write_bytes(content)
    media = MediaProcessor(db).process_new_file(
        str(staged),
        "simulation.jpg",
        "image/jpeg",
        len(content),
        "Simulation preview",
    )
    first_id = _approved_queue_draft(
        db,
        text="First simulated post",
        category="gym_strategy",
        score=90,
        media_id=media["id"],
    )
    second_id = _approved_queue_draft(
        db,
        text="Second simulated post",
        category="proof",
        score=89,
    )
    planner = _planner(db)
    plans = planner.reconcile(NOW)
    due_times = sorted(datetime.fromisoformat(plan["scheduled_for"]) for plan in plans)

    simulated = []
    for due in due_times:
        simulated.extend(planner.simulate_due(due + timedelta(minutes=1)))

    assert len(simulated) == 2
    assert {db.get_post_draft(first_id)["status"], db.get_post_draft(second_id)["status"]} == {
        "approved"
    }
    assert all(plan["status"] == "simulated" for plan in simulated)
    preserved_media = db.get_media_by_id(media["id"])
    assert preserved_media["lifecycle_state"] == "reserved"
    assert preserved_media["reserved_by_draft_id"] == first_id
    assert db.get_queue_counts(
        due.date(), "America/New_York"
    )["approved_or_planned"] == 2


def test_second_dry_run_day_prefers_unsimulated_drafts_without_consuming_queue(
    tmp_path,
):
    db = Database(str(tmp_path / "two-simulation-days.db"))
    db.set_state("paused", "false")
    draft_ids = [
        _approved_queue_draft(
            db,
            text=f"Simulation reserve {index}",
            category=category,
            score=90 - index,
        )
        for index, category in enumerate((
            "gym_strategy", "proof", "shareable_insight", "founder_story",
        ))
    ]
    first_planner = _planner(db, now=NOW)
    first_plans = first_planner.reconcile(NOW)
    first_ids = {plan["draft_id"] for plan in first_plans}
    first_due_times = sorted(
        datetime.fromisoformat(plan["scheduled_for"]) for plan in first_plans
    )
    first_simulated = []
    for due in first_due_times:
        first_simulated.extend(first_planner.simulate_due(due + timedelta(minutes=1)))
    assert len(first_simulated) == 2

    next_day = NOW + timedelta(days=1)
    second_planner = _planner(db, now=next_day)
    second_plans = second_planner.reconcile(next_day)
    second_ids = {plan["draft_id"] for plan in second_plans}

    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == set(draft_ids)
    assert all(db.get_post_draft(draft_id)["status"] == "approved" for draft_id in draft_ids)


def test_plan_reason_decoder_rejects_sensitive_or_malformed_values(tmp_path):
    db = Database(str(tmp_path / "plan-reason-safety.db"))
    plans = _planner(db).ensure_day(NOW)
    with db._conn() as conn:
        conn.execute(
            "UPDATE publication_plans SET selection_reason_json = ? WHERE id = ?",
            (
                json.dumps({
                    "timing_reason": "cold_start",
                    "timing_bucket": "morning:0",
                    "source_urgency": "RAW SOURCE SECRET",
                }),
                plans[0]["id"],
            ),
        )

    assert db.list_publication_positions(
        NOW.astimezone(_planner(db).audience_zone).date()
    ) == [plans[1]]


def _planned_publication_fixture(
    tmp_path,
    *,
    text="ENGLISH_PLAN_SENTINEL",
    media_id=None,
):
    db = Database(str(tmp_path / "planned-publication.db"))
    draft_id = _approved_queue_draft(
        db,
        text=text,
        category="gym_strategy",
        score=91,
        media_id=media_id,
    )
    db.set_state("paused", "false")
    plans = _planner(db, dry_run=False).reconcile(NOW)
    plan = next(plan for plan in plans if plan.get("draft_id") == draft_id)
    return db, draft_id, plan


def test_plan_claim_transitions_plan_and_exact_draft_in_one_transaction(tmp_path):
    db, draft_id, plan = _planned_publication_fixture(tmp_path)
    due = datetime.fromisoformat(plan["scheduled_for"])

    claimed = db.claim_due_publication_plan(
        plan["id"], plan["revision"], due, grace_minutes=90,
    )

    assert claimed is not None
    claimed_draft, draft_claim, plan_claim = claimed
    assert claimed_draft["status"] == "publishing"
    assert draft_claim.draft_id == draft_id
    assert plan_claim.plan_id == plan["id"]
    assert plan_claim.draft_id == draft_id
    assert plan_claim.draft_revision == draft_claim.revision
    stored_plan = db.get_publication_plan(plan["id"])
    stored_draft = db.get_post_draft(draft_id)
    assert stored_plan["status"] == "publishing"
    assert stored_draft["status"] == "publishing"
    assert stored_plan["claim_token"] == plan_claim.claim_token
    assert db.claim_due_publication_plan(
        plan["id"], plan["revision"], due, grace_minutes=90,
    ) is None


def test_plan_claim_rejects_early_late_pause_and_stale_revision(tmp_path):
    db, draft_id, plan = _planned_publication_fixture(tmp_path)
    due = datetime.fromisoformat(plan["scheduled_for"])

    assert db.claim_due_publication_plan(
        plan["id"], plan["revision"], due - timedelta(microseconds=1), 90,
    ) is None
    assert db.claim_due_publication_plan(
        plan["id"], plan["revision"] + 1, due, 90,
    ) is None
    db.set_state("paused", "true")
    assert db.claim_due_publication_plan(
        plan["id"], plan["revision"], due, 90,
    ) is None
    db.set_state("paused", "false")
    assert db.claim_due_publication_plan(
        plan["id"], plan["revision"], due + timedelta(minutes=91), 90,
    ) is None
    assert db.get_publication_plan(plan["id"])["status"] == "planned"
    assert db.get_post_draft(draft_id)["status"] == "approved"


class _PlanXClient:
    def __init__(self, *, error=None, tweet_id="123456"):
        self.error = error
        self.tweet_id = tweet_id
        self.calls = []
        self.media_bytes = []
        self._lock = threading.Lock()

    def post_tweet(self, text, media_path=None, *_args, **_kwargs):
        payload = media_path.read() if media_path is not None else None
        with self._lock:
            self.calls.append({"text": text})
            self.media_bytes.append(payload)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(data={"id": self.tweet_id})


def test_publish_plan_sends_only_english_and_finalizes_plan_atomically(tmp_path):
    db, draft_id, plan = _planned_publication_fixture(tmp_path)
    due = datetime.fromisoformat(plan["scheduled_for"])
    x_client = _PlanXClient()

    result = Publisher(
        db,
        x_client,
        dry_run=False,
        plan_grace_minutes=90,
    ).publish_plan(plan["id"], now=due)

    assert result.status == "published"
    assert result.tweet_id == "123456"
    italian = db.get_queue_draft(draft_id)["translation_it"]
    assert x_client.calls == [{"text": "ENGLISH_PLAN_SENTINEL"}]
    assert italian not in repr(x_client.calls)
    assert db.get_publication_plan(plan["id"])["status"] == "published"
    stored = db.get_post_draft(draft_id)
    assert stored["status"] == "published"
    assert stored["published_tweet_id"] == "123456"
    with db._conn() as conn:
        posted = conn.execute(
            "SELECT text FROM posted_tweets WHERE tweet_id = '123456'"
        ).fetchone()[0]
    assert posted == "ENGLISH_PLAN_SENTINEL"
    assert italian not in posted


def test_spawned_plan_claimers_commit_exactly_one_winner(tmp_path):
    db, draft_id, plan = _planned_publication_fixture(tmp_path)
    due = datetime.fromisoformat(plan["scheduled_for"])
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(4)
    outcomes = context.Queue()
    processes = [
        context.Process(
            target=_publication_plan_claim_process,
            args=(
                db.db_path,
                plan["id"],
                plan["revision"],
                due.isoformat(),
                barrier,
                outcomes,
            ),
        )
        for _ in range(4)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)

    assert not any(process.is_alive() for process in processes)
    assert all(process.exitcode == 0 for process in processes)
    assert sorted(outcomes.get(timeout=2) for _ in processes) == [
        False, False, False, True,
    ]
    assert Database(db.db_path).get_publication_plan(plan["id"])["status"] == (
        "publishing"
    )
    assert Database(db.db_path).get_post_draft(draft_id)["status"] == "publishing"


def test_publish_plan_timeout_is_unknown_and_never_retried(tmp_path):
    db, draft_id, plan = _planned_publication_fixture(tmp_path)
    due = datetime.fromisoformat(plan["scheduled_for"])
    x_client = _PlanXClient(error=TimeoutError("raw provider timeout"))
    publisher = Publisher(
        db,
        x_client,
        dry_run=False,
        plan_grace_minutes=90,
    )

    first = publisher.publish_plan(plan["id"], now=due)
    second = publisher.publish_plan(plan["id"], now=due + timedelta(minutes=1))

    assert first.status == "publication_unknown"
    assert second.status == "not_publishable"
    assert len(x_client.calls) == 1
    assert db.get_publication_plan(plan["id"])["status"] == "unknown"
    assert db.get_post_draft(draft_id)["status"] == "publication_unknown"


def test_publish_plan_dry_run_marks_plan_simulated_without_x(tmp_path):
    db, draft_id, plan = _planned_publication_fixture(tmp_path)
    due = datetime.fromisoformat(plan["scheduled_for"])
    x_client = _PlanXClient()

    result = Publisher(
        db,
        x_client,
        dry_run=True,
        plan_grace_minutes=90,
    ).publish_plan(plan["id"], now=due)

    assert result.status == "dry_run"
    assert x_client.calls == []
    assert db.get_publication_plan(plan["id"])["status"] == "simulated"
    assert db.get_post_draft(draft_id)["status"] == "approved"


def test_dry_run_revalidates_revoked_source_before_marking_simulated(tmp_path):
    db, draft_id, plan = _planned_publication_fixture(tmp_path)
    due = datetime.fromisoformat(plan["scheduled_for"])
    source_id = db.get_queue_draft(draft_id)["source_ids"][0]
    with db._conn() as conn:
        conn.execute(
            "UPDATE content_sources SET trust_state = 'revoked' WHERE id = ?",
            (source_id,),
        )
    x_client = _PlanXClient()

    result = Publisher(
        db, x_client, dry_run=True, plan_grace_minutes=90,
    ).publish_plan(plan["id"], now=due)

    assert result.status == "already_claimed"
    assert db.get_publication_plan(plan["id"])["status"] == "planned"
    assert db.get_post_draft(draft_id)["status"] == "approved"
    assert x_client.calls == []


def test_publish_plan_definite_rejection_restores_same_plan_within_grace(tmp_path):
    db, draft_id, plan = _planned_publication_fixture(tmp_path)
    due = datetime.fromisoformat(plan["scheduled_for"])
    x_client = _PlanXClient(error=XPublicationRejected("raw provider body"))

    result = Publisher(
        db, x_client, dry_run=False, plan_grace_minutes=90,
    ).publish_plan(plan["id"], now=due)

    assert result.status == "publication_failed"
    stored_plan = db.get_publication_plan(plan["id"])
    stored_draft = db.get_post_draft(draft_id)
    assert stored_plan["status"] == "planned"
    assert stored_plan["claim_token"] is None
    assert stored_plan["draft_revision"] == stored_draft["revision"]
    assert stored_draft["status"] == "approved"
    assert "provider" not in repr(stored_draft).lower()


def test_publish_plan_db_failure_after_x_rolls_back_and_becomes_unknown(tmp_path):
    db, draft_id, plan = _planned_publication_fixture(tmp_path)
    due = datetime.fromisoformat(plan["scheduled_for"])
    with db._conn() as conn:
        conn.execute("""
            CREATE TRIGGER fail_planned_publish
            BEFORE UPDATE OF status ON publication_plans
            WHEN NEW.status = 'published'
            BEGIN
                SELECT RAISE(ABORT, 'forced plan finalization failure');
            END
        """)
    x_client = _PlanXClient(tweet_id="987654")

    result = Publisher(
        db, x_client, dry_run=False, plan_grace_minutes=90,
    ).publish_plan(plan["id"], now=due)

    assert result.status == "publication_unknown"
    assert len(x_client.calls) == 1
    assert db.get_publication_plan(plan["id"])["status"] == "unknown"
    assert db.get_post_draft(draft_id)["status"] == "publication_unknown"
    with db._conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM posted_tweets WHERE tweet_id = '987654'"
        ).fetchone()[0] == 0


class _BarrierPlanReadDatabase(Database):
    barrier = None

    def __init__(self, path):
        super().__init__(path)
        self._waited = False

    def get_publication_plan(self, plan_id):
        plan = super().get_publication_plan(plan_id)
        if plan and plan["status"] == "planned" and not self._waited:
            self._waited = True
            self.barrier.wait(timeout=5)
        return plan


def test_two_publishers_claim_one_plan_and_call_x_once(tmp_path):
    path = str(tmp_path / "plan-publisher-race.db")
    setup, draft_id, plan = _planned_publication_fixture(tmp_path)
    setup_path = setup.db_path
    assert setup_path != path
    path = setup_path
    due = datetime.fromisoformat(plan["scheduled_for"])
    _BarrierPlanReadDatabase.barrier = threading.Barrier(2)
    x_client = _PlanXClient(tweet_id="234567")
    results = []
    errors = []

    def worker():
        try:
            results.append(Publisher(
                _BarrierPlanReadDatabase(path),
                x_client,
                dry_run=False,
                plan_grace_minutes=90,
            ).publish_plan(plan["id"], now=due))
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert not any(thread.is_alive() for thread in threads)
    assert sorted(result.status for result in results) == [
        "already_claimed", "published",
    ]
    assert len(x_client.calls) == 1
    assert Database(path).get_post_draft(draft_id)["status"] == "published"


def test_publish_plan_uses_verified_media_stream_and_consumes_reservation(tmp_path):
    db = Database(str(tmp_path / "plan-media.db"))
    media_root = tmp_path / "plan-media-root"
    media_root.mkdir(mode=0o700)
    staged = media_root / "staged.jpg"
    content = b"\xff\xd8\xff\xe0planned-media"
    staged.write_bytes(content)
    media = MediaProcessor(db).process_new_file(
        str(staged),
        "planned.jpg",
        "image/jpeg",
        len(content),
        "Planned publication preview",
    )
    draft_id = _approved_queue_draft(
        db,
        text="ENGLISH_MEDIA_PLAN",
        category="proof",
        score=91,
        media_id=media["id"],
    )
    db.set_state("paused", "false")
    plan = next(
        row for row in _planner(db, dry_run=False).reconcile(NOW)
        if row.get("draft_id") == draft_id
    )
    due = datetime.fromisoformat(plan["scheduled_for"])
    x_client = _PlanXClient(tweet_id="345678")

    result = Publisher(
        db, x_client, dry_run=False, plan_grace_minutes=90,
    ).publish_plan(plan["id"], now=due)

    assert result.status == "published"
    assert x_client.media_bytes == [content]
    stored_media = db.get_media_by_id(media["id"])
    assert stored_media["lifecycle_state"] == "used"
    assert stored_media["used_in_tweet_id"] == "345678"


class _RecordingPlanPublisher:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = []

    def publish_plan(self, plan_id, now=None):
        from modules.publisher import PublishResult

        self.calls.append((plan_id, now))
        status = self.statuses.pop(0)
        return PublishResult(status, "456789" if status == "published" else "")


def test_publication_planner_publishes_two_due_positions_in_order(tmp_path):
    db = Database(str(tmp_path / "publish-due.db"))
    for index, category in enumerate(("gym_strategy", "proof")):
        _approved_queue_draft(
            db,
            text=f"Due publication {index}",
            category=category,
            score=90 - index,
        )
    publisher = _RecordingPlanPublisher(["published", "published"])
    planner = _planner(db, dry_run=False)
    plans = planner.reconcile(NOW)
    due = max(datetime.fromisoformat(plan["scheduled_for"]) for plan in plans)

    outcomes = planner.publish_due(due, publisher=publisher)

    assert [outcome["status"] for outcome in outcomes] == [
        "published", "published",
    ]
    assert [call[0] for call in publisher.calls] == [
        plan["id"] for plan in sorted(plans, key=lambda row: row["position"])
    ]
    assert all(call[1] == due for call in publisher.calls)


def test_publication_planner_stops_cycle_after_ambiguous_outcome(tmp_path):
    db = Database(str(tmp_path / "publish-stop.db"))
    for index, category in enumerate(("gym_strategy", "proof")):
        _approved_queue_draft(
            db,
            text=f"Ambiguous publication {index}",
            category=category,
            score=90 - index,
        )
    publisher = _RecordingPlanPublisher(["publication_unknown", "published"])
    planner = _planner(db, dry_run=False)
    plans = planner.reconcile(NOW)
    due = max(datetime.fromisoformat(plan["scheduled_for"]) for plan in plans)

    outcomes = planner.publish_due(due, publisher=publisher)

    assert [outcome["status"] for outcome in outcomes] == ["publication_unknown"]
    assert len(publisher.calls) == 1


def test_live_publication_planner_without_publisher_fails_closed(tmp_path):
    db = Database(str(tmp_path / "publish-no-boundary.db"))
    _approved_queue_draft(
        db,
        text="No live publisher boundary",
        category="gym_strategy",
        score=90,
    )
    planner = _planner(db, dry_run=False)
    plans = planner.reconcile(NOW)
    due = max(datetime.fromisoformat(plan["scheduled_for"]) for plan in plans)

    assert planner.publish_due(due) == []
    assert all(
        plan["status"] == "planned"
        for plan in db.list_publication_positions(statuses=["planned"])
    )
