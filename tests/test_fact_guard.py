from datetime import datetime, timedelta, timezone

from modules.fact_guard import FactGuard


class ClaimAnalyzer:
    def __init__(self, claims):
        self.claims = claims

    def analyze_claims(self, text, sources):
        return {"claims": self.claims}


class FailingClaimAnalyzer:
    def analyze_claims(self, text, sources):
        raise RuntimeError("analysis unavailable")


def test_first_person_claim_requires_founder_note():
    guard = FactGuard(ClaimAnalyzer([{"type": "first_person", "supported_by": []}]))
    result = guard.check("I visited a studio today.", [])
    assert result.approved is False
    assert "first_person" in result.reasons[0]


def test_first_person_claim_requires_publishable_founder_note():
    source = {
        "id": 3,
        "source_type": "founder_note",
        "trust_state": "verified",
        "metadata": {"publishable": False},
    }
    analyzer = ClaimAnalyzer([{"type": "first_person", "supported_by": [3]}])
    assert FactGuard(analyzer).check("I visited a studio.", [source]).approved is False


def test_publishable_founder_note_supports_first_person_claim():
    source = {
        "id": 3,
        "source_type": "founder_note",
        "trust_state": "verified",
        "metadata": {"publishable": True},
    }
    analyzer = ClaimAnalyzer([{"type": "first_person", "supported_by": [3]}])
    assert FactGuard(analyzer).check("I visited a studio.", [source]).approved is True


def test_supported_product_number_passes():
    source = {"id": 7, "source_type": "product_fact", "trust_state": "verified"}
    analyzer = ClaimAnalyzer([{"type": "number", "supported_by": [7]}])
    assert FactGuard(analyzer).check("The verified fee is 15%.", [source]).approved is True


def test_analyzer_failure_blocks_publication():
    assert FactGuard(ClaimAnalyzer(None)).check("Safe-looking copy", []).approved is False
    assert FactGuard(FailingClaimAnalyzer()).check("Safe-looking copy", []).approved is False


def test_unknown_claim_type_blocks_publication():
    analyzer = ClaimAnalyzer([{"type": "rumor", "supported_by": [1]}])
    source = {"id": 1, "source_type": "verified_news", "trust_state": "verified"}
    result = FactGuard(analyzer).check("A rumor.", [source])
    assert result.approved is False
    assert result.reasons == ["unsupported_claim_type:rumor"]


def test_expired_or_malformed_expiry_cannot_support_a_claim():
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    analyzer = ClaimAnalyzer([{"type": "product_claim", "supported_by": [1]}])
    base = {"id": 1, "source_type": "product_fact", "trust_state": "verified"}
    assert FactGuard(analyzer).check("Claim.", [{**base, "expires_at": expired}]).approved is False
    assert FactGuard(analyzer).check("Claim.", [{**base, "expires_at": "not-a-date"}]).approved is False


def test_sensitive_incident_requires_explicit_disclosure_approval():
    analyzer = ClaimAnalyzer([
        {"type": "incident", "subtype": "privacy", "supported_by": [5]},
    ])
    source = {
        "id": 5,
        "source_type": "founder_note",
        "trust_state": "verified",
        "metadata": {"publishable": True},
    }
    blocked = FactGuard(analyzer).check("We had a privacy incident.", [source])
    approved = FactGuard(analyzer).check(
        "We had a privacy incident.",
        [{**source, "metadata": {"publishable": True, "disclosure_approved": True}}],
    )
    assert blocked.approved is False
    assert blocked.reasons == ["disclosure_not_approved:privacy"]
    assert approved.approved is True


def test_malformed_source_is_ignored_instead_of_crashing():
    analyzer = ClaimAnalyzer([{"type": "number", "supported_by": [7]}])
    result = FactGuard(analyzer).check("A number.", [None, {"id": 7}])
    assert result.approved is False


def test_malformed_source_id_cannot_authorize_a_claim():
    analyzer = ClaimAnalyzer([{"type": "number", "supported_by": [None]}])
    source = {"id": None, "source_type": "product_fact", "trust_state": "verified"}
    assert FactGuard(analyzer).check("A number.", [source]).approved is False


def test_non_list_source_bundle_blocks_instead_of_crashing():
    result = FactGuard(ClaimAnalyzer([])).check("Move today.", None)
    assert result.approved is False
    assert result.reasons == ["malformed_sources"]


def test_claim_free_copy_can_pass_with_empty_claim_list():
    assert FactGuard(ClaimAnalyzer([])).check("Move today.", []).approved is True
