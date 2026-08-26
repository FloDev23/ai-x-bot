"""Verified, path-free Telegram rendering for one media-library item."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from modules.media_processor import media_content_matches
from modules.media_store import open_verified_media


_MEDIA_PAIRS = {
    ("image", "image/jpeg"): "photo",
    ("image", "image/png"): "photo",
    ("image", "image/webp"): "photo",
    ("video", "video/mp4"): "video",
    ("video", "video/quicktime"): "video",
    ("video", "video/x-m4v"): "video",
    ("document", "application/pdf"): "document",
}


class MediaBrowser:
    """A persisted media selector whose Telegram boundary only receives FDs."""

    view_kind = "media_browser"

    def __init__(self, database, telegram_api):
        self.db = database
        self.telegram_api = telegram_api

    @staticmethod
    def _safe_text(value: Any, limit: int = 320) -> str:
        if not isinstance(value, str):
            return "n/d"
        # Locators should never be reflected even when an operator supplied one
        # as a description by mistake.
        text = " ".join(value.replace("\\", "/").split())
        if text.startswith("/") or "/Users/" in text or "/home/" in text:
            return "n/d"
        return text[:limit] or "n/d"

    @staticmethod
    def _media_kind(record: Dict) -> Optional[str]:
        return _MEDIA_PAIRS.get((record.get("media_type"), record.get("mime_type")))

    def _markup(self, token: str, record: Dict) -> Dict:
        media_id = record["id"]
        revision = record["revision"]
        rows = [
                [
                    {"text": "Precedente", "callback_data": f"mb:p:{token}"},
                    {"text": "Successivo", "callback_data": f"mb:n:{token}"},
                ],
                [{"text": "Usa questo", "callback_data": f"mb:u:{token}:{media_id}:{revision}"}],
                [
                    {"text": "Nessun media", "callback_data": f"mb:z:{token}"},
                    {"text": "Gestisci media", "callback_data": f"mb:g:{token}"},
                ],
                [{"text": "Annulla", "callback_data": f"mb:c:{token}"}],
        ]
        if record.get("lifecycle_state") == "available":
            rows.insert(3, [{"text": "Archivia", "callback_data": f"mb:a:{token}:{media_id}:{revision}"}])
        elif record.get("lifecycle_state") == "archived":
            rows.insert(3, [{"text": "Ripristina", "callback_data": f"mb:r:{token}:{media_id}:{revision}"}])
        if (
            record.get("used") == 0 and record.get("reserved_by_draft_id") is None
            and record.get("lifecycle_state") in {"available", "archived"}
        ):
            rows.insert(4, [{"text": "Elimina definitivamente", "callback_data": f"mb:d:{token}:{media_id}:{revision}"}])
        return {"inline_keyboard": rows}

    def select(self, *, media_id: int, expected_revision: int) -> Optional[Dict]:
        if (
            type(media_id) is not int or media_id <= 0
            or type(expected_revision) is not int or expected_revision < 0
        ):
            return None
        record = self.db.get_media_by_id(media_id)
        if (
            not isinstance(record, dict)
            or record.get("revision") != expected_revision
            or record.get("lifecycle_state") != "available"
            or record.get("file_deleted") != 0
            or record.get("reserved_by_draft_id") is not None
            or record.get("used") != 0
            or self._media_kind(record) is None
        ):
            return None
        return record

    def _send_record(self, chat_id: str, token: str, record: Dict) -> Optional[int]:
        media_kind = self._media_kind(record)
        if media_kind is None:
            return None
        caption = "\n".join((
            f"Media #{record['id']}",
            f"Tipo: {media_kind}",
            f"Data: {self._safe_text(record.get('uploaded_at'), 48)}",
            f"Descrizione: {self._safe_text(record.get('ai_description') or record.get('user_context'))}",
        ))
        try:
            with open_verified_media(record) as stream:
                if media_kind != "document" and not media_content_matches(stream, record["mime_type"]):
                    return None
                # Re-read the row while the trusted root lease remains held.
                # A changed lifecycle/revision fails closed before Telegram sees it.
                current = self.select(
                    media_id=record["id"], expected_revision=record["revision"],
                )
                if current is None or current.get("file_sha256") != record.get("file_sha256"):
                    return None
                result = self.telegram_api.send_media(
                    chat_id, stream, media_kind, caption=caption,
                    reply_markup=self._markup(token, record),
                )
        except Exception:
            return None
        if not isinstance(result, dict) or type(result.get("message_id")) is not int:
            return None
        return result["message_id"]

    def show(self, *, chat_id: str, media_id: int | None, context: str) -> str:
        if not isinstance(chat_id, str) or not chat_id or not isinstance(context, str):
            raise ValueError("invalid_media_browser_request")
        rows = self.db.get_available_media(limit=300)
        target_ids = [row["id"] for row in rows if type(row.get("id")) is int]
        if media_id is not None and type(media_id) is int and media_id in target_ids:
            target_ids.remove(media_id)
            target_ids.insert(0, media_id)
        token = self.db.create_telegram_view(
            chat_id, self.view_kind,
            {"target_ids": target_ids, "direction": "current", "filters": {"context": context}, "last_message_id": None},
        )
        self.render(token=token, chat_id=chat_id)
        return token

    def render(self, *, token: str, chat_id: str, direction: str = "current") -> bool:
        view = self.db.get_telegram_view(token, chat_id, self.view_kind)
        if view is None:
            return False
        ids = list(view["state"]["target_ids"])
        if direction == "next" and len(ids) > 1:
            ids = ids[1:] + ids[:1]
        elif direction == "previous" and len(ids) > 1:
            ids = ids[-1:] + ids[:-1]
        candidate = self.db.get_media_by_id(ids[0]) if ids else None
        record = (
            self.select(media_id=ids[0], expected_revision=candidate["revision"])
            if isinstance(candidate, dict) and type(candidate.get("revision")) is int
            else None
        )
        if record is None:
            try:
                self.telegram_api.send_message(chat_id, "Nessun media disponibile.")
            except Exception:
                pass
            return False
        message_id = self._send_record(chat_id, token, record)
        if message_id is None:
            try:
                self.telegram_api.send_message(chat_id, "Media non disponibile.")
            except Exception:
                pass
            return False
        state = dict(view["state"])
        previous_message_id = state.get("last_message_id")
        state.update(target_ids=ids, direction=direction, last_message_id=message_id)
        if not self.db.update_telegram_view(
            token, chat_id, self.view_kind, view["revision"], state,
        ):
            return False
        if previous_message_id is not None:
            try:
                self.telegram_api.delete_message(chat_id, previous_message_id)
            except Exception:
                pass
        return True
