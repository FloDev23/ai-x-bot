"""Manual operator authority boundary for exact Telegram copy."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re


class ManualPostService:
    """Persist operator copy directly into the approved editorial reserve."""

    def __init__(self, db, *, now_fn=None):
        self.db = db
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def create_approved_from_telegram(
        self,
        *,
        text: str,
        category: str,
        source_ids: list[int],
        media_id: int | None,
        state_key: str,
        expected_state_value: str,
        session_token: str,
        operator: str,
    ) -> tuple[dict | None, str]:
        try:
            state = json.loads(expected_state_value)
        except (TypeError, ValueError, RecursionError):
            return None, "rejected"
        if (
            type(state) is not dict
            or state.get("token") != session_token
            or type(session_token) is not str
            or re.fullmatch(r"[A-Za-z0-9_-]{16,64}", session_token) is None
        ):
            return None, "rejected"
        current = self.now_fn()
        if (
            type(current) is not datetime
            or current.tzinfo is None
            or current.utcoffset() is None
        ):
            return None, "rejected"
        current = current.astimezone(timezone.utc)
        microsecond = int.from_bytes(
            hashlib.sha256(session_token.encode("ascii")).digest()[:4],
            "big",
        ) % 1_000_000
        intended_slot = current.replace(microsecond=microsecond).isoformat()
        return self.db.create_manual_approved_draft_consuming_state_atomic(
            text=text,
            category=category,
            source_ids=source_ids,
            media_id=media_id,
            intended_slot=intended_slot,
            state_key=state_key,
            expected_state_value=expected_state_value,
            session_token=session_token,
            operator=operator,
            now=current,
        )
