#!/usr/bin/env python3
"""Move the FlexDropin Product Hunt launch messaging to October 7, 2026.

The script only mutates four future plan bindings and four exact approved
drafts. Every current value is checked before the transaction starts, so it
fails closed if production has changed since the audit.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv("CAMPAIGN_DB_PATH", str(ROOT / "bot_data.db"))).resolve()

P9 = "operator-campaign:producthunt-2026-09:P9"
P10 = "operator-campaign:producthunt-2026-09:P10"
PRODUCT_CLASS_LIST = "telegram-manual:ProdJourneyP3_20261002"
RESEARCH_LOCATIONS = "telegram-manual:HumanEditorial_20261007"
SEP30_MORNING = "telegram-manual:HumanMorning_20260930"
OCT7_MORNING = "telegram-manual:HumanMorning_20261007"
SEP30_MIDDAY = "telegram-manual:HumanMidday_20260930"
PRODUCT_THREAD = "telegram-manual:ProdJourneyT1_20261005"


TEXT_UPDATES = {
    SEP30_MORNING: (
        "Today is one of those days when excitement and nerves feel almost "
        "identical. Putting something you care about in front of people is "
        "its own kind of workout.",
        "I used to treat 20 minutes as not enough. Now I see it as 20 "
        "minutes more than doing nothing.",
    ),
    OCT7_MORNING: (
        "I used to treat 20 minutes as not enough. Now I see it as 20 "
        "minutes more than doing nothing.",
        "Today is one of those days when excitement and nerves feel almost "
        "identical. Putting something you care about in front of people is "
        "its own kind of workout.",
    ),
    SEP30_MIDDAY: (
        "Launching something makes every small detail feel enormous. Then it "
        "goes live, and the only useful thing left is listening.",
        "Some days, putting one small workout on the calendar is enough to "
        "make the whole week feel less chaotic.",
    ),
}

THREAD_OLD_FINAL = (
    "And the widget keeps your next workout visible from the Home Screen. "
    "Less admin between you and training.\n\n"
    "FlexDropin launched on Product Hunt on September 30."
)
THREAD_NEW_FINAL = (
    "And the widget keeps your next workout visible from the Home Screen. "
    "Less admin between you and training.\n\n"
    "FlexDropin launched on Product Hunt on October 7."
)

EXPECTED_CURRENT_DATES = {
    P9: "2026-09-29T18:30:00-04:00",
    PRODUCT_CLASS_LIST: "2026-10-06T18:30:00-04:00",
    P10: "2026-09-30T18:30:00-04:00",
    RESEARCH_LOCATIONS: "2026-10-07T18:30:00-04:00",
}
EXPECTED_UPDATED_DATES = {
    P9: "2026-10-06T18:30:00-04:00",
    PRODUCT_CLASS_LIST: "2026-09-29T18:30:00-04:00",
    P10: "2026-10-07T18:30:00-04:00",
    RESEARCH_LOCATIONS: "2026-09-30T18:30:00-04:00",
}


def rows_by_key(conn: sqlite3.Connection, keys: tuple[str, ...]) -> dict[str, sqlite3.Row]:
    placeholders = ", ".join("?" for _ in keys)
    rows = conn.execute(
        f"SELECT * FROM post_drafts WHERE publication_key IN ({placeholders})",
        keys,
    ).fetchall()
    result = {row["publication_key"]: row for row in rows}
    if set(result) != set(keys):
        raise RuntimeError("draft target set mismatch")
    return result


def plans_by_key(conn: sqlite3.Connection, keys: tuple[str, ...]) -> dict[str, sqlite3.Row]:
    placeholders = ", ".join("?" for _ in keys)
    rows = conn.execute(
        f"""
        SELECT p.*, d.publication_key
        FROM publication_plans p
        JOIN post_drafts d ON d.id = p.draft_id
        WHERE d.publication_key IN ({placeholders})
        """,
        keys,
    ).fetchall()
    result = {row["publication_key"]: row for row in rows}
    if set(result) != set(keys):
        raise RuntimeError("publication-plan target set mismatch")
    return result


def is_future_plan(row: sqlite3.Row, now: datetime) -> bool:
    try:
        scheduled = datetime.fromisoformat(row["scheduled_for"])
    except (TypeError, ValueError):
        return False
    return row["status"] == "planned" and scheduled > now


def fully_applied(drafts: dict[str, sqlite3.Row], plans: dict[str, sqlite3.Row]) -> bool:
    if any(
        plans[key]["scheduled_for"] != expected
        for key, expected in EXPECTED_UPDATED_DATES.items()
    ):
        return False
    if any(drafts[key]["text"] != expected for key, (_, expected) in TEXT_UPDATES.items()):
        return False
    try:
        thread_tweets = json.loads(drafts[PRODUCT_THREAD]["thread_tweets_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(thread_tweets, list) and thread_tweets[-1] == THREAD_NEW_FINAL


def update_draft_text(
    conn: sqlite3.Connection,
    draft: sqlite3.Row,
    expected_text: str,
    new_text: str,
    now_iso: str,
) -> int:
    if draft["status"] != "approved" or draft["text"] != expected_text:
        raise RuntimeError(f"draft text precondition failed: {draft['publication_key']}")
    cursor = conn.execute(
        """
        UPDATE post_drafts
        SET text = ?, updated_at = ?, revision = revision + 1
        WHERE id = ? AND revision = ? AND status = 'approved' AND text = ?
        """,
        (new_text, now_iso, draft["id"], draft["revision"], expected_text),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f"draft update conflict: {draft['publication_key']}")
    return draft["revision"] + 1


def update_thread_final(
    conn: sqlite3.Connection, draft: sqlite3.Row, now_iso: str
) -> int:
    if draft["status"] != "approved":
        raise RuntimeError("product thread is not approved")
    try:
        tweets = json.loads(draft["thread_tweets_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("product thread payload is invalid") from error
    if not isinstance(tweets, list) or tweets[-1] != THREAD_OLD_FINAL:
        raise RuntimeError("product thread final tweet precondition failed")
    tweets[-1] = THREAD_NEW_FINAL
    cursor = conn.execute(
        """
        UPDATE post_drafts
        SET thread_tweets_json = ?, updated_at = ?, revision = revision + 1
        WHERE id = ? AND revision = ? AND status = 'approved'
        """,
        (
            json.dumps(tweets, ensure_ascii=False, separators=(",", ":")),
            now_iso,
            draft["id"],
            draft["revision"],
        ),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("product thread update conflict")
    return draft["revision"] + 1


def refresh_plan_revision(
    conn: sqlite3.Connection, draft_id: int, old_revision: int, new_revision: int,
    now_iso: str,
) -> None:
    cursor = conn.execute(
        """
        UPDATE publication_plans
        SET draft_revision = ?, updated_at = ?, revision = revision + 1
        WHERE draft_id = ? AND draft_revision = ? AND status = 'planned'
        """,
        (new_revision, now_iso, draft_id, old_revision),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f"plan revision refresh failed for draft {draft_id}")


def swap_plan_drafts(
    conn: sqlite3.Connection, left: sqlite3.Row, right: sqlite3.Row, now_iso: str
) -> None:
    for plan in (left, right):
        if plan["status"] != "planned":
            raise RuntimeError("publication plan is not planned")
    # uq_publication_plans_active_draft forbids two planned rows sharing a
    # draft, so release the left draft first; the transaction hides the gap.
    cursor = conn.execute(
        """
        UPDATE publication_plans
        SET draft_id = NULL, draft_revision = NULL
        WHERE id = ? AND revision = ? AND status = 'planned'
        """,
        (left["id"], left["revision"]),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("plan swap release conflict")
    cursor = conn.execute(
        """
        UPDATE publication_plans
        SET draft_id = ?, draft_revision = ?, updated_at = ?, revision = revision + 1
        WHERE id = ? AND revision = ? AND status = 'planned'
        """,
        (left["draft_id"], left["draft_revision"], now_iso, right["id"], right["revision"]),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("second plan swap conflict")
    cursor = conn.execute(
        """
        UPDATE publication_plans
        SET draft_id = ?, draft_revision = ?, updated_at = ?, revision = revision + 1
        WHERE id = ? AND revision = ? AND status = 'planned'
        """,
        (right["draft_id"], right["draft_revision"], now_iso, left["id"], left["revision"]),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("first plan swap conflict")


def main() -> None:
    text_keys = tuple(TEXT_UPDATES)
    plan_keys = tuple(EXPECTED_CURRENT_DATES)
    all_keys = text_keys + (PRODUCT_THREAD,) + plan_keys
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        drafts = rows_by_key(conn, all_keys)
        plans = plans_by_key(conn, tuple(dict.fromkeys(text_keys + (PRODUCT_THREAD,) + plan_keys)))
        if fully_applied(drafts, plans):
            conn.commit()
            print(json.dumps({"outcome": "already_applied"}))
            return
        if any(not is_future_plan(plan, now) for plan in plans.values()):
            raise RuntimeError("one or more target plans are no longer future planned posts")
        if any(
            plans[key]["scheduled_for"] != expected
            for key, expected in EXPECTED_CURRENT_DATES.items()
        ):
            raise RuntimeError("publication-plan schedule precondition failed")

        swap_plan_drafts(conn, plans[P9], plans[PRODUCT_CLASS_LIST], now_iso)
        swap_plan_drafts(conn, plans[P10], plans[RESEARCH_LOCATIONS], now_iso)

        for key, (old_text, new_text) in TEXT_UPDATES.items():
            old_revision = drafts[key]["revision"]
            new_revision = update_draft_text(
                conn, drafts[key], old_text, new_text, now_iso
            )
            refresh_plan_revision(
                conn, drafts[key]["id"], old_revision, new_revision, now_iso
            )

        old_revision = drafts[PRODUCT_THREAD]["revision"]
        new_revision = update_thread_final(conn, drafts[PRODUCT_THREAD], now_iso)
        refresh_plan_revision(
            conn, drafts[PRODUCT_THREAD]["id"], old_revision, new_revision, now_iso
        )

        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"database integrity failure: {integrity}")
        conn.commit()
        print(json.dumps({
            "outcome": "updated",
            "launch_date": "2026-10-07",
            "swapped_plan_pairs": 2,
            "updated_drafts": 4,
            "integrity": integrity,
        }, indent=2))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
