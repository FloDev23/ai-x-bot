# Like Suggestions and Following Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace reply suggestions with like suggestions, restrict follow suggestions to gyms, and track the real X following list so non-gym accounts without follow-back are proposed for manual unfollow after 30 days.

**Architecture:** Evolve the existing daily Growth Digest instead of adding a subsystem. A new additive SQLite table `x_following` is filled by a read-only following traversal in the existing 23:15 follower job. The digest keeps its three persisted kinds (`account`, `post`, `reevaluate`) because `growth_suggestions.kind` has a CHECK constraint; their meaning becomes gyms to follow, posts to like and unfollow proposals. Reply Copilot is detached from the runtime.

**Tech Stack:** Python 3.11, SQLite (`modules/database.py`), tweepy v2 client (`modules/twitter_client.py`), APScheduler (`main.py`), Telegram Bot API (`modules/telegram_controller.py`), pytest.

**Spec:** `docs/superpowers/specs/2026-09-15-like-follow-tracking-design.md`

## Global Constraints

- No X engagement write, ever: no like, follow, unfollow, reply, repost or DM endpoint. `tests/test_x_write_safety.py` must pass unchanged.
- New X read: `GET /2/users/:id/following` only, metered as `owned_read`.
- Follow suggestions: segment `primary` only, at most 5 per Rome day.
- Like suggestions: at most 10 per Rome day. Quotas `followed_gym` 4, `suggested_gym` 3, `operator_pain` 3. Fill order `followed_gym`, `operator_pain`, `suggested_gym`.
- Followed-gym post age ≤ 72 hours; suggested-gym post age ≤ 7 days; author cooldown 3 days; one post per author per digest.
- Unfollow proposals: non-gym, followed ≥ 30 days, no follow-back in a complete follower run taken at least 30 days after the follow, at most 5 per Rome week (Monday–Sunday). `keep` suppresses for 90 days. `È una palestra` exempts forever.
- `GROWTH_POST_QUERY_BUDGET` maximum and default 1. `GROWTH_UNFOLLOW_REVIEW_DAYS` default 30, minimum 30.
- Every Telegram callback stays revision-bound and idempotent; callback data ≤ 64 bytes.
- User-facing Telegram copy is Italian; code, tests and commit messages are English.
- Run tests with `venv/bin/python -m pytest`. The full suite must pass at the end of every task.
- End commit messages with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `config.py` | env validation | budget max 1, unfollow days ≥ 30 |
| `modules/twitter_client.py` | X reads | shared owned user-graph traversal, `read_following_profiles`, latest post metrics |
| `modules/database.py` | persistence | `x_following` table, sync, unfollow proposals, decisions, weekly following stats |
| `modules/analytics.py` | follower/following capture, weekly report | `sync_following`, `following_summary` |
| `modules/growth_digest.py` | digest selection | like sources/quotas, gym-only accounts, unfollow rows, single search query |
| `modules/telegram_controller.py` | Telegram UI | new header, cards, callbacks, following notices, stats block |
| `main.py` | wiring | following sync in follower job, Reply Copilot detached |
| `tests/fakes.py` | test doubles | `read_following_profiles` on `FakeXClient` |
| `tests/test_following_client.py` | new | client traversal tests |
| `tests/test_following_tracking.py` | new | sync, proposals, decisions, analytics, notices, main cycle |
| `tests/test_growth_digest.py`, `tests/test_growth_digest_telegram.py`, `tests/test_end_to_end_dry_run.py`, `tests/test_main_startup.py`, `tests/test_growth_analytics.py` | existing | updated to the new behavior |
| `README.md`, `SETUP.md`, `.env.example` | docs | new manual flow and VPS env changes |

---

### Task 1: Configuration limits

**Files:**
- Modify: `config.py` (function `_growth_digest_configuration`, around lines 244–272)
- Modify: `.env.example` (lines with `GROWTH_POST_QUERY_BUDGET` and `GROWTH_UNFOLLOW_REVIEW_DAYS`)
- Test: `tests/test_main_startup.py`

**Interfaces:**
- Produces: `config.GROWTH_POST_QUERY_BUDGET == 1` by default, maximum 1; `config.GROWTH_UNFOLLOW_REVIEW_DAYS == 30` by default, values below 30 fail at import.

- [ ] **Step 1: Update the failing tests**

In `tests/test_main_startup.py`, inside the `@pytest.mark.parametrize` of `test_growth_digest_configuration_fails_closed`, replace:

```python
        ("GROWTH_POST_QUERY_BUDGET", "3"),
        ("GROWTH_SUGGESTION_COOLDOWN_DAYS", "31"),
        ("GROWTH_UNFOLLOW_REVIEW_DAYS", "13"),
```

with:

```python
        ("GROWTH_POST_QUERY_BUDGET", "2"),
        ("GROWTH_SUGGESTION_COOLDOWN_DAYS", "31"),
        ("GROWTH_UNFOLLOW_REVIEW_DAYS", "29"),
```

In `test_growth_digest_configuration_has_bounded_release_defaults`, replace:

```python
    assert result.stdout.strip() == "09:00 5 10 2 30 14"
```

with:

```python
    assert result.stdout.strip() == "09:00 5 10 1 30 30"
```

Append this test to the same file:

```python
def test_unfollow_review_days_accepts_longer_windows():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os; os.environ['GROWTH_UNFOLLOW_REVIEW_DAYS'] = '45'; "
                "import config; print(config.GROWTH_UNFOLLOW_REVIEW_DAYS)"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "45"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_main_startup.py -v`
Expected: FAIL. `GROWTH_POST_QUERY_BUDGET=2` imports successfully, defaults print `09:00 5 10 2 30 14`, and `45` raises.

- [ ] **Step 3: Implement**

In `config.py`, inside `_growth_digest_configuration`, replace:

```python
    post_query_budget = _bounded_positive_int_env(
        "GROWTH_POST_QUERY_BUDGET", 2, 2,
    )
```

with:

```python
    post_query_budget = _bounded_positive_int_env(
        "GROWTH_POST_QUERY_BUDGET", 1, 1,
    )
```

and replace:

```python
    review_days = _strict_positive_int_env("GROWTH_UNFOLLOW_REVIEW_DAYS", 14)
    if review_days != 14:
        raise ValueError(
            "GROWTH_UNFOLLOW_REVIEW_DAYS must be exactly 14 for this release"
        )
```

with:

```python
    review_days = _strict_positive_int_env("GROWTH_UNFOLLOW_REVIEW_DAYS", 30)
    if review_days < 30:
        raise ValueError("GROWTH_UNFOLLOW_REVIEW_DAYS must be at least 30")
```

In `.env.example`, set `GROWTH_POST_QUERY_BUDGET=1` and `GROWTH_UNFOLLOW_REVIEW_DAYS=30`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_main_startup.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: all pass. `GrowthDigestService` still accepts a budget up to 2, so nothing else depends on the old default.

- [ ] **Step 6: Commit**

```bash
git add config.py .env.example tests/test_main_startup.py
git commit -m "feat: bound growth search budget and unfollow review window

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Read the following list and latest-post metrics from X

**Files:**
- Modify: `modules/twitter_client.py` (`read_followers_profiles`, lines ~917–989; `get_latest_original_post`, lines ~1059–1112)
- Modify: `tests/fakes.py` (`FakeXClient`)
- Create: `tests/test_following_client.py`

**Interfaces:**
- Produces: `TwitterClient.read_following_profiles() -> FollowerProfilesRead` (fields `profiles: Tuple[Dict, ...]`, `complete: bool`; each profile has the `_profile_dict` shape with `id`, `user_id`, `username`, `description`, `protected`, `location`, `created_at`, `followers_count`, `following_count`, `tweet_count`, `listed_count`, `spam_signals`).
- Produces: `get_latest_original_post(user_id)` result gains optional key `public_metrics` (the five keys `like_count`, `retweet_count`, `reply_count`, `quote_count`, `impression_count`), present only when X returns all five as bounded ints.
- Produces: `FakeXClient.following` (list) and `FakeXClient.read_following_profiles()`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_following_client.py`:

```python
from types import SimpleNamespace

from modules.twitter_client import TwitterClient


def _client(backend, self_id="1"):
    client = TwitterClient.__new__(TwitterClient)
    client._client = backend
    client._cached_self_id = self_id
    return client


def _user(user_id, username, description="Independent gym in Austin, TX."):
    return SimpleNamespace(
        id=user_id,
        username=username,
        description=description,
        protected=False,
        location="Austin, TX",
        created_at=None,
        public_metrics={
            "followers_count": 10,
            "following_count": 5,
            "tweet_count": 3,
            "listed_count": 0,
        },
    )


class GraphBackend:
    def __init__(self, pages, fail_on=None):
        self.pages = pages
        self.fail_on = fail_on
        self.calls = []

    def get_users_following(self, **kwargs):
        self.calls.append(kwargs)
        index = len(self.calls) - 1
        if index == self.fail_on:
            raise RuntimeError("private transport detail")
        users, token = self.pages[index]
        meta = {} if token is None else {"next_token": token}
        return SimpleNamespace(data=users, meta=meta)


def test_following_read_pages_until_the_end_and_reports_complete():
    backend = GraphBackend([
        ([_user(11, "gym_a")], "t1"),
        ([_user("12", "gym_b"), _user(11, "gym_a")], None),
    ])

    result = _client(backend).read_following_profiles()

    assert result.complete is True
    assert [profile["user_id"] for profile in result.profiles] == ["11", "12"]
    assert backend.calls[0] == {
        "id": "1",
        "max_results": 1000,
        "user_fields": [
            "username", "description", "protected", "location",
            "created_at", "public_metrics",
        ],
    }
    assert backend.calls[1]["pagination_token"] == "t1"


def test_following_read_failure_is_incomplete_and_keeps_partial_rows():
    backend = GraphBackend([([_user(11, "gym_a")], "t1")], fail_on=1)

    result = _client(backend).read_following_profiles()

    assert result.complete is False
    assert [profile["user_id"] for profile in result.profiles] == ["11"]


def test_following_read_without_self_id_reads_nothing():
    backend = GraphBackend([])

    result = _client(backend, self_id=None).read_following_profiles()

    assert result.complete is False
    assert backend.calls == []


def test_latest_original_post_keeps_bounded_public_metrics_only_when_complete():
    metrics = {
        "like_count": 3, "retweet_count": 0, "reply_count": 1,
        "quote_count": 0, "impression_count": 90,
    }

    class Backend:
        def __init__(self, public_metrics):
            self.public_metrics = public_metrics

        def get_users_tweets(self, **_kwargs):
            return SimpleNamespace(data=[SimpleNamespace(
                id=501,
                text="New class schedule at our gym",
                created_at="2026-09-14T10:00:00+00:00",
                lang="en",
                public_metrics=self.public_metrics,
            )])

    with_metrics = _client(Backend(metrics)).get_latest_original_post("11")
    without_metrics = _client(Backend({"like_count": 3})).get_latest_original_post("11")

    assert with_metrics["public_metrics"] == metrics
    assert "public_metrics" not in without_metrics
    assert without_metrics["id"] == "501"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_following_client.py -v`
Expected: FAIL with `AttributeError: 'TwitterClient' object has no attribute 'read_following_profiles'` and a `KeyError: 'public_metrics'`.

- [ ] **Step 3: Implement the shared traversal**

In `modules/twitter_client.py`, replace the entire `read_followers_profiles` method with:

```python
    def _read_owned_user_graph(self, label: str, request) -> FollowerProfilesRead:
        """Read one full owned follower/following traversal with a completion bit."""
        self_id = self.get_authenticated_user_id_cached()
        if not self_id:
            return FollowerProfilesRead((), False)
        profiles = []
        seen_ids = set()
        seen_tokens = set()
        pagination_token = None
        for _page_number in range(100):
            if pagination_token is not None:
                if pagination_token in seen_tokens:
                    return FollowerProfilesRead(tuple(profiles), False)
                seen_tokens.add(pagination_token)
            params = {
                "id": self_id,
                "max_results": 1000,
                "user_fields": self._growth_user_fields(),
            }
            if pagination_token is not None:
                params["pagination_token"] = pagination_token
            try:
                response = self._metered_read(
                    "owned_read",
                    params["max_results"],
                    lambda: request(params),
                )
            except Exception as error:
                logger.warning(
                    "x_growth_%s_read_failed error_type=%s",
                    label,
                    type(error).__name__,
                )
                return FollowerProfilesRead(tuple(profiles), False)
            try:
                response_users = getattr(response, "data", None)
                if not isinstance(response_users, (list, tuple)):
                    raise ValueError(f"malformed {label} page data")
            except Exception as error:
                logger.warning(
                    "x_growth_%s_page_skipped error_type=%s",
                    label,
                    type(error).__name__,
                )
                return FollowerProfilesRead(tuple(profiles), False)
            for user in response_users:
                try:
                    profile = self._profile_dict(user)
                except Exception as error:
                    logger.warning(
                        "x_growth_%s_record_skipped error_type=%s",
                        label,
                        type(error).__name__,
                    )
                    continue
                if profile is None or profile["id"] in seen_ids:
                    continue
                seen_ids.add(profile["id"])
                profiles.append(profile)
            try:
                meta = getattr(response, "meta", None)
                if not isinstance(meta, Mapping):
                    raise ValueError(f"malformed {label} page metadata")
                next_token = meta.get("next_token")
            except Exception as error:
                logger.warning(
                    "x_growth_%s_page_skipped error_type=%s",
                    label,
                    type(error).__name__,
                )
                return FollowerProfilesRead(tuple(profiles), False)
            if next_token is None:
                return FollowerProfilesRead(tuple(profiles), True)
            if type(next_token) is not str or not next_token:
                return FollowerProfilesRead(tuple(profiles), False)
            pagination_token = next_token
        return FollowerProfilesRead(tuple(profiles), False)

    def read_followers_profiles(self) -> FollowerProfilesRead:
        """Read one full follower traversal with an explicit completion bit."""
        return self._read_owned_user_graph(
            "followers",
            lambda params: self._client.get_users_followers(**params),
        )

    def read_following_profiles(self) -> FollowerProfilesRead:
        """Read the complete list of accounts @FlexDropin follows."""
        return self._read_owned_user_graph(
            "following",
            lambda params: self._client.get_users_following(**params),
        )
```

Use explicit lambdas, not `getattr(self._client, name)`: the static scan in `tests/test_x_write_safety.py` flags dynamic attribute access on the X boundary.

- [ ] **Step 4: Keep latest-post metrics**

In `get_latest_original_post`, replace the final `return { ... }` block with:

```python
        result = {
            "id": normalized_id,
            "text": text,
            "created_at": created_at,
            "lang": lang,
            "is_original": True,
        }
        metrics = self._bounded_metrics(
            getattr(latest, "public_metrics", None),
            (
                "like_count", "retweet_count", "reply_count", "quote_count",
                "impression_count",
            ),
        )
        if metrics is not None:
            result["public_metrics"] = metrics
        return result
```

- [ ] **Step 5: Extend the fake X client**

In `tests/fakes.py`, in `FakeXClient.__init__`, add `self.following = []` after `self.followers = []`, then add this method after `read_followers_profiles`:

```python
    def read_following_profiles(self):
        self.read_calls.append(("read_following_profiles",))
        return SimpleNamespace(profiles=list(self.following), complete=True)
```

- [ ] **Step 6: Run the tests**

Run: `venv/bin/python -m pytest tests/test_following_client.py tests/test_x_write_safety.py tests/test_growth_analytics.py -v`
Expected: PASS

- [ ] **Step 7: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add modules/twitter_client.py tests/fakes.py tests/test_following_client.py
git commit -m "feat: read owned following list and latest post metrics

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Persist and diff the following list

**Files:**
- Modify: `modules/database.py` (schema in the table-creation method, next to `CREATE TABLE IF NOT EXISTS growth_suggestions`; new methods after `get_known_follower_ids`; import from `modules.growth_candidate_schema`)
- Create: `tests/test_following_tracking.py`

**Interfaces:**
- Consumes: `is_canonical_growth_profile`, `has_managed_fitness_facility_context`, `parse_growth_datetime` from `modules.growth_candidate_schema`; `follower_snapshot_runs` / `follower_snapshots` written by `capture_follower_snapshot_batch`.
- Produces:
  - table `x_following` (columns from the spec).
  - `Database._effective_gym(is_gym, override) -> bool` (staticmethod).
  - `Database.sync_following_snapshot(observed_at: datetime, profiles: List[Dict], *, complete: bool) -> Optional[Dict]`. It returns `None` when `complete is not True` or input is invalid. Otherwise it returns `{"following_total": int, "new_following": int, "unfollowed": int, "gyms_following": int, "still_followed_after_unfollow": List[str]}`.
  - `Database.get_following_state() -> Dict[str, Dict]`: active (not unfollowed) rows keyed by user id, values `{"username": str, "is_gym": bool}` with the effective gym status.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_following_tracking.py`:

```python
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from modules.database import Database
from modules.growth_candidate_schema import has_managed_fitness_facility_context


NOW = datetime(2026, 9, 15, 21, 15, tzinfo=timezone.utc)
GYM_BIO = "Independent gym in Austin, TX. Classes daily."
OTHER_BIO = "Fitness podcast host and marathon runner."


def profile(user_id, username, description=GYM_BIO):
    return {
        "id": user_id,
        "user_id": user_id,
        "username": username,
        "description": description,
        "protected": False,
        "location": "Austin, TX",
        "created_at": None,
        "followers_count": 100,
        "following_count": 50,
        "tweet_count": 20,
        "listed_count": 1,
        "spam_signals": [],
    }


def capture_followers(db, when, profiles):
    observed_on = when.astimezone(ZoneInfo("Europe/Rome")).date().isoformat()
    assert db.capture_follower_snapshot_batch(
        observed_on, when, [(item, False) for item in profiles], len(profiles),
    ) is not None


def following_row(db, user_id):
    with db._conn() as conn:
        return conn.execute(
            "SELECT * FROM x_following WHERE user_id = ?", (user_id,)
        ).fetchone()


def test_fixture_bios_classify_as_expected():
    assert has_managed_fitness_facility_context(profile("11", "gym_a")) is True
    assert has_managed_fitness_facility_context(
        profile("12", "runner", OTHER_BIO)
    ) is False


def test_complete_sync_inserts_rows_with_gym_flag_and_follow_back(tmp_path):
    db = Database(str(tmp_path / "following.db"))
    capture_followers(db, NOW - timedelta(hours=1), [profile("11", "gym_a")])

    summary = db.sync_following_snapshot(
        NOW,
        [profile("11", "gym_a"), profile("12", "runner", OTHER_BIO)],
        complete=True,
    )

    assert summary == {
        "following_total": 2,
        "new_following": 2,
        "unfollowed": 0,
        "gyms_following": 1,
        "still_followed_after_unfollow": [],
    }
    gym = following_row(db, "11")
    runner = following_row(db, "12")
    assert (gym["is_gym"], gym["follows_back"]) == (1, 1)
    assert gym["first_seen_following_at"] == NOW.isoformat()
    assert gym["follows_back_checked_at"] == (NOW - timedelta(hours=1)).isoformat()
    assert (runner["is_gym"], runner["follows_back"]) == (0, 0)
    assert db.get_following_state() == {
        "11": {"username": "gym_a", "is_gym": True},
        "12": {"username": "runner", "is_gym": False},
    }


def test_incomplete_sync_writes_nothing(tmp_path):
    db = Database(str(tmp_path / "incomplete.db"))

    assert db.sync_following_snapshot(
        NOW, [profile("11", "gym_a")], complete=False,
    ) is None
    with db._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM x_following").fetchone()[0] == 0


def test_follow_back_unchanged_without_complete_follower_run(tmp_path):
    db = Database(str(tmp_path / "no-run.db"))

    db.sync_following_snapshot(NOW, [profile("11", "gym_a")], complete=True)

    row = following_row(db, "11")
    assert (row["follows_back"], row["follows_back_checked_at"]) == (0, None)


def test_missing_account_is_marked_unfollowed_and_refollow_resets(tmp_path):
    db = Database(str(tmp_path / "diff.db"))
    runner = profile("12", "runner", OTHER_BIO)
    db.sync_following_snapshot(
        NOW, [profile("11", "gym_a"), runner], complete=True,
    )

    later = NOW + timedelta(days=1)
    summary = db.sync_following_snapshot(
        later, [profile("11", "gym_a")], complete=True,
    )

    assert summary["unfollowed"] == 1
    assert following_row(db, "12")["unfollowed_at"] == later.isoformat()
    assert set(db.get_following_state()) == {"11"}

    again = NOW + timedelta(days=2)
    summary = db.sync_following_snapshot(
        again, [profile("11", "gym_a"), runner], complete=True,
    )

    assert summary["new_following"] == 1
    row = following_row(db, "12")
    assert row["unfollowed_at"] is None
    assert row["first_seen_following_at"] == again.isoformat()


def test_legacy_candidate_becomes_followed_when_sync_sees_it(tmp_path):
    db = Database(str(tmp_path / "legacy.db"))
    db.upsert_growth_candidate({
        "user_id": "11",
        "username": "gym_a",
        "profile": {},
        "latest_post": None,
        "score": 88,
        "score_data": {},
        "discovery_source": "topic_search",
    })

    db.sync_following_snapshot(NOW, [profile("11", "gym_a")], complete=True)

    with db._conn() as conn:
        candidate = conn.execute(
            "SELECT decision, decision_at, manual_followed_at "
            "FROM growth_candidates WHERE user_id = '11'"
        ).fetchone()
    assert tuple(candidate) == (
        "followed_manually", NOW.isoformat(), NOW.isoformat(),
    )


def test_manual_unfollow_still_listed_is_reported_and_cleared(tmp_path):
    db = Database(str(tmp_path / "still-followed.db"))
    runner = profile("12", "runner", OTHER_BIO)
    db.sync_following_snapshot(NOW, [runner], complete=True)
    with db._conn() as conn:
        conn.execute(
            "UPDATE x_following SET unfollow_decision = 'unfollowed_manually', "
            "unfollow_decision_at = ? WHERE user_id = '12'",
            (NOW.isoformat(),),
        )

    summary = db.sync_following_snapshot(
        NOW + timedelta(days=1), [runner], complete=True,
    )

    assert summary["still_followed_after_unfollow"] == ["runner"]
    row = following_row(db, "12")
    assert (row["unfollow_decision"], row["unfollow_decision_at"]) == (None, None)
    assert json.loads(row["profile_json"])["username"] == "runner"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_following_tracking.py -v`
Expected: FAIL with `AttributeError: 'Database' object has no attribute 'sync_following_snapshot'` (the fixture test passes).

- [ ] **Step 3: Add the table**

In `modules/database.py`, in the schema method, immediately before the statement `CREATE TABLE IF NOT EXISTS growth_suggestions`, add:

```python
            c.execute("""
                CREATE TABLE IF NOT EXISTS x_following (
                    user_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    profile_json TEXT NOT NULL,
                    first_seen_following_at TEXT NOT NULL,
                    last_seen_following_at TEXT NOT NULL,
                    unfollowed_at TEXT,
                    follows_back INTEGER NOT NULL DEFAULT 0,
                    follows_back_checked_at TEXT,
                    is_gym INTEGER NOT NULL DEFAULT 0,
                    gym_override TEXT CHECK (
                        gym_override IN ('gym', 'not_gym')
                    ),
                    unfollow_decision TEXT CHECK (
                        unfollow_decision IN ('keep', 'unfollowed_manually')
                    ),
                    unfollow_decision_at TEXT,
                    keep_until TEXT
                )
            """)
```

Add `has_managed_fitness_facility_context` to the existing `from modules.growth_candidate_schema import (...)` list near the top of the file, if it is not already there.

- [ ] **Step 4: Implement sync and state reads**

In `modules/database.py`, add these methods directly after `get_known_follower_ids`:

```python
    # ---------- Real X following list ----------

    @staticmethod
    def _effective_gym(is_gym: Any, override: Any) -> bool:
        return override == "gym" or (is_gym == 1 and override != "not_gym")

    def sync_following_snapshot(
        self,
        observed_at: datetime,
        profiles: List[Dict],
        *,
        complete: bool,
    ) -> Optional[Dict]:
        """Diff one complete following traversal into ``x_following``."""
        if (
            complete is not True
            or type(observed_at) is not datetime
            or observed_at.tzinfo is None
            or observed_at.utcoffset() is None
            or not isinstance(profiles, (list, tuple))
        ):
            return None
        current = observed_at.astimezone(timezone.utc)
        observed_iso = current.isoformat()
        valid: Dict[str, Dict] = {}
        for item in profiles:
            if not is_canonical_growth_profile(item):
                continue
            user_id = item.get("user_id", item.get("id"))
            if not self._canonical_growth_object_id(user_id) or user_id in valid:
                continue
            valid[user_id] = item
        summary = {
            "following_total": len(valid),
            "new_following": 0,
            "unfollowed": 0,
            "gyms_following": 0,
            "still_followed_after_unfollow": [],
        }
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            follower_ids = None
            follower_checked_at = None
            for run in conn.execute("""
                SELECT observed_on, captured_at FROM follower_snapshot_runs
                WHERE completed = 1
                ORDER BY julianday(captured_at) DESC
            """).fetchall():
                captured = parse_growth_datetime(run["captured_at"])
                if captured is None or captured > current:
                    continue
                follower_checked_at = captured.isoformat()
                follower_ids = {
                    row["user_id"]
                    for row in conn.execute("""
                        SELECT user_id FROM follower_snapshots
                        WHERE observed_on = ? AND captured_at IS NOT NULL
                    """, (run["observed_on"],)).fetchall()
                }
                break
            existing = {
                row["user_id"]: row
                for row in conn.execute("SELECT * FROM x_following").fetchall()
            }
            for user_id, item in valid.items():
                is_gym = 1 if has_managed_fitness_facility_context(item) else 0
                profile_json = json.dumps(item, allow_nan=False, sort_keys=True)
                row = existing.get(user_id)
                override = row["gym_override"] if row is not None else None
                if row is None or row["unfollowed_at"] is not None:
                    summary["new_following"] += 1
                    conn.execute("""
                        INSERT INTO x_following (
                            user_id, username, profile_json,
                            first_seen_following_at, last_seen_following_at,
                            is_gym
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET
                            username = excluded.username,
                            profile_json = excluded.profile_json,
                            first_seen_following_at =
                                excluded.first_seen_following_at,
                            last_seen_following_at =
                                excluded.last_seen_following_at,
                            unfollowed_at = NULL,
                            unfollow_decision = NULL,
                            unfollow_decision_at = NULL,
                            keep_until = NULL,
                            is_gym = excluded.is_gym
                    """, (
                        user_id, item["username"], profile_json,
                        observed_iso, observed_iso, is_gym,
                    ))
                else:
                    if row["unfollow_decision"] == "unfollowed_manually":
                        summary["still_followed_after_unfollow"].append(
                            item["username"]
                        )
                    conn.execute("""
                        UPDATE x_following
                        SET username = ?, profile_json = ?,
                            last_seen_following_at = ?, is_gym = ?,
                            unfollow_decision = CASE
                                WHEN unfollow_decision = 'unfollowed_manually'
                                THEN NULL ELSE unfollow_decision END,
                            unfollow_decision_at = CASE
                                WHEN unfollow_decision = 'unfollowed_manually'
                                THEN NULL ELSE unfollow_decision_at END
                        WHERE user_id = ?
                    """, (
                        item["username"], profile_json, observed_iso, is_gym,
                        user_id,
                    ))
                if follower_ids is not None:
                    conn.execute("""
                        UPDATE x_following
                        SET follows_back = ?, follows_back_checked_at = ?
                        WHERE user_id = ?
                    """, (
                        1 if user_id in follower_ids else 0,
                        follower_checked_at,
                        user_id,
                    ))
                if self._effective_gym(is_gym, override):
                    summary["gyms_following"] += 1
                conn.execute("""
                    UPDATE growth_candidates
                    SET decision = 'followed_manually',
                        decision_at = (
                            SELECT first_seen_following_at FROM x_following
                            WHERE user_id = ?
                        ),
                        manual_followed_at = COALESCE(manual_followed_at, (
                            SELECT first_seen_following_at FROM x_following
                            WHERE user_id = ?
                        )),
                        rejection_reason = NULL, suppressed_until = NULL
                    WHERE user_id = ? AND decision IN ('new', 'saved')
                """, (user_id, user_id, user_id))
            for user_id, row in existing.items():
                if user_id in valid or row["unfollowed_at"] is not None:
                    continue
                cursor = conn.execute("""
                    UPDATE x_following SET unfollowed_at = ?
                    WHERE user_id = ? AND unfollowed_at IS NULL
                """, (observed_iso, user_id))
                summary["unfollowed"] += cursor.rowcount
        return summary

    def get_following_state(self) -> Dict[str, Dict]:
        """Return accounts currently followed, keyed by user id."""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT user_id, username, is_gym, gym_override
                FROM x_following WHERE unfollowed_at IS NULL
            """).fetchall()
        return {
            row["user_id"]: {
                "username": row["username"],
                "is_gym": self._effective_gym(row["is_gym"], row["gym_override"]),
            }
            for row in rows
            if self._canonical_growth_object_id(row["user_id"])
        }
```

- [ ] **Step 5: Run the tests**

Run: `venv/bin/python -m pytest tests/test_following_tracking.py -v`
Expected: PASS

- [ ] **Step 6: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add modules/database.py tests/test_following_tracking.py
git commit -m "feat: track the real X following list in SQLite

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Run the following sync nightly and notify stale unfollows

**Files:**
- Modify: `modules/analytics.py` (new method after `capture_follower_snapshot`)
- Modify: `modules/telegram_controller.py` (new method after `push_growth_digest`)
- Modify: `main.py` (`follower_snapshot_cycle`, around line 704)
- Test: `tests/test_following_tracking.py`

**Interfaces:**
- Consumes: `Database.sync_following_snapshot` (Task 3), `client.read_following_profiles()` (Task 2).
- Produces:
  - `PerformanceAnalyzer.sync_following(observed_at: datetime) -> Dict`: same keys as the sync summary; empty zeros and `[]` on any failure.
  - `TelegramController.push_following_notices(usernames) -> str`: returns `"following_notices"` or `"following_notices_empty"`.
  - `FlexDropinGrowthAgent.follower_snapshot_cycle(now=None)` still returns the follower summary. It then calls `analytics.sync_following(current)` when available and pushes notices.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_following_tracking.py`:

```python
from types import SimpleNamespace

from modules.analytics import PerformanceAnalyzer
from modules.telegram_controller import TelegramController
from tests.fakes import FakeTelegramApi


EMPTY_SYNC = {
    "following_total": 0,
    "new_following": 0,
    "unfollowed": 0,
    "gyms_following": 0,
    "still_followed_after_unfollow": [],
}


class FollowingX:
    def __init__(self, following, complete=True):
        self.following = list(following)
        self.complete = complete
        self.reads = 0
        self.write_calls = []

    def read_following_profiles(self):
        self.reads += 1
        return SimpleNamespace(profiles=tuple(self.following), complete=self.complete)


class NoopNotifier:
    def notify_error(self, operation, error):
        del operation, error


def test_analytics_sync_following_persists_complete_reads_only(tmp_path):
    db = Database(str(tmp_path / "analytics-following.db"))
    complete = FollowingX([profile("11", "gym_a")])

    assert PerformanceAnalyzer(complete, db).sync_following(NOW)["following_total"] == 1
    assert complete.reads == 1

    other = Database(str(tmp_path / "analytics-partial.db"))
    partial = FollowingX([profile("11", "gym_a")], complete=False)
    assert PerformanceAnalyzer(partial, other).sync_following(NOW) == EMPTY_SYNC
    assert other.get_following_state() == {}

    legacy_client = SimpleNamespace()
    assert PerformanceAnalyzer(legacy_client, other).sync_following(NOW) == EMPTY_SYNC


def test_following_notices_list_only_safe_usernames(tmp_path):
    db = Database(str(tmp_path / "notices.db"))
    telegram = FakeTelegramApi(tmp_path / "media")
    controller = TelegramController(
        telegram, db, NoopNotifier(), "42", now_fn=lambda: NOW,
    )

    assert controller.push_following_notices(["runner", "bad name!"]) == (
        "following_notices"
    )
    assert "@runner" in telegram.messages[-1][1]
    assert "bad name" not in telegram.messages[-1][1]
    assert controller.push_following_notices([]) == "following_notices_empty"
    assert len(telegram.messages) == 1


def test_follower_cycle_syncs_following_and_pushes_notices(tmp_path):
    from main import FlexDropinGrowthAgent
    from tests.test_end_to_end_dry_run import NOW as AGENT_NOW, dependency_bundle

    class Analytics:
        def __init__(self):
            self.calls = []

        def capture_follower_snapshot(self, current):
            self.calls.append(("followers", current))
            return {"followers_total": 1}

        def sync_following(self, current):
            self.calls.append(("following", current))
            return {**EMPTY_SYNC, "still_followed_after_unfollow": ["runner"]}

    class Controller:
        def __init__(self):
            self.notices = []

        def push_following_notices(self, usernames):
            self.notices.append(list(usernames))
            return "following_notices"

    analytics, controller = Analytics(), Controller()
    agent = FlexDropinGrowthAgent(dependency_bundle(
        tmp_path, analytics=analytics, telegram_controller=controller,
    ))

    assert agent.follower_snapshot_cycle(now=AGENT_NOW) == {"followers_total": 1}
    assert analytics.calls == [("followers", AGENT_NOW), ("following", AGENT_NOW)]
    assert controller.notices == [["runner"]]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_following_tracking.py -v`
Expected: FAIL. `PerformanceAnalyzer` has no `sync_following`, `TelegramController` has no `push_following_notices`, and the cycle never calls `sync_following`.

- [ ] **Step 3: Implement analytics**

In `modules/analytics.py`, add after `capture_follower_snapshot`:

```python
    def sync_following(self, observed_at: datetime) -> Dict:
        """Read the real following list once and diff it into SQLite."""
        current_time = self._aware_datetime(observed_at, "observed_at")
        empty_summary = {
            "following_total": 0,
            "new_following": 0,
            "unfollowed": 0,
            "gyms_following": 0,
            "still_followed_after_unfollow": [],
        }
        try:
            read_following = getattr(self.client, "read_following_profiles", None)
            if not callable(read_following):
                return empty_summary
            fetched = read_following()
            profiles = getattr(fetched, "profiles", None)
            complete = getattr(fetched, "complete", None) is True
        except Exception as error:
            logger.warning(
                "following_sync_read_failed error_type=%s",
                type(error).__name__,
            )
            return empty_summary
        if not complete or not isinstance(profiles, (list, tuple)):
            return empty_summary
        result = self.db.sync_following_snapshot(
            current_time, list(profiles), complete=True,
        )
        return result if isinstance(result, dict) else empty_summary
```

- [ ] **Step 4: Implement the Telegram notice**

In `modules/telegram_controller.py`, add after `push_growth_digest`:

```python
    def push_following_notices(self, usernames) -> str:
        """Warn when a locally recorded unfollow is still listed on X."""
        if not isinstance(usernames, (list, tuple)):
            return "following_notices_empty"
        safe = [
            username for username in usernames
            if type(username) is str
            and re.fullmatch(r"[A-Za-z0-9_]{1,15}", username) is not None
        ][:10]
        if not safe:
            return "following_notices_empty"
        self._send(
            self.authorized_chat_id,
            "\n".join([
                "Unfollow segnati ma ancora seguiti su X:",
                *(f"@{username}" for username in safe),
                "Controlla su X: la decisione locale è stata azzerata.",
            ]),
        )
        return "following_notices"
```

- [ ] **Step 5: Wire the cycle**

In `main.py`, replace the whole `follower_snapshot_cycle` method with:

```python
    def follower_snapshot_cycle(self, now=None):
        try:
            current = self._now() if now is None else now
            summary = self.analytics.capture_follower_snapshot(current)
        except Exception as error:
            self._notify_error("cycle", error)
            return {}
        sync_following = getattr(self.analytics, "sync_following", None)
        if callable(sync_following):
            try:
                following = sync_following(current)
                notices = (
                    following.get("still_followed_after_unfollow")
                    if isinstance(following, dict)
                    else None
                )
                if notices:
                    self.telegram_controller.push_following_notices(notices)
            except Exception as error:
                self._notify_error("following_sync_cycle", error)
        return summary
```

- [ ] **Step 6: Run the tests**

Run: `venv/bin/python -m pytest tests/test_following_tracking.py tests/test_end_to_end_dry_run.py -v`
Expected: PASS

- [ ] **Step 7: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add modules/analytics.py modules/telegram_controller.py main.py tests/test_following_tracking.py
git commit -m "feat: sync following list in the nightly follower job

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Unfollow proposals, like reasons and new local decisions in SQLite

This task only adds capabilities. Legacy decisions stay accepted until Task 7 switches the Telegram buttons, so the suite stays green.

**Files:**
- Modify: `modules/database.py`: `_GROWTH_ACCOUNT_REASONS` and `_GROWTH_POST_REASONS` (lines ~130–146), `_normalize_growth_suggestion_row` (~7702), `_load_growth_digest_conn` decision set (~7860), `mark_growth_suggestion_decision` (~8446), new methods after `get_following_state`
- Test: `tests/test_following_tracking.py`

**Interfaces:**
- Consumes: `x_following` (Task 3).
- Produces:
  - Post reason codes `followed_gym`, `suggested_gym`, `operator_pain`; account reason code `no_follow_back_after_30_days`.
  - New `reevaluate` payload shape `{"user_id", "username", "public_metrics", "followed_since", "reason_codes"}`, accepted alongside the legacy shape.
  - `Database.get_unfollow_proposals(now: datetime, *, limit: int = 5, review_days: int = 30) -> List[Dict]`. Each item is `{"user_id": str, "username": str, "public_metrics": Dict[str, int], "followed_since": str ISO, "days_following": int}`. At most `5 − reevaluate rows persisted since Monday (Rome)` items are returned.
  - `Database.get_recent_growth_post_authors(since: datetime) -> Set[str]`.
  - `mark_growth_suggestion_decision` additionally accepts `account: not_relevant`, `post: liked_manually | skipped`, `reevaluate: unfollowed_manually | keep | marked_gym`, with side effects on `growth_candidates` / `x_following`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_following_tracking.py`:

```python
import pytest


METRICS = {
    "followers_count": 100, "following_count": 50,
    "tweet_count": 20, "listed_count": 1,
}


def seed_suggestion(db, kind, object_id, username, payload, reasons, *,
                    observed_on="2026-09-15", suggested_at=NOW):
    with db._conn() as conn:
        conn.execute(
            """
            INSERT INTO growth_suggestions (
                observed_on, kind, object_id, username, payload_json, score,
                reason_codes_json, suggested_at, cooldown_until, rank_position
            ) VALUES (?, ?, ?, ?, ?, 50, ?, ?, ?, 0)
            """,
            (
                observed_on, kind, object_id, username, json.dumps(payload),
                json.dumps(reasons), suggested_at.isoformat(),
                (suggested_at + timedelta(days=30)).isoformat(),
            ),
        )
        counts = {"account": 0, "post": 0, "reevaluate": 0}
        counts[kind] = 1
        conn.execute(
            "INSERT INTO growth_digest_runs VALUES (?, ?, ?)",
            (
                observed_on, suggested_at.isoformat(),
                json.dumps({"observed_on": observed_on, "counts": counts}),
            ),
        )
    return db.get_growth_digest(observed_on)


def unfollow_payload(user_id="12", username="runner"):
    reasons = ["no_follow_back_after_30_days"]
    return {
        "user_id": user_id,
        "username": username,
        "public_metrics": dict(METRICS),
        "followed_since": (NOW - timedelta(days=31)).isoformat(),
        "reason_codes": reasons,
    }, reasons


def post_payload(post_id="7001", author_id="200", username="gym_a",
                 reasons=("followed_gym",), created_at=None):
    reasons = list(reasons)
    return {
        "id": post_id,
        "author_id": author_id,
        "author_username": username,
        "excerpt": "New class schedule at our gym.",
        "created_at": (created_at or NOW - timedelta(hours=3)).isoformat(),
        "public_metrics": {
            "like_count": 1, "retweet_count": 0, "reply_count": 0,
            "quote_count": 0, "impression_count": 10,
        },
        "reason_codes": reasons,
    }, reasons


def account_payload(user_id="101", username="studio_owner"):
    reasons = ["primary_operator_role", "active_within_7_days"]
    return {
        "user_id": user_id,
        "username": username,
        "public_metrics": dict(METRICS),
        "latest_activity_id": "9001",
        "latest_activity_at": (NOW - timedelta(hours=3)).isoformat(),
        "segment": "primary",
        "reason_codes": reasons,
    }, reasons


def follow_for_maturity(db, profiles, followed_at=NOW - timedelta(days=31),
                        followers=()):
    db.sync_following_snapshot(followed_at, profiles, complete=True)
    capture_followers(db, NOW - timedelta(hours=1), list(followers))
    db.sync_following_snapshot(NOW, profiles, complete=True)


def test_unfollow_proposal_requires_maturity_non_gym_and_later_follower_run(tmp_path):
    db = Database(str(tmp_path / "proposals.db"))
    gym = profile("11", "gym_a")
    runner = profile("12", "runner", OTHER_BIO)
    newbie = profile("13", "newbie", OTHER_BIO)
    db.sync_following_snapshot(NOW - timedelta(days=31), [gym, runner], complete=True)
    db.sync_following_snapshot(
        NOW - timedelta(days=10), [gym, runner, newbie], complete=True,
    )
    capture_followers(db, NOW - timedelta(hours=1), [])
    db.sync_following_snapshot(NOW, [gym, runner, newbie], complete=True)

    assert db.get_unfollow_proposals(NOW) == [{
        "user_id": "12",
        "username": "runner",
        "public_metrics": METRICS,
        "followed_since": (NOW - timedelta(days=31)).isoformat(),
        "days_following": 31,
    }]
    assert db.get_unfollow_proposals(NOW, review_days=14) == []


def test_follow_back_excludes_unfollow_proposal(tmp_path):
    db = Database(str(tmp_path / "follow-back.db"))
    runner = profile("12", "runner", OTHER_BIO)
    follow_for_maturity(db, [runner], followers=[runner])

    assert db.get_unfollow_proposals(NOW) == []


def test_weekly_cap_counts_reevaluate_rows_since_rome_monday(tmp_path):
    db = Database(str(tmp_path / "weekly-cap.db"))
    follow_for_maturity(db, [profile("12", "runner", OTHER_BIO)])
    with db._conn() as conn:
        for index in range(5):
            conn.execute(
                """
                INSERT INTO growth_suggestions (
                    observed_on, kind, object_id, username, payload_json,
                    score, reason_codes_json, suggested_at
                ) VALUES ('2026-09-14', 'reevaluate', ?, 'old', '{}', 0, '[]', ?)
                """,
                (str(501 + index), (NOW - timedelta(days=1)).isoformat()),
            )

    assert db.get_unfollow_proposals(NOW) == []
    assert db.get_unfollow_proposals(NOW + timedelta(days=7))[0]["user_id"] == "12"


@pytest.mark.parametrize(
    ("decision", "column", "expected"),
    [
        ("unfollowed_manually", "unfollow_decision", "unfollowed_manually"),
        ("keep", "unfollow_decision", "keep"),
        ("marked_gym", "gym_override", "gym"),
    ],
)
def test_unfollow_decisions_update_following_state(tmp_path, decision, column, expected):
    db = Database(str(tmp_path / f"{decision}.db"))
    follow_for_maturity(db, [profile("12", "runner", OTHER_BIO)])
    payload, reasons = unfollow_payload()
    digest = seed_suggestion(db, "reevaluate", "12", "runner", payload, reasons)
    suggestion = digest["reevaluate"][0]

    assert db.mark_growth_suggestion_decision(
        suggestion["id"], suggestion["revision"], decision, decided_at=NOW,
    ) == "updated"

    row = following_row(db, "12")
    assert row[column] == expected
    later = NOW + timedelta(days=8)
    assert db.get_unfollow_proposals(later) == []
    if decision == "keep":
        assert row["keep_until"] == (NOW + timedelta(days=90)).isoformat()
        assert db.get_unfollow_proposals(NOW + timedelta(days=91))[0]["user_id"] == "12"
    if decision == "marked_gym":
        assert db.get_following_state()["12"]["is_gym"] is True


def test_account_not_relevant_rejects_candidate_for_30_days(tmp_path):
    db = Database(str(tmp_path / "not-relevant.db"))
    db.upsert_growth_candidate({
        "user_id": "101", "username": "studio_owner", "profile": {},
        "latest_post": None, "score": 88, "score_data": {},
        "discovery_source": "topic_search",
    })
    payload, reasons = account_payload()
    suggestion = seed_suggestion(
        db, "account", "101", "studio_owner", payload, reasons,
    )["accounts"][0]

    assert db.mark_growth_suggestion_decision(
        suggestion["id"], suggestion["revision"], "not_relevant", decided_at=NOW,
    ) == "updated"
    with db._conn() as conn:
        candidate = conn.execute(
            "SELECT decision, rejection_reason, suppressed_until "
            "FROM growth_candidates WHERE user_id = '101'"
        ).fetchone()
    assert tuple(candidate) == (
        "rejected", "not_relevant", (NOW + timedelta(days=30)).isoformat(),
    )


@pytest.mark.parametrize("decision", ["liked_manually", "skipped"])
def test_like_decisions_are_local_and_idempotent(tmp_path, decision):
    db = Database(str(tmp_path / f"{decision}.db"))
    payload, reasons = post_payload()
    suggestion = seed_suggestion(db, "post", "7001", "gym_a", payload, reasons)["posts"][0]

    assert db.mark_growth_suggestion_decision(
        suggestion["id"], suggestion["revision"], decision, decided_at=NOW,
    ) == "updated"
    assert db.mark_growth_suggestion_decision(
        suggestion["id"], suggestion["revision"], decision, decided_at=NOW,
    ) == "duplicate"
    assert db.get_growth_digest("2026-09-15")["posts"][0]["decision"] == decision


def test_recent_post_authors_respect_the_since_boundary(tmp_path):
    db = Database(str(tmp_path / "authors.db"))
    suggested_at = NOW - timedelta(days=2)
    payload, reasons = post_payload(created_at=suggested_at - timedelta(hours=3))
    seed_suggestion(
        db, "post", "7001", "gym_a", payload, reasons,
        observed_on="2026-09-13", suggested_at=suggested_at,
    )

    assert db.get_recent_growth_post_authors(NOW - timedelta(days=3)) == {"200"}
    assert db.get_recent_growth_post_authors(NOW - timedelta(days=1)) == set()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_following_tracking.py -v`
Expected: FAIL. `get_unfollow_proposals` and `get_recent_growth_post_authors` are missing, digests with the new reason codes load as `{}`, and the new decisions return `"invalid"`.

- [ ] **Step 3: Extend reason codes**

In `modules/database.py`, add `"no_follow_back_after_30_days",` to `_GROWTH_ACCOUNT_REASONS` right after `"no_follow_back_after_14_days",`. Add to `_GROWTH_POST_REASONS`, right after `"followed_account",`:

```python
    "followed_gym", "suggested_gym", "operator_pain",
```

- [ ] **Step 4: Accept the new unfollow payload**

In `_normalize_growth_suggestion_row`, the branch currently reads `if kind == "post": ... else: ...`. Insert this `elif` between them, directly before the line `        else:` that starts the account/reevaluate validation:

```python
        elif kind == "reevaluate" and set(payload) == {
            "user_id", "username", "public_metrics", "followed_since",
            "reason_codes",
        }:
            followed_since = parse_growth_datetime(payload.get("followed_since"))
            if (
                payload.get("user_id") != object_id
                or payload.get("username") != username
                or not cls._growth_metrics_are_closed(
                    payload.get("public_metrics"), _GROWTH_PUBLIC_METRIC_KEYS
                )
                or followed_since is None
                or followed_since > completed_at
                or payload.get("reason_codes") != reasons
            ):
                return None
```

- [ ] **Step 5: Accept new decisions in the loader**

In `_load_growth_digest_conn`, replace:

```python
                    or item["decision"] not in {
                        "new", "saved", "followed_manually", "dismissed",
                        "still_relevant",
                    }
```

with:

```python
                    or item["decision"] not in {
                        "new", "saved", "followed_manually", "dismissed",
                        "still_relevant", "not_relevant", "liked_manually",
                        "skipped", "unfollowed_manually", "keep", "marked_gym",
                    }
```

- [ ] **Step 6: Extend decisions and side effects**

In `mark_growth_suggestion_decision`, replace:

```python
        allowed = {
            "account": {"followed_manually"},
            "reevaluate": {"still_relevant", "dismissed"},
        }
        if (
            type(suggestion_id) is not int
            or suggestion_id <= 0
            or type(expected_revision) is not int
            or expected_revision < 0
            or decision not in {"followed_manually", "still_relevant", "dismissed"}
        ):
```

with:

```python
        allowed = {
            "account": {"followed_manually", "not_relevant"},
            "post": {"liked_manually", "skipped"},
            "reevaluate": {
                "still_relevant", "dismissed",
                "unfollowed_manually", "keep", "marked_gym",
            },
        }
        if (
            type(suggestion_id) is not int
            or suggestion_id <= 0
            or type(expected_revision) is not int
            or expected_revision < 0
            or decision not in set().union(*allowed.values())
        ):
```

Replace the tail of the method, from `            if decision == "followed_manually":` to the final `            return "updated"`, with:

```python
            current_utc = current.astimezone(timezone.utc)
            if decision == "followed_manually":
                conn.execute(
                    """
                    UPDATE growth_candidates
                    SET decision = 'followed_manually', decision_at = ?,
                        manual_followed_at = COALESCE(manual_followed_at, ?),
                        rejection_reason = NULL, suppressed_until = NULL
                    WHERE user_id = ? AND decision IN ('new', 'saved')
                    """,
                    (decided_iso, decided_iso, row["object_id"]),
                )
            elif decision == "not_relevant":
                conn.execute(
                    """
                    UPDATE growth_candidates
                    SET decision = 'rejected', rejection_reason = 'not_relevant',
                        decision_at = ?, suppressed_until = ?
                    WHERE user_id = ? AND decision IN ('new', 'saved')
                    """,
                    (
                        decided_iso,
                        (current_utc + timedelta(days=30)).isoformat(),
                        row["object_id"],
                    ),
                )
            elif decision == "unfollowed_manually":
                conn.execute(
                    """
                    UPDATE x_following
                    SET unfollow_decision = 'unfollowed_manually',
                        unfollow_decision_at = ?
                    WHERE user_id = ? AND unfollowed_at IS NULL
                    """,
                    (decided_iso, row["object_id"]),
                )
            elif decision == "keep":
                conn.execute(
                    """
                    UPDATE x_following
                    SET unfollow_decision = 'keep', unfollow_decision_at = ?,
                        keep_until = ?
                    WHERE user_id = ?
                    """,
                    (
                        decided_iso,
                        (current_utc + timedelta(days=90)).isoformat(),
                        row["object_id"],
                    ),
                )
            elif decision == "marked_gym":
                conn.execute(
                    "UPDATE x_following SET gym_override = 'gym' WHERE user_id = ?",
                    (row["object_id"],),
                )
            return "updated"
```

- [ ] **Step 7: Add proposal and author queries**

Add after `get_following_state`:

```python
    def get_unfollow_proposals(
        self,
        now: datetime,
        *,
        limit: int = 5,
        review_days: int = 30,
    ) -> List[Dict]:
        """Return non-gym followed accounts that never followed back."""
        if (
            type(now) is not datetime
            or now.tzinfo is None
            or now.utcoffset() is None
            or type(limit) is not int
            or limit <= 0
            or type(review_days) is not int
            or review_days < 30
        ):
            return []
        current = now.astimezone(timezone.utc)
        rome_today = current.astimezone(ZoneInfo("Europe/Rome")).date()
        week_start = (rome_today - timedelta(days=rome_today.weekday())).isoformat()
        maturity = timedelta(days=review_days)
        with self._conn() as conn:
            used = conn.execute("""
                SELECT COUNT(*) AS count FROM growth_suggestions
                WHERE kind = 'reevaluate' AND observed_on >= ?
            """, (week_start,)).fetchone()["count"]
            rows = conn.execute("""
                SELECT * FROM x_following
                WHERE unfollowed_at IS NULL AND follows_back = 0
            """).fetchall()
        remaining = min(limit, 5) - (used if type(used) is int else 5)
        if remaining <= 0:
            return []
        eligible = []
        for row in rows:
            first_seen = parse_growth_datetime(row["first_seen_following_at"])
            checked = parse_growth_datetime(row["follows_back_checked_at"])
            keep_until = parse_growth_datetime(row["keep_until"])
            if (
                not self._canonical_growth_object_id(row["user_id"])
                or first_seen is None
                or checked is None
                or first_seen > current - maturity
                or checked < first_seen + maturity
                or checked > current
                or self._effective_gym(row["is_gym"], row["gym_override"])
                or row["unfollow_decision"] == "unfollowed_manually"
                or (
                    row["unfollow_decision"] == "keep"
                    and (keep_until is None or keep_until > current)
                )
            ):
                continue
            try:
                stored = json.loads(row["profile_json"])
            except (TypeError, ValueError):
                continue
            if type(stored) is not dict:
                continue
            metrics = {key: stored.get(key) for key in _GROWTH_PUBLIC_METRIC_KEYS}
            if not self._growth_metrics_are_closed(metrics, _GROWTH_PUBLIC_METRIC_KEYS):
                continue
            eligible.append({
                "user_id": row["user_id"],
                "username": row["username"],
                "public_metrics": metrics,
                "followed_since": first_seen.isoformat(),
                "days_following": (current - first_seen).days,
            })
        eligible.sort(key=lambda item: (item["followed_since"], item["user_id"]))
        return eligible[:remaining]

    def get_recent_growth_post_authors(self, since: datetime) -> Set[str]:
        """Return author ids of like suggestions made at or after ``since``."""
        if (
            type(since) is not datetime
            or since.tzinfo is None
            or since.utcoffset() is None
        ):
            return set()
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT payload_json FROM growth_suggestions
                WHERE kind = 'post'
                  AND julianday(suggested_at) >= julianday(?)
            """, (since.astimezone(timezone.utc).isoformat(),)).fetchall()
        authors = set()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                continue
            author_id = payload.get("author_id") if type(payload) is dict else None
            if self._canonical_growth_object_id(author_id):
                authors.add(author_id)
        return authors
```

- [ ] **Step 8: Run the tests**

Run: `venv/bin/python -m pytest tests/test_following_tracking.py tests/test_growth_digest.py tests/test_growth_digest_telegram.py -v`
Expected: PASS

- [ ] **Step 9: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add modules/database.py tests/test_following_tracking.py
git commit -m "feat: persist unfollow proposals and manual like decisions

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Digest selects gyms, likes and unfollow proposals

**Files:**
- Modify: `modules/growth_digest.py`
- Modify: `modules/database.py` (delete `get_growth_reevaluation_candidates`)
- Modify: `main.py` (`GrowthDigestService(...)` construction, ~line 401; import `GROWTH_UNFOLLOW_REVIEW_DAYS` from `config`)
- Modify: `tests/test_growth_digest.py`
- Modify: `tests/test_end_to_end_dry_run.py` (line ~799)

**Interfaces:**
- Consumes: `Database.get_following_state`, `get_unfollow_proposals`, `get_recent_growth_post_authors` (Tasks 3, 5); latest-post `public_metrics` (Task 2).
- Produces:
  - `growth_digest.GROWTH_POST_QUERY_BUDGET = 1`; `POST_QUERY_PORTFOLIO` holds only `gym_operator_pain`.
  - `growth_digest.score_gym_post(post: Dict, now: datetime, *, source: str, max_age: timedelta) -> Optional[Dict]` returning `{"score": int, "created_at": datetime, "reason_codes": List[str]}`.
  - `GrowthDigestService(..., post_query_budget: int = 1 (max 1), unfollow_review_days: int = 30 (min 30))`.
  - Digest `posts` rows carry the source as the first reason code (`followed_gym` / `suggested_gym` / `operator_pain`).
  - Digest `reevaluate` rows use the new payload shape with reason `no_follow_back_after_30_days`.

- [ ] **Step 1: Update the test doubles and write the failing tests**

In `tests/test_growth_digest.py`, replace the `DigestX` class with:

```python
class DigestX:
    def __init__(self, pages=None, complete=True, following_rows=None,
                 following_complete=None):
        self.pages = pages or {}
        self.complete = complete
        self.following_complete = (
            complete if following_complete is None else following_complete
        )
        self.following_rows = list(following_rows or [])
        self.queries = []
        self.following_reads = 0
        self.engagement_writes = []

    def read_relevant_posts(self, query, limit=25):
        self.queries.append((query, limit))
        rows = self.pages.get(len(self.queries) - 1, [])
        return RelevantPostsRead(tuple(rows), self.complete)

    def read_following_timeline(self, limit=25):
        assert limit == 25
        self.following_reads += 1
        return RelevantPostsRead(
            tuple(self.following_rows), self.following_complete,
        )
```

Replace `test_service_builds_closed_ranked_daily_digest_with_fixed_read_budget`, `test_service_includes_relevant_posts_from_accounts_followed_on_x` and `test_duplicate_search_post_keeps_followed_account_provenance` with:

```python
GYM_BIO = "Independent gym in Austin, TX. Classes daily."


def _gym_profile(user_id, username, description=GYM_BIO):
    return {
        "id": user_id, "user_id": user_id, "username": username,
        "description": description, "protected": False,
        "location": "Austin, TX", "created_at": None,
        "followers_count": 100, "following_count": 50,
        "tweet_count": 20, "listed_count": 1, "spam_signals": [],
    }


def _follow(database, profiles, when=None):
    assert database.sync_following_snapshot(
        when or NOW - timedelta(days=1), profiles, complete=True,
    ) is not None


def test_service_builds_closed_ranked_daily_digest_with_one_search(tmp_path):
    database = Database(str(tmp_path / "service.db"))
    x_client = DigestX({
        0: [
            _normalized_post(
                "9002", "Pilates studios can fill empty class spots.",
                author_id="102", author_username="pilatesowner",
            ),
            _normalized_post("9001"),
            _normalized_post("9003", "A generic fitness motivation quote."),
        ],
    })
    discovery = DigestDiscovery()

    digest = GrowthDigestService(x_client, database, discovery=discovery).build(NOW)

    assert digest["observed_on"] == "2026-08-26"
    assert digest["outcome"] == "created"
    assert [row["object_id"] for row in digest["accounts"]] == ["101"]
    assert [row["object_id"] for row in digest["posts"]] == ["9001", "9002"]
    assert digest["posts"][0]["reason_codes"][0] == "operator_pain"
    assert digest["posts"][0]["score"] == 84
    assert len(x_client.queries) == 1
    assert x_client.queries[0][1] == 25
    assert discovery.calls == 1
    serialized = json.dumps(digest, allow_nan=False)
    assert "private body" not in serialized
    assert "source body" not in serialized
    assert "generic fitness motivation" not in serialized
    assert x_client.engagement_writes == []


def test_followed_gym_posts_skip_keywords_but_respect_age_and_gym_status(tmp_path):
    database = Database(str(tmp_path / "followed-gym.db"))
    _follow(database, [
        _gym_profile("301", "gym_one"),
        _gym_profile("302", "runner", "Fitness podcast host and runner."),
    ])
    x_client = DigestX({0: []}, following_rows=[
        _normalized_post(
            "9101", "Saturday open session at 9am, see you there!",
            author_id="301", author_username="gym_one",
            created_at=(NOW - timedelta(hours=10)).isoformat(),
        ),
        _normalized_post(
            "9102", "Old news from the gym floor.",
            author_id="301", author_username="gym_one",
            created_at=(NOW - timedelta(hours=80)).isoformat(),
        ),
        _normalized_post(
            "9103", "Gym owners can fill empty class capacity.",
            author_id="302", author_username="runner",
        ),
    ])

    digest = GrowthDigestService(
        x_client, database, discovery=DigestDiscovery([]),
    ).build(NOW)

    assert x_client.following_reads == 1
    assert [row["object_id"] for row in digest["posts"]] == ["9101"]
    assert digest["posts"][0]["reason_codes"] == ["followed_gym", "recent"]


def test_like_quotas_fill_order_and_one_post_per_author(tmp_path):
    database = Database(str(tmp_path / "quotas.db"))
    _follow(database, [
        _gym_profile(str(301 + index), f"gym_{index}") for index in range(5)
    ])
    timeline = [
        _normalized_post(
            str(9200 + index), "Open gym tonight at 6pm.",
            author_id=str(301 + index), author_username=f"gym_{index}",
        )
        for index in range(5)
    ]
    search = [
        _normalized_post(
            str(9300 + index),
            author_id=str(401 + index), author_username=f"owner_{index}",
        )
        for index in range(6)
    ] + [
        _normalized_post("9399", author_id="401", author_username="owner_0"),
    ]

    digest = GrowthDigestService(
        DigestX({0: search}, following_rows=timeline),
        database,
        discovery=DigestDiscovery([]),
    ).build(NOW)

    sources = [row["reason_codes"][0] for row in digest["posts"]]
    assert sources == (
        ["followed_gym"] * 4 + ["operator_pain"] * 3
        + ["followed_gym"] + ["operator_pain"] * 2
    )
    authors = [row["payload"]["author_id"] for row in digest["posts"]]
    assert len(authors) == len(set(authors)) == 10


def test_suggested_gym_latest_post_becomes_a_like(tmp_path):
    candidate = _candidate()
    candidate["latest_post"].update({
        "text": "Open gym Saturday at 9am.",
        "lang": "en",
        "public_metrics": {
            "like_count": 2, "retweet_count": 0, "reply_count": 0,
            "quote_count": 0, "impression_count": 40,
        },
    })

    digest = GrowthDigestService(
        DigestX({0: []}),
        Database(str(tmp_path / "suggested.db")),
        discovery=DigestDiscovery([candidate]),
    ).build(NOW)

    assert [row["object_id"] for row in digest["accounts"]] == ["101"]
    assert [row["object_id"] for row in digest["posts"]] == ["8001"]
    assert digest["posts"][0]["reason_codes"] == ["suggested_gym", "recent"]


def test_like_author_cooldown_lasts_three_days(tmp_path):
    path = str(tmp_path / "author-cooldown.db")

    def build(post_id, when):
        return GrowthDigestService(
            DigestX({0: [_normalized_post(
                post_id, author_id="401", author_username="owner_one",
                created_at=(when - timedelta(hours=2)).isoformat(),
            )]}),
            Database(path),
            discovery=DigestDiscovery([]),
        ).build(when)

    assert [row["object_id"] for row in build("9001", NOW)["posts"]] == ["9001"]
    assert build("9002", NOW + timedelta(days=1))["posts"] == []
    later = NOW + timedelta(days=3, hours=1)
    assert [row["object_id"] for row in build("9003", later)["posts"]] == ["9003"]


def test_account_suggestions_are_only_unfollowed_primary_gyms(tmp_path):
    database = Database(str(tmp_path / "accounts.db"))
    _follow(database, [_gym_profile("101", "gymowner")])
    amplifier = _candidate("102", "amplifier")
    amplifier["audience_segment"] = "amplifier"
    amplifier["reasons"] = ["amplifier_role"]

    digest = GrowthDigestService(
        DigestX({0: []}),
        database,
        discovery=DigestDiscovery([
            _candidate("101", "gymowner"), amplifier, _candidate("103", "newgym"),
        ]),
    ).build(NOW)

    assert [row["object_id"] for row in digest["accounts"]] == ["103"]


def test_timeline_failure_keeps_digest_complete(tmp_path):
    digest = GrowthDigestService(
        DigestX({0: [_normalized_post()]}, following_complete=False),
        Database(str(tmp_path / "timeline-failure.db")),
        discovery=DigestDiscovery([]),
    ).build(NOW)

    assert digest["outcome"] == "created"
    assert [row["object_id"] for row in digest["posts"]] == ["9001"]


def test_unfollow_rows_come_from_following_tracking(tmp_path):
    database = Database(str(tmp_path / "unfollow-rows.db"))
    runner = _gym_profile("12", "runner", "Fitness podcast host and runner.")
    _follow(database, [runner], when=NOW - timedelta(days=31))
    observed_on = (NOW - timedelta(hours=1)).astimezone(
        ZoneInfo("Europe/Rome")
    ).date().isoformat()
    assert database.capture_follower_snapshot_batch(
        observed_on, NOW - timedelta(hours=1), [], 0,
    ) is not None
    _follow(database, [runner], when=NOW - timedelta(minutes=30))

    digest = GrowthDigestService(
        DigestX({0: []}), database, discovery=DigestDiscovery([]),
    ).build(NOW)

    assert [row["object_id"] for row in digest["reevaluate"]] == ["12"]
    assert digest["reevaluate"][0]["payload"]["followed_since"] == (
        (NOW - timedelta(days=31)).isoformat()
    )
    assert digest["reevaluate"][0]["reason_codes"] == [
        "no_follow_back_after_30_days",
    ]


@pytest.mark.parametrize(
    "overrides",
    [{"post_query_budget": 2}, {"unfollow_review_days": 29}],
)
def test_service_rejects_out_of_policy_limits(tmp_path, overrides):
    with pytest.raises(ValueError):
        GrowthDigestService(
            DigestX(), Database(str(tmp_path / "limits.db")),
            discovery=DigestDiscovery([]), **overrides,
        )
```

Add `from zoneinfo import ZoneInfo` to the imports at the top of `tests/test_growth_digest.py`.

Delete `test_reevaluation_requires_complete_absent_snapshot_after_fourteen_days` and `test_reevaluation_accepts_older_complete_snapshot_after_accounts_own_boundary`. The method they cover is removed in Step 6; Task 5 tests cover the replacement.

In `test_service_two_threads_return_one_exact_committed_digest`, replace `assert len(x_client.queries) == 2` with `assert len(x_client.queries) == 1`.

In `tests/test_end_to_end_dry_run.py`, replace:

```python
    assert agent.db.get_growth_reevaluation_candidates(digest_now, limit=5) == []
```

with:

```python
    assert agent.db.get_unfollow_proposals(digest_now, limit=5) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_growth_digest.py -v`
Expected: FAIL. Two queries still run, reason codes lack a source, amplifiers and followed accounts are still suggested, and the timeline failure makes the digest `incomplete`.

- [ ] **Step 3: Replace constants and add gym-post scoring**

In `modules/growth_digest.py`, replace `GROWTH_POST_QUERY_BUDGET = 2` and the whole `POST_QUERY_PORTFOLIO` tuple with:

```python
GROWTH_POST_QUERY_BUDGET = 1
POST_QUERY_PORTFOLIO: Tuple[Tuple[str, str], ...] = (
    (
        "gym_operator_pain",
        '("gym owner" OR "studio owner" OR "box owner" OR "fitness studio" OR '
        '"CrossFit box" OR "boxing gym" OR "pilates studio" OR "yoga studio") '
        '("empty spots" OR "no-shows" OR "no show" OR "last-minute cancellation" OR '
        '"WhatsApp booking" OR "manual booking" OR "day pass" OR "drop-in" OR '
        'bookings OR revenue OR waitlist OR capacity) '
        'lang:en -is:retweet -is:reply',
    ),
)
LIKE_SOURCE_QUOTAS: Tuple[Tuple[str, int], ...] = (
    ("followed_gym", 4),
    ("suggested_gym", 3),
    ("operator_pain", 3),
)
LIKE_FILL_ORDER: Tuple[str, ...] = ("followed_gym", "operator_pain", "suggested_gym")
FOLLOWED_GYM_MAX_AGE = timedelta(hours=72)
SUGGESTED_GYM_MAX_AGE = timedelta(days=7)
LIKE_AUTHOR_COOLDOWN = timedelta(days=3)
```

Add this function directly after `score_growth_post`:

```python
def score_gym_post(
    post: Dict,
    now: datetime,
    *,
    source: str,
    max_age: timedelta,
) -> Optional[Dict]:
    """Score one original gym post without requiring operator keywords."""
    if (
        type(post) is not dict
        or type(now) is not datetime
        or source not in {"followed_gym", "suggested_gym"}
    ):
        return None
    text = post.get("text")
    created_at = parse_growth_datetime(post.get("created_at"))
    if (
        not _canonical_id(post.get("id"))
        or not _canonical_id(post.get("author_id"))
        or not _username(post.get("author_username"))
        or type(text) is not str
        or not text.strip()
        or len(text) > 1000
        or post.get("lang") != "en"
        or created_at is None
        or not _closed_metrics(post.get("public_metrics"), _POST_METRIC_KEYS)
        or _NOISE_PATTERN.search(text.lower())
    ):
        return None
    age = now.astimezone(timezone.utc) - created_at
    if age < timedelta(0) or age > max_age:
        return None
    recent = age <= timedelta(days=1)
    return {
        "score": 80 if recent else 65,
        "created_at": created_at,
        "reason_codes": [source, "recent"] if recent else [source],
    }
```

- [ ] **Step 4: Update the constructor**

In `GrowthDigestService.__init__`, add the parameter `unfollow_review_days: int = 30,` after `cooldown_days: int = 30,`. In the limits tuple, change `("post_query_budget", post_query_budget, 2),` to `("post_query_budget", post_query_budget, 1),`. After the `cooldown_days` check, add:

```python
        if type(unfollow_review_days) is not int or unfollow_review_days < 30:
            raise ValueError("unfollow_review_days must be at least 30")
```

and after `self.cooldown_days = cooldown_days`, add `self.unfollow_review_days = unfollow_review_days`.

- [ ] **Step 5: Restrict accounts, build likes and unfollow rows**

Change the `_account_rows` signature to `def _account_rows(self, candidates: object, now: datetime, followed_ids: frozenset) -> List[Dict]:`. In its big validation `if (...)`, replace `or segment not in {"primary", "amplifier", "end_user"}` with:

```python
                or segment != "primary"
                or user_id in followed_ids
```

Replace the whole `_post_rows` method with:

```python
    def _like_rows(
        self,
        scored_posts: Sequence[Tuple[str, Dict, Dict]],
        now: datetime,
    ) -> List[Dict]:
        current = now.astimezone(timezone.utc)
        recent_authors = self.db.get_recent_growth_post_authors(
            current - LIKE_AUTHOR_COOLDOWN
        )
        buckets: Dict[str, List[Tuple[Dict, Dict]]] = {
            source: [] for source, _quota in LIKE_SOURCE_QUOTAS
        }
        seen_posts = set()
        for source, post, scored in scored_posts:
            if (
                source not in buckets
                or post["id"] in seen_posts
                or post["author_id"] in recent_authors
                or self.db.growth_object_in_cooldown("post", post["id"], current)
            ):
                continue
            seen_posts.add(post["id"])
            buckets[source].append((post, scored))
        for items in buckets.values():
            items.sort(key=lambda item: (
                -item[1]["score"],
                -item[1]["created_at"].timestamp(),
                item[0]["id"],
            ))
        selected: List[Tuple[Dict, Dict]] = []
        authors = set()

        def take(source: str, count: int) -> None:
            taken = 0
            while buckets[source] and taken < count and len(selected) < self.post_limit:
                post, scored = buckets[source].pop(0)
                if post["author_id"] in authors:
                    continue
                authors.add(post["author_id"])
                selected.append((post, scored))
                taken += 1

        for source, quota in LIKE_SOURCE_QUOTAS:
            take(source, quota)
        for source in LIKE_FILL_ORDER:
            take(source, self.post_limit)
        rows = []
        for post, scored in selected:
            reasons = list(scored["reason_codes"])[:10]
            payload = {
                "id": post["id"],
                "author_id": post["author_id"],
                "author_username": post["author_username"],
                "excerpt": " ".join(post["text"].split())[:280],
                "created_at": scored["created_at"].isoformat(),
                "public_metrics": dict(post["public_metrics"]),
                "reason_codes": reasons,
            }
            rows.append({
                "object_id": post["id"],
                "username": post["author_username"],
                "payload": payload,
                "score": scored["score"],
                "reason_codes": reasons,
                "cooldown_until": (
                    current + timedelta(days=self.cooldown_days)
                ).isoformat(),
            })
        return rows

    def _scored_like_candidates(
        self,
        posts: Sequence[Dict],
        candidates: object,
        suggested_ids: set,
        followed_gym_ids: frozenset,
        now: datetime,
    ) -> List[Tuple[str, Dict, Dict]]:
        scored_posts = []
        for post in posts:
            if type(post) is not dict or post.get("source_kind") != "following":
                continue
            if post.get("author_id") not in followed_gym_ids:
                continue
            scored = score_gym_post(
                post, now, source="followed_gym", max_age=FOLLOWED_GYM_MAX_AGE,
            )
            if scored is not None:
                scored_posts.append(("followed_gym", post, scored))
        for post in posts:
            if type(post) is not dict or post.get("source_kind") == "following":
                continue
            scored = score_growth_post(post, now)
            if scored is not None:
                scored_posts.append(("operator_pain", post, {
                    **scored,
                    "reason_codes": ["operator_pain", *scored["reason_codes"]],
                }))
        for candidate in candidates if isinstance(candidates, (list, tuple)) else []:
            if type(candidate) is not dict or candidate.get("user_id") not in suggested_ids:
                continue
            latest = candidate.get("latest_post")
            if type(latest) is not dict:
                continue
            post = {
                "id": latest.get("id"),
                "text": latest.get("text"),
                "author_id": candidate.get("user_id"),
                "author_username": candidate.get("username"),
                "created_at": latest.get("created_at"),
                "lang": latest.get("lang"),
                "public_metrics": latest.get("public_metrics"),
            }
            scored = score_gym_post(
                post, now, source="suggested_gym", max_age=SUGGESTED_GYM_MAX_AGE,
            )
            if scored is not None:
                scored_posts.append(("suggested_gym", post, scored))
        return scored_posts
```

Replace the whole `_reevaluation_rows` method with:

```python
    def _unfollow_rows(self, now: datetime) -> List[Dict]:
        current = now.astimezone(timezone.utc)
        rows = []
        for candidate in self.db.get_unfollow_proposals(
            current,
            limit=self.reevaluate_limit,
            review_days=self.unfollow_review_days,
        ):
            if type(candidate) is not dict:
                continue
            user_id = candidate.get("user_id")
            username = candidate.get("username")
            metrics = candidate.get("public_metrics")
            followed_since = parse_growth_datetime(candidate.get("followed_since"))
            days = candidate.get("days_following")
            if (
                not _canonical_id(user_id)
                or not _username(username)
                or not _closed_metrics(metrics, _AUTHOR_METRIC_KEYS)
                or followed_since is None
                or self.db.growth_object_in_cooldown("reevaluate", user_id, current)
            ):
                continue
            reasons = ["no_follow_back_after_30_days"]
            rows.append({
                "object_id": user_id,
                "username": username,
                "payload": {
                    "user_id": user_id,
                    "username": username,
                    "public_metrics": dict(metrics),
                    "followed_since": followed_since.isoformat(),
                    "reason_codes": list(reasons),
                },
                "score": min(days, 100) if type(days) is int and days >= 0 else 0,
                "reason_codes": list(reasons),
                "cooldown_until": (
                    current + timedelta(days=self.cooldown_days)
                ).isoformat(),
            })
            if len(rows) >= self.reevaluate_limit:
                break
        return rows
```

- [ ] **Step 6: Make the timeline read fail-soft**

In `_read_posts`, inside `if callable(following_reader):`, replace the `try: ... except ...:` block (from `            try:` through the two `return None` lines of the except clause) with:

```python
            try:
                result = following_reader(limit=25)
                page_rows = getattr(result, "posts", None)
                complete = getattr(result, "complete", None)
                if complete is not True or not isinstance(page_rows, (list, tuple)):
                    logger.warning("growth_digest_following_read_incomplete")
                    page_rows = ()
            except Exception as error:
                logger.warning(
                    "growth_digest_following_read_failed error_type=%s",
                    type(error).__name__,
                )
                page_rows = ()
```

The claim token stays in `claim_tokens`, so the atomic persist still marks it completed.

- [ ] **Step 7: Wire `build`**

In `build`, replace:

```python
        post_candidates, query_claim_tokens = post_read
        account_rows = self._account_rows(candidates, current)
        post_rows = self._post_rows(post_candidates, current)
        reevaluate_rows = self._reevaluation_rows(
            self.db.get_growth_reevaluation_candidates(
                current, limit=self.reevaluate_limit
            ),
            current,
        )
```

with:

```python
        post_candidates, query_claim_tokens = post_read
        following = self.db.get_following_state()
        followed_ids = frozenset(following)
        followed_gym_ids = frozenset(
            user_id for user_id, state in following.items() if state["is_gym"]
        )
        account_rows = self._account_rows(candidates, current, followed_ids)
        post_rows = self._like_rows(
            self._scored_like_candidates(
                post_candidates,
                candidates,
                {row["object_id"] for row in account_rows},
                followed_gym_ids,
                current,
            ),
            current,
        )
        reevaluate_rows = self._unfollow_rows(current)
```

- [ ] **Step 8: Remove the old reevaluation query and wire config**

In `modules/database.py`, delete the whole `get_growth_reevaluation_candidates` method.

In `main.py`, add `GROWTH_UNFOLLOW_REVIEW_DAYS,` to the `from config import (...)` list, and add `unfollow_review_days=GROWTH_UNFOLLOW_REVIEW_DAYS,` to the `GrowthDigestService(...)` call after `cooldown_days=GROWTH_SUGGESTION_COOLDOWN_DAYS,`.

- [ ] **Step 9: Run the tests**

Run: `venv/bin/python -m pytest tests/test_growth_digest.py tests/test_end_to_end_dry_run.py tests/test_reply_copilot.py -v`
Expected: PASS. If a remaining legacy test in `tests/test_growth_digest.py` builds a digest with `post_query_budget=2`, change it to `1`. If one asserts two search queries, change the expectation to one.

- [ ] **Step 10: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 11: Commit**

```bash
git add modules/growth_digest.py modules/database.py main.py tests/test_growth_digest.py tests/test_end_to_end_dry_run.py
git commit -m "feat: digest suggests gyms, likes and unfollow proposals

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Telegram cards and callbacks for the manual flow

**Files:**
- Modify: `modules/telegram_controller.py` (`push_growth_digest`, `_growth_digest_detail`, `_growth_digest_action`, `_help`; import `parse_growth_datetime`)
- Modify: `modules/database.py` (`mark_growth_suggestion_decision`: drop legacy decisions)
- Modify: `tests/test_growth_digest_telegram.py`
- Modify: `tests/test_end_to_end_dry_run.py` (`test_daily_growth_digest_restart_and_all_callbacks_never_write_x`, `test_operator_acceptance_story_dry_run` step 6)
- Modify: `tests/test_reply_copilot_telegram.py` (help text assertion)

**Interfaces:**
- Consumes: decisions from Task 5; row shapes from Task 6.
- Produces: callback prefixes `gd:{a|p|r}:{id}:{rev}` (detail, unchanged) and `gda:{n|l|s|u|k|g}:{id}:{rev}` (actions). Header button labels `Seguire`, `Like`, `Unfollow`.

- [ ] **Step 1: Update the Telegram tests**

In `tests/test_growth_digest_telegram.py`:

In `test_manual_command_and_scheduled_push_share_one_compact_formatter`, replace the three `assert "... : 1" in telegram.messages[0][1]` lines and the button assertion with:

```python
    assert "Palestre da seguire: 1" in telegram.messages[0][1]
    assert "Like: 1" in telegram.messages[0][1]
    assert "Unfollow proposti: 1" in telegram.messages[0][1]
    assert [button["text"] for button in _buttons(telegram.messages[0])] == [
        "Seguire", "Like", "Unfollow",
    ]
```

Replace `test_account_detail_uses_public_url_and_local_idempotent_follow_ack` with:

```python
def test_account_detail_offers_profile_links_and_local_not_relevant(tmp_path):
    db = Database(str(tmp_path / "digest.db"))
    db.upsert_growth_candidate({
        "user_id": "101",
        "username": "studio_owner",
        "profile": {},
        "latest_post": None,
        "score": 88,
        "score_data": {},
        "discovery_source": "topic_search",
    })
    digest = _seed_digest(db, kinds=("account",))
    controller, _db, telegram, _service = _controller(tmp_path, digest, db=db)
    controller.push_growth_digest(digest, explicit=True)
    navigation = _buttons(telegram.messages[-1])[0]

    assert controller.process_update(
        callback_update(10, navigation["callback_data"])
    ) == "processed"
    detail = telegram.messages[-1]
    assert "Palestra da seguire @studio_owner" in detail[1]
    assert "follower: 1200" in detail[1]
    buttons = _buttons(detail)
    assert buttons[0] == {"text": "Apri profilo", "url": "https://x.com/studio_owner"}
    assert buttons[1] == {
        "text": "Apri ultimo post",
        "url": "https://x.com/studio_owner/status/9001",
    }
    assert buttons[2]["text"] == "Non pertinente"

    action = buttons[2]["callback_data"]
    assert controller.process_update(callback_update(11, action)) == "processed"
    assert "non pertinente" in telegram.messages[-1][1].lower()
    with db._conn() as conn:
        saved = conn.execute(
            "SELECT decision, revision FROM growth_suggestions"
        ).fetchone()
        candidate = conn.execute(
            "SELECT decision FROM growth_candidates WHERE user_id = '101'"
        ).fetchone()
    assert tuple(saved) == ("not_relevant", 1)
    assert candidate["decision"] == "rejected"

    restarted = TelegramController(
        FakeTelegramApi(tmp_path / "restart-media"), Database(db.db_path),
        NoopNotifier(), "42", growth_digest=FixedDigest(digest), now_fn=lambda: NOW,
    )
    assert restarted.process_update(callback_update(12, action)) == "processed"
    assert "non pertinente" in restarted.telegram_api.messages[-1][1].lower()
    with db._conn() as conn:
        assert tuple(conn.execute(
            "SELECT decision, revision FROM growth_suggestions"
        ).fetchone()) == ("not_relevant", 1)
```

Replace `test_post_and_reevaluation_details_never_offer_x_write_callbacks` with:

```python
def test_like_and_unfollow_cards_only_record_local_decisions(tmp_path):
    db = Database(str(tmp_path / "digest.db"))
    digest = _seed_digest(db, kinds=("post", "reevaluate"))
    controller, _db, telegram, _service = _controller(tmp_path, digest, db=db)
    controller.push_growth_digest(digest, explicit=True)
    navigation = {button["text"]: button for button in _buttons(telegram.messages[-1])}

    controller.process_update(callback_update(20, navigation["Like"]["callback_data"]))
    post_card = telegram.messages[-1]
    post_buttons = _buttons(post_card)
    assert post_card[1].startswith("Like — ")
    assert post_buttons[0] == {
        "text": "Apri post su X",
        "url": "https://x.com/gym_writer/status/7001",
    }
    assert [button["text"] for button in post_buttons[1:]] == ["Like messo", "Salta"]
    assert all(
        button["callback_data"].startswith("gda:") for button in post_buttons[1:]
    )
    controller.process_update(callback_update(21, post_buttons[1]["callback_data"]))

    controller.process_update(
        callback_update(22, navigation["Unfollow"]["callback_data"])
    )
    unfollow_buttons = _buttons(telegram.messages[-1])
    assert unfollow_buttons[0] == {
        "text": "Apri profilo", "url": "https://x.com/old_contact",
    }
    assert [button["text"] for button in unfollow_buttons[1:]] == [
        "Unfollow fatto", "Tieni", "È una palestra",
    ]
    controller.process_update(callback_update(23, unfollow_buttons[2]["callback_data"]))

    with db._conn() as conn:
        decisions = dict(conn.execute(
            "SELECT kind, decision FROM growth_suggestions"
        ).fetchall())
    assert decisions == {"post": "liked_manually", "reevaluate": "keep"}
```

In `test_reevaluation_dismiss_is_local_and_wrong_revision_fails_closed`, rename it to `test_unfollow_keep_is_local_and_wrong_revision_fails_closed` and replace the decision assertion `== "dismissed"` with `== "keep"`. The line `dismiss = _buttons(telegram.messages[-1])[2]["callback_data"]` stays valid, because index 2 is now `Tieni`.

In `test_follow_ack_has_one_sqlite_winner_and_rolls_back_on_error`, replace both `"followed_manually"` strings with `"not_relevant"`, so the tuple assertion becomes `("not_relevant", 1)`.

Append:

```python
def test_legacy_growth_decisions_are_rejected(tmp_path):
    db = Database(str(tmp_path / "legacy-decisions.db"))
    digest = _seed_digest(db)
    account = digest["accounts"][0]
    reevaluate = digest["reevaluate"][0]

    assert db.mark_growth_suggestion_decision(
        account["id"], account["revision"], "followed_manually", decided_at=NOW,
    ) == "invalid"
    assert db.mark_growth_suggestion_decision(
        reevaluate["id"], reevaluate["revision"], "dismissed", decided_at=NOW,
    ) == "invalid"
```

In `tests/test_end_to_end_dry_run.py`, `test_daily_growth_digest_restart_and_all_callbacks_never_write_x`:
- `navigation["Account"]` → `navigation["Seguire"]`
- `account_action = _buttons(...)[1]["callback_data"]` → `account_action = _buttons(dependencies["telegram_api"].messages[-1])[2]["callback_data"]`
- `navigation["Post"]` → `navigation["Like"]`
- `navigation["Da rivalutare"]` → `navigation["Unfollow"]`
- `assert "nessuna azione" in restart_dependencies[...]` → `assert "non pertinente" in restart_dependencies["telegram_api"].messages[-1][1].lower()`

In `test_operator_acceptance_story_dry_run`, step 6:
- `btn(digest_msg, "Account")` → `btn(digest_msg, "Seguire")`
- `btn(account_detail, "Segnala come seguito")` → `btn(account_detail, "Non pertinente")`
- `btn(digest_msg, "Post")` → `btn(digest_msg, "Like")`
- replace `assert all("gda" not in (b.get("callback_data") or "") for b in all_btns)` with `assert [b["text"] for b in all_btns if b.get("callback_data")] == ["Like messo", "Salta"]`

In `tests/test_reply_copilot_telegram.py`, `test_status_and_help_describe_reply_copilot_without_exposing_copy`, replace:

```python
    assert "/replies — risposte X da pubblicare manualmente" in (
        telegram.messages[-1][1]
    )
```

with:

```python
    assert "/replies" not in telegram.messages[-1][1]
    assert "/growth — palestre da seguire, like e unfollow suggeriti" in (
        telegram.messages[-1][1]
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_growth_digest_telegram.py tests/test_end_to_end_dry_run.py tests/test_reply_copilot_telegram.py -v`
Expected: FAIL on labels, buttons, help text and the legacy decision test.

- [ ] **Step 3: Implement the header**

In `push_growth_digest`, replace the `lines = [...]` list and the tuple in the `for collection, label, code in (...)` loop with:

```python
        lines = [
            "Growth giornaliero — azioni manuali su X",
            f"Palestre da seguire: {len(collections['accounts'])}",
            f"Like: {len(collections['posts'])}",
            f"Unfollow proposti: {len(collections['reevaluate'])}",
        ]
        rows = []
        for collection, label, code in (
            ("accounts", "Seguire", "a"),
            ("posts", "Like", "p"),
            ("reevaluate", "Unfollow", "r"),
        ):
```

- [ ] **Step 4: Implement the cards**

Add `parse_growth_datetime` to the imports: `from modules.growth_candidate_schema import parse_growth_datetime`. Add a module-level constant near the other constants at the top of `modules/telegram_controller.py`:

```python
_LIKE_SOURCE_LABELS = {
    "followed_gym": "palestra seguita",
    "suggested_gym": "palestra suggerita",
    "operator_pain": "gestore",
}
```

In `_growth_digest_detail`, replace everything from `        if expected_kind == "post":` up to (not including) `        digest = self.db.get_growth_digest(suggestion["observed_on"])` with:

```python
        suggestion_id_text = f"{suggestion_id}:{revision}"
        if expected_kind == "post":
            source = next(
                (
                    _LIKE_SOURCE_LABELS[code]
                    for code in suggestion["reason_codes"]
                    if code in _LIKE_SOURCE_LABELS
                ),
                "discussione pertinente",
            )
            created_at = parse_growth_datetime(payload.get("created_at"))
            age_hours = (
                max(int((self._now() - created_at).total_seconds() // 3600), 0)
                if created_at is not None
                else 0
            )
            text = "\n".join([
                f"Like — {source}",
                f"@{username} · {age_hours} h fa",
                f"Estratto: {self._clean_text(payload.get('excerpt'), 500)}",
                f"Motivo: {reason}",
            ])
            rows = [
                [{
                    "text": "Apri post su X",
                    "url": f"https://x.com/{username}/status/{suggestion['object_id']}",
                }],
                [
                    self._callback_button("Like messo", f"gda:l:{suggestion_id_text}"),
                    self._callback_button("Salta", f"gda:s:{suggestion_id_text}"),
                ],
            ]
        elif expected_kind == "account":
            metrics = payload["public_metrics"]
            text = "\n".join([
                f"Palestra da seguire @{username}",
                f"follower: {metrics['followers_count']}",
                f"following: {metrics['following_count']}",
                f"post: {metrics['tweet_count']}",
                f"Motivo: {reason}",
                "Segui manualmente su X: il bot rileva il follow entro 24 ore.",
            ])
            rows = [
                [
                    {"text": "Apri profilo", "url": f"https://x.com/{username}"},
                    {
                        "text": "Apri ultimo post",
                        "url": (
                            f"https://x.com/{username}/status/"
                            f"{payload['latest_activity_id']}"
                        ),
                    },
                ],
                [self._callback_button(
                    "Non pertinente", f"gda:n:{suggestion_id_text}",
                )],
            ]
        else:
            metrics = payload["public_metrics"]
            followed_since = payload.get("followed_since")
            text_lines = [
                f"Unfollow proposto @{username}",
                f"follower: {metrics['followers_count']}",
                f"following: {metrics['following_count']}",
            ]
            if type(followed_since) is str:
                text_lines.append(f"Seguito dal: {followed_since[:10]}")
            text_lines.extend([
                "Non ti segue e non risulta una palestra.",
                "Se decidi, togli il segui manualmente su X.",
            ])
            text = "\n".join(text_lines)
            rows = [
                [{"text": "Apri profilo", "url": f"https://x.com/{username}"}],
                [
                    self._callback_button(
                        "Unfollow fatto", f"gda:u:{suggestion_id_text}",
                    ),
                    self._callback_button("Tieni", f"gda:k:{suggestion_id_text}"),
                ],
                [self._callback_button(
                    "È una palestra", f"gda:g:{suggestion_id_text}",
                )],
            ]
```

- [ ] **Step 5: Implement the actions**

Replace the whole `_growth_digest_action` method with:

```python
    def _growth_digest_action(self, chat_id: str, parts):
        decisions = {
            "n": "not_relevant",
            "l": "liked_manually",
            "s": "skipped",
            "u": "unfollowed_manually",
            "k": "keep",
            "g": "marked_gym",
        }
        confirmations = {
            "n": "Account segnato come non pertinente per 30 giorni.",
            "l": "Like registrato solo localmente; nessuna azione è stata inviata a X.",
            "s": "Post saltato.",
            "u": "Unfollow registrato solo localmente; il controllo notturno lo verifica.",
            "k": "Account tenuto: non verrà riproposto per 90 giorni.",
            "g": "Segnato come palestra: non verrà mai proposto per l'unfollow.",
        }
        if len(parts) != 4 or parts[1] not in decisions:
            self._send(chat_id, "Azione locale non valida.")
            return "growth_digest_action_invalid"
        suggestion_id = self._positive_id(parts[2])
        revision = self._nonnegative_id(parts[3])
        if suggestion_id is None or revision is None:
            self._send(chat_id, "Azione locale non valida.")
            return "growth_digest_action_invalid"
        outcome = self.db.mark_growth_suggestion_decision(
            suggestion_id,
            revision,
            decisions[parts[1]],
            decided_at=self._now(),
        )
        if outcome not in {"updated", "duplicate"}:
            self._send(chat_id, "Azione non disponibile o già sostituita.")
            return "growth_digest_action_rejected"
        self._send(chat_id, confirmations[parts[1]])
        return decisions[parts[1]]
```

- [ ] **Step 6: Drop legacy decisions in SQLite**

In `mark_growth_suggestion_decision` (`modules/database.py`), replace the `allowed` dict with:

```python
        allowed = {
            "account": {"not_relevant"},
            "post": {"liked_manually", "skipped"},
            "reevaluate": {"unfollowed_manually", "keep", "marked_gym"},
        }
```

Delete the `if decision == "followed_manually":` branch and its `conn.execute(...)`, and turn the next `elif decision == "not_relevant":` into `if decision == "not_relevant":`. The loader keeps accepting legacy decision values, so historical rows still render.

- [ ] **Step 7: Update help text**

In `_help`, replace the lines `"/growth — candidati manuali",` and `"/replies — risposte X da pubblicare manualmente",` with the single line:

```python
            "/growth — palestre da seguire, like e unfollow suggeriti",
```

- [ ] **Step 8: Run the tests**

Run: `venv/bin/python -m pytest tests/test_growth_digest_telegram.py tests/test_end_to_end_dry_run.py tests/test_reply_copilot_telegram.py tests/test_following_tracking.py tests/test_x_write_safety.py -v`
Expected: PASS

- [ ] **Step 9: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: all pass. If a test in `tests/test_telegram_controller.py` or `tests/test_telegram_workflows.py` asserts the old `/help` lines, update that expectation to the new `/growth` line and remove the `/replies` line.

- [ ] **Step 10: Commit**

```bash
git add modules/telegram_controller.py modules/database.py tests/test_growth_digest_telegram.py tests/test_end_to_end_dry_run.py tests/test_reply_copilot_telegram.py
git commit -m "feat: Telegram cards for manual likes, gyms and unfollows

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Detach Reply Copilot from the runtime

**Files:**
- Modify: `main.py` (imports, `__init__` reply wiring ~206–213 and ~413–427, `growth_digest_cycle`, `_register_telegram_commands`)
- Modify: `tests/test_end_to_end_dry_run.py` (delete four Reply Copilot tests, add one)

**Interfaces:**
- Produces: `FlexDropinGrowthAgent.reply_copilot is None` always. `reply_copilot_enabled` is still validated as a bool, and `True` only logs `reply_copilot_retired`. No `/replies` menu entry. `growth_digest_cycle` returns the controller result without calling Reply Copilot.

- [ ] **Step 1: Replace the tests**

In `tests/test_end_to_end_dry_run.py`, delete these four functions entirely:
`test_reply_copilot_wiring_is_disabled_by_default_and_requires_boolean_flag`, `test_reply_copilot_adds_a_command_but_no_scheduler_job`, `test_reply_copilot_failure_does_not_change_growth_digest_result`, `test_reply_copilot_full_manual_flow_adds_no_x_calls_or_usage`. Add in their place:

```python
def test_reply_copilot_is_retired_even_when_flag_is_true(tmp_path):
    dependencies = dependency_bundle(tmp_path, reply_copilot_enabled=True)
    agent = FlexDropinGrowthAgent(dependencies)
    agent._register_telegram_commands()

    assert agent.reply_copilot is None
    assert agent.telegram_controller.reply_copilot is None
    assert "replies" not in {
        item["command"] for item in dependencies["telegram_api"].commands
    }
    assert {job.id for job in agent.register_jobs()} == {
        "source_refresh",
        "queue_replenishment",
        "translation_retry",
        "publication_planning",
        "adaptive_publish",
        "growth_digest",
        "follower_snapshot",
        "performance_metrics",
        "weekly_growth_report",
    }
    with pytest.raises(ValueError, match="reply_copilot_enabled"):
        FlexDropinGrowthAgent(dependency_bundle(
            tmp_path / "invalid", reply_copilot_enabled="true",
        ))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_end_to_end_dry_run.py::test_reply_copilot_is_retired_even_when_flag_is_true -v`
Expected: FAIL, because `agent.reply_copilot` is a `ReplyCopilotService`.

- [ ] **Step 3: Implement**

In `main.py`:
- Remove `ENABLE_REPLY_COPILOT`, `REPLY_COPILOT_DAILY_LIMIT`, `REPLY_COPILOT_MAX_AGE_HOURS`, `REPLY_COPILOT_MAX_REGENERATIONS` from the `from config import (...)` list, and delete `from modules.reply_copilot import ReplyCopilotService`.
- Replace:

```python
        self.reply_copilot_enabled = supplied.get(
            "reply_copilot_enabled", ENABLE_REPLY_COPILOT
        )
        if type(self.reply_copilot_enabled) is not bool:
            raise ValueError("reply_copilot_enabled must be a boolean")
```

with:

```python
        reply_copilot_enabled = supplied.get("reply_copilot_enabled", False)
        if type(reply_copilot_enabled) is not bool:
            raise ValueError("reply_copilot_enabled must be a boolean")
        if reply_copilot_enabled:
            logger.warning("reply_copilot_retired: ENABLE_REPLY_COPILOT is ignored")
```

- Replace the whole `self.reply_copilot = ( resolve("reply_copilot", ...) if self.reply_copilot_enabled else None )` expression with `self.reply_copilot = None`.
- In `growth_digest_cycle`, delete the entire `if self.reply_copilot_enabled and type(digest) is dict:` block, so the `try` body is:

```python
            current = self._now() if now is None else now
            digest = self.growth_digest.build(current)
            return self.telegram_controller.push_growth_digest(
                digest, explicit=False,
            )
```

- In `_register_telegram_commands`, delete the `if self.reply_copilot_enabled: commands.insert(...)` block, and change the growth entry description to `"Palestre, like e unfollow suggeriti"`.
- If `date` is no longer used in `main.py`, change `from datetime import date, datetime, timedelta` to `from datetime import datetime, timedelta`. Check with `grep -n "date\." main.py`.

The `ENABLE_REPLY_COPILOT` env variable stays parsed in `config.py`, so existing `.env` files keep validating.

- [ ] **Step 4: Run the tests**

Run: `venv/bin/python -m pytest tests/test_end_to_end_dry_run.py tests/test_main_startup.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: all pass. `tests/test_reply_copilot*.py` exercise the module directly and keep passing.

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_end_to_end_dry_run.py
git commit -m "refactor: retire Reply Copilot from runtime wiring

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: Weekly following and like statistics

**Files:**
- Modify: `modules/database.py` (`get_weekly_growth_analytics`)
- Modify: `modules/analytics.py` (`build_weekly_report`)
- Modify: `modules/telegram_controller.py` (`format_weekly_report`)
- Modify: `tests/test_growth_analytics.py` (`REPORT_KEYS`, `test_weekly_report_empty_has_exact_stable_schema`)
- Test: `tests/test_following_tracking.py`

**Interfaces:**
- Consumes: `x_following`, `growth_suggestions` decisions.
- Produces: raw key `following` and report key `following_summary`, shaped `{"following_total": int, "gyms_following": int, "gym_follow_back_rate": float, "other_follow_back_rate": float, "unfollows": int, "likes_by_source": Dict[str, int]}`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_growth_analytics.py`, add `"following_summary",` to `REPORT_KEYS`. In `test_weekly_report_empty_has_exact_stable_schema`, add this entry to the expected dict, right before `"attribution_label": "correlation",`:

```python
        "following_summary": {
            "following_total": 0,
            "gyms_following": 0,
            "gym_follow_back_rate": 0.0,
            "other_follow_back_rate": 0.0,
            "unfollows": 0,
            "likes_by_source": {},
        },
```

Append to `tests/test_following_tracking.py`:

```python
def test_weekly_report_summarizes_following_and_likes(tmp_path):
    db = Database(str(tmp_path / "weekly-following.db"))
    gym = profile("11", "gym_a")
    other = profile("12", "runner", OTHER_BIO)
    gone = profile("13", "gone", OTHER_BIO)
    db.sync_following_snapshot(NOW - timedelta(days=3), [gym, other, gone], complete=True)
    capture_followers(db, NOW - timedelta(hours=2), [gym])
    db.sync_following_snapshot(NOW - timedelta(hours=1), [gym, other], complete=True)
    payload, reasons = post_payload()
    suggestion = seed_suggestion(db, "post", "7001", "gym_a", payload, reasons)["posts"][0]
    db.mark_growth_suggestion_decision(
        suggestion["id"], suggestion["revision"], "liked_manually", decided_at=NOW,
    )

    report = PerformanceAnalyzer(SimpleNamespace(), db).build_weekly_report(NOW)

    assert report["following_summary"] == {
        "following_total": 2,
        "gyms_following": 1,
        "gym_follow_back_rate": 1.0,
        "other_follow_back_rate": 0.0,
        "unfollows": 1,
        "likes_by_source": {"followed_gym": 1},
    }
    text = TelegramController.format_weekly_report(report)
    assert "Following" in text
    assert "palestre: 1" in text
    assert "palestra seguita: 1" in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_growth_analytics.py tests/test_following_tracking.py -v`
Expected: FAIL with `KeyError: 'following_summary'` and a schema mismatch.

- [ ] **Step 3: Implement the raw query**

In `get_weekly_growth_analytics`, inside the `with self._conn() as conn:` block, after the `budget_rows = ...` query, add:

```python
            following_rows = conn.execute("""
                SELECT is_gym, gym_override, follows_back,
                       follows_back_checked_at, unfollowed_at
                FROM x_following
            """).fetchall()
            like_rows = conn.execute("""
                SELECT reason_codes_json FROM growth_suggestions
                WHERE kind = 'post' AND decision = 'liked_manually'
                  AND julianday(decision_at) >= julianday(?)
                  AND julianday(decision_at) < julianday(?)
            """, (start_iso, end_iso)).fetchall()
```

Before the final `return {`, add:

```python
        active = [row for row in following_rows if row["unfollowed_at"] is None]
        gyms = [
            row for row in active
            if self._effective_gym(row["is_gym"], row["gym_override"])
        ]
        others = [
            row for row in active
            if not self._effective_gym(row["is_gym"], row["gym_override"])
        ]

        def follow_back_rate(rows):
            checked = [row for row in rows if row["follows_back_checked_at"]]
            if not checked:
                return 0.0
            return round(sum(row["follows_back"] == 1 for row in checked) / len(checked), 4)

        unfollows = 0
        for row in following_rows:
            gone_at = parse_growth_datetime(row["unfollowed_at"])
            if gone_at is not None and start_at <= gone_at < end_at:
                unfollows += 1
        likes_by_source: Dict[str, int] = {}
        for row in like_rows:
            try:
                reasons = json.loads(row["reason_codes_json"])
            except (TypeError, ValueError):
                continue
            source = next(
                (
                    code for code in reasons
                    if code in {"followed_gym", "suggested_gym", "operator_pain"}
                ),
                None,
            ) if type(reasons) is list else None
            if source is not None:
                likes_by_source[source] = likes_by_source.get(source, 0) + 1
        following_summary = {
            "following_total": len(active),
            "gyms_following": len(gyms),
            "gym_follow_back_rate": follow_back_rate(gyms),
            "other_follow_back_rate": follow_back_rate(others),
            "unfollows": unfollows,
            "likes_by_source": dict(sorted(likes_by_source.items())),
        }
```

and add `"following": following_summary,` to the returned dict.

- [ ] **Step 4: Expose it in the report**

In `modules/analytics.py`, `build_weekly_report`, before `return {`, add:

```python
        following = raw.get("following")
        following_summary = {
            "following_total": 0,
            "gyms_following": 0,
            "gym_follow_back_rate": 0.0,
            "other_follow_back_rate": 0.0,
            "unfollows": 0,
            "likes_by_source": {},
        }
        if isinstance(following, dict) and set(following) == set(following_summary):
            following_summary = following
```

and add `"following_summary": following_summary,` to the returned dict, before `"attribution_label": "correlation",`.

- [ ] **Step 5: Render it**

In `modules/telegram_controller.py`, `format_weekly_report`, directly before `return "\n".join(lines)`, add:

```python
        following = report.get("following_summary")
        following = following if isinstance(following, dict) else {}

        def following_int(key):
            value = following.get(key)
            return value if type(value) is int and value >= 0 else 0

        def following_rate(key):
            value = following.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return 0.0
            return max(float(value), 0.0) * 100

        lines += [
            "",
            "Following",
            f"  totali: {following_int('following_total')}"
            f"  |  palestre: {following_int('gyms_following')}",
            f"  follow-back palestre: {following_rate('gym_follow_back_rate'):.0f}%"
            f"  |  altri: {following_rate('other_follow_back_rate'):.0f}%",
            f"  unfollow: {following_int('unfollows')}",
        ]
        likes = following.get("likes_by_source")
        likes = likes if isinstance(likes, dict) else {}
        like_parts = [
            f"  {_LIKE_SOURCE_LABELS[source]}: {count}"
            for source, count in sorted(likes.items())
            if source in _LIKE_SOURCE_LABELS and type(count) is int and count >= 0
        ]
        if like_parts:
            lines.append("Like segnati")
            lines.extend(like_parts)
```

- [ ] **Step 6: Run the tests**

Run: `venv/bin/python -m pytest tests/test_growth_analytics.py tests/test_following_tracking.py -v`
Expected: PASS

- [ ] **Step 7: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add modules/database.py modules/analytics.py modules/telegram_controller.py tests/test_growth_analytics.py tests/test_following_tracking.py
git commit -m "feat: weekly following and like statistics

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 10: Documentation and final verification

**Files:**
- Modify: `README.md`, `SETUP.md`, `.env.example`

- [ ] **Step 1: Update README**

In `README.md`:
- Replace the sentence "Il processo non mette like, non segue/smette di seguire, non invia risposte e non invia DM. Può preparare risposte utili da copiare o aprire manualmente su X, ma non possiede un percorso API per pubblicarle." with "Il processo non mette like, non segue/smette di seguire, non invia risposte e non invia DM: le regole di automazione di X lo vietano. Suggerisce palestre da seguire, post dove mettere like e account non-palestra da non seguire più; ogni azione resta manuale su X."
- In "Scheduler sicuro", replace the growth digest bullet with "- digest growth read-only alle 09:00 `Europe/Rome`: palestre da seguire, like suggeriti e unfollow proposti;". Replace the follower snapshot bullet with "- snapshot follower e sincronizzazione della lista following reale alle 23:15;".
- In "Flusso quotidiano", replace the paragraph starting "Il digest growth arriva alle 09:00" with:

```markdown
Il digest growth arriva alle 09:00 `Europe/Rome` con al massimo 5 palestre da
seguire (solo attività fitness, USA prima), 10 post dove mettere like (4 da
palestre seguite, 3 da palestre suggerite, 3 da gestori che parlano di posti
vuoti, no-show o prenotazioni) e 5 unfollow proposti a settimana. Ogni sera il
bot legge la lista following reale di `@FlexDropin`: un account non-palestra
seguito da almeno 30 giorni che non ricambia viene proposto per l'unfollow; le
palestre non vengono mai proposte. I pulsanti `Like messo`, `Salta`,
`Non pertinente`, `Unfollow fatto`, `Tieni` ed `È una palestra` registrano solo
decisioni locali in SQLite.
```

- Delete the whole "## Reply Copilot manuale" section. Remove `/replies` from the command list sentence. Replace the module bullet for `modules/reply_copilot.py` with "- `modules/reply_copilot.py`: modulo dismesso, non collegato al runtime."

- [ ] **Step 2: Update SETUP and env example**

In `SETUP.md`:
- Set `GROWTH_POST_QUERY_BUDGET=1` and `GROWTH_UNFOLLOW_REVIEW_DAYS=30` in the env block.
- In section 7, replace the checklist items from "digest growth alle 09:00 Rome" through "il ledger X non cambia durante build, apertura, copia..." with:

```markdown
- [ ] digest growth alle 09:00 Rome: al massimo 5 palestre, 10 like, unfollow proposti ≤ 5 a settimana;
- [ ] schede like: `Apri post su X`, `Like messo`, `Salta`; nessuna scrittura X;
- [ ] schede palestra: `Apri profilo`, `Apri ultimo post`, `Non pertinente`;
- [ ] schede unfollow: `Apri profilo`, `Unfollow fatto`, `Tieni`, `È una palestra`; mai palestre;
- [ ] dopo le 23:15 la tabella `x_following` contiene la lista following reale;
```

- Add a subsection at the end of section 6, the one about the Reply Copilot rollback, titled "### Aggiornamento like/following (settembre 2026)":

```markdown
Prima del deploy aggiornare `.env` sul VPS, altrimenti `validate_config()` fallisce:

    GROWTH_POST_QUERY_BUDGET=1
    GROWTH_UNFOLLOW_REVIEW_DAYS=30

`ENABLE_REPLY_COPILOT` viene ignorato. La tabella `x_following` è additiva e si
popola al primo job delle 23:15; gli unfollow proposti compaiono solo dopo 30
giorni di osservazione.
```

Check that `.env.example` already carries the Task 1 values.

- [ ] **Step 3: Full verification**

Run:

```bash
venv/bin/python -m pytest -q
venv/bin/python -m compileall -q main.py config.py modules dashboard
git diff --check
```

Expected: all tests pass, compileall prints nothing, and diff check prints nothing.

- [ ] **Step 4: Commit**

```bash
git add README.md SETUP.md .env.example
git commit -m "docs: describe manual like and following workflow

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```
