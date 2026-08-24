import os
import json
import sqlite3
import subprocess
import sys
import threading
import time as time_module
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from modules.database import Database
from modules.adaptive_timing import DailyTimingDecision
from modules.content_planner import ContentPlan
from modules.draft_pipeline import DraftPipeline
from modules.fact_guard import FactCheckResult
from modules.review_translation import ReviewTranslation


QUEUE_TABLES = {
    "editorial_queue": (
        "draft_id",
        "translation_it",
        "translation_status",
        "review_ready_at",
        "approved_queue_at",
        "not_before",
        "blocked_reason",
        "revision",
        "created_at",
        "updated_at",
    ),
    "publication_plans": (
        "id",
        "local_date",
        "position",
        "scheduled_for",
        "draft_id",
        "draft_revision",
        "status",
        "selection_reason_json",
        "claim_token",
        "published_tweet_id",
        "revision",
        "created_at",
        "updated_at",
    ),
    "draft_replenishment_claims": (
        "token",
        "operator_date",
        "status",
        "claimed_at",
        "expires_at",
        "draft_id",
        "created_at",
        "updated_at",
    ),
}


def _columns(conn, table):
    return tuple(row[1] for row in conn.execute(f"PRAGMA table_info({table})"))


def test_approved_queue_schema_is_additive_and_exact(tmp_path):
    path = tmp_path / "queue.db"
    Database(str(path))

    conn = sqlite3.connect(path)
    try:
        for table, expected_columns in QUEUE_TABLES.items():
            assert _columns(conn, table) == expected_columns
        indexes = {
            row[1]
            for row in conn.execute("PRAGMA index_list(publication_plans)")
        }
        assert "uq_publication_plans_active_draft" in indexes
        marker = conn.execute(
            "SELECT value FROM bot_state WHERE key = ?",
            ("migration:approved_editorial_queue_v1",),
        ).fetchone()
        assert marker == ("complete",)
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        conn.close()

def test_queue_schema_constraints_fail_closed(tmp_path):
    path = tmp_path / "constraints.db"
    Database(str(path))
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(path)
    try:
        with conn:
            for invalid_status in ("", "ready ", "approved", None):
                with pytest.raises(sqlite3.IntegrityError):
                    conn.execute(
                        "INSERT INTO editorial_queue "
                        "(draft_id, translation_status, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?)",
                        (100 + len(str(invalid_status)), invalid_status, now, now),
                    )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO publication_plans "
                    "(local_date, position, scheduled_for, status, created_at, updated_at) "
                    "VALUES ('2026-08-24', 3, ?, 'open', ?, ?)",
                    (now, now, now),
                )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO draft_replenishment_claims "
                    "(token, operator_date, status, claimed_at, expires_at, created_at, updated_at) "
                    "VALUES ('bad', '2026-08-24', 'pending', ?, ?, ?, ?)",
                    (now, now, now, now),
                )
    finally:
        conn.close()


def test_queue_migration_backfills_pending_and_approved_without_mutation(tmp_path):
    path = tmp_path / "legacy-queue.db"
    db = Database(str(path))
    source_id = db.add_content_source("evergreen_idea", "Keep classes discoverable.")
    pending_id = db.create_post_draft(
        "Pending", "gym_strategy", [source_id], {"total": 82},
        "2026-08-20T10:00:00+00:00", "legacy-pending",
    )
    approved_id = db.create_post_draft(
        "Approved", "gym_strategy", [source_id], {"total": 83},
        "2026-08-20T20:00:00+00:00", "legacy-approved",
    )
    assert db.transition_post_draft(approved_id, ["pending_approval"], "approved")
    before = {
        draft_id: db.get_post_draft(draft_id)
        for draft_id in (pending_id, approved_id)
    }

    conn = sqlite3.connect(path)
    with conn:
        conn.execute("DROP TABLE editorial_queue")
        conn.execute("DROP TABLE publication_plans")
        conn.execute("DROP TABLE draft_replenishment_claims")
        conn.execute(
            "DELETE FROM bot_state WHERE key = ?",
            ("migration:approved_editorial_queue_v1",),
        )
    conn.close()

    migrated = Database(str(path))
    with migrated._conn() as check:
        queue_rows = {
            row["draft_id"]: dict(row)
            for row in check.execute(
                "SELECT * FROM editorial_queue ORDER BY draft_id"
            ).fetchall()
        }
    for draft_id in (pending_id, approved_id):
        after = migrated.get_post_draft(draft_id)
        assert after == before[draft_id]
        assert queue_rows[draft_id]["translation_status"] == "pending"
        assert queue_rows[draft_id]["translation_it"] is None
        assert queue_rows[draft_id]["approved_queue_at"] is None


def test_queue_migration_serializes_concurrent_constructors(tmp_path):
    path = tmp_path / "concurrent.db"
    barrier = threading.Barrier(8)
    errors = []

    def construct():
        try:
            barrier.wait(timeout=5)
            Database(str(path))
        except BaseException as error:  # captured for the parent assertion
            errors.append(error)

    threads = [threading.Thread(target=construct) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    conn = sqlite3.connect(path)
    try:
        for table in QUEUE_TABLES:
            assert _columns(conn, table) == QUEUE_TABLES[table]
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        conn.close()


def test_queue_migration_hard_crash_rolls_back_ddl_and_marker(tmp_path):
    path = tmp_path / "crash.db"
    script = r'''
import os
import sqlite3
import sys
import modules.database as database_module

original_connect = sqlite3.connect

class CrashCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        if parameters and parameters[0] == "migration:approved_editorial_queue_v1":
            os._exit(91)
        return super().execute(sql, parameters)

class CrashConnection(sqlite3.Connection):
    def cursor(self, *args, **kwargs):
        kwargs["factory"] = CrashCursor
        return super().cursor(*args, **kwargs)

def crash_connect(path):
    return original_connect(path, factory=CrashConnection)

database_module.sqlite3.connect = crash_connect
database_module.Database(sys.argv[1])
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        check=False,
        timeout=15,
    )
    assert result.returncode == 91

    conn = sqlite3.connect(path)
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert not names.intersection(QUEUE_TABLES)
    finally:
        conn.close()

    Database(str(path))
    Database(str(path))
    conn = sqlite3.connect(path)
    try:
        assert set(QUEUE_TABLES).issubset({
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        })
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        conn.close()


def _queue_fixture(tmp_path, *, key="queue-draft", slot=None):
    db = Database(str(tmp_path / f"{key}.db"))
    source_id = db.add_content_source(
        "evergreen_idea",
        "Flexible booking helps studios fill otherwise empty class capacity.",
    )
    draft_id = db.create_post_draft(
        "Empty spots are perishable inventory.",
        "gym_strategy",
        [source_id],
        {"total": 84},
        slot or "2020-01-01T10:00:00+00:00",
        key,
    )
    return db, source_id, draft_id


def test_queue_translation_and_approval_are_revision_bound(tmp_path):
    db, _source_id, draft_id = _queue_fixture(tmp_path)
    draft = db.get_post_draft(draft_id)
    queue = db.ensure_editorial_queue(draft_id)

    assert queue["translation_status"] == "pending"
    assert db.save_review_translation(
        draft_id,
        draft["revision"],
        "I posti vuoti sono inventario deperibile.",
    )
    current = db.get_queue_draft(draft_id)
    assert current["translation_status"] == "ready"
    assert db.approve_queued_draft_atomic(
        draft_id,
        draft["revision"],
        current["queue_revision"],
        "floriano",
        datetime.now(timezone.utc).isoformat(),
    )
    approved = db.get_queue_draft(draft_id)
    assert approved["status"] == "approved"
    assert approved["approved_by"] == "floriano"
    assert approved["approved_queue_at"] is not None
    assert approved["intended_slot"] == "2020-01-01T10:00:00+00:00"


def test_queue_approval_rejects_missing_translation_and_stale_revisions(tmp_path):
    db, _source_id, draft_id = _queue_fixture(tmp_path, key="stale")
    draft = db.get_post_draft(draft_id)
    queue = db.ensure_editorial_queue(draft_id)
    approved_at = datetime.now(timezone.utc).isoformat()

    assert not db.approve_queued_draft_atomic(
        draft_id, draft["revision"], queue["queue_revision"],
        "floriano", approved_at,
    )
    assert db.save_review_translation(draft_id, draft["revision"], "Traduzione")
    ready = db.get_queue_draft(draft_id)
    assert not db.approve_queued_draft_atomic(
        draft_id, draft["revision"] + 1, ready["queue_revision"],
        "floriano", approved_at,
    )
    assert not db.approve_queued_draft_atomic(
        draft_id, draft["revision"], ready["queue_revision"] - 1,
        "floriano", approved_at,
    )


def test_source_revocation_rolls_back_queue_approval(tmp_path):
    db, source_id, draft_id = _queue_fixture(tmp_path, key="revoked")
    draft = db.get_post_draft(draft_id)
    db.ensure_editorial_queue(draft_id)
    assert db.save_review_translation(draft_id, draft["revision"], "Traduzione")
    ready = db.get_queue_draft(draft_id)
    with db._conn() as conn:
        conn.execute(
            "UPDATE content_sources SET trust_state = 'revoked' WHERE id = ?",
            (source_id,),
        )

    assert not db.approve_queued_draft_atomic(
        draft_id, draft["revision"], ready["queue_revision"],
        "floriano", datetime.now(timezone.utc).isoformat(),
    )
    unchanged = db.get_queue_draft(draft_id)
    assert unchanged["status"] == "pending_approval"
    assert unchanged["approved_queue_at"] is None
    assert unchanged["queue_revision"] == ready["queue_revision"]


def test_two_queue_approval_workers_have_one_winner(tmp_path):
    db, _source_id, draft_id = _queue_fixture(tmp_path, key="approve-race")
    draft = db.get_post_draft(draft_id)
    db.ensure_editorial_queue(draft_id)
    assert db.save_review_translation(draft_id, draft["revision"], "Traduzione")
    ready = db.get_queue_draft(draft_id)
    barrier = threading.Barrier(2)
    results = []

    def approve():
        worker = Database(db.db_path)
        barrier.wait(timeout=5)
        results.append(worker.approve_queued_draft_atomic(
            draft_id, draft["revision"], ready["queue_revision"],
            "floriano", datetime.now(timezone.utc).isoformat(),
        ))

    threads = [threading.Thread(target=approve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert sorted(results) == [False, True]


def test_translation_invalidation_clears_queue_approval_atomically(tmp_path):
    db, _source_id, draft_id = _queue_fixture(tmp_path, key="invalidate")
    draft = db.get_post_draft(draft_id)
    db.ensure_editorial_queue(draft_id)
    assert db.save_review_translation(draft_id, draft["revision"], "Traduzione")
    ready = db.get_queue_draft(draft_id)
    assert db.approve_queued_draft_atomic(
        draft_id, draft["revision"], ready["queue_revision"],
        "floriano", datetime.now(timezone.utc).isoformat(),
    )
    approved = db.get_queue_draft(draft_id)

    assert db.invalidate_review_translation(draft_id, approved["revision"])
    invalidated = db.get_queue_draft(draft_id)
    assert invalidated["translation_status"] == "invalidated"
    assert invalidated["translation_it"] is None
    assert invalidated["approved_queue_at"] is None
    assert invalidated["status"] == "pending_approval"
    assert invalidated["approved_at"] is None
    assert invalidated["approved_by"] is None


def test_queue_counts_use_operator_timezone_and_allowlisted_states(tmp_path):
    db, _source_id, draft_id = _queue_fixture(tmp_path, key="counts")
    db.ensure_editorial_queue(draft_id)
    assert db.get_queue_counts(date(2026, 8, 25), "Europe/Rome") == {
        "awaiting_translation": 1,
        "awaiting_review": 0,
        "approved_available": 0,
        "approved_or_planned": 0,
        "planned_today": 0,
        "blocked": 0,
    }


def test_replenishment_claim_cap_release_expiry_and_completion(tmp_path):
    db = Database(str(tmp_path / "claims.db"))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    operator_day = now.date()
    claims = [
        db.claim_replenishment(operator_day, 4, now)
        for _ in range(4)
    ]
    assert all(claim and claim["status"] == "claimed" for claim in claims)
    assert db.claim_replenishment(operator_day, 4, now) is None

    assert db.release_replenishment_claim(claims[0]["token"])
    replacement = db.claim_replenishment(operator_day, 4, now)
    assert replacement is not None

    _other_db, _source_id, draft_id = _queue_fixture(
        tmp_path, key="claim-complete",
    )
    assert not db.complete_replenishment_claim(replacement["token"], draft_id)
    own_source = db.add_content_source("evergreen_idea", "Own source")
    own_draft = db.create_post_draft(
        "Own draft", "gym_strategy", [own_source], {"total": 80},
        "2026-08-24T12:00:00+00:00", "own-claim-draft",
    )
    assert db.complete_replenishment_claim(replacement["token"], own_draft)
    assert not db.release_replenishment_claim(replacement["token"])

    future = now + timedelta(seconds=1801)
    assert db.claim_replenishment(operator_day, 4, future) is not None


def test_queue_rejects_retroactive_approval_after_source_expiry(tmp_path):
    db, source_id, draft_id = _queue_fixture(tmp_path, key="expired-source")
    draft = db.get_post_draft(draft_id)
    db.ensure_editorial_queue(draft_id)
    assert db.save_review_translation(draft_id, draft["revision"], "Traduzione")
    ready = db.get_queue_draft(draft_id)
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    with db._conn() as conn:
        conn.execute(
            "UPDATE content_sources SET expires_at = ? WHERE id = ?",
            (yesterday.isoformat(), source_id),
        )

    assert not db.approve_queued_draft_atomic(
        draft_id,
        draft["revision"],
        ready["queue_revision"],
        "floriano",
        (yesterday - timedelta(days=1)).isoformat(),
    )
    assert db.get_queue_draft(draft_id)["status"] == "pending_approval"


def test_queue_rejects_legacy_media_without_verified_identity(tmp_path):
    db, _source_id, draft_id = _queue_fixture(tmp_path, key="legacy-media")
    media_id = db.add_media("legacy.jpg", "/tmp/legacy.jpg", "image")
    assert db.reserve_media(media_id, draft_id)
    with db._conn() as conn:
        conn.execute(
            "UPDATE post_drafts SET media_id = ? WHERE id = ?",
            (media_id, draft_id),
        )
    draft = db.get_post_draft(draft_id)
    db.ensure_editorial_queue(draft_id)
    assert db.save_review_translation(draft_id, draft["revision"], "Traduzione")
    ready = db.get_queue_draft(draft_id)

    assert not db.approve_queued_draft_atomic(
        draft_id,
        draft["revision"],
        ready["queue_revision"],
        "floriano",
        datetime.now(timezone.utc).isoformat(),
    )


def test_queue_approval_fails_closed_on_partial_media_identity(tmp_path):
    db, _source_id, draft_id = _queue_fixture(tmp_path, key="partial-media")
    draft = db.get_post_draft(draft_id)
    db.ensure_editorial_queue(draft_id)
    assert db.save_review_translation(draft_id, draft["revision"], "Traduzione")
    media_id = db.add_media("partial.jpg", "/tmp/partial.jpg", "image")
    assert db.reserve_media(media_id, draft_id)
    with db._conn() as conn:
        conn.execute(
            "UPDATE media_library SET file_device = 7 WHERE id = ?",
            (media_id,),
        )
        conn.execute(
            "UPDATE post_drafts SET media_id = ? WHERE id = ?",
            (media_id, draft_id),
        )
    ready = db.get_queue_draft(draft_id)

    assert not db.approve_queued_draft_atomic(
        draft_id,
        ready["revision"],
        ready["queue_revision"],
        "floriano",
        datetime.now(timezone.utc).isoformat(),
    )


def test_queue_decoder_rejects_future_approval_and_duplicate_sources(tmp_path):
    db, source_id, draft_id = _queue_fixture(tmp_path, key="corrupt-queue")
    db.ensure_editorial_queue(draft_id)
    future = datetime.now(timezone.utc) + timedelta(days=1)
    with db._conn() as conn:
        conn.execute(
            "UPDATE editorial_queue SET approved_queue_at = ? WHERE draft_id = ?",
            (future.isoformat(), draft_id),
        )
    assert db.get_queue_draft(draft_id) is None

    with db._conn() as conn:
        conn.execute(
            "UPDATE editorial_queue SET approved_queue_at = NULL WHERE draft_id = ?",
            (draft_id,),
        )
        conn.execute(
            "UPDATE post_drafts SET source_ids_json = ? WHERE id = ?",
            (f"[{source_id},{source_id}]", draft_id),
        )
    assert db.get_queue_draft(draft_id) is None


def test_queue_decoder_rejects_invalid_draft_status(tmp_path):
    db, _source_id, draft_id = _queue_fixture(tmp_path, key="corrupt-status")
    db.ensure_editorial_queue(draft_id)
    with db._conn() as conn:
        conn.execute(
            "UPDATE post_drafts SET status = 'queue_readyish' WHERE id = ?",
            (draft_id,),
        )
    assert db.get_queue_draft(draft_id) is None

def test_one_draft_cannot_complete_two_replenishment_claims(tmp_path):
    db = Database(str(tmp_path / "duplicate-completion.db"))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    first = db.claim_replenishment(now.date(), 4, now)
    second = db.claim_replenishment(now.date(), 4, now)
    source_id = db.add_content_source("evergreen_idea", "One source")
    draft_id = db.create_post_draft(
        "One draft", "gym_strategy", [source_id], {"total": 80},
        now.isoformat(), "one-completed-draft",
    )

    assert db.complete_replenishment_claim(first["token"], draft_id)
    assert not db.complete_replenishment_claim(second["token"], draft_id)


def test_replenishment_claims_enforce_cap_across_workers(tmp_path):
    path = tmp_path / "claim-race.db"
    Database(str(path))
    barrier = threading.Barrier(12)
    results = []

    def claim():
        worker = Database(str(path))
        barrier.wait(timeout=5)
        results.append(worker.claim_replenishment(
            date(2026, 8, 24), 4,
            datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc),
        ))

    threads = [threading.Thread(target=claim) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert sum(result is not None for result in results) == 4


def test_replenishment_claim_hard_crash_before_commit_consumes_no_cap(tmp_path):
    path = tmp_path / "claim-crash.db"
    Database(str(path))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    script = r'''
import os
import sqlite3
import sys
from datetime import datetime, timezone
import modules.database as database_module

original_connect = sqlite3.connect

class CrashConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        cursor = super().execute(sql, parameters)
        if "INSERT INTO draft_replenishment_claims" in sql:
            os._exit(92)
        return cursor

def crash_connect(path):
    return original_connect(path, factory=CrashConnection)

database_module.sqlite3.connect = crash_connect
now = datetime.fromisoformat(sys.argv[2])
database_module.Database(sys.argv[1]).claim_replenishment(now.date(), 4, now)
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(path), now.isoformat()],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        check=False,
        timeout=15,
    )
    assert result.returncode == 92
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM draft_replenishment_claims"
        ).fetchone()[0] == 0
    assert Database(str(path)).claim_replenishment(now.date(), 1, now) is not None


def _timing_decision(local_day=date(2026, 8, 24)):
    zone = ZoneInfo("America/New_York")
    return DailyTimingDecision(
        times=(
            datetime.combine(local_day, datetime.min.time(), tzinfo=zone)
            + timedelta(hours=9, minutes=15),
            datetime.combine(local_day, datetime.min.time(), tzinfo=zone)
            + timedelta(hours=18, minutes=20),
        ),
        bucket_ids=("morning:0", "evening:1"),
        reason="cold_start",
    )


def test_publication_plan_creation_is_stable_and_simulated_is_revision_cas(tmp_path):
    db = Database(str(tmp_path / "plans.db"))
    now = datetime(2026, 8, 24, 4, 5, tzinfo=timezone.utc)
    decision = _timing_decision()

    first = db.create_or_get_publication_positions(date(2026, 8, 24), decision, now)
    second = db.create_or_get_publication_positions(date(2026, 8, 24), decision, now)

    assert first == second
    assert [row["position"] for row in first] == [1, 2]
    assert [row["status"] for row in first] == ["open", "open"]
    assert first[0]["selection_reason"] == {
        "timing_bucket": "morning:0",
        "timing_reason": "cold_start",
    }
    assert not db.mark_publication_plan_simulated(first[0]["id"], 99)
    assert not db.mark_publication_plan_simulated(
        first[0]["id"], first[0]["revision"],
    )


def test_publication_plan_creation_is_one_stable_pair_across_workers(tmp_path):
    path = tmp_path / "plan-race.db"
    Database(str(path))
    barrier = threading.Barrier(8)
    results = []

    def create_positions():
        worker = Database(str(path))
        barrier.wait(timeout=5)
        results.append(worker.create_or_get_publication_positions(
            date(2026, 8, 24),
            _timing_decision(),
            datetime(2026, 8, 24, 4, 5, tzinfo=timezone.utc),
        ))

    threads = [threading.Thread(target=create_positions) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert len(results) == 8
    assert len({tuple(row["id"] for row in result) for result in results}) == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM publication_plans"
        ).fetchone() == (2,)


def test_publication_plan_decoder_rejects_nonallowlisted_reason(tmp_path):
    db = Database(str(tmp_path / "plan-corrupt.db"))
    now = datetime(2026, 8, 24, 4, 5, tzinfo=timezone.utc)
    assert len(db.create_or_get_publication_positions(
        date(2026, 8, 24), _timing_decision(), now,
    )) == 2
    with db._conn() as conn:
        conn.execute(
            "UPDATE publication_plans SET selection_reason_json = ? WHERE position = 1",
            ('{"prompt":"secret","reason":"cold_start"}',),
        )
    assert db.create_or_get_publication_positions(
        date(2026, 8, 24), _timing_decision(), now,
    ) == []


def test_plan_assignment_is_exact_revision_and_one_draft_per_plan(tmp_path):
    db, _source_id, draft_id = _queue_fixture(tmp_path, key="plan-assign")
    draft = db.get_post_draft(draft_id)
    db.ensure_editorial_queue(draft_id)
    assert db.save_review_translation(draft_id, draft["revision"], "Traduzione")
    ready = db.get_queue_draft(draft_id)
    assert db.approve_queued_draft_atomic(
        draft_id, draft["revision"], ready["queue_revision"],
        "floriano", datetime.now(timezone.utc).isoformat(),
    )
    approved = db.get_queue_draft(draft_id)
    plans = db.create_or_get_publication_positions(
        date(2026, 8, 24), _timing_decision(),
        datetime(2026, 8, 24, 4, 5, tzinfo=timezone.utc),
    )

    assert not db.assign_publication_plan_atomic(
        plans[0]["id"], draft_id, approved["revision"] + 1, {"score": 80},
    )
    assert db.assign_publication_plan_atomic(
        plans[0]["id"], draft_id, approved["revision"], {"score": 80},
    )
    assert not db.assign_publication_plan_atomic(
        plans[1]["id"], draft_id, approved["revision"], {"score": 80},
    )
    available = db.list_approved_queue(datetime.now(timezone.utc))
    assert available == []
    with db._conn() as conn:
        plan = dict(conn.execute(
            "SELECT * FROM publication_plans WHERE id = ?", (plans[0]["id"],)
        ).fetchone())
    assert plan["status"] == "planned"
    assert plan["draft_id"] == draft_id
    assert plan["draft_revision"] == approved["revision"]
    stored_reason = json.loads(plan["selection_reason_json"])
    assert stored_reason == {
        "score": 80,
        "timing_bucket": "morning:0",
        "timing_reason": "cold_start",
    }
    counts = db.get_queue_counts(date(2026, 8, 24), "Europe/Rome")
    assert counts["planned_today"] == 1
    assert db.mark_publication_plan_simulated(
        plans[0]["id"], plan["revision"],
    )
    assert [row["id"] for row in db.list_approved_queue(
        datetime.now(timezone.utc)
    )] == [draft_id]


@pytest.mark.parametrize("invalid_id", (True, False, 0, -1, "1", 1.0, None))
def test_queue_and_plan_identifier_boundaries_fail_closed(tmp_path, invalid_id):
    db = Database(str(tmp_path / f"invalid-{invalid_id!s}.db"))
    assert db.ensure_editorial_queue(invalid_id) is None
    assert db.get_queue_draft(invalid_id) is None
    assert not db.save_review_translation(invalid_id, 0, "Traduzione")
    assert not db.invalidate_review_translation(invalid_id, 0)
    assert not db.assign_publication_plan_atomic(invalid_id, 1, 0, {})
    assert not db.complete_replenishment_claim("token", invalid_id)


def test_plan_assignment_revalidates_source_and_media_binding(tmp_path):
    db, source_id, draft_id = _queue_fixture(tmp_path, key="plan-invalid")
    draft = db.get_post_draft(draft_id)
    db.ensure_editorial_queue(draft_id)
    assert db.save_review_translation(draft_id, draft["revision"], "Traduzione")
    ready = db.get_queue_draft(draft_id)
    assert db.approve_queued_draft_atomic(
        draft_id, draft["revision"], ready["queue_revision"],
        "floriano", datetime.now(timezone.utc).isoformat(),
    )
    approved = db.get_queue_draft(draft_id)
    plans = db.create_or_get_publication_positions(
        date(2026, 8, 24), _timing_decision(),
        datetime(2026, 8, 24, 4, 5, tzinfo=timezone.utc),
    )
    with db._conn() as conn:
        conn.execute(
            "UPDATE content_sources SET trust_state = 'revoked' WHERE id = ?",
            (source_id,),
        )
    assert not db.assign_publication_plan_atomic(
        plans[0]["id"], draft_id, approved["revision"], {},
    )
    with db._conn() as conn:
        conn.execute(
            "UPDATE content_sources SET trust_state = 'verified' WHERE id = ?",
            (source_id,),
        )
        conn.execute(
            "UPDATE post_drafts SET media_id = 999999 WHERE id = ?",
            (draft_id,),
        )
    rebound = db.get_post_draft(draft_id)
    assert not db.assign_publication_plan_atomic(
        plans[0]["id"], draft_id, rebound["revision"], {},
    )


class _QueuePipelineFake:
    def __init__(self, db, *, outcome="created", delay=0):
        self.db = db
        self.outcome = outcome
        self.delay = delay
        self.calls = []
        self.source_id = db.add_content_source(
            "evergreen_idea", "Reduce empty capacity without discounting.",
        )

    def create_for_queue_with_outcome(self, anchor):
        self.calls.append(anchor)
        if self.delay:
            time_module.sleep(self.delay)
        if self.outcome == "raise":
            raise RuntimeError("raw pipeline failure")
        if self.outcome == "rejected":
            return None, "rejected"
        draft, outcome = self.db.create_or_get_post_draft(
            text="Empty class capacity expires when class starts.",
            category="gym_strategy",
            source_ids=[self.source_id],
            score_data={"total": 86},
            intended_slot=anchor.isoformat(),
            publication_key=f"queue-fake:{anchor.isoformat()}",
        )
        if draft is None:
            return None, "rejected"
        queued = self.db.ensure_editorial_queue(draft["id"])
        return queued, outcome


class _QueueTranslatorFake:
    def __init__(self, response="La capacità vuota scade quando inizia la lezione."):
        self.response = response
        self.calls = []

    def translate(self, english_text):
        self.calls.append(english_text)
        if self.response is None:
            return None
        return ReviewTranslation(self.response)


class _QueueMediaMatcherFake:
    def __init__(self):
        self.calls = []

    def attach_best(self, draft_id):
        self.calls.append(draft_id)
        return None


def _replenisher(db, pipeline, translator, media_matcher=None):
    from modules.publication_queue import QueueReplenisher

    return QueueReplenisher(
        db=db,
        pipeline=pipeline,
        translator=translator,
        media_matcher=media_matcher,
        operator_timezone="Europe/Rome",
        approved_queue_target=7,
        pending_review_limit=3,
        daily_generation_cap=4,
    )


def test_replenisher_creates_translates_completes_and_announces_once(tmp_path):
    db = Database(str(tmp_path / "replenish.db"))
    pipeline = _QueuePipelineFake(db)
    translator = _QueueTranslatorFake()
    media = _QueueMediaMatcherFake()
    now = datetime.now(timezone.utc).replace(microsecond=0)

    result = _replenisher(db, pipeline, translator, media).run(now)

    assert result.outcome == "created"
    assert result.draft_id is not None
    assert result.announce is True
    queued = db.get_queue_draft(result.draft_id)
    assert queued["translation_status"] == "ready"
    assert queued["translation_it"] == translator.response
    assert media.calls == [result.draft_id]
    with db._conn() as conn:
        claim = dict(conn.execute(
            "SELECT * FROM draft_replenishment_claims"
        ).fetchone())
    assert claim["status"] == "completed"
    assert claim["draft_id"] == result.draft_id


def _seed_queue_state(db, count, *, approved):
    source_id = db.add_content_source("evergreen_idea", "Queue seed source")
    for index in range(count):
        draft_id = db.create_post_draft(
            f"Queue seed {index}",
            "gym_strategy",
            [source_id],
            {"total": 80 + index},
            f"2026-09-{index + 1:02d}T10:00:00+00:00",
            f"queue-seed:{approved}:{index}",
        )
        draft = db.get_post_draft(draft_id)
        db.ensure_editorial_queue(draft_id)
        if approved:
            assert db.save_review_translation(
                draft_id, draft["revision"], f"Traduzione {index}",
            )
            ready = db.get_queue_draft(draft_id)
            assert db.approve_queued_draft_atomic(
                draft_id,
                draft["revision"],
                ready["queue_revision"],
                "floriano",
                datetime.now(timezone.utc).isoformat(),
            )


@pytest.mark.parametrize(
    ("approved", "count", "expected"),
    ((True, 7, "queue_full"), (False, 3, "pending_full")),
)
def test_replenisher_respects_queue_and_pending_limits(
    tmp_path, approved, count, expected,
):
    db = Database(str(tmp_path / f"limit-{expected}.db"))
    _seed_queue_state(db, count, approved=approved)
    pipeline = _QueuePipelineFake(db)

    result = _replenisher(db, pipeline, _QueueTranslatorFake()).run(
        datetime.now(timezone.utc)
    )

    assert result.outcome == expected
    assert result.draft_id is None
    assert result.announce is False
    assert pipeline.calls == []
    with db._conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM draft_replenishment_claims"
        ).fetchone()[0] == 0


def test_replenisher_counts_future_not_before_approved_reserve(tmp_path):
    db = Database(str(tmp_path / "future-reserve.db"))
    _seed_queue_state(db, 7, approved=True)
    with db._conn() as conn:
        conn.execute(
            "UPDATE editorial_queue SET not_before = ?",
            ((datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),),
        )
    pipeline = _QueuePipelineFake(db)

    result = _replenisher(db, pipeline, _QueueTranslatorFake()).run(
        datetime.now(timezone.utc)
    )

    assert result.outcome == "queue_full"
    assert pipeline.calls == []


def test_replenisher_releases_rejected_or_systemic_generation_claim(tmp_path):
    db = Database(str(tmp_path / "rejected-replenish.db"))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    rejected = _replenisher(
        db, _QueuePipelineFake(db, outcome="rejected"), _QueueTranslatorFake(),
    ).run(now)
    failed = _replenisher(
        db, _QueuePipelineFake(db, outcome="raise"), _QueueTranslatorFake(),
    ).run(now + timedelta(minutes=1))

    assert rejected.outcome == "generation_rejected"
    assert failed.outcome == "failed"
    with db._conn() as conn:
        states = [row[0] for row in conn.execute(
            "SELECT status FROM draft_replenishment_claims ORDER BY claimed_at"
        )]
    assert states == ["released", "released"]


def test_replenisher_reports_daily_cap_after_four_completed_drafts(tmp_path):
    db = Database(str(tmp_path / "service-daily-cap.db"))
    local_day = datetime.now(ZoneInfo("Europe/Rome")).date()
    base = datetime.combine(
        local_day,
        datetime.min.time(),
        tzinfo=ZoneInfo("Europe/Rome"),
    ).astimezone(timezone.utc) + timedelta(hours=10)
    source_id = db.add_content_source("evergreen_idea", "Daily cap source")
    for index in range(4):
        claim = db.claim_replenishment(local_day, 4, base + timedelta(minutes=index))
        draft_id = db.create_post_draft(
            f"Daily cap draft {index}", "gym_strategy", [source_id],
            {"total": 80},
            (base + timedelta(hours=index)).isoformat(),
            f"daily-cap-draft:{index}",
        )
        assert db.complete_replenishment_claim(claim["token"], draft_id)
    pipeline = _QueuePipelineFake(db)

    result = _replenisher(db, pipeline, _QueueTranslatorFake()).run(
        base + timedelta(hours=6)
    )

    assert result.outcome == "daily_cap"
    assert pipeline.calls == []


def test_replenisher_restart_waits_for_expiry_then_reclaims(tmp_path):
    db = Database(str(tmp_path / "restart-claim.db"))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    operator_day = now.astimezone(ZoneInfo("Europe/Rome")).date()
    claim = db.claim_replenishment(
        operator_day,
        4,
        now,
        cycle_key="crashed-cycle",
    )
    assert claim is not None
    pipeline = _QueuePipelineFake(db)
    crashed_anchor = datetime.fromisoformat(claim["claimed_at"]) + timedelta(
        microseconds=claim["ordinal"]
    )
    crashed_draft, crashed_outcome = pipeline.create_for_queue_with_outcome(
        crashed_anchor
    )
    assert crashed_outcome == "created"
    pipeline.calls.clear()
    service = _replenisher(db, pipeline, _QueueTranslatorFake())

    before_expiry = service.run(now + timedelta(minutes=1))
    after_expiry = service.run(now + timedelta(minutes=31))

    assert before_expiry.outcome == "daily_cap"
    assert after_expiry.outcome == "existing"
    assert after_expiry.draft_id == crashed_draft["id"]
    assert after_expiry.announce is False
    assert len(pipeline.calls) == 1
    assert len(db.list_post_drafts()) == 1


def test_replenisher_translation_failure_keeps_retryable_pending_draft(tmp_path):
    db = Database(str(tmp_path / "translation-pending.db"))
    pipeline = _QueuePipelineFake(db)
    translator = _QueueTranslatorFake(response=None)
    service = _replenisher(db, pipeline, translator)
    now = datetime.now(timezone.utc).replace(microsecond=0)

    result = service.run(now)

    assert result.outcome == "translation_pending"
    assert result.draft_id is not None
    assert result.announce is False
    assert db.get_queue_draft(result.draft_id)["translation_status"] == "pending"
    assert service.retry_pending_translations(now + timedelta(minutes=5)) == []
    translator.response = "Traduzione pronta."
    assert service.retry_pending_translations(now + timedelta(minutes=6)) == [
        result.draft_id
    ]
    assert len(pipeline.calls) == 1
    assert db.get_queue_draft(result.draft_id)["translation_status"] == "ready"


def test_translation_retry_limit_bounds_attempts_not_only_successes(tmp_path):
    db = Database(str(tmp_path / "retry-limit.db"))
    _seed_queue_state(db, 5, approved=False)
    translator = _QueueTranslatorFake(response=None)
    service = _replenisher(db, _QueuePipelineFake(db), translator)

    assert service.retry_pending_translations(
        datetime.now(timezone.utc), limit=3,
    ) == []
    assert len(translator.calls) == 3


def test_stale_translation_result_cannot_attach_after_draft_revision_change(tmp_path):
    db = Database(str(tmp_path / "stale-translation.db"))
    pipeline = _QueuePipelineFake(db)

    class MutatingTranslator:
        def translate(self, _english_text):
            draft = db.list_post_drafts(["pending_approval"], limit=1)[0]
            assert db.transition_post_draft(
                draft["id"], ["pending_approval"], "pending_approval"
            )
            return ReviewTranslation("Traduzione ormai stale.")

    result = _replenisher(db, pipeline, MutatingTranslator()).run(
        datetime.now(timezone.utc).replace(microsecond=0)
    )

    assert result.outcome == "translation_pending"
    queued = db.get_queue_draft(result.draft_id)
    assert queued["translation_status"] == "pending"
    assert queued["translation_it"] is None


def test_replenisher_concurrent_same_cycle_creates_one_announced_draft(tmp_path):
    path = tmp_path / "replenish-race.db"
    Database(str(path))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    barrier = threading.Barrier(2)
    results = []

    def run_worker():
        db = Database(str(path))
        service = _replenisher(
            db,
            _QueuePipelineFake(db, delay=0.05),
            _QueueTranslatorFake(),
        )
        barrier.wait(timeout=5)
        results.append(service.run(now))

    threads = [threading.Thread(target=run_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert sum(result.announce for result in results) == 1
    assert sum(result.outcome == "created" for result in results) == 1
    check = Database(str(path))
    assert len(check.list_post_drafts()) == 1


class _PipelinePlanner:
    def __init__(self, source_id):
        self.source_id = source_id
        self.caps = []

    def plan(self, intended_slot, daily_draft_cap=2):
        self.caps.append(daily_draft_cap)
        return ContentPlan(
            category="gym_strategy",
            source_ids=[self.source_id],
            intended_slot=intended_slot,
            include_link=False,
        )


class _PipelineGenerator:
    def generate_grounded_tweet(self, *_args, **_kwargs):
        return {"text": "Empty class capacity expires when class starts."}


class _PipelineFactGuard:
    def check(self, _text, _sources):
        return FactCheckResult(True, [])


class _PipelineScorer:
    def score_draft(self, *_args, **_kwargs):
        return {"total": 86}


def test_pipeline_creates_queue_draft_with_explicit_daily_cap_and_approves(tmp_path):
    db = Database(str(tmp_path / "queue-pipeline.db"))
    source_id = db.add_content_source("evergreen_idea", "Queue pipeline source")
    planner = _PipelinePlanner(source_id)
    pipeline = DraftPipeline(
        db,
        planner,
        _PipelineGenerator(),
        _PipelineFactGuard(),
        _PipelineScorer(),
        now_fn=lambda: datetime.now(timezone.utc),
    )
    anchor = datetime(2020, 1, 1, 10, 0, tzinfo=timezone.utc)

    draft, outcome = pipeline.create_for_queue_with_outcome(anchor)

    assert outcome == "created"
    assert planner.caps == [4]
    queued = db.get_queue_draft(draft["id"])
    assert queued["translation_status"] == "pending"
    assert db.save_review_translation(
        draft["id"], draft["revision"], "La capacità vuota scade.",
    )
    assert pipeline.approve_queue(draft["id"], "floriano")
    assert db.get_queue_draft(draft["id"])["status"] == "approved"


def test_queue_edit_replacement_starts_without_translation_or_approval(tmp_path):
    db = Database(str(tmp_path / "queue-edit.db"))
    source_id = db.add_content_source("evergreen_idea", "Queue edit source")
    planner = _PipelinePlanner(source_id)
    pipeline = DraftPipeline(
        db,
        planner,
        _PipelineGenerator(),
        _PipelineFactGuard(),
        _PipelineScorer(),
        now_fn=lambda: datetime.now(timezone.utc),
    )
    prior, _outcome = pipeline.create_for_queue_with_outcome(
        datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
    )
    assert db.save_review_translation(
        prior["id"], prior["revision"], "Traduzione precedente.",
    )

    replacement = pipeline.edit(
        prior["id"], "A materially different source-grounded operator insight.",
    )

    assert replacement is not None
    old_queue = db.get_queue_draft(prior["id"])
    new_queue = db.get_queue_draft(replacement["id"])
    assert old_queue["translation_it"] == "Traduzione precedente."
    assert old_queue["status"] == "superseded"
    assert new_queue["translation_status"] == "pending"
    assert new_queue["translation_it"] is None
    assert new_queue["approved_queue_at"] is None


def test_media_only_attach_and_detach_preserve_translation(tmp_path):
    db, _source_id, draft_id = _queue_fixture(tmp_path, key="media-preserve")
    draft = db.get_post_draft(draft_id)
    db.ensure_editorial_queue(draft_id)
    assert db.save_review_translation(
        draft_id, draft["revision"], "Traduzione da preservare.",
    )
    media_id = db.add_media("legacy.jpg", "/tmp/legacy-queue.jpg", "image")
    db.add_content_source(
        "media_context",
        "Contesto media",
        metadata={"media_id": media_id},
    )
    before = db.get_queue_draft(draft_id)

    assert db.attach_media_to_draft(media_id, draft_id)
    attached = db.get_queue_draft(draft_id)
    assert attached["revision"] == before["revision"] + 1
    assert attached["translation_it"] == before["translation_it"]
    assert attached["translation_status"] == "ready"
    assert db.detach_media_from_draft(draft_id)
    detached = db.get_queue_draft(draft_id)
    assert detached["revision"] == attached["revision"] + 1
    assert detached["translation_it"] == before["translation_it"]
    assert detached["translation_status"] == "ready"
