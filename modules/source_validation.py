"""Shared fail-closed validation for editorial source records."""
from datetime import date, datetime
import re
from typing import Mapping
from urllib.parse import urlparse


_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


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
