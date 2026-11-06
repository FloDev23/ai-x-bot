#!/usr/bin/env python3
"""Create and schedule the November 2026 X campaign (60 posts, Nov 6-30).

Idempotent: drafts are found by publication key, media by user_context and
plans by (date, position). Run first against a copy of the production DB:

    CAMPAIGN_DB_PATH=/tmp/copy.db python scripts/schedule_november_campaign.py --check
    CAMPAIGN_DB_PATH=/tmp/copy.db python scripts/schedule_november_campaign.py

``--check`` validates copy, length, duplicates and schedule without writing.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import sys
from zoneinfo import ZoneInfo

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.adaptive_timing import DailyTimingDecision
from modules.database import Database
from modules.manual_post_service import ManualPostService
from modules.media_processor import MediaProcessor
from modules.scoring import semantic_similarity


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv("CAMPAIGN_DB_PATH", str(ROOT / "bot_data.db"))).resolve()
# Media files are staged in the (private) media library root, like the
# earlier campaigns; the processor moves them to their final name.
MEDIA_ROOT = Path(
    os.getenv("CAMPAIGN_MEDIA_ROOT", str(ROOT / "media_library"))
).resolve()
ZONE = ZoneInfo("America/New_York")
TOKEN_PREFIX = "NovCampaign26"
LINK_CAP_PER_WEEK = 2
SIMILARITY_LIMIT = 0.6

GET_APP = "https://flexdropin.com"
PARTNER = "https://flexdropin.com/partner"

# key -> (staged file name, mime, final name, context)
MEDIA = {
    "v_athlete": ("nov-athlete.mp4", "video/mp4", "campaign-nov-athlete-flow.mp4", "Nov 2026: AthleteFlow video, 16:9 EN."),
    "v_story": ("nov-story.mp4", "video/mp4", "campaign-nov-story-flow.mp4", "Nov 2026: StoryFlow video, 16:9 EN."),
    "v_pin": ("nov-pin.mp4", "video/mp4", "campaign-nov-map-pin.mp4", "Nov 2026: AdMapPin video, 16:9 EN."),
    "v_wb": ("nov-whiteboard.mp4", "video/mp4", "campaign-nov-whiteboard.mp4", "Nov 2026: Whiteboard video, 16:9 EN."),
    "v_fill15": ("nov-fill15.mp4", "video/mp4", "campaign-nov-fill-spots-15s.mp4", "Nov 2026: AdFillSpots 15s vertical EN."),
    "v_pin15": ("nov-pin15.mp4", "video/mp4", "campaign-nov-map-pin-15s.mp4", "Nov 2026: AdMapPin 15s vertical EN."),
    "i_explore": ("nov-screen-explore.png", "image/png", "campaign-nov-screen-explore.png", "Nov 2026: Explore screen card."),
    "i_list": ("nov-screen-class-list.png", "image/png", "campaign-nov-screen-class-list.png", "Nov 2026: class list screen card."),
    "i_bookings": ("nov-screen-bookings.png", "image/png", "campaign-nov-screen-bookings.png", "Nov 2026: bookings screen card."),
    "i_payment": ("nov-screen-payment.png", "image/png", "campaign-nov-screen-payment.png", "Nov 2026: payment screen card."),
    "i_gym": ("nov-screen-gym-detail.png", "image/png", "campaign-nov-screen-gym-detail.png", "Nov 2026: gym detail screen card."),
    "c_crossfit": ("nov-card-crossfit.png", "image/png", "campaign-nov-card-crossfit.png", "Nov 2026: CrossFit first class card."),
    "c_yoga": ("nov-card-yoga.png", "image/png", "campaign-nov-card-yoga.png", "Nov 2026: yoga etiquette card."),
    "c_boxing": ("nov-card-boxing.png", "image/png", "campaign-nov-card-boxing.png", "Nov 2026: boxing first class card."),
    "c_pilates": ("nov-card-pilates.png", "image/png", "campaign-nov-card-pilates.png", "Nov 2026: pilates card."),
    "c_hyrox": ("nov-card-hyrox.png", "image/png", "campaign-nov-card-hyrox.png", "Nov 2026: HYROX card."),
    "c_cali": ("nov-card-calisthenics.png", "image/png", "campaign-nov-card-calisthenics.png", "Nov 2026: calisthenics card."),
    "c_lift": ("nov-card-lifting.png", "image/png", "campaign-nov-card-lifting.png", "Nov 2026: Olympic lifting card."),
    "c_cycle": ("nov-card-cycling.png", "image/png", "campaign-nov-card-cycling.png", "Nov 2026: indoor cycling card."),
}

H, F, P, G, J, S = (
    "human_thought", "fitness_education", "product_proof",
    "gym_strategy", "founder_journey", "shareable_fitness",
)

# (date, slot, category, text, media key or None, thread tweets or None)
ITEMS = [
    ("2026-11-06", "M", H, "Friday is where training plans meet real life. I like a plan for Monday and a backup plan for Friday.", None, None),
    ("2026-11-06", "E", P, "Want to work out today? No membership, just book a class. Find a gym nearby, see the price and spots left, pay in the app and get a reminder before it starts. That's the whole flow.", "v_athlete", None),

    ("2026-11-07", "M", H, "Saturday classes feel different. Nobody is rushing back to a desk, and the room gets a little more generous with each other.", None, None),
    ("2026-11-07", "D", F, "First time in a CrossFit class? Arrive ten minutes early, tell the coach about any injury, and scale the first workout without apology. Nobody remembers your weights. They remember if you came back.", "c_crossfit", None),
    ("2026-11-07", "E", S, "Fitness coaches and creators: what is the one thing you wish people knew before their first class with you? Curious to read the answers.", None, None),

    ("2026-11-08", "M", H, "Sunday asks a quiet question: what does next week's training actually need to look like? The honest answer is usually smaller than the plan.", None, None),
    ("2026-11-08", "E", P, "A new city usually means a new routine. Your workouts don't have to restart from zero. Same app, new gym, one tap to book.\n\n" + GET_APP, "v_story", None),

    ("2026-11-09", "M", H, "Monday motivation is overrated. Monday logistics are underrated: bag packed, class chosen, time blocked. The workout gets easier after that.", None, None),
    ("2026-11-09", "E", P, "Three gyms within 20 km. Different disciplines, different prices, no contract with any of them. That's what the first screen of FlexDropin looks like when you open it in a new place.", "i_explore", None),

    ("2026-11-10", "M", H, "Hot take: the best warm-up is the one you actually do. Five minutes beats the perfect routine you skip.", None, None),
    ("2026-11-10", "D", F, "Yoga etiquette in one line: arrive early, silence your phone, and take the pose you need instead of the pose the room is doing.", "c_yoga", None),
    ("2026-11-10", "E", F, "You can try a new gym without a membership, a sales call or a contract. Here is a simple way to do it, step by step.", None, [
        "You can try a new gym without a membership, a sales call or a contract. Here is a simple way to do it, step by step. 🧵",
        "1. Pick the discipline first. Yoga, boxing, CrossFit and pilates feel very different. A gym is easier to judge once you know what kind of class you want.",
        "2. Check the basics before you go: class time, length, price, spots left and what to bring. If any of that is hidden, ask or skip.",
        "3. Book one class, not a plan. The goal is to find out whether the coaching, the room and the people suit you.",
        "4. Arrive ten minutes early and say it's your first time. Good coaches will adjust the class around you.",
        "5. Decide afterwards. Come back, try another gym, or leave it there. One class is not a commitment, and that is the point.",
    ]),

    ("2026-11-11", "M", H, "I trust a gym more when the schedule is easy to read. Clear times, clear levels, clear prices. It says the people running it respect your time.", None, None),
    ("2026-11-11", "E", P, "Right now, someone nearby wants a class. Travellers, freelancers, first-timers. Gyms list classes on FlexDropin for free and pay 15% only on paid bookings, plus Stripe's standard fees.", "v_pin", None),

    ("2026-11-12", "M", H, "Consistency isn't doing the same thing every day. It's coming back after the day it didn't work.", None, None),
    ("2026-11-12", "D", F, "A first boxing class is usually footwork, shadowboxing and bag work, with a lot of breathing. You won't spar in week one. The goal is learning to move.", "c_boxing", None),
    ("2026-11-12", "E", G, "A class description that fills spots answers four things: what you will do, who it's for, what to bring and how hard it is. First-timers book the class they understand.", None, None),

    ("2026-11-13", "M", H, "The best part of a group class is being slightly worse than the person next to you and pushing anyway.", None, None),
    ("2026-11-13", "E", P, "Booked classes are easier to keep when they live in one place. Today, upcoming and past, with the time, the address and directions a tap away. That's the Bookings tab in FlexDropin.", "i_bookings", None),

    ("2026-11-14", "M", H, "Weekend long run people: music, podcast or silence?", None, None),
    ("2026-11-14", "D", F, "Pilates is not a gentle class. Reformer or mat, the slow tempo is the workout. If your first session leaves your legs shaking, that's normal.", "c_pilates", None),
    ("2026-11-14", "E", P, "Your next athlete is closer than you think. A neighbour who has never heard of your gym can book one class in seconds. FlexDropin is free to join; 15% on paid bookings, plus Stripe fees.\n\n" + PARTNER, "v_wb", None),

    ("2026-11-15", "M", H, "Rest days are training. I just wish my brain filed them under training too.", None, None),
    ("2026-11-15", "E", S, "What makes someone leave a gym review? I suspect it is usually a person, not the equipment. A coach who remembered a name, a front desk that smiled.", None, None),

    ("2026-11-16", "M", H, "A Monday workout doesn't need to be impressive. It needs to be booked.", None, None),
    ("2026-11-16", "E", G, "A gym that only sells memberships loses the visitor who wants one class. A gym that only sells classes loses the regular. Offering both is the boring answer that tends to work.", None, None),

    ("2026-11-17", "M", H, "Training in an unfamiliar gym is a small act of trust: the coach, the room, the people. First impressions count double.", None, None),
    ("2026-11-17", "D", F, "HYROX in one sentence: running plus eight functional stations, repeated. If you have done neither, start by training the two separately.", "c_hyrox", None),
    ("2026-11-17", "E", F, "Training while travelling: a checklist that fits in one thread.", None, [
        "Training while travelling: a checklist that fits in one thread. 🧵",
        "Before you go, decide how many workouts you actually want. Two is enough for most trips and easy to keep.",
        "Look near where you'll be staying, not near the airport. Ten minutes on foot beats a plan that needs a taxi.",
        "Check three things before you book: time, price and entry rules. Hotels, gyms and studios all treat visitors differently.",
        "Pack once: shoes, a towel, a bottle and clothes you can wash in the sink.",
        "Choose a class format you already like, in a room you don't know. Familiar workout, unfamiliar city, and the trip suddenly feels easier.",
    ]),

    ("2026-11-18", "M", H, "Some days the workout is the reward. Some days the shower afterwards is. Both count.", None, None),
    ("2026-11-18", "E", P, "Empty spots? Zero income. List your classes on FlexDropin for free, set your own price and get paid through your own Stripe account. 15% only on paid bookings.", "v_fill15", None),

    ("2026-11-19", "M", H, "A good coach's first sentence sets the tone for the whole class. I think we underrate that.", None, None),
    ("2026-11-19", "D", F, "Calisthenics is strength you can practise anywhere: pull-ups, dips, push-ups and a lot of patience. Progress is slow, then suddenly it isn't.", "c_cali", None),
    ("2026-11-19", "E", J, "We keep FlexDropin simple on purpose: find a class, see the price, book, show up. Everything else is a distraction until those four steps feel effortless.", None, None),

    ("2026-11-20", "M", H, "Fridays reveal priorities. Whoever trains on a Friday evening has decided something about their week.", None, None),
    ("2026-11-20", "E", P, "Travellers. Freelancers. First-timers. All looking for one class, not a contract. FlexDropin connects them with gyms nearby: no membership, pay only for what you book.", "v_pin15", None),

    ("2026-11-21", "M", H, "A good playlist, a good coach and a good training partner can carry a bad day. You only need one of the three.", None, None),
    ("2026-11-21", "D", F, "Before picking a class, ask yourself: do I want to sweat, learn or recover? The right answer changes week to week, and that's fine.", None, None),
    ("2026-11-21", "E", G, "Gym owners: a drop-in visitor decides quickly. They look at the price, the time and whether anyone answers. Make those three obvious and you've done most of the selling.", None, None),

    ("2026-11-22", "M", H, "Sunday walks are underrated training. No tracking, no targets, just legs moving and a head clearing.", None, None),
    ("2026-11-22", "E", P, "Booking a class should end with one tap, not a transfer reference. In FlexDropin you pay inside the app with Apple Pay or card, and the gym gets paid directly.\n\n" + GET_APP, "i_payment", None),

    ("2026-11-23", "M", H, "My favourite workouts are the ones I didn't plan: a free hour, a class nearby, booked in a minute.", None, None),
    ("2026-11-23", "E", S, "Ask a coach what they would tell a nervous first-timer in one sentence. The answer is almost always kinder than the first-timer's own inner voice.", None, None),

    ("2026-11-24", "M", H, "The gap between I should train and I'm booked is where most workouts get lost. Anything that shrinks it helps.", None, None),
    ("2026-11-24", "D", F, "Learning Olympic lifting? Expect a lot of empty-bar work first. The snatch and the clean and jerk reward technique long before they reward load.", "c_lift", None),
    ("2026-11-24", "E", G, "A first-timer decides whether to book your class before they ever meet you.", None, [
        "A first-timer decides whether to book your class before they ever meet you. These are the five things they look for. 🧵",
        "1. What the class is. Not just HIIT. Say what happens: warm-up, main work, finisher, how long it lasts.",
        "2. Who it's for. All levels only works if you say how you scale.",
        "3. The price and what it includes. Mats, towels, shoes. Surprises at the desk cost you the next booking.",
        "4. The time and the place. A clear start time, the address, which door to use.",
        "5. How many spots are left. It tells people the class is real and that deciding now has a point. FlexDropin shows all five before someone books.",
    ]),

    ("2026-11-25", "M", H, "Not every good session ends with a personal record. Some end with I showed up and moved well. That is a good session.", None, None),
    ("2026-11-25", "E", P, "Same day, three classes, and the price and spots left next to each one. Choosing gets easier when everything sits on a single list.", "i_list", None),

    ("2026-11-26", "M", H, "Happy Thanksgiving to everyone celebrating. May your workout today be optional and your leftovers plentiful.", None, None),
    ("2026-11-26", "D", S, "Turkey trot, family walk or a quick class before the meal: training on a holiday works best when it's short and agreed in advance.", None, None),
    ("2026-11-26", "E", J, "We built FlexDropin for people whose week doesn't repeat. Some weeks you train four times, some weeks once, some weeks you're on a plane. A contract assumes the first kind of week.", None, None),

    ("2026-11-27", "M", H, "Black Friday for fitness is mostly commitment dressed up as a discount. Try the class first. Decide on the plan later.", None, None),
    ("2026-11-27", "E", P, "Reviews tell you what photos can't: how the coaching feels and whether beginners are welcome. On a FlexDropin gym page they sit next to the disciplines and the class schedule.", "i_gym", None),

    ("2026-11-28", "M", H, "A Saturday workout in a crowded class is a reminder that most people are just trying to get through their own set.", None, None),
    ("2026-11-28", "D", F, "Indoor cycling is about rhythm, not keeping up. Find the cadence that lets you finish the ride, then add resistance when you're ready.", "c_cycle", None),
    ("2026-11-28", "E", G, "A quiet Tuesday 7 PM class is not a failed class. It's a class that hasn't met its audience yet. Fixing visibility is cheaper than cutting the schedule.", None, None),

    ("2026-11-29", "M", H, "Planning next week: three workouts, one of them flexible. The flexible one is the one that saves the week.", None, None),
    ("2026-11-29", "E", G, "Good gyms make it easy to try them: a clear schedule, a clear price and clear first-time instructions. Coaches and gym owners, which of the three do you still handle by DM?\n\n" + PARTNER, None, None),

    ("2026-11-30", "M", H, "November taught me that consistency has seasons. Shorter days, same habit, slightly smaller workouts. That still counts.", None, None),
    ("2026-11-30", "E", S, "November is done. If you tried one new class this month, that's already a win. Same question for December: what is one class you've been curious about?", None, None),
]


# A thread's first tweet is the draft text.
ITEMS = [
    (day, slot, cat, thread[0] if thread else text, media, thread)
    for day, slot, cat, text, media, thread in ITEMS
]


def media_path(key: str) -> Path:
    return MEDIA_ROOT / MEDIA[key][0]


def token(item) -> str:
    day, slot = item[0], item[1]
    return f"{TOKEN_PREFIX}_{day.replace('-', '')}{slot}"


def slots_for(day: date) -> tuple[str, ...]:
    return ("M", "D", "E") if day.weekday() in {1, 3, 5} else ("M", "E")


def planned_times(day: date) -> tuple[datetime, ...]:
    rng = random.Random(f"nov26-{day.isoformat()}")
    three = len(slots_for(day)) == 3

    def at(hour, minute):
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZONE)

    if three:
        morning = at(8, 35) + timedelta(minutes=rng.randint(0, 40))
        midday = at(13, 20) + timedelta(minutes=rng.randint(0, 60))
        evening = max(midday + timedelta(hours=4, minutes=5), at(18, 10)) + timedelta(
            minutes=rng.randint(0, 50)
        )
        return morning, midday, evening
    morning = at(8, 40) + timedelta(minutes=rng.randint(0, 95))
    evening = at(18, 10) + timedelta(minutes=rng.randint(0, 120))
    return morning, evening


def validate_copy(existing_texts: list[str]) -> list[str]:
    problems = []
    seen_days: dict[str, list[str]] = {}
    for item in ITEMS:
        day, slot, category, text, media, thread = item
        seen_days.setdefault(day, []).append(slot)
        parts = thread or [text]
        for part in parts:
            visible = re.sub(r"https?://\S+", "x" * 23, part)
            if len(visible) > 280:
                problems.append(f"{token(item)}: {len(visible)} chars")
            if "—" in part or "–" in part:
                problems.append(f"{token(item)}: dash")
            if "IFF" in part:
                problems.append(f"{token(item)}: forbidden name")
        if media is not None and media not in MEDIA:
            problems.append(f"{token(item)}: unknown media {media}")
        if thread is not None and not 2 <= len(thread) <= 10:
            problems.append(f"{token(item)}: bad thread length")
        if thread is not None and thread[0] != text and not text:
            problems.append(f"{token(item)}: root")
    for day, slots in seen_days.items():
        expected = list(slots_for(date.fromisoformat(day)))
        if slots != expected:
            problems.append(f"{day}: slots {slots} != {expected}")
    own = [(token(item), " ".join(item[5] or [item[3]])) for item in ITEMS]
    for index, (name, text) in enumerate(own):
        for other_name, other in own[index + 1:]:
            score = semantic_similarity(text, other)
            if score >= SIMILARITY_LIMIT:
                problems.append(f"{name} ~ {other_name}: {score:.2f}")
        for old in existing_texts:
            score = semantic_similarity(text, old)
            if score >= SIMILARITY_LIMIT:
                problems.append(f"{name} ~ existing '{old[:50]}': {score:.2f}")
    links = sorted(
        datetime.fromisoformat(item[0]) for item in ITEMS
        if re.search(r"https?://", item[3])
    )
    for current in links:
        window = [value for value in links if current - timedelta(days=7) < value <= current]
        if len(window) > LINK_CAP_PER_WEEK:
            problems.append(f"too many links in the week ending {current.date()}")
    if len({token(item) for item in ITEMS}) != len(ITEMS):
        problems.append("duplicate token")
    return problems


def existing_draft(db: Database, tok: str):
    with sqlite3.connect(db.db_path) as conn:
        row = conn.execute(
            "SELECT id FROM post_drafts WHERE publication_key = ?",
            ("telegram-manual:" + tok,),
        ).fetchone()
    return db.get_post_draft(int(row[0])) if row else None


def register_media(db: Database, key: str) -> dict:
    staged, mime, final_name, context = MEDIA[key]
    for row in db.get_all_media(limit=2000):
        if row.get("user_context") == context:
            if row.get("file_deleted") or row.get("lifecycle_state") == "archived":
                raise RuntimeError(f"existing media unavailable: {key}")
            return row
    path = media_path(key)
    if not path.is_file():
        raise RuntimeError(f"missing staged media: {path}")
    return MediaProcessor(db).process_new_file(
        str(path), final_name, mime, path.stat().st_size, context,
    )


def create_draft(db: Database, item, media: dict | None) -> dict:
    day, slot, category, text, _media_key, thread = item
    tok = token(item)
    existing = existing_draft(db, tok)
    if existing is not None:
        if (
            existing.get("text") != text
            or existing.get("category") != category
            or existing.get("status") not in {"approved"}
            or existing.get("thread_tweets") != thread
            or existing.get("media_id") != (media["id"] if media else None)
        ):
            raise RuntimeError(f"existing draft mismatch: {tok}")
        return existing
    state_key = "campaign:nov26:" + tok
    expected = json.dumps({"token": tok}, sort_keys=True, separators=(",", ":"))
    db.set_state(state_key, expected)
    draft, outcome = ManualPostService(db).create_approved_from_telegram(
        text=text,
        category=category,
        source_ids=[],
        media_id=media["id"] if media else None,
        state_key=state_key,
        expected_state_value=expected,
        session_token=tok,
        operator="codex_operator",
        thread_tweets=thread,
    )
    if draft is None or outcome not in {"created", "already_applied"}:
        raise RuntimeError(f"draft creation failed: {tok} ({outcome})")
    return draft


def day_plans(db: Database, day: date) -> list[dict]:
    existing = db.list_publication_positions(day)
    if existing:
        return existing
    times = planned_times(day)
    buckets = (
        ("morning:0", "midday:0", "evening:0")
        if len(times) == 3
        else ("morning:0", "evening:0")
    )
    plans = db.create_or_get_publication_positions(
        day,
        DailyTimingDecision(times=times, bucket_ids=buckets, reason="cold_start"),
        datetime.now(timezone.utc),
    )
    if len(plans) != len(times):
        raise RuntimeError(f"unable to create plans for {day}")
    return plans


def assign(db: Database, plan: dict, draft: dict, tok: str) -> None:
    if plan["status"] == "planned" and plan.get("draft_id") == draft["id"]:
        return
    if plan["status"] != "open" or plan.get("draft_id") is not None:
        raise RuntimeError(f"slot conflict for {tok}: {plan['status']}")
    ok = db.assign_publication_plan_atomic(
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
        max_links_per_week=LINK_CAP_PER_WEEK + 1,
    )
    if not ok:
        raise RuntimeError(f"assignment failed for {tok}")


def main() -> None:
    check_only = "--check" in sys.argv
    db = Database(str(DB_PATH))
    with sqlite3.connect(db.db_path) as conn:
        existing_texts = [
            row[0] for row in conn.execute(
                "SELECT text FROM post_drafts WHERE publication_key NOT LIKE ?",
                ("telegram-manual:" + TOKEN_PREFIX + "%",),
            )
        ]
    problems = validate_copy(existing_texts)
    if problems:
        print("\n".join(problems))
        raise SystemExit(f"{len(problems)} problem(s)")
    missing = [k for k in {i[4] for i in ITEMS if i[4]} if not media_path(k).is_file()]
    already = {
        row.get("user_context") for row in db.get_all_media(limit=2000)
    }
    missing = [k for k in missing if MEDIA[k][3] not in already]
    if missing:
        raise SystemExit(f"missing staged media: {sorted(missing)}")
    print(f"copy ok: {len(ITEMS)} items, {sum(1 for i in ITEMS if i[4])} with media, "
          f"{sum(1 for i in ITEMS if i[5])} threads")
    if check_only:
        return

    now = datetime.now(timezone.utc)
    media_rows: dict[str, dict] = {}
    plans: dict[date, list[dict]] = {}
    for item in ITEMS:
        day = date.fromisoformat(item[0])
        if datetime(day.year, day.month, day.day, 23, tzinfo=ZONE) < now:
            raise RuntimeError(f"date in the past: {day}")
    for item in ITEMS:
        key = item[4]
        if key and key not in media_rows:
            media_rows[key] = register_media(db, key)
    drafts = {
        token(item): create_draft(db, item, media_rows.get(item[4]))
        for item in ITEMS
    }
    for item in ITEMS:
        day = date.fromisoformat(item[0])
        plans.setdefault(day, day_plans(db, day))
    for item in ITEMS:
        day = date.fromisoformat(item[0])
        slot_index = slots_for(day).index(item[1])
        if len(plans[day]) != len(slots_for(day)):
            raise RuntimeError(
                f"{day}: {len(plans[day])} plans exist, expected {len(slots_for(day))}"
            )
        assign(db, plans[day][slot_index], drafts[token(item)], token(item))

    report = []
    for item in ITEMS:
        day = date.fromisoformat(item[0])
        slot_index = slots_for(day).index(item[1])
        plan = db.list_publication_positions(day)[slot_index]
        draft = db.get_post_draft(drafts[token(item)]["id"])
        if (
            plan["status"] != "planned"
            or plan.get("draft_id") != draft["id"]
            or draft.get("status") != "approved"
        ):
            raise RuntimeError(f"verification failed: {token(item)}")
        if item[4]:
            media = db.get_media_by_id(draft["media_id"])
            if (
                media.get("lifecycle_state") != "reserved"
                or media.get("reserved_by_draft_id") != draft["id"]
            ):
                raise RuntimeError(f"media not reserved: {token(item)}")
        report.append((plan["scheduled_for"], draft["id"], item[2], bool(item[5]), item[4] or "-"))
    for row in report:
        print(*row, sep="  ")


if __name__ == "__main__":
    main()
