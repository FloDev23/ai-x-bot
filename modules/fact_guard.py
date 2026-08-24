"""Fail-closed validation for factual claims in generated posts."""
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List

from modules.source_validation import is_complete_owned_blog_article


@dataclass(frozen=True)
class FactCheckResult:
    approved: bool
    reasons: List[str]


REQUIRED_SOURCE_TYPES = {
    "first_person": {"founder_note"},
    "number": {
        "founder_note",
        "product_fact",
        "verified_news",
        "owned_blog_article",
    },
    "product_claim": {"product_fact"},
    "incident": {"founder_note"},
    "medical": {"verified_news"},
    "testimonial": {"founder_note"},
    "named_entity": {
        "founder_note",
        "product_fact",
        "verified_news",
        "owned_blog_article",
    },
    "named_current_event": {"verified_news"},
}
SUPPORTED_CLAIM_TYPES = frozenset(REQUIRED_SOURCE_TYPES)
SUPPORTED_SOURCE_TYPES = frozenset().union(*REQUIRED_SOURCE_TYPES.values())

_NUMBER_RE = re.compile(
    r"(?<![\w])"
    r"(?P<sign>[+\-\u2212])?"
    r"(?P<value>\d+(?:[.,]\d+)?)"
    r"(?:[\s\u00a0\u202f]*(?P<scale>[KMB]|(?i:thousand|million|billion|mila|milione|milioni|miliardo|miliardi))\b)?"
    r"[\s\u00a0\u202f]*(?P<percent>%|(?i:percent|percento)\b)?",
)
_RANGE_SEPARATOR_RE = re.compile(r"[\s\u00a0\u202f]*[-\u2013\u2014][\s\u00a0\u202f]*")
_NUMBER_SCALES = {
    "k": "k",
    "thousand": "k",
    "mila": "k",
    "m": "m",
    "million": "m",
    "milione": "m",
    "milioni": "m",
    "b": "b",
    "billion": "b",
    "miliardo": "b",
    "miliardi": "b",
}

INCIDENT_SUBTYPES = frozenset({
    "payment",
    "privacy",
    "security",
    "customer_impacting",
})


def _parse_timestamp(raw):
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def source_is_expired(source, now=None):
    """Treat malformed expiry data as expired so it cannot authorize a claim."""
    if not isinstance(source, dict):
        return True
    if "expires_at" not in source or source.get("expires_at") is None:
        return False
    expires_at = _parse_timestamp(source.get("expires_at"))
    if expires_at is None:
        return True
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return expires_at <= moment.astimezone(timezone.utc)


def valid_source_id(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    if isinstance(value, str):
        return bool(value.strip())
    return False


def normalize_claim_type(value):
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    return normalized if normalized in SUPPORTED_CLAIM_TYPES else None


def normalize_incident_subtype(value):
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized if normalized in INCIDENT_SUBTYPES else None


def _source_is_valid(source, allowed_types):
    if not isinstance(source, dict):
        return False
    if not valid_source_id(source.get("id")):
        return False
    if source.get("trust_state") != "verified":
        return False
    if source.get("source_type") not in allowed_types:
        return False
    if source_is_expired(source):
        return False
    if "verified_at" in source and source.get("verified_at") is not None:
        if _parse_timestamp(source.get("verified_at")) is None:
            return False
    if "metadata" in source and not isinstance(source.get("metadata"), dict):
        return False
    if source.get("source_type") == "founder_note":
        return source.get("metadata", {}).get("publishable") is True
    if source.get("source_type") == "owned_blog_article":
        return is_complete_owned_blog_article(source)
    return True


def _canonical_number(sign, value, scale, percent):
    if "," in value and "." not in value:
        left, right = value.split(",", 1)
        value = left + right if len(right) == 3 else left + "." + right
    else:
        value = value.replace(",", "")
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    suffix = _NUMBER_SCALES.get((scale or "").lower(), "")
    if sign in {"-", "\u2212"}:
        prefix = "-"
    elif sign == "+":
        prefix = "+"
    else:
        prefix = ""
    return prefix + value + suffix + ("%" if percent else "")


def numeric_occurrences(value):
    if not isinstance(value, str):
        return ()
    matches = list(_NUMBER_RE.finditer(value))
    tokens = []
    for index, match in enumerate(matches):
        scale = match.group("scale")
        percent = match.group("percent")
        if index + 1 < len(matches):
            following = matches[index + 1]
            between = value[match.end():following.start()]
            if _RANGE_SEPARATOR_RE.fullmatch(between):
                scale = scale or following.group("scale")
                percent = percent or following.group("percent")
        tokens.append(_canonical_number(
            match.group("sign"),
            match.group("value"),
            scale,
            percent,
        ))
    return tuple(tokens)


def numeric_tokens(value):
    """Return canonical signed numeric facts found in English or Italian text."""
    return set(numeric_occurrences(value))


_numeric_tokens = numeric_tokens


def _metadata_numeric_tokens(value):
    if isinstance(value, bool) or value is None:
        return set()
    if isinstance(value, str):
        return _numeric_tokens(value)
    if isinstance(value, (int, float)):
        return _numeric_tokens(str(value))
    if isinstance(value, dict):
        return set().union(*(
            _metadata_numeric_tokens(item) for item in value.values()
        )) if value else set()
    if isinstance(value, (list, tuple)):
        return set().union(*(
            _metadata_numeric_tokens(item) for item in value
        )) if value else set()
    return set()


def _supported_numeric_tokens(sources):
    supported = set()
    for source in sources:
        if not _source_is_valid(source, SUPPORTED_SOURCE_TYPES):
            continue
        supported.update(_numeric_tokens(source.get("text")))
        supported.update(_metadata_numeric_tokens(source.get("metadata")))
    return supported


class FactGuard:
    def __init__(self, analyzer):
        self.analyzer = analyzer

    def check(self, text, sources):
        try:
            analysis = self.analyzer.analyze_claims(text, sources)
        except Exception:
            return FactCheckResult(False, ["claim_analysis_unavailable"])
        if not isinstance(analysis, dict) or not isinstance(analysis.get("claims"), list):
            return FactCheckResult(False, ["claim_analysis_unavailable"])
        if not isinstance(sources, list):
            return FactCheckResult(False, ["malformed_sources"])

        by_id = {
            str(source["id"]): source
            for source in sources
            if isinstance(source, dict) and "id" in source
        }
        reasons = []
        for claim in analysis["claims"]:
            if not isinstance(claim, dict):
                reasons.append("malformed_claim")
                continue
            claim_type = claim.get("type")
            claim_text = claim.get("text")
            supported_by = claim.get("supported_by")
            if (
                not isinstance(claim_type, str)
                or not claim_type.strip()
                or not isinstance(claim_text, str)
                or not claim_text.strip()
                or not isinstance(supported_by, list)
                or any(not valid_source_id(value) for value in supported_by)
            ):
                reasons.append("malformed_claim")
                continue
            raw_claim_type = claim_type.strip()
            claim_type = normalize_claim_type(raw_claim_type)
            if claim_type is None:
                reasons.append("unsupported_claim_type:" + raw_claim_type)
                continue
            required = REQUIRED_SOURCE_TYPES[claim_type]
            subtype = None
            if claim_type == "incident":
                subtype = normalize_incident_subtype(claim.get("subtype"))
                if subtype is None:
                    reasons.append("invalid_incident_subtype")
                    continue
            supporting = [by_id.get(str(value)) for value in supported_by]
            valid = [source for source in supporting if _source_is_valid(source, required)]
            if not valid:
                reasons.append("unsupported_claim:" + claim_type)
                continue

            if claim_type == "number":
                claim_numbers = _numeric_tokens(claim_text)
                if claim_numbers - _supported_numeric_tokens(valid):
                    reasons.append("unsupported_number")
                    continue

            if subtype is not None and not any(
                source.get("metadata", {}).get("disclosure_approved") is True
                for source in valid
            ):
                reasons.append("disclosure_not_approved:" + subtype)

        if not reasons:
            claimed_numbers = _numeric_tokens(text)
            if claimed_numbers - _supported_numeric_tokens(sources):
                reasons.append("unsupported_number")

        return FactCheckResult(not reasons, reasons)
