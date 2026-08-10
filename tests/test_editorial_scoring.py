import json
from types import SimpleNamespace

from modules.ai_generator import AIGenerator
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


def test_grounded_generation_fails_when_rewrite_fails(fake_ai):
    fake_ai.responses = ["Long sentence. " * 30, "Still incomplete"]
    assert fake_ai.generate_grounded_tweet("gym_strategy", [], include_link=False) is None


def test_claim_analysis_requires_strict_structured_json(fake_ai):
    valid = {
        "claims": [{"type": "number", "text": "15% fee", "supported_by": [7]}],
    }
    fake_ai.responses = [json.dumps(valid), "not json", '{"claims": null}']
    assert fake_ai.analyze_claims("The fee is 15%.", [{"id": 7}]) == valid
    assert fake_ai.analyze_claims("Text.", []) is None
    assert fake_ai.analyze_claims("Text.", []) is None


def test_claim_analysis_rejects_structured_values_as_source_ids(fake_ai):
    malformed = {
        "claims": [{"type": "number", "text": "15% fee", "supported_by": [{"id": 7}]}],
    }
    fake_ai.responses = [json.dumps(malformed)]
    assert fake_ai.analyze_claims("The fee is 15%.", [{"id": 7}]) is None


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
