# Task 11 report — relevant follower growth and weekly analytics

## Revisions

- Required base SHA: `b2b99caaf5013ca62b8bf76befcc50375be323e5`
- Implementation SHA / final HEAD: `71cd5f7932b29698bab106e4c26a8487f6b36155`
- Commit: `feat: report relevant follower growth`
- `progress.md` was not modified.

## Result

Implemented `PerformanceAnalyzer.capture_follower_snapshot(observed_at)` with
one paginated follower-profile boundary read per invocation and no other X
query. Each exact, canonical follower ID is persisted once per operating date
in `BOT_TIMEZONE`; invalid profiles are isolated and boolean/integer IDs are
never coerced. A separate idempotent daily-run row preserves a successful
zero-follower snapshot, which makes historical totals correct even when no
per-user row exists.

Newness is claimed under SQLite `BEGIN IMMEDIATE`, survives restart, has one
winner across processes and is never restored by removal, a gap, a re-follow
or a retroactive clock replay. Same-day Task 10 pre-observations can be
classified once by the Task 11 capture, while later same-day captures only
upsert the current profile/relevance without recounting the follower.

Relevance uses the current canonical follower profile plus only the validated,
unexpired Task 10 candidate cache. It invokes the exact shared
`passes_candidate_filters` and `score_growth_candidate` functions and the
canonical `GROWTH_SCORE_THRESHOLD`. Missing, malformed, future, expired or
actively suppressed candidate/latest-post state is fail-closed. There is no
`get_latest_original_post`, search/network query or hidden profile budget use
in analytics.

When, and only when, a genuinely new follower has an exact candidate decision
of `followed_manually`, the same snapshot transaction sets
`followed_back_at` once. The conversion also requires the follower observation
to be at or after the recorded manual action. Saved, rejected, discarded,
automatic, already-seen and retrodated followers cannot convert. Discovery
source attribution is stored independently from the Task 10 snapshot audit
source.

Implemented `build_weekly_report(end_date)` as a deterministic seven-day
operating window. Its top-level dictionary contains exactly:

- `followers_total`, `new_followers`, `new_relevant_followers`,
  `relevant_follower_rate`;
- `candidate_count`, `decision_counts`, `follow_back_rate_by_source`;
- `median_impressions`, `post_count`, `content_by_category`;
- `query_budget_used`, `profiles_evaluated`, `factual_blocks`; and
- `attribution_label='correlation'`.

The report treats the end date as inclusive, converts aware datetimes into
`BOT_TIMEZONE`, uses half-open timestamp boundaries, reports deterministic odd
and even medians, and keeps every zero denominator stable. Factual blocks make
the period and follower/manual/follow-back source counts explicit. Query and
profile counters are summed only for exact operating-day keys in the period.

`refresh_own_tweet_metrics` remains unchanged. Category reweighting now
returns without mutation until at least 30 days have elapsed since the first
valid published post; at the 30-day boundary it resumes the existing bounded
CTR policy using an explicit aware clock.

Telegram now has one canonical, bounded, plain-text weekly formatter. `/stats`
and the callable `push_weekly_report(end_date)` send the exact same formatted
message; `/growth` sends that same status block before manual candidate cards.
The push is callable for Task 12 but this task registers no scheduler. The
formatter reads only allowlisted report fields, sanitizes labels/categories,
uses `parse_mode=None` through the existing transport boundary and never emits
a raw Telegram payload.

## Files

- `modules/analytics.py`: snapshot orchestration, canonical cached relevance,
  weekly report and first-30-day reweight gate.
- `modules/database.py`: additive decision/snapshot/run migrations, atomic
  snapshot/conversion capture and factual weekly-window repository reads.
- `modules/telegram_controller.py`: shared weekly formatter, `/stats` and
  `/growth` status integration, callable weekly push.
- `tests/test_growth_analytics.py`: real-SQLite/fake-X/process/restart/timezone
  and formatter coverage.

## TDD evidence

The complete initial Task 11 test module was written before production code.
The canonical RED was:

```text
$ venv/bin/python -m pytest tests/test_growth_analytics.py -v
18 failed in 0.43s
```

Every failure named the intended missing Task 11 boundary: snapshot/report
methods, decision clock, concurrency schema, formatter/push or 30-day gate.
After the first implementation, Task 11 + Task 10 reached:

```text
45 passed, 1 warning in 0.73s
```

Cross-suite review then reproduced the existing Telegram label regression as
one failure; the minimal compatibility correction passed both the original
and parity tests, followed by `334 passed` in the broad set.

Self-review added and observed focused RED cases before production fixes:

- two failures for retroactive newness and `decided_at=False` coercion;
- one failure for a successful empty daily snapshot reporting the old total;
- one failure for a follower observation earlier than its manual action; and
- one failure for actively suppressed cached candidates being labelled
  relevant.

Each focused regression was GREEN before the final suites. Explicit
malformed/missing latest-post cases were already fail-closed through the
canonical Task 10 cache validator and were retained as integration coverage.

## Final verification

Commands:

```sh
venv/bin/python -m pytest tests/test_growth_analytics.py tests/test_growth_discovery.py tests/test_growth_discovery_review.py tests/test_growth_discovery_review_round2.py tests/test_growth_discovery_review_round3.py tests/test_growth_discovery_review_round4.py tests/test_growth_discovery_review_round5.py tests/test_telegram_controller.py tests/test_telegram_workflows.py tests/test_x_write_safety.py -q
venv/bin/python -m pytest -q
venv/bin/python -m py_compile modules/analytics.py modules/database.py modules/telegram_controller.py tests/test_growth_analytics.py
venv/bin/python -m pip check
git diff --check
git diff --cached --check
git diff HEAD^ HEAD --check
git show --check --stat --oneline HEAD
rg -n 'post_tweet|create_tweet|media_upload|follow_user|unfollow_user|like_tweet|reply_to_tweet|send_dm|create_friendship|destroy_friendship|create_favorite|destroy_favorite|get_latest_original_post|search_recent_authors|get_network_candidates|add_job|CronTrigger|BackgroundScheduler' modules/analytics.py modules/telegram_controller.py tests/test_growth_analytics.py
```

Outputs:

- Final post-commit Task 11 + all Task 10 reviews + Telegram + X safety:
  `344 passed, 1 warning in 4.58s`.
- Final post-commit full suite: `585 passed, 1 warning in 7.27s`.
- The only pytest warning is Tweepy's pre-existing Python 3.13 `imghdr`
  deprecation warning.
- Python compilation exited zero with no output.
- `pip check`: `No broken requirements found.` The sandbox emitted only the
  non-fatal warning that the user pip cache is not writable.
- Staged/unstaged/committed whitespace checks exited zero.
- The X-write/hidden-read/scheduler scan returned no matches (expected `rg`
  exit 1).
- The committed file set is exactly the four files listed above.

## Self-review and residual concerns

- All X and Telegram boundaries were replaced locally. No live X, Telegram,
  Groq or News request was performed.
- Follower relevance intentionally waits for Task 10's validated candidate
  cache. A follower without a valid cached latest post is recorded as
  non-relevant for that capture; analytics never spends an unbudgeted X read.
- First-30-day reweighting is anchored to the earliest valid published-post
  timestamp. Malformed/missing publication history leaves weights unchanged.
- The requested independent review agent could not be started because the
  thread limit was already occupied, so the final review was performed
  locally against the canonical brief and focused mutation cases.
- Task 12 remains responsible for production dependency injection and
  scheduling `follower_snapshot` and `weekly_growth_report`.

## Fix round 1

The follower reader now returns an explicit `{profiles, complete}` boundary
result. Analytics accepts only a complete traversal: partial or failed reads
return an empty summary and persist neither snapshots nor a daily-run marker;
the legacy partial-list helper is not called.

Complete captures use one SQLite `BEGIN IMMEDIATE` transaction for every
snapshot row, first-observation/newness decision, manual follow-back conversion
and daily-run marker/summary. A process hard-crash during a later row therefore
rolls back all earlier rows and conversions. Same-day equal or older captures
return the committed summary unchanged; later captures can refresh profiles
without reclassifying past observations as new.

The conversion predicate preserves a Task 10 pre-observation's original
`first_seen_at` and requires it to parse strictly after `decision_at`; malformed,
equal and earlier values fail closed. Reports and metrics now accept only a
nonempty trimmed text `tweet_id`, and daily growth counters skip malformed or
unreasonably large numeric payloads before integer conversion.

Verification:

- RED: `venv/bin/python -m pytest tests/test_growth_analytics.py tests/test_growth_discovery_review_round5.py -q` → `10 failed, 35 passed`.
- Focused GREEN: same command → `45 passed, 1 warning`.
- Task 10/11, Telegram and X-safety suite → `357 passed, 1 warning`.
- Full suite → `598 passed, 1 warning` (pre-existing Tweepy `imghdr` warning).
- `py_compile` and `git diff --check` passed. `progress.md` remains unchanged.
