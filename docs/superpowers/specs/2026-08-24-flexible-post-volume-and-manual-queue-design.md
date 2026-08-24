# Flexible Post Volume and Manual Queue Design

**Date:** 2026-08-24
**Status:** Approved design, pending written-spec review
**Audience:** FlexDropin operator and bot maintainers

## Objective

Grow a deeper reserve of approved, relevant X posts while publishing at least
two posts per US audience day and three posts on selected days. The operator
must also be able to add an exact post manually from Telegram, review its
Italian translation, attach existing media, and approve it into the same queue
used by AI-generated drafts.

## Safety invariants

- The bot never likes, follows, unfollows, reposts, replies, comments, or sends
  direct messages on X.
- Reading follower profiles and metrics remains read-only. “Followed on X” is
  only a local record of an action performed manually by the operator.
- The only production X write remains
  `Publisher -> TwitterClient.post_tweet -> create_tweet`.
- Every X post must still come from an exact approved draft and an exact
  publication plan.
- `DRY_RUN=true` remains in production until the already-agreed dry-run
  acceptance period succeeds and the operator separately authorizes live X
  publishing.

## Publication volume

### Cold start

Until 30 mature published-post samples exist:

- Monday, Wednesday, Friday, and Sunday have two positions.
- Tuesday, Thursday, and Saturday have three positions.
- Two-position days use a morning and evening window.
- Three-position days use morning, midday, and evening windows.

The approved US Eastern windows are:

- morning: `08:30–10:30 America/New_York`;
- midday: `13:00–15:30 America/New_York`;
- evening: `18:00–20:30 America/New_York`.

Selected times must be strictly ordered and at least four hours apart. The
planner creates exactly the number of positions required for the audience day;
it never posts outside a window to compensate for a missed position.

### Learned selection

After 30 mature samples, the bot ranks weekdays using only allowlisted owned
metrics: impressions, likes, reposts, replies, and bookmarks. It selects the
three strongest weekdays for a third position each week. Until a weekday has
enough trustworthy observations, its cold-start prior remains in the score so
one anomalous post cannot dominate the schedule. The existing 90-sample
threshold continues to govern finer weekday/time-bucket learning.

The weekly plan therefore remains 17 target posts: four two-post days and
three three-post days. Missing approved inventory causes an open or skipped
position, never an unapproved publication or a quality-gate bypass.

## Approved queue

The queue configuration becomes:

- approved queue target: 14;
- pending Telegram review limit: 5;
- automatic draft generation cap: 5 successful generation claims per Rome
  operator day;
- automatic replenishment continues every 30 minutes and stops when either the
  pending-review limit or queue target is reached.

Fourteen approved posts provide roughly five to six US audience days of
inventory at the new average volume. Automatic generation failures, including
Groq rate limits, release their claim without creating partial drafts. They are
retried by the normal scheduler; no factual or quality gate is relaxed.

Manual drafts do not consume the automatic generation cap. Translation calls
can still be rate-limited and retried independently.

## Manual Telegram workflow

A new `/newpost` command starts a persisted, restart-safe Telegram session.
Only the authorized chat can use it.

1. The bot asks for the exact English X text.
2. It validates nonblank text, the 280-character X limit, safe URLs, numeric
   claims, and unsupported payload types.
3. The operator selects one or more verified existing sources. A “founder
   opinion” path is allowed only for non-numeric, non-factual personal opinion;
   it cannot self-authorize product, performance, customer, pricing, or market
   claims.
4. The operator selects an available library image/video or chooses text-only.
5. The operator chooses automatic Italian translation or supplies an exact
   Italian translation manually.
6. The bot runs the same fact, novelty, editorial-score, media-identity, and
   source-trust gates used by generated drafts. The AI generation step alone is
   bypassed, so the submitted English text is never rewritten.
7. A normal bilingual Telegram card is sent with the existing approve, edit,
   postpone, media, text-only, and discard controls.
8. Approval places the manual draft in the same ranked approved queue as every
   other draft.

The workflow uses a deterministic operation key derived from the Telegram
session token. Persisting the draft and consuming the session are atomic, so
replay, restart, or concurrent callbacks cannot create duplicate drafts.

### Manual validation outcomes

- Invalid length, URL, or payload: no draft is persisted; the session remains
  available for corrected input.
- Missing or ineligible source for a factual claim: no draft is persisted.
- Fact, novelty, or score rejection: the operator receives a safe reason code
  and may submit a revised post.
- Translation service unavailable: the exact English draft is persisted once
  with `translation_status=pending`; the retry scheduler completes the private
  Italian translation later.
- Media missing, stale, reserved, or tampered: the draft fails closed or is
  saved text-only only after explicit operator choice. Raw paths are never sent
  to Telegram.

## Ranking and publication

Manual and generated posts share the same publication ranking. Source urgency,
quality score, category diversity, format diversity, approval age, weekly link
quota, media binding, and source validity remain authoritative. Manual origin
does not grant priority and cannot bypass a limit.

For a three-position day, the planner assigns three distinct approved drafts.
It favors category and media-format variety across the day. Atomic assignment
prevents one draft from occupying more than one live plan. Concurrent planner
or publisher workers preserve at-most-once X behavior.

## Configuration

Configuration stays strict and fail-closed. The implementation adds or updates
explicit values for:

- base posts per day: 2;
- third-post days per week: 3;
- approved queue target: 14;
- pending review limit: 5;
- generation cap: 5;
- the three US Eastern windows;
- minimum post gap: 4 hours;
- third-day learning threshold: 30 mature posts.

Malformed, missing, inverted, overlapping, boolean-like, or out-of-range
values stop startup. Documentation and `.env.example` must match production.

## Observability

`/status` must show:

- approved inventory and target;
- pending reviews and limit;
- today’s planned position count (`2` or `3`);
- each ET time and its Rome conversion;
- whether third-day selection is cold-start or learned;
- generation cap usage and sanitized rate-limit state;
- `DRY_RUN` and pause state.

Manual drafts are identifiable by a safe `manual` origin in audit events and a
deterministic publication-key prefix. No Telegram raw update, translation,
source body, token, credential, or model reasoning enters logs or audit details.

## Testing and acceptance

Automated tests must cover:

- zero production like/follow/unfollow/reply/repost/DM capabilities;
- the 2/3/2/3/2/3/2 cold-start week in America/New_York;
- DST, window bounds, four-hour gaps, restart stability, and concurrent
  planning;
- learned selection of exactly three third-post weekdays after 30 samples;
- three distinct drafts assigned on a three-position day;
- queue target 14, pending limit 5, generation cap 5, Groq rate-limit retry;
- `/newpost` authorization, persisted session, restart, replay, concurrency,
  exact English preservation, source/fact/novelty/score gates, translation
  retry, media verification, edit, approval, discard, and scheduling;
- manual and generated drafts sharing the same atomic Publisher boundary;
- dry-run producing zero X writes.

Production acceptance remains staged:

1. deploy with `DRY_RUN=true` and `APPROVAL_REQUIRED=true`;
2. create one text-only and one media manual draft through Telegram;
3. approve generated and manual drafts until inventory begins growing;
4. observe complete two-position and three-position US audience days;
5. confirm simulated counts, timing, translations, queue preservation, and zero
   X writes;
6. enable live publishing only after a separate explicit operator approval.

## Non-goals

- Automatic X engagement of any kind.
- Automatic approval of generated or manual posts.
- Publishing more than three posts in one US audience day.
- Generating filler content to satisfy a position.
- Bypassing sources, factual validation, novelty, score, link quota, media
  identity, pause, grace period, or dry-run gates.
