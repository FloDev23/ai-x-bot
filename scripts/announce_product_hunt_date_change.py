#!/usr/bin/env python3
"""Announce that the Product Hunt launch moved from September 30 to October 7.

The September 28 pre-launch thread was already public with the old date, so
this script creates one approved operator post and gives it the September 29
midday slot. The human thought that held that slot moves to a new October 1
midday slot. Every current value is checked before writing, and a second run
reports ``already_applied``.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.database import Database
from modules.manual_post_service import ManualPostService


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv("CAMPAIGN_DB_PATH", str(ROOT / "bot_data.db"))).resolve()

TOKEN = "ProductHuntDateUpdate_20260929"
ANNOUNCEMENT_KEY = "telegram-manual:" + TOKEN
DISPLACED_KEY = "telegram-manual:HumanMidday_20260929"
ANNOUNCEMENT_TEXT = (
    "Quick update: FlexDropin's Product Hunt launch has moved to October 7.\n\n"
    "If you were planning to support us tomorrow, thank you—we'd love to see "
    "you there on the 7th instead.\n\n"
    "Follow the launch:\n"
    "https://www.producthunt.com/products/flexdropin?launch=flexdropin"
)
ANNOUNCEMENT_CATEGORY = "founder_journey"

ANNOUNCEMENT_SLOT = "2026-09-29T12:25:00-04:00"
OCT1_DATE = "2026-10-01"
OCT1_EVENING = "2026-10-01T18:30:00-04:00"
OCT1_MIDDAY = "2026-10-01T12:50:00-04:00"
MIDDAY_REASON = json.dumps(
    {
        "approval_age": 0,
        "category_diversity": 1,
        "format_diversity": 1,
        "score": 75,
        "source_urgency": 0,
        "timing_bucket": "midday:0",
        "timing_reason": "cold_start",
    },
    sort_keys=True,
    separators=(",", ":"),
)


def draft_by_key(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM post_drafts WHERE publication_key = ?", (key,)
    ).fetchone()


def plan_at(conn: sqlite3.Connection, scheduled_for: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM publication_plans WHERE scheduled_for = ?", (scheduled_for,)
    ).fetchone()


def get_or_create_announcement(db: Database) -> None:
    with sqlite3.connect(db.db_path) as conn:
        conn.row_factory = sqlite3.Row
        existing = draft_by_key(conn, ANNOUNCEMENT_KEY)
    if existing is not None:
        if (
            existing["text"] != ANNOUNCEMENT_TEXT
            or existing["category"] != ANNOUNCEMENT_CATEGORY
            or existing["status"] not in {"approved", "published"}
        ):
            raise RuntimeError("existing announcement draft mismatch")
        return
    state_key = "campaign:product-hunt-date:" + TOKEN
    expected_state = json.dumps({"token": TOKEN}, sort_keys=True, separators=(",", ":"))
    db.set_state(state_key, expected_state)
    draft, outcome = ManualPostService(db).create_approved_from_telegram(
        text=ANNOUNCEMENT_TEXT,
        category=ANNOUNCEMENT_CATEGORY,
        source_ids=[],
        media_id=None,
        state_key=state_key,
        expected_state_value=expected_state,
        session_token=TOKEN,
        operator="codex_operator",
    )
    if draft is None or outcome not in {"created", "already_applied"}:
        raise RuntimeError(f"announcement draft creation failed ({outcome})")


def main() -> None:
    db = Database(str(DB_PATH))
    get_or_create_announcement(db)

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        announcement = draft_by_key(conn, ANNOUNCEMENT_KEY)
        displaced = draft_by_key(conn, DISPLACED_KEY)
        slot = plan_at(conn, ANNOUNCEMENT_SLOT)
        evening = plan_at(conn, OCT1_EVENING)
        midday = plan_at(conn, OCT1_MIDDAY)
        if announcement is None or displaced is None or slot is None or evening is None:
            raise RuntimeError("target rows missing")

        if (
            slot["draft_id"] == announcement["id"]
            and midday is not None
            and midday["draft_id"] == displaced["id"]
        ):
            conn.commit()
            print(json.dumps({"outcome": "already_applied"}))
            return

        if datetime.fromisoformat(ANNOUNCEMENT_SLOT) <= now:
            raise RuntimeError("announcement slot is no longer in the future")
        if (
            slot["status"] != "planned"
            or slot["draft_id"] != displaced["id"]
            or displaced["status"] != "approved"
            or announcement["status"] != "approved"
        ):
            raise RuntimeError("September 29 midday precondition failed")
        if midday is not None:
            raise RuntimeError("October 1 midday slot already exists")
        oct1_rows = conn.execute(
            "SELECT id, position FROM publication_plans WHERE local_date = ? ORDER BY position",
            (OCT1_DATE,),
        ).fetchall()
        if [row["position"] for row in oct1_rows] != [1, 2] or oct1_rows[1]["id"] != evening["id"]:
            raise RuntimeError("October 1 layout precondition failed")

        # Release the displaced draft before re-planning it: the active-draft
        # unique index allows one planned row per draft.
        cursor = conn.execute(
            """
            UPDATE publication_plans
            SET draft_id = ?, draft_revision = ?, updated_at = ?, revision = revision + 1
            WHERE id = ? AND revision = ? AND status = 'planned'
            """,
            (announcement["id"], announcement["revision"], now_iso, slot["id"], slot["revision"]),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("September 29 midday update conflict")
        cursor = conn.execute(
            """
            UPDATE publication_plans
            SET position = 3, updated_at = ?, revision = revision + 1
            WHERE id = ? AND revision = ? AND status = 'planned'
            """,
            (now_iso, evening["id"], evening["revision"]),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("October 1 evening reposition conflict")
        conn.execute(
            """
            INSERT INTO publication_plans (
                local_date, position, scheduled_for, draft_id,
                draft_revision, status, selection_reason_json,
                created_at, updated_at
            ) VALUES (?, 2, ?, ?, ?, 'planned', ?, ?, ?)
            """,
            (
                OCT1_DATE, OCT1_MIDDAY, displaced["id"], displaced["revision"],
                MIDDAY_REASON, now_iso, now_iso,
            ),
        )

        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok" or conn.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError(f"database integrity failure: {integrity}")
        conn.commit()
        print(json.dumps({
            "outcome": "updated",
            "announcement_slot": ANNOUNCEMENT_SLOT,
            "moved_human_thought_to": OCT1_MIDDAY,
            "integrity": integrity,
        }, indent=2))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
