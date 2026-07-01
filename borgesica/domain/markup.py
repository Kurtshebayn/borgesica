"""Markup module — inline tag strip / reinsert / validate.

Pure domain module: stdlib only, no I/O, no external dependencies.

Supported tags: <i>, <b>, <u>, <em>, <strong>, <span ...>, <a ...> and their
closing forms.

Design:
  strip(text)
    Returns (plain_text, tags) where tags is an ordered list of (tag_str, pos)
    and pos is the character offset in plain_text where the tag was found.

  reinsert(plain_translation, tags, original_plain)
    Maps each tag's position as a fraction of the original plain text length,
    then inserts the tag at the same fractional position in the translation.

  validate_tags(original, translated)
    Counts all inline tags in both strings; returns True iff the counts match.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Tag pattern — matches opening and closing forms of all supported tags.
# Order matters: more-specific patterns (span, a with attributes) before bare.
# ---------------------------------------------------------------------------

_TAG_PATTERN = re.compile(
    r"<(?:"
    r"/(?:i|b|u|em|strong|span|a)"  # closing: </i> </b> etc.
    r"|(?:i|b|u|em|strong)"  # bare opening: <i> <b> etc.
    r"|span(?:\s[^>]*)?"  # <span> or <span ...>
    r"|a(?:\s[^>]*)?"  # <a> or <a href="...">
    r")>"
)


def strip(text: str) -> tuple[str, list[tuple[str, int]]]:
    """Strip inline tags from *text* and return (plain_text, tags).

    *tags* is an ordered list of ``(tag_string, position)`` where *position*
    is the character offset in *plain_text* where the tag would be reinserted.
    """
    tags: list[tuple[str, int]] = []
    plain_parts: list[str] = []
    cursor = 0  # current position in *text*
    plain_cursor = 0  # character count accumulated in plain text so far

    for match in _TAG_PATTERN.finditer(text):
        start, end = match.start(), match.end()
        # Append the text fragment before this tag to the plain output.
        fragment = text[cursor:start]
        plain_parts.append(fragment)
        plain_cursor += len(fragment)
        # Record the tag at the current plain-text cursor position.
        tags.append((match.group(), plain_cursor))
        cursor = end

    # Append any trailing text after the last tag.
    plain_parts.append(text[cursor:])

    return "".join(plain_parts), tags


def _is_word_boundary(text: str, pos: int) -> bool:
    """True iff inserting at *pos* would NOT split a word.

    A boundary is the string start/end or any index adjacent to whitespace.
    """
    n = len(text)
    if pos <= 0 or pos >= n:
        return True
    return text[pos - 1] == " " or text[pos] == " "


def _snap_to_word_boundary(text: str, pos: int, prefer_right: bool) -> int:
    """Move *pos* to the nearest word boundary in *text* (never mid-word).

    On a tie, *prefer_right* breaks it: opening tags prefer the right boundary
    (start of the next word), closing tags prefer the left (end of the word).
    """
    n = len(text)
    pos = max(0, min(pos, n))
    if _is_word_boundary(text, pos):
        return pos

    left = pos
    while left > 0 and not _is_word_boundary(text, left):
        left -= 1
    right = pos
    while right < n and not _is_word_boundary(text, right):
        right += 1

    dist_left = pos - left
    dist_right = right - pos
    if dist_left < dist_right:
        return left
    if dist_right < dist_left:
        return right
    return right if prefer_right else left


def reinsert(
    plain_translation: str,
    tags: list[tuple[str, int]],
    original_plain: str,
) -> str:
    """Reinsert *tags* into *plain_translation*.

    Strategy: map each tag's position as a fraction of *original_plain* length,
    then snap that proportional position to the nearest WORD BOUNDARY in
    *plain_translation* so a tag never splits a translated word (M4-7 / #277).
    Opening tags prefer the start of the next word; closing tags prefer the end
    of the preceding word. Tags are inserted right-to-left to avoid offset drift.

    # NOTE: As of M2-0, this function is FALLBACK-ONLY.
    # The primary translation path sends source text WITH inline tags to the provider
    # and instructs the model to carry them (see TranslationOrchestrator._translate_with_retry).
    # reinsert() is called only when all primary (tags-in-text) attempts fail validation.
    # M4-7 hardened the placement: proportional position is snapped to a word boundary,
    # which keeps tags off mid-word positions for weak/local-model fallbacks. It remains
    # a positional heuristic (not token-alignment), but never fractures a word.
    """
    if not tags:
        return plain_translation

    src_len = len(original_plain)
    tgt_len = len(plain_translation)

    # Compute target insertion positions, snapped to word boundaries.
    positioned: list[tuple[int, str]] = []
    for tag_str, src_pos in tags:
        if src_len == 0:
            fraction = 0.0
        else:
            fraction = src_pos / src_len
        tgt_pos = min(round(fraction * tgt_len), tgt_len)
        prefer_right = not tag_str.startswith("</")  # opening tags → next word
        tgt_pos = _snap_to_word_boundary(plain_translation, tgt_pos, prefer_right)
        positioned.append((tgt_pos, tag_str))

    # Stable sort by target position (preserves original tag order on ties),
    # then insert right-to-left so earlier offsets stay valid.
    positioned_sorted = sorted(positioned, key=lambda x: x[0])

    result = plain_translation
    for tgt_pos, tag_str in reversed(positioned_sorted):
        result = result[:tgt_pos] + tag_str + result[tgt_pos:]

    return result


def validate_tags(original: str, translated: str) -> bool:
    """Return True iff the number of inline tags in *original* and *translated* match."""
    orig_tags = _TAG_PATTERN.findall(original)
    tran_tags = _TAG_PATTERN.findall(translated)
    return len(orig_tags) == len(tran_tags)
