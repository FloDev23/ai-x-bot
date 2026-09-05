import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from modules.database import Database


def _create_manual_approved_draft(db: Database, *, token: str) -> int:
    state_key = f"telegram_session:{token}"
    state = json.dumps({"token": token, "v": 1}, sort_keys=True, separators=(",", ":"))
    db.set_state(state_key, state)
    draft, outcome = db.create_manual_approved_draft_consuming_state_atomic(
        text="Production-shaped approved operator post.",
        category="gym_strategy",
        source_ids=[],
        media_id=None,
        intended_slot="2020-03-01T14:00:00+00:00",
        state_key=state_key,
        expected_state_value=state,
        session_token=token,
        operator="floriano",
        now=datetime(2020, 3, 1, 13, 0, tzinfo=timezone.utc),
    )
    assert outcome == "created"
    return draft["id"]


def _create_required_approved_draft(
    db: Database,
    path,
    *,
    publication_key: str,
    updated_at: str,
    translation_it,
) -> int:
    source_id = db.add_content_source(
        "product_fact",
        "FlexDropin supports flexible fitness bookings.",
        verified_by="floriano",
    )
    draft_id = db.create_post_draft(
        "Approved generated post.",
        "product_proof",
        [source_id],
        {"total": 80},
        "2020-03-02T14:00:00+00:00",
        publication_key,
    )
    db.ensure_editorial_queue(draft_id)
    aware = "2020-03-02T13:00:00+00:00"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE post_drafts SET status = 'approved', approved_at = ?, "
            "approved_by = 'floriano', updated_at = ? WHERE id = ?",
            (aware, updated_at, draft_id),
        )
        conn.execute(
            "UPDATE editorial_queue SET translation_it = ?, "
            "translation_status = 'ready', review_ready_at = ?, "
            "approved_queue_at = ?, translation_policy = 'required' "
            "WHERE draft_id = ?",
            (translation_it, aware, aware, draft_id),
        )
    return draft_id


def test_startup_repairs_exact_legacy_queue_timestamps_and_advisory_state(tmp_path):
    path = tmp_path / "production-shaped.db"
    db = Database(str(path))
    draft_id = _create_manual_approved_draft(
        db,
        token="productionLegacy01",
    )
    legacy = "2020-03-01 13:00:00"

    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE post_drafts SET created_at = ?, updated_at = ?, approved_at = ? "
            "WHERE id = ?",
            (legacy, legacy, legacy, draft_id),
        )
        conn.execute(
            "UPDATE editorial_queue SET translation_it = NULL, "
            "translation_status = 'ready', review_ready_at = ?, "
            "approved_queue_at = ?, not_before = ?, created_at = ?, updated_at = ? "
            "WHERE draft_id = ?",
            (legacy, legacy, legacy, legacy, legacy, draft_id),
        )

    repaired = Database(str(path))
    queue_draft = repaired.get_queue_draft(draft_id)

    assert queue_draft is not None
    assert queue_draft["translation_policy"] == "advisory"
    assert queue_draft["translation_status"] == "pending"
    assert queue_draft["translation_it"] is None
    assert queue_draft["review_ready_at"] is None
    assert queue_draft["approved_queue_at"] == "2020-03-01T13:00:00+00:00"
    assert queue_draft["not_before"] == "2020-03-01T13:00:00+00:00"
    assert queue_draft["approved_at"] == "2020-03-01T13:00:00+00:00"
    assert queue_draft["created_at"] == "2020-03-01T13:00:00+00:00"
    assert queue_draft["queue_created_at"] == "2020-03-01T13:00:00+00:00"
    assert Database._strict_aware_datetime(queue_draft["updated_at"]) is not None
    assert Database._strict_aware_datetime(queue_draft["queue_updated_at"]) is not None
    assert [row["id"] for row in repaired.list_approved_queue(
        datetime(2020, 3, 2, tzinfo=timezone.utc)
    )] == [draft_id]

    with sqlite3.connect(path) as conn:
        first_repair = (
            conn.execute(
                "SELECT created_at, updated_at, approved_at FROM post_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone(),
            conn.execute(
                "SELECT translation_it, translation_status, review_ready_at, "
                "approved_queue_at, not_before, revision, created_at, updated_at "
                "FROM editorial_queue WHERE draft_id = ?",
                (draft_id,),
            ).fetchone(),
        )
    Database(str(path))
    with sqlite3.connect(path) as conn:
        second_repair = (
            conn.execute(
                "SELECT created_at, updated_at, approved_at FROM post_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone(),
            conn.execute(
                "SELECT translation_it, translation_status, review_ready_at, "
                "approved_queue_at, not_before, revision, created_at, updated_at "
                "FROM editorial_queue WHERE draft_id = ?",
                (draft_id,),
            ).fetchone(),
        )
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert second_repair == first_repair


@pytest.mark.parametrize("malformed_column", ["updated_at", "review_ready_at"])
def test_startup_does_not_hide_malformed_advisory_queue_timestamps(
    tmp_path,
    malformed_column,
):
    path = tmp_path / f"malformed-advisory-{malformed_column}.db"
    db = Database(str(path))
    draft_id = _create_manual_approved_draft(
        db,
        token=f"malformed{malformed_column.replace('_', '')}01",
    )
    aware = "2020-03-01T13:00:00+00:00"
    queue_updated_at = (
        "malformed-updated-at" if malformed_column == "updated_at" else aware
    )
    review_ready_at = "malformed-review-ready-at" if (
        malformed_column == "review_ready_at"
    ) else aware
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE editorial_queue SET translation_it = NULL, "
            "translation_status = 'ready', review_ready_at = ?, updated_at = ? "
            "WHERE draft_id = ?",
            (review_ready_at, queue_updated_at, draft_id),
        )

    reopened = Database(str(path))

    assert reopened.get_queue_draft(draft_id) is None
    assert reopened.list_approved_queue(datetime(2020, 3, 2, tzinfo=timezone.utc)) == []
    with sqlite3.connect(path) as conn:
        queue = conn.execute(
            "SELECT translation_status, review_ready_at, updated_at "
            "FROM editorial_queue WHERE draft_id = ?",
            (draft_id,),
        ).fetchone()
    assert queue == ("ready", review_ready_at, queue_updated_at)


def test_startup_does_not_repair_required_translation_without_text(tmp_path):
    path = tmp_path / "required-missing-translation.db"
    db = Database(str(path))
    draft_id = _create_required_approved_draft(
        db,
        path,
        publication_key="required-missing-translation",
        updated_at="2020-03-02 13:00:00",
        translation_it=None,
    )

    reopened = Database(str(path))

    assert reopened.get_queue_draft(draft_id) is None
    assert reopened.list_approved_queue(datetime(2020, 3, 3, tzinfo=timezone.utc)) == []
    with sqlite3.connect(path) as conn:
        queue = conn.execute(
            "SELECT translation_status, translation_it, review_ready_at "
            "FROM editorial_queue WHERE draft_id = ?",
            (draft_id,),
        ).fetchone()
        draft_updated_at = conn.execute(
            "SELECT updated_at FROM post_drafts WHERE id = ?",
            (draft_id,),
        ).fetchone()[0]
    assert queue == ("ready", None, "2020-03-02T13:00:00+00:00")
    assert draft_updated_at == "2020-03-02T13:00:00+00:00"


@pytest.mark.parametrize("invalid_timestamp", [
    "not-a-timestamp",
    "2020-03-02T13:00:00",
])
def test_startup_leaves_nonlegacy_invalid_timestamps_fail_closed(
    tmp_path,
    invalid_timestamp,
):
    path = tmp_path / "invalid-timestamp.db"
    db = Database(str(path))
    draft_id = _create_required_approved_draft(
        db,
        path,
        publication_key=f"invalid-{invalid_timestamp}",
        updated_at=invalid_timestamp,
        translation_it="Traduzione pronta.",
    )

    reopened = Database(str(path))

    assert reopened.get_queue_draft(draft_id) is None
    assert reopened.list_approved_queue(datetime(2020, 3, 3, tzinfo=timezone.utc)) == []
    with sqlite3.connect(path) as conn:
        stored = conn.execute(
            "SELECT updated_at FROM post_drafts WHERE id = ?",
            (draft_id,),
        ).fetchone()[0]
    assert stored == invalid_timestamp
