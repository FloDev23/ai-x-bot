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
- v3.1 (pattern "character file" alla Eliza/ai16z): persona, agenti, knowledge
  e mapping categoria->agenti non sono più stringhe hardcoded qui, ma vivono
  in character.json (root del repo) e vengono costruiti da modules/character.py.
  Cambiare tono/persona = modificare character.json, non questo file.

NOTA STRATEGICA: su richiesta di Floriano, X è ora dedicato al mercato
internazionale (gestori di palestre/boutique studio fuori dall'Italia).
FlexDropin gestisce IVA condizionale e UI IT/EN a seconda del paese della
palestra, quindi i contenuti X sono generati in INGLESE. Il mercato italiano
resta presidiato via Instagram e visite di persona (fuori da questo bot).
"""
import logging
import json
from groq import Groq
from config import GROQ_API_KEY, GROQ_MODEL, GROQ_VISION_MODEL, FLEXDROPIN_PLAY_STORE, FLEXDROPIN_APP_STORE, FLEXDROPIN_WEBSITE
from modules import character as character_module
from typing import Dict, Optional, List, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Persona e agenti ora vengono costruiti da character.json (vedi modules/character.py).
# FOUNDER_PERSONA resta come nome per non rompere il resto del file, ma il suo
# contenuto è generato dinamicamente dal character file invece di essere hardcoded.
# ---------------------------------------------------------------------------
_CHARACTER = character_module.load_character()
FOUNDER_PERSONA = character_module.build_persona(_CHARACTER)


def _agent_prompt(agent_name: str) -> str:
    """Persona + stile specifico di un agente, letto da character.json"""
    return character_module.build_agent_persona(agent_name, _CHARACTER)


def _category_agents(category: str) -> List[str]:
    """Agenti da usare per una categoria del palinsesto, letto da character.json"""
    return character_module.get_category_agents(category, _CHARACTER)


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
                        seasonal_context: Optional[str] = None,
                        media_description: Optional[str] = None) -> Optional[Dict]:
        """
        Genera un tweet per una categoria del palinsesto, usando 1-2 agenti e
        scegliendo il migliore. Ritorna un dict {text, agent_used} o None.

        Se media_description è passato (perché è già stato scelto un media
        dalla libreria per questo post), il testo viene scritto per
        accompagnare quell'immagine/video in modo naturale, non a caso.
        """
        agent_names = _category_agents(category)
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

        media_block = ""
        if media_description:
            media_block = (
                f"\nThis tweet will be posted together with an image/video showing: "
                f"\"{media_description}\". Write the text so it pairs naturally with what's "
                f"shown (reference or build on it, don't just caption it literally)."
            )

        link_instruction = (
            f"Naturally include the link {_get_link()} as a call-to-action."
            if include_link else
            "Do NOT include any link or explicit call to download the app: "
            "this is a value/content post, not promotional."
        )

        candidates = []
        for agent_name in agent_names:
            system_prompt = _agent_prompt(agent_name)
            user_prompt = f"""Write ONE tweet (max 280 characters) for the category "{category}".
{f'Topic/angle: {topic_hint}' if topic_hint else ''}
{avoid_block}
{context_block}
{media_block}
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

IMPORTANT: only reference facts/details actually stated in the tweet. Do
NOT invent or assume specifics the person didn't write (e.g. don't claim
they mentioned a specific problem if they didn't). If the tweet itself is
vague, keep your comment equally general instead of making things up.

Reply ONLY with the comment text."""

        text = self._complete(FOUNDER_PERSONA, prompt, temperature=0.75)
        return self._truncate(text, 280) if text else None

    def generate_lead_dm(self, tweet_text: str) -> Optional[str]:
        """
        Genera una bozza di DM diretto per un lead commerciale reale (punto 19
        di lead_finder.py): breve, personale, menziona FlexDropin in modo
        naturale legato al problema espresso nel tweet. Il bot NON invia mai
        questo DM da solo: è solo una bozza pronta da rivedere e copiare.
        """
        prompt = f"""{FOUNDER_PERSONA}

A potential customer wrote this on X:
"{tweet_text}"

Write a short, friendly direct message (max 300 characters) that acknowledges
their specific situation and introduces FlexDropin as a possible solution,
with one clear soft call to action (e.g. "want me to show you how it works?").
Personal tone, not salesy, no hard pitch.

CRITICAL: only reference what the tweet ACTUALLY says. Do not invent details,
problems, or context the person didn't write (no fabricated specifics like
"empty class spots" or "extra admin work" unless those exact concerns are in
the tweet). If the tweet doesn't give you much to work with, write a shorter,
more general opener instead of making up a backstory.

Reply ONLY with the DM text."""

        text = self._complete(FOUNDER_PERSONA, prompt, temperature=0.7)
        return self._truncate(text, 500) if text else None

    def analyze_image(self, image_path: str) -> Optional[Dict]:
        """
        Analizza un'immagine (o un frame estratto da un video) per la
        libreria media: descrizione, categoria suggerita, tag e una bozza
        di didascalia in italiano nel tono di Floriano. Usa un modello Groq
        con supporto vision (vedi GROQ_VISION_MODEL in config.py).

        Ritorna None se l'analisi fallisce: il file viene comunque
        registrato nella libreria (categoria 'other', da rivedere a mano
        nella dashboard) invece di bloccare l'upload.
        """
        import base64
        import json

        try:
            with open(image_path, 'rb') as f:
                b64 = base64.b64encode(f.read()).decode('utf-8')
            ext = image_path.rsplit('.', 1)[-1].lower()
            mime = 'image/png' if ext == 'png' else 'image/jpeg'

            prompt = """Analyze this image for a FlexDropin social media post
(FlexDropin is a drop-in fitness class booking app for gyms and studios).

Reply ONLY with a JSON object, no other text, no markdown code fences,
with exactly this structure:
{"description": "1-2 sentences in English describing what's in the image",
 "category": "gym_visit|app_demo|behind_scenes|community|other",
 "tags": ["tag1", "tag2", "tag3"],
 "caption_it": "a short caption in Italian, direct and self-deprecating tone, suitable to accompany this image in a post"}

Pick "category" as the single best fit from the list."""

            response = self.client.chat.completions.create(
                model=GROQ_VISION_MODEL,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ],
                }],
                max_tokens=700,
                temperature=0.4,
                # Qwen 3.6 27B è un modello "reasoning": di default entra in
                # thinking mode e può consumare tutto il budget di token nel
                # ragionamento interno, lasciando vuota la risposta finale.
                # reasoning_effort="none" disattiva il thinking mode per
                # avere direttamente l'output JSON richiesto.
                reasoning_effort="none",
            )
            raw = (response.choices[0].message.content or '').strip()
            if not raw:
                finish_reason = response.choices[0].finish_reason
                logger.error(f"❌ Risposta vuota dal modello vision (finish_reason: {finish_reason})")
                return None
            raw = raw.replace('```json', '').replace('```', '').strip()
            return json.loads(raw)
        except Exception as e:
            logger.error(f"❌ Errore analisi immagine ({image_path}): {e}")
            return None

    def select_best_media(self, category: str, topic_hint: str,
                           candidates: List[Dict]) -> Optional[int]:
        """
        Sceglie, tra i media non ancora usati, quello più adatto al post di
        OGGI in base al contenuto (categoria + argomento) — non il più
        vecchio. Ragiona sulle descrizioni già prodotte dall'analisi vision
        al momento dell'upload, non rianalizza le immagini.

        Ritorna l'id del media scelto, o None se nessuno è genuinamente
        adatto: in quel caso il post resta solo testo, non forziamo mai un
        abbinamento casuale pur di allegare qualcosa.
        """
        if not candidates:
            return None

        options_block = "\n".join(
            f'- id {c["id"]}: type={c["media_type"]}, category={c["category"]}, '
            f'description="{c["ai_description"] or "n/a"}", tags={c["ai_tags"] or "n/a"}'
            for c in candidates
        )

        prompt = f"""You're picking which photo/video should accompany a social media post
for FlexDropin (a drop-in fitness class booking app).

Today's post:
- category: "{category}"
- topic/angle: "{topic_hint or 'general'}"

Available unused media in the library:
{options_block}

Pick the id of the single best-matching media for THIS post's topic, based
on its description/tags. If nothing genuinely fits well, don't force it.

Reply ONLY with a JSON object, no other text: {{"media_id": <id or null>}}"""

        raw = self._complete(FOUNDER_PERSONA, prompt, max_tokens=100, temperature=0.2)
        if not raw:
            return None
        try:
            raw_clean = raw.strip().replace('```json', '').replace('```', '').strip()
            data = json.loads(raw_clean)
            media_id = data.get('media_id')
            return int(media_id) if media_id else None
        except Exception as e:
            logger.warning(f"⚠️ Impossibile interpretare la scelta media dell'AI (raw: {raw[:150]}): {e}")
            return None