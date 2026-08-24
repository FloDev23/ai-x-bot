# Adaptive Approved Post Queue Design

## Goal

Decouple FlexDropin draft creation from fixed publication slots, show the
operator a faithful Italian review translation for every English draft, retain
approved posts in a durable queue, and publish two approved posts per day at
adaptive times for a United States audience.

The change must preserve Telegram approval, source and factual validation,
media identity checks, duplicate prevention, pause behavior, `DRY_RUN`, and the
single existing X write boundary.

## Approved Decisions

- Groq remains the only AI provider.
- X posts remain in English.
- Telegram shows an Italian translation that is never sent to X.
- The initial publication target is exactly two posts per United States day.
- The approved queue target is seven posts.
- At most three drafts may wait for operator review at once.
- The primary audience is the United States, initially Eastern and Central
  time zones.
- Publication may happen during the Italian night after the exact draft was
  approved in Telegram.
- The scheduler chooses times deterministically from broad windows and later
  adapts them using owned-post performance. Groq never chooses an X write time.
- Continuous live publishing is enabled only after a dry-run acceptance phase
  and separate explicit operator authorization.

## Approaches Considered

1. **Adaptive deterministic scheduler — selected.** Use broad United States
   windows, persist a daily plan, then weight time buckets from measured owned
   post performance. This is restart-safe, explainable, and does not spend AI
   quota on timing decisions.
2. **Fixed windows with random jitter.** This is simpler and avoids exact
   repetitive times, but never learns from actual audience response.
3. **Groq-selected timing.** This is rejected because timing would be less
   deterministic, harder to audit, vulnerable to provider outages, and would
   consume limited Groq quota without adding reliable evidence.

## Architecture

The implementation adds four isolated responsibilities around the existing
`DraftPipeline`, Telegram controller, analytics, and `Publisher`:

1. `QueueReplenisher` decides whether one new review draft is needed. It does
   not assign a publication time.
2. `ReviewTranslator` creates and validates the Italian review translation
   after an English candidate has passed every existing editorial gate.
3. `PublicationPlanner` creates one durable two-item daily plan in
   `America/New_York` and selects eligible approved drafts.
4. `AdaptiveTimingPolicy` chooses the two daily timestamps from allowlisted
   time buckets using cold-start defaults and, later, owned-post performance.

`Publisher` remains the sole component allowed to call
`TwitterClient.post_tweet`. It receives an exact persisted publication-plan
claim rather than deriving due time from the legacy draft slot.

The old fixed-slot scheduler is removed only after the queue path and legacy
data migration are verified. No automatic like, follow, reply, repost, or
direct-message capability is added.

## Draft Replenishment

A periodic job runs every 30 minutes. It creates at most one draft per run and
only when all of these conditions hold:

- fewer than seven drafts are in `approved` or `planned` queue states;
- fewer than three drafts are waiting for review;
- fewer than four review drafts were created during the current Europe/Rome
  operator day;
- verified eligible sources are available.

The four-per-day ceiling permits the queue to build initially without flooding
Telegram. Once the approved queue reaches seven, replenishment stops. In steady
state, two publications normally create room for two replacement drafts.

The replenisher passes an immutable unique editorial planning anchor into the
existing content planner. The anchor is used for portfolio, source rotation,
link-budget, and idempotency decisions, but is not a promised publication
time. It generates one bounded candidate tournament and persists at most one
winning draft exactly as today.

Concurrent replenishment workers use one SQLite claim per operator day and
attempt number. Only the process that commits the claim may generate and send
the resulting Telegram card. A crash before persistence leaves a reclaimable
claim; a committed draft is never regenerated or announced twice.

## Italian Review Translation

The English text remains the canonical publication text. After it passes
source trust, expiry, claim, numeric, length, novelty, and scoring gates,
`ReviewTranslator` asks Groq for a faithful Italian translation with low
temperature and a bounded output size.

The translation contract requires:

- one non-empty Italian string;
- no commentary, score, markdown wrapper, or alternative copy;
- the same URLs as the English text;
- the same signed numbers, percentages, ranges, and compact scales;
- a bounded character and byte length;
- sanitized failure handling with no prompt, source body, model reasoning, or
  secret written to logs or SQLite.

The translation is auxiliary and does not enter the factual or editorial
score of the English post. It is stored separately and cannot replace or mutate
the English `text` column. A draft whose translation is missing, malformed, or
numerically inconsistent remains in a retryable `translation_pending` state
and is not shown with an approval button. Translation retry uses bounded
backoff and does not rerun the candidate tournament.

Existing pending or approved legacy drafts receive a translation lazily before
their next Telegram display or queue scheduling. A legacy draft cannot be
published by the new planner until its translation and current approval
snapshot are complete.

## Telegram Review Experience

Each review card contains, in this order:

1. the complete English text labelled `Tweet da pubblicare`;
2. the complete Italian text labelled
   `Traduzione italiana — solo per revisione`;
3. category, sources, score, media preview, and queue information;
4. the existing safe controls: Approve, Edit, Regenerate, Choose media,
   Text only, Postpone/not-before, and Discard.

Telegram message-size budgeting must preserve both complete texts. Metadata is
degraded or moved to a separate message before either text may be truncated.
If necessary, media is sent first, the English and Italian texts are sent as
separate messages, and controls are attached to the final message.

Editing or regenerating the English text invalidates the prior Italian
translation and approval revision. A new translation is required before the
replacement can be approved. Changing media alone does not change the text or
translation but still increments and revalidates the draft snapshot.

`/posts` reports:

- drafts awaiting translation;
- drafts awaiting review;
- approved and available posts;
- today's two planned posts and their United States times;
- blocked posts and bounded reason codes;
- recent published posts.

Every timestamp is shown in both Eastern Time and Europe/Rome for the operator.

## Durable Approved Queue

The existing `post_drafts` row remains the canonical text, sources, score,
status, approval, and media record. Queue-specific data is kept in additive
tables so the legacy slot schema does not need a destructive table rewrite.

### `editorial_queue`

One row per queue-enabled draft contains:

- `draft_id` primary and foreign key;
- `translation_it`;
- `translation_status`;
- `review_ready_at`;
- `approved_queue_at`;
- optional `not_before`;
- optional bounded `blocked_reason`;
- `revision` and timestamps.

The legacy `intended_slot` becomes an immutable editorial planning anchor for
queue-enabled drafts. New publication code must not use it as a due time.
Legacy fixed-slot drafts remain readable and can be migrated lazily.

### `publication_plans`

Each row contains:

- United States local date;
- daily position `1` or `2`;
- exact aware `scheduled_for` timestamp;
- nullable selected `draft_id` and exact draft revision until an approved post
  can be assigned;
- status `open`, `planned`, `publishing`, `published`, `simulated`, `skipped`,
  or `unknown`;
- bounded selection-reason data;
- claim token, timestamps, and published tweet ID when known.

SQLite constraints enforce one row per local date and position, one active plan
per draft, and one exact scheduled timestamp. Daily plan creation uses
`BEGIN IMMEDIATE`, compare-and-set revisions, and idempotent create-or-get
semantics. Two workers or a restart therefore observe the same plan.

The planner creates the two open positions at 00:05 Eastern and reconciles
unassigned positions every 15 minutes. This lets a post approved later in the
day fill a future position without changing its already persisted time. An
unfilled position is skipped when its window begins and produces one concise
Telegram notification.

No approved post is automatically deleted. Invalid or stale posts remain
auditable as blocked. The operator may edit, regenerate, or discard them.

## Selecting Queue Content

Before a draft may enter a publication plan, the planner revalidates:

- exact approved status and revision;
- complete Italian review translation;
- current source trust and expiry through the intended publication time;
- factual source binding and stored English copy identity;
- media reservation and verified file identity when media is attached;
- duplicate, portfolio, link-frequency, and weekly link-budget constraints;
- `not_before` and absence of another active plan.

Eligible drafts are ranked deterministically by:

1. source-expiry urgency, while requiring a safety margin beyond publication;
2. editorial total score descending;
3. difference from the most recently published category;
4. underrepresented category in the recent portfolio;
5. media/link-format diversity;
6. oldest approval time and stable draft ID.

The second daily selection is made after treating the first selection as part
of the recent portfolio. The two planned posts cannot use the same category
unless no valid alternative exists. All selection reasons are allowlisted,
bounded, and visible in Telegram; raw draft or source text is not copied into
planner audit metadata.

Immediately before X transport, `Publisher` repeats exact draft, source, link,
and media validation under the existing CAS and media-lock boundaries. A plan
that becomes invalid is marked `skipped`, the draft is blocked or returned to
the queue as appropriate, and Telegram receives one concise notification.

## Adaptive United States Timing

`America/New_York` is the canonical audience timezone. Daylight-saving changes
are handled by `zoneinfo`; fixed UTC offsets are forbidden.

### Cold start

Until at least 30 published posts have complete performance measurements, each
United States day receives:

- one time in the morning window, 08:30–11:30 Eastern;
- one time in the late-afternoon/evening window, 16:30–20:30 Eastern.

The exact minute is selected by a deterministic pseudorandom function seeded
only from the United States local date, daily position, and a non-secret stable
installation identifier. The two times must be at least six hours apart. Times
are persisted before drafts are assigned, so restart behavior is stable.

### Learning phase

The two windows are divided into bounded 90-minute buckets. A published tweet
is eligible for timing analysis only after its owned metrics are at least 24
hours old and contain valid nonnegative impressions and engagements.

After 30 eligible posts, buckets with at least three observations receive a
smoothed engagement-rate weight. One bucket is still selected from each broad
window, preserving the six-hour gap and a small deterministic exploration
share. After 90 eligible posts, a bounded weekday adjustment may be applied.
Sparse, malformed, future, or rolled-back-clock data is ignored. It can never
open an X write gate or increase the daily publication count.

The daily timing decision stores only bucket identifiers, sample counts,
bounded scores, and an allowlisted reason such as `cold_start`,
`performance_weighted`, or `exploration`.

## Publication Execution

A poller runs every five minutes and claims due plan rows. The first production
release targets exactly two successful publications per United States day. It
does not create an automatic third post.

The plan has a 90-minute execution window. Definite failures before an X write
may be retried with bounded backoff inside that window. An ambiguous X response
uses the existing `publication_unknown` behavior and is never retried
automatically. A missed plan returns an otherwise valid draft to the approved
queue and notifies Telegram; it is not published late into an unrelated time
window.

The following remain mandatory:

- `APPROVAL_REQUIRED=true`;
- exact approved revision and plan claim;
- publication pause open only on canonical `false`;
- source and media revalidation;
- strict X tweet ID validation;
- one atomic terminal database transition;
- no retry after an ambiguous write;
- English `text` supplied to X, never `translation_it`.

`DRY_RUN=true` records and displays the same daily plan but does not claim or
consume approved drafts and performs no X/media upload. When a dry-run position
becomes due it is marked `simulated`; its approved draft remains available, and
the next dry-run plan prefers other eligible drafts so the operator can inspect
the breadth of the queue without losing it.

## Failure Handling

- Groq generation failure records one sanitized error and waits for a later
  replenishment run.
- Translation failure leaves only the exact English draft in a non-approvable
  retry state.
- Telegram delivery failure retains the draft and allows `/posts` to redisplay
  it without duplicate database rows.
- Planner failure leaves the approved queue unchanged and creates no partial
  daily plan.
- Source expiry, revocation, or factual mismatch blocks the affected draft and
  never substitutes another text under an existing approval.
- A transient definite X failure may retry within the same plan window.
- An ambiguous X result is terminally unknown and requires manual
  reconciliation.
- SQLite lock contention fails closed without exceeding draft-generation or
  publication budgets.
- Shutdown checks are repeated before generation, planning, claim, media
  upload, and `create_tweet` where applicable. The existing `/pause` command
  continues to stop planning claims and publication only; it does not prevent
  draft replenishment or translation, so the approved reserve can keep growing.

## Migration and Compatibility

Schema changes are additive, transactional, idempotent, and crash-safe.
Migration never deletes or rewrites source, media, posted-tweet, evaluation, or
analytics history.

On first startup:

1. create the queue and publication-plan tables and migration marker in one
   serialized schema transaction;
2. leave terminal drafts unchanged;
3. register nonterminal legacy drafts for lazy translation and queue review;
4. preserve each exact approval revision, but require a fresh queue approval
   if the legacy slot has expired or any text/source/media identity changed;
5. keep the legacy slot jobs disabled only after the queue scheduler is
   registered successfully.

Rollback before live enablement consists of disabling the new jobs and
reenabling the legacy scheduler while `DRY_RUN=true`. Once continuous adaptive
live publication is explicitly authorized, rollback pauses publication first
and never attempts to reconstruct already consumed slot drafts.

## Configuration

The initial configuration is:

```dotenv
POSTS_PER_DAY=2
APPROVED_QUEUE_TARGET=7
PENDING_REVIEW_LIMIT=3
DRAFT_GENERATION_DAILY_CAP=4
AUDIENCE_TIMEZONE=America/New_York
MORNING_WINDOW=08:30-11:30
EVENING_WINDOW=16:30-20:30
MIN_POST_GAP_HOURS=6
ADAPTIVE_TIMING_MIN_POSTS=30
ADAPTIVE_WEEKDAY_MIN_POSTS=90
PUBLICATION_PLAN_GRACE_MINUTES=90
```

Startup validates every value and fails closed on malformed booleans,
timezones, windows, ranges, gaps, caps, or contradictory limits. Safe defaults
remain `DRY_RUN=true` and `APPROVAL_REQUIRED=true`.

## Testing

Tests use deterministic provider, clock, Telegram, X, scheduler, and media
fakes plus real SQLite concurrency and subprocess crash probes. They must prove:

- replenishment is independent of publication times and respects queue,
  pending, and daily caps;
- concurrent replenishment creates and announces at most one draft per claim;
- translation numbers, ranges, scales, and URLs match the English text;
- editing invalidates translation and approval while media-only changes do not
  change either text;
- complete English and Italian text survive Telegram message limits;
- the Italian translation cannot reach any X client call or posted-tweet row;
- approval is queue-based and does not expire at the legacy planning anchor;
- daily plans are stable across restart, timezone, DST, and concurrent workers;
- exactly two plan positions and at most two successful X posts exist per
  United States day;
- scheduled times obey both windows and the six-hour gap;
- cold-start and learned timing use only valid, sufficiently mature owned
  metrics;
- content selection is deterministic and revalidates source, copy, link, and
  media identity;
- source revocation, stale media, pause, dry run, missed windows, X failures,
  ambiguous results, and SQLite contention all fail closed;
- the legacy data migration is idempotent, concurrent-safe, and crash-safe;
- no follow, like, reply, repost, DM, or second X write path is introduced;
- the complete regression suite, compilation, dependency, and diff checks
  remain clean.

## Rollout and Acceptance

### Dry-run phase

1. Back up production SQLite.
2. Deploy code and schema with `DRY_RUN=true` and
   `APPROVAL_REQUIRED=true`.
3. Confirm source refresh, replenishment, translation, Telegram controls, and
   queue counts.
4. Approve enough drafts to build a seven-post reserve.
5. Observe at least two consecutive United States daily plans and confirm that
   both selected drafts and Italian translations are correct.
6. Prove from logs and X owned data that dry run performed zero X writes.

### Live enablement

Continuous live publication requires a separate, explicit operator
authorization after dry-run evidence is presented. The first live day is
observed through both planned publications. Any unknown result, duplicate
signal, unexpected queue mutation, or invalid translation pauses publication
automatically.

The feature is accepted when all tests pass, independent review reports no
Critical or Important finding, production remains stable through the dry-run
observation period, the operator confirms the Telegram presentation, and two
approved English posts can be published on one United States day without any
unapproved or duplicate X write.
