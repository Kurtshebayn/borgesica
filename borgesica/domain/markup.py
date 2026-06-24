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


def reinsert(
    plain_translation: str,
    tags: list[tuple[str, int]],
    original_plain: str,
) -> str:
    """Reinsert *tags* into *plain_translation*.

    Strategy: map each tag's position as a fraction of *original_plain* length,
    then place it at the proportionally equivalent position in *plain_translation*.
    Tags are inserted from right to left to avoid offset drift.
    """
    if not tags:
        return plain_translation

    src_len = len(original_plain)
    tgt_len = len(plain_translation)

    # Compute target insertion positions.
    # Use integer rounding; clamp to [0, tgt_len].
    positioned: list[tuple[int, str]] = []
    for tag_str, src_pos in tags:
        if src_len == 0:
            fraction = 0.0
        else:
            fraction = src_pos / src_len
        tgt_pos = min(round(fraction * tgt_len), tgt_len)
        positioned.append((tgt_pos, tag_str))

    # Sort by target position ascending, then insert right-to-left to preserve
    # earlier offsets.
    positioned_sorted = sorted(positioned, key=lambda x: x[0])

    result = plain_translation
    # Insert from right to left.
    for tgt_pos, tag_str in reversed(positioned_sorted):
        result = result[:tgt_pos] + tag_str + result[tgt_pos:]

    return result


def validate_tags(original: str, translated: str) -> bool:
    """Return True iff the number of inline tags in *original* and *translated* match."""
    orig_tags = _TAG_PATTERN.findall(original)
    tran_tags = _TAG_PATTERN.findall(translated)
    return len(orig_tags) == len(tran_tags)
