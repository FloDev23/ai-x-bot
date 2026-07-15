"""
Tweet Scoring - Punto 3 dell'analisi
Ogni tweet viene valutato su 4 assi (0-10 ciascuno, totale 0-40) prima
di essere pubblicato. Sotto soglia viene rigenerato invece che pubblicato.

Usa Groq (già in uso nel bot, costo trascurabile), NON chiama X: costo zero
lato X API.
"""
import json
import logging
from typing import Dict
from groq import Groq

logger = logging.getLogger(__name__)

SCORE_AXES = ["utilita", "originalita", "discussione", "promozione"]


class TweetScorer:
    def __init__(self, groq_client: Groq, model: str):
        self.client = groq_client
        self.model = model

    def score(self, tweet_text: str) -> Dict:
        """
        Ritorna un dict: {utilita, originalita, discussione, promozione, totale}
        Ogni asse è 0-10. In caso di errore ritorna un punteggio neutro (20/40)
        per non bloccare il ciclo di pubblicazione.
        """
        prompt = f"""Evaluate this tweet for a fitness/startup account on X (Twitter):

"{tweet_text}"

Score each of these 4 axes from 0 to 10:
- utilita (usefulness): how useful/informative it is for the reader
- originalita (originality): how genuine it sounds vs generic/robotic
- discussione (discussion potential): how likely it is to spark comments/interaction
- promozione (promo balance): how well-balanced the promotional part is (10 = perfect balance, 0 = too salesy or missing when it should be there)

Reply ONLY with a valid JSON object, no other text, in this exact format:
{{"utilita": <0-10>, "originalita": <0-10>, "discussione": <0-10>, "promozione": <0-10>}}"""

        try:
            response = self.client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model=self.model,
                max_tokens=100,
                temperature=0.2,
            )
            raw = response.choices[0].message.content.strip()
            raw = raw.replace('```json', '').replace('```', '').strip()
            data = json.loads(raw)

            scores = {axis: int(data.get(axis, 5)) for axis in SCORE_AXES}
            scores['totale'] = sum(scores.values())
            return scores

        except Exception as e:
            logger.warning(f"⚠️ Errore nello scoring (uso punteggio neutro): {e}")
            neutral = {axis: 5 for axis in SCORE_AXES}
            neutral['totale'] = 20
            return neutral

    def passes_threshold(self, tweet_text: str, threshold: int = 24) -> Dict:
        """Valuta il tweet e ritorna gli score arricchiti con 'approved': bool"""
        scores = self.score(tweet_text)
        scores['approved'] = scores['totale'] >= threshold
        return scores
