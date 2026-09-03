# Phase 0 Stabilization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore a green baseline, make partial thread publication observable, add configurable X API spend controls and telemetry, and activate the existing adaptive category weights.

**Architecture:** Preserve the approval-only publisher and existing dependency-injection seams. Use additive SQLite tables for thread checkpoints and API usage claims, a dedicated usage-meter component at the X boundary, and normalized category targets in the existing content planner. All transitions remain fail-closed and backward compatible.

**Tech Stack:** Python 3.11, SQLite, Tweepy 4.14, APScheduler, pytest.

**Spec:** `docs/superpowers/specs/2026-08-10-x-growth-telegram-design.md`

## Global Constraints

- Keep `APPROVAL_REQUIRED=true`; do not add automated likes, follows, unsolicited replies, DMs, or browser automation.
- Preserve existing databases through additive schema expansion only.
- Never retry an X write whose outcome may be unknown.
- Treat prices as configurable estimates; the X Developer Console remains authoritative.
- Keep existing public interfaces backward compatible unless a test proves a required extension.
- Do not modify or include the user's untracked `VPS_ROLLOUT.md` or database backup.

---

### Task 1: Restore the baseline

**Files:**
- Modify: `modules/database.py:7710`
- Modify: `tests/test_telegram_workflows.py:1098`
- Test: `tests/test_growth_digest.py:1239`

**Interfaces:**
- Consumes: `Database.claim_growth_read_query(...) -> tuple[str, str | None]`.
- Produces: stale, expired claims from retired query keys no longer consume the current daily query budget.

- [x] **Step 1: Confirm the existing growth recovery test is red**

Run:
```bash
python -m pytest -q tests/test_growth_digest.py::test_hard_crash_before_commit_recovers_only_after_lease_expiry
```
Expected: FAIL because retired query keys remain counted after their leases expire.

- [x] **Step 2: Implement expired-lease reclamation**

Inside the same `BEGIN IMMEDIATE` transaction used by `claim_growth_read_query`, delete only non-builder rows whose state is `claimed`, whose expiry is at or before `claimed_at`, and whose key differs from the key currently being reclaimed. Do not delete completed or failed claims.

- [x] **Step 3: Verify growth recovery**

Run the test from Step 1. Expected: PASS, including the existing assertion that exactly the builder and two current query claims complete.

- [x] **Step 4: Align Telegram workflow tests with the intentional queue-only index**

Change `test_posts_renders_complete_safe_draft_card_and_latest_published` to assert that the pending draft is present and the published draft is absent. In `test_published_preview_requires_exact_tweet_media_binding`, open `/posts` and capture the pending detail callback before changing the draft/media rows to the published mismatch; invoke the already-bound callback afterward to exercise the publication race safely.

- [x] **Step 5: Verify all three former failures**

Run:
```bash
python -m pytest -q tests/test_growth_digest.py::test_hard_crash_before_commit_recovers_only_after_lease_expiry tests/test_telegram_workflows.py::test_posts_renders_complete_safe_draft_card_and_hides_published tests/test_telegram_workflows.py::test_published_preview_requires_exact_tweet_media_binding
```
Expected: 3 passed.

- [x] **Step 6: Commit**

```bash
git add modules/database.py tests/test_telegram_workflows.py
git commit -m "fix: restore growth and Telegram baseline"
```

---

### Task 2: Persist thread publication checkpoints

**Files:**
- Modify: `modules/database.py`
- Modify: `modules/twitter_client.py:236`
- Modify: `modules/publisher.py:465`
- Test: `tests/test_thread_support.py`

**Interfaces:**
- Produces: `Database.record_thread_publication_part(claim, part_index, tweet_id, reply_to_tweet_id) -> bool`.
- Produces: `Database.get_thread_publication_parts(draft_id) -> list[dict]`.
- Extends: `TwitterClient.post_thread(..., on_tweet_posted=None) -> list[str]`.

- [x] **Step 1: Write red database checkpoint tests**

Add tests proving that an exact active publication claim can append parts sequentially, duplicate identical checkpoints are idempotent, conflicting/out-of-order checkpoints fail closed, and rows survive reopening `Database`.

- [x] **Step 2: Verify the database tests fail because the API/table is absent**

Run the new tests only. Expected: FAIL with missing `record_thread_publication_part`.

- [x] **Step 3: Add the compatibility-safe table and methods**

Create `thread_publication_parts(draft_id, part_index, tweet_id, reply_to_tweet_id, recorded_at)` with primary key `(draft_id, part_index)`, unique `tweet_id`, and a foreign key to `post_drafts`. Validate the immutable publication claim and enforce contiguous parent-linked inserts in one write transaction.

- [x] **Step 4: Verify database checkpoint tests pass**

Run the tests from Step 1. Expected: PASS.

- [x] **Step 5: Write red transport and publisher tests**

Add one `TwitterClient` test asserting the callback receives `(0, root_id, None)` then each child with its exact parent. Add one publisher integration test whose fake X client checkpoints the root and first child, then raises `XPublicationUnknown`; assert status `publication_unknown` and both checkpoint rows persist.

- [x] **Step 6: Verify the new transport tests fail**

Run the two new tests. Expected: FAIL because neither transport nor publisher supplies checkpoint callbacks.

- [x] **Step 7: Wire checkpoint callbacks through the existing introspection seam**

Invoke `on_tweet_posted` immediately after validating every returned X ID. Extend `Publisher._call_post_thread` to pass a database-backed callback only when supported by the client signature. A checkpoint persistence failure after an X success must raise/propagate as unknown; it must never trigger a retry.

- [x] **Step 8: Verify all thread tests**

Run:
```bash
python -m pytest -q tests/test_thread_support.py tests/test_publisher.py
```
Expected: PASS.

- [x] **Step 9: Commit**

```bash
git add modules/database.py modules/twitter_client.py modules/publisher.py tests/test_thread_support.py
git commit -m "feat: checkpoint partial thread publication"
```

---

### Task 3: Add configurable X API budget telemetry

**Files:**
- Create: `modules/x_api_usage.py`
- Create: `tests/test_x_api_usage.py`
- Modify: `modules/database.py`
- Modify: `modules/twitter_client.py`
- Modify: `config.py`
- Modify: `.env.example`
- Modify: `main.py`

**Interfaces:**
- Produces: `XApiUsageMeter.reserve(operation, max_units, now=None) -> XApiUsageClaim | None`.
- Produces: `XApiUsageMeter.complete(claim, actual_units)`, `fail(claim)`, and `unknown(claim)`.
- Produces: `Database.get_x_api_usage_summary(period_key) -> dict`.
- Consumes environment: optional monthly USD budget plus per-operation USD rates; zero monthly budget means telemetry-only for backward compatibility.

- [x] **Step 1: Write red ledger tests**

Cover atomic reservation under a configured monthly cap, actual-unit reconciliation, failed calls costing zero, unknown calls retaining the reservation, telemetry-only mode, invalid inputs, and persistence across restart. Use integer micro-USD expected values derived by hand.

- [x] **Step 2: Verify ledger tests fail**

Run `python -m pytest -q tests/test_x_api_usage.py`. Expected: import/API failure.

- [x] **Step 3: Implement additive ledger and meter**

Add `x_api_usage_events` with opaque request token, period, operation, reserved/actual units, integer unit cost, state (`reserved`, `completed`, `failed`, `unknown`), and timestamps. Reserve atomically with `BEGIN IMMEDIATE`; completed cost uses actual units, while reserved/unknown cost uses maximum units.

- [x] **Step 4: Verify ledger tests pass**

Run the test file from Step 2. Expected: PASS.

- [x] **Step 5: Write red X-boundary metering tests**

Cover one successful read, one definite failed read, one successful post, a post blocked before network by the budget, and an ambiguous post failure retained as unknown. Assertions target the real database summary and recorded fake-network calls.

- [x] **Step 6: Verify boundary tests fail**

Run the new focused tests. Expected: FAIL because `TwitterClient` does not use the meter.

- [x] **Step 7: Instrument X calls and wire configuration**

Add a narrow request helper to `TwitterClient`. Reserve before a request, complete using returned resource count, fail on definite no-response failures, and retain unknown write reservations on transport/server ambiguity. Gate an entire thread before its first write. Construct the meter from the existing database in `main.py`; keep direct `TwitterClient()` construction valid with metering disabled.

- [x] **Step 8: Document configuration and remove stale fixed-price commentary**

Add the optional budget/rate variables to `.env.example`. Describe rates as estimates that must match the Developer Console; remove approximate price claims from runtime comments.

- [x] **Step 9: Verify X boundary and configuration tests**

Run:
```bash
python -m pytest -q tests/test_x_api_usage.py tests/test_publisher.py tests/test_thread_support.py tests/test_config_validation.py tests/test_main_architecture.py
```
Expected: PASS.

- [x] **Step 10: Commit**

```bash
git add modules/x_api_usage.py modules/database.py modules/twitter_client.py config.py .env.example main.py tests/test_x_api_usage.py
git commit -m "feat: meter and cap X API usage"
```

---

### Task 4: Activate adaptive category weights

**Files:**
- Modify: `modules/content_planner.py`
- Modify: `modules/analytics.py`
- Modify: `main.py`
- Modify: `tests/fakes.py`
- Test: `tests/test_content_planner.py`
- Test: `tests/test_growth_analytics.py`
- Test: `tests/test_main_architecture.py`

**Interfaces:**
- Produces: `effective_portfolio(weights=None) -> dict[str, float]` normalized to 1.0.
- Extends: `choose_portfolio_category(counts, weights=None) -> str`.
- Consumes: `Database.get_all_category_weights()`.

- [x] **Step 1: Write red planner tests**

Prove that a high valid learned weight changes the selected eligible category, normalized targets retain every static category, missing weights preserve current behavior, and malformed/non-finite/out-of-range weights fail closed to the static portfolio.

- [x] **Step 2: Verify planner tests fail**

Run the new tests only. Expected: FAIL because weights are ignored.

- [x] **Step 3: Use normalized effective targets in the planner**

Multiply each base share by its valid learned weight and normalize the result. Read weights once per `plan` call and use the same effective portfolio for eligibility deficit selection. Preserve the static portfolio when the database method is missing or data is malformed.

- [x] **Step 4: Verify content planner tests pass**

Run `python -m pytest -q tests/test_content_planner.py`. Expected: PASS.

- [x] **Step 5: Write red scheduler-cycle test**

Assert that `performance_metrics_cycle(now)` refreshes owned metrics first and then calls `recompute_category_weights(now=now)` exactly once, while preserving current error handling.

- [x] **Step 6: Verify the scheduler test fails**

Run the focused orchestration test. Expected: FAIL because recomputation is not called.

- [x] **Step 7: Activate recomputation after metrics refresh**

Extend `performance_metrics_cycle` with optional `now`, run refresh first, then recompute using the same aware time. Correct the stale analytics docstring that references the removed scheduler.

- [x] **Step 8: Verify analytics and orchestration tests**

Run:
```bash
python -m pytest -q tests/test_content_planner.py tests/test_growth_analytics.py tests/test_main_architecture.py
```
Expected: PASS.

- [x] **Step 9: Commit**

```bash
git add modules/content_planner.py modules/analytics.py main.py tests/fakes.py tests/test_content_planner.py tests/test_main_architecture.py
git commit -m "feat: apply learned editorial weights"
```

---

### Task 5: Full verification and operator documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/plans/2026-09-03-phase-0-stabilization.md`

**Interfaces:**
- Documents the new recovery, budget, and learning behavior without changing runtime behavior.

- [x] **Step 1: Update README**

Document thread checkpoint observability, budget variables and telemetry-only default, the Developer Console as pricing authority, and when adaptive weights begin affecting planning.

- [x] **Step 2: Run focused safety tests**

Run all test files changed or directly affected by Tasks 1–4. Expected: PASS.

- [x] **Step 3: Run the complete suite**

Run `python -m pytest -q`. Expected: zero failures.

- [x] **Step 4: Run static repository checks**

Run:
```bash
python -m compileall -q main.py config.py modules tests
git diff --check
git status --short
```
Expected: compile success, no whitespace errors, and only intentional Phase 0 changes.

- [x] **Step 5: Mark every completed plan checkbox and commit documentation**

```bash
git add README.md docs/superpowers/plans/2026-09-03-phase-0-stabilization.md
git commit -m "docs: describe phase zero safeguards"
```
