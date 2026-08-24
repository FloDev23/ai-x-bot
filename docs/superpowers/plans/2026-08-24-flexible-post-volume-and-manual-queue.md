# Flexible Post Volume and Manual Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish two US-audience posts on normal days and three on selected days, maintain a 14-post approved reserve, and let the authorized operator add exact manual posts through Telegram.

**Architecture:** Add a cadence policy that decides whether an audience day has two or three positions, while the existing adaptive timing policy selects safe times inside morning, midday, and evening windows. Expand the publication-plan persistence boundary to positions 1–3 with a crash-safe SQLite migration. Add a restart-safe `/newpost` workflow that validates exact operator copy through the canonical fact/novelty/score gates and atomically consumes the Telegram session while creating the queue draft and optional media reservation.

**Tech Stack:** Python 3.11+, SQLite, APScheduler, Telegram Bot HTTP API, Groq through the existing generator, pytest, `zoneinfo`.

## Global Constraints

- The bot must never like, follow, unfollow, repost, reply, comment, or send a direct message on X.
- The sole production X write remains `Publisher -> TwitterClient.post_tweet -> create_tweet`.
- Production remains `DRY_RUN=true` and `APPROVAL_REQUIRED=true` throughout implementation and deployment.
- Cold start uses three posts on Tuesday, Thursday, and Saturday; all other days use two.
- After 30 mature samples, exactly three weekdays receive a third position; the 90-sample threshold remains the finer weekday/time-learning gate.
- Windows are morning `08:30-10:30`, midday `13:00-15:30`, and evening `18:00-20:30` in `America/New_York`, with at least four hours between positions.
- Queue target is 14 approved/planned drafts, pending Telegram review limit is 5, and automatic generation cap is 5 successful claims per Rome day.
- Manual English copy is never rewritten by AI and must pass the same source, fact, novelty, score, media, link, pause, approval, and publication gates as generated copy.
- Manual drafts never consume the automatic generation cap.
- No raw Telegram update, Italian translation, source body, model reasoning, file path, token, or credential may enter logs or audit details.

## File Structure

- Create `modules/publication_cadence.py`: decide two-versus-three daily volume from mature owned metrics.
- Modify `modules/adaptive_timing.py`: choose either two or three safe restart-stable times.
- Modify `modules/database.py`: migrate and persist publication positions 1–3; atomically create manual queue drafts.
- Modify `modules/draft_pipeline.py`: validate exact manual copy through canonical gates without generation.
- Modify `modules/review_translation.py`: expose the existing translation invariants for operator-supplied Italian copy.
- Modify `modules/publication_queue.py`: combine cadence and timing, fill and publish at most three positions.
- Modify `modules/telegram_controller.py`: implement `/newpost`, manual callbacks, bilingual preview, and richer `/status`.
- Modify `main.py`: inject cadence policy and new strict configuration.
- Modify `config.py`, `.env.example`, `README.md`, and `SETUP.md`: declare and document the new limits and workflow.
- Create `tests/test_publication_cadence.py`: cadence, learning, timing, DST, and validation tests.
- Create `tests/test_manual_post_queue.py`: manual Telegram/domain/SQLite/media/replay tests.
- Modify existing adaptive, queue, Telegram, end-to-end, and X-write safety tests.

---

### Task 1: Add strict cadence configuration and two/three-day policy

**Files:**
- Create: `modules/publication_cadence.py`
- Create: `tests/test_publication_cadence.py`
- Modify: `config.py:106-190`
- Modify: `.env.example:24-36`

**Interfaces:**
- Consumes: `modules.adaptive_timing.TimingSample` records with aware `scheduled_for`, aware `measured_at`, nonnegative impressions, and nonnegative engagements.
- Produces: `CadenceDecision(post_count: int, third_post_weekdays: tuple[int, int, int], reason: str)`.
- Produces: `PublicationCadencePolicy.choose(local_date: date, samples: Iterable[TimingSample]) -> CadenceDecision`.
- Produces configuration constants `POSTS_PER_DAY`, `THIRD_POST_DAYS_PER_WEEK`, `MIDDAY_WINDOW`, `THIRD_POST_TIMING_MIN_POSTS`, `APPROVED_QUEUE_TARGET`, `PENDING_REVIEW_LIMIT`, and `DRAFT_GENERATION_DAILY_CAP`.

- [ ] **Step 1: Write failing strict-configuration and cold-start cadence tests**

```python
def test_cold_start_week_is_2_3_2_3_2_3_2():
    policy = PublicationCadencePolicy(
        audience_timezone="America/New_York",
        third_days_per_week=3,
        learning_min_posts=30,
    )
    monday = date(2026, 8, 24)
    counts = [policy.choose(monday + timedelta(days=i), []).post_count for i in range(7)]
    assert counts == [2, 3, 2, 3, 2, 3, 2]


@pytest.mark.parametrize("value", ["", "0", "-1", "true", "3.0", " 3"])
def test_third_day_count_fails_closed(monkeypatch, value):
    monkeypatch.setenv("THIRD_POST_DAYS_PER_WEEK", value)
    with pytest.raises(ValueError):
        load_config_fresh()
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `venv/bin/python -m pytest tests/test_publication_cadence.py -v`

Expected: collection fails because `modules.publication_cadence` does not exist and the new constants are absent.

- [ ] **Step 3: Implement the exact cadence decision and strict defaults**

```python
@dataclass(frozen=True)
class CadenceDecision:
    post_count: int
    third_post_weekdays: tuple[int, int, int]
    reason: str


class PublicationCadencePolicy:
    _COLD_THIRD_DAYS = (1, 3, 5)  # Tue, Thu, Sat; Monday is zero.

    def choose(self, local_date, samples):
        mature = self._mature_samples(local_date, samples)
        if len(mature) < self.learning_min_posts:
            selected = self._COLD_THIRD_DAYS
            reason = "cold_start"
        else:
            selected = self._rank_three_weekdays(mature)
            reason = "performance_weighted"
        return CadenceDecision(
            post_count=3 if local_date.weekday() in selected else 2,
            third_post_weekdays=selected,
            reason=reason,
        )
```

Use a deterministic smoothed weekday score:

```python
prior_engagements = 5 if weekday in self._COLD_THIRD_DAYS else 4
score = (sum_engagements + prior_engagements) / (sum_impressions + 100)
```

Sort by descending score and then ascending weekday; return exactly three unique weekdays. Reject malformed, future, naive, immature (under 24 hours), post-dated, negative, and engagement-greater-than-impressions samples. Do not inspect tweet text.

Set strict defaults:

```python
POSTS_PER_DAY=2
THIRD_POST_DAYS_PER_WEEK=3
APPROVED_QUEUE_TARGET=14
PENDING_REVIEW_LIMIT=5
DRAFT_GENERATION_DAILY_CAP=5
MIDDAY_WINDOW=13:00-15:30
MIN_POST_GAP_HOURS=4
THIRD_POST_TIMING_MIN_POSTS=30
```

Keep `POSTS_PER_DAY` as the base count and validate it is exactly 2. Validate third days are exactly 3 for this release, queue target is at least 14, pending limit is at most the target, and generation cap is at least the pending limit.

- [ ] **Step 4: Add learned-ranking, timezone, maturity, and deterministic-tie tests**

```python
def test_after_30_samples_exactly_three_best_weekdays_are_selected():
    samples = mature_weekday_samples({0: 1, 1: 8, 2: 2, 3: 9, 4: 3, 5: 7, 6: 1})
    decision = policy.choose(date(2026, 9, 7), samples)
    assert decision.third_post_weekdays == (1, 3, 5)
    assert decision.reason == "performance_weighted"
```

Add negative controls for 29 samples, zero impressions, malformed records, future measurements, alternate UTC offsets, and exact ties.

- [ ] **Step 5: Run Task 1 tests and existing configuration tests**

Run: `venv/bin/python -m pytest tests/test_publication_cadence.py tests/test_end_to_end_dry_run.py -q`

Expected: all selected tests pass with only the existing Tweepy `imghdr` warning.

- [ ] **Step 6: Commit Task 1**

```bash
git add modules/publication_cadence.py config.py .env.example tests/test_publication_cadence.py tests/test_end_to_end_dry_run.py
git commit -m "feat: choose flexible daily post cadence"
```

---

### Task 2: Expand adaptive timing and SQLite plans to three positions

**Files:**
- Modify: `modules/adaptive_timing.py:15-290`
- Modify: `modules/database.py:580-720, 2300-2500`
- Modify: `tests/test_adaptive_timing.py`
- Modify: `tests/test_adaptive_publication.py`
- Modify: `tests/test_approved_post_queue.py`

**Interfaces:**
- Consumes: `CadenceDecision.post_count` from Task 1.
- Produces: `DailyTimingDecision(times: tuple[datetime, ...], bucket_ids: tuple[str, ...], reason: str)` with exactly two or three entries.
- Produces: `AdaptiveTimingPolicy.choose(local_date, installation_id, samples, *, post_count: int) -> DailyTimingDecision`.
- Produces: `Database.create_or_get_publication_positions(...) -> list[dict]` containing two or three stable rows.

- [ ] **Step 1: Write RED tests for three windows and existing-database migration**

```python
def test_three_post_day_uses_all_three_windows_with_four_hour_gaps():
    decision = timing.choose(DAY, "install-1", [], post_count=3)
    assert len(decision.times) == 3
    assert [value.tzinfo.key for value in decision.times] == [
        "America/New_York", "America/New_York", "America/New_York",
    ]
    assert decision.times[1] - decision.times[0] >= timedelta(hours=4)
    assert decision.times[2] - decision.times[1] >= timedelta(hours=4)
    assert decision.bucket_ids[1].startswith("midday:")


def test_legacy_two_position_database_migrates_without_losing_plans(tmp_path):
    create_legacy_database_with_two_plans(tmp_path / "legacy.db")
    db = Database(tmp_path / "legacy.db")
    assert [row["position"] for row in db.list_publication_positions()] == [1, 2]
    assert sqlite_schema_accepts_position_three(db)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `venv/bin/python -m pytest tests/test_adaptive_timing.py tests/test_adaptive_publication.py -k 'three_post or legacy_two_position' -v`

Expected: failures because timing accepts only two windows and SQLite has `CHECK(position IN (1, 2))`.

- [ ] **Step 3: Generalize timing to two or three positions**

Add `midday_window` to `AdaptiveTimingPolicy.__init__`. Store ordered windows as:

```python
self._windows = (
    ("morning", TimeWindow.parse(morning_window)),
    ("midday", TimeWindow.parse(midday_window)),
    ("evening", TimeWindow.parse(evening_window)),
)
```

For `post_count=2`, select morning and evening. For `post_count=3`, select all three. Reject bool, noninteger, and values outside `{2, 3}`. Validate non-overlap and that a four-hour gap is possible between every selected adjacent pair. Generalize cold and learned bucket selection without changing the existing bucket scoring formula.

- [ ] **Step 4: Implement a crash-atomic publication-plan schema migration**

Inside the existing schema initialization transaction, detect the old table SQL through `sqlite_master`. When it contains `position IN (1, 2)`, rebuild it as `publication_plans_v3` with `CHECK(position IN (1, 2, 3))`, copy every column and row, drop the old table, rename the new table, and recreate `uq_publication_plans_active_draft` before commit.

The migration must:

- run under `BEGIN IMMEDIATE` before any concurrent constructor can inspect the schema;
- preserve IDs, revisions, statuses, claim tokens, tweet IDs, timestamps, and reasons exactly;
- remain idempotent after restart;
- roll back both DDL and copied rows on a hard crash;
- preserve `PRAGMA integrity_check=ok` and the partial unique active-draft index.

- [ ] **Step 5: Generalize plan persistence and decoding**

Accept `len(decision.times) in {2, 3}` and the same length for bucket IDs. Accept `midday:[0-9]{1,2}` in timing reasons. Require consecutive positions beginning at 1. Existing rows are authoritative for their already-created local date: a valid two-row current day remains two-row after deployment and a new Tuesday can create three rows.

```python
expected_count = len(decision.times)
if existing:
    decoded = [self._decode_publication_plan(row) for row in existing]
    positions = [row["position"] for row in decoded if row]
    return decoded if positions in ([1, 2], [1, 2, 3]) else []
```

- [ ] **Step 6: Add concurrent constructor and subprocess hard-crash tests**

Use real SQLite files and spawned processes. Assert 12 concurrent `Database()` constructors all succeed, one table exists, old plans remain byte-for-byte equivalent, and only one `(local_date, position)` row can exist. Add a subprocess that exits after the copy but before commit; reopening must recover the old table and a subsequent constructor must complete the migration.

- [ ] **Step 7: Run Task 2 and migration regression suites**

Run: `venv/bin/python -m pytest tests/test_adaptive_timing.py tests/test_adaptive_publication.py tests/test_approved_post_queue.py tests/test_media_lifecycle.py -q`

Expected: all tests pass; no plan, media binding, or SQLite concurrency regression.

- [ ] **Step 8: Commit Task 2**

```bash
git add modules/adaptive_timing.py modules/database.py tests/test_adaptive_timing.py tests/test_adaptive_publication.py tests/test_approved_post_queue.py tests/test_media_lifecycle.py
git commit -m "feat: support three-position publication days"
```

---

### Task 3: Integrate cadence with planning, replenishment, publishing, and status

**Files:**
- Modify: `modules/publication_queue.py:298-690`
- Modify: `modules/draft_pipeline.py:530-565`
- Modify: `modules/telegram_controller.py:270-335`
- Modify: `main.py:285-355, 680-750`
- Modify: `tests/test_adaptive_publication.py`
- Modify: `tests/test_end_to_end_dry_run.py`
- Modify: `tests/test_telegram_workflows.py`

**Interfaces:**
- Consumes: `PublicationCadencePolicy` and generalized timing from Tasks 1–2.
- Produces: `PublicationPlanner(..., cadence_policy=...)` whose `ensure_day()` creates two or three positions.
- Produces: `DraftPipeline.create_for_queue_with_outcome(anchor, *, daily_draft_cap: int)`.
- Preserves: `PublicationPlanner.publish_due()` at-most-once behavior with a hard daily maximum of three.

- [ ] **Step 1: Write RED integration tests for Tuesday/Sunday planning and five-draft replenishment**

```python
def test_tuesday_plans_three_distinct_approved_drafts():
    seed_three_approved_drafts(db)
    plans = planner.reconcile(TUESDAY_NOW)
    assert [row["position"] for row in plans] == [1, 2, 3]
    assert len({row["draft_id"] for row in plans}) == 3


def test_sunday_remains_two_positions():
    assert len(planner.reconcile(SUNDAY_NOW)) == 2


def test_replenisher_passes_configured_cap_to_content_planner():
    replenisher.run(NOW)
    assert pipeline.calls == [{"daily_draft_cap": 5}]
```

- [ ] **Step 2: Run RED integration tests**

Run: `venv/bin/python -m pytest tests/test_adaptive_publication.py tests/test_end_to_end_dry_run.py -k 'tuesday or sunday or configured_cap' -v`

Expected: two-position assumptions and hard-coded `daily_draft_cap=4` fail.

- [ ] **Step 3: Inject and apply cadence once per planning cycle**

`PublicationPlanner.ensure_day()` must read timing samples once, freeze them as a tuple, compute cadence, then compute timing:

```python
samples = tuple(self.timing_sample_provider(current) or ())
cadence = self.cadence_policy.choose(local_date, samples)
timing = self.timing_policy.choose(
    local_date,
    installation_id,
    samples,
    post_count=cadence.post_count,
)
return self.db.create_or_get_publication_positions(local_date, timing, current)
```

Return `[]` on malformed provider results or exceptions. Do not read metrics, installation ID, or clock twice. Generalize `reconcile()` from exactly two to `{2, 3}` plans and retain category/media diversity over all positions.

- [ ] **Step 4: Generalize dry-run and live due processing**

Process ordered due plans with a hard maximum of three terminal successes per audience date. A missed position is marked by the existing grace logic and is not moved to another window. Preserve ambiguous-X `unknown`, definite rejection restore, media verification, pause, exact revision, and no-retry behavior.

- [ ] **Step 5: Pass the configured generation cap through the queue boundary**

Change the pipeline signature to:

```python
def create_for_queue_with_outcome(self, anchor, *, daily_draft_cap: int):
    if type(daily_draft_cap) is not int or daily_draft_cap <= 0:
        return None, "rejected"
    plan = self.planner.plan(anchor, daily_draft_cap=daily_draft_cap)
```

`QueueReplenisher.run()` passes `self.daily_generation_cap`. Existing test fakes must accept and record the keyword argument. The replenishment claim remains the authoritative successful-generation cap.

- [ ] **Step 6: Update `/status` to report dynamic positions and queue counts**

Use `db.get_queue_counts()` and list up to three publication positions. Output safe scalar lines for approved inventory/14, pending/5, generation used/5, today target 2 or 3, cadence reason, ET/Rome time pairs, `DRY_RUN`, and pause state. Never include draft text, translation, source body, or model errors.

- [ ] **Step 7: Run planner, publisher, Telegram, and end-to-end tests**

Run: `venv/bin/python -m pytest tests/test_adaptive_publication.py tests/test_publisher.py tests/test_telegram_workflows.py tests/test_end_to_end_dry_run.py tests/test_approved_post_queue.py -q`

Expected: all tests pass, including two- and three-position dry-run simulations with zero fake X writes.

- [ ] **Step 8: Commit Task 3**

```bash
git add modules/publication_queue.py modules/draft_pipeline.py modules/telegram_controller.py main.py tests/test_adaptive_publication.py tests/test_end_to_end_dry_run.py tests/test_telegram_workflows.py tests/test_approved_post_queue.py
git commit -m "feat: schedule flexible approved volume"
```

---

### Task 4: Add atomic manual-draft validation and persistence

**Files:**
- Modify: `modules/database.py:1400-2150, 4400-4850`
- Modify: `modules/draft_pipeline.py:100-820`
- Modify: `modules/review_translation.py:20-110`
- Create: `tests/test_manual_post_queue.py`

**Interfaces:**
- Produces: `ReviewTranslator.validate(english_text: str, italian_text: str) -> Optional[ReviewTranslation]`.
- Produces: `DraftPipeline.create_manual_from_telegram_session(...) -> tuple[Optional[dict], str]`.
- Produces: `Database.create_manual_queue_draft_consuming_state_atomic(...) -> tuple[Optional[dict], str]`.
- Outcome allowlist: `created`, `already_applied`, `session_conflict`, `rejected`, `no_eligible_source`, `media_unavailable`.

- [ ] **Step 1: Write RED canonical-gate and exact-copy tests**

```python
def test_manual_copy_bypasses_generation_but_uses_fact_novelty_and_score_gates():
    draft, outcome = pipeline.create_manual_from_telegram_session(
        text="Exact operator copy.",
        category="founder_journey",
        source_ids=[founder_source_id],
        media_id=None,
        translation_it="Testo esatto dell'operatore.",
        state_key=SESSION_KEY,
        expected_state_value=SESSION_JSON,
        session_token=TOKEN,
    )
    assert outcome == "created"
    assert draft["text"] == "Exact operator copy."
    assert generator.calls == []
    assert fact_guard.calls == 1
    assert scorer.calls == 1
```

Add RED cases for 281 characters, unsafe URL, unsupported number, unverified/revoked/expired source, category/source mismatch, semantic duplicate, score 74, exact score 75, and founder opinion attempting a product/customer/market claim.

- [ ] **Step 2: Run manual domain tests and verify RED**

Run: `venv/bin/python -m pytest tests/test_manual_post_queue.py -k 'manual_copy or founder or exact_score' -v`

Expected: failures because neither manual API exists.

- [ ] **Step 3: Factor translation validation without changing generated translations**

Move the current URL, hashtag, numeric-token, numeric-occurrence, UTF-8, size, quote-wrapper, and code-fence checks into:

```python
def validate(self, english_text, italian_text):
    # Return ReviewTranslation only when every existing invariant passes.
```

`translate()` calls the provider once and then delegates to `validate()`. Operator-supplied translation calls `validate()` directly and never calls Groq.

- [ ] **Step 4: Add the canonical manual-copy pipeline boundary**

Use `_source_context()` and `_validate_copy()` directly; never call `_prepare()` or the generator. Require a category in `content_planner.PORTFOLIO` and require every selected source type to be allowed by `content_planner.SOURCE_TYPES[category]`. Use an aware `now_fn()` value and a deterministic microsecond derived from the session token for the internal unique `intended_slot`. Use publication key `telegram-manual:<session_token>`.

Call the new database API only after all external fact/scoring work finishes. Audit safe details only:

```python
{
    "origin": "manual",
    "source_ids": safe_source_ids,
    "scores": prepared.score_data,
}
```

- [ ] **Step 5: Write RED SQLite transaction, media, replay, and crash tests**

Use real SQLite and real media identity records. Cover:

```python
def test_manual_draft_and_session_consume_commit_together(): ...
def test_manual_draft_rollback_preserves_session_and_media(): ...
def test_same_session_replay_returns_exact_already_applied(): ...
def test_different_payload_same_token_is_rejected(): ...
def test_two_workers_create_one_manual_draft(): ...
def test_hard_crash_before_commit_leaves_session_and_no_draft(): ...
def test_media_reservation_is_in_same_transaction(): ...
```

- [ ] **Step 6: Implement atomic DB persistence**

Validate exact scalar/list/dict types and JSON with `allow_nan=False`. If `media_id` is present, acquire its trusted media-root mutation lock before `BEGIN IMMEDIATE`; otherwise begin directly. Inside the same transaction:

1. compare `bot_state.value` with the exact expected session JSON;
2. check publication key replay before inserting;
3. revalidate source trust/expiry and category-independent source eligibility;
4. insert one `post_drafts` row with exact English text;
5. insert one safe `pending_approval` evaluation with `origin=manual`;
6. insert one `editorial_queue` row as `ready` when validated Italian is supplied, otherwise `pending`;
7. reserve and bind the exact media identity when selected;
8. delete the exact session row with a CAS predicate;
9. commit all effects together.

An exact replay compares text, category, source JSON, score JSON, intended slot, media ID, translation status/text, publication key, and audit origin before returning `already_applied`.

- [ ] **Step 7: Run manual domain and SQLite concurrency tests**

Run: `venv/bin/python -m pytest tests/test_manual_post_queue.py tests/test_draft_pipeline.py tests/test_draft_pipeline_sqlite.py tests/test_media_lifecycle.py -q`

Expected: all tests pass with no partial row, duplicate, stale media binding, or session loss.

- [ ] **Step 8: Commit Task 4**

```bash
git add modules/database.py modules/draft_pipeline.py modules/review_translation.py tests/test_manual_post_queue.py tests/test_draft_pipeline.py tests/test_draft_pipeline_sqlite.py tests/test_media_lifecycle.py
git commit -m "feat: persist exact manual queue drafts"
```

---

### Task 5: Implement the restart-safe `/newpost` Telegram workflow

**Files:**
- Modify: `modules/telegram_controller.py:35-55, 95-145, 270-450, 810-1160, 1260-1430`
- Modify: `main.py:350-390`
- Modify: `tests/test_manual_post_queue.py`
- Modify: `tests/test_telegram_workflows.py`
- Modify: `tests/test_telegram_controller.py`

**Interfaces:**
- Consumes: Task 4 manual pipeline and translation validator.
- Produces command `/newpost` and callback prefixes `manual:category:`, `manual:source:`, `manual:sources_done`, `manual:media:`, and `manual:translation:`.
- Extends persisted session kind `manual_post` with steps `text`, `category`, `sources`, `media`, `translation_mode`, and `translation_text`.

- [ ] **Step 1: Write RED authorization, session, and callback-flow tests**

```python
def test_newpost_exact_text_survives_restart_to_bilingual_card():
    assert controller.process_update(command_update(1, "/newpost")) == "manual_text_input"
    assert controller.process_update(text_update(2, ENGLISH)) == "manual_category_input"
    restarted = controller_for_same_database()
    choose_category(restarted, "founder_journey")
    choose_source(restarted, founder_source_id)
    finish_sources(restarted)
    choose_no_media(restarted)
    choose_manual_translation(restarted)
    assert restarted.process_update(text_update(8, ITALIAN)) == "manual_draft_created"
    assert latest_card.english == ENGLISH
    assert latest_card.italian == ITALIAN
```

Add unauthorized, malformed mixed-subtype, expired session, callback replay, concurrent callback, cancel, restart at every step, and callback-data-length tests.

- [ ] **Step 2: Run Telegram workflow tests and verify RED**

Run: `venv/bin/python -m pytest tests/test_manual_post_queue.py tests/test_telegram_workflows.py -k 'newpost or manual_' -v`

Expected: `/newpost` is unknown and manual callbacks are rejected.

- [ ] **Step 3: Add strict persisted session schemas**

Extend `_SESSION_KINDS` and `_valid_session_payload()` with exact key sets for each step. Store only:

- English text up to 280 characters;
- one allowlisted category;
- a unique list of at most three positive SQLite source IDs;
- optional positive media ID;
- translation mode `auto` or `manual`;
- no Italian text until the terminal manual-translation message.

The existing 8192-byte session ceiling remains. Every transition uses `_replace_session()` CAS and every terminal creation uses the Task 4 atomic consume API.

- [ ] **Step 4: Implement safe category, source, and media keyboards**

Category buttons use the five exact `content_planner.PORTFOLIO` keys. Source buttons list at most ten currently verified eligible sources compatible with the chosen category, using only safe type/title labels and numeric IDs. Allow toggling up to three IDs and include a `Fonti completate` button. Media buttons list at most five currently available media records plus `Solo testo`; display sanitized filename/description but send only the numeric ID in callback data.

If no compatible source exists, keep the session at `sources` and direct the operator to `/ideas`. Founder opinion uses an existing verified `founder_note` whose `metadata.publishable` is exact `True`; it cannot use the post text as its own source.

- [ ] **Step 5: Implement translation choice and terminal creation**

- `manual:translation:auto` calls the manual pipeline with `translation_it=None`, persists one pending translation, invokes `queue_service.retry_pending_translations(now, limit=1, draft_id=id)`, and sends a card only if the refreshed status is `ready`.
- `manual:translation:manual` moves to `translation_text`; the next message is checked through `ReviewTranslator.validate()` before calling the manual pipeline.
- On rate limit, send “Bozza salvata; traduzione in preparazione.” and let the existing 30-minute retry job finish it.
- On a canonical gate rejection, preserve or recreate the session at `text` with the safe reason code and no draft.
- On `already_applied`, fetch and show the one existing card without creating another row.

- [ ] **Step 6: Add manual media preview and approval integration tests**

Prove text-only, verified photo, verified video, stale/missing/tampered media fail-closed, auto-translation pending/retry, manual translation URL/number mismatch rejection, edit, approve, discard, and later planner assignment. Assert the card shows complete English and Italian text in separate messages when Telegram caption limits would truncate either one.

- [ ] **Step 7: Add `/newpost` to help and dependency wiring**

Wire the existing `draft_pipeline`, `queue_service`, and `review_translator` through `main.py` without constructing any new X client. Add `/newpost — aggiungi un post esatto alla coda` to `/help`. Keep `/ideas` as source intake only.

- [ ] **Step 8: Run all Telegram, transport, media, and manual tests**

Run: `venv/bin/python -m pytest tests/test_manual_post_queue.py tests/test_telegram_workflows.py tests/test_telegram_controller.py tests/test_telegram_api.py tests/test_media_lifecycle.py -q`

Expected: all pass; no raw path, raw update, token, translation, or untrusted metadata appears in logs/audits.

- [ ] **Step 9: Commit Task 5**

```bash
git add modules/telegram_controller.py main.py tests/test_manual_post_queue.py tests/test_telegram_workflows.py tests/test_telegram_controller.py tests/test_telegram_api.py tests/test_media_lifecycle.py
git commit -m "feat: add manual posts from Telegram"
```

---

### Task 6: Complete safety verification, documentation, and DRY_RUN deployment

**Files:**
- Modify: `README.md`
- Modify: `SETUP.md`
- Modify: `.env.example`
- Modify: `tests/test_x_write_safety.py`
- Modify: `tests/test_end_to_end_dry_run.py`
- Create: `tests/test_flexible_queue_acceptance.py`

**Interfaces:**
- Consumes all Tasks 1–5.
- Produces operator documentation and acceptance evidence; introduces no production capability.

- [ ] **Step 1: Write end-to-end acceptance tests before documentation changes**

Cover a complete cold-start week, one learned week, queue growth to 14, five pending reviews, automatic generation stopping at both limits, manual text/media draft approval, two- and three-position planning, due dry-run simulations, restart, pause, and zero X writes.

```python
def test_three_position_dry_run_never_calls_x():
    approve_three_distinct_drafts(agent)
    plans = agent.publication_planning_cycle(now=TUESDAY_MORNING)
    for due in sorted(datetime.fromisoformat(row["scheduled_for"]) for row in plans):
        agent.adaptive_publish_cycle(now=due + timedelta(minutes=1))
    assert [row["status"] for row in db.list_publication_positions()] == [
        "simulated", "simulated", "simulated",
    ]
    assert fake_x.write_calls == []
```

- [ ] **Step 2: Run acceptance tests and fix only feature-scoped failures**

Run: `venv/bin/python -m pytest tests/test_flexible_queue_acceptance.py tests/test_x_write_safety.py tests/test_end_to_end_dry_run.py -v`

Expected: all pass and the write-safety test finds no follow, unfollow, like, favorite, repost, retweet, reply, comment, or DM call.

- [ ] **Step 3: Update operator documentation**

Document:

- weekly `2/3/2/3/2/3/2` cold-start cadence and US Eastern windows;
- learned third-day behavior after 30 mature samples;
- target 14, pending 5, generation cap 5;
- `/newpost` English/category/source/media/translation/approval steps;
- `/ideas` is for sources, not exact posts;
- Groq free-tier rate limiting retries safely and never lowers gates;
- no automatic X engagement;
- `DRY_RUN=false` requires separate explicit authorization after acceptance.

- [ ] **Step 4: Run the complete verification gate**

Run:

```bash
venv/bin/python -m pytest -q
venv/bin/python -m compileall -q config.py main.py modules tests
venv/bin/python -m pip check
git diff --check
rg -n "create_friendship|destroy_friendship|create_favorite|favorite|follow_user|unfollow|send_direct_message|reply|retweet" modules main.py
rg -n "create_tweet" modules main.py
```

Expected:

- full suite passes with only the pre-existing Tweepy `imghdr` warning;
- compile and dependency checks exit zero;
- mutation scan has no production engagement call;
- `create_tweet` appears only inside `TwitterClient.post_tweet`;
- worktree contains only planned files.

- [ ] **Step 5: Commit Task 6**

```bash
git add README.md SETUP.md .env.example tests/test_x_write_safety.py tests/test_end_to_end_dry_run.py tests/test_flexible_queue_acceptance.py
git commit -m "test: verify flexible manual queue safety"
```

- [ ] **Step 6: Deploy to the VPS without enabling X**

Before mutation, verify local and remote worktrees are clean, remote service is active, required secret names are present without printing values, `DRY_RUN=true`, `APPROVAL_REQUIRED=true`, disk space is sufficient, and `PRAGMA integrity_check=ok`. Stop the service, create a timestamped SQLite backup, pull `main`, install requirements, and set only these non-secret values:

```dotenv
POSTS_PER_DAY=2
THIRD_POST_DAYS_PER_WEEK=3
APPROVED_QUEUE_TARGET=14
PENDING_REVIEW_LIMIT=5
DRAFT_GENERATION_DAILY_CAP=5
AUDIENCE_TIMEZONE=America/New_York
MORNING_WINDOW=08:30-10:30
MIDDAY_WINDOW=13:00-15:30
EVENING_WINDOW=18:00-20:30
MIN_POST_GAP_HOURS=4
THIRD_POST_TIMING_MIN_POSTS=30
DRY_RUN=true
APPROVAL_REQUIRED=true
```

Run `config.validate_config()`, initialize `Database` once to migrate, verify integrity and preserved plan counts, restart `flexdropin-bot`, and confirm scheduler IDs. Do not send a live Telegram or X request during deployment verification.

- [ ] **Step 7: Perform authorized Telegram DRY_RUN acceptance**

Use the authorized operator chat to create:

1. one exact text-only `/newpost` with manual Italian translation;
2. one exact `/newpost` with an existing verified image or video and automatic translation;
3. one generated bilingual draft.

Approve them, inspect `/status`, and observe one complete two-position ET day and one complete three-position ET day. Verify every due plan becomes `simulated`, all approved drafts remain reusable in the queue, media remain reserved, and posted-tweet/X-write counts do not change. Leave production in `DRY_RUN=true` and request a separate operator decision before any live activation.
