#!/usr/bin/env python3
"""Replace 12 generic November morning thoughts with FlexDropin feature posts.

Run after ``schedule_november_campaign.py``. For each swap the old approved
draft is removed through ``discard_queued_draft_atomic`` (which re-opens its
plan and releases media), a new approved draft is created and assigned to
the same slot. Idempotent: a swap already applied is skipped.
"""

from __future__ import annotations

from datetime import date
import os
from pathlib import Path
import sqlite3
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import schedule_november_campaign as base  # noqa: E402
from modules.database import Database  # noqa: E402
from modules.scoring import semantic_similarity  # noqa: E402

P = "product_proof"
G = "gym_strategy"

# (date, category, text, media key)
SWAPS = [
    ("2026-11-09", P, "FlexDropin is an app to book a single fitness class at a gym near you. No membership, no contract. You pick a class, pay in the app, show up and train. The gym gets paid directly.", None),
    ("2026-11-11", P, "After you book on FlexDropin, the class goes into your bookings with the time and the address, and you get a reminder before it starts. Less remembering, more training.", None),
    ("2026-11-13", P, "Booking a class in another time zone? FlexDropin shows class times in the gym's own time zone and lets you switch to your local time, so you don't miscount the hours.", None),
    ("2026-11-15", P, "Yoga, CrossFit, HYROX, boxing, pilates, calisthenics and dozens more. On FlexDropin you can search by the discipline you want to train, not only by the name of a gym.", None),
    ("2026-11-16", P, "Sort gyms by distance, rating or price. Filter by discipline. Search by name, or switch city before a trip. Finding a class on FlexDropin takes a few taps.", None),
    ("2026-11-18", P, "When you pick a paid class on FlexDropin, your spot is held for 15 minutes while you check out. Nobody can take it halfway through paying.", None),
    ("2026-11-20", G, "Gyms on FlexDropin can set a class once as a recurring weekly series. Add the instructor, capacity and price, and the dates are generated for you. Listing is free; 15% on paid bookings, plus Stripe fees.", "i_choose"),
    ("2026-11-22", P, "Not every class has a price tag. Gyms on FlexDropin can list free classes too: you book one and it's confirmed right away.", None),
    ("2026-11-23", P, "How gyms get paid on FlexDropin: the customer pays inside the app, the money goes to the gym's own Stripe account, and Stripe pays out to the gym's bank. FlexDropin takes 15% of each paid booking.", "i_stripe"),
    ("2026-11-25", P, "The FlexDropin widget puts your next class on your Home Screen: gym, start time and a countdown. You see it every time you unlock your phone.", "i_widget"),
    ("2026-11-27", G, "If you run a gym, FlexDropin is a free way to sell spots in classes that aren't full. You list the class, set the price, and people nearby can book it in the app.", "i_partner"),
    ("2026-11-29", G, "Gyms: save a class format as a template, assign an instructor and publish it in a few taps. That is how a regular week gets built on FlexDropin.", None),
]


def new_token(day: str) -> str:
    return f"{base.TOKEN_PREFIX}_{day.replace('-', '')}F"


def old_token(day: str) -> str:
    return f"{base.TOKEN_PREFIX}_{day.replace('-', '')}M"


def main() -> None:
    check_only = "--check" in sys.argv
    db = Database(str(base.DB_PATH))
    with sqlite3.connect(db.db_path) as conn:
        others = [
            row[0] for row in conn.execute(
                "SELECT text FROM post_drafts WHERE publication_key NOT LIKE ? "
                "AND status != 'discarded'",
                ("telegram-manual:" + base.TOKEN_PREFIX + "%M",),
            )
        ]
    problems = []
    for day, category, text, media in SWAPS:
        if len(text) > 280 or "—" in text or "–" in text:
            problems.append(f"{day}: length or dash")
        if media is not None and media not in base.MEDIA:
            problems.append(f"{day}: unknown media {media}")
        for other in others + [t for _d, _c, t, _m in SWAPS if t != text]:
            score = semantic_similarity(text, other)
            if score >= base.SIMILARITY_LIMIT:
                problems.append(f"{day} ~ '{other[:50]}': {score:.2f}")
    if problems:
        print("\n".join(problems))
        raise SystemExit(f"{len(problems)} problem(s)")
    print(f"swap copy ok: {len(SWAPS)} posts")
    if check_only:
        return

    for day, category, text, media_key in SWAPS:
        day_date = date.fromisoformat(day)
        slot_index = 0
        item = (day, "F", category, text, media_key, None)
        existing_new = base.existing_draft(db, new_token(day))
        if existing_new is not None:
            print(day, "already swapped")
            continue
        old = base.existing_draft(db, old_token(day))
        if old is None or old.get("status") != "approved":
            raise RuntimeError(f"{day}: old draft missing or not approved")
        queue = db.get_queue_draft(old["id"])
        if queue is None:
            raise RuntimeError(f"{day}: queue row missing")
        media = base.register_media(db, media_key) if media_key else None
        removed, outcome = db.discard_queued_draft_atomic(
            old["id"], old["revision"], queue["queue_revision"],
            "codex_operator", f"nov26-swap-{day}",
        )
        if removed is None:
            raise RuntimeError(f"{day}: discard {outcome}")
        plan = db.list_publication_positions(day_date)[slot_index]
        if plan["status"] != "open":
            raise RuntimeError(f"{day}: plan not reopened ({plan['status']})")
        base_item = (day, "F", category, text, media_key, None)
        # token() reads item[0] and item[1]; slot F gives the new token
        draft = base.create_draft(db, base_item, media)
        base.assign(db, plan, draft, new_token(day))
        verified = db.list_publication_positions(day_date)[slot_index]
        if verified["status"] != "planned" or verified["draft_id"] != draft["id"]:
            raise RuntimeError(f"{day}: verification failed")
        print(day, verified["scheduled_for"], draft["id"], category, media_key or "-")


if __name__ == "__main__":
    main()
