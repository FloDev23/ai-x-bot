"""
Lead Finder / Opportunity Detector - Punto 19 dell'analisi
("la funzionalità che implementerei per prima" nel documento originale,
qui ridimensionata in Fase 4 per motivi di costo: ogni ricerca X costa,
quindi va fatta con query mirate e poche volte al giorno, non a ciclo continuo).

MERCATO: keyword in inglese, mirate a gestori di palestre/boutique studio
fuori dall'Italia (il mercato italiano resta presidiato via Instagram/di persona).

Cerca frasi che indicano intenzione commerciale reale (apertura palestra,
ricerca gestionale, pochi iscritti, ecc.), assegna un punteggio 0-100,
salva tutto in un piccolo CRM locale (modules/database.py) e SUGGERISCE
un'azione. Non risponde mai in automatico: la decisione finale resta a
Floriano, per evitare sia costi di lettura/scrittura inutili sia il rischio
di spam percepito.
"""
import logging
from typing import Dict, List
from groq import Groq
from modules.database import Database

logger = logging.getLogger(__name__)

# Frasi che indicano una potenziale opportunità commerciale (punto 19)
# In inglese: mercato internazionale (gestori palestre/studio fuori Italia)
PROBLEM_KEYWORDS = [
    "opening a gym",
    "starting a boutique studio",
    "looking for gym software",
    "how to fill my classes",
    "low class attendance",
    "which gym management software",
    "how do you get new gym members",
    "need a booking app for my studio",
    "gym management software recommendation",
    "drop-in class booking",
]


class LeadFinder:
    def __init__(self, twitter_client, groq_client: Groq, model: str, db: Database):
        self.client = twitter_client
        self.groq = groq_client
        self.model = model
        self.db = db

    def find_opportunities(self, max_per_keyword: int = 3) -> List[Dict]:
        """
        Esegue ricerche mirate (poche, non a strascico) sulle keyword ad alto
        valore commerciale. Da chiamare 2-3 volte al giorno, non ogni 30 minuti.
        """
        found = []
        for keyword in PROBLEM_KEYWORDS:
            tweets = self.client.search_tweets(keyword, limit=max_per_keyword)
            for tweet in tweets:
                if self.db.lead_already_seen(tweet['id']):
                    continue

                score, action = self._score_lead(tweet['text'], keyword)
                self.db.add_lead(
                    tweet_id=tweet['id'],
                    author_username=tweet.get('author_username', ''),
                    author_id=tweet.get('author_id', ''),
                    text=tweet['text'],
                    score=score,
                    matched_keyword=keyword,
                    action_suggested=action,
                )
                found.append({
                    'tweet_id': tweet['id'], 'text': tweet['text'],
                    'score': score, 'action': action, 'keyword': keyword,
                })
                logger.info(f"🎯 Lead trovato (score {score}/100, azione: {action}): {tweet['text'][:60]}...")

        return found

    def _score_lead(self, text: str, keyword: str) -> (int, str):
        """
        Assegna un punteggio 0-100 al potenziale lead e suggerisce l'azione
        migliore tra: Ignora, Like, Commenta, DM, Commenta+DM
        """
        prompt = f"""A user on X wrote this tweet, which contains the key phrase "{keyword}":

"{text}"

You are the commercial analyst for FlexDropin, an app that helps gyms and boutique
studios manage drop-in class bookings and find new customers.

Rate how much this tweet represents a REAL COMMERCIAL LEAD (not spam, not sarcasm,
not an out-of-target user) with a score from 0 to 100.

Then pick the BEST action among these exact options:
Ignora, Like, Commenta, DM, Commenta+DM

Reply ONLY in this exact one-line format:
SCORE|ACTION

Example: 78|Commenta"""

        try:
            response = self.groq.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model=self.model,
                max_tokens=20,
                temperature=0.3,
            )
            raw = response.choices[0].message.content.strip()
            score_str, action = raw.split('|')
            score = max(0, min(100, int(score_str.strip())))
            action = action.strip()
            if action not in ("Ignora", "Like", "Commenta", "DM", "Commenta+DM"):
                action = "Ignora"
            return score, action
        except Exception as e:
            logger.warning(f"⚠️ Errore nello scoring del lead: {e}")
            return 0, "Ignora"

    def get_actionable_leads(self, min_score: int = 60) -> List[Dict]:
        """Ritorna i lead sopra soglia, pronti per una revisione manuale/azione"""
        return self.db.get_open_leads(min_score=min_score)
