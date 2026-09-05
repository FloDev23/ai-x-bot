# SDD ledger — publication queue incident 2026-09-04

Branch: `fix/publication-queue-repair`
Base: `6d98fc415d6e6a2e07af706c0841595eb5769706`
Baseline: 1312 passed, 1 pre-existing Tweepy deprecation warning

Incident repair: complete

RED:
- Command: `/Users/floriano/flo_mobile_app/ai-x-bot-main/venv/bin/python -m pytest -q tests/test_publication_queue_compatibility.py tests/test_translation_script_timestamps.py`
- Result before production changes: `4 failed, 2 passed`
- Expected failures: production-shaped advisory draft remained undecodable; both translation scripts stored naive SQLite timestamps.

GREEN:
- Focused regressions: `6 passed`
- Nearest affected suites plus regressions: `94 passed, 1 pre-existing Tweepy deprecation warning`
- Full suite: `1318 passed, 1 pre-existing Tweepy deprecation warning`

Implementation:
- Startup repair canonicalizes only exact `YYYY-MM-DD HH:MM:SS` legacy queue timestamps to aware UTC ISO values in the existing schema transaction.
- Advisory `ready` rows without translation text become `pending`, clear `review_ready_at`, preserve queue approval, and receive an aware update timestamp.
- Required missing translations and other malformed/general naive timestamps remain fail-closed.
- Both one-off translation scripts now persist aware UTC ISO update timestamps.

Quality review repair:
- Verified issue: advisory state repair overwrote malformed `updated_at` and
  cleared malformed `review_ready_at`, making corrupt rows decodable.
- RED: focused malformed advisory timestamp regression: `2 failed`.
- GREEN: focused review regression: `2 passed`; all incident regressions:
  `8 passed`.
- Nearest affected suites plus regressions: `96 passed, 1 pre-existing Tweepy
  deprecation warning`.
- Full suite: `1320 passed, 1 pre-existing Tweepy deprecation warning`.
- State repair now skips rows unless every timestamp it would overwrite is
  already strict-aware after legacy normalization (or nullable and `NULL`).
