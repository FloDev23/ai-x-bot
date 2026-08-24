# FlexDropin source-pool seeding design

## Goal

Populate the production editorial database with a small, high-quality batch of
external sources that can support relevant, interesting X drafts without
expanding publishing authority.

## Scope

- Research and insert 8–12 external sources.
- Store every external item as `verified_news`; do not infer `product_fact` or
  `founder_note` records from public web pages.
- Prefer primary research, official statistics, established fitness-industry
  organizations, and directly attributable reports.
- Cover a balanced set of topics: fitness participation and demand, flexible
  access/day passes, gym capacity and revenue, consumer behavior, travel
  fitness, and community or retention.
- Prefer sources published in 2024–2026. Older sources are allowed only when
  they are authoritative and still materially useful.

## Acceptance rules

Each inserted source must have:

- a unique HTTPS URL;
- a non-empty title and factual summary;
- an attributable source name;
- a valid publication date;
- a trustworthy, traceable host;
- claims limited to facts directly supported by the linked page.

Duplicate URLs, inaccessible pages, promotional claims without evidence,
anonymous material, bug-focused content, and sources unrelated to FlexDropin's
audience are rejected.

## Data flow and safety

1. Read the current production inventory without exposing secrets.
2. Research candidate pages and verify each claim against the original page.
3. Validate the final records with the same `is_complete_verified_news`
   boundary used by the bot.
4. Recheck URL duplicates on the production database.
5. Insert the accepted batch in one SQLite transaction and verify the rows in
   read-only mode after commit.

The operation creates no draft, sends no Telegram card, performs no X write,
and does not change `DRY_RUN=true` or `APPROVAL_REQUIRED=true`.

## Follow-up

Observe the next scheduled draft cycles and their scores. A permanent scheduled
news importer is deliberately out of scope until the initial batch demonstrates
useful output quality.
