"""
Character loader (pattern "Eliza-style" da ai16z/eliza).

Prima: persona e agenti erano stringhe Python hardcoded in ai_generator.py
(FOUNDER_PERSONA, AGENTS). Ora tutto il contesto (bio, lore, knowledge,
stile, esempi di post, agenti, mapping categoria->agenti) vive in un unico
file dichiarativo: character.json, alla radice del repo.

Vantaggi pratici:
- Modificare la voce/persona del bot non richiede toccare Python.
- Un solo file da versionare/confrontare quando cambi tono o target.
- Stesso pattern usato dai character file di Eliza (ai16z), che separano
  esplicitamente bio/lore/knowledge/style/topics/adjectives dal codice
  che li usa.

Questo loader NON introduce dipendenze esterne: usa solo json della
standard library. Se character.json manca o è malformato, ai_generator.py
ricade su una persona minima di default (vedi _DEFAULT_CHARACTER più sotto),
così il bot continua a funzionare anche con un file corrotto.
"""
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

CHARACTER_PATH = Path(__file__).resolve().parent.parent / "character.json"

_DEFAULT_CHARACTER: Dict = {
    "name": "Floriano",
    "bio": ["Solo founder of FlexDropin, a drop-in fitness class booking app."],
    "lore": [],
    "knowledge": [],
    "topics": [],
    "adjectives": ["direct", "human"],
    "style": {"all": ["Always write in English (US)."], "post": [], "chat": []},
    "agents": {},
    "categoryAgents": {},
}

_cache: Optional[Dict] = None


def load_character(force_reload: bool = False) -> Dict:
    """Carica character.json (con cache in-process). force_reload=True per
    ricaricare da disco senza riavviare il processo (utile in sviluppo)."""
    global _cache
    if _cache is not None and not force_reload:
        return _cache

    if not CHARACTER_PATH.exists():
        logger.warning(f"⚠️ {CHARACTER_PATH} non trovato, uso persona di default minima")
        _cache = _DEFAULT_CHARACTER
        return _cache

    try:
        with open(CHARACTER_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # merge leggero coi default, per non rompere se manca una chiave
        merged = {**_DEFAULT_CHARACTER, **data}
        _cache = merged
        return merged
    except Exception as e:
        logger.error(f"❌ Errore nel parsing di character.json: {e} - uso persona di default")
        _cache = _DEFAULT_CHARACTER
        return _cache


def _bullet_block(title: str, items: List[str]) -> str:
    if not items:
        return ""
    lines = "\n".join(f"- {item}" for item in items)
    return f"\n{title}:\n{lines}"


def build_persona(character: Optional[Dict] = None) -> str:
    """Ricostruisce l'equivalente di FOUNDER_PERSONA a partire dal character file."""
    c = character or load_character()

    name = c.get("name", "the founder")
    bio = " ".join(c.get("bio", []))
    adjectives = ", ".join(c.get("adjectives", []))

    parts = [f"You are {name}. {bio}".strip()]
    if adjectives:
        parts.append(f"Tone: {adjectives}.")

    parts.append(_bullet_block("Background", c.get("lore", [])))
    parts.append(_bullet_block("Relevant knowledge", c.get("knowledge", [])))
    parts.append(_bullet_block("Topics you can write about", c.get("topics", [])))
    parts.append(_bullet_block("Style rules (always apply)", c.get("style", {}).get("all", [])))
    parts.append(_bullet_block("Style rules for posts", c.get("style", {}).get("post", [])))

    return "\n".join(p for p in parts if p).strip()


def build_agent_persona(agent_name: str, character: Optional[Dict] = None) -> str:
    """Persona base + stile specifico di un agente (es. business_expert)."""
    c = character or load_character()
    base = build_persona(c)
    agent = c.get("agents", {}).get(agent_name, {})
    style = agent.get("style", [])
    focus = agent.get("focus", "")

    extra = ""
    if focus:
        extra += f"\nWrite as: {focus}."
    extra += _bullet_block("Specific instructions for this angle", style)

    return (base + "\n" + extra).strip()


def get_agent_names(character: Optional[Dict] = None) -> List[str]:
    c = character or load_character()
    return list(c.get("agents", {}).keys())


def get_category_agents(category: str, character: Optional[Dict] = None,
                         default: Optional[List[str]] = None) -> List[str]:
    c = character or load_character()
    mapping = c.get("categoryAgents", {})
    return mapping.get(category, default or ["startup_founder"])


def get_post_examples(character: Optional[Dict] = None) -> List[str]:
    c = character or load_character()
    return c.get("postExamples", [])
