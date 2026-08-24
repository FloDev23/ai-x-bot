import hashlib
import json
import re
from datetime import date, datetime, timezone
from urllib.parse import urlsplit


FLEXDROPIN_EDITORIAL_FEED_URL = (
    "https://flexdropin.com/api/editorial-feed"
)
MAX_EDITORIAL_FEED_BYTES = 256 * 1024
MAX_EDITORIAL_FEED_ITEMS = 100

_TOP_LEVEL_FIELDS = frozenset({"version", "language", "items"})
_ITEM_FIELDS = frozenset({
    "slug",
    "url",
    "title",
    "summary",
    "published_at",
})
_SLUG_PATTERN = re.compile(
    r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    flags=re.ASCII,
)
_ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$", flags=re.ASCII)


class EditorialFeedError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _invalid_schema():
    raise EditorialFeedError("invalid_feed_schema") from None


def _canonical_json(value):
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        _invalid_schema()


def _canonical_text(value, maximum):
    if type(value) is not str:
        _invalid_schema()
    if not value or value != value.strip() or len(value) > maximum:
        _invalid_schema()
    return value


def _canonical_date(value, today):
    if type(value) is not str or not _ISO_DATE_PATTERN.fullmatch(value):
        _invalid_schema()
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        _invalid_schema()
    if parsed.isoformat() != value or parsed > today:
        _invalid_schema()
    return value


def _canonical_url(value, slug):
    if type(value) is not str:
        _invalid_schema()
    expected = f"https://flexdropin.com/blog/{slug}"
    if value != expected:
        _invalid_schema()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        _invalid_schema()
    if (
        parsed.scheme != "https"
        or parsed.hostname != "flexdropin.com"
        or parsed.netloc != "flexdropin.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != f"/blog/{slug}"
        or parsed.query
        or parsed.fragment
    ):
        _invalid_schema()
    return value


def validate_editorial_feed(payload, today):
    """Return canonical feed records or one sanitized validation error."""
    try:
        if type(payload) is not dict or type(today) is not date:
            _invalid_schema()
        _canonical_json(payload)
        if frozenset(payload) != _TOP_LEVEL_FIELDS:
            _invalid_schema()
        if type(payload["version"]) is not int or payload["version"] != 1:
            _invalid_schema()
        if type(payload["language"]) is not str or payload["language"] != "en":
            _invalid_schema()
        items = payload["items"]
        if type(items) is not list or len(items) > MAX_EDITORIAL_FEED_ITEMS:
            _invalid_schema()

        seen_slugs = set()
        seen_urls = set()
        records = []
        for item in items:
            if type(item) is not dict or frozenset(item) != _ITEM_FIELDS:
                _invalid_schema()
            slug = _canonical_text(item["slug"], 200)
            if not _SLUG_PATTERN.fullmatch(slug):
                _invalid_schema()
            url = _canonical_url(item["url"], slug)
            title = _canonical_text(item["title"], 200)
            summary = _canonical_text(item["summary"], 1000)
            published_at = _canonical_date(item["published_at"], today)
            if slug in seen_slugs or url in seen_urls:
                _invalid_schema()
            seen_slugs.add(slug)
            seen_urls.add(url)

            public_record = {
                "slug": slug,
                "url": url,
                "title": title,
                "summary": summary,
                "published_at": published_at,
            }
            digest = hashlib.sha256(
                _canonical_json(public_record).encode("utf-8")
            ).hexdigest()
            records.append({**public_record, "content_hash": digest})
        return records
    except EditorialFeedError:
        raise
    except Exception:
        _invalid_schema()


class FlexDropinEditorialFeedClient:
    def __init__(self, http, now_fn=None):
        self.http = http
        self.now_fn = now_fn or (
            lambda: datetime.now(timezone.utc).date()
        )

    def fetch(self):
        response = None
        try:
            response = self.http.get(
                FLEXDROPIN_EDITORIAL_FEED_URL,
                headers={"Accept-Encoding": "identity"},
                timeout=(5, 10),
                allow_redirects=False,
                stream=True,
            )
            if type(response.status_code) is not int or response.status_code != 200:
                raise EditorialFeedError("invalid_feed_response")

            content_type = response.headers.get("Content-Type")
            if (
                type(content_type) is not str
                or content_type.split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                raise EditorialFeedError("invalid_feed_response")

            raw_length = response.headers.get("Content-Length")
            if (
                type(raw_length) is not str
                or not raw_length
                or len(raw_length) > 19
                or not raw_length.isascii()
                or not raw_length.isdecimal()
            ):
                raise EditorialFeedError("invalid_feed_response")
            declared_length = int(raw_length)
            if declared_length > MAX_EDITORIAL_FEED_BYTES:
                raise EditorialFeedError("feed_too_large")

            chunks = []
            received = 0
            for chunk in response.iter_content(chunk_size=16 * 1024):
                if not chunk:
                    continue
                if type(chunk) is not bytes:
                    raise EditorialFeedError("invalid_feed_response")
                received += len(chunk)
                if received > MAX_EDITORIAL_FEED_BYTES:
                    raise EditorialFeedError("feed_too_large")
                chunks.append(chunk)

            try:
                decoded = b"".join(chunks).decode("utf-8")
                payload = json.loads(decoded)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise EditorialFeedError("invalid_feed_response") from None
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
