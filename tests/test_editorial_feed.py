import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from modules.editorial_feed import (
    FLEXDROPIN_EDITORIAL_FEED_URL,
    MAX_EDITORIAL_FEED_BYTES,
    EditorialFeedError,
    FlexDropinEditorialFeedClient,
    validate_editorial_feed,
)
from modules.database import Database
from modules.source_validation import is_complete_owned_blog_article


VALID_ITEM = {
    "slug": "gym-drop-ins-sell-single-classes",
    "url": (
        "https://flexdropin.com/blog/"
        "gym-drop-ins-sell-single-classes"
    ),
    "title": "Gym drop-ins: how to test demand",
    "summary": "A bounded operating guide for gym owners.",
    "published_at": "2026-08-20",
}
VALID_FEED = {
    "version": 1,
    "language": "en",
    "items": [VALID_ITEM],
}
TODAY = date(2026, 8, 24)


def clone_feed():
    return json.loads(json.dumps(VALID_FEED))


def test_schema_returns_canonical_record_with_stable_hash():
    records = validate_editorial_feed(clone_feed(), TODAY)

    canonical_json = json.dumps(
        VALID_ITEM,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert records == [{
        **VALID_ITEM,
        "content_hash": hashlib.sha256(canonical_json).hexdigest(),
    }]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda feed: feed.update(extra=True),
        lambda feed: feed.pop("language"),
        lambda feed: feed.update(version=2),
        lambda feed: feed.update(version=True),
        lambda feed: feed.update(language="it"),
        lambda feed: feed.update(items=[VALID_ITEM] * 101),
        lambda feed: feed["items"][0].update(extra=True),
        lambda feed: feed["items"][0].pop("title"),
        lambda feed: feed["items"][0].update(slug="Upper-Case"),
        lambda feed: feed["items"][0].update(slug="../escape"),
        lambda feed: feed["items"][0].update(
            url="https://flexdropin.com/blog/other-slug",
        ),
        lambda feed: feed["items"][0].update(
            url="https://flexdropin.example/blog/"
            "gym-drop-ins-sell-single-classes",
        ),
        lambda feed: feed["items"][0].update(
            url="https://www.flexdropin.com/blog/"
            "gym-drop-ins-sell-single-classes",
        ),
        lambda feed: feed["items"][0].update(
            url="https://flexdropin.com:443/blog/"
            "gym-drop-ins-sell-single-classes",
        ),
        lambda feed: feed["items"][0].update(
            url="http://flexdropin.com/blog/"
            "gym-drop-ins-sell-single-classes",
        ),
        lambda feed: feed["items"][0].update(
            url="https://flexdropin.com/blog/"
            "gym-drop-ins-sell-single-classes?ref=x",
        ),
        lambda feed: feed["items"][0].update(
            url="https://flexdropin.com/blog/"
            "gym-drop-ins-sell-single-classes#x",
        ),
        lambda feed: feed["items"][0].update(
            url="https://flexdropin.com/blog/gym%2Ddrop-ins",
        ),
        lambda feed: feed["items"][0].update(title="x" * 201),
        lambda feed: feed["items"][0].update(summary="x" * 1001),
        lambda feed: feed["items"][0].update(published_at="2026-02-30"),
        lambda feed: feed["items"][0].update(published_at="2026-08-25"),
        lambda feed: feed.update(items=[None]),
    ],
)
def test_schema_rejects_noncanonical_payloads(mutate):
    feed = clone_feed()
    mutate(feed)

    with pytest.raises(EditorialFeedError) as error:
        validate_editorial_feed(feed, TODAY)

    assert error.value.code == "invalid_feed_schema"
    assert str(error.value) == "invalid_feed_schema"


def test_schema_rejects_duplicate_slug_and_url():
    feed = clone_feed()
    feed["items"].append(dict(VALID_ITEM))

    with pytest.raises(EditorialFeedError, match="^invalid_feed_schema$"):
        validate_editorial_feed(feed, TODAY)


def test_schema_rejects_boolean_string_fields_and_recursive_payloads():
    feed = clone_feed()
    feed["items"][0]["title"] = True
    with pytest.raises(EditorialFeedError, match="^invalid_feed_schema$"):
        validate_editorial_feed(feed, TODAY)

    recursive = clone_feed()
    recursive["items"] = recursive
    with pytest.raises(EditorialFeedError, match="^invalid_feed_schema$"):
        validate_editorial_feed(recursive, TODAY)


class FakeResponse:
    def __init__(
        self,
        body,
        *,
        status_code=200,
        content_type="application/json",
        content_length=None,
        iter_error=None,
        close_error=None,
    ):
        self.status_code = status_code
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(
                len(body) if content_length is None else content_length
            ),
        }
        self.body = body
        self.iter_error = iter_error
        self.close_error = close_error
        self.close_calls = 0

    def iter_content(self, chunk_size):
        assert chunk_size > 0
        if self.iter_error is not None:
            raise self.iter_error
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class FakeHttp:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def encoded_feed(padding=0):
    return json.dumps(VALID_FEED).encode("utf-8") + (b" " * padding)


def test_transport_uses_only_fixed_bounded_request_and_closes_response():
    response = FakeResponse(encoded_feed())
    http = FakeHttp(response)

    result = FlexDropinEditorialFeedClient(
        http,
        now_fn=lambda: TODAY,
    ).fetch()

    assert result[0]["slug"] == VALID_ITEM["slug"]
    assert http.calls == [(
        FLEXDROPIN_EDITORIAL_FEED_URL,
        {
            "timeout": (5, 10),
            "allow_redirects": False,
            "stream": True,
        },
    )]
    assert response.close_calls == 1


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (FakeResponse(encoded_feed(), status_code=301), "invalid_feed_response"),
        (FakeResponse(encoded_feed(), status_code=500), "invalid_feed_response"),
        (FakeResponse(encoded_feed(), content_type="text/html"), "invalid_feed_response"),
        (FakeResponse(encoded_feed(), content_length="-1"), "invalid_feed_response"),
        (FakeResponse(encoded_feed(), content_length="many"), "invalid_feed_response"),
        (FakeResponse(encoded_feed(), content_length=MAX_EDITORIAL_FEED_BYTES + 1), "feed_too_large"),
        (FakeResponse(b"\xff"), "invalid_feed_response"),
        (FakeResponse(b"{"), "invalid_feed_response"),
        (FakeResponse(encoded_feed(), iter_error=RuntimeError("SECRET_BODY")), "feed_transport_failed"),
    ],
)
def test_transport_failures_are_sanitized_and_close_once(response, code):
    http = FakeHttp(response)

    with pytest.raises(EditorialFeedError) as error:
        FlexDropinEditorialFeedClient(http, now_fn=lambda: TODAY).fetch()

    assert error.value.code == code
    assert str(error.value) == code
    assert "SECRET_BODY" not in str(error.value)
    assert response.close_calls == 1


def test_transport_requires_content_length_header():
    response = FakeResponse(encoded_feed())
    response.headers.pop("Content-Length")

    with pytest.raises(
        EditorialFeedError,
        match="^invalid_feed_response$",
    ):
        FlexDropinEditorialFeedClient(
            FakeHttp(response),
            now_fn=lambda: TODAY,
        ).fetch()

    assert response.close_calls == 1


def test_transport_enforces_streamed_limit_and_accepts_exact_limit():
    oversized = FakeResponse(
        encoded_feed(MAX_EDITORIAL_FEED_BYTES),
        content_length=MAX_EDITORIAL_FEED_BYTES,
    )
    with pytest.raises(EditorialFeedError, match="^feed_too_large$"):
        FlexDropinEditorialFeedClient(
            FakeHttp(oversized), now_fn=lambda: TODAY,
        ).fetch()

    base = encoded_feed()
    exact = FakeResponse(
        base + b" " * (MAX_EDITORIAL_FEED_BYTES - len(base)),
    )
    records = FlexDropinEditorialFeedClient(
        FakeHttp(exact), now_fn=lambda: TODAY,
    ).fetch()
    assert records[0]["slug"] == VALID_ITEM["slug"]


def test_transport_swallows_close_error_without_leaking_it():
    response = FakeResponse(
        encoded_feed(),
        close_error=RuntimeError("SECRET_CLOSE"),
    )

    records = FlexDropinEditorialFeedClient(
        FakeHttp(response), now_fn=lambda: TODAY,
    ).fetch()

    assert records[0]["slug"] == VALID_ITEM["slug"]
    assert response.close_calls == 1


def valid_database_records():
    feed = clone_feed()
    feed["items"].append({
        "slug": "drop-in-vs-gym-membership",
        "url": "https://flexdropin.com/blog/drop-in-vs-gym-membership",
        "title": "Drop-in vs gym membership",
        "summary": "A practical comparison for flexible training.",
        "published_at": "2026-08-04",
    })
    return validate_editorial_feed(feed, TODAY)


def test_database_imports_complete_owned_blog_articles(tmp_path):
    database = Database(tmp_path / "bot.db")

    counts = database.import_owned_blog_articles(valid_database_records())

    assert counts == {"inserted": 2, "updated": 0, "unchanged": 0}
    sources = database.get_eligible_sources("owned_blog_article")
    assert len(sources) == 2
    source = next(item for item in sources if item["url"] == VALID_ITEM["url"])
    assert source["source_type"] == "owned_blog_article"
    assert source["trust_state"] == "verified"
    assert source["verified_by"] == "flexdropin_editorial_feed"
    assert source["expires_at"] is None
    assert source["text"] == (
        source["metadata"]["title"]
        + "\n"
        + source["metadata"]["summary"]
    )
    assert source["metadata"] == {
        "title": VALID_ITEM["title"],
        "summary": VALID_ITEM["summary"],
        "published_at": VALID_ITEM["published_at"],
        "source_name": "FlexDropin Blog",
        "slug": VALID_ITEM["slug"],
        "feed_version": 1,
        "content_hash": source["metadata"]["content_hash"],
    }
    assert is_complete_owned_blog_article(source) is True


def test_database_import_is_idempotent_updates_changed_and_retains_missing(tmp_path):
    database = Database(tmp_path / "bot.db")
    records = valid_database_records()
    database.import_owned_blog_articles(records)
    original = database.get_eligible_sources("owned_blog_article")
    created_by_url = {source["url"]: source["created_at"] for source in original}

    assert database.import_owned_blog_articles(records) == {
        "inserted": 0,
        "updated": 0,
        "unchanged": 2,
    }

    changed_feed = clone_feed()
    changed_feed["items"][0]["summary"] = "A revised bounded guide."
    changed = validate_editorial_feed(changed_feed, TODAY)
    assert database.import_owned_blog_articles(changed) == {
        "inserted": 0,
        "updated": 1,
        "unchanged": 0,
    }
    after = database.get_eligible_sources("owned_blog_article")
    assert len(after) == 2
    changed_source = next(item for item in after if item["url"] == VALID_ITEM["url"])
    assert changed_source["metadata"]["summary"] == "A revised bounded guide."
    assert changed_source["created_at"] == created_by_url[VALID_ITEM["url"]]


def test_database_import_conflict_rolls_back_entire_batch(tmp_path):
    database = Database(tmp_path / "bot.db")
    records = valid_database_records()
    database.add_content_source(
        "verified_news",
        "Existing unrelated source",
        url=records[1]["url"],
    )

    with pytest.raises(ValueError, match="^owned_blog_source_conflict$"):
        database.import_owned_blog_articles(records)

    assert database.content_source_exists(records[0]["url"]) is False


def test_database_import_does_not_reenable_or_overwrite_revoked_article(tmp_path):
    database = Database(tmp_path / "bot.db")
    original = valid_database_records()[:1]
    database.import_owned_blog_articles(original)
    with sqlite3.connect(database.db_path) as connection:
        connection.execute(
            "UPDATE content_sources SET trust_state = 'pending' WHERE url = ?",
            (original[0]["url"],),
        )

    changed_feed = clone_feed()
    changed_feed["items"][0]["summary"] = "Changed after manual revocation."
    changed = validate_editorial_feed(changed_feed, TODAY)

    assert database.import_owned_blog_articles(changed) == {
        "inserted": 0,
        "updated": 0,
        "unchanged": 1,
    }
    with sqlite3.connect(database.db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM content_sources WHERE url = ?",
            (original[0]["url"],),
        ).fetchone()
    assert row["trust_state"] == "pending"
    assert json.loads(row["metadata_json"])["summary"] == VALID_ITEM["summary"]
    assert database.get_eligible_sources("owned_blog_article") == []


def test_database_import_trigger_failure_rolls_back_first_insert(tmp_path):
    database = Database(tmp_path / "bot.db")
    records = valid_database_records()
    with sqlite3.connect(database.db_path) as connection:
        connection.execute("""
            CREATE TRIGGER abort_second_owned_blog
            BEFORE INSERT ON content_sources
            WHEN NEW.url = 'https://flexdropin.com/blog/drop-in-vs-gym-membership'
            BEGIN
                SELECT RAISE(ABORT, 'blocked');
            END
        """)

    with pytest.raises(sqlite3.IntegrityError):
        database.import_owned_blog_articles(records)

    assert database.content_source_exists(records[0]["url"]) is False
    assert database.content_source_exists(records[1]["url"]) is False


def test_database_import_is_serialized_across_connections(tmp_path):
    path = tmp_path / "bot.db"
    Database(path)
    records = valid_database_records()[:1]

    def import_once():
        return Database(path).import_owned_blog_articles(records)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: import_once(), range(2)))

    assert sorted(result["inserted"] for result in results) == [0, 1]
    with sqlite3.connect(path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM content_sources WHERE url = ?",
            (records[0]["url"],),
        ).fetchone()[0]
    assert count == 1


def test_database_import_rejects_ambiguous_existing_url_rows(tmp_path):
    database = Database(tmp_path / "bot.db")
    record = valid_database_records()[0]
    for _index in range(2):
        database.add_content_source(
            "owned_blog_article",
            "Legacy duplicate",
            url=record["url"],
            verified_by="flexdropin_editorial_feed",
        )

    with pytest.raises(ValueError, match="^owned_blog_source_conflict$"):
        database.import_owned_blog_articles([record])


def test_database_import_does_not_coerce_hostile_values(tmp_path):
    class HostileValue:
        def __str__(self):
            raise RuntimeError("SECRET_VALUE")

    database = Database(tmp_path / "bot.db")
    records = valid_database_records()
    records[0]["title"] = HostileValue()

    with pytest.raises(ValueError, match="^invalid_owned_blog_import$"):
        database.import_owned_blog_articles(records)

    assert database.get_eligible_sources("owned_blog_article") == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda record: record.update(content_hash="0" * 64),
        lambda record: record.update(url="https://example.com/blog/unsafe"),
        lambda record: record.update(published_at="not-a-date"),
        lambda record: record.update(extra="field"),
        lambda record: record.update(title=True),
    ],
)
def test_database_import_rejects_malformed_records_before_mutation(
    tmp_path,
    mutate,
):
    database = Database(tmp_path / "bot.db")
    records = valid_database_records()
    mutate(records[1])

    with pytest.raises(ValueError, match="^invalid_owned_blog_import$"):
        database.import_owned_blog_articles(records)

    assert database.get_eligible_sources("owned_blog_article") == []
