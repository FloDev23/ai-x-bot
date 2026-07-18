#!/usr/bin/env python3
"""
AI X Bot v3 - FlexDropin Growth Agent
Da "bot che pubblica" a "agente di crescita": memoria a lungo termine,
scoring pre-pubblicazione, auto-learning sulle performance, opportunity
detector, engagement mirato su account curati, build in public settimanale.

Punti implementati (vedi analisi allegata):
1. Memoria a lungo termine        -> modules/database.py
2. Performance analytics          -> modules/analytics.py
3. Score dei tweet                 -> modules/scoring.py
4. Database delle idee             -> modules/database.py (tabella ideas)
5. Human mode                      -> modules/ai_generator.py
6. Build in public                 -> ciclo weekly_build_in_public_cycle
7. Riconoscere gli influencer       -> modules/engagement.py (score_influence)
8. Decidere l'azione su un tweet    -> modules/engagement.py (decide_action)
9. Anti-spam                       -> modules/database.py + content_scheduler.py
10-11. Eventi e calendario stagionale -> modules/content_scheduler.py
12. Persona founder                -> modules/ai_generator.py (FOUNDER_PERSONA)
13. Multi-agente + Editor           -> modules/ai_generator.py
17-18. Thread e A/B test           -> modules/ai_generator.py
19. Opportunity detector / lead     -> modules/lead_finder.py

NOTA COSTI (X API 2026): il bot è stato riprogettato per usare cicli a
frequenza fissa (poche volte al giorno) invece di intervalli continui,
per contenere il costo delle letture/post a pagamento. Vedi config.py.
"""

import logging
import random
import sys
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

try:
    from config import (
        validate_config, DEBUG, FLEXDROPIN_PROMO,
        DAILY_POST_TIMES, OPPORTUNITY_CYCLE_TIMES, TARGETED_ENGAGEMENT_TIMES,
        PERFORMANCE_CYCLE_TIME, BUILD_IN_PUBLIC_DAY,
        TWEET_SCORE_THRESHOLD, MAX_REGENERATION_ATTEMPTS,
        MAX_FLEXDROPIN_MENTIONS_PER_DAY, MAX_LINKS_PER_WEEK,
        TARGET_ACCOUNTS, HUMAN_MODE_PROBABILITY, SEARCH_TOPICS,
        MAX_COMMENTS_PER_SESSION, MEGA_ACCOUNT_FOLLOWER_THRESHOLD,
        TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, LEAD_NOTIFY_MIN_SCORE,
    )
    from modules.database import Database
    from modules.notifier import TelegramNotifier
    from modules.content_scheduler import (
        get_categories_for_today, get_seasonal_context, get_active_events,
        pick_category, should_include_link, PROMO_CATEGORIES,
    )
    from modules.scoring import TweetScorer
    from modules.ai_generator import AIGenerator
    from modules.twitter_client import TwitterClient
    from modules.engagement import EngagementManager
    from modules.lead_finder import LeadFinder
    from modules.analytics import PerformanceAnalyzer
    from modules.news_fetcher import NewsFetcher
except ImportError as e:
    print(f"❌ Errore di import: {e}")
    print("Installa le dipendenze con: pip install -r requirements.txt")
    sys.exit(1)

# Setup logging
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)


class FlexDropinGrowthAgent:
    """Growth Agent per FlexDropin: contenuti, engagement mirato, lead, analytics"""

    def __init__(self):
        logger.info("🤖 Inizializzazione FlexDropin Growth Agent v3...")

        try:
            validate_config()

            self.db = Database()
            self.notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
            self.news_fetcher = NewsFetcher()
            self.ai_generator = AIGenerator()
            self.twitter_client = TwitterClient()
            self.scorer = TweetScorer(self.ai_generator.client, self.ai_generator.model)
            self.engagement_manager = EngagementManager(
                self.twitter_client, self.ai_generator, self.db,
                max_comments_per_session=MAX_COMMENTS_PER_SESSION,
                mega_account_threshold=MEGA_ACCOUNT_FOLLOWER_THRESHOLD,
            )
            self.lead_finder = LeadFinder(
                self.twitter_client, self.ai_generator.client, self.ai_generator.model, self.db
            )
            self.analyzer = PerformanceAnalyzer(self.twitter_client, self.db)

            self.scheduler = BackgroundScheduler()
            logger.info("✅ Growth Agent inizializzato con successo")
            logger.info(f"💚 Promozione FlexDropin: {'ABILITATA' if FLEXDROPIN_PROMO else 'DISABILITATA'}")

        except Exception as e:
            logger.error(f"❌ Errore durante l'inizializzazione: {e}")
            raise

    # ------------------------------------------------------------------
    # Ciclo 1: contenuti giornalieri (palinsesto + multi-agente + scoring)
    # ------------------------------------------------------------------
    def daily_content_cycle(self):
        logger.info(f"🔄 Inizio ciclo di contenuti - {datetime.now()}")
        try:
            # Punto 5: ogni tanto, invece del palinsesto, un post "umano"
            if random.random() < HUMAN_MODE_PROBABILITY:
                text = self.ai_generator.generate_human_mode_post()
                if text:
                    self._publish(text, category='human_mode', topic='human_mode',
                                   has_link=False, score_total=None, agent_used='human_mode')
                return

            recent_topics = self.db.get_recent_topics(days=3)
            weights = self.db.get_all_category_weights()

            # Punto 9: evita di ripetere una categoria già pubblicata nelle ultime ore
            candidates = get_categories_for_today()
            avoid = [c for c in candidates if self.db.category_posted_recently(c, hours=20)]
            category = pick_category(weights, avoid_categories=avoid)

            # Tetto giornaliero menzioni FlexDropin (punto 9)
            if category in PROMO_CATEGORIES and \
                    self.db.count_flexdropin_mentions_today() >= MAX_FLEXDROPIN_MENTIONS_PER_DAY:
                non_promo = [c for c in candidates if c not in PROMO_CATEGORIES] or ['trend_fitness']
                logger.info("⏭️ Tetto menzioni FlexDropin raggiunto per oggi, passo a categoria non promo")
                category = random.choice(non_promo)

            include_link = should_include_link(
                category,
                links_posted_last_7_days=self.db.count_links_last_days(7),
                max_links_per_week=MAX_LINKS_PER_WEEK,
                last_post_had_link=self.db.last_post_had_link(),
            )

            topic_hint = self._get_topic_hint(category)
            event_context = get_active_events()
            seasonal_context = get_seasonal_context()

            text, agent_used, scores = self._generate_and_score(
                category, topic_hint, recent_topics, include_link,
                event_context, seasonal_context
            )
            if not text:
                logger.warning("⚠️ Nessun tweet generato/approvato in questo ciclo, salto la pubblicazione")
                return

            self._publish(text, category=category, topic=topic_hint, has_link=include_link,
                           score_total=scores.get('totale') if scores else None, agent_used=agent_used)

        except Exception as e:
            logger.error(f"❌ Errore nel ciclo di contenuti: {e}")
            self.notifier.notify_error("daily_content_cycle", e)

    def _get_topic_hint(self, category: str) -> str:
        """Punto 15 (parziale): spunto da news reali per le categorie trend"""
        if category != 'trend_fitness':
            return ''
        try:
            topic = random.choice(SEARCH_TOPICS)
            articles = self.news_fetcher.get_trending_news(topic, limit=1)
            if articles:
                return articles[0].get('title', topic)
            return topic
        except Exception:
            return ''

    def _generate_and_score(self, category, topic_hint, recent_topics, include_link,
                             event_context, seasonal_context):
        """Genera un tweet e lo rigenera se sotto soglia di qualità (punto 3)"""
        attempts = 0
        last_scores = None
        candidate = None
        while attempts <= MAX_REGENERATION_ATTEMPTS:
            candidate = self.ai_generator.generate_tweet(
                category=category, topic_hint=topic_hint, recent_topics=recent_topics,
                include_link=include_link, event_context=event_context,
                seasonal_context=seasonal_context,
            )
            if not candidate:
                attempts += 1
                continue

            scores = self.scorer.passes_threshold(candidate['text'], threshold=TWEET_SCORE_THRESHOLD)
            last_scores = scores
            logger.info(f"📝 Tentativo {attempts + 1}: score {scores['totale']}/40 "
                        f"({'✅ approvato' if scores['approved'] else '❌ sotto soglia'})")

            if scores['approved']:
                return candidate['text'], candidate['agent_used'], scores
            attempts += 1

        # Se dopo i tentativi non passa la soglia, pubblica comunque l'ultimo
        # candidato generato piuttosto che saltare il post: evita di lasciare
        # l'account silente, ma logga chiaramente il punteggio basso.
        if candidate:
            logger.warning("⚠️ Pubblico comunque l'ultimo candidato sotto soglia")
            return candidate['text'], candidate['agent_used'], last_scores
        return None, None, None

    def _publish(self, text, category, topic, has_link, score_total, agent_used):
        result = self.twitter_client.post_tweet(text)
        if not result or not getattr(result, 'data', None):
            logger.error(f"❌ Pubblicazione fallita [{category}/{agent_used}]: {text[:80]}...")
            return

        tweet_id = result.data.get('id', '')
        self.db.log_posted_tweet(
            text=text, category=category, topic=topic, tweet_id=tweet_id,
            has_link=has_link, score_total=score_total, agent_used=agent_used,
        )
        logger.info(f"✅ Tweet pubblicato [{category}/{agent_used}]: {text[:80]}...")

    # ------------------------------------------------------------------
    # Ciclo 2: build in public settimanale (punto 6)
    # ------------------------------------------------------------------
    def weekly_build_in_public_cycle(self):
        logger.info(f"📢 Ciclo build in public - {datetime.now()}")
        try:
            recent = self.db.get_recent_topics(days=7, limit=10)
            highlights = recent if recent else []
            text = self.ai_generator.generate_build_in_public_post(highlights)
            if text:
                self._publish(text, category='build_in_public', topic='weekly_recap',
                              has_link=False, score_total=None, agent_used='startup_founder')
        except Exception as e:
            logger.error(f"❌ Errore nel ciclo build in public: {e}")
            self.notifier.notify_error("weekly_build_in_public_cycle", e)

    # ------------------------------------------------------------------
    # Ciclo 3: opportunity detector / lead (punto 19)
    # ------------------------------------------------------------------
    def opportunity_cycle(self):
        logger.info(f"🎯 Ciclo opportunity detector - {datetime.now()}")
        try:
            found = self.lead_finder.find_opportunities(
                ai_generator=self.ai_generator, notifier=self.notifier,
                notify_min_score=LEAD_NOTIFY_MIN_SCORE,
            )
            logger.info(f"✅ Ciclo opportunity completato: {len(found)} nuovi lead salvati nel CRM locale")
        except Exception as e:
            logger.error(f"❌ Errore nel ciclo opportunity: {e}")
            self.notifier.notify_error("opportunity_cycle", e)

    # ------------------------------------------------------------------
    # Ciclo 4: engagement mirato su account curati (punti 7, 8, 9)
    # ------------------------------------------------------------------
    def targeted_engagement_cycle(self):
        logger.info(f"💬 Ciclo engagement mirato - {datetime.now()}")
        try:
            self.engagement_manager.run_targeted_engagement(notifier=self.notifier)
        except Exception as e:
            logger.error(f"❌ Errore nel ciclo di engagement mirato: {e}")
            self.notifier.notify_error("targeted_engagement_cycle", e)

    # ------------------------------------------------------------------
    # Ciclo 5: performance analytics / auto-learning (punto 2)
    # ------------------------------------------------------------------
    def performance_cycle(self):
        logger.info(f"📊 Ciclo performance analytics - {datetime.now()}")
        try:
            self.analyzer.refresh_own_tweet_metrics()
            self.analyzer.recompute_category_weights()
            ranking = self.analyzer.get_ranking()
            if ranking:
                logger.info("📈 Classifica categorie per CTR: " +
                            ", ".join(f"{r['category']} ({r['ctr']}%)" for r in ranking))
        except Exception as e:
            logger.error(f"❌ Errore nel ciclo di performance: {e}")
            self.notifier.notify_error("performance_cycle", e)

    # ------------------------------------------------------------------
    # Setup account target curati (una tantum / settimanale)
    # ------------------------------------------------------------------
    def sync_targets(self):
        if not TARGET_ACCOUNTS:
            logger.info("ℹ️ TARGET_ACCOUNTS vuoto in config.py/.env: nessun account curato da sincronizzare. "
                        "Aggiungi username separati da virgola in TARGET_ACCOUNTS per abilitare "
                        "l'engagement mirato (punto 7).")
            return
        try:
            self.engagement_manager.sync_target_accounts(TARGET_ACCOUNTS)
        except Exception as e:
            logger.error(f"❌ Errore nella sincronizzazione dei target: {e}")

    # ------------------------------------------------------------------
    # Avvio
    # ------------------------------------------------------------------
    def start(self):
        logger.info("🚀 Avvio FlexDropin Growth Agent...")

        for t in DAILY_POST_TIMES:
            hh, mm = t.strip().split(':')
            self.scheduler.add_job(
                self.daily_content_cycle, CronTrigger(hour=int(hh), minute=int(mm)),
                id=f'content_{t}', name=f'Daily Content {t}'
            )

        for t in OPPORTUNITY_CYCLE_TIMES:
            hh, mm = t.strip().split(':')
            self.scheduler.add_job(
                self.opportunity_cycle, CronTrigger(hour=int(hh), minute=int(mm)),
                id=f'opportunity_{t}', name=f'Opportunity Detector {t}'
            )

        for t in TARGETED_ENGAGEMENT_TIMES:
            hh, mm = t.strip().split(':')
            self.scheduler.add_job(
                self.targeted_engagement_cycle, CronTrigger(hour=int(hh), minute=int(mm)),
                id=f'engagement_{t}', name=f'Targeted Engagement {t}'
            )

        hh, mm = PERFORMANCE_CYCLE_TIME.strip().split(':')
        self.scheduler.add_job(
            self.performance_cycle, CronTrigger(hour=int(hh), minute=int(mm)),
            id='performance_cycle', name='Performance Analytics'
        )

        self.scheduler.add_job(
            self.weekly_build_in_public_cycle,
            CronTrigger(day_of_week=BUILD_IN_PUBLIC_DAY[:3].lower(), hour=9, minute=30),
            id='build_in_public', name='Build in Public settimanale'
        )

        # Sincronizza i target curati una volta all'avvio e poi settimanalmente
        self.sync_targets()
        self.scheduler.add_job(
            self.sync_targets, CronTrigger(day_of_week='mon', hour=8, minute=0),
            id='sync_targets', name='Sync Target Accounts'
        )

        self.scheduler.start()
        logger.info("✅ Growth Agent avviato e in esecuzione. Premi Ctrl+C per fermare.")
        logger.info(f"📅 Post giornalieri: {DAILY_POST_TIMES} | Opportunity: {OPPORTUNITY_CYCLE_TIMES} | "
                    f"Engagement mirato: {TARGETED_ENGAGEMENT_TIMES}")

        try:
            import time
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("⏹️ Arresto del bot...")
            self.scheduler.shutdown()
            logger.info("✅ Bot arrestato")


def main():
    """Main entry point"""
    try:
        agent = FlexDropinGrowthAgent()
        agent.start()
    except KeyboardInterrupt:
        logger.info("⏹️ Bot arrestato dall'utente")
    except Exception as e:
        logger.error(f"❌ Errore fatale: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
