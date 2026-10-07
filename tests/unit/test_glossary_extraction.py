"""Tests for the batched whole-book glossary extraction pass (domain half).

Strict TDD: tests written before implementation.

Covers:
  - plan_extraction_batches: greedy grouping of consecutive chunks by tokens;
  - LlmGlossaryExtractor.extract_batch: one provider call, raw additions and
    the real usage of that call;
  - merge_extraction_batches: plurality across batches, with every proposed
    rendering kept per term.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from borgesica.domain.glossary import (
    EXTRACTION_BATCH_TOKENS,
    LlmGlossaryExtractor,
    merge_extraction_batches,
    plan_extraction_batches,
    settlement_counts,
)
from borgesica.domain.models import (
    ExtractionBatch,
    Glossary,
    GlossaryEntry,
    GlossaryVotes,
    JobConfig,
    SourceType,
    TranslationResult,
    TranslationUnit,
    Usage,
)


def make_config(**kwargs) -> JobConfig:
    defaults = dict(source_type=SourceType.EPUB, model="claude-haiku-4-5")
    defaults.update(kwargs)
    return JobConfig(**defaults)


def words(n: int) -> str:
    """A text of ``n`` tokens under the word-count tokenizer used below."""
    return " ".join(["w"] * n)


def count_words(text: str) -> int:
    return len(text.split())


def entry(term: str, translation: str, **kwargs) -> GlossaryEntry:
    return GlossaryEntry(term=term, translation=translation, **kwargs)


@dataclass
class ScriptedExtractionProvider:
    """Returns one scripted list of glossary additions per call, in order.

    The shared FakeTranslationProvider returns the same canned unit on every
    call and derives usage from the text; extraction needs different additions
    per batch and a usage value the test controls.
    """

    additions_per_call: list[list[GlossaryEntry]]
    usage: Usage = field(default_factory=lambda: Usage(input_tokens=1200, output_tokens=340))
    call_log: list[tuple[str, str, str]] = field(default_factory=list)

    def translate(
        self, system: str, user: str, model: str, segment_count: int | None = None
    ) -> TranslationResult:
        index = len(self.call_log)
        self.call_log.append((system, user, model))
        unit = TranslationUnit(
            translation="",
            translations=[],
            summary_update="Terminology extraction complete.",
            glossary_additions=self.additions_per_call[index],
        )
        return TranslationResult(unit=unit, usage=self.usage)

    def count_tokens(self, text: str, model: str) -> int:  # noqa: ARG002
        return count_words(text)

    def price(self, model: str) -> tuple[float, float]:  # noqa: ARG002
        return (1.0, 5.0)

    def cache_price(self, model: str) -> float:  # noqa: ARG002
        return 1.0


# ---------------------------------------------------------------------------
# plan_extraction_batches
# ---------------------------------------------------------------------------


def test_batch_budget_is_six_thousand_tokens() -> None:
    assert EXTRACTION_BATCH_TOKENS == 6000


def test_plan_of_no_chunks_is_no_batches() -> None:
    assert plan_extraction_batches([], count_words) == []


def test_plan_groups_consecutive_chunks_within_budget() -> None:
    texts = [words(3), words(3), words(3), words(3)]

    assert plan_extraction_batches(texts, count_words, budget=6) == [[0, 1], [2, 3]]


def test_plan_fills_a_batch_up_to_the_budget_inclusive() -> None:
    texts = [words(2), words(3), words(1), words(4)]

    # 2 + 3 + 1 == 6 fits exactly; the 4 starts a new batch.
    assert plan_extraction_batches(texts, count_words, budget=6) == [[0, 1, 2], [3]]


def test_plan_gives_an_oversized_chunk_its_own_batch() -> None:
    texts = [words(2), words(10), words(2)]

    assert plan_extraction_batches(texts, count_words, budget=5) == [[0], [1], [2]]


def test_plan_never_emits_an_empty_batch_when_the_first_chunk_is_oversized() -> None:
    texts = [words(10), words(1), words(1)]

    assert plan_extraction_batches(texts, count_words, budget=5) == [[0], [1, 2]]


def test_plan_keeps_every_chunk_exactly_once_in_order() -> None:
    texts = [words(n) for n in (5, 1, 7, 2, 2, 9, 3)]

    batches = plan_extraction_batches(texts, count_words, budget=8)

    assert [i for batch in batches for i in batch] == list(range(len(texts)))
    assert all(batch for batch in batches)


def test_plan_default_budget_holds_seven_prose_chunks_of_800_tokens() -> None:
    seven = [words(800)] * 7
    eight = [words(800)] * 8

    assert plan_extraction_batches(seven, count_words) == [list(range(7))]
    assert plan_extraction_batches(eight, count_words) == [list(range(7)), [7]]


# ---------------------------------------------------------------------------
# LlmGlossaryExtractor.extract_batch
# ---------------------------------------------------------------------------


def test_extract_batch_returns_the_raw_additions_and_the_real_usage() -> None:
    additions = [entry("Vis", "Vis"), entry("vis", "Vis"), entry("Will", "Voluntad")]
    usage = Usage(input_tokens=4321, output_tokens=987, cached_input_tokens=100)
    provider = ScriptedExtractionProvider([additions], usage=usage)

    batch = LlmGlossaryExtractor(provider).extract_batch("Some text.", make_config())

    assert isinstance(batch, ExtractionBatch)
    # Raw: the case duplicate is still there; merging is a separate step.
    assert batch.entries == additions
    assert batch.usage == usage


def test_extract_batch_makes_one_call_with_the_extraction_prompt() -> None:
    provider = ScriptedExtractionProvider([[]])
    config = make_config(model="deepseek-v4-flash")

    LlmGlossaryExtractor(provider).extract_batch("The Hierarchy rules.", config)

    assert len(provider.call_log) == 1
    system, user, model = provider.call_log[0]
    assert "terminology extractor" in system
    assert user == "Extract terminology from the following text:\n\nThe Hierarchy rules."
    assert model == "deepseek-v4-flash"


def test_extract_and_extract_batch_send_the_same_prompts() -> None:
    batch_provider = ScriptedExtractionProvider([[]])
    whole_provider = ScriptedExtractionProvider([[]])

    LlmGlossaryExtractor(batch_provider).extract_batch("Text.", make_config())
    LlmGlossaryExtractor(whole_provider).extract("Text.", make_config())

    assert batch_provider.call_log == whole_provider.call_log


# ---------------------------------------------------------------------------
# merge_extraction_batches
# ---------------------------------------------------------------------------


def test_merge_of_no_batches_is_empty() -> None:
    glossary, proposals = merge_extraction_batches([])

    assert glossary == Glossary()
    assert proposals == {}


def test_merge_records_a_proposal_for_every_term() -> None:
    glossary, proposals = merge_extraction_batches(
        [[entry("Vis", "Vis"), entry("Will", "Voluntad")]]
    )

    assert [(e.term, e.translation) for e in glossary.entries] == [
        ("Vis", "Vis"),
        ("Will", "Voluntad"),
    ]
    assert proposals == {"vis": ("Vis",), "will": ("Voluntad",)}


def test_merge_picks_the_plurality_and_keeps_every_proposal_in_batch_order() -> None:
    batches = [
        [entry("Birthright", "Derecho de nacimiento")],
        [entry("Birthright", "Primogenitura")],
        [entry("Birthright", "Primogenitura")],
    ]

    glossary, proposals = merge_extraction_batches(batches)

    assert glossary.entries[0].translation == "Primogenitura"
    assert proposals["birthright"] == (
        "Derecho de nacimiento",
        "Primogenitura",
        "Primogenitura",
    )


def test_merge_tie_goes_to_the_earliest_batch() -> None:
    batches = [
        [entry("Birthright", "Derecho de nacimiento")],
        [entry("Birthright", "Primogenitura")],
    ]

    glossary, _proposals = merge_extraction_batches(batches)

    assert glossary.entries[0].translation == "Derecho de nacimiento"


def test_merge_groups_renderings_by_case() -> None:
    batches = [
        [entry("Will cage", "Jaula de Voluntad")],
        [entry("Will cage", "Otra cosa")],
        [entry("Will cage", "jaula de voluntad")],
    ]

    glossary, proposals = merge_extraction_batches(batches)

    assert glossary.entries[0].translation == "Jaula de Voluntad"
    # The proposals keep each batch's own spelling: the conflict flag later
    # compares them casefolded, but the reviewer sees what was proposed.
    assert proposals["will cage"] == ("Jaula de Voluntad", "Otra cosa", "jaula de voluntad")


def test_merge_counts_a_term_once_per_batch() -> None:
    batches = [
        [entry("Vis", "Vis"), entry("VIS", "Visto"), entry("vis", "Visto")],
        [entry("Vis", "Visto")],
    ]

    glossary, proposals = merge_extraction_batches(batches)

    # One vote per batch: "Vis" from batch 0, "Visto" from batch 1 — a tie the
    # earliest batch wins. Repeats inside batch 0 must not outvote batch 1.
    assert proposals == {"vis": ("Vis", "Visto")}
    assert glossary.entries[0].translation == "Vis"


def test_merge_normalizes_terms_and_renderings() -> None:
    batches = [
        [entry("  Ulcisor   Academy ", "  Academia   Ulcisor ")],
        [entry("ULCISOR ACADEMY", "Academia Ulcisor")],
    ]

    glossary, proposals = merge_extraction_batches(batches)

    assert [(e.term, e.translation) for e in glossary.entries] == [
        ("Ulcisor Academy", "Academia Ulcisor")
    ]
    assert proposals == {"ulcisor academy": ("Academia Ulcisor", "Academia Ulcisor")}


def test_merge_keeps_the_first_non_empty_note() -> None:
    batches = [
        [entry("Leathfhear", "Leathfhear")],
        [entry("Leathfhear", "Leathfhear", note="")],
        [entry("Leathfhear", "Leathfhear", note="half-man")],
        [entry("Leathfhear", "Leathfhear", note="later note")],
    ]

    glossary, _proposals = merge_extraction_batches(batches)

    assert glossary.entries[0].note == "half-man"


def test_merge_output_is_unlocked_ungendered_and_not_a_quorum_draw() -> None:
    batches = [
        [entry("Vis", "Vis", locked=True, gender="masculine", first_draw="Vis")],
        [entry("Vis", "Vis")],
    ]

    glossary, _proposals = merge_extraction_batches(batches)

    merged = glossary.entries[0]
    assert merged.locked is False
    assert merged.gender is None
    assert merged.first_draw is None
    # Nothing a pre-pass produced may count as quorum settling a term.
    assert settlement_counts(glossary, GlossaryVotes()).settled == 0


def test_merge_drops_blank_terms() -> None:
    glossary, proposals = merge_extraction_batches(
        [[entry("   ", "algo"), entry("Vis", "Vis")], [entry("", "nada")]]
    )

    assert [e.term for e in glossary.entries] == ["Vis"]
    assert proposals == {"vis": ("Vis",)}


def test_merge_does_not_count_a_blank_rendering_as_a_vote() -> None:
    batches = [
        [entry("Birthright", "")],
        [entry("Birthright", "   ")],
        [entry("Birthright", "Primogenitura")],
    ]

    glossary, proposals = merge_extraction_batches(batches)

    # Two blanks would otherwise outvote the one real rendering, and an entry
    # "Birthright -> ''" orders the translator to delete the word.
    assert glossary.entries[0].translation == "Primogenitura"
    assert proposals == {"birthright": ("Primogenitura",)}


def test_merge_drops_a_term_with_no_non_blank_rendering() -> None:
    glossary, proposals = merge_extraction_batches(
        [[entry("Vis", "Vis"), entry("Hierarchy", "")], [entry("Hierarchy", "  ")]]
    )

    assert [e.term for e in glossary.entries] == ["Vis"]
    assert proposals == {"vis": ("Vis",)}


def test_merge_lets_a_batch_vote_after_a_blank_occurrence() -> None:
    # A blank occurrence is not the batch's vote, so a later non-blank one is.
    batches = [
        [entry("Will", ""), entry("Will", "Voluntad")],
        [entry("Will", "Albedrío")],
        [entry("Will", "Voluntad")],
    ]

    glossary, proposals = merge_extraction_batches(batches)

    assert proposals == {"will": ("Voluntad", "Albedrío", "Voluntad")}
    assert glossary.entries[0].translation == "Voluntad"


def test_merge_orders_entries_by_first_appearance_across_batches() -> None:
    batches = [
        [entry("Caeror", "Caeror"), entry("Vis", "Vis")],
        [entry("Aequa", "Aequa"), entry("Caeror", "Caeror")],
    ]

    glossary, proposals = merge_extraction_batches(batches)

    assert [e.term for e in glossary.entries] == ["Caeror", "Vis", "Aequa"]
    assert list(proposals) == ["caeror", "vis", "aequa"]


def test_merge_leaves_direction_decisions_to_the_caller() -> None:
    # A reversed pair is resolved later against the source text by
    # sanitize_glossary; merging alone has no source to decide with.
    batches = [[entry("Will", "Voluntad")], [entry("Voluntad", "Will")]]

    glossary, _proposals = merge_extraction_batches(batches)

    assert [e.term for e in glossary.entries] == ["Will", "Voluntad"]
