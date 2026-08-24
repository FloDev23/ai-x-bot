import logging
import sqlite3

import pytest
import requests

from modules.database import Database
from modules.news_fetcher import NewsFetchUnavailable, NewsFetcher
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
    assert fake_db.sources[0]["metadata"] == {
        "title": "Operators rethink class capacity",
        "summary": "A concrete reported change.",
        "published_at": "2026-08-10T08:00:00Z",
        "source_name": "Industry Example",
    }


def test_ingestor_rejects_incomplete_or_lookalike_or_duplicate_urls(fake_db, fake_news):
    fake_db.sources = [{"id": 1, "url": "https://industry.example/duplicate"}]
    fake_news.articles = [
        {
            "title": "Missing publication date",
            "description": "Cannot be used.",
            "url": "https://industry.example/missing-date",
            "published_at": "",
            "source": "Industry Example",
        },
        {
            "title": "Lookalike domain",
            "description": "Cannot be used.",
            "url": "https://notindustry.example/report",
            "published_at": "2026-08-10T08:00:00Z",
            "source": "Industry Example",
        },
        {
            "title": "Duplicate",
            "description": "Cannot be added twice.",
            "url": "https://industry.example/duplicate",
            "published_at": "2026-08-10T08:00:00Z",
            "source": "Industry Example",
        },
        *[
            {
                "title": "Malformed authority",
                "description": "Cannot be verified.",
                "url": url,
                "published_at": "2026-08-10T08:00:00Z",
                "source": "Industry Example",
            }
            for url in (
                "https://industry.example:bad/report",
                "https://industry.example:99999/report",
                "https://user:pass@industry.example/report",
                "https://industry .example/report",
                "https://industry.example../report",
            )
        ],
    ]
    ingestor = SourceIngestor(fake_db, fake_news, {"industry.example"})

    assert ingestor._is_trusted_https_url(
        "https://industry.example.:443/report"
    ) is True
    assert ingestor.refresh_verified_news(["gym operations"], per_topic=10) == 0
    assert len(fake_db.sources) == 1


def test_empty_allowlist_disables_automatic_ingestion(fake_db, fake_news):
    fake_news.articles = [{"title": "Would otherwise be valid"}]
    ingestor = SourceIngestor(fake_db, fake_news, set())

    assert ingestor.refresh_verified_news(["gym operations"]) == 0
    assert fake_news.queries == []


def valid_article(url, title="Operators rethink class capacity"):
    return {
        "title": title,
        "description": "A concrete reported change.",
        "url": url,
        "publishedAt": "2026-08-10T08:00:00Z",
        "source": {"name": "Industry Example"},
    }


def test_ingestor_collects_all_topics_before_one_batch_write():
    calls = []

    class News:
        def get_trending_news(self, topic, limit):
            calls.append(("fetch", topic, limit))
            return [valid_article(f"https://industry.example/{topic}")]

    class DatabaseBoundary:
        def insert_verified_news_batch(self, records):
            calls.append(("batch", [record["url"] for record in records]))
            return len(records)

        def add_content_source(self, **_source):
            pytest.fail("per-article writes are forbidden")

    ingestor = SourceIngestor(
        DatabaseBoundary(), News(), {"industry.example"},
    )

    assert ingestor.refresh_verified_news(["one", "two"], per_topic=1) == 2
    assert calls == [
        ("fetch", "one", 1),
        ("fetch", "two", 1),
        (
            "batch",
            [
                "https://industry.example/one",
                "https://industry.example/two",
            ],
        ),
    ]


def test_ingestor_deduplicates_urls_across_topics_before_batch():
    article = valid_article("https://industry.example/shared")

    class News:
        def get_trending_news(self, _topic, limit):
            assert limit == 2
            return [article, {"title": "Incomplete"}]

    class DatabaseBoundary:
        def __init__(self):
            self.batches = []

        def insert_verified_news_batch(self, records):
            self.batches.append(records)
            return len(records)

    database = DatabaseBoundary()
    ingestor = SourceIngestor(database, News(), {"industry.example"})

    assert ingestor.refresh_verified_news(["one", "two"], per_topic=2) == 1
    assert len(database.batches) == 1
    assert len(database.batches[0]) == 1


def test_verified_news_batch_rolls_back_on_second_insert(tmp_path):
    database = Database(tmp_path / "bot.db")
    second_url = "https://industry.example/two"
    with sqlite3.connect(database.db_path) as connection:
        connection.execute(f"""
            CREATE TRIGGER abort_second_news
            BEFORE INSERT ON content_sources
            WHEN NEW.url = '{second_url}'
            BEGIN
                SELECT RAISE(ABORT, 'blocked');
            END
        """)

    class News:
        def get_trending_news(self, topic, limit):
            assert limit == 1
            return [valid_article(f"https://industry.example/{topic}")]

    ingestor = SourceIngestor(
        database, News(), {"industry.example"},
    )

    with pytest.raises(sqlite3.IntegrityError):
        ingestor.refresh_verified_news(["one", "two"], per_topic=1)

    assert database.get_eligible_sources("verified_news") == []


@pytest.mark.parametrize(
    "mutation",
    [
        {"verified_by": "untrusted_importer"},
        {"trust_state": "pending"},
        {"source_type": "product_fact"},
        {"extra": "field"},
    ],
)
def test_verified_news_batch_requires_exact_ingestion_record(tmp_path, mutation):
    database = Database(tmp_path / "bot.db")
    article = valid_article("https://industry.example/one")
    details = SourceIngestor(
        database,
        object(),
        {"industry.example"},
    )._valid_article_details(article)
    record = {
        "source_type": "verified_news",
        "text": details["summary"],
        "url": details["url"],
        "metadata": {
            "title": details["title"],
            "summary": details["summary"],
            "published_at": details["published_at"],
            "source_name": details["source_name"],
        },
        "trust_state": "verified",
        "verified_by": "trusted_news_ingestion",
    }
    record.update(mutation)

    with pytest.raises(ValueError, match="^invalid_verified_news_batch$"):
        database.insert_verified_news_batch([record])

    assert database.get_eligible_sources("verified_news") == []


class NewsResponse:
    def __init__(self, payload, status_error=None):
        self.payload = payload
        self.status_error = status_error

    def raise_for_status(self):
        if self.status_error:
            raise self.status_error

    def json(self):
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


class NewsHttp:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@pytest.mark.parametrize(
    "outcome",
    [
        requests.exceptions.ConnectionError("SECRET_API_KEY SECRET_BODY"),
        NewsResponse({}, requests.exceptions.HTTPError("SECRET_BODY")),
        NewsResponse(ValueError("SECRET_BODY")),
        NewsResponse({"articles": "not-a-list"}),
    ],
)
def test_news_fetcher_outages_are_sanitized(outcome, caplog):
    http = NewsHttp(outcome)
    fetcher = NewsFetcher(
        api_key="SECRET_API_KEY",
        requests_client=http,
    )

    with caplog.at_level(logging.ERROR), pytest.raises(
        NewsFetchUnavailable,
        match="^news_fetch_unavailable$",
    ) as error:
        fetcher.get_trending_news("SECRET_QUERY", limit=1)

    rendered = caplog.text + str(error.value)
    assert "SECRET_API_KEY" not in rendered
    assert "SECRET_BODY" not in rendered
    assert "SECRET_QUERY" not in rendered


def test_news_fetcher_absent_key_remains_disabled_without_http():
    http = NewsHttp(pytest.fail)

    assert NewsFetcher(api_key="", requests_client=http).get_trending_news(
        "gym operations",
    ) == []
    assert http.calls == []
