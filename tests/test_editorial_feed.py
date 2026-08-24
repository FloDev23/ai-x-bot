import hashlib
import json
from datetime import date

import pytest

from modules.editorial_feed import (
    FLEXDROPIN_EDITORIAL_FEED_URL,
    MAX_EDITORIAL_FEED_BYTES,
    EditorialFeedError,
    FlexDropinEditorialFeedClient,
    validate_editorial_feed,
)


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
