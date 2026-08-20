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
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List
from zoneinfo import ZoneInfo

from config import BOT_TIMEZONE, GROWTH_SCORE_THRESHOLD
from modules.database import Database
from modules.growth_candidate_schema import (
    is_canonical_growth_latest_post,
    is_canonical_growth_profile,
)
from modules.growth_discovery import (
    passes_candidate_filters,
    score_growth_candidate,
)

logger = logging.getLogger(__name__)


class PerformanceAnalyzer:
    """Costruisce la classifica di performance per categoria e aggiorna i pesi"""

    def __init__(self, twitter_client, db: Database):
        self.client = twitter_client
        self.db = db

    @staticmethod
    def _aware_datetime(value, name: str) -> datetime:
        if (
            type(value) is not datetime
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise ValueError(f"{name} must be timezone-aware")
        return value.astimezone(timezone.utc)

    @classmethod
    def _operating_date(cls, value, name: str) -> date:
        if type(value) is date:
            return value
        current_time = cls._aware_datetime(value, name)
        return current_time.astimezone(ZoneInfo(BOT_TIMEZONE)).date()

    def capture_follower_snapshot(self, observed_at: datetime) -> Dict:
        """Capture current followers once and attribute manual follow-backs."""
        current_time = self._aware_datetime(observed_at, "observed_at")
        observed_on = current_time.astimezone(
            ZoneInfo(BOT_TIMEZONE)
        ).date().isoformat()
        try:
            fetched = self.client.get_followers_profiles()
            read_succeeded = isinstance(fetched, (list, tuple))
        except Exception as error:
            logger.warning(
                "follower_snapshot_read_failed error_type=%s",
                type(error).__name__,
            )
            fetched = []
            read_succeeded = False
        profiles = fetched if isinstance(fetched, (list, tuple)) else []
        valid_profiles = []
        seen = set()
        for profile in profiles:
            if not is_canonical_growth_profile(profile):
                continue
            user_id = profile.get("user_id", profile.get("id"))
            if user_id in seen:
                continue
            seen.add(user_id)
            valid_profiles.append(profile)

        summary = {
            "followers_total": len(valid_profiles),
            "new_total": 0,
            "new_relevant": 0,
            "source_counts": {},
            "follow_backs_by_source": {},
        }
        for profile in valid_profiles:
            user_id = profile.get("user_id", profile.get("id"))
            candidate = None
            if not self.db.is_growth_candidate_suppressed(user_id, current_time):
                candidate = self.db.get_cached_growth_candidate(
                    user_id, current_time,
                )
            relevant = False
            if isinstance(candidate, dict):
                latest_post = candidate.get("latest_post")
                if is_canonical_growth_latest_post(latest_post):
                    passed, _reason = passes_candidate_filters(
                        profile, latest_post, current_time,
                    )
                    if passed:
                        score = score_growth_candidate(
                            profile, latest_post, current_time,
                        )
                        relevant = (
                            type(score.get("total")) is int
                            and score["total"] >= GROWTH_SCORE_THRESHOLD
                        )
            result = self.db.capture_follower_observation(
                observed_on,
                current_time,
                profile,
                relevant,
            )
            if not isinstance(result, dict) or result.get("is_new") is not True:
                continue
            summary["new_total"] += 1
            if result.get("relevant") is True:
                summary["new_relevant"] += 1
            source = result.get("attribution_source")
            if type(source) is not str or not source:
                source = "unattributed"
            summary["source_counts"][source] = (
                summary["source_counts"].get(source, 0) + 1
            )
            if result.get("followed_back") is True:
                summary["follow_backs_by_source"][source] = (
                    summary["follow_backs_by_source"].get(source, 0) + 1
                )
        summary["source_counts"] = dict(sorted(summary["source_counts"].items()))
        summary["follow_backs_by_source"] = dict(
            sorted(summary["follow_backs_by_source"].items())
        )
        if read_succeeded:
            self.db.save_follower_snapshot_run(
                observed_on,
                current_time,
                len(valid_profiles),
            )
        return summary

    def build_weekly_report(self, end_date) -> Dict:
        """Build one deterministic seven-operating-day factual report."""
        operating_end = self._operating_date(end_date, "end_date")
        operating_start = operating_end - timedelta(days=6)
        local_zone = ZoneInfo(BOT_TIMEZONE)
        start_at = datetime.combine(operating_start, time.min, tzinfo=local_zone)
        end_at = datetime.combine(
            operating_end + timedelta(days=1), time.min, tzinfo=local_zone,
        )
        raw = self.db.get_weekly_growth_analytics(
            operating_start.isoformat(),
            operating_end.isoformat(),
            start_at,
            end_at,
        )

        content_by_category: Dict[str, int] = {}
        impressions = []
        posts = raw.get("posts") if isinstance(raw.get("posts"), list) else []
        for post in posts:
            if not isinstance(post, dict):
                continue
            category = post.get("category")
            if type(category) is not str or not category.strip():
                category = "generico"
            content_by_category[category] = (
                content_by_category.get(category, 0) + 1
            )
            value = post.get("impressions")
            if type(value) is int and value >= 0:
                impressions.append(value)
        impressions.sort()
        if not impressions:
            median_impressions = 0.0
        else:
            midpoint = len(impressions) // 2
            if len(impressions) % 2:
                median_impressions = impressions[midpoint]
            else:
                median_impressions = (
                    impressions[midpoint - 1] + impressions[midpoint]
                ) / 2

        new_followers = raw["new_followers"]
        new_relevant = raw["new_relevant_followers"]
        relevant_rate = (
            round(new_relevant / new_followers, 4)
            if new_followers > 0
            else 0.0
        )
        manual_by_source = raw["manual_follows_by_source"]
        followed_by_source = raw["follow_backs_by_source"]
        rate_sources = set(raw["candidate_sources"]) | set(manual_by_source)
        follow_back_rate_by_source = {}
        for source in sorted(rate_sources):
            denominator = manual_by_source.get(source, 0)
            numerator = followed_by_source.get(source, 0)
            follow_back_rate_by_source[source] = (
                round(numerator / denominator, 4)
                if denominator > 0
                else 0.0
            )

        return {
            "followers_total": raw["followers_total"],
            "new_followers": new_followers,
            "new_relevant_followers": new_relevant,
            "relevant_follower_rate": relevant_rate,
            "candidate_count": raw["candidate_count"],
            "decision_counts": raw["decision_counts"],
            "follow_back_rate_by_source": follow_back_rate_by_source,
            "median_impressions": median_impressions,
            "post_count": len(posts),
            "content_by_category": dict(sorted(content_by_category.items())),
            "query_budget_used": raw["query_budget_used"],
            "profiles_evaluated": raw["profiles_evaluated"],
            "factual_blocks": {
                "period": {
                    "start_date": operating_start.isoformat(),
                    "end_date": operating_end.isoformat(),
                },
                "new_follower_sources": raw["new_follower_sources"],
                "manual_follows_by_source": manual_by_source,
                "follow_backs_by_source": followed_by_source,
            },
            "attribution_label": "correlation",
        }

    def weekly_report(self) -> Dict:
        """Compatibility wrapper for existing read-only Telegram consumers."""
        return self.build_weekly_report(datetime.now(timezone.utc))

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

    def recompute_category_weights(
        self,
        days: int = 30,
        min_weight: float = 0.3,
        max_weight: float = 3.0,
        *,
        now: datetime = None,
    ) -> Dict[str, float]:
        """
        Calcola il CTR (engagement/impression) per categoria e ne deriva un peso.
        Le categorie che performano meglio della media ottengono un peso > 1,
        quelle sotto media un peso < 1. Questo peso viene poi usato da
        content_scheduler.pick_category() per aumentare automaticamente
        i contenuti che funzionano (auto-ottimizzazione, come richiesto).
        """
        current_time = self._aware_datetime(
            now or datetime.now(timezone.utc), "now",
        )
        first_posted_at = self.db.get_first_posted_at()
        if type(first_posted_at) is not str:
            logger.info("ℹ️ Nessun post pubblicato: pesi invariati")
            return {}
        try:
            first_post = datetime.fromisoformat(first_posted_at)
        except (TypeError, ValueError):
            logger.warning("first_posted_at_malformed: pesi invariati")
            return {}
        if first_post.tzinfo is None or first_post.utcoffset() is None:
            first_post = first_post.replace(tzinfo=timezone.utc)
        else:
            first_post = first_post.astimezone(timezone.utc)
        if first_post > current_time or current_time - first_post < timedelta(days=30):
            logger.info("ℹ️ Primi 30 giorni: pesi categoria invariati")
            return {}

        perf = self.db.get_category_performance(days=days, end_at=current_time)
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
