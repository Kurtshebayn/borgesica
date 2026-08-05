"""Tests for output-language validation (backlog A2).

Strict TDD: written before the implementation.

Motivating incident: a 502-chunk DeepSeek run of a real book emitted one
chunk translated into Chinese in the middle of the Spanish output. Nothing
in the pipeline noticed — the only validators are structural (tag counts and
"\\n\\n" segment counts), so a perfectly well-formed chunk in the wrong
language passes every check and ships.

The guard's bias mirrors the prose guard's: no false positives. A stray
character or a short legitimate quotation must never fail a chunk.
"""
from __future__ import annotations

import pytest

from borgesica.domain.language import detect_unexpected_script

SPANISH = (
    "El niño corrió hacia la montaña mientras el señor Muñoz observaba "
    "en silencio. ¿Qué más podía hacer? Nada, salvo esperar la señal."
)


# ---------------------------------------------------------------------------
# No false positives
# ---------------------------------------------------------------------------


def test_plain_spanish_is_clean() -> None:
    assert detect_unexpected_script(SPANISH, "es-neutral") is None


def test_accents_tildes_and_inverted_punctuation_are_not_foreign() -> None:
    """Latin-1 supplement characters are the TARGET language, not an anomaly."""
    text = "¡Qué día! La cigüeña voló sobre el árbol de aguacate: ñandú, ümlaut."
    assert detect_unexpected_script(text, "es-neutral") is None


def test_emoji_and_symbols_are_not_scripts() -> None:
    """Arrows, maths and emoji carry no language — they must not trip the guard."""
    text = "El resultado fue 3 → 5 ± 2 ≈ 6 🎉 y todos celebraron el hallazgo."
    assert detect_unexpected_script(text, "es-neutral") is None


def test_empty_and_whitespace_are_clean() -> None:
    assert detect_unexpected_script("", "es-neutral") is None
    assert detect_unexpected_script("   \n\t ", "es-neutral") is None


def test_a_short_quoted_foreign_word_does_not_trip_the_guard() -> None:
    """A legitimate inline quotation is not a mistranslation."""
    text = SPANISH + ' El cartel decía "你好" y siguió caminando por la calle.'
    assert detect_unexpected_script(text, "es-neutral") is None


def test_english_source_leaking_through_is_not_this_guard_s_job() -> None:
    """Untranslated English is Latin script — a different problem, not ours.

    Flagging it here would make the guard fire on every proper noun.
    """
    assert detect_unexpected_script("The quick brown fox jumps.", "es-neutral") is None


# ---------------------------------------------------------------------------
# Real detections
# ---------------------------------------------------------------------------


def test_a_chunk_translated_into_chinese_is_detected() -> None:
    """The actual incident: a whole passage in the wrong language."""
    text = "这是一个完全用中文写的段落，它本应该是西班牙语的翻译，但是模型换了语言。"
    assert detect_unexpected_script(text, "es-neutral") == "CJK"


def test_a_chinese_passage_embedded_in_spanish_is_detected() -> None:
    """The incident's real shape: mostly Spanish, one passage swapped."""
    text = SPANISH + " 这是一个完全用中文写的段落，它本应该是西班牙语的翻译。"
    assert detect_unexpected_script(text, "es-neutral") == "CJK"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Привет, это совершенно русский текст, а не испанский перевод.", "Cyrillic"),
        ("これは完全に日本語で書かれた段落であり、スペイン語ではありません。", "CJK"),
        ("이것은 스페인어가 아니라 완전히 한국어로 작성된 단락입니다.", "CJK"),
        ("هذه فقرة مكتوبة بالكامل باللغة العربية وليست ترجمة إسبانية.", "Arabic"),
    ],
)
def test_other_scripts_are_detected(text: str, expected: str) -> None:
    assert detect_unexpected_script(text, expected_target="es-neutral") == expected


# ---------------------------------------------------------------------------
# Target-language guard
# ---------------------------------------------------------------------------


def test_non_latin_target_disables_the_check() -> None:
    """Translating INTO Chinese must not flag Chinese output.

    borgesica targets neutral Spanish today, but target_lang is configurable
    and this guard must not silently break a non-Latin target.
    """
    text = "这是一个完全用中文写的段落，它本应该是西班牙语的翻译，但是模型换了语言。"
    assert detect_unexpected_script(text, "zh") is None
