"""
Telegram Notifier - notifiche in tempo reale su Telegram per:

- Nuovi lead commerciali trovati dall'opportunity detector (punto 19):
  punteggio, azione suggerita, link al tweet, link al profilo autore e,
  quando l'azione lo prevede (Commenta/Commenta+DM/DM), una bozza di
  testo già pronta da copiare.
- Riepilogo del ciclo di engagement mirato (punti 7-8-9): per ogni account
  target, quale azione è stata eseguita (Like / Like+Follow / Retweet con
  commento), con link diretto al tweet/profilo.
- Errori nei cicli del bot (post, engagement, opportunity, performance),
  per accorgersi subito se qualcosa si rompe senza dover controllare i log
  via SSH.

Setup (vedi SETUP.md):
1. Scrivi a @BotFather su Telegram -> /newbot -> copia il token.
2. Scrivi un qualsiasi messaggio al tuo nuovo bot (obbligatorio prima del
   passo successivo, altrimenti getUpdates resta vuoto).
3. Apri nel browser:
   https://api.telegram.org/bot<IL_TUO_TOKEN>/getUpdates
   e leggi il campo "chat":{"id": ...} -> è il tuo TELEGRAM_CHAT_ID.
4. Aggiungi nel .env:
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=...

Se le variabili non sono configurate, il notifier resta silenziosamente
disabilitato (self.enabled = False) e il bot continua a funzionare
normalmente: nessun crash per Telegram assente.
"""
import logging
import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)
        if not self.enabled:
            logger.info(
                "ℹ️ Telegram non configurato (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID "
                "mancanti in .env): notifiche disattivate."
            )

    def _send(self, text: str):
        if not self.enabled:
            return
        try:
            url = TELEGRAM_API_URL.format(token=self.bot_token)
            resp = requests.post(
                url,
                data={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                },
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(f"⚠️ Telegram ha risposto {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            # Una notifica Telegram fallita non deve mai bloccare il ciclo del bot
            logger.warning(f"⚠️ Errore invio notifica Telegram: {e}")

    @staticmethod
    def _escape(text: str) -> str:
        """Escape minimo per HTML parse_mode di Telegram"""
        return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def notify_lead(self, lead: dict, suggested_text: str = None):
        """
        lead: dict con tweet_id, text, score, action, keyword, author_username
        suggested_text: bozza di commento o DM pronta da copiare (se l'azione
        suggerita è Commenta / Commenta+DM / DM)
        """
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
            lines.append(f"💬 <b>Bozza pronta da copiare:</b>\n{self._escape(suggested_text)}")

        self._send("\n".join(lines))

    def notify_engagement_summary(self, actions: list):
        """
        actions: lista di dict {username, action, tweet_id}
        Una sola azione per riga, con link diretto al tweet interessato.
        """
        real_actions = [a for a in actions if a.get("action") and a["action"] != "Ignora"]
        if not real_actions:
            return

        lines = ["💬 <b>Ciclo engagement mirato completato</b>", ""]
        for a in real_actions:
            username = a.get("username", "")
            tweet_id = a.get("tweet_id", "")
            url = (
                f"https://x.com/{username}/status/{tweet_id}"
                if username and tweet_id
                else f"https://x.com/{username}"
            )
            lines.append(f"• @{username} → <b>{self._escape(a.get('action', ''))}</b> — {url}")

        self._send("\n".join(lines))

    def notify_error(self, context: str, error: Exception):
        text = f"🚨 <b>Errore bot</b> ({self._escape(context)})\n\n{self._escape(str(error))[:500]}"
        self._send(text)
