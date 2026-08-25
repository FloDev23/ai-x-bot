"""Faithful Italian review translations for canonical English X copy."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Optional

from modules.fact_guard import numeric_occurrences, numeric_tokens


logger = logging.getLogger(__name__)

_URL_PATTERN = re.compile(r"https?://[^\s<>\[\]{}\"']+")
_HASHTAG_PATTERN = re.compile(r"(?<!\w)#[^\W#]+", re.UNICODE)
_TRAILING_URL_PUNCTUATION = ".,!?;:)"
_MAX_TRANSLATION_CHARACTERS = 1000
_MAX_TRANSLATION_BYTES = 4000


@dataclass(frozen=True)
class ReviewTranslation:
    text_it: str


def _ordered_urls(value: str) -> tuple[str, ...]:
    return tuple(
        match.group(0).rstrip(_TRAILING_URL_PUNCTUATION)
        for match in _URL_PATTERN.finditer(value)
    )


def _ordered_hashtags(value: str) -> tuple[str, ...]:
    return tuple(match.group(0) for match in _HASHTAG_PATTERN.finditer(value))


def _has_outer_quote_wrapper(value: str) -> bool:
    return any(
        value.startswith(opening) and value.endswith(closing)
        for opening, closing in (("\"", "\""), ("'", "'"), ("“", "”"), ("‘", "’"))
    )


def _valid_utf8(value: str) -> bool:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return True


class ReviewTranslator:
    """Validate a one-call provider translation against immutable facts."""

    def __init__(self, generator):
        self.generator = generator

    def translate(self, english_text: str) -> Optional[ReviewTranslation]:
        if (
            type(english_text) is not str
            or not english_text.strip()
            or len(english_text) > _MAX_TRANSLATION_CHARACTERS
            or not _valid_utf8(english_text)
            or len(english_text.encode("utf-8")) > _MAX_TRANSLATION_BYTES
        ):
            return None
        try:
            translated = self.generator.translate_review_copy(english_text)
        except Exception as error:
            logger.error(
                "review_translation_failed error_type=%s",
                type(error).__name__,
            )
            return None
        if type(translated) is not str:
            return None
        return self.validate(english_text, translated.strip())

    def validate(
        self,
        english_text: str,
        italian_text: str,
    ) -> Optional[ReviewTranslation]:
        """Validate operator-supplied Italian copy without calling a provider."""
        if (
            type(english_text) is not str
            or not english_text.strip()
            or len(english_text) > _MAX_TRANSLATION_CHARACTERS
            or not _valid_utf8(english_text)
            or len(english_text.encode("utf-8")) > _MAX_TRANSLATION_BYTES
            or type(italian_text) is not str
        ):
            return None
        text_it = italian_text
        if (
            not text_it
            or text_it != text_it.strip()
            or len(text_it) > _MAX_TRANSLATION_CHARACTERS
            or not _valid_utf8(text_it)
            or len(text_it.encode("utf-8")) > _MAX_TRANSLATION_BYTES
            or "```" in text_it
            or _has_outer_quote_wrapper(text_it)
        ):
            return None
        if _ordered_urls(english_text) != _ordered_urls(text_it):
            return None
        if _ordered_hashtags(english_text) != _ordered_hashtags(text_it):
            return None
        if numeric_tokens(english_text) != numeric_tokens(text_it):
            return None
        if numeric_occurrences(english_text) != numeric_occurrences(text_it):
            return None
        return ReviewTranslation(text_it=text_it)
