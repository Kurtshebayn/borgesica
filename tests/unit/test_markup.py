"""Tests for borgesica.domain.markup — M1-3 (strict TDD).

All test scenarios driven from spec: subtitle-translation/inline-tags.
"""
from __future__ import annotations

import pytest

from borgesica.domain.markup import (
    reinsert,
    restore_tags,
    strip,
    strip_all_tags,
    tokenize_tags,
    validate_placeholders,
    validate_segments,
    validate_tags,
)


# ---------------------------------------------------------------------------
# strip()
# ---------------------------------------------------------------------------


def test_strip_single_italic_returns_plain_and_two_tags() -> None:
    """Spec scenario: single italic tag round-trips correctly (strip half)."""
    plain, tags = strip("The <i>quick</i> fox.")
    assert plain == "The quick fox."
    assert len(tags) == 2
    tag_strs = [t for t, _pos in tags]
    assert "<i>" in tag_strs
    assert "</i>" in tag_strs


def test_strip_records_positions_for_italic() -> None:
    """Tags positions represent character offsets in the stripped plain text."""
    plain, tags = strip("The <i>quick</i> fox.")
    # "<i>" opens at position 4 in plain "The quick fox."
    # "</i>" closes at position 9 in plain "The quick fox."
    tag_dict = {t: pos for t, pos in tags}
    assert tag_dict["<i>"] == 4
    assert tag_dict["</i>"] == 9


def test_strip_no_tags_returns_unchanged_text_and_empty_list() -> None:
    """Text with no tags round-trips unchanged."""
    plain, tags = strip("Hello world.")
    assert plain == "Hello world."
    assert tags == []


def test_strip_bold_tag() -> None:
    plain, tags = strip("<b>Bold</b> text.")
    assert plain == "Bold text."
    assert len(tags) == 2
    tag_strs = [t for t, _pos in tags]
    assert "<b>" in tag_strs
    assert "</b>" in tag_strs


def test_strip_underline_tag() -> None:
    plain, tags = strip("Some <u>underlined</u> word.")
    assert plain == "Some underlined word."
    assert len(tags) == 2


def test_strip_em_tag() -> None:
    plain, tags = strip("Say <em>hello</em> now.")
    assert plain == "Say hello now."
    assert len(tags) == 2


def test_strip_strong_tag() -> None:
    plain, tags = strip("A <strong>very</strong> strong word.")
    assert plain == "A very strong word."
    assert len(tags) == 2


def test_strip_nested_tags_preserve_order() -> None:
    """Nested tags preserve order (outer open, inner open, inner close, outer close)."""
    plain, tags = strip("<b><i>text</i></b>")
    assert plain == "text"
    assert len(tags) == 4
    tag_strs = [t for t, _pos in tags]
    assert tag_strs[0] == "<b>"
    assert tag_strs[1] == "<i>"
    assert tag_strs[2] == "</i>"
    assert tag_strs[3] == "</b>"


def test_strip_multiple_tags_counted_correctly() -> None:
    """Source with 2 opening + 2 closing tags (4 total) for validate_tags spec."""
    source = "<b>Bold</b> and <i>italic</i>."
    plain, tags = strip(source)
    assert plain == "Bold and italic."
    assert len(tags) == 4


def test_strip_span_tag() -> None:
    """Spec: <span ...> and </span> supported."""
    plain, tags = strip('<span class="x">Hello</span> world.')
    assert plain == "Hello world."
    assert len(tags) == 2
    tag_strs = [t for t, _pos in tags]
    assert any(t.startswith("<span") for t in tag_strs)
    assert "</span>" in tag_strs


def test_strip_anchor_tag() -> None:
    """Spec: <a ...> and </a> supported."""
    plain, tags = strip('<a href="x">link</a> text.')
    assert plain == "link text."
    assert len(tags) == 2


# ---------------------------------------------------------------------------
# reinsert()
# ---------------------------------------------------------------------------


def test_reinsert_single_italic_produces_valid_tags() -> None:
    """Spec scenario: single italic tag round-trips correctly (reinsert half)."""
    source = "The <i>quick</i> fox."
    _plain_src, tags = strip(source)
    # Simulate translation with plain text (no tags in model output)
    result = reinsert("El veloz zorro.", tags, "The quick fox.")
    assert "<i>" in result
    assert "</i>" in result


def test_reinsert_no_tags_returns_translation_unchanged() -> None:
    """Text with no tags round-trips unchanged."""
    result = reinsert("El mundo.", [], "The world.")
    assert result == "El mundo."


def test_reinsert_preserves_tag_count() -> None:
    """After reinsert, tag count should match original."""
    source = "<b>Bold</b> and <i>italic</i>."
    plain_src, tags = strip(source)
    translated = "Negritas y cursiva."
    result = reinsert(translated, tags, plain_src)
    # 4 tags total: <b> </b> <i> </i>
    import re

    found = re.findall(r"</?(?:b|i|u|em|strong|span[^>]*|a[^>]*)>", result)
    assert len(found) == 4


# ---------------------------------------------------------------------------
# M4-7 (#277) — fallback reinsert snaps tags to WORD boundaries (never mid-word)
# ---------------------------------------------------------------------------


def _tag_is_at_word_boundary(result: str, tag: str) -> bool:
    """True iff every occurrence of *tag* sits at a word boundary in *result*
    (i.e. is not wedged between two non-space characters)."""
    start = 0
    while True:
        idx = result.find(tag, start)
        if idx == -1:
            return True
        before_ok = idx == 0 or result[idx - 1] == " "
        after = idx + len(tag)
        after_ok = after >= len(result) or result[after] == " "
        # A boundary means at least one side is a space / string edge.
        if not (before_ok or after_ok):
            return False
        start = idx + len(tag)


def test_reinsert_snaps_opening_tag_to_word_boundary() -> None:
    """M4-7: proportional position lands mid-word; reinsert must snap the opening
    tag to a word boundary so it never splits a translated word.
    Spec: subtitle-translation/inline-tags-in-text (fallback placement hardening).
    """
    # "<i>" opens before "quick" (src_pos 4 in "The quick brown fox").
    # Proportional maps to char 4 of "El veloz zorro pardo" → inside "veloz".
    _plain, tags = strip("The <i>quick</i> brown fox")
    result = reinsert("El veloz zorro pardo", tags, "The quick brown fox")
    # The <i> must not land inside a word (e.g. NOT "El v<i>eloz").
    assert "v<i>eloz" not in result
    assert _tag_is_at_word_boundary(result, "<i>")
    # Count still preserved.
    assert result.count("<i>") == 1 and result.count("</i>") == 1


def test_reinsert_never_splits_a_word_multi_tag() -> None:
    """M4-7: no reinserted tag may be wedged between two non-space characters."""
    source = "The <b>quick</b> brown <i>lazy</i> fox jumps"
    plain, tags = strip(source)
    result = reinsert("El zorro perezoso marron salta rapido", tags, plain)
    for tag in ("<b>", "</b>", "<i>", "</i>"):
        assert _tag_is_at_word_boundary(result, tag), f"{tag} split a word in {result!r}"
    assert validate_tags(source, result) is True


# ---------------------------------------------------------------------------
# validate_tags()
# ---------------------------------------------------------------------------


def test_validate_tags_true_when_counts_match() -> None:
    """Spec: validate_tags returns True when tag counts match."""
    original = "The <i>quick</i> fox."
    result = "El <i>veloz</i> zorro."
    assert validate_tags(original, result) is True


def test_validate_tags_false_when_counts_differ() -> None:
    """Spec: validate_tags returns False when they differ."""
    original = "The <i>quick</i> fox."
    result = "El veloz zorro."  # missing tags
    assert validate_tags(original, result) is False


def test_validate_tags_no_tags_both_sides() -> None:
    """No tags on either side → True."""
    assert validate_tags("Hello.", "Hola.") is True


def test_validate_tags_multiple_tag_types() -> None:
    original = "<b>Bold</b> and <i>italic</i>."
    result = "<b>Negrita</b> e <i>cursiva</i>."
    assert validate_tags(original, result) is True


def test_validate_tags_extra_tag_in_result_is_false() -> None:
    original = "<i>word</i>"
    result = "<i>word</i><b>extra</b>"
    assert validate_tags(original, result) is False


# ---------------------------------------------------------------------------
# M2-0 Test 7 — strip/reinsert/validate_tags behavior unchanged (no regression)
# reinsert is now fallback-only but its behavior is identical.
# ---------------------------------------------------------------------------


def test_m2_0_strip_reinsert_validate_roundtrip_unchanged() -> None:
    """M2-0: strip/reinsert/validate_tags behavior is UNCHANGED — they are now
    fallback-only, but every existing guarantee still holds.
    Spec: subtitle-translation/inline-tags-in-text (reinsert is fallback-only from M2-0).
    See NOTE in borgesica/domain/markup.py: reinsert is fallback-only; M4 will harden
    the placement heuristic.
    """
    # Round-trip: strip then reinsert must preserve tag count
    source = "We don't have <i>much</i> time"
    plain, tags = strip(source)
    assert plain == "We don't have much time"
    assert len(tags) == 2  # <i> and </i>

    # Simulate a translated plain text (as fallback would do)
    translated_plain = "No tenemos mucho tiempo"
    reinserted = reinsert(translated_plain, tags, plain)

    # validate_tags must pass: source has 2 tags, reinserted must also have 2 tags
    assert validate_tags(source, reinserted) is True, (
        "Fallback path: reinsert must produce same tag count as source"
    )


# ---------------------------------------------------------------------------
# validate_segments()
#
# The "\n\n" segment count is part of the model output contract: readers join
# block nodes with "\n\n" and writers split translated_text on "\n\n" to map
# segments back positionally. A merged or split paragraph desynchronizes
# every node after the divergence point. No strip()/normalization: the
# validator must count segments EXACTLY as the writers split them.
# ---------------------------------------------------------------------------


def test_validate_segments_equal_counts() -> None:
    """Same number of \n\n segments on both sides passes."""
    assert validate_segments("Uno.\n\nDos.", "One.\n\nTwo.") is True


def test_validate_segments_detects_merge() -> None:
    """Two source paragraphs merged into one translated segment fails."""
    assert validate_segments("Uno.\n\nDos.", "One. Two.") is False


def test_validate_segments_detects_split() -> None:
    """One source paragraph split into two translated segments fails."""
    assert validate_segments("Uno. Dos.", "One.\n\nTwo.") is False


def test_validate_segments_single_segment() -> None:
    """Single-paragraph chunks trivially pass."""
    assert validate_segments("Uno.", "One.") is True


# ---------------------------------------------------------------------------
# strip_all_tags()
#
# GUARD-ONLY helper: removes ALL markup tags (known inline, void, unknown)
# so the prose guard can decide whether any translatable prose remains.
# Unlike strip(), it is destructive (no positions) — never used for the
# strip/reinsert round-trip.
# ---------------------------------------------------------------------------


def test_strip_all_tags_removes_self_closing_img() -> None:
    """The nested-cover shape: an <img> with letter-bearing attributes."""
    assert strip_all_tags('<img src="images/cover.jpg" alt="Cover art"/>').strip() == ""


def test_strip_all_tags_removes_unknown_wrapper_tags() -> None:
    """Tags outside strip()'s known set (figure/figcaption) are removed too."""
    assert strip_all_tags("<figure><figcaption></figcaption></figure>").strip() == ""


def test_strip_all_tags_keeps_text_content() -> None:
    """Only markup goes away — prose between tags survives."""
    assert strip_all_tags('<img src="map.png"/> The journey begins.').strip() == "The journey begins."


def test_strip_all_tags_no_tags_returns_unchanged() -> None:
    assert strip_all_tags("Plain prose, no markup.") == "Plain prose, no markup."


def test_strip_all_tags_does_not_eat_escaped_angle_brackets() -> None:
    """Real prose with a literal < arrives entity-escaped from the reader."""
    assert strip_all_tags("a &lt;b&gt; c") == "a &lt;b&gt; c"


# ---------------------------------------------------------------------------
# tokenize_tags() / restore_tags() / validate_placeholders()
#
# Placeholder-based inline tag round-trip: real tags (with their attributes —
# hrefs, classes, ids) never reach the provider. Each tag is replaced by an
# opaque numbered placeholder (paired tags: "⟦1⟧" ... "⟦/1⟧",
# numbered in source order); a registry maps each id back to the exact
# original tag text. validate_placeholders checks the placeholder SEQUENCE
# (multiset + pairing + nesting) rather than raw tag counts, so a model that
# swaps an attribute or drops one tag while adding an unrelated one can no
# longer slip past validation the way count-only validate_tags did.
#
# Placeholder character choice: U+27E6/U+27E7 (MATHEMATICAL WHITE SQUARE
# BRACKET) — extremely unlikely to occur in real prose or be "translated" by
# the model (unlike ASCII brackets or angle brackets, which collide with
# markup/code the source text may legitimately contain), and stable across
# JSON string encoding.
# ---------------------------------------------------------------------------


def test_tokenize_tags_single_pair_produces_numbered_placeholder() -> None:
    """A single <i>...</i> pair becomes ⟦1⟧...⟦/1⟧, and the
    registry maps id 1 back to the exact open/close tag strings."""
    placeholder_text, registry = tokenize_tags("The <i>quick</i> fox.")
    assert placeholder_text == "The ⟦1⟧quick⟦/1⟧ fox."
    assert registry == {1: ("<i>", "</i>")}


def test_tokenize_tags_no_tags_returns_text_unchanged() -> None:
    placeholder_text, registry = tokenize_tags("Plain text, no tags.")
    assert placeholder_text == "Plain text, no tags."
    assert registry == {}


def test_tokenize_tags_numbers_in_source_order() -> None:
    """Two sibling pairs get ids 1 and 2 in the order they appear."""
    placeholder_text, registry = tokenize_tags("<b>Bold</b> and <i>italic</i>.")
    assert placeholder_text == "⟦1⟧Bold⟦/1⟧ and ⟦2⟧italic⟦/2⟧."
    assert registry == {1: ("<b>", "</b>"), 2: ("<i>", "</i>")}


def test_tokenize_tags_nested_pairs_get_distinct_ids() -> None:
    """Nested tags each get their own id; the outer pair opens first (id 1),
    the inner pair opens second (id 2)."""
    placeholder_text, registry = tokenize_tags("<b><i>text</i></b>")
    assert placeholder_text == "⟦1⟧⟦2⟧text⟦/2⟧⟦/1⟧"
    assert registry == {1: ("<b>", "</b>"), 2: ("<i>", "</i>")}


def test_tokenize_tags_preserves_attributes_in_registry_only() -> None:
    """Attributes (hrefs, classes) are captured in the registry, never in the
    text that would be sent to the provider."""
    placeholder_text, registry = tokenize_tags('<a href="https://example.com/x">link</a>')
    assert "href" not in placeholder_text
    assert "example.com" not in placeholder_text
    assert registry[1][0] == '<a href="https://example.com/x">'
    assert registry[1][1] == "</a>"


# --- Required test 1: round trip ---


def test_placeholder_roundtrip_identity_translation_byte_identical() -> None:
    """strip -> placeholders -> (identity 'translation') -> restore reproduces
    the original text byte-for-byte."""
    source = '<a href="x">foo</a> and <em>bar</em>'
    placeholder_text, registry = tokenize_tags(source)
    # Simulate an identity "translation" — the provider echoes the placeholder
    # text back unchanged.
    translated = placeholder_text
    restored = restore_tags(translated, registry)
    assert restored == source


# --- Required test 2: attributes never reach the provider ---


def test_placeholder_text_never_contains_attributes() -> None:
    source = '<a href="https://secret.example/path?token=abc" class="ext">Click</a>'
    placeholder_text, _registry = tokenize_tags(source)
    assert "href" not in placeholder_text
    assert "secret.example" not in placeholder_text
    assert "token=abc" not in placeholder_text
    assert "class" not in placeholder_text
    assert "<a" not in placeholder_text
    assert "</a>" not in placeholder_text


# --- Required test 3: swapped links — hrefs follow their own placeholder id ---


def test_swapped_placeholders_restore_hrefs_by_id_not_position() -> None:
    """If the model's output swaps WHICH placeholder id wraps which word, the
    restored hrefs follow their own numbered id, wherever it landed — not the
    textual position it originally occupied. This is the deliberate, documented
    outcome: restore() is keyed by id, so each attribute set travels with its
    number, never with a text position.
    """
    source = '<a href="https://x.example">Foo</a> and <a href="https://y.example">Bar</a>'
    placeholder_text, registry = tokenize_tags(source)
    assert placeholder_text == "⟦1⟧Foo⟦/1⟧ and ⟦2⟧Bar⟦/2⟧"

    # The model swaps which id wraps which word (a legitimate word-order change
    # or a genuine mistake — validate_placeholders cannot tell the difference,
    # and does not need to: see validate_placeholders' reordering test below).
    swapped = "⟦2⟧Foo⟦/2⟧ and ⟦1⟧Bar⟦/1⟧"
    restored = restore_tags(swapped, registry)

    assert restored == (
        '<a href="https://y.example">Foo</a> and <a href="https://x.example">Bar</a>'
    )
    # id 2's original href ends up wherever id 2 landed (wrapping "Foo"), and
    # id 1's href follows id 1 (wrapping "Bar") — each href stayed attached to
    # its own placeholder id, never to the original word.


# --- Required test 4: dropped / duplicated / broken nesting -> invalid ---


def test_validate_placeholders_dropped_placeholder_is_invalid() -> None:
    source = "The <i>quick</i> fox."
    placeholder_text, registry = tokenize_tags(source)
    translated = "El zorro rapido."  # placeholder pair dropped entirely
    result = validate_placeholders(placeholder_text, translated, registry)
    assert result.valid is False


def test_validate_placeholders_duplicated_placeholder_is_invalid() -> None:
    source = "<b>Bold</b> and <i>italic</i>."
    placeholder_text, registry = tokenize_tags(source)
    # id 2 duplicated, id 1 dropped.
    translated = "⟦2⟧Negrita⟦/2⟧ y ⟦2⟧cursiva⟦/2⟧."
    result = validate_placeholders(placeholder_text, translated, registry)
    assert result.valid is False


def test_validate_placeholders_broken_nesting_is_invalid() -> None:
    """Crossing placeholders (⟦1⟧⟦2⟧...⟦/1⟧⟦/2⟧)
    — each id appears exactly once, but the pairing is not properly nested."""
    source = "<b><i>text</i></b>"
    placeholder_text, registry = tokenize_tags(source)
    translated = "⟦1⟧⟦2⟧texto⟦/1⟧⟦/2⟧"
    result = validate_placeholders(placeholder_text, translated, registry)
    assert result.valid is False


def test_validate_placeholders_valid_roundtrip_passes() -> None:
    source = "The <i>quick</i> fox."
    placeholder_text, registry = tokenize_tags(source)
    translated = "El zorro ⟦1⟧rapido⟦/1⟧."
    result = validate_placeholders(placeholder_text, translated, registry)
    assert result.valid is True
    assert result.reordered is False


# --- Required test 5: old count-only validate_tags wrongly accepts this;
#     the new placeholder validator correctly rejects it. ---


def test_validate_placeholders_rejects_what_count_only_validation_wrongly_accepted() -> None:
    """Source has two SEPARATE <em> pairs. A translation that drops the first
    pair entirely and duplicates the second still has the OLD count-only
    validate_tags' magic number (4 raw tags total) — validate_tags wrongly
    accepts it. validate_placeholders must reject it.
    """
    source = "<em>alpha</em> and <em>beta</em>."
    placeholder_text, registry = tokenize_tags(source)
    assert registry == {1: ("<em>", "</em>"), 2: ("<em>", "</em>")}

    # Old-world equivalent: drop the first <em> pair, duplicate the second —
    # net tag count unchanged (still 4 raw tags), so validate_tags(source,
    # equivalent_raw) would return True. Demonstrate that directly:
    equivalent_raw = "<em>uno</em> y <em>uno</em>."
    assert validate_tags(source, equivalent_raw) is True  # old validator fooled

    # New world: the same semantic corruption expressed as placeholders —
    # id 1 missing, id 2 duplicated.
    translated = "uno y ⟦2⟧uno⟦/2⟧ y ⟦2⟧uno⟦/2⟧."
    result = validate_placeholders(placeholder_text, translated, registry)
    assert result.valid is False


# --- Reordering: flagged, not rejected (explicit design decision) ---


def test_validate_placeholders_sibling_reorder_is_valid_but_flagged() -> None:
    """Two sibling placeholders appearing in a different order than the source
    (a legitimate word-order change in translation) is VALID — multiset and
    nesting both hold — but the result flags it via `reordered=True` so
    callers may log/inspect it without rejecting the translation.
    """
    source = "<b>Bold</b> and <i>italic</i>."
    placeholder_text, registry = tokenize_tags(source)
    # Sibling order swapped: id 2 now appears before id 1.
    translated = "⟦2⟧cursiva⟦/2⟧ y ⟦1⟧negrita⟦/1⟧."
    result = validate_placeholders(placeholder_text, translated, registry)
    assert result.valid is True
    assert result.reordered is True


def test_validate_placeholders_unknown_placeholder_id_is_invalid() -> None:
    """A placeholder id that was never issued by tokenize_tags (hallucinated
    by the model) is invalid."""
    source = "The <i>quick</i> fox."
    placeholder_text, registry = tokenize_tags(source)
    translated = "El ⟦1⟧zorro⟦/1⟧ ⟦2⟧rapido⟦/2⟧."
    result = validate_placeholders(placeholder_text, translated, registry)
    assert result.valid is False


# --- restore_tags() edge cases ---


def test_restore_tags_no_placeholders_returns_text_unchanged() -> None:
    assert restore_tags("Plain text.", {}) == "Plain text."


def test_restore_tags_unknown_id_left_verbatim() -> None:
    """A placeholder marker with no matching registry entry is left as-is
    rather than crashing — defensive, matches the spirit of validate_placeholders
    rejecting it upstream before restore is ever called on invalid output."""
    assert restore_tags("⟦99⟧text⟦/99⟧", {}) == "⟦99⟧text⟦/99⟧"
