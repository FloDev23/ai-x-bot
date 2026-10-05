"""Draft manual replies to the day's founder conversations, with Italian text.

The bot never replies on X: the operator reads the Italian translation, copies
the English reply from Telegram and posts it by hand.
"""

import logging
import re
from datetime import datetime
from typing import Dict, List, Optional


logger = logging.getLogger(__name__)

REPLY_DAILY_LIMIT = 5
REPLY_MAX_LENGTH = 240
REPLY_MAX_GENERATIONS = 3
# Founder conversations first: they invite answers. Suggested accounts' latest
# posts fill the remaining slots.
_REPLY_SOURCES = ("founder_conversation", "suggested_account")
# Em dashes are the quickest tell of machine-written replies.
_FORBIDDEN_IN_REPLY = re.compile(
    r"https?://|www\.|(?<!\w)[#@]\w|flexdropin|[—–]", re.IGNORECASE,
)


def validate_reply_draft(draft: object) -> Optional[Dict[str, str]]:
    """Return the three cleaned texts, or None when any rule is broken."""
    if not isinstance(draft, dict):
        return None
    texts = {}
    for key, limit in (
        ("post_it", 1500), ("reply_en", REPLY_MAX_LENGTH), ("reply_it", 400),
    ):
        value = draft.get(key)
        if type(value) is not str:
            return None
        value = " ".join(value.split()).strip("\"' ")
        if not value or len(value) > limit:
            return None
        texts[key] = value
    if len(texts["reply_en"]) < 15 or _FORBIDDEN_IN_REPLY.search(texts["reply_en"]):
        return None
    return texts


class GrowthReplyService:
    """Generate and store at most a few reply drafts per Rome day."""

    def __init__(self, generator, db, *, daily_limit: int = REPLY_DAILY_LIMIT):
        self.generator = generator
        self.db = db
        self.daily_limit = daily_limit

    def _draft(self, post_text: str) -> Optional[Dict[str, str]]:
        try:
            return validate_reply_draft(self.generator.draft_founder_reply(post_text))
        except Exception as error:
            logger.warning(
                "growth_reply_generation_failed error_type=%s",
                type(error).__name__,
            )
            return None

    def build(self, observed_on: str, now: datetime) -> List[Dict]:
        """Fill today's drafts up to the daily limit; reruns are idempotent."""
        digest = self.db.get_growth_digest(observed_on)
        posts = digest.get("posts", []) if isinstance(digest, dict) else []
        existing = self.db.list_growth_reply_drafts(observed_on)
        drafted_ids = {row["suggestion_id"] for row in existing}
        ranked = [
            post
            for source in _REPLY_SOURCES
            for post in posts
            if post.get("reason_codes", [None])[0] == source
        ]
        missing = self.daily_limit - len(existing)
        for post in ranked:
            if missing <= 0:
                break
            if post["id"] in drafted_ids:
                continue
            excerpt = post["payload"]["excerpt"]
            text = self._draft(excerpt)
            if text is None:
                continue
            if self.db.insert_growth_reply_draft({
                "suggestion_id": post["id"],
                "observed_on": observed_on,
                "tweet_id": post["object_id"],
                "author_username": post["username"],
                "post_excerpt": excerpt,
                **text,
            }, now):
                missing -= 1
        return self.db.list_growth_reply_drafts(observed_on)

    def regenerate(self, draft_id: int, revision: int, now: datetime):
        draft = self.db.get_growth_reply_draft(draft_id)
        if (
            draft is None
            or draft["revision"] != revision
            or draft["status"] != "ready"
            or draft["generation_count"] >= REPLY_MAX_GENERATIONS
        ):
            return draft, "rejected"
        text = self._draft(draft["post_excerpt"])
        if text is None:
            return draft, "failed"
        outcome = self.db.replace_growth_reply_text(
            draft_id, revision, text, now, max_generations=REPLY_MAX_GENERATIONS,
        )
        return self.db.get_growth_reply_draft(draft_id), outcome
