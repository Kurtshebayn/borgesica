"""Tests for GlossaryExtractor (M1-7).

Strict TDD: tests written before implementation.
Spec: context-continuity/glossary-seeded, context-continuity/mid-run-additions
"""
from __future__ import annotations

import pytest

from tests.fakes import FakeTranslationProvider
from borgesica.domain.models import (
    Glossary,
    GlossaryEntry,
    JobConfig,
    SourceType,
    TranslationUnit,
)
from borgesica.domain.ports import GlossaryExtractor as GlossaryExtractorProtocol


def make_config(**kwargs) -> JobConfig:
    defaults = dict(source_type=SourceType.SRT, model="claude-haiku-4-5")
    defaults.update(kwargs)
    return JobConfig(**defaults)


# ---------------------------------------------------------------------------
# Test 1 — LlmGlossaryExtractor satisfies GlossaryExtractor Protocol
# ---------------------------------------------------------------------------


def test_llm_extractor_satisfies_protocol():
    """LlmGlossaryExtractor must be an instance of GlossaryExtractor Protocol."""
    from borgesica.domain.glossary import LlmGlossaryExtractor

    provider = FakeTranslationProvider()
    extractor = LlmGlossaryExtractor(provider=provider)
    assert isinstance(extractor, GlossaryExtractorProtocol)


# ---------------------------------------------------------------------------
# Test 2 — LlmGlossaryExtractor extracts glossary_additions from canned unit
# ---------------------------------------------------------------------------


def test_llm_extractor_returns_glossary_from_provider_response():
    """With canned unit containing glossary_additions, extract() returns those entries."""
    from borgesica.domain.glossary import LlmGlossaryExtractor

    canned = TranslationUnit(
        translation="dummy",
        summary_update="dummy summary",
        glossary_additions=[
            GlossaryEntry(term="Thornwood", translation="Thornwood"),
            GlossaryEntry(term="Elara", translation="Elara"),
        ],
    )
    provider = FakeTranslationProvider(canned_unit=canned)
    extractor = LlmGlossaryExtractor(provider=provider)
    config = make_config(glossary_strategy="llm")

    glossary = extractor.extract("Some source text with Thornwood and Elara.", config)

    assert isinstance(glossary, Glossary)
    terms = {e.term for e in glossary.entries}
    assert "Thornwood" in terms
    assert "Elara" in terms


# ---------------------------------------------------------------------------
# Test 3 — glossary_strategy="none" → NullGlossaryExtractor returns empty Glossary
# ---------------------------------------------------------------------------


def test_null_extractor_returns_empty_glossary():
    """NullGlossaryExtractor always returns Glossary() regardless of input."""
    from borgesica.domain.glossary import NullGlossaryExtractor

    extractor = NullGlossaryExtractor()
    config = make_config(glossary_strategy="none")

    glossary = extractor.extract("Any text here", config)

    assert isinstance(glossary, Glossary)
    assert glossary.entries == []


# ---------------------------------------------------------------------------
# Test 4 — get_extractor factory selects correct extractor by strategy
# ---------------------------------------------------------------------------


def test_get_extractor_factory_llm_strategy():
    """get_extractor('llm', provider) returns an LlmGlossaryExtractor."""
    from borgesica.domain.glossary import LlmGlossaryExtractor, get_extractor

    provider = FakeTranslationProvider()
    extractor = get_extractor("llm", provider)
    assert isinstance(extractor, LlmGlossaryExtractor)


def test_get_extractor_factory_none_strategy():
    """get_extractor('none', provider) returns a NullGlossaryExtractor."""
    from borgesica.domain.glossary import NullGlossaryExtractor, get_extractor

    provider = FakeTranslationProvider()
    extractor = get_extractor("none", provider)
    assert isinstance(extractor, NullGlossaryExtractor)


# ---------------------------------------------------------------------------
# Test 5 — NullGlossaryExtractor satisfies Protocol
# ---------------------------------------------------------------------------


def test_null_extractor_satisfies_protocol():
    """NullGlossaryExtractor must satisfy GlossaryExtractor Protocol."""
    from borgesica.domain.glossary import NullGlossaryExtractor

    extractor = NullGlossaryExtractor()
    assert isinstance(extractor, GlossaryExtractorProtocol)


# ---------------------------------------------------------------------------
# Test 6 — LlmGlossaryExtractor makes exactly one provider.translate call
# ---------------------------------------------------------------------------


def test_llm_extractor_makes_one_provider_call():
    """LlmGlossaryExtractor should call provider.translate exactly once per extract() call."""
    from borgesica.domain.glossary import LlmGlossaryExtractor

    provider = FakeTranslationProvider()
    extractor = LlmGlossaryExtractor(provider=provider)
    config = make_config(glossary_strategy="llm")

    extractor.extract("Source text to extract terms from.", config)

    assert provider.call_count == 1


# ---------------------------------------------------------------------------
# normalize_term — whitespace and Unicode canonicalisation
# ---------------------------------------------------------------------------


def test_normalize_term_strips_surrounding_whitespace():
    """Leading/trailing whitespace is not part of a term."""
    from borgesica.domain.glossary import normalize_term

    assert normalize_term("  Crannog \t") == "Crannog"


def test_normalize_term_collapses_internal_whitespace():
    """Runs of internal whitespace collapse to a single space."""
    from borgesica.domain.glossary import normalize_term

    assert normalize_term("Ddram\n\tcyfraith") == "Ddram cyfraith"
    assert normalize_term("Gleaner   patrol") == "Gleaner patrol"


def test_normalize_term_applies_nfc_composition():
    """Decomposed accents compose, so accented spellings cannot diverge."""
    import unicodedata

    from borgesica.domain.glossary import normalize_term

    composed = "Segador Bostón"
    decomposed = unicodedata.normalize("NFD", composed)
    assert decomposed != composed
    assert normalize_term(decomposed) == composed


def test_normalize_term_preserves_case():
    """Normalisation canonicalises spacing, never casing — display form is kept."""
    from borgesica.domain.glossary import normalize_term

    assert normalize_term("Alupi") == "Alupi"
    assert normalize_term("alupi") == "alupi"


# ---------------------------------------------------------------------------
# dedupe_glossary — collapse case-only duplicates
# ---------------------------------------------------------------------------


def test_dedupe_collapses_case_only_duplicates_keeping_first_form():
    """'Alupi' and 'alupi' are one term; the first-seen spelling survives."""
    from borgesica.domain.glossary import dedupe_glossary

    glossary = Glossary(
        entries=[
            GlossaryEntry(term="Alupi", translation="Alupi"),
            GlossaryEntry(term="alupi", translation="alupi"),
        ]
    )

    result = dedupe_glossary(glossary)

    assert [e.term for e in result.entries] == ["Alupi"]
    assert result.entries[0].translation == "Alupi"


def test_dedupe_collapses_whitespace_only_duplicates():
    """Terms differing only in spacing are the same term."""
    from borgesica.domain.glossary import dedupe_glossary

    glossary = Glossary(
        entries=[
            GlossaryEntry(term="Ddram cyfraith", translation="ddram cyfraith"),
            GlossaryEntry(term="Ddram  cyfraith ", translation="ddram cyfraith"),
        ]
    )

    result = dedupe_glossary(glossary)

    assert [e.term for e in result.entries] == ["Ddram cyfraith"]


def test_dedupe_lets_a_locked_duplicate_win_over_an_earlier_unlocked_one():
    """Locking is an explicit human decision — it outranks first-seen order."""
    from borgesica.domain.glossary import dedupe_glossary

    glossary = Glossary(
        entries=[
            GlossaryEntry(term="draoi", translation="druida"),
            GlossaryEntry(term="Draoi", translation="Draoi", locked=True),
        ]
    )

    result = dedupe_glossary(glossary)

    assert len(result.entries) == 1
    assert result.entries[0].term == "Draoi"
    assert result.entries[0].translation == "Draoi"
    assert result.entries[0].locked is True


def test_dedupe_keeps_the_first_locked_entry_when_several_are_locked():
    """Two locked variants still collapse to one — the first locked one wins."""
    from borgesica.domain.glossary import dedupe_glossary

    glossary = Glossary(
        entries=[
            GlossaryEntry(term="Caer", translation="Caer", locked=True),
            GlossaryEntry(term="caer", translation="fortaleza", locked=True),
        ]
    )

    result = dedupe_glossary(glossary)

    assert len(result.entries) == 1
    assert result.entries[0].term == "Caer"
    assert result.entries[0].translation == "Caer"


def test_dedupe_recovers_a_note_from_the_discarded_duplicate():
    """The note is the human's only context — dedupe must not be the thing that loses it."""
    from borgesica.domain.glossary import dedupe_glossary

    glossary = Glossary(
        entries=[
            GlossaryEntry(term="Crannog", translation="Crannog", note=None),
            GlossaryEntry(
                term="crannog", translation="crannog", note="Iron-age lake dwelling."
            ),
        ]
    )

    result = dedupe_glossary(glossary)

    assert len(result.entries) == 1
    assert result.entries[0].term == "Crannog"
    assert result.entries[0].note == "Iron-age lake dwelling."


def test_dedupe_keeps_the_winners_own_note_when_it_has_one():
    """A note on the surviving entry is never overwritten by a duplicate's note."""
    from borgesica.domain.glossary import dedupe_glossary

    glossary = Glossary(
        entries=[
            GlossaryEntry(term="Crannog", translation="Crannog", note="Kept."),
            GlossaryEntry(term="crannog", translation="crannog", note="Discarded."),
        ]
    )

    result = dedupe_glossary(glossary)

    assert result.entries[0].note == "Kept."


def test_dedupe_drops_entries_whose_term_is_blank():
    """A blank term can never match source text — it is pure prompt weight."""
    from borgesica.domain.glossary import dedupe_glossary

    glossary = Glossary(
        entries=[
            GlossaryEntry(term="   ", translation="algo"),
            GlossaryEntry(term="", translation="nada"),
            GlossaryEntry(term="Alupi", translation="Alupi"),
        ]
    )

    result = dedupe_glossary(glossary)

    assert [e.term for e in result.entries] == ["Alupi"]


def test_dedupe_normalizes_the_terms_it_keeps():
    """Surviving entries carry the normalised term, not the raw one."""
    from borgesica.domain.glossary import dedupe_glossary

    glossary = Glossary(entries=[GlossaryEntry(term=" Caer  Dathyl ", translation="Caer Dathyl")])

    result = dedupe_glossary(glossary)

    assert result.entries[0].term == "Caer Dathyl"


def test_dedupe_preserves_order_and_distinct_entries():
    """Deduplication is not reordering — distinct terms keep their sequence."""
    from borgesica.domain.glossary import dedupe_glossary

    glossary = Glossary(
        entries=[
            GlossaryEntry(term="Alupi", translation="Alupi"),
            GlossaryEntry(term="Crannog", translation="Crannog"),
            GlossaryEntry(term="alupi", translation="alupi"),
            GlossaryEntry(term="Draoi", translation="Draoi"),
        ]
    )

    result = dedupe_glossary(glossary)

    assert [e.term for e in result.entries] == ["Alupi", "Crannog", "Draoi"]


def test_dedupe_of_a_clean_glossary_is_a_no_op():
    """Nothing to collapse means nothing changes."""
    from borgesica.domain.glossary import dedupe_glossary

    glossary = Glossary(
        entries=[
            GlossaryEntry(term="Alupi", translation="Alupi"),
            GlossaryEntry(term="Draoi", translation="druida", locked=True, note="n"),
        ]
    )

    assert dedupe_glossary(glossary).entries == glossary.entries


# ---------------------------------------------------------------------------
# merge_additions — case-insensitive matching
# ---------------------------------------------------------------------------


def test_merge_rejects_an_addition_differing_only_in_case():
    """The 12 measured collisions were all case-only; matching must be case-insensitive."""
    from borgesica.domain.glossary import merge_additions

    glossary = Glossary(entries=[GlossaryEntry(term="Alupi", translation="Alupi")])
    additions = [GlossaryEntry(term="alupi", translation="alupi")]

    result = merge_additions(glossary, additions)

    assert [e.term for e in result.entries] == ["Alupi"]


def test_merge_rejects_an_addition_differing_only_in_case_from_a_locked_entry():
    """Locked precedence must survive a case difference too."""
    from borgesica.domain.glossary import merge_additions

    glossary = Glossary(
        entries=[GlossaryEntry(term="Draoi", translation="druida", locked=True)]
    )
    additions = [GlossaryEntry(term="draoi", translation="draoi")]

    result = merge_additions(glossary, additions)

    assert len(result.entries) == 1
    assert result.entries[0].translation == "druida"
    assert result.entries[0].locked is True


def test_merge_collapses_case_variants_inside_a_single_addition_batch():
    """One chunk can emit both spellings at once; only one may land."""
    from borgesica.domain.glossary import merge_additions

    additions = [
        GlossaryEntry(term="Caer", translation="Caer"),
        GlossaryEntry(term="caer", translation="caer"),
    ]

    result = merge_additions(Glossary(), additions)

    assert [e.term for e in result.entries] == ["Caer"]


def test_merge_cleans_case_duplicates_already_in_the_live_glossary():
    """Glossaries persisted before this fix are repaired on the next merge."""
    from borgesica.domain.glossary import merge_additions

    glossary = Glossary(
        entries=[
            GlossaryEntry(term="Alupi", translation="Alupi"),
            GlossaryEntry(term="alupi", translation="alupi"),
        ]
    )
    additions = [GlossaryEntry(term="Crannog", translation="Crannog")]

    result = merge_additions(glossary, additions)

    assert [e.term for e in result.entries] == ["Alupi", "Crannog"]


def test_merge_normalizes_the_term_of_an_accepted_addition():
    """An added term is stored in its normalised form."""
    from borgesica.domain.glossary import merge_additions

    additions = [GlossaryEntry(term="  Gleaner   patrol ", translation="patrulla Gleaner")]

    result = merge_additions(Glossary(), additions)

    assert result.entries[0].term == "Gleaner patrol"


def test_merge_drops_an_addition_whose_term_is_blank():
    """A blank term is never a usable addition."""
    from borgesica.domain.glossary import merge_additions

    result = merge_additions(Glossary(), [GlossaryEntry(term="  ", translation="algo")])

    assert result.entries == []


def test_merge_still_adds_genuinely_new_terms_as_unlocked():
    """The existing contract is unchanged for non-colliding additions."""
    from borgesica.domain.glossary import merge_additions

    glossary = Glossary(entries=[GlossaryEntry(term="Alupi", translation="Alupi")])
    additions = [GlossaryEntry(term="Draoi", translation="druida", locked=True, note="n")]

    result = merge_additions(glossary, additions)

    assert [e.term for e in result.entries] == ["Alupi", "Draoi"]
    assert result.entries[1].locked is False
    assert result.entries[1].note == "n"


# ---------------------------------------------------------------------------
# Extraction is deduplicated at the source
# ---------------------------------------------------------------------------


def test_llm_extractor_dedupes_its_own_output():
    """A seeded glossary must not start life with case-variant duplicates."""
    from borgesica.domain.glossary import LlmGlossaryExtractor

    canned = TranslationUnit(
        translation="dummy",
        summary_update="done",
        glossary_additions=[
            GlossaryEntry(term="Alupi", translation="Alupi"),
            GlossaryEntry(term="alupi", translation="alupi"),
        ],
    )
    provider = FakeTranslationProvider(canned_unit=canned)
    extractor = LlmGlossaryExtractor(provider=provider)

    glossary = extractor.extract("text", make_config(glossary_strategy="llm"))

    assert [e.term for e in glossary.entries] == ["Alupi"]
