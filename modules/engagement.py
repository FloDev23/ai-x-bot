"""
Engagement v3 - Punti 7, 8, 9 dell'analisi

- Punto 7: riconoscere gli influencer con uno score (follower, engagement medio,
  verifica, settore) invece di trattare tutti i tweet allo stesso modo.
- Punto 8: decidere l'azione giusta per ogni tweet incontrato, tra
  Ignora / Like / Like+Follow / Commenta / Retweet / Retweet con commento,
  invece di rispondere sempre allo stesso modo.
- Punto 9: regole anti-spam esplicite (mai più di 2 menzioni FlexDropin/giorno,
  mai stesso utente commentato 2 volte in 24h, mai hashtag ripetuti, mai link
  in due tweet consecutivi - quest'ultimo gestito da content_scheduler.py).

Nota costi X API 2026: la ricerca generica (search_recent_tweets) è l'azione
più costosa disponibile. Per questo l'engagement cycle NON gira più ogni 30
minuti su 14 topic, ma su una lista ristretta di account/keyword curati,
poche volte al giorno (vedi main.py).
"""
import logging
from typing import Dict, List, Optional
from modules.database import Database

logger = logging.getLogger(__name__)


def score_influence(follower_count: int, engagement_avg: float, verified: bool) -> int:
    """
    Punto 7: punteggio 0-100 di quanto vale la pena interagire con un account,
    basato su follower, engagement medio e verifica. Puramente aritmetico,
    zero chiamate esterne.
    """
    # Follower: scala logaritmica semplificata a bucket
    if follower_count >= 100_000:
        follower_score = 40
    elif follower_count >= 20_000:
        follower_score = 32
    elif follower_count >= 5_000:
        follower_score = 24
    elif follower_count >= 1_000:
        follower_score = 15
    else:
        follower_score = 5

    engagement_score = min(40, engagement_avg * 4)  # engagement_avg atteso 0-10
    verified_bonus = 20 if verified else 0

    return int(min(100, follower_score + engagement_score + verified_bonus))


def decide_action(influence_score: int, is_lead: bool = False,
                   sentiment: str = "neutro") -> str:
    """
    Punto 8: decide la migliore azione tra
    Ignora, Like, Like+Follow, Commenta, Retweet, Retweet con commento

    Regole semplici e trasparenti (facilmente ritarabili):
    - lamentele/negativo verso terzi → di norma meglio ignorare
    - lead commerciale reale → Commenta (gestito comunque da lead_finder, non qui)
    - influencer alto punteggio → Retweet con commento o Commenta
    - influencer medio → Like+Follow
    - basso punteggio → Like o Ignora
    """
    if sentiment == "negativo" and not is_lead:
        return "Ignora"
    if is_lead:
        return "Commenta"
    if influence_score >= 70:
        return "Retweet con commento"
    if influence_score >= 45:
        return "Like+Follow"
    if influence_score >= 20:
        return "Like"
    return "Ignora"


class EngagementManager:
    """Gestisce follow/like/commenti verso una lista curata di target (non a strascico)"""

    def __init__(self, twitter_client, ai_generator, db: Database, max_comments_per_session: int = 3):
        self.client = twitter_client
        self.ai = ai_generator
        self.db = db
        self.max_comments_per_session = max_comments_per_session

    def sync_target_accounts(self, usernames: List[str]):
        """
        Aggiorna nel DB i dati (follower, verifica) della lista curata di account
        target (config.TARGET_ACCOUNTS). Da chiamare poche volte a settimana:
        è comunque una lettura, quindi non va fatta ogni ciclo.
        """
        for username in usernames:
            info = self.client.get_user_info(username)
            if not info:
                continue
            score = score_influence(
                follower_count=info.get('followers_count', 0),
                engagement_avg=info.get('engagement_avg', 1.0),
                verified=info.get('verified', False),
            )
            self.db.upsert_target_account(
                username=username,
                user_id=info.get('id', ''),
                follower_count=info.get('followers_count', 0),
                engagement_score=info.get('engagement_avg', 1.0),
                verified=info.get('verified', False),
                score=score,
            )
            logger.info(f"👤 Target aggiornato: @{username} → score {score}/100")

    def run_targeted_engagement(self, max_targets: int = 5):
        """
        Cicla sui migliori account target curati (punto 7), decide l'azione
        (punto 8) e la esegue rispettando le regole anti-spam (punto 9).
        """
        targets = self.db.get_top_targets(limit=max_targets)
        if not targets:
            logger.info("ℹ️ Nessun target curato in DB. Configura TARGET_ACCOUNTS in config.py "
                        "e chiama sync_target_accounts().")
            return

        comments_made = 0
        for target in targets:
            username = target['username']

            latest_tweet = self.client.get_latest_tweet(username)
            if not latest_tweet:
                continue

            action = decide_action(influence_score=target['score'])
            logger.info(f"🎯 @{username} (score {target['score']}) → azione: {action}")

            if action == "Ignora":
                continue

            if action in ("Like", "Like+Follow", "Retweet con commento"):
                self.client.like_tweet(latest_tweet['id'])

            if action == "Like+Follow":
                self.client.follow_user(target.get('user_id') or username)

            if action == "Retweet con commento" and comments_made < self.max_comments_per_session:
                if self.db.commented_on_user_recently(username):
                    logger.info(f"⏭️ Salto @{username}: già commentato nelle ultime 24h (anti-spam)")
                    continue
                comment = self.ai.generate_flexdropin_comment(latest_tweet['text'], promotional=False)
                if comment:
                    self.client.reply_to_tweet(latest_tweet['id'], comment)
                    self.db.mark_commented_on_user(username)
                    comments_made += 1

            self.db.mark_target_interacted(username)

        logger.info(f"✅ Ciclo engagement mirato completato: {comments_made} commenti pubblicati")
