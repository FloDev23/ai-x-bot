"""
Crescita rete (punto nuovo, richiesto dopo aver notato 0 visualizzazioni):
l'opportunity detector cerca CLIENTI POTENZIALI (persone con un problema
commerciale specifico) — questo modulo invece cerca e segue PERSONE REALI
interessate al mondo fitness/palestre, per costruire una rete che dia
visibilità organica ai post. Sono due obiettivi diversi e complementari.

Regole di sicurezza (X sospende chi segue in modo aggressivo/robotico):
- Massimo GROWTH_FOLLOW_PER_DAY follow al giorno (default prudente: 8)
- Una pausa tra un follow e l'altro, mai a raffica
- Mai account mega/verificati con troppi follower (non ricambieranno mai)
- Unfollow automatico di chi non ricambia entro GROWTH_UNFOLLOW_AFTER_DAYS,
  per mantenere un rapporto follow/follower sano
"""
import logging
import random
import time
from typing import List, Optional

logger = logging.getLogger(__name__)


class GrowthManager:
    def __init__(self, twitter_client, db):
        self.client = twitter_client
        self.db = db

    def run_daily_follow_cycle(self, hashtags: List[str], per_day: int = 8,
                                follower_min: int = 300, follower_max: int = 20000,
                                delay_seconds: int = 45) -> List[dict]:
        """
        Cerca contenuti reali (non "problem keywords") sugli hashtag/topic
        fitness dati, e segue un piccolo numero di account genuini e in
        target (per numero di follower, non mega-brand). Ritorna la lista
        di chi è stato seguito in questo ciclo (per la notifica Telegram).
        """
        already_today = self.db.count_growth_follows_today()
        remaining = max(0, per_day - already_today)
        if remaining == 0:
            logger.info(f"ℹ️ Tetto giornaliero di follow ({per_day}) già raggiunto oggi, salto il ciclo")
            return []

        followed = []
        seen_usernames = set()
        random.shuffle(hashtags)

        for tag in hashtags:
            if len(followed) >= remaining:
                break
            try:
                tweets = self.client.search_tweets(tag, limit=10)
            except Exception as e:
                logger.warning(f"⚠️ Ricerca fallita per '{tag}': {e}")
                continue

            for tweet in tweets:
                if len(followed) >= remaining:
                    break

                username = tweet.get('author_username', '')
                if not username or username in seen_usernames:
                    continue
                seen_usernames.add(username)

                info = self.client.get_user_info(username)
                if not info:
                    continue

                # Filtri di sicurezza/qualità: no mega-account, no account
                # già seguiti in un ciclo precedente, no verificati (spesso
                # brand/celebrity, non ricambiano mai un piccolo account)
                followers = info.get('followers_count', 0)
                if not (follower_min <= followers <= follower_max):
                    continue
                if info.get('verified'):
                    continue
                if self.db.already_growth_followed(info['id']):
                    continue

                success = self.client.follow_user(info['id'])
                if success:
                    self.db.add_growth_follow(username, info['id'])
                    followed.append({'username': username, 'followers': followers})
                    logger.info(f"➕ Growth follow: @{username} ({followers} follower)")
                    time.sleep(delay_seconds)  # mai a raffica

        logger.info(f"✅ Ciclo crescita rete completato: {len(followed)} nuovi follow")
        return followed

    def run_unfollow_check(self, days_old: int = 21) -> List[dict]:
        """
        Controlla chi, seguito da almeno `days_old` giorni, non ha ancora
        ricambiato il follow, e lo rimuove per mantenere un buon rapporto
        follow/follower. Un'unica chiamata per leggere i follower del bot,
        non una per ogni account da controllare.
        """
        candidates = self.db.get_growth_follows_pending_check(days_old=days_old)
        if not candidates:
            return []

        follower_ids = self.client.get_follower_ids()
        unfollowed = []

        for candidate in candidates:
            if candidate['user_id'] in follower_ids:
                self.db.mark_growth_followed_back(candidate['id'])
                continue

            success = self.client.unfollow_user(candidate['user_id'])
            if success:
                self.db.mark_growth_unfollowed(candidate['id'])
                unfollowed.append({'username': candidate['username']})
                logger.info(f"➖ Unfollow (non ricambiato dopo {days_old}gg): @{candidate['username']}")
                time.sleep(10)

        logger.info(f"✅ Controllo unfollow completato: {len(unfollowed)} rimossi")
        return unfollowed
