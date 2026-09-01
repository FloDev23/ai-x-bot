#!/usr/bin/env python3
"""Replace Italian text with English translations in both content_sources and post_drafts.

Targets source IDs 34-53 and their corresponding drafts (#19-38).
"""
import sqlite3

DB = "/home/ubuntu/ai-x-bot/bot_data.db"

# Maps source_id → (english_text, category)
TRANSLATIONS = {
    34: (
        "gym_strategy",
        "If your gym membership makes you feel guilty every time you skip, "
        "that's not fitness.\nIt's an emotional installment plan.\n"
        "Working out should fit your life — not the other way around.",
    ),
    35: (
        "gym_strategy",
        "January: annual membership.\n"
        "February: \"I'll catch up next week.\"\n"
        "March: monthly donation to the gym.\n\n"
        "Maybe you don't lack discipline. "
        "You just lack a more flexible way to train.",
    ),
    36: (
        "gym_strategy",
        "Unpopular opinion: you don't owe loyalty to one gym.\n\n"
        "CrossFit on Tuesday. Yoga on Thursday. Boxing on Saturday.\n\n"
        "Your training can change with your life.",
    ),
    37: (
        "gym_strategy",
        "The real luxury in fitness?\n"
        "Not the included towel.\n"
        "Not the sauna.\n\n"
        "Being able to choose where and when you train — "
        "without signing a 12-month contract.",
    ),
    38: (
        "gym_strategy",
        "You're not inconsistent.\n"
        "Maybe you just have shifting meetings, kids to pick up, "
        "delayed trains, and weeks that are all different.\n\n"
        "Flexible fitness exists for real life.",
    ),
    39: (
        "gym_strategy",
        "An annual gym membership can make financial sense. "
        "But only if you actually go.\n\n"
        "If you travel, change schedules often, or want to try different disciplines, "
        "paying per class might make more sense.",
    ),
    40: (
        "gym_strategy",
        "You can pick a movie without subscribing to a cinema.\n"
        "You can pick a house without buying the hotel.\n\n"
        "Why should trying a fitness class require a membership?",
    ),
    41: (
        "gym_strategy",
        "Drop-ins don't have to replace memberships.\n\n"
        "They capture demand from people who would never buy a monthly pass — "
        "tourists, business travelers, students, people with irregular schedules.\n\n"
        "It's additional revenue, not competition.",
    ),
    42: (
        "product_proof",
        "If you look for a gym before a restaurant when you travel, "
        "you're one of us.\n\n"
        "With FlexDropin, you find classes nearby, book in-app, "
        "and pay only for what you do.",
    ),
    43: (
        "product_proof",
        "One app. 40+ disciplines. Zero required subscriptions.\n\n"
        "Pilates today. CrossFit tomorrow. Climbing Saturday.\n\n"
        "Your routine can be having no routine.",
    ),
    44: (
        "product_proof",
        "To try a class at a new gym, you shouldn't have to:\n"
        "→ DM them\n"
        "→ wait for a reply\n"
        "→ ask about pricing\n"
        "→ find an ATM\n\n"
        "You should just be able to book. That's it.",
    ),
    45: (
        "product_proof",
        "POV: you're in a new city.\n"
        "You have an hour free.\n\n"
        "Open FlexDropin, find a class nearby, book it, and train.\n\n"
        "No contract. No phone call.",
    ),
    46: (
        "product_proof",
        "Every empty spot in a class has an expiration date.\n\n"
        "Once the class starts, that spot can't be sold.\n\n"
        "FlexDropin helps gyms and studios turn it into a booking.",
    ),
    47: (
        "product_proof",
        "Instagram DM. Name copied to WhatsApp. "
        "Payment \"when I get there.\" Manual confirmation.\n\n"
        "That's not a booking — it's an administrative task.\n\n"
        "FlexDropin automates the flow.",
    ),
    48: (
        "product_proof",
        "A gym shouldn't pay to \"get visibility\" and then hope.\n\n"
        "On FlexDropin, partner activation is free. "
        "The 15% commission applies only to bookings received.",
    ),
    49: (
        "product_proof",
        "Creating the same class every Monday is wasted time.\n\n"
        "With FlexDropin, set a recurring series once "
        "and dates are generated automatically.\n\n"
        "More time coaching. Less time on the calendar.",
    ),
    50: (
        "product_proof",
        "The metrics that matter for a gym aren't likes.\n\n"
        "They're bookings, occupancy, and revenue.\n\n"
        "The FlexDropin dashboard is built to show you exactly that.",
    ),
    51: (
        "founder_journey",
        "We're building FlexDropin on a simple belief:\n\n"
        "Working out shouldn't require a contract.\n"
        "Selling an open spot shouldn't require ten messages.",
    ),
    52: (
        "founder_journey",
        "The big platforms made flights, homes, and restaurants bookable.\n\n"
        "Yet many fitness classes are still booked by DM.\n\n"
        "That's the piece of the internet we're here to fix.",
    ),
    53: (
        "founder_journey",
        "The FlexDropin Manifesto:\n\n"
        "Move without constraints.\n"
        "Try without fear.\n"
        "Train even when traveling.\n"
        "Pay for what you use.\n\n"
        "Fitness should adapt to life — not the other way around.",
    ),
}


def main():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row

    updated_sources = 0
    updated_drafts = 0
    errors = []

    for source_id, (category, english_text) in TRANSLATIONS.items():
        if len(english_text) > 280:
            errors.append(f"source #{source_id}: text too long ({len(english_text)} chars)")
            continue

        # Update content_source
        r = conn.execute(
            "UPDATE content_sources SET text = ? WHERE id = ?",
            (english_text, source_id),
        )
        if r.rowcount == 1:
            updated_sources += 1
        else:
            errors.append(f"source #{source_id}: content_source not found")

        # Find and update corresponding draft via source_ids_json
        draft = conn.execute(
            "SELECT id, text FROM post_drafts WHERE source_ids_json = ?",
            (f"[{source_id}]",),
        ).fetchone()
        if draft is None:
            errors.append(f"source #{source_id}: no draft with source_ids_json=[{source_id}]")
            continue
        conn.execute(
            "UPDATE post_drafts SET text = ?, category = ?, updated_at = datetime('now') WHERE id = ?",
            (english_text, category, draft["id"]),
        )
        updated_drafts += 1
        print(f"  #{draft['id']} (source #{source_id}): {english_text[:60].strip()!r}")

    conn.commit()
    conn.close()

    print(f"\nDone: {updated_sources} sources updated, {updated_drafts} drafts updated.")
    if errors:
        print("\nErrors:")
        for e in errors:
            print(f"  {e}")


if __name__ == "__main__":
    main()
