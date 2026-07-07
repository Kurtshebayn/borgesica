"""Prose detection for the orchestrator's passthrough guard.

Structural nodes with no translatable prose — front-matter page lists like
"i ii iii 1 2 3", scene-break asterisks, bare numbers — must never reach the
LLM: small local models answer such fragments with meta-commentary instead
of a translation, and that commentary leaks into the book. The guard's bias
is zero false positives: one prose token anywhere makes the whole text
translatable, so no real sentence is ever silently skipped.
"""

import re

# Strict roman-numeral grammar (1–4999). Grammar-valid only, so ordinary
# words made of roman letters ("mild", "civic", "did") do NOT match, while
# genuine numerals — including oddballs like "mix" (1009) — do. The
# lookahead rejects the empty string the optional groups would otherwise
# accept.
_ROMAN_NUMERAL = re.compile(
    r"^(?=[mdclxvi])m{0,4}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$",
    re.IGNORECASE,
)

_NON_ALNUM = re.compile(r"[\W_]+", re.UNICODE)


def _is_prose_token(token: str) -> bool:
    """A token is prose unless, reduced to its alphanumerics, it is empty
    (pure punctuation), all digits, or a strict roman numeral."""
    core = _NON_ALNUM.sub("", token)
    if not core:
        return False
    if core.isdigit():
        return False
    if _ROMAN_NUMERAL.match(core):
        return False
    return True


def has_translatable_prose(text: str) -> bool:
    """Return True iff *text* contains at least one prose token.

    Callers strip markup first (:func:`borgesica.domain.markup.strip_all_tags`);
    this function reasons over plain text only. Empty/whitespace-only text has
    no prose.
    """
    return any(_is_prose_token(token) for token in text.split())
