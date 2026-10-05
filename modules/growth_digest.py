"""Daily persisted read-only X growth suggestions for manual operator action."""

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from modules.growth_candidate_schema import (
    has_noise_signals,
    parse_growth_datetime,
)
from modules.growth_discovery import GrowthDiscovery


logger = logging.getLogger(__name__)

ROME = ZoneInfo("Europe/Rome")
GROWTH_POST_QUERY_BUDGET = 1
POST_QUERY_PORTFOLIO: Tuple[Tuple[str, str], ...] = (
    (
        "founder_conversation",
        '("build in public" OR #buildinpublic OR "indie hacker" OR '
        '"solo founder" OR "Product Hunt" OR "first customers" OR MRR OR '
        '"feedback on my") '
        'lang:en -is:retweet -is:reply',
    ),
)
LIKE_SOURCE_QUOTAS: Tuple[Tuple[str, int], ...] = (
    ("followed_account", 4),
    ("suggested_account", 3),
    ("founder_conversation", 3),
)
LIKE_FILL_ORDER: Tuple[str, ...] = (
    "followed_account", "founder_conversation", "suggested_account",
)
FOLLOWED_ACCOUNT_MAX_AGE = timedelta(hours=72)
SUGGESTED_ACCOUNT_MAX_AGE = timedelta(days=7)
LIKE_AUTHOR_COOLDOWN = timedelta(days=3)
_ACCOUNT_REASON_CODES = frozenset({
    "founder_bio", "multiple_startup_topics", "one_startup_topic",
    "active_within_7_days", "active_within_30_days", "english_market",
    "follow_back_range", "fitness_affinity",
})
_POST_METRIC_KEYS = frozenset({
    "like_count", "retweet_count", "reply_count", "quote_count",
    "impression_count",
})
_AUTHOR_METRIC_KEYS = frozenset({
    "followers_count", "following_count", "tweet_count", "listed_count",
})
# Weighted signals that a post is a founder conversation worth joining.
_POST_SIGNALS: Tuple[Tuple[str, int, str], ...] = (
    ("build_in_public", 15, r"build(?:ing)? in public|#buildinpublic|indie ?hack"),
    ("launch", 12, r"\blaunch(?:ed|ing)?\b|product hunt|\bshipped\b"),
    (
        "traction",
        12,
        r"\bmrr\b|\barr\b|\brevenue\b|paying customers?|"
        r"first (?:\d+ )?(?:customers?|users?)|\bchurn\b",
    ),
    (
        "feedback_request",
        10,
        r"\bfeedback\b|\broast\b|would you use|what do you think",
    ),
    (
        "fitness_tech",
        10,
        r"\b(?:fitness|gym|workout|wellness)\b.*\b(?:app|startup|saas|booking)\b|"
        r"\b(?:app|startup|saas|booking)\b.*\b(?:fitness|gym|workout|wellness)\b",
    ),
    (
        "founder_struggle",
        8,
        r"\bdistribution\b|marketing is hard|no users|\bburnout\b|"
        r"\bpivot(?:ed|ing)?\b",
    ),
    ("question", 6, r"\?"),
)


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


def _valid_post(post: Dict) -> bool:
    text = post.get("text")
    return (
        _canonical_id(post.get("id"))
        and _canonical_id(post.get("author_id"))
        and _username(post.get("author_username"))
        and type(text) is str
        and bool(text.strip())
        and len(text) <= 1000
        and post.get("lang") == "en"
        and _closed_metrics(post.get("public_metrics"), _POST_METRIC_KEYS)
        and not has_noise_signals(text)
    )


def score_growth_post(post: Dict, now: datetime) -> Optional[Dict]:
    """Score one searched post as a founder conversation worth joining."""
    if type(post) is not dict or type(now) is not datetime:
        return None
    created_at = parse_growth_datetime(post.get("created_at"))
    author_metrics = post.get("author_public_metrics")
    if (
        not _valid_post(post)
        or created_at is None
        or not _closed_metrics(author_metrics, _AUTHOR_METRIC_KEYS)
    ):
        return None
    age = now.astimezone(timezone.utc) - created_at
    if age < timedelta(0) or age > timedelta(days=30):
        return None
    lowered = post["text"].lower()
    weighted_reasons = [
        (reason, points)
        for reason, points, pattern in _POST_SIGNALS
        if re.search(pattern, lowered) is not None
    ]
    relevance = min(sum(points for _reason, points in weighted_reasons), 55)
    if relevance < 10:
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
    # A reply under a bigger author reaches more people.
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
    specificity = min(len(weighted_reasons) * 4, 15)
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


def score_recent_post(
    post: Dict,
    now: datetime,
    *,
    source: str,
    max_age: timedelta,
) -> Optional[Dict]:
    """Score a recent post by a known peer: any topic is worth a like."""
    if (
        type(post) is not dict
        or type(now) is not datetime
        or source not in {"followed_account", "suggested_account"}
    ):
        return None
    created_at = parse_growth_datetime(post.get("created_at"))
    if not _valid_post(post) or created_at is None:
        return None
    age = now.astimezone(timezone.utc) - created_at
    if age < timedelta(0) or age > max_age:
        return None
    recent = age <= timedelta(days=1)
    return {
        "score": 80 if recent else 65,
        "created_at": created_at,
        "reason_codes": [source, "recent"] if recent else [source],
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
        unfollow_review_days: int = 30,
        claim_ttl: timedelta = timedelta(minutes=5),
        wait_attempts: int = 200,
    ):
        for name, value, maximum in (
            ("account_limit", account_limit, 5),
            ("post_limit", post_limit, 10),
            ("reevaluate_limit", reevaluate_limit, 5),
            ("post_query_budget", post_query_budget, 1),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be between 1 and {maximum}")
        if cooldown_days != 30:
            raise ValueError("cooldown_days must be exactly 30")
        if type(unfollow_review_days) is not int or unfollow_review_days < 30:
            raise ValueError("unfollow_review_days must be at least 30")
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
        self.unfollow_review_days = unfollow_review_days
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

    def _account_rows(
        self, candidates: object, now: datetime, followed_ids: frozenset
    ) -> List[Dict]:
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
                or segment != "peer"
                or user_id in followed_ids
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

    def _like_rows(
        self,
        scored_posts: Sequence[Tuple[str, Dict, Dict]],
        now: datetime,
    ) -> List[Dict]:
        current = now.astimezone(timezone.utc)
        recent_authors = self.db.get_recent_growth_post_authors(
            current - LIKE_AUTHOR_COOLDOWN
        )
        buckets: Dict[str, List[Tuple[Dict, Dict]]] = {
            source: [] for source, _quota in LIKE_SOURCE_QUOTAS
        }
        seen_posts = set()
        for source, post, scored in scored_posts:
            if (
                source not in buckets
                or post["id"] in seen_posts
                or post["author_id"] in recent_authors
                or self.db.growth_object_in_cooldown("post", post["id"], current)
            ):
                continue
            seen_posts.add(post["id"])
            buckets[source].append((post, scored))
        for items in buckets.values():
            items.sort(key=lambda item: (
                -item[1]["score"],
                -item[1]["created_at"].timestamp(),
                item[0]["id"],
            ))
        selected: List[Tuple[Dict, Dict]] = []
        authors = set()

        def take(source: str, count: int) -> None:
            taken = 0
            while buckets[source] and taken < count and len(selected) < self.post_limit:
                post, scored = buckets[source].pop(0)
                if post["author_id"] in authors:
                    continue
                authors.add(post["author_id"])
                selected.append((post, scored))
                taken += 1

        for source, quota in LIKE_SOURCE_QUOTAS:
            take(source, quota)
        for source in LIKE_FILL_ORDER:
            take(source, self.post_limit)
        rows = []
        for post, scored in selected:
            reasons = list(scored["reason_codes"])[:10]
            payload = {
                "id": post["id"],
                "author_id": post["author_id"],
                "author_username": post["author_username"],
                "excerpt": " ".join(post["text"].split())[:280],
                "created_at": scored["created_at"].isoformat(),
                "public_metrics": dict(post["public_metrics"]),
                "reason_codes": reasons,
            }
            rows.append({
                "object_id": post["id"],
                "username": post["author_username"],
                "payload": payload,
                "score": scored["score"],
                "reason_codes": reasons,
                "cooldown_until": (
                    current + timedelta(days=self.cooldown_days)
                ).isoformat(),
            })
        return rows

    def _scored_like_candidates(
        self,
        posts: Sequence[Dict],
        candidates: object,
        suggested_ids: set,
        followed_peer_ids: frozenset,
        now: datetime,
    ) -> List[Tuple[str, Dict, Dict]]:
        scored_posts = []
        for post in posts:
            if type(post) is not dict:
                continue
            if post.get("source_kind") == "following":
                # The following list still holds off-topic accounts from the
                # gym era: only peers' posts are worth a like.
                if post.get("author_id") not in followed_peer_ids:
                    continue
                scored = score_recent_post(
                    post, now,
                    source="followed_account", max_age=FOLLOWED_ACCOUNT_MAX_AGE,
                )
                if scored is not None:
                    scored_posts.append(("followed_account", post, scored))
                continue
            scored = score_growth_post(post, now)
            if scored is not None:
                scored_posts.append(("founder_conversation", post, {
                    **scored,
                    "reason_codes": ["founder_conversation", *scored["reason_codes"]],
                }))
        for candidate in candidates if isinstance(candidates, (list, tuple)) else []:
            if type(candidate) is not dict or candidate.get("user_id") not in suggested_ids:
                continue
            latest = candidate.get("latest_post")
            if type(latest) is not dict:
                continue
            post = {
                "id": latest.get("id"),
                "text": latest.get("text"),
                "author_id": candidate.get("user_id"),
                "author_username": candidate.get("username"),
                "created_at": latest.get("created_at"),
                "lang": latest.get("lang"),
                "public_metrics": latest.get("public_metrics"),
            }
            scored = score_recent_post(
                post, now,
                source="suggested_account", max_age=SUGGESTED_ACCOUNT_MAX_AGE,
            )
            if scored is not None:
                scored_posts.append(("suggested_account", post, scored))
        return scored_posts

    def _unfollow_rows(self, now: datetime) -> List[Dict]:
        current = now.astimezone(timezone.utc)
        rows = []
        for candidate in self.db.get_unfollow_proposals(
            current,
            limit=self.reevaluate_limit,
            review_days=self.unfollow_review_days,
        ):
            if type(candidate) is not dict:
                continue
            user_id = candidate.get("user_id")
            username = candidate.get("username")
            metrics = candidate.get("public_metrics")
            followed_since = parse_growth_datetime(candidate.get("followed_since"))
            days = candidate.get("days_following")
            if (
                not _canonical_id(user_id)
                or not _username(username)
                or not _closed_metrics(metrics, _AUTHOR_METRIC_KEYS)
                or followed_since is None
                or self.db.growth_object_in_cooldown("reevaluate", user_id, current)
            ):
                continue
            reasons = ["no_follow_back_after_30_days"]
            rows.append({
                "object_id": user_id,
                "username": username,
                "payload": {
                    "user_id": user_id,
                    "username": username,
                    "public_metrics": dict(metrics),
                    "followed_since": followed_since.isoformat(),
                    "reason_codes": list(reasons),
                },
                "score": min(days, 100) if type(days) is int and days >= 0 else 0,
                "reason_codes": list(reasons),
                "cooldown_until": (
                    current + timedelta(days=self.cooldown_days)
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
        following_reader = getattr(self.x, "read_following_timeline", None)
        read_budget = self.post_query_budget + int(callable(following_reader))
        for query_key, query in POST_QUERY_PORTFOLIO[: self.post_query_budget]:
            claim, claim_token = self.db.claim_growth_read_query(
                observed_on,
                query_key,
                now,
                now + self.claim_ttl,
                budget=read_budget,
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
        if callable(following_reader):
            query_key = "following_timeline"
            claim, claim_token = self.db.claim_growth_read_query(
                observed_on,
                query_key,
                now,
                now + self.claim_ttl,
                budget=read_budget,
            )
            if claim != "claimed" or claim_token is None:
                self._fail_claims(observed_on, claim_tokens)
                return None
            claim_tokens[query_key] = claim_token
            try:
                result = following_reader(limit=25)
                page_rows = getattr(result, "posts", None)
                complete = getattr(result, "complete", None)
                if complete is not True or not isinstance(page_rows, (list, tuple)):
                    logger.warning("growth_digest_following_read_incomplete")
                    page_rows = ()
            except Exception as error:
                logger.warning(
                    "growth_digest_following_read_failed error_type=%s",
                    type(error).__name__,
                )
                page_rows = ()
            rows.extend({**post, "source_kind": "following"} for post in page_rows)
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
        following = self.db.get_following_state()
        followed_ids = frozenset(following)
        followed_peer_ids = frozenset(
            user_id for user_id, state in following.items() if state["peer"]
        )
        account_rows = self._account_rows(candidates, current, followed_ids)
        post_rows = self._like_rows(
            self._scored_like_candidates(
                post_candidates,
                candidates,
                {row["object_id"] for row in account_rows},
                followed_peer_ids,
                current,
            ),
            current,
        )
        reevaluate_rows = self._unfollow_rows(current)
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
