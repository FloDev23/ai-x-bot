"""Budgeted, deterministic, read-only discovery of relevant X accounts."""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from config import (
    GROWTH_DIGEST_LIMIT,
    GROWTH_NEW_PROFILE_BUDGET,
    GROWTH_PROFILE_CACHE_DAYS,
    GROWTH_QUERY_BUDGET,
    GROWTH_SCORE_THRESHOLD,
    GROWTH_SEED_ACCOUNTS,
)


logger = logging.getLogger(__name__)

DEFAULT_TOPIC_QUERIES = (
    '("gym owner" OR "studio owner" OR "box owner") '
    '(class OR schedule OR member OR booking) lang:en -is:retweet',
    '(pilates OR yoga OR fitness) (studio OR gym OR coach) '
    '(booking OR retention OR occupancy OR "drop-in") lang:en -is:retweet',
)
_PRIMARY_ROLE_TERMS = ("owner", "founder", "manager")
_AMPLIFIER_ROLE_TERMS = (
    "coach",
    "trainer",
    "fitness tech",
    "consultant",
    "journalist",
    "creator",
)
_END_USER_TERMS = (
    "studio",
    "gym",
    "box",
    "pilates",
    "yoga",
    "fitness",
    "athlete",
    "member",
)
_OPERATING_TOPIC_TERMS = (
    "class",
    "schedule",
    "retention",
    "member",
    "no-show",
    "occupancy",
    "booking",
    "drop-in",
)
_AFFINITY_TERMS = ("drop-in", "drop in", "class booking", "flexdropin")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value) -> Optional[datetime]:
    if type(value) is not str or not value:
        return None
    try:
        return _utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _contains(text: str, term: str) -> bool:
    pattern = r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])"
    if term in {"class", "member"}:
        pattern = r"(?<![a-z0-9])" + re.escape(term) + r"s?(?![a-z0-9])"
    return re.search(pattern, text) is not None


def passes_candidate_filters(
    profile: Dict,
    latest_post: Optional[Dict],
    now: datetime,
) -> Tuple[bool, str]:
    """Apply non-negotiable eligibility gates without an AI fallback."""
    current_time = _utc(now)
    if not isinstance(profile, dict) or profile.get("protected") is True:
        return False, "protected_profile"
    latest_post = latest_post if isinstance(latest_post, dict) else {}
    activity_at = _parse_datetime(latest_post.get("created_at"))
    if (
        latest_post.get("is_original") is not True
        or activity_at is None
        or activity_at > current_time
        or current_time - activity_at > timedelta(days=30)
    ):
        return False, "no_original_post_within_30_days"
    bio = profile.get("description")
    text = latest_post.get("text")
    bio_context = bio.strip() if type(bio) is str else ""
    post_context = text.strip() if type(text) is str else ""
    if not bio_context and not post_context:
        return False, "insufficient_bio_post_context"
    spam_signals = profile.get("spam_signals")
    if spam_signals or profile.get("follow_farming") is True:
        return False, "spam_or_follow_farming_signals"
    suppressed_until = _parse_datetime(profile.get("suppressed_until"))
    if suppressed_until is not None and suppressed_until > current_time:
        return False, "suppressed_within_30_days"
    return True, "accepted"


def score_growth_candidate(
    profile: Dict,
    latest_post: Optional[Dict],
    now: datetime,
) -> Dict:
    """Return the approved arithmetic 0-100 relevance score."""
    profile = profile if isinstance(profile, dict) else {}
    latest_post = latest_post if isinstance(latest_post, dict) else {}
    bio = profile.get("description")
    post_text = latest_post.get("text")
    bio_text = bio.lower() if type(bio) is str else ""
    recent_text = post_text.lower() if type(post_text) is str else ""
    reasons = []

    if any(_contains(bio_text, term) for term in _PRIMARY_ROLE_TERMS):
        segment = "primary"
        role_bio = 30
        reasons.append("primary_operator_role")
    elif any(_contains(bio_text, term) for term in _AMPLIFIER_ROLE_TERMS):
        segment = "amplifier"
        role_bio = 20
        reasons.append("amplifier_role")
    elif any(_contains(bio_text, term) for term in _END_USER_TERMS):
        segment = "end_user"
        role_bio = 10
        reasons.append("relevant_end_user")
    else:
        segment = "end_user"
        role_bio = 0

    topic_matches = sum(
        _contains(recent_text, term) for term in _OPERATING_TOPIC_TERMS
    )
    if topic_matches >= 2:
        recent_topic_fit = 25
        reasons.append("multiple_operating_topics")
    elif topic_matches == 1:
        recent_topic_fit = 15
        reasons.append("one_operating_topic")
    else:
        recent_topic_fit = 0

    current_time = _utc(now)
    activity_at = _parse_datetime(latest_post.get("created_at"))
    age = current_time - activity_at if activity_at is not None else None
    if age is not None and timedelta(0) <= age <= timedelta(days=7):
        activity = 15
        reasons.append("active_within_7_days")
    elif age is not None and timedelta(0) <= age <= timedelta(days=30):
        activity = 8
        reasons.append("active_within_30_days")
    else:
        activity = 0

    market = 15 if latest_post.get("lang") == "en" else 0
    if market:
        reasons.append("english_market")

    followers = profile.get("followers_count")
    following = profile.get("following_count")
    plausible_metrics = (
        type(followers) is int
        and type(following) is int
        and 10 <= followers <= 100_000_000
        and 0 <= following <= 1_000_000
        and not profile.get("spam_signals")
    )
    account_quality = 10 if plausible_metrics else 0
    if account_quality:
        reasons.append("plausible_public_metrics")

    combined_text = f"{bio_text} {recent_text}"
    affinity = 5 if any(
        _contains(combined_text, term) for term in _AFFINITY_TERMS
    ) else 0
    if affinity:
        reasons.append("direct_drop_in_affinity")

    components = {
        "role_bio": min(role_bio, 30),
        "recent_topic_fit": min(recent_topic_fit, 25),
        "activity": min(activity, 15),
        "market": min(market, 15),
        "account_quality": min(account_quality, 10),
        "affinity": min(affinity, 5),
    }
    total = min(sum(components.values()), 100)
    return {
        **components,
        "total": total,
        "audience_segment": segment,
        "reasons": reasons,
        "activity_at": activity_at.isoformat() if activity_at else None,
    }


class GrowthDiscovery:
    """Collect, score and persist X profiles without performing any X write."""

    def __init__(
        self,
        x_client,
        db,
        *,
        score_threshold: int = GROWTH_SCORE_THRESHOLD,
        query_budget: int = GROWTH_QUERY_BUDGET,
        new_profile_budget: int = GROWTH_NEW_PROFILE_BUDGET,
        profile_cache_days: int = GROWTH_PROFILE_CACHE_DAYS,
        digest_limit: int = GROWTH_DIGEST_LIMIT,
        seed_accounts: Sequence[str] = GROWTH_SEED_ACCOUNTS,
        topic_queries: Sequence[str] = DEFAULT_TOPIC_QUERIES,
    ):
        for name, value in (
            ("score_threshold", score_threshold),
            ("query_budget", query_budget),
            ("new_profile_budget", new_profile_budget),
            ("profile_cache_days", profile_cache_days),
            ("digest_limit", digest_limit),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if len(topic_queries) != 2:
            raise ValueError("topic_queries must contain exactly two queries")
        self.x = x_client
        self.db = db
        self.score_threshold = score_threshold
        self.query_budget = min(query_budget, 3)
        self.new_profile_budget = new_profile_budget
        self.profile_cache_days = profile_cache_days
        self.digest_limit = min(digest_limit, 5)
        self.seed_accounts = tuple(seed_accounts)
        self.topic_queries = tuple(topic_queries)

    @staticmethod
    def _state_count(db, key: str) -> int:
        try:
            value = int(db.get_state(key, "0"))
        except (TypeError, ValueError):
            return 2**31 - 1
        return max(value, 0)

    @staticmethod
    def _valid_profile(profile) -> bool:
        if not isinstance(profile, dict):
            return False
        user_id = profile.get("user_id", profile.get("id"))
        username = profile.get("username")
        return (
            type(user_id) is str
            and bool(user_id)
            and type(username) is str
            and re.fullmatch(r"[A-Za-z0-9_]{1,15}", username) is not None
        )

    def _read_followers(self) -> List[Dict]:
        try:
            result = self.x.get_followers_profiles()
        except Exception as error:
            logger.warning(
                "x_growth_followers_failed error_type=%s", type(error).__name__
            )
            return []
        return list(result) if isinstance(result, (list, tuple)) else []

    def _query_sources(self, day_key: str) -> List[Tuple[str, List[Dict]]]:
        count_key = f"growth_queries:{day_key}"
        used = self._state_count(self.db, count_key)
        remaining = max(0, self.query_budget - used)
        try:
            offset = int(self.db.get_state("growth_source_offset", "0")) % 3
        except (TypeError, ValueError):
            offset = 0
        sources = [
            (
                f"topic_search:{self.topic_queries[0]}",
                lambda: self.x.search_recent_authors(self.topic_queries[0]),
            ),
            (
                f"topic_search:{self.topic_queries[1]}",
                lambda: self.x.search_recent_authors(self.topic_queries[1]),
            ),
            ("network", lambda: self.x.get_network_candidates(self.seed_accounts)),
        ]
        ordered = sources[offset:] + sources[:offset]
        results = []
        for source, read in ordered[:remaining]:
            used += 1
            self.db.set_state(count_key, str(used))
            try:
                profiles = read()
            except Exception as error:
                logger.warning(
                    "x_growth_source_failed source=%s error_type=%s",
                    source,
                    type(error).__name__,
                )
                profiles = []
            results.append((
                source,
                list(profiles) if isinstance(profiles, (list, tuple)) else [],
            ))
        if remaining:
            self.db.set_state("growth_source_offset", str((offset - 1) % 3))
        return results

    def run(self, now: datetime) -> List[Dict]:
        current_time = _utc(now)
        observed_on = current_time.date().isoformat()
        followers = self._read_followers()
        follower_candidates = []
        for candidate_profile in followers:
            if not self._valid_profile(candidate_profile):
                continue
            self.db.save_follower_snapshot(
                observed_on,
                candidate_profile,
                relevant=False,
                source="x_followers",
            )
            follower_candidates.append(candidate_profile)

        collected: List[Tuple[str, Dict]] = [
            ("new_follower", candidate_profile)
            for candidate_profile in follower_candidates
        ]
        for source, profiles in self._query_sources(observed_on):
            collected.extend(
                (source, candidate_profile) for candidate_profile in profiles
            )

        evaluation_key = f"growth_profile_evaluations:{observed_on}"
        evaluations = self._state_count(self.db, evaluation_key)
        seen = set()
        for source, candidate_profile in collected:
            if not self._valid_profile(candidate_profile):
                continue
            user_id = candidate_profile.get("user_id", candidate_profile.get("id"))
            if user_id in seen:
                continue
            seen.add(user_id)
            if self.db.is_growth_candidate_suppressed(user_id, current_time):
                continue
            cached = self.db.get_cached_growth_candidate(user_id, current_time)
            if cached is not None:
                if source == "new_follower":
                    score_data = cached.get("score_data") or {}
                    self.db.save_follower_snapshot(
                        observed_on,
                        candidate_profile,
                        relevant=(
                            score_data.get("hard_filter_passed") is not False
                            and cached["score"] >= self.score_threshold
                        ),
                        source=f"candidate:{cached['id']}",
                    )
                continue
            if evaluations >= self.new_profile_budget:
                break

            evaluations += 1
            self.db.set_state(evaluation_key, str(evaluations))
            try:
                latest_post = self.x.get_latest_original_post(user_id)
            except Exception as error:
                logger.warning(
                    "x_growth_profile_read_failed user_id=%s error_type=%s",
                    user_id,
                    type(error).__name__,
                )
                latest_post = None
            passed, filter_reason = passes_candidate_filters(
                candidate_profile,
                latest_post,
                current_time,
            )
            score_data = score_growth_candidate(
                candidate_profile,
                latest_post,
                current_time,
            )
            score_data["hard_filter_passed"] = passed
            score_data["filter_reason"] = filter_reason
            candidate_id = self.db.upsert_growth_candidate({
                "user_id": user_id,
                "username": candidate_profile["username"],
                "profile": candidate_profile,
                "latest_post": latest_post,
                "score": score_data["total"],
                "score_data": score_data,
                "discovery_source": source,
                "last_evaluated_at": current_time.isoformat(),
                "profile_expires_at": (
                    current_time + timedelta(days=self.profile_cache_days)
                ).isoformat(),
            })
            if source == "new_follower":
                self.db.save_follower_snapshot(
                    observed_on,
                    candidate_profile,
                    relevant=passed and score_data["total"] >= self.score_threshold,
                    source=f"candidate:{candidate_id}",
                )

        return self.db.get_digest_candidates(
            limit=self.digest_limit,
            now=current_time,
            threshold=self.score_threshold,
        )
