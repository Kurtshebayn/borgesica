"""Output-language validation for the orchestrator's translation loop.

A model can return a chunk that is structurally perfect — tag counts intact,
"\\n\\n" segment counts aligned — and yet written in the wrong language. That
happened in production: one chunk of a 502-chunk book came back in Chinese and
shipped, because every validator in the pipeline checks structure and none
checks language.

The check is deterministic and local: no provider call, no model, no
dependency. It reasons over Unicode script ranges, which is enough to catch a
whole passage swapped into another writing system — the failure that actually
occurs. It deliberately does NOT try to tell Spanish from English: both are
Latin script, and a guard that fired on Latin text would fire on every proper
noun, title, and quotation in the book.

Bias, mirroring the prose guard: no false positives. A short quotation in
another script is legitimate content, not a mistranslation, so detection
requires a passage-sized amount of foreign script rather than a single
character.
"""

from __future__ import annotations

import re

# Scripts that cannot plausibly appear as incidental content in a Spanish
# translation. Greek is deliberately EXCLUDED: technical and mathematical prose
# uses alpha/beta/pi as symbols, and flagging it would fail legitimate chunks.
# Each entry is (script name, list of inclusive codepoint ranges).
_FOREIGN_SCRIPTS: tuple[tuple[str, tuple[tuple[int, int], ...]], ...] = (
    (
        "CJK",
        (
            (0x3040, 0x309F),  # Hiragana
            (0x30A0, 0x30FF),  # Katakana
            (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
            (0x4E00, 0x9FFF),  # CJK Unified Ideographs
            (0xAC00, 0xD7AF),  # Hangul Syllables
            (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
        ),
    ),
    ("Cyrillic", ((0x0400, 0x04FF), (0x0500, 0x052F))),
    ("Arabic", ((0x0600, 0x06FF), (0x0750, 0x077F))),
    ("Hebrew", ((0x0590, 0x05FF),)),
    ("Devanagari", ((0x0900, 0x097F),)),
    ("Thai", ((0x0E00, 0x0E7F),)),
)

# A passage, not a word. Both thresholds must be met: the absolute floor keeps
# an inline quotation ("你好") from failing a chunk, and the share keeps a long
# Spanish chunk from being condemned by a handful of stray characters.
_MIN_FOREIGN_CHARS = 12
_MIN_FOREIGN_SHARE = 0.10

# Target languages this guard understands. A non-Latin target would make every
# correct translation look foreign, so the check disables itself instead.
_LATIN_SCRIPT_TARGETS = ("es", "en", "pt", "fr", "it", "ca", "gl", "de")


def _script_of(char: str) -> str | None:
    """Return the flagged script *char* belongs to, or None."""
    code = ord(char)
    for name, ranges in _FOREIGN_SCRIPTS:
        for low, high in ranges:
            if low <= code <= high:
                return name
    return None


def detect_unexpected_script(text: str, expected_target: str) -> str | None:
    """Return the name of a foreign script dominating *text*, or None if clean.

    Args:
        text:            The translated output to inspect.
        expected_target: JobConfig.target_lang (e.g. "es-neutral").

    Returns:
        The script name (e.g. "CJK") when *text* carries a passage-sized amount
        of a writing system that cannot belong in a Latin-script translation;
        None when the text is clean, empty, or the target is not Latin-script.
    """
    if not text or not text.strip():
        return None
    if not expected_target.lower().startswith(_LATIN_SCRIPT_TARGETS):
        return None

    counts: dict[str, int] = {}
    letters = 0
    for char in text:
        if not char.isalpha():
            # Digits, punctuation, whitespace, emoji and maths symbols carry no
            # language — counting them would dilute the share and hide a real
            # foreign passage inside a heavily punctuated chunk.
            continue
        letters += 1
        script = _script_of(char)
        if script is not None:
            counts[script] = counts.get(script, 0) + 1

    if not counts or letters == 0:
        return None

    script, count = max(counts.items(), key=lambda kv: kv[1])
    if count >= _MIN_FOREIGN_CHARS and count / letters >= _MIN_FOREIGN_SHARE:
        return script
    return None


# ---------------------------------------------------------------------------
# JobConfig.target_lang -> BCP 47 language code, for writers that stamp the
# OUTPUT language (e.g. EPUB dc:language). No such mapping existed anywhere
# in the codebase before this — target_lang is a free-form string ("en",
# "pt", the project's own "es-neutral" convention, ...), never a validated
# code, so a writer that wants a real BCP 47 tag must resolve one itself.
#
# Every target_lang this codebase currently issues (see _LATIN_SCRIPT_TARGETS
# above and JobConfig.target_lang's default) uses a bare 2-letter ISO 639-1
# primary subtag, optionally followed by a project-specific suffix ("-neutral")
# that is NOT a registered BCP 47 subtag. Validating against exactly that
# shape — 2 alphabetic characters before the first "-" or "_" — accepts every
# real code this project uses while rejecting garbage (empty, digits, a
# single letter, or a multi-word non-code string) rather than fabricating a
# plausible-looking but wrong language declaration.
# ---------------------------------------------------------------------------

_PRIMARY_SUBTAG_RE = re.compile(r"^[A-Za-z]{2}$")


def resolve_bcp47(target_lang: str) -> str | None:
    """Resolve *target_lang* to a valid BCP 47 language code, or None.

    Args:
        target_lang: JobConfig.target_lang (e.g. "es-neutral", "en", "pt-BR").

    Returns:
        The lowercased 2-letter primary language subtag (e.g. "es") when
        *target_lang* starts with one, else None — callers MUST leave any
        existing language declaration untouched on None rather than write an
        unresolvable value.
    """
    if not target_lang:
        return None
    primary = target_lang.strip().split("-", 1)[0].split("_", 1)[0]
    if _PRIMARY_SUBTAG_RE.fullmatch(primary):
        return primary.lower()
    return None
