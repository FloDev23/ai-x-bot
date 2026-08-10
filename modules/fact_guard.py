"""Fail-closed validation for factual claims in generated posts."""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List


@dataclass(frozen=True)
class FactCheckResult:
    approved: bool
    reasons: List[str]


REQUIRED_SOURCE_TYPES = {
    "first_person": {"founder_note"},
    "number": {"founder_note", "product_fact", "verified_news"},
    "product_claim": {"product_fact"},
    "incident": {"founder_note"},
    "medical": {"verified_news"},
    "testimonial": {"founder_note"},
    "named_entity": {"founder_note", "product_fact", "verified_news"},
    "named_current_event": {"verified_news"},
}

SENSITIVE_INCIDENT_SUBTYPES = {
    "payment",
    "privacy",
    "security",
    "customer_impacting",
}


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


def _source_is_valid(source, allowed_types):
    if not isinstance(source, dict):
        return False
    source_id = source.get("id")
    if (
        isinstance(source_id, bool)
        or not isinstance(source_id, (int, str))
        or (isinstance(source_id, str) and not source_id.strip())
    ):
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
    return True


def _incident_subtype(claim):
    raw = claim.get("subtype") or claim.get("incident_type")
    if claim.get("customer_impacting") is True:
        return "customer_impacting"
    if not isinstance(raw, str):
        return None
    return raw.strip().lower().replace("-", "_").replace(" ", "_")


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
            claim_type = claim.get("type", "unknown")
            if not isinstance(claim_type, str):
                reasons.append("malformed_claim")
                continue
            required = REQUIRED_SOURCE_TYPES.get(claim_type)
            if required is None:
                reasons.append("unsupported_claim_type:" + claim_type)
                continue
            supported_by = claim.get("supported_by", [])
            if not isinstance(supported_by, list):
                supported_by = []
            supporting = [by_id.get(str(value)) for value in supported_by]
            valid = [source for source in supporting if _source_is_valid(source, required)]
            if not valid:
                reasons.append("unsupported_claim:" + claim_type)
                continue

            subtype = _incident_subtype(claim) if claim_type == "incident" else None
            if subtype in SENSITIVE_INCIDENT_SUBTYPES and not any(
                source.get("metadata", {}).get("disclosure_approved") is True
                for source in valid
            ):
                reasons.append("disclosure_not_approved:" + subtype)

        return FactCheckResult(not reasons, reasons)
