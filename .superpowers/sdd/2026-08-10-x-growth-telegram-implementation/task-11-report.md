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

## Fix round 2

- Reviewed base SHA: `c04e48ce85a1847cc7c801ba838746fa0859e3e4`.
- Commit: `fix: repair legacy follower analytics`.

Legacy `follower_snapshot_runs` rows that receive the additive migration
defaults `completed=0` and `summary_json='{}'` are now explicitly nonfinal.
Weekly reports read follower totals and new-follower rows only from runs with
`completed=1`. A later complete capture repairs an incomplete day inside the
existing `BEGIN IMMEDIATE` transaction: it rebuilds the day's snapshot rows,
summary and marker, and only the committed transaction transitions the marker
to `completed=1`. Concurrent repair has one transition, and a process hard
crash rolls the deleted legacy rows, conversions and marker update back
together.

Repair preserves each legacy row's original `first_seen_at` before rebuilding
the complete day. This prevents the repair time from replacing the actual
first observation and creating a false manual-follow conversion. Conversion
then reloads `first_seen_at`, `decision_at`, decision state and prior conversion
state after the snapshot upsert in the same write transaction. Both timestamps
must pass the shared Python parser as exact strings with timezone-aware ISO
values, and conversion requires the normalized instant
`first_seen_at > decision_at`. Naive, malformed, equal and earlier timestamps
fail closed. The guarded update revalidates candidate identity, decision,
exact stored decision timestamp and the still-null conversion state.

TDD evidence:

- The round-2 regression tests were overlaid on an isolated archive of the
  required base and produced `6 failed, 5 passed, 38 deselected`; failures
  covered legacy report admission/repair and naive timestamp attribution.
- The same reviewer regression selection on the working implementation then
  produced `11 passed, 38 deselected`.
- Self-review added a repair-specific first-observation regression. Its RED
  showed `followed_back_at='2026-08-10T12:00:00+00:00'` instead of `None`;
  after preserving the legacy timestamp it produced `1 passed`.
- The complete round-2 selection produced `12 passed, 38 deselected`.

Final verification:

- Task 11: `50 passed in 1.08s`.
- Task 10/11, Telegram and X-safety focused suite:
  `369 passed, 1 warning in 5.37s`.
- Full suite: `610 passed, 1 warning in 8.13s`.
- The sole warning remains Tweepy's pre-existing `imghdr` deprecation.
- `py_compile` exited zero; `pip check` reported no broken requirements.
- Staged and unstaged whitespace checks exited zero.
- The production/test file set is exactly `modules/database.py` and
  `tests/test_growth_analytics.py`; this report was also updated and
  `progress.md` was not modified.
- No live X, Telegram, Groq or News request was made, and no scheduler or X
  mutation was added. An additional review agent was requested but could not
  start because the task-thread limit was already occupied; the implementation
  received a local diff/spec self-review instead.

## Fix round 3

- Reviewed base SHA: `97b212c895724a49ded0d4416a3792181af4c489`.
- Commit: `fix: rebuild orphaned follower snapshots`.

Pre-round-1 process crashes could leave Task 11 `follower_snapshots` rows with
non-null `captured_at` but no `follower_snapshot_runs` marker. A retry formerly
treated those rows as final observations, admitted followers no longer present,
and produced a committed summary whose new-follower count differed from the
weekly report.

When no completed marker exists, the existing `BEGIN IMMEDIATE` transaction now
treats only same-day rows with non-null `captured_at` as nonfinal Task 11 state.
It preserves their original `first_seen_at`, removes them, rebuilds the complete
current follower set, and writes the completed marker in the same transaction.
Task 10 pre-observations (`captured_at IS NULL`) remain in place, including their
original first-observation time. A crash during repair rolls back the deletion,
rebuilt rows and marker together; concurrent retries serialize to one completed
transition, and a replay returns the same committed summary.

TDD evidence:

- The three round-3 tests were overlaid on an isolated archive of the required
  base and produced `3 failed, 50 deselected`. The failures showed summary/report
  mismatches of `0` versus `2` for a normal retry, `0` versus `1` under concurrent
  repair, and `1` versus `3` after a hard-crash retry.
- The identical selection on the working implementation produced
  `3 passed, 50 deselected`.
- The tests use real SQLite files and spawned processes. They verify orphan
  removal, Task 10 row/`first_seen_at` preservation, transaction rollback,
  one concurrent completed transition and replay idempotence.

Final verification:

- Task 11: `53 passed in 1.49s`.
- Task 10/11, Telegram and X-safety focused suite:
  `372 passed, 1 warning in 6.12s`.
- Full suite: `613 passed, 1 warning in 9.54s`.
- The sole pytest warning remains Tweepy's pre-existing `imghdr` deprecation.
- `py_compile`, `pip check` and `git diff --check` passed; pip reported no
  broken requirements and only its non-fatal unwritable-cache warning.
- The production/test file set is exactly `modules/database.py` and
  `tests/test_growth_analytics.py`; this report was also updated and
  `progress.md` was not modified.
- No live X, Telegram, Groq or News request was made, and no scheduler or X
  mutation was added. An independent review agent could not start because the
  task-thread limit was occupied; the diff received a local spec/edge-case
  self-review instead, with no Critical or Important finding.
