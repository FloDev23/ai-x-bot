# Grounded Candidate Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate at most three source-grounded post candidates and persist only the highest-scoring safe candidate when it reaches the unchanged score threshold of 75.

**Architecture:** `AIGenerator` produces one source-bounded angle per call using a closed integer selector. `DraftPipeline` snapshots sources and recent posts once, evaluates candidates without intermediate persistence, stops on systemic boundary failures, and atomically persists only the stable winner. Manual Telegram edits continue to use the same single-copy gates and are not expanded into AI candidates.

**Tech Stack:** Python 3.11, Groq client, SQLite, pytest, existing `FactGuard`, `TweetScorer`, `DraftPipeline`, Telegram approval workflow.

## Global Constraints

- Work directly on `main`, as explicitly requested by the user.
- Keep `DRAFT_SCORE_THRESHOLD=75`; do not introduce a lower fallback threshold.
- Use exactly the planner-selected source bundle for every candidate; current planner output contains one source.
- Generate at most three candidates for a slot or regeneration.
- Never persist rejected candidate text or raw model reasoning.
- Never send rejected candidates to Telegram.
- Do not add X methods or call X from generation, evaluation, Telegram, or tests.
- Production must remain `DRY_RUN=true` and `APPROVAL_REQUIRED=true`.
- Use `apply_patch` for edits and preserve unrelated/user-owned files.

---

### Task 1: Closed candidate-angle generation

**Files:**
- Modify: `modules/ai_generator.py`
- Modify: `tests/test_editorial_scoring.py`
- Modify: `tests/fakes.py`
- Modify: `tests/test_draft_pipeline.py`
- Modify: `tests/test_draft_pipeline_sqlite.py`
- Modify: `tests/test_telegram_workflows.py`

**Interfaces:**
- Consumes: existing `AIGenerator.generate_grounded_tweet(category, sources, include_link)` behavior.
- Produces: `AIGenerator.generate_grounded_tweet(category, sources, include_link, candidate_index=None) -> Optional[Dict]`.
- `candidate_index` accepts only `None`, `0`, `1`, or `2`; booleans and all other values fail closed before `_complete`.
- Indices map internally to sourced contrast, overlooked sourced trend/metric, and supported operator question. Callers cannot inject free-form angle text.

- [ ] **Step 1: Write failing prompt-boundary tests**

Add tests in `tests/test_editorial_scoring.py` that capture the generation prompt and prove the closed mapping:

```python
@pytest.mark.parametrize(
    ("candidate_index", "expected"),
    [
        (0, "build the post around one sharp sourced contrast"),
        (1, "build the post around one overlooked sourced trend or metric"),
        (2, "build the post around one operator-relevant question"),
    ],
)
def test_grounded_candidate_index_selects_closed_angle(
    fake_ai, candidate_index, expected
):
    captured = {}

    def complete(_system, user, **_kwargs):
        captured["user"] = user
        return "Grounded post."

    fake_ai._complete = complete
    fake_ai.generate_grounded_tweet(
        "gym_strategy",
        [{
            "id": 8,
            "source_type": "verified_news",
            "trust_state": "verified",
            "text": "Membership reached 81 million in 2025.",
            "url": "https://industry.example/report",
            "metadata": {
                "title": "Industry report",
                "summary": "Membership reached 81 million in 2025.",
                "published_at": "2026-08-23",
                "source_name": "Industry Association",
            },
        }],
        False,
        candidate_index=candidate_index,
    )
    assert expected in captured["user"].lower()
```

Add a separate invalid-input test that passes `True`, `-1`, `3`, and a string; it asserts `None` and zero `_complete` calls. The production mutation caught by these tests is accepting caller-controlled angle instructions or mapping the wrong attempt to the wrong framing.

- [ ] **Step 2: Run the Task 1 tests and verify RED**

Run:

```bash
venv/bin/python -m pytest tests/test_editorial_scoring.py -k 'candidate_index or closed_angle' -v
```

Expected: failures because `generate_grounded_tweet` does not accept `candidate_index`.

- [ ] **Step 3: Implement the minimal closed mapping**

In `modules/ai_generator.py`, add a tuple of three constant angle instructions and a private validator:

```python
_CANDIDATE_ANGLE_INSTRUCTIONS = (
    "Build the post around one sharp sourced contrast.",
    "Build the post around one overlooked sourced trend or metric.",
    "Build the post around one operator-relevant question supported by the source.",
)

def _candidate_angle_instruction(candidate_index):
    if candidate_index is None:
        return ""
    if (
        isinstance(candidate_index, bool)
        or not isinstance(candidate_index, int)
        or not 0 <= candidate_index < len(_CANDIDATE_ANGLE_INSTRUCTIONS)
    ):
        return None
    return _CANDIDATE_ANGLE_INSTRUCTIONS[candidate_index]
```

Validate before `_complete`, insert only the mapped constant into the generation prompt, and preserve every existing source/fact restriction.

- [ ] **Step 4: Update deterministic test fakes**

Change fake generator signatures to accept `candidate_index=None`, record it, and preserve their existing output. Do not add production-only behavior to the fakes.

- [ ] **Step 5: Run Task 1 GREEN and regressions**

Run:

```bash
venv/bin/python -m pytest tests/test_editorial_scoring.py tests/test_draft_pipeline.py tests/test_draft_pipeline_sqlite.py tests/test_telegram_workflows.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add modules/ai_generator.py tests/test_editorial_scoring.py tests/fakes.py tests/test_draft_pipeline.py tests/test_draft_pipeline_sqlite.py tests/test_telegram_workflows.py
git diff --cached --check
git commit -m "feat: add grounded candidate angles"
```

---

### Task 2: Evaluate a bounded tournament without intermediate writes

**Files:**
- Modify: `modules/draft_pipeline.py`
- Modify: `tests/test_draft_pipeline.py`

**Interfaces:**
- Consumes: Task 1 `candidate_index` interface; existing `FactGuard.check`, `TweetScorer.score_draft`, and `semantic_similarity`.
- Produces: private immutable `_CandidateEvaluation(prepared, outcome, details, abort)` and a three-attempt `_prepare` tournament.
- `_validate_copy` remains the single-copy wrapper for manual edits and Telegram edit sessions.
- Recent post text is read exactly once per `_prepare` call and reused by all scoring and duplicate comparisons.

- [ ] **Step 1: Extend test doubles with per-attempt outcomes**

In `tests/test_draft_pipeline.py`, let `FakeGenerator`, `FakeGuard`, and `FakeScorer` optionally consume response sequences while preserving the current scalar defaults. Each double records candidate index, text, source object identity, and recent-list object identity.

The sequence API must be literal and deterministic, for example:

```python
generator.results = [
    {"text": "Candidate one."},
    {"text": "Candidate two."},
    {"text": "Candidate three."},
]
scorer.results = [
    {"total": 76, "hook": 7},
    {"total": 84, "hook": 9},
    {"total": 81, "hook": 8},
]
```

- [ ] **Step 2: Write RED tests for selection and stable context**

Add these tests using the sequence-enabled doubles from Step 1:

```python
def test_candidate_tournament_persists_only_highest_safe_score(pipeline_parts):
    pipeline, database, _, generator, _, scorer = pipeline_parts
    generator.results = [
        {"text": "Candidate one."},
        {"text": "Candidate two."},
        {"text": "Candidate three."},
    ]
    scorer.results = [
        {"total": 76, "hook": 7},
        {"total": 84, "hook": 9},
        {"total": 81, "hook": 8},
    ]

    draft = pipeline.create_for_slot(database.next_slot)

    assert draft["text"] == "Candidate two."
    assert draft["score_data"] == {"total": 84, "hook": 9}
    assert len(database.created_drafts) == 1
    assert generator.candidate_indices == [0, 1, 2]

def test_candidate_tournament_uses_earliest_attempt_on_equal_total(pipeline_parts):
    pipeline, database, _, generator, _, scorer = pipeline_parts
    generator.results = [
        {"text": "First tied candidate."},
        {"text": "Second tied candidate."},
        {"text": "Lower candidate."},
    ]
    scorer.results = [
        {"total": 82},
        {"total": 82},
        {"total": 80},
    ]

    draft = pipeline.create_for_slot(database.next_slot)

    assert draft["text"] == "First tied candidate."

def test_candidate_tournament_reuses_source_and_recent_snapshot(pipeline_parts):
    pipeline, database, _, generator, guard, scorer = pipeline_parts
    database.recent_texts = ["Existing unrelated operator post."]

    assert pipeline.create_for_slot(database.next_slot) is not None

    assert database.recent_calls == 1
    assert len(generator.generated) == 3
    assert len(guard.calls) == 3
    assert len(scorer.contexts) == 3
    assert all(context[0] is scorer.contexts[0][0] for context in scorer.contexts)
    assert all(context[1] is scorer.contexts[0][1] for context in scorer.contexts)
```

Assert exact call count `3`, exact candidate indices `[0, 1, 2]`, one persisted draft, and no evaluation containing candidate text or `raw_reasoning`.

- [ ] **Step 3: Run the selection tests and verify RED**

Run:

```bash
venv/bin/python -m pytest tests/test_draft_pipeline.py -k 'candidate_tournament' -v
```

Expected: failures because `_prepare` calls the generator and scorer once.

- [ ] **Step 4: Write RED tests for rejection continuation**

Add literal tests for these flows:

- first candidate has `unsupported_number`, second scores 81, third scores 79: candidate two wins;
- first candidate is a semantic duplicate, second scores 78: second can win;
- scores 70, 74, 72: no draft and one final `rejected_score` audit whose best sanitized score is 74;
- all candidates are fact-invalid: no draft and one sanitized `rejected_fact` audit;
- malformed candidate dictionary cannot win or leak raw content.

Run the new test selection and confirm each fails for the one-shot implementation.

- [ ] **Step 5: Write RED tests for fail-fast systemic errors**

Add tests that prove exact bounded calls:

- generator exception or `None`: one generation call, zero fact/scorer calls;
- fact checker exception or `claim_analysis_unavailable`: one generation and one fact call, zero scorer calls;
- scorer exception or malformed response: one generation/fact/scorer call;
- source invalid before generation: zero generator calls.

Each audit must contain only allowlisted reason codes, source IDs, attempt numbers, and safe scores.

- [ ] **Step 6: Implement internal candidate evaluation**

In `modules/draft_pipeline.py`:

1. Add immutable `_CandidateEvaluation` with fields `prepared`, `outcome`, `details`, and `abort`.
2. Extract recent loading into `_recent_texts(exclude_draft_id=None)`.
3. Extract the existing non-persisting gates into `_evaluate_copy`, whose
   keyword arguments are `text`, `category`, `safe_source_ids`, `sources`,
   `slot_iso`, and `recent_texts`.
4. Do not record inside `_evaluate_copy`; return sanitized outcomes instead.
5. Let `_validate_copy` call `_evaluate_copy`, write the single existing audit for manual edits, and return `prepared`.
6. Let `_prepare` load context/recent once, call candidate indices `0..2`, stop only when `abort` is true, and select the maximum total with stable first-attempt ties.
7. Enforce `total >= self.score_threshold` only after selecting the best safe candidate.
8. Write one sanitized final rejection audit if no winner exists.

The implementation must never include candidate text, source body, exception text, or model reasoning in audit details.

- [ ] **Step 7: Run Task 2 GREEN tests**

Run:

```bash
venv/bin/python -m pytest tests/test_draft_pipeline.py -v
```

Expected: all Task 2 and legacy manual-edit tests pass.

- [ ] **Step 8: Run the editorial safety regression set**

Run:

```bash
venv/bin/python -m pytest tests/test_content_planner.py tests/test_editorial_scoring.py tests/test_fact_guard.py tests/test_draft_pipeline.py tests/test_draft_pipeline_sqlite.py tests/test_telegram_workflows.py tests/test_x_write_safety.py -q
```

Expected: all selected tests pass and no new X mutation path appears.

- [ ] **Step 9: Commit Task 2**

```bash
git add modules/draft_pipeline.py tests/test_draft_pipeline.py
git diff --cached --check
git commit -m "feat: select the strongest safe draft"
```

---

### Task 3: SQLite, orchestration, and concurrency acceptance

**Files:**
- Modify: `tests/test_draft_pipeline_sqlite.py`
- Modify: `tests/test_end_to_end_dry_run.py`
- Modify only if a failing real integration test proves necessary: `main.py`

**Interfaces:**
- Consumes: Task 2 tournament and existing SQLite `create_or_get_post_draft` partial unique index/CAS.
- Produces: regression proof that only one draft and one Telegram card can result, even with multiple candidates and concurrent workers.

- [ ] **Step 1: Write a real-SQLite winner test**

Add a test using the real `Database` that supplies three deterministic candidate texts and scores. Assert:

- one `post_drafts` row for the intended slot;
- its text and score belong to the highest eligible candidate;
- no rejected candidate text exists in `post_drafts` or `draft_evaluations`;
- the persisted status remains `pending_approval`.

- [ ] **Step 2: Verify the SQLite test RED**

Run the exact new test with `-v`. Expected: failure because the current pipeline evaluates only one candidate.

- [ ] **Step 3: Write a concurrent two-worker test**

Use two real `Database` instances pointing to the same temporary SQLite file and a barrier immediately before persistence. Each pipeline may evaluate three candidates; after release, assert exactly one live draft for the slot and both callers resolve to that same draft ID or one safely returns the existing draft.

The test must use bounded joins and fail if either thread remains alive.

- [ ] **Step 4: Write orchestration/Telegram acceptance test**

In `tests/test_end_to_end_dry_run.py`, run `FlexDropinGrowthAgent.create_draft_cycle` with injected boundaries. Assert exactly one Telegram draft card for the winning draft, zero cards when all scores are below 75, and `FakeXClient.posts == []` in both cases.

- [ ] **Step 5: Implement only integration fixes proven necessary**

If Task 2 already satisfies these tests, make no production change. If a test exposes duplicate Telegram delivery or orchestration drift, patch only the narrow boundary demonstrated by the RED test; do not move candidate selection into `main.py`.

- [ ] **Step 6: Run Task 3 GREEN and concurrency stress**

Run:

```bash
venv/bin/python -m pytest tests/test_draft_pipeline_sqlite.py tests/test_end_to_end_dry_run.py -q
for run in {1..10}; do venv/bin/python -m pytest tests/test_draft_pipeline_sqlite.py -k 'candidate and concurrent' -q || exit 1; done
```

Expected: every run passes with no thread leak.

- [ ] **Step 7: Commit Task 3**

```bash
git add tests/test_draft_pipeline_sqlite.py tests/test_end_to_end_dry_run.py main.py
git diff --cached --check
git commit -m "test: prove candidate selection end to end"
```

Do not stage `main.py` when it is unchanged.

---

### Task 4: Independent review, full verification, and safe deployment

**Files:**
- No expected production edits; any review fix must start with its own RED test.

**Interfaces:**
- Consumes: Tasks 1–3 commits.
- Produces: reviewed commit range, deployed safe configuration, and one bounded live acceptance result.

- [ ] **Step 1: Request independent read-only review**

Provide the reviewer the base SHA before Task 1 and current HEAD. Require checks for source trust, prompt injection, candidate call bounds, failure classification, audit sanitization, stable tie selection, recent snapshot reuse, concurrency, Telegram single delivery, and zero X expansion.

- [ ] **Step 2: Address every Critical or Important finding with RED/GREEN**

Use `superpowers:receiving-code-review`, reproduce each finding on the exact code, write a failing regression, apply the minimal fix, and request re-review. Do not proceed with an open Critical or Important finding.

- [ ] **Step 3: Run fresh completion gates**

Run:

```bash
venv/bin/python -m pytest -q
venv/bin/python -m compileall -q modules tests main.py config.py
venv/bin/python -m pip check
git diff --check
git status --short
```

Expected: zero test failures, compilation exit 0, no broken requirements, clean diff checks, and only intended tracked changes before final commits.

- [ ] **Step 4: Audit the X write boundary**

Run a static scan confirming the only production tweet write remains `Publisher -> TwitterClient.post_tweet -> create_tweet`; verify no follow, unfollow, like, reply, repost, or DM mutation was added.

- [ ] **Step 5: Push `main` and deploy with safe preflight**

Push the reviewed commits. On the VPS:

1. `git pull --ff-only origin main`;
2. run `config.validate_config()`;
3. print only boolean `DRY_RUN` and `APPROVAL_REQUIRED` values;
4. require both to be `True`;
5. restart only `flexdropin-bot`;
6. verify the service is active and the deployed SHA matches local HEAD.

- [ ] **Step 6: Run one bounded live dry-run slot**

Choose a future unoccupied content slot and invoke one `create_draft_cycle`. Print only slot, draft ID/status/category/source IDs/score or the sanitized final evaluation. Acceptance is either one Telegram card with score at least 75 or a bounded rejection; any X call is a failure.

- [ ] **Step 7: Report the outcome in Italian**

State the deployed SHA, tests, review verdict, safe configuration, live candidate count/outcome, and the exact Telegram action needed from the user. Do not expose tokens, chat IDs, raw Telegram updates, `.env`, or private model reasoning.
