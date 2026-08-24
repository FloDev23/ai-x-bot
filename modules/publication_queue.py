"""Durable replenishment of the bilingual editorial review queue."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from modules.review_translation import ReviewTranslation


logger = logging.getLogger(__name__)

_OUTCOMES = frozenset({
    "created",
    "existing",
    "queue_full",
    "pending_full",
    "daily_cap",
    "generation_rejected",
    "translation_pending",
    "failed",
})


@dataclass(frozen=True)
class QueueReplenishResult:
    outcome: str
    draft_id: Optional[int]
    announce: bool


def _aware_utc(value) -> Optional[datetime]:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


class QueueReplenisher:
    """Create one translated draft per safe claim; never send Telegram."""

    def __init__(
        self,
        *,
        db,
        pipeline,
        translator,
        media_matcher=None,
        operator_timezone: str = "Europe/Rome",
        approved_queue_target: int = 7,
        pending_review_limit: int = 3,
        daily_generation_cap: int = 4,
    ):
        if type(operator_timezone) is not str or not operator_timezone:
            raise ValueError("operator_timezone must be an IANA timezone")
        try:
            self.operator_zone = ZoneInfo(operator_timezone)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("operator_timezone must be an IANA timezone") from error
        for name, value in (
            ("approved_queue_target", approved_queue_target),
            ("pending_review_limit", pending_review_limit),
            ("daily_generation_cap", daily_generation_cap),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.db = db
        self.pipeline = pipeline
        self.translator = translator
        self.media_matcher = media_matcher
        self.approved_queue_target = approved_queue_target
        self.pending_review_limit = pending_review_limit
        self.daily_generation_cap = daily_generation_cap

    @staticmethod
    def _result(outcome: str, draft_id=None, announce=False) -> QueueReplenishResult:
        safe_outcome = outcome if outcome in _OUTCOMES else "failed"
        safe_draft_id = draft_id if type(draft_id) is int and draft_id > 0 else None
        return QueueReplenishResult(
            safe_outcome,
            safe_draft_id,
            bool(announce and safe_draft_id is not None),
        )

    def run(self, now: datetime) -> QueueReplenishResult:
        current = _aware_utc(now)
        if current is None:
            return self._result("failed")
        operator_date = current.astimezone(self.operator_zone).date()
        try:
            counts = self.db.get_queue_counts(operator_date, self.operator_zone.key)
        except Exception as error:
            logger.error("queue_count_failed error_type=%s", type(error).__name__)
            return self._result("failed")
        if not self._valid_counts(counts):
            return self._result("failed")
        if counts["approved_or_planned"] >= self.approved_queue_target:
            return self._result("queue_full")
        if counts["awaiting_translation"] + counts["awaiting_review"] >= (
            self.pending_review_limit
        ):
            return self._result("pending_full")

        cycle_key = current.astimezone(self.operator_zone).strftime("%Y%m%dT%H%M")
        try:
            claim = self.db.claim_replenishment(
                operator_date,
                self.daily_generation_cap,
                current,
                cycle_key=cycle_key,
            )
        except Exception as error:
            logger.error("queue_claim_failed error_type=%s", type(error).__name__)
            return self._result("failed")
        if not claim:
            return self._result("daily_cap")
        token = claim.get("token")
        anchor = self._claim_anchor(claim)
        if type(token) is not str or not token or anchor is None:
            self._release(token)
            return self._result("failed")

        try:
            draft, persistence_outcome = self.pipeline.create_for_queue_with_outcome(
                anchor
            )
        except Exception as error:
            logger.error(
                "queue_generation_failed error_type=%s", type(error).__name__
            )
            self._release(token)
            return self._result("failed")
        if (
            not draft
            or persistence_outcome not in {"created", "existing"}
            or type(draft.get("id")) is not int
            or draft["id"] <= 0
        ):
            self._release(token)
            return self._result("generation_rejected")
        draft_id = draft["id"]

        if persistence_outcome == "created" and self.media_matcher is not None:
            try:
                self.media_matcher.attach_best(draft_id)
            except Exception as error:
                logger.error(
                    "queue_media_match_failed error_type=%s", type(error).__name__
                )

        current_draft = self.db.get_queue_draft(draft_id)
        if current_draft is None:
            self._release(token)
            return self._result("failed", draft_id)
        translated = self._translate_snapshot(current_draft)
        if translated:
            current_draft = self.db.get_queue_draft(draft_id)

        completed = self.db.complete_replenishment_claim(token, draft_id)
        if not completed:
            self._release(token)
            return self._result(
                "existing" if persistence_outcome == "existing" else "failed",
                draft_id,
            )
        if not translated:
            return self._result("translation_pending", draft_id)
        return self._result(
            persistence_outcome,
            draft_id,
            announce=persistence_outcome == "created",
        )

    def retry_pending_translations(
        self,
        now: datetime,
        limit: int = 3,
        draft_id: Optional[int] = None,
    ) -> list[int]:
        if (
            _aware_utc(now) is None
            or type(limit) is not int
            or not 1 <= limit <= 20
            or (
                draft_id is not None
                and (type(draft_id) is not int or draft_id <= 0)
            )
        ):
            return []
        requested_draft_id = draft_id
        ready_ids = []
        attempts = 0
        try:
            if requested_draft_id is not None:
                requested = self.db.get_queue_draft(requested_draft_id)
                drafts = [requested] if requested is not None else []
            else:
                drafts = self.db.list_post_drafts(
                    ["pending_approval", "approved"],
                    limit=100,
                )
        except Exception as error:
            logger.error(
                "translation_retry_list_failed error_type=%s",
                type(error).__name__,
            )
            return []
        for draft in drafts:
            if attempts >= limit:
                break
            draft_id = draft.get("id") if isinstance(draft, dict) else None
            if (
                requested_draft_id is not None
                and draft_id != requested_draft_id
            ):
                continue
            queued = self.db.get_queue_draft(draft_id) if type(draft_id) is int else None
            if not queued or queued.get("translation_status") not in {
                "pending", "failed", "invalidated",
            }:
                continue
            attempts += 1
            if self._translate_snapshot(queued):
                ready_ids.append(draft_id)
        return ready_ids

    def _translate_snapshot(self, draft: dict) -> bool:
        text = draft.get("text")
        revision = draft.get("revision")
        if type(text) is not str or type(revision) is not int or revision < 0:
            return False
        try:
            result = self.translator.translate(text)
        except Exception as error:
            logger.error(
                "queue_translation_failed error_type=%s", type(error).__name__
            )
            return False
        if not isinstance(result, ReviewTranslation):
            return False
        latest = self.db.get_queue_draft(draft.get("id"))
        if (
            latest is None
            or latest.get("revision") != revision
            or latest.get("text") != text
        ):
            return False
        return self.db.save_review_translation(
            draft["id"],
            revision,
            result.text_it,
        )

    def _release(self, token) -> None:
        if type(token) is not str or not token:
            return
        try:
            self.db.release_replenishment_claim(token)
        except Exception as error:
            logger.error("queue_release_failed error_type=%s", type(error).__name__)

    @staticmethod
    def _claim_anchor(claim) -> Optional[datetime]:
        if type(claim) is not dict:
            return None
        claimed_at = claim.get("claimed_at")
        ordinal = claim.get("ordinal")
        if type(claimed_at) is not str or type(ordinal) is not int or ordinal <= 0:
            return None
        try:
            parsed = datetime.fromisoformat(claimed_at)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed + timedelta(microseconds=ordinal)

    @staticmethod
    def _valid_counts(counts) -> bool:
        required = {
            "awaiting_translation",
            "awaiting_review",
            "approved_available",
            "approved_or_planned",
            "planned_today",
            "blocked",
        }
        return (
            type(counts) is dict
            and set(counts) == required
            and all(type(counts[key]) is int and counts[key] >= 0 for key in required)
        )
