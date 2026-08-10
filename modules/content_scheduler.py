"""Seasonal context helpers for enriching source-backed editorial plans."""
from datetime import date, datetime
from typing import List, Optional


# Compatibility export while legacy orchestration is replaced in Task 12.
PROMO_CATEGORIES = {"product_proof"}

SEASONAL_CONTEXT = {
    1: "Gennaio: periodo di buoni propositi e nuovi abbonamenti in palestra",
    6: "Estate: stagione di outdoor training, meno frequentazione indoor",
    7: "Estate: stagione di outdoor training, meno frequentazione indoor",
    8: "Estate: stagione di outdoor training, meno frequentazione indoor",
    9: "Settembre: rientro dalle vacanze, ripartenza abbonamenti palestre",
    11: "Novembre: avvicinamento al Black Friday, promozioni",
    12: "Dicembre: Natale, propositi per il nuovo anno in arrivo",
}

EVENTS_CALENDAR = [
    {"name": "CrossFit Games", "month": 8, "day": 1, "window_days": 14},
    {"name": "Rimini Wellness", "month": 5, "day": 28, "window_days": 10},
    {"name": "FIBO", "month": 4, "day": 9, "window_days": 10},
    {"name": "Hyrox", "month": 10, "day": 1, "window_days": 20},
    {"name": "Maratona di New York", "month": 11, "day": 1, "window_days": 14},
]


def get_seasonal_context(today: Optional[date] = None) -> Optional[str]:
    today = today or datetime.now().date()
    return SEASONAL_CONTEXT.get(today.month)


def get_active_events(today: Optional[date] = None) -> List[str]:
    """Return event names whose annual activity window contains ``today``."""
    today = today or datetime.now().date()
    active = []
    for event in EVENTS_CALENDAR:
        try:
            event_date = date(today.year, event["month"], event["day"])
        except ValueError:
            continue
        if abs((today - event_date).days) <= event["window_days"]:
            active.append(event["name"])
    return active
