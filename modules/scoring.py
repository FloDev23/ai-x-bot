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
        prompt = f"""Valuta questo tweet per un account fitness/startup su X (Twitter):

"{tweet_text}"

Assegna un punteggio da 0 a 10 per ciascuno di questi 4 assi:
- utilita: quanto è utile/informativo per chi legge
- originalita: quanto suona genuino e non generico/robotico
- discussione: quanto probabilmente genera commenti/interazione
- promozione: quanto è equilibrata la parte promozionale (10 = perfetto equilibrio, 0 = troppo pubblicitario o assente quando serviva)

Rispondi SOLO con un oggetto JSON valido, senza altro testo, in questo formato esatto:
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
