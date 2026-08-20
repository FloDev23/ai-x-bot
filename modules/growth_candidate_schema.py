"""Canonical record and hard-filter rules for X growth candidates."""

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple


_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_]{1,15}")


def as_utc(value: datetime) -> datetime:
    """Normalize an application clock while preserving the existing UTC default."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_growth_datetime(value: Any) -> Optional[datetime]:
    """Parse a canonical aware ISO timestamp and normalize it to UTC."""
    if type(value) is not str or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (OverflowError, TypeError, ValueError):
        return None


def is_json_safe_mapping(value: Any) -> bool:
    """Return whether a value has the exact persisted JSON-object shape."""
    if type(value) is not dict:
        return False
    try:
        json.dumps(value, allow_nan=False)
    except (OverflowError, RecursionError, TypeError, ValueError):
        return False
    return True


def is_canonical_growth_profile(
    profile: Any,
    *,
    user_id: Optional[str] = None,
    username: Optional[str] = None,
) -> bool:
    """Validate the producer profile schema and optional persisted identity."""
    if not is_json_safe_mapping(profile):
        return False
    aliases = [profile[name] for name in ("id", "user_id") if name in profile]
    if (
        not aliases
        or any(type(alias) is not str or not alias for alias in aliases)
        or len(set(aliases)) != 1
        or (user_id is not None and aliases[0] != user_id)
    ):
        return False
    profile_username = profile.get("username")
    if (
        type(profile_username) is not str
        or _USERNAME_PATTERN.fullmatch(profile_username) is None
        or (username is not None and profile_username != username)
        or type(profile.get("description")) is not str
        or type(profile.get("protected")) is not bool
        or type(profile.get("followers_count")) is not int
        or profile["followers_count"] < 0
        or type(profile.get("following_count")) is not int
        or profile["following_count"] < 0
        or type(profile.get("spam_signals")) is not list
        or any(type(signal) is not str for signal in profile["spam_signals"])
        or (
            "follow_farming" in profile
            and type(profile.get("follow_farming")) is not bool
        )
    ):
        return False
    return True


def is_canonical_growth_latest_post(latest_post: Any) -> bool:
    """Validate the latest-post schema emitted by the read-only collector."""
    if not is_json_safe_mapping(latest_post):
        return False
    latest_id = latest_post.get("id")
    if (
        type(latest_id) is not str
        or not latest_id.isascii()
        or not latest_id.isdigit()
        or (
            "tweet_id" in latest_post
            and (
                type(latest_post.get("tweet_id")) is not str
                or latest_post["tweet_id"] != latest_id
            )
        )
        or type(latest_post.get("text")) is not str
        or parse_growth_datetime(latest_post.get("created_at")) is None
        or type(latest_post.get("lang")) is not str
        or type(latest_post.get("is_original")) is not bool
    ):
        return False
    return True


def evaluate_growth_candidate_filters(
    profile: Dict,
    latest_post: Optional[Dict],
    now: datetime,
) -> Tuple[bool, str]:
    """Apply the one canonical set of non-negotiable eligibility gates."""
    current_time = as_utc(now)
    if not isinstance(profile, dict) or profile.get("protected") is not False:
        return False, "protected_profile"
    latest_post = latest_post if isinstance(latest_post, dict) else {}
    activity_at = parse_growth_datetime(latest_post.get("created_at"))
    if (
        latest_post.get("is_original") is not True
        or activity_at is None
        or activity_at > current_time
        or current_time - activity_at > timedelta(days=30)
    ):
        return False, "no_original_post_within_30_days"
    bio = profile.get("description")
    text = latest_post.get("text")
    spam_signals = profile.get("spam_signals")
    followers = profile.get("followers_count")
    following = profile.get("following_count")
    if (
        type(bio) is not str
        or type(text) is not str
        or type(latest_post.get("lang")) is not str
        or type(spam_signals) is not list
        or any(type(signal) is not str for signal in spam_signals)
        or type(followers) is not int
        or followers < 0
        or type(following) is not int
        or following < 0
        or (
            "follow_farming" in profile
            and type(profile.get("follow_farming")) is not bool
        )
    ):
        return False, "malformed_candidate_record"
    if not bio.strip() and not text.strip():
        return False, "insufficient_bio_post_context"
    if spam_signals or profile.get("follow_farming") is True:
        return False, "spam_or_follow_farming_signals"
    raw_suppressed_until = profile.get("suppressed_until")
    suppressed_until = parse_growth_datetime(raw_suppressed_until)
    if raw_suppressed_until is not None and suppressed_until is None:
        return False, "malformed_candidate_record"
    if suppressed_until is not None and suppressed_until > current_time:
        return False, "suppressed_within_30_days"
    return True, "accepted"
