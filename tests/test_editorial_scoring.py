import json
from types import SimpleNamespace

from modules.ai_generator import AIGenerator
from modules.character import get_category_agents
from modules.scoring import SCORE_AXES, TweetScorer, semantic_similarity


def _response(content):
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


class FakeCompletions:
    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error

    def create(self, **_kwargs):
        if self.error:
            raise self.error
        return _response(self.content)


def _scorer(content=None, error=None):
    completions = FakeCompletions(content, error)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return TweetScorer(client, "fake-model")


def test_semantic_similarity_detects_reworded_duplicate():
    left = "Three ways gym owners can reduce empty class spots"
    right = "Gym owners: 3 ways to reduce empty spots in classes"
    assert semantic_similarity(left, right) >= 0.72


def test_semantic_similarity_does_not_match_unrelated_copy():
    left = "Gym owners can reduce empty class spots"
    right = "Morning mobility makes ankles feel less stiff"
    assert semantic_similarity(left, right) < 0.20


def test_rewrite_is_used_instead_of_slicing(fake_ai):
    fake_ai.responses = ["One complete rewritten post."]
    long_text = "A complete sentence. " * 20
    rewritten = fake_ai.rewrite_to_limit(long_text, [], limit=280)
    assert rewritten == "One complete rewritten post."
    assert all(not rewritten.endswith(marker) for marker in ("…", "..."))


def test_rewrite_fails_closed_for_missing_long_or_incomplete_output(fake_ai):
    long_output = "Still far too long. " * 20
    for output in (None, long_output, "This thought stops midway", "An ellipsis…", "Three dots..."):
        fake_ai.responses = [output]
        assert fake_ai.rewrite_to_limit("Original text.", [], limit=50) is None


def test_long_grounded_generation_uses_complete_rewrite(fake_ai):
    fake_ai.responses = ["Long sentence. " * 30, "A concise complete post."]
    result = fake_ai.generate_grounded_tweet("gym_strategy", [], include_link=False)
    assert result["text"] == "A concise complete post."
    assert len(result["text"]) <= 280


def test_long_grounded_generation_keeps_category_instruction_during_rewrite(fake_ai):
    prompts = []

    def complete(_system_prompt, user_prompt, **_kwargs):
        prompts.append(" ".join(user_prompt.split()))
        if len(prompts) == 1:
            return "Long sentence. " * 30
        return "Useful standalone advice for operators."

    fake_ai._complete = complete

    result = fake_ai.generate_grounded_tweet(
        "gym_strategy",
        [{"id": 1, "source_type": "evergreen_idea", "text": "Useful idea."}],
        include_link=False,
    )

    assert result["text"] == "Useful standalone advice for operators."
    assert len(prompts) == 2
    assert all(
        "Do not mention FlexDropin or describe its features" in prompt
        for prompt in prompts
    )


def test_grounded_generation_fails_when_rewrite_fails(fake_ai):
    fake_ai.responses = ["Long sentence. " * 30, "Still incomplete"]
    assert fake_ai.generate_grounded_tweet("gym_strategy", [], include_link=False) is None


def test_current_editorial_categories_use_specialist_agents():
    assert get_category_agents("gym_strategy")[0] == "business_expert"
    assert get_category_agents("fitness_business_insight")[0] == "business_expert"
    assert get_category_agents("shareable_fitness")[0] == "fitness_expert"
    assert get_category_agents("product_proof")[0] == "copywriter"
    assert get_category_agents("founder_journey")[0] == "startup_founder"


def test_grounded_generation_targets_the_editorial_score_axes(fake_ai):
    captured = {}

    def complete(system_prompt, user_prompt, **_kwargs):
        captured["system"] = system_prompt
        captured["user"] = user_prompt
        return "A sharp, grounded post."

    fake_ai._complete = complete

    result = fake_ai.generate_grounded_tweet(
        "gym_strategy",
        [{"id": 1, "source_type": "evergreen_idea", "text": "Useful idea."}],
        include_link=False,
    )

    assert result["text"] == "A sharp, grounded post."
    assert "fitness business expert" in captured["system"]
    normalized_prompt = " ".join(captured["user"].split())
    assert "Do not mention FlexDropin or describe its features" in normalized_prompt
    for requirement in (
        "strong non-clickbait opening",
        "concrete actionable takeaway",
        "specific to gym owners",
        "non-obvious angle",
        "worth following",
    ):
        assert requirement in normalized_prompt


def test_product_proof_prompt_requires_direct_product_fact_support(fake_ai):
    captured = {}

    def complete(_system_prompt, user_prompt, **_kwargs):
        captured["user"] = user_prompt
        return "A directly supported product post."

    fake_ai._complete = complete

    fake_ai.generate_grounded_tweet(
        "product_proof",
        [{"id": 8, "source_type": "product_fact", "text": "Verified fact."}],
        include_link=False,
    )

    normalized_prompt = " ".join(captured["user"].split())
    assert "directly supported by a product_fact" in normalized_prompt


def test_claim_analysis_requires_strict_structured_json(fake_ai):
    valid = {
        "claims": [{"type": "number", "text": "15% fee", "supported_by": [7]}],
    }
    fake_ai.responses = [json.dumps(valid), "not json", '{"claims": null}']
    assert fake_ai.analyze_claims("The fee is 15%.", [{"id": 7}]) == valid
    assert fake_ai.analyze_claims("Text.", []) is None
    assert fake_ai.analyze_claims("Text.", []) is None


def test_claim_analysis_scopes_product_claim_to_flexdropin(fake_ai):
    captured = {}

    def complete(_system_prompt, user_prompt, **_kwargs):
        captured["user"] = " ".join(user_prompt.split())
        return '{"claims": []}'

    fake_ai._complete = complete

    result = fake_ai.analyze_claims(
        "Offer a simple drop-in option with clear rules.",
        [{"id": 4, "source_type": "evergreen_idea"}],
    )

    assert result == {"claims": []}
    assert (
        "product_claim means only an assertion about FlexDropin"
        in captured["user"]
    )
    assert "Recommendations and imperatives are not factual claims" in captured["user"]


def test_claim_analysis_rejects_structured_values_as_source_ids(fake_ai):
    malformed = {
        "claims": [{"type": "number", "text": "15% fee", "supported_by": [{"id": 7}]}],
    }
    fake_ai.responses = [json.dumps(malformed)]
    assert fake_ai.analyze_claims("The fee is 15%.", [{"id": 7}]) is None


def test_claim_analysis_requires_known_incident_subtype(fake_ai):
    missing = {
        "claims": [{"type": "incident", "text": "Incident", "supported_by": [5]}],
    }
    unknown = {
        "claims": [{
            "type": "incident",
            "subtype": "availability",
            "text": "Incident",
            "supported_by": [5],
        }],
    }
    fake_ai.responses = [json.dumps(missing), json.dumps(unknown)]
    assert fake_ai.analyze_claims("Incident.", [{"id": 5}]) is None
    assert fake_ai.analyze_claims("Incident.", [{"id": 5}]) is None


def test_claim_analysis_accepts_only_supported_claim_types(fake_ai):
    unsupported = {
        "claims": [{"type": "rumor", "text": "A rumor", "supported_by": [1]}],
    }
    valid_types = (
        "first_person",
        "number",
        "product_claim",
        "incident",
        "medical",
        "testimonial",
        "named_entity",
        "named_current_event",
    )
    valid_analyses = []
    for claim_type in valid_types:
        claim = {"type": claim_type, "text": "A claim", "supported_by": [1]}
        if claim_type == "incident":
            claim["subtype"] = "security"
        valid_analyses.append({"claims": [claim]})

    fake_ai.responses = [json.dumps(unsupported)] + [
        json.dumps(analysis) for analysis in valid_analyses
    ]
    assert fake_ai.analyze_claims("A rumor.", [{"id": 1}]) is None
    for analysis in valid_analyses:
        assert fake_ai.analyze_claims("A claim.", [{"id": 1}]) == analysis


def test_score_draft_normalizes_seven_axes_to_one_hundred():
    payload = {axis: 10 for axis in SCORE_AXES}
    result = _scorer(json.dumps(payload)).score_draft("Useful draft")
    assert result == {**payload, "total": 100}


def test_score_draft_returns_none_on_api_parse_or_schema_failure():
    missing_axis = {axis: 5 for axis in SCORE_AXES[:-1]}
    out_of_range = {axis: 5 for axis in SCORE_AXES}
    out_of_range[SCORE_AXES[0]] = 11
    assert _scorer(error=RuntimeError("offline")).score_draft("Draft") is None
    assert _scorer("not json").score_draft("Draft") is None
    assert _scorer(json.dumps(missing_axis)).score_draft("Draft") is None
    assert _scorer(json.dumps(out_of_range)).score_draft("Draft") is None


def test_autonomous_source_bypassing_post_generators_are_not_exposed():
    assert not hasattr(AIGenerator, "generate_tweet")
    assert not hasattr(AIGenerator, "generate_human_mode_post")
    assert not hasattr(AIGenerator, "generate_build_in_public_post")
