"""Safe primitives for Telegram-only, manually published X reply drafts."""

import re
import unicodedata
from typing import Optional
from urllib.parse import urlencode


_OPERATOR_REASON_CODES = frozenset({
    "gym_owner",
    "empty_capacity",
    "booking_problem",
    "fitness_operations",
})
_END_USER_REASON_CODES = frozenset({
    "explicit_intent",
    "travel_context",
    "day_pass_model",
    "drop_in",
    "urgency",
    "discipline_match",
})
_URL_PATTERN = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])",
    re.IGNORECASE,
)
_HASHTAG_PATTERN = re.compile(r"(?<!\w)#[A-Z0-9_]+", re.IGNORECASE)
_MENTION_PATTERN = re.compile(r"(?<!\w)@[A-Z0-9_]+", re.IGNORECASE)
_BRAND_PATTERN = re.compile(r"\bflex[\s_-]*drop[\s_-]*in\b", re.IGNORECASE)
_PROMOTION_PATTERN = re.compile(
    r"\b(?:download(?:\s+the)?\s+app|sign\s+up|try\s+our\s+app|"
    r"visit\s+our\s+(?:site|website)|book\s+with\s+us|dm\s+us|"
    r"learn\s+more|follow\s+us)\b",
    re.IGNORECASE,
)
_PROMPT_INJECTION_PATTERN = re.compile(
    r"\b(?:ignore\s+(?:all\s+|any\s+|the\s+|previous\s+|prior\s+)*"
    r"instructions?|reveal\s+(?:the\s+)?system\s+prompt|system\s+prompt|"
    r"developer\s+message)\b",
    re.IGNORECASE,
)
_HIGH_RISK_PATTERN = re.compile(
    r"\b(?:diagnos(?:e|is|ing)|prescri(?:be|ption)|medical\s+advice|"
    r"legal\s+advice|lawyer|attorney|emergency|suicid(?:e|al)|self[- ]harm|"
    r"overdose|crisis\s+line)\b",
    re.IGNORECASE,
)
_TWEET_ID_PATTERN = re.compile(r"[1-9][0-9]{0,19}")


def classify_reply_segment(reason_codes: object) -> Optional[str]:
    """Map trusted Growth Digest reason codes to the approved audience split."""
    if not isinstance(reason_codes, (list, tuple, set, frozenset)):
        return None
    normalized = {
        value for value in reason_codes
        if isinstance(value, str) and value
    }
    if normalized & _OPERATOR_REASON_CODES:
        return "operator"
    if normalized & _END_USER_REASON_CODES:
        return "end_user"
    return None


def normalize_and_validate_reply(value: object) -> Optional[str]:
    """Return normalized safe reply copy, or ``None`` when it fails closed."""
    if not isinstance(value, str):
        return None
    try:
        if any(unicodedata.category(character).startswith("C") for character in value):
            return None
        normalized = unicodedata.normalize("NFC", value)
    except (TypeError, ValueError, UnicodeError):
        return None
    normalized = " ".join(normalized.strip().split())
    if not 30 <= len(normalized) <= 256:
        return None
    if any(pattern.search(normalized) for pattern in (
        _URL_PATTERN,
        _EMAIL_PATTERN,
        _HASHTAG_PATTERN,
        _MENTION_PATTERN,
        _BRAND_PATTERN,
        _PROMOTION_PATTERN,
        _PROMPT_INJECTION_PATTERN,
        _HIGH_RISK_PATTERN,
    )):
        return None
    return normalized


def build_reply_web_intent(tweet_id: object, reply_text: object) -> Optional[str]:
    """Build a manual X composer URL without using X API credentials."""
    if not isinstance(tweet_id, str) or _TWEET_ID_PATTERN.fullmatch(tweet_id) is None:
        return None
    reply = normalize_and_validate_reply(reply_text)
    if reply is None:
        return None
    return "https://x.com/intent/tweet?" + urlencode({
        "in_reply_to": tweet_id,
        "text": reply,
    })
