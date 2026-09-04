# Task brief — repair approved publication queue compatibility

## Production evidence

- VPS HEAD and local `main`: `6d98fc415d6e6a2e07af706c0841595eb5769706`.
- Service and APScheduler jobs are healthy; `DRY_RUN=False`, publishing is not paused.
- The database has 36 approved drafts and two open publication slots for 2026-09-04, but `Database.list_approved_queue(now)` returns an empty list.
- Drafts 16–38 have `post_drafts.updated_at` written as naive SQLite UTC (`YYYY-MM-DD HH:MM:SS`) by `scripts/translate_italian_posts_to_english.py` and `scripts/translate_threads_to_english.py` on 2026-09-01. The strict queue decoder rejects them.
- Thirty advisory/manual rows have `translation_status='ready'` with `translation_it IS NULL`; that inconsistent legacy state is also rejected. For advisory rows, lack of an Italian translation must not block publication.
- There are no SQLite triggers.

## Required behavior

Implement the narrowest startup compatibility repair in `modules/database.py`:

1. Canonicalize only the exact legacy SQLite UTC format `YYYY-MM-DD HH:MM:SS` found in timestamp columns needed by the editorial queue, converting it to aware ISO UTC (`YYYY-MM-DDTHH:MM:SS+00:00`). Do not reinterpret arbitrary malformed or general naive timestamps.
2. For `translation_policy='advisory'` only, repair `translation_status='ready' AND translation_it IS NULL` to the untranslated advisory state (`pending`), clear `review_ready_at`, preserve `approved_queue_at`, and use an aware update timestamp. Do not relax or repair required translations.
3. The repair must be idempotent, transaction-safe as part of schema initialization, and safe on an already-valid database.
4. After reopening a database containing the production-shaped legacy rows, `get_queue_draft()` and `list_approved_queue()` must return the eligible advisory draft.
5. Malformed timestamps and required translations missing text must remain fail-closed.

Prevent recurrence in the two translation scripts by writing aware UTC ISO timestamps instead of SQLite `datetime('now')`. Test their observable database effect; do not test by grepping source text.

## Constraints

- Work only in `/Users/floriano/flo_mobile_app/ai-x-bot-main/.worktrees/publication-queue-repair` on branch `fix/publication-queue-repair`.
- Preserve all behavior outside this incident. No refactors or unrelated cleanup.
- Use strict TDD: write a focused regression test, run it and capture the expected failure, then implement the minimal fix.
- Do not access or mutate the VPS, do not change `.env`, and do not publish anything.
- Baseline before work: `1312 passed, 1 Tweepy deprecation warning`.

## Verification

- Focused new regression tests.
- Nearest affected suites: `tests/test_approved_post_queue.py`, `tests/test_adaptive_publication.py`, and any new script regression test.
- Full suite.
- Commit with a focused Conventional Commit message and report commit hash, changed files, RED/GREEN evidence, and test totals.
