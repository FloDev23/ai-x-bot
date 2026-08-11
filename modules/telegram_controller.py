"""Authorized, idempotent dispatch boundary for Telegram updates."""

from typing import Any, Callable, Dict, Optional

from modules.telegram_api import TELEGRAM_POLL_TIMEOUT, TelegramApiError


_TRANSPORT_BACKOFF_SECONDS = (1, 2, 4, 8, 30)
_EMPTY_POLL_DELAY_SECONDS = 0.1


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
    def _chat_id(update: Dict[str, Any]):
        message = update.get("message")
        if isinstance(message, dict):
            chat = message.get("chat")
            if isinstance(chat, dict):
                return chat.get("id")

        callback = update.get("callback_query")
        if isinstance(callback, dict):
            callback_message = callback.get("message")
            if isinstance(callback_message, dict):
                chat = callback_message.get("chat")
                if isinstance(chat, dict):
                    return chat.get("id")
        return None

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
                {"error": type(error).__name__},
            )
        except Exception as persistence_error:
            self._notify_failure("telegram_update_state", persistence_error)
            return "failed"
        self._notify_failure("telegram_update", error)
        return "failed"

    def process_update(self, update: Dict[str, Any]) -> str:
        update_id = int(update["update_id"])
        chat_id = self._chat_id(update)
        if not self.db.claim_telegram_update(update_id, str(chat_id)):
            return "duplicate"
        if str(chat_id) != self.authorized_chat_id:
            try:
                self.db.complete_telegram_update(update_id, "unauthorized", {})
                return "unauthorized"
            except Exception as exc:
                self._notify_failure("telegram_update_state", exc)
                return "failed"

        callback = update.get("callback_query")
        callback_id = (
            str(callback["id"])
            if isinstance(callback, dict) and callback.get("id") is not None
            else None
        )
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

            failure_index = 0
            if stop_event.is_set():
                return
            if not updates:
                if stop_event.wait(_EMPTY_POLL_DELAY_SECONDS):
                    return
                continue

            update_ids = []
            for update in updates:
                if not isinstance(update, dict):
                    continue
                try:
                    update_id = int(update["update_id"])
                except (KeyError, TypeError, ValueError):
                    continue
                self.process_update(update)
                update_ids.append(update_id)
            if update_ids:
                offset = max(update_ids) + 1
            elif stop_event.wait(_EMPTY_POLL_DELAY_SECONDS):
                return
