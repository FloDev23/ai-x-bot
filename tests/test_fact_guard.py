import hashlib
import json
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


def owned_blog_source():
    item = {
        "slug": "gym-drop-ins-sell-single-classes",
        "url": (
            "https://flexdropin.com/blog/"
            "gym-drop-ins-sell-single-classes"
        ),
        "title": "FlexDropin tested a pilot with 3 class formats",
        "summary": "The guide explains a measured drop-in pilot.",
        "published_at": "2026-08-20",
    }
    content_hash = hashlib.sha256(json.dumps(
        item,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return {
        "id": 42,
        "source_type": "owned_blog_article",
        "trust_state": "verified",
        "verified_by": "flexdropin_editorial_feed",
        "text": item["title"] + "\n" + item["summary"],
        "url": item["url"],
        "metadata": {
            "title": item["title"],
            "summary": item["summary"],
            "published_at": item["published_at"],
            "source_name": "FlexDropin Blog",
            "slug": item["slug"],
            "feed_version": 1,
            "content_hash": content_hash,
        },
    }


def test_first_person_claim_requires_founder_note():
    guard = FactGuard(ClaimAnalyzer([
        {"type": "first_person", "text": "I visited a studio", "supported_by": []},
    ]))
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
    analyzer = ClaimAnalyzer([
        {"type": "first_person", "text": "I visited a studio", "supported_by": [3]},
    ])
    assert FactGuard(analyzer).check("I visited a studio.", [source]).approved is False


def test_publishable_founder_note_supports_first_person_claim():
    source = {
        "id": 3,
        "source_type": "founder_note",
        "trust_state": "verified",
        "metadata": {"publishable": True},
    }
    analyzer = ClaimAnalyzer([
        {"type": "first_person", "text": "I visited a studio", "supported_by": [3]},
    ])
    assert FactGuard(analyzer).check("I visited a studio.", [source]).approved is True


def test_supported_product_number_passes():
    source = {
        "id": 7,
        "source_type": "product_fact",
        "trust_state": "verified",
        "text": "The verified fee is 15%.",
    }
    analyzer = ClaimAnalyzer([
        {"type": "number", "text": "15% fee", "supported_by": [7]},
    ])
    assert FactGuard(analyzer).check("The verified fee is 15%.", [source]).approved is True


def test_owned_blog_article_supports_only_cited_exact_number_and_named_entity():
    source = owned_blog_source()
    claims = [
        {
            "type": "number",
            "text": "3 class formats",
            "supported_by": [42],
        },
        {
            "type": "named_entity",
            "text": "FlexDropin",
            "supported_by": [42],
        },
    ]

    result = FactGuard(ClaimAnalyzer(claims)).check(
        "FlexDropin tested 3 class formats.",
        [source],
    )

    assert result.approved is True
    assert result.reasons == []


def test_owned_blog_article_cannot_support_sensitive_claim_classes():
    source = owned_blog_source()
    claims = (
        {"type": "first_person", "text": "I tested it", "supported_by": [42]},
        {"type": "product_claim", "text": "The app converts", "supported_by": [42]},
        {
            "type": "incident",
            "subtype": "payment",
            "text": "A payment incident",
            "supported_by": [42],
        },
        {"type": "testimonial", "text": "A customer loved it", "supported_by": [42]},
        {"type": "medical", "text": "It improves health", "supported_by": [42]},
        {
            "type": "named_current_event",
            "text": "A current event",
            "supported_by": [42],
        },
    )

    for claim in claims:
        result = FactGuard(ClaimAnalyzer([claim])).check(claim["text"], [source])
        assert result.approved is False
        assert result.reasons == ["unsupported_claim:" + claim["type"]]


def test_owned_blog_number_cannot_borrow_from_another_source():
    owned = owned_blog_source()
    product = {
        "id": 7,
        "source_type": "product_fact",
        "trust_state": "verified",
        "text": "The verified fee is 15%.",
    }
    claim = {
        "type": "number",
        "text": "The guide reports 15%",
        "supported_by": [42],
    }

    result = FactGuard(ClaimAnalyzer([claim])).check(
        "The guide reports 15%.",
        [owned, product],
    )

    assert result.approved is False
    assert result.reasons == ["unsupported_number"]


def test_number_claim_is_rejected_when_value_is_absent_from_source_content():
    source = {
        "id": 8,
        "source_type": "verified_news",
        "trust_state": "verified",
        "text": "81 million members and more than 100 million facility users.",
    }
    analyzer = ClaimAnalyzer([
        {
            "type": "number",
            "text": "Reserve 10-15% of every class",
            "supported_by": [8],
        },
    ])

    result = FactGuard(analyzer).check(
        "Reserve 10-15% of every class.",
        [source],
    )

    assert result.approved is False
    assert result.reasons == ["unsupported_number"]


def test_supported_number_abbreviations_match_source_content():
    source = {
        "id": 8,
        "source_type": "verified_news",
        "trust_state": "verified",
        "text": (
            "81 million Americans were members, more than 100 million used "
            "facilities, and membership rose 5.2%."
        ),
    }
    analyzer = ClaimAnalyzer([
        {
            "type": "number",
            "text": "81M members, 100M+ users, up 5.2%",
            "supported_by": [8],
        },
    ])

    result = FactGuard(analyzer).check(
        "81M members, 100M+ users, up 5.2%.",
        [source],
    )

    assert result.approved is True


def test_italian_number_words_support_english_post_abbreviations():
    source = {
        "id": 8,
        "source_type": "verified_news",
        "trust_state": "verified",
        "text": (
            "Nel 2025, 81 milioni di americani erano iscritti, oltre 100 "
            "milioni usavano le strutture e gli iscritti sono cresciuti "
            "del 5,2 percento."
        ),
    }
    analyzer = ClaimAnalyzer([
        {
            "type": "number",
            "text": "81M members, 100M+ users, up 5.2% in 2025",
            "supported_by": [8],
        },
    ])

    result = FactGuard(analyzer).check(
        "81M members, 100M+ users, up 5.2% in 2025.",
        [source],
    )

    assert result.approved is True


def test_negative_number_cannot_be_authorized_by_positive_source_value():
    source = {
        "id": 8,
        "source_type": "verified_news",
        "trust_state": "verified",
        "text": "Membership rose 5.2%.",
    }
    analyzer = ClaimAnalyzer([
        {
            "type": "number",
            "text": "Membership fell -5.2%",
            "supported_by": [8],
        },
    ])

    result = FactGuard(analyzer).check("Membership fell -5.2%.", [source])

    assert result.approved is False
    assert result.reasons == ["unsupported_number"]


def test_number_claim_uses_only_its_explicit_supporting_sources():
    news = {
        "id": 8,
        "source_type": "verified_news",
        "trust_state": "verified",
        "text": "81 million Americans were members.",
    }
    product_fact = {
        "id": 7,
        "source_type": "product_fact",
        "trust_state": "verified",
        "text": "The verified fee is 15%.",
    }
    analyzer = ClaimAnalyzer([
        {
            "type": "number",
            "text": "The report found 15%",
            "supported_by": [8],
        },
    ])

    result = FactGuard(analyzer).check(
        "The report found 15%.",
        [news, product_fact],
    )

    assert result.approved is False
    assert result.reasons == ["unsupported_number"]


def test_percent_words_and_compact_ranges_have_equivalent_numeric_support():
    source = {
        "id": 8,
        "source_type": "verified_news",
        "trust_state": "verified",
        "text": "The range was between 10% and 15%, up 5.2 percent.",
    }
    analyzer = ClaimAnalyzer([
        {
            "type": "number",
            "text": "The range was 10-15%, up 5.2%",
            "supported_by": [8],
        },
    ])

    result = FactGuard(analyzer).check(
        "The range was 10-15%, up 5.2%.",
        [source],
    )

    assert result.approved is True


def test_unit_words_cannot_authorize_abbreviated_large_numbers():
    cases = (
        ("The class lasts 20 minutes.", "20M members"),
        ("The room is 20 meters long.", "20M members"),
        ("Athletes completed a 20 m sprint.", "20M members"),
        ("The bar weighs 20 kg.", "20K members"),
        ("The studio stores 20 bikes.", "20B visits"),
    )
    for source_text, claim_text in cases:
        source = {
            "id": 8,
            "source_type": "verified_news",
            "trust_state": "verified",
            "text": source_text,
        }
        analyzer = ClaimAnalyzer([
            {
                "type": "number",
                "text": claim_text,
                "supported_by": [8],
            },
        ])

        result = FactGuard(analyzer).check(claim_text + ".", [source])

        assert result.approved is False
        assert result.reasons == ["unsupported_number"]


def test_analyzer_failure_blocks_publication():
    assert FactGuard(ClaimAnalyzer(None)).check("Safe-looking copy", []).approved is False
    assert FactGuard(FailingClaimAnalyzer()).check("Safe-looking copy", []).approved is False


def test_unknown_claim_type_blocks_publication():
    analyzer = ClaimAnalyzer([
        {"type": "rumor", "text": "A rumor", "supported_by": [1]},
    ])
    source = {"id": 1, "source_type": "verified_news", "trust_state": "verified"}
    result = FactGuard(analyzer).check("A rumor.", [source])
    assert result.approved is False
    assert result.reasons == ["unsupported_claim_type:rumor"]


def test_expired_or_malformed_expiry_cannot_support_a_claim():
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    analyzer = ClaimAnalyzer([
        {"type": "product_claim", "text": "Claim", "supported_by": [1]},
    ])
    base = {"id": 1, "source_type": "product_fact", "trust_state": "verified"}
    assert FactGuard(analyzer).check("Claim.", [{**base, "expires_at": expired}]).approved is False
    assert FactGuard(analyzer).check("Claim.", [{**base, "expires_at": "not-a-date"}]).approved is False


def test_sensitive_incident_requires_explicit_disclosure_approval():
    analyzer = ClaimAnalyzer([
        {
            "type": "incident",
            "subtype": "privacy",
            "text": "We had a privacy incident",
            "supported_by": [5],
        },
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
    analyzer = ClaimAnalyzer([
        {"type": "number", "text": "A number", "supported_by": [7]},
    ])
    result = FactGuard(analyzer).check("A number.", [None, {"id": 7}])
    assert result.approved is False


def test_malformed_source_id_cannot_authorize_a_claim():
    analyzer = ClaimAnalyzer([
        {"type": "number", "text": "A number", "supported_by": [None]},
    ])
    source = {"id": None, "source_type": "product_fact", "trust_state": "verified"}
    assert FactGuard(analyzer).check("A number.", [source]).approved is False


def test_non_list_source_bundle_blocks_instead_of_crashing():
    result = FactGuard(ClaimAnalyzer([])).check("Move today.", None)
    assert result.approved is False
    assert result.reasons == ["malformed_sources"]


def test_claim_free_copy_can_pass_with_empty_claim_list():
    assert FactGuard(ClaimAnalyzer([])).check("Move today.", []).approved is True


def test_incident_requires_known_explicit_subtype():
    source = {
        "id": 5,
        "source_type": "founder_note",
        "trust_state": "verified",
        "metadata": {"publishable": True, "disclosure_approved": True},
    }
    for claim in (
        {"type": "incident", "text": "An incident", "supported_by": [5]},
        {
            "type": "incident",
            "subtype": "availability",
            "text": "An availability incident",
            "supported_by": [5],
        },
    ):
        result = FactGuard(ClaimAnalyzer([claim])).check("An incident.", [source])
        assert result.approved is False
        assert result.reasons == ["invalid_incident_subtype"]


def test_every_supported_incident_subtype_requires_disclosure_approval():
    for subtype in ("payment", "privacy", "security", "customer_impacting"):
        claim = {
            "type": "incident",
            "subtype": subtype,
            "text": f"A {subtype} incident",
            "supported_by": [5],
        }
        source = {
            "id": 5,
            "source_type": "founder_note",
            "trust_state": "verified",
            "metadata": {"publishable": True},
        }
        blocked = FactGuard(ClaimAnalyzer([claim])).check("Incident.", [source])
        approved = FactGuard(ClaimAnalyzer([claim])).check(
            "Incident.",
            [{**source, "metadata": {"publishable": True, "disclosure_approved": True}}],
        )
        assert blocked.approved is False
        assert blocked.reasons == [f"disclosure_not_approved:{subtype}"]
        assert approved.approved is True


def test_fact_guard_independently_rejects_malformed_claim_schema():
    source = {"id": 7, "source_type": "product_fact", "trust_state": "verified"}
    malformed_claims = (
        {"type": "number", "supported_by": [7]},
        {"type": "number", "text": " ", "supported_by": [7]},
        {"type": " ", "text": "15% fee", "supported_by": [7]},
        {"type": None, "text": "15% fee", "supported_by": [7]},
        {"type": "number", "text": "15% fee"},
        {"type": "number", "text": "15% fee", "supported_by": "7"},
        {"type": "number", "text": "15% fee", "supported_by": [True]},
        {"type": "number", "text": "15% fee", "supported_by": [None]},
        {"type": "number", "text": "15% fee", "supported_by": [{"id": 7}]},
        {"type": "number", "text": "15% fee", "supported_by": [[7]]},
    )
    for claim in malformed_claims:
        result = FactGuard(ClaimAnalyzer([claim])).check("The fee is 15%.", [source])
        assert result.approved is False
        assert result.reasons == ["malformed_claim"]
