"""Outbound Telegram notifications through the shared safe transport."""

import logging

from modules.telegram_api import TelegramApi, safe_exception_class, sanitize_error


logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        database=None,
        telegram_api=None,
    ):
        self.bot_token = str(bot_token)
        self.chat_id = str(chat_id)
        self.database = database
        self.enabled = bool(bot_token and chat_id)
        self.telegram_api = telegram_api
        if self.enabled and self.telegram_api is None:
            self.telegram_api = TelegramApi(self.bot_token)
        if not self.enabled:
            logger.info(
                "Telegram non configurato (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID "
                "mancanti): notifiche disattivate."
            )

    def _send(self, text: str):
        if not self.enabled:
            return None
        try:
            return self.telegram_api.send_message(
                self.chat_id,
                text,
                parse_mode="HTML",
                disable_web_page_preview=False,
            )
        except Exception as exc:
            logger.warning(
                "Telegram notification delivery failed: %s",
                sanitize_error(exc, operation="notification_delivery"),
            )
            return None

    @staticmethod
    def _escape(text: str) -> str:
        """Escape minimal Telegram HTML markup."""
        return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def notify_lead(self, lead: dict, suggested_text: str = None):
        """Send one read-only lead suggestion when optional discovery is enabled."""
        username = lead.get("author_username", "")
        tweet_id = lead.get("tweet_id", "")
        tweet_url = (
            f"https://x.com/{username}/status/{tweet_id}"
            if username and tweet_id
            else f"https://x.com/i/status/{tweet_id}"
        )
        profile_url = f"https://x.com/{username}" if username else None

        lines = [
            f"🎯 <b>Nuovo lead</b> (score {lead.get('score', 0)}/100)",
            f"Keyword: <i>{self._escape(lead.get('keyword', ''))}</i>",
            f"Azione suggerita: <b>{self._escape(lead.get('action', ''))}</b>",
            "",
            f"📝 {self._escape(lead.get('text', ''))[:300]}",
            "",
            f"🔗 Tweet: {tweet_url}",
        ]
        if profile_url:
            lines.append(f"👤 Profilo: {profile_url}")
        if suggested_text:
            lines.append("")
            lines.append(
                "💬 <b>Bozza pronta da copiare:</b>\n"
                f"{self._escape(suggested_text)}"
            )

        self._send("\n".join(lines))

    def notify_error(self, context: str, error: Exception):
        """Persist one sanitized event, then attempt one sanitized notification."""
        safe_context = sanitize_error(None, operation=context)
        operation = context if safe_context else "telegram_error"
        safe_context = sanitize_error(None, operation=operation)
        safe_message = sanitize_error(error, operation=operation)
        error_type = safe_exception_class(error)
        if self.database is not None:
            try:
                self.database.log_error(safe_context, error_type, safe_message)
            except Exception as exc:
                logger.warning(
                    "Telegram error persistence failed: %s",
                    sanitize_error(exc, operation="error_persistence"),
                )

        text = (
            f"🚨 <b>Errore bot</b> ({self._escape(safe_context)})\n\n"
            f"{self._escape(safe_message)[:500]}"
        )
        self._send(text)
