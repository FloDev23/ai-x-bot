"""Canonical record and hard-filter rules for X growth candidates."""

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple


_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_]{1,15}")
GROWTH_RELEVANCE_POLICY = "managed_fitness_facility_us_priority_v3"

_US_STATE_NAMES = (
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming", "district of columbia",
)
_US_STATE_CODES = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI",
    "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY", "DC",
)
_US_LOCATION_PATTERN = re.compile(
    r"\b(?:united states(?: of america)?|usa)\b|"
    r"(?<!\w)u\.s(?:\.a)?\.?(?!\w)|"
    + r"\b(?:" + "|".join(re.escape(name) for name in _US_STATE_NAMES) + r")\b|"
    + r"(?:,\s*|^)(?:" + "|".join(_US_STATE_CODES) + r")(?:\s+\d{5}(?:-\d{4})?)?\s*$",
    re.IGNORECASE,
)

_FACILITY_MANAGEMENT_ROLE_PATTERN = re.compile(
    r"\b(?:co[- ]?(?:owners?|founders?)|owners?|founders?|managers?|operators?|directors?|"
    r"head coaches?)\b",
    re.IGNORECASE,
)
_UNRELATED_ROLE_MODIFIER_PATTERN = re.compile(
    r"\b(?:product|project|account|content|marketing|social media) managers?$|"
    r"\bcamera operators?$",
    re.IGNORECASE,
)
_FITNESS_DISCIPLINE_FRAGMENT = (
    r"(?:crossfit|hyrox|athx|functional (?:training|fitness)|calisthenics|"
    r"weightlifting|powerlifting|bodybuilding|circuit training|bootcamp|hiit|"
    r"trx|fitcamp|strength (?:and|&) conditioning|spinning|indoor cycling|"
    r"rowing|cardio fitness|outdoor running|outdoor fitness|yoga|pilates|barre|"
    r"meditation|stretching|postural gymnastics|zumba|dance fitness|aqua zumba|"
    r"boxing|kickboxing|mma|muay thai|karate|bjj|jiu[ -]?jitsu|martial arts|"
    r"swimming|aqua fitness|hydrospinning|aquatic|climbing|bouldering|"
    r"pole dance|parkour|skateboard|personal training)"
)
_FITNESS_VENUE_FRAGMENT = (
    r"(?:training )?(?:studios?|gyms?|boxes?|centers?|centres?|facilit(?:y|ies)|"
    r"clubs?|schools?|academ(?:y|ies)|dojos?)"
)
_QUALIFIED_FITNESS_FACILITY_FRAGMENT = (
    _FITNESS_DISCIPLINE_FRAGMENT + r"\s+" + _FITNESS_VENUE_FRAGMENT
)
_FITNESS_FACILITY_REFERENCE_PATTERN = re.compile(
    rf"\b(?:gyms?|dojos?|health clubs?|"
    rf"fitness (?:studios?|centers?|centres?|clubs?|facilit(?:y|ies))|"
    rf"crossfit affiliates?|{_QUALIFIED_FITNESS_FACILITY_FRAGMENT})\b",
    re.IGNORECASE,
)
_FITNESS_FACILITY_IDENTITY_PATTERN = re.compile(
    rf"\b(?:dojos?|fitness (?:studios?|centers?|centres?|clubs?|facilit(?:y|ies))|"
    rf"crossfit affiliates?|{_QUALIFIED_FITNESS_FACILITY_FRAGMENT}|"
    rf"(?:official|independent|community|boutique|local|24[ /-]?7) gyms?|"
    rf"gyms? (?:in|based in|located in))\b",
    re.IGNORECASE,
)
_NON_MANAGEMENT_PERSON_PATTERN = re.compile(
    r"(?:^|[|,/·•]\s*)(?:(?:crossfit|hyrox|fitness|yoga|pilates|bjj|"
    r"boxing|weightlifting)\s+)?\b(?:athlete|member|enthusiast|creator|"
    r"influencer|personal trainer|coach|instructor|teacher)\b|"
    r"\b(?:athlete|member|personal trainer|coach|instructor|teacher) "
    r"(?:at|for|with)\b|\b(?:gyms?|boxes?|studios?) "
    r"(?:members?|athletes?|enthusiasts?)\b",
    re.IGNORECASE,
)


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


def classify_growth_market(profile: Any) -> str:
    """Classify a public profile location without excluding global accounts."""
    if not isinstance(profile, dict):
        return "unknown"
    location = profile.get("location")
    if location is None or (type(location) is str and not location.strip()):
        return "unknown"
    if type(location) is not str:
        return "unknown"
    return "usa" if _US_LOCATION_PATTERN.search(location.strip()) else "other"


def has_managed_fitness_facility_context(profile: Any) -> bool:
    """Require an explicit physical fitness facility or its management role."""
    if not isinstance(profile, dict):
        return False
    description = profile.get("description")
    if type(description) is not str or not description.strip():
        return False
    facility_matches = list(
        _FITNESS_FACILITY_REFERENCE_PATTERN.finditer(description)
    )
    role_matches = list(_FACILITY_MANAGEMENT_ROLE_PATTERN.finditer(description))
    for role_match in role_matches:
        role_prefix = description[max(0, role_match.start() - 24):role_match.end()]
        if _UNRELATED_ROLE_MODIFIER_PATTERN.search(role_prefix):
            continue
        for facility_match in facility_matches:
            if facility_match.end() <= role_match.start():
                bridge = description[facility_match.end():role_match.start()]
                if (
                    len(re.findall(r"[A-Za-z0-9]+", bridge)) <= 3
                    and re.search(r"[.!?;]", bridge) is None
                    and re.search(
                        r"\b(?:members?|athletes?|enthusiasts?|creators?)\b",
                        bridge,
                        re.IGNORECASE,
                    ) is None
                ):
                    return True
            elif role_match.end() <= facility_match.start():
                bridge = description[role_match.end():facility_match.start()]
                role_text = role_match.group(0).lower()
                linked = re.search(
                    r"\b(?:of|at|for)\b|[|/@,:\-–—]",
                    bridge,
                    re.IGNORECASE,
                )
                if (
                    len(re.findall(r"[A-Za-z0-9]+", bridge)) <= 5
                    and re.search(r"[.!?;]", bridge) is None
                    and linked is not None
                    and not ("operator" in role_text and "of" not in bridge.lower())
                    and re.match(
                        r"\s+(?:members?|athletes?|enthusiasts?)\b",
                        description[facility_match.end():],
                        re.IGNORECASE,
                    ) is None
                ):
                    return True
    return bool(
        _FITNESS_FACILITY_IDENTITY_PATTERN.search(description)
        and not _NON_MANAGEMENT_PERSON_PATTERN.search(description)
    )


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
    if not has_managed_fitness_facility_context(profile):
        return False, "no_managed_fitness_facility_context"
    return True, "accepted"
