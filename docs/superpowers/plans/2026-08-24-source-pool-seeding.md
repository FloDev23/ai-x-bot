# Source Pool Seeding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 8–12 attributable, relevant, non-duplicate external sources to the production FlexDropin editorial database without creating drafts or performing X writes.

**Architecture:** Research produces a local JSON manifest only. The manifest is validated against the bot's existing verified-news schema, then inserted on the VPS in one SQLite transaction after a recoverable database backup and a final duplicate check. A read-only verification proves eligibility, unchanged draft counts, and unchanged safety flags.

**Tech Stack:** Python 3.11, SQLite, `modules.source_validation`, web research, SSH.

## Global Constraints

- Insert 8–12 external records, all with `source_type=verified_news`.
- Every record must have a unique HTTPS URL, title, factual summary, source name, and valid publication date.
- Prefer primary research, official statistics, and established fitness-industry organizations published in 2024–2026.
- Cover fitness participation, flexible access, gym capacity/revenue, consumer behavior, travel fitness, and community/retention as a balanced portfolio.
- Do not infer product facts or founder claims from external web pages.
- Do not create drafts, send Telegram cards, call X, or change `DRY_RUN=true` and `APPROVAL_REQUIRED=true`.
- Preserve a recoverable pre-insertion SQLite backup on the VPS.

---

### Task 1: Research and curate the source manifest

**Files:**
- Create temporarily: `/tmp/flexdropin-source-batch.json`
- Read: `docs/superpowers/specs/2026-08-24-source-pool-seeding-design.md`

**Interfaces:**
- Consumes: public web pages and the current production URL inventory.
- Produces: a JSON array with exactly 8–12 objects containing `source_type`, `text`, `url`, `metadata`, `trust_state`, and `verified_by`.

- [ ] **Step 1: Search authoritative candidate pages**

Use web search for current primary or established industry sources in the six required topic groups. Open each original page; do not rely on search snippets or secondary summaries.

- [ ] **Step 2: Verify each candidate manually**

For each page, confirm that the exact facts in `text` appear on the page, the page identifies its publisher, and its publication date is visible or provided in authoritative page metadata.

- [ ] **Step 3: Build the manifest**

Each record must use this exact shape:

```json
{
  "source_type": "verified_news",
  "text": "A concise factual summary supported directly by the page.",
  "url": "https://authoritative.example/report",
  "metadata": {
    "title": "Original page title",
    "summary": "The same concise factual summary.",
    "published_at": "2026-01-15",
    "source_name": "Authoritative publisher"
  },
  "trust_state": "verified",
  "verified_by": "codex_manual_research"
}
```

- [ ] **Step 4: Check portfolio balance and duplicates**

Reject any record whose URL already exists in the production inventory. Confirm the final list contains multiple topic groups rather than many near-identical participation statistics.

---

### Task 2: Validate the manifest locally

**Files:**
- Read: `/tmp/flexdropin-source-batch.json`
- Read: `modules/source_validation.py`

**Interfaces:**
- Consumes: the Task 1 JSON array.
- Produces: exit code 0 only when all records satisfy the production verified-news boundary and URL uniqueness.

- [ ] **Step 1: Run schema and eligibility validation**

Run a Python validation that loads the JSON with duplicate-key-safe assumptions, requires 8–12 records, rejects duplicate URLs, checks the fixed outer/metadata key sets, and calls:

```python
is_complete_verified_news({
    **record,
    "metadata": record["metadata"],
})
```

Expected: `manifest_valid count=<N>` and exit 0.

- [ ] **Step 2: Scan for unsafe or irrelevant content**

Reject source summaries containing unsupported product claims, first-person FlexDropin claims, private data, raw model reasoning, or instructions embedded in source text.

- [ ] **Step 3: Record the final source titles and links**

Keep the reviewed manifest in `/tmp` for transfer. Do not add fetched page bodies or copyrighted long-form content to Git.

---

### Task 3: Back up and insert the batch atomically

**Files:**
- Create remotely: `/home/ubuntu/ai-x-bot/backups/bot_data-before-source-seed-<UTC timestamp>.db`
- Modify remotely: `/home/ubuntu/ai-x-bot/bot_data.db`

**Interfaces:**
- Consumes: the validated Task 2 manifest.
- Produces: 8–12 new `content_sources` rows or no database change on any validation/SQL failure.

- [ ] **Step 1: Capture pre-insertion invariants**

Read and retain counts for `content_sources`, `post_drafts`, and `draft_evaluations`, plus current `DRY_RUN`, `APPROVAL_REQUIRED`, service state, and Git SHA. Do not print tokens or chat IDs.

- [ ] **Step 2: Create a consistent SQLite backup**

Use `sqlite3.Connection.backup()` from `bot_data.db` to the explicit timestamped backup path. Verify the backup with `PRAGMA integrity_check` before mutation.

- [ ] **Step 3: Revalidate and insert under one transaction**

Open `bot_data.db`, execute `BEGIN IMMEDIATE`, recheck every URL with:

```sql
SELECT 1 FROM content_sources WHERE url = ? LIMIT 1
```

If any URL exists, rollback the complete batch. Otherwise insert every record with one shared UTC timestamp for `verified_at`, `created_at`, and `updated_at`; commit once after all rows succeed.

- [ ] **Step 4: Do not restart the service**

SQLite rows are available to future planner cycles immediately. Avoid an unnecessary restart and do not invoke any draft, Telegram, publisher, or X method.

---

### Task 4: Verify production state and report

**Files:**
- Read remotely: `/home/ubuntu/ai-x-bot/bot_data.db`
- Read: `/tmp/flexdropin-source-batch.json`

**Interfaces:**
- Consumes: committed Task 3 database state.
- Produces: a concise user report with inserted IDs/titles/links and safety evidence.

- [ ] **Step 1: Verify every inserted row through application validation**

Load the new rows read-only and assert every row passes `is_complete_verified_news` and appears in `Database.get_eligible_sources("verified_news")`.

- [ ] **Step 2: Verify no unrelated side effects**

Assert `post_drafts` and `draft_evaluations` counts equal the pre-insertion counts, `flexdropin-bot` remains active, the Git SHA is unchanged, and config remains `DRY_RUN=true` plus `APPROVAL_REQUIRED=true`.

- [ ] **Step 3: Report the outcome**

List each inserted source with its database ID, topic, publisher, and direct URL. State the backup path and explain that future scheduled draft cycles will evaluate the new pool automatically; no immediate draft or X post was created.
