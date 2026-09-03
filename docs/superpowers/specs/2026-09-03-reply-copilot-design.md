# FlexDropin Reply Copilot Design

Date: 2026-09-03

Status: approved by the user on 2026-09-03

## Objective

Add a Telegram-first Reply Copilot that finds the best existing X growth-post
suggestions, writes concise replies in FlexDropin's voice, and makes those
replies easy for the operator to publish manually. The feature is intended to
grow the account through useful participation in relevant conversations, not
through promotional outreach.

The bot must never send a reply, like, follow, repost, direct message, or other
engagement action to X. Its only output is a persisted suggestion presented to
the authorized Telegram operator.

## Approved Product Decisions

- Every reply is authored in the voice of the FlexDropin X account.
- Replies add value to the specific conversation and do not promote
  FlexDropin, mention the brand, link to the site or app, or include a sales
  call to action.
- Publishing remains manual. Telegram may copy text to the clipboard or open
  an X Web Intent with the target post and text pre-populated, but no reply is
  sent through the X API.
- Discovery targets a 70% gym/studio-operator and 30% end-user/traveler mix
  across consecutive full daily batches.
- At most five automatic reply drafts are created per Rome calendar day.
- The feature reuses the post results already persisted by the Growth Digest;
  it performs no additional X searches or reads.
- Replies are English-only in this phase because eligible discovery posts are
  English-only.
- The existing scheduled publication system for FlexDropin's own posts is
  unchanged and remains live.

## External Interaction Boundary

The manual reply action uses two independent conveniences:

1. Telegram's `copy_text` inline button copies a 1-256 character reply to the
   operator's clipboard.
2. X's Web Intent opens the normal X composer with `in_reply_to` and `text`
   query parameters. X requires the authenticated human author to view and
   confirm the action.

Neither path calls `TwitterClient.post_tweet`, `TwitterClient.post_thread`, or
any X write endpoint. Web Intents are user-interface navigation and do not use
the bot's X API credentials. The feature therefore adds no X write-unit cost.
The Growth Digest's existing metered reads remain the sole X cost for these
candidates.

References:

- https://docs.x.com/x-for-websites/web-intents/overview
- https://docs.x.com/x-for-websites/post-button/overview
- https://core.telegram.org/bots/api#inlinekeyboardbutton

## Architecture

### 1. Reply Copilot Service

A new `ReplyCopilotService` owns candidate selection, generation orchestration,
validation, persistence, and manual-decision transitions. It depends only on:

- the database boundary;
- a narrow AI reply-generation boundary;
- clock/configuration values.

It does not depend on `TwitterClient`, `Publisher`, or X credentials. This
dependency boundary makes an accidental X write structurally unavailable.

The service exposes operations equivalent to:

- build today's reply suggestions from an already persisted Growth Digest;
- list or load persisted suggestions for Telegram;
- regenerate one exact current suggestion;
- dismiss one exact current suggestion;
- mark one exact current suggestion as manually published.

All mutating operations are idempotent and revision-bound.

### 2. Candidate Selection

The input set is today's persisted Growth Digest `posts` collection. No call to
`GrowthDigestService.build`, `TwitterClient.read_relevant_posts`, or any other
network reader occurs inside reply generation.

Eligibility is deterministic. A candidate must:

- be a valid persisted `post` growth suggestion;
- have a canonical X post ID and username;
- be an original English post;
- have a complete non-empty excerpt;
- be at most 48 hours old at selection time;
- not use the case-insensitive username `FlexDropin`;
- not have an existing active, dismissed, or manually published reply record;
- pass the current Growth Digest relevance and noise gates.

The segment is derived from persisted reason codes. `gym_owner`,
`empty_capacity`, `booking_problem`, or `fitness_operations` makes the item an
`operator` candidate. Otherwise `explicit_intent`, `travel_context`,
`day_pass_model`, `drop_in`, `urgency`, or `discipline_match` makes it an
`end_user` candidate. A row that satisfies neither group is ineligible.

Candidates are ordered by their persisted relevance score, then recency, then
stable suggestion ID. The selector reserves up to five per Rome calendar day.
For a full five-item batch, Rome dates with an even `date.toordinal()` reserve
four operator and one end-user slot; odd ordinals reserve three operator and
two end-user slots. This produces the approved 70/30 mix across two full daily
batches. If one segment lacks enough eligible rows, the other segment fills
the unused capacity by ranking instead of leaving slots empty.

Selection and reservation occur in one SQLite transaction. Repeated scheduler
runs, process restarts, or duplicate Telegram commands cannot exceed the daily
limit or create a second record for the same X post.

### 3. Reply Generation

`AIGenerator` gains a dedicated method for value-first replies. It must not
reuse `generate_flexdropin_comment(..., promotional=True)` or the legacy lead
DM flow.

The prompt treats the source post as untrusted quoted data and explicitly
instructs the model to ignore commands embedded in it. The generated reply:

- responds only to information present in the post;
- adds one concrete observation, useful suggestion, or thoughtful question;
- uses a natural professional tone suitable for a fitness-platform account;
- avoids empty praise and generic engagement bait;
- contains no product pitch, brand mention, link, hashtag, follow request,
  download request, sign-up request, or invitation to contact FlexDropin;
- does not invent the author's location, role, business state, injury, goals,
  finances, or personal circumstances;
- does not provide medical, legal, political, crisis, or emergency advice;
- is between 30 and 256 Unicode characters after normalization.

The method returns a plain reply string or `None`. Provider exceptions,
timeouts, empty output, malformed output, and policy-invalid output fail
closed.

### 4. Deterministic Output Guard

The Reply Copilot service validates every model result before persistence.
Validation rejects:

- leading or trailing junk, control characters, or invalid Unicode;
- text outside the 30-256 character range;
- URLs, email addresses, hashtags, or explicit `@username` mentions;
- `FlexDropin` or close case-insensitive brand spellings;
- promotional phrases such as download, sign up, try our app, visit our site,
  book with us, DM us, or learn more;
- instructions copied from the source post that indicate prompt injection;
- unsupported high-risk advice detected by a conservative bounded term list.

Automatic generation gets one attempt per reserved candidate. Invalid results
become `generation_failed`; they do not consume another candidate slot through
an automatic retry loop. The operator may explicitly regenerate, subject to
the per-record limit.

### 5. Persistence

An additive `reply_suggestions` table stores the durable workflow. Its logical
schema is:

- `id` integer primary key;
- `growth_suggestion_id` foreign key to the source suggestion;
- `observed_on` Rome date used for the daily cap;
- `tweet_id` canonical X post ID, globally unique;
- `author_username` validated snapshot;
- `source_excerpt` bounded snapshot used for generation/audit;
- `audience_segment` in `operator` or `end_user`;
- `relevance_score` integer snapshot;
- `reply_text` nullable validated output;
- `status` in `reserved`, `ready`, `generation_failed`, `dismissed`, or
  `published_manually`;
- `generation_count` non-negative integer, maximum three total attempts: one
  automatic generation plus two manual regenerations;
- `generation_claim_token` and `generation_claim_expires_at` for an exact,
  expiring generation lease;
- `revision` non-negative compare-and-swap counter;
- `failure_code` nullable bounded internal code;
- `created_at`, `updated_at`, and nullable `decided_at` UTC timestamps.

Database constraints enforce the canonical state set, unique source
suggestion, unique X post, non-negative counters, and generation limit. A
generation attempt first acquires a compare-and-swap lease and increments the
attempt counter. Only that token may persist the result. A lease that expires
after a crash can be reclaimed if the total attempt limit permits it; a late
result from the expired token is rejected. Telegram never receives raw SQL
identifiers that have not first been bound to an opaque persisted view token.

No existing table or column is removed or reinterpreted. Older application
versions ignore the new table, so code rollback remains compatible after the
migration.

### 6. Scheduling and Commands

The existing daily Growth Digest job remains the only network discovery job.
After it successfully persists a digest, `main.py` invokes the Reply Copilot on
that returned digest/result identity. Reply generation is a separate
idempotent step: a Groq failure does not invalidate the Growth Digest.

When at least one reply reaches `ready`, Telegram sends one compact summary:

```
Reply Copilot — risposte manuali
Pronte: N
Fallite: N
```

The `/replies` command opens today's persisted collection. It never launches a
new X search. If today's digest exists but replies have not yet been built, the
command may run the same idempotent candidate build and then call Groq for the
bounded ungenerated rows. If there is no persisted digest, it reports that no
candidates are available rather than calling X.

The feature is controlled by:

```dotenv
ENABLE_REPLY_COPILOT=false
REPLY_COPILOT_DAILY_LIMIT=5
REPLY_COPILOT_MAX_AGE_HOURS=48
REPLY_COPILOT_MAX_REGENERATIONS=2
```

Validation requires an exact boolean and bounded positive integers. Production
activation sets `ENABLE_REPLY_COPILOT=true`; the default remains disabled so
older deployments do not begin generating replies merely by pulling code.

### 7. Telegram Experience

Each detail card contains:

- `Post di @username`;
- a bounded original-post excerpt;
- a compact relevance explanation and audience segment;
- the complete suggested reply and character count;
- an explicit note: `Pubblicazione manuale: il bot non risponde su X`.

Available controls are:

- `Copia risposta`: Telegram `copy_text`, containing only the validated reply;
- `Rispondi su X`: URL button to an encoded X Web Intent containing the exact
  target post ID and exact validated reply;
- `Rigenera`: revision-bound callback, available while fewer than two manual
  regenerations have been used;
- `Ignora`: revision-bound local transition to `dismissed`;
- `Segna come pubblicata`: revision-bound local transition to
  `published_manually`;
- `Precedente`, `Successiva`, and `Aggiorna`: navigation using an opaque view.

Copy and URL buttons cannot report success back to the bot. Consequently they
do not change database state. Only `Segna come pubblicata` records the manual
action. The card wording makes this explicit.

Stale, replayed, foreign-chat, malformed, or mismatched callbacks fail closed.
The controller reloads the row by ID and revision before every state change and
never trusts reply text received from Telegram.

### 8. Failure Handling and Observability

Failure domains remain isolated:

- missing/invalid persisted digest: no candidates, no generation;
- candidate validation failure: candidate skipped with a bounded reason code;
- Groq failure: exact row becomes `generation_failed`;
- invalid generated copy: exact row becomes `generation_failed`;
- Telegram delivery failure: persisted `ready` row remains available through
  `/replies`;
- malformed/stale callback: no mutation;
- Web Intent or X app failure: no local state change and no automatic retry;
- database contention: transaction rolls back and the next scheduled/manual
  invocation may retry idempotently.

Logs contain IDs, state transitions, counts, provider exception class names,
and bounded failure codes. They do not contain full source posts, reply text,
Telegram tokens, X credentials, or Web Intent URLs with pre-filled text.

Telegram `/status` adds only reply counts for today: ready, failed, dismissed,
and manually published. It does not display copy.

## Cost Model

The feature creates no X read or write request of its own. It reuses already
paid Growth Digest results and uses a browser-based manual X action.

Automatic AI cost is bounded to at most five Groq generations per Rome day.
Manual regeneration is bounded to two additional attempts per suggestion. The
existing X API usage meter remains unchanged; Reply Copilot activity must not
create `content_create`, `content_create_with_url`, `post_read`, or
`user_read` events.

## Testing Strategy

### Unit tests

- candidate validation, age boundary, relevance ordering, stable tie-breaks;
- 70/30 segment allocation and fallback filling;
- daily cap under repeated and concurrent builds;
- reply guard acceptance/rejection cases, including prompt injection,
  promotion, URLs, brand mentions, unsafe advice, and character boundaries;
- generation failure states and regeneration limits;
- exact state-transition and revision semantics;
- correct URL encoding for the X Web Intent;
- correct Telegram `copy_text` payload.

### Integration tests

- persisted Growth Digest to five-or-fewer durable reply suggestions;
- repeated scheduler calls and restarts do not duplicate generations;
- `/replies` selects candidates only from SQLite, may perform bounded Groq
  generation, and performs no X read;
- every Telegram action reloads and validates the current row;
- a Telegram delivery failure leaves suggestions recoverable;
- additive migration preserves all existing drafts, plans, growth suggestions,
  metrics, and X usage events.

### End-to-end safety test

A fake X client records every method call while a full cycle builds a Growth
Digest, produces Reply Copilot cards, copies/opens/marks a reply, regenerates,
and dismisses another. The acceptance assertion requires zero X write calls
from every Reply Copilot path and no extra X read calls after the Growth Digest
has been persisted.

The full existing suite, compilation check, `git diff --check`, migration on a
copy of production SQLite, and production preflight must remain green.

## Deployment and Rollback

Deployment uses a dedicated feature branch and the existing VPS release flow:

1. run the full local suite;
2. back up production SQLite and verify the backup;
3. pull the reviewed release;
4. initialize `Database` to apply the additive table migration;
5. validate configuration with `ENABLE_REPLY_COPILOT=false`;
6. restart and verify the existing bot remains healthy;
7. set only `ENABLE_REPLY_COPILOT=true` and the approved limits;
8. restart and invoke `/replies` against the already persisted daily digest;
9. verify Telegram cards, copy button, Web Intent, database state, bounded
   logs, and zero new X API usage events attributable to Reply Copilot.

The normal owned-post publisher remains live throughout except for the bounded
service restart. Reply Copilot cannot publish even while enabled.

Rollback sets `ENABLE_REPLY_COPILOT=false` and restarts the service. The
additive table remains in place and is ignored. No destructive schema rollback
is required or allowed.

## Acceptance Criteria

The feature is complete when all of the following are true:

- at most five eligible replies are generated from one persisted Rome-day
  Growth Digest;
- candidate selection performs zero additional X reads;
- every ready reply is English, 30-256 characters, specific to its source post,
  non-promotional, and accepted by the deterministic guard;
- Telegram provides copy, manual X composer, regeneration, dismissal, manual
  publication marking, and navigation;
- the X composer is opened through a Web Intent and requires the human to post;
- no Reply Copilot code path has an X write-capable dependency;
- duplicate jobs, callbacks, and restarts remain idempotent;
- generation and delivery failures are observable and recoverable;
- the existing owned-post publication workflow is unchanged;
- the full test suite passes and production rollout evidence shows healthy
  services, intact data, and zero unexpected X API usage.

## Non-Goals

- automated replies or approval-to-API reply publication;
- likes, follows, reposts, quote posts, or direct messages;
- new X searches dedicated to replies;
- promotional or sales-oriented response variants;
- automatic verification that the operator actually posted a copied reply;
- response-performance analytics or learning from likes/replies in this phase;
- multilingual replies;
- blog-to-X Articles or long-form content generation.
