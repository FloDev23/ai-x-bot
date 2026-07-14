"""
Content Scheduler - Punti 10 e 11 dell'analisi
Definisce cosa pubblicare in base al giorno della settimana, al periodo
dell'anno e agli eventi fitness/startup in corso, e combina questo con
i pesi di performance calcolati da modules/analytics.py (auto-learning,
punto 2).

Nessuna chiamata esterna: modulo a costo zero.
"""
import random
import logging
from datetime import datetime, date
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Palinsesto settimanale (punto 11 dei tuoi appunti + "12. Persona diversa")
# Ogni giorno ha una o più categorie possibili, il peso iniziale verrà
# poi corretto dai dati reali di performance (modules/analytics.py)
WEEKLY_SCHEDULE: Dict[int, List[str]] = {
    0: ['business_palestra', 'trend_fitness'],       # Lunedì
    1: ['trend_fitness', 'consiglio_pratico'],       # Martedì
    2: ['behind_the_scenes', 'human_mode'],          # Mercoledì
    3: ['consiglio_pratico', 'business_palestra'],   # Giovedì
    4: ['trasparenza', 'human_mode'],                # Venerdì
    5: ['behind_the_scenes', 'community'],           # Sabato
    6: ['trend_fitness', 'community'],               # Domenica
}

# Solo queste categorie possono contenere link/CTA diretta verso l'app
# (punto 9: "mai parlare di FlexDropin più di 2 volte al giorno")
PROMO_CATEGORIES = {'business_palestra', 'consiglio_pratico'}

# Calendario stagionale/marketing (punto 11)
SEASONAL_CONTEXT = {
    1: "Gennaio: periodo di buoni propositi e nuovi abbonamenti in palestra",
    6: "Estate: stagione di outdoor training, meno frequentazione indoor",
    7: "Estate: stagione di outdoor training, meno frequentazione indoor",
    8: "Estate: stagione di outdoor training, meno frequentazione indoor",
    9: "Settembre: rientro dalle vacanze, ripartenza abbonamenti palestre",
    11: "Novembre: avvicinamento al Black Friday, promozioni",
    12: "Dicembre: Natale, propositi per il nuovo anno in arrivo",
}

# Eventi fitness/startup rilevanti (punto 10). Date indicative annuali:
# vanno aggiornate ogni anno con le date ufficiali reali.
EVENTS_CALENDAR = [
    {"name": "CrossFit Games", "month": 8, "day": 1, "window_days": 14},
    {"name": "Rimini Wellness", "month": 5, "day": 28, "window_days": 10},
    {"name": "FIBO", "month": 4, "day": 9, "window_days": 10},
    {"name": "Hyrox", "month": 10, "day": 1, "window_days": 20},
    {"name": "Maratona di New York", "month": 11, "day": 1, "window_days": 14},
]


def get_categories_for_today(today: Optional[date] = None) -> List[str]:
    """Ritorna le categorie previste dal palinsesto per il giorno corrente"""
    today = today or datetime.now().date()
    return WEEKLY_SCHEDULE.get(today.weekday(), ['trend_fitness'])


def get_seasonal_context(today: Optional[date] = None) -> Optional[str]:
    today = today or datetime.now().date()
    return SEASONAL_CONTEXT.get(today.month)


def get_active_events(today: Optional[date] = None) -> List[str]:
    """Ritorna gli eventi il cui 'periodo caldo' comprende oggi"""
    today = today or datetime.now().date()
    active = []
    for ev in EVENTS_CALENDAR:
        # Confronto approssimato ignorando l'anno esatto dell'evento
        try:
            event_date = date(today.year, ev['month'], ev['day'])
        except ValueError:
            continue
        delta_days = abs((today - event_date).days)
        if delta_days <= ev['window_days']:
            active.append(ev['name'])
    return active


def pick_category(category_weights: Optional[Dict[str, float]] = None,
                   avoid_categories: Optional[List[str]] = None) -> str:
    """
    Sceglie la categoria da pubblicare oggi combinando:
    - palinsesto settimanale (punto 11)
    - pesi di performance da auto-learning (punto 2), se disponibili
    - categorie da evitare perché già pubblicate di recente (punto 1, anti-ripetizione)
    """
    candidates = get_categories_for_today()
    avoid_categories = avoid_categories or []
    filtered = [c for c in candidates if c not in avoid_categories] or candidates

    if not category_weights:
        return random.choice(filtered)

    weights = [max(category_weights.get(c, 1.0), 0.1) for c in filtered]
    return random.choices(filtered, weights=weights, k=1)[0]


def should_include_link(category: str, links_posted_last_7_days: int,
                         max_links_per_week: int, last_post_had_link: bool) -> bool:
    """
    Regola di anti-spam economica (punto 9 + il vincolo di costo X API:
    un post con link costa $0.20 contro $0.015 senza link).
    Include link solo se:
    - la categoria è promozionale
    - non abbiamo superato il tetto settimanale di link
    - l'ultimo post non aveva già un link (mai due link consecutivi)
    """
    if category not in PROMO_CATEGORIES:
        return False
    if links_posted_last_7_days >= max_links_per_week:
        return False
    if last_post_had_link:
        return False
    return True
