"""Deferred media matching for an already-created editorial concept."""
from typing import Dict, Optional

from config import MEDIA_MATCH_REASON_MAX_CHARS, MEDIA_MATCH_THRESHOLD


class MediaMatcher:
    def __init__(self, db, generator, threshold: int = MEDIA_MATCH_THRESHOLD):
        self.db = db
        self.generator = generator
        self.threshold = threshold

    def attach_best(self, draft_id: int) -> Optional[Dict]:
        draft = self.db.get_post_draft(draft_id)
        if not draft or draft.get("media_id") is not None:
            return None
        candidates = self.db.get_available_media(limit=15)
        if not candidates:
            return None
        choice = self.generator.select_best_media(
            draft["category"], draft["text"], candidates,
        )
        if not isinstance(choice, dict):
            return None
        media_id = choice.get("media_id")
        relevance = choice.get("relevance", 0)
        reason = choice.get("reason")
        if (
            type(media_id) is not int
            or media_id <= 0
            or type(relevance) is not int
            or not 0 <= relevance <= 100
            or relevance < self.threshold
            or type(reason) is not str
            or not reason.strip()
            or len(reason.strip()) > MEDIA_MATCH_REASON_MAX_CHARS
        ):
            return None
        if media_id not in {candidate["id"] for candidate in candidates}:
            return None

        attach = getattr(self.db, "attach_media_to_draft", None)
        reserved = (
            attach(media_id, draft_id)
            if attach
            else self.db.reserve_media(media_id, draft_id)
        )
        if not reserved:
            return None
        return self.db.get_media_by_id(media_id)
