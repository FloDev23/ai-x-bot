# Automatic Source Refresh and Rotation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import the official FlexDropin editorial feed and allowlisted external news every day, then rotate those sources into grounded Telegram-approved drafts without weakening any X safety boundary.

**Architecture:** A fixed-host feed client owns HTTP and schema validation, database methods own atomic URL-based imports, and a coordinator isolates the blog and external-news channels. `ContentPlanner` ranks one eligible source from persisted usage history; the existing `DraftPipeline`, `FactGuard`, Telegram approval, and `Publisher` remain the only downstream path.

**Tech Stack:** Python 3.11, Requests, SQLite with `BEGIN IMMEDIATE`, APScheduler, pytest, existing Groq/Telegram/X boundaries.

## Global Constraints

- Work in `/Users/floriano/flo_mobile_app/ai-x-bot-main` on `main` only after verifying a clean worktree.
- The official feed URL is the code constant `https://flexdropin.com/api/editorial-feed`; production does not read it from `.env`.
- Feed response limit is 256 KiB, maximum items is 100, redirects are disabled, and connect/read timeouts are bounded.
- Store official articles only as `owned_blog_article` with English canonical URLs.
- `owned_blog_article` may support only general copy plus exact `number` and `named_entity` claims from stored title/summary.
- Preserve `DRY_RUN=true`, `APPROVAL_REQUIRED=true`, score threshold `75`, two-draft daily cap, candidate tournament, duplicate gate, and at-most-one Telegram card.
- Blog links consume the existing global `MAX_LINKS_PER_WEEK=1` budget and the same article cannot be linked again for 30 days.
- The refresh cycle runs daily at 10:30 Europe/Rome and never creates a draft or calls X.
- Normal success/no-change results do not notify Telegram; each failed channel produces at most one sanitized serious notification.
- Use test-driven development, real temporary SQLite for transaction/concurrency tests, and one focused commit per task.

---

### Task 1: Strict fixed-host editorial-feed client

**Files:**
- Create: `modules/editorial_feed.py`
- Create: `tests/test_editorial_feed.py`
- Read: `modules/source_validation.py`

**Interfaces:**
- Consumes: a Requests-compatible object exposing `get(url, timeout, allow_redirects, stream)`.
- Produces: `EditorialFeedError`, `FLEXDROPIN_EDITORIAL_FEED_URL`, `validate_editorial_feed(payload, today) -> List[Dict]`, and `FlexDropinEditorialFeedClient.fetch() -> List[Dict]`.

- [ ] **Step 1: Write the failing pure-schema tests**

Create a valid payload fixture:

```python
VALID_FEED = {
    "version": 1,
    "language": "en",
    "items": [{
        "slug": "gym-drop-ins-sell-single-classes",
        "url": "https://flexdropin.com/blog/gym-drop-ins-sell-single-classes",
        "title": "Gym drop-ins: how to test demand",
        "summary": "A bounded operating guide for gym owners.",
        "published_at": "2026-08-20",
    }],
}
```

Assert a valid record receives a lowercase 64-character SHA-256
`content_hash`. Add parameterized failures for extra/missing keys, version
`2`, language `it`, 101 items, duplicate slug/URL, uppercase or path-traversal
slug, lookalike host, subdomain, port, query, fragment, encoded path,
non-HTTPS URL, title length 201, summary length 1001, invalid/future date,
non-dict item, booleans, and deeply nested/cyclic input.

- [ ] **Step 2: Run schema tests to verify RED**

Run: `venv/bin/python -m pytest tests/test_editorial_feed.py -k schema -v`

Expected: collection FAIL with `ModuleNotFoundError: modules.editorial_feed`.

- [ ] **Step 3: Implement strict pure validation**

Define these constants and shapes in `modules/editorial_feed.py`:

```python
FLEXDROPIN_EDITORIAL_FEED_URL = (
    "https://flexdropin.com/api/editorial-feed"
)
MAX_EDITORIAL_FEED_BYTES = 256 * 1024
MAX_EDITORIAL_FEED_ITEMS = 100
_TOP_LEVEL_FIELDS = frozenset({"version", "language", "items"})
_ITEM_FIELDS = frozenset({
    "slug", "url", "title", "summary", "published_at",
})

class EditorialFeedError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def validate_editorial_feed(payload, today):
    """Return canonical records or raise one sanitized EditorialFeedError."""
```

Use `type(value) is dict/list/str/int`, exact key sets, an ASCII slug regex,
`urllib.parse.urlsplit`, exact scheme/hostname/default port/path, empty
query/fragment/userinfo, ISO date round-trip, bounded strings, duplicate sets,
and `json.dumps(..., allow_nan=False)` inside a `try` that converts recursion or
type failures to `EditorialFeedError("invalid_feed_schema") from None`.
Compute the hash from compact sorted JSON of the five public item fields.

- [ ] **Step 4: Run schema tests to verify GREEN**

Run: `venv/bin/python -m pytest tests/test_editorial_feed.py -k schema -v`

Expected: all schema tests PASS.

- [ ] **Step 5: Write failing transport-boundary tests**

Add Requests fakes proving:

- URL equals `FLEXDROPIN_EDITORIAL_FEED_URL`;
- timeout equals `(5, 10)`;
- `allow_redirects is False` and `stream is True`;
- status other than 200, redirect 301, non-JSON content type, missing/negative/
  non-decimal/over-limit `Content-Length`, streamed overflow, invalid UTF-8,
  invalid JSON, `iter_content` failure, and `close()` failure raise only an
  allowlisted `EditorialFeedError.code`;
- the response is closed exactly once and no raw body/token/URL appears in the
  exception, log record, or traceback;
- a body exactly 256 KiB is accepted only when its JSON is otherwise valid.

- [ ] **Step 6: Run transport tests to verify RED**

Run: `venv/bin/python -m pytest tests/test_editorial_feed.py -k transport -v`

Expected: FAIL because `FlexDropinEditorialFeedClient` is missing.

- [ ] **Step 7: Implement the bounded transport**

```python
class FlexDropinEditorialFeedClient:
    def __init__(self, http, now_fn=None):
        self.http = http
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc).date())

    def fetch(self):
        response = None
        try:
            response = self.http.get(
                FLEXDROPIN_EDITORIAL_FEED_URL,
                timeout=(5, 10),
                allow_redirects=False,
                stream=True,
            )
            # Validate status, exact JSON media type, bounded Content-Length,
            # then count bytes from iter_content before UTF-8/JSON parsing.
            return validate_editorial_feed(payload, self.now_fn())
        except EditorialFeedError:
            raise
        except Exception:
            raise EditorialFeedError("feed_transport_failed") from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
```

The final implementation must not interpolate the response, URL, headers, or
underlying exception into error text.

- [ ] **Step 8: Run all feed-client tests and commit**

Run: `venv/bin/python -m pytest tests/test_editorial_feed.py -v`

Expected: PASS.

```bash
git add modules/editorial_feed.py tests/test_editorial_feed.py
git commit -m "feat: validate FlexDropin editorial feed"
```

### Task 2: Atomic blog storage and eligibility boundary

**Files:**
- Modify: `modules/source_validation.py`
- Modify: `modules/database.py`
- Modify: `modules/fact_guard.py`
- Modify: `tests/test_editorial_feed.py`
- Modify: `tests/test_fact_guard.py`

**Interfaces:**
- Consumes: validated records from `FlexDropinEditorialFeedClient.fetch()`.
- Produces: `is_complete_owned_blog_article(source) -> bool` and `Database.import_owned_blog_articles(records) -> Dict[str, int]` with exact keys `inserted`, `updated`, `unchanged`.

- [ ] **Step 1: Write failing SQLite import tests**

Use a real `Database(tmp_path / "bot.db")` and prove:

```python
counts = db.import_owned_blog_articles(valid_records)
assert counts == {"inserted": 2, "updated": 0, "unchanged": 0}
source = db.get_content_source(1)
assert source["source_type"] == "owned_blog_article"
assert source["trust_state"] == "verified"
assert source["verified_by"] == "flexdropin_editorial_feed"
assert source["text"] == source["metadata"]["title"] + "\n" + source["metadata"]["summary"]
```

Add cases for unchanged rerun, changed hash update, missing later item retained,
existing different source type conflict with zero batch writes, manually
revoked owned row not re-enabled, trigger-aborted second insert rolling back the
first, two `Database` instances importing concurrently with one URL row, and a
malformed metadata/hash/date/URL record rejected before mutation.

- [ ] **Step 2: Run import tests to verify RED**

Run: `venv/bin/python -m pytest tests/test_editorial_feed.py -k database -v`

Expected: FAIL because the database import method and source validator are missing.

- [ ] **Step 3: Implement the owned-source validator**

In `modules/source_validation.py`, add:

```python
def is_complete_owned_blog_article(source) -> bool:
    """Accept only one exact official-feed row suitable for planning."""
```

Require exact source type/trust/verifier, non-empty exact `text`, exact
canonical URL and slug agreement, metadata mapping with title, summary,
published date, source name `FlexDropin Blog`, slug, integer feed version `1`,
and a lowercase 64-hex content hash. Recompute the hash from the canonical five
feed fields and require `text == title + "\n" + summary`.

- [ ] **Step 4: Implement one-transaction compare/update import**

Add to `Database`:

```python
def import_owned_blog_articles(self, records: List[Dict]) -> Dict[str, int]:
    with self._conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Validate the complete batch and every URL conflict before mutation.
        # Insert unseen rows; update only feed-owned verified rows with a
        # changed content_hash; leave unchanged rows untouched.
        return {"inserted": inserted, "updated": updated, "unchanged": unchanged}
```

Set `verified_at`, `created_at`, and `updated_at` from one injected/current UTC
timestamp. Preserve original `created_at`. Do not update `trust_state` or
`verified_by` on an existing row. Any conflict raises a sanitized
`ValueError("owned_blog_source_conflict")` and rolls back the entire batch.

Call `is_complete_owned_blog_article` from both
`_eligible_content_sources_in_conn` and `get_eligible_sources`, parallel to the
existing `verified_news` check, so direct database corruption fails closed.

- [ ] **Step 5: Run database tests to verify GREEN**

Run: `venv/bin/python -m pytest tests/test_editorial_feed.py -k database -v`

Expected: PASS, including rollback and two-worker cases.

- [ ] **Step 6: Write RED factual-permission tests**

In `tests/test_fact_guard.py`, assert one verified `owned_blog_article`:

- supports a `number` only when the exact numeric token is present in that
  article's title/summary and the analyzer cites its ID;
- supports a cited `named_entity`;
- rejects `first_person`, `product_claim`, `incident`, `testimonial`,
  `medical`, and `named_current_event`;
- cannot borrow a number from another source.

- [ ] **Step 7: Implement narrow claim permissions and verify GREEN**

Change only these entries in `REQUIRED_SOURCE_TYPES`:

```python
"number": {"founder_note", "product_fact", "verified_news", "owned_blog_article"},
"named_entity": {"founder_note", "product_fact", "verified_news", "owned_blog_article"},
```

Do not add the new type to any other claim class.

Run: `venv/bin/python -m pytest tests/test_fact_guard.py tests/test_editorial_feed.py -v`

Expected: PASS.

- [ ] **Step 8: Commit atomic storage and trust rules**

```bash
git add modules/source_validation.py modules/database.py modules/fact_guard.py tests/test_editorial_feed.py tests/test_fact_guard.py
git commit -m "feat: store official blog sources atomically"
```

### Task 3: Atomic external-news refresh and channel coordinator

**Files:**
- Modify: `modules/news_fetcher.py`
- Modify: `modules/source_ingestion.py`
- Create: `modules/source_refresh.py`
- Modify: `modules/database.py`
- Modify: `tests/fakes.py`
- Modify: `tests/test_source_ingestion.py`
- Create: `tests/test_source_refresh.py`

**Interfaces:**
- Consumes: `FlexDropinEditorialFeedClient.fetch()` and `NewsFetcher.get_trending_news()`.
- Produces: `Database.insert_verified_news_batch(records) -> int`, `SourceRefreshChannel`, `SourceRefreshResult`, and `SourceRefreshCoordinator.refresh(topics, per_topic=1) -> SourceRefreshResult`.

- [ ] **Step 1: Write RED tests for all-or-nothing external persistence**

Use valid and malformed NewsAPI records to prove `SourceIngestor` fetches all
topics before the first database write, deduplicates URLs across topics,
skips incomplete/untrusted items, and inserts the valid batch in one SQLite
transaction. Add a trigger failure on the second insert and assert zero new
rows.

Run: `venv/bin/python -m pytest tests/test_source_ingestion.py -v`

Expected: FAIL because the current implementation commits per article.

- [ ] **Step 2: Add a sanitized NewsAPI outage boundary**

Define in `modules/news_fetcher.py`:

```python
class NewsFetchUnavailable(RuntimeError):
    pass
```

Keep an absent API key as the existing disabled `[]` result. For Requests,
HTTP, JSON, or response-shape failures, log only the exception class and raise
`NewsFetchUnavailable("news_fetch_unavailable") from None`. Never log the
request URL, query params, API key, response body, or original exception text.

Add tests with sentinel API keys/bodies and `caplog` proving no sentinel
appears in logs, exceptions, or tracebacks.

- [ ] **Step 3: Implement batched news collection and persistence**

Refactor `SourceIngestor.refresh_verified_news` to collect canonical records:

```python
{
    "source_type": "verified_news",
    "text": summary,
    "url": url,
    "metadata": {
        "title": title,
        "summary": summary,
        "published_at": published_at,
        "source_name": source_name,
    },
    "trust_state": "verified",
    "verified_by": "trusted_news_ingestion",
}
```

Then call `Database.insert_verified_news_batch(records)`. That method opens one
`BEGIN IMMEDIATE`, revalidates every record with
`is_complete_verified_news`, skips URLs already present, and inserts all new
rows before one commit. Preserve the public integer return value.

- [ ] **Step 4: Write RED coordinator independence tests**

Create fakes and assert these exact outcomes:

```python
result = coordinator.refresh(["gym operations"], per_topic=1)
assert result.blog.error_code == ""
assert result.blog.inserted == 2
assert result.news.error_code == "external_news_refresh_failed"
assert result.news.inserted == 0
```

Mirror the case with a failed blog channel and successful news channel. Also
test both success, both failure, blog updates/unchanged counts, external
allowlist disabled, and result fields containing only bounded integers and
allowlisted error codes.

- [ ] **Step 5: Implement the coordinator data types and isolation**

```python
@dataclass(frozen=True)
class SourceRefreshChannel:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    error_code: str = ""

@dataclass(frozen=True)
class SourceRefreshResult:
    blog: SourceRefreshChannel
    news: SourceRefreshChannel

class SourceRefreshCoordinator:
    def __init__(self, database, editorial_feed_client, news_ingestor): ...

    def refresh(self, topics, per_topic=1):
        # Execute blog and news in separate try blocks and transactions.
```

Catch arbitrary channel exceptions but expose only
`blog_refresh_failed` or `external_news_refresh_failed`. Do not put exception
messages, source bodies, URLs, or records into the result.

- [ ] **Step 6: Verify source ingestion/coordinator GREEN and commit**

Run: `venv/bin/python -m pytest tests/test_source_ingestion.py tests/test_source_refresh.py tests/test_editorial_feed.py -v`

Expected: PASS.

```bash
git add modules/news_fetcher.py modules/source_ingestion.py modules/source_refresh.py modules/database.py tests/fakes.py tests/test_source_ingestion.py tests/test_source_refresh.py
git commit -m "feat: refresh source channels atomically"
```

### Task 4: Wire the daily independent refresh into the agent

**Files:**
- Modify: `main.py`
- Modify: `tests/fakes.py`
- Modify: `tests/test_end_to_end_dry_run.py`
- Modify: `tests/test_main_startup.py`
- Modify: `tests/test_source_refresh.py`

**Interfaces:**
- Consumes: `FlexDropinEditorialFeedClient`, `SourceRefreshCoordinator`, existing notifier, `SEARCH_TOPICS`.
- Produces: `FlexDropinGrowthAgent.refresh_sources_cycle()` and one scheduler job `source_refresh` at 10:30 Europe/Rome.

- [ ] **Step 1: Write failing dependency-injection and scheduler tests**

Extend `dependency_bundle` with a `FakeEditorialFeedClient`. Assert an injected
agent constructs no real Requests boundary, rejects a missing/`None`
`editorial_feed_client`, and respects falsey fake objects without fallback.

Update the scheduler assertion to require exactly:

```python
job = scheduler.jobs["source_refresh"]
assert job.name == "Editorial source refresh"
assert str(job.trigger.timezone) == "Europe/Rome"
assert job.trigger.fields[5].expressions[0].first == 10
assert job.trigger.fields[6].expressions[0].first == 30
assert "verified_news_refresh" not in scheduler.jobs
```

- [ ] **Step 2: Run focused orchestration tests to verify RED**

Run: `venv/bin/python -m pytest tests/test_end_to_end_dry_run.py tests/test_main_startup.py tests/test_source_refresh.py -k "source_refresh or dependencies or schedule" -v`

Expected: FAIL because the new boundary and job are absent.

- [ ] **Step 3: Wire production and injected dependencies**

In `main.py`:

- import Requests only inside the default editorial-client factory or pass the
  imported `requests` module there;
- add `editorial_feed_client` and `source_refresh` to `_DEPENDENCY_KEYS`;
- add `editorial_feed_client` to `_REQUIRED_INJECTED_BOUNDARIES`;
- construct `FlexDropinEditorialFeedClient(requests)` only in non-injected
  production composition;
- construct `SourceRefreshCoordinator(self.db, client, self.source_ingestor)`;
- replace `refresh_verified_news_cycle` with `refresh_sources_cycle`;
- register only `source_refresh` at 10:30.

The cycle must call `coordinator.refresh(SEARCH_TOPICS, per_topic=1)`, notify
once for each non-empty channel error code via a sanitized exception class,
return the result, and never call planner, pipeline, Telegram cards, Publisher,
or X.

- [ ] **Step 4: Add notification/no-noise assertions**

Test successful and unchanged outcomes produce zero notifier calls. Test one
failed channel produces exactly one notification with an allowlisted operation
name and no source/body/API-key sentinel. Test both failed channels produce
exactly two notifications while the cycle still returns normally.

- [ ] **Step 5: Run orchestration tests to verify GREEN**

Run: `venv/bin/python -m pytest tests/test_end_to_end_dry_run.py tests/test_main_startup.py tests/test_source_refresh.py -v`

Expected: PASS with zero X writes and zero draft creation during refresh.

- [ ] **Step 6: Commit the scheduled wiring**

```bash
git add main.py tests/fakes.py tests/test_end_to_end_dry_run.py tests/test_main_startup.py tests/test_source_refresh.py
git commit -m "feat: schedule independent source refresh"
```

### Task 5: Source rotation and conservative blog-link quota

**Files:**
- Modify: `modules/database.py`
- Modify: `modules/content_planner.py`
- Modify: `main.py`
- Modify: `tests/fakes.py`
- Modify: `tests/test_content_planner.py`
- Modify: `tests/test_draft_pipeline_sqlite.py`

**Interfaces:**
- Consumes: eligible sources and persisted `post_drafts`/`posted_tweets` history.
- Produces: `Database.get_content_source_usage(source_ids, now=None) -> Optional[Dict[int, Dict]]`, `Database.count_links_last_days(days=7, now=None) -> int`, and deterministic one-source plans.

- [ ] **Step 1: Write RED usage-history tests on real SQLite**

Create sources and drafts/tweets that prove the method returns per source:

```python
{
    source_id: {
        "bound_to_live_draft": False,
        "last_published_at": "2026-07-01T10:00:00+00:00",
        "last_linked_at": None,
    }
}
```

Cover pending, approved, publishing, and `publication_unknown` as live; rejected,
expired, discarded, and publication-failed as non-live; published rows joined
to the canonical `posted_tweets.created_at`; malformed `source_ids_json`
returns `None` for fail-closed planning. Add an injected `now` test for the
rolling link count instead of wall-clock behavior.

- [ ] **Step 2: Run usage tests to verify RED**

Run: `venv/bin/python -m pytest tests/test_draft_pipeline_sqlite.py -k source_usage -v`

Expected: FAIL because the usage API does not exist.

- [ ] **Step 3: Implement usage aggregation without JSON1 assumptions**

Read candidate draft/tweet rows in one connection, decode `source_ids_json` in
Python using exact positive integer IDs, and initialize every requested source
with empty usage. Return `None` if any relevant JSON row is malformed. Normalize
all timestamps to aware UTC values before comparison; return ISO strings or
`None` in the public mapping.

Extend `count_links_last_days(days=7, now=None)` to use the injected timestamp
while keeping old callers source-compatible.

- [ ] **Step 4: Write the rotation and link-policy RED matrix**

In `tests/test_content_planner.py`, add tests for:

- `owned_blog_article` eligibility only in the three approved categories;
- live-bound sources excluded;
- never-used beats old, old beats recent, and recent falls back to least recent;
- same bucket breaks ties by newest `metadata.published_at`, then descending row
  ID to preserve the established newest-row fallback;
- ranking applies across `owned_blog_article`, `verified_news`, and
  `evergreen_idea` rather than fixed type priority;
- malformed usage returns no plan;
- a blog source gets a link below the global cap and no link at/above it;
- a blog source linked less than 30 days ago gets no link even when the weekly
  budget is free;
- a blog source may still produce a link-free plan;
- product-proof link behavior remains unchanged;
- exactly one source ID is returned.

- [ ] **Step 5: Implement deterministic rotation**

Change category types to:

```python
SOURCE_TYPES = {
    "gym_strategy": {"evergreen_idea", "verified_news", "founder_note", "owned_blog_article"},
    "fitness_business_insight": {"verified_news", "owned_blog_article"},
    "shareable_fitness": {"evergreen_idea", "verified_news", "owned_blog_article"},
    "product_proof": {"product_fact"},
    "founder_journey": {"founder_note"},
}
```

Remove fixed `SOURCE_TYPE_PRIORITY`. Inject `max_links_per_week` into
`ContentPlanner.__init__` from `config.MAX_LINKS_PER_WEEK`. Load usage once for
all eligible source IDs and sort each category's candidates by this key:

```python
(
    usage_bucket,          # 0 never, 1 >=30 days, 2 <30 days
    last_used_or_min,      # ascending: least recent first
    -published_timestamp,  # newest source date wins a tie
    -source_id,            # stable existing newest-row fallback
)
```

Exclude live-bound rows before sorting. Parse malformed publication dates as
the oldest possible source date; source eligibility still handles malformed
official/news metadata fail-closed.

Set `include_link` for `product_proof` as before. For an owned blog source,
also require weekly global count below the injected cap and no
`last_linked_at` within 30 days.

- [ ] **Step 6: Verify planner and SQLite integration GREEN**

Run: `venv/bin/python -m pytest tests/test_content_planner.py tests/test_draft_pipeline_sqlite.py tests/test_draft_pipeline.py -v`

Expected: PASS, with one source per plan and existing portfolio/day limits intact.

- [ ] **Step 7: Commit rotation and quota behavior**

```bash
git add modules/database.py modules/content_planner.py main.py tests/fakes.py tests/test_content_planner.py tests/test_draft_pipeline_sqlite.py
git commit -m "feat: rotate editorial sources safely"
```

### Task 6: End-to-end source refresh acceptance and operator docs

**Files:**
- Modify: `tests/test_end_to_end_dry_run.py`
- Modify: `tests/test_x_write_safety.py`
- Modify: `README.md`
- Modify: `SETUP.md`
- Modify: `.env.example`

**Interfaces:**
- Consumes: completed feed client, imports, coordinator, scheduler, planner, and existing pipeline.
- Produces: documented automatic source refresh with no new X write path.

- [ ] **Step 1: Write the full fake-to-SQLite acceptance test**

Build an injected agent with one official feed record and one external record.
Run `refresh_sources_cycle()`, assert two source types are stored, run a draft
cycle, and assert:

```python
assert draft["source_ids"] == [selected_source_id]
assert draft["score_data"]["total"] >= 75
assert len(fake_telegram.messages) == 1
assert fake_x.posts == []
assert fake_x.engagement_writes == []
```

Run the refresh a second time and assert no duplicate source, no draft, no
Telegram notification, and no X call. Add a two-agent shared-SQLite refresh
race followed by a draft race: unique source URLs, one live draft, one card.

- [ ] **Step 2: Add static X-safety assertions**

Extend `tests/test_x_write_safety.py` so `modules/editorial_feed.py`,
`modules/source_ingestion.py`, `modules/source_refresh.py`, and
`modules/content_planner.py` contain no `post_tweet`, `create_tweet`, follow,
like, reply, repost, DM, or media-upload capability. Confirm the only production
tweet write remains `Publisher -> TwitterClient.post_tweet`.

- [ ] **Step 3: Run acceptance tests**

Run: `venv/bin/python -m pytest tests/test_end_to_end_dry_run.py tests/test_x_write_safety.py tests/test_source_refresh.py tests/test_content_planner.py -v`

Expected: PASS with zero external writes.

- [ ] **Step 4: Update configuration and operator documentation**

Document:

- the fixed official feed needs no secret or environment variable;
- `NEWS_TRUSTED_DOMAINS` and `NEWSAPI_KEY` control only external news;
- the daily refresh time is 10:30 Europe/Rome;
- successful/no-change cycles are silent;
- `/errors` shows sanitized systemic failures;
- `owned_blog_article` is not a product fact;
- global weekly link budget remains `MAX_LINKS_PER_WEEK=1`;
- permanent production flags remain `DRY_RUN=true` and
  `APPROVAL_REQUIRED=true`.

Keep example secret values blank. Remove no existing approval, media, or growth
instructions.

- [ ] **Step 5: Run the complete verification gate**

Run:

```bash
venv/bin/python -m pytest -v
venv/bin/python -m compileall -q main.py config.py modules dashboard
venv/bin/python -m pip check
git diff --check
```

Expected: full suite PASS, compilation exits 0, no broken requirements, and no
whitespace errors. The existing Tweepy `imghdr` deprecation warning may remain
but no new warning is accepted.

- [ ] **Step 6: Review the complete diff before commit**

Use `superpowers:requesting-code-review`. Fix every Critical or Important
finding with a fresh RED/GREEN test and rerun Step 5.

- [ ] **Step 7: Commit acceptance and documentation**

```bash
git add tests/test_end_to_end_dry_run.py tests/test_x_write_safety.py README.md SETUP.md .env.example
git commit -m "docs: describe automatic source refresh"
```

- [ ] **Step 8: Record the bot SHA**

Run: `git show --check --oneline HEAD && git status --short && git rev-parse HEAD`

Expected: clean tracked worktree and one final 40-character bot SHA for the controlled deployment plan.
