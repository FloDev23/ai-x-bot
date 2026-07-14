"""
AI Generator v3 - Growth Agent
Cambiamenti principali rispetto alla v1:
- Punto 12: persona cambiata da "social media manager" a "founder che costruisce in pubblico"
- Punto 13: multi-agente (Business/Fitness/Founder/Copywriter/Community) + Editor che sceglie
- Punto 5: "human mode" - post occasionali informali, non promozionali
- Punto 6: "build in public" - recap settimanale di progressi/bug/numeri
- Nuovo: thread generator, generatore varianti A/B, memoria anti-ripetizione (riceve
  gli argomenti recenti dal database e li evita esplicitamente nel prompt)
- Nuovo: controllo esplicito se includere il link (il link costa $0.20 invece di
  $0.015 a post sull'API X 2026, quindi va usato solo quando necessario)
"""
import logging
import json
from groq import Groq
from config import GROQ_API_KEY, GROQ_MODEL, FLEXDROPIN_PLAY_STORE, FLEXDROPIN_APP_STORE, FLEXDROPIN_WEBSITE
from typing import Dict, Optional, List, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Punto 12: nuova persona. Non più "social media manager" ma founder reale.
# ---------------------------------------------------------------------------
FOUNDER_PERSONA = """Sei Floriano, founder solista di FlexDropin, un'app che permette
di prenotare lezioni fitness drop-in (yoga, crossfit, pilates, functional...) e aiuta
le palestre a riempire i posti vuoti nelle lezioni. Costruisci tutto da solo: prodotto,
marketing, vendite. Scrivi su X in prima persona, come un founder che condivide il
percorso reale (build in public), non come un social media manager che fa promozione.
Tono: diretto, umano, a volte autoironico, mai gonfio di marketing-speak. Puoi parlare
di business del fitness, del prodotto, di startup, oppure semplicemente della tua
giornata da founder."""

# ---------------------------------------------------------------------------
# Punto 13: Multi-agente. Ogni agente ha un taglio diverso sullo stesso spunto.
# ---------------------------------------------------------------------------
AGENTS = {
    "business_expert": FOUNDER_PERSONA + """
Scrivi come esperto di business del fitness: parla di margini, retention,
riempimento corsi, gestione palestre, drop-in come modello di ricavo aggiuntivo.""",

    "fitness_expert": FOUNDER_PERSONA + """
Scrivi come appassionato di fitness: parla di allenamento, discipline, community,
motivazione, senza fare business talk.""",

    "startup_founder": FOUNDER_PERSONA + """
Scrivi come founder che racconta la costruzione di FlexDropin: bug risolti,
decisioni difficili, numeri, lezioni imparate, dubbi.""",

    "copywriter": FOUNDER_PERSONA + """
Scrivi in modo estremamente sintetico e ad alto impatto, ottimizzato per generare
discussione/risposte, stile hook diretto nelle prime parole.""",

    "community_manager": FOUNDER_PERSONA + """
Scrivi in modo caldo e colloquiale, come se stessi rispondendo a un amico nella
community fitness, poche frasi, tono leggero.""",
}

# Quale/i agenti usare per ciascuna categoria del palinsesto (content_scheduler.py)
CATEGORY_TO_AGENTS = {
    "business_palestra": ["business_expert", "startup_founder"],
    "trend_fitness": ["fitness_expert", "copywriter"],
    "behind_the_scenes": ["startup_founder", "community_manager"],
    "consiglio_pratico": ["business_expert", "fitness_expert"],
    "trasparenza": ["startup_founder"],
    "community": ["community_manager", "fitness_expert"],
    "human_mode": ["startup_founder"],
}


def _get_link() -> str:
    return FLEXDROPIN_WEBSITE


class AIGenerator:
    """Genera contenuti con AI usando Groq - Growth Agent per FlexDropin"""

    def __init__(self):
        self.client = Groq(api_key=GROQ_API_KEY)
        self.model = GROQ_MODEL

    # ------------------------------------------------------------------
    # Helper generico di chiamata a Groq
    # ------------------------------------------------------------------
    def _complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 120,
                   temperature: float = 0.8) -> Optional[str]:
        try:
            response = self.client.chat.completions.create(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"❌ Errore chiamata Groq: {e}")
            return None

    @staticmethod
    def _truncate(text: str, limit: int = 280) -> str:
        if len(text) > limit:
            return text[:limit - 3] + "..."
        return text

    # ------------------------------------------------------------------
    # Generazione principale multi-agente (punti 12 e 13)
    # ------------------------------------------------------------------
    def generate_tweet(self, category: str, topic_hint: str = "",
                        recent_topics: Optional[List[str]] = None,
                        include_link: bool = False,
                        event_context: Optional[List[str]] = None,
                        seasonal_context: Optional[str] = None) -> Optional[Dict]:
        """
        Genera un tweet per una categoria del palinsesto, usando 1-2 agenti e
        scegliendo il migliore. Ritorna un dict {text, agent_used} o None.
        """
        agent_names = CATEGORY_TO_AGENTS.get(category, ["startup_founder"])
        recent_topics = recent_topics or []

        avoid_block = ""
        if recent_topics:
            avoid_block = "Argomenti già trattati di recente (NON ripeterli, scegli qualcos'altro): " + \
                           "; ".join(recent_topics[:8])

        context_block = ""
        if event_context:
            context_block += f"\nEventi fitness in corso di cui potresti parlare: {', '.join(event_context)}."
        if seasonal_context:
            context_block += f"\nContesto stagionale: {seasonal_context}."

        link_instruction = (
            f"Includi naturalmente il link {_get_link()} come call-to-action."
            if include_link else
            "NON includere nessun link né invito esplicito a scaricare l'app: "
            "è un post di contenuto/valore, non promozionale."
        )

        candidates = []
        for agent_name in agent_names:
            system_prompt = AGENTS[agent_name]
            user_prompt = f"""Scrivi UN tweet (max 280 caratteri) per la categoria "{category}".
{f'Spunto/topic: {topic_hint}' if topic_hint else ''}
{avoid_block}
{context_block}
{link_instruction}

Rispondi SOLO con il testo del tweet, senza virgolette, senza spiegazioni."""

            text = self._complete(system_prompt, user_prompt)
            if text:
                candidates.append({"agent": agent_name, "text": self._truncate(text)})

        if not candidates:
            return None
        if len(candidates) == 1:
            return {"text": candidates[0]["text"], "agent_used": candidates[0]["agent"]}

        return self._editor_pick(candidates, category)

    def _editor_pick(self, candidates: List[Dict], category: str) -> Dict:
        """L'agente 'Editor' sceglie il candidato migliore tra quelli generati (punto 13)"""
        options_block = "\n".join(f"{i+1}. {c['text']}" for i, c in enumerate(candidates))
        prompt = f"""Sei l'editor di un account X di startup fitness. Categoria: {category}.
Scegli quale di questi tweet pubblicheresti, considerando naturalezza, originalità
e potenziale di discussione:

{options_block}

Rispondi SOLO con il numero della scelta (es: 1)."""

        choice = self._complete(
            "Sei un editor esperto di social media, rigoroso e sintetico.",
            prompt, max_tokens=5, temperature=0.1
        )
        try:
            idx = int(choice.strip()) - 1
            if 0 <= idx < len(candidates):
                return {"text": candidates[idx]["text"], "agent_used": candidates[idx]["agent"]}
        except (ValueError, AttributeError, TypeError):
            pass
        # fallback: primo candidato
        return {"text": candidates[0]["text"], "agent_used": candidates[0]["agent"]}

    # ------------------------------------------------------------------
    # Punto 17/18: Thread generator e A/B test
    # ------------------------------------------------------------------
    def generate_thread(self, topic: str, num_tweets: int = 5) -> List[str]:
        """Genera un thread multi-tweet (punto 17: campagne strutturate)"""
        prompt = f"""{FOUNDER_PERSONA}

Scrivi un thread X di {num_tweets} tweet sul tema: "{topic}".
Struttura consigliata: 1) problema/hook, 2) dato o fatto, 3) caso reale o esempio,
4) consiglio pratico, 5) call to action leggera verso FlexDropin ({_get_link()}).

Rispondi SOLO con un array JSON di {num_tweets} stringhe, una per tweet, senza numerazione
visibile nel testo, senza altre spiegazioni. Esempio: ["testo1", "testo2", ...]"""

        raw = self._complete(FOUNDER_PERSONA, prompt, max_tokens=600, temperature=0.8)
        if not raw:
            return []
        try:
            raw = raw.replace('```json', '').replace('```', '').strip()
            tweets = json.loads(raw)
            return [self._truncate(t) for t in tweets][:num_tweets]
        except Exception as e:
            logger.warning(f"⚠️ Errore nel parsing del thread, ritorno lista vuota: {e}")
            return []

    def generate_ab_variants(self, topic: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Genera due varianti dello stesso tweet con hook diversi (punto 18):
        A = apertura con domanda, B = apertura con dato/fatto.
        """
        prompt_a = f"""{FOUNDER_PERSONA}
Scrivi un tweet (max 280 caratteri) sul tema "{topic}" che INIZI con una domanda
diretta al lettore. Rispondi solo col testo del tweet."""
        prompt_b = f"""{FOUNDER_PERSONA}
Scrivi un tweet (max 280 caratteri) sul tema "{topic}" che INIZI con un dato o un
fatto concreto (non una domanda). Rispondi solo col testo del tweet."""

        variant_a = self._complete(FOUNDER_PERSONA, prompt_a)
        variant_b = self._complete(FOUNDER_PERSONA, prompt_b)
        return (
            self._truncate(variant_a) if variant_a else None,
            self._truncate(variant_b) if variant_b else None,
        )

    # ------------------------------------------------------------------
    # Punto 5: Human mode
    # ------------------------------------------------------------------
    def generate_human_mode_post(self) -> Optional[str]:
        """Post informale che fa sembrare l'account umano, non promozionale"""
        prompt = f"""{FOUNDER_PERSONA}

Scrivi un tweet breve, informale, imperfetto, come quelli che scrivono i founder
veri quando raccontano una giornata storta o un momento buffo dello sviluppo
(es. bug assurdi, aspettative sbagliate, piccole vittorie). Niente promozione,
niente link, niente hashtag. Max 280 caratteri."""

        text = self._complete(FOUNDER_PERSONA, prompt, temperature=0.95)
        return self._truncate(text) if text else None

    # ------------------------------------------------------------------
    # Punto 6: Build in public
    # ------------------------------------------------------------------
    def generate_build_in_public_post(self, highlights: List[str]) -> Optional[str]:
        """
        Genera il recap settimanale 'build in public' (punto 6).
        highlights: lista di frasi tipo ["risolto bug pagamenti", "+12 palestre onboardate"]
        """
        highlights_block = "\n".join(f"- {h}" for h in highlights) if highlights else \
            "- una settimana di lavoro tranquilla, senza grandi novità"

        prompt = f"""{FOUNDER_PERSONA}

Scrivi un tweet di recap settimanale "build in public" basato su questi punti reali:
{highlights_block}

Tono onesto e concreto, non da comunicato stampa. Max 280 caratteri."""

        text = self._complete(FOUNDER_PERSONA, prompt, temperature=0.85)
        return self._truncate(text) if text else None

    # ------------------------------------------------------------------
    # Commento a tweet (uso mirato: solo su target curati / lead, non a strascico)
    # ------------------------------------------------------------------
    def generate_flexdropin_comment(self, tweet_text: str, promotional: bool = False) -> Optional[str]:
        """
        Genera un commento di valore. Se promotional=False (default consigliato),
        NON menziona FlexDropin: aggiunge solo valore reale alla conversazione,
        utile per commentare account target senza sembrare spam (punto 9).
        """
        promo_line = (
            "Puoi menzionare FlexDropin in modo naturale, senza essere invadente."
            if promotional else
            "NON menzionare FlexDropin: il commento deve aggiungere valore puro alla conversazione."
        )

        prompt = f"""{FOUNDER_PERSONA}

Leggi questo tweet:
"{tweet_text}"

Scrivi un commento breve (max 200 caratteri), cortese, che aggiunge valore reale
alla conversazione. {promo_line}

Rispondi SOLO col testo del commento."""

        text = self._complete(FOUNDER_PERSONA, prompt, temperature=0.75)
        return self._truncate(text, 280) if text else None
