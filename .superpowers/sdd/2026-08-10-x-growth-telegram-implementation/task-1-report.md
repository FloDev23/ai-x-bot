# Task 1 Report: Establish the test harness and remove prohibited X writes

## Implementation details

- Added the pytest development harness and the required X-write safety tests.
- Added approval-only rollout configuration with safe defaults, including dry-run enabled, approval required, Europe/Rome timezone, two content slots, and a one-link weekly limit.
- Replaced the persona bio, style rules, and examples with source-aware guidance that prohibits invented experiences or claims.
- Limited `TwitterClient` writes to `post_tweet(text, media_path=None, media_type="image")`; removed reply, like, follow, and unfollow methods and reply parameters.
- Deleted the engagement module. Removed its manager, the legacy growth manager, their cycles, target synchronization, and all scheduled automated publishing, engagement, follow, unfollow, and weekly build-in-public jobs from `main.py`.
- Preserved lead discovery and performance analytics schedules; neither invokes `TwitterClient.post_tweet()`.

## Files changed

- Added `requirements-dev.txt`
- Added `tests/__init__.py`
- Added `tests/test_x_write_safety.py`
- Modified `config.py`
- Modified `character.json`
- Modified `modules/twitter_client.py`
- Modified `main.py`
- Deleted `modules/engagement.py`

## RED evidence

Command:

```sh
venv/bin/python -m pytest tests/test_x_write_safety.py -v
```

Result: 3 failed, as expected. The client still exposed prohibited write methods, safe rollout fields were absent, and `character.json` contained a fictional Stripe webhook example.

## GREEN and final verification

The repository-local, untracked `.env` defines `MAX_LINKS_PER_WEEK=3`, so its explicit configuration overrides the new default of `1`. The supplied test removes only `DRY_RUN` and `CONTENT_SLOTS`; to verify default behavior without altering user configuration, the verification commands set the value to the safe default:

```sh
env MAX_LINKS_PER_WEEK=1 venv/bin/python -m pytest tests/test_x_write_safety.py -v
env MAX_LINKS_PER_WEEK=1 venv/bin/python -m pytest -v
venv/bin/python -m py_compile config.py main.py modules/twitter_client.py
git diff --check
```

Results:

- Focused safety suite: 3 passed (with one upstream Tweepy `imghdr` deprecation warning).
- Full suite: 3 passed (the repository currently contains the focused suite only), with the same upstream warning.
- Python compilation: passed.
- `git diff --check`: passed with no output.
- Static safety sweep: no prohibited X-write methods or automatic engagement cycles remain in `main.py`, `modules/twitter_client.py`, `config.py`, or `character.json`; `main.py` now schedules only opportunity discovery and performance analytics.

## Self-review

- Confirmed the test observes the public `TwitterClient` interface rather than a mock.
- Confirmed `post_tweet` has no reply argument or reply API branch.
- Confirmed no scheduled function can reach `post_tweet` before Task 7.
- Confirmed the deleted engagement module is not imported or referenced by the main process.
- Confirmed the exact rollout defaults requested in the task are defined in `config.py`.

## Concerns

- The existing local `.env` explicitly sets `MAX_LINKS_PER_WEEK=3`. This is preserved as user configuration, but it makes the provided default-value test fail when run without an environment override because `config.py` intentionally honors explicit environment values. Update that local setting to `1` (or remove it) before running the suite without the verification override.
- The existing Tweepy dependency emits an `imghdr` deprecation warning under Python 3.11; it is unrelated to this change.

## Fix Round 1

- Updated `test_rollout_defaults_are_safe` to clear every environment variable whose default it asserts: `DRY_RUN`, `APPROVAL_REQUIRED`, `BOT_TIMEZONE`, `CONTENT_SLOTS`, and `MAX_LINKS_PER_WEEK`.
- Patched `dotenv.load_dotenv` only for the reload in that test, so the repository-local `.env` cannot repopulate cleared values. This verifies configuration defaults without masking local configuration through a command-line override.

Covering tests and verification commands:

```sh
venv/bin/python -m pytest tests/test_x_write_safety.py -v
venv/bin/python -m pytest -v
git diff --check
git diff --cached --check
```

Outputs:

- Focused safety suite: `3 passed, 1 warning`.
- Full suite: `3 passed, 1 warning`.
- Both diff checks exited successfully with no output.
- The one warning is Tweepy's upstream Python 3.11 `imghdr` deprecation warning.
