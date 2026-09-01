#!/usr/bin/env python3
"""Convert operator content sources into directly-approved post_drafts.

Reads content_sources with verified_by='floriano' and source_ids >= 34
(the 20 Italian posts added previously), creates a pending-to-approved
post_draft for each one that doesn't already have a corresponding draft.
"""
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

DB = "/home/ubuntu/ai-x-bot/bot_data.db"
NOW = datetime.now(timezone.utc)
NOW_ISO = NOW.isoformat()

SOURCE_IDS_TO_CONVERT = list(range(34, 54))  # 34..53 inclusive

CATEGORY_MAP = {
    "evergreen_idea": "gym_strategy",
    "product_fact": "product_proof",
    "founder_note": "founder_journey",
    "verified_news": "gym_strategy",
}

SCORE_JSON = json.dumps(
    {"total": 75, "authority": "operator"},
    allow_nan=False, sort_keys=True, separators=(",", ":"),
)


def main():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row

    sources = conn.execute(
        "SELECT id, source_type, text FROM content_sources WHERE id IN ({})".format(
            ", ".join("?" for _ in SOURCE_IDS_TO_CONVERT)
        ),
        SOURCE_IDS_TO_CONVERT,
    ).fetchall()

    added = 0
    skipped = 0
    for index, source in enumerate(sources):
        source_id = source["id"]
        source_type = source["source_type"]
        text = source["text"]
        category = CATEGORY_MAP.get(source_type, "gym_strategy")

        existing = conn.execute(
            "SELECT id FROM post_drafts WHERE source_ids_json = ?",
            (f"[{source_id}]",),
        ).fetchone()
        if existing:
            print(f"  skip (draft exists): source #{source_id}")
            skipped += 1
            continue

        pub_key = f"source-draft:{source_id}:{secrets.token_urlsafe(8)}"
        intended_slot = (NOW + timedelta(days=30, minutes=index)).isoformat()
        source_ids_json = f"[{source_id}]"

        cursor = conn.execute(
            """INSERT INTO post_drafts
               (publication_key, text, category, source_ids_json, score_json,
                intended_slot, status, origin, approved_at, approved_by,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'approved', 'manual_operator', ?, 'floriano', ?, ?)""",
            (
                pub_key, text, category, source_ids_json, SCORE_JSON,
                intended_slot, NOW_ISO, NOW_ISO, NOW_ISO,
            ),
        )
        draft_id = cursor.lastrowid
        conn.execute(
            """INSERT INTO editorial_queue
               (draft_id, translation_status, translation_policy, created_at, updated_at)
               VALUES (?, 'pending', 'advisory', ?, ?)""",
            (draft_id, NOW_ISO, NOW_ISO),
        )
        print(f"  added draft #{draft_id} ({category}): {text[:60].strip()!r}")
        added += 1

    conn.commit()
    conn.close()
    print(f"\nDone: {added} added, {skipped} skipped.")


if __name__ == "__main__":
    main()
