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
import re
from typing import Dict, List, Tuple
from groq import Groq
from modules.database import Database

logger = logging.getLogger(__name__)

# Frasi che indicano una potenziale opportunità commerciale (punto 19)
# In inglese: mercato internazionale (gestori palestre/studio fuori Italia)
#
# Due archetipi di lead ad alto valore, entrambi coperti qui sotto:
# 1) Chi ha APPENA APERTO una palestra/studio (fase di avvio, cerca clienti
#    e strumenti fin dal day one)
# 2) Chi si LAMENTA di classi vuote / pochi iscritti / non sa come trovare
#    clienti (dolore attivo, già in attività ma in difficoltà)
#
# Le frasi sono volutamente più lunghe/specifiche (non singole parole
# comuni): riduce drasticamente i falsi positivi (corse di cavalli, notizie
# sportive, thread motivazionali generici che matchavano per puro caso).
PROBLEM_KEYWORDS = [
    # Archetipo 1: appena aperto / in fase di apertura
    "just opened my gym and",
    "just opened our gym and",
    "opening my own gym",
    "starting a boutique fitness studio",
    "new gym owner here",
    "just launched my studio",

    # Archetipo 2: classi vuote / pochi clienti / non sa come trovarli
    "low attendance in my classes",
    "how do I fill my classes",
    "my classes are always empty",
    "no one is signing up for my classes",
    "can't get new members for my gym",
    "struggling to get clients for my studio",
    "how do you get new gym members",

    # Segnali di ricerca attiva di uno strumento (intento commerciale diretto)
    "looking for gym management software",
    "which gym management software",
    "need a booking app for my studio",
    "gym management software recommendation",
    "drop-in class booking app",
]


class LeadFinder:
    def __init__(self, twitter_client, groq_client: Groq, model: str, db: Database):
        self.client = twitter_client
        self.groq = groq_client
        self.model = model
        self.db = db

    def find_opportunities(self, max_per_keyword: int = 10, ai_generator=None,
                            notifier=None, notify_min_score: int = 40) -> List[Dict]:
        """
        Esegue ricerche mirate (poche, non a strascico) sulle keyword ad alto
        valore commerciale. Da chiamare 2-3 volte al giorno, non ogni 30 minuti.

        Se ai_generator e notifier sono passati, per ogni lead con score >=
        notify_min_score e azione diversa da "Ignora" viene generata una bozza
        di commento/DM pronta e inviata su Telegram (nessuna azione viene mai
        eseguita in automatico su X: solo notifica + bozza).
        """
        found = []
        for keyword in PROBLEM_KEYWORDS:
            # -is:retweet: un retweet non è mai la persona che vive il
            # problema in prima persona. lang:en: il mercato target è
            # anglofono, evita falsi positivi in altre lingue.
            query = f'"{keyword}" -is:retweet lang:en'
            tweets = self.client.search_tweets(query, limit=max_per_keyword)
            for tweet in tweets:
                if self.db.lead_already_seen(tweet['id']):
                    continue

                score, action = self._score_lead(tweet['text'], keyword)

                if action == "Ignora":
                    # Non ci interessa vederlo in dashboard: non lo salviamo
                    # come lead, ma segniamo il tweet come già valutato per
                    # non richiamare l'AI su di lui in futuro.
                    self.db.mark_tweet_seen(tweet['id'])
                    continue

                self.db.add_lead(
                    tweet_id=tweet['id'],
                    author_username=tweet.get('author_username', ''),
                    author_id=tweet.get('author_id', ''),
                    text=tweet['text'],
                    score=score,
                    matched_keyword=keyword,
                    action_suggested=action,
                )
                lead = {
                    'tweet_id': tweet['id'], 'text': tweet['text'],
                    'score': score, 'action': action, 'keyword': keyword,
                    'author_username': tweet.get('author_username', ''),
                }
                found.append(lead)
                logger.info(f"🎯 Lead trovato (score {score}/100, azione: {action}): {tweet['text'][:60]}...")

                if notifier and score >= notify_min_score:
                    suggested_text = None
                    if ai_generator and action in ("Commenta", "Commenta+DM"):
                        suggested_text = ai_generator.generate_flexdropin_comment(
                            tweet['text'], promotional=True
                        )
                    elif ai_generator and action == "DM":
                        suggested_text = ai_generator.generate_lead_dm(tweet['text'])
                    notifier.notify_lead(lead, suggested_text=suggested_text)

        return found

    def _has_meaningful_content(self, text: str) -> bool:
        """
        Filtro prima ancora di chiamare l'AI: un tweet che, tolti link e
        menzioni, non lascia quasi nessun testo reale non può essere
        valutato onestamente come lead (l'AI finirebbe per "inventare" un
        contesto che non esiste). Meglio scartarlo subito: risparmia anche
        la chiamata Groq.
        """
        stripped = re.sub(r'https?://\S+', '', text)
        stripped = re.sub(r'@\w+', '', stripped)
        stripped = stripped.strip()
        return len(stripped) >= 25

    def _score_lead(self, text: str, keyword: str) -> Tuple[int, str]:
        """
        Assegna un punteggio 0-100 al potenziale lead e suggerisce l'azione
        migliore tra: Ignora, Like, Commenta, DM, Commenta+DM
        """
        if not self._has_meaningful_content(text):
            logger.info("⏭️ Tweet senza contenuto testuale sostanziale (solo link/mention): salto, score 0")
            return 0, "Ignora"

        prompt = f"""A user on X wrote this tweet, which contains the key phrase "{keyword}":

"{text}"

You are the commercial analyst for FlexDropin, an app that helps gyms and boutique
studios manage drop-in class bookings and find new customers.

Rate how much this tweet represents a REAL COMMERCIAL LEAD, with a score from 0 to 100.

The TWO highest-value lead types (score them 70-100 if clearly present):
1) Someone who JUST OPENED (or is about to open) a gym/studio and is in the
   early "let's get this going" phase — they need customers from day one.
2) Someone who is COMPLAINING about empty classes, low attendance, or not
   knowing how to get new clients for their existing gym/studio — active pain.

STRICT RULES — be conservative, false positives waste real human time:
- Only give a score above 40 if the tweet's OWN WORDS clearly express one of
  the two lead types above, or an equally concrete, specific need.
- If the tweet is vague, off-topic, just a link/headline, sarcastic, a news
  story, someone else's business (e.g. a consultant advertising services,
  a municipality announcement), or you would need to GUESS or ASSUME
  details not actually written in the tweet, score it 20 or below and
  choose "Ignora".
- Never treat a coincidental keyword match as a lead if the surrounding
  context is unrelated (news, sports, unrelated business, spam/ads).

Then pick the BEST action among these exact options:
Ignora, Like, Commenta, DM, Commenta+DM

Reply ONLY in this exact one-line format:
SCORE|ACTION

Example: 78|Commenta"""

        try:
            response = self.groq.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model=self.model,
                max_tokens=300,
                temperature=0.3,
            )
            raw = (response.choices[0].message.content or '').strip()

            # Parsing tollerante: il modello a volte aggiunge testo attorno
            # al formato richiesto, quindi cerchiamo il pattern SCORE|AZIONE
            # ovunque compaia nella risposta invece di pretendere che sia
            # l'unica cosa restituita.
            match = re.search(
                r'(\d{1,3})\s*\|\s*(Ignora|Commenta\+DM|Commenta|DM|Like)',
                raw, re.IGNORECASE,
            )
            if not match:
                logger.warning(f"⚠️ Formato scoring non riconosciuto (risposta grezza: '{raw[:150]}')")
                return 0, "Ignora"

            score = max(0, min(100, int(match.group(1))))
            action = match.group(2)
            # Normalizza la capitalizzazione sulle opzioni valide
            for valid in ("Ignora", "Like", "Commenta+DM", "Commenta", "DM"):
                if action.lower() == valid.lower():
                    action = valid
                    break
            return score, action
        except Exception as e:
            logger.warning(f"⚠️ Errore nello scoring del lead: {e}")
            return 0, "Ignora"

    def get_actionable_leads(self, min_score: int = 60) -> List[Dict]:
        """Ritorna i lead sopra soglia, pronti per una revisione manuale/azione"""
        return self.db.get_open_leads(min_score=min_score)