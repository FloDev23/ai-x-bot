# Automatic Editorial Source Refresh and Controlled First Publication Design

## Goal

Keep the FlexDropin editorial source pool fresh without manual database work,
use the English FlexDropin blog as an official source, continue importing
authoritative external news, and complete one controlled real X publication
after Telegram approval.

The change must improve editorial variety without weakening factual gates,
increasing promotional frequency, or enabling unattended live publishing.

## Approved Decisions

- The website publishes a small, versioned JSON editorial feed derived from
  its canonical English blog records.
- The bot refreshes both the FlexDropin feed and allowlisted external news
  every day at 10:30 Europe/Rome.
- FlexDropin articles are stored as `owned_blog_article`, not as
  `verified_news`, `product_fact`, or `founder_note`.
- The planner rotates toward unused sources and does not repeatedly select the
  newest database row.
- A FlexDropin blog link consumes the existing global weekly link budget, so
  the bot can publish at most one such link in a rolling seven-day window.
- Production remains permanently configured with `DRY_RUN=true` and
  `APPROVAL_REQUIRED=true`.
- The initial live launch is exactly one approved post, published by a
  dedicated one-shot command with a temporary process-only override. It does
  not turn on continuous live publishing.

## Architecture

The work spans two repositories:

1. `flexDropin-website` exposes the official feed from the same `BLOG_POSTS`
   data used to render the site.
2. `ai-x-bot-main` downloads and validates that feed, imports valid external
   news through the existing NewsAPI boundary, stores sources, rotates them in
   the content planner, and keeps all publication behind Telegram approval.

The two source channels are independent. Failure of the FlexDropin feed must
not prevent a valid external refresh, and failure of NewsAPI must not prevent a
valid FlexDropin refresh. Existing stored sources remain usable after a network
failure.

## Official FlexDropin Feed

### Endpoint

The website exposes:

`https://flexdropin.com/api/editorial-feed`

The route is generated only from `BLOG_POSTS` and returns English canonical
records. Italian alternatives are deliberately omitted to prevent duplicate
sources for the same article.

### Schema

The top-level JSON shape is exact and versioned:

```json
{
  "version": 1,
  "language": "en",
  "items": [
    {
      "slug": "canonical-english-slug",
      "url": "https://flexdropin.com/blog/canonical-english-slug",
      "title": "English article title",
      "summary": "English article excerpt",
      "published_at": "2026-08-20"
    }
  ]
}
```

The route returns at most 100 items, newest first, with deterministic output.
It does not expose Italian text, full article blocks, private metadata, or
runtime secrets. The response uses JSON content type and public cache headers.

Website tests prove that each URL is derived from `slugEn`, only the five
allowlisted item fields are present, ordering is deterministic, and malformed
blog records fail the build/test boundary rather than leaking into the feed.

## Feed Transport and Validation

The bot fetches only the fixed HTTPS endpoint on the exact
`flexdropin.com` host. Production does not accept an arbitrary feed URL from an
environment variable. Tests may inject a fake transport.

The client applies:

- separate connect/read timeouts;
- redirects disabled;
- an accepted JSON content type;
- a declared and streamed body limit of 256 KiB;
- strict UTF-8 and JSON parsing;
- exact top-level and item fields;
- `version == 1` and `language == "en"`;
- at most 100 items;
- unique slugs and URLs;
- lowercase URL-safe slugs;
- exact canonical URLs on `https://flexdropin.com/blog/<slug>`;
- non-empty bounded title and summary fields;
- valid ISO publication dates that are not in the future.

Any malformed item rejects the entire FlexDropin feed. No partial feed is
written, and errors are sanitized so response bodies and secrets cannot enter
logs, Telegram, or SQLite.

## Storage, Updates, and Trust

Each accepted blog record maps to one `content_sources` row:

- `source_type`: `owned_blog_article`;
- `text`: bounded English title plus summary;
- `url`: the canonical English article URL;
- `trust_state`: `verified`;
- `verified_by`: `flexdropin_editorial_feed`;
- metadata: title, summary, publication date, source name, slug, feed version,
  and a deterministic content hash.

The database import uses `BEGIN IMMEDIATE` and URL-based compare/update logic:

- an unseen canonical URL is inserted;
- an unchanged content hash is left untouched;
- an existing `owned_blog_article` with changed canonical content is updated;
- a URL already owned by a different source type is a conflict and rejects the
  feed;
- a manually revoked, rejected, or otherwise non-verified existing row is
  never re-enabled by the automatic feed;
- concurrent refreshes cannot create duplicate rows;
- items missing from a later feed are not deleted automatically.

This source type is editorially trusted but does not inherit every factual
permission of external news. It may support a general idea, a named entity, or
an exact numeric claim present in its stored title/summary. It may not authorize
first-person statements, product claims, incidents, testimonials, medical
claims, or named current events. Those claims continue to require their
existing dedicated source types.

External NewsAPI results remain `verified_news`. Only complete rows from exact
allowlisted domains are retained. Malformed articles are skipped, valid new
URLs are inserted atomically as a batch, and an existing external URL is not
silently rewritten. An empty domain allowlist disables only external refresh;
it does not disable the FlexDropin feed.

## Planning and Rotation

`owned_blog_article` is eligible for the existing editorial categories:

- `gym_strategy`;
- `fitness_business_insight`;
- `shareable_fitness`.

It is never eligible for `product_proof` or `founder_journey`.

Within an eligible category, the planner chooses exactly one source using a
deterministic rotation:

1. exclude sources already bound to a live draft;
2. prefer sources that have never appeared in a published draft;
3. next prefer sources whose last published use is at least 30 days old;
4. if every source was used more recently, prefer the least recently used one;
5. break ties by most recent source publication date and then stable row ID.

This ranking is applied across eligible source types rather than using a fixed
type priority that could permanently starve blog or external sources. The
existing portfolio mix, two-drafts-per-day cap, candidate tournament, score
threshold `75`, fact validation, novelty checks, and at-most-one Telegram card
remain unchanged.

When the selected source is an `owned_blog_article`, the planner includes its
canonical URL only if the existing rolling seven-day global link budget is
available and that exact article URL has not been published in the previous 30
days. A blog article may still ground a link-free post when the link budget is
exhausted. Blog links and product links share the same conservative global
budget.

## Scheduling and Operator Feedback

The current 10:30 Europe/Rome news job becomes one source-refresh cycle:

1. fetch and validate the FlexDropin feed;
2. persist the complete blog batch atomically;
3. fetch external topics through NewsAPI when its key and domain allowlist are
   configured;
4. persist the valid external batch atomically;
5. record only bounded counts (`inserted`, `updated`, `unchanged`, `rejected`)
   and sanitized error codes.

Normal successful or no-change refreshes do not create Telegram noise. A
systemic failure that prevents one channel from refreshing produces one concise
operator notification while the other channel continues. The cycle never
creates a draft directly and never calls X.

## Controlled First Real Publication

Deployment and publication are separate phases.

### Safe deployment and dry-run

1. Run website and bot test suites locally.
2. Deploy the website feed and verify its public schema and response limits.
3. Deploy the bot with `DRY_RUN=true` and `APPROVAL_REQUIRED=true` unchanged.
4. Back up the production SQLite database.
5. Run the source refresh and verify inserted/updated counts without printing
   secrets or source response bodies.
6. Run a draft cycle in dry-run and wait for a single Telegram card scoring at
   least `75`.
7. The operator reads the complete text and media preview and approves that
   exact draft in Telegram.

### One-shot live write

A dedicated CLI publishes one exact approved draft ID. It requires an explicit
confirmation argument, refuses drafts that are stale, expired, unapproved, or
already claimed, and delegates the write to the existing `Publisher` CAS and
verified-media boundaries.

Immediately before the live command, the operator must explicitly confirm the
exact post. The service is stopped briefly to remove scheduler competition.
The CLI is then run with a process-only `DRY_RUN=false` override; `.env` is not
edited. Operational cleanup restarts the service even if the CLI fails. After
the command:

- a valid decimal X tweet ID and atomic published audit must exist;
- an ambiguous X result becomes `publication_unknown` and is never retried;
- the service is restarted;
- runtime checks must again show `DRY_RUN=true` and
  `APPROVAL_REQUIRED=true`.

The one-shot CLI has no loop and cannot publish a second draft in the same
invocation. Continuous live publishing is explicitly out of scope.
If X returns an ambiguous result, acceptance pauses for manual reconciliation
on X; the command is not repeated to manufacture a successful result.

## Testing

Tests use deterministic transports and real temporary SQLite databases. They
must cover:

- exact website feed shape, English canonical URLs, ordering, and field limits;
- hostile redirects, hosts, paths, content types, oversized bodies, malformed
  JSON, duplicate items, future dates, and unsupported versions;
- whole-feed rollback and idempotent insert/update behavior;
- two-worker refresh concurrency without duplicate URLs or partial state;
- independent blog and NewsAPI failure handling;
- external domain allowlisting and incomplete article rejection;
- strict factual permissions for `owned_blog_article`;
- unused/least-recently-used rotation, stable ties, live-draft exclusion, and
  30-day source reuse behavior;
- the shared seven-day link quota and exact-article 30-day link suppression;
- no draft creation during refresh and no new X capability;
- one Telegram card for one persisted winner under concurrent draft workers;
- one-shot CLI rejection cases, dry-run behavior, exact-one-call behavior,
  ambiguous outcome handling, and unchanged persistent configuration;
- full website and bot regression suites, compilation, dependency checks, and
  clean diffs.

## Acceptance Criteria

The feature is complete when:

- the public website feed passes schema and transport probes;
- both source channels refresh independently on the daily schedule;
- the production database contains deduplicated `owned_blog_article` rows and
  continues to contain valid external sources;
- planner evidence shows deterministic rotation without weakening the score or
  fact gates;
- the bot is deployed with permanent safety flags still true;
- a score-`75`-or-higher Telegram-approved draft is published exactly once;
- the resulting X ID and database audit agree;
- the service returns to `DRY_RUN=true`, remains active, and performs no second
  live write.

## Non-Goals

- Scraping rendered blog HTML or sitemap pages.
- Importing Italian duplicates.
- Treating blog copy as a product-fact, medical, testimonial, incident, or
  current-event authority.
- Lowering the editorial threshold to force a draft.
- Automatic likes, replies, follows, unfollows, direct messages, or other X
  engagement.
- Enabling unattended continuous live publishing.
