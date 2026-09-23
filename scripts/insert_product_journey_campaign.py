#!/usr/bin/env python3
"""Insert the approved October 2026 FlexDropin product-journey campaign."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from modules.adaptive_timing import DailyTimingDecision
from modules.database import Database
from modules.manual_post_service import ManualPostService
from modules.media_processor import MediaProcessor


ROOT = Path(__file__).resolve().parents[1]
MEDIA_ROOT = Path(
    os.getenv("CAMPAIGN_MEDIA_ROOT", str(ROOT / "media_library"))
).resolve()
DB_PATH = Path(os.getenv("CAMPAIGN_DB_PATH", str(ROOT / "bot_data.db"))).resolve()
ZONE = ZoneInfo("America/New_York")


THREAD = [
    "Fitness should fit your schedule—not the other way around. Here’s how "
    "FlexDropin takes you from “I want to train” to walking through the gym "
    "door. 🧵",
    "Start nearby. Search by location, sort by distance, filter by discipline, "
    "compare prices and see how many classes are available today.",
    "Open a gym and make an informed choice. Check disciplines, distance, "
    "reviews, weekly class volume, address and booking requirements before "
    "committing.",
    "Choose the class that actually works. Browse upcoming dates, times, "
    "coaches, availability and per-class prices in one place.",
    "Review the details before paying: class duration, gym timezone, local "
    "time, spots left, description and cancellation terms.",
    "Book with Apple Pay or card, then manage today’s and upcoming classes "
    "with a countdown, directions and calendar access.",
    "And the widget keeps your next workout visible from the Home Screen. "
    "Less admin between you and training.\n\nFlexDropin launched on Product "
    "Hunt on September 30.",
]


ITEMS = [
    {
        "key": "p1",
        "token": "ProdJourneyP1_20261001",
        "date": date(2026, 10, 1),
        "position": 1,
        "category": "product_proof",
        "text": "Your next workout shouldn’t start with ten browser tabs. "
        "FlexDropin lets you search nearby gyms, compare distance, ratings, "
        "today’s classes and starting prices in one place—then choose what "
        "fits today.",
        "stage": "stage-p1.png",
        "filename": "campaign-product-journey-01-explore.png",
        "context": "Approved campaign creative P1: FlexDropin Explore screen.",
    },
    {
        "key": "p2",
        "token": "ProdJourneyP2_20261001",
        "date": date(2026, 10, 1),
        "position": 2,
        "category": "product_proof",
        "text": "Before you book, you should know what you’re walking into. "
        "See disciplines, ratings, distance, weekly class volume, location "
        "and booking requirements on one gym page. Less guesswork. Better "
        "drop-ins.",
        "stage": "stage-p2.png",
        "filename": "campaign-product-journey-02-gym-detail.png",
        "context": "Approved campaign creative P2: FlexDropin gym detail screen.",
    },
    {
        "key": "p3",
        "token": "ProdJourneyP3_20261002",
        "date": date(2026, 10, 2),
        "position": 1,
        "category": "product_proof",
        "text": "A good workout can still be the wrong workout if the time, "
        "price or availability doesn’t fit. FlexDropin puts upcoming dates, "
        "class types, remaining spots and per-class pricing in one view.",
        "stage": "stage-p3.png",
        "filename": "campaign-product-journey-03-class-list.png",
        "context": "Approved campaign creative P3: FlexDropin class list screen.",
    },
    {
        "key": "p4",
        "token": "ProdJourneyP4_20261002",
        "date": date(2026, 10, 2),
        "position": 2,
        "category": "product_proof",
        "text": "Small details decide whether a drop-in works: gym timezone, "
        "local time, class length, spots left, price and cancellation terms. "
        "FlexDropin shows them before you pay.",
        "stage": "stage-p4.png",
        "filename": "campaign-product-journey-04-class-detail.png",
        "context": "Approved campaign creative P4: FlexDropin class detail screen.",
    },
    {
        "key": "p5",
        "token": "ProdJourneyP5_20261003",
        "date": date(2026, 10, 3),
        "position": 1,
        "category": "product_proof",
        "text": "Found the right class? Book it without leaving the flow. "
        "FlexDropin supports Apple Pay and card payments, so the path from "
        "class detail to confirmed booking stays simple.",
        "stage": "stage-p5.png",
        "filename": "campaign-product-journey-05-payment.png",
        "context": "Approved campaign creative P5: FlexDropin payment screen.",
    },
    {
        "key": "p6",
        "token": "ProdJourneyP6_20261003",
        "date": date(2026, 10, 3),
        "position": 2,
        "category": "product_proof",
        "text": "A booking is only useful if you can act on it. FlexDropin "
        "keeps today’s and upcoming classes together—with a countdown, "
        "directions and calendar access when it matters.",
        "stage": "stage-p6.png",
        "filename": "campaign-product-journey-06-bookings.png",
        "context": "Approved campaign creative P6: FlexDropin bookings screen.",
    },
    {
        "key": "p7",
        "token": "ProdJourneyP7_20261004",
        "date": date(2026, 10, 4),
        "position": 1,
        "category": "product_proof",
        "text": "The best reminder is the one you don’t have to look for. The "
        "FlexDropin widget keeps your next gym, class, start time and countdown "
        "visible from your Home Screen.",
        "stage": "stage-p7.png",
        "filename": "campaign-product-journey-07-widget.png",
        "context": "Approved campaign creative P7: FlexDropin iOS widget.",
    },
    {
        "key": "p8",
        "token": "ProdJourneyP8_20261004",
        "date": date(2026, 10, 4),
        "position": 2,
        "category": "shareable_fitness",
        "text": "From “I want to train” to “I’m booked”:\n\n1. Explore nearby "
        "gyms\n2. Compare the details\n3. Pick a class\n4. Review and book\n\n"
        "One flow, built for people who want flexibility without the usual "
        "friction.",
        "stage": "stage-p8.png",
        "filename": "campaign-product-journey-08-discover-compare-book-v2.png",
        "context": "Approved corrected campaign creative P8: four-step journey.",
    },
    {
        "key": "p9",
        "token": "ProdJourneyP9_20261005",
        "date": date(2026, 10, 5),
        "position": 1,
        "category": "product_proof",
        "text": "Booking shouldn’t disappear into an inbox.\n\nPay, manage the "
        "class, get directions, add it to your calendar and keep the next "
        "workout visible on your Home Screen.\n\nFlexDropin is designed around "
        "the full drop-in journey—not just checkout.",
        "stage": "stage-p9.png",
        "filename": "campaign-product-journey-09-book-track-show-up.png",
        "context": "Approved campaign creative P9: booking-to-workout journey.",
    },
    {
        "key": "t1",
        "token": "ProdJourneyT1_20261005",
        "date": date(2026, 10, 5),
        "position": 2,
        "category": "product_proof",
        "text": THREAD[0],
        "thread": THREAD,
        "stage": "stage-t1.png",
        "filename": "campaign-product-journey-10-thread-root-v2.png",
        "context": "Approved campaign thread root: corrected four-step journey.",
    },
]


def existing_draft_id(db: Database, token: str) -> int | None:
    publication_key = "telegram-manual:" + token
    with sqlite3.connect(db.db_path) as conn:
        row = conn.execute(
            "SELECT id FROM post_drafts WHERE publication_key = ?",
            (publication_key,),
        ).fetchone()
    return int(row[0]) if row else None


def get_or_register_media(db: Database, item: dict) -> dict:
    existing = [
        row for row in db.get_all_media(limit=1000)
        if row.get("user_context") == item["context"]
    ]
    if len(existing) > 1:
        raise RuntimeError(f"duplicate media context for {item['key']}")
    if existing:
        row = existing[0]
        if row.get("file_deleted") or row.get("lifecycle_state") == "archived":
            raise RuntimeError(f"existing media unavailable for {item['key']}")
        return row

    staged = MEDIA_ROOT / item["stage"]
    if not staged.is_file():
        raise RuntimeError(f"missing staged media: {staged}")
    return MediaProcessor(db).process_new_file(
        str(staged),
        item["filename"],
        "image/png",
        staged.stat().st_size,
        item["context"],
    )


def get_or_create_draft(db: Database, item: dict, media: dict) -> dict:
    draft_id = existing_draft_id(db, item["token"])
    if draft_id is not None:
        draft = db.get_post_draft(draft_id)
        expected_thread = item.get("thread")
        if (
            draft is None
            or draft.get("text") != item["text"]
            or draft.get("category") != item["category"]
            or draft.get("media_id") != media["id"]
            or draft.get("status") != "approved"
            or draft.get("thread_tweets") != expected_thread
        ):
            raise RuntimeError(f"existing draft mismatch for {item['key']}")
        return draft

    state_key = "campaign:product-journey:" + item["key"]
    expected_state = json.dumps(
        {"token": item["token"]}, sort_keys=True, separators=(",", ":")
    )
    db.set_state(state_key, expected_state)
    draft, outcome = ManualPostService(db).create_approved_from_telegram(
        text=item["text"],
        category=item["category"],
        source_ids=[],
        media_id=media["id"],
        state_key=state_key,
        expected_state_value=expected_state,
        session_token=item["token"],
        operator="codex_operator",
        thread_tweets=item.get("thread"),
    )
    if draft is None or outcome not in {"created", "already_applied"}:
        raise RuntimeError(f"draft creation failed for {item['key']}: {outcome}")
    return draft


def create_day_plans(db: Database, local_date: date) -> list[dict]:
    times = (
        datetime(local_date.year, local_date.month, local_date.day, 9, 30, tzinfo=ZONE),
        datetime(local_date.year, local_date.month, local_date.day, 18, 30, tzinfo=ZONE),
    )
    plans = db.create_or_get_publication_positions(
        local_date,
        DailyTimingDecision(
            times=times,
            bucket_ids=("morning:0", "evening:0"),
            reason="cold_start",
        ),
        datetime.now(timezone.utc),
    )
    if len(plans) != 2:
        raise RuntimeError(f"unable to create two plans for {local_date}")
    expected = [value.isoformat() for value in times]
    if [plan["scheduled_for"] for plan in plans] != expected:
        raise RuntimeError(f"schedule mismatch for {local_date}")
    return plans


def assign_plan(db: Database, plan: dict, draft: dict, key: str) -> None:
    if plan["status"] == "planned" and plan.get("draft_id") == draft["id"]:
        return
    if plan["status"] != "open" or plan.get("draft_id") is not None:
        raise RuntimeError(f"publication slot conflict for {key}")
    assigned = db.assign_publication_plan_atomic(
        plan["id"],
        draft["id"],
        draft["revision"],
        {
            "score": 75,
            "source_urgency": 0,
            "category_diversity": 1,
            "format_diversity": 1,
            "approval_age": 0,
        },
        max_links_per_week=3,
    )
    if not assigned:
        raise RuntimeError(f"publication assignment failed for {key}")


def main() -> None:
    if any(len(item["text"]) > 280 for item in ITEMS):
        raise RuntimeError("post exceeds X character limit")
    if any(len(tweet) > 280 for tweet in THREAD):
        raise RuntimeError("thread tweet exceeds X character limit")

    db = Database(str(DB_PATH))
    drafts: dict[str, dict] = {}
    media_rows: dict[str, dict] = {}
    for item in ITEMS:
        media_rows[item["key"]] = get_or_register_media(db, item)
        drafts[item["key"]] = get_or_create_draft(
            db, item, media_rows[item["key"]]
        )

    plans_by_date = {
        day: create_day_plans(db, day)
        for day in sorted({item["date"] for item in ITEMS})
    }
    for item in ITEMS:
        plan = plans_by_date[item["date"]][item["position"] - 1]
        assign_plan(db, plan, drafts[item["key"]], item["key"])

    result = []
    for item in ITEMS:
        plans = db.list_publication_positions(item["date"])
        plan = next(row for row in plans if row["position"] == item["position"])
        draft = db.get_post_draft(drafts[item["key"]]["id"])
        media = db.get_media_by_id(media_rows[item["key"]]["id"])
        if (
            plan["status"] != "planned"
            or plan.get("draft_id") != draft["id"]
            or draft.get("status") != "approved"
            or media.get("lifecycle_state") != "reserved"
            or media.get("reserved_by_draft_id") != draft["id"]
        ):
            raise RuntimeError(f"post-insert verification failed for {item['key']}")
        result.append(
            {
                "key": item["key"],
                "draft_id": draft["id"],
                "media_id": media["id"],
                "scheduled_for": plan["scheduled_for"],
                "plan_status": plan["status"],
                "thread_tweets": len(item.get("thread", [])),
            }
        )

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
