"""Editorial quality scoring and deterministic duplicate detection."""
import json
import logging
import math
import re
from collections import Counter
from typing import Dict, Optional
from groq import Groq

logger = logging.getLogger(__name__)

SCORE_AXES = [
    "hook",
    "usefulness",
    "specificity",
    "originality",
    "audience_relevance",
    "follow_worthiness",
    "semantic_novelty",
]

_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "for",
    "from", "in", "is", "it", "of", "on", "or", "that", "the", "this",
    "to", "with", "you", "your",
}
_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10",
}


def _singularize(token: str) -> str:
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith(("sses", "ches", "shes", "xes", "zes")):
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss") and len(token) > 3:
        return token[:-1]
    return token


def _normalized_tokens(text: str):
    tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
    normalized = []
    for token in tokens:
        token = _NUMBER_WORDS.get(token, token)
        if token in _STOP_WORDS:
            continue
        normalized.append(_singularize(token))
    return normalized


def semantic_similarity(left: str, right: str) -> float:
    """Cosine similarity over normalized English token frequencies."""
    left_counts = Counter(_normalized_tokens(left))
    right_counts = Counter(_normalized_tokens(right))
    if left_counts == right_counts:
        return 1.0
    if not left_counts or not right_counts:
        return 0.0
    dot_product = sum(
        count * right_counts.get(token, 0)
        for token, count in left_counts.items()
    )
    left_norm = math.sqrt(sum(count * count for count in left_counts.values()))
    right_norm = math.sqrt(sum(count * count for count in right_counts.values()))
    return dot_product / (left_norm * right_norm)


class TweetScorer:
    def __init__(self, groq_client: Groq, model: str):
        self.client = groq_client
        self.model = model

    def score_draft(self, tweet_text: str) -> Optional[Dict]:
        """Return normalized editorial scores, or ``None`` on any failure."""
        prompt = f"""Evaluate this draft for the FlexDropin X account:

"{tweet_text}"

The audience is gym owners and boutique fitness operators. Score each axis
from 0 to 10: {", ".join(SCORE_AXES)}.

Reply ONLY with one JSON object containing every named axis."""

        try:
            response = self.client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model=self.model,
                max_tokens=500,
                temperature=0.2,
                reasoning_effort="low",
            )
            raw = (response.choices[0].message.content or '').strip()
            raw = raw.replace('```json', '').replace('```', '').strip()
            data = json.loads(raw)
            if not isinstance(data, dict) or any(axis not in data for axis in SCORE_AXES):
                return None
            scores = {}
            for axis in SCORE_AXES:
                value = data[axis]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return None
                if not 0 <= value <= 10:
                    return None
                scores[axis] = value
            scores['total'] = round(sum(scores.values()) * 100 / 70)
            return scores

        except Exception as e:
            logger.warning(f"Editorial scoring unavailable: {e}")
            return None
