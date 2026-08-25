# Telegram Editorial Control and Manual Growth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Every task follows RED/GREEN/refactor, is reviewed before its commit, and leaves `DRY_RUN=true`.

**Goal:** Turn Telegram into a compact control center where operator-written posts enter the approved reserve immediately, sources and media are optional and browsable, approved posts and media can be safely managed, and daily X growth suggestions remain strictly read-only/manual.

**Architecture:** Introduce an explicit `manual_operator` authority boundary in the editorial queue, with advisory translation and no AI/fact/score gates. Add persisted Telegram view/session state for nested source intake and compact post/media browsers. Extend SQLite with crash-safe, revision-bound operations for queue removal, media lifecycle, and daily growth suggestions. Keep adaptive publication unchanged and keep the only X write behind `Publisher -> TwitterClient.post_tweet`; the rollout remains a dry-run simulation.

**Tech Stack:** Python 3.11+, SQLite, APScheduler, Telegram Bot HTTP API, Tweepy read APIs, Groq only for existing generation/translation paths, `zoneinfo`, pytest.

## Non-negotiable constraints

- `DRY_RUN=true` and `APPROVAL_REQUIRED=true` before, during, and after deployment.
- No automated like, follow, unfollow, repost, reply, mention, comment, DM, or browser automation on X.
- The only production X write remains `Publisher -> TwitterClient.post_tweet -> create_tweet`; no new caller may invoke it.
- Operator copy is persisted exactly and never sent through generation, claim analysis, fact guard, novelty, rewrite, or editorial scoring.
- Generated content retains every existing source, fact, novelty, scoring, translation, approval, and publication gate.
- Manual posts may have zero to three sources and zero or one media item.
- Italian translation is advisory for `manual_operator` and required for generated posts. It never changes the canonical English X payload.
- Telegram callbacks contain only bounded IDs, revisions, actions, and opaque view tokens—never post/source text, paths, provider responses, or secrets.
- Media is opened, previewed, and deleted only through the verified root/descriptor boundary; raw paths never enter Telegram.
- SQLite mutations use exact types, revisions, deterministic operation keys, `BEGIN IMMEDIATE`, and bounded fail-closed retry where root-lock discovery can change.
- Logs/audits contain allowlisted reason codes and IDs only; no raw Telegram update, post copy, source body, media path, token, credential, model reasoning, or exception payload.

## File map

- Create `modules/manual_post_service.py`: operator-authority validation, exact replay, and direct-approved persistence boundary.
- Create `modules/telegram_media_browser.py`: verified single-item preview and safe media actions.
- Create `modules/telegram_post_browser.py`: compact post index/detail presentation and opaque view cursors.
- Create `modules/growth_digest.py`: read-only account/post discovery, ranking, persistence, and 14-day re-evaluation.
- Modify `modules/database.py`: additive schema, manual queue policy, Telegram views, post removal/restore, media lifecycle intents, and growth suggestions.
- Modify `modules/draft_pipeline.py`: delegate manual posts to the dedicated service and preserve required generated gates.
- Modify `modules/review_translation.py` and `modules/publication_queue.py`: advisory translation retry and origin-aware queue eligibility.
- Modify `modules/media_store.py`: descriptor-bound quarantine/delete/recovery helpers.
- Modify `modules/twitter_client.py`: strict read-only relevant-post search boundary.
- Modify `modules/telegram_api.py`: safe `delete_message` used to replace browser previews.
- Modify `modules/telegram_controller.py`: nested `/newpost`, compact `/posts`, `/media`, and growth digest/detail callbacks.
- Modify `main.py`, `config.py`, `.env.example`, `README.md`, and `SETUP.md`: dependency wiring, 09:00 Rome job, strict limits, operator guide, and dry-run rollout.
- Create/modify focused pytest files named in each task.

---

### Task 1: Persist operator posts as direct-approved, source-optional queue entries

**Files:**
- Create: `modules/manual_post_service.py`
- Create: `tests/test_manual_operator_posts.py`
- Modify: `modules/database.py:400-750, 1600-1980, 2100-2990`
- Modify: `modules/draft_pipeline.py:588-708`
- Modify: `modules/review_translation.py`
- Modify: `modules/publication_queue.py`
- Modify: `tests/test_manual_post_queue.py`
- Modify: `tests/test_approved_post_queue.py`

**Interfaces:**

```python
class ManualPostService:
    def create_approved_from_telegram(
        self,
        *,
        text: str,
        category: str,
        source_ids: list[int],
        media_id: int | None,
        state_key: str,
        expected_state_value: str,
        session_token: str,
        operator: str,
    ) -> tuple[dict | None, str]: ...

Database.create_manual_approved_draft_consuming_state_atomic(
    *, text, category, source_ids, media_id, intended_slot,
    state_key, expected_state_value, session_token, operator, now,
) -> tuple[dict | None, Literal[
    "created", "already_applied", "session_conflict", "rejected",
    "no_eligible_source", "media_unavailable",
]]
```

- [ ] **Step 1: Write RED tests for the authority boundary**

Add tests proving zero, one, and three sources; text-only and media-backed posts; exact 280-character UTF-8 acceptance; 281/empty/surrogate rejection; exact replay; changed-payload rejection; and two workers racing the same session.

```python
def test_operator_text_enters_approved_reserve_without_editorial_ai(tmp_path):
    service, forbidden = manual_service_with_forbidden_ai(tmp_path)
    draft, outcome = service.create_approved_from_telegram(
        text="Empty class spots are perishable inventory.",
        category="business_insight",
        source_ids=[],
        media_id=None,
        state_key=STATE_KEY,
        expected_state_value=STATE,
        session_token=TOKEN,
        operator="floriano",
    )
    assert outcome == "created"
    assert draft["status"] == "approved"
    assert draft["origin"] == "manual_operator"
    assert draft["text"] == "Empty class spots are perishable inventory."
    assert db.get_queue_draft(draft["id"])["translation_policy"] == "advisory"
    assert forbidden.calls == []
```

Forbidden doubles must cover generator, rewrite, claim analyzer, fact guard, scorer, novelty, media matcher, and translator. Run:

`venv/bin/python -m pytest tests/test_manual_operator_posts.py -v`

Expected RED: missing service/schema/API and current one-source/AI-gate behavior.

- [ ] **Step 2: Add an additive, crash-atomic schema migration**

Under the existing schema `BEGIN IMMEDIATE` transaction add:

```sql
ALTER TABLE post_drafts
    ADD COLUMN origin TEXT NOT NULL DEFAULT 'generated';
ALTER TABLE editorial_queue
    ADD COLUMN translation_policy TEXT NOT NULL DEFAULT 'required';
```

Backfill `origin='manual_operator'` only when `publication_key LIKE 'telegram-manual:%'` and an exact existing manual audit agrees; otherwise retain `generated`. Backfill queue policy from origin. Add schema checks in Python decoders:

```python
source_ids == []        # valid only when origin == "manual_operator"
translation_policy     # exact "required" or "advisory"
```

Do not loosen `_decode_source_ids` globally for generated drafts. Extract an origin-aware decoder used by queue reads while source-driven methods continue requiring a non-empty list.

Add legacy DB, concurrent constructors, hard-exit before migration commit, restart, and `PRAGMA integrity_check=ok` tests.

- [ ] **Step 3: Implement the atomic direct-approved write**

In one transaction (and under the existing draft/media root-lock protocol when media exists):

1. validate exact positive IDs and canonical session token;
2. re-read exact Telegram state;
3. revalidate every selected source when the list is non-empty;
4. revalidate/reserve optional media and append its `media_context` source only for traceability;
5. insert `post_drafts` with `status='approved'`, `origin='manual_operator'`, `approved_by=operator`, and `approved_at=now`;
6. insert `editorial_queue` with `translation_status='pending'`, `translation_policy='advisory'`, and `approved_queue_at=now`;
7. insert one `draft_evaluations` event with `origin`, IDs, and `authority='operator'` but no copy;
8. CAS-delete the exact Telegram state;
9. commit once.

Use deterministic `publication_key='telegram-manual:' + session_token` and a token-derived microsecond only as a schema-compatible unique `intended_slot`; it must not become the publication time. Preserve exact replay by comparing text/category/operator/source/media/policy/audit, not merely publication key.

Persist a fixed synthetic scheduling score at the existing eligibility floor
(`{"total": 75, "authority": "operator"}`), not `100`: operator authority means
“approved”, not “guaranteed highest editorial priority”. The planner remains free to
mix manual and generated posts by age, diversity, urgency, and score.

- [ ] **Step 4: Make advisory translation non-blocking**

Change queue eligibility to:

```sql
AND (
    q.translation_policy = 'advisory'
    OR (q.translation_policy = 'required' AND q.translation_status = 'ready')
)
```

Generated approval continues to require `required + ready`. `save_review_translation` preserves `approved_queue_at` for advisory rows but retains the existing reset/re-review semantics for required rows. Translation retry includes approved advisory rows and is idempotent; provider error/429 keeps the post approved and eligible.

Add a test where the translator raises a rate-limit exception, planning still selects the manual post, retry later stores Italian, and X payload remains exact English.

- [ ] **Step 5: Delegate without preserving old manual editorial gates**

`DraftPipeline.create_manual_from_telegram_session` becomes a compatibility delegate to `ManualPostService`; it must not call `_source_context`, `_validate_copy`, FactGuard, scorer, novelty, or translation. Keep generated methods unchanged.

- [ ] **Step 6: Verify Task 1 and commit**

Run:

```bash
venv/bin/python -m pytest \
  tests/test_manual_operator_posts.py \
  tests/test_manual_post_queue.py \
  tests/test_approved_post_queue.py \
  tests/test_adaptive_publication.py \
  tests/test_publisher.py -q
```

Expected: selected suites pass; concurrent SQLite test yields one `created`, one `already_applied`, one draft, and one audit.

Commit:

```bash
git add modules/manual_post_service.py modules/database.py modules/draft_pipeline.py \
  modules/review_translation.py modules/publication_queue.py \
  tests/test_manual_operator_posts.py tests/test_manual_post_queue.py \
  tests/test_approved_post_queue.py
git commit -m "feat: trust operator posts into approved queue"
```

---

### Task 2: Build restart-safe optional source intake inside `/newpost`

**Files:**
- Modify: `modules/database.py`
- Modify: `modules/telegram_controller.py:880-1090, 1640-1880`
- Create: `tests/test_manual_post_source_workflow.py`
- Modify: `tests/test_telegram_workflows.py`

**Session contract:** one persisted parent state, with a nested child rather than a second competing chat state.

```json
{
  "version": 1,
  "kind": "manual_post",
  "step": "source_child_text",
  "token": "parent-token",
  "payload": {
    "text": "exact English",
    "category": "business_insight",
    "source_ids": [],
    "child": {"token": "child-token", "return_step": "sources"}
  },
  "expires_at": "aware-ISO"
}
```

- [ ] **Step 1: Write RED workflow tests**

Cover:

- `Nessuna fonte` proceeds to media;
- `Scegli fonti esistenti` allows 0–3 exact eligible IDs and paginates without source body;
- `Aggiungi una fonte` enters child text/classification/news metadata flow;
- child success atomically inserts the source and resumes the parent with the new ID selected;
- child cancel returns to the parent unchanged;
- process restart at every child step resumes exactly;
- stale source at final commit preserves the parent session and asks for reselection;
- duplicate callback/replayed update does not duplicate the source or post;
- unauthorized chats receive no state or content.

Run: `venv/bin/python -m pytest tests/test_manual_post_source_workflow.py -v`

Expected RED: current mandatory-source branch and source intake deletes/replaces the only session.

- [ ] **Step 2: Add atomic child-source persistence**

Add:

```python
Database.add_content_source_and_resume_manual_state_atomic(
    *, state_key: str, expected_state_value: str,
    resumed_state_value: str, child_token: str,
    source_type: str, text: str, url: str | None,
    metadata: dict, trust_state: str, verified_by: str,
) -> tuple[int | None, str]
```

Use `publication_key`-style child idempotency via a small additive `telegram_child_operations(token PRIMARY KEY, result_json, created_at)` table. In one `BEGIN IMMEDIATE`, validate the exact parent/child state, insert-or-recognize the exact source, replace bot state with resumed parent JSON, and persist the result. A changed payload for the same child token fails closed.

- [ ] **Step 3: Implement the source-choice screens**

After category selection show exactly:

- `Scegli fonti esistenti` → `manual:sources:existing`;
- `Aggiungi una fonte` → `manual:sources:add`;
- `Nessuna fonte` → `manual:sources:none`;
- `Annulla`.

Existing-source rows expose only `#id`, allowlisted type, bounded title/source name, and trust label. Keep selection count and allow zero on `Fonti selezionate`. The final post service, not the callback, revalidates sources.

Reuse the same validation helpers as `/ideas`; do not duplicate URL allowlisting or founder-note `publishable=True` logic. Manual copy URLs receive structural HTTPS safety validation only and are not required to match a selected source.

- [ ] **Step 4: Commit the post before attempting translation**

After media choice call `ManualPostService` immediately with translation pending. Only after successful commit, request a best-effort advisory translation or leave it for the 30-minute retry job. The Telegram confirmation must say `Post approvato e aggiunto alla coda` before any provider-dependent result.

- [ ] **Step 5: Verify and commit**

Run:

```bash
venv/bin/python -m pytest \
  tests/test_manual_post_source_workflow.py \
  tests/test_telegram_workflows.py \
  tests/test_telegram_controller.py \
  tests/test_source_ingestion.py -q
```

Commit:

```bash
git add modules/database.py modules/telegram_controller.py \
  tests/test_manual_post_source_workflow.py tests/test_telegram_workflows.py
git commit -m "feat: make manual post sources optional"
```

---

### Task 3: Add verified media browsing, archive/restore, and crash-safe deletion

**Files:**
- Create: `modules/telegram_media_browser.py`
- Create: `tests/test_telegram_media_browser.py`
- Modify: `modules/database.py:330-420, 5200-5530`
- Modify: `modules/media_store.py`
- Modify: `modules/telegram_api.py:590-690`
- Modify: `modules/telegram_controller.py`
- Modify: `tests/test_media_lifecycle.py`
- Modify: `tests/fakes.py`

**Interfaces:**

```python
MediaBrowser.show(*, chat_id: str, media_id: int | None, context: str) -> str
MediaBrowser.select(*, media_id: int, expected_revision: int) -> dict | None

TelegramApi.delete_message(chat_id: str, message_id: int) -> bool

Database.archive_media_atomic(media_id, expected_revision, expected_sha256) -> bool
Database.restore_media_atomic(media_id, expected_revision, expected_sha256) -> bool
Database.prepare_unused_media_delete_atomic(...) -> tuple[dict | None, str]
Database.complete_unused_media_delete_atomic(intent_token, exact_identity) -> bool
```

- [ ] **Step 1: Write RED preview/browser tests**

Use real private `0700` roots and SQLite records. Assert the browser:

- sends one verified photo/video/document stream, not a path;
- shows safe ID/type/date/description only;
- replaces the previous preview with `deleteMessage` during navigation;
- has `Precedente`, `Successivo`, `Usa questo`, `Nessun media`, `Gestisci media`, `Annulla`;
- uses stable IDs/revisions while new uploads arrive;
- fails closed on missing file, symlink swap, digest mismatch, MIME/type mismatch, archived/reserved/deleted media, stale revision, and absent `O_NOFOLLOW`;
- never leaks a path in messages/errors.

Run: `venv/bin/python -m pytest tests/test_telegram_media_browser.py -v`

- [ ] **Step 2: Add media revision and view persistence**

Add `media_library.revision INTEGER NOT NULL DEFAULT 0` and increment it on reserve/release/use/archive/restore/delete/reusable changes. Add:

```sql
CREATE TABLE telegram_views (
    token TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    view_kind TEXT NOT NULL,
    state_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

State JSON contains only target IDs, page direction, filters, and last Telegram message ID. Tokens are 96-bit URL-safe values; callbacks remain below 64 bytes. Decoder rejects unknown fields, booleans-as-IDs, oversized JSON, naive/future-invalid expiry, and wrong chat/view kind.

- [ ] **Step 3: Implement verified single-item rendering**

`telegram_media_browser.py` obtains a record, maps exact media/MIME pairs, enters `open_verified_media(record)`, revalidates the DB snapshot while the root lease is held, and calls `send_media` with the open stream. It updates the view only after successful send and then best-effort deletes the prior preview. A delete-message failure is sanitized and leaves selection safe.

Use this browser for both `/newpost` media choice and `/media`; remove filename-only media buttons from `_manual_media_markup`.

Management controls are exact and state-dependent: `Archivia` for available,
`Ripristina` for archived, and `Elimina definitivamente` only for an eligible
never-used/unreserved record. Permanent deletion opens a second confirmation
bound to media ID, revision, and identity digest; `Annulla` returns to the same
browser view. There is never a one-click permanent delete.

- [ ] **Step 4: Implement lifecycle actions and a recoverable delete state machine**

Archive is available-only; restore is archived-only and re-verifies identity. Permanent deletion is allowed only when `used=0`, lifecycle is `available|archived`, no reservation exists, and the exact revision/digest matches.

Create additive tables:

```sql
CREATE TABLE media_delete_intents (
    token TEXT PRIMARY KEY,
    media_id INTEGER NOT NULL UNIQUE,
    expected_revision INTEGER NOT NULL,
    expected_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('prepared','quarantined','complete')),
    quarantine_name TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE media_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id INTEGER NOT NULL,
    event TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
```

Deletion protocol:

1. under sorted media-root lease + `BEGIN IMMEDIATE`, validate exact row, set lifecycle `deleting`, increment revision, and persist `prepared` intent;
2. through a trusted dir-FD helper, open/verify exact inode+size+SHA, atomically rename to a random quarantine basename under the same root, and fsync the directory;
3. transactionally mark the intent `quarantined` while the row remains unusable in lifecycle `deleting`; retain the locator only as transient recovery state;
4. unlink the quarantine entry by dir-FD and fsync the directory;
5. transactionally tombstone the media row (`deleted`, no reservation, locator/identity cleared) and mark intent `complete`;
6. startup reconciliation resumes `prepared|quarantined` intents idempotently. If the quarantined file is already absent after a crash, the exact intent plus absence of the original locator authorizes the final tombstone.

If verification or filesystem work fails before quarantine, restore the prior lifecycle in a compensating exact-revision transaction. Never physically `DELETE` the historical media row from Telegram.

- [ ] **Step 5: Add hard-crash/concurrency tests**

Use subprocess `os._exit` at every protocol boundary, two delete callbacks, archive/delete races, reservation races, file swap, directory FD leak counts, and restart reconciliation. Assert one terminal tombstone, no orphan usable file, no double unlink, and `integrity_check=ok`.

- [ ] **Step 6: Verify and commit**

Run:

```bash
venv/bin/python -m pytest \
  tests/test_telegram_media_browser.py \
  tests/test_media_lifecycle.py \
  tests/test_media_dashboard.py \
  tests/test_telegram_workflows.py -q
```

Commit:

```bash
git add modules/telegram_media_browser.py modules/database.py modules/media_store.py \
  modules/telegram_api.py modules/telegram_controller.py tests/fakes.py \
  tests/test_telegram_media_browser.py tests/test_media_lifecycle.py
git commit -m "feat: manage media safely from Telegram"
```

---

### Task 4: Replace `/posts` with a compact index and recoverable queue removal

**Files:**
- Create: `modules/telegram_post_browser.py`
- Create: `tests/test_telegram_post_browser.py`
- Modify: `modules/database.py:2050-2440, 2860-3300, 4240-4355`
- Modify: `modules/telegram_controller.py:360-680, 1450-1660`
- Modify: `tests/test_telegram_workflows.py`
- Modify: `tests/test_adaptive_publication.py`
- Modify: `tests/test_publisher.py`

**Interfaces:**

```python
Database.list_post_index_page(
    *, cursor: dict | None, limit: int = 8, include_discarded: bool = False,
) -> tuple[list[dict], dict | None, dict | None]

Database.discard_queued_draft_atomic(
    draft_id: int, expected_draft_revision: int,
    expected_queue_revision: int, operator: str, operation_key: str,
) -> tuple[dict | None, str]

Database.restore_discarded_draft_atomic(
    draft_id: int, expected_draft_revision: int,
    expected_queue_revision: int, operator: str, operation_key: str,
) -> tuple[dict | None, str]
```

- [ ] **Step 1: Write RED compact-index tests**

Seed more than 20 drafts across pending, approved, planned, publishing, unknown, published, and discarded states. Assert:

- first page has at most eight one-line callback rows;
- the summary is one message and no full card/media is sent until a row is clicked;
- excerpts are normalized to 70–100 safe characters but detail preserves exact full English;
- ordering is attention-required, approved/planned, recent published; discarded appears only via explicit filter;
- next/previous/refresh remain stable when a newer draft is inserted;
- ET and Rome times appear only in detail/compact labels;
- hostile IDs, stale tokens, wrong chat, expired view, and callback length fail closed;
- back returns to the same opaque persisted page.

- [ ] **Step 2: Implement keyset pagination through `telegram_views`**

Use a fixed rank expression and stable tuple `(rank, updated_at, id)`. Store cursor tuples server-side in the view row; callback format is `posts:<token>:next|prev|refresh` and detail is `post:<token>:<id>:<revision>`. Do not use offset pagination.

`telegram_post_browser.py` is a pure safe presenter: it receives decoded rows, creates excerpts, renders buttons, and asks the controller to send a full existing draft card only for detail.

- [ ] **Step 3: Write RED removal/restore tests**

Cover approved text-only, approved media-backed, and planned drafts. Assert discard:

- CASes draft/queue revisions;
- rejects publishing, `publication_unknown`, published, stale callback, and wrong operation token;
- clears a future plan back to `open` without deleting the position;
- sets `status='discarded'`, `blocked_reason='operator_removed_from_queue'`;
- releases reserved media but retains the draft-to-media ID for possible restore;
- writes one allowlisted audit and is exactly replayable;
- remains atomic under trigger failure, hard crash, and two workers.

Restore must revalidate origin policy, every non-media source, the optional original media identity/availability, and reserve media atomically. Manual rows return directly to approved/advisory. Generated rows always return to `pending_approval`, invalidate the prior translation, and require a fresh required translation plus Telegram approval.

- [ ] **Step 4: Implement removal and restore with the existing root-lock order**

Use `_post_draft_mutation_lock(draft_id)` so current/prospective media roots are acquired before `BEGIN IMMEDIATE`. Persist deterministic operation keys in an additive `operator_operations` table to distinguish exact retry from conflicting reuse.

Only a genuinely future `planned` position may be released. If the plan is due,
publishing, unknown, simulated, or published, removal fails closed and asks the
operator to refresh; it never races the publisher by silently reopening the slot.

Do not call a DB mutator while `open_verified_media` is held. Detail preview keeps its existing root-lease revalidation/send boundary; discard/restore happens only after the preview context exits.

- [ ] **Step 5: Wire Telegram confirmation**

Detail actions:

- `Rimuovi dalla coda` → safe excerpt confirmation;
- `Conferma rimozione` / `Annulla`;
- discarded detail → `Ripristina` / `Torna all'elenco`.

On success show the updated detail and compact status. Media is optional and absence never blocks removal or restore.

- [ ] **Step 6: Verify and commit**

Run:

```bash
venv/bin/python -m pytest \
  tests/test_telegram_post_browser.py \
  tests/test_telegram_workflows.py \
  tests/test_adaptive_publication.py \
  tests/test_publisher.py \
  tests/test_media_lifecycle.py -q
```

Commit:

```bash
git add modules/telegram_post_browser.py modules/database.py \
  modules/telegram_controller.py tests/test_telegram_post_browser.py \
  tests/test_telegram_workflows.py tests/test_adaptive_publication.py
git commit -m "feat: browse and remove queued posts"
```

---

### Task 5: Discover and persist daily read-only growth suggestions

**Files:**
- Create: `modules/growth_digest.py`
- Create: `tests/test_growth_digest.py`
- Modify: `modules/twitter_client.py:225-275, 450-530`
- Modify: `modules/database.py:420-620, 5530-5900, 6260-6470`
- Modify: `modules/growth_discovery.py`
- Modify: `tests/fakes.py`
- Modify: `tests/test_x_write_safety.py`

**Interfaces:**

```python
TwitterClient.search_relevant_posts(query: str, limit: int = 25) -> list[dict]

GrowthDigestService.build(now: datetime) -> dict
# {"observed_on": "YYYY-MM-DD", "accounts": [...max 5],
#  "posts": [...max 10], "reevaluate": [...max 5], "outcome": str}

Database.persist_growth_digest_atomic(
    *, observed_on: str, account_rows: list[dict], post_rows: list[dict],
    reevaluate_rows: list[dict], completed_at: str,
) -> tuple[dict, str]
```

- [ ] **Step 1: Write RED normalized X-read tests**

`search_relevant_posts` must request exact fields for tweet ID/text/author/created time/lang/public metrics plus author username/protected/metrics. Per-record normalization rejects booleans, non-ASCII/nonpositive IDs, missing author, protected author, retweet/reply, future/stale time, oversized text, malformed metrics, unsafe URL, and incomplete includes. Exceptions log only operation + error type and return a typed incomplete result or empty list without payload.

Add pagination/token repetition and partial-page isolation tests. Never expose Tweepy's mutable client as a public capability.

- [ ] **Step 2: Add restart-safe suggestion schema**

```sql
CREATE TABLE growth_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_on TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('account','post','reevaluate')),
    object_id TEXT NOT NULL,
    username TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    score INTEGER NOT NULL,
    reason_codes_json TEXT NOT NULL,
    suggested_at TEXT NOT NULL,
    decision TEXT NOT NULL DEFAULT 'new',
    decision_at TEXT,
    cooldown_until TEXT,
    UNIQUE(observed_on, kind, object_id)
);
CREATE TABLE growth_digest_runs (
    observed_on TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL,
    summary_json TEXT NOT NULL
);
CREATE TABLE growth_read_claims (
    observed_on TEXT NOT NULL,
    query_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('claimed','completed','failed')),
    claimed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (observed_on, query_key)
);
```

All JSON is a closed, JSON-safe projection. Account payload contains IDs, username, bounded public metrics, latest activity ID/time, segment, and reason codes. Post payload contains ID, author ID/username, bounded excerpt, created time, public metrics, and reason codes. It contains no raw response, bio/source body, token, headers, or exception.

- [ ] **Step 3: Implement deterministic filtering/ranking**

Accounts come from the existing canonical `GrowthDiscovery`/`get_digest_candidates` path and retain its threshold/cache/query budgets. Limit five.

Posts use a fixed allowlisted query portfolio for gym ownership, drop-ins, class capacity, functional fitness, CrossFit, Pilates, martial arts, and fitness-business operations. Claim each query key in SQLite before calling X, cap total read queries per Rome day (new strict `GROWTH_POST_QUERY_BUDGET`, default `2`, maximum `2`), deduplicate exact post IDs, and reject anything suggested during a 30-day cooldown. A stale claim may be recovered after its bounded expiry; a completed claim is never reread that Rome day.

Rank posts with an integer-only projection:

```python
score = relevance_0_50 + recency_0_20 + author_quality_0_15 + specificity_0_15
```

Sort by descending score, descending created time, then exact post ID. Store only the top ten. Reason codes are an allowlist such as `gym_owner`, `empty_capacity`, `drop_in`, `pilates`, `martial_arts`, `recent`, `credible_author`.

Re-evaluation rows come only from `growth_candidates.decision='followed_manually'` with `manual_followed_at <= now-14d`, no `followed_back_at`, and a completed follower snapshot after the 14-day boundary that does not contain the user. Limit five and never infer from partial/failed snapshots.

- [ ] **Step 4: Make a daily run atomic and idempotent**

Build read results outside a write transaction. Then use `BEGIN IMMEDIATE` to insert the exact selected rows and run marker. If a completed Rome date exists, return its exact persisted rows without new X reads. Two processes yield one completed digest and identical results. A failed/incomplete X read must not overwrite a prior completed run or consume cooldown for missing results.

Add hard-crash pre/post commit, clock rollback, DST, malformed persisted JSON, duplicate IDs across queries, and concurrent process tests.

- [ ] **Step 5: Prove the no-engagement boundary**

Runtime fakes must fail if any `like`, `favorite`, `follow`, `unfollow`, friendship, reply, repost, DM, or generic write method is touched. Static test scans production AST/call sites and permits only `post_tweet`, its private media upload, and `create_tweet` beneath Publisher.

- [ ] **Step 6: Verify and commit**

Run:

```bash
venv/bin/python -m pytest \
  tests/test_growth_digest.py \
  tests/test_growth_discovery.py \
  tests/test_growth_discovery_review.py \
  tests/test_growth_analytics.py \
  tests/test_x_write_safety.py -q
```

Commit:

```bash
git add modules/growth_digest.py modules/twitter_client.py modules/database.py \
  modules/growth_discovery.py tests/fakes.py tests/test_growth_digest.py \
  tests/test_x_write_safety.py
git commit -m "feat: prepare manual growth digest"
```

---

### Task 6: Deliver the compact growth digest at 09:00 Europe/Rome

**Files:**
- Modify: `modules/telegram_controller.py`
- Modify: `main.py:150-380, 607-650, 660-770`
- Modify: `config.py:245-265, 330-405`
- Modify: `.env.example`
- Create: `tests/test_growth_digest_telegram.py`
- Modify: `tests/test_end_to_end_dry_run.py`
- Modify: `tests/test_main_startup.py`

**Configuration:**

```dotenv
GROWTH_DIGEST_TIME=09:00
GROWTH_ACCOUNT_SUGGESTION_LIMIT=5
GROWTH_POST_SUGGESTION_LIMIT=10
GROWTH_POST_QUERY_BUDGET=2
GROWTH_SUGGESTION_COOLDOWN_DAYS=30
GROWTH_UNFOLLOW_REVIEW_DAYS=14
```

Parse time and positive integers strictly. Accept account limit `1..5`, post limit `1..10`, and query budget `1..2`; enforce exact release values 30/14 so malformed configuration cannot silently broaden automation/cost. Scheduler timezone is explicitly `Europe/Rome`, independent of the US publication audience timezone.

- [ ] **Step 1: Write RED Telegram digest tests**

Assert the 09:00 job sends one compact message with counts and at most three navigation buttons: `Account`, `Post`, `Da rivalutare`. Empty unchanged scheduled runs are silent; explicit `/growth` shows `Nessun nuovo suggerimento`.

Detail rules:

- account: safe metrics/reason + URL button `Apri account su X` + callback `Segnala come seguito`;
- post: author/excerpt/reason + URL button `Apri post su X`; **no like callback**;
- reevaluate: account link + local callback `Segna ancora pertinente` or dismiss; **no unfollow callback**.

The followed callback updates only SQLite (`followed_manually`) and says no X action was sent. Duplicate callback is idempotent. Unauthorized chat sees nothing.

- [ ] **Step 2: Add safe URLs and callbacks**

Build URLs only from exact canonical IDs/usernames:

```python
account_url = f"https://x.com/{username}"
post_url = f"https://x.com/{username}/status/{post_id}"
```

Telegram `url` buttons open X for manual action. Callback handlers never receive or resolve arbitrary URLs. Detail data is loaded by suggestion ID + revision from SQLite.

- [ ] **Step 3: Wire the service and scheduler**

Inject `GrowthDigestService` through the existing complete dependency mapping; partial/falsy dependency maps remain fail-closed. Replace the current multi-card 11:00 discovery send with:

```python
def growth_digest_cycle(self, now=None):
    current = self._now() if now is None else now
    digest = self.growth_digest.build(current)
    return self.telegram_controller.push_growth_digest(digest, explicit=False)
```

Register one cron job at 09:00 with timezone `ZoneInfo('Europe/Rome')`, `replace_existing=True`, `max_instances=1`, and safe misfire/coalescing semantics. Remove the old 11:00 notification job so the operator receives only one daily digest; retain read budgets and `/growth` on demand.

- [ ] **Step 4: Verify no automatic engagement**

In an E2E fake, run scheduler registration, daily digest, every callback, follower re-evaluation, and repeated restart. Assert zero entries in `FakeXClient.engagement_writes`, zero `post_tweet`, and no methods matching like/follow/unfollow/reply/repost/DM.

- [ ] **Step 5: Verify and commit**

Run:

```bash
venv/bin/python -m pytest \
  tests/test_growth_digest_telegram.py \
  tests/test_end_to_end_dry_run.py \
  tests/test_main_startup.py \
  tests/test_telegram_controller.py \
  tests/test_x_write_safety.py -q
```

Commit:

```bash
git add modules/telegram_controller.py main.py config.py .env.example \
  tests/test_growth_digest_telegram.py tests/test_end_to_end_dry_run.py \
  tests/test_main_startup.py
git commit -m "feat: send daily manual growth digest"
```

---

### Task 7: End-to-end acceptance, documentation, review, and dry-run VPS rollout

**Files:**
- Modify: `README.md`
- Modify: `SETUP.md`
- Modify: `.env.example`
- Modify: `tests/test_end_to_end_dry_run.py`
- Modify: `tests/test_production_preflight.py`
- Modify: `tests/test_x_write_safety.py`
- Create: `docs/superpowers/reports/2026-08-26-telegram-editorial-control-acceptance.md` if reports are tracked; otherwise write the existing ignored SDD report path.

- [ ] **Step 1: Write the complete RED acceptance story first**

One real-SQLite/no-network test must execute:

1. authorized `/newpost` with exact English;
2. choose `Nessuna fonte` and `Nessun media`;
3. observe immediate approved/advisory queue entry despite translator 429;
4. create another post using nested new source and verified media browser;
5. `/posts` shows only the compact page; detail shows full copy and preview;
6. remove the first approved post, verify plan/media release, then restore it;
7. `/media` archives/restores one item and permanently deletes a different never-used item through double confirmation;
8. run daily growth digest and mark one account followed locally while opening only URL links for post/account actions;
9. run adaptive planning and publishing at due time;
10. with `DRY_RUN=true`, assert simulation result, exact English preserved, no X call, and queue remains recoverable according to existing simulation semantics.

Add restart checkpoints between every persisted Telegram step and two-worker variants for final post creation, removal, media deletion, and growth run.

- [ ] **Step 2: Update operator documentation**

README/SETUP/help must state plainly:

- manual copy is immediately approved and sources/media are optional;
- generated drafts still require Telegram approval;
- translation is private and advisory only for manual posts;
- `/posts` and `/media` are compact browsers;
- approved removal is recoverable, permanent media deletion is restricted;
- digest arrives at 09:00 Rome and all likes/follows/unfollows remain manual in X;
- adaptive publication uses two positions normally and three on selected days;
- the deployment remains dry-run until a separate explicit authorization changes it.

Remove stale claims that the dashboard is the required control surface or that the bot performs automated engagement.

- [ ] **Step 3: Run the full local verification gate**

Run fresh, in this order:

```bash
venv/bin/python -m pytest tests/test_end_to_end_dry_run.py \
  tests/test_production_preflight.py tests/test_x_write_safety.py -q
venv/bin/python -m pytest -q
venv/bin/python -m compileall -q main.py config.py modules tests
venv/bin/python -m pip check
git diff --check
git status --short
```

Static scans must show:

- zero like/follow/unfollow/reply/repost/DM production capabilities/callers;
- `post_tweet` is called only by Publisher;
- `create_tweet` only inside `TwitterClient.post_tweet`;
- no raw `Path.open`, `unlink`, or `os.remove` at Telegram media call sites;
- no `DRY_RUN=false` in committed configuration/docs/deploy steps.

- [ ] **Step 4: Request independent code review and fix every Critical/Important finding TDD-first**

Prepare the exact base/head review package. Review:

- manual authority does not weaken generated gates;
- empty source lists are origin-bound;
- advisory translation cannot block planning;
- callback/replay/SQLite crash boundaries;
- media root-lock order and filesystem reconciliation;
- post discard/restore vs publisher races;
- X read normalization, budgets, cooldowns, and absence of engagement writes;
- scheduler timezone and dry-run boundary;
- test honesty via controlled negative mutations.

For every accepted finding: add a failing regression, observe RED, implement the smallest fix, rerun focused + full suites, and re-review until zero Critical/Important.

- [ ] **Step 5: Commit documentation and acceptance evidence**

```bash
git add README.md SETUP.md .env.example tests/test_end_to_end_dry_run.py \
  tests/test_production_preflight.py tests/test_x_write_safety.py
git commit -m "docs: explain Telegram editorial control"
```

- [ ] **Step 6: Perform a guarded VPS rollout with backup and dry-run proof**

Before any restart:

1. connect only through the approved SSH target;
2. record current SHA/service state and create a timestamped SQLite + `.env` backup without printing secrets;
3. `git pull --ff-only origin main`;
4. install declared dependencies;
5. run `validate_config()` with output limited to booleans/non-secret status;
6. assert exact `DRY_RUN=true` and `APPROVAL_REQUIRED=true` from the loaded service environment;
7. run additive DB migrations and `PRAGMA integrity_check`;
8. restart only `flexdropin-bot`;
9. verify active service, expected HEAD, scheduler jobs, sanitized journal, and no restart loop.

Live Telegram smoke (authorized chat only):

- `/newpost` with no source/media;
- nested source add and cancel;
- verified media browsing;
- compact `/posts` detail/remove/restore;
- `/media` archive/restore using a disposable unused item (do not permanently delete user media during smoke);
- explicit `/growth` detail links;
- due dry-run simulation.

Final remote proof must state `dry_run=True`, `approval_required=True`, zero real X writes, service active, and backup paths/recovery command. Enabling real publication is explicitly outside this plan and requires a new user authorization.

---

## Final acceptance checklist

- [ ] Manual posts with 0/1/3 sources enter approved reserve exactly once.
- [ ] Manual posts invoke no generation/fact/scoring/novelty gates.
- [ ] Translation 429 cannot block or remove a manual approved post.
- [ ] Generated drafts still require eligible sources, factual grounding, score, ready translation, and approval.
- [ ] Media is optional and selections are visible verified previews.
- [ ] `/posts` sends at most eight compact rows and opens one full detail on demand.
- [ ] Approved/planned removal is recoverable and publishing/unknown/published removal fails closed.
- [ ] Archive/restore/delete media transitions are revision- and identity-bound.
- [ ] Permanent deletion cannot affect used/reserved media and survives crash/restart.
- [ ] Daily digest has at most 5 accounts, 10 posts, and 5 re-evaluations.
- [ ] Every growth action is a local decision or X URL; there is no engagement write.
- [ ] Existing adaptive 2/3-post scheduler automatically uses the approved reserve.
- [ ] Full suite, compilation, dependency, diff, migration, and security scans pass.
- [ ] VPS ends active on the intended SHA with `DRY_RUN=true` and `APPROVAL_REQUIRED=true`.
