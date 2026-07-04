"""Tests for borgesica.domain.markup — M1-3 (strict TDD).

All test scenarios driven from spec: subtitle-translation/inline-tags.
"""
from __future__ import annotations

import pytest

from borgesica.domain.markup import (
    reinsert,
    strip,
    strip_all_tags,
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
