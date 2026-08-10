# FlexDropin X Growth Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current autonomous posting and engagement bot with an approval-only, source-grounded content system and a Telegram control plane that helps grow relevant followers without automatic X engagement.

**Architecture:** Keep the existing Python process, APScheduler, SQLite, Tweepy, Groq and Flask fallback dashboard, but separate planning, factual validation, draft state, media matching, publication and growth discovery into focused modules. Telegram long polling becomes the daily control surface; every state-changing callback is persisted and idempotent before execution. X write access is reduced to publishing a specifically approved draft, while all account discovery and suggested growth actions remain read-only.

**Tech Stack:** Python 3.11 or newer, SQLite, APScheduler 3.10, Tweepy 4.14, Groq, Requests, Flask fallback dashboard, pytest 8.x.

## Global Constraints

- The X account is `@FlexDropin`; generated post copy is English (US), while Telegram operational copy may be Italian.
- Prepare at most two candidates per day for 14:00 and 20:00 `Europe/Rome`, sending each draft two hours before its intended slot.
- Every post requires Telegram approval during the initial 30-day controlled period; late approval never publishes immediately.
- A draft approved before its slot may publish within a five-minute scheduler grace window; approval at or after the slot is rejected and requires explicit rescheduling.
- Never automatically like, follow, unfollow, reply to, retweet or direct-message an X user.
- Never publish a below-threshold, fact-check-failed, expired or unapproved draft as a fallback.
- Content mix over a rolling 30-day window: 35% gym strategy, 25% sourced fitness-business insight, 20% shareable fitness content, 10% FlexDropin/product proof and 10% authentic founder journey.
- Product facts expire after 90 days unless re-verified; external links are limited to one planned post per seven days.
- First-person experiences, numbers, product incidents, medical claims, testimonials and named-current-event claims require explicit supporting sources; factual validation fails closed.
- Semantic novelty is checked against published and active draft text from the previous 30 days.
- Media is selected only after the post concept exists, requires at least 80/100 relevance, is marked used only after confirmed publication and is never deleted automatically.
- Growth candidates require a score of at least 75/100, with no more than three X search/network queries and 25 newly evaluated profiles per day; profiles are cached for seven days.
- Telegram accepts operational commands and callbacks only from `TELEGRAM_CHAT_ID`; every update is idempotent.
- `DRY_RUN=true` is the safe default. A timeout with an unknown X publication outcome is never automatically retried.
- The Flask dashboard remains available as a fallback until the Telegram flow is production-verified.
- Tests must use fake X, Telegram, Groq and news clients and must never create real external side effects.

---

## File Structure

### New focused modules

- `modules/content_planner.py`: rolling portfolio deficits, source eligibility, link quota and intended-slot planning.
- `modules/source_ingestion.py`: allowlisted NewsAPI article validation and persisted `verified_news` sources.
- `modules/fact_guard.py`: source expiry checks, structured claim validation and fail-closed factual decisions.
- `modules/draft_pipeline.py`: generation, rewrite, fact guard, score, novelty, persistence and draft transitions.
- `modules/media_matcher.py`: post-first media ranking and reservation at the 80/100 threshold.
- `modules/publisher.py`: pause, approval, expiry and idempotency checks around the sole permitted X write.
- `modules/telegram_api.py`: Telegram HTTP transport, long polling, message/media delivery and file download.
- `modules/telegram_controller.py`: authorization, update claiming, commands, callbacks and conversational input state.
- `modules/growth_discovery.py`: read-only candidate collection, hard filters, relevance scoring and daily digest selection.
- `tests/fakes.py`: deterministic external-service doubles shared by unit and integration tests.

### Existing files changed

- `config.py`: safe defaults, explicit timezone, candidate slots, thresholds, Telegram and discovery budgets.
- `character.json`: remove invented founder/bug cues and encode the approved founder-led brand voice.
- `modules/database.py`: additive schema migration and repositories for sources, drafts, media lifecycle, candidates, followers, Telegram updates and bot state.
- `modules/ai_generator.py`: source-bounded generation, complete rewrite, structured claim analysis, structured scoring and scored media selection.
- `modules/scoring.py`: seven editorial axes, normalized 0–100 result and failure instead of a neutral fallback.
- `modules/media_processor.py`: MIME/size validation, user context and non-destructive lifecycle registration.
- `modules/twitter_client.py`: retain approved post creation and add read-only profile/follower/activity queries; remove engagement write methods.
- `modules/analytics.py`: follower snapshots and weekly growth/content report without automatic portfolio reweighting.
- `modules/notifier.py`: delegate outbound messages to the reusable Telegram transport.
- `modules/news_fetcher.py`: expose complete article provenance needed by source ingestion.
- `modules/content_scheduler.py`: retain only seasonal helpers and compatibility wrappers; category selection moves to `ContentPlanner`.
- `main.py`: dependency injection, safe job registration, Telegram polling, draft creation, approved publication and read-only discovery.
- `dashboard/app.py` and `dashboard/templates/media.html`: reflect the new media lifecycle while preserving fallback upload/edit/delete operations.
- `requirements-dev.txt`: reproducible pytest dependency.
- `README.md` and `SETUP.md`: operational commands, environment variables, dry-run and rollout instructions.

---

### Task 1: Establish the test harness and remove prohibited X writes

**Files:**
- Create: `requirements-dev.txt`
- Create: `tests/__init__.py`
- Create: `tests/test_x_write_safety.py`
- Modify: `config.py:20-125`
- Modify: `character.json:10-105`
- Modify: `modules/twitter_client.py:51-198`
- Modify: `main.py:38-65, 84-116, 274-434`
- Delete: `modules/engagement.py`

**Interfaces:**
- Consumes: existing X authentication and APScheduler process.
- Produces: `DRY_RUN: bool`, `BOT_TIMEZONE: str`, `CONTENT_SLOTS: list[str]`, `DRAFT_LEAD_MINUTES: int`, `APPROVAL_REQUIRED: bool`; `TwitterClient.post_tweet(text, media_path=None, media_type="image")`, whose signature cannot create a reply.

- [ ] **Step 1: Add the failing safety tests and pytest dependency**

```text
# requirements-dev.txt
-r requirements.txt
pytest>=8.0,<9.0
```

```python
# tests/test_x_write_safety.py
import importlib
import inspect

import config
from modules.twitter_client import TwitterClient


def test_twitter_client_exposes_only_approved_post_write():
    prohibited = {
        "like_tweet",
        "follow_user",
        "unfollow_user",
        "reply_to_tweet",
        "retweet",
        "send_dm",
    }
    assert prohibited.isdisjoint(set(dir(TwitterClient)))
    assert hasattr(TwitterClient, "post_tweet")
    assert "reply_to" not in inspect.signature(TwitterClient.post_tweet).parameters


def test_rollout_defaults_are_safe(monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("CONTENT_SLOTS", raising=False)
    reloaded = importlib.reload(config)
    assert reloaded.DRY_RUN is True
    assert reloaded.APPROVAL_REQUIRED is True
    assert reloaded.BOT_TIMEZONE == "Europe/Rome"
    assert reloaded.CONTENT_SLOTS == ["14:00", "20:00"]
    assert reloaded.MAX_LINKS_PER_WEEK == 1


def test_character_contains_no_invented_bug_example():
    character_text = open("character.json", encoding="utf-8").read().lower()
    prohibited = ("absurd bugs", "stripe webhook", "bugs fixed", "rough day")
    assert all(term not in character_text for term in prohibited)
```

- [ ] **Step 2: Install the development test dependency**

Run: `venv/bin/python -m pip install -r requirements-dev.txt`

Expected: pytest 8.x installs successfully in the project virtual environment.

- [ ] **Step 3: Run the safety tests and confirm the current implementation fails**

Run: `venv/bin/python -m pytest tests/test_x_write_safety.py -v`

Expected: FAIL because the client still exposes engagement writes and the safe configuration fields do not exist.

- [ ] **Step 4: Add safe configuration and remove unsafe persona cues**

```python
# config.py — replace autonomous posting/growth defaults with these fields
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Europe/Rome")
CONTENT_SLOTS = [value.strip() for value in os.getenv(
    "CONTENT_SLOTS", "14:00,20:00"
).split(",") if value.strip()]
DRAFT_LEAD_MINUTES = int(os.getenv("DRAFT_LEAD_MINUTES", "120"))
PUBLISH_GRACE_SECONDS = int(os.getenv("PUBLISH_GRACE_SECONDS", "300"))
APPROVAL_REQUIRED = os.getenv("APPROVAL_REQUIRED", "true").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
DRAFT_SCORE_THRESHOLD = int(os.getenv("DRAFT_SCORE_THRESHOLD", "75"))
SEMANTIC_DUPLICATE_THRESHOLD = float(os.getenv("SEMANTIC_DUPLICATE_THRESHOLD", "0.72"))
MAX_LINKS_PER_WEEK = int(os.getenv("MAX_LINKS_PER_WEEK", "1"))
ENABLE_LEAD_DISCOVERY = os.getenv("ENABLE_LEAD_DISCOVERY", "false").lower() == "true"
```

Replace the `character.json` bio/style with source-aware rules and keep no fictional example:

```json
"bio": [
  "Founder-led account for FlexDropin, a drop-in fitness class booking app.",
  "Shares useful, verified ideas for gym and studio operators and practical fitness content."
],
"style": {
  "all": [
    "Always write in English (US).",
    "Use we only for verified company facts and I only for an explicit founder note.",
    "Never invent visits, customers, bugs, numbers, testimonials or personal experiences."
  ],
  "post": [
    "Maximum 280 characters.",
    "Give the reader a useful complete idea before any call to action."
  ],
  "chat": ["Keep operational replies short and concrete."]
},
"postExamples": []
```

- [ ] **Step 5: Remove all automatic engagement entry points**

Delete `TwitterClient.reply_to_tweet`, `like_tweet`, `follow_user` and `unfollow_user`, and remove the `reply_to` parameter/branch from `post_tweet`. Delete `modules/engagement.py`. Remove `EngagementManager` and the old `GrowthManager` from `main.py`, along with `targeted_engagement_cycle`, `growth_cycle`, `unfollow_check_cycle`, their scheduled jobs and weekly automatic `weekly_build_in_public_cycle`. Keep the lead finder because it only produces manual suggestions; do not schedule any path that invokes `TwitterClient.post_tweet()` until Task 7.

- [ ] **Step 6: Verify the safety baseline**

Run: `venv/bin/python -m pytest tests/test_x_write_safety.py -v`

Expected: 3 passed.

Run: `git diff --check`

Expected: no output.

- [ ] **Step 7: Commit the safety baseline**

```bash
git add requirements-dev.txt tests/__init__.py tests/test_x_write_safety.py config.py character.json modules/twitter_client.py main.py
git rm modules/engagement.py
git commit -m "fix: remove automated X engagement"
```

---

### Task 2: Add additive persistence for sources, drafts, Telegram and growth

**Files:**
- Create: `tests/test_database_growth_schema.py`
- Modify: `modules/database.py:42-191, 460-587`

**Interfaces:**
- Consumes: `Database(db_path: str)` and existing tables without data loss.
- Produces additionally: `set_media_reusable(media_id, reusable) -> bool`, used only after an explicit Telegram/dashboard decision.
- Produces: source methods `add_content_source(source_type, text, url=None, metadata=None, trust_state="verified", verified_by=None, verified_at=None) -> int`, `get_content_source(source_id) -> dict | None`, `get_eligible_sources(source_type=None, now=None) -> list[dict]`, `content_source_exists(url) -> bool`; draft methods `create_post_draft(text, category, source_ids, score_data, intended_slot, publication_key) -> int`, `get_post_draft(draft_id) -> dict | None`, `list_post_drafts(statuses=None, limit=50) -> list[dict]`, `get_active_draft_for_slot(intended_slot) -> dict | None`, `get_content_mix_counts(days=30) -> dict[str, int]`, `count_drafts_for_local_date(local_date, timezone_name) -> int`, `get_recent_content_texts(days=30) -> list[str]`, `transition_post_draft(draft_id, expected_statuses, new_status, **changes) -> bool`; media methods `get_available_media(limit=15) -> list[dict]`, `reserve_media(media_id, draft_id) -> bool`, `release_media_for_draft(draft_id) -> None`, `mark_media_used(media_id, tweet_id) -> None`, `archive_media(media_id) -> bool`; growth methods `upsert_growth_candidate(candidate) -> int`, `get_digest_candidates(limit=5) -> list[dict]`, `mark_candidate_decision(candidate_id, decision, reason=None) -> bool`, `save_follower_snapshot(observed_on, profile, relevant, source) -> bool`, `get_known_follower_ids(before_date=None) -> set[str]`, `mark_candidate_followed_back(user_id, observed_at) -> bool`; control methods `claim_telegram_update(update_id, chat_id) -> bool`, `complete_telegram_update(update_id, state, result) -> None`, `set_state(key, value) -> None`, `get_state(key, default=None) -> str | None`, `log_error(context, error_type, safe_message) -> int` and `get_recent_errors(limit=10, unresolved_only=True) -> list[dict]`.

- [ ] **Step 1: Write migration and repository behavior tests**

```python
# tests/test_database_growth_schema.py
from datetime import datetime, timedelta

from modules.database import Database


def test_schema_migration_preserves_existing_posts(tmp_path):
    path = tmp_path / "bot.db"
    db = Database(str(path))
    db.log_posted_tweet("existing", "legacy", tweet_id="123")
    migrated = Database(str(path))
    assert migrated.get_recent_posts(1)[0]["tweet_id"] == "123"


def test_expired_product_fact_is_not_eligible(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    source_id = db.add_content_source(
        source_type="product_fact",
        text="Partner fee is 15%.",
        verified_by="floriano",
        verified_at=(datetime.utcnow() - timedelta(days=91)).isoformat(),
    )
    assert source_id > 0
    assert db.get_eligible_sources(source_type="product_fact") == []


def test_draft_transition_is_compare_and_swap(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    source_id = db.add_content_source("evergreen_idea", "Reduce class no-shows.")
    draft_id = db.create_post_draft(
        text="A useful draft",
        category="gym_strategy",
        source_ids=[source_id],
        score_data={"total": 82},
        intended_slot="2026-08-11T14:00:00+02:00",
        publication_key="draft-20260811-1400",
    )
    assert db.transition_post_draft(draft_id, ["pending_approval"], "approved") is True
    assert db.transition_post_draft(draft_id, ["pending_approval"], "approved") is False


def test_telegram_update_can_be_claimed_once(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    assert db.claim_telegram_update(901, "42") is True
    assert db.claim_telegram_update(901, "42") is False
```

- [ ] **Step 2: Run the database tests and verify missing methods fail**

Run: `venv/bin/python -m pytest tests/test_database_growth_schema.py -v`

Expected: 3 failures with missing `add_content_source`, `create_post_draft` and `claim_telegram_update`; the legacy preservation assertion passes.

- [ ] **Step 3: Add the schema in one idempotent migration**

Add `content_sources`, `post_drafts`, `growth_candidates`, `follower_snapshots`, `telegram_updates` and `bot_state` using `CREATE TABLE IF NOT EXISTS`. Use these status defaults and uniqueness constraints:

```sql
CREATE TABLE IF NOT EXISTS content_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    text TEXT NOT NULL,
    url TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    trust_state TEXT NOT NULL DEFAULT 'verified',
    verified_by TEXT,
    verified_at TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS post_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_key TEXT NOT NULL UNIQUE,
    text TEXT NOT NULL,
    category TEXT NOT NULL,
    source_ids_json TEXT NOT NULL,
    score_json TEXT NOT NULL,
    intended_slot TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_approval',
    media_id INTEGER,
    approved_at TEXT,
    approved_by TEXT,
    published_tweet_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS growth_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    latest_post_json TEXT,
    score INTEGER NOT NULL,
    score_json TEXT NOT NULL,
    discovery_source TEXT NOT NULL,
    decision TEXT NOT NULL DEFAULT 'new',
    rejection_reason TEXT,
    first_seen_at TEXT NOT NULL,
    last_evaluated_at TEXT NOT NULL,
    profile_expires_at TEXT NOT NULL,
    digest_sent_at TEXT,
    manual_followed_at TEXT,
    followed_back_at TEXT,
    suppressed_until TEXT
);

CREATE TABLE IF NOT EXISTS follower_snapshots (
    observed_on TEXT NOT NULL,
    user_id TEXT NOT NULL,
    username TEXT NOT NULL,
    relevant INTEGER NOT NULL,
    source TEXT,
    profile_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (observed_on, user_id)
);

CREATE TABLE IF NOT EXISTS telegram_updates (
    update_id INTEGER PRIMARY KEY,
    chat_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'processing',
    result_json TEXT NOT NULL DEFAULT '{}',
    received_at TEXT NOT NULL,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS error_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    context TEXT NOT NULL,
    error_type TEXT NOT NULL,
    safe_message TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS draft_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intended_slot TEXT NOT NULL,
    category TEXT NOT NULL,
    outcome TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
```

Add columns to `media_library` only when absent: `lifecycle_state TEXT DEFAULT 'available'`, `reusable INTEGER DEFAULT 0`, `user_context TEXT DEFAULT ''`, `reserved_by_draft_id INTEGER`, `mime_type TEXT` and `file_size INTEGER DEFAULT 0`. Backfill `used` rows to `used`, `file_deleted` rows to `deleted`, and all remaining rows to `available`.

Use a `bot_state` marker named `migration:legacy_ideas_to_sources` to copy each existing non-used `ideas` row once into an `evergreen_idea` source. Keep the original `ideas` table readable so the dashboard and audit history are not broken.

- [ ] **Step 4: Implement transactional repository methods**

Use a conditional update for every one-time transition:

```python
def transition_post_draft(self, draft_id, expected_statuses, new_status, **changes):
    allowed = {
        "text", "score_json", "intended_slot", "media_id", "approved_at", "approved_by",
        "published_tweet_id", "error", "updated_at",
    }
    invalid = set(changes) - allowed
    if invalid:
        raise ValueError("Unsupported draft fields: " + ", ".join(sorted(invalid)))
    assignments = ["status = ?", "updated_at = ?"]
    values = [new_status, datetime.now().isoformat()]
    for name, value in changes.items():
        assignments.append(name + " = ?")
        values.append(value)
    placeholders = ", ".join("?" for _ in expected_statuses)
    values.extend([draft_id] + list(expected_statuses))
    with self._conn() as conn:
        cursor = conn.execute(
            "UPDATE post_drafts SET " + ", ".join(assignments)
            + " WHERE id = ? AND status IN (" + placeholders + ")",
            values,
        )
        return cursor.rowcount == 1
```

`claim_telegram_update` must use `INSERT OR IGNORE` and return `cursor.rowcount == 1`. For a `product_fact`, `add_content_source` requires `verified_by`, uses the supplied `verified_at` or the insertion time, and computes `expires_at = verified_at + 90 days`; otherwise it saves the fact as `pending` and the planner cannot select it. `get_eligible_sources` excludes non-verified and expired rows. Add `record_draft_evaluation(intended_slot, category, outcome, details) -> int` and `count_draft_evaluations(outcome, days=7) -> int` for non-sensitive quality audit totals. Add `log_error(context, error_type, safe_message) -> int` and `get_recent_errors(limit=10, unresolved_only=True) -> list[dict]`; never persist tokens or raw request payloads. JSON fields are serialized on write and decoded before returning dictionaries.

- [ ] **Step 5: Verify the schema migration**

Run: `venv/bin/python -m pytest tests/test_database_growth_schema.py -v`

Expected: 4 passed.

Run: `git diff --check`

Expected: no output.

- [ ] **Step 6: Commit the schema migration**

```bash
git add tests/test_database_growth_schema.py modules/database.py
git commit -m "feat: add growth workflow persistence"
```

---

### Task 3: Implement rolling editorial planning and source selection

**Files:**
- Create: `modules/content_planner.py`
- Create: `modules/source_ingestion.py`
- Create: `tests/test_content_planner.py`
- Create: `tests/test_source_ingestion.py`
- Create: `tests/conftest.py`
- Create: `tests/fakes.py`
- Modify: `modules/content_scheduler.py:17-118`
- Modify: `modules/news_fetcher.py:8-82`
- Modify: `config.py`

**Interfaces:**
- Consumes: `Database.get_eligible_sources()`, `Database.get_content_mix_counts(days=30)` and `Database.count_links_last_days(7)`.
- Produces: `ContentPlan(category: str, source_ids: list[int], intended_slot: datetime, include_link: bool)`, `ContentPlanner.plan(intended_slot: datetime) -> ContentPlan | None`, `choose_portfolio_category(counts: dict[str, int]) -> str`, `SourceIngestor.refresh_verified_news(topics, per_topic=1) -> int`.

- [ ] **Step 1: Write failing quota, cap and source-eligibility tests**

```python
# tests/test_content_planner.py
from datetime import datetime
from zoneinfo import ZoneInfo

from modules.content_planner import ContentPlanner, choose_portfolio_category


def test_largest_rolling_deficit_wins():
    counts = {
        "gym_strategy": 1,
        "fitness_business_insight": 5,
        "shareable_fitness": 4,
        "product_proof": 2,
        "founder_journey": 2,
    }
    assert choose_portfolio_category(counts) == "gym_strategy"


def test_planner_skips_when_category_has_no_eligible_source(fake_db):
    fake_db.content_counts = {}
    fake_db.sources = []
    planner = ContentPlanner(fake_db)
    slot = datetime(2026, 8, 11, 14, 0, tzinfo=ZoneInfo("Europe/Rome"))
    assert planner.plan(slot) is None


def test_planner_never_exceeds_two_slots_in_local_day(fake_db):
    fake_db.drafts_today = 2
    planner = ContentPlanner(fake_db)
    slot = datetime(2026, 8, 11, 20, 0, tzinfo=ZoneInfo("Europe/Rome"))
    assert planner.plan(slot) is None
```

```python
# tests/test_source_ingestion.py
from modules.source_ingestion import SourceIngestor


def test_only_complete_allowlisted_article_becomes_verified_source(fake_db, fake_news):
    fake_news.articles = [
        {
            "title": "Operators rethink class capacity",
            "description": "A concrete reported change.",
            "url": "https://industry.example/report",
            "published_at": "2026-08-10T08:00:00Z",
            "source": "Industry Example",
        },
        {
            "title": "Untrusted claim",
            "description": "Should not enter the source pool.",
            "url": "https://spam.example/post",
            "published_at": "2026-08-10T08:00:00Z",
            "source": "Spam Example",
        },
    ]
    ingestor = SourceIngestor(fake_db, fake_news, {"industry.example"})
    assert ingestor.refresh_verified_news(["gym operations"]) == 1
    assert fake_db.sources[0]["source_type"] == "verified_news"
    assert fake_db.sources[0]["url"] == "https://industry.example/report"
```

Define `fake_db` in `tests/conftest.py` with mutable `content_counts`, `sources`, `drafts_today` and the exact methods consumed by `ContentPlanner`.

- [ ] **Step 2: Run planner tests and confirm import failure**

Run: `venv/bin/python -m pytest tests/test_content_planner.py -v`

Expected: collection error because `modules.content_planner` does not exist.

- [ ] **Step 3: Implement deterministic portfolio-deficit selection**

```python
# modules/content_planner.py
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional


PORTFOLIO = {
    "gym_strategy": 0.35,
    "fitness_business_insight": 0.25,
    "shareable_fitness": 0.20,
    "product_proof": 0.10,
    "founder_journey": 0.10,
}

SOURCE_TYPES = {
    "gym_strategy": {"evergreen_idea", "verified_news", "founder_note"},
    "fitness_business_insight": {"verified_news"},
    "shareable_fitness": {"evergreen_idea", "verified_news"},
    "product_proof": {"product_fact"},
    "founder_journey": {"founder_note"},
}


@dataclass(frozen=True)
class ContentPlan:
    category: str
    source_ids: List[int]
    intended_slot: datetime
    include_link: bool


def choose_portfolio_category(counts):
    next_total = sum(counts.values()) + 1
    return max(
        PORTFOLIO,
        key=lambda category: PORTFOLIO[category] * next_total - counts.get(category, 0),
    )
```

`ContentPlanner.plan()` must exclude categories without eligible sources, cap drafts per local date at two, prefer the greatest remaining deficit, and set `include_link` only for `product_proof` when the seven-day count is below one.

Implement `SourceIngestor` so an article becomes `verified_news` only when title, factual summary, HTTPS URL, publication date and source name are present and the hostname equals or is a subdomain of one entry in `NEWS_TRUSTED_DOMAINS`. Store the exact URL and publication metadata, skip duplicate URLs using `Database.content_source_exists(url)`, and return the inserted count. An empty allowlist disables automatic news ingestion without weakening the factual gate; Floriano can still submit and approve a sourced news item through Telegram.

- [ ] **Step 4: Replace the legacy weekly category map with compatibility exports**

Keep `get_seasonal_context()` and `get_active_events()` for source enrichment. Remove `WEEKLY_SCHEDULE`, human-mode selection and performance-weighted random choice. If an old caller still imports `PROMO_CATEGORIES`, export `PROMO_CATEGORIES = {"product_proof"}` until Task 12 removes the compatibility import.

- [ ] **Step 5: Verify planning and source ingestion**

Run: `venv/bin/python -m pytest tests/test_content_planner.py tests/test_source_ingestion.py tests/test_database_growth_schema.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit planning and source ingestion**

```bash
git add modules/content_planner.py modules/source_ingestion.py modules/content_scheduler.py modules/news_fetcher.py config.py tests/conftest.py tests/fakes.py tests/test_content_planner.py tests/test_source_ingestion.py
git commit -m "feat: plan source-backed content mix"
```

---

### Task 4: Build fail-closed factual checks, editorial scoring and complete rewriting

**Files:**
- Create: `modules/fact_guard.py`
- Create: `tests/test_fact_guard.py`
- Create: `tests/test_editorial_scoring.py`
- Modify: `modules/ai_generator.py:65-193, 242-272, 396-442`
- Modify: `modules/scoring.py:1-69`

**Interfaces:**
- Consumes: a source list containing `id`, `source_type`, `text`, `url`, `metadata`, `verified_at` and `expires_at`.
- Produces: `AIGenerator.generate_grounded_tweet(category, sources, include_link) -> dict | None`, `AIGenerator.rewrite_to_limit(text, sources, limit=280) -> str | None`, `AIGenerator.analyze_claims(text, sources) -> dict | None`, `FactGuard.check(text, sources) -> FactCheckResult`, `TweetScorer.score_draft(text) -> dict | None`, `semantic_similarity(left, right) -> float`.

- [ ] **Step 1: Write factual-gate, score-failure and rewrite tests**

```python
# tests/test_fact_guard.py
from modules.fact_guard import FactGuard


class ClaimAnalyzer:
    def __init__(self, claims):
        self.claims = claims

    def analyze_claims(self, text, sources):
        return {"claims": self.claims}


def test_first_person_claim_requires_founder_note():
    guard = FactGuard(ClaimAnalyzer([{"type": "first_person", "supported_by": []}]))
    result = guard.check("I visited a studio today.", [])
    assert result.approved is False
    assert "first_person" in result.reasons[0]


def test_supported_product_number_passes():
    source = {"id": 7, "source_type": "product_fact", "trust_state": "verified"}
    analyzer = ClaimAnalyzer([{"type": "number", "supported_by": [7]}])
    assert FactGuard(analyzer).check("The verified fee is 15%.", [source]).approved is True


def test_analyzer_failure_blocks_publication():
    assert FactGuard(ClaimAnalyzer(None)).check("Safe-looking copy", []).approved is False
```

```python
# tests/test_editorial_scoring.py
from modules.scoring import semantic_similarity


def test_semantic_similarity_detects_reworded_duplicate():
    left = "Three ways gym owners can reduce empty class spots"
    right = "Gym owners: 3 ways to reduce empty spots in classes"
    assert semantic_similarity(left, right) >= 0.72


def test_rewrite_is_used_instead_of_slicing(fake_ai):
    long_text = "A complete sentence. " * 20
    rewritten = fake_ai.rewrite_to_limit(long_text, [], limit=280)
    assert rewritten == "One complete rewritten post."
    assert all(not rewritten.endswith(marker) for marker in ("…", "." * 3))
```

- [ ] **Step 2: Run the tests and confirm missing modules/functions fail**

Run: `venv/bin/python -m pytest tests/test_fact_guard.py tests/test_editorial_scoring.py -v`

Expected: collection failures for `FactGuard` and `semantic_similarity`.

- [ ] **Step 3: Implement structured, fail-closed factual checking**

```python
# modules/fact_guard.py
from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class FactCheckResult:
    approved: bool
    reasons: List[str]


REQUIRED_SOURCE_TYPES = {
    "first_person": {"founder_note"},
    "number": {"founder_note", "product_fact", "verified_news"},
    "product_claim": {"product_fact"},
    "incident": {"founder_note"},
    "medical": {"verified_news"},
    "testimonial": {"founder_note"},
    "named_entity": {"founder_note", "product_fact", "verified_news"},
    "named_current_event": {"verified_news"},
}


class FactGuard:
    def __init__(self, analyzer):
        self.analyzer = analyzer

    def check(self, text, sources):
        analysis = self.analyzer.analyze_claims(text, sources)
        if not analysis or not isinstance(analysis.get("claims"), list):
            return FactCheckResult(False, ["claim_analysis_unavailable"])
        by_id = {source["id"]: source for source in sources}
        reasons = []
        for claim in analysis["claims"]:
            claim_type = claim.get("type", "unknown")
            required = REQUIRED_SOURCE_TYPES.get(claim_type)
            if required is None:
                reasons.append("unsupported_claim_type:" + claim_type)
                continue
            supporting = [by_id.get(value) for value in claim.get("supported_by", [])]
            valid = [
                source for source in supporting
                if source
                and source.get("trust_state") == "verified"
                and source.get("source_type") in required
                and not source_is_expired(source)
                and (
                    source.get("source_type") != "founder_note"
                    or source.get("metadata", {}).get("publishable") is True
                )
            ]
            if not valid:
                reasons.append("unsupported_claim:" + claim_type)
        return FactCheckResult(not reasons, reasons)


def source_is_expired(source, now=None):
    from datetime import datetime, timezone

    raw = source.get("expires_at")
    if not raw:
        return False
    try:
        expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    moment = now or datetime.now(timezone.utc)
    return expires <= moment.astimezone(timezone.utc)
```

Use aware ISO timestamps throughout persistence. In `AIGenerator.analyze_claims`, require JSON with `claims: [{type, text, supported_by}]`, list every factual claim and permit an empty list only for genuinely claim-free copy. Classify named companies/products as `named_entity` and breaking events as `named_current_event`. Explicitly classify payment/privacy/security/customer-impacting incidents as `incident`; `FactGuard` must additionally block those incident subtypes unless the supporting founder note metadata contains `disclosure_approved: true`.

- [ ] **Step 4: Replace truncation and neutral scoring fallbacks**

`generate_grounded_tweet` receives the full source bundle and instructs the model to use no fact outside it. If the returned text is longer than 280 characters, call `rewrite_to_limit`; reject it if the rewrite is missing, still too long or ends mid-sentence. Remove the obsolete autonomous `generate_tweet`, `generate_human_mode_post` and `generate_build_in_public_post` entry points so they cannot bypass sources. Replace `SCORE_AXES` with `hook`, `usefulness`, `specificity`, `originality`, `audience_relevance`, `follow_worthiness` and `semantic_novelty`; normalize the sum from 0–70 to 0–100. On parse/API failure, `score_draft` returns `None`, never a neutral score.

Implement `semantic_similarity` as cosine similarity over normalized token-frequency counters: lowercase alphanumeric tokens, normalize number words such as `three` to `3`, strip simple English plural suffixes and remove connective stop words such as `a`, `the`, `to`, `in` and `can`. Exact normalized equality returns `1.0`. This keeps the threshold interpretable while detecting reordered near-duplicates such as the test case.

- [ ] **Step 5: Verify factual gates and editorial scoring**

Run: `venv/bin/python -m pytest tests/test_fact_guard.py tests/test_editorial_scoring.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit factual gates and editorial scoring**

```bash
git add modules/fact_guard.py modules/ai_generator.py modules/scoring.py tests/test_fact_guard.py tests/test_editorial_scoring.py tests/conftest.py
git commit -m "feat: gate drafts on verified facts"
```

---

### Task 5: Assemble the draft pipeline with no low-quality fallback

**Files:**
- Create: `modules/draft_pipeline.py`
- Create: `tests/test_draft_pipeline.py`

**Interfaces:**
- Consumes: `ContentPlanner.plan()`, `AIGenerator.generate_grounded_tweet()`, `FactGuard.check()`, `TweetScorer.score_draft()`, `Database.create_post_draft()` and recent draft/post text.
- Produces: `DraftPipeline.create_for_slot(intended_slot) -> dict | None`, `DraftPipeline.regenerate(draft_id) -> dict | None`, `DraftPipeline.approve(draft_id, approved_by) -> bool`, `DraftPipeline.discard(draft_id, reason) -> bool`, `DraftPipeline.postpone(draft_id, new_slot) -> bool`.

- [ ] **Step 1: Write end-to-end draft-state tests with fakes**

```python
# tests/test_draft_pipeline.py
def test_low_score_skips_slot(draft_pipeline, fake_scorer, fake_db):
    fake_scorer.result = {"total": 74}
    assert draft_pipeline.create_for_slot(fake_db.next_slot) is None
    assert fake_db.created_drafts == []
    assert fake_db.evaluations[-1]["outcome"] == "rejected_score"


def test_fact_failure_skips_slot(draft_pipeline, fake_guard, fake_db):
    fake_guard.approved = False
    assert draft_pipeline.create_for_slot(fake_db.next_slot) is None
    assert fake_db.created_drafts == []
    assert fake_db.evaluations[-1]["outcome"] == "rejected_fact"


def test_duplicate_skips_slot(draft_pipeline, fake_db):
    fake_db.recent_texts = ["Gym owners can reduce empty class spots with drop-ins"]
    draft_pipeline.generator.text = "Gym owners: reduce empty spots in classes with drop-ins"
    assert draft_pipeline.create_for_slot(fake_db.next_slot) is None


def test_good_draft_waits_for_approval(draft_pipeline, fake_db):
    draft = draft_pipeline.create_for_slot(fake_db.next_slot)
    assert draft["status"] == "pending_approval"
    assert draft["source_ids"]
```

- [ ] **Step 2: Run the draft tests and confirm import failure**

Run: `venv/bin/python -m pytest tests/test_draft_pipeline.py -v`

Expected: collection error because `modules.draft_pipeline` does not exist.

- [ ] **Step 3: Implement the ordered gate pipeline**

```python
# modules/draft_pipeline.py
from uuid import uuid4

from modules.scoring import semantic_similarity


class DraftPipeline:
    def __init__(self, db, planner, generator, fact_guard, scorer,
                 score_threshold=75, duplicate_threshold=0.72):
        self.db = db
        self.planner = planner
        self.generator = generator
        self.fact_guard = fact_guard
        self.scorer = scorer
        self.score_threshold = score_threshold
        self.duplicate_threshold = duplicate_threshold

    def create_for_slot(self, intended_slot):
        existing = self.db.get_active_draft_for_slot(intended_slot.isoformat())
        if existing:
            return existing
        plan = self.planner.plan(intended_slot)
        if plan is None:
            return None
        sources = [self.db.get_content_source(value) for value in plan.source_ids]
        sources = [source for source in sources if source]
        candidate = self.generator.generate_grounded_tweet(
            plan.category, sources, plan.include_link,
        )
        if not candidate:
            return None
        text = candidate["text"].strip()
        if len(text) > 280:
            text = self.generator.rewrite_to_limit(text, sources, 280)
        if not text or len(text) > 280:
            return None
        fact_result = self.fact_guard.check(text, sources)
        if not fact_result.approved:
            self.db.record_draft_evaluation(
                plan.intended_slot.isoformat(), plan.category,
                "rejected_fact", {"reasons": fact_result.reasons},
            )
            return None
        score = self.scorer.score_draft(text)
        if not score or score["total"] < self.score_threshold:
            self.db.record_draft_evaluation(
                plan.intended_slot.isoformat(), plan.category,
                "rejected_score", {"score": score},
            )
            return None
        for previous in self.db.get_recent_content_texts(days=30):
            if semantic_similarity(text, previous) >= self.duplicate_threshold:
                self.db.record_draft_evaluation(
                    plan.intended_slot.isoformat(), plan.category,
                    "rejected_duplicate", {},
                )
                return None
        draft_id = self.db.create_post_draft(
            text=text,
            category=plan.category,
            source_ids=plan.source_ids,
            score_data=score,
            intended_slot=plan.intended_slot.isoformat(),
            publication_key="draft:" + uuid4().hex,
        )
        self.db.record_draft_evaluation(
            plan.intended_slot.isoformat(), plan.category,
            "pending_approval", {"draft_id": draft_id},
        )
        return self.db.get_post_draft(draft_id)
```

- [ ] **Step 4: Implement single-use state transitions**

Record `no_eligible_source`, `generation_failed` and `rewrite_failed` evaluation outcomes on their corresponding early returns as well. Store only reason codes, source IDs and scores in evaluation details, never raw model reasoning.

`approve` accepts only `pending_approval`, writes `approved_at` and `approved_by`, and refuses approval at or after the intended slot. `postpone` accepts `pending_approval`, `approved` or `expired`, sets the explicit new future slot, clears approval and returns status to `pending_approval`. `discard` releases any reserved media and transitions only a non-published draft. `regenerate` creates a new draft with a new `draft:{uuid}` publication key for the same category/sources/slot and marks the prior draft `superseded`; it never overwrites audit history. `get_active_draft_for_slot` prevents scheduler replay from creating a second live draft for one slot.

- [ ] **Step 5: Verify the draft pipeline**

Run: `venv/bin/python -m pytest tests/test_draft_pipeline.py tests/test_fact_guard.py tests/test_content_planner.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit the draft pipeline**

```bash
git add modules/draft_pipeline.py tests/test_draft_pipeline.py tests/conftest.py
git commit -m "feat: create approval-only draft pipeline"
```

---

### Task 6: Implement deferred Telegram media lifecycle and matching

**Files:**
- Create: `modules/media_matcher.py`
- Create: `tests/test_media_lifecycle.py`
- Modify: `modules/media_processor.py:20-99`
- Modify: `modules/ai_generator.py:335-442`
- Modify: `dashboard/app.py:138-197`
- Modify: `dashboard/templates/media.html`

**Interfaces:**
- Consumes: Telegram file metadata, `MediaProcessor.process_new_file(filepath, filename, mime_type, file_size, user_context)`, available media rows and an existing draft concept.
- Produces: `validate_media_upload(filename, mime_type, file_size) -> tuple[bool, str]`, `MediaMatcher.attach_best(draft_id) -> dict | None`, explicit media states `available`, `reserved`, `used`, `archived`, `deleted`.

- [ ] **Step 1: Write validation, lifecycle and threshold tests**

```python
# tests/test_media_lifecycle.py
from modules.media_processor import validate_media_upload


def test_media_validation_rejects_mime_extension_mismatch():
    valid, reason = validate_media_upload("photo.jpg", "video/mp4", 1024)
    assert valid is False
    assert reason == "mime_extension_mismatch"


def test_upload_only_adds_available_media(media_processor, fake_db):
    record = media_processor.process_new_file(
        fake_db.image_path, "gym.jpg", "image/jpeg", 2048, "Real studio floor",
    )
    assert record["lifecycle_state"] == "available"
    assert fake_db.created_drafts == []


def test_match_below_80_keeps_draft_text_only(media_matcher, fake_ai):
    fake_ai.media_choice = {"media_id": 4, "relevance": 79}
    assert media_matcher.attach_best(11) is None


def test_failed_publication_releases_reservation(fake_db):
    fake_db.reserve_media(4, 11)
    fake_db.release_media_for_draft(11)
    assert fake_db.media[4]["lifecycle_state"] == "available"
```

- [ ] **Step 2: Run tests and confirm failures**

Run: `venv/bin/python -m pytest tests/test_media_lifecycle.py -v`

Expected: import/signature failures because validation, matcher and lifecycle fields are absent.

- [ ] **Step 3: Validate before storing and preserve user context**

Accept JPEG, PNG, WebP, MP4, MOV and M4V only when extension and MIME agree. Default limits are 10 MiB for images and 50 MiB for videos, exposed as `TELEGRAM_MAX_IMAGE_BYTES` and `TELEGRAM_MAX_VIDEO_BYTES`; expose `MEDIA_MATCH_THRESHOLD=80` in the same configuration section. Sanitize the filename before opening a destination path. `MediaProcessor` stores `user_context` alongside the AI description/tags and never creates a draft. It also writes a `media_context` content source whose metadata contains `media_id`; this source is not eligible for initial topic planning. When a media item is later attached, its `media_context` source ID is appended to the existing draft source IDs so the final post remains fully traceable without allowing the upload to drive immediate generation.

- [ ] **Step 4: Match and reserve only after the concept exists**

```python
# modules/media_matcher.py
class MediaMatcher:
    def __init__(self, db, generator, threshold=80):
        self.db = db
        self.generator = generator
        self.threshold = threshold

    def attach_best(self, draft_id):
        draft = self.db.get_post_draft(draft_id)
        if not draft:
            return None
        candidates = self.db.get_available_media(limit=15)
        choice = self.generator.select_best_media(
            draft["category"], draft["text"], candidates,
        )
        if not choice or choice.get("relevance", 0) < self.threshold:
            return None
        media_id = int(choice["media_id"])
        if not self.db.reserve_media(media_id, draft_id):
            return None
        return self.db.get_media_by_id(media_id)
```

Change the AI response schema to `{"media_id": 4, "relevance": 86, "reason": "Shows the class format discussed"}`. The dashboard lists lifecycle state and reusable status, archives rather than physically deleting by default, and offers explicit permanent deletion as the only destructive action.

- [ ] **Step 5: Verify media validation and lifecycle**

Run: `venv/bin/python -m pytest tests/test_media_lifecycle.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit the media lifecycle**

```bash
git add modules/media_matcher.py modules/media_processor.py modules/ai_generator.py modules/database.py dashboard/app.py dashboard/templates/media.html tests/test_media_lifecycle.py tests/conftest.py config.py
git commit -m "feat: defer and match Telegram media"
```

---

### Task 7: Publish only approved, due and idempotently claimed drafts

**Files:**
- Create: `modules/publisher.py`
- Create: `tests/test_publisher.py`
- Modify: `modules/twitter_client.py:33-87`

**Interfaces:**
- Consumes: `Database.get_post_draft()`, `Database.transition_post_draft()`, `Database.get_state("paused")`, `TwitterClient.post_tweet()` and optional reserved media.
- Produces: `Publisher.publish(draft_id, now) -> PublishResult`, states `published`, `expired`, `publication_failed`, `publication_unknown`, and no automatic retry after an unknown outcome.

- [ ] **Step 1: Write approval, pause, expiry, dry-run and idempotency tests**

```python
# tests/test_publisher.py
def test_unapproved_draft_never_calls_x(publisher, fake_x, fake_db):
    fake_db.draft["status"] = "pending_approval"
    result = publisher.publish(fake_db.draft["id"], fake_db.slot)
    assert result.status == "not_publishable"
    assert fake_x.posts == []


def test_pause_is_checked_immediately_before_write(publisher, fake_x, fake_db):
    fake_db.paused = True
    result = publisher.publish(fake_db.draft["id"], fake_db.slot)
    assert result.status == "paused"
    assert fake_x.posts == []


def test_second_publish_is_idempotent(publisher, fake_x, fake_db):
    first = publisher.publish(fake_db.draft["id"], fake_db.slot)
    second = publisher.publish(fake_db.draft["id"], fake_db.slot)
    assert first.status == "published"
    assert second.status == "already_published"
    assert len(fake_x.posts) == 1


def test_timeout_becomes_unknown_without_retry(publisher, fake_x, fake_db):
    fake_x.raise_timeout = True
    result = publisher.publish(fake_db.draft["id"], fake_db.slot)
    assert result.status == "publication_unknown"
    assert fake_db.draft["status"] == "publication_unknown"
```

- [ ] **Step 2: Run publisher tests and confirm import failure**

Run: `venv/bin/python -m pytest tests/test_publisher.py -v`

Expected: collection error because `modules.publisher` does not exist.

- [ ] **Step 3: Implement the publication claim and final pause check**

```python
# modules/publisher.py
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class PublishResult:
    status: str
    tweet_id: str = ""


class Publisher:
    def __init__(self, db, x_client, dry_run=True, clock=None, grace_seconds=300,
                 timezone_name="Europe/Rome"):
        self.db = db
        self.x_client = x_client
        self.dry_run = dry_run
        self.clock = clock or (lambda: datetime.now(ZoneInfo(timezone_name)))
        self.grace_seconds = grace_seconds

    def publish(self, draft_id, now=None):
        draft = self.db.get_post_draft(draft_id)
        if not draft:
            return PublishResult("not_found")
        if draft.get("published_tweet_id"):
            return PublishResult("already_published", draft["published_tweet_id"])
        if draft["status"] != "approved":
            return PublishResult("not_publishable")
        current = now or self.clock()
        slot = datetime.fromisoformat(draft["intended_slot"])
        if current > slot + timedelta(seconds=self.grace_seconds):
            self.db.transition_post_draft(draft_id, ["approved"], "expired")
            return PublishResult("expired")
        if self.db.get_state("paused", "false") == "true":
            return PublishResult("paused")
        if self.dry_run:
            return PublishResult("dry_run")
        if not self.db.transition_post_draft(draft_id, ["approved"], "publishing"):
            return PublishResult("already_claimed")
        return self._write_claimed_draft(draft)
```

- [ ] **Step 4: Distinguish definite failure from unknown outcome**

Make `TwitterClient.post_tweet()` raise `XPublicationUnknown` for timeout/connection failures and `XPublicationRejected` for definite API rejection. `Publisher` sets `publication_unknown` for the former and never retries; for the latter it sets `publication_failed` and releases reserved media. On success, record the tweet, transition the media to `used`, preserve the original file and set the draft to `published`. Do not silently post text-only when an approved media upload fails.

- [ ] **Step 5: Verify safe publication behavior**

Run: `venv/bin/python -m pytest tests/test_publisher.py tests/test_x_write_safety.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit safe publication**

```bash
git add modules/publisher.py modules/twitter_client.py modules/database.py tests/test_publisher.py tests/conftest.py
git commit -m "feat: publish approved drafts idempotently"
```

---

### Task 8: Add Telegram transport, authorization and update idempotency

**Files:**
- Create: `modules/telegram_api.py`
- Create: `modules/telegram_controller.py`
- Create: `tests/test_telegram_controller.py`
- Modify: `modules/notifier.py:30-158`

**Interfaces:**
- Consumes: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `Database.claim_telegram_update()`, `Database.complete_telegram_update()` and controller dependencies.
- Produces: `TelegramApi.get_updates(offset, timeout)`, `send_message`, `send_media`, `get_file`, `download_file`, `answer_callback`; `TelegramController.process_update(update) -> str`; `TelegramController.run_forever(stop_event)`.

- [ ] **Step 1: Write unauthorized-chat and replay tests**

```python
# tests/test_telegram_controller.py
def test_unknown_chat_cannot_read_or_mutate(controller, fake_telegram, fake_db):
    update = {"update_id": 10, "message": {"chat": {"id": 999}, "text": "/status"}}
    assert controller.process_update(update) == "unauthorized"
    assert fake_db.telegram_updates[10]["state"] == "unauthorized"
    assert fake_db.operational_mutations == []
    assert fake_telegram.messages == []


def test_replayed_update_is_ignored(controller, fake_telegram):
    update = {"update_id": 11, "message": {"chat": {"id": 42}, "text": "/pause"}}
    assert controller.process_update(update) == "processed"
    assert controller.process_update(update) == "duplicate"
    assert len(fake_telegram.messages) == 1


def test_callback_is_answered_once(controller, fake_telegram):
    update = {
        "update_id": 12,
        "callback_query": {
            "id": "callback-12",
            "from": {"id": 42},
            "message": {"chat": {"id": 42}},
            "data": "draft:approve:7",
        },
    }
    controller.process_update(update)
    assert fake_telegram.answered_callbacks == ["callback-12"]
```

- [ ] **Step 2: Run tests and confirm import failure**

Run: `venv/bin/python -m pytest tests/test_telegram_controller.py -v`

Expected: collection error because the Telegram transport/controller modules do not exist.

- [ ] **Step 3: Implement a bounded Requests transport**

`TelegramApi` uses `https://api.telegram.org/bot{token}/{method}`, a 10-second request timeout for sends/download metadata and `TELEGRAM_POLL_TIMEOUT=25` seconds for long polling. `get_updates` passes `allowed_updates=["message", "callback_query"]`; every HTTP/JSON error raises `TelegramApiError` with sanitized context. Download destinations are explicit paths under `MEDIA_LIBRARY_DIR`.

- [ ] **Step 4: Claim updates before dispatch and authorize first**

```python
# modules/telegram_controller.py
class TelegramController:
    def process_update(self, update):
        update_id = int(update["update_id"])
        chat_id = self._chat_id(update)
        if not self.db.claim_telegram_update(update_id, str(chat_id)):
            return "duplicate"
        if str(chat_id) != self.authorized_chat_id:
            self.db.complete_telegram_update(update_id, "unauthorized", {})
            return "unauthorized"
        try:
            result = self._dispatch(update)
            self.db.complete_telegram_update(update_id, "processed", {"result": result})
            return "processed"
        except Exception as exc:
            self.db.complete_telegram_update(update_id, "failed", {"error": type(exc).__name__})
            self.notifier.notify_error("telegram_update", exc)
            return "failed"
```

`run_forever` advances the offset only after receiving an update batch, catches transport errors with bounded exponential delays of 1, 2, 4, 8 and 30 seconds, and exits when `stop_event.is_set()` is true. Define `sanitize_error` to redact configured bot/API tokens, authorization headers and URL query strings before persistence or notification. Refactor `TelegramNotifier` to call `TelegramApi.send_message()`, accept the database, persist each sanitized error once in `notify_error`, and remove obsolete automated-engagement summaries.

- [ ] **Step 5: Verify Telegram transport safety**

Run: `venv/bin/python -m pytest tests/test_telegram_controller.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit Telegram transport safety**

```bash
git add modules/telegram_api.py modules/telegram_controller.py modules/notifier.py modules/database.py tests/test_telegram_controller.py tests/conftest.py
git commit -m "feat: add secure Telegram control transport"
```

---

### Task 9: Implement Telegram commands, draft cards and deferred uploads

**Files:**
- Create: `tests/test_telegram_workflows.py`
- Modify: `modules/telegram_controller.py`
- Modify: `modules/telegram_api.py`

**Interfaces:**
- Consumes: draft pipeline, media processor/matcher, publisher, analytics, scheduler status and database state.
- Produces: `/status`, `/posts`, `/growth`, `/stats`, `/ideas`, `/pause`, `/resume`, `/errors`, `/help`; callbacks `draft:*`, `growth:*`, `input:*`; photo/video/document ingestion without immediate drafting.

- [ ] **Step 1: Write command, approval, late-approval and upload tests**

```python
# tests/test_telegram_workflows.py
def test_pause_and_resume_are_persistent(controller, fake_db):
    controller.process_update(message_update(20, "/pause"))
    assert fake_db.get_state("paused") == "true"
    controller.process_update(message_update(21, "/resume"))
    assert fake_db.get_state("paused") == "false"


def test_late_approval_requires_reschedule(controller, fake_db, fake_pipeline):
    fake_db.draft["intended_slot"] = "2026-08-10T14:00:00+02:00"
    result = controller.process_update(callback_update(22, "draft:approve:7"))
    assert result == "processed"
    assert fake_db.draft["status"] == "expired"
    assert fake_pipeline.publish_calls == []


def test_media_upload_only_enters_library(controller, fake_media_processor, fake_pipeline):
    controller.process_update(photo_update(23, caption="Real Pilates studio in Rome"))
    assert fake_media_processor.uploads[0]["user_context"] == "Real Pilates studio in Rome"
    assert fake_pipeline.created == []


def test_followed_button_records_manual_action_only(controller, fake_x, fake_db):
    controller.process_update(callback_update(24, "growth:followed:9"))
    assert fake_db.candidates[9]["decision"] == "followed_manually"
    assert fake_x.writes == []
```

- [ ] **Step 2: Run workflow tests and confirm dispatch failures**

Run: `venv/bin/python -m pytest tests/test_telegram_workflows.py -v`

Expected: failures because command and callback handlers are not implemented.

- [ ] **Step 3: Implement commands with concise Italian responses**

Map commands exactly:

```python
self.command_handlers = {
    "/status": self._status,
    "/posts": self._posts,
    "/growth": self._growth,
    "/stats": self._stats,
    "/ideas": self._ideas,
    "/pause": self._pause,
    "/resume": self._resume,
    "/errors": self._errors,
    "/help": self._help,
}
```

`/status` shows dry-run, pause and next scheduled jobs. `/posts` lists pending/approved/scheduled and the five latest published drafts. `/ideas` lists active source counts and prompts plain text classification with `Founder note`, `Product fact`, `Evergreen idea` and `Verified news` buttons. A manually submitted news source asks for its HTTPS URL, publication date and source name before saving it as verified. `/errors` reads the ten latest sanitized `error_events` rows rather than scraping logs. Store the pending plain-text or edit/reschedule interaction in `bot_state` under `telegram_session:{chat_id}` so a process restart does not lose it.

- [ ] **Step 4: Implement draft cards and media ingestion**

Draft callbacks use `draft:approve:{id}`, `draft:regen:{id}`, `draft:edit:{id}`, `draft:media:{id}`, `draft:textonly:{id}`, `draft:postpone:{id}` and `draft:discard:{id}`. The card contains category, intended slot, source labels, score breakdown, complete post text and selected media. Edited copy must pass the same length rewrite, factual gate, score threshold and 30-day novelty checks before replacing the pending draft text. Approval only changes draft state; the scheduled publisher performs the X write at the slot.

Growth cards use a direct URL button for `Open on X` and callbacks `growth:save:{id}`, `growth:followed:{id}` and `growth:discard:{id}`. Discard opens a short reason selector before setting a 30-day suppression. None of these handlers receives or imports an X write-capable method.

For Telegram media, choose the largest photo or the video/document file ID, validate declared size and MIME before download, save a collision-safe name, then call `MediaProcessor.process_new_file`. Reply with library ID, state, description, tags and user context. Do not call `DraftPipeline.create_for_slot` from any upload handler.

- [ ] **Step 5: Verify Telegram workflows**

Run: `venv/bin/python -m pytest tests/test_telegram_controller.py tests/test_telegram_workflows.py tests/test_media_lifecycle.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit Telegram workflows**

```bash
git add modules/telegram_controller.py modules/telegram_api.py tests/test_telegram_workflows.py tests/conftest.py
git commit -m "feat: control drafts and media from Telegram"
```

---

### Task 10: Add read-only X discovery and candidate scoring

**Files:**
- Replace: `modules/growth.py` with `modules/growth_discovery.py`
- Create: `tests/test_growth_discovery.py`
- Modify: `modules/twitter_client.py:113-163, 200-270`
- Modify: `config.py`

**Interfaces:**
- Consumes: read-only X methods `get_followers_profiles()`, `search_recent_authors()`, `get_network_candidates()`, `get_latest_original_post()` and cached candidate rows.
- Produces: `score_growth_candidate(profile, latest_post, now) -> dict`, `passes_candidate_filters(profile, latest_post, now) -> tuple[bool, str]`, `GrowthDiscovery.run(now) -> list[dict]` with at most five digest candidates.

- [ ] **Step 1: Write exact scoring, hard-filter and budget tests**

```python
# tests/test_growth_discovery.py
from datetime import datetime, timezone

from modules.growth_discovery import passes_candidate_filters, score_growth_candidate


def test_relevant_gym_owner_scores_at_least_75():
    profile = {
        "description": "Owner of an independent strength and conditioning studio",
        "followers_count": 1800,
        "following_count": 650,
        "protected": False,
        "spam_signals": [],
    }
    post = {
        "text": "Testing a new class timetable for our members",
        "created_at": "2026-08-09T10:00:00+00:00",
        "lang": "en",
        "is_original": True,
    }
    result = score_growth_candidate(profile, post, datetime(2026, 8, 10, tzinfo=timezone.utc))
    assert result["total"] >= 75


def test_inactive_profile_fails_hard_filter():
    profile = {"description": "Gym owner", "protected": False, "spam_signals": []}
    post = {"created_at": "2026-06-01T10:00:00+00:00", "is_original": True}
    accepted, reason = passes_candidate_filters(
        profile, post, datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    assert accepted is False
    assert reason == "no_original_post_within_30_days"


def test_daily_budget_caps_queries_and_new_profiles(growth_discovery, fake_x):
    growth_discovery.run(datetime(2026, 8, 10, tzinfo=timezone.utc))
    assert fake_x.search_and_network_query_count <= 3
    assert fake_x.new_profile_evaluation_count <= 25
```

- [ ] **Step 2: Run discovery tests and confirm import failure**

Run: `venv/bin/python -m pytest tests/test_growth_discovery.py -v`

Expected: collection error because `modules.growth_discovery` does not exist.

- [ ] **Step 3: Implement hard filters and the approved 0–100 score**

Use these maximums exactly: role/bio 30, recent-topic fit 25, activity 15, English/international market 15, account quality 10 and direct drop-in/FlexDropin affinity 5. Match role terms such as `owner`, `founder`, `manager`, `coach`, `trainer`, `studio`, `gym`, `box`, `pilates`, `yoga` and `fitness tech`; match operating-topic terms such as `class`, `schedule`, `retention`, `member`, `no-show`, `occupancy`, `booking` and `drop-in`. Assign `audience_segment` as `primary`, `amplifier` or `end_user` in the score result so the 70/20/10 target mix can be measured.

Award role/bio points as 30 for a primary operator role, 20 for an amplifier role and 10 for a relevant end-user profile. Award topic points as 25 for at least two operating-topic matches and 15 for one; activity as 15 within seven days and 8 within 30; market as 15 for an English latest post; quality as 10 when the account has no spam signals and plausible public metrics; affinity as 5 for `drop-in`, `class booking`, `FlexDropin` or equivalent explicit intent. Cap each component and total arithmetically without an AI fallback.

Hard filters reject protected profiles, no original post in 30 days, insufficient bio/post context, stored spam/follow-farming signals and candidates suppressed within the prior 30 days. A score below 75 is stored for audit but is not included in a digest.

Add `GROWTH_SCORE_THRESHOLD=75`, `GROWTH_QUERY_BUDGET=3`, `GROWTH_NEW_PROFILE_BUDGET=25`, `GROWTH_PROFILE_CACHE_DAYS=7`, `GROWTH_DIGEST_LIMIT=5` and comma-separated `GROWTH_SEED_ACCOUNTS` to `config.py`, validating every numeric value as positive and capping the query budget at three.

- [ ] **Step 4: Implement budgeted discovery using read-only X methods**

`GrowthDiscovery.run` always considers newly observed followers, then rotates through two topic-author searches and one network query against `GROWTH_SEED_ACCOUNTS` while staying at or below three. Seed accounts are discovery inputs, not automatic engagement targets. Deduplicate by user ID before evaluation. Reuse a stored profile until `profile_expires_at`; count only newly fetched/evaluated profiles against the daily cap of 25. Upsert all evaluated candidates and return at most five rows sorted by score, then activity recency.

Delete the legacy follow/unfollow implementation. `TwitterClient` must request description, protected status, location, created time and public metrics for follower/profile reads, and must expose no X mutation other than `post_tweet`.

- [ ] **Step 5: Verify read-only growth discovery**

Run: `venv/bin/python -m pytest tests/test_growth_discovery.py tests/test_x_write_safety.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit read-only growth discovery**

```bash
git add modules/growth_discovery.py modules/twitter_client.py modules/database.py config.py tests/test_growth_discovery.py tests/conftest.py
git rm modules/growth.py
git commit -m "feat: discover relevant followers safely"
```

---

### Task 11: Add follower snapshots, manual-action conversion and weekly reporting

**Files:**
- Create: `tests/test_growth_analytics.py`
- Modify: `modules/analytics.py:18-97`
- Modify: `modules/database.py`
- Modify: `modules/telegram_controller.py`

**Interfaces:**
- Consumes: current follower profiles, growth candidates, Telegram decisions, published drafts and tweet metrics.
- Produces: `PerformanceAnalyzer.capture_follower_snapshot(observed_at) -> dict`, `PerformanceAnalyzer.build_weekly_report(end_date) -> dict`, `/stats` and `/growth` formatted reports.

- [ ] **Step 1: Write new-follower and follow-back conversion tests**

```python
# tests/test_growth_analytics.py
def test_snapshot_detects_new_relevant_follower(analyzer, fake_x, fake_db):
    fake_x.followers = [fake_db.relevant_owner]
    summary = analyzer.capture_follower_snapshot(fake_db.today)
    assert summary["new_total"] == 1
    assert summary["new_relevant"] == 1


def test_manual_candidate_follow_back_is_attributed(analyzer, fake_x, fake_db):
    fake_db.mark_candidate_decision(9, "followed_manually")
    fake_x.followers = [fake_db.candidates[9]["profile"]]
    analyzer.capture_follower_snapshot(fake_db.today)
    assert fake_db.candidates[9]["followed_back_at"] is not None


def test_report_labels_post_attribution_as_correlation(analyzer):
    report = analyzer.build_weekly_report(analyzer.today)
    assert report["attribution_label"] == "correlation"
    assert "relevant_follower_rate" in report
    assert "median_impressions" in report
```

- [ ] **Step 2: Run analytics tests and confirm failures**

Run: `venv/bin/python -m pytest tests/test_growth_analytics.py -v`

Expected: failures because snapshot/report methods do not exist.

- [ ] **Step 3: Capture daily snapshots and conversions**

Fetch follower profiles once, classify relevance with the same score/filter functions from Task 10, insert one row per date/user and compare user IDs with all prior snapshots. When a new follower matches a candidate with `decision='followed_manually'`, set `followed_back_at`; never initiate an X action. Return total followers, new total, new relevant and source counts.

- [ ] **Step 4: Build a stable weekly report without auto-reweighting**

The report dictionary contains `followers_total`, `new_followers`, `new_relevant_followers`, `relevant_follower_rate`, `candidate_count`, `decision_counts`, `follow_back_rate_by_source`, `median_impressions`, `post_count`, `content_by_category`, `query_budget_used`, `profiles_evaluated`, `factual_blocks` and `attribution_label='correlation'`. Keep `refresh_own_tweet_metrics`; stop calling `recompute_category_weights` during the first 30 days.

Format `/stats` as a weekly summary and `/growth` as candidate/manual-decision/follow-back status. Add a weekly Telegram push using the same formatter so command output and scheduled output cannot diverge.

- [ ] **Step 5: Verify follower analytics**

Run: `venv/bin/python -m pytest tests/test_growth_analytics.py tests/test_growth_discovery.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit follower analytics**

```bash
git add modules/analytics.py modules/database.py modules/telegram_controller.py tests/test_growth_analytics.py tests/conftest.py
git commit -m "feat: report relevant follower growth"
```

---

### Task 12: Wire the safe scheduler and prove the complete dry-run flow

**Files:**
- Create: `tests/test_end_to_end_dry_run.py`
- Create: `.env.example`
- Modify: `main.py:29-458`
- Modify: `README.md`
- Modify: `SETUP.md`
- Modify: `tests/fakes.py`

**Interfaces:**
- Consumes: all components from Tasks 1–11.
- Produces: `FlexDropinGrowthAgent(dependencies=None)`, `register_jobs()`, graceful Telegram polling shutdown and a complete production dry-run with zero X writes.

- [ ] **Step 1: Write the full-flow integration test**

```python
# tests/test_end_to_end_dry_run.py
from datetime import datetime
from zoneinfo import ZoneInfo


def test_source_to_approval_to_dry_run_without_external_writes(agent, fakes):
    source_id = agent.db.add_content_source(
        "founder_note",
        "I decided to reduce posting frequency so every post earns attention.",
        metadata={"publishable": True},
        verified_by="floriano",
    )
    assert source_id > 0
    slot = datetime(2026, 8, 11, 14, 0, tzinfo=ZoneInfo("Europe/Rome"))
    draft = agent.draft_pipeline.create_for_slot(slot)
    assert draft["status"] == "pending_approval"
    agent.telegram_controller.process_update(
        fakes.callback_update(301, "draft:approve:" + str(draft["id"])),
    )
    result = agent.publisher.publish(draft["id"], slot)
    assert result.status == "dry_run"
    assert fakes.x.posts == []
    assert fakes.x.engagement_writes == []


def test_media_upload_does_not_create_draft(agent, fakes):
    before = len(agent.db.list_post_drafts())
    agent.telegram_controller.process_update(fakes.photo_update(302, "Future studio content"))
    assert len(agent.db.list_post_drafts()) == before
```

- [ ] **Step 2: Run the integration test and confirm wiring failures**

Run: `venv/bin/python -m pytest tests/test_end_to_end_dry_run.py -v`

Expected: fixture or constructor failures until dependencies and jobs are wired.

- [ ] **Step 3: Build explicit dependency injection and schedule only safe jobs**

Register these jobs in `Europe/Rome`:

- `verified_news_refresh` once daily at 10:30, which stores only complete articles from `NEWS_TRUSTED_DOMAINS`;
- `draft_14:00` at 12:00 and `draft_20:00` at 18:00, each calling `create_draft_cycle(intended_slot_time)`;
- `publish_14:00` and `publish_20:00`, each checking for an approved draft for that exact slot;
- `growth_discovery` once daily at 11:00, which sends one Telegram digest only when at least one score-75 candidate exists;
- `follower_snapshot` once daily at 23:15;
- `performance_metrics` once daily at 23:30;
- `weekly_growth_report` each Monday at 09:00;
- existing lead discovery only when `ENABLE_LEAD_DISCOVERY=true`, because follower growth is the priority and lead suggestions are secondary even though they perform no X write.

Start `TelegramController.run_forever()` in one named daemon thread and use a `threading.Event` for graceful shutdown. Do not register legacy engagement, follow/unfollow, human-mode or automatic build-in-public jobs. Use `BackgroundScheduler(timezone=ZoneInfo(BOT_TIMEZONE))` and inject fake dependencies without calling `validate_config()` in tests.

Update `validate_config()` so Telegram token/chat ID are mandatory for this approval-only release, NewsAPI is mandatory only when `NEWS_TRUSTED_DOMAINS` is non-empty, and `APPROVAL_REQUIRED=false` is rejected. This release has no unattended-publication mode even when `DRY_RUN=false`; that switch only enables due drafts already approved through Telegram.

- [ ] **Step 4: Update operating documentation with exact rollout switches**

Document these environment variables and initial values:

```dotenv
BOT_TIMEZONE=Europe/Rome
CONTENT_SLOTS=14:00,20:00
DRAFT_LEAD_MINUTES=120
PUBLISH_GRACE_SECONDS=300
APPROVAL_REQUIRED=true
DRY_RUN=true
DRAFT_SCORE_THRESHOLD=75
SEMANTIC_DUPLICATE_THRESHOLD=0.72
MAX_LINKS_PER_WEEK=1
ENABLE_LEAD_DISCOVERY=false
MEDIA_MATCH_THRESHOLD=80
TELEGRAM_POLL_TIMEOUT=25
TELEGRAM_MAX_IMAGE_BYTES=10485760
TELEGRAM_MAX_VIDEO_BYTES=52428800
GROWTH_SCORE_THRESHOLD=75
GROWTH_QUERY_BUDGET=3
GROWTH_NEW_PROFILE_BUDGET=25
GROWTH_PROFILE_CACHE_DAYS=7
GROWTH_DIGEST_LIMIT=5
GROWTH_SEED_ACCOUNTS=
NEWS_TRUSTED_DOMAINS=
```

Document the dry-run checklist: Telegram authorization, one text source, one photo and one video upload, draft preview, all draft buttons, pause/resume, duplicate callback, growth digest, follower snapshot and confirmation that X write count is zero. State that `DRY_RUN=false` is changed only after that checklist passes on the VPS.

- [ ] **Step 5: Run the complete verification suite**

Run: `venv/bin/python -m pytest -v`

Expected: all tests pass with no network access and no real X/Telegram/Groq/News calls.

Run: `venv/bin/python -m compileall -q main.py config.py modules dashboard`

Expected: exit code 0.

Run: `git diff --check`

Expected: no output.

- [ ] **Step 6: Commit the integrated dry-run system**

```bash
git add .env.example main.py README.md SETUP.md tests/test_end_to_end_dry_run.py tests/conftest.py tests/fakes.py
git commit -m "feat: run growth bot through Telegram approvals"
```

---

### Task 13: Perform the production dry-run acceptance gate

**Files:**
- Modify only if evidence exposes a defect: the smallest directly responsible source/test file.
- Record operational evidence in the deployment log or Telegram update history; do not add secrets or live payloads to Git.

**Interfaces:**
- Consumes: deployed service with real read credentials, authorized Telegram chat and `DRY_RUN=true`.
- Produces: verified acceptance evidence required before enabling approval-only X publishing.

- [ ] **Step 1: Deploy with writes disabled and verify health**

Run on the VPS: `systemctl restart flexdropin-bot && systemctl status flexdropin-bot --no-pager`

Expected: service active, Telegram polling started, scheduler shows the safe job set and logs contain no automated engagement job.

- [ ] **Step 2: Exercise Telegram control and media persistence**

From the authorized chat, run `/status`, `/posts`, `/growth`, `/stats`, `/ideas`, `/pause`, `/resume`, `/errors` and `/help`; upload one image and one video with captions. Confirm both files remain `available`, no draft is created by either upload and an unauthorized test chat receives no operational data.

- [ ] **Step 3: Exercise draft approval without publishing**

Create one founder note and one evergreen idea, wait for or invoke a candidate cycle, inspect sources/score/media, press each non-destructive draft action at least once and approve the final draft. At the intended slot, confirm the result is `dry_run`, the draft is not recorded as published and no X post exists.

- [ ] **Step 4: Exercise discovery and reporting without engagement writes**

Run one discovery cycle, confirm query count is at most three, new profile evaluation count is at most 25 and digest count is at most five. Use `Open on X`, `Save`, `Followed on X` and `Discard`; confirm only local decisions change and no follow/like/reply/DM is issued by the bot.

- [ ] **Step 5: Review the rollout gate with Floriano**

Present the dry-run evidence: zero X writes, zero unsupported drafts reaching approval, duplicate callbacks ignored, uploads preserved, pause respected, discovery budgets respected and dashboard still operational. Change `DRY_RUN=false` only after Floriano explicitly approves enabling scheduled publication of Telegram-approved drafts.
