#!/usr/bin/env python3
"""Insert the approved October 2026 FlexDropin US gym-owner campaign."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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


THREAD_CONTROLLED_TEST = [
    "Drop-ins can create revenue from spare capacity—but only if you control "
    "the inventory.\n\nHere's a practical 6-step test for gym owners. 🧵",
    "Start with low-risk classes.\n\nChoose sessions that regularly have spare "
    "capacity. Don't release your highest-demand member slots just to say you "
    "offer drop-ins.",
    "Cap the inventory.\n\nOpen a small, fixed number of spots per class. You "
    "decide the capacity; the drop-in channel should work around your member "
    "experience.",
    "Protect the price.\n\nA drop-in is convenience and flexibility—not "
    "automatically a deep discount. Set a price that respects coaching, "
    "equipment, location and demand.",
    "Make the rules obvious.\n\nPublish arrival time, cancellation terms, "
    "equipment requirements and anything users need before class. Clear "
    "expectations reduce front-desk friction.",
    "Measure what matters.\n\nTrack bookings, occupancy, net revenue and repeat "
    "visitors. A channel is useful when it creates incremental value—not just "
    "more activity.",
    "Expand only what works.\n\nAdd more classes or spots where demand is real. "
    "Keep the test controlled everywhere else.\n\nFlexible access should "
    "complement memberships, not compete with them.",
]


THREAD_BOOKING_WORKFLOW = [
    "If every drop-in begins in DMs, your gym has built a manual booking system "
    "by accident.\n\nHere's the cleaner workflow. 🧵",
    "Publish the class once.\n\nSet the date, time, instructor, price and "
    "available spots—or create a recurring series for your weekly schedule.",
    "Let the customer choose and pay.\n\nThey see availability, book the class "
    "and complete payment in one flow. No price questions. No “Is there still "
    "space?” messages.",
    "Receive the booking.\n\nThe gym gets a real-time notification and a booking "
    "list with user and payment status, class by class.",
    "Receive the net payout.\n\nPayment goes to the connected Stripe account, "
    "with the platform commission deducted automatically. Everything stays "
    "traceable.",
    "Review the operation.\n\nUse bookings, net revenue, occupancy and instructor "
    "activity to see where drop-in demand is actually helping.",
    "FlexDropin partner activation is free. The platform commission is 15% on "
    "app bookings; Stripe processing fees apply.\n\nLearn more: "
    "https://flexdropin.com/partner",
]


ITEMS = [
    {
        "key": "p1",
        "token": "USGymOwnerP1_20261006",
        "date": date(2026, 10, 6),
        "position": 1,
        "category": "gym_strategy",
        "text": "An empty spot in a 6:00 PM class is perishable inventory. At "
        "6:01, its value is $0.\n\nThe opportunity isn't discounting every "
        "class. It's releasing a controlled number of spots to people ready to "
        "book and pay.\n\nHow does your gym handle unused capacity?",
        "stage": "stage-us-gym-owner-p1.png",
        "filename": "campaign-us-gym-owner-01-empty-spots-expire.png",
        "context": "Approved US gym-owner campaign P1: empty spots expire.",
    },
    {
        "key": "p2",
        "token": "USGymOwnerP2_20261006",
        "date": date(2026, 10, 6),
        "position": 2,
        "category": "product_proof",
        "text": "If a drop-in booking requires a DM, three messages, a waiver "
        "reminder and a manual payment, the class may be the easy part.\n\n"
        "FlexDropin gives gym owners one flow for availability, booking, payment "
        "and confirmation.\n\nMore bookings. Less admin.",
        "stage": "stage-us-gym-owner-p2.png",
        "filename": "campaign-us-gym-owner-02-dropins-not-dms.png",
        "context": "Approved US gym-owner campaign P2: replace the DM workflow.",
    },
    {
        "key": "p3",
        "token": "USGymOwnerP3_20261007",
        "date": date(2026, 10, 7),
        "position": 1,
        "category": "gym_strategy",
        "text": "Your membership software and your drop-in channel solve "
        "different jobs.\n\nOne manages recurring members. The other helps "
        "travelers, trial users and irregular schedules book a single class.\n\n"
        "FlexDropin is built for the second—without forcing you to replace the "
        "first.",
    },
    {
        "key": "p4",
        "token": "USGymOwnerP4_20261007",
        "date": date(2026, 10, 7),
        "position": 2,
        "category": "product_proof",
        "text": "Software fees are hardest to justify before a new channel "
        "proves itself.\n\nFlexDropin partner activation is free. The platform "
        "commission is 15% on bookings received through the app; Stripe "
        "processing fees also apply.\n\nTest demand without another fixed monthly "
        "bill.",
        "stage": "stage-us-gym-owner-p4.png",
        "filename": "campaign-us-gym-owner-04-pricing-model.png",
        "context": "Approved US gym-owner campaign P4: pricing model.",
    },
    {
        "key": "p5",
        "token": "USGymOwnerP5_20261008",
        "date": date(2026, 10, 8),
        "position": 1,
        "category": "product_proof",
        "text": "Cash, screenshots and payment-chasing don't scale.\n\nWith "
        "FlexDropin, customers pay when they book, the commission is deducted "
        "automatically, and the net payout goes directly to your connected "
        "Stripe account.\n\nEvery transaction stays traceable.",
        "stage": "stage-us-gym-owner-p5.png",
        "filename": "campaign-us-gym-owner-05-booking-to-payout.png",
        "context": "Approved US gym-owner campaign P5: booking to payout.",
    },
    {
        "key": "p6",
        "token": "USGymOwnerP6_20261008",
        "date": date(2026, 10, 8),
        "position": 2,
        "category": "product_proof",
        "text": "Your Tuesday 6:00 PM class shouldn't be rebuilt 52 times a "
        "year.\n\nCreate a recurring series once, set the instructor, capacity, "
        "price and date range, and let FlexDropin generate the sessions "
        "automatically.",
        "stage": "stage-us-gym-owner-p6.png",
        "filename": "campaign-us-gym-owner-06-recurring-classes.png",
        "context": "Approved US gym-owner campaign P6: recurring classes.",
    },
    {
        "key": "p7",
        "token": "USGymOwnerP7_20261009",
        "date": date(2026, 10, 9),
        "position": 1,
        "category": "gym_strategy",
        "text": "Before adding more classes, know what the current ones are "
        "doing.\n\nFlexDropin surfaces bookings, net revenue, occupancy rate and "
        "active instructors across today, week and month—so spare capacity "
        "becomes visible, not anecdotal.",
    },
    {
        "key": "p8",
        "token": "USGymOwnerP8_20261009",
        "date": date(2026, 10, 9),
        "position": 2,
        "category": "product_proof",
        "text": "Getting a gym online shouldn't become an IT project.\n\n"
        "1. Register your venue\n2. Connect Stripe\n3. Create classes and set "
        "prices\n\nFlexDropin needs a smartphone—not extra hardware or installed "
        "software.",
        "stage": "stage-us-gym-owner-p8.png",
        "filename": "campaign-us-gym-owner-08-go-live-three-steps.png",
        "context": "Approved US gym-owner campaign P8: three-step setup.",
    },
    {
        "key": "t1",
        "token": "USGymOwnerT1_20261010",
        "date": date(2026, 10, 10),
        "position": 1,
        "category": "gym_strategy",
        "text": THREAD_CONTROLLED_TEST[0],
        "thread": THREAD_CONTROLLED_TEST,
        "stage": "stage-us-gym-owner-t1.png",
        "filename": "campaign-us-gym-owner-09-thread-controlled-test.png",
        "context": "Approved US gym-owner campaign T1: controlled drop-in test.",
    },
    {
        "key": "t2",
        "token": "USGymOwnerT2_20261010",
        "date": date(2026, 10, 10),
        "position": 2,
        "category": "product_proof",
        "text": THREAD_BOOKING_WORKFLOW[0],
        "thread": THREAD_BOOKING_WORKFLOW,
        "stage": "stage-us-gym-owner-t2.png",
        "filename": "campaign-us-gym-owner-10-thread-booking-workflow.png",
        "context": "Approved US gym-owner campaign T2: no-DM booking workflow.",
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


def get_or_register_media(db: Database, item: dict) -> dict | None:
    if "stage" not in item:
        return None
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


def get_or_create_draft(db: Database, item: dict, media: dict | None) -> dict:
    media_id = media["id"] if media is not None else None
    draft_id = existing_draft_id(db, item["token"])
    if draft_id is not None:
        draft = db.get_post_draft(draft_id)
        if (
            draft is None
            or draft.get("text") != item["text"]
            or draft.get("category") != item["category"]
            or draft.get("media_id") != media_id
            or draft.get("status") != "approved"
            or draft.get("thread_tweets") != item.get("thread")
        ):
            raise RuntimeError(f"existing draft mismatch for {item['key']}")
        return draft

    state_key = "campaign:us-gym-owner:" + item["key"]
    expected_state = json.dumps(
        {"token": item["token"]}, sort_keys=True, separators=(",", ":")
    )
    db.set_state(state_key, expected_state)
    draft, outcome = ManualPostService(db).create_approved_from_telegram(
        text=item["text"],
        category=item["category"],
        source_ids=[],
        media_id=media_id,
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
    all_tweets = [item["text"] for item in ITEMS]
    all_tweets.extend(
        tweet
        for item in ITEMS
        for tweet in item.get("thread", [])[1:]
    )
    if any(len(tweet) > 280 for tweet in all_tweets):
        raise RuntimeError("campaign tweet exceeds X character limit")

    db = Database(str(DB_PATH))
    drafts: dict[str, dict] = {}
    media_rows: dict[str, dict | None] = {}
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
        media = (
            db.get_media_by_id(media_rows[item["key"]]["id"])
            if media_rows[item["key"]] is not None
            else None
        )
        if (
            plan["status"] != "planned"
            or plan.get("draft_id") != draft["id"]
            or draft.get("status") != "approved"
            or draft.get("media_id") != (media["id"] if media is not None else None)
            or (
                media is not None
                and (
                    media.get("lifecycle_state") != "reserved"
                    or media.get("reserved_by_draft_id") != draft["id"]
                )
            )
        ):
            raise RuntimeError(f"post-insert verification failed for {item['key']}")
        result.append(
            {
                "key": item["key"],
                "draft_id": draft["id"],
                "media_id": media["id"] if media is not None else None,
                "scheduled_for": plan["scheduled_for"],
                "plan_status": plan["status"],
                "thread_tweets": len(item.get("thread", [])),
            }
        )

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
