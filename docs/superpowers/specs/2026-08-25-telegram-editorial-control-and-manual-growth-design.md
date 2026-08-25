# Telegram Editorial Control and Manual Growth Design

Date: 2026-08-25

Status: approved by the user on 2026-08-26

## Objective

Make Telegram the compact control center for FlexDropin's editorial reserve and
manual growth work. Operator-authored posts must be easy to add, browse,
schedule, remove, and pair with visible media. The bot may recommend relevant X
accounts and posts, but must not perform automated likes, follows, or unfollows.

The deployed service remains approval-only and in `DRY_RUN=true` throughout
this change. Enabling real X publication is a separate, explicitly authorized
operation after acceptance testing.

## Current Evidence and Root Cause

The production `/newpost` flow itself processed every Telegram update
successfully. Two terminal attempts failed for different reasons:

1. the manually entered copy contained a claim that the selected source did not
   support, so the fact guard rejected it;
2. a later attempt reached Groq claim analysis, but Groq returned HTTP 429 and
   the fail-closed pipeline returned `claim_analysis_unavailable`.

The last attempt stopped on the source-selection screen because that screen had
no `Nessuna fonte` or nested `Aggiungi fonte` path. This is a workflow-design
problem rather than a Telegram transport failure.

## Policy Boundary

X's current automation rules expressly prohibit automated likes. X's account
behavior guidance also prohibits automated proactive following and automated
unfollowing, while its authenticity policy identifies follow churn as
prohibited engagement manipulation.

Consequently this design provides read-only discovery and manual action links.
It does not add any hidden or direct X capability for likes, follows,
unfollows, replies, mentions, or direct messages.

Automated publication of an approved, scheduled post remains the only X write
path. It continues to pass through `Publisher` and `TwitterClient.post_tweet`.

## Product Decisions

- The approved approach is a compact Telegram control center rather than more
  standalone commands or a richer web dashboard.
- Operator-authored English copy is trusted as already approved.
- Manual posts bypass AI generation, claim analysis, fact guard, semantic
  scoring, and editorial approval callbacks.
- Only technical validation remains: exact non-empty UTF-8 text, at most 280
  characters, valid Telegram state, and safe media identity when media is used.
- Sources are optional for manual posts.
- Media is optional for every post.
- Italian translation is private review metadata and never blocks a manual
  post from entering the approved reserve.
- Generated posts keep all existing source, fact, score, novelty, translation,
  and Telegram approval gates.
- `/posts` becomes a compact, paginated index rather than a stream of complete
  cards.
- A post can be removed from the approved reserve through a recoverable,
  audited discard operation.
- Unused media uploaded by mistake can be archived or permanently deleted with
  explicit confirmation.
- A daily manual-growth digest is delivered at 09:00 `Europe/Rome`.
- Production remains in dry-run after deployment.

## Architecture

### 1. Manual Post Service

A dedicated manual-post service owns the operator-authority boundary. The
Telegram controller gathers input, but the service validates and persists the
final command.

The service accepts:

- exact English text;
- one portfolio category;
- zero to three eligible source IDs;
- zero or one available media ID;
- the exact persisted Telegram session token;
- optional Italian translation metadata.

It atomically:

1. validates technical invariants;
2. revalidates any selected source and media records;
3. creates one `pending_approval` post-draft record for schema compatibility;
4. creates/updates the editorial-queue record as `approved` in the same
   transaction;
5. marks the origin as `manual_operator` in the audit record;
6. reserves the selected media, if any;
7. consumes the exact Telegram session with compare-and-swap semantics.

The resulting post is already approved and eligible for automatic planning.
No generator, claim analyzer, fact guard, scorer, or novelty service is called.

Repeated callbacks with the same session token return the existing exact
result. A different payload using the same token fails closed.

### 2. Nested Source Intake

The manual-post session is a persisted parent workflow. The source intake is a
persisted child workflow rather than a replacement for the parent.

At the source step Telegram presents:

- `Scegli fonti esistenti`;
- `Aggiungi una fonte`;
- `Nessuna fonte`;
- `Annulla`.

Existing sources are filtered by the chosen category and displayed using safe
metadata only: ID, type, title/source name, and verification state. Source body
text is never copied into Telegram buttons or list pages.

If `Aggiungi una fonte` is selected, the current manual-post draft is frozen in
the parent session. The existing source-intake flow runs as a child. A
successful child commit stores the source, adds its ID to the parent selection,
and returns to the manual-post source step. Cancellation returns to the parent
without selecting a source. A hard crash or restart resumes the exact active
parent/child state.

Selecting `Nessuna fonte` records an empty source list. This is valid only for
the manual-operator origin; it does not weaken the generated-content pipeline.

### 3. Non-Blocking Review Translation

After the manual post is committed as approved, translation runs as a separate
idempotent operation:

- a valid result stores `translation_it` and marks it `ready`;
- provider failure, malformed output, rate limit, or timeout marks/keeps the
  translation `pending`;
- retry runs through the existing bounded translation scheduler;
- translation status never removes, blocks, or delays manual approval,
  planning, or publication eligibility;
- only the canonical English text can reach X.

Queue selection therefore distinguishes an advisory manual translation from a
required generated-post translation. Generated posts remain ineligible until
their translation is ready; manual posts do not.

The Telegram completion card immediately confirms that the post entered the
approved reserve. It shows the Italian translation when ready and a clear
`Traduzione in preparazione` label otherwise.

### 4. Media Browser

Media selection uses a single-item browser so the chat does not become an
album-sized wall of messages.

For each available record Telegram sends the verified photo, video, or
document stream plus a safe caption containing media ID, type, upload date, and
short AI description. Controls are:

- `Precedente`;
- `Successivo`;
- `Usa questo`;
- `Nessun media`;
- `Gestisci media`;
- `Annulla`.

Navigation state stores a stable page cursor and selected media identity.
Before selection and again during the atomic manual-post commit, the database
revalidates lifecycle, reservation, deletion state, and the persisted media
identity. Raw filesystem paths are never sent to Telegram or accepted back from
callbacks.

The browser uses an open verified media stream. If preview delivery fails, the
record is shown as unavailable and cannot be selected from that view.

### 5. Compact `/posts` Index

`/posts` first sends aggregate counts and then one index page of at most eight
items. Each row contains:

- post ID;
- normalized state;
- the first 70-100 safe characters of English copy;
- an optional compact ET/Rome schedule label.

Rows are inline callback buttons. Pagination uses an opaque, validated cursor
and provides `Precedenti`, `Successivi`, and `Aggiorna`. Ordering is stable:

1. attention required;
2. approved/planned;
3. recently published;
4. discarded only when explicitly requested.

Selecting an item opens its complete card only. The complete view contains:

- exact English copy;
- Italian review translation or pending state;
- real verified media preview when present;
- source labels;
- queue position or ET/Rome schedule;
- actions valid for the current revision and state;
- `Torna all'elenco` preserving the original page cursor.

Callbacks carry only IDs, actions, revisions, and bounded cursors; they never
carry post text.

### 6. Removing an Approved Post

The complete card for an approved or planned post contains `Rimuovi dalla
coda`. It opens a second confirmation containing the post ID and safe excerpt.

Confirmation performs one atomic operation that:

- compare-and-swaps the expected draft and queue revisions;
- rejects `publishing`, `publication_unknown`, and `published` states;
- releases an assigned future publication position;
- transitions the post to `discarded` with reason
  `operator_removed_from_queue`;
- releases reserved media without deleting it;
- writes one audit event.

The discarded post remains recoverable through its detail view. `Ripristina`
returns it to the correct origin-specific flow after current sources and media
are revalidated. Translation is required again for generated posts and remains
advisory for manual posts. No row is physically deleted.

### 7. Media Library and Deletion

`/media` opens the same single-item preview browser independently of a post.
Available actions depend on lifecycle:

- `Archivia`: available-only, reversible, hides the item from matching and
  manual selection;
- `Ripristina`: archived-only, after identity validation;
- `Elimina definitivamente`: only for never-used, unreserved media;
- `Rimuovi dal post`: only through the owning post workflow.

Permanent deletion requires a second confirmation bound to media ID, revision,
identity digest, and current lifecycle. The operation:

1. acquires the existing media-root mutation lease;
2. revalidates the exact database row in the same transaction;
3. marks the record deleted and records the audit intent;
4. removes the verified file through the trusted root boundary;
5. preserves a tombstone without raw path or content.

If filesystem deletion fails, the database must not claim a successful
deletion. Used, reserved, missing-identity, or stale records fail closed.

### 8. Daily Manual Growth Digest

A new read-only discovery service runs daily at 09:00 `Europe/Rome`. Its SQLite
budgets and history are restart-safe.

#### Account suggestions

At most five accounts are selected from existing read-only growth discovery.
Each item contains username, safe profile metrics, latest relevant activity,
and a bounded relevance explanation. Actions are:

- URL button `Apri account su X`;
- callback `Segnala come seguito`.

The callback records a manual decision only; it does not call X.

Fourteen days after a manually recorded follow, accounts not observed among
FlexDropin followers may appear under `Da rivalutare`, with a link to the
account. Any unfollow remains manual.

#### Post suggestions

At most ten public posts are suggested. Discovery is read-only and uses bounded
queries related to FlexDropin's audiences: independent gyms, drop-ins, class
capacity, functional fitness, CrossFit, Pilates, martial arts, and fitness
business operations.

Hard filters reject reposts, replies when unsupported by context, protected or
malformed authors, stale posts, spam, unsafe links, duplicate IDs, and records
already suggested within the cooldown. Ranking uses relevance, recency,
author quality, and specificity to FlexDropin.

Each result contains author, safe excerpt, relevance reason, and a URL button
`Apri post su X`. There is no like callback and no X write. The operator likes
the post manually in X.

The daily digest is one compact message with paginated/detail callbacks rather
than fifteen complete cards. Empty or unchanged results are silent unless the
operator explicitly invokes the command.

### 9. Automatic Publication

The existing adaptive cadence remains authoritative:

- at least two US-audience publication positions per day;
- three positions on the configured cold-start/adaptive days;
- automatic assignment from the approved reserve;
- due-time, grace-window, pause, revision, source/media, and idempotency checks
  immediately before the X boundary.

Manual posts and generated posts share the same planner after approval. Manual
origin does not bypass due-time, pause, idempotency, or media safety.

During this release `DRY_RUN=true` converts due plans to simulations and keeps
approved drafts available according to existing dry-run semantics. No real X
post is created. A future explicit rollout may set `DRY_RUN=false` while
keeping `APPROVAL_REQUIRED=true` for generated posts.

## Data Model

The implementation may extend existing tables additively. Required logical
state includes:

- manual origin and operator identity on draft/audit records;
- direct-approved timestamp for manual posts;
- origin-specific translation policy (`required` for generated content,
  `advisory` for manual content);
- parent/child Telegram session identity;
- compact-list cursor state that never contains copy;
- media revision/lifecycle used by callbacks;
- growth suggestion kind, X object ID, score, suggested date, cooldown, and
  manual decision;
- manual-follow observation date for the 14-day re-evaluation list.

All unique operations use deterministic keys:

- manual post: Telegram session token;
- nested source: parent token plus child token;
- daily account suggestion: audience date plus X user ID;
- daily post suggestion: audience date plus X post ID;
- removal/deletion: target ID plus expected revision/identity.

Schema migrations must be additive, idempotent, concurrency-safe, and
crash-recoverable under SQLite.

## Failure Handling

- Telegram session expiry or stale callback: no mutation, safe restart prompt.
- Duplicate callback: return prior exact outcome, no duplicate card or draft.
- Groq translation failure: approved manual post remains queued; bounded retry.
- Read-only X discovery failure: no digest mutation beyond a safe error record;
  no partial over-budget run.
- Selected source becomes stale: no commit occurs and the operator must choose
  another source or explicitly choose `Nessuna fonte`; sources are never
  silently removed from an operator-confirmed selection.
- Media becomes stale: selection fails; a text-only manual post may proceed
  only when the operator chose or confirms `Nessun media`.
- Planned-post removal race: revision/state CAS chooses exactly one winner.
- Media deletion race: lifecycle/identity CAS and root lease choose exactly one
  winner.
- Publication ambiguity after the X call: retain existing
  `publication_unknown` behavior and never retry automatically.
- All persisted/logged errors use existing sanitization and must not contain
  Telegram tokens, X credentials, raw update bodies, source bodies, post copy,
  filesystem paths, or provider payloads.

## Testing and Acceptance

### Manual posts

- zero, one, and three sources;
- nested add-source success, cancellation, restart, rollback, and replay;
- no AI generation/fact/score calls for manual origin;
- direct approved queue state committed atomically;
- translation 429 leaves approved post and retries later;
- text-only and media-backed posts;
- stale media/source races and two-worker SQLite contention.

### Telegram UX

- `/posts` eight-item pagination and stable ordering;
- excerpt never loses access to full exact copy;
- callback byte limits and hostile/malformed IDs;
- preview uses a verified open stream, never a pathname;
- back-navigation preserves page cursor;
- authorized chat only;
- no raw source body or secret-bearing data in messages.

### Removal and media lifecycle

- approved/planned discard releases plan and media atomically;
- publishing/published removal fails closed;
- restore revalidates dependencies;
- archive/restore/delete transitions;
- permanent deletion rejects used/reserved/stale media;
- hard-crash, rollback, filesystem error, and concurrent deletion probes.

### Growth digest

- no more than five account and ten post suggestions per Rome day;
- deduplication and cooldown survive restart/concurrency;
- malformed X records are isolated;
- 14-day re-evaluation uses complete follower snapshots only;
- all actions are links or local database callbacks;
- static and runtime proofs show zero like/follow/unfollow/reply/DM calls.

### Publication and rollout

- manual approved posts participate in the adaptive 2/3-post planner;
- due simulation has zero X writes with `DRY_RUN=true`;
- full test suite, compilation, dependency, diff, and side-effect scans pass;
- VPS backup, migration, fail-closed preflight, restart, and stability check;
- Telegram live smoke covers `/newpost`, `/posts`, `/media`, digest detail, post
  removal, and media archive using non-production X boundaries;
- final VPS configuration remains `DRY_RUN=true` and
  `APPROVAL_REQUIRED=true`.

## Documentation Changes

README, SETUP, `.env.example`, `/help`, and Telegram operator messages must
describe:

- direct-approved manual posts;
- optional sources and media;
- compact `/posts` and `/media` navigation;
- recoverable post removal and safe media deletion;
- 09:00 manual growth digest;
- prohibition on automated engagement;
- automatic scheduling versus dry-run simulation;
- the separate approval required to enable real publication.

## Out of Scope

- automated likes;
- automated proactive follows or unfollows;
- replies, mentions, reposts, or DMs;
- browser scripting of x.com;
- permanent deletion of published posts or used media;
- changing `DRY_RUN` to false;
- replacing Groq or changing the generated-content editorial gates;
- rebuilding the web dashboard.
