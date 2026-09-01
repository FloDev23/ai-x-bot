#!/usr/bin/env python3
"""Replace Italian thread content with English translations (drafts #16, #17, #18)."""
import json
import sqlite3

DB = "/home/ubuntu/ai-x-bot/bot_data.db"

THREADS = [
    {
        "draft_id": 16,
        "category": "gym_strategy",
        "tweets": [
            "Monthly membership or drop-in?\n\n"
            "It's not a preference. It's a lifestyle question.\n\n"
            "Here's when each one actually makes sense 🧵",

            "A monthly membership makes sense if:\n\n"
            "→ you go at least 3 times a week\n"
            "→ your schedule is fixed\n"
            "→ you never travel\n"
            "→ you never get tired of the same routine\n\n"
            "If all 4 apply, a membership is efficient.",

            "The problem? For most people, none of those 4 conditions stay stable.\n\n"
            "Meetings shift. Kids happen. Work trips. Chaotic weeks.\n\n"
            "And the monthly fee runs regardless.",

            "Drop-ins make sense if:\n\n"
            "→ you travel often\n"
            "→ your schedule varies\n"
            "→ you want to try different disciplines\n"
            "→ you don't want to feel guilty when you skip\n\n"
            "You pay only for what you use. No guilt.",

            "The uncomfortable truth: most memberships are a form of optimism.\n\n"
            "\"I'll go more this month.\"\n\n"
            "Drop-ins are realism.\n\n"
            "\"I go when I can. I pay only then.\"",

            "FlexDropin was built on exactly this: fitness should adapt to real life, "
            "not the other way around.\n\n"
            "Book a class. Pay for that one. Done.\n\n"
            "No contracts. No guilt.\n\nflexdropin.com",
        ],
    },
    {
        "draft_id": 17,
        "category": "gym_strategy",
        "tweets": [
            "Every day, thousands of class spots go unfilled in gyms and studios.\n\n"
            "Not because people don't want to train.\n\n"
            "Because the booking system is broken 🧵",

            "An empty spot in a class isn't just unused space.\n\n"
            "It's a spot that won't come back.\n\n"
            "Once the class starts, that revenue is gone forever.",

            "The classic drop-in process:\n\n"
            "→ DM on Instagram\n"
            "→ wait for a reply\n"
            "→ ask the price\n"
            "→ go to the front desk\n"
            "→ pay in cash\n\n"
            "It's 2026 and we're still booking fitness classes this way.",

            "The result? Many gyms give up on drop-ins.\n\n"
            "Too much friction. Too much confusion.\n\n"
            "And those spots stay empty.",

            "With digital booking:\n\n"
            "→ the client books in 30 seconds\n"
            "→ pays online\n"
            "→ gets automatic confirmation\n"
            "→ you don't touch anything\n\n"
            "The same spot generates real revenue instead of zero.",

            "FlexDropin automates this flow for gyms and studios.\n\n"
            "Free to activate. Commission only on bookings received.\n\n"
            "If nobody books, you pay nothing.\n\nflexdropin.com",
        ],
    },
    {
        "draft_id": 18,
        "category": "founder_journey",
        "tweets": [
            "Why I built FlexDropin.\n\n"
            "A story that starts with a CrossFit class and a DM that never got a reply 🧵",

            "I was on a work trip in Milan.\n\n"
            "I had an hour free. I wanted to train.\n\n"
            "I found a CrossFit box nearby and messaged them on Instagram to ask about drop-ins.\n\n"
            "No reply.",

            "I tried another studio. Reply after 3 hours: "
            "\"Yes, but cash only. Come directly.\"\n\n"
            "I went to find an ATM. It was out of service.\n\n"
            "I didn't train.",

            "It wasn't a motivation problem. It was a systems problem.\n\n"
            "Booking a hotel: 30 seconds.\n"
            "Booking a flight: 2 minutes.\n"
            "Booking a fitness class: an odyssey.",

            "I started talking to gym owners.\n\n"
            "They all said the same thing: \"Drop-ins interest us, but the management is a mess.\"\n\n"
            "DMs, WhatsApp, cash, manual confirmations. For every single booking.",

            "FlexDropin is the answer to both problems.\n\n"
            "For athletes: book in 30 seconds, pay only for what you do.\n"
            "For operators: no DMs, no cash, automatic bookings.\n\n"
            "Fitness that works for everyone.\n\nflexdropin.com",
        ],
    },
]


def main():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")

    for thread in THREADS:
        draft_id = thread["draft_id"]
        tweets = thread["tweets"]
        category = thread["category"]

        for i, tweet in enumerate(tweets):
            if len(tweet) > 280:
                print(f"  WARNING draft #{draft_id} tweet {i+1}: {len(tweet)} chars (>280)")

        first_tweet = tweets[0]
        thread_tweets_json = json.dumps(tweets, ensure_ascii=False, separators=(",", ":"))

        conn.execute(
            """UPDATE post_drafts
               SET text = ?, category = ?, thread_tweets_json = ?,
                   updated_at = datetime('now')
               WHERE id = ?""",
            (first_tweet, category, thread_tweets_json, draft_id),
        )
        print(f"  draft #{draft_id}: updated ({len(tweets)} tweets, first: {first_tweet[:55].strip()!r})")

    conn.commit()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
