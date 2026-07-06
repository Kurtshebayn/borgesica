"""Unit tests for borgesica.domain.prose.has_translatable_prose.

Root cause (job 34d5d0a7, per-paragraph mode, qwen3:14b): structural nodes
with no real prose — front-matter page lists like "i ii iii 1 2 3" — passed
the orchestrator's zero-alphabetic prose guard because roman numerals ARE
alphabetic. Small local models answer such fragments with meta-commentary
("No hay narrativa ni diálogo en este fragmento...") which then leaks into
the book as the "translation".

Contract: has_translatable_prose(text) returns False iff EVERY whitespace
token is non-prose — empty after removing non-alphanumerics, all digits, or
a STRICT roman numeral (grammar-valid, so "mild"/"civic" don't match). One
prose token anywhere → True (zero-false-positive bias: never silently skip
a translatable sentence).
"""

import pytest

from borgesica.domain.prose import has_translatable_prose


# --- non-prose: guard must catch these (False) ------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \n\t  ",
        "123",
        "1 2 3 4 5",
        "i ii iii iv v",
        "I II III IV V",
        "i ii iii 1 2 3 4 5 6",  # the real leaked front-matter shape
        "xiv",
        "MCMXCIV",
        "mix",  # 1009 — grammar-valid roman numeral
        "iv. v. vi.",  # trailing punctuation stripped per token
        "12,345",
        "1.5",
        "* * *",  # scene-break asterisks
        "— — —",
        "...",
        "7 — 12",
    ],
)
def test_non_prose_text_has_no_translatable_prose(text):
    assert has_translatable_prose(text) is False


# --- prose: guard must NEVER catch these (True) ------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Prologue",  # single-word heading — must be translated
        "Chapter 1",
        "mild",  # roman letters but grammar-invalid → word
        "civic",
        "did",
        "The journey begins here.",
        "i think so",  # 'think'/'so' are prose even though 'i' is roman
        "3rd",  # mixed alnum token is prose
        "OceanofPDF.com",
        "Capítulo XII",  # prose word + roman numeral
        "É",  # non-ASCII letter counts as prose
    ],
)
def test_prose_text_has_translatable_prose(text):
    assert has_translatable_prose(text) is True
