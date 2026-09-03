# FlexDropin Reply Copilot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local-only, Telegram-first Reply Copilot that creates at most five useful, non-promotional English reply drafts per Rome day from the already persisted Growth Digest and leaves every X publication action to the human operator.

**Architecture:** Add a strict configuration block, an additive SQLite workflow, a reply-specific AI method and deterministic guard, a service with no X-capable dependency, and revision-bound Telegram controls. The scheduled path piggybacks on the existing Growth Digest job; manual copy and X Web Intent buttons never call the X API.

**Tech Stack:** Python 3.11, SQLite, Groq client, Telegram Bot API inline keyboards, X Web Intents, APScheduler, pytest.

**Spec:** `docs/superpowers/specs/2026-09-03-reply-copilot-design.md`

## Global Constraints

- Work only on local branch `codex/reply-copilot`. Do not run `git push`, SSH/SCP, VPS commands, deployment commands, or edit production configuration.
- Do not modify or include the user's untracked `VPS_ROLLOUT.md` or `bot_data.db.bak-20260827-173658`.
- Do not add any X read or write to Reply Copilot. Candidate input must come exclusively from `Database.get_growth_digest(observed_on)`.
- Do not inject `TwitterClient`, `Publisher`, X credentials, or any write-capable boundary into `ReplyCopilotService`.
- Keep replies in FlexDropin's account voice while rejecting brand mentions, links, sales calls to action, hashtags, and explicit mentions.
- Preserve the current owned-post publication flow, approval boundary, Growth Digest behavior, scheduler cadence, and X API metering.
- Enforce at most five reservations per Rome date, source age at most 48 hours, and at most three total generation attempts per record: one initial attempt plus two manual regenerations.
- Achieve the 70/30 segment target across two consecutive full batches by alternating quotas: even ordinal date `4 operator + 1 end_user`, odd ordinal date `3 operator + 2 end_user`; fill shortages from the other segment by rank.
- Keep `ENABLE_REPLY_COPILOT=false` by default. Configuration may tighten limits but may not exceed `5`, `48`, and `2` respectively.
- Use additive SQLite schema only. Migration and recovery must be compatible with existing databases and older application code.
- Never log source excerpts, generated reply text, Telegram view tokens, or prefilled Web Intent URLs.
- Use the existing local environment and dependencies; do not install packages.

---

### Task 1: Add strict, disabled-by-default configuration

**Files:**
- Modify: `config.py`
- Modify: `.env.example`
- Modify: `tests/test_main_startup.py`

**Interfaces:**
- Produces: `_reply_copilot_configuration() -> dict`.
- Produces constants: `ENABLE_REPLY_COPILOT`, `REPLY_COPILOT_DAILY_LIMIT`, `REPLY_COPILOT_MAX_AGE_HOURS`, `REPLY_COPILOT_MAX_REGENERATIONS`.
- Extends: `validate_config()` with an import-time snapshot comparison.

- [ ] **Step 1: Write failing subprocess configuration tests**

Add cases beside the Growth Digest configuration tests:

```python
@pytest.mark.parametrize("name,value", [
    ("ENABLE_REPLY_COPILOT", "True"),
    ("ENABLE_REPLY_COPILOT", "1"),
    ("REPLY_COPILOT_DAILY_LIMIT", "0"),
    ("REPLY_COPILOT_DAILY_LIMIT", "6"),
    ("REPLY_COPILOT_MAX_AGE_HOURS", "49"),
    ("REPLY_COPILOT_MAX_REGENERATIONS", "3"),
])
def test_reply_copilot_configuration_fails_closed(name, value):
    result = import_config_in_subprocess({name: value})
    assert result.returncode != 0


def test_reply_copilot_configuration_has_safe_defaults():
    result = import_config_values(
        "ENABLE_REPLY_COPILOT",
        "REPLY_COPILOT_DAILY_LIMIT",
        "REPLY_COPILOT_MAX_AGE_HOURS",
        "REPLY_COPILOT_MAX_REGENERATIONS",
    )
    assert result == [False, 5, 48, 2]
```

Reuse the test file's existing subprocess helpers and sanitized environment rather than importing and reloading `config` in-process.

- [ ] **Step 2: Run the focused tests and confirm the missing configuration is red**

Run:

```bash
venv/bin/python -m pytest -q tests/test_main_startup.py -k reply_copilot
```

Expected: FAIL because the constants and strict parser block do not exist.

- [ ] **Step 3: Implement the cached strict configuration block**

Add:

```python
def _reply_copilot_configuration():
    enabled, enabled_valid = _strict_boolean_env(
        "ENABLE_REPLY_COPILOT", False,
    )
    if not enabled_valid:
        raise ValueError("ENABLE_REPLY_COPILOT must be exactly true or false")
    return {
        "enabled": enabled,
        "daily_limit": _bounded_positive_int_env(
            "REPLY_COPILOT_DAILY_LIMIT", 5, 5,
        ),
        "max_age_hours": _bounded_positive_int_env(
            "REPLY_COPILOT_MAX_AGE_HOURS", 48, 48,
        ),
        "max_regenerations": _bounded_positive_int_env(
            "REPLY_COPILOT_MAX_REGENERATIONS", 2, 2,
        ),
    }
```

Cache it as `_REPLY_COPILOT_CONFIGURATION`, expose the four constants, and compare a fresh parse to the cached value in `validate_config()` so post-import environment changes fail closed.

- [ ] **Step 4: Document the four environment variables**

Add the exact disabled defaults to `.env.example` next to the Growth Digest controls. State that the limits may only be reduced and that enabling the feature creates AI drafts but never sends replies through X.

- [ ] **Step 5: Verify configuration behavior**

Run:

```bash
venv/bin/python -m pytest -q tests/test_main_startup.py
```

Expected: all tests pass.

- [ ] **Step 6: Commit locally**

```bash
git add config.py .env.example tests/test_main_startup.py
git commit -m "feat: add reply copilot configuration"
```

Do not push the commit.

---

### Task 2: Add reply generation and deterministic safety primitives

**Files:**
- Create: `modules/reply_copilot.py`
- Create: `tests/test_reply_copilot.py`
- Modify: `modules/ai_generator.py`
- Modify: `tests/test_ai_generator.py`

**Interfaces:**
- Produces: `classify_reply_segment(reason_codes: object) -> str | None`.
- Produces: `normalize_and_validate_reply(value: object) -> str | None`.
- Produces: `build_reply_web_intent(tweet_id: object, reply_text: object) -> str | None`.
- Produces: `AIGenerator.generate_value_reply(tweet_text: str) -> str | None`.

- [ ] **Step 1: Write failing pure-function guard tests**

Cover all of these exact behaviors in `tests/test_reply_copilot.py`:

- operator reason codes take precedence over end-user codes;
- an end-user-only reason set maps to `end_user`;
- malformed, empty, or unrelated reason sets return `None`;
- safe text of exactly 30 and exactly 256 Unicode characters is accepted;
- 29 and 257 characters are rejected;
- control characters, invalid surrogate code points, URLs, email addresses, hashtags, `@mentions`, `FlexDropin` variants, promotional calls to action, prompt-injection phrases, and bounded medical/legal/crisis advice terms are rejected;
- ordinary surrounding whitespace is normalized without changing the meaning;
- `https://x.com/intent/tweet` receives URL-encoded `in_reply_to` and `text` parameters, and malformed IDs or unsafe reply text return `None`.

Use parsed query parameters rather than comparing encoded parameter order:

```python
intent = build_reply_web_intent("123456789", safe_reply)
parsed = urlparse(intent)
assert parsed.scheme == "https"
assert parsed.netloc == "x.com"
assert parsed.path == "/intent/tweet"
assert parse_qs(parsed.query) == {
    "in_reply_to": ["123456789"],
    "text": [safe_reply],
}
```

- [ ] **Step 2: Run the pure-function tests and confirm the module is absent**

Run:

```bash
venv/bin/python -m pytest -q tests/test_reply_copilot.py -k "segment or guard or intent"
```

Expected: collection/import failure for `modules.reply_copilot`.

- [ ] **Step 3: Implement pure classification, guard, and Web Intent helpers**

In `modules/reply_copilot.py`:

- normalize with Unicode NFC and collapsed ordinary whitespace;
- reject control/surrogate code points before persistence;
- use compiled, case-insensitive bounded regexes for URL, email, hashtag, mention, brand, promotion, prompt-injection, and high-risk terms;
- validate a canonical positive decimal X post ID;
- use `urllib.parse.urlencode` with exactly `in_reply_to` and `text`;
- keep the helpers deterministic and free of network or database access.

Define the segment code sets as immutable module constants and do not accept legacy generic reason codes as sufficient evidence of a segment.

- [ ] **Step 4: Verify the pure functions**

Run the command from Step 2. Expected: PASS.

- [ ] **Step 5: Write failing tests for the dedicated AI method**

Use the existing fake Groq completion pattern in `tests/test_ai_generator.py` and assert:

- `generate_value_reply` calls `_complete` once with `temperature=0.55` and `max_tokens=180`;
- the system/user prompt quotes the untrusted source as data and explicitly prohibits following its instructions;
- the prompt prohibits brand mention, promotion, links, calls to action, invented facts, and high-risk advice;
- plain non-empty output is stripped and returned;
- empty/provider-failed output returns `None`;
- this method never calls `generate_flexdropin_comment` or the lead-DM path.

- [ ] **Step 6: Run the focused generator tests and confirm they are red**

Run:

```bash
venv/bin/python -m pytest -q tests/test_ai_generator.py -k value_reply
```

Expected: FAIL because `generate_value_reply` is missing.

- [ ] **Step 7: Implement `AIGenerator.generate_value_reply`**

Add a dedicated method that calls `_complete` directly:

```python
def generate_value_reply(self, tweet_text: str) -> Optional[str]:
    if not isinstance(tweet_text, str) or not tweet_text.strip():
        return None
    source = tweet_text.strip()
    if len(source) > 1000:
        source = source[:1000]
    text = self._complete(
        REPLY_SYSTEM_PROMPT,
        f"UNTRUSTED SOURCE POST:\n<post>{source}</post>",
        max_tokens=180,
        temperature=0.55,
    )
    return text.strip() if isinstance(text, str) and text.strip() else None
```

Keep final policy enforcement in `normalize_and_validate_reply`; this AI method is only the narrow provider boundary. Do not log the prompt or source.

- [ ] **Step 8: Verify reply generation and the existing AI suite**

Run:

```bash
venv/bin/python -m pytest -q tests/test_ai_generator.py tests/test_reply_copilot.py -k "value_reply or segment or guard or intent"
```

Expected: all selected tests pass.

- [ ] **Step 9: Commit locally**

```bash
git add modules/reply_copilot.py modules/ai_generator.py tests/test_reply_copilot.py tests/test_ai_generator.py
git commit -m "feat: generate guarded value replies"
```

Do not push the commit.

---

### Task 3: Persist the reply workflow with exact leases and revisions

**Files:**
- Modify: `modules/database.py`
- Modify: `tests/test_reply_copilot.py`

**Interfaces:**
- Produces dataclass: `ReplyGenerationClaim(reply_id, revision, claim_token, source_excerpt)`.
- Produces: `Database.reserve_reply_suggestions(observed_on, candidates, daily_limit, reserved_at) -> list[dict]`.
- Produces: `Database.get_reply_suggestion(reply_id, expected_revision=None) -> dict | None`.
- Produces: `Database.list_reply_suggestions(observed_on, statuses=None) -> list[dict]`.
- Produces: `Database.claim_reply_generation(reply_id, expected_revision, claim_token, claimed_at, claim_ttl, max_attempts) -> ReplyGenerationClaim | None`.
- Produces: `Database.complete_reply_generation(claim, reply_text, completed_at) -> bool`.
- Produces: `Database.fail_reply_generation(claim, failure_code, failed_at) -> bool`.
- Produces: `Database.transition_reply_suggestion(reply_id, expected_revision, target_status, decided_at) -> str`.
- Produces: `Database.get_reply_copilot_counts(observed_on) -> dict[str, int]`.

- [ ] **Step 1: Write failing additive-migration and row-validation tests**

Create a database with existing growth suggestions, drafts, publication plans, metrics, and X usage events, reopen it with the new `Database`, and assert every pre-existing count is unchanged. Assert `reply_suggestions` exists with:

```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
growth_suggestion_id INTEGER NOT NULL UNIQUE,
observed_on TEXT NOT NULL,
tweet_id TEXT NOT NULL UNIQUE,
author_username TEXT NOT NULL,
source_excerpt TEXT NOT NULL,
audience_segment TEXT NOT NULL CHECK (audience_segment IN ('operator','end_user')),
relevance_score INTEGER NOT NULL,
reply_text TEXT,
status TEXT NOT NULL CHECK (status IN (
  'reserved','ready','generation_failed','dismissed','published_manually'
)),
generation_count INTEGER NOT NULL DEFAULT 0 CHECK (
  generation_count BETWEEN 0 AND 3
),
generation_claim_token TEXT,
generation_claim_expires_at TEXT,
revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
failure_code TEXT,
created_at TEXT NOT NULL,
updated_at TEXT NOT NULL,
decided_at TEXT,
FOREIGN KEY(growth_suggestion_id) REFERENCES growth_suggestions(id)
```

Add indexes for `(observed_on, status, relevance_score)` and expired generation claims. Invalid enum/counter rows must fail at SQLite constraint level.

- [ ] **Step 2: Run the migration test and confirm it is red**

Run:

```bash
venv/bin/python -m pytest -q tests/test_reply_copilot.py -k "migration or schema"
```

Expected: FAIL because the table is absent.

- [ ] **Step 3: Add the schema and immutable claim dataclass**

Create `ReplyGenerationClaim` next to the existing publication claim dataclasses. Add only `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` statements inside `_init_schema`; do not alter, drop, or reinterpret any existing table.

- [ ] **Step 4: Verify additive migration**

Run the command from Step 2. Expected: PASS.

- [ ] **Step 5: Write failing atomic reservation tests**

Prove that `reserve_reply_suggestions`:

- requires a valid Rome date and aware UTC reservation timestamp;
- accepts only validated candidate snapshots with existing `kind='post'` source IDs;
- inserts in input order up to the remaining daily capacity;
- never exceeds `daily_limit`, including two concurrent `Database` instances racing on the same file;
- treats duplicate source IDs and tweet IDs idempotently;
- does not make dismissed or manually published tweets eligible again;
- returns normalized persisted rows in deterministic relevance/recency/ID order.

Use `threading.Barrier` for the two-instance race and assert the final SQL count, not just each caller's return value.

- [ ] **Step 6: Run reservation tests and confirm the API is red**

Run:

```bash
venv/bin/python -m pytest -q tests/test_reply_copilot.py -k reservation
```

Expected: FAIL because the reservation method is missing.

- [ ] **Step 7: Implement reservation and read models**

Use `BEGIN IMMEDIATE` around capacity calculation and inserts. Bound all stored strings, parse JSON only through existing Growth Digest readers, and return plain dictionaries with parsed aware datetimes. `get_reply_suggestion` must honor `expected_revision` when supplied; `list_reply_suggestions` must reject unknown statuses instead of interpolating them into SQL.

- [ ] **Step 8: Verify reservation tests**

Run the command from Step 6. Expected: PASS.

- [ ] **Step 9: Write failing generation-lease tests**

Cover:

- exact revision compare-and-swap claim acquisition;
- attempt count increment at claim time;
- one active lease per row;
- expired-lease reclaim while `generation_count < max_attempts`;
- maximum three total attempts;
- completion/failure only by the exact current token and claim revision;
- late completion from an expired/replaced token rejected;
- success sets `ready`, stores validated copy, clears failure/lease, and increments revision;
- failure sets `generation_failed`, stores only a bounded failure code, clears copy/lease, and increments revision;
- all state survives `Database` restart.

- [ ] **Step 10: Run lease tests and confirm they are red**

Run:

```bash
venv/bin/python -m pytest -q tests/test_reply_copilot.py -k "claim or lease or generation_state"
```

Expected: FAIL because the claim methods are absent.

- [ ] **Step 11: Implement claim, completion, and failure transitions**

Use exact token equality and `BEGIN IMMEDIATE`. A claim changes the row to `reserved`, clears obsolete output/failure fields, increments `generation_count` and `revision`, and returns that new revision in `ReplyGenerationClaim`. Completion or failure increments revision again. Never persist an exception message; accept only stable internal failure codes such as `provider_unavailable`, `invalid_output`, and `claim_lost`.

- [ ] **Step 12: Write and implement manual-decision/count tests**

First add red tests, then implement:

- only `ready -> dismissed` and `ready -> published_manually` are accepted;
- repeating the same target transition is `duplicate` and does not increment revision;
- stale revision, reverse transition, or transition from failure/reserved returns `rejected`;
- counts always return integer keys `reserved`, `ready`, `generation_failed`, `dismissed`, `published_manually`, including zeroes.

Run:

```bash
venv/bin/python -m pytest -q tests/test_reply_copilot.py -k "transition or counts"
```

Expected after implementation: PASS.

- [ ] **Step 13: Verify the complete persistence slice**

Run:

```bash
venv/bin/python -m pytest -q tests/test_reply_copilot.py
```

Expected: all persistence and pure-function tests pass.

- [ ] **Step 14: Commit locally**

```bash
git add modules/database.py tests/test_reply_copilot.py
git commit -m "feat: persist reply copilot workflow"
```

Do not push the commit.

---

### Task 4: Build bounded suggestions only from persisted Growth Digest data

**Files:**
- Modify: `modules/reply_copilot.py`
- Modify: `tests/test_reply_copilot.py`

**Interfaces:**
- Produces: `ReplyCopilotService(db, generator, *, daily_limit=5, max_age_hours=48, max_regenerations=2, clock=None, token_factory=None, claim_ttl=timedelta(minutes=5))`.
- Produces: `ReplyCopilotService.build(observed_on, now=None) -> dict`.
- Produces: `ReplyCopilotService.list(observed_on, statuses=None) -> list[dict]`.
- Produces: `ReplyCopilotService.regenerate(reply_id, expected_revision, now=None) -> tuple[dict | None, str]`.
- Produces: `ReplyCopilotService.dismiss(reply_id, expected_revision, now=None) -> tuple[dict | None, str]`.
- Produces: `ReplyCopilotService.mark_published(reply_id, expected_revision, now=None) -> tuple[dict | None, str]`.
- Produces: `ReplyCopilotService.counts(observed_on) -> dict[str, int]`.

- [ ] **Step 1: Write failing deterministic selection tests**

Seed real `growth_suggestions` through existing Growth Digest persistence helpers. Assert:

- `build` calls `db.get_growth_digest(observed_on)` and never calls a builder or X boundary;
- malformed digest rows, non-post rows, missing canonical ID/username/excerpt, FlexDropin's own username, unrelated reason codes, and future/older-than-48-hour sources are skipped;
- exactly-48-hour-old candidates remain eligible;
- selection orders by descending stored score, descending source `created_at`, then ascending stable suggestion ID;
- even ordinal full batches select four operators and one end user;
- odd ordinal full batches select three operators and two end users;
- segment shortages are filled by the highest-ranked remaining candidate;
- repeat builds, restarts, and concurrent builds reserve at most five and never duplicate a tweet.

Add a sentinel object whose every attribute access raises and prove it is never needed by the constructor or `build`; this is the structural no-X-dependency test.

- [ ] **Step 2: Run selection tests and confirm the service is red**

Run:

```bash
venv/bin/python -m pytest -q tests/test_reply_copilot.py -k "service_selection or daily_mix or no_x_dependency"
```

Expected: FAIL because `ReplyCopilotService` is missing.

- [ ] **Step 3: Implement validation, ranking, and alternating allocation**

The service must reload the persisted digest even when the caller already has a digest dictionary. Parse `payload.created_at` as an aware UTC time, calculate age from the passed aware `now`, and build only normalized candidate snapshots for `Database.reserve_reply_suggestions`.

Implement the full-batch quota as:

```python
operator_quota, end_user_quota = (
    (4, 1)
    if date.fromisoformat(observed_on).toordinal() % 2 == 0
    else (3, 2)
)
```

Select within each segment by the shared stable rank, then fill unused capacity from all remaining candidates by that same rank. Do not inspect live X state.

- [ ] **Step 4: Verify selection behavior**

Run the command from Step 2. Expected: PASS.

- [ ] **Step 5: Write failing generation-orchestration tests**

Using a fake generator with queued responses, prove:

- every newly reserved row receives one automatic attempt;
- valid output becomes `ready`;
- provider `None`/exception becomes `generation_failed` with a bounded code;
- guard rejection becomes `generation_failed` without storing the bad text;
- one candidate failure does not stop later candidates;
- a repeated build does not regenerate `ready` or failed rows automatically;
- explicit `regenerate` is revision-bound, can run at most two further attempts, and uses the persisted source excerpt rather than Telegram data;
- `dismiss` and `mark_published` use only exact current rows/revisions;
- summaries have exactly `observed_on`, `outcome`, `reserved`, `ready`, `failed`, and `suggestions`, where `suggestions` contains current safe rows only.

- [ ] **Step 6: Run orchestration tests and confirm they are red**

Run:

```bash
venv/bin/python -m pytest -q tests/test_reply_copilot.py -k "generation_orchestration or regenerate or service_transition"
```

Expected: FAIL until service generation methods exist.

- [ ] **Step 7: Implement bounded orchestration and failure isolation**

For each row eligible for generation:

1. create an opaque token with `token_factory`;
2. acquire an exact database claim with `max_attempts=1 + max_regenerations`;
3. call only `generator.generate_value_reply(claim.source_excerpt)`;
4. pass output through `normalize_and_validate_reply`;
5. complete or fail the exact claim;
6. log only row ID, state, count, bounded failure code, and provider exception class.

`build` may generate newly reserved/expired-claim rows, but must never automatically retry a completed `generation_failed` row. `regenerate` is the only ordinary retry path.

- [ ] **Step 8: Verify the complete service**

Run:

```bash
venv/bin/python -m pytest -q tests/test_reply_copilot.py
```

Expected: all tests pass.

- [ ] **Step 9: Commit locally**

```bash
git add modules/reply_copilot.py tests/test_reply_copilot.py
git commit -m "feat: build bounded reply suggestions"
```

Do not push the commit.

---

### Task 5: Add revision-bound Telegram Reply Copilot controls

**Files:**
- Create: `tests/test_reply_copilot_telegram.py`
- Modify: `modules/telegram_controller.py`
- Modify: `modules/telegram_api.py`
- Modify: `tests/test_telegram_workflows.py`

**Interfaces:**
- Extends: `TelegramController.__init__(..., reply_copilot=None)`.
- Produces command: `/replies`.
- Produces: `TelegramController.push_reply_digest(summary, explicit=False) -> str`.
- Adds callback prefixes: `rp` for detail/navigation and `rpa` for mutations.
- Extends Telegram reply markup with validated `copy_text` buttons.

- [ ] **Step 1: Write failing `/replies` and card-rendering tests**

In `tests/test_reply_copilot_telegram.py`, build the real controller with a real temporary database and a fake Reply Copilot service. Assert:

- `/replies` uses the controller's UTC clock once, converts it to the Rome date, and calls `build` for that date;
- no persisted digest yields a clear empty message and no X interaction;
- a ready row produces a compact summary followed by a detail card;
- the card includes bounded source excerpt, segment, relevance, full reply, character count, and `Pubblicazione manuale: il bot non risponde su X`;
- `Copia risposta` has exactly `{"copy_text": {"text": reply_text}}`;
- `Rispondi su X` contains the exact parsed `in_reply_to` and reply text;
- copy and URL buttons contain no callback data and cause no database transition.

- [ ] **Step 2: Run the new Telegram tests and confirm they are red**

Run:

```bash
venv/bin/python -m pytest -q tests/test_reply_copilot_telegram.py -k "command or card or copy or web_intent"
```

Expected: FAIL because Reply Copilot is not wired into the controller.

- [ ] **Step 3: Implement the command, summary, opaque view, and card**

Add `reply_copilot` as an optional keyword-only constructor dependency and register `/replies`. Use `view_kind="reply_copilot"` and persist the current row IDs with `Database.create_telegram_view`.

Callback formats must stay below Telegram's 64-byte limit even for SQLite's maximum integer values:

```text
rp:<view_token>:<reply_id>:<revision>
rpa:g:<view_token>:<reply_id>:<revision>
rpa:d:<view_token>:<reply_id>:<revision>
rpa:p:<view_token>:<reply_id>:<revision>
```

Before rendering, reload the opaque view for the authorized chat, confirm membership of `reply_id`, and reload the exact revision. Build `copy_text` and Web Intent from the persisted validated reply only.

- [ ] **Step 4: Extend reply-markup validation for `copy_text`**

In `_validate_reply_markup`, require each inline button to contain exactly one supported action among `callback_data`, `url`, and `copy_text`. For `copy_text`, require exactly `{"text": <str>}` and a 1-256-character value. Preserve all existing valid callback and HTTPS URL buttons.

Add focused transport tests in `tests/test_telegram_workflows.py` for a valid copy button, empty/257-character copy, malformed object, and a button with multiple action kinds.

- [ ] **Step 5: Verify command/card/transport tests**

Run:

```bash
venv/bin/python -m pytest -q tests/test_reply_copilot_telegram.py tests/test_telegram_workflows.py -k "reply_copilot or copy_text or reply_markup"
```

Expected: all selected tests pass.

- [ ] **Step 6: Write failing callback, navigation, and replay tests**

Cover:

- previous, next, and refresh render only IDs bound to the view;
- `Rigenera` passes the exact stored ID/revision to the service and replaces the card with the new revision;
- the regenerate button disappears once the three-attempt cap is reached;
- `Ignora` and `Segna come pubblicata` call the exact local transition;
- stale revision, malformed ID, unknown action, expired token, foreign chat, ID not in view, and replayed mutation all fail closed;
- a Telegram delivery failure leaves the persisted row unchanged and recoverable by a later `/replies`;
- no callback ever accepts reply text from its data payload.

- [ ] **Step 7: Run callback tests and confirm they are red**

Run:

```bash
venv/bin/python -m pytest -q tests/test_reply_copilot_telegram.py -k "callback or navigation or replay or delivery"
```

Expected: FAIL until the callback branches are implemented.

- [ ] **Step 8: Implement revision-bound callbacks and failure messages**

Route `rp` and `rpa` in `_handle_callback`. Validate part count and canonical numeric fields with `_positive_id`/`_nonnegative_id`, reload `view_kind="reply_copilot"`, verify membership, then call the service. Treat `updated` and the exact same terminal state as safe outcomes; reject stale or conflicting callbacks without mutation.

Do not mark a reply as published when the copy or URL button is rendered or used. Only `rpa:p` records `published_manually`.

- [ ] **Step 9: Write failing `/status`, `/help`, and push-summary tests**

Assert:

- `/status` adds today's `ready`, `generation_failed`, `dismissed`, and `published_manually` counts when the service is enabled, without printing reply copy;
- `/help` documents `/replies` as manual;
- scheduled `push_reply_digest` is silent unless at least one newly built row is ready;
- explicit `/replies` reports empty/failure states instead of staying silent;
- the summary text never claims that X was posted automatically.

- [ ] **Step 10: Implement status/help/summary behavior and verify Telegram**

Run:

```bash
venv/bin/python -m pytest -q tests/test_reply_copilot_telegram.py tests/test_telegram_workflows.py
```

Expected: all tests pass.

- [ ] **Step 11: Commit locally**

```bash
git add modules/telegram_controller.py modules/telegram_api.py tests/test_reply_copilot_telegram.py tests/test_telegram_workflows.py
git commit -m "feat: add Telegram reply copilot"
```

Do not push the commit.

---

### Task 6: Wire Reply Copilot into the existing Growth Digest cycle

**Files:**
- Modify: `main.py`
- Modify: `tests/fakes.py`
- Modify: `tests/test_end_to_end_dry_run.py`
- Modify: `tests/test_main_startup.py`

**Interfaces:**
- Extends dependency keys: `reply_copilot`, `reply_copilot_enabled`.
- Constructs: `ReplyCopilotService` only behind the enabled flag.
- Extends: `FlexDropinGrowthAgent.growth_digest_cycle(now=None)` without adding a scheduler job.
- Extends: `_register_telegram_commands()` with `/replies` only while enabled.

- [ ] **Step 1: Write failing dependency and scheduler tests**

Assert:

- `reply_copilot_enabled` must be an exact `bool` when injected;
- disabled default leaves `agent.reply_copilot is None` and existing behavior unchanged;
- enabled construction receives only `db`, `generator`, limits, and clock-related values;
- registered jobs still contain exactly one existing `growth_digest` job and no Reply Copilot-specific job;
- the Telegram command menu contains `/replies` only when the feature is enabled.

- [ ] **Step 2: Run focused wiring tests and confirm they are red**

Run:

```bash
venv/bin/python -m pytest -q tests/test_main_startup.py tests/test_end_to_end_dry_run.py -k "reply_copilot or registered_jobs"
```

Expected: FAIL because the dependency keys and wiring are absent.

- [ ] **Step 3: Implement construction and command registration**

Import the four config constants and `ReplyCopilotService`. Add `reply_copilot` and `reply_copilot_enabled` to `_DEPENDENCY_KEYS`, validate the injected flag with `type(value) is bool`, and create the service only when enabled:

```python
self.reply_copilot = (
    resolve(
        "reply_copilot",
        lambda: ReplyCopilotService(
            self.db,
            self.ai_generator,
            daily_limit=REPLY_COPILOT_DAILY_LIMIT,
            max_age_hours=REPLY_COPILOT_MAX_AGE_HOURS,
            max_regenerations=REPLY_COPILOT_MAX_REGENERATIONS,
            clock=self.clock,
        ),
    )
    if self.reply_copilot_enabled
    else None
)
```

Pass only this service into `TelegramController`. Do not pass the X client through it. Append the `/replies` menu entry only when enabled.

- [ ] **Step 4: Verify dependency and scheduler tests**

Run the command from Step 2. Expected: PASS.

- [ ] **Step 5: Write failing cycle-isolation tests**

Extend the current clock-once Growth Digest test to prove:

- `growth_digest.build(current)` and `push_growth_digest` run first;
- when enabled, `reply_copilot.build(digest["observed_on"], now=current)` runs and its summary reaches `push_reply_digest(..., explicit=False)`;
- an invalid/missing `observed_on` does not start Reply Copilot;
- a Reply Copilot or Telegram reply-summary exception logs context `reply_copilot_cycle` and preserves the existing Growth Digest return value;
- a Growth Digest failure preserves the current `growth_digest_failed` result;
- the clock is still read once per cycle.

- [ ] **Step 6: Run cycle tests and confirm they are red**

Run:

```bash
venv/bin/python -m pytest -q tests/test_end_to_end_dry_run.py -k "growth_digest_cycle and reply"
```

Expected: FAIL because `growth_digest_cycle` does not invoke Reply Copilot.

- [ ] **Step 7: Implement isolated piggyback orchestration**

Keep the existing Growth Digest `try` boundary. Capture its current presentation result, then run Reply Copilot in a nested `try` only when enabled and `observed_on` is a canonical date string. On nested failure call `_notify_error("reply_copilot_cycle", error)` and still return the original Growth Digest result.

- [ ] **Step 8: Write the end-to-end zero-X-cost safety test**

In `tests/test_end_to_end_dry_run.py`, use the real database, Growth Digest, Reply Copilot service, Telegram controller, and a recording fake X client. Run:

1. one Growth Digest build that performs its expected metered discovery reads;
2. Reply Copilot automatic generation;
3. `/replies` after the digest is persisted;
4. card navigation;
5. copy/Web Intent inspection;
6. one regeneration;
7. one manual-publication mark;
8. one dismissal;
9. process restart and `/replies` recovery.

Snapshot X calls and `Database.get_x_api_usage_summary(period_key)` immediately after Step 1. At the end assert:

```python
assert x_client.read_calls == reads_after_growth_digest
assert x_client.write_calls == []
assert db.get_x_api_usage_summary(period_key) == usage_after_growth_digest
```

Also assert the existing owned-post publication path can still publish independently through its established approval flow; do not route it through Reply Copilot.

- [ ] **Step 9: Run the wiring and end-to-end safety slice**

Run:

```bash
venv/bin/python -m pytest -q tests/test_reply_copilot.py tests/test_reply_copilot_telegram.py tests/test_end_to_end_dry_run.py tests/test_main_startup.py
```

Expected: all tests pass, with zero Reply Copilot X calls.

- [ ] **Step 10: Commit locally**

```bash
git add main.py tests/fakes.py tests/test_end_to_end_dry_run.py tests/test_main_startup.py
git commit -m "feat: wire reply copilot into growth digest"
```

Do not push the commit.

---

### Task 7: Document operations and prove local release readiness

**Files:**
- Modify: `README.md`
- Modify: `SETUP_GUIDE.md`
- Modify: `docs/superpowers/plans/2026-09-03-reply-copilot.md`
- Test: full repository suite

**Interfaces:**
- Documents operator flow: `/replies` -> inspect -> copy or open X -> human publishes -> mark locally.
- Documents safety/cost boundary: no extra X reads, no X writes, bounded Groq generations.
- Documents local feature toggle and rollback without performing either VPS action.

- [ ] **Step 1: Update operator documentation**

Document:

- the four environment variables and disabled default;
- the 5/day, 48-hour, and regeneration limits;
- the alternating 70/30 audience mix;
- that suggestions are English and non-promotional;
- that `Copia risposta` and `Rispondi su X` do not publish automatically;
- that only `Segna come pubblicata` changes local status;
- that the feature reuses persisted Growth Digest posts and adds no X API usage;
- safe disable/rollback by setting the feature flag false, described only as future operator instructions.

Do not claim deployment or VPS activation has occurred.

- [ ] **Step 2: Run focused Reply Copilot tests**

```bash
venv/bin/python -m pytest -q tests/test_reply_copilot.py tests/test_reply_copilot_telegram.py
```

Expected: all tests pass.

- [ ] **Step 3: Run the full regression suite**

```bash
venv/bin/python -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 4: Compile production modules**

```bash
venv/bin/python -m compileall -q config.py main.py modules
```

Expected: exit code 0.

- [ ] **Step 5: Test migration on a disposable local database copy**

Create a temporary directory with `mktemp -d`, copy the local SQLite fixture/database into that exact directory, open the copy with `Database`, then compare pre/post counts for every pre-existing table and verify `reply_suggestions` starts empty. Do not open or copy any VPS database.

Expected: all pre-existing counts are identical; only the additive table/indexes appear.

- [ ] **Step 6: Run static safety searches**

```bash
rg -n "TwitterClient|Publisher|post_tweet|post_thread|reply_to_tweet|search_relevant_posts|read_relevant_posts" modules/reply_copilot.py
rg -n "reply_text|source_excerpt|intent/tweet" modules/reply_copilot.py main.py modules/telegram_controller.py
```

Expected: the first command finds no matches. Review the second command manually and confirm none of those values are passed to logging calls.

- [ ] **Step 7: Inspect repository integrity**

```bash
git diff --check
git status --short --branch
git diff --stat main..HEAD
```

Expected: no whitespace errors; only planned files are changed/committed; the two pre-existing untracked user files remain untouched.

- [ ] **Step 8: Mark this plan complete and commit documentation locally**

Change completed checkboxes to `[x]`, record exact local test counts in a short `## Verification Evidence` section, then run:

```bash
git add README.md SETUP_GUIDE.md docs/superpowers/plans/2026-09-03-reply-copilot.md
git commit -m "docs: document reply copilot operations"
```

Do not push the commit and do not deploy it. Stop with a local-only handoff containing the branch name, commit range, tests, migration result, and an explicit statement that the VPS was not contacted or changed.
