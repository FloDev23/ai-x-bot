import json
import logging
from types import SimpleNamespace

import pytest

from modules.ai_generator import AIGenerator
from modules.database import Database


class FakeTranslatorGenerator:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def translate_review_copy(self, english_text):
        self.calls.append(english_text)
        if self.error is not None:
            raise self.error
        return self.response


def _translate(response, english):
    from modules.review_translation import ReviewTranslator

    generator = FakeTranslatorGenerator(response)
    return ReviewTranslator(generator).translate(english), generator


def test_translation_preserves_numbers_ranges_scales_and_urls():
    from modules.review_translation import ReviewTranslation

    result, generator = _translate(
        "I ricavi sono aumentati del 15% da 81M a 100M. "
        "https://flexdropin.com/blog/x",
        "Revenue rose 15% from 81M to 100M. "
        "https://flexdropin.com/blog/x",
    )

    assert result == ReviewTranslation(
        "I ricavi sono aumentati del 15% da 81M a 100M. "
        "https://flexdropin.com/blog/x"
    )
    assert generator.calls == [
        "Revenue rose 15% from 81M to 100M. "
        "https://flexdropin.com/blog/x"
    ]


def test_translation_accepts_equivalent_italian_numeric_words():
    result, _generator = _translate(
        "Il report cita 81 milioni, 20 mila, 3 miliardi e 5,2 percento.",
        "The report cites 81M, 20K, 3B and 5.2%.",
    )

    assert result is not None


@pytest.mark.parametrize(
    ("english", "italian"),
    (
        ("Revenue fell -5.2%.", "I ricavi sono saliti +5,2%."),
        ("Revenue fell −5.2%.", "I ricavi sono scesi 5,2%."),
        ("The range was 10-15%.", "L'intervallo era 10-16%."),
        ("Two cohorts reached 15% and 15%.", "Una coorte ha raggiunto il 15%."),
        ("Athletes ran 20 m.", "Gli iscritti erano 20M."),
        ("The rate was 15%.", "Il tasso era 15."),
    ),
)
def test_translation_rejects_changed_numeric_meaning(english, italian):
    result, _generator = _translate(italian, english)

    assert result is None


def test_translation_accepts_equivalent_unicode_minus_and_range_forms():
    result, _generator = _translate(
        "Il calo è stato -5,2 percento, in un intervallo tra 10% e 15%.",
        "The decline was −5.2%, in a 10–15% range.",
    )

    assert result is not None


@pytest.mark.parametrize(
    "italian",
    (
        "Leggi https://flexdropin.com/blog/b invece di https://flexdropin.com/blog/a",
        "Leggi https://flexdropin.com/blog/a e https://example.com/extra",
        "Leggi il nostro articolo.",
        "Prima https://flexdropin.com/b poi https://flexdropin.com/a",
    ),
)
def test_translation_rejects_changed_missing_extra_or_reordered_urls(italian):
    english = (
        "First https://flexdropin.com/a then https://flexdropin.com/b"
        if "/b" in italian
        else "Read https://flexdropin.com/blog/a"
    )
    result, _generator = _translate(italian, english)

    assert result is None


@pytest.mark.parametrize(
    "response",
    (
        None,
        17,
        "",
        "   ",
        "```italian\nTesto.\n```",
        '"Testo racchiuso tra virgolette."',
        "x" * 1001,
        "Testo con surrogate \ud800.",
    ),
)
def test_translation_rejects_malformed_or_unbounded_output(response):
    result, generator = _translate(response, "A useful post.")

    assert result is None
    assert generator.calls == ["A useful post."]


def test_translation_strips_only_outer_whitespace():
    from modules.review_translation import ReviewTranslation

    result, _generator = _translate("  Testo fedele.\n", "Faithful copy.")

    assert result == ReviewTranslation("Testo fedele.")


def test_translation_preserves_hashtags_exactly_and_in_order():
    accepted, _generator = _translate(
        "Consiglio per #GymOwners e #BoutiqueFitness.",
        "Advice for #GymOwners and #BoutiqueFitness.",
    )
    changed, _generator = _translate(
        "Consiglio per #Palestre e #BoutiqueFitness.",
        "Advice for #GymOwners and #BoutiqueFitness.",
    )

    assert accepted is not None
    assert changed is None


def test_translation_rejects_unbounded_input_before_provider_call():
    from modules.review_translation import ReviewTranslator

    generator = FakeTranslatorGenerator("Traduzione")
    assert ReviewTranslator(generator).translate("x" * 1001) is None
    assert generator.calls == []


def test_public_numeric_helpers_keep_signed_bilingual_unit_boundaries():
    from modules.fact_guard import numeric_occurrences, numeric_tokens, _numeric_tokens

    assert numeric_tokens("81 milioni, 20 mila, 3 miliardi, 5,2 percento") == {
        "81m", "20k", "3b", "5.2%",
    }
    assert numeric_tokens("20 m sprint") == {"20"}
    assert numeric_tokens("20M members") == {"20m"}
    assert numeric_occurrences("-5%, 15%, 15%") == ("-5%", "15%", "15%")
    assert _numeric_tokens is numeric_tokens


def test_ai_translation_uses_one_bounded_json_data_call():
    english_sentinel = "UNTRUSTED_TRANSLATION_INPUT_59271 15%"
    calls = []
    generator = AIGenerator.__new__(AIGenerator)

    def complete(system_prompt, user_prompt, **kwargs):
        calls.append((system_prompt, user_prompt, kwargs))
        return "INPUT DI TRADUZIONE NON ATTENDIBILE 59271 15%"

    generator._complete = complete
    result = generator.translate_review_copy(english_sentinel)

    assert result == "INPUT DI TRADUZIONE NON ATTENDIBILE 59271 15%"
    assert len(calls) == 1
    system_prompt, user_prompt, kwargs = calls[0]
    assert english_sentinel not in system_prompt
    assert json.loads(user_prompt) == {"english_tweet": english_sentinel}
    assert kwargs == {"max_tokens": 500, "temperature": 0.1}
    assert "untrusted data" in system_prompt.lower()


def test_translation_failure_does_not_log_or_persist_raw_payloads(
    tmp_path,
    caplog,
):
    from modules.review_translation import ReviewTranslator

    input_sentinel = "RAW_ENGLISH_TRANSLATION_71829"
    error_sentinel = "PROVIDER_TRANSLATION_SECRET_93014"
    response_sentinel = "RAW_PROVIDER_REASONING_44170"
    db_path = tmp_path / "translation.db"
    Database(str(db_path))
    translator = ReviewTranslator(FakeTranslatorGenerator(
        response=response_sentinel,
        error=RuntimeError(error_sentinel),
    ))

    with caplog.at_level(logging.ERROR):
        assert translator.translate(input_sentinel) is None

    combined_logs = "\n".join(record.getMessage() for record in caplog.records)
    stored = db_path.read_bytes()
    for sentinel in (input_sentinel, error_sentinel, response_sentinel):
        assert sentinel not in combined_logs
        assert sentinel.encode() not in stored


def test_ai_completion_exception_is_sanitized_for_translation(caplog):
    secret = "GROQ_TRANSLATION_SECRET_89311"

    class RaisingCompletions:
        def create(self, **_kwargs):
            raise RuntimeError(secret)

    generator = AIGenerator.__new__(AIGenerator)
    generator.model = "fake-model"
    generator.client = SimpleNamespace(
        chat=SimpleNamespace(completions=RaisingCompletions())
    )

    with caplog.at_level(logging.ERROR):
        assert generator.translate_review_copy("Translate this.") is None

    assert secret not in "\n".join(record.getMessage() for record in caplog.records)


def test_empty_provider_finish_reason_is_not_logged(caplog):
    secret = "RAW_FINISH_REASON_SECRET_77531"

    class EmptyCompletions:
        def create(self, **_kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=""),
                finish_reason=secret,
            )])

    generator = AIGenerator.__new__(AIGenerator)
    generator.model = "fake-model"
    generator.client = SimpleNamespace(
        chat=SimpleNamespace(completions=EmptyCompletions())
    )

    with caplog.at_level(logging.WARNING):
        assert generator.translate_review_copy("Translate this.") is None

    assert secret not in "\n".join(record.getMessage() for record in caplog.records)
