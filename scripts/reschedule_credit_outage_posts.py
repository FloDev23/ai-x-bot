#!/usr/bin/env python3
"""Reschedule the six posts skipped on October 1-2 while X API credits were depleted.

Every October 1 and October 2 slot failed with ``402 Payment Required`` and was
marked ``skipped``; the drafts went back to ``approved``. This script gives each
draft a new midday slot on a two-post day, moving that day's evening plan to
position 3. Every current value is checked before writing, and a second run
reports ``already_applied``.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv("CAMPAIGN_DB_PATH", str(ROOT / "bot_data.db"))).resolve()

# (publication_key, local_date, new midday slot)
MOVES = [
    ("telegram-manual:HumanMidday_20260929", "2026-10-05", "2026-10-05T13:10:00-04:00"),
    ("telegram-manual:ProdJourneyP1_20261001", "2026-10-09", "2026-10-09T13:20:00-04:00"),
    ("telegram-manual:HumanMorning_20261002", "2026-10-11", "2026-10-11T12:35:00-04:00"),
    ("telegram-manual:USGymOwnerP1_20261006", "2026-10-13", "2026-10-13T13:05:00-04:00"),
    ("telegram-manual:HumanMorning_20261001", "2026-10-15", "2026-10-15T12:50:00-04:00"),
    ("telegram-manual:HumanMidday_20261002", "2026-10-17", "2026-10-17T13:15:00-04:00"),
]
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


def main() -> None:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        results = []
        for key, local_date, slot in MOVES:
            draft = draft_by_key(conn, key)
            if draft is None:
                raise RuntimeError(f"draft missing: {key}")
            midday = plan_at(conn, slot)
            if midday is not None:
                if midday["draft_id"] != draft["id"]:
                    raise RuntimeError(f"{slot} already holds another draft")
                results.append({"draft_id": draft["id"], "slot": slot, "outcome": "already_applied"})
                continue

            if datetime.fromisoformat(slot) <= now:
                raise RuntimeError(f"{slot} is no longer in the future")
            if draft["status"] != "approved":
                raise RuntimeError(f"draft {draft['id']} is {draft['status']}, not approved")
            active = conn.execute(
                "SELECT id FROM publication_plans WHERE draft_id = ? "
                "AND status IN ('planned', 'publishing', 'unknown')",
                (draft["id"],),
            ).fetchone()
            if active is not None:
                raise RuntimeError(f"draft {draft['id']} already has active plan {active['id']}")
            day_rows = conn.execute(
                "SELECT * FROM publication_plans WHERE local_date = ? ORDER BY position",
                (local_date,),
            ).fetchall()
            if [row["position"] for row in day_rows] != [1, 2]:
                raise RuntimeError(f"{local_date} layout precondition failed")
            evening = day_rows[1]
            if evening["status"] != "planned" or evening["scheduled_for"] <= slot:
                raise RuntimeError(f"{local_date} evening precondition failed")

            cursor = conn.execute(
                """
                UPDATE publication_plans
                SET position = 3, updated_at = ?, revision = revision + 1
                WHERE id = ? AND revision = ? AND status = 'planned'
                """,
                (now_iso, evening["id"], evening["revision"]),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"{local_date} evening reposition conflict")
            conn.execute(
                """
                INSERT INTO publication_plans (
                    local_date, position, scheduled_for, draft_id,
                    draft_revision, status, selection_reason_json,
                    created_at, updated_at
                ) VALUES (?, 2, ?, ?, ?, 'planned', ?, ?, ?)
                """,
                (
                    local_date, slot, draft["id"], draft["revision"],
                    MIDDAY_REASON, now_iso, now_iso,
                ),
            )
            results.append({"draft_id": draft["id"], "slot": slot, "outcome": "planned"})

        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok" or conn.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError(f"database integrity failure: {integrity}")
        conn.commit()
        print(json.dumps({"results": results, "integrity": integrity}, indent=2))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
