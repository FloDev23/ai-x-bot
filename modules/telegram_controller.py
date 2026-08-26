"""Authorized, idempotent Telegram control and workflow boundary."""

import hashlib
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

from config import (
    APPROVED_QUEUE_TARGET,
    AUDIENCE_TIMEZONE,
    DRAFT_GENERATION_DAILY_CAP,
    DRY_RUN,
    NEWS_TRUSTED_DOMAINS,
    PENDING_REVIEW_LIMIT,
    POSTS_PER_DAY,
)
from modules.media_store import open_verified_media
from modules.telegram_media_browser import MediaBrowser
from modules.telegram_post_browser import PostBrowser
from modules.content_planner import PORTFOLIO, SOURCE_TYPES as MANUAL_SOURCE_TYPES
from modules.source_validation import is_complete_verified_news, is_safe_https_url
from modules.telegram_api import (
    TELEGRAM_CAPTION_MAX_CHARS,
    TELEGRAM_CALLBACK_DATA_MAX_BYTES,
    TELEGRAM_MESSAGE_MAX_CHARS,
    TELEGRAM_POLL_TIMEOUT,
    TelegramApiError,
    sanitize_error,
    telegram_media_metadata,
)


_TRANSPORT_BACKOFF_SECONDS = (1, 2, 4, 8, 30)
_EMPTY_POLL_DELAY_SECONDS = 0.1
_SQLITE_INTEGER_MAX = (1 << 63) - 1
_SUPPORTED_SUBTYPES = ("message", "callback_query")
_SESSION_VERSION = 1
_SESSION_TTL = timedelta(minutes=30)
_SESSION_KINDS = {
    "source_intake": {"text", "classification", "news_url", "news_date", "news_source"},
    "draft_edit": {"text"},
    "draft_postpone": {"slot"},
    "manual_post": {
        "text", "category", "sources", "media",
        "source_child_text", "source_child_classification",
        "source_child_news_url", "source_child_news_date",
        "source_child_news_source",
    },
}
_SOURCE_TYPES = {
    "founder_note": "Founder note",
    "product_fact": "Product fact",
    "evergreen_idea": "Evergreen idea",
    "verified_news": "Verified news",
}
_SOURCE_TRUST_LABELS = {"verified": "Verificata"}
_MANUAL_CATEGORY_LABELS = {
    "gym_strategy": "Strategia palestra",
    "fitness_business_insight": "Business fitness",
    "shareable_fitness": "Fitness condivisibile",
    "product_proof": "Prodotto",
    "founder_journey": "Percorso founder",
}
_GROWTH_REASONS = {
    "not_relevant": "Non pertinente",
    "low_quality": "Qualità bassa",
    "already_known": "Già noto",
}
_SAFE_USERNAME = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_SAFE_CALLBACK_ID = re.compile(r"^[^\x00-\x1f\x7f]{1,4096}$")
_PREVIEW_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_PREVIEW_VIDEO_MIME_TYPES = frozenset({
    "video/mp4", "video/quicktime", "video/x-m4v",
})


class TelegramController:
    """Claim Telegram updates before dispatching them to business handlers."""

    def __init__(
        self,
        telegram_api,
        db,
        notifier,
        authorized_chat_id: str,
        dispatcher: Optional[Callable[[Dict[str, Any]], Any]] = None,
        poll_timeout: int = TELEGRAM_POLL_TIMEOUT,
        *,
        draft_pipeline=None,
        media_processor=None,
        media_matcher=None,
        analytics=None,
        scheduler_status=None,
        queue_service=None,
        dry_run: Optional[bool] = None,
        now_fn=None,
        news_trusted_domains=None,
    ):
        self.telegram_api = telegram_api
        self.db = db
        self.notifier = notifier
        self.authorized_chat_id = str(authorized_chat_id)
        self.dispatcher = dispatcher
        self.poll_timeout = int(poll_timeout)
        self.draft_pipeline = draft_pipeline
        self.media_processor = media_processor
        self.media_matcher = media_matcher
        self.analytics = analytics
        self.scheduler_status = scheduler_status
        self.queue_service = queue_service
        self.dry_run = DRY_RUN if dry_run is None else bool(dry_run)
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        domains = NEWS_TRUSTED_DOMAINS if news_trusted_domains is None else news_trusted_domains
        self.news_trusted_domains = {
            domain.strip().lower().rstrip(".")
            for domain in domains
            if isinstance(domain, str) and domain.strip()
        }
        self.command_handlers = {
            "/status": self._status,
            "/posts": self._posts,
            "/growth": self._growth,
            "/stats": self._stats,
            "/ideas": self._ideas,
            "/newpost": self._newpost,
            "/media": self._media,
            "/pause": self._pause,
            "/resume": self._resume,
            "/errors": self._errors,
            "/help": self._help,
        }
        self.media_browser = MediaBrowser(db, telegram_api)
        self.post_browser = PostBrowser()

    @staticmethod
    def _supported_subtype(update: Dict[str, Any]):
        present = [name for name in _SUPPORTED_SUBTYPES if name in update]
        if len(present) != 1:
            return None, None
        subtype = present[0]
        payload = update[subtype]
        if not isinstance(payload, dict):
            return None, None
        return subtype, payload

    @staticmethod
    def _chat_id(subtype: str, payload: Dict[str, Any]):
        if subtype == "message":
            chat = payload.get("chat")
            if isinstance(chat, dict):
                return chat.get("id")
        elif subtype == "callback_query":
            callback_message = payload.get("message")
            if isinstance(callback_message, dict):
                chat = callback_message.get("chat")
                if isinstance(chat, dict):
                    return chat.get("id")
        return None

    @staticmethod
    def _valid_update_id(value: Any) -> bool:
        return type(value) is int and 0 <= value <= _SQLITE_INTEGER_MAX

    @staticmethod
    def _stopped(stop_event) -> bool:
        return stop_event is not None and stop_event.is_set()

    def _dispatch(self, update: Dict[str, Any]):
        if self.dispatcher is not None:
            return self.dispatcher(update)
        return self._dispatch_workflow(update)

    @staticmethod
    def _clean_text(value: Any, limit: int) -> str:
        if not isinstance(value, str):
            return ""
        return " ".join(value.split())[:limit]

    @staticmethod
    def _positive_id(value: Any) -> Optional[int]:
        if not isinstance(value, str) or not value.isascii() or not value.isdigit():
            return None
        try:
            parsed = int(value)
        except (ValueError, OverflowError):
            return None
        if not 0 < parsed <= _SQLITE_INTEGER_MAX:
            return None
        return parsed

    @staticmethod
    def _nonnegative_id(value: Any) -> Optional[int]:
        if not isinstance(value, str) or not value.isascii() or not value.isdigit():
            return None
        try:
            parsed = int(value)
        except (ValueError, OverflowError):
            return None
        return parsed if 0 <= parsed <= _SQLITE_INTEGER_MAX else None

    def _now(self) -> datetime:
        value = self.now_fn()
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime):
            raise ValueError("invalid controller clock")
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _send(self, chat_id: str, text: Any, *, reply_markup=None):
        """Send plain text so user data cannot become HTML/Markdown markup."""
        rendered = str(text).strip() or "Nessun dato disponibile."
        if len(rendered) > TELEGRAM_MESSAGE_MAX_CHARS:
            rendered = rendered[: TELEGRAM_MESSAGE_MAX_CHARS - 1] + "…"
        return self.telegram_api.send_message(
            str(chat_id),
            rendered,
            parse_mode=None,
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )

    @staticmethod
    def _callback_markup(rows):
        return {"inline_keyboard": rows}

    @staticmethod
    def _fit_labeled_lines(fields, budget: int) -> str:
        """Fit metadata into ``budget`` while retaining every field label."""
        values = [str(value) for _label, value in fields]

        def render():
            return "\n".join(
                label + value for (label, _original), value in zip(fields, values)
            )

        rendered = render()
        while len(rendered) > budget:
            candidates = [
                index for index, value in enumerate(values) if len(value) > 1
            ]
            if not candidates:
                return rendered[:max(0, budget)]
            index = max(candidates, key=lambda candidate: len(values[candidate]))
            excess = len(rendered) - budget
            target = max(1, len(values[index]) - excess)
            values[index] = (
                "…"
                if target == 1
                else values[index][:target - 1] + "…"
            )
            rendered = render()
        return rendered

    @staticmethod
    def _callback_button(text: str, data: str) -> Dict[str, str]:
        try:
            size = len(data.encode("utf-8"))
        except UnicodeError:
            raise ValueError("invalid callback data") from None
        if not 1 <= size <= TELEGRAM_CALLBACK_DATA_MAX_BYTES:
            raise ValueError("invalid callback data")
        return {"text": text, "callback_data": data}

    def _dispatch_workflow(self, update: Dict[str, Any]):
        if "message" in update:
            message = update["message"]
            chat_id = str(message["chat"]["id"])
            if any(name in message for name in ("photo", "video", "document")):
                return self._ingest_media(chat_id, message)
            text = message.get("text")
            if not isinstance(text, str) or not text.strip():
                self._send(chat_id, "Messaggio non supportato.")
                return "unsupported_message"
            if len(text) > TELEGRAM_MESSAGE_MAX_CHARS:
                self._send(chat_id, "Messaggio troppo lungo.")
                return "invalid_message"
            stripped = text.strip()
            if stripped.startswith("/"):
                command = stripped.split(None, 1)[0].split("@", 1)[0].lower()
                handler = self.command_handlers.get(command)
                if handler is None:
                    self._send(chat_id, "Comando non riconosciuto. Usa /help.")
                    return "unknown_command"
                return handler(chat_id)
            return self._handle_text_input(chat_id, text)

        callback = update["callback_query"]
        chat_id = str(callback["message"]["chat"]["id"])
        data = callback.get("data")
        if not isinstance(data, str):
            self._send(chat_id, "Azione non valida.")
            return "invalid_callback"
        try:
            size = len(data.encode("utf-8"))
        except UnicodeError:
            size = 0
        if not 1 <= size <= TELEGRAM_CALLBACK_DATA_MAX_BYTES:
            self._send(chat_id, "Azione non valida.")
            return "invalid_callback"
        return self._handle_callback(chat_id, data)

    def _status(self, chat_id: str):
        try:
            paused = self.db.get_state("paused") != "false"
        except Exception:
            paused = True
        current = self._now()
        try:
            audience_date = current.astimezone(
                ZoneInfo(AUDIENCE_TIMEZONE)
            ).date()
            publication_positions = self.db.list_publication_positions(
                audience_date
            )
        except Exception:
            publication_positions = []
        target_today = (
            len(publication_positions)
            if len(publication_positions) in {2, 3}
            else POSTS_PER_DAY
        )
        cadence_reason = None
        if publication_positions:
            reason = publication_positions[0].get("selection_reason")
            if isinstance(reason, dict) and reason.get("timing_reason") in {
                "cold_start", "performance_weighted",
            }:
                cadence_reason = reason["timing_reason"]
        approved_count = pending_count = generation_used = 0
        try:
            operator_date = current.astimezone(ZoneInfo("Europe/Rome")).date()
            counts = self.db.get_queue_counts(operator_date, "Europe/Rome")
            approved_value = counts.get("approved_or_planned")
            awaiting_translation = counts.get("awaiting_translation")
            awaiting_review = counts.get("awaiting_review")
            if all(
                type(value) is int and value >= 0
                for value in (
                    approved_value, awaiting_translation, awaiting_review,
                )
            ):
                approved_count = approved_value
                pending_count = awaiting_translation + awaiting_review
            usage = self.db.get_replenishment_usage(operator_date, current)
            if type(usage) is int and usage >= 0:
                generation_used = usage
        except Exception:
            approved_count = pending_count = generation_used = 0
        lines = [
            "Stato",
            f"dry-run: {'attivo' if self.dry_run else 'disattivo'}",
            f"pausa: {'attiva' if paused else 'disattiva'}",
            f"coda approvata target: {APPROVED_QUEUE_TARGET}",
            f"coda approvata: {approved_count}/{APPROVED_QUEUE_TARGET}",
            f"in revisione: {pending_count}/{PENDING_REVIEW_LIMIT}",
            f"generazione oggi: {generation_used}/{DRAFT_GENERATION_DAILY_CAP}",
            f"pubblicazioni target oggi: {target_today}",
            f"pubblico: Stati Uniti ({AUDIENCE_TIMEZONE})",
        ]
        if cadence_reason is not None:
            lines.append(f"cadenza: {cadence_reason}")
        jobs = []
        if callable(self.scheduler_status):
            try:
                reported = self.scheduler_status()
                if isinstance(reported, (list, tuple)):
                    jobs = list(reported)[:10]
            except Exception:
                jobs = []
        if jobs:
            lines.append("prossimi job:")
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                name = self._clean_text(job.get("name"), 80) or "job"
                when = self._clean_text(job.get("next_run"), 80) or "non disponibile"
                lines.append(f"- {name}: {when}")
        else:
            lines.append("prossimi job: non disponibili")
        for plan in publication_positions[:3]:
            scheduled = self._clean_text(plan.get("scheduled_for"), 80)
            try:
                parsed = datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
            except (AttributeError, TypeError, ValueError):
                continue
            if parsed.tzinfo is None:
                continue
            et = parsed.astimezone(ZoneInfo(AUDIENCE_TIMEZONE))
            rome = parsed.astimezone(ZoneInfo("Europe/Rome"))
            lines.append(
                f"pubblicazione {plan.get('position')}: "
                f"{et.strftime('%Y-%m-%d %H:%M %Z')} / "
                f"{rome.strftime('%Y-%m-%d %H:%M %Z')}"
            )
        self._send(chat_id, "\n".join(lines))
        return "status"

    def _draft_markup(
        self,
        draft_id: int,
        *,
        queue_status: Optional[str] = None,
    ):
        prefix = str(draft_id)
        if queue_status is not None and queue_status != "ready":
            return self._callback_markup([
                [self._callback_button(
                    "Riprova traduzione",
                    f"draft:retry_translation:{prefix}",
                )],
                [
                    self._callback_button("Modifica", f"draft:edit:{prefix}"),
                    self._callback_button("Rigenera", f"draft:regen:{prefix}"),
                ],
                [self._callback_button("Scarta", f"draft:discard:{prefix}")],
            ])
        if queue_status == "ready":
            return self._callback_markup([
                [
                    self._callback_button("Approva", f"draft:approve:{prefix}"),
                    self._callback_button("Rigenera", f"draft:regen:{prefix}"),
                ],
                [
                    self._callback_button("Modifica", f"draft:edit:{prefix}"),
                    self._callback_button("Scegli media", f"draft:media:{prefix}"),
                ],
                [self._callback_button("Solo testo", f"draft:textonly:{prefix}")],
                [self._callback_button("Scarta", f"draft:discard:{prefix}")],
            ])
        return self._callback_markup([
            [
                self._callback_button("Approva", f"draft:approve:{prefix}"),
                self._callback_button("Rigenera", f"draft:regen:{prefix}"),
            ],
            [
                self._callback_button("Modifica", f"draft:edit:{prefix}"),
                self._callback_button("Scegli media", f"draft:media:{prefix}"),
            ],
            [
                self._callback_button("Solo testo", f"draft:textonly:{prefix}"),
                self._callback_button("Posticipa", f"draft:postpone:{prefix}"),
            ],
            [self._callback_button("Scarta", f"draft:discard:{prefix}")],
        ])

    def _draft_card_text(self, draft: Dict[str, Any]) -> str:
        source_labels = []
        for source_id in draft.get("source_ids") or []:
            if type(source_id) is not int:
                continue
            source = self.db.get_content_source(source_id)
            if not source:
                continue
            label = self._clean_text(source.get("source_type"), 60) or "source"
            metadata = source.get("metadata") or {}
            if isinstance(metadata, dict):
                detail = self._clean_text(
                    metadata.get("source_name") or metadata.get("title"), 100,
                )
                if detail:
                    label += f" ({detail})"
            source_labels.append(label)
            if len(source_labels) >= 3:
                break
        score = draft.get("score_data") or {}
        score_parts = []
        if isinstance(score, dict):
            for key in sorted(score):
                value = score[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                score_parts.append(f"{self._clean_text(str(key), 40)}: {value}")
        media_label = "nessuno"
        media_id = draft.get("media_id")
        if type(media_id) is int:
            media = self.db.get_media_by_id(media_id)
            if media:
                media_label = self._clean_text(media.get("filename"), 160) or str(media_id)
                description = self._clean_text(media.get("ai_description"), 240)
                if description:
                    media_label += f" — {description}"
        scheduled_for = draft.get("scheduled_for")
        planning_label = "non ancora pianificato"
        if isinstance(scheduled_for, str):
            try:
                scheduled = datetime.fromisoformat(scheduled_for.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                scheduled = None
            if scheduled is not None and scheduled.tzinfo is not None:
                et = scheduled.astimezone(ZoneInfo(AUDIENCE_TIMEZONE))
                rome = scheduled.astimezone(ZoneInfo("Europe/Rome"))
                planning_label = (
                    f"{et.strftime('%Y-%m-%d %H:%M %Z')} / "
                    f"{rome.strftime('%Y-%m-%d %H:%M %Z')}"
                )
        elif type(draft.get("queue_position")) is int:
            planning_label = f"in coda, posizione {draft['queue_position']}"
        fields = [
            ("Bozza #", draft.get("id")),
            ("stato: ", self._clean_text(draft.get("status"), 40) or "sconosciuto"),
            ("origine: ", self._clean_text(draft.get("origin"), 40) or "sconosciuta"),
            ("categoria: ", self._clean_text(draft.get("category"), 100) or "n/d"),
            ("pianificazione: ", planning_label),
            ("fonti: ", ", ".join(source_labels[:3]) if source_labels else "nessuna"),
            ("score: ", ", ".join(score_parts) if score_parts else "n/d"),
            ("media: ", media_label),
        ]
        return self._fit_labeled_lines(fields, TELEGRAM_MESSAGE_MAX_CHARS)

    def _send_complete_section(self, chat_id: str, label: str, body: str):
        if not isinstance(body, str) or not body:
            return
        prefix = label + "\n\n"
        first_size = TELEGRAM_MESSAGE_MAX_CHARS - len(prefix)
        self._send(chat_id, prefix + body[:first_size])
        offset = first_size
        while offset < len(body):
            self._send(chat_id, body[offset:offset + TELEGRAM_MESSAGE_MAX_CHARS])
            offset += TELEGRAM_MESSAGE_MAX_CHARS

    def _send_draft_card(self, chat_id: str, draft: Dict[str, Any]):
        draft_id = draft.get("id")
        queue_status = (
            draft.get("translation_status")
            if "translation_status" in draft
            else None
        )
        markup = (
            self._draft_markup(draft_id, queue_status=queue_status)
            if type(draft_id) is int
            else None
        )
        self._send_draft_preview(chat_id, draft)
        text = draft.get("text") if isinstance(draft.get("text"), str) else ""
        self._send_complete_section(chat_id, "Tweet da pubblicare", text)
        translation = draft.get("translation_it")
        if queue_status == "ready" and isinstance(translation, str):
            self._send_complete_section(
                chat_id,
                "Traduzione italiana — solo per revisione",
                translation,
            )
        elif queue_status is not None:
            self._send(
                chat_id,
                "Traduzione italiana — solo per revisione\n\n"
                "Non ancora disponibile.",
            )
        self._send(chat_id, self._draft_card_text(draft), reply_markup=markup)

    @staticmethod
    def _draft_preview_type(media: Dict[str, Any]) -> Optional[str]:
        media_type = media.get("media_type")
        mime_type = media.get("mime_type")
        filename = media.get("filename")
        if not isinstance(filename, str):
            return None
        was_document = filename.startswith("telegram-document-")
        if (
            media_type in {"image", "photo"}
            and mime_type in _PREVIEW_IMAGE_MIME_TYPES
        ):
            return "document" if was_document else "photo"
        if media_type == "video" and mime_type in _PREVIEW_VIDEO_MIME_TYPES:
            return "document" if was_document else "video"
        if (
            media_type == "document"
            and was_document
            and mime_type in _PREVIEW_IMAGE_MIME_TYPES | _PREVIEW_VIDEO_MIME_TYPES
        ):
            return "document"
        return None

    def _send_draft_preview(self, chat_id: str, draft: Dict[str, Any]) -> bool:
        draft_id = draft.get("id")
        media_id = draft.get("media_id")
        if type(draft_id) is not int or type(media_id) is not int:
            return False
        media = self.db.get_media_by_id(media_id)
        if not isinstance(media, dict) or media.get("id") != media_id:
            return False
        status = draft.get("status")
        lifecycle = media.get("lifecycle_state")
        if status == "published":
            tweet_id = draft.get("published_tweet_id")
            if (
                lifecycle != "used"
                or not isinstance(tweet_id, str)
                or not tweet_id
                or media.get("used_in_tweet_id") != tweet_id
            ):
                return False
        elif lifecycle != "reserved" or media.get("reserved_by_draft_id") != draft_id:
            return False
        preview_type = self._draft_preview_type(media)
        if preview_type is None:
            return False
        try:
            with open_verified_media(media) as media_file:
                if not self.db.validate_post_draft_preview_media(draft, media):
                    return False
                self.telegram_api.send_media(
                    chat_id,
                    media_file,
                    preview_type,
                    caption=f"Anteprima media bozza #{draft_id}",
                )
        except (OSError, RuntimeError, TelegramApiError, TypeError, ValueError):
            return False
        return True

    def _posts(self, chat_id: str):
        rows, next_cursor, _previous = self.db.list_post_index_page(cursor=None)
        token = self.db.create_telegram_view(
            chat_id, self.post_browser.view_kind,
            {"target_ids": [row["id"] for row in rows], "direction": "current",
             "filters": {"discarded": 0}, "last_message_id": None,
             "cursor": None, "previous_cursor": None,
             "next_cursor": next_cursor, "history": []},
        )
        discarded_rows, discarded_next, _previous = self.db.list_post_index_page(
            cursor=None, include_discarded=True,
        )
        discarded_token = self.db.create_telegram_view(
            chat_id, self.post_browser.view_kind,
            {"target_ids": [row["id"] for row in discarded_rows],
             "direction": "current", "filters": {"discarded": 1},
             "last_message_id": None, "cursor": None, "previous_cursor": None,
             "next_cursor": discarded_next, "history": []},
        )
        markup = self.post_browser.render_index(
            rows, token=token, has_next=next_cursor is not None, has_previous=False,
            discarded_token=discarded_token,
        )["reply_markup"]
        self._send(
            chat_id,
            self.post_browser.summary(rows, include_discarded=False),
            reply_markup=markup,
        )
        return "posts"

    def _render_post_browser(self, chat_id: str, token: str, action: str) -> bool:
        view = self.db.get_telegram_view(token, chat_id, self.post_browser.view_kind)
        if view is None:
            return False
        state = dict(view["state"])
        include_discarded = state["filters"].get("discarded") == 1
        cursor = state.get("cursor")
        next_cursor = state.get("next_cursor")
        history = list(state.get("history") or [])
        if action == "next":
            if next_cursor is None:
                return False
            history.append({
                "cursor": cursor, "target_ids": list(state["target_ids"]),
            })
            page_cursor = next_cursor
            rows, new_next, _back = self.db.list_post_index_page(
                cursor=page_cursor, include_discarded=include_discarded,
            )
        elif action == "prev":
            if not history:
                return False
            prior = history.pop()
            new_next = cursor
            page_cursor = prior["cursor"]
            rows = self.db.get_post_index_rows(
                prior["target_ids"], include_discarded=include_discarded,
            )
        elif action == "refresh":
            page_cursor = cursor
            new_next = next_cursor
            rows = self.db.get_post_index_rows(
                state["target_ids"], include_discarded=include_discarded,
            )
        else:
            return False
        state.update(
            target_ids=[row["id"] for row in rows], cursor=page_cursor,
            previous_cursor=history[-1]["cursor"] if history else None,
            next_cursor=new_next, history=history,
            filters={"discarded": int(include_discarded)},
        )
        if not self.db.update_telegram_view(
            token, chat_id, self.post_browser.view_kind, view["revision"], state,
        ):
            return False
        markup = self.post_browser.render_index(
            rows, token=token, has_next=new_next is not None,
            has_previous=bool(history), include_discarded=include_discarded,
        )["reply_markup"]
        self._send(
            chat_id,
            self.post_browser.summary(
                rows, include_discarded=include_discarded,
            ),
            reply_markup=markup,
        )
        return True

    def _post_detail(self, chat_id: str, token: str, draft_id: int, revision: int) -> str:
        view = self.db.get_telegram_view(token, chat_id, self.post_browser.view_kind)
        if view is None or draft_id not in view["state"]["target_ids"]:
            self._send(chat_id, "Post non valido o scaduto. Aggiorna l'elenco.")
            return "post_unavailable"
        draft = self.db.get_queue_draft(draft_id) or self.db.get_post_draft(draft_id)
        if draft is None or draft.get("revision") != revision:
            self._send(chat_id, "Post aggiornato: apri di nuovo l'elenco.")
            return "post_unavailable"
        index_rows = self.db.get_post_index_rows([draft_id], include_discarded=True)
        if index_rows:
            draft = {
                **draft,
                "scheduled_for": index_rows[0].get("scheduled_for"),
                "plan_status": index_rows[0].get("plan_status"),
            }
        self._send_draft_preview(chat_id, draft)
        self._send_complete_section(chat_id, "Tweet da pubblicare", draft.get("text") or "")
        if isinstance(draft.get("translation_it"), str):
            self._send_complete_section(
                chat_id,
                "Traduzione italiana — solo per revisione",
                draft["translation_it"],
            )
        else:
            self._send(
                chat_id,
                "Traduzione italiana — solo per revisione\n\n"
                "Non ancora disponibile.",
            )
        status = draft.get("status")
        rows = []
        if status == "approved":
            rows.append([self._callback_button(
                "Rimuovi dalla coda", f"postrm:{token}:{draft_id}:{revision}",
            )])
        elif status == "discarded":
            restore_token = self.db.create_telegram_view(
                chat_id,
                "post_restore_action",
                {
                    "target_ids": [draft_id],
                    "direction": "current",
                    "last_message_id": None,
                    "filters": {
                        "revision": revision,
                        "queue_revision": draft.get("queue_revision"),
                        "parent": token,
                    },
                },
            )
            rows.append([self._callback_button(
                "Ripristina", f"postrs:{restore_token}",
            )])
        rows.append([self._callback_button("Torna all'elenco", f"posts:{token}:refresh")])
        self._send(chat_id, self._draft_card_text(draft), reply_markup=self._callback_markup(rows))
        return "post_detail"

    @staticmethod
    def _candidate_url(candidate: Dict[str, Any]) -> Optional[str]:
        profile = candidate.get("profile") or {}
        username = candidate.get("username") or (
            profile.get("username") if isinstance(profile, dict) else None
        )
        if not isinstance(username, str) or _SAFE_USERNAME.fullmatch(username) is None:
            return None
        latest = candidate.get("latest_post") or {}
        tweet_id = None
        if isinstance(latest, dict):
            tweet_id = latest.get("id") or latest.get("tweet_id")
        if isinstance(tweet_id, str) and tweet_id.isascii() and tweet_id.isdigit():
            return f"https://x.com/{username}/status/{tweet_id}"
        return f"https://x.com/{username}"

    def _growth_card(self, candidate: Dict[str, Any]):
        profile = candidate.get("profile") or {}
        latest = candidate.get("latest_post") or {}
        username = self._clean_text(candidate.get("username"), 40) or "sconosciuto"
        bio = self._clean_text(
            profile.get("description") if isinstance(profile, dict) else None, 400,
        )
        followers = profile.get("followers_count") if isinstance(profile, dict) else None
        latest_text = self._clean_text(
            latest.get("text") if isinstance(latest, dict) else None, 500,
        )
        lines = [
            f"Candidato #{candidate.get('id')} — @{username}",
            f"score: {candidate.get('score')}",
            f"fonte: {self._clean_text(candidate.get('discovery_source'), 80) or 'n/d'}",
            f"follower: {followers if type(followers) is int else 'n/d'}",
        ]
        if bio:
            lines.append(f"bio: {bio}")
        if latest_text:
            lines.append(f"segnale: {latest_text}")
        candidate_id = candidate.get("id")
        rows = []
        direct_url = self._candidate_url(candidate)
        if direct_url:
            rows.append([{"text": "Open on X", "url": direct_url}])
        if type(candidate_id) is int:
            rows.extend([
                [
                    self._callback_button("Salva", f"growth:save:{candidate_id}"),
                    self._callback_button(
                        "Seguito su X", f"growth:followed:{candidate_id}",
                    ),
                ],
                [self._callback_button("Scarta", f"growth:discard:{candidate_id}")],
            ])
        return "\n".join(lines), self._callback_markup(rows) if rows else None

    def _growth(self, chat_id: str):
        report = self._build_weekly_analytics_report(self._now())
        if isinstance(report, dict) and report:
            self._send(chat_id, self.format_weekly_report(report))
        candidates = self.db.get_digest_candidates(limit=5)
        if not candidates:
            self._send(chat_id, "Nessun candidato growth disponibile.")
            return "growth_empty"
        self._send(chat_id, f"Growth: {len(candidates)} candidati. Azioni solo manuali.")
        for candidate in candidates:
            text, markup = self._growth_card(candidate)
            self._send(chat_id, text, reply_markup=markup)
        return "growth"

    def _build_weekly_analytics_report(self, end_date):
        report = None
        if self.analytics is not None:
            build = getattr(self.analytics, "build_weekly_report", None)
            if callable(build):
                report = build(end_date)
            for method_name in ("weekly_report", "get_weekly_report"):
                if report is not None:
                    break
                method = getattr(self.analytics, method_name, None)
                if callable(method):
                    report = method()
                    break
            if report is None:
                ranking = getattr(self.analytics, "get_ranking", None)
                if callable(ranking):
                    report = {"ranking": ranking(days=7)}
        return report

    @classmethod
    def format_weekly_report(cls, report: Dict[str, Any]) -> str:
        """Render the one canonical plain-text weekly analytics summary."""
        if not isinstance(report, dict) or not report:
            return "Statistiche non ancora disponibili."

        def integer(key):
            value = report.get(key)
            return value if type(value) is int and value >= 0 else 0

        def decimal(key):
            value = report.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return 0.0
            return max(float(value), 0.0)

        factual = report.get("factual_blocks")
        factual = factual if isinstance(factual, dict) else {}
        period = factual.get("period")
        period = period if isinstance(period, dict) else {}
        start_date = cls._clean_text(period.get("start_date"), 20) or "n/d"
        end_date = cls._clean_text(period.get("end_date"), 20) or "n/d"
        lines = [
            "Riepilogo settimanale",
            f"periodo: {start_date} — {end_date}",
            f"follower totali (followers_total: {integer('followers_total')})",
            (
                "nuovi follower: "
                f"{integer('new_followers')} "
                f"(pertinenti: {integer('new_relevant_followers')}, "
                f"tasso: {decimal('relevant_follower_rate') * 100:.1f}%)"
            ),
            f"candidati valutati nel report: {integer('candidate_count')}",
        ]

        decision_counts = report.get("decision_counts")
        decision_counts = (
            decision_counts if isinstance(decision_counts, dict) else {}
        )
        lines.append(
            "decisioni: "
            f"salvati {decision_counts.get('saved', 0) if type(decision_counts.get('saved')) is int else 0}, "
            f"seguiti manualmente {decision_counts.get('followed_manually', 0) if type(decision_counts.get('followed_manually')) is int else 0}, "
            f"scartati {decision_counts.get('discarded', 0) if type(decision_counts.get('discarded')) is int else 0}, "
            f"rifiutati {decision_counts.get('rejected', 0) if type(decision_counts.get('rejected')) is int else 0}"
        )

        rates = report.get("follow_back_rate_by_source")
        rates = rates if isinstance(rates, dict) else {}
        rate_parts = []
        for source in sorted(rates, key=lambda value: str(value))[:12]:
            rate = rates[source]
            if type(source) is not str or isinstance(rate, bool) or not isinstance(
                rate, (int, float),
            ):
                continue
            safe_source = cls._clean_text(source, 80)
            if safe_source:
                rate_parts.append(f"{safe_source} {max(float(rate), 0.0) * 100:.1f}%")
        lines.append(
            "follow-back per fonte: " + (", ".join(rate_parts) or "nessun dato")
        )
        lines.extend([
            (
                f"post: {integer('post_count')}; "
                f"impression mediane: {decimal('median_impressions'):g}"
            ),
            (
                f"budget query usato: {integer('query_budget_used')}; "
                f"profili valutati: {integer('profiles_evaluated')}"
            ),
        ])
        categories = report.get("content_by_category")
        categories = categories if isinstance(categories, dict) else {}
        category_parts = []
        for category in sorted(categories, key=lambda value: str(value))[:12]:
            count = categories[category]
            if type(category) is not str or type(count) is not int or count < 0:
                continue
            safe_category = cls._clean_text(category, 80)
            if safe_category:
                category_parts.append(f"{safe_category} {count}")
        lines.append(
            "contenuti per categoria: "
            + (", ".join(category_parts) or "nessun dato")
        )
        attribution = cls._clean_text(report.get("attribution_label"), 40)
        lines.append(f"attribuzione post/follower: {attribution or 'correlation'}")
        return "\n".join(lines)

    def _stats(self, chat_id: str):
        report = self._build_weekly_analytics_report(self._now())
        if not isinstance(report, dict) or not report:
            self._send(chat_id, "Statistiche non ancora disponibili.")
            return "stats_empty"
        self._send(chat_id, self.format_weekly_report(report))
        return "stats"

    def push_weekly_report(self, end_date=None):
        """Callable Task 12 may schedule; this task registers no job."""
        report = self._build_weekly_analytics_report(
            self._now() if end_date is None else end_date
        )
        if not isinstance(report, dict) or not report:
            return "weekly_report_empty"
        self._send(
            self.authorized_chat_id,
            self.format_weekly_report(report),
        )
        return "weekly_report_sent"

    def _source_type_markup(self):
        return self._callback_markup([
            [self._callback_button(label, f"input:source:{source_type}")]
            for source_type, label in _SOURCE_TYPES.items()
        ])

    def _ideas(self, chat_id: str):
        counts = {source_type: 0 for source_type in _SOURCE_TYPES}
        for source in self.db.get_eligible_sources():
            source_type = source.get("source_type")
            if source_type in counts:
                counts[source_type] += 1
        self._set_session(chat_id, "source_intake", "text", {})
        lines = ["Fonti attive"]
        for source_type, label in _SOURCE_TYPES.items():
            lines.append(f"{label}: {counts[source_type]}")
        lines.append("Invia ora il testo da classificare.")
        self._send(chat_id, "\n".join(lines))
        return "ideas"

    def _newpost(self, chat_id: str):
        self._set_session(chat_id, "manual_post", "text", {})
        self._send(
            chat_id,
            "Invia il testo inglese esatto del post (massimo 280 caratteri).",
            reply_markup=self._callback_markup([[
                self._callback_button("Annulla", "manual:cancel"),
            ]]),
        )
        return "manual_text_input"

    def _media(self, chat_id: str):
        """Open the persisted, verified media browser (never filename buttons)."""
        try:
            self.media_browser.show(chat_id=chat_id, media_id=None, context="manage")
        except Exception:
            self._send(chat_id, "Media non disponibile.")
            return "media_unavailable"
        return "media_browser"

    def _manual_category_markup(self):
        rows = [
            [self._callback_button(
                _MANUAL_CATEGORY_LABELS[category],
                f"manual:category:{category}",
            )]
            for category in PORTFOLIO
        ]
        rows.append([self._callback_button("Annulla", "manual:cancel")])
        return self._callback_markup(rows)

    @staticmethod
    def _manual_text_urls_are_safe(text: str) -> bool:
        urls = tuple(
            match.group(0).rstrip(".,!?;:)")
            for match in re.finditer(
                r"https?://[^\s<>\[\]{}\"']+",
                text,
                flags=re.IGNORECASE,
            )
        )
        return all(is_safe_https_url(url) for url in urls)

    def _manual_source_choice_markup(self):
        return self._callback_markup([
            [self._callback_button("Scegli fonti esistenti", "manual:sources:existing")],
            [self._callback_button("Aggiungi una fonte", "manual:sources:add")],
            [self._callback_button("Nessuna fonte", "manual:sources:none")],
            [self._callback_button("Annulla", "manual:cancel")],
        ])

    def _manual_source_markup(self, category: str, selected_ids, page: int = 0):
        allowed_types = MANUAL_SOURCE_TYPES.get(category, set())
        try:
            sources = self.db.get_eligible_sources(now=self._now())
        except Exception:
            sources = []
        rows = []
        selected = set(selected_ids)
        eligible = []
        for source in sources:
            source_id = source.get("id")
            source_type = source.get("source_type")
            if (
                type(source_id) is not int
                or source_id <= 0
                or source_type not in allowed_types
            ):
                continue
            eligible.append(source)
        page_size = 10
        start = page * page_size
        for source in eligible[start:start + page_size]:
            source_id = source["id"]
            source_type = source["source_type"]
            metadata = source.get("metadata")
            title = ""
            if isinstance(metadata, dict):
                title = self._clean_text(
                    metadata.get("title") or metadata.get("source_name"), 42,
                )
            marker = "✓ " if source_id in selected else ""
            label = f"{marker}#{source_id} {source_type}"
            trust_label = _SOURCE_TRUST_LABELS.get(source.get("trust_state"))
            trust_suffix = f" · {trust_label}" if trust_label else ""
            if trust_label:
                title_budget = 64 - len(label) - len(trust_suffix) - 3
                if title and title_budget > 0:
                    label += " — " + title[:title_budget]
                label += trust_suffix
            elif title:
                label += " — " + title
            rows.append([
                self._callback_button(label[:64], f"manual:source:{source_id}")
            ])
        if page > 0:
            rows.append([self._callback_button("←", f"manual:sources:page:{page - 1}")])
        if start + page_size < len(eligible):
            rows.append([self._callback_button("→", f"manual:sources:page:{page + 1}")])
        rows.append([
            self._callback_button("Fonti selezionate", "manual:sources_done")
        ])
        rows.append([self._callback_button("Annulla", "manual:cancel")])
        return self._callback_markup(rows)

    def _manual_child_source_markup(self, category: str):
        rows = [
            [self._callback_button(label, f"manual:child:source:{source_type}")]
            for source_type, label in _SOURCE_TYPES.items()
            if source_type in MANUAL_SOURCE_TYPES.get(category, set())
        ]
        rows.append([
            self._callback_button("Annulla", "manual:sources:child_cancel"),
        ])
        return self._callback_markup(rows)

    def _manual_media_markup(self):
        rows = [
            [self._callback_button("Sfoglia media", "manual:media:browse")],
            [self._callback_button("Nessun media", "manual:media:none")],
            [self._callback_button("Annulla", "manual:cancel")],
        ]
        return self._callback_markup(rows)

    def _finish_manual_post(
        self,
        chat_id: str,
        raw: str,
        session: Dict[str, Any],
    ):
        if self.draft_pipeline is None:
            self._send(chat_id, "Pipeline bozze non disponibile.")
            return "draft_unavailable"
        payload = session["payload"]
        try:
            draft, outcome = (
                self.draft_pipeline.create_manual_from_telegram_session(
                    text=payload["text"],
                    category=payload["category"],
                    source_ids=list(payload["source_ids"]),
                    media_id=payload["media_id"],
                    translation_it=None,
                    state_key=self._session_key(chat_id),
                    expected_state_value=raw,
                    session_token=session["token"],
                )
            )
        except Exception:
            self._send(
                chat_id,
                "Creazione non riuscita. La sessione è ancora disponibile.",
            )
            return "manual_create_failed"
        if outcome == "session_conflict":
            self._send(chat_id, "Operazione già gestita.")
            return "session_conflict"
        if outcome not in {"created", "already_applied"} or not isinstance(
            draft, dict,
        ):
            if outcome == "no_eligible_source":
                retained_source_ids = []
                for source_id in payload["source_ids"]:
                    try:
                        eligible = self.db.get_eligible_content_sources(
                            [source_id], now=self._now(),
                        )
                    except Exception:
                        eligible = []
                    if (
                        len(eligible) == 1
                        and eligible[0].get("id") == source_id
                        and eligible[0].get("source_type")
                        in MANUAL_SOURCE_TYPES[payload["category"]]
                    ):
                        retained_source_ids.append(source_id)
                resumed_payload = self._manual_parent_payload(payload)
                resumed_payload["source_ids"] = retained_source_ids
                if not self._replace_session(
                    chat_id,
                    raw,
                    "manual_post",
                    "sources",
                    resumed_payload,
                ):
                    self._send(chat_id, "Operazione già gestita.")
                    return "session_conflict"
                self._send(
                    chat_id,
                    "Le fonti selezionate non sono più disponibili. Selezionale di nuovo.",
                    reply_markup=self._manual_source_choice_markup(),
                )
                return "manual_source_stale"
            messages = {
                "media_unavailable": "Il media selezionato non è più disponibile.",
            }
            self._send(
                chat_id,
                messages.get(
                    outcome,
                    "Post respinto dai controlli editoriali. Correggi e riprova.",
                ),
            )
            return "manual_rejected"
        queued = self.db.get_queue_draft(draft.get("id")) or draft
        self._send(
            chat_id,
            "Post approvato e aggiunto alla coda approvata; "
            "traduzione italiana facoltativa in preparazione.",
        )
        self._send_draft_card(chat_id, queued)
        return "manual_created"

    def _pause(self, chat_id: str):
        self.db.set_state("paused", "true")
        self._send(chat_id, "Pubblicazioni in pausa.")
        return "paused"

    def _resume(self, chat_id: str):
        self.db.set_state("paused", "false")
        self._send(chat_id, "Pubblicazioni riattivate.")
        return "resumed"

    def _errors(self, chat_id: str):
        errors = self.db.get_recent_errors(limit=10, unresolved_only=False)
        if not errors:
            self._send(chat_id, "Nessun errore recente.")
            return "errors_empty"
        lines = ["Errori recenti"]
        for event in errors:
            context = self._clean_text(event.get("context"), 100)
            kind = self._clean_text(event.get("error_type"), 100)
            message = self._clean_text(event.get("safe_message"), 500)
            created = self._clean_text(event.get("created_at"), 80)
            lines.append(f"- {created} | {context} | {kind} | {message}")
        self._send(chat_id, "\n".join(lines))
        return "errors"

    def _help(self, chat_id: str):
        self._send(chat_id, "\n".join([
            "Comandi",
            "/status — stato e prossimi job",
            "/posts — bozze e pubblicati",
            "/growth — candidati manuali",
            "/stats — riepilogo performance",
            "/ideas — aggiungi una fonte",
            "/newpost — aggiungi un post manuale alla coda",
            "/pause — ferma le pubblicazioni",
            "/resume — riattiva le pubblicazioni",
            "/errors — ultimi errori sicuri",
            "/help — questo elenco",
        ]))
        return "help"

    @staticmethod
    def _session_key(chat_id: str) -> str:
        return f"telegram_session:{chat_id}"

    def _session_value(
        self,
        kind: str,
        step: str,
        payload: Dict[str, Any],
        *,
        token: Optional[str] = None,
    ) -> str:
        session = {
            "version": _SESSION_VERSION,
            "token": token or uuid4().hex,
            "kind": kind,
            "step": step,
            "payload": payload,
            "expires_at": (self._now() + _SESSION_TTL).isoformat(),
        }
        return json.dumps(session, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _set_session(self, chat_id: str, kind: str, step: str, payload: Dict[str, Any]):
        value = self._session_value(kind, step, payload)
        self.db.set_state(self._session_key(chat_id), value)
        return value

    def _valid_session_payload(self, kind: str, step: str, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        if kind == "source_intake":
            if step == "text":
                return payload == {}
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                return False
            if step == "classification":
                return set(payload) == {"text"}
            url = payload.get("url")
            if step == "news_url":
                return set(payload) == {"text"}
            if not isinstance(url, str) or not self._trusted_news_url(url):
                return False
            if step == "news_date":
                return set(payload) == {"text", "url"}
            published_at = payload.get("published_at")
            try:
                published = date.fromisoformat(published_at)
            except (TypeError, ValueError):
                return False
            return (
                step == "news_source"
                and set(payload) == {"text", "url", "published_at"}
                and published_at == published.isoformat()
                and published <= self._now().date()
            )
        if kind in {"draft_edit", "draft_postpone"}:
            return (
                set(payload) == {"draft_id"}
                and type(payload.get("draft_id")) is int
                and 0 < payload["draft_id"] <= _SQLITE_INTEGER_MAX
            )
        if kind == "manual_post":
            if step == "text":
                return payload == {}
            text = payload.get("text")
            if (
                type(text) is not str
                or not text.strip()
                or len(text) > 280
            ):
                return False
            if step == "category":
                return set(payload) == {"text"}
            category = payload.get("category")
            source_ids = payload.get("source_ids")
            if (
                type(category) is not str
                or category not in PORTFOLIO
                or type(source_ids) is not list
                or len(source_ids) > 3
                or any(
                    type(source_id) is not int
                    or not 0 < source_id <= _SQLITE_INTEGER_MAX
                    for source_id in source_ids
                )
                or len(set(source_ids)) != len(source_ids)
            ):
                return False
            if step == "sources":
                return set(payload) == {"text", "category", "source_ids"}
            if step == "media":
                return set(payload) == {"text", "category", "source_ids"}
            child = payload.get("child")
            if (
                not isinstance(child, dict)
                or type(child.get("token")) is not str
                or _SAFE_TOKEN.fullmatch(child["token"]) is None
                or child.get("return_step") != "sources"
            ):
                return False
            parent_keys = {"text", "category", "source_ids", "child"}
            if step == "source_child_text":
                return set(payload) == parent_keys and set(child) == {"token", "return_step"}
            child_text = child.get("text")
            if (
                type(child_text) is not str
                or not child_text.strip()
                or len(child_text) > 2000
            ):
                return False
            if step == "source_child_classification":
                return set(payload) == parent_keys and set(child) == {
                    "token", "return_step", "text",
                }
            if step == "source_child_news_url":
                return set(payload) == parent_keys and set(child) == {
                    "token", "return_step", "text",
                }
            child_url = child.get("url")
            if type(child_url) is not str or not self._trusted_news_url(child_url):
                return False
            if step == "source_child_news_date":
                return set(payload) == parent_keys and set(child) == {
                    "token", "return_step", "text", "url",
                }
            published_at = child.get("published_at")
            try:
                published = date.fromisoformat(published_at)
            except (TypeError, ValueError):
                return False
            return (
                step == "source_child_news_source"
                and set(payload) == parent_keys
                and set(child) == {
                    "token", "return_step", "text", "url", "published_at",
                }
                and published_at == published.isoformat()
                and published <= self._now().date()
            )
            media_id = payload.get("media_id")
            if media_id is not None and (
                type(media_id) is not int
                or not 0 < media_id <= _SQLITE_INTEGER_MAX
            ):
                return False
            return False
        return False

    def _decode_session(self, raw: Any):
        if not isinstance(raw, str) or len(raw) > 8192:
            return None
        try:
            session = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(session, dict) or set(session) != {
            "version", "token", "kind", "step", "payload", "expires_at",
        }:
            return None
        kind = session.get("kind")
        step = session.get("step")
        token = session.get("token")
        if (
            session.get("version") != _SESSION_VERSION
            or kind not in _SESSION_KINDS
            or step not in _SESSION_KINDS[kind]
            or not isinstance(token, str)
            or _SAFE_TOKEN.fullmatch(token) is None
            or not self._valid_session_payload(kind, step, session.get("payload"))
        ):
            return None
        try:
            expires_at = datetime.fromisoformat(
                session["expires_at"].replace("Z", "+00:00")
            )
            if expires_at.tzinfo is None or expires_at.astimezone(timezone.utc) <= self._now():
                return None
        except (AttributeError, TypeError, ValueError):
            return None
        return session

    def _load_session(self, chat_id: str):
        key = self._session_key(chat_id)
        raw = self.db.get_state(key)
        if raw is None:
            return None, None, False
        session = self._decode_session(raw)
        if session is None:
            self.db.compare_and_clear_state(key, raw)
            return raw, None, True
        return raw, session, False

    def _replace_session(
        self,
        chat_id: str,
        expected_raw: str,
        kind: str,
        step: str,
        payload: Dict[str, Any],
    ) -> bool:
        try:
            prior = json.loads(expected_raw)
        except (TypeError, ValueError):
            return False
        token = prior.get("token") if isinstance(prior, dict) else None
        if not isinstance(token, str) or _SAFE_TOKEN.fullmatch(token) is None:
            return False
        new_value = self._session_value(kind, step, payload, token=token)
        return self.db.compare_and_set_state(
            self._session_key(chat_id), expected_raw, new_value,
        )

    def _consume_session(self, chat_id: str, expected_raw: str) -> bool:
        return self.db.compare_and_clear_state(
            self._session_key(chat_id), expected_raw,
        )

    @staticmethod
    def _manual_parent_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "text": payload["text"],
            "category": payload["category"],
            "source_ids": list(payload["source_ids"]),
        }

    def _replace_manual_child_session(
        self,
        chat_id: str,
        raw: str,
        step: str,
        payload: Dict[str, Any],
        child: Dict[str, Any],
    ) -> bool:
        next_payload = self._manual_parent_payload(payload)
        next_payload["child"] = child
        return self._replace_session(chat_id, raw, "manual_post", step, next_payload)

    def _resume_manual_after_child(
        self,
        chat_id: str,
        raw: str,
        session: Dict[str, Any],
        *,
        source_type: str,
        text: str,
        url: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        payload = session["payload"]
        child = payload["child"]
        resumed_payload = self._manual_parent_payload(payload)
        resumed_raw = self._session_value(
            "manual_post", "sources", resumed_payload, token=session["token"],
        )
        source_id, outcome = self.db.add_content_source_and_resume_manual_state_atomic(
            state_key=self._session_key(chat_id),
            expected_state_value=raw,
            resumed_state_value=resumed_raw,
            child_token=child["token"],
            source_type=source_type,
            text=text,
            url=url,
            metadata=metadata or {},
            trust_state="verified",
            verified_by="floriano",
        )
        if outcome == "session_conflict":
            self._send(chat_id, "Operazione già gestita.")
            return "session_conflict"
        if outcome == "child_replay_mismatch":
            self._send(chat_id, "Operazione non valida.")
            return "invalid_session"
        if outcome == "duplicate":
            self._send(chat_id, "Questa fonte è già presente.")
            return "duplicate_source"
        if outcome not in {"created", "already_applied"} or type(source_id) is not int:
            self._send(chat_id, "Salvataggio fonte non riuscito. La sessione è ancora disponibile.")
            return "source_save_failed"
        self._send(
            chat_id,
            "Fonte salvata. Scegli altre fonti, oppure continua senza aggiungerne.",
            reply_markup=self._manual_source_choice_markup(),
        )
        return "manual_source_saved"

    def _handle_text_input(self, chat_id: str, text: str):
        raw, session, invalid = self._load_session(chat_id)
        if invalid:
            self._send(chat_id, "Sessione non valida o scaduta. Riprova.")
            return "invalid_session"
        if session is None:
            self._send(chat_id, "Usa /ideas o un pulsante per iniziare.")
            return "no_session"

        clean = text.strip()
        kind = session["kind"]
        step = session["step"]
        payload = session["payload"]
        if kind == "manual_post":
            if step == "text":
                if (
                    not text.strip()
                    or len(text) > 280
                    or not self._manual_text_urls_are_safe(text)
                ):
                    self._send(
                        chat_id,
                        "Testo non valido: massimo 280 caratteri e solo URL HTTPS sicuri.",
                    )
                    return "invalid_manual_text"
                if not self._replace_session(
                    chat_id, raw, kind, "category", {"text": text},
                ):
                    self._send(chat_id, "Operazione già gestita.")
                    return "session_conflict"
                self._send(
                    chat_id,
                    "Scegli la categoria editoriale.",
                    reply_markup=self._manual_category_markup(),
                )
                return "manual_category"
            if step == "source_child_text":
                if not clean or len(clean) > 2000:
                    self._send(chat_id, "Testo non valido: massimo 2000 caratteri.")
                    return "invalid_source_text"
                child = {**payload["child"], "text": clean}
                if not self._replace_manual_child_session(
                    chat_id, raw, "source_child_classification", payload, child,
                ):
                    self._send(chat_id, "Operazione già gestita.")
                    return "session_conflict"
                self._send(
                    chat_id, "Scegli il tipo di fonte.",
                    reply_markup=self._manual_child_source_markup(payload["category"]),
                )
                return "manual_child_classification"
            if step == "source_child_news_url":
                if not self._trusted_news_url(clean):
                    self._send(chat_id, "URL non valido: usa una pagina HTTPS allowlisted.")
                    return "invalid_news_url"
                child = {**payload["child"], "url": clean}
                if not self._replace_manual_child_session(
                    chat_id, raw, "source_child_news_date", payload, child,
                ):
                    self._send(chat_id, "Operazione già gestita.")
                    return "session_conflict"
                self._send(chat_id, "Inserisci la data di pubblicazione: YYYY-MM-DD.")
                return "manual_child_news_date"
            if step == "source_child_news_date":
                try:
                    published = date.fromisoformat(clean)
                except (TypeError, ValueError):
                    published = None
                if (
                    published is None or clean != published.isoformat()
                    or published > self._now().date()
                ):
                    self._send(chat_id, "Data non valida: usa YYYY-MM-DD, non futura.")
                    return "invalid_news_date"
                child = {**payload["child"], "published_at": clean}
                if not self._replace_manual_child_session(
                    chat_id, raw, "source_child_news_source", payload, child,
                ):
                    self._send(chat_id, "Operazione già gestita.")
                    return "session_conflict"
                self._send(chat_id, "Inserisci il nome della fonte.")
                return "manual_child_news_source"
            if step == "source_child_news_source":
                source_name = self._clean_text(clean, 120)
                if not source_name or len(clean) > 120:
                    self._send(chat_id, "Nome fonte non valido: massimo 120 caratteri.")
                    return "invalid_news_source"
                child = payload["child"]
                metadata = {
                    "title": self._clean_text(child["text"].splitlines()[0], 200),
                    "summary": child["text"],
                    "published_at": child["published_at"],
                    "source_name": source_name,
                }
                candidate = {
                    "source_type": "verified_news", "text": child["text"],
                    "url": child["url"], "metadata": metadata,
                    "trust_state": "verified",
                }
                if not is_complete_verified_news(candidate):
                    self._send(chat_id, "News incompleta o non valida.")
                    return "invalid_news_source"
                return self._resume_manual_after_child(
                    chat_id, raw, session, source_type="verified_news",
                    text=child["text"], url=child["url"], metadata=metadata,
                )
        if kind == "source_intake":
            if step == "text":
                if not clean or len(clean) > 2000:
                    self._send(chat_id, "Testo non valido: massimo 2000 caratteri.")
                    return "invalid_source_text"
                if not self._replace_session(
                    chat_id, raw, kind, "classification", {"text": clean},
                ):
                    self._send(chat_id, "Operazione già gestita.")
                    return "session_conflict"
                self._send(
                    chat_id,
                    "Scegli il tipo di fonte.",
                    reply_markup=self._source_type_markup(),
                )
                return "source_classification"
            if step == "news_url":
                if not self._trusted_news_url(clean):
                    self._send(chat_id, "URL non valido: usa una pagina HTTPS allowlisted.")
                    return "invalid_news_url"
                if not self._replace_session(
                    chat_id,
                    raw,
                    kind,
                    "news_date",
                    {"text": payload["text"], "url": clean},
                ):
                    self._send(chat_id, "Operazione già gestita.")
                    return "session_conflict"
                self._send(chat_id, "Inserisci la data di pubblicazione: YYYY-MM-DD.")
                return "news_date"
            if step == "news_date":
                try:
                    published = date.fromisoformat(clean)
                except (TypeError, ValueError):
                    published = None
                if (
                    published is None
                    or clean != published.isoformat()
                    or published > self._now().date()
                ):
                    self._send(chat_id, "Data non valida: usa YYYY-MM-DD, non futura.")
                    return "invalid_news_date"
                if not self._replace_session(
                    chat_id,
                    raw,
                    kind,
                    "news_source",
                    {
                        "text": payload["text"],
                        "url": payload["url"],
                        "published_at": clean,
                    },
                ):
                    self._send(chat_id, "Operazione già gestita.")
                    return "session_conflict"
                self._send(chat_id, "Inserisci il nome della fonte.")
                return "news_source"
            if step == "news_source":
                source_name = self._clean_text(clean, 120)
                if not source_name or len(clean) > 120:
                    self._send(chat_id, "Nome fonte non valido: massimo 120 caratteri.")
                    return "invalid_news_source"
                summary = payload["text"]
                title = self._clean_text(summary.splitlines()[0], 200)
                _source_id, outcome = (
                    self.db.add_content_source_consuming_state_atomic(
                        state_key=self._session_key(chat_id),
                        expected_state_value=raw,
                        source_type="verified_news",
                        text=summary,
                        url=payload["url"],
                        metadata={
                            "title": title,
                            "summary": summary,
                            "published_at": payload["published_at"],
                            "source_name": source_name,
                        },
                        trust_state="verified",
                        verified_by="floriano",
                    )
                )
                if outcome == "session_conflict":
                    self._send(chat_id, "Operazione già gestita.")
                    return "session_conflict"
                if outcome == "duplicate":
                    self._send(chat_id, "Questa fonte è già presente.")
                    return "duplicate_source"
                self._send(chat_id, "News verificata salvata.")
                return "source_saved"

        if kind == "draft_edit" and step == "text":
            if self.draft_pipeline is None:
                self._send(chat_id, "Pipeline bozze non disponibile.")
                return "draft_unavailable"
            replacement, outcome = self.draft_pipeline.edit_from_telegram_session(
                payload["draft_id"],
                clean,
                state_key=self._session_key(chat_id),
                expected_state_value=raw,
                session_token=session["token"],
            )
            if outcome == "session_conflict":
                self._send(chat_id, "Operazione già gestita.")
                return "session_conflict"
            if not isinstance(replacement, dict):
                self._send(chat_id, "Modifica respinta dai controlli editoriali.")
                return "draft_edit_rejected"
            replacement = self.db.get_queue_draft(replacement.get("id")) or replacement
            if (
                replacement.get("translation_status") not in {None, "ready"}
                and self.queue_service is not None
            ):
                try:
                    self.queue_service.retry_pending_translations(
                        self._now(), limit=1, draft_id=replacement.get("id"),
                    )
                except Exception:
                    pass
                replacement = self.db.get_queue_draft(replacement.get("id")) or replacement
            if replacement.get("translation_status") not in {None, "ready"}:
                self._send(
                    chat_id,
                    "Modifica validata; traduzione ancora in preparazione.",
                )
                return "draft_edited"
            self._send(chat_id, "Modifica validata; nuova bozza in approvazione.")
            self._send_draft_card(chat_id, replacement)
            return "draft_edited"

        if kind == "draft_postpone" and step == "slot":
            try:
                slot = datetime.fromisoformat(clean.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                slot = None
            if slot is None or slot.tzinfo is None or slot.astimezone(timezone.utc) <= self._now():
                self._send(chat_id, "Slot non valido: usa ISO 8601 con fuso e data futura.")
                return "invalid_slot"
            if self.draft_pipeline is None:
                self._send(chat_id, "Pipeline bozze non disponibile.")
                return "draft_unavailable"
            normalized = slot.isoformat()
            outcome = self.draft_pipeline.postpone_from_telegram_session(
                payload["draft_id"],
                normalized,
                state_key=self._session_key(chat_id),
                expected_state_value=raw,
            )
            if outcome == "session_conflict":
                self._send(chat_id, "Operazione già gestita.")
                return "session_conflict"
            if outcome != "postponed":
                self._send(chat_id, "Riprogrammazione non riuscita.")
                return "draft_postpone_rejected"
            self._send(chat_id, "Bozza riprogrammata; serve una nuova approvazione.")
            return "draft_postponed"

        self.db.compare_and_clear_state(self._session_key(chat_id), raw)
        self._send(chat_id, "Sessione non valida o scaduta. Riprova.")
        return "invalid_session"

    def _trusted_news_url(self, value: str) -> bool:
        if not self.news_trusted_domains or not isinstance(value, str) or len(value) > 2048:
            return False
        try:
            parsed = urlparse(value)
            port = parsed.port
        except (TypeError, ValueError):
            return False
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme.lower() != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
            or not parsed.path
            or parsed.path == "/"
        ):
            return False
        return any(
            host == domain or host.endswith("." + domain)
            for domain in self.news_trusted_domains
        )

    def _handle_callback(self, chat_id: str, data: str):
        parts = data.split(":")
        if parts[0] == "posts" and len(parts) == 3:
            rendered = self._render_post_browser(chat_id, parts[1], parts[2])
            if not rendered:
                self._send(chat_id, "Elenco non valido o scaduto. Usa /posts.")
            return "posts_browser" if rendered else "post_unavailable"
        if parts[0] == "post" and len(parts) == 4:
            draft_id, revision = self._positive_id(parts[2]), self._nonnegative_id(parts[3])
            if draft_id is not None and revision is not None:
                return self._post_detail(chat_id, parts[1], draft_id, revision)
        if parts[0] == "postrm" and len(parts) == 4:
            draft_id, revision = self._positive_id(parts[2]), self._nonnegative_id(parts[3])
            if draft_id is not None and revision is not None:
                return self._post_queue_action(chat_id, parts[0], parts[1], draft_id, revision)
        if parts[0] == "postrs" and len(parts) == 2:
            return self._restore_post_queue_action(chat_id, parts[1])
        if parts[0] == "postok" and len(parts) == 2:
            return self._confirm_post_removal(chat_id, parts[1])
        if parts[0] == "mb":
            return self._media_browser_callback(chat_id, parts)
        if parts[0] == "draft" and len(parts) == 3:
            draft_id = self._positive_id(parts[2])
            if draft_id is not None:
                return self._draft_callback(chat_id, parts[1], draft_id)
        if parts[0] == "growth":
            return self._growth_callback(chat_id, parts)
        if parts[0] == "input":
            return self._input_callback(chat_id, parts)
        if parts[0] == "manual":
            return self._manual_callback(chat_id, parts)
        self._send(chat_id, "Azione non valida.")
        return "invalid_callback"

    def _post_queue_action(
        self, chat_id: str, action: str, parent: str,
        draft_id: int, revision: int,
    ) -> str:
        view = self.db.get_telegram_view(parent, chat_id, self.post_browser.view_kind)
        draft = self.db.get_queue_draft(draft_id)
        if (
            view is None or draft_id not in view["state"]["target_ids"]
            or draft is None or draft.get("revision") != revision
        ):
            self._send(chat_id, "Azione non valida o scaduta. Aggiorna l'elenco.")
            return "post_unavailable"
        if draft.get("status") != "approved":
            self._send(chat_id, "Rimozione non disponibile. Aggiorna l'elenco.")
            return "post_remove_rejected"
        try:
            confirmation = self.db.create_telegram_view(
                chat_id, "post_remove_confirm",
                {"target_ids": [draft_id], "direction": "current", "last_message_id": None,
                 "cursor": None, "previous_cursor": None,
                 "filters": {
                     "revision": revision,
                     "queue_revision": draft["queue_revision"],
                     "parent": parent,
                 }},
            )
            excerpt = self.post_browser.excerpt(draft.get("text"))
            self._send(
                chat_id,
                f"Rimuovere dalla coda?\n{excerpt}",
                reply_markup=self._callback_markup([[
                    self._callback_button(
                        "Conferma rimozione", f"postok:{confirmation}",
                    ),
                    self._callback_button(
                        "Annulla", f"post:{parent}:{draft_id}:{revision}",
                    ),
                ]]),
            )
            return "post_remove_confirmation"
        except Exception:
            self._send(chat_id, "Rimozione non disponibile.")
            return "post_remove_rejected"

    def _restore_post_queue_action(self, chat_id: str, token: str) -> str:
        action = self.db.get_telegram_view(
            token, chat_id, "post_restore_action",
        )
        if action is None or len(action["state"]["target_ids"]) != 1:
            self._send(chat_id, "Ripristino non valido o scaduto.")
            return "post_restore_rejected"
        draft_id = action["state"]["target_ids"][0]
        filters = action["state"]["filters"]
        parent = filters.get("parent")
        parent_view = (
            self.db.get_telegram_view(
                parent, chat_id, self.post_browser.view_kind,
            )
            if isinstance(parent, str) else None
        )
        if parent_view is None or draft_id not in parent_view["state"]["target_ids"]:
            self._send(chat_id, "Ripristino non valido o scaduto.")
            return "post_restore_rejected"
        restored, outcome = self.db.restore_discarded_draft_atomic(
            draft_id,
            filters.get("revision"),
            filters.get("queue_revision"),
            "floriano",
            token,
        )
        if outcome not in {"restored", "already_applied"} or restored is None:
            self._send(chat_id, "Ripristino non disponibile. Aggiorna l'elenco.")
            return "post_restore_rejected"
        self._send(chat_id, "Post ripristinato.")
        return self._post_detail(
            chat_id, parent, draft_id, restored["revision"],
        )

    def _confirm_post_removal(self, chat_id: str, token: str) -> str:
        confirm = self.db.get_telegram_view(token, chat_id, "post_remove_confirm")
        if confirm is None:
            self._send(chat_id, "Conferma non valida o scaduta.")
            return "post_remove_rejected"
        draft_id = confirm["state"]["target_ids"][0]
        filters = confirm["state"]["filters"]
        parent = filters.get("parent")
        parent_view = (
            self.db.get_telegram_view(
                parent, chat_id, self.post_browser.view_kind,
            )
            if isinstance(parent, str) else None
        )
        if parent_view is None or draft_id not in parent_view["state"]["target_ids"]:
            self._send(chat_id, "Conferma non valida o scaduta.")
            return "post_remove_rejected"
        changed, outcome = self.db.discard_queued_draft_atomic(
            draft_id, filters.get("revision"), filters.get("queue_revision"), "floriano", token,
        )
        if outcome not in {"discarded", "already_applied"} or changed is None:
            self._send(chat_id, "Rimozione non disponibile. Aggiorna l'elenco.")
            return "post_remove_rejected"
        self._send(chat_id, "Post rimosso dalla coda.")
        return self._post_detail(chat_id, parent, draft_id, changed["revision"])

    def _media_browser_callback(self, chat_id: str, parts):
        if len(parts) == 3 and parts[1] in {"p", "n"}:
            rendered = self.media_browser.render(
                token=parts[2], chat_id=chat_id,
                direction="previous" if parts[1] == "p" else "next",
            )
            return "media_browser" if rendered else "media_unavailable"
        def validated_action_view(token, media_id, revision):
            view = self.db.get_telegram_view(
                token, chat_id, self.media_browser.view_kind,
            )
            if (
                view is None
                or media_id not in view["state"].get("target_ids", [])
                or type(revision) is not int or revision < 0
            ):
                return None
            return view

        if len(parts) == 5 and parts[1] == "u":
            try:
                media_id = int(parts[3])
                revision = int(parts[4])
            except ValueError:
                return "invalid_callback"
            if validated_action_view(parts[2], media_id, revision) is None:
                self._send(chat_id, "Selezione media non valida o scaduta.")
                return "media_unavailable"
            record = self.media_browser.select(
                media_id=media_id, expected_revision=revision,
            )
            if record is None:
                self._send(chat_id, "Media non più disponibile.")
                return "media_unavailable"
            raw, session, invalid = self._load_session(chat_id)
            if not invalid and session is not None and session.get("kind") == "manual_post" and session.get("step") == "media":
                payload = dict(session["payload"])
                payload["media_id"] = media_id
                return self._finish_manual_post(chat_id, raw, {**session, "payload": payload})
            self._send(chat_id, f"Media #{media_id} selezionato.")
            return "media_selected"
        if len(parts) == 5 and parts[1] in {"a", "r", "d"}:
            try:
                media_id = int(parts[3])
                revision = int(parts[4])
            except ValueError:
                return "invalid_callback"
            source_view = validated_action_view(parts[2], media_id, revision)
            if source_view is None:
                self._send(chat_id, "Azione media non valida o scaduta.")
                return "media_unavailable"
            record = self.db.get_media_by_id(media_id)
            if (
                not isinstance(record, dict) or record.get("revision") != revision
                or not isinstance(record.get("file_sha256"), str)
            ):
                self._send(chat_id, "Media non più disponibile.")
                return "media_unavailable"
            digest = record["file_sha256"]
            if parts[1] == "a":
                changed = self.db.archive_media_atomic(media_id, revision, digest)
                self._send(chat_id, "Media archiviato." if changed else "Media non più disponibile.")
                return "media_archived" if changed else "media_unavailable"
            if parts[1] == "r":
                changed = self.db.restore_media_atomic(media_id, revision, digest)
                self._send(chat_id, "Media ripristinato." if changed else "Media non più disponibile.")
                return "media_restored" if changed else "media_unavailable"
            try:
                confirm = self.db.create_telegram_view(
                    chat_id, "media_delete_confirm",
                    {"target_ids": [media_id], "direction": "current", "filters": {
                        "revision": revision, "sha256": digest,
                        "parent_token": parts[2],
                    }, "last_message_id": None},
                )
                self._send(chat_id, "Confermi l'eliminazione definitiva?", reply_markup=self._callback_markup([[
                    self._callback_button("Conferma eliminazione", f"mb:x:{confirm}"),
                    self._callback_button("Annulla", f"mb:q:{parts[2]}"),
                ]]))
            except Exception:
                self._send(chat_id, "Media non disponibile.")
                return "media_unavailable"
            return "media_delete_confirmation"
        if len(parts) == 3 and parts[1] == "x":
            view = self.db.get_telegram_view(parts[2], chat_id, "media_delete_confirm")
            if view is None or len(view["state"]["target_ids"]) != 1:
                return "media_unavailable"
            media_id = view["state"]["target_ids"][0]
            filters = view["state"]["filters"]
            parent_token = filters.get("parent_token")
            parent_view = (
                self.db.get_telegram_view(
                    parent_token, chat_id, self.media_browser.view_kind,
                ) if isinstance(parent_token, str) else None
            )
            if parent_view is None or media_id not in parent_view["state"]["target_ids"]:
                self._send(chat_id, "Conferma eliminazione non valida o scaduta.")
                return "media_unavailable"
            changed = self.db.delete_unused_media_safely(
                media_id, filters.get("revision"), filters.get("sha256"),
            )
            self._send(chat_id, "Media eliminato definitivamente." if changed else "Media non più eliminabile.")
            return "media_deleted" if changed else "media_unavailable"
        if len(parts) == 3 and parts[1] in {"z", "c"}:
            if parts[1] == "z":
                raw, session, invalid = self._load_session(chat_id)
                if not invalid and session is not None and session.get("kind") == "manual_post" and session.get("step") == "media":
                    payload = dict(session["payload"])
                    payload["media_id"] = None
                    return self._finish_manual_post(chat_id, raw, {**session, "payload": payload})
            self._send(chat_id, "Selezione media annullata.")
            return "media_cancelled"
        if len(parts) == 3 and parts[1] == "q":
            rendered = self.media_browser.render(
                token=parts[2], chat_id=chat_id, direction="current",
            )
            return "media_browser" if rendered else "media_unavailable"
        if len(parts) == 3 and parts[1] == "g":
            self._send(chat_id, "Gestione media: apri /media per una selezione aggiornata.")
            return "media_manage"
        self._send(chat_id, "Azione media non valida.")
        return "invalid_callback"

    def _draft_callback(self, chat_id: str, action: str, draft_id: int):
        if action in {"approve", "regen", "edit", "postpone", "discard"} and (
            self.draft_pipeline is None
        ):
            self._send(chat_id, "Pipeline bozze non disponibile.")
            return "draft_unavailable"
        if action == "approve":
            queued = self.db.get_queue_draft(draft_id)
            if queued is not None:
                approve_queue = getattr(self.draft_pipeline, "approve_queue", None)
                approved = bool(
                    callable(approve_queue)
                    and approve_queue(draft_id, "floriano")
                )
            else:
                approved = self.draft_pipeline.approve(draft_id, "floriano")
            draft = self.db.get_queue_draft(draft_id) or self.db.get_post_draft(draft_id)
            if approved:
                self._send(
                    chat_id,
                    "Bozza approvata: entrerà nella pianificazione automatica.",
                )
                return "draft_approved"
            if draft and draft.get("status") == "expired":
                self._send(
                    chat_id,
                    "Slot scaduto: la bozza non sarà pubblicata. Riprogrammala.",
                    reply_markup=self._callback_markup([[
                        self._callback_button(
                            "Riprogramma", f"draft:postpone:{draft_id}",
                        )
                    ]]),
                )
                return "draft_expired"
            self._send(chat_id, "Approvazione non disponibile.")
            return "draft_approve_rejected"
        if action == "retry_translation":
            if self.queue_service is None:
                self._send(chat_id, "Servizio traduzione non disponibile.")
                return "translation_unavailable"
            queued = self.db.get_queue_draft(draft_id)
            if not queued or queued.get("status") not in {
                "pending_approval", "approved",
            }:
                self._send(chat_id, "Bozza non traducibile.")
                return "translation_rejected"
            try:
                self.queue_service.retry_pending_translations(
                    self._now(), limit=1, draft_id=draft_id,
                )
            except Exception:
                self._send(chat_id, "Traduzione non riuscita. Riprova più tardi.")
                return "translation_failed"
            refreshed = self.db.get_queue_draft(draft_id)
            if not refreshed or refreshed.get("translation_status") != "ready":
                self._send(chat_id, "Traduzione non riuscita. Riprova più tardi.")
                return "translation_failed"
            self._send(chat_id, "Traduzione pronta per la revisione.")
            self._send_draft_card(chat_id, refreshed)
            return "translation_ready"
        if action == "regen":
            replacement = self.draft_pipeline.regenerate(draft_id)
            if not isinstance(replacement, dict):
                self._send(chat_id, "Rigenerazione non riuscita.")
                return "draft_regen_rejected"
            replacement = self.db.get_queue_draft(replacement.get("id")) or replacement
            if (
                replacement.get("translation_status") not in {None, "ready"}
                and self.queue_service is not None
            ):
                try:
                    self.queue_service.retry_pending_translations(
                        self._now(), limit=1, draft_id=replacement.get("id"),
                    )
                except Exception:
                    pass
                replacement = self.db.get_queue_draft(replacement.get("id")) or replacement
            if replacement.get("translation_status") not in {None, "ready"}:
                self._send(
                    chat_id,
                    "Nuova bozza generata; traduzione ancora in preparazione.",
                )
                return "draft_regenerated"
            self._send(chat_id, "Nuova bozza generata.")
            self._send_draft_card(chat_id, replacement)
            return "draft_regenerated"
        if action == "edit":
            draft = self.db.get_post_draft(draft_id)
            if not draft or draft.get("status") != "pending_approval":
                self._send(chat_id, "Bozza non modificabile.")
                return "draft_edit_rejected"
            self._set_session(chat_id, "draft_edit", "text", {"draft_id": draft_id})
            self._send(chat_id, "Invia il nuovo testo completo.")
            return "draft_edit_input"
        if action == "media":
            if self.media_matcher is None:
                self._send(chat_id, "Matcher media non disponibile.")
                return "media_unavailable"
            media = self.media_matcher.attach_best(draft_id)
            if not isinstance(media, dict):
                self._send(chat_id, "Nessun media sufficientemente pertinente.")
                return "media_not_matched"
            self._send(chat_id, f"Media #{media.get('id')} associato alla bozza.")
            draft = self.db.get_post_draft(draft_id)
            if draft:
                self._send_draft_card(chat_id, draft)
            return "media_matched"
        if action == "textonly":
            detach = getattr(self.db, "detach_media_from_draft", None)
            if not callable(detach) or not detach(draft_id):
                self._send(chat_id, "Impossibile impostare la bozza solo testo.")
                return "textonly_rejected"
            self._send(chat_id, "Bozza impostata solo testo.")
            return "textonly"
        if action == "postpone":
            draft = self.db.get_post_draft(draft_id)
            if not draft or draft.get("status") not in {
                "pending_approval", "approved", "expired",
            }:
                self._send(chat_id, "Bozza non riprogrammabile.")
                return "draft_postpone_rejected"
            self._set_session(
                chat_id, "draft_postpone", "slot", {"draft_id": draft_id},
            )
            self._send(chat_id, "Invia il nuovo slot ISO 8601 con fuso orario.")
            return "draft_postpone_input"
        if action == "discard":
            if not self.draft_pipeline.discard(draft_id, "user_discarded"):
                self._send(chat_id, "Bozza non scartabile.")
                return "draft_discard_rejected"
            self._send(chat_id, "Bozza scartata.")
            return "draft_discarded"
        self._send(chat_id, "Azione bozza non valida.")
        return "invalid_callback"

    def _input_callback(self, chat_id: str, parts):
        if parts == ["input", "cancel"]:
            raw = self.db.get_state(self._session_key(chat_id))
            if raw is not None:
                self.db.compare_and_clear_state(self._session_key(chat_id), raw)
            self._send(chat_id, "Operazione annullata.")
            return "input_cancelled"
        if len(parts) != 3 or parts[1] != "source" or parts[2] not in _SOURCE_TYPES:
            self._send(chat_id, "Classificazione non valida.")
            return "invalid_callback"
        raw, session, invalid = self._load_session(chat_id)
        if invalid or session is None:
            self._send(chat_id, "Sessione non valida o scaduta. Riprova.")
            return "invalid_session"
        if session["kind"] != "source_intake" or session["step"] != "classification":
            self._send(chat_id, "Questa sessione non attende una classificazione.")
            return "invalid_session"
        source_type = parts[2]
        text = session["payload"]["text"]
        if source_type == "verified_news":
            if not self._replace_session(
                chat_id, raw, "source_intake", "news_url", {"text": text},
            ):
                self._send(chat_id, "Operazione già gestita.")
                return "session_conflict"
            self._send(chat_id, "Inserisci l'URL HTTPS allowlisted dell'articolo.")
            return "news_url"
        _source_id, outcome = self.db.add_content_source_consuming_state_atomic(
            state_key=self._session_key(chat_id),
            expected_state_value=raw,
            source_type=source_type,
            text=text,
            metadata=(
                {"publishable": True}
                if source_type == "founder_note"
                else None
            ),
            trust_state="verified",
            verified_by="floriano",
        )
        if outcome == "session_conflict":
            self._send(chat_id, "Operazione già gestita.")
            return "session_conflict"
        self._send(chat_id, f"{_SOURCE_TYPES[source_type]} salvata.")
        return "source_saved"

    def _manual_callback(self, chat_id: str, parts):
        if parts == ["manual", "cancel"]:
            raw = self.db.get_state(self._session_key(chat_id))
            if raw is not None:
                self.db.compare_and_clear_state(self._session_key(chat_id), raw)
            self._send(chat_id, "Creazione del post annullata.")
            return "manual_cancelled"

        raw, session, invalid = self._load_session(chat_id)
        if invalid or session is None:
            self._send(chat_id, "Sessione non valida o scaduta. Riprova.")
            return "invalid_session"
        if session["kind"] != "manual_post":
            self._send(chat_id, "Questa sessione non riguarda un post manuale.")
            return "invalid_session"

        step = session["step"]
        payload = session["payload"]
        if len(parts) == 3 and parts[1] == "category":
            category = parts[2]
            if step != "category" or category not in PORTFOLIO:
                self._send(chat_id, "Categoria non valida.")
                return "invalid_callback"
            next_payload = {
                "text": payload["text"],
                "category": category,
                "source_ids": [],
            }
            if not self._replace_session(
                chat_id, raw, "manual_post", "sources", next_payload,
            ):
                self._send(chat_id, "Operazione già gestita.")
                return "session_conflict"
            self._send(
                chat_id,
                "Scegli fino a tre fonti facoltative. Scegli come procedere.",
                reply_markup=self._manual_source_choice_markup(),
            )
            return "manual_sources"

        if parts == ["manual", "sources", "existing"]:
            if step != "sources":
                self._send(chat_id, "Questa sessione non attende fonti.")
                return "invalid_session"
            self._send(
                chat_id,
                f"Fonti selezionate: {len(payload['source_ids'])}/3.",
                reply_markup=self._manual_source_markup(
                    payload["category"], payload["source_ids"], 0,
                ),
            )
            return "manual_existing_sources"

        if parts == ["manual", "sources", "add"]:
            if step != "sources":
                self._send(chat_id, "Questa sessione non attende fonti.")
                return "invalid_session"
            child = {"token": uuid4().hex, "return_step": "sources"}
            if not self._replace_manual_child_session(
                chat_id, raw, "source_child_text", payload, child,
            ):
                self._send(chat_id, "Operazione già gestita.")
                return "session_conflict"
            self._send(
                chat_id, "Invia il testo della nuova fonte (massimo 2000 caratteri).",
                reply_markup=self._callback_markup([[
                    self._callback_button("Annulla", "manual:sources:child_cancel"),
                ]]),
            )
            return "manual_child_text"

        if parts == ["manual", "sources", "none"]:
            if step != "sources":
                self._send(chat_id, "Questa sessione non attende fonti.")
                return "invalid_session"
            next_payload = self._manual_parent_payload(payload)
            next_payload["source_ids"] = []
            if not self._replace_session(
                chat_id, raw, "manual_post", "media", next_payload,
            ):
                self._send(chat_id, "Operazione già gestita.")
                return "session_conflict"
            self._send(
                chat_id,
                "Scegli un media disponibile oppure continua senza media.",
                reply_markup=self._manual_media_markup(),
            )
            return "manual_media"

        if parts == ["manual", "sources", "child_cancel"]:
            if not step.startswith("source_child_"):
                self._send(chat_id, "Questa sessione non attende una fonte.")
                return "invalid_session"
            if not self._replace_session(
                chat_id, raw, "manual_post", "sources", self._manual_parent_payload(payload),
            ):
                self._send(chat_id, "Operazione già gestita.")
                return "session_conflict"
            self._send(
                chat_id, "Aggiunta fonte annullata.", reply_markup=self._manual_source_choice_markup(),
            )
            return "manual_child_cancelled"

        if len(parts) == 4 and parts[:3] == ["manual", "sources", "page"]:
            raw_page = parts[3]
            page = int(raw_page) if raw_page.isascii() and raw_page.isdigit() else -1
            if step != "sources" or page < 0 or page > 100:
                self._send(chat_id, "Pagina fonti non valida.")
                return "invalid_callback"
            self._send(
                chat_id,
                f"Fonti selezionate: {len(payload['source_ids'])}/3.",
                reply_markup=self._manual_source_markup(
                    payload["category"], payload["source_ids"], page,
                ),
            )
            return "manual_sources_page"

        if len(parts) == 4 and parts[:3] == ["manual", "child", "source"]:
            source_type = parts[3]
            if (
                step != "source_child_classification"
                or source_type not in _SOURCE_TYPES
                or source_type not in MANUAL_SOURCE_TYPES[payload["category"]]
            ):
                self._send(chat_id, "Classificazione non valida.")
                return "invalid_callback"
            child = payload["child"]
            if source_type == "verified_news":
                if not self._replace_manual_child_session(
                    chat_id, raw, "source_child_news_url", payload, child,
                ):
                    self._send(chat_id, "Operazione già gestita.")
                    return "session_conflict"
                self._send(chat_id, "Inserisci l'URL HTTPS allowlisted dell'articolo.")
                return "manual_child_news_url"
            return self._resume_manual_after_child(
                chat_id, raw, session, source_type=source_type,
                text=child["text"], metadata=(
                    {"publishable": True} if source_type == "founder_note" else {}
                ),
            )

        if len(parts) == 3 and parts[1] == "source":
            source_id = self._positive_id(parts[2])
            if step != "sources" or source_id is None:
                self._send(chat_id, "Fonte non valida.")
                return "invalid_callback"
            selected = list(payload["source_ids"])
            if source_id not in selected:
                if len(selected) >= 3:
                    self._send(chat_id, "Puoi selezionare al massimo tre fonti.")
                    return "manual_source_limit"
                try:
                    eligible = self.db.get_eligible_content_sources(
                        [source_id], now=self._now(),
                    )
                except Exception:
                    eligible = []
                if (
                    len(eligible) != 1
                    or eligible[0].get("id") != source_id
                    or eligible[0].get("source_type")
                    not in MANUAL_SOURCE_TYPES[payload["category"]]
                ):
                    self._send(chat_id, "Fonte non disponibile per questa categoria.")
                    return "manual_source_rejected"
                selected.append(source_id)
            next_payload = dict(payload)
            next_payload["source_ids"] = selected
            if not self._replace_session(
                chat_id, raw, "manual_post", "sources", next_payload,
            ):
                self._send(chat_id, "Operazione già gestita.")
                return "session_conflict"
            self._send(
                chat_id,
                f"Fonti selezionate: {len(selected)}/3.",
                reply_markup=self._manual_source_markup(
                    payload["category"], selected,
                ),
            )
            return "manual_source_selected"

        if parts == ["manual", "sources_done"]:
            if step != "sources":
                self._send(chat_id, "Questa sessione non attende fonti.")
                return "invalid_session"
            if not self._replace_session(
                chat_id, raw, "manual_post", "media", dict(payload),
            ):
                self._send(chat_id, "Operazione già gestita.")
                return "session_conflict"
            self._send(
                chat_id,
                "Scegli un media disponibile oppure continua senza media.",
                reply_markup=self._manual_media_markup(),
            )
            return "manual_media"

        if parts == ["manual", "media", "browse"]:
            if step != "media":
                self._send(chat_id, "Questa sessione non attende un media.")
                return "invalid_session"
            try:
                self.media_browser.show(chat_id=chat_id, media_id=None, context="manual")
            except Exception:
                self._send(chat_id, "Media non disponibile.")
                return "media_unavailable"
            return "manual_media_browser"

        if len(parts) == 3 and parts[1] == "media":
            if step != "media":
                self._send(chat_id, "Questa sessione non attende un media.")
                return "invalid_session"
            media_id = None if parts[2] == "none" else self._positive_id(parts[2])
            if parts[2] != "none" and media_id is None:
                self._send(chat_id, "Media non valido.")
                return "invalid_callback"
            if media_id is not None:
                try:
                    media = self.db.get_media_by_id(media_id)
                except Exception:
                    media = None
                if (
                    not isinstance(media, dict)
                    or media.get("id") != media_id
                    or media.get("lifecycle_state") != "available"
                    or media.get("file_deleted") != 0
                ):
                    self._send(chat_id, "Media non più disponibile.")
                    return "manual_media_rejected"
            next_payload = dict(payload)
            next_payload["media_id"] = media_id
            return self._finish_manual_post(
                chat_id,
                raw,
                {**session, "payload": next_payload},
            )

        self._send(chat_id, "Azione manuale non valida.")
        return "invalid_callback"

    def _growth_callback(self, chat_id: str, parts):
        if len(parts) == 3 and parts[1] in {"save", "followed", "discard"}:
            candidate_id = self._positive_id(parts[2])
            if candidate_id is None:
                self._send(chat_id, "Candidato non valido.")
                return "invalid_callback"
            if parts[1] == "discard":
                rows = [[
                    self._callback_button(
                        label, f"growth:reason:{candidate_id}:{reason}",
                    )
                ] for reason, label in _GROWTH_REASONS.items()]
                self._send(
                    chat_id,
                    "Perché vuoi scartarlo?",
                    reply_markup=self._callback_markup(rows),
                )
                return "growth_discard_reason"
            decision = "saved" if parts[1] == "save" else "followed_manually"
            if not self.db.mark_candidate_decision(candidate_id, decision):
                self._send(chat_id, "Decisione già registrata o non valida.")
                return "growth_decision_rejected"
            message = "Candidato salvato." if decision == "saved" else (
                "Azione manuale registrata; nessuna azione è stata inviata a X."
            )
            self._send(chat_id, message)
            return decision
        if len(parts) == 4 and parts[1] == "reason":
            candidate_id = self._positive_id(parts[2])
            reason = parts[3]
            if candidate_id is None or reason not in _GROWTH_REASONS:
                self._send(chat_id, "Motivo non valido.")
                return "invalid_callback"
            if not self.db.mark_candidate_decision(candidate_id, "discarded", reason):
                self._send(chat_id, "Decisione già registrata o non valida.")
                return "growth_decision_rejected"
            self._send(chat_id, "Candidato scartato per 30 giorni.")
            return "growth_discarded"
        self._send(chat_id, "Azione growth non valida.")
        return "invalid_callback"

    def _ingest_media(self, chat_id: str, message: Dict[str, Any]):
        if self.media_processor is None:
            self._send(chat_id, "Upload media non disponibile.")
            return "media_unavailable"
        caption = message.get("caption", "")
        if (
            not isinstance(caption, str)
            or len(caption) > TELEGRAM_CAPTION_MAX_CHARS
        ):
            self._send(chat_id, "Upload non valido.")
            return "invalid_media"
        try:
            metadata = telegram_media_metadata(message)
        except TelegramApiError:
            self._send(chat_id, "Upload non valido o non supportato.")
            return "invalid_media"

        downloaded = None
        try:
            remote = self.telegram_api.get_file(metadata["file_id"])
            if not self._get_file_matches(remote, metadata):
                self._send(chat_id, "File Telegram non valido.")
                return "invalid_media"
            root = getattr(self.telegram_api, "media_library_dir", None)
            if root is None:
                self._send(chat_id, "Archivio media non disponibile.")
                return "media_unavailable"
            root = Path(os.path.abspath(os.fspath(root)))
            if not root.is_absolute() or not root.is_dir():
                self._send(chat_id, "Archivio media non disponibile.")
                return "media_unavailable"
            suffix = Path(metadata["message_filename"]).suffix.lower()
            destination = root / f".telegram-download-{uuid4().hex}{suffix}"
            downloaded = destination
            returned_path = self.telegram_api.download_file(
                remote["file_path"],
                destination,
                message_filename=metadata["message_filename"],
                mime_type=metadata["mime_type"],
                expected_size=metadata["expected_size"],
            )
            if Path(returned_path) != destination:
                raise ValueError("unexpected_download_destination")
            record = self.media_processor.process_new_file(
                str(downloaded),
                metadata["message_filename"],
                metadata["mime_type"],
                metadata["expected_size"],
                caption.strip(),
            )
        except Exception:
            self._cleanup_download(downloaded)
            self._send(chat_id, "Upload non riuscito. Riprova.")
            return "media_failed"
        finally:
            self._cleanup_download(downloaded)

        if not isinstance(record, dict) or type(record.get("id")) is not int:
            self._send(chat_id, "Upload non riuscito. Riprova.")
            return "media_failed"
        state = self._clean_text(
            record.get("lifecycle_state") or record.get("state"), 80,
        ) or "available"
        description = self._clean_text(record.get("ai_description"), 500) or "n/d"
        tags = self._clean_text(record.get("ai_tags"), 500) or "n/d"
        context = self._clean_text(record.get("user_context"), 500) or "n/d"
        self._send(chat_id, "\n".join([
            f"Libreria #{record['id']}",
            f"stato: {state}",
            f"descrizione: {description}",
            f"tag: {tags}",
            f"contesto: {context}",
        ]))
        return "media_saved"

    @staticmethod
    def _get_file_matches(remote: Any, metadata: Dict[str, Any]) -> bool:
        if not isinstance(remote, dict) or not isinstance(remote.get("file_path"), str):
            return False
        file_path = remote["file_path"]
        if not file_path or len(file_path) > 4096:
            return False
        file_id = remote.get("file_id")
        if file_id is not None and file_id != metadata["file_id"]:
            return False
        file_size = remote.get("file_size")
        if file_size is not None and file_size != metadata["expected_size"]:
            return False
        unique_id = remote.get("file_unique_id")
        if unique_id is not None:
            if (
                not isinstance(unique_id, str)
                or not unique_id
                or len(unique_id) > 4096
                or any(ord(character) < 32 for character in unique_id)
            ):
                return False
            try:
                digest = hashlib.sha256(unique_id.encode("utf-8")).hexdigest()
            except UnicodeError:
                return False
            if not Path(metadata["message_filename"]).stem.endswith("-" + digest):
                return False
        return True

    @staticmethod
    def _cleanup_download(path: Any) -> None:
        if path is None:
            return
        try:
            candidate = Path(path)
            if candidate.is_symlink():
                candidate.unlink()
            elif candidate.exists() and candidate.is_file():
                candidate.unlink()
        except (OSError, TypeError, ValueError):
            pass

    def _notify_failure(self, context: str, error: Exception) -> None:
        try:
            self.notifier.notify_error(context, error)
        except Exception:
            pass

    def _complete_failed_update(self, update_id: int, error: Exception) -> str:
        try:
            self.db.complete_telegram_update(
                update_id,
                "failed",
                {"error": sanitize_error(error)},
            )
        except Exception as persistence_error:
            self._notify_failure("telegram_update_state", persistence_error)
            return "failed"
        self._notify_failure("telegram_update", error)
        return "failed"

    def _complete_local_state(self, update_id: int, state: str) -> str:
        try:
            self.db.complete_telegram_update(update_id, state, {})
            return state
        except Exception:
            return "failed"

    def process_update(self, update: Dict[str, Any], stop_event=None) -> str:
        result, _acknowledged = self._process_update(update, stop_event)
        return result

    def _process_update(self, update: Dict[str, Any], stop_event=None):
        """Return the public result and whether this update is durably claimed."""
        if self._stopped(stop_event) or not isinstance(update, dict):
            result = "stopped" if self._stopped(stop_event) else "malformed"
            return result, False

        update_id = update.get("update_id")
        if not self._valid_update_id(update_id):
            return "malformed", False

        subtype, payload = self._supported_subtype(update)
        chat_id = self._chat_id(subtype, payload) if subtype is not None else None
        if self._stopped(stop_event):
            return "stopped", False
        try:
            claimed = self.db.claim_telegram_update(update_id, str(chat_id))
        except Exception:
            return "failed", False
        if claimed is False:
            return "duplicate", True
        if claimed is not True:
            return "failed", False
        if self._stopped(stop_event):
            return self._complete_local_state(update_id, "stopped"), True
        if subtype is None:
            return self._complete_local_state(update_id, "malformed"), True
        if str(chat_id) != self.authorized_chat_id:
            return self._complete_local_state(update_id, "unauthorized"), True
        if subtype == "callback_query" and (
            not isinstance(payload.get("id"), str)
            or _SAFE_CALLBACK_ID.fullmatch(payload["id"]) is None
        ):
            return self._complete_local_state(update_id, "malformed"), True
        if self._stopped(stop_event):
            return self._complete_local_state(update_id, "stopped"), True

        callback_id = payload["id"] if subtype == "callback_query" else None
        try:
            result = self._dispatch(update)
        except Exception as exc:
            if callback_id is not None:
                try:
                    self.telegram_api.answer_callback(
                        callback_id,
                        text="Operazione non riuscita.",
                    )
                except Exception:
                    pass
            return self._complete_failed_update(update_id, exc), True

        if callback_id is not None:
            try:
                self.telegram_api.answer_callback(callback_id)
            except Exception as exc:
                return self._complete_failed_update(update_id, exc), True

        try:
            self.db.complete_telegram_update(
                update_id,
                "processed",
                {"result": result},
            )
            return "processed", True
        except Exception as exc:
            self._notify_failure("telegram_update_state", exc)
            return "failed", True

    def run_forever(self, stop_event) -> None:
        """Long-poll until stopped, preserving batch offsets and bounded waits."""
        offset = None
        failure_index = 0
        while not stop_event.is_set():
            try:
                updates = self.telegram_api.get_updates(
                    offset=offset,
                    timeout=self.poll_timeout,
                )
            except TelegramApiError:
                delay = _TRANSPORT_BACKOFF_SECONDS[
                    min(failure_index, len(_TRANSPORT_BACKOFF_SECONDS) - 1)
                ]
                failure_index += 1
                if stop_event.wait(delay):
                    return
                continue

            if stop_event.is_set():
                return
            if not isinstance(updates, list):
                delay = _TRANSPORT_BACKOFF_SECONDS[
                    min(failure_index, len(_TRANSPORT_BACKOFF_SECONDS) - 1)
                ]
                failure_index += 1
                if stop_event.wait(delay):
                    return
                continue
            if not updates:
                failure_index = 0
                if stop_event.wait(_EMPTY_POLL_DELAY_SECONDS):
                    return
                continue

            ordered_updates = sorted(
                (
                    update for update in updates
                    if isinstance(update, dict)
                    and self._valid_update_id(update.get("update_id"))
                ),
                key=lambda update: update["update_id"],
            )
            last_acknowledged_id = None
            retryable_failure = False
            for update in ordered_updates:
                if stop_event.is_set():
                    return
                update_id = update.get("update_id")
                try:
                    result, acknowledged = self._process_update(
                        update, stop_event=stop_event,
                    )
                except Exception:
                    result, acknowledged = "failed", False
                if result == "stopped":
                    return
                if not acknowledged:
                    retryable_failure = True
                    break
                last_acknowledged_id = update_id
            if last_acknowledged_id is not None:
                next_offset = last_acknowledged_id + 1
                offset = next_offset if offset is None else max(offset, next_offset)
            if ordered_updates and not retryable_failure:
                failure_index = 0
            else:
                delay = _TRANSPORT_BACKOFF_SECONDS[
                    min(failure_index, len(_TRANSPORT_BACKOFF_SECONDS) - 1)
                ]
                failure_index += 1
                if stop_event.wait(delay):
                    return
