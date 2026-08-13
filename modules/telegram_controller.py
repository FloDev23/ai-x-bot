"""Authorized, idempotent dispatch boundary for Telegram updates."""

from typing import Any, Callable, Dict, Optional

from modules.telegram_api import TELEGRAM_POLL_TIMEOUT, TelegramApiError, sanitize_error


_TRANSPORT_BACKOFF_SECONDS = (1, 2, 4, 8, 30)
_EMPTY_POLL_DELAY_SECONDS = 0.1
_SQLITE_INTEGER_MAX = (1 << 63) - 1
_SUPPORTED_SUBTYPES = ("message", "callback_query")


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
    ):
        self.telegram_api = telegram_api
        self.db = db
        self.notifier = notifier
        self.authorized_chat_id = str(authorized_chat_id)
        self.dispatcher = dispatcher
        self.poll_timeout = int(poll_timeout)

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
        if self.dispatcher is None:
            return "ignored"
        return self.dispatcher(update)

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
        if self._stopped(stop_event) or not isinstance(update, dict):
            return "stopped" if self._stopped(stop_event) else "malformed"

        update_id = update.get("update_id")
        if not self._valid_update_id(update_id):
            return "malformed"

        subtype, payload = self._supported_subtype(update)
        chat_id = self._chat_id(subtype, payload) if subtype is not None else None
        if self._stopped(stop_event):
            return "stopped"
        try:
            claimed = self.db.claim_telegram_update(update_id, str(chat_id))
        except Exception:
            return "failed"
        if claimed is False:
            return "duplicate"
        if claimed is not True:
            return "failed"
        if self._stopped(stop_event):
            return self._complete_local_state(update_id, "stopped")
        if subtype is None:
            return self._complete_local_state(update_id, "malformed")
        if str(chat_id) != self.authorized_chat_id:
            return self._complete_local_state(update_id, "unauthorized")
        if subtype == "callback_query" and (
            not isinstance(payload.get("id"), str) or not payload["id"]
        ):
            return self._complete_local_state(update_id, "malformed")
        if self._stopped(stop_event):
            return self._complete_local_state(update_id, "stopped")

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
            return self._complete_failed_update(update_id, exc)

        if callback_id is not None:
            try:
                self.telegram_api.answer_callback(callback_id)
            except Exception as exc:
                return self._complete_failed_update(update_id, exc)

        try:
            self.db.complete_telegram_update(
                update_id,
                "processed",
                {"result": result},
            )
            return "processed"
        except Exception as exc:
            self._notify_failure("telegram_update_state", exc)
            return "failed"

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

            update_ids = []
            for update in updates:
                if stop_event.is_set():
                    return
                if not isinstance(update, dict):
                    continue
                update_id = update.get("update_id")
                if not self._valid_update_id(update_id):
                    continue
                try:
                    result = self.process_update(update, stop_event=stop_event)
                except Exception:
                    result = "failed"
                if result == "stopped":
                    return
                update_ids.append(update_id)
            if update_ids:
                offset = max(update_ids) + 1
                failure_index = 0
            else:
                delay = _TRANSPORT_BACKOFF_SECONDS[
                    min(failure_index, len(_TRANSPORT_BACKOFF_SECONDS) - 1)
                ]
                failure_index += 1
                if stop_event.wait(delay):
                    return
