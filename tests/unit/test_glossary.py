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

    result, _dropped = dedupe_glossary(glossary)

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

    result, _dropped = dedupe_glossary(glossary)

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

    result, _dropped = dedupe_glossary(glossary)

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

    result, _dropped = dedupe_glossary(glossary)

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

    result, _dropped = dedupe_glossary(glossary)

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

    result, _dropped = dedupe_glossary(glossary)

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

    result, _dropped = dedupe_glossary(glossary)

    assert [e.term for e in result.entries] == ["Alupi"]


def test_dedupe_normalizes_the_terms_it_keeps():
    """Surviving entries carry the normalised term, not the raw one."""
    from borgesica.domain.glossary import dedupe_glossary

    glossary = Glossary(entries=[GlossaryEntry(term=" Caer  Dathyl ", translation="Caer Dathyl")])

    result, _dropped = dedupe_glossary(glossary)

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

    result, _dropped = dedupe_glossary(glossary)

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

    assert dedupe_glossary(glossary)[0].entries == glossary.entries


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


# ---------------------------------------------------------------------------
# B1d — reversed / contradictory entries
#
# The real 491-entry glossary of job 13b43ac6 contained six entries pointing
# the wrong way, four of them as outright inverse pairs:
#     Birthright -> Derecho de Nacimiento  ||  Derecho de Nacimiento -> Birthright
#     Religion   -> Religion (es)          ||  Religion (es)         -> Religion
#     Will       -> Voluntad               ||  Voluntad              -> Will
#     Will cage  -> Jaula de Voluntad      ||  jaula de Voluntad     -> Will cage
# Injected as-is they instruct translating INTO English. In all six the
# English-source direction was recorded FIRST and the Spanish rendering leaked
# back as a source term later, after the model had already produced it.
# ---------------------------------------------------------------------------


def test_drop_reversed_keeps_the_first_half_of_an_inverse_pair():
    """A -> B and B -> A cannot both be right; the earlier one is the source direction."""
    from borgesica.domain.glossary import drop_reversed_entries

    glossary = Glossary(
        entries=[
            GlossaryEntry(term="Birthright", translation="Derecho de Nacimiento"),
            GlossaryEntry(term="Derecho de Nacimiento", translation="Birthright"),
        ]
    )

    cleaned, dropped = drop_reversed_entries(glossary)

    assert [e.term for e in cleaned.entries] == ["Birthright"]
    assert [e.term for e in dropped] == ["Derecho de Nacimiento"]


def test_drop_reversed_catches_a_reversal_with_no_exact_inverse():
    """'Placement -> Colocacion' makes any '... -> Placement' entry contradictory."""
    from borgesica.domain.glossary import drop_reversed_entries

    glossary = Glossary(
        entries=[
            GlossaryEntry(term="Placement", translation="Colocacion"),
            GlossaryEntry(term="Asignacion", translation="Placement"),
        ]
    )

    cleaned, dropped = drop_reversed_entries(glossary)

    assert [e.term for e in cleaned.entries] == ["Placement"]
    assert [e.term for e in dropped] == ["Asignacion"]


def test_drop_reversed_keeps_a_mapping_whose_output_is_only_a_do_not_translate_term():
    """The false positive that must never come back.

    'The Tongue -> la Lengua' is correct even though 'la Lengua' is itself an
    entry, because that entry is 'la Lengua -> la Lengua' — a do-not-translate
    term, not something the glossary says must be translated further. On the
    real glossary a rule without this exemption discarded 8 valid mappings.
    """
    from borgesica.domain.glossary import drop_reversed_entries

    glossary = Glossary(
        entries=[
            GlossaryEntry(term="la Lengua", translation="la Lengua"),
            GlossaryEntry(term="The Tongue", translation="la Lengua"),
        ]
    )

    cleaned, dropped = drop_reversed_entries(glossary)

    assert [e.term for e in cleaned.entries] == ["la Lengua", "The Tongue"]
    assert dropped == []


def test_drop_reversed_never_drops_a_locked_entry():
    """A locked entry is a human decision; inference does not get to overrule it."""
    from borgesica.domain.glossary import drop_reversed_entries

    glossary = Glossary(
        entries=[
            GlossaryEntry(term="Will", translation="Voluntad"),
            GlossaryEntry(term="Voluntad", translation="Will", locked=True),
        ]
    )

    cleaned, dropped = drop_reversed_entries(glossary)

    assert [e.term for e in cleaned.entries] == ["Will", "Voluntad"]
    assert dropped == []


def test_drop_reversed_never_drops_an_identity_entry():
    """'Aaru -> Aaru' states no direction, so it can never contradict one."""
    from borgesica.domain.glossary import drop_reversed_entries

    glossary = Glossary(
        entries=[
            GlossaryEntry(term="Gleaner", translation="Segador"),
            GlossaryEntry(term="Segador", translation="Segador"),
        ]
    )

    cleaned, dropped = drop_reversed_entries(glossary)

    assert [e.term for e in cleaned.entries] == ["Gleaner", "Segador"]
    assert dropped == []


def test_drop_reversed_is_case_and_whitespace_insensitive():
    """'jaula de Voluntad' must still contradict 'Jaula de Voluntad'."""
    from borgesica.domain.glossary import drop_reversed_entries

    glossary = Glossary(
        entries=[
            GlossaryEntry(term="Will cage", translation="Jaula  de Voluntad"),
            GlossaryEntry(term="jaula de voluntad", translation="Will cage"),
        ]
    )

    cleaned, dropped = drop_reversed_entries(glossary)

    assert [e.term for e in cleaned.entries] == ["Will cage"]
    assert len(dropped) == 1


def test_drop_reversed_leaves_a_consistent_glossary_untouched():
    """No contradiction means no drops and no reordering."""
    from borgesica.domain.glossary import drop_reversed_entries

    glossary = Glossary(
        entries=[
            GlossaryEntry(term="Gleaner", translation="Segador"),
            GlossaryEntry(term="Gleaners", translation="Segadores"),
            GlossaryEntry(term="Aaru", translation="Aaru"),
        ]
    )

    cleaned, dropped = drop_reversed_entries(glossary)

    assert cleaned.entries == glossary.entries
    assert dropped == []


def test_merge_rejects_a_reversed_addition():
    """A chunk that emits the Spanish rendering as a source term is refused."""
    from borgesica.domain.glossary import merge_additions

    glossary = Glossary(entries=[GlossaryEntry(term="Will", translation="Voluntad")])
    additions = [GlossaryEntry(term="Voluntad", translation="Will")]

    result = merge_additions(glossary, additions)

    assert [e.term for e in result.entries] == ["Will"]


def test_merge_cleans_a_reversed_pair_already_in_the_live_glossary():
    """Glossaries persisted before this rule repair themselves on the next merge."""
    from borgesica.domain.glossary import merge_additions

    glossary = Glossary(
        entries=[
            GlossaryEntry(term="Religion", translation="Religion-es"),
            GlossaryEntry(term="Religion-es", translation="Religion"),
        ]
    )
    additions = [GlossaryEntry(term="Aaru", translation="Aaru")]

    result = merge_additions(glossary, additions)

    assert [e.term for e in result.entries] == ["Religion", "Aaru"]


# ---------------------------------------------------------------------------
# Confirmation by repetition — the first draw no longer decides the book
# ---------------------------------------------------------------------------


def test_a_minority_first_draw_is_replaced_once_the_majority_reaches_quorum():
    """Measured on 422 real calls: 'Birthright' drew 10 distinct renderings and
    the dominant one won only 79% of first draws. Committing that single draw
    pinned a minority rendering for the remaining ~470 chunks one run in five.
    """
    from borgesica.domain.glossary import GlossaryVotes, apply_additions

    glossary, votes = Glossary(), GlossaryVotes()

    glossary, votes = apply_additions(
        glossary, votes, [GlossaryEntry(term="Birthright", translation="Primogenitura")]
    )
    # Committed immediately: a term seen once must still reach the prompt, or
    # rare terms would never be glossed at all.
    assert [(e.term, e.translation) for e in glossary.entries] == [
        ("Birthright", "Primogenitura")
    ]

    for _ in range(2):
        glossary, votes = apply_additions(
            glossary,
            votes,
            [GlossaryEntry(term="Birthright", translation="Derecho de Nacimiento")],
        )

    assert [(e.term, e.translation) for e in glossary.entries] == [
        ("Birthright", "Derecho de Nacimiento")
    ]


def test_a_term_proposed_once_keeps_its_first_draw():
    """Rare terms must not be starved. "Will shells" appears in four chunks of a
    502-chunk book, so a mechanism that only glossed terms reaching quorum would
    leave it out of the prompt entirely.
    """
    from borgesica.domain.glossary import GlossaryVotes, apply_additions

    glossary, votes = apply_additions(
        Glossary(),
        GlossaryVotes(),
        [GlossaryEntry(term="Will shells", translation="proyectiles de Voluntad")],
    )

    assert [(e.term, e.translation) for e in glossary.entries] == [
        ("Will shells", "proyectiles de Voluntad")
    ]
    assert votes.by_term == {"will shells": ("proyectiles de Voluntad",)}


def test_a_settled_term_ignores_later_proposals():
    """Once quorum decided a rendering, the term stops listening — otherwise the
    glossary would keep churning for the rest of the book.
    """
    from borgesica.domain.glossary import GlossaryVotes, apply_additions

    glossary, votes = Glossary(), GlossaryVotes()
    for _ in range(3):
        glossary, votes = apply_additions(
            glossary, votes, [GlossaryEntry(term="Caer", translation="Caer")]
        )
    assert votes.by_term == {}

    glossary, votes = apply_additions(
        glossary, votes, [GlossaryEntry(term="Caer", translation="Otra Cosa")]
    )

    assert [(e.term, e.translation) for e in glossary.entries] == [("Caer", "Caer")]


def test_a_locked_term_takes_no_votes_and_is_never_revised():
    """Locking is a human decision; inference does not get to overrule it."""
    from borgesica.domain.glossary import GlossaryVotes, apply_additions

    glossary = Glossary(
        entries=[GlossaryEntry(term="Birthright", translation="Derecho de Nacimiento", locked=True)]
    )
    votes = GlossaryVotes()
    for _ in range(4):
        glossary, votes = apply_additions(
            glossary, votes, [GlossaryEntry(term="Birthright", translation="Primogenitura")]
        )

    assert [(e.term, e.translation) for e in glossary.entries] == [
        ("Birthright", "Derecho de Nacimiento")
    ]
    assert votes.by_term == {}


def test_quorum_breaks_a_three_way_tie_by_earliest_proposal():
    """With no repetition to go on, the first draw is still the best evidence."""
    from borgesica.domain.glossary import GlossaryVotes, apply_additions

    glossary, votes = Glossary(), GlossaryVotes()
    for rendering in ("Herencia", "Primogenitura", "Legítimo Derecho"):
        glossary, votes = apply_additions(
            glossary, votes, [GlossaryEntry(term="Birthright", translation=rendering)]
        )

    assert [(e.term, e.translation) for e in glossary.entries] == [
        ("Birthright", "Herencia")
    ]


def test_case_variants_of_one_rendering_count_as_the_same_vote():
    """13 of 83 real proposals for "Will cage" differed only in capitalisation;
    counting them apart would split the majority against itself.
    """
    from borgesica.domain.glossary import GlossaryVotes, apply_additions

    glossary, votes = Glossary(), GlossaryVotes()
    for rendering in ("Jaula de Voluntad", "jaula de voluntad", "otra cosa"):
        glossary, votes = apply_additions(
            glossary, votes, [GlossaryEntry(term="Will cage", translation=rendering)]
        )

    assert [(e.term, e.translation) for e in glossary.entries] == [
        ("Will cage", "Jaula de Voluntad")
    ]


def test_a_glossary_persisted_before_voting_existed_is_treated_as_settled():
    """A resumed job carries entries but no votes. They must not reopen."""
    from borgesica.domain.glossary import GlossaryVotes, apply_additions

    glossary = Glossary(entries=[GlossaryEntry(term="Aaru", translation="Aaru")])

    glossary, votes = apply_additions(
        glossary, GlossaryVotes(), [GlossaryEntry(term="Aaru", translation="Aarú")]
    )

    assert [(e.term, e.translation) for e in glossary.entries] == [("Aaru", "Aaru")]
    assert votes.by_term == {}


def test_a_reversed_proposal_is_still_dropped():
    """The direction guard runs on the result like every other glossary boundary."""
    from borgesica.domain.glossary import GlossaryVotes, apply_additions

    glossary = Glossary(entries=[GlossaryEntry(term="Will", translation="Voluntad")])

    glossary, _votes = apply_additions(
        glossary, GlossaryVotes(), [GlossaryEntry(term="Voluntad", translation="Will")]
    )

    assert [e.term for e in glossary.entries] == ["Will"]


# ---------------------------------------------------------------------------
# The first draw survives settlement, so the correction rate is reportable
# ---------------------------------------------------------------------------


def test_a_committed_term_records_the_rendering_it_was_committed_with():
    """The vote tally is erased when a term settles, so the first draw has to be
    kept on the entry or it is unrecoverable.
    """
    from borgesica.domain.glossary import GlossaryVotes, apply_additions

    glossary, _votes = apply_additions(
        Glossary(),
        GlossaryVotes(),
        [GlossaryEntry(term="Birthright", translation="Primogenitura")],
    )

    assert glossary.entries[0].first_draw == "Primogenitura"


def test_a_settled_term_still_remembers_the_draw_quorum_replaced():
    """The whole point: after quorum the entry carries both renderings, so a
    reader can say the mechanism CHANGED this term rather than confirmed it.
    """
    from borgesica.domain.glossary import GlossaryVotes, apply_additions

    glossary, votes = apply_additions(
        Glossary(),
        GlossaryVotes(),
        [GlossaryEntry(term="Birthright", translation="Primogenitura")],
    )
    for _ in range(2):
        glossary, votes = apply_additions(
            glossary,
            votes,
            [GlossaryEntry(term="Birthright", translation="Derecho de Nacimiento")],
        )

    assert votes.by_term == {}
    entry = glossary.entries[0]
    assert (entry.translation, entry.first_draw) == (
        "Derecho de Nacimiento",
        "Primogenitura",
    )


def test_settlement_counts_separate_corrections_from_confirmations():
    """The 2026-08-14 run of job 9be143da settled 32 of 549 terms and nothing
    could say how many of those 32 the mechanism actually corrected.
    """
    from borgesica.domain.glossary import (
        GlossaryVotes,
        apply_additions,
        settlement_counts,
    )

    glossary, votes = apply_additions(
        Glossary(),
        GlossaryVotes(),
        [GlossaryEntry(term="Birthright", translation="Primogenitura")],
    )
    for _ in range(2):
        glossary, votes = apply_additions(
            glossary,
            votes,
            [GlossaryEntry(term="Birthright", translation="Derecho de Nacimiento")],
        )
    for _ in range(3):
        glossary, votes = apply_additions(
            glossary, votes, [GlossaryEntry(term="Aaru", translation="Aaru")]
        )

    counts = settlement_counts(glossary, votes)

    assert (counts.changed, counts.confirmed, counts.settled) == (1, 1, 2)


def test_a_term_still_collecting_votes_is_not_settled_either_way():
    """A first draw that has not reached quorum yet decided nothing. Counting it
    as confirmed would report the old single-draw behaviour as a confirmation.
    """
    from borgesica.domain.glossary import (
        GlossaryVotes,
        apply_additions,
        settlement_counts,
    )

    glossary, votes = apply_additions(
        Glossary(),
        GlossaryVotes(),
        [GlossaryEntry(term="Will shells", translation="proyectiles de Voluntad")],
    )

    counts = settlement_counts(glossary, votes)

    assert (counts.changed, counts.confirmed, counts.settled) == (0, 0, 0)


def test_an_entry_stored_before_the_first_draw_was_recorded_is_not_counted():
    """Job 9be143da's own glossary predates the column. Its terms are settled but
    their first draw is genuinely unknown, and the report must not invent one.
    """
    from borgesica.domain.glossary import GlossaryVotes, settlement_counts

    glossary = Glossary(entries=[GlossaryEntry(term="Aaru", translation="Aaru")])

    counts = settlement_counts(glossary, GlossaryVotes())

    assert (counts.changed, counts.confirmed, counts.settled) == (0, 0, 0)


def test_a_case_only_difference_from_the_first_draw_is_a_confirmation():
    """``_plurality`` groups renderings by casefold, so two capitalisations are
    one rendering. The report has to agree, or it would count a correction the
    mechanism never made.
    """
    from borgesica.domain.glossary import GlossaryVotes, settlement_counts

    glossary = Glossary(
        entries=[
            GlossaryEntry(
                term="Will cage",
                translation="Jaula de Voluntad",
                first_draw="jaula de voluntad",
            )
        ]
    )

    counts = settlement_counts(glossary, GlossaryVotes())

    assert (counts.changed, counts.confirmed) == (0, 1)


# ---------------------------------------------------------------------------
# Character gender seeding — derived from the English source, with a margin
# ---------------------------------------------------------------------------

# Wide enough that no mention's window reaches the next one, so each name
# occurrence is credited with exactly its own pronoun and the counts under
# test are the ones the passage literally states.
_GAP = " " + ("filler " * 40)


def _passage(name: str, pronoun: str, count: int) -> str:
    return _GAP.join(f"{name} paused and {pronoun} nodded." for _ in range(count))


def _mixed(name: str, masculine: int, feminine: int) -> str:
    return _passage(name, "he", masculine) + _GAP + _passage(name, "she", feminine)


def _seed(glossary: Glossary, source_text: str) -> Glossary:
    """Scan the source for gender evidence and apply it, as a run does."""
    from borgesica.domain.glossary import (
        character_gender_evidence,
        seed_character_gender,
    )

    return seed_character_gender(glossary, character_gender_evidence(source_text))


def test_seeds_masculine_gender_from_overwhelming_source_evidence():
    """Caeror ran 108 masculine to 4 feminine across the real book."""
    glossary = Glossary(entries=[GlossaryEntry(term="Caeror", translation="Caeror")])

    seeded = _seed(glossary, _passage("Caeror", "he", 25))

    assert seeded.entries[0].gender == "masculine"


def test_seeds_feminine_gender_from_overwhelming_source_evidence():
    """Lanistia ran 11 masculine to 60 feminine across the real book."""
    glossary = Glossary(entries=[GlossaryEntry(term="Lanistia", translation="Lanistia")])

    seeded = _seed(glossary, _passage("Lanistia", "she", 25))

    assert seeded.entries[0].gender == "feminine"


def test_leaves_a_thinly_mentioned_name_unclassified():
    """A unanimous handful of mentions is not evidence.

    A 60-character window catches whoever else is standing nearby, so a small
    sample is mostly noise. Unclassified is the honest answer and costs
    nothing: the summary's naming rule still applies to that character.
    """
    glossary = Glossary(entries=[GlossaryEntry(term="Tara", translation="Tara")])

    seeded = _seed(glossary, _passage("Tara", "she", 5))

    assert seeded.entries[0].gender is None


def test_leaves_a_split_name_unclassified():
    """The real reason the margin exists: Aequa ran 51/115 and Emissa 16/32 —
    66-69% feminine, plainly female to a reader and still under the threshold.
    Guessing them right would not have been knowledge, so they stay unset.
    """
    glossary = Glossary(entries=[GlossaryEntry(term="Aequa", translation="Aequa")])

    seeded = _seed(glossary, _mixed("Aequa", masculine=13, feminine=7))

    assert seeded.entries[0].gender is None


def test_classifies_exactly_at_the_margin():
    """The threshold is inclusive — 70/30 on 20 mentions classifies."""
    glossary = Glossary(entries=[GlossaryEntry(term="Vis", translation="Vis")])

    seeded = _seed(glossary, _mixed("Vis", masculine=14, feminine=6))

    assert seeded.entries[0].gender == "masculine"


def test_never_overwrites_an_existing_classification():
    """A gender already set was either seeded from a fuller text or chosen by
    a human. Counting pronouns again must not silently overrule either.
    """
    glossary = Glossary(
        entries=[GlossaryEntry(term="Vis", translation="Vis", gender="masculine")]
    )

    seeded = _seed(glossary, _passage("Vis", "she", 40))

    assert seeded.entries[0].gender == "masculine"


def test_leaves_non_character_terms_alone():
    """Most of a glossary is places and invented nouns, not people."""
    glossary = Glossary(
        entries=[
            GlossaryEntry(term="Birthright", translation="Derecho de Nacimiento"),
            GlossaryEntry(term="Aaru", translation="Aaru"),
        ]
    )

    seeded = _seed(glossary, _passage("Caeror", "he", 25))

    assert [e.gender for e in seeded.entries] == [None, None]


def test_seeding_preserves_every_other_field():
    """Seeding sets one field; it is not a glossary rewrite."""
    glossary = Glossary(
        entries=[
            GlossaryEntry(
                term="Caeror",
                translation="Caeror",
                locked=True,
                note="the other one on stage",
                first_draw="Caeror",
            )
        ]
    )

    entry = _seed(glossary, _passage("Caeror", "he", 25)).entries[0]

    assert entry.gender == "masculine"
    assert (entry.term, entry.translation) == ("Caeror", "Caeror")
    assert entry.locked is True
    assert entry.note == "the other one on stage"
    assert entry.first_draw == "Caeror"


def test_leaves_a_common_noun_unclassified_however_strong_the_evidence():
    """Pronoun evidence tells you a PERSON's gender, never a noun's.

    Measured on the real book: the seeder classified "Religion", "Governance",
    "Military" and "Concurrence" masculine, because capitalised common nouns
    sit near pronouns like anyone else. Those are not characters, and Spanish
    grammatical gender governs them instead — "la religión" is feminine, so
    the anchor would have overridden a correct agreement with a wrong one.
    The fix is the word's own casing: a proper name essentially never appears
    lowercase, and a common noun does.
    """
    glossary = Glossary(
        entries=[GlossaryEntry(term="Religion", translation="Religion")]
    )
    source = _passage("Religion", "he", 25) + _GAP + "a matter of religion at last"

    seeded = _seed(glossary, source)

    assert seeded.entries[0].gender is None


def test_a_sentence_initial_name_is_still_a_proper_name():
    """The lowercase test must read real lowercase use, not capitalisation that
    merely happens to start a sentence — otherwise every name that ever opens
    a sentence would be disqualified.
    """
    glossary = Glossary(entries=[GlossaryEntry(term="Caeror", translation="Caeror")])
    source = _passage("Caeror", "he", 25) + _GAP + "Caeror waited alone."

    seeded = _seed(glossary, source)

    assert seeded.entries[0].gender == "masculine"


def test_leaves_a_translated_term_unclassified():
    """The anchor only governs a name carried into Spanish UNCHANGED.

    Once a term is translated, its Spanish gender is GRAMMATICAL and belongs
    to the Spanish noun, not to whatever the English referent was. Measured on
    the real book: "Concurrence", "Governance" and "Religion" are capitalised
    institutions that never appear lowercase, so casing alone did not catch
    them — and declaring "Concurrencia" masculine because the English noun sat
    near "he" would override a correct agreement with a wrong one.

    A character whose name IS translated loses its anchor to this rule. That
    is the safe direction: unclassified, and still covered by the naming rule.
    """
    glossary = Glossary(
        entries=[GlossaryEntry(term="Concurrence", translation="Concurrencia")]
    )

    seeded = _seed(glossary, _passage("Concurrence", "he", 25))

    assert seeded.entries[0].gender is None
