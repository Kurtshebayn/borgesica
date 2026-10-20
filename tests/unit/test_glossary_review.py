"""Tests for the pre-translation glossary review: per-entry gender evidence and
deterministic risk flags, computed from the glossary, the extraction pass's
per-batch proposals and the English source.

Nothing here calls a provider: every signal is derived, so it is recomputed on
each read rather than stored.
"""
from __future__ import annotations

from borgesica.domain.glossary_review import review_glossary
from borgesica.domain.models import Glossary, GlossaryEntry, RenderingProposal


def entry(term: str, translation: str, **kwargs) -> GlossaryEntry:
    return GlossaryEntry(term=term, translation=translation, **kwargs)


def review(entries, proposals=None, source="", **kwargs):
    return review_glossary(
        Glossary(entries=entries), proposals or {}, source, **kwargs
    ).entries


# Wider than the 60-character pronoun window and free of letters, so each
# sentence is counted on its own and the filler adds nothing to the vocabulary.
_GAP = " " + "." * 80 + " "


def apart(*sentences: str) -> str:
    return _GAP.join(sentences)


def by_term(reviewed):
    return {item.entry.term: item for item in reviewed}


# ---------------------------------------------------------------------------
# Gender and its evidence
# ---------------------------------------------------------------------------


def test_every_entry_carries_its_pronoun_evidence() -> None:
    source = apart(*["Vis said he was ready."] * 3, "Vis knew she was wrong.")
    reviewed = by_term(review([entry("Vis", "Vis"), entry("Caten", "Caten")], source=source))

    assert (reviewed["Vis"].masculine_mentions, reviewed["Vis"].feminine_mentions) == (3, 1)
    assert (reviewed["Caten"].masculine_mentions, reviewed["Caten"].feminine_mentions) == (0, 0)


def test_the_seeded_gender_is_reported_with_its_source() -> None:
    source = apart(*["Vis said he was ready."] * 4)
    reviewed = by_term(review([entry("Vis", "Vis")], source=source, min_mentions=3))

    assert reviewed["Vis"].gender == "masculine"
    assert reviewed["Vis"].gender_source == "seeded"
    # The stored entry is reported as stored: deriving never rewrites it.
    assert reviewed["Vis"].entry.gender is None


def test_a_stored_gender_wins_over_the_evidence() -> None:
    source = apart(*["Vis said he was ready."] * 4)
    reviewed = by_term(
        review([entry("Vis", "Vis", gender="feminine")], source=source, min_mentions=3)
    )

    assert reviewed["Vis"].gender == "feminine"
    assert reviewed["Vis"].gender_source == "stored"


def test_a_character_with_enough_mentions_but_no_majority_is_flagged_undecided() -> None:
    # 2 masculine, 2 feminine: past the mention floor, under the ratio.
    source = apart(*["Aequa saw he left."] * 2, *["Aequa said she stayed."] * 2)
    reviewed = by_term(review([entry("Aequa", "Aequa")], source=source, min_mentions=4))

    assert reviewed["Aequa"].gender is None
    assert reviewed["Aequa"].gender_source is None
    assert "undecided_gender" in reviewed["Aequa"].flags


def test_too_few_mentions_is_not_undecided() -> None:
    source = apart("Aequa saw he left.", "Aequa said she stayed.")
    reviewed = by_term(review([entry("Aequa", "Aequa")], source=source, min_mentions=4))

    assert "undecided_gender" not in reviewed["Aequa"].flags


def test_a_translated_entry_is_never_undecided() -> None:
    # The seeder never classifies a translated term: its Spanish gender is
    # grammatical, so there is nothing to decide.
    source = apart(
        *["Concurrence saw he left."] * 2, *["Concurrence said she stayed."] * 2
    )
    reviewed = by_term(
        review([entry("Concurrence", "Concurrencia")], source=source, min_mentions=4)
    )

    assert "undecided_gender" not in reviewed["Concurrence"].flags


# ---------------------------------------------------------------------------
# Per-batch proposals
# ---------------------------------------------------------------------------


def test_disagreeing_batches_are_flagged_and_their_renderings_listed() -> None:
    proposals = {"birthright": ("Derecho de nacimiento", "Primogenitura", "derecho de nacimiento")}
    reviewed = by_term(
        review([entry("Birthright", "Derecho de nacimiento")], proposals=proposals)
    )

    assert "conflicting_renderings" in reviewed["Birthright"].flags
    # Grouped the way the plurality groups votes, most-proposed first.
    assert reviewed["Birthright"].proposals == [
        RenderingProposal(rendering="Derecho de nacimiento", batches=2),
        RenderingProposal(rendering="Primogenitura", batches=1),
    ]


def test_a_case_only_difference_is_not_a_conflict() -> None:
    proposals = {"will": ("Voluntad", "voluntad")}
    reviewed = by_term(review([entry("Will", "Voluntad")], proposals=proposals))

    assert "conflicting_renderings" not in reviewed["Will"].flags
    assert reviewed["Will"].proposals == [RenderingProposal(rendering="Voluntad", batches=2)]


def test_an_entry_no_batch_proposed_has_no_proposals() -> None:
    reviewed = by_term(review([entry("Will", "Voluntad")]))

    assert reviewed["Will"].proposals == []
    assert "conflicting_renderings" not in reviewed["Will"].flags


def test_some_batches_keeping_the_term_and_others_translating_it_is_a_split() -> None:
    proposals = {"octavii": ("Octavii", "Octavios", "Octavii")}
    reviewed = by_term(review([entry("Octavii", "Octavii")], proposals=proposals))

    assert "kept_vs_translated" in reviewed["Octavii"].flags
    assert "conflicting_renderings" in reviewed["Octavii"].flags


def test_batches_disagreeing_on_translations_only_are_not_a_split() -> None:
    proposals = {"birthright": ("Derecho de nacimiento", "Primogenitura")}
    reviewed = by_term(
        review([entry("Birthright", "Derecho de nacimiento")], proposals=proposals)
    )

    assert "kept_vs_translated" not in reviewed["Birthright"].flags


# ---------------------------------------------------------------------------
# Identity entries that are ordinary vocabulary
# ---------------------------------------------------------------------------


def test_a_capitalised_identity_term_the_source_writes_lowercase_is_flagged() -> None:
    source = "The Thrum rose. A low thrum filled the hall."
    reviewed = by_term(review([entry("Thrum", "Thrum")], source=source))

    assert "lowercase_identity" in reviewed["Thrum"].flags


def test_an_identity_term_spelled_lowercase_is_not_flagged() -> None:
    # An invented word the book always writes lowercase: the extractor kept it
    # verbatim, which is exactly right.
    source = "The alupi waited. Every alupi knew."
    reviewed = by_term(review([entry("alupi", "alupi")], source=source))

    assert "lowercase_identity" not in reviewed["alupi"].flags


def test_a_name_never_written_lowercase_is_not_flagged() -> None:
    source = "Caeror spoke. Then Caeror left."
    reviewed = by_term(review([entry("Caeror", "Caeror")], source=source))

    assert "lowercase_identity" not in reviewed["Caeror"].flags


def test_an_accented_name_is_not_split_into_lowercase_fragments() -> None:
    # An ASCII word pattern reads "Artán" as "Art" + "n", and "n" is
    # lowercase somewhere in any book.
    source = "Artán spoke. The n of the word."
    reviewed = by_term(review([entry("Artán", "Artán")], source=source))

    assert "lowercase_identity" not in reviewed["Artán"].flags


# ---------------------------------------------------------------------------
# Possible glosses on invented words
# ---------------------------------------------------------------------------


def test_a_rendering_unrelated_to_a_never_lowercase_term_is_a_possible_gloss() -> None:
    source = "He called him Leathfhear. Leathfhear, he said."
    reviewed = by_term(review([entry("Leathfhear", "medio hombre")], source=source))

    assert "possible_gloss" in reviewed["Leathfhear"].flags


def test_a_rendering_sharing_a_word_with_the_term_is_not_a_gloss() -> None:
    source = "The Catenan Academy stood."
    reviewed = by_term(
        review([entry("Catenan Academy", "Academia Catenan")], source=source)
    )

    assert "possible_gloss" not in reviewed["Catenan Academy"].flags


def test_a_shared_word_counts_across_accents() -> None:
    source = "Pyramid of Ka. The Pyramid stood."
    reviewed = by_term(review([entry("Ka Pyramid", "Pirámide de Ká")], source=source))

    assert "possible_gloss" not in reviewed["Ka Pyramid"].flags


def test_a_term_the_source_also_writes_lowercase_is_not_a_gloss() -> None:
    # Ordinary English vocabulary, translated: the expected case.
    source = "The Senate met. The senate voted."
    reviewed = by_term(review([entry("Senate", "el Senado")], source=source))

    assert "possible_gloss" not in reviewed["Senate"].flags


def test_a_term_without_words_is_never_a_gloss() -> None:
    # Nothing to compare: with no word in the term, "shares no word" and
    # "never written lowercase" are both vacuous, not evidence of a gloss.
    source = "In 1984 they left. In 1984, again."
    reviewed = by_term(
        review([entry("1984", "mil novecientos ochenta y cuatro")], source=source)
    )

    assert "possible_gloss" not in reviewed["1984"].flags


def test_an_identity_entry_is_never_a_gloss() -> None:
    source = "Leathfhear, he said."
    reviewed = by_term(review([entry("Leathfhear", "Leathfhear")], source=source))

    assert "possible_gloss" not in reviewed["Leathfhear"].flags


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_entries_keep_the_glossary_order_and_flags_a_fixed_order() -> None:
    source = "The Thrum rose. A low thrum filled the hall."
    proposals = {"thrum": ("Thrum", "zumbido")}
    reviewed = review(
        [entry("Thrum", "Thrum"), entry("Alupi", "Alupi")],
        proposals=proposals,
        source=source,
    )

    assert [item.entry.term for item in reviewed] == ["Thrum", "Alupi"]
    assert reviewed[0].flags == [
        "conflicting_renderings",
        "kept_vs_translated",
        "lowercase_identity",
    ]
    assert reviewed[1].flags == []


def test_the_thresholds_used_are_reported_with_the_entries() -> None:
    result = review_glossary(Glossary(), {}, "", min_mentions=5, min_ratio=0.8)

    assert (result.min_gender_mentions, result.min_gender_ratio) == (5, 0.8)
    assert result.entries == []
