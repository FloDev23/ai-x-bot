"""Approval-only, source-grounded draft creation and state transitions."""

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
from typing import Dict, List, Optional
from uuid import uuid4

from modules.scoring import SCORE_AXES, semantic_similarity
from modules.fact_guard import INCIDENT_SUBTYPES, SUPPORTED_CLAIM_TYPES


_REASON_CODE = re.compile(r"^[a-z0-9_:-]+$")
_SCORE_KEYS = frozenset(SCORE_AXES) | {"total"}
_SAFE_SIMPLE_FACT_REASONS = frozenset(
    {
        "claim_analysis_unavailable",
        "malformed_sources",
        "malformed_claim",
        "invalid_incident_subtype",
    }
)
_DISCARD_REASON_CODES = frozenset(
    {
        "discarded",
        "duplicate",
        "low_quality",
        "not_relevant",
        "unsafe",
        "user_discarded",
        "wrong_timing",
    }
)
_REGENERATABLE_STATUSES = ["pending_approval", "approved", "expired"]
_DISCARDABLE_STATUSES = [
    "pending_approval",
    "approved",
    "expired",
    "publication_failed",
]


@dataclass(frozen=True)
class _PreparedDraft:
    text: str
    category: str
    source_ids: List[int]
    score_data: Dict
    intended_slot: str


def _aware_datetime(value) -> Optional[datetime]:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _slot_iso(value) -> Optional[str]:
    parsed = _aware_datetime(value)
    return parsed.isoformat() if parsed else None


def _safe_reason_codes(reasons) -> List[str]:
    if not isinstance(reasons, (list, tuple)):
        return ["invalid_reason_code"]
    safe = []
    for reason in reasons:
        if not isinstance(reason, str) or not _REASON_CODE.fullmatch(reason):
            safe.append("invalid_reason_code")
            continue
        base, separator, suffix = reason.partition(":")
        if base == "unsupported_claim_type":
            safe.append(base)
        elif (
            base == "unsupported_claim"
            and separator
            and suffix in SUPPORTED_CLAIM_TYPES
        ):
            safe.append(reason)
        elif (
            base == "disclosure_not_approved"
            and separator
            and suffix in INCIDENT_SUBTYPES
        ):
            safe.append(reason)
        elif not separator and base in _SAFE_SIMPLE_FACT_REASONS:
            safe.append(base)
        else:
            safe.append("invalid_reason_code")
    return safe


def _safe_scores(score) -> Dict:
    if not isinstance(score, dict):
        return {}
    return {
        key: value
        for key, value in score.items()
        if key in _SCORE_KEYS
        and not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    }


class DraftPipeline:
    """Build drafts through ordered gates; never publish or fall back."""

    def __init__(
        self,
        db,
        planner,
        generator,
        fact_guard,
        scorer,
        score_threshold: int = 75,
        duplicate_threshold: float = 0.72,
        now_fn=None,
    ):
        self.db = db
        self.planner = planner
        self.generator = generator
        self.fact_guard = fact_guard
        self.scorer = scorer
        self.score_threshold = score_threshold
        self.duplicate_threshold = duplicate_threshold
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def _record(self, slot, category, outcome, details=None):
        slot_iso = _slot_iso(slot)
        if slot_iso is None:
            return
        self.db.record_draft_evaluation(
            slot_iso,
            category or "unplanned",
            outcome,
            details or {},
        )

    def _source_context(
        self,
        *,
        category: str,
        source_ids,
        intended_slot,
    ):
        slot_iso = _slot_iso(intended_slot)
        if slot_iso is None:
            return None

        safe_source_ids = [
            source_id
            for source_id in source_ids or []
            if isinstance(source_id, int) and not isinstance(source_id, bool)
        ]
        sources = self.db.get_eligible_content_sources(
            safe_source_ids,
            now=self.now_fn(),
        )
        if not safe_source_ids or len(sources) != len(safe_source_ids):
            self._record(
                slot_iso,
                category,
                "no_eligible_source",
                {"source_ids": safe_source_ids},
            )
            return None
        return slot_iso, safe_source_ids, sources

    def _validate_copy(
        self,
        *,
        text,
        category: str,
        safe_source_ids,
        sources,
        slot_iso: str,
        exclude_draft_id: Optional[int] = None,
    ) -> Optional[_PreparedDraft]:
        if not isinstance(text, str):
            text = ""
        text = text.strip()
        if not text:
            self._record(
                slot_iso,
                category,
                "generation_failed",
                {"source_ids": safe_source_ids},
            )
            return None
        if len(text) > 280:
            try:
                text = self.generator.rewrite_to_limit(text, sources, 280)
            except Exception:
                self._record(
                    slot_iso,
                    category,
                    "rewrite_failed",
                    {
                        "reason_codes": ["rewrite_unavailable"],
                        "source_ids": safe_source_ids,
                    },
                )
                return None
            if not isinstance(text, str):
                text = ""
            text = text.strip()
            if not text or len(text) > 280:
                self._record(
                    slot_iso,
                    category,
                    "rewrite_failed",
                    {"source_ids": safe_source_ids},
                )
                return None

        try:
            fact_result = self.fact_guard.check(text, sources)
        except Exception:
            self._record(
                slot_iso,
                category,
                "rejected_fact",
                {
                    "reason_codes": ["fact_check_unavailable"],
                    "source_ids": safe_source_ids,
                },
            )
            return None
        if getattr(fact_result, "approved", False) is not True:
            self._record(
                slot_iso,
                category,
                "rejected_fact",
                {
                    "reason_codes": _safe_reason_codes(
                        getattr(fact_result, "reasons", [])
                    ),
                    "source_ids": safe_source_ids,
                },
            )
            return None

        try:
            score = self.scorer.score_draft(text)
        except Exception:
            self._record(
                slot_iso,
                category,
                "rejected_score",
                {
                    "reason_codes": ["scoring_unavailable"],
                    "source_ids": safe_source_ids,
                },
            )
            return None
        safe_score = _safe_scores(score)
        total = safe_score.get("total")
        if (
            total is None
            or not 0 <= total <= 100
            or total < self.score_threshold
        ):
            self._record(
                slot_iso,
                category,
                "rejected_score",
                {"scores": safe_score},
            )
            return None

        if exclude_draft_id is None:
            recent_texts = self.db.get_recent_content_texts(
                days=30,
                now=self.now_fn(),
            )
        else:
            recent_texts = self.db.get_recent_content_texts(
                days=30,
                exclude_draft_id=exclude_draft_id,
                now=self.now_fn(),
            )
        for previous in recent_texts:
            if not isinstance(previous, str):
                continue
            if semantic_similarity(text, previous) >= self.duplicate_threshold:
                self._record(
                    slot_iso,
                    category,
                    "rejected_duplicate",
                    {"source_ids": safe_source_ids},
                )
                return None

        return _PreparedDraft(
            text=text,
            category=category,
            source_ids=safe_source_ids,
            score_data=safe_score,
            intended_slot=slot_iso,
        )

    def _prepare(
        self,
        *,
        category: str,
        source_ids,
        intended_slot,
        include_link: bool,
    ) -> Optional[_PreparedDraft]:
        context = self._source_context(
            category=category,
            source_ids=source_ids,
            intended_slot=intended_slot,
        )
        if context is None:
            return None
        slot_iso, safe_source_ids, sources = context

        try:
            candidate = self.generator.generate_grounded_tweet(
                category,
                sources,
                include_link,
            )
        except Exception:
            self._record(
                slot_iso,
                category,
                "generation_failed",
                {
                    "reason_codes": ["generation_unavailable"],
                    "source_ids": safe_source_ids,
                },
            )
            return None
        if not isinstance(candidate, dict) or not isinstance(
            candidate.get("text"), str
        ):
            self._record(
                slot_iso,
                category,
                "generation_failed",
                {"source_ids": safe_source_ids},
            )
            return None
        return self._validate_copy(
            text=candidate["text"],
            category=category,
            safe_source_ids=safe_source_ids,
            sources=sources,
            slot_iso=slot_iso,
        )

    def _persist(self, prepared: _PreparedDraft) -> Optional[Dict]:
        draft, outcome = self.db.create_or_get_post_draft(
            text=prepared.text,
            category=prepared.category,
            source_ids=prepared.source_ids,
            score_data=prepared.score_data,
            intended_slot=prepared.intended_slot,
            publication_key="draft:" + uuid4().hex,
        )
        if outcome == "no_eligible_source":
            self._record(
                prepared.intended_slot,
                prepared.category,
                "no_eligible_source",
                {"source_ids": prepared.source_ids},
            )
            return None
        return draft

    def create_for_slot(self, intended_slot) -> Optional[Dict]:
        slot_iso = _slot_iso(intended_slot)
        if slot_iso is None:
            return None
        existing = self.db.get_active_draft_for_slot(slot_iso)
        if existing:
            return existing

        plan = self.planner.plan(intended_slot)
        if plan is None:
            self._record(slot_iso, "unplanned", "no_eligible_source")
            return None
        prepared = self._prepare(
            category=plan.category,
            source_ids=plan.source_ids,
            intended_slot=plan.intended_slot,
            include_link=plan.include_link,
        )
        return self._persist(prepared) if prepared else None

    def regenerate(self, draft_id) -> Optional[Dict]:
        prior = self.db.get_post_draft(draft_id)
        if not prior or prior.get("status") not in _REGENERATABLE_STATUSES:
            return None
        source_ids = prior.get("source_ids")
        intended_slot = prior.get("intended_slot")
        include_link = "http://" in prior.get("text", "") or "https://" in prior.get(
            "text", ""
        )
        prepared = self._prepare(
            category=prior.get("category"),
            source_ids=source_ids,
            intended_slot=intended_slot,
            include_link=include_link,
        )
        if prepared is None:
            return None
        try:
            replacement, outcome = self.db.replace_post_draft_atomic(
                prior_draft_id=draft_id,
                expected_revision=prior.get("revision", 0),
                expected_slot=intended_slot,
                expected_category=prior.get("category"),
                expected_source_ids=source_ids,
                text=prepared.text,
                score_data=prepared.score_data,
                publication_key="draft:" + uuid4().hex,
            )
        except Exception:
            self._record(
                intended_slot,
                prior.get("category"),
                "generation_failed",
                {
                    "reason_codes": ["draft_persistence_unavailable"],
                    "source_ids": source_ids,
                },
            )
            return None
        if outcome == "no_eligible_source":
            self._record(
                intended_slot,
                prior.get("category"),
                "no_eligible_source",
                {"source_ids": source_ids},
            )
            return None
        if outcome != "created":
            self._record(
                prepared.intended_slot,
                prepared.category,
                "regeneration_conflict",
                {
                    "supersedes_draft_id": draft_id,
                },
            )
            return None
        return replacement

    def edit(self, draft_id, text) -> Optional[Dict]:
        """Validate user copy through the canonical gates, then supersede it."""
        prior = self.db.get_post_draft(draft_id)
        if not prior or prior.get("status") != "pending_approval":
            return None
        source_ids = prior.get("source_ids")
        intended_slot = prior.get("intended_slot")
        context = self._source_context(
            category=prior.get("category"),
            source_ids=source_ids,
            intended_slot=intended_slot,
        )
        if context is None:
            return None
        slot_iso, safe_source_ids, sources = context
        prepared = self._validate_copy(
            text=text,
            category=prior.get("category"),
            safe_source_ids=safe_source_ids,
            sources=sources,
            slot_iso=slot_iso,
            exclude_draft_id=draft_id,
        )
        if prepared is None:
            return None
        try:
            replacement, outcome = self.db.replace_post_draft_atomic(
                prior_draft_id=draft_id,
                expected_revision=prior.get("revision", 0),
                expected_slot=intended_slot,
                expected_category=prior.get("category"),
                expected_source_ids=source_ids,
                text=prepared.text,
                score_data=prepared.score_data,
                publication_key="draft:" + uuid4().hex,
            )
        except Exception:
            self._record(
                intended_slot,
                prior.get("category"),
                "generation_failed",
                {
                    "reason_codes": ["draft_persistence_unavailable"],
                    "source_ids": source_ids,
                },
            )
            return None
        if outcome == "no_eligible_source":
            self._record(
                intended_slot,
                prior.get("category"),
                "no_eligible_source",
                {"source_ids": source_ids},
            )
            return None
        if outcome != "created":
            self._record(
                prepared.intended_slot,
                prepared.category,
                "regeneration_conflict",
                {"supersedes_draft_id": draft_id},
            )
            return None
        return replacement

    def edit_from_telegram_session(
        self,
        draft_id,
        text,
        *,
        state_key: str,
        expected_state_value: str,
        session_token: str,
    ):
        """Run canonical edit gates, then atomically consume and replace."""
        prior = self.db.get_post_draft(draft_id)
        if not prior or prior.get("status") != "pending_approval":
            return None, "rejected"
        source_ids = prior.get("source_ids")
        intended_slot = prior.get("intended_slot")
        context = self._source_context(
            category=prior.get("category"),
            source_ids=source_ids,
            intended_slot=intended_slot,
        )
        if context is None:
            return None, "rejected"
        slot_iso, safe_source_ids, sources = context
        prepared = self._validate_copy(
            text=text,
            category=prior.get("category"),
            safe_source_ids=safe_source_ids,
            sources=sources,
            slot_iso=slot_iso,
            exclude_draft_id=draft_id,
        )
        if prepared is None:
            return None, "rejected"
        replacement, outcome = self.db.replace_post_draft_consuming_state_atomic(
            state_key=state_key,
            expected_state_value=expected_state_value,
            prior_draft_id=draft_id,
            expected_revision=prior.get("revision", 0),
            expected_slot=intended_slot,
            expected_category=prior.get("category"),
            expected_source_ids=source_ids,
            text=prepared.text,
            score_data=prepared.score_data,
            publication_key="telegram-edit:" + session_token,
        )
        return replacement, outcome

    def approve(self, draft_id, approved_by) -> bool:
        draft = self.db.get_post_draft(draft_id)
        if not draft or draft.get("status") != "pending_approval":
            return False
        if not isinstance(approved_by, str) or not approved_by.strip():
            return False
        slot = _aware_datetime(draft.get("intended_slot"))
        if slot is None:
            return False
        return self.db.approve_post_draft_atomic(
            draft_id,
            draft.get("revision", 0),
            draft.get("intended_slot"),
            approved_by.strip(),
            self.now_fn,
        )

    def postpone(self, draft_id, new_slot) -> bool:
        draft = self.db.get_post_draft(draft_id)
        if not draft or draft.get("status") not in {
            "pending_approval",
            "approved",
            "expired",
        }:
            return False
        slot = _aware_datetime(new_slot)
        now = _aware_datetime(self.now_fn())
        if slot is None or now is None or slot <= now:
            return False
        slot_iso = slot.isoformat()
        return self.db.postpone_post_draft_atomic(
            draft_id,
            draft.get("revision", 0),
            ["pending_approval", "approved", "expired"],
            slot_iso,
        )

    def postpone_from_telegram_session(
        self,
        draft_id,
        new_slot,
        *,
        state_key: str,
        expected_state_value: str,
    ) -> str:
        """Atomically consume a Telegram session with draft postponement."""
        draft = self.db.get_post_draft(draft_id)
        if not draft or draft.get("status") not in {
            "pending_approval", "approved", "expired",
        }:
            return "draft_conflict"
        slot = _aware_datetime(new_slot)
        now = _aware_datetime(self.now_fn())
        if slot is None or now is None or slot <= now:
            return "draft_conflict"
        return self.db.postpone_post_draft_consuming_state_atomic(
            state_key=state_key,
            expected_state_value=expected_state_value,
            draft_id=draft_id,
            expected_revision=draft.get("revision", 0),
            expected_statuses=["pending_approval", "approved", "expired"],
            new_slot=slot.isoformat(),
        )

    def discard(self, draft_id, reason) -> bool:
        draft = self.db.get_post_draft(draft_id)
        if not draft or draft.get("status") not in _DISCARDABLE_STATUSES:
            return False
        reason_code = (
            reason
            if isinstance(reason, str) and reason in _DISCARD_REASON_CODES
            else "discarded"
        )
        transitioned = self.db.transition_post_draft(
            draft_id,
            _DISCARDABLE_STATUSES,
            "discarded",
            error=reason_code,
        )
        if transitioned:
            self.db.release_media_for_draft(draft_id)
        return transitioned
