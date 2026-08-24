"""Durable replenishment of the bilingual editorial review queue."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import re
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


class PublicationPlanner:
    """Create two stable US publication positions and fill them safely."""

    _INSTALLATION_STATE_KEY = "adaptive_publication_installation_id"

    def __init__(
        self,
        *,
        db,
        timing_policy,
        timing_sample_provider,
        now_fn,
        audience_timezone: str,
        installation_id_provider,
        source_expiry_safety_margin: timedelta,
        max_links_per_week: int,
        dry_run: bool,
    ):
        if type(audience_timezone) is not str or not audience_timezone:
            raise ValueError("audience_timezone must be an IANA timezone")
        try:
            self.audience_zone = ZoneInfo(audience_timezone)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("audience_timezone must be an IANA timezone") from error
        if not callable(timing_sample_provider) or not callable(now_fn):
            raise ValueError("timing providers must be callable")
        if not callable(installation_id_provider):
            raise ValueError("installation_id_provider must be callable")
        if (
            type(source_expiry_safety_margin) is not timedelta
            or source_expiry_safety_margin < timedelta(0)
            or source_expiry_safety_margin > timedelta(days=30)
        ):
            raise ValueError("invalid source expiry safety margin")
        if type(max_links_per_week) is not int or max_links_per_week < 0:
            raise ValueError("max_links_per_week must be non-negative")
        if type(dry_run) is not bool:
            raise ValueError("dry_run must be exact bool")
        self.db = db
        self.timing_policy = timing_policy
        self.timing_sample_provider = timing_sample_provider
        self.now_fn = now_fn
        self.installation_id_provider = installation_id_provider
        self.source_expiry_safety_margin = source_expiry_safety_margin
        self.max_links_per_week = max_links_per_week
        self.dry_run = dry_run

    def _now(self, supplied=None) -> Optional[datetime]:
        value = self.now_fn() if supplied is None else supplied
        return _aware_utc(value)

    def _installation_id(self) -> Optional[str]:
        try:
            existing = self.db.get_state(self._INSTALLATION_STATE_KEY)
        except Exception as error:
            logger.error(
                "installation_state_failed error_type=%s", type(error).__name__
            )
            return None
        if (
            type(existing) is str
            and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", existing) is not None
        ):
            return existing
        try:
            candidate = self.installation_id_provider()
        except Exception as error:
            logger.error(
                "installation_id_failed error_type=%s", type(error).__name__
            )
            return None
        if (
            type(candidate) is not str
            or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", candidate) is None
        ):
            return None
        try:
            stored = self.db.get_or_create_state(
                self._INSTALLATION_STATE_KEY,
                candidate,
            )
        except Exception as error:
            logger.error(
                "installation_state_failed error_type=%s", type(error).__name__
            )
            return None
        return (
            stored
            if type(stored) is str
            and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", stored) is not None
            else None
        )

    def ensure_day(self, now=None) -> list[dict]:
        current = self._now(now)
        installation_id = self._installation_id()
        if current is None or installation_id is None:
            return []
        local_date = current.astimezone(self.audience_zone).date()
        try:
            samples = self.timing_sample_provider(current)
            decision = self.timing_policy.choose(
                local_date,
                installation_id,
                samples if isinstance(samples, (list, tuple)) else (),
            )
            return self.db.create_or_get_publication_positions(
                local_date,
                decision,
                current,
            )
        except Exception as error:
            logger.error("publication_day_failed error_type=%s", type(error).__name__)
            return []

    @staticmethod
    def _parse_aware(value) -> Optional[datetime]:
        if type(value) is not str or not value or len(value) > 64:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)

    def _candidate_rank(
        self,
        draft,
        scheduled: datetime,
        current: datetime,
        selected_categories: set[str],
        selected_formats: set[str],
    ):
        if type(draft) is not dict:
            return None
        draft_id = draft.get("id")
        text = draft.get("text")
        category = draft.get("category")
        source_ids = draft.get("source_ids")
        score_data = draft.get("score_data")
        approval = self._parse_aware(draft.get("approved_queue_at"))
        if (
            type(draft_id) is not int
            or draft_id <= 0
            or type(text) is not str
            or not text.strip()
            or len(text) > 280
            or type(category) is not str
            or not category.strip()
            or type(source_ids) is not list
            or not source_ids
            or any(type(source_id) is not int or source_id <= 0 for source_id in source_ids)
            or type(score_data) is not dict
            or type(score_data.get("total")) is not int
            or not 0 <= score_data["total"] <= 100
            or approval is None
            or approval > current
        ):
            return None
        cutoff = scheduled + self.source_expiry_safety_margin
        expiries = []
        for source_id in source_ids:
            source = self.db.get_content_source(source_id)
            if type(source) is not dict or source.get("trust_state") != "verified":
                return None
            raw_expiry = source.get("expires_at")
            if raw_expiry is None:
                continue
            expiry = self._parse_aware(raw_expiry)
            if expiry is None or expiry <= cutoff:
                return None
            expiries.append(expiry)
        has_expiry = bool(expiries)
        seconds_to_expiry = (
            int((min(expiries) - cutoff).total_seconds())
            if expiries
            else 10**12
        )
        media_format = "media" if type(draft.get("media_id")) is int else "text"
        category_diversity = int(category not in selected_categories)
        format_diversity = int(media_format not in selected_formats)
        approval_age = min(31_536_000, max(0, int((current - approval).total_seconds())))
        reason = {
            "source_urgency": min(1_000_000, max(0, seconds_to_expiry // 3600))
            if has_expiry else 0,
            "score": score_data["total"],
            "category_diversity": category_diversity,
            "format_diversity": format_diversity,
            "approval_age": approval_age,
        }
        ranking = (
            int(has_expiry),
            -seconds_to_expiry,
            score_data["total"],
            category_diversity,
            format_diversity,
            approval_age,
            -draft_id,
        )
        return ranking, reason, media_format, cutoff

    def reconcile(self, now=None) -> list[dict]:
        current = self._now(now)
        if current is None:
            return []
        plans = self.ensure_day(current)
        if len(plans) != 2:
            return []
        selected_categories = set()
        selected_formats = set()
        simulated_ids = self.db.get_simulated_draft_ids() if self.dry_run else set()
        final = []
        for initial_plan in sorted(plans, key=lambda item: item.get("position", 99)):
            if initial_plan.get("status") == "planned":
                draft = self.db.get_queue_draft(initial_plan.get("draft_id"))
                if draft:
                    selected_categories.add(draft.get("category"))
                    selected_formats.add(
                        "media" if type(draft.get("media_id")) is int else "text"
                    )
                final.append(initial_plan)
                continue
            if initial_plan.get("status") != "open":
                final.append(initial_plan)
                continue
            scheduled = self._parse_aware(initial_plan.get("scheduled_for"))
            if scheduled is None or scheduled < current:
                final.append(initial_plan)
                continue
            rejected_ids = set()
            for _attempt in range(5):
                candidates = self.db.list_approved_queue(current)
                candidates = [
                    candidate for candidate in candidates
                    if candidate.get("id") not in rejected_ids
                ]
                if self.dry_run and any(
                    candidate.get("id") not in simulated_ids
                    for candidate in candidates
                ):
                    candidates = [
                        candidate for candidate in candidates
                        if candidate.get("id") not in simulated_ids
                    ]
                ranked = []
                current_links = self.db.count_links_last_days(7, now=current)
                for candidate in candidates:
                    if re.search(r"https?://", candidate.get("text", "")) and (
                        current_links >= self.max_links_per_week
                        or any(
                            re.search(r"https?://", planned.get("text", ""))
                            for planned in (
                                self.db.get_queue_draft(plan.get("draft_id"))
                                for plan in final
                            )
                            if isinstance(planned, dict)
                        )
                    ):
                        continue
                    ranked_value = self._candidate_rank(
                        candidate,
                        scheduled,
                        current,
                        selected_categories,
                        selected_formats,
                    )
                    if ranked_value is not None:
                        ranked.append((ranked_value[0], candidate, *ranked_value[1:]))
                if not ranked:
                    break
                _rank, winner, reason, media_format, cutoff = max(
                    ranked,
                    key=lambda item: item[0],
                )
                if self.db.assign_publication_plan_atomic(
                    initial_plan["id"],
                    winner["id"],
                    winner["revision"],
                    reason,
                    source_valid_at=cutoff,
                    max_links_per_week=self.max_links_per_week,
                ):
                    refreshed = self.db.list_publication_positions(
                        current.astimezone(self.audience_zone).date()
                    )
                    assigned = next(
                        (row for row in refreshed if row["id"] == initial_plan["id"]),
                        None,
                    )
                    if assigned is not None:
                        final.append(assigned)
                        selected_categories.add(winner["category"])
                        selected_formats.add(media_format)
                    break
                rejected_ids.add(winner["id"])
                refreshed = self.db.list_publication_positions(
                    current.astimezone(self.audience_zone).date()
                )
                concurrent = next(
                    (row for row in refreshed if row["id"] == initial_plan["id"]),
                    None,
                )
                if concurrent is not None and concurrent.get("status") != "open":
                    final.append(concurrent)
                    concurrent_draft = self.db.get_queue_draft(
                        concurrent.get("draft_id")
                    )
                    if concurrent_draft:
                        selected_categories.add(concurrent_draft.get("category"))
                        selected_formats.add(
                            "media"
                            if type(concurrent_draft.get("media_id")) is int
                            else "text"
                        )
                    break
            else:
                final.append(initial_plan)
        known = {plan.get("id") for plan in final}
        final.extend(plan for plan in plans if plan.get("id") not in known)
        return sorted(final, key=lambda item: item.get("position", 99))

    def simulate_due(self, now=None) -> list[dict]:
        current = self._now(now)
        if current is None or not self.dry_run:
            return []
        simulated = []
        for plan in self.db.list_publication_positions(statuses=["planned"]):
            scheduled = self._parse_aware(plan.get("scheduled_for"))
            if scheduled is None or scheduled > current:
                continue
            if self.db.mark_publication_plan_simulated(plan["id"], plan["revision"]):
                refreshed = self.db.list_publication_positions(
                    statuses=["simulated"]
                )
                row = next((item for item in refreshed if item["id"] == plan["id"]), None)
                if row is not None:
                    simulated.append(row)
        return simulated
