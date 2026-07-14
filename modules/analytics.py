"""
Analytics / Auto-learning - Punto 2 dell'analisi (il modulo più importante)

IMPORTANTE (costi X API 2026): da febbraio 2026 X è passata a pay-per-use.
Le letture generiche costano ~$0.005 ciascuna, MA le "owned reads" (leggere
le metriche dei PROPRI tweet, i propri like, i propri bookmark, ecc.) sono
scese a ~$0.001: 5-10 volte più economiche. Questo modulo usa quindi
SOLO endpoint sui propri tweet, mai search generica, per tenere il costo
del ciclo di analytics quasi a zero.
"""
import logging
from typing import Dict, List
from modules.database import Database

logger = logging.getLogger(__name__)


class PerformanceAnalyzer:
    """Costruisce la classifica di performance per categoria e aggiorna i pesi"""

    def __init__(self, twitter_client, db: Database):
        self.client = twitter_client
        self.db = db

    def refresh_own_tweet_metrics(self, max_tweets: int = 20):
        """
        Legge le metriche pubbliche dei propri ultimi tweet (owned read,
        economico) e le salva nel DB. Va chiamato 1 volta al giorno.
        """
        tweet_ids = self.db.get_recent_tweet_ids(limit=max_tweets)
        if not tweet_ids:
            logger.info("ℹ️ Nessun tweet_id salvato ancora, salto refresh metriche")
            return

        try:
            metrics = self.client.get_tweet_metrics(tweet_ids)
        except Exception as e:
            logger.error(f"❌ Errore nel leggere le metriche dei propri tweet: {e}")
            return

        for tweet_id, m in metrics.items():
            self.db.save_tweet_metrics(
                tweet_id=tweet_id,
                impressions=m.get('impression_count', 0),
                likes=m.get('like_count', 0),
                retweets=m.get('retweet_count', 0),
                replies=m.get('reply_count', 0),
                bookmarks=m.get('bookmark_count', 0),
            )
        logger.info(f"✅ Metriche aggiornate per {len(metrics)} tweet")

    def recompute_category_weights(self, days: int = 30, min_weight: float = 0.3,
                                    max_weight: float = 3.0) -> Dict[str, float]:
        """
        Calcola il CTR (engagement/impression) per categoria e ne deriva un peso.
        Le categorie che performano meglio della media ottengono un peso > 1,
        quelle sotto media un peso < 1. Questo peso viene poi usato da
        content_scheduler.pick_category() per aumentare automaticamente
        i contenuti che funzionano (auto-ottimizzazione, come richiesto).
        """
        perf = self.db.get_category_performance(days=days)
        if not perf:
            logger.info("ℹ️ Nessun dato di performance ancora disponibile")
            return {}

        ctrs = {}
        for cat, data in perf.items():
            impressions = max(data['impressions'], 1)
            ctrs[cat] = data['engagement'] / impressions

        if not ctrs:
            return {}

        avg_ctr = sum(ctrs.values()) / len(ctrs)
        weights = {}
        for cat, ctr in ctrs.items():
            if avg_ctr > 0:
                raw_weight = ctr / avg_ctr
            else:
                raw_weight = 1.0
            weight = max(min_weight, min(max_weight, raw_weight))
            weights[cat] = weight
            self.db.update_category_weight(cat, weight, avg_ctr=ctr)
            logger.info(f"📊 Categoria '{cat}': CTR={ctr:.4f} → peso={weight:.2f}")

        return weights

    def get_ranking(self, days: int = 30) -> List[Dict]:
        """Ritorna una classifica leggibile categoria -> CTR, per log/debug"""
        perf = self.db.get_category_performance(days=days)
        ranking = []
        for cat, data in perf.items():
            impressions = max(data['impressions'], 1)
            ctr = data['engagement'] / impressions
            ranking.append({'category': cat, 'ctr': round(ctr * 100, 2), 'posts': data['posts']})
        ranking.sort(key=lambda x: x['ctr'], reverse=True)
        return ranking
