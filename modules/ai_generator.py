"""
AI Generator v3 - Growth Agent (ENGLISH / international market)
Cambiamenti principali rispetto alla v1:
- Punto 12: persona cambiata da "social media manager" a "founder che condivide il percorso"
- Punto 13: multi-agente (Business/Fitness/Founder/Copywriter/Community) + Editor che sceglie
- Punto 5: "human mode" - post occasionali informali, non promozionali
- Punto 6: "build in public" - recap settimanale di progressi/bug/numeri
- Nuovo: thread generator, generatore varianti A/B, memoria anti-ripetizione (riceve
  gli argomenti recenti dal database e li evita esplicitamente nel prompt)
- Nuovo: controllo esplicito se includere il link (il link costa $0.20 invece di
  $0.015 a post sull'API X 2026, quindi va usato solo quando necessario)

NOTA STRATEGICA: su richiesta di Floriano, X è ora dedicato al mercato
internazionale (gestori di palestre/boutique studio fuori dall'Italia).
FlexDropin gestisce IVA condizionale e UI IT/EN a seconda del paese della
palestra, quindi i contenuti X sono generati in INGLESE. Il mercato italiano
resta presidiato via Instagram e visite di persona (fuori da questo bot).
"""
import logging
import json
from groq import Groq
from config import GROQ_API_KEY, GROQ_MODEL, FLEXDROPIN_PLAY_STORE, FLEXDROPIN_APP_STORE, FLEXDROPIN_WEBSITE
from typing import Dict, Optional, List, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Punto 12: nuova persona. Non più "social media manager" ma founder reale.
# In inglese: il pubblico target ora sono gestori di palestre/studio fuori Italia.
# ---------------------------------------------------------------------------
FOUNDER_PERSONA = """You are Floriano, solo founder of FlexDropin, an app that lets people
book drop-in fitness classes (yoga, crossfit, pilates, functional training...) and helps
gyms and boutique studios fill empty spots in their classes. You build everything yourself:
product, marketing, sales. You write on X in first person, as a founder sharing the real
journey (build in public), not as a social media manager doing promotion.
Tone: direct, human, occasionally self-deprecating, never full of marketing-speak. You can
talk about the fitness business, the product, startup life, or just your day as a founder.

IMPORTANT: Always write your reply in English (US), regardless of the topic or any other
language cue in the conversation. Never reply in Italian or any other language."""

# ---------------------------------------------------------------------------
# Punto 13: Multi-agente. Ogni agente ha un taglio diverso sullo stesso spunto.
# ---------------------------------------------------------------------------
AGENTS = {
    "business_expert": FOUNDER_PERSONA + """
Write as a fitness business expert: talk about margins, retention, filling classes,
gym/studio management, drop-in as an extra revenue stream.""",

    "fitness_expert": FOUNDER_PERSONA + """
Write as a fitness enthusiast: talk about training, disciplines, community,
motivation, no business talk.""",

    "startup_founder": FOUNDER_PERSONA + """
Write as the founder telling the story of building FlexDropin: bugs fixed,
hard decisions, numbers, lessons learned, doubts.""",

    "copywriter": FOUNDER_PERSONA + """
Write extremely concise, high-impact copy, optimized to spark discussion/replies,
hook-style opening in the first few words.""",

    "community_manager": FOUNDER_PERSONA + """
Write warm and conversational, like replying to a friend in the fitness community,
short sentences, light tone.""",
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
    def _complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 400,
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
                # gpt-oss-120b/20b sono modelli "reasoning": con max_tokens basso
                # esauriscono il budget nel ragionamento interno e tornano
                # content vuoto pur rispondendo 200 OK. reasoning_effort="low"
                # riduce i token spesi a "pensare" prima di scrivere la risposta.
                reasoning_effort="low",
            )
            content = (response.choices[0].message.content or "").strip()
            if not content:
                finish_reason = response.choices[0].finish_reason
                logger.warning(
                    f"⚠️ Groq ha risposto 200 OK ma content vuoto "
                    f"(finish_reason={finish_reason}, max_tokens={max_tokens}) - "
                    f"probabile budget esaurito nel reasoning interno"
                )
                return None
            return content
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
            avoid_block = "Topics already covered recently (do NOT repeat them, pick something else): " + \
                           "; ".join(recent_topics[:8])

        context_block = ""
        if event_context:
            context_block += f"\nRelevant fitness events happening now you could mention: {', '.join(event_context)}."
        if seasonal_context:
            context_block += f"\nSeasonal context: {seasonal_context}."

        link_instruction = (
            f"Naturally include the link {_get_link()} as a call-to-action."
            if include_link else
            "Do NOT include any link or explicit call to download the app: "
            "this is a value/content post, not promotional."
        )

        candidates = []
        for agent_name in agent_names:
            system_prompt = AGENTS[agent_name]
            user_prompt = f"""Write ONE tweet (max 280 characters) for the category "{category}".
{f'Topic/angle: {topic_hint}' if topic_hint else ''}
{avoid_block}
{context_block}
{link_instruction}

Reply ONLY with the tweet text, no quotes, no explanations."""

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
        prompt = f"""You are the editor of a fitness startup X account. Category: {category}.
Choose which of these tweets you would publish, considering naturalness, originality
and discussion potential:

{options_block}

Reply ONLY with the number of your choice (e.g: 1)."""

        choice = self._complete(
            "You are an experienced, rigorous, concise social media editor.",
            prompt, max_tokens=200, temperature=0.1
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

Write an X thread of {num_tweets} tweets about: "{topic}".
Suggested structure: 1) problem/hook, 2) data point or fact, 3) real case or example,
4) practical tip, 5) light call to action towards FlexDropin ({_get_link()}).

Reply ONLY with a JSON array of {num_tweets} strings, one per tweet, no visible numbering
in the text, no other explanations. Example: ["text1", "text2", ...]"""

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
Write a tweet (max 280 characters) about "{topic}" that OPENS with a direct
question to the reader. Reply only with the tweet text."""
        prompt_b = f"""{FOUNDER_PERSONA}
Write a tweet (max 280 characters) about "{topic}" that OPENS with a concrete
data point or fact (not a question). Reply only with the tweet text."""

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

Write a short, informal, imperfect tweet, the kind real founders write when telling
about a rough day or a funny moment in development (e.g. absurd bugs, wrong
expectations, small wins). No promotion, no link, no hashtags. Max 280 characters."""

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
            "- a quiet week of work, no big news"

        prompt = f"""{FOUNDER_PERSONA}

Write a weekly "build in public" recap tweet based on these real points:
{highlights_block}

Honest, concrete tone, not like a press release. Max 280 characters."""

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
            "You can mention FlexDropin naturally, without being pushy."
            if promotional else
            "Do NOT mention FlexDropin: the comment should add pure value to the conversation."
        )

        prompt = f"""{FOUNDER_PERSONA}

Read this tweet:
"{tweet_text}"

Write a short comment (max 200 characters), polite, that adds real value to
the conversation. {promo_line}

Reply ONLY with the comment text."""

        text = self._complete(FOUNDER_PERSONA, prompt, temperature=0.75)
        return self._truncate(text, 280) if text else None