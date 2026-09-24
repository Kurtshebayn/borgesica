"""Markup module — inline tag strip / reinsert / validate, and the
placeholder-based tokenize / restore / validate round-trip.

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
    FALLBACK-ONLY as of the placeholder rework below: strip/reinsert still use
    it, but the PRIMARY tags-in-text path no longer does (see tokenize_tags).

  tokenize_tags(text)
    Replaces every inline tag with an opaque numbered placeholder ("⟦1⟧"
    ... "⟦/1⟧", numbered in source order) and returns
    (placeholder_text, registry), where registry maps each id to the EXACT
    original (open_tag, close_tag) text — attributes included. This is what
    the PRIMARY path now sends to the provider instead of raw tags: attributes
    (hrefs, classes, ids) never reach the model, so they cannot be corrupted.

  restore_tags(text, registry)
    Replaces every placeholder marker in *text* with the original tag text
    from *registry*, keyed by numeric id (not by textual position — see
    validate_placeholders for why that matters under legitimate reordering).

  validate_placeholders(source_placeholder_text, translated, registry)
    Validates the placeholder SEQUENCE: every id present exactly once as an
    open and once as a close (multiset), and properly paired/nested (no
    crossing). Sibling reordering (translation-driven word-order changes) is
    VALID but flagged via `reordered=True` — see PlaceholderValidation.
"""
from __future__ import annotations

import re
from typing import NamedTuple

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

# Any markup tag whatsoever: opening, closing, self-closing, known or unknown
# (<img .../>, <figure>, </figcaption>, ...). Requires a letter after "<" (or
# "</") so entity-escaped brackets and stray "<" in prose never match.
# Used ONLY by strip_all_tags (prose-guard decisions) — the round-trip
# machinery stays on the conservative _TAG_PATTERN above.
_ANY_TAG_PATTERN = re.compile(r"</?[A-Za-z][^>]*>")


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


def strip_all_tags(text: str) -> str:
    """Remove ALL markup tags — known inline tags, void tags (``<img/>``),
    and unknown wrappers (``<figure>``) alike.

    GUARD-ONLY helper: the orchestrator's prose guard uses it to decide
    whether a chunk contains any translatable prose at all (an ``<img>``
    nested in a ``<p>`` serializes into source_text, and its src/alt
    attribute characters must not count as prose). Unlike :func:`strip`,
    this is destructive — it records no positions — so it must NEVER be
    used for the strip/reinsert round-trip. Entity-escaped brackets in real
    prose (``&lt;``) are untouched.
    """
    return _ANY_TAG_PATTERN.sub("", text)


def validate_segments(original: str, translated: str) -> bool:
    """Return True iff *original* and *translated* have the same number of
    ``\\n\\n``-separated segments.

    The segment count is part of the model output contract: readers join block
    nodes with ``\\n\\n`` and writers split translated text on ``\\n\\n`` to map
    segments back to document nodes positionally, so a merged or split
    paragraph desynchronizes every node after the divergence point. Counts are
    taken on the raw strings — no strip()/normalization — because that is
    exactly how the writers split.
    """
    return len(original.split("\n\n")) == len(translated.split("\n\n"))


# ---------------------------------------------------------------------------
# Placeholder-based tokenize / restore / validate.
#
# Registry type: dict mapping a numeric placeholder id to the EXACT original
# (open_tag_str, close_tag_str) pair. Attributes (hrefs, classes, ids) live
# ONLY in this registry — never in the text handed to the provider.
#
# Placeholder characters: U+27E6/U+27E7 (MATHEMATICAL WHITE SQUARE BRACKET),
# not ASCII "[]" or ordinary angle brackets. Rationale: these code points are
# vanishingly unlikely to occur in real prose or get "translated"/reworded by
# the model the way an ASCII bracket or a word like "TAG1" might, they survive
# JSON string encoding unescaped, and they are visually unlike the "<...>"
# markup the source may legitimately contain (so a model that DOES leak a
# literal "<em>" into its prose never gets misread as a placeholder). The
# numeric id (rather than a bare "⟦⟧" pair marker) keeps every tag's
# identity unambiguous even when the model reorders siblings.
#
# Scope: only the PAIRED tags _TAG_PATTERN already recognizes (i/b/u/em/
# strong/span/a) ever reach tokenize_tags. Void/self-closing tags (<img/>,
# etc.) are handled exclusively by strip_all_tags for the prose guard and
# never enter the round-trip machinery — same boundary strip()/reinsert()
# already drew — so no self-closing placeholder form ("⟦1/⟧") is
# needed today; the id-based registry design leaves room for one without a
# format change if that scope ever grows.
# ---------------------------------------------------------------------------

_PLACEHOLDER_OPEN_RE = re.compile(r"⟦(\d+)⟧")
_PLACEHOLDER_CLOSE_RE = re.compile(r"⟦/(\d+)⟧")
_PLACEHOLDER_ANY_RE = re.compile(r"⟦(/?)(\d+)⟧")


def _open_marker(tag_id: int) -> str:
    return f"⟦{tag_id}⟧"


def _close_marker(tag_id: int) -> str:
    return f"⟦/{tag_id}⟧"


def tokenize_tags(text: str) -> tuple[str, dict[int, tuple[str, str]]]:
    """Replace every inline tag in *text* with a numbered placeholder.

    Returns (placeholder_text, registry). *registry* maps each id to the
    exact (open_tag_str, close_tag_str) pair as they appeared in *text* —
    the single source of truth restore_tags uses to put real markup back.

    Pairing/nesting is resolved with a stack in source order: an opening tag
    pushes a fresh id; a closing tag pops the most recently opened id and
    completes that id's registry entry. This assumes well-nested source
    markup (true of real EPUB/SRT text); an unmatched closing tag (no
    corresponding open on the stack) is left as literal text — nothing to
    pair it with — and an unmatched opening tag left on the stack at the end
    is still registered (open half only) so its placeholder still restores.
    """
    registry: dict[int, tuple[str, str]] = {}
    stack: list[tuple[int, str]] = []
    parts: list[str] = []
    cursor = 0
    counter = 0

    for match in _TAG_PATTERN.finditer(text):
        start, end = match.start(), match.end()
        parts.append(text[cursor:start])
        cursor = end
        tag_str = match.group()

        if tag_str.startswith("</"):
            if stack:
                tag_id, open_tag = stack.pop()
                registry[tag_id] = (open_tag, tag_str)
                parts.append(_close_marker(tag_id))
            else:
                # Unmatched closing tag — malformed source; nothing to pair
                # it with, pass it through unchanged rather than invent an id.
                parts.append(tag_str)
        else:
            counter += 1
            stack.append((counter, tag_str))
            parts.append(_open_marker(counter))

    parts.append(text[cursor:])

    # Any tags never closed (malformed source) still get a registry entry
    # (open half only) so their placeholder round-trips instead of raising.
    for tag_id, open_tag in stack:
        registry.setdefault(tag_id, (open_tag, ""))

    return "".join(parts), registry


def restore_tags(text: str, registry: dict[int, tuple[str, str]]) -> str:
    """Replace every placeholder marker in *text* with its registered tag.

    Restoration is keyed by numeric id, not by textual position: whatever
    word ends up inside "⟦N⟧...⟦/N⟧", id N's ORIGINAL tag
    (attributes included) wraps it — even if the model reordered siblings.
    A marker whose id has no registry entry (should never happen for output
    that passed validate_placeholders) is left verbatim rather than raising.
    """

    def _repl_open(m: re.Match[str]) -> str:
        pair = registry.get(int(m.group(1)))
        return pair[0] if pair is not None else m.group(0)

    def _repl_close(m: re.Match[str]) -> str:
        pair = registry.get(int(m.group(1)))
        return pair[1] if pair is not None else m.group(0)

    text = _PLACEHOLDER_CLOSE_RE.sub(_repl_close, text)
    text = _PLACEHOLDER_OPEN_RE.sub(_repl_open, text)
    return text


class PlaceholderValidation(NamedTuple):
    """Result of validate_placeholders.

    valid:     True iff every registered id appears exactly once as an open
               marker and exactly once as a close marker (multiset), AND the
               markers form a properly nested/paired sequence (no crossing
               like "⟦1⟧⟦2⟧...⟦/1⟧⟦/2⟧").
    reordered: True iff `valid` and the ORDER in which sibling ids first open
               differs from the source order. Reordering between siblings can
               be a legitimate word-order change in translation, so it is
               flagged, not rejected — see module docstring.
    issues:    Human-readable diagnostics for every multiset/nesting problem
               found (empty when valid).
    """

    valid: bool
    reordered: bool
    issues: tuple[str, ...]


def _placeholder_events(text: str) -> list[tuple[int, bool]]:
    """Return [(id, is_close), ...] for every placeholder marker in *text*,
    in the order they appear."""
    return [
        (int(m.group(2)), m.group(1) == "/") for m in _PLACEHOLDER_ANY_RE.finditer(text)
    ]


def validate_placeholders(
    source_placeholder_text: str,
    translated: str,
    registry: dict[int, tuple[str, str]],
) -> PlaceholderValidation:
    """Validate the placeholder SEQUENCE in *translated* against *registry*.

    Unlike count-only validate_tags, this checks that every id the source
    issued comes back exactly once as an open and once as a close (so a
    dropped tag can no longer be masked by an unrelated extra tag), and that
    the markers nest properly (catches crossing/broken pairing). Sibling
    reordering is accepted but reported via `reordered`.
    """
    issues: list[str] = []
    events = _placeholder_events(translated)
    expected_ids = set(registry.keys())

    open_counts: dict[int, int] = {}
    close_counts: dict[int, int] = {}
    for tag_id, is_close in events:
        counts = close_counts if is_close else open_counts
        counts[tag_id] = counts.get(tag_id, 0) + 1

    multiset_ok = True
    for tag_id in sorted(expected_ids):
        opens = open_counts.get(tag_id, 0)
        closes = close_counts.get(tag_id, 0)
        if opens != 1 or closes != 1:
            multiset_ok = False
            issues.append(
                f"placeholder {tag_id}: expected exactly 1 open + 1 close, "
                f"got {opens} open + {closes} close"
            )

    unknown_ids = (set(open_counts) | set(close_counts)) - expected_ids
    for tag_id in sorted(unknown_ids):
        multiset_ok = False
        issues.append(f"placeholder {tag_id}: not issued by the source (hallucinated)")

    # Nesting/pairing: a valid bracket sequence over the events as they
    # actually appear (regardless of numeric id order).
    nesting_ok = True
    stack: list[int] = []
    for tag_id, is_close in events:
        if not is_close:
            stack.append(tag_id)
            continue
        if stack and stack[-1] == tag_id:
            stack.pop()
        else:
            nesting_ok = False
            issues.append(f"placeholder {tag_id}: broken nesting/pairing at close")
            if tag_id in stack:
                while stack and stack[-1] != tag_id:
                    stack.pop()
                if stack:
                    stack.pop()
    if stack:
        nesting_ok = False
        issues.append(
            "unclosed placeholders: " + ", ".join(str(i) for i in stack)
        )

    valid = multiset_ok and nesting_ok

    reordered = False
    if valid:
        source_events = _placeholder_events(source_placeholder_text)
        source_order = [tid for tid, is_close in source_events if not is_close]
        translated_order = [tid for tid, is_close in events if not is_close]
        reordered = source_order != translated_order

    return PlaceholderValidation(valid=valid, reordered=reordered, issues=tuple(issues))
