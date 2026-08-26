"""Pure, compact and path-free presentation for Telegram post browsing."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Iterable
from zoneinfo import ZoneInfo


class PostBrowser:
    """Render compact index controls; the controller owns detail and preview I/O."""

    view_kind = "post_browser"

    @staticmethod
    def _safe(value: Any, limit: int = 100) -> str:
        if not isinstance(value, str):
            return "n/d"
        value = " ".join(value.split())
        value = re.sub(r"[<>]", "", value)
        return value[:limit].strip() or "n/d"

    @classmethod
    def excerpt(cls, value: Any) -> str:
        text = cls._safe(value, 100)
        if len(text) < 70:
            return text
        return text[:99].rstrip() + ("…" if len(text) == 100 else "")

    @staticmethod
    def _label(row: Dict) -> str:
        plan_status = row.get("plan_status")
        plan_labels = {
            "planned": "Pianificato",
            "publishing": "In pubblicazione",
            "unknown": "Attenzione",
            "simulated": "Simulato",
        }
        if plan_status in plan_labels:
            return plan_labels[plan_status]
        status = str(row.get("status") or "sconosciuto")
        labels = {
            "pending_approval": "Da approvare",
            "approved": "Approvato",
            "publishing": "In pubblicazione",
            "published": "Pubblicato",
            "discarded": "Rimosso",
        }
        return labels.get(status, "Attenzione")

    @staticmethod
    def _button(text: str, data: str) -> Dict[str, str]:
        if not 1 <= len(data.encode("utf-8")) <= 64:
            raise ValueError("invalid callback data")
        return {"text": text, "callback_data": data}

    def render_index(
        self, rows: Iterable[Dict], *, token: str, has_next: bool, has_previous: bool,
        include_discarded: bool = False, discarded_token: str | None = None,
    ) -> Dict[str, Any]:
        buttons = []
        for row in list(rows)[:8]:
            draft_id, revision = row.get("id"), row.get("revision")
            if type(draft_id) is not int or type(revision) is not int:
                continue
            timing = self.compact_time(row.get("scheduled_for"))
            timing_label = f" · {timing}" if timing else ""
            text = (
                f"#{draft_id} · {self._label(row)}{timing_label} · "
                f"{self.excerpt(row.get('text'))}"
            )
            buttons.append([self._button(text[:120], f"post:{token}:{draft_id}:{revision}")])
        navigation = []
        if has_previous:
            navigation.append(self._button("Precedente", f"posts:{token}:prev"))
        if has_next:
            navigation.append(self._button("Successivo", f"posts:{token}:next"))
        if navigation:
            buttons.append(navigation)
        buttons.append([self._button("Aggiorna", f"posts:{token}:refresh")])
        if not include_discarded and discarded_token is not None:
            buttons.append([
                self._button("Mostra rimossi", f"posts:{discarded_token}:refresh")
            ])
        return {"reply_markup": {"inline_keyboard": buttons}}

    def summary(self, rows: Iterable[Dict], *, include_discarded: bool) -> str:
        count = len(list(rows))
        suffix = " · inclusi rimossi" if include_discarded else ""
        return f"Post — {count} risultati{suffix}\nSeleziona una bozza per i dettagli."

    @staticmethod
    def compact_time(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        try:
            point = datetime.fromisoformat(value.replace("Z", "+00:00"))
            et = point.astimezone(ZoneInfo("America/New_York"))
            rome = point.astimezone(ZoneInfo("Europe/Rome"))
            return f"{et.strftime('%d/%m %H:%M ET')} / {rome.strftime('%H:%M Roma')}"
        except ValueError:
            return ""
