from datetime import datetime, timedelta, timezone

import pytest

from modules.content_planner import ContentPlan
from modules.draft_pipeline import DraftPipeline
from modules.fact_guard import FactCheckResult


NOW = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)
NEXT_SLOT = NOW + timedelta(hours=2)


class FakeDraftDatabase:
    def __init__(self):
        self.next_slot = NEXT_SLOT
        self.sources = {
            7: {
                "id": 7,
                "source_type": "evergreen_idea",
                "text": "Drop-ins can help fill otherwise empty class places.",
                "trust_state": "verified",
            }
        }
        self.drafts = {}
        self.created_drafts = []
        self.evaluations = []
        self.recent_texts = []
        self.recent_calls = 0
        self.released_media = []
        self.reject_transitions_for = set()
        self.raise_replace = False
        self._next_id = 1

    def get_active_draft_for_slot(self, intended_slot):
        return next(
            (
                draft
                for draft in self.drafts.values()
                if draft["intended_slot"] == intended_slot
                and draft["status"]
                in {
                    "pending_approval",
                    "approved",
                    "publishing",
                    "published",
                    "publication_unknown",
                }
            ),
            None,
        )

    def get_content_source(self, source_id):
        return self.sources.get(source_id)

    def get_eligible_content_sources(self, source_ids, now=None):
        del now
        sources = [self.sources.get(source_id) for source_id in source_ids]
        if any(
            source is None or source.get("trust_state") != "verified"
            for source in sources
        ):
            return []
        return sources

    def get_recent_content_texts(self, days=30, exclude_draft_id=None, now=None):
        assert days == 30
        self.recent_calls += 1
        del exclude_draft_id, now
        return list(self.recent_texts)

    def create_post_draft(self, **values):
        draft_id = self._next_id
        self._next_id += 1
        draft = {
            "id": draft_id,
            "status": "pending_approval",
            "media_id": None,
            "approved_at": None,
            "approved_by": None,
            "revision": 0,
            **values,
        }
        self.drafts[draft_id] = draft
        self.created_drafts.append(draft)
        return draft_id

    def create_or_get_post_draft(self, **values):
        existing = self.get_active_draft_for_slot(values["intended_slot"])
        if existing:
            return existing, "existing"
        if not self.get_eligible_content_sources(values["source_ids"]):
            return None, "no_eligible_source"
        draft_id = self.create_post_draft(**values)
        draft = self.get_post_draft(draft_id)
        self.record_draft_evaluation(
            draft["intended_slot"],
            draft["category"],
            "pending_approval",
            {
                "draft_id": draft_id,
                "source_ids": draft["source_ids"],
                "scores": draft["score_data"],
            },
        )
        return draft, "created"

    def get_post_draft(self, draft_id):
        return self.drafts.get(draft_id)

    def transition_post_draft(
        self, draft_id, expected_statuses, new_status, **changes
    ):
        draft = self.drafts.get(draft_id)
        if (
            draft is None
            or draft_id in self.reject_transitions_for
            or draft["status"] not in expected_statuses
        ):
            return False
        draft.update(changes)
        draft["status"] = new_status
        draft["revision"] += 1
        return True

    def postpone_post_draft_atomic(
        self, draft_id, expected_revision, expected_statuses, new_slot
    ):
        draft = self.drafts.get(draft_id)
        occupied = self.get_active_draft_for_slot(new_slot)
        if (
            draft is None
            or draft["revision"] != expected_revision
            or draft["status"] not in expected_statuses
            or (occupied and occupied["id"] != draft_id)
        ):
            return False
        draft.update(
            status="pending_approval",
            intended_slot=new_slot,
            approved_at=None,
            approved_by=None,
        )
        draft["revision"] += 1
        return True

    def approve_post_draft_atomic(
        self,
        draft_id,
        expected_revision,
        expected_slot,
        approved_by,
        now_fn,
    ):
        draft = self.drafts.get(draft_id)
        slot = datetime.fromisoformat(expected_slot)
        now = now_fn()
        if (
            draft is None
            or draft["status"] != "pending_approval"
            or draft["revision"] != expected_revision
            or draft["intended_slot"] != expected_slot
        ):
            return False
        if now >= slot:
            draft["status"] = "expired"
            draft["revision"] += 1
            return False
        draft.update(
            status="approved",
            approved_at=now.isoformat(),
            approved_by=approved_by,
        )
        draft["revision"] += 1
        return True

    def replace_post_draft_atomic(
        self,
        *,
        prior_draft_id,
        expected_revision,
        expected_slot,
        expected_category,
        expected_source_ids,
        text,
        score_data,
        publication_key,
    ):
        if self.raise_replace:
            raise RuntimeError("raw sqlite payload")
        prior = self.drafts.get(prior_draft_id)
        if (
            prior is None
            or prior_draft_id in self.reject_transitions_for
            or prior["revision"] != expected_revision
            or prior["status"]
            not in {"pending_approval", "approved", "expired"}
            or prior["intended_slot"] != expected_slot
            or prior["category"] != expected_category
            or prior["source_ids"] != expected_source_ids
        ):
            return None, "conflict"
        if not self.get_eligible_content_sources(expected_source_ids):
            return None, "no_eligible_source"
        prior["status"] = "superseded"
        prior["revision"] += 1
        replacement_id = self.create_post_draft(
            text=text,
            category=expected_category,
            source_ids=expected_source_ids,
            score_data=score_data,
            intended_slot=expected_slot,
            publication_key=publication_key,
        )
        replacement = self.get_post_draft(replacement_id)
        self.record_draft_evaluation(
            expected_slot,
            expected_category,
            "pending_approval",
            {
                "draft_id": replacement_id,
                "source_ids": expected_source_ids,
                "scores": score_data,
                "supersedes_draft_id": prior_draft_id,
            },
        )
        self.release_media_for_draft(prior_draft_id)
        return replacement, "created"

    def record_draft_evaluation(
        self, intended_slot, category, outcome, details
    ):
        self.evaluations.append(
            {
                "intended_slot": intended_slot,
                "category": category,
                "outcome": outcome,
                "details": details,
            }
        )

    def release_media_for_draft(self, draft_id):
        self.released_media.append(draft_id)


class FakePlanner:
    def __init__(self):
        self.result = ContentPlan(
            category="gym_strategy",
            source_ids=[7],
            intended_slot=NEXT_SLOT,
            include_link=False,
        )
        self.calls = []

    def plan(self, intended_slot):
        self.calls.append(intended_slot)
        return self.result


class FakeGenerator:
    def __init__(self):
        self.text = "Fill empty class places with flexible drop-in access."
        self.results = None
        self.rewrite_result = None
        self.rewrite_results = None
        self.raise_generate = False
        self.raise_rewrite = False
        self.generated = []
        self.candidate_indices = []
        self.rewrites = []

    def generate_grounded_tweet(
        self, category, sources, include_link, candidate_index=None
    ):
        self.generated.append((category, sources, include_link, candidate_index))
        self.candidate_indices.append(candidate_index)
        if self.raise_generate:
            raise RuntimeError("Bearer sensitive-token-must-not-be-stored")
        if self.results is not None:
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        if self.text is None:
            return None
        return {
            "text": self.text,
            "agent_used": "fake",
            "raw_reasoning": "must never be persisted",
        }

    def rewrite_to_limit(self, text, sources, limit, category=None):
        self.rewrites.append((text, sources, limit, category))
        if self.raise_rewrite:
            raise RuntimeError("private model payload")
        if self.rewrite_results is not None:
            result = self.rewrite_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return self.rewrite_result


class FakeGuard:
    def __init__(self):
        self.approved = True
        self.reasons = []
        self.results = None
        self.raise_check = False
        self.calls = []

    def check(self, text, sources):
        self.calls.append((text, sources))
        if self.raise_check:
            raise RuntimeError("raw analyzer response")
        if self.results is not None:
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return FactCheckResult(self.approved, self.reasons)


class FakeScorer:
    def __init__(self):
        self.result = {"total": 88, "hook": 9}
        self.results = None
        self.raise_score = False
        self.calls = []
        self.contexts = []

    def score_draft(self, text, sources=None, recent_texts=None):
        self.calls.append(text)
        self.contexts.append((sources, recent_texts))
        if self.raise_score:
            raise RuntimeError("secret scoring payload")
        if self.results is not None:
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return self.result


@pytest.fixture
def pipeline_parts():
    database = FakeDraftDatabase()
    planner = FakePlanner()
    generator = FakeGenerator()
    guard = FakeGuard()
    scorer = FakeScorer()
    pipeline = DraftPipeline(
        database,
        planner,
        generator,
        guard,
        scorer,
        now_fn=lambda: NOW,
    )
    return pipeline, database, planner, generator, guard, scorer


def test_candidate_tournament_persists_only_highest_safe_score(pipeline_parts):
    pipeline, database, _, generator, _, scorer = pipeline_parts
    generator.results = [
        {"text": "Candidate one."},
        {"text": "Candidate two."},
        {"text": "Candidate three."},
    ]
    scorer.results = [
        {"total": 76, "hook": 7},
        {"total": 84, "hook": 9},
        {"total": 81, "hook": 8},
    ]

    draft = pipeline.create_for_slot(database.next_slot)

    assert draft["text"] == "Candidate two."
    assert draft["score_data"] == {"total": 84, "hook": 9}
    assert len(database.created_drafts) == 1
    assert generator.candidate_indices == [0, 1, 2]
    assert "Candidate one." not in repr(database.evaluations)
    assert "Candidate three." not in repr(database.evaluations)
    assert "raw_reasoning" not in repr(database.evaluations)


def test_candidate_tournament_uses_earliest_attempt_on_equal_total(
    pipeline_parts,
):
    pipeline, database, _, generator, _, scorer = pipeline_parts
    generator.results = [
        {"text": "First tied candidate."},
        {"text": "Second tied candidate."},
        {"text": "Lower candidate."},
    ]
    scorer.results = [
        {"total": 82},
        {"total": 82},
        {"total": 80},
    ]

    draft = pipeline.create_for_slot(database.next_slot)

    assert draft["text"] == "First tied candidate."
    assert generator.candidate_indices == [0, 1, 2]


def test_candidate_tournament_reuses_source_and_recent_snapshot(pipeline_parts):
    pipeline, database, _, generator, guard, scorer = pipeline_parts
    database.recent_texts = ["Existing unrelated operator post."]

    assert pipeline.create_for_slot(database.next_slot) is not None

    assert database.recent_calls == 1
    assert len(generator.generated) == 3
    assert generator.candidate_indices == [0, 1, 2]
    assert len(guard.calls) == 3
    assert len(scorer.contexts) == 3
    sources = generator.generated[0][1]
    assert all(
        call[1] is sources
        for call in generator.generated
    )
    assert all(call[1] is sources for call in guard.calls)
    assert all(context[0] is sources for context in scorer.contexts)
    assert all(context[1] is scorer.contexts[0][1] for context in scorer.contexts)


def test_candidate_tournament_continues_after_candidate_fact_rejection(
    pipeline_parts,
):
    pipeline, database, _, generator, guard, scorer = pipeline_parts
    generator.results = [
        {"text": "Unsupported first candidate."},
        {"text": "Grounded second candidate."},
        {"text": "Grounded third candidate."},
    ]
    guard.results = [
        FactCheckResult(False, ["unsupported_number"]),
        FactCheckResult(True, []),
        FactCheckResult(True, []),
    ]
    scorer.results = [
        {"total": 81},
        {"total": 79},
    ]

    draft = pipeline.create_for_slot(database.next_slot)

    assert draft["text"] == "Grounded second candidate."
    assert generator.candidate_indices == [0, 1, 2]
    assert scorer.calls == [
        "Grounded second candidate.",
        "Grounded third candidate.",
    ]
    assert len(database.created_drafts) == 1
    assert "Unsupported first candidate." not in repr(database.evaluations)


def test_candidate_tournament_continues_after_semantic_duplicate(
    pipeline_parts,
):
    pipeline, database, _, generator, _, scorer = pipeline_parts
    duplicate = "Gym owners can fill empty class places with drop-ins."
    database.recent_texts = [duplicate]
    generator.results = [
        {"text": duplicate},
        {"text": "Flexible access can turn spare capacity into demand."},
        {"text": "Offer visiting athletes a simpler route into class."},
    ]
    scorer.results = [
        {"total": 90},
        {"total": 78},
        {"total": 77},
    ]

    draft = pipeline.create_for_slot(database.next_slot)

    assert draft["text"] == "Flexible access can turn spare capacity into demand."
    assert len(scorer.calls) == 3
    assert len(database.created_drafts) == 1


def test_candidate_tournament_records_only_best_safe_low_score(pipeline_parts):
    pipeline, database, _, generator, _, scorer = pipeline_parts
    generator.results = [
        {"text": "Low candidate one."},
        {"text": "Best low candidate."},
        {"text": "Low candidate three."},
    ]
    scorer.results = [
        {"total": 70, "hook": 6, "reasoning": "private one"},
        {"total": 74, "hook": 8, "reasoning": "private two"},
        {"total": 72, "hook": 7, "reasoning": "private three"},
    ]

    assert pipeline.create_for_slot(database.next_slot) is None

    assert database.created_drafts == []
    assert database.evaluations == [
        {
            "intended_slot": database.next_slot.isoformat(),
            "category": "gym_strategy",
            "outcome": "rejected_score",
            "details": {
                "attempt": 2,
                "source_ids": [7],
                "scores": {"total": 74, "hook": 8},
            },
        }
    ]
    audit = repr(database.evaluations)
    assert "Best low candidate." not in audit
    assert "reasoning" not in audit


def test_candidate_tournament_records_one_sanitized_fact_rejection(
    pipeline_parts,
):
    pipeline, database, _, generator, guard, scorer = pipeline_parts
    generator.results = [
        {"text": "Fact-invalid candidate one."},
        {"text": "Fact-invalid candidate two."},
        {"text": "Fact-invalid candidate three."},
    ]
    guard.results = [
        FactCheckResult(False, ["unsupported_number"]),
        FactCheckResult(False, ["private_model_payload"]),
        FactCheckResult(False, ["unsupported_claim:number"]),
    ]

    assert pipeline.create_for_slot(database.next_slot) is None

    assert scorer.calls == []
    assert database.created_drafts == []
    assert database.evaluations == [
        {
            "intended_slot": database.next_slot.isoformat(),
            "category": "gym_strategy",
            "outcome": "rejected_fact",
            "details": {
                "attempt": 3,
                "reason_codes": ["unsupported_claim:number"],
                "source_ids": [7],
            },
        }
    ]
    assert "Fact-invalid" not in repr(database.evaluations)


def test_candidate_tournament_malformed_candidate_cannot_win_or_leak(
    pipeline_parts,
):
    pipeline, database, _, generator, _, scorer = pipeline_parts
    generator.results = [
        {
            "text": {"private": "Malformed candidate body."},
            "raw_reasoning": "private model chain",
        },
        {"text": "Valid tournament winner."},
        {"text": "Valid lower candidate."},
    ]
    scorer.results = [
        {"total": 82},
        {"total": 79},
    ]

    draft = pipeline.create_for_slot(database.next_slot)

    assert draft["text"] == "Valid tournament winner."
    assert len(database.created_drafts) == 1
    assert generator.candidate_indices == [0, 1, 2]
    audit = repr(database.evaluations)
    assert "Malformed candidate body." not in audit
    assert "raw_reasoning" not in audit
    assert "private model chain" not in audit


@pytest.mark.parametrize("failure", ["exception", "none"])
def test_candidate_tournament_generator_failure_aborts_first_attempt(
    pipeline_parts, failure
):
    pipeline, database, _, generator, guard, scorer = pipeline_parts
    if failure == "exception":
        generator.raise_generate = True
    else:
        generator.text = None

    assert pipeline.create_for_slot(database.next_slot) is None

    assert len(generator.generated) == 1
    assert generator.candidate_indices == [0]
    assert guard.calls == []
    assert scorer.calls == []
    assert database.created_drafts == []
    assert database.evaluations == [
        {
            "intended_slot": database.next_slot.isoformat(),
            "category": "gym_strategy",
            "outcome": "generation_failed",
            "details": {
                "attempt": 1,
                "reason_codes": ["generation_unavailable"],
                "source_ids": [7],
            },
        }
    ]


@pytest.mark.parametrize("failure", ["exception", "claim_analysis_unavailable"])
def test_candidate_tournament_fact_service_failure_aborts_first_attempt(
    pipeline_parts, failure
):
    pipeline, database, _, generator, guard, scorer = pipeline_parts
    if failure == "exception":
        guard.raise_check = True
        expected_reason = "fact_check_unavailable"
    else:
        guard.approved = False
        guard.reasons = ["claim_analysis_unavailable"]
        expected_reason = "claim_analysis_unavailable"

    assert pipeline.create_for_slot(database.next_slot) is None

    assert len(generator.generated) == 1
    assert len(guard.calls) == 1
    assert scorer.calls == []
    assert database.created_drafts == []
    assert database.evaluations == [
        {
            "intended_slot": database.next_slot.isoformat(),
            "category": "gym_strategy",
            "outcome": "rejected_fact",
            "details": {
                "attempt": 1,
                "reason_codes": [expected_reason],
                "source_ids": [7],
            },
        }
    ]


@pytest.mark.parametrize("failure", ["exception", "malformed"])
def test_candidate_tournament_scorer_failure_aborts_first_attempt(
    pipeline_parts, failure
):
    pipeline, database, _, generator, guard, scorer = pipeline_parts
    if failure == "exception":
        scorer.raise_score = True
        expected_details = {
            "attempt": 1,
            "reason_codes": ["scoring_unavailable"],
            "source_ids": [7],
        }
    else:
        scorer.result = {
            "total": "private malformed total",
            "reasoning": "private scoring response",
        }
        expected_details = {
            "attempt": 1,
            "source_ids": [7],
            "scores": {},
        }

    assert pipeline.create_for_slot(database.next_slot) is None

    assert len(generator.generated) == 1
    assert len(guard.calls) == 1
    assert len(scorer.calls) == 1
    assert database.created_drafts == []
    assert database.evaluations == [
        {
            "intended_slot": database.next_slot.isoformat(),
            "category": "gym_strategy",
            "outcome": "rejected_score",
            "details": expected_details,
        }
    ]
    audit = repr(database.evaluations)
    assert "private malformed total" not in audit
    assert "private scoring response" not in audit


@pytest.mark.parametrize(
    ("failure", "outcome", "expected_details", "call_counts"),
    [
        (
            "generator_exception",
            "generation_failed",
            {"reason_codes": ["generation_unavailable"]},
            (2, 1, 1),
        ),
        (
            "generator_none",
            "generation_failed",
            {"reason_codes": ["generation_unavailable"]},
            (2, 1, 1),
        ),
        (
            "fact_exception",
            "rejected_fact",
            {"reason_codes": ["fact_check_unavailable"]},
            (2, 2, 1),
        ),
        (
            "claim_analysis_unavailable",
            "rejected_fact",
            {"reason_codes": ["claim_analysis_unavailable"]},
            (2, 2, 1),
        ),
        (
            "scorer_exception",
            "rejected_score",
            {"reason_codes": ["scoring_unavailable"]},
            (2, 2, 2),
        ),
        (
            "scorer_malformed",
            "rejected_score",
            {"scores": {}},
            (2, 2, 2),
        ),
    ],
)
def test_candidate_tournament_later_systemic_failure_discards_eligible_first(
    pipeline_parts, failure, outcome, expected_details, call_counts
):
    pipeline, database, _, generator, guard, scorer = pipeline_parts
    generator.results = [
        {"text": "Eligible first candidate."},
        {"text": "Systemic second candidate."},
        {"text": "Third candidate must not run."},
    ]
    guard.results = [
        FactCheckResult(True, []),
        FactCheckResult(True, []),
        FactCheckResult(True, []),
    ]
    scorer.results = [
        {"total": 82},
        {"total": 81},
        {"total": 80},
    ]

    if failure == "generator_exception":
        generator.results[1] = RuntimeError("private generator failure")
    elif failure == "generator_none":
        generator.results[1] = None
    elif failure == "fact_exception":
        guard.results[1] = RuntimeError("private fact failure")
    elif failure == "claim_analysis_unavailable":
        guard.results[1] = FactCheckResult(
            False,
            ["claim_analysis_unavailable"],
        )
    elif failure == "scorer_exception":
        scorer.results[1] = RuntimeError("private scorer failure")
    else:
        scorer.results[1] = {
            "total": "private malformed total",
            "reasoning": "private scorer reasoning",
        }

    assert pipeline.create_for_slot(database.next_slot) is None

    generator_calls, guard_calls, scorer_calls = call_counts
    assert len(generator.generated) == generator_calls
    assert generator.candidate_indices == [0, 1]
    assert len(guard.calls) == guard_calls
    assert len(scorer.calls) == scorer_calls
    assert database.created_drafts == []
    assert database.evaluations == [
        {
            "intended_slot": database.next_slot.isoformat(),
            "category": "gym_strategy",
            "outcome": outcome,
            "details": {
                "attempt": 2,
                "source_ids": [7],
                **expected_details,
            },
        }
    ]
    audit = repr(database.evaluations)
    assert "Eligible first candidate." not in audit
    assert "private" not in audit


def test_candidate_tournament_invalid_source_stops_before_generation(
    pipeline_parts,
):
    pipeline, database, _, generator, guard, scorer = pipeline_parts
    database.sources.clear()

    assert pipeline.create_for_slot(database.next_slot) is None

    assert generator.generated == []
    assert guard.calls == []
    assert scorer.calls == []
    assert database.recent_calls == 0
    assert database.created_drafts == []
    assert database.evaluations == [
        {
            "intended_slot": database.next_slot.isoformat(),
            "category": "gym_strategy",
            "outcome": "no_eligible_source",
            "details": {"source_ids": [7]},
        }
    ]


def test_low_score_skips_slot_and_records_only_scores(pipeline_parts):
    pipeline, database, _, _, _, scorer = pipeline_parts
    scorer.result = {"total": 74, "hook": 7, "reasoning": "private"}

    assert pipeline.create_for_slot(database.next_slot) is None

    assert database.created_drafts == []
    assert database.evaluations[-1]["outcome"] == "rejected_score"
    assert database.evaluations[-1]["details"] == {
        "attempt": 1,
        "source_ids": [7],
        "scores": {"total": 74, "hook": 7}
    }


def test_scorer_receives_verified_sources_and_recent_copy(pipeline_parts):
    pipeline, database, _, _, _, scorer = pipeline_parts
    database.recent_texts = ["A different recent gym-operator post."]

    assert pipeline.create_for_slot(database.next_slot) is not None

    assert len(scorer.contexts) == 3
    assert all(
        context == ([database.sources[7]], database.recent_texts)
        for context in scorer.contexts
    )


def test_fact_failure_skips_slot_and_records_reason_codes(pipeline_parts):
    pipeline, database, _, _, guard, _ = pipeline_parts
    guard.approved = False
    guard.reasons = ["unsupported_claim:number"]

    assert pipeline.create_for_slot(database.next_slot) is None

    assert database.created_drafts == []
    assert database.evaluations[-1]["outcome"] == "rejected_fact"
    assert database.evaluations[-1]["details"] == {
        "attempt": 3,
        "reason_codes": ["unsupported_claim:number"],
        "source_ids": [7],
    }


def test_unsupported_number_reason_is_preserved_for_audit(pipeline_parts):
    pipeline, database, _, _, guard, _ = pipeline_parts
    guard.approved = False
    guard.reasons = ["unsupported_number"]

    assert pipeline.create_for_slot(database.next_slot) is None

    assert database.evaluations[-1]["details"]["reason_codes"] == [
        "unsupported_number"
    ]


@pytest.mark.parametrize(
    ("raw_reason", "safe_reason"),
    [
        ("unsupported_claim_type:private-model-payload", "unsupported_claim_type"),
        ("private_model_payload", "invalid_reason_code"),
    ],
)
def test_model_supplied_fact_reason_is_reduced_to_a_safe_code(
    pipeline_parts, raw_reason, safe_reason
):
    pipeline, database, _, _, guard, _ = pipeline_parts
    guard.approved = False
    guard.reasons = [raw_reason]

    assert pipeline.create_for_slot(database.next_slot) is None

    assert database.evaluations[-1]["details"]["reason_codes"] == [
        safe_reason
    ]


def test_duplicate_skips_slot(pipeline_parts):
    pipeline, database, _, generator, _, _ = pipeline_parts
    database.recent_texts = [
        "Gym owners can reduce empty class spots with drop-ins."
    ]
    generator.text = (
        "Gym owners: reduce empty spots in classes with drop-ins."
    )

    assert pipeline.create_for_slot(database.next_slot) is None

    assert database.evaluations[-1]["outcome"] == "rejected_duplicate"
    assert database.evaluations[-1]["details"] == {
        "attempt": 3,
        "source_ids": [7],
    }


def test_good_draft_waits_for_approval(pipeline_parts):
    pipeline, database, _, _, _, _ = pipeline_parts

    draft = pipeline.create_for_slot(database.next_slot)

    assert draft["status"] == "pending_approval"
    assert draft["source_ids"] == [7]
    assert draft["publication_key"].startswith("draft:")
    assert database.evaluations[-1]["outcome"] == "pending_approval"


def test_scheduler_replay_returns_existing_live_draft(pipeline_parts):
    pipeline, database, planner, _, _, _ = pipeline_parts
    existing = database.create_post_draft(
        text="Existing",
        category="gym_strategy",
        source_ids=[7],
        score_data={"total": 90},
        intended_slot=database.next_slot.isoformat(),
        publication_key="draft:existing",
    )

    result = pipeline.create_for_slot(database.next_slot)

    assert result["id"] == existing
    assert planner.calls == []
    assert len(database.created_drafts) == 1


@pytest.mark.parametrize(
    ("setup", "outcome"),
    [
        ("no_plan", "no_eligible_source"),
        ("missing_source", "no_eligible_source"),
        ("no_generation", "generation_failed"),
        ("no_score", "rejected_score"),
    ],
)
def test_fail_closed_early_returns_are_audited(
    pipeline_parts, setup, outcome
):
    pipeline, database, planner, generator, _, scorer = pipeline_parts
    if setup == "no_plan":
        planner.result = None
    elif setup == "missing_source":
        database.sources.clear()
    elif setup == "no_generation":
        generator.text = None
    else:
        scorer.result = None

    assert pipeline.create_for_slot(database.next_slot) is None

    assert database.created_drafts == []
    assert database.evaluations[-1]["outcome"] == outcome


@pytest.mark.parametrize(
    ("failure", "outcome", "reason_code"),
    [
        ("generation", "generation_failed", "generation_unavailable"),
        ("rewrite", "rewrite_failed", "rewrite_unavailable"),
        ("fact", "rejected_fact", "fact_check_unavailable"),
        ("score", "rejected_score", "scoring_unavailable"),
    ],
)
def test_dependency_exceptions_fail_closed_without_persisting_payloads(
    pipeline_parts, failure, outcome, reason_code
):
    pipeline, database, _, generator, guard, scorer = pipeline_parts
    if failure == "generation":
        generator.raise_generate = True
    elif failure == "rewrite":
        generator.text = "x" * 281
        generator.raise_rewrite = True
    elif failure == "fact":
        guard.raise_check = True
    else:
        scorer.raise_score = True

    assert pipeline.create_for_slot(database.next_slot) is None

    evaluation = database.evaluations[-1]
    assert evaluation["outcome"] == outcome
    assert evaluation["details"]["reason_codes"] == [reason_code]
    assert "payload" not in str(evaluation).lower()


@pytest.mark.parametrize("invalid_total", [float("nan"), float("inf"), 101])
def test_malformed_score_totals_fail_closed(pipeline_parts, invalid_total):
    pipeline, database, _, _, _, scorer = pipeline_parts
    scorer.result = {"total": invalid_total}

    assert pipeline.create_for_slot(database.next_slot) is None

    assert database.evaluations[-1]["outcome"] == "rejected_score"


def test_overlong_copy_is_completely_rewritten_before_fact_gate(pipeline_parts):
    pipeline, database, _, generator, guard, scorer = pipeline_parts
    generator.text = "x" * 281
    generator.rewrite_result = "A complete grounded rewrite."

    draft = pipeline.create_for_slot(database.next_slot)

    assert draft["text"] == "A complete grounded rewrite."
    assert generator.rewrites[0][2] == 280
    assert generator.rewrites[0][3] == "gym_strategy"
    assert guard.calls[0][0] == "A complete grounded rewrite."
    assert scorer.calls == ["A complete grounded rewrite."] * 3


@pytest.mark.parametrize("rewritten", [None, "", "x" * 281])
def test_candidate_tournament_invalid_rewrite_continues_to_safe_winner(
    pipeline_parts, rewritten
):
    pipeline, database, _, generator, guard, scorer = pipeline_parts
    generator.results = [
        {"text": "x" * 281},
        {"text": "Safe winner after invalid rewrite."},
        {"text": "Safe lower final candidate."},
    ]
    generator.rewrite_results = [rewritten]
    scorer.results = [
        {"total": 81},
        {"total": 79},
    ]

    draft = pipeline.create_for_slot(database.next_slot)

    assert draft["text"] == "Safe winner after invalid rewrite."
    assert generator.candidate_indices == [0, 1, 2]
    assert len(generator.rewrites) == 1
    assert [call[0] for call in guard.calls] == [
        "Safe winner after invalid rewrite.",
        "Safe lower final candidate.",
    ]
    assert scorer.calls == [
        "Safe winner after invalid rewrite.",
        "Safe lower final candidate.",
    ]
    assert len(database.created_drafts) == 1
    assert [evaluation["outcome"] for evaluation in database.evaluations] == [
        "pending_approval"
    ]


def test_gate_order_stops_before_later_services(pipeline_parts):
    pipeline, database, _, _, guard, scorer = pipeline_parts
    guard.approved = False

    assert pipeline.create_for_slot(database.next_slot) is None

    assert scorer.calls == []
    assert database.recent_texts == []


def test_approve_is_single_use_and_requires_time_before_slot(pipeline_parts):
    pipeline, database, _, _, _, _ = pipeline_parts
    draft = pipeline.create_for_slot(database.next_slot)

    assert pipeline.approve(draft["id"], "floriano") is True
    assert draft["status"] == "approved"
    assert draft["approved_by"] == "floriano"
    assert draft["approved_at"] == NOW.isoformat()
    assert pipeline.approve(draft["id"], "again") is False


@pytest.mark.parametrize("slot", [NOW, NOW - timedelta(seconds=1)])
def test_approve_expires_at_or_after_slot(pipeline_parts, slot):
    pipeline, database, _, _, _, _ = pipeline_parts
    draft_id = database.create_post_draft(
        text="Ready.",
        category="gym_strategy",
        source_ids=[7],
        score_data={"total": 90},
        intended_slot=slot.isoformat(),
        publication_key="draft:late",
    )

    assert pipeline.approve(draft_id, "floriano") is False
    assert database.get_post_draft(draft_id)["status"] == "expired"


@pytest.mark.parametrize("initial_status", ["pending_approval", "approved", "expired"])
def test_postpone_moves_allowed_draft_to_explicit_future_slot(
    pipeline_parts, initial_status
):
    pipeline, database, _, _, _, _ = pipeline_parts
    draft = pipeline.create_for_slot(database.next_slot)
    draft.update(
        status=initial_status,
        approved_at=NOW.isoformat(),
        approved_by="floriano",
    )
    new_slot = NEXT_SLOT + timedelta(days=1)

    assert pipeline.postpone(draft["id"], new_slot) is True
    assert draft["status"] == "pending_approval"
    assert draft["intended_slot"] == new_slot.isoformat()
    assert draft["approved_at"] is None
    assert draft["approved_by"] is None


def test_postpone_rejects_nonfuture_or_published_draft(pipeline_parts):
    pipeline, database, _, _, _, _ = pipeline_parts
    draft = pipeline.create_for_slot(database.next_slot)

    assert pipeline.postpone(draft["id"], NOW) is False
    draft["status"] = "published"
    assert pipeline.postpone(draft["id"], NEXT_SLOT + timedelta(days=1)) is False


def test_discard_is_single_use_and_releases_reserved_media(pipeline_parts):
    pipeline, database, _, _, _, _ = pipeline_parts
    draft = pipeline.create_for_slot(database.next_slot)
    draft["media_id"] = 44

    assert pipeline.discard(draft["id"], "not_relevant") is True
    assert draft["status"] == "discarded"
    assert draft["error"] == "not_relevant"
    assert database.released_media == [draft["id"]]


def test_discard_does_not_persist_an_arbitrary_reason_payload(pipeline_parts):
    pipeline, database, _, _, _, _ = pipeline_parts
    draft = pipeline.create_for_slot(database.next_slot)

    assert pipeline.discard(draft["id"], "private_model_payload") is True

    assert draft["error"] == "discarded"
    assert pipeline.discard(draft["id"], "again") is False
    assert database.released_media == [draft["id"]]


@pytest.mark.parametrize(
    "status", ["published", "publishing", "publication_unknown"]
)
def test_discard_never_changes_publication_states(pipeline_parts, status):
    pipeline, database, _, _, _, _ = pipeline_parts
    draft = pipeline.create_for_slot(database.next_slot)
    draft["status"] = status

    assert pipeline.discard(draft["id"], "unsafe") is False
    assert database.released_media == []


def test_regenerate_creates_new_audited_draft_and_supersedes_prior(
    pipeline_parts,
):
    pipeline, database, planner, generator, _, _ = pipeline_parts
    prior = pipeline.create_for_slot(database.next_slot)
    old_key = prior["publication_key"]
    prior["media_id"] = 91
    planner.calls.clear()
    generator.text = "A newly generated grounded post."

    replacement = pipeline.regenerate(prior["id"])

    assert planner.calls == []
    assert replacement["id"] != prior["id"]
    assert replacement["category"] == prior["category"]
    assert replacement["source_ids"] == prior["source_ids"]
    assert replacement["intended_slot"] == prior["intended_slot"]
    assert replacement["publication_key"] != old_key
    assert replacement["publication_key"].startswith("draft:")
    assert prior["status"] == "superseded"
    assert database.released_media == [prior["id"]]
    assert database.evaluations[-1]["outcome"] == "pending_approval"
    assert database.evaluations[-1]["details"]["supersedes_draft_id"] == prior["id"]


def test_failed_regeneration_preserves_prior_draft(pipeline_parts):
    pipeline, database, _, generator, _, _ = pipeline_parts
    prior = pipeline.create_for_slot(database.next_slot)
    generator.text = None

    assert pipeline.regenerate(prior["id"]) is None

    assert prior["status"] == "pending_approval"
    assert len(database.created_drafts) == 1


def test_regenerate_is_single_use(pipeline_parts):
    pipeline, database, _, generator, _, _ = pipeline_parts
    prior = pipeline.create_for_slot(database.next_slot)
    generator.text = "First replacement."

    assert pipeline.regenerate(prior["id"]) is not None
    assert pipeline.regenerate(prior["id"]) is None
    assert len(database.created_drafts) == 2


def test_regeneration_conflict_creates_no_replacement_and_preserves_prior(
    pipeline_parts,
):
    pipeline, database, _, generator, _, _ = pipeline_parts
    prior = pipeline.create_for_slot(database.next_slot)
    database.reject_transitions_for.add(prior["id"])
    generator.text = "A valid but concurrently replaced draft."

    assert pipeline.regenerate(prior["id"]) is None

    assert prior["status"] == "pending_approval"
    assert len(database.created_drafts) == 1
    assert database.evaluations[-1]["outcome"] == "regeneration_conflict"


def test_regeneration_persistence_failure_is_safely_audited(pipeline_parts):
    pipeline, database, _, generator, _, _ = pipeline_parts
    prior = pipeline.create_for_slot(database.next_slot)
    database.raise_replace = True
    generator.text = "A valid replacement that cannot be stored."

    assert pipeline.regenerate(prior["id"]) is None

    assert prior["status"] == "pending_approval"
    assert database.evaluations[-1] == {
        "intended_slot": prior["intended_slot"],
        "category": prior["category"],
        "outcome": "generation_failed",
        "details": {
            "reason_codes": ["draft_persistence_unavailable"],
            "source_ids": prior["source_ids"],
        },
    }
