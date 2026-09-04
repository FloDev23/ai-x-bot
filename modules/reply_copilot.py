"""Safe primitives for Telegram-only, manually published X reply drafts."""

import logging
import re
import secrets
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo


logger = logging.getLogger(__name__)


_OPERATOR_REASON_CODES = frozenset({
    "gym_owner",
    "empty_capacity",
    "booking_problem",
    "fitness_operations",
})
_END_USER_REASON_CODES = frozenset({
    "explicit_intent",
    "travel_context",
    "day_pass_model",
    "drop_in",
    "urgency",
    "discipline_match",
})
_URL_PATTERN = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_DOMAIN_PATTERN = re.compile(
    r"(?<![@\w])(?:[A-Z0-9-]+\.)+[A-Z]{2,63}(?:/[A-Z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?",
    re.IGNORECASE,
)
_EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])",
    re.IGNORECASE,
)
_HASHTAG_PATTERN = re.compile(r"(?<!\w)#[A-Z0-9_]+", re.IGNORECASE)
_MENTION_PATTERN = re.compile(r"(?<!\w)@[A-Z0-9_]+", re.IGNORECASE)
_BRAND_PATTERN = re.compile(r"\bflex[\s_.-]*drop[\s_.-]*in\b", re.IGNORECASE)
_PROMOTION_PATTERN = re.compile(
    r"\b(?:download(?:\s+the)?\s+app|sign\s+up|try\s+our\s+app|"
    r"visit\s+our\s+(?:site|website)|book\s+with\s+us|dm\s+us|"
    r"learn\s+more|follow\s+us|check\s+out\s+our|contact\s+us|"
    r"get\s+started|we\s+can\s+help|our\s+(?:app|platform|product|"
    r"service|solution))\b",
    re.IGNORECASE,
)
_PROMPT_INJECTION_PATTERN = re.compile(
    r"\b(?:ignore\s+(?:all\s+|any\s+|the\s+|previous\s+|prior\s+)*"
    r"instructions?|reveal\s+(?:the\s+)?system\s+prompt|system\s+prompt|"
    r"developer\s+message)\b",
    re.IGNORECASE,
)
_HIGH_RISK_PATTERN = re.compile(
    r"\b(?:diagnos(?:e|is|ing)|prescri(?:be|ption)|medical\s+advice|"
    r"legal\s+advice|lawyer|attorney|emergency|suicid(?:e|al)|self[- ]harm|"
    r"overdose|crisis\s+line)\b",
    re.IGNORECASE,
)
_TWEET_ID_PATTERN = re.compile(r"[1-9][0-9]{0,19}")


def classify_reply_segment(reason_codes: object) -> Optional[str]:
    """Map trusted Growth Digest reason codes to the approved audience split."""
    if not isinstance(reason_codes, (list, tuple, set, frozenset)):
        return None
    normalized = {
        value for value in reason_codes
        if isinstance(value, str) and value
    }
    if normalized & _OPERATOR_REASON_CODES:
        return "operator"
    if normalized & _END_USER_REASON_CODES:
        return "end_user"
    return None


def normalize_and_validate_reply(value: object) -> Optional[str]:
    """Return normalized safe reply copy, or ``None`` when it fails closed."""
    if not isinstance(value, str):
        return None
    try:
        if any(unicodedata.category(character).startswith("C") for character in value):
            return None
        normalized = unicodedata.normalize("NFKC", value)
    except (TypeError, ValueError, UnicodeError):
        return None
    normalized = " ".join(normalized.strip().split())
    if not 30 <= len(normalized) <= 256:
        return None
    if any(pattern.search(normalized) for pattern in (
        _URL_PATTERN,
        _DOMAIN_PATTERN,
        _EMAIL_PATTERN,
        _HASHTAG_PATTERN,
        _MENTION_PATTERN,
        _BRAND_PATTERN,
        _PROMOTION_PATTERN,
        _PROMPT_INJECTION_PATTERN,
        _HIGH_RISK_PATTERN,
    )):
        return None
    return normalized


def build_reply_web_intent(tweet_id: object, reply_text: object) -> Optional[str]:
    """Build a manual X composer URL without using X API credentials."""
    if not isinstance(tweet_id, str) or _TWEET_ID_PATTERN.fullmatch(tweet_id) is None:
        return None
    reply = normalize_and_validate_reply(reply_text)
    if reply is None:
        return None
    return "https://x.com/intent/tweet?" + urlencode({
        "in_reply_to": tweet_id,
        "text": reply,
    })


class ReplyCopilotService:
    """Create durable reply drafts without any X client or publisher dependency."""

    def __init__(
        self,
        db,
        generator,
        *,
        daily_limit: int = 5,
        max_age_hours: int = 48,
        max_regenerations: int = 2,
        clock=None,
        token_factory=None,
        claim_ttl: timedelta = timedelta(minutes=5),
    ):
        if (
            type(daily_limit) is not int
            or not 1 <= daily_limit <= 5
            or type(max_age_hours) is not int
            or not 1 <= max_age_hours <= 48
            or type(max_regenerations) is not int
            or not 1 <= max_regenerations <= 2
            or type(claim_ttl) is not timedelta
            or not 0 < claim_ttl.total_seconds() <= 900
        ):
            raise ValueError("invalid Reply Copilot limits")
        self.db = db
        self.generator = generator
        self.daily_limit = daily_limit
        self.max_age_hours = max_age_hours
        self.max_regenerations = max_regenerations
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.token_factory = token_factory or (lambda: secrets.token_urlsafe(24))
        self.claim_ttl = claim_ttl

    @staticmethod
    def _aware_utc(value: object) -> Optional[datetime]:
        if (
            type(value) is not datetime
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            return None
        return value.astimezone(timezone.utc)

    @classmethod
    def _parse_aware_utc(cls, value: object) -> Optional[datetime]:
        if not isinstance(value, str):
            return None
        try:
            return cls._aware_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except (TypeError, ValueError, OverflowError):
            return None

    def _now(self, value: Optional[datetime]) -> Optional[datetime]:
        try:
            current = self.clock() if value is None else value
        except Exception as error:
            logger.warning(
                "reply_clock_failed error_type=%s", type(error).__name__,
            )
            return None
        return self._aware_utc(current)

    @staticmethod
    def _empty_summary(observed_on: object, outcome: str) -> Dict:
        return {
            "observed_on": observed_on if isinstance(observed_on, str) else "",
            "outcome": outcome,
            "reserved": 0,
            "ready": 0,
            "failed": 0,
            "suggestions": [],
        }

    def _summary(self, observed_on: str, outcome: str) -> Dict:
        counts = self.db.get_reply_copilot_counts(observed_on)
        return {
            "observed_on": observed_on,
            "outcome": outcome,
            "reserved": counts["reserved"],
            "ready": counts["ready"],
            "failed": counts["generation_failed"],
            "suggestions": self.db.list_reply_suggestions(observed_on),
        }

    def _eligible_candidates(
        self,
        digest: Dict,
        current: datetime,
    ) -> List[Dict]:
        posts = digest.get("posts")
        if type(posts) is not list:
            return []
        ranked = []
        for suggestion in posts:
            if type(suggestion) is not dict:
                continue
            payload = suggestion.get("payload")
            reason_codes = suggestion.get("reason_codes")
            segment = classify_reply_segment(reason_codes)
            created_at = self._parse_aware_utc(
                payload.get("created_at") if type(payload) is dict else None
            )
            suggestion_id = suggestion.get("id")
            tweet_id = suggestion.get("object_id")
            username = suggestion.get("username")
            excerpt = payload.get("excerpt") if type(payload) is dict else None
            score = suggestion.get("score")
            age = current - created_at if created_at is not None else None
            if (
                type(suggestion_id) is not int
                or suggestion_id <= 0
                or suggestion.get("kind") != "post"
                or suggestion.get("decision") != "new"
                or not isinstance(tweet_id, str)
                or _TWEET_ID_PATTERN.fullmatch(tweet_id) is None
                or not isinstance(username, str)
                or re.fullmatch(r"[A-Za-z0-9_]{1,15}", username) is None
                or username.casefold() == "flexdropin"
                or not isinstance(excerpt, str)
                or excerpt != excerpt.strip()
                or not 1 <= len(excerpt) <= 500
                or type(score) is not int
                or not 0 <= score <= 100
                or segment is None
                or age is None
                or age < timedelta(0)
                or age > timedelta(hours=self.max_age_hours)
                or payload.get("id") != tweet_id
                or payload.get("author_username") != username
                or payload.get("reason_codes") != reason_codes
            ):
                continue
            ranked.append({
                "growth_suggestion_id": suggestion_id,
                "tweet_id": tweet_id,
                "author_username": username,
                "source_excerpt": excerpt,
                "audience_segment": segment,
                "relevance_score": score,
                "_created_at": created_at,
            })
        ranked.sort(key=lambda item: (
            -item["relevance_score"],
            -item["_created_at"].timestamp(),
            item["growth_suggestion_id"],
        ))
        for rank, item in enumerate(ranked):
            item["_rank"] = rank
        return ranked

    def _allocate(
        self,
        candidates: List[Dict],
        observed_on: str,
        existing: List[Dict],
    ) -> List[Dict]:
        remaining = max(0, self.daily_limit - len(existing))
        if remaining == 0:
            return []
        known_tweets = {row.get("tweet_id") for row in existing}
        available = [
            item for item in candidates if item["tweet_id"] not in known_tweets
        ]
        if self.daily_limit == 5:
            operator_target = (
                4 if date.fromisoformat(observed_on).toordinal() % 2 == 0 else 3
            )
        else:
            operator_target = max(1, round(self.daily_limit * 0.7))
        end_user_target = self.daily_limit - operator_target
        existing_operator = sum(
            row.get("audience_segment") == "operator" for row in existing
        )
        existing_end_user = sum(
            row.get("audience_segment") == "end_user" for row in existing
        )
        operator_need = max(0, operator_target - existing_operator)
        end_user_need = max(0, end_user_target - existing_end_user)
        operators = [
            item for item in available if item["audience_segment"] == "operator"
        ]
        end_users = [
            item for item in available if item["audience_segment"] == "end_user"
        ]
        selected = operators[:operator_need] + end_users[:end_user_need]
        selected_ids = {item["growth_suggestion_id"] for item in selected}
        selected.extend(
            item for item in available
            if item["growth_suggestion_id"] not in selected_ids
        )
        selected = sorted(selected[:remaining], key=lambda item: item["_rank"])
        return [{
            key: value for key, value in item.items() if not key.startswith("_")
        } for item in selected]

    def _generate_row(
        self,
        row: Dict,
        current: datetime,
    ) -> tuple[Optional[Dict], str]:
        try:
            token = self.token_factory()
        except Exception as error:
            logger.warning(
                "reply_generation_token_failed reply_id=%s error_type=%s",
                row.get("id"), type(error).__name__,
            )
            return self.db.get_reply_suggestion(row.get("id")), "rejected"
        claim = self.db.claim_reply_generation(
            row.get("id"),
            row.get("revision"),
            token,
            current,
            self.claim_ttl,
            1 + self.max_regenerations,
        )
        if claim is None:
            return self.db.get_reply_suggestion(row.get("id")), "rejected"
        try:
            generated = self.generator.generate_value_reply(claim.source_excerpt)
        except Exception as error:
            logger.warning(
                "reply_generation_failed reply_id=%s error_type=%s",
                claim.reply_id, type(error).__name__,
            )
            completed = self.db.fail_reply_generation(
                claim, "provider_unavailable", current,
            )
            return (
                self.db.get_reply_suggestion(claim.reply_id),
                "failed" if completed else "rejected",
            )
        reply = normalize_and_validate_reply(generated)
        if reply is None:
            completed = self.db.fail_reply_generation(
                claim, "invalid_output", current,
            )
            return (
                self.db.get_reply_suggestion(claim.reply_id),
                "failed" if completed else "rejected",
            )
        completed = self.db.complete_reply_generation(claim, reply, current)
        return (
            self.db.get_reply_suggestion(claim.reply_id),
            "updated" if completed else "rejected",
        )

    def build(
        self,
        observed_on: str,
        now: Optional[datetime] = None,
    ) -> Dict:
        current = self._now(now)
        try:
            valid_date = date.fromisoformat(observed_on).isoformat() == observed_on
        except (TypeError, ValueError):
            valid_date = False
        if (
            current is None
            or not valid_date
            or current.astimezone(ZoneInfo("Europe/Rome")).date().isoformat()
            != observed_on
        ):
            return self._empty_summary(observed_on, "invalid")
        digest = self.db.get_growth_digest(observed_on)
        if type(digest) is not dict or digest.get("observed_on") != observed_on:
            return self._empty_summary(observed_on, "no_digest")
        existing = self.db.list_reply_suggestions(observed_on)
        candidates = self._eligible_candidates(digest, current)
        known_tweets = self.db.get_existing_reply_tweet_ids([
            candidate["tweet_id"] for candidate in candidates
        ])
        current_tweets = {row.get("tweet_id") for row in existing}
        candidates = [
            candidate for candidate in candidates
            if candidate["tweet_id"] not in known_tweets
            or candidate["tweet_id"] in current_tweets
        ]
        selected = self._allocate(candidates, observed_on, existing)
        inserted = self.db.reserve_reply_suggestions(
            observed_on, selected, self.daily_limit, current,
        )
        pending = self.db.list_reply_suggestions(
            observed_on, ["reserved"],
        )
        for row in pending:
            self._generate_row(row, current)
        rows = self.db.list_reply_suggestions(observed_on)
        if inserted:
            outcome = "created"
        elif rows:
            outcome = "existing"
        else:
            outcome = "no_candidates"
        return self._summary(observed_on, outcome)

    def list(
        self,
        observed_on: str,
        statuses: Optional[List[str]] = None,
    ) -> List[Dict]:
        return self.db.list_reply_suggestions(observed_on, statuses)

    def regenerate(
        self,
        reply_id: int,
        expected_revision: int,
        now: Optional[datetime] = None,
    ) -> tuple[Optional[Dict], str]:
        current = self._now(now)
        if current is None:
            return None, "invalid"
        row = self.db.get_reply_suggestion(reply_id, expected_revision)
        if row is None or row.get("status") not in {"ready", "generation_failed"}:
            return self.db.get_reply_suggestion(reply_id), "rejected"
        return self._generate_row(row, current)

    def dismiss(
        self,
        reply_id: int,
        expected_revision: int,
        now: Optional[datetime] = None,
    ) -> tuple[Optional[Dict], str]:
        return self._transition(
            reply_id, expected_revision, "dismissed", now,
        )

    def mark_published(
        self,
        reply_id: int,
        expected_revision: int,
        now: Optional[datetime] = None,
    ) -> tuple[Optional[Dict], str]:
        return self._transition(
            reply_id, expected_revision, "published_manually", now,
        )

    def _transition(
        self,
        reply_id: int,
        expected_revision: int,
        target_status: str,
        now: Optional[datetime],
    ) -> tuple[Optional[Dict], str]:
        current = self._now(now)
        if current is None:
            return None, "invalid"
        outcome = self.db.transition_reply_suggestion(
            reply_id, expected_revision, target_status, current,
        )
        return self.db.get_reply_suggestion(reply_id), outcome

    def counts(self, observed_on: str) -> Dict[str, int]:
        return self.db.get_reply_copilot_counts(observed_on)
