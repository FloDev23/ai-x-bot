"""Daily persisted read-only X growth suggestions for manual operator action."""

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from modules.growth_candidate_schema import parse_growth_datetime
from modules.growth_discovery import GrowthDiscovery


logger = logging.getLogger(__name__)

ROME = ZoneInfo("Europe/Rome")
GROWTH_POST_QUERY_BUDGET = 2
POST_QUERY_PORTFOLIO: Tuple[Tuple[str, str], ...] = (
    (
        "dropin_booking_operators",
        '("drop-in" OR "day pass" OR "class pass" OR "class booking" OR '
        '"studio booking" OR "gym booking" OR "drop in class") '
        '(gym OR studio OR fitness OR pilates OR yoga OR crossfit OR martial) '
        'lang:en -is:retweet -is:reply',
    ),
    (
        "fitness_capacity_management",
        '("gym owner" OR "studio owner" OR "fitness business" OR '
        '"gym management" OR "class capacity" OR "no-show" OR "member retention") '
        '(booking OR schedule OR capacity OR software OR app OR management) '
        'lang:en -is:retweet -is:reply',
    ),
)
_ACCOUNT_REASON_CODES = frozenset({
    "primary_operator_role", "amplifier_role", "relevant_end_user",
    "multiple_operating_topics", "one_operating_topic",
    "active_within_7_days", "active_within_30_days", "english_market",
    "plausible_public_metrics", "direct_drop_in_affinity",
})
_POST_METRIC_KEYS = frozenset({
    "like_count", "retweet_count", "reply_count", "quote_count",
    "impression_count",
})
_AUTHOR_METRIC_KEYS = frozenset({
    "followers_count", "following_count", "tweet_count", "listed_count",
})


def _canonical_id(value: object) -> bool:
    return (
        type(value) is str
        and value.isascii()
        and value.isdigit()
        and not value.startswith("0")
        and len(value) <= 20
        and int(value) <= (1 << 64) - 1
    )


def _username(value: object) -> bool:
    return type(value) is str and re.fullmatch(r"[A-Za-z0-9_]{1,15}", value) is not None


def _closed_metrics(value: object, keys: frozenset) -> bool:
    return (
        type(value) is dict
        and frozenset(value) == keys
        and all(
            type(metric) is int and 0 <= metric <= 1_000_000_000_000
            for metric in value.values()
        )
    )


def _contains(text: str, pattern: str) -> bool:
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def score_growth_post(post: Dict, now: datetime) -> Optional[Dict]:
    """Validate and score one closed normalized post with integer components."""
    if type(post) is not dict or type(now) is not datetime:
        return None
    post_id = post.get("id")
    author_id = post.get("author_id")
    username = post.get("author_username")
    text = post.get("text")
    created_at = parse_growth_datetime(post.get("created_at"))
    metrics = post.get("public_metrics")
    author_metrics = post.get("author_public_metrics")
    if (
        not _canonical_id(post_id)
        or not _canonical_id(author_id)
        or not _username(username)
        or type(text) is not str
        or not text.strip()
        or len(text) > 1000
        or post.get("lang") != "en"
        or created_at is None
        or not _closed_metrics(metrics, _POST_METRIC_KEYS)
        or not _closed_metrics(author_metrics, _AUTHOR_METRIC_KEYS)
    ):
        return None
    current = now.astimezone(timezone.utc)
    age = current - created_at
    if age < timedelta(0) or age > timedelta(days=30):
        return None
    lowered = text.lower()
    if not _contains(
        lowered,
        r"\b(?:gym|studio|class|fitness|crossfit|pilates|martial arts?|"
        r"bjj|jiu[ -]?jitsu|dojo|mma)\b",
    ):
        return None
    weighted_reasons = []
    if _contains(lowered, r"\b(?:gym|studio|box) owners?\b"):
        weighted_reasons.append(("gym_owner", 15))
    if _contains(
        lowered,
        r"\b(?:empty (?:class |studio )?(?:spot|spots|capacity)|"
        r"class capacity|unused capacity|fill (?:an? )?(?:class|spot))\b",
    ):
        weighted_reasons.append(("empty_capacity", 12))
    if _contains(lowered, r"\bdrop[ -]?ins?\b"):
        weighted_reasons.append(("drop_in", 12))
    if _contains(lowered, r"\bfunctional fitness\b"):
        weighted_reasons.append(("functional_fitness", 8))
    if _contains(lowered, r"\bcrossfit\b"):
        weighted_reasons.append(("crossfit", 8))
    if _contains(lowered, r"\bpilates\b"):
        weighted_reasons.append(("pilates", 8))
    if _contains(lowered, r"\b(?:martial arts?|bjj|jiu[ -]?jitsu|dojo|mma)\b"):
        weighted_reasons.append(("martial_arts", 8))
    if _contains(
        lowered,
        r"\b(?:fitness business|gym business|studio operations|"
        r"member retention|class schedule|no[ -]?show|revenue)\b",
    ):
        weighted_reasons.append(("fitness_operations", 12))
    relevance = min(sum(points for _reason, points in weighted_reasons), 50)
    if relevance < 12:
        return None
    if age <= timedelta(days=1):
        recency = 20
    elif age <= timedelta(days=3):
        recency = 15
    elif age <= timedelta(days=7):
        recency = 10
    elif age <= timedelta(days=14):
        recency = 5
    else:
        recency = 0
    followers = author_metrics["followers_count"]
    listed = author_metrics["listed_count"]
    if followers >= 1000 and listed >= 1:
        author_quality = 15
    elif followers >= 100:
        author_quality = 10
    elif followers >= 10:
        author_quality = 5
    else:
        author_quality = 0
    specificity = min(len(weighted_reasons) * 5, 15)
    reasons = [reason for reason, _points in weighted_reasons]
    if recency >= 10:
        reasons.append("recent")
    if author_quality >= 10:
        reasons.append("credible_author")
    return {
        "score": relevance + recency + author_quality + specificity,
        "created_at": created_at,
        "reason_codes": reasons,
    }


class GrowthDigestService:
    """Build one Rome-day digest using only bounded X read interfaces."""

    def __init__(
        self,
        x_client,
        db,
        *,
        discovery=None,
        account_limit: int = 5,
        post_limit: int = 10,
        reevaluate_limit: int = 5,
        post_query_budget: int = GROWTH_POST_QUERY_BUDGET,
        cooldown_days: int = 30,
        claim_ttl: timedelta = timedelta(minutes=5),
        wait_attempts: int = 200,
    ):
        for name, value, maximum in (
            ("account_limit", account_limit, 5),
            ("post_limit", post_limit, 10),
            ("reevaluate_limit", reevaluate_limit, 5),
            ("post_query_budget", post_query_budget, 2),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be between 1 and {maximum}")
        if cooldown_days != 30:
            raise ValueError("cooldown_days must be exactly 30")
        if (
            type(claim_ttl) is not timedelta
            or claim_ttl <= timedelta(0)
            or claim_ttl > timedelta(minutes=15)
        ):
            raise ValueError("claim_ttl must be positive and at most 15 minutes")
        if type(wait_attempts) is not int or wait_attempts < 0 or wait_attempts > 500:
            raise ValueError("wait_attempts must be between 0 and 500")
        self.x = x_client
        self.db = db
        self.discovery = discovery or GrowthDiscovery(x_client, db)
        self.account_limit = account_limit
        self.post_limit = post_limit
        self.reevaluate_limit = reevaluate_limit
        self.post_query_budget = post_query_budget
        self.cooldown_days = cooldown_days
        self.claim_ttl = claim_ttl
        self.wait_attempts = wait_attempts

    @staticmethod
    def _empty(observed_on: str, outcome: str) -> Dict:
        return {
            "observed_on": observed_on,
            "accounts": [],
            "posts": [],
            "reevaluate": [],
            "outcome": outcome,
        }

    @staticmethod
    def _with_outcome(digest: Dict, outcome: str) -> Dict:
        return {
            "observed_on": digest["observed_on"],
            "accounts": digest["accounts"],
            "posts": digest["posts"],
            "reevaluate": digest["reevaluate"],
            "outcome": outcome,
        }

    def _wait_for_existing(self, observed_on: str) -> Optional[Dict]:
        for _attempt in range(self.wait_attempts):
            persisted = self.db.get_growth_digest(observed_on)
            if persisted is not None:
                return persisted
            time.sleep(0.01)
        return self.db.get_growth_digest(observed_on)

    def _account_rows(self, candidates: object, now: datetime) -> List[Dict]:
        if not isinstance(candidates, (list, tuple)):
            return []
        rows = []
        seen = set()
        for candidate in candidates:
            if type(candidate) is not dict:
                continue
            user_id = candidate.get("user_id")
            username = candidate.get("username")
            profile = candidate.get("profile")
            latest = candidate.get("latest_post")
            reasons = candidate.get("reasons")
            score = candidate.get("score")
            segment = candidate.get("audience_segment")
            activity_at = (
                parse_growth_datetime(latest.get("created_at"))
                if type(latest) is dict
                else None
            )
            if (
                not _canonical_id(user_id)
                or user_id in seen
                or not _username(username)
                or type(profile) is not dict
                or type(latest) is not dict
                or not _canonical_id(latest.get("id"))
                or activity_at is None
                or activity_at > now.astimezone(timezone.utc)
                or now.astimezone(timezone.utc) - activity_at > timedelta(days=30)
                or type(score) is not int
                or not 0 <= score <= 100
                or segment not in {"primary", "amplifier", "end_user"}
                or type(reasons) is not list
                or not reasons
                or len(set(reasons)) != len(reasons)
                or any(reason not in _ACCOUNT_REASON_CODES for reason in reasons)
            ):
                continue
            metrics = {key: profile.get(key) for key in _AUTHOR_METRIC_KEYS}
            if not _closed_metrics(metrics, _AUTHOR_METRIC_KEYS):
                continue
            if self.db.growth_object_in_cooldown("account", user_id, now):
                continue
            seen.add(user_id)
            payload = {
                "user_id": user_id,
                "username": username,
                "public_metrics": metrics,
                "latest_activity_id": latest["id"],
                "latest_activity_at": activity_at.isoformat(),
                "segment": segment,
                "reason_codes": list(reasons),
            }
            rows.append({
                "object_id": user_id,
                "username": username,
                "payload": payload,
                "score": score,
                "reason_codes": list(reasons),
                "cooldown_until": (
                    now.astimezone(timezone.utc) + timedelta(days=self.cooldown_days)
                ).isoformat(),
            })
            if len(rows) >= self.account_limit:
                break
        return rows

    def _post_rows(self, posts: Sequence[Dict], now: datetime) -> List[Dict]:
        best = {}
        for post in posts:
            scored = score_growth_post(post, now)
            if scored is None or self.db.growth_object_in_cooldown(
                "post", post.get("id"), now
            ):
                continue
            prior = best.get(post["id"])
            rank = (scored["score"], scored["created_at"], post["id"])
            if prior is not None and prior[0] >= rank:
                continue
            best[post["id"]] = (rank, post, scored)
        ordered = sorted(
            best.values(),
            key=lambda item: (-item[2]["score"], -item[2]["created_at"].timestamp(), item[1]["id"]),
        )
        rows = []
        for _rank, post, scored in ordered[: self.post_limit]:
            reasons = scored["reason_codes"]
            excerpt = " ".join(post["text"].split())[:280]
            payload = {
                "id": post["id"],
                "author_id": post["author_id"],
                "author_username": post["author_username"],
                "excerpt": excerpt,
                "created_at": scored["created_at"].isoformat(),
                "public_metrics": dict(post["public_metrics"]),
                "reason_codes": list(reasons),
            }
            rows.append({
                "object_id": post["id"],
                "username": post["author_username"],
                "payload": payload,
                "score": scored["score"],
                "reason_codes": list(reasons),
                "cooldown_until": (
                    now.astimezone(timezone.utc) + timedelta(days=self.cooldown_days)
                ).isoformat(),
            })
        return rows

    def _reevaluation_rows(self, candidates: object, now: datetime) -> List[Dict]:
        if not isinstance(candidates, (list, tuple)):
            return []
        rows = []
        for candidate in candidates:
            if type(candidate) is not dict:
                continue
            user_id = candidate.get("user_id")
            username = candidate.get("username")
            reasons = candidate.get("reason_codes")
            metrics = candidate.get("public_metrics")
            if (
                not _canonical_id(user_id)
                or not _username(username)
                or not _canonical_id(candidate.get("latest_activity_id"))
                or parse_growth_datetime(candidate.get("latest_activity_at")) is None
                or candidate.get("segment") not in {
                    "primary", "amplifier", "end_user",
                }
                or type(candidate.get("score")) is not int
                or reasons != ["no_follow_back_after_14_days"]
                or not _closed_metrics(metrics, _AUTHOR_METRIC_KEYS)
                or self.db.growth_object_in_cooldown("reevaluate", user_id, now)
            ):
                continue
            payload = {
                "user_id": user_id,
                "username": username,
                "public_metrics": dict(metrics),
                "latest_activity_id": candidate["latest_activity_id"],
                "latest_activity_at": parse_growth_datetime(
                    candidate["latest_activity_at"]
                ).isoformat(),
                "segment": candidate["segment"],
                "reason_codes": list(reasons),
            }
            rows.append({
                "object_id": user_id,
                "username": username,
                "payload": payload,
                "score": candidate["score"],
                "reason_codes": list(reasons),
                "cooldown_until": (
                    now.astimezone(timezone.utc) + timedelta(days=self.cooldown_days)
                ).isoformat(),
            })
            if len(rows) >= self.reevaluate_limit:
                break
        return rows

    def _fail_claims(self, observed_on: str, tokens: Dict[str, str]) -> None:
        for query_key, token in tokens.items():
            self.db.fail_growth_read_query(observed_on, query_key, token)

    def _read_posts(
        self, observed_on: str, now: datetime
    ) -> Optional[Tuple[List[Dict], Dict[str, str]]]:
        rows = []
        claim_tokens = {}
        for query_key, query in POST_QUERY_PORTFOLIO[: self.post_query_budget]:
            claim, claim_token = self.db.claim_growth_read_query(
                observed_on,
                query_key,
                now,
                now + self.claim_ttl,
                budget=self.post_query_budget,
            )
            if claim != "claimed" or claim_token is None:
                self._fail_claims(observed_on, claim_tokens)
                return None
            claim_tokens[query_key] = claim_token
            try:
                reader = getattr(self.x, "read_relevant_posts", None)
                if callable(reader):
                    result = reader(query, limit=25)
                    page_rows = getattr(result, "posts", None)
                    complete = getattr(result, "complete", None)
                    if complete is not True or not isinstance(page_rows, (list, tuple)):
                        self._fail_claims(observed_on, claim_tokens)
                        return None
                    page_rows = list(page_rows)
                else:
                    page_rows = self.x.search_relevant_posts(query, limit=25)
                    if not isinstance(page_rows, list):
                        self._fail_claims(observed_on, claim_tokens)
                        return None
            except Exception as error:
                logger.warning(
                    "growth_digest_post_read_failed error_type=%s",
                    type(error).__name__,
                )
                self._fail_claims(observed_on, claim_tokens)
                return None
            rows.extend(page_rows)
        return rows, claim_tokens

    def build(self, now: datetime) -> Dict:
        if (
            type(now) is not datetime
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            return self._empty("", "invalid")
        current = now.astimezone(timezone.utc)
        observed_on = current.astimezone(ROME).date().isoformat()
        existing = self.db.get_growth_digest(observed_on)
        if existing is not None:
            return (
                self._with_outcome(existing, "existing")
                if existing
                else self._empty(observed_on, "invalid_persisted")
            )
        lease, builder_token = self.db.claim_growth_digest_build(
            observed_on, current, current + self.claim_ttl
        )
        if lease == "busy":
            persisted = self._wait_for_existing(observed_on)
            if persisted:
                return self._with_outcome(persisted, "existing")
            return self._empty(observed_on, "incomplete")
        if lease != "claimed" or builder_token is None:
            persisted = self.db.get_growth_digest(observed_on)
            if persisted:
                return self._with_outcome(persisted, "existing")
            return self._empty(observed_on, "incomplete")
        try:
            candidates = self.discovery.run(current)
        except Exception as error:
            logger.warning(
                "growth_digest_accounts_read_failed error_type=%s",
                type(error).__name__,
            )
            self.db.fail_growth_read_query(
                observed_on, "__digest_build__", builder_token
            )
            return self._empty(observed_on, "incomplete")
        post_read = self._read_posts(observed_on, current)
        if post_read is None:
            self.db.fail_growth_read_query(
                observed_on, "__digest_build__", builder_token
            )
            return self._empty(observed_on, "incomplete")
        post_candidates, query_claim_tokens = post_read
        account_rows = self._account_rows(candidates, current)
        post_rows = self._post_rows(post_candidates, current)
        reevaluate_rows = self._reevaluation_rows(
            self.db.get_growth_reevaluation_candidates(
                current, limit=self.reevaluate_limit
            ),
            current,
        )
        try:
            persisted, outcome = self.db.persist_growth_digest_atomic(
                observed_on=observed_on,
                account_rows=account_rows,
                post_rows=post_rows,
                reevaluate_rows=reevaluate_rows,
                completed_at=current.isoformat(),
                builder_token=builder_token,
                query_claim_tokens=query_claim_tokens,
            )
        except Exception as error:
            logger.warning(
                "growth_digest_persist_failed error_type=%s",
                type(error).__name__,
            )
            persisted = self.db.get_growth_digest(observed_on)
            if persisted:
                return self._with_outcome(persisted, "existing")
            self._fail_claims(observed_on, query_claim_tokens)
            self.db.fail_growth_read_query(
                observed_on, "__digest_build__", builder_token
            )
            return self._empty(observed_on, "incomplete")
        if not persisted:
            self._fail_claims(observed_on, query_claim_tokens)
            self.db.fail_growth_read_query(
                observed_on, "__digest_build__", builder_token
            )
            return self._empty(
                observed_on,
                "invalid_persisted" if outcome == "invalid_existing" else "incomplete",
            )
        return self._with_outcome(persisted, outcome)
