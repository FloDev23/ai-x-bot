"""Shared fail-closed validation for editorial source records."""
import hashlib
import json
from datetime import date, datetime
import re
from typing import Mapping
from urllib.parse import urlparse


_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_OWNED_BLOG_SLUG = re.compile(
    r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    flags=re.ASCII,
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$", flags=re.ASCII)
_OWNED_BLOG_METADATA_FIELDS = frozenset({
    "title",
    "summary",
    "published_at",
    "source_name",
    "slug",
    "feed_version",
    "content_hash",
})


def is_safe_https_url(url, allowed_hosts=None) -> bool:
    if not isinstance(url, str) or not url.strip():
        return False
    clean = url.strip()
    if any(ord(character) <= 32 or ord(character) == 127 for character in clean):
        return False
    try:
        parsed = urlparse(clean)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
        username = parsed.username
        password = parsed.password
    except (TypeError, ValueError):
        return False
    if (
        scheme != "https"
        or not hostname
        or port not in (None, 443)
        or username is not None
        or password is not None
    ):
        return False
    host = hostname.lower()
    if host.endswith("."):
        host = host[:-1]
    if not host or host.endswith("."):
        return False
    if len(host) > 253 or not host.isascii():
        return False
    labels = host.split(".")
    if len(labels) < 2 or any(not _DNS_LABEL.fullmatch(label) for label in labels):
        return False
    if allowed_hosts is None:
        return True
    allowed = {
        value.strip().lower().rstrip(".")
        for value in allowed_hosts
        if isinstance(value, str) and value.strip()
    }
    return any(host == domain or host.endswith("." + domain) for domain in allowed)


def is_complete_verified_news(source) -> bool:
    """Return true only for attributable, planning-safe verified news."""
    if not isinstance(source, Mapping):
        return False
    if source.get("source_type") != "verified_news":
        return False
    if source.get("trust_state") != "verified":
        return False
    text = source.get("text")
    if not isinstance(text, str) or not text.strip():
        return False
    if not is_safe_https_url(source.get("url")):
        return False
    metadata = source.get("metadata")
    if not isinstance(metadata, Mapping):
        return False
    for field in ("title", "summary", "published_at", "source_name"):
        value = metadata.get(field)
        if not isinstance(value, str) or not value.strip():
            return False
    published_at = metadata["published_at"].strip()
    try:
        date.fromisoformat(published_at)
    except ValueError:
        try:
            datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        except ValueError:
            return False
    return True


def is_complete_owned_blog_article(source) -> bool:
    """Accept only one exact official-feed row suitable for planning."""
    if not isinstance(source, Mapping):
        return False
    if source.get("source_type") != "owned_blog_article":
        return False
    if source.get("trust_state") != "verified":
        return False
    if source.get("verified_by") != "flexdropin_editorial_feed":
        return False
    text = source.get("text")
    url = source.get("url")
    metadata = source.get("metadata")
    if type(text) is not str or type(url) is not str:
        return False
    if not isinstance(metadata, Mapping):
        return False
    if frozenset(metadata) != _OWNED_BLOG_METADATA_FIELDS:
        return False

    title = metadata.get("title")
    summary = metadata.get("summary")
    published_at = metadata.get("published_at")
    slug = metadata.get("slug")
    content_hash = metadata.get("content_hash")
    if any(
        type(value) is not str or not value or value != value.strip()
        for value in (title, summary, published_at, slug, content_hash)
    ):
        return False
    if len(title) > 200 or len(summary) > 1000:
        return False
    if not _OWNED_BLOG_SLUG.fullmatch(slug):
        return False
    if not _SHA256.fullmatch(content_hash):
        return False
    if metadata.get("source_name") != "FlexDropin Blog":
        return False
    if type(metadata.get("feed_version")) is not int:
        return False
    if metadata.get("feed_version") != 1:
        return False
    expected_url = f"https://flexdropin.com/blog/{slug}"
    if url != expected_url or not is_safe_https_url(url, {"flexdropin.com"}):
        return False
    try:
        parsed_date = date.fromisoformat(published_at)
    except (TypeError, ValueError):
        return False
    if parsed_date.isoformat() != published_at or parsed_date > date.today():
        return False
    if text != title + "\n" + summary:
        return False

    public_item = {
        "slug": slug,
        "url": url,
        "title": title,
        "summary": summary,
        "published_at": published_at,
    }
    try:
        canonical = json.dumps(
            public_item,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError):
        return False
    return hashlib.sha256(canonical).hexdigest() == content_hash
