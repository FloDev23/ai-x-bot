"""Regression coverage for the compact, persisted Telegram post browser."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from modules.database import Database
from modules.media_processor import MediaProcessor


def _draft(db, key, text, *, status="pending_approval", origin="generated"):
    source_id = db.add_content_source("founder_note", "A grounded source.")
    with db._conn() as conn:
        ordinal = conn.execute("SELECT COUNT(*) FROM post_drafts").fetchone()[0]
    draft_id = db.create_post_draft(
        text, "founder_journey", [source_id], {"total": 91},
        (datetime(2030, 8, 15, 12, tzinfo=timezone.utc)
         + timedelta(minutes=ordinal)).isoformat(), key,
    )
    db.ensure_editorial_queue(draft_id)
    with db._conn() as conn:
        conn.execute(
            "UPDATE post_drafts SET status = ?, origin = ?, updated_at = ? WHERE id = ?",
            (status, origin, datetime.now(timezone.utc).isoformat(), draft_id),
        )
        if origin == "manual_operator":
            conn.execute(
                "UPDATE editorial_queue SET translation_policy = 'advisory' "
                "WHERE draft_id = ?", (draft_id,),
            )
    return draft_id


def test_post_index_is_keyset_ranked_and_hides_discarded_until_requested(tmp_path):
    """Catches an offset index leaking removed rows or reordering after inserts."""
    db = Database(str(tmp_path / "posts.db"))
    ids = [
        _draft(db, f"post-{number}", f"Pending {number}")
        for number in range(10)
    ]
    approved = _draft(db, "approved", "Approved")
    discarded = _draft(db, "discarded", "Discarded", status="discarded")
    with db._conn() as conn:
        conn.execute(
            "UPDATE editorial_queue SET translation_status = 'ready', translation_it = 'Italian' "
            "WHERE draft_id = ?", (ids[0],),
        )
        conn.execute(
            "UPDATE post_drafts SET status = 'approved' WHERE id = ?", (approved,),
        )

    first, next_cursor, previous = db.list_post_index_page(cursor=None, limit=8)

    assert len(first) == 8
    assert first[0]["status"] == "pending_approval"
    assert first[0]["translation_status"] == "pending"
    assert discarded not in [row["id"] for row in first]
    assert next_cursor is not None
    assert previous is None
    _draft(db, "newer", "New attention row")
    second, _next, back = db.list_post_index_page(cursor=next_cursor, limit=8)
    assert [row["id"] for row in second]
    assert back is not None
    cursor = None
    visible_ids = []
    while True:
        discarded_rows, cursor, _previous = db.list_post_index_page(
            cursor=cursor, limit=8, include_discarded=True,
        )
        visible_ids.extend(row["id"] for row in discarded_rows)
        if cursor is None:
            break
    assert discarded in visible_ids


def test_post_index_ranks_unknown_attention_before_queue_and_published(tmp_path):
    """Catches an ambiguous publication being buried below ordinary history."""
    db = Database(str(tmp_path / "attention.db"))
    published = _draft(db, "published-rank", "Published", status="published")
    approved = _draft(db, "approved-rank", "Approved", status="approved")
    unknown = _draft(db, "unknown-rank", "Unknown", status="publication_unknown")

    rows, _next, _previous = db.list_post_index_page(cursor=None, limit=8)

    positions = {row["id"]: index for index, row in enumerate(rows)}
    assert positions[unknown] < positions[approved] < positions[published]


def test_post_index_pages_more_than_twenty_mixed_queue_states(tmp_path):
    """Catches the compact rank contract dropping or leaking a mixed queue state."""
    db = Database(str(tmp_path / "mixed-index.db"))
    seeded = []
    for status, count in (
        ("pending_approval", 6),
        ("approved", 5),
        ("publishing", 3),
        ("publication_unknown", 3),
        ("published", 3),
        ("discarded", 2),
    ):
        for number in range(count):
            seeded.append((status, _draft(
                db, f"mixed-{status}-{number}", f"{status} {number}", status=status,
            )))
    planned_id = next(draft_id for status, draft_id in seeded if status == "approved")
    future = datetime.now(timezone.utc) + timedelta(days=1)
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO publication_plans "
            "(local_date, position, scheduled_for, draft_id, draft_revision, status, "
            "selection_reason_json, created_at, updated_at) "
            "VALUES (?, 1, ?, ?, 0, 'planned', ?, ?, ?)",
            (future.date().isoformat(), future.isoformat(), planned_id,
             '{"timing_bucket":"morning:9","timing_reason":"cold_start"}',
             future.isoformat(), future.isoformat()),
        )

    rows = []
    cursor = None
    while True:
        page, cursor, _previous = db.list_post_index_page(cursor=cursor, limit=8)
        assert len(page) <= 8
        rows.extend(page)
        if cursor is None:
            break

    assert len(rows) == 20
    assert [row["index_rank"] for row in rows] == sorted(
        row["index_rank"] for row in rows
    )
    assert any(row["id"] == planned_id and row["plan_status"] == "planned" for row in rows)
    discarded_ids = {draft_id for status, draft_id in seeded if status == "discarded"}
    assert discarded_ids.isdisjoint(row["id"] for row in rows)


def test_discard_and_restore_are_cas_replayable_and_keep_media_binding(tmp_path):
    """Catches removal releasing a future plan without a recoverable reservation."""
    db = Database(str(tmp_path / "discard.db"))
    draft_id = _draft(db, "manual", "Exact manual copy", origin="manual_operator")
    with db._conn() as conn:
        conn.execute("UPDATE post_drafts SET status = 'approved' WHERE id = ?", (draft_id,))
        now = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO publication_plans "
            "(local_date, position, scheduled_for, draft_id, draft_revision, "
            "status, selection_reason_json, created_at, updated_at) "
            "VALUES (?, 1, ?, ?, 0, 'planned', ?, ?, ?)",
            (
                (now + timedelta(days=1)).date().isoformat(),
                (now + timedelta(days=1)).isoformat(), draft_id,
                '{"timing_bucket":"morning:9","timing_reason":"cold_start"}',
                now.isoformat(), now.isoformat(),
            ),
        )
    queued = db.get_queue_draft(draft_id)
    removed, outcome = db.discard_queued_draft_atomic(
        draft_id, queued["revision"], queued["queue_revision"], "floriano", "remove-1",
    )
    assert outcome == "discarded"
    assert removed["status"] == "discarded"
    assert removed["blocked_reason"] == "operator_removed_from_queue"
    assert db.get_publication_plan(1)["status"] == "open"
    replay, replay_outcome = db.discard_queued_draft_atomic(
        draft_id, queued["revision"], queued["queue_revision"], "floriano", "remove-1",
    )
    assert replay_outcome == "already_applied"
    assert replay["id"] == draft_id
    restored, restore_outcome = db.restore_discarded_draft_atomic(
        draft_id, removed["revision"], removed["queue_revision"], "floriano", "restore-1",
    )
    assert restore_outcome == "restored"
    assert restored["status"] == "approved"
    assert restored["translation_policy"] == "advisory"
    original_removal, replay_outcome = db.discard_queued_draft_atomic(
        draft_id, queued["revision"], queued["queue_revision"],
        "floriano", "remove-1",
    )
    assert replay_outcome == "already_applied"
    assert original_removal["status"] == "discarded"
    restore_replay, replay_outcome = db.restore_discarded_draft_atomic(
        draft_id, removed["revision"], removed["queue_revision"],
        "floriano", "restore-1",
    )
    assert replay_outcome == "already_applied"
    assert restore_replay == restored


def test_discard_rejects_pending_and_conflicting_operation_reuse(tmp_path):
    """Catches queue removal accepting unapproved work or an aliased retry."""
    db = Database(str(tmp_path / "reject-pending.db"))
    pending_id = _draft(db, "pending-remove", "Pending")
    pending = db.get_queue_draft(pending_id)

    assert db.discard_queued_draft_atomic(
        pending_id, pending["revision"], pending["queue_revision"],
        "floriano", "remove-pending",
    ) == (None, "rejected")

    approved_id = _draft(db, "approved-remove", "Approved", status="approved")
    approved = db.get_queue_draft(approved_id)
    removed, outcome = db.discard_queued_draft_atomic(
        approved_id, approved["revision"], approved["queue_revision"],
        "floriano", "one-operation-key",
    )
    assert outcome == "discarded"
    assert removed is not None
    assert db.discard_queued_draft_atomic(
        approved_id, approved["revision"], approved["queue_revision"],
        "other-operator", "one-operation-key",
    ) == (None, "rejected")
    with db._conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM draft_evaluations "
            "WHERE outcome = 'operator_discarded'"
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    ("draft_status", "plan_status", "scheduled_delta"),
    [
        ("publishing", None, None),
        ("publication_unknown", None, None),
        ("published", None, None),
        ("approved", "planned", timedelta(seconds=-1)),
        ("approved", "publishing", timedelta(days=1)),
        ("approved", "unknown", timedelta(days=1)),
        ("approved", "simulated", timedelta(days=1)),
        ("approved", "published", timedelta(days=1)),
    ],
)
def test_discard_rejects_publisher_owned_or_non_future_work(
    tmp_path, draft_status, plan_status, scheduled_delta,
):
    """Catches removal reopening a due or publisher-owned publication slot."""
    db = Database(str(tmp_path / f"reject-{draft_status}-{plan_status}.db"))
    draft_id = _draft(db, "unsafe-remove", "Unsafe", status=draft_status)
    if plan_status is not None:
        now = datetime.now(timezone.utc)
        scheduled = now + scheduled_delta
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO publication_plans "
                "(local_date, position, scheduled_for, draft_id, draft_revision, "
                "status, selection_reason_json, created_at, updated_at) "
                "VALUES (?, 1, ?, ?, 0, ?, '{}', ?, ?)",
                (scheduled.date().isoformat(), scheduled.isoformat(), draft_id,
                 plan_status, now.isoformat(), now.isoformat()),
            )
    queued = db.get_queue_draft(draft_id)

    assert db.discard_queued_draft_atomic(
        draft_id, queued["revision"], queued["queue_revision"],
        "floriano", f"reject-{draft_status}-{plan_status}",
    ) == (None, "rejected")


def test_discard_trigger_failure_rolls_back_draft_queue_plan_media_and_audit(tmp_path):
    """Catches a late audit failure committing any earlier removal side effect."""
    db = Database(str(tmp_path / "discard-trigger.db"))
    draft_id = _draft(db, "trigger", "Approved", status="approved")
    now = datetime.now(timezone.utc)
    with db._conn() as conn:
        plan_id = conn.execute(
            "INSERT INTO publication_plans "
            "(local_date, position, scheduled_for, draft_id, draft_revision, "
            "status, selection_reason_json, created_at, updated_at) "
            "VALUES (?, 1, ?, ?, 0, 'planned', '{}', ?, ?)",
            ((now + timedelta(days=1)).date().isoformat(),
             (now + timedelta(days=1)).isoformat(), draft_id,
             now.isoformat(), now.isoformat()),
        ).lastrowid
        conn.execute("""
            CREATE TRIGGER reject_operator_discard_audit
            BEFORE INSERT ON draft_evaluations
            WHEN NEW.outcome = 'operator_discarded'
            BEGIN SELECT RAISE(ABORT, 'forced audit failure'); END
        """)
    queued = db.get_queue_draft(draft_id)

    assert db.discard_queued_draft_atomic(
        draft_id, queued["revision"], queued["queue_revision"],
        "floriano", "trigger-failure",
    ) == (None, "rejected")
    assert db.get_queue_draft(draft_id)["status"] == "approved"
    with db._conn() as conn:
        assert conn.execute(
            "SELECT status FROM publication_plans WHERE id = ?", (plan_id,),
        ).fetchone()[0] == "planned"
        assert conn.execute("SELECT COUNT(*) FROM operator_operations").fetchone()[0] == 0


def test_discard_rejects_future_plan_bound_to_a_different_draft_revision(tmp_path):
    """Catches removal releasing a position whose planner snapshot is not the CAS target."""
    db = Database(str(tmp_path / "stale-plan-binding.db"))
    draft_id = _draft(db, "stale-plan", "Approved", status="approved")
    queued = db.get_queue_draft(draft_id)
    future = datetime.now(timezone.utc) + timedelta(days=1)
    reason = '{"timing_bucket":"morning:9","timing_reason":"cold_start"}'
    with db._conn() as conn:
        plan_id = conn.execute(
            "INSERT INTO publication_plans "
            "(local_date, position, scheduled_for, draft_id, draft_revision, "
            "status, selection_reason_json, created_at, updated_at) "
            "VALUES (?, 1, ?, ?, ?, 'planned', ?, ?, ?)",
            (future.date().isoformat(), future.isoformat(), draft_id,
             queued["revision"] + 1, reason, future.isoformat(), future.isoformat()),
        ).lastrowid

    assert db.discard_queued_draft_atomic(
        draft_id, queued["revision"], queued["queue_revision"],
        "floriano", "stale-plan-binding",
    ) == (None, "rejected")
    assert db.get_queue_draft(draft_id)["status"] == "approved"
    assert db.get_publication_plan(plan_id)["status"] == "planned"


def test_two_workers_have_one_discard_winner_and_one_stale_rejection(tmp_path):
    """Catches concurrent workers both reporting a fresh queue mutation."""
    path = str(tmp_path / "discard-race.db")
    setup = Database(path)
    draft_id = _draft(setup, "race", "Approved", status="approved")
    queued = setup.get_queue_draft(draft_id)
    barrier = threading.Barrier(2)
    outcomes = []

    def remove(index):
        worker = Database(path)
        barrier.wait(timeout=5)
        outcomes.append(worker.discard_queued_draft_atomic(
            draft_id, queued["revision"], queued["queue_revision"],
            "floriano", f"race-{index}",
        )[1])

    workers = [threading.Thread(target=remove, args=(index,)) for index in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert sorted(outcomes) == ["discarded", "rejected"]
    with setup._conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM draft_evaluations "
            "WHERE outcome = 'operator_discarded'"
        ).fetchone()[0] == 1


def test_hard_crash_before_operation_receipt_rolls_back_removal(tmp_path):
    """Catches a process death between state changes and the operation receipt."""
    path = str(tmp_path / "discard-crash.db")
    db = Database(path)
    draft_id = _draft(db, "crash", "Approved", status="approved")
    queued = db.get_queue_draft(draft_id)
    script = """
import os
import sys
from modules.database import Database

class CrashBeforeReceipt(Database):
    def _record_operator_operation_in_conn(self, *args, **kwargs):
        os._exit(73)

db = CrashBeforeReceipt(sys.argv[1])
draft_id = int(sys.argv[2])
db.discard_queued_draft_atomic(
    draft_id, int(sys.argv[3]), int(sys.argv[4]), "floriano", "crash-operation",
)
"""

    crashed = subprocess.run(
        [sys.executable, "-c", script, path, str(draft_id),
         str(queued["revision"]), str(queued["queue_revision"])],
        cwd=str(Path(__file__).resolve().parents[1]), check=False,
    )

    assert crashed.returncode == 73
    stored = Database(path).get_queue_draft(draft_id)
    assert stored["status"] == "approved"
    assert stored["blocked_reason"] is None
    with db._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operator_operations").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM draft_evaluations "
            "WHERE outcome = 'operator_discarded'"
        ).fetchone()[0] == 0


def test_restore_revalidates_origin_policy_sources_and_generated_translation(tmp_path):
    """Catches restore trusting stale policy/source dependencies or prior translation."""
    db = Database(str(tmp_path / "restore-dependencies.db"))
    draft_id = _draft(db, "generated", "Generated", status="approved")
    with db._conn() as conn:
        conn.execute(
            "UPDATE editorial_queue SET translation_it = 'Old translation', "
            "translation_status = 'ready' WHERE draft_id = ?", (draft_id,),
        )
    queued = db.get_queue_draft(draft_id)
    removed, outcome = db.discard_queued_draft_atomic(
        draft_id, queued["revision"], queued["queue_revision"],
        "floriano", "remove-generated",
    )
    assert outcome == "discarded"
    restored, outcome = db.restore_discarded_draft_atomic(
        draft_id, removed["revision"], removed["queue_revision"],
        "floriano", "restore-generated",
    )
    assert outcome == "restored"
    assert restored["status"] == "pending_approval"
    assert restored["translation_policy"] == "required"
    assert restored["translation_status"] == "invalidated"
    assert restored["translation_it"] is None
    assert restored["approved_queue_at"] is None
    assert restored["approved_at"] is None

    second_id = _draft(
        db, "bad-policy", "Manual", status="approved",
        origin="manual_operator",
    )
    second = db.get_queue_draft(second_id)
    removed, outcome = db.discard_queued_draft_atomic(
        second_id, second["revision"], second["queue_revision"],
        "floriano", "remove-bad-policy",
    )
    assert outcome == "discarded"
    with db._conn() as conn:
        conn.execute(
            "UPDATE editorial_queue SET translation_policy = 'required' WHERE draft_id = ?",
            (second_id,),
        )
    assert db.restore_discarded_draft_atomic(
        second_id, removed["revision"], removed["queue_revision"],
        "floriano", "restore-bad-policy",
    ) == (None, "rejected")


def test_restore_rejects_changed_non_media_source_and_missing_media_file(tmp_path):
    """Catches restore reserving a draft whose source or original file disappeared."""
    db = Database(str(tmp_path / "restore-media.db"))
    draft_id = _draft(db, "media", "Approved")
    media_root = tmp_path / "media-root"
    media_root.mkdir(mode=0o700)
    staged = media_root / "staged.jpg"
    staged.write_bytes(b"\xff\xd8\xff\xe0task-four-media")
    media = MediaProcessor(db).process_new_file(
        str(staged), "original.jpg", "image/jpeg", staged.stat().st_size, "Original",
    )
    assert db.attach_media_to_draft(media["id"], draft_id)
    with db._conn() as conn:
        conn.execute("UPDATE post_drafts SET status = 'approved' WHERE id = ?", (draft_id,))
    queued = db.get_queue_draft(draft_id)
    removed, outcome = db.discard_queued_draft_atomic(
        draft_id, queued["revision"], queued["queue_revision"],
        "floriano", "remove-media",
    )
    assert outcome == "discarded"
    assert removed["media_id"] == media["id"]
    assert db.get_media_by_id(media["id"])["lifecycle_state"] == "available"
    (media_root / "original.jpg").unlink()

    assert db.restore_discarded_draft_atomic(
        draft_id, removed["revision"], removed["queue_revision"],
        "floriano", "restore-missing-media",
    ) == (None, "rejected")
    assert db.get_media_by_id(media["id"])["lifecycle_state"] == "available"

    text_id = _draft(db, "source", "Approved", status="approved")
    text = db.get_queue_draft(text_id)
    removed, outcome = db.discard_queued_draft_atomic(
        text_id, text["revision"], text["queue_revision"],
        "floriano", "remove-source",
    )
    assert outcome == "discarded"
    with db._conn() as conn:
        source_id = json.loads(conn.execute(
            "SELECT source_ids_json FROM post_drafts WHERE id = ?", (text_id,),
        ).fetchone()[0])[0]
        conn.execute(
            "UPDATE content_sources SET trust_state = 'rejected' WHERE id = ?", (source_id,),
        )
    assert db.restore_discarded_draft_atomic(
        text_id, removed["revision"], removed["queue_revision"],
        "floriano", "restore-stale-source",
    ) == (None, "rejected")


def test_media_backed_discard_and_restore_releases_then_reserves_same_identity(tmp_path):
    """Catches restore dropping the original media ID or reserving another object."""
    db = Database(str(tmp_path / "restore-same-media.db"))
    draft_id = _draft(db, "same-media", "Approved")
    media_root = tmp_path / "same-media-root"
    media_root.mkdir(mode=0o700)
    staged = media_root / "staged.jpg"
    content = b"\xff\xd8\xff\xe0same-media-identity"
    staged.write_bytes(content)
    media = MediaProcessor(db).process_new_file(
        str(staged), "same.jpg", "image/jpeg", len(content), "Same identity",
    )
    assert db.attach_media_to_draft(media["id"], draft_id)
    with db._conn() as conn:
        conn.execute("UPDATE post_drafts SET status = 'approved' WHERE id = ?", (draft_id,))
    queued = db.get_queue_draft(draft_id)

    removed, outcome = db.discard_queued_draft_atomic(
        draft_id, queued["revision"], queued["queue_revision"],
        "floriano", "remove-same-media",
    )
    assert outcome == "discarded"
    released = db.get_media_by_id(media["id"])
    assert removed["media_id"] == media["id"]
    assert released["lifecycle_state"] == "available"
    assert released["reserved_by_draft_id"] is None

    restored, outcome = db.restore_discarded_draft_atomic(
        draft_id, removed["revision"], removed["queue_revision"],
        "floriano", "restore-same-media",
    )
    assert outcome == "restored"
    reserved = db.get_media_by_id(media["id"])
    assert restored["media_id"] == media["id"]
    assert reserved["lifecycle_state"] == "reserved"
    assert reserved["reserved_by_draft_id"] == draft_id


def test_restore_trigger_failure_rolls_back_media_and_all_queue_state(tmp_path):
    """Catches a late restore audit failure leaving media reserved or state half-restored."""
    db = Database(str(tmp_path / "restore-trigger.db"))
    draft_id = _draft(db, "restore-trigger", "Approved", status="approved")
    queued = db.get_queue_draft(draft_id)
    removed, outcome = db.discard_queued_draft_atomic(
        draft_id, queued["revision"], queued["queue_revision"],
        "floriano", "remove-before-restore-trigger",
    )
    assert outcome == "discarded"
    with db._conn() as conn:
        conn.execute("""
            CREATE TRIGGER reject_operator_restore_audit
            BEFORE INSERT ON draft_evaluations
            WHEN NEW.outcome = 'operator_restored'
            BEGIN SELECT RAISE(ABORT, 'forced restore failure'); END
        """)

    assert db.restore_discarded_draft_atomic(
        draft_id, removed["revision"], removed["queue_revision"],
        "floriano", "restore-trigger-failure",
    ) == (None, "rejected")
    stored = db.get_queue_draft(draft_id)
    assert stored["status"] == "discarded"
    assert stored["blocked_reason"] == "operator_removed_from_queue"


def test_post_browser_renders_safe_one_line_rows_without_full_copy():
    """Catches index rendering exposing full English copy before detail selection."""
    from modules.telegram_post_browser import PostBrowser

    browser = PostBrowser()
    rendered = browser.render_index(
        [{"id": 9, "revision": 3, "status": "approved", "text": "<unsafe> " + "x" * 180,
          "category": "founder_journey", "scheduled_for": None}],
        token="ABCDEFGHIJKLMNOP", has_next=False, has_previous=False,
    )
    row = rendered["reply_markup"]["inline_keyboard"][0][0]
    assert len(row["callback_data"].encode()) <= 64
    assert "<unsafe>" not in row["text"]
    assert 70 <= len(browser.excerpt("<unsafe> " + "x" * 180)) <= 100


def test_post_browser_keeps_et_and_rome_time_in_compact_label_not_summary():
    """Catches scheduling times leaking into the summary or losing either operator zone."""
    from modules.telegram_post_browser import PostBrowser

    browser = PostBrowser()
    row = {
        "id": 9, "revision": 3, "status": "approved", "text": "Scheduled",
        "plan_status": "planned",
        "scheduled_for": "2030-01-10T14:00:00+00:00",
    }
    rendered = browser.render_index(
        [row], token="ABCDEFGHIJKLMNOP", has_next=False, has_previous=False,
    )
    label = rendered["reply_markup"]["inline_keyboard"][0][0]["text"]

    assert "ET" in label
    assert "Roma" in label
    assert "Pianificato" in label
    assert "ET" not in browser.summary([row], include_discarded=False)
    assert "Roma" not in browser.summary([row], include_discarded=False)


def test_post_browser_rejects_callback_data_over_telegram_limit():
    """Catches an oversized opaque token producing a Telegram-rejected keyboard."""
    from modules.telegram_post_browser import PostBrowser

    with pytest.raises(ValueError, match="invalid callback data"):
        PostBrowser().render_index(
            [{"id": 9, "revision": 3, "status": "approved", "text": "Safe"}],
            token="x" * 60, has_next=False, has_previous=False,
        )
