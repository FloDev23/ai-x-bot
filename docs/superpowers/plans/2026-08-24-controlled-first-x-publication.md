# Controlled First X Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy automatic source refresh safely, generate one score-75-or-higher Telegram-approved draft, publish that exact draft once on X, and return the persistent service to `DRY_RUN=true`.

**Architecture:** A small one-shot CLI fingerprints one persisted draft and delegates the only write to the existing idempotent `Publisher`; it does not instantiate the scheduler or Telegram poller. A read-only preflight protects normal deploys, while the live operational step uses a process-only environment override and always restarts the permanently dry-run service.

**Tech Stack:** Python 3.11, argparse, SHA-256, existing SQLite `Database`, `Publisher`, `TwitterClient`, systemd, SSH, GitHub/Vercel deployment.

## Global Constraints

- Complete the website-feed plan and automatic-source-refresh plan first, with both repositories clean and reviewed.
- Permanent VPS configuration remains exactly `DRY_RUN=true` and `APPROVAL_REQUIRED=true`.
- Never print `.env`, X credentials, Telegram token/chat ID, Groq key, NewsAPI key, raw Telegram updates, or HTTP response bodies.
- The one-shot command targets exactly one positive integer draft ID and one immutable SHA-256 fingerprint.
- The draft must still be `approved`, due, inside the existing five-minute grace window, unclaimed, and byte-for-byte identical to the inspected snapshot.
- The existing `Publisher` remains the sole X write boundary; no retry occurs after an ambiguous response.
- Stop the service before the one-shot write and restart it in cleanup even when publication fails.
- Do not perform the live X command until the user explicitly approves the exact final draft immediately beforehand.
- A live result is accepted only when the X tweet ID and SQLite publication audit agree; `publication_unknown` requires manual reconciliation and no retry.

---

### Task 1: One-shot draft fingerprint and publication CLI

**Files:**
- Create: `scripts/__init__.py`
- Create: `scripts/publish_once.py`
- Modify: `modules/publisher.py`
- Create: `tests/test_publish_once.py`
- Modify: `tests/test_publisher.py`

**Interfaces:**
- Consumes: `Database.get_post_draft`, `Publisher.publish`, `TwitterClient`, and existing configuration.
- Produces: `draft_fingerprint(draft) -> str`, `inspect_draft(db, draft_id) -> Dict`, `publish_exact_draft(db, x_client, draft_id, expected_fingerprint, now) -> PublishResult`, and CLI subcommands `inspect`/`publish`.

- [ ] **Step 1: Write RED fingerprint tests**

Create one approved fake/SQLite draft and assert:

```python
fingerprint = draft_fingerprint(draft)
assert re.fullmatch(r"[0-9a-f]{64}", fingerprint)
assert fingerprint == draft_fingerprint(dict(draft))
```

Mutate each of `id`, `revision`, `publication_key`, `text`, `category`,
`source_ids`, `score_data`, `intended_slot`, `media_id`, `approved_at`,
`approved_by`, `status`, and `published_tweet_id`; each mutation must change
the fingerprint. Add cyclic/non-JSON-safe/malformed draft tests that fail closed
without calling hostile `__str__` methods.

- [ ] **Step 2: Run fingerprint tests to verify RED**

Run: `venv/bin/python -m pytest tests/test_publish_once.py -k fingerprint -v`

Expected: FAIL with missing `scripts.publish_once`.

- [ ] **Step 3: Implement canonical fingerprinting**

```python
FINGERPRINT_FIELDS = (
    "id", "revision", "publication_key", "text", "category",
    "source_ids", "score_data", "intended_slot", "media_id",
    "approved_at", "approved_by", "status", "published_tweet_id",
)

def draft_fingerprint(draft):
    canonical = {field: draft.get(field) for field in FINGERPRINT_FIELDS}
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
```

Require an exact mapping, exact positive integer ID/revision, list of exact
positive source IDs, mapping score data, exact strings for immutable text/key/
category/slot, `status == "approved"`, empty `published_tweet_id`, and
JSON-safe values before hashing. Convert all failures to
`ValueError("invalid_draft_snapshot") from None`.

- [ ] **Step 4: Write RED inspection tests**

Assert `inspect_draft` returns only:

```python
{
    "draft_id": 7,
    "revision": 2,
    "intended_slot": "2030-01-10T14:00:00+01:00",
    "score_total": 88,
    "has_media": False,
    "fingerprint": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
}
```

The returned/logged object must not contain draft text, media path, source
payload, approval identity, credentials, or arbitrary score reasoning. Missing,
unapproved, published, malformed, and boolean IDs raise sanitized local errors.

- [ ] **Step 5: Implement inspection and verify GREEN**

Implement `inspect_draft(db, draft_id)` with one read and
`hmac.compare_digest` reserved for later fingerprint comparison.

Run: `venv/bin/python -m pytest tests/test_publish_once.py -k "fingerprint or inspect" -v`

Expected: PASS.

- [ ] **Step 6: Write RED exact-publication tests**

Using the existing fake X and real SQLite helpers, cover:

- wrong fingerprint, wrong draft ID, changed revision/text/media/status/slot,
  missing score, not due, expired, paused, and persistent dry-run gate: zero X
  calls;
- an approved-to-approved revision mutation inserted after CLI validation but
  before the Publisher read/claim: zero X calls;
- exact approved snapshot: one `post_tweet` call and `published` result;
- second call: no second X call and non-success already-published result;
- X timeout/connection/server ambiguity: one attempted call,
  `publication_unknown`, no retry, reserved media preserved, no published audit;
- definite 4xx rejection: one call and `publication_failed`;
- finalization failure after X: `publication_unknown`, no retry;
- all logs/output omit draft text and injected secret/error sentinels.

- [ ] **Step 7: Run exact-publication tests to verify RED**

Run: `venv/bin/python -m pytest tests/test_publish_once.py -k publish -v`

Expected: FAIL because `publish_exact_draft` and CLI are missing.

- [ ] **Step 8: Bind Publisher to the inspected revision**

Extend the existing public method compatibly:

```python
def publish(self, draft_id, now=None, expected_revision=None):
    draft = self.db.get_post_draft(draft_id)
    if expected_revision is not None:
        if (
            type(expected_revision) is not int
            or expected_revision <= 0
            or draft is None
            or draft.get("revision") != expected_revision
        ):
            return PublishResult("snapshot_changed")
    # Continue through the existing due/pause/dry-run gates and the existing
    # claim_post_draft_for_publication(draft_id, draft["revision"]) CAS.
```

The optional argument preserves every current caller. A supported draft
mutation increments `revision`; a mutation before the Publisher read returns
`snapshot_changed`, and a mutation between read and claim loses the existing
revision CAS before any X call.

- [ ] **Step 9: Implement fingerprint comparison and Publisher delegation**

```python
def publish_exact_draft(db, x_client, draft_id, expected_fingerprint, now):
    draft = db.get_post_draft(draft_id)
    if draft is None:
        return PublishResult("not_found")
    actual = draft_fingerprint(draft)
    if not hmac.compare_digest(actual, expected_fingerprint):
        return PublishResult("snapshot_changed")
    publisher = Publisher(
        db,
        x_client,
        dry_run=False,
        clock=lambda: now,
        grace_seconds=PUBLISH_GRACE_SECONDS,
        timezone_name=BOT_TIMEZONE,
    )
    return publisher.publish(
        draft_id,
        now=now,
        expected_revision=draft["revision"],
    )
```

The fingerprint is an extra operator guard, not a replacement for the
Publisher revision/status/media CAS. Do not add a loop or retry. Add a
deterministic hook/barrier test in `tests/test_publisher.py` proving the
approved-to-approved mutation cannot reach X.

- [ ] **Step 10: Implement strict CLI commands**

`inspect --draft-id N` validates configuration and requires persistent
`DRY_RUN=true`; it prints only the bounded inspection dictionary.

`publish --draft-id N --fingerprint HEX --confirm PUBLISH_ONE_APPROVED_FLEXDROPIN_DRAFT`
requires process configuration `DRY_RUN=false` and
`APPROVAL_REQUIRED=true`. It instantiates `Database()` and `TwitterClient()`
only after argument/config validation, invokes `publish_exact_draft` once, and
prints only status plus a validated tweet ID when present.

Use exit codes: `0` published, `2` definite/no-write refusal, `3`
`publication_unknown`, `4` configuration/argument error. Catch top-level
exceptions and print only an allowlisted error code/type.

- [ ] **Step 11: Verify the CLI and commit**

Run: `venv/bin/python -m pytest tests/test_publish_once.py tests/test_publisher.py tests/test_x_write_safety.py -v`

Expected: PASS with no network.

```bash
git add scripts/__init__.py scripts/publish_once.py modules/publisher.py tests/test_publish_once.py tests/test_publisher.py
git commit -m "feat: add one-shot approved publisher"
```

### Task 2: Read-only production preflight and safer deploy

**Files:**
- Create: `scripts/preflight_production.py`
- Create: `tests/test_production_preflight.py`
- Modify: `deploy.sh`
- Modify: `SETUP.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: current `config.validate_config`, Python `sqlite3`, SQLite integrity check.
- Produces: `run_preflight(require_dry_run=True, db_path="bot_data.db") -> Dict` and a deploy step that refuses unsafe persistent configuration before systemd restart.

- [ ] **Step 1: Write RED preflight tests**

Test `run_preflight` returns only:

```python
{
    "config_valid": True,
    "approval_required": True,
    "dry_run": True,
    "database_integrity": "ok",
    "news_key_present": False,
    "trusted_domain_count": 0,
}
```

Patch configuration values and SQLite to prove it fails on `DRY_RUN=false`
when `require_dry_run=True`, noncanonical/false approval, malformed Telegram
configuration, missing required secrets, non-`ok` integrity, and database open
failure. Assert no credential value enters result, stdout, log, or exception.

- [ ] **Step 2: Run preflight tests to verify RED**

Run: `venv/bin/python -m pytest tests/test_production_preflight.py -v`

Expected: FAIL with missing `scripts.preflight_production`.

- [ ] **Step 3: Implement read-only preflight**

Call `validate_config()`, require exact booleans, then open the configured
database read-only:

```python
database_uri = Path(db_path).resolve().as_uri() + "?mode=ro"
with sqlite3.connect(database_uri, uri=True) as connection:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
```

Do not instantiate `Database`, run schema setup, or create a missing file.
Report only booleans, counts, and `ok`. The CLI accepts only
`--require-dry-run` and `--db-path`; the default deploy invocation uses both
safe defaults.

- [ ] **Step 4: Put preflight before service restart**

In `deploy.sh`, after dependency installation and before `systemctl` changes,
run:

```bash
"$VENV_DIR/bin/python" "$REPO_DIR/scripts/preflight_production.py" --require-dry-run --db-path "$REPO_DIR/bot_data.db"
```

If it exits nonzero, `set -euo pipefail` stops the deploy before either service
is restarted. Do not source or print `.env` in shell.

- [ ] **Step 5: Write a static deploy regression test and verify GREEN**

Assert the preflight invocation text occurs before the first
`systemctl restart` in `deploy.sh`, contains `--require-dry-run`, and no command
prints `.env` or any credential name/value.

Run: `venv/bin/python -m pytest tests/test_production_preflight.py tests/test_main_startup.py -v`

Expected: PASS.

- [ ] **Step 6: Document exact dry-run and one-shot procedures**

Add to `SETUP.md` and `README.md`:

- normal deploy always requires persistent dry-run;
- source-feed verification and external NewsAPI key/domain presence checks;
- Telegram approval of the exact card before live action;
- `inspect` then explicit confirmation;
- temporary process-only `DRY_RUN=false` command;
- service stop/restart cleanup;
- published/unknown/rejected outcomes;
- manual X reconciliation without retry for unknown;
- immediate post-run proof that persistent dry-run is restored.

Do not include real IPs, key filenames, tokens, chat IDs, or draft fingerprints
in committed docs.

- [ ] **Step 7: Verify and commit preflight/docs**

Run:

```bash
venv/bin/python -m pytest tests/test_production_preflight.py tests/test_publish_once.py tests/test_main_startup.py -v
venv/bin/python -m compileall -q scripts main.py config.py modules
git diff --check
```

Expected: PASS/exit 0.

```bash
git add scripts/preflight_production.py tests/test_production_preflight.py deploy.sh SETUP.md README.md
git commit -m "fix: preflight safe production deploys"
```

### Task 3: Full local verification and independent review

**Files:**
- Review: all files changed by the three implementation plans in both repositories.
- Create: `.superpowers/sdd/2026-08-24-automatic-editorial-refresh/implementation-report.md` (ignored operational report).

**Interfaces:**
- Consumes: final website and bot SHAs.
- Produces: evidence that deployment is safe to begin; no production mutation.

- [ ] **Step 1: Run website verification**

In `/Users/floriano/flo_mobile_app/flexDropin-website`:

```bash
npm test
npm run build
git diff --check
git status --short
git rev-parse HEAD
```

Expected: tests/build PASS, clean tracked worktree, recorded website SHA.

- [ ] **Step 2: Run bot verification**

In `/Users/floriano/flo_mobile_app/ai-x-bot-main`:

```bash
venv/bin/python -m pytest -v
venv/bin/python -m compileall -q main.py config.py modules scripts dashboard
venv/bin/python -m pip check
git diff --check
git status --short
git rev-parse HEAD
```

Expected: full suite PASS, one known Tweepy `imghdr` deprecation warning at
most, clean tracked worktree, recorded bot SHA.

- [ ] **Step 3: Run capability and secret scans**

Confirm the only production X write is still
`Publisher -> TwitterClient.post_tweet -> create_tweet`; no refresh/planner/
Telegram code contains follow, like, reply, repost, DM, or media-write calls.
Scan the diff and ignored report for token/key/chat-ID patterns and remove any
secret occurrence before proceeding.

- [ ] **Step 4: Request independent code review**

Use `superpowers:requesting-code-review` separately for the website SHA range
and bot SHA range. Fix every Critical or Important finding with a fresh
RED/GREEN test, rerun Steps 1–3, and record the final SHAs and reviewer verdicts.

- [ ] **Step 5: Commit any review-only fixes and confirm clean state**

Run `git show --check --oneline HEAD` and `git status --short` in both
repositories. Expected: check passes and status is empty.

### Task 4: Deploy feed and bot in permanent dry-run

**Files:**
- No source edits expected.
- Production database backup: `/home/ubuntu/ai-x-bot/backups/`.

**Interfaces:**
- Consumes: reviewed website and bot commits.
- Produces: active VPS service with automatic source refresh, permanent safety flags true, and zero X writes.

- [ ] **Step 1: Push both reviewed repositories**

Push the exact reviewed `main` commits to each `origin`. Record both remote
SHAs. Do not push an unreviewed follow-up commit.

- [ ] **Step 2: Verify the public feed after website deployment**

Poll `https://flexdropin.com/api/editorial-feed` with a bounded timeout until
the recorded website SHA is deployed. Validate status 200, JSON content type,
body <=256 KiB, exact schema/version/language, canonical English URLs, and item
count 1–100. Do not log the body; print only status, byte count, version,
language, and item count.

If GitHub push does not trigger the configured website deployment, stop and use
the repository's documented Vercel deployment workflow; do not improvise a new
project or domain.

- [ ] **Step 3: Inspect VPS prerequisites without exposing secrets**

Connect using the already authorized SSH target and run a Python preflight that
prints only:

- current bot SHA;
- `dry_run=True`;
- `approval_required=True`;
- NewsAPI key present boolean;
- trusted-domain count;
- SQLite integrity `ok`;
- service active/inactive state.

If the NewsAPI key or domain allowlist is missing, keep the bot in dry-run and
ask the operator to configure them through the VPS shell. Never request that a
secret be pasted into chat.

- [ ] **Step 4: Back up SQLite and deploy the bot**

Create `/home/ubuntu/ai-x-bot/backups` if absent. Copy `bot_data.db` to a new
timestamped file in that directory without overwriting an existing backup.
Run the hardened `deploy.sh`; it must pull the reviewed bot SHA, pass the
read-only dry-run preflight, and restart both services successfully.

- [ ] **Step 5: Run one manual source refresh in dry-run**

Invoke `FlexDropinGrowthAgent().refresh_sources_cycle()` once from the VPS
virtualenv. Print only bounded channel counts/error codes. Then query SQLite
read-only and print only source-type counts, duplicate-URL count, invalid-owned
count, and integrity result.

Acceptance for this step:

- at least one valid `owned_blog_article` exists;
- duplicate URL count is zero;
- invalid owned count is zero;
- existing external rows remain present;
- NewsAPI either imports valid rows or reports one sanitized configuration/
  channel error;
- `post_drafts` count does not change because of refresh;
- X write count remains zero.

- [ ] **Step 6: Observe one dry-run draft cycle**

Let the next scheduled cycle run, or invoke the next configured slot once
through the existing idempotent `create_draft_cycle`. Require one Telegram card
only when a candidate scores at least `75`. Verify its single source, score,
text length, fact audit, media preview state, and absence of any X write.

If every candidate scores below `75` or fails a factual gate, do not lower the
threshold. Inspect sanitized evaluation reasons, improve/refresh the source
pool if justified, and run a later slot.

### Task 5: Publish exactly one approved post and return to dry-run

**Files:**
- No source edits expected.
- Production SQLite audit only.

**Interfaces:**
- Consumes: one exact Telegram-approved draft scoring at least `75` and inside its publication window.
- Produces: one confirmed X tweet or one fail-closed terminal outcome, followed by an active permanently dry-run service.

- [ ] **Step 1: Approve the exact card in Telegram**

The operator reads the complete post and verified media preview, then presses
`Approva`. Confirm read-only that the same draft ID is `approved`, score is at
least `75`, source remains eligible, and the slot has not expired.

- [ ] **Step 2: Inspect and fingerprint without a write**

Run `scripts/publish_once.py inspect` for that exact positive draft ID. Capture
only its bounded output: draft ID, revision, slot, score total, media boolean,
and 64-hex fingerprint. Re-read the Telegram card and compare the draft ID.

- [ ] **Step 3: Request final explicit user authorization**

Report the exact draft ID, slot, score, whether media is attached, and that
permanent `DRY_RUN=true` will not be changed. Ask the user to authorize this
single X write. Do not execute the next step until that authorization is
received in the active conversation.

- [ ] **Step 4: Execute the one-shot command with cleanup**

At the exact slot and within the configured grace window:

1. resolve the approved draft ID and 64-hex fingerprint from Step 2 as explicit
   validated shell arguments;
2. stop `flexdropin-bot`;
3. install an `EXIT`, `INT`, and `TERM` cleanup trap that restarts the service;
4. run the CLI once with process-only `DRY_RUN=false`, the exact ID,
   fingerprint, and confirmation phrase
   `PUBLISH_ONE_APPROVED_FLEXDROPIN_DRAFT`;
5. capture only exit code, status, and validated tweet ID;
6. allow cleanup to restart the service.

Do not edit `.env`. Do not run the command a second time after any ambiguous
result.

- [ ] **Step 5: Verify the terminal state**

For `published`, require:

- one valid decimal X tweet ID;
- draft status `published` with the same ID;
- exactly one `posted_tweets` row with that ID;
- reserved media, if any, bound as used by that same ID;
- one successful audit and no second X call.

For `publication_unknown`, stop and manually reconcile the X account; never
retry automatically. For a definite refusal/rejection, keep the no-write audit
and diagnose before scheduling a different future draft.

- [ ] **Step 6: Prove permanent safety after cleanup**

Run the read-only production preflight and service checks. Require:

- `DRY_RUN=true`;
- `APPROVAL_REQUIRED=true`;
- `flexdropin-bot` active;
- SQLite integrity `ok`;
- scheduler contains one source-refresh job and no legacy mutation jobs;
- no second approved draft was published.

Record only sanitized counts, SHAs, outcome, and the public tweet ID in the
implementation report.
