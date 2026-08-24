# Adaptive Approved Post Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace fixed-slot draft publication with a durable seven-post approved queue, faithful Italian Telegram review translations, and two adaptive United States publication times per day.

**Architecture:** Keep `post_drafts` as the canonical safety and publication record, add queue and daily-plan tables, and introduce focused translation, timing, replenishment, and planning services. The existing `Publisher` remains the only X write boundary and gains an atomic plan-claim path; legacy fixed-slot APIs remain available during migration but are no longer scheduled.

**Tech Stack:** Python 3.11, SQLite, APScheduler 3.10, Groq Python SDK, Tweepy 4.14, Telegram Bot HTTP API, pytest, standard-library `zoneinfo` and `hashlib`.

## Global Constraints

- Keep Groq as the only AI provider; do not add Gemini, OpenAI, Qwen, or Kimi dependencies.
- Publish English `post_drafts.text` only; `translation_it` is review-only and must never reach X or `posted_tweets.text`.
- Use `America/New_York` for the audience day and DST handling; never use a fixed UTC offset.
- Create exactly two daily publication positions, one inside `08:30-11:30` ET and one inside `16:30-20:30` ET, separated by at least six hours.
- Target seven approved/planned posts, permit at most three pending reviews, and create at most four review drafts per Europe/Rome day.
- Keep `APPROVAL_REQUIRED=true`; safe defaults remain `DRY_RUN=true` and publication pause closed unless database state is exact `false`.
- Preserve source trust/expiry, claim, numeric, link, novelty, score, media identity, revision CAS, ambiguous-X no-retry, and single-X-write boundaries.
- Do not add automatic follow, unfollow, like, repost, reply, comment, or DM behavior.
- All schema migrations must be additive, serialized with `BEGIN IMMEDIATE`, idempotent, concurrency-safe, and crash-safe.
- Never log or persist raw Groq prompts, source bundles, provider response bodies, model reasoning, Telegram tokens, X credentials, or hostile exception text.
- Execute every production change test-first, retain observed RED evidence, run focused and full regressions, and commit each task separately.

---

## File Map

### New focused modules

- `modules/adaptive_timing.py`: parse publication windows and choose restart-stable cold-start or performance-weighted ET times.
- `modules/review_translation.py`: validate Groq's Italian review translation against English URLs and numeric claims.
- `modules/publication_queue.py`: replenish review drafts, build daily plan positions, rank approved drafts, and coordinate dry-run simulation.

### Existing production modules to extend

- `config.py`: strict queue, timing, timezone, cap, and grace configuration.
- `modules/ai_generator.py`: one bounded Groq translation completion method.
- `modules/fact_guard.py`: expose the canonical numeric-token extractor for translation validation without changing factual behavior.
- `modules/content_planner.py`: accept an explicit draft-generation cap instead of the fixed literal `2`.
- `modules/draft_pipeline.py`: create queue drafts and invalidate/rebuild translations after text changes.
- `modules/database.py`: additive queue/plan/claim schema and atomic queue, plan, and publication primitives.
- `modules/telegram_controller.py`: complete bilingual cards, queue-aware approval, `/posts`, and dual-time display.
- `modules/analytics.py`: produce mature owned-post timing samples.
- `modules/publisher.py`: atomically claim and finalize persisted publication plans.
- `main.py`: dependency injection, interval jobs, queue cycles, and removal of scheduled fixed-slot jobs.
- `.env.example`, `README.md`, `SETUP.md`: operator configuration and safe rollout documentation.

### Tests

- Create `tests/test_adaptive_timing.py`.
- Create `tests/test_review_translation.py`.
- Create `tests/test_approved_post_queue.py`.
- Create `tests/test_adaptive_publication.py`.
- Modify `tests/fakes.py`.
- Modify `tests/test_database_growth_schema.py`.
- Modify `tests/test_draft_pipeline.py`.
- Modify `tests/test_draft_pipeline_sqlite.py`.
- Modify `tests/test_telegram_workflows.py`.
- Modify `tests/test_publisher.py`.
- Modify `tests/test_end_to_end_dry_run.py`.
- Modify `tests/test_x_write_safety.py`.

---

### Task 1: Strict queue configuration and deterministic timing policy

**Files:**
- Create: `modules/adaptive_timing.py`
- Create: `tests/test_adaptive_timing.py`
- Modify: `config.py:65-89`
- Modify: `.env.example:12-35`
- Test: `tests/test_end_to_end_dry_run.py`

**Interfaces:**
- Produces: `TimeWindow.parse(value: str) -> TimeWindow`.
- Produces: `TimingSample(scheduled_for, measured_at, impressions, engagements)`.
- Produces: `DailyTimingDecision(times, bucket_ids, reason)`.
- Produces: `AdaptiveTimingPolicy.choose(local_date, installation_id, samples) -> DailyTimingDecision`.
- Produces config constants `POSTS_PER_DAY`, `APPROVED_QUEUE_TARGET`, `PENDING_REVIEW_LIMIT`, `DRAFT_GENERATION_DAILY_CAP`, `AUDIENCE_TIMEZONE`, `MORNING_WINDOW`, `EVENING_WINDOW`, `MIN_POST_GAP_HOURS`, `ADAPTIVE_TIMING_MIN_POSTS`, `ADAPTIVE_WEEKDAY_MIN_POSTS`, and `PUBLICATION_PLAN_GRACE_MINUTES`.

- [ ] **Step 1: Write failing strict-config tests**

Add table-driven tests that reload `config` with the approved defaults and reject malformed values. Include exact assertions:

```python
def test_queue_defaults_are_safe(monkeypatch):
    for name in (
        "POSTS_PER_DAY", "APPROVED_QUEUE_TARGET", "PENDING_REVIEW_LIMIT",
        "DRAFT_GENERATION_DAILY_CAP", "AUDIENCE_TIMEZONE",
        "MORNING_WINDOW", "EVENING_WINDOW", "MIN_POST_GAP_HOURS",
        "ADAPTIVE_TIMING_MIN_POSTS", "ADAPTIVE_WEEKDAY_MIN_POSTS",
        "PUBLICATION_PLAN_GRACE_MINUTES",
    ):
        monkeypatch.delenv(name, raising=False)
    loaded = importlib.reload(config)
    assert loaded.POSTS_PER_DAY == 2
    assert loaded.APPROVED_QUEUE_TARGET == 7
    assert loaded.PENDING_REVIEW_LIMIT == 3
    assert loaded.DRAFT_GENERATION_DAILY_CAP == 4
    assert loaded.AUDIENCE_TIMEZONE == "America/New_York"
    assert loaded.MORNING_WINDOW == "08:30-11:30"
    assert loaded.EVENING_WINDOW == "16:30-20:30"
    assert loaded.MIN_POST_GAP_HOURS == 6
    assert loaded.ADAPTIVE_TIMING_MIN_POSTS == 30
    assert loaded.ADAPTIVE_WEEKDAY_MIN_POSTS == 90
    assert loaded.PUBLICATION_PLAN_GRACE_MINUTES == 90
```

Reject zero, negative, boolean-like, non-integer, inverted windows, overlapping
windows, invalid IANA zones, `POSTS_PER_DAY != 2`, queue target below two,
pending limit above queue target, and a minimum gap impossible for the windows.

- [ ] **Step 2: Run the config tests and record RED**

Run:

```bash
venv/bin/python -m pytest tests/test_adaptive_timing.py -k config -v
```

Expected: collection or assertions fail because the constants and parser do not exist.

- [ ] **Step 3: Write failing cold-start, DST, and learning tests**

Create exact deterministic tests using `ZoneInfo("America/New_York")`:

```python
def test_cold_start_is_stable_inside_two_windows():
    policy = AdaptiveTimingPolicy(
        audience_timezone="America/New_York",
        morning_window="08:30-11:30",
        evening_window="16:30-20:30",
        minimum_gap_hours=6,
        timing_min_posts=30,
        weekday_min_posts=90,
    )
    first = policy.choose(date(2026, 8, 24), "install-1", [])
    second = policy.choose(date(2026, 8, 24), "install-1", [])
    assert first == second
    assert first.reason == "cold_start"
    assert len(first.times) == 2
    assert time(8, 30) <= first.times[0].timetz().replace(tzinfo=None) <= time(11, 30)
    assert time(16, 30) <= first.times[1].timetz().replace(tzinfo=None) <= time(20, 30)
    assert first.times[1] - first.times[0] >= timedelta(hours=6)
```

Also assert different dates usually select different minutes, the same local
wall windows survive March and November DST transitions, 29 mature samples
remain cold start, 30 mature samples with at least three observations in a
bucket produce `performance_weighted`, samples under 24 hours old are ignored,
and malformed/future/negative samples fail closed.

- [ ] **Step 4: Run the timing tests and record RED**

Run:

```bash
venv/bin/python -m pytest tests/test_adaptive_timing.py -v
```

Expected: FAIL because `modules.adaptive_timing` and its types do not exist.

- [ ] **Step 5: Implement strict configuration**

Add bounded parsers in `config.py`. Parse integer fields with
`type(value) is int` semantics after conversion, validate the IANA timezone by
constructing `ZoneInfo`, and validate windows through `TimeWindow.parse` during
`validate_config()`. Keep raw strings in config so dependency-injected tests can
construct policies directly.

Add the exact approved values to `.env.example`; remove `CONTENT_SLOTS` and
`DRAFT_LEAD_MINUTES` from the active example but leave their Python constants
temporarily for legacy API tests.

- [ ] **Step 6: Implement `AdaptiveTimingPolicy`**

Use frozen dataclasses and SHA-256, never Python's randomized `hash()`:

```python
@dataclass(frozen=True)
class TimeWindow:
    start: time
    end: time

@dataclass(frozen=True)
class TimingSample:
    scheduled_for: datetime
    measured_at: datetime
    impressions: int
    engagements: int

@dataclass(frozen=True)
class DailyTimingDecision:
    times: tuple[datetime, datetime]
    bucket_ids: tuple[str, str]
    reason: str
```

Divide each window into bounded 90-minute buckets. Derive deterministic bytes
from `sha256(f"{installation_id}:{local_date.isoformat()}:{position}")`.
Cold start selects one minute from each full window. Learning computes a
smoothed rate `(engagements + 1) / (impressions + 100)` only for mature valid
samples, requires three observations in a bucket, and uses 20 percent
deterministic exploration. Weekday weighting remains disabled until 90 mature
samples. Return aware ET datetimes and reject any decision violating windows or
the six-hour gap.

- [ ] **Step 7: Run focused tests and regressions**

Run:

```bash
venv/bin/python -m pytest tests/test_adaptive_timing.py tests/test_end_to_end_dry_run.py -v
```

Expected: PASS, with existing fixed-slot tests still green until Task 8 changes scheduler expectations.

- [ ] **Step 8: Commit Task 1**

```bash
git add config.py .env.example modules/adaptive_timing.py tests/test_adaptive_timing.py tests/test_end_to_end_dry_run.py
git commit -m "feat: add adaptive US timing policy"
```

---

### Task 2: Additive queue, replenishment, and daily-plan SQLite schema

**Files:**
- Modify: `modules/database.py:110-650,1351-2490`
- Modify: `tests/test_database_growth_schema.py`
- Create: `tests/test_approved_post_queue.py`
- Test: `tests/test_draft_pipeline_sqlite.py`
- Test: `tests/test_media_lifecycle.py`

**Interfaces:**
- Produces: `PublicationPlanClaim(plan_id: int, plan_revision: int, draft_id: int, draft_revision: int, scheduled_for: str, claim_token: str)` frozen dataclass.
- Produces: `Database.ensure_editorial_queue(draft_id) -> Optional[dict]`.
- Produces: `Database.get_queue_draft(draft_id) -> Optional[dict]`.
- Produces: `Database.get_queue_counts(operator_date, timezone_name) -> dict`.
- Produces: `Database.save_review_translation(draft_id, expected_draft_revision, text_it) -> bool`.
- Produces: `Database.invalidate_review_translation(draft_id, expected_draft_revision) -> bool`.
- Produces: `Database.approve_queued_draft_atomic(draft_id, expected_draft_revision, expected_queue_revision, approved_by, approved_at) -> bool`.
- Produces: `Database.claim_replenishment(operator_date, max_daily, now, ttl_seconds=1800) -> Optional[dict]`.
- Produces: `Database.complete_replenishment_claim(token, draft_id) -> bool` and `release_replenishment_claim(token) -> bool`.
- Produces: `Database.create_or_get_publication_positions(local_date, decision, now) -> list[dict]`.
- Produces: `Database.list_approved_queue(now) -> list[dict]`.
- Produces: `Database.assign_publication_plan_atomic(plan_id, draft_id, expected_draft_revision, reason) -> bool`.
- Produces: `Database.mark_publication_plan_simulated(plan_id, expected_revision) -> bool`.

- [ ] **Step 1: Write schema and migration RED tests**

Assert exact columns, constraints, and migration behavior. The new schema is:

```sql
CREATE TABLE editorial_queue (
    draft_id INTEGER PRIMARY KEY REFERENCES post_drafts(id),
    translation_it TEXT,
    translation_status TEXT NOT NULL CHECK (
        translation_status IN ('pending', 'ready', 'failed', 'invalidated')
    ),
    review_ready_at TEXT,
    approved_queue_at TEXT,
    not_before TEXT,
    blocked_reason TEXT,
    revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE publication_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    local_date TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position IN (1, 2)),
    scheduled_for TEXT NOT NULL UNIQUE,
    draft_id INTEGER REFERENCES post_drafts(id),
    draft_revision INTEGER,
    status TEXT NOT NULL CHECK (
        status IN ('open', 'planned', 'publishing', 'published',
                   'simulated', 'skipped', 'unknown')
    ),
    selection_reason_json TEXT NOT NULL DEFAULT '{}',
    claim_token TEXT,
    published_tweet_id TEXT,
    revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(local_date, position)
);

CREATE TABLE draft_replenishment_claims (
    token TEXT PRIMARY KEY,
    operator_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('claimed', 'completed', 'released')
    ),
    claimed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    draft_id INTEGER REFERENCES post_drafts(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

Add a partial unique index on `publication_plans(draft_id)` for non-null
`draft_id` in `planned`, `publishing`, and `unknown` states. Assert concurrent
constructors, a hard crash between DDL and marker, and a second migration leave
`PRAGMA integrity_check = 'ok'` and exactly one copy of each table/index.

- [ ] **Step 2: Run schema tests and record RED**

Run:

```bash
venv/bin/python -m pytest tests/test_database_growth_schema.py tests/test_approved_post_queue.py -k "schema or migration" -v
```

Expected: FAIL because the tables and methods do not exist.

- [ ] **Step 3: Implement serialized additive migration**

Create all three tables and indexes inside the existing schema migration's
`BEGIN IMMEDIATE` transaction. Store a versioned `bot_state` marker only in the
same transaction. Use `CREATE TABLE/INDEX IF NOT EXISTS`; never rebuild or drop
`post_drafts`, sources, media, tweets, evaluations, or analytics tables.

Define the publication token once in `modules/database.py`:

```python
@dataclass(frozen=True)
class PublicationPlanClaim:
    plan_id: int
    plan_revision: int
    draft_id: int
    draft_revision: int
    scheduled_for: str
    claim_token: str
```

For each nonterminal legacy draft, insert one `editorial_queue` row with
`translation_status='pending'` using `INSERT OR IGNORE`. Do not change its draft
status, approval fields, revision, media reservation, or legacy slot.

- [ ] **Step 4: Write atomic primitive RED tests**

Cover:

```python
def test_queue_translation_and_approval_are_revision_bound(tmp_path):
    db, draft = queue_fixture(tmp_path)
    queue = db.ensure_editorial_queue(draft["id"])
    assert queue["translation_status"] == "pending"
    assert db.save_review_translation(draft["id"], draft["revision"], "Traduzione")
    current = db.get_queue_draft(draft["id"])
    assert db.approve_queued_draft_atomic(
        draft["id"], draft["revision"], current["queue_revision"],
        "floriano", "2026-08-24T12:00:00+00:00",
    )
    assert db.get_post_draft(draft["id"])["status"] == "approved"
```

Also prove: missing translation rejects approval; stale draft or queue revision
rejects; source revocation rolls back approval; two approval workers yield one
winner; queue approval ignores an expired legacy `intended_slot`; invalidation
clears Italian text and approval atomically; queue counts use Europe/Rome dates;
claim cap is four completed/active claims; released and expired claims are
reclaimable; crash before commit consumes no cap; plan creation is stable; plan
assignment is exact-revision, one-draft/one-plan, and source/media revalidated.

- [ ] **Step 5: Run primitive tests and record RED**

Run:

```bash
venv/bin/python -m pytest tests/test_approved_post_queue.py -k "queue or claim or plan" -v
```

Expected: FAIL on missing database APIs.

- [ ] **Step 6: Implement queue and plan primitives**

Decode queue rows into allowlisted dictionaries and reject malformed persisted
JSON, booleans-as-IDs, naive timestamps, future approval timestamps, duplicate
source IDs, and invalid status values. Use existing `_post_draft_mutation_lock`
whenever draft/media state participates. Acquire media roots before
`BEGIN IMMEDIATE`; do not wait for filesystem locks while holding a SQLite
write transaction.

Make translation save and invalidation exact on both English draft revision and
queue revision. Approval updates `post_drafts.status`, approval fields, draft
revision, queue approval time, and queue revision in one transaction.

Use a 30-minute replenishment claim TTL. Count `claimed` non-expired plus
`completed` rows toward the daily cap. Complete/release by exact token CAS.

Create plan positions from a validated `DailyTimingDecision` and store reason
JSON with only `reason`, `bucket_id`, and bounded sample count/score fields.

- [ ] **Step 7: Run database, media, and draft regressions**

Run:

```bash
venv/bin/python -m pytest tests/test_approved_post_queue.py tests/test_database_growth_schema.py tests/test_draft_pipeline_sqlite.py tests/test_media_lifecycle.py -v
```

Expected: PASS with no legacy slot, media-lock, or migration regression.

- [ ] **Step 8: Commit Task 2**

```bash
git add modules/database.py tests/test_database_growth_schema.py tests/test_approved_post_queue.py tests/test_draft_pipeline_sqlite.py tests/test_media_lifecycle.py
git commit -m "feat: persist approved editorial queue"
```

---

### Task 3: Faithful Groq Italian review translation

**Files:**
- Create: `modules/review_translation.py`
- Create: `tests/test_review_translation.py`
- Modify: `modules/ai_generator.py:180-235`
- Modify: `modules/fact_guard.py:20-90`
- Modify: `tests/test_editorial_scoring.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: canonical numeric tokens from `modules.fact_guard.numeric_tokens(text) -> set[str]`.
- Produces: `AIGenerator.translate_review_copy(english_text: str) -> Optional[str]`.
- Produces: `ReviewTranslation(text_it: str)` frozen dataclass.
- Produces: `ReviewTranslator.translate(english_text: str) -> Optional[ReviewTranslation]`.

- [ ] **Step 1: Write translation validation RED tests**

Create a fake generator that returns controlled Italian text. Test the exact
contract:

```python
def test_translation_preserves_numbers_ranges_scales_and_urls():
    translator = ReviewTranslator(FakeTranslatorGenerator(
        "I ricavi sono aumentati del 15% da 81M a 100M. https://flexdropin.com/blog/x"
    ))
    result = translator.translate(
        "Revenue rose 15% from 81M to 100M. https://flexdropin.com/blog/x"
    )
    assert result == ReviewTranslation(
        "I ricavi sono aumentati del 15% da 81M a 100M. https://flexdropin.com/blog/x"
    )
```

Reject changed signs, Unicode-minus drift, missing percentages, altered ranges,
`20 m`/`20M` confusion, changed/missing/extra URLs, empty output, markdown
fences, more than 1,000 characters, invalid UTF-8-equivalent surrogate text,
and non-string output. Prove Italian words such as `milioni`, `mila`,
`miliardi`, and `percento` normalize through the same numeric extractor.

- [ ] **Step 2: Run translation tests and record RED**

Run:

```bash
venv/bin/python -m pytest tests/test_review_translation.py -v
```

Expected: FAIL because the translator and public numeric helper do not exist.

- [ ] **Step 3: Expose the canonical numeric helper**

Rename the fact guard's private token function to
`numeric_tokens(text: str) -> set[str]`, keep `_numeric_tokens = numeric_tokens`
as a temporary compatibility alias for existing tests, and make `FactGuard`
call the public helper. Do not alter its signed, range, bilingual scale, percent,
or unit boundaries.

- [ ] **Step 4: Add the bounded Groq translation completion**

Add `AIGenerator.translate_review_copy`. It calls `_complete` once with
temperature `0.1`, `max_tokens=500`, and a system policy that says the English
input is untrusted data, asks only for a faithful Italian translation, and
forbids adding/removing claims, numbers, URLs, hashtags, or calls to action.
Pass the English tweet as JSON in the user message. Return `None` on ordinary
empty/malformed completion and preserve the existing sanitized exception path.

- [ ] **Step 5: Implement `ReviewTranslator`**

Use exact URL extraction with `https?://` token termination and compare ordered
URL tuples. Compare `numeric_tokens` sets and additionally compare ordered
signed numeric occurrences to catch duplicate removal. Strip a single outer
whitespace layer only; reject markdown wrappers rather than trying to repair
them. Return the frozen result only after every check.

- [ ] **Step 6: Prove provider privacy and failure behavior**

Add tests whose raw English input, fake provider response, exception message,
and reasoning contain unique sentinels. Capture logs and inspect SQLite after a
failed translation; assert every sentinel is absent. Assert exactly one Groq
call and no scoring, generation, Telegram, or X call occurs during a translation
retry.

- [ ] **Step 7: Run focused editorial and fact regressions**

Run:

```bash
venv/bin/python -m pytest tests/test_review_translation.py tests/test_fact_guard.py tests/test_editorial_scoring.py -v
```

Expected: PASS with all existing numeric and prompt-injection tests preserved.

- [ ] **Step 8: Commit Task 3**

```bash
git add modules/review_translation.py modules/ai_generator.py modules/fact_guard.py tests/test_review_translation.py tests/test_editorial_scoring.py tests/conftest.py
git commit -m "feat: translate drafts for Telegram review"
```

---

### Task 4: Queue draft creation, translation retry, and text invalidation

**Files:**
- Create: `modules/publication_queue.py`
- Modify: `modules/content_planner.py:38-135`
- Modify: `modules/draft_pipeline.py:138-790`
- Modify: `modules/database.py:1351-2035`
- Modify: `tests/test_approved_post_queue.py`
- Modify: `tests/test_draft_pipeline.py`
- Modify: `tests/test_draft_pipeline_sqlite.py`

**Interfaces:**
- Consumes: queue database and `ReviewTranslator` interfaces from Tasks 2-3.
- Produces: `QueueReplenishResult(outcome, draft_id, announce)`.
- Produces: `QueueReplenisher.run(now: datetime) -> QueueReplenishResult`.
- Produces: `QueueReplenisher.retry_pending_translations(now: datetime, limit=3) -> list[int]`.
- Produces: `DraftPipeline.create_for_queue_with_outcome(anchor) -> tuple[Optional[dict], str]`.
- Produces: `DraftPipeline.approve_queue(draft_id, approved_by) -> bool`.

- [ ] **Step 1: Write replenishment RED tests**

Use real SQLite plus fake planner, generator, translator, and media matcher.
Assert:

```python
result = replenisher.run(now)
assert result.outcome == "created"
assert db.get_queue_draft(result.draft_id)["translation_status"] == "ready"
assert result.announce is True
```

Add cases for approved/planned count seven, pending count three, daily completed
claim cap four, one draft per invocation, rejected generation releasing the
claim, translation failure retaining a pending draft with no Telegram card,
retry translating without regenerating, two threads and two spawned processes
sending one card, and crash/restart reclaiming only an expired claim.

- [ ] **Step 2: Run replenishment tests and record RED**

Run:

```bash
venv/bin/python -m pytest tests/test_approved_post_queue.py -k replenish -v
```

Expected: FAIL because `QueueReplenisher` and queue pipeline methods are absent.

- [ ] **Step 3: Make the content planner cap explicit**

Change the signature to:

```python
def plan(self, intended_slot: datetime, daily_draft_cap: int = 2) -> Optional[ContentPlan]:
```

Validate `daily_draft_cap` as an exact positive integer and replace the literal
`>= 2`. Legacy calls retain two; `create_for_queue_with_outcome` passes four.
Keep source rotation, portfolio, and link decisions unchanged.

- [ ] **Step 4: Implement queue draft pipeline methods**

`create_for_queue_with_outcome` reuses `_prepare` and `_persist`, then calls
`ensure_editorial_queue` only for the exact created/existing draft. It never
sends Telegram. `approve_queue` reads a merged queue snapshot and delegates to
`approve_queued_draft_atomic`; it does not parse or compare the legacy slot.

Update regenerate, edit, and atomic Telegram edit replacement so the replacement
gets a queue row in `translation_status='pending'` in the same SQLite
transaction. The superseded draft retains its prior translation for audit.
Text replacement must never inherit approval or Italian text.

- [ ] **Step 5: Implement `QueueReplenisher`**

Define its bounded result before the service:

```python
@dataclass(frozen=True)
class QueueReplenishResult:
    outcome: str
    draft_id: Optional[int]
    announce: bool
```

The service performs this exact sequence:

1. validate aware `now` and read queue counts;
2. return `queue_full` or `pending_full` without a claim when applicable;
3. claim one Europe/Rome daily replenishment token;
4. derive a unique aware anchor from claim creation time plus stable claim
   ordinal;
5. call `create_for_queue_with_outcome(anchor)`;
6. release on rejection or systemic exception;
7. attach the best media only for a newly created draft;
8. translate and save by exact draft revision;
9. complete the claim with the draft ID;
10. return `announce=True` only to the process that saved the ready exact row.

The service never owns Telegram. The orchestration cycle sends the returned
draft ID only when `announce=True`, avoiding a dependency cycle between the
queue service and Telegram controller. A failed Telegram delivery leaves the
durable ready draft discoverable through `/posts` and is not retried as an
unsolicited duplicate notification.

Sanitize the result to allowlisted outcomes: `created`, `existing`,
`queue_full`, `pending_full`, `daily_cap`, `generation_rejected`,
`translation_pending`, or `failed`.

- [ ] **Step 6: Add translation retry and mutation tests**

Prove retry calls only `ReviewTranslator`, card delivery happens once after
ready persistence, edit/regenerate invalidates translation and approval,
media-only attach/detach preserves both texts while incrementing the binding
revision, and a stale translator result cannot attach to changed English text.

- [ ] **Step 7: Run focused pipeline, SQLite, and media regressions**

Run:

```bash
venv/bin/python -m pytest tests/test_approved_post_queue.py tests/test_draft_pipeline.py tests/test_draft_pipeline_sqlite.py tests/test_media_lifecycle.py -v
```

Expected: PASS, including legacy `create_for_slot` compatibility.

- [ ] **Step 8: Commit Task 4**

```bash
git add modules/publication_queue.py modules/content_planner.py modules/draft_pipeline.py modules/database.py tests/test_approved_post_queue.py tests/test_draft_pipeline.py tests/test_draft_pipeline_sqlite.py tests/test_media_lifecycle.py
git commit -m "feat: replenish bilingual review queue"
```

---

### Task 5: Complete bilingual Telegram cards and queue approval

**Files:**
- Modify: `modules/telegram_controller.py:268-451,1009-1103`
- Modify: `tests/test_telegram_workflows.py`
- Modify: `tests/fakes.py`

**Interfaces:**
- Consumes: merged queue draft dictionaries from `Database.get_queue_draft`.
- Consumes: `DraftPipeline.approve_queue` and `QueueReplenisher.retry_pending_translations`.
- Produces: Telegram card rendering with complete English/Italian text and queue status.

- [ ] **Step 1: Write bilingual card RED tests**

Add tests for no media, photo, video, document, and 4,096-character pressure.
Assert exact labels and complete texts:

```python
controller._send_draft_card("42", queue_draft)
joined = "\n".join(call[1] for call in api.sent_messages)
assert "Tweet da pubblicare" in joined
assert queue_draft["text"] in joined
assert "Traduzione italiana — solo per revisione" in joined
assert queue_draft["translation_it"] in joined
assert api.sent_media[0].caption == "Anteprima media bozza #1"
```

Assert the approval markup appears only when `translation_status == 'ready'`;
pending/failed translation cards expose Retry translation, Edit, Regenerate,
and Discard but no Approve.

- [ ] **Step 2: Run Telegram tests and record RED**

Run:

```bash
venv/bin/python -m pytest tests/test_telegram_workflows.py -k "translation or bilingual or queue_card" -v
```

Expected: FAIL because current cards contain only English and slot metadata.

- [ ] **Step 3: Implement message budgeting without text truncation**

Replace the single concatenated card with a renderer that produces a bounded
list of messages. Send media first. Send complete English and Italian bodies in
separate messages when necessary. Drop optional metadata fields in this order:
selection explanation, source labels beyond three, per-axis scores, media
description. Never truncate either complete review text. Attach callback markup
to the final metadata/control message.

Display `scheduled_for` in both ET and Europe/Rome only for planned posts.
Pending review cards say `non ancora pianificato`; approved cards show queue
position when available.

- [ ] **Step 4: Make callbacks queue-aware**

For `approve`, call `approve_queue` when an editorial queue row exists; preserve
legacy approval only for a legacy row. Add callback `draft:retry_translation:<id>`
which performs a bounded retry through the injected queue service and redisplays
the exact current row. Edit/regenerate redisplay only after the replacement
translation becomes ready. Keep callback answer-once, replay claims, session
CAS, and source/media boundaries unchanged.

- [ ] **Step 5: Update `/posts` and `/status`**

Return exact counts for translation pending, review pending, approved available,
today planned, blocked, and recent published. Render at most 50 detail cards but
always show complete aggregate counts. Add queue target `7`, publication target
`2`, audience timezone, and both next planned times to `/status` without
exposing internal database paths or provider errors.

- [ ] **Step 6: Prove Italian text cannot leak to transport fakes**

Use distinct English/Italian sentinels. Exercise approve, edit, regenerate,
media attach, `/posts`, and planned display. Assert Italian appears in Telegram
only, never in fake X calls, media captions beyond the bounded label, source
rows, draft evaluation details, or error events.

- [ ] **Step 7: Run Telegram transport/workflow regressions**

Run:

```bash
venv/bin/python -m pytest tests/test_telegram_controller.py tests/test_telegram_workflows.py tests/test_media_lifecycle.py -v
```

Expected: PASS with callback replay, download, authorization, and logging safety intact.

- [ ] **Step 8: Commit Task 5**

```bash
git add modules/telegram_controller.py tests/test_telegram_workflows.py tests/fakes.py
git commit -m "feat: review bilingual drafts in Telegram"
```

---

### Task 6: Mature timing analytics and deterministic daily content planning

**Files:**
- Modify: `modules/database.py:2572-2636,4280-4352`
- Modify: `modules/analytics.py:211-310`
- Modify: `modules/publication_queue.py`
- Create: `tests/test_adaptive_publication.py`
- Modify: `tests/test_growth_analytics.py`
- Modify: `tests/test_approved_post_queue.py`

**Interfaces:**
- Produces: `Database.get_publication_timing_samples(now, min_age_hours=24) -> list[TimingSample]`.
- Produces: `PerformanceAnalyzer.timing_samples(now) -> list[TimingSample]`.
- Produces: `PublicationPlanner.ensure_day(now) -> list[dict]`.
- Produces: `PublicationPlanner.reconcile(now) -> list[dict]`.

- [ ] **Step 1: Write mature-metrics RED tests**

Insert posted tweets and metrics around the 24-hour boundary. Assert only rows
with exact decimal tweet IDs, aware nonfuture timestamps, nonnegative metrics,
`impressions >= engagements`, and age at least 24 hours become `TimingSample`.
Reject empty tweet IDs, ghost/unconfirmed rows, bool metrics, malformed
timestamps, future metrics, rollback clocks, duplicate tweet IDs, and rows with
missing performance.

- [ ] **Step 2: Run analytics tests and record RED**

Run:

```bash
venv/bin/python -m pytest tests/test_growth_analytics.py tests/test_adaptive_publication.py -k timing -v
```

Expected: FAIL on missing timing-sample APIs.

- [ ] **Step 3: Implement fail-closed timing sample projection**

Join `posted_tweets` with the existing metrics table using exact tweet ID. Keep
only allowlisted fields needed by `TimingSample`; no tweet text, source data, or
provider metadata leaves the database method. Convert timestamps to UTC-aware
then let `AdaptiveTimingPolicy` convert to ET.

- [ ] **Step 4: Write daily plan selection RED tests**

Construct seven approved queue drafts covering expiring/nonexpiring sources,
scores, categories, links, and media. Assert:

- `ensure_day` creates exactly two stable open positions;
- two concurrent workers return identical IDs/times;
- restart returns the same rows;
- `reconcile` assigns at most one draft per position;
- an expiring-but-valid source outranks a nonexpiring source;
- then higher score wins;
- category diversity changes the second choice;
- approval age and draft ID break final ties;
- a source expiring before `scheduled_for + safety_margin` is blocked;
- revoked source, malformed score, stale translation, text revision, reserved
  media mismatch, weekly link-budget violation, `not_before`, or duplicate active
  plan excludes the draft;
- raw source text and translations are absent from `selection_reason_json`.

- [ ] **Step 5: Run plan tests and record RED**

Run:

```bash
venv/bin/python -m pytest tests/test_adaptive_publication.py -k "ensure_day or reconcile or selection" -v
```

Expected: FAIL because `PublicationPlanner` is not implemented.

- [ ] **Step 6: Implement `PublicationPlanner`**

Inject database, timing policy, timing-sample provider, clock, audience timezone,
installation ID provider, source-expiry safety margin, and dry-run flag.

`ensure_day` obtains/creates the stable installation ID with atomic
`compare_and_set_state`, asks the policy for two times, and creates plan rows.
`reconcile` re-reads open future positions and approved queue candidates, applies
the exact ranking from the design, and assigns with revision CAS. Treat the
first planned selection as part of the portfolio before selecting the second.

Allow only these reason keys: `source_urgency`, `score`, `category_diversity`,
`format_diversity`, `approval_age`, `timing_reason`, and `timing_bucket`. Bound
numeric values and reject any string containing draft/source text.

- [ ] **Step 7: Add dry-run plan simulation tests**

At due time with `dry_run=True`, mark the row `simulated`, retain draft status
`approved`, preserve media reservation, perform zero X/media calls, and prefer a
different eligible draft on the next dry-run day. Prove two dry-run days do not
consume the seven-post reserve.

- [ ] **Step 8: Run analytics, queue, and timing regressions**

Run:

```bash
venv/bin/python -m pytest tests/test_adaptive_timing.py tests/test_adaptive_publication.py tests/test_growth_analytics.py tests/test_approved_post_queue.py -v
```

Expected: PASS.

- [ ] **Step 9: Commit Task 6**

```bash
git add modules/database.py modules/analytics.py modules/publication_queue.py tests/test_adaptive_publication.py tests/test_growth_analytics.py tests/test_approved_post_queue.py
git commit -m "feat: plan adaptive daily publications"
```

---

### Task 7: Atomic planned publication through the existing Publisher

**Files:**
- Modify: `modules/database.py:2081-2474`
- Modify: `modules/publisher.py:35-337`
- Modify: `modules/publication_queue.py`
- Modify: `tests/test_publisher.py`
- Modify: `tests/test_adaptive_publication.py`
- Modify: `tests/test_media_lifecycle.py`
- Modify: `tests/test_x_write_safety.py`

**Interfaces:**
- Consumes: assigned persisted publication plans from Task 6.
- Produces: `Database.claim_due_publication_plan(plan_id, expected_plan_revision, now, grace_minutes) -> Optional[tuple[dict, PostDraftPublicationClaim, PublicationPlanClaim]]`.
- Produces: atomic plan-aware finalize, restore, fail, and unknown database transitions.
- Produces: `Publisher.publish_plan(plan_id, now=None) -> PublishResult`.
- Produces: `PublicationPlanner.publish_due(now) -> list[PublishResult]`.

- [ ] **Step 1: Write atomic plan-claim RED tests**

Use real SQLite and two databases. Assert one worker transitions exact
`planned -> publishing` and exact draft `approved -> publishing` in the same
transaction. Stale plan revision, stale draft revision, early time, time after
90-minute grace, pause, source revocation, changed translation readiness,
second active plan, and invalid media all reject before X.

Add hard-crash subprocess tests before commit and after committed claim. Restart
must expose either both rows unclaimed or both rows publishing with the same
claim token; never one claimed and one approved.

- [ ] **Step 2: Run claim tests and record RED**

Run:

```bash
venv/bin/python -m pytest tests/test_adaptive_publication.py -k plan_claim -v
```

Expected: FAIL on missing plan publication claim APIs.

- [ ] **Step 3: Implement plan-aware database transitions**

Use the exact `PublicationPlanClaim` fields defined in Task 2. Discover media
roots outside a write
transaction, acquire sorted roots, then `BEGIN IMMEDIATE` and re-read the exact
plan, draft, queue, sources, and reservation. Transition plan and draft in one
commit.

Final success must atomically update plan `published`, draft `published`, media
`used`, `posted_tweets`, and audit data. Definite pre-X failure restores plan to
`planned` only inside grace or marks `skipped` and returns draft to `approved`.
Ambiguous X marks plan `unknown` and draft `publication_unknown` together.
No terminal path may hold a media root while acquiring a second root or waiting
on network I/O.

- [ ] **Step 4: Write Publisher transport RED tests**

Cover English and Italian sentinels:

```python
result = publisher.publish_plan(plan_id, now=scheduled_for)
assert result.status == "published"
assert x_client.calls == [{"text": english_text, "media": None}]
assert italian_text not in repr(x_client.calls)
assert db.get_publication_plan(plan_id)["status"] == "published"
assert db.get_post_draft(draft_id)["published_tweet_id"] == "123456"
```

Also test strict tweet ID, timeout/connection unknown, definite X rejection,
pause after media upload but before create_tweet, context-exit failure after X,
database trigger rollback, two-worker publication, late grace, dry run, and
legacy A+B media reservations without deadlock.

- [ ] **Step 5: Run Publisher tests and record RED**

Run:

```bash
venv/bin/python -m pytest tests/test_publisher.py tests/test_adaptive_publication.py -k "publish_plan or planned_publication" -v
```

Expected: FAIL because `Publisher.publish_plan` is absent.

- [ ] **Step 6: Implement `Publisher.publish_plan`**

Keep legacy `publish` unchanged for compatibility. The new method validates
aware `now`, reads exact plan revision, checks canonical pause and `DRY_RUN`,
claims plan+draft atomically, then delegates to the existing verified media/X
transport using the claimed draft's English `text` only. Persist the transport
outcome after leaving the media lease, matching current lock ordering.

`PublicationPlanner.publish_due` processes due rows in scheduled order, stops
after the first ambiguous/systemic database outcome, and never exceeds two
successful plan rows for the ET local date.

- [ ] **Step 7: Run publisher, media, and X safety regressions**

Run:

```bash
venv/bin/python -m pytest tests/test_publisher.py tests/test_adaptive_publication.py tests/test_media_lifecycle.py tests/test_x_write_safety.py -v
```

Expected: PASS and static X mutation scan still finds only
`Publisher -> TwitterClient.post_tweet -> create_tweet`.

- [ ] **Step 8: Commit Task 7**

```bash
git add modules/database.py modules/publisher.py modules/publication_queue.py tests/test_publisher.py tests/test_adaptive_publication.py tests/test_media_lifecycle.py tests/test_x_write_safety.py
git commit -m "feat: publish claimed adaptive plans"
```

---

### Task 8: Wire interval jobs and retire fixed-slot scheduling

**Files:**
- Modify: `main.py:1-665`
- Modify: `tests/test_end_to_end_dry_run.py`
- Modify: `tests/fakes.py`
- Modify: `modules/telegram_controller.py:268-298`

**Interfaces:**
- Consumes: `QueueReplenisher`, `ReviewTranslator`, `AdaptiveTimingPolicy`, and `PublicationPlanner`.
- Produces agent cycles `queue_replenishment_cycle`, `translation_retry_cycle`, `publication_planning_cycle`, and `adaptive_publish_cycle`.
- Produces scheduler jobs with intervals 30, 30, 15, and 5 minutes respectively.

- [ ] **Step 1: Write scheduler replacement RED tests**

Replace fixed-slot expectations with an exact allowlist. Assert scheduler IDs:

```python
assert {job.id for job in agent.register_jobs()} == {
    "source_refresh",
    "queue_replenishment",
    "translation_retry",
    "publication_planning",
    "adaptive_publish",
    "growth_discovery",
    "follower_snapshot",
    "performance_metrics",
    "weekly_growth_report",
}
```

When lead discovery is enabled, add only its existing allowlisted IDs. Assert
there are no IDs beginning `draft_` or `publish_`, no `CONTENT_SLOTS` loop, and
the interval jobs use 30/30/15/5 minutes. Preserve source refresh and analytics
cron times.

- [ ] **Step 2: Run scheduler tests and record RED**

Run:

```bash
venv/bin/python -m pytest tests/test_end_to_end_dry_run.py -k "scheduler or register_jobs or queue_cycle" -v
```

Expected: FAIL because fixed-slot jobs still exist.

- [ ] **Step 3: Extend dependency injection without real-boundary fallback**

Add dependency keys for `adaptive_timing`, `review_translator`,
`queue_replenisher`, and `publication_planner`. Resolve injected falsey objects
using the existing exact-`None` resolver. A partially injected mapping must fail
before constructing Groq, X, Telegram, SQLite, or scheduler real boundaries.

Production construction uses the strict config values and passes the existing
agent clock to every service. Preserve `approval_required is True` validation.

- [ ] **Step 4: Implement safe orchestration cycles**

Each cycle catches exceptions, logs only error type through `_notify_error`, and
returns a bounded result. `adaptive_publish_cycle` reads the clock once and
passes that exact time through due selection and `Publisher.publish_plan` so a
late scheduler invocation cannot bypass grace. Shutdown event checks happen
before each service call.

- [ ] **Step 5: Register interval jobs and remove active fixed slots**

Import `IntervalTrigger` and add an `_add_interval_job` helper with timezone,
replace-existing, bounded coalescing, `max_instances=1`, and deterministic IDs.
Remove the active `for slot_time in self.content_slots` job registration.
Retain legacy `create_draft_cycle`/`publish_cycle` methods only as unscheduled
compatibility wrappers until the final rollout proves migration.

- [ ] **Step 6: Write end-to-end dry-run flow**

With real SQLite and all network boundaries faked:

1. refresh a verified owned blog source;
2. replenish and translate one English draft;
3. approve it through a Telegram callback;
4. build a seven-item approved reserve;
5. create two ET plan positions;
6. reconcile two selected drafts;
7. run both due cycles in `DRY_RUN=true`;
8. assert both plans become simulated, all drafts remain approved, Telegram
   contains complete English/Italian pairs, and X calls remain empty.

Repeat agent construction on the same SQLite file and assert no duplicate
cards, plans, or simulated events.

- [ ] **Step 7: Run orchestration, Telegram, and safety regressions**

Run:

```bash
venv/bin/python -m pytest tests/test_end_to_end_dry_run.py tests/test_telegram_workflows.py tests/test_x_write_safety.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit Task 8**

```bash
git add main.py modules/telegram_controller.py tests/test_end_to_end_dry_run.py tests/fakes.py
git commit -m "feat: schedule approved queue adaptively"
```

---

### Task 9: Crash, concurrency, privacy, and regression acceptance

**Files:**
- Modify: `tests/test_approved_post_queue.py`
- Modify: `tests/test_adaptive_publication.py`
- Modify: `tests/test_end_to_end_dry_run.py`
- Modify: `tests/test_x_write_safety.py`
- Modify: `README.md`
- Modify: `SETUP.md`
- Modify: `.env.example`

**Interfaces:**
- Consumes all production interfaces from Tasks 1-8.
- Produces complete automated acceptance evidence and operator documentation.

- [ ] **Step 1: Add multiprocessing acceptance tests**

Spawn two to eight processes on one SQLite file with barriers at these
boundaries: replenishment claim, translation save, queue approval, plan create,
plan assignment, and publication claim. Assert one committed winner, no live
thread/process after bounded joins, no database lock escapes, exact daily caps,
one Telegram card, and one X call only when `dry_run=False` is explicitly set in
the fake-only test.

- [ ] **Step 2: Add hard-crash acceptance tests**

Use subprocess `os._exit` immediately before and after commits for schema,
replenishment, translation, approval, plan creation, assignment, publication
claim, and finalization. Each restart must yield one of the documented complete
states, pass `PRAGMA integrity_check`, and never duplicate draft, plan, media
reservation, posted tweet, audit, or X intent.

- [ ] **Step 3: Add timezone and DST acceptance tests**

Cover the United States spring-forward and fall-back dates, Europe/Rome dates
that differ from ET, server clock rollback, restart between plan creation and
execution, and approval just before/after midnight ET. Assert two positions are
counted by ET date, four draft creations by Rome date, aware timestamps are
unambiguous, and both planned times remain at least six elapsed hours apart.

- [ ] **Step 4: Add privacy and mutation scans**

Inject unique sentinels into English, Italian, source metadata, Groq response,
reasoning, exception, Telegram update, and credentials. Assert only English is
present in the fake X call and posted tweet row; Italian is present only in
`editorial_queue` and Telegram. Assert raw provider/source/credential sentinels
are absent from logs, error events, plan reasons, and draft evaluations.

Run static scans that fail on new production calls matching follow, unfollow,
like, favorite, repost, retweet, reply, direct message, or any `create_tweet`
outside `TwitterClient.post_tweet`.

- [ ] **Step 5: Update operator documentation**

Document the queue counts, bilingual cards, ET/Rome times, two-post target,
seven-post reserve, `/pause`, `/resume`, dry-run behavior, translation retry,
blocked-post reasons, adaptive-learning thresholds, and the exact environment
keys. Remove instructions claiming drafts are tied to `CONTENT_SLOTS` or expire
after a five-minute fixed slot.

Add a dry-run checklist that requires two simulated US days, seven approved
posts, zero X writes, clean `/errors`, and explicit separate authorization
before changing `DRY_RUN`.

- [ ] **Step 6: Run the required focused suite**

Run:

```bash
venv/bin/python -m pytest \
  tests/test_adaptive_timing.py \
  tests/test_review_translation.py \
  tests/test_approved_post_queue.py \
  tests/test_adaptive_publication.py \
  tests/test_draft_pipeline.py \
  tests/test_draft_pipeline_sqlite.py \
  tests/test_telegram_controller.py \
  tests/test_telegram_workflows.py \
  tests/test_publisher.py \
  tests/test_media_lifecycle.py \
  tests/test_end_to_end_dry_run.py \
  tests/test_x_write_safety.py -v
```

Expected: all pass; only the known upstream Tweepy `imghdr` deprecation warning is allowed.

- [ ] **Step 7: Run the full verification gate**

Run:

```bash
venv/bin/python -m pytest -q
venv/bin/python -m compileall -q config.py main.py modules tests
venv/bin/python -m pip check
git diff --check
git status --short
```

Expected: full suite passes, compilation succeeds, no broken requirements,
diff check clean, and status contains only the intended Task 9 files before commit.

- [ ] **Step 8: Request and resolve independent code review**

Use `superpowers:requesting-code-review` on the complete implementation range.
Require explicit spec and quality verdicts. Reproduce every Critical or
Important finding with a failing test before changing production, implement the
minimal fix, rerun the focused suite and full gate, and repeat review until no
Critical or Important finding remains.

- [ ] **Step 9: Commit Task 9**

```bash
git add tests/test_approved_post_queue.py tests/test_adaptive_publication.py tests/test_end_to_end_dry_run.py tests/test_x_write_safety.py README.md SETUP.md .env.example
git commit -m "test: verify adaptive queue safety"
```

---

### Task 10: Production dry-run deployment and evidence gate

**Files:**
- No source mutation.
- Runtime backup: `/home/ubuntu/ai-x-bot/backups/bot_data-before-adaptive-queue-<timestamp>.db`
- Runtime config: `/home/ubuntu/ai-x-bot/.env`

**Interfaces:**
- Consumes the fully verified commits from Tasks 1-9.
- Produces production dry-run evidence only; it does not authorize live X publication.

- [ ] **Step 1: Confirm local release state**

Run:

```bash
git status --short
git log -10 --oneline
git show --check --oneline HEAD
```

Expected: clean worktree and all Task 1-9 commits present.

- [ ] **Step 2: Inspect production without mutation**

SSH to `ubuntu@80.225.89.115`, confirm service path and active unit, print only
the names—not values—of required environment keys, and verify current git HEAD,
SQLite path, free disk, Python version, and `PRAGMA integrity_check`. Abort on an
unknown repo path, dirty production tree, invalid database, missing Telegram or
Groq key, or `APPROVAL_REQUIRED` not exact `true`.

- [ ] **Step 3: Back up SQLite before deployment**

Stop `flexdropin-bot`, create the explicit backup directory, use SQLite's online
backup command or `.backup` against the resolved database file, verify backup
integrity, and record its exact path. Do not copy `.env`, SSH keys, media, or logs.

- [ ] **Step 4: Deploy with safe configuration**

Pull `main` with `git pull --ff-only`, install locked requirements, and set only
these non-secret queue values in `.env` while preserving existing secrets:

```dotenv
DRY_RUN=true
APPROVAL_REQUIRED=true
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

Run `config.validate_config()` and the additive migration before restarting.
Abort rather than auto-correct any malformed value.

- [ ] **Step 5: Restart and perform immediate dry-run checks**

Restart `flexdropin-bot`, require `systemctl is-active` to return `active`, and
inspect bounded logs for scheduler IDs, schema completion, sanitized errors,
and absence of fixed `draft_*`/`publish_*` jobs. Query only counts/statuses from
SQLite; do not print translations, raw sources, tokens, or credentials.

- [ ] **Step 6: Operator acceptance through Telegram**

Use `/status` and `/posts` to confirm bilingual cards, ET/Rome planned times,
pending cap three, and target reserve seven. Approve enough exact drafts to
reach seven. Verify edit/regenerate invalidates and rebuilds translation, media
preview remains correct, and `/pause` stops simulated due execution while queue
replenishment can continue.

- [ ] **Step 7: Observe two complete ET dry-run days**

For both days require exactly two stable plan positions, correct windows and
gap, two simulated outcomes, unchanged approved draft statuses, zero new X
owned posts, no `publication_unknown`, no duplicate Telegram card, and clean
database integrity. Capture only IDs, timestamps, states, counts, and bounded
reason codes.

- [ ] **Step 8: Stop at the live-publication authorization gate**

Present the two-day evidence and queue state to the operator. Do not change
`DRY_RUN=true` until the operator gives a new explicit authorization after
reviewing the exact first two English posts. A later live-enablement procedure
must retain Telegram approval and may be rolled back immediately with `/pause`
and `DRY_RUN=true`.

---

## Completion Criteria

The implementation is complete only when:

- every Task 1-9 focused test and the full suite pass;
- no Critical or Important independent-review finding remains;
- schema, concurrency, crash, DST, translation, media, and X safety probes pass;
- production runs the new scheduler for two complete ET days in dry-run;
- Telegram shows complete English and Italian review text;
- the approved reserve reaches seven without duplicate cards;
- exactly two dry-run positions are observed per ET day with zero X writes;
- production remains `DRY_RUN=true` until a separate explicit live authorization.
