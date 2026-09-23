"""M4-2 — Deterministic unit tests for QualityHarness and advisory_gate.

All tests use FakeTranslationProvider — no live LLM, no network.
Strictly follows TDD: tests were written first, then quality.py was implemented.

Test coverage:
1. Harness returns valid QualityScore when provider returns canned JSON.
2. advisory_gate: any dimension < 4 → FAIL; all >= 4 → PASS.
3. Back-translation: evaluate() makes exactly 1 judge call (Tier-2 only);
   back_translation_similarity() makes exactly 1 provider call and returns a float in [0.0, 1.0].
4. Malformed judge output (non-JSON / out-of-range) → clear domain error.
"""
from __future__ import annotations

import json

import pytest

from borgesica.domain.errors import MalformedOutput
from borgesica.domain.models import (
    Glossary,
    GlossaryEntry,
    QualityScore,
    TranslationUnit,
)
from borgesica.domain.quality import (
    AdvisoryResult,
    QualityHarness,
    advisory_gate,
)
from tests.fakes import FakeTranslationProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_score_unit(
    accuracy: int = 4,
    fluency: int = 4,
    neutral_register: int = 4,
    glossary_consistency: int = 4,
    summary: str = "Judge summary.",
) -> TranslationUnit:
    """Return a TranslationUnit whose 'translation' field carries judge JSON."""
    score_dict = {
        "accuracy": accuracy,
        "fluency": fluency,
        "neutral_register": neutral_register,
        "glossary_consistency": glossary_consistency,
    }
    return TranslationUnit(
        translation=json.dumps(score_dict),
        summary_update=summary,
        glossary_additions=[],
    )


# ---------------------------------------------------------------------------
# Test 1 — Harness returns valid QualityScore from a canned provider response
# ---------------------------------------------------------------------------


def test_harness_returns_valid_quality_score() -> None:
    """Happy path: provider returns JSON in TranslationUnit.translation → valid QualityScore."""
    canned = _make_score_unit(accuracy=5, fluency=4, neutral_register=3, glossary_consistency=4)
    provider = FakeTranslationProvider(canned_unit=canned)
    harness = QualityHarness(provider=provider)
    glossary = Glossary()

    score = harness.evaluate(
        source="The cat sat on the mat.",
        translation="El gato estaba sentado en el tapete.",
        glossary=glossary,
        model="test-model",
    )

    assert isinstance(score, QualityScore)
    assert score.accuracy == 5
    assert score.fluency == 4
    assert score.neutral_register == 3
    assert score.glossary_consistency == 4
    # All within [1, 5] — pydantic would raise on out-of-range
    for dim in (score.accuracy, score.fluency, score.neutral_register, score.glossary_consistency):
        assert 1 <= dim <= 5


def test_harness_calls_provider_exactly_once_for_judge() -> None:
    """Exactly 1 provider call: the judge call. No extras."""
    canned = _make_score_unit()
    provider = FakeTranslationProvider(canned_unit=canned)
    harness = QualityHarness(provider=provider)

    harness.evaluate(
        source="Hello.",
        translation="Hola.",
        glossary=Glossary(),
        model="test-model",
    )

    assert provider.call_count == 1


def test_harness_judge_prompt_contains_all_four_dimensions() -> None:
    """The judge system prompt must mention all 4 rubric dimensions."""
    canned = _make_score_unit()
    provider = FakeTranslationProvider(canned_unit=canned)
    harness = QualityHarness(provider=provider)

    harness.evaluate(
        source="Hello.",
        translation="Hola.",
        glossary=Glossary(),
        model="test-model",
    )

    system_prompt = provider.call_log[0][0].lower()
    assert "accuracy" in system_prompt
    assert "fluency" in system_prompt
    assert "neutral_register" in system_prompt or "neutral register" in system_prompt
    assert "glossary_consistency" in system_prompt or "glossary consistency" in system_prompt


def test_harness_injects_glossary_locked_terms_into_prompt() -> None:
    """Locked glossary terms must appear in the judge system prompt."""
    canned = _make_score_unit()
    provider = FakeTranslationProvider(canned_unit=canned)
    harness = QualityHarness(provider=provider)
    glossary = Glossary(
        entries=[GlossaryEntry(term="Thornwood", translation="Thornwood", locked=True)]
    )

    harness.evaluate(
        source="Welcome to Thornwood.",
        translation="Bienvenido a Thornwood.",
        glossary=glossary,
        model="test-model",
    )

    system_prompt = provider.call_log[0][0]
    assert "Thornwood" in system_prompt


# ---------------------------------------------------------------------------
# B1a follow-up — the judge must see the SAME glossary view as the translator
#
# The judge scores a `glossary_consistency` dimension, so a glossary block
# truncated more aggressively than the translator's would penalise the model
# for terms it was never shown.
# ---------------------------------------------------------------------------


def _novel_glossary(n: int) -> Glossary:
    return Glossary(
        entries=[
            GlossaryEntry(term=f"Gleaners{i:03d}", translation=f"Espigadores{i:03d}")
            for i in range(n)
        ]
    )


def test_harness_glossary_budget_defaults_to_the_translator_budget() -> None:
    """By default the judge renders the glossary at the shared default budget."""
    from borgesica.domain.models import DEFAULT_GLOSSARY_BUDGET_TOKENS

    canned = _make_score_unit()
    provider = FakeTranslationProvider(canned_unit=canned)
    harness = QualityHarness(provider=provider)

    harness.evaluate(
        source="Hello.",
        translation="Hola.",
        glossary=_novel_glossary(300),
        model="test-model",
    )

    user_message = provider.call_log[0][1]
    # Entry 299 sits far past the old hardcoded-300 ceiling (~67 entries).
    assert "Gleaners299" in user_message
    assert DEFAULT_GLOSSARY_BUDGET_TOKENS >= 1200


def test_harness_glossary_budget_is_overridable_per_call() -> None:
    """A job with a tuned budget can align the judge with its own prompt."""
    canned = _make_score_unit()
    provider = FakeTranslationProvider(canned_unit=canned)
    harness = QualityHarness(provider=provider)

    harness.evaluate(
        source="Hello.",
        translation="Hola.",
        glossary=_novel_glossary(300),
        model="test-model",
        glossary_budget_tokens=30,
    )

    user_message = provider.call_log[0][1]
    assert "Gleaners299" not in user_message


# ---------------------------------------------------------------------------
# Test 2 — advisory_gate: any dimension < 4 → FAIL; all >= 4 → PASS
# ---------------------------------------------------------------------------


def test_advisory_gate_pass_when_all_dimensions_four_or_above() -> None:
    """All dims >= 4 → PASS advisory."""
    score = QualityScore(accuracy=4, fluency=5, neutral_register=4, glossary_consistency=4)
    result = advisory_gate(score)
    assert result.passed is True
    assert not result.failing_dimensions


def test_advisory_gate_pass_all_fives() -> None:
    score = QualityScore(accuracy=5, fluency=5, neutral_register=5, glossary_consistency=5)
    result = advisory_gate(score)
    assert result.passed is True


def test_advisory_gate_fail_when_accuracy_below_four() -> None:
    score = QualityScore(accuracy=3, fluency=4, neutral_register=4, glossary_consistency=4)
    result = advisory_gate(score)
    assert result.passed is False
    assert "accuracy" in result.failing_dimensions


def test_advisory_gate_fail_when_fluency_below_four() -> None:
    score = QualityScore(accuracy=4, fluency=2, neutral_register=4, glossary_consistency=4)
    result = advisory_gate(score)
    assert result.passed is False
    assert "fluency" in result.failing_dimensions


def test_advisory_gate_fail_when_neutral_register_below_four() -> None:
    """Spec scenario: neutral_register == 3 → FAIL advisory for neutral-register dimension."""
    score = QualityScore(accuracy=4, fluency=4, neutral_register=3, glossary_consistency=4)
    result = advisory_gate(score)
    assert result.passed is False
    assert "neutral_register" in result.failing_dimensions


def test_advisory_gate_fail_when_glossary_consistency_below_four() -> None:
    score = QualityScore(accuracy=4, fluency=4, neutral_register=4, glossary_consistency=2)
    result = advisory_gate(score)
    assert result.passed is False
    assert "glossary_consistency" in result.failing_dimensions


def test_advisory_gate_fail_multiple_dimensions() -> None:
    """Multiple dims below threshold → all appear in failing_dimensions."""
    score = QualityScore(accuracy=1, fluency=2, neutral_register=3, glossary_consistency=4)
    result = advisory_gate(score)
    assert result.passed is False
    assert "accuracy" in result.failing_dimensions
    assert "fluency" in result.failing_dimensions
    assert "neutral_register" in result.failing_dimensions
    assert "glossary_consistency" not in result.failing_dimensions


def test_advisory_gate_does_not_raise() -> None:
    """advisory_gate is ADVISORY — never raises, even on worst score."""
    score = QualityScore(accuracy=1, fluency=1, neutral_register=1, glossary_consistency=1)
    result = advisory_gate(score)  # must not raise
    assert result.passed is False


def test_advisory_gate_returns_advisory_result_type() -> None:
    """advisory_gate returns an AdvisoryResult (typed result object)."""
    score = QualityScore(accuracy=4, fluency=4, neutral_register=4, glossary_consistency=4)
    result = advisory_gate(score)
    assert isinstance(result, AdvisoryResult)


# ---------------------------------------------------------------------------
# Test 3 — Back-translation gating (Tier-2 vs Tier-3 separation)
# ---------------------------------------------------------------------------


def test_evaluate_makes_exactly_one_judge_call() -> None:
    """evaluate() is Tier-2 only: exactly 1 provider call (the judge). No back-translation."""
    canned = _make_score_unit()
    provider = FakeTranslationProvider(canned_unit=canned)
    harness = QualityHarness(provider=provider)

    harness.evaluate(
        source="The door is locked.",
        translation="La puerta está cerrada con llave.",
        glossary=Glossary(),
        model="test-model",
    )

    assert provider.call_count == 1


def test_back_translation_similarity_makes_one_call_and_returns_ratio() -> None:
    """back_translation_similarity() is Tier-3: exactly 1 provider call, returns float in [0, 1].

    Uses identical source and back-translation to assert ratio == 1.0 (strongest guarantee).
    """
    source = "The door is locked."
    # Fake provider returns back-translated text identical to source (best-case ratio = 1.0).
    back_unit = TranslationUnit(
        translation=source,
        summary_update="Back-translation result.",
        glossary_additions=[],
    )
    provider = FakeTranslationProvider(canned_unit=back_unit)
    harness = QualityHarness(provider=provider)

    ratio = harness.back_translation_similarity(
        source=source,
        translation="La puerta está cerrada con llave.",
        model="test-model",
    )

    assert provider.call_count == 1
    assert isinstance(ratio, float)
    assert 0.0 <= ratio <= 1.0
    assert ratio == 1.0  # identical strings → perfect similarity


# ---------------------------------------------------------------------------
# Test 4 — Malformed judge output handling
# ---------------------------------------------------------------------------


def test_malformed_non_json_output_raises_domain_error() -> None:
    """Non-JSON from provider → MalformedOutput raised (not a generic Exception)."""
    bad_unit = TranslationUnit(
        translation="This is not JSON at all!!!",
        summary_update="Oops.",
        glossary_additions=[],
    )
    provider = FakeTranslationProvider(canned_unit=bad_unit)
    harness = QualityHarness(provider=provider)

    with pytest.raises(MalformedOutput):
        harness.evaluate(
            source="Hello.",
            translation="Hola.",
            glossary=Glossary(),
            model="test-model",
        )


def test_malformed_out_of_range_score_raises_domain_error() -> None:
    """Out-of-range score values (e.g. 0 or 7) → MalformedOutput raised."""
    bad_json = json.dumps(
        {"accuracy": 0, "fluency": 4, "neutral_register": 4, "glossary_consistency": 4}
    )
    bad_unit = TranslationUnit(
        translation=bad_json,
        summary_update="Bad.",
        glossary_additions=[],
    )
    provider = FakeTranslationProvider(canned_unit=bad_unit)
    harness = QualityHarness(provider=provider)

    with pytest.raises(MalformedOutput):
        harness.evaluate(
            source="Hello.",
            translation="Hola.",
            glossary=Glossary(),
            model="test-model",
        )


def test_malformed_missing_field_raises_domain_error() -> None:
    """JSON missing required fields → MalformedOutput raised."""
    partial_json = json.dumps({"accuracy": 4, "fluency": 4})  # missing neutral_register + glossary
    bad_unit = TranslationUnit(
        translation=partial_json,
        summary_update="Partial.",
        glossary_additions=[],
    )
    provider = FakeTranslationProvider(canned_unit=bad_unit)
    harness = QualityHarness(provider=provider)

    with pytest.raises(MalformedOutput):
        harness.evaluate(
            source="Hello.",
            translation="Hola.",
            glossary=Glossary(),
            model="test-model",
        )


# ---------------------------------------------------------------------------
# Deterministic character-gender detector — free, post-hoc, no provider calls
# ---------------------------------------------------------------------------


def _cast() -> "Glossary":
    from borgesica.domain.models import Glossary, GlossaryEntry

    return Glossary(
        entries=[
            GlossaryEntry(term="Vis", translation="Vis", gender="masculine"),
            GlossaryEntry(term="Lanistia", translation="Lanistia", gender="feminine"),
            # Below the seeding margin on the real book — no anchor, nothing to
            # check against.
            GlossaryEntry(term="Emissa", translation="Emissa"),
        ]
    )


def test_detects_the_defect_that_actually_shipped():
    """Chunk 19 of job 9be143da: source "Easy, Vis." became "—Tranquila, Vis".

    The one defect that surfaced in the whole book. Feminine agreement sitting
    directly against a masculine name is the shape the detector exists for.
    """
    from borgesica.domain.quality import detect_gender_defects

    defects = detect_gender_defects("—Tranquila, Vis. El pasillo estaba vacío.", _cast())

    assert len(defects) == 1
    assert defects[0].name == "Vis"
    assert defects[0].expected == "masculine"
    assert defects[0].marker.casefold() == "tranquila"


def test_accepts_correct_agreement():
    from borgesica.domain.quality import detect_gender_defects

    assert detect_gender_defects("—Tranquilo, Vis. Ya pasó.", _cast()) == []


def test_detects_agreement_following_the_name():
    from borgesica.domain.quality import detect_gender_defects

    defects = detect_gender_defects("Vis, cansada, bajó la escalera.", _cast())

    assert [d.marker.casefold() for d in defects] == ["cansada"]


def test_checks_feminine_characters_too():
    """The anchor is symmetric — the defect happens to run one way in this
    book, but a detector that only knows one direction is not a detector.
    """
    from borgesica.domain.quality import detect_gender_defects

    defects = detect_gender_defects("—Cansado, Lanistia.", _cast())

    assert [d.expected for d in defects] == ["feminine"]


def test_ignores_a_character_with_no_anchor():
    """Unclassified means unknown, and an unknown expectation cannot be
    violated. Flagging it would invent the fact the seeder refused to guess.
    """
    from borgesica.domain.quality import detect_gender_defects

    assert detect_gender_defects("—Tranquilo, Emissa.", _cast()) == []


def test_ignores_gendered_words_that_are_not_adjacent():
    """Recall is limited BY DESIGN. Here "ella" refers to Lanistia, not to
    Vis; only adjacency makes attribution safe, and a detector that guesses
    at distance would drown its real findings in false ones.
    """
    from borgesica.domain.quality import detect_gender_defects

    text = "Vis miró a Lanistia durante un rato y ella sonrió, agotada."

    assert detect_gender_defects(text, _cast()) == []


def test_does_not_read_across_a_sentence_boundary():
    """A word in the NEXT sentence is not agreement with this name."""
    from borgesica.domain.quality import detect_gender_defects

    assert detect_gender_defects("Todo terminó para Vis. Cansada, se fue.", _cast()) == []


def test_reports_an_excerpt_for_review():
    """A flag nobody can act on is not a finding. Retrying is not an option —
    the same poisoned summary produces the same output — so the excerpt is
    what a human uses to judge it.
    """
    from borgesica.domain.quality import detect_gender_defects

    defects = detect_gender_defects(
        "El pasillo estaba en silencio. —Tranquila, Vis. Nadie respondió.", _cast()
    )

    assert "Tranquila, Vis" in defects[0].excerpt


def test_ignores_agreement_that_belongs_to_a_preceding_noun():
    """Measured on the real book, this was the single largest false-positive
    class: "la voz de Caeror, apagada" agrees with "voz", not with Caeror.

    A name introduced by "de" is a genitive complement — the head noun before
    it owns any agreement that follows, so the name is not a candidate.
    """
    from borgesica.domain.models import Glossary, GlossaryEntry
    from borgesica.domain.quality import detect_gender_defects

    cast = Glossary(
        entries=[GlossaryEntry(term="Caeror", translation="Caeror", gender="masculine")]
    )
    text = "Escucho la voz de Caeror, apagada y distorsionada."

    assert detect_gender_defects(text, cast) == []


def test_ignores_a_marker_that_does_not_open_its_clause():
    """The other large false-positive class. In "alguien llamado Netiqret",
    "En un momento dado, Kiya" and "de nuevo, Netiqret", the gendered word
    belongs to the phrase it sits in and merely happens to end up beside a
    name. Real vocative agreement opens its clause — "—Tranquila, Vis".
    """
    from borgesica.domain.models import Glossary, GlossaryEntry
    from borgesica.domain.quality import detect_gender_defects

    cast = Glossary(
        entries=[
            GlossaryEntry(term="Netiqret", translation="Netiqret", gender="feminine"),
            GlossaryEntry(term="Kiya", translation="Kiya", gender="feminine"),
        ]
    )

    assert detect_gender_defects("Alguien llamado Netiqret me lo dio.", cast) == []
    assert detect_gender_defects("En un momento dado, Kiya desaparece.", cast) == []
    assert detect_gender_defects("Podemos intentarlo de nuevo, Netiqret.", cast) == []


def test_does_not_read_the_adverb_solo_as_agreement():
    """"solo" is overwhelmingly the adverb "only" and carries no agreement;
    "sola" has no adverbial sense and does. The asymmetry is the language's,
    not an oversight — measured, "solo" produced the last surviving false
    positive in the summaries of a full book.
    """
    from borgesica.domain.models import Glossary, GlossaryEntry
    from borgesica.domain.quality import detect_gender_defects

    cast = Glossary(
        entries=[
            GlossaryEntry(term="Siamun", translation="Siamun", gender="feminine"),
            GlossaryEntry(term="Vis", translation="Vis", gender="masculine"),
        ]
    )

    assert detect_gender_defects("Siamun solo le había dicho eso.", cast) == []
    # The feminine form still marks agreement.
    assert len(detect_gender_defects("—Sola, Vis. Nadie más queda.", cast)) == 1


# ---------------------------------------------------------------------------
# Deterministic do-not-translate detector — free, post-hoc, no provider calls
#
# The KEEP INVENTED LANGUAGE VERBATIM prompt rule asks the model not to
# hispanicise an in-world word. Asking is not verifying: "Catenicus → Catenicus"
# was present as an identity entry in EVERY job, and two older runs still
# emitted "Catenico" (13b43ac6 ch401, afe326df ch405). This checks the promise
# the identity entry makes.
#
# Measured on job 9be143da (569 chunks, 549 glossary entries, 278 of them
# identity): 2 findings.
#   - ch32  "Quintus" -> "Quinto"   — real, the defect this exists for
#   - ch224 "iunctus" -> "iunctii"  — a Latin plural, the known FP class
# ---------------------------------------------------------------------------


def _in_world() -> "Glossary":
    from borgesica.domain.models import Glossary, GlossaryEntry

    return Glossary(
        entries=[
            # Identity — must survive untouched.
            GlossaryEntry(term="Quintus", translation="Quintus"),
            GlossaryEntry(term="Catenicus", translation="Catenicus"),
            # A real mapping — the glossary asks for exactly this change.
            GlossaryEntry(term="Gleaner", translation="Segador"),
        ]
    )


def test_flags_an_identity_term_hispanicised_away():
    """Job 9be143da ch32: a rank name became "Quinto" in the translation."""
    from borgesica.domain.quality import detect_untranslated_defects

    defects = detect_untranslated_defects(
        "But there is a Quintus position to be had.",
        "Pero hay un puesto de Quinto disponible.",
        _in_world(),
    )

    assert [d.term for d in defects] == ["Quintus"]
    assert defects[0].occurrences == 1


def test_accepts_a_term_carried_over_unchanged():
    from borgesica.domain.quality import detect_untranslated_defects

    assert detect_untranslated_defects(
        "But there is a Quintus position to be had.",
        "Pero hay un puesto de Quintus disponible.",
        _in_world(),
    ) == []


def test_a_capitalisation_change_is_not_an_erasure():
    """Job 9be143da ch222: Spanish moved the adjective after the noun, so the
    in-world word opened the sentence and got a capital. The word is THERE.

    Case-sensitive matching reported this as a defect, taking the detector's
    precision on the real book from 1-in-2 down to 1-in-4. Matching is
    case-insensitive for exactly this reason.
    """
    from borgesica.domain.models import Glossary, GlossaryEntry
    from borgesica.domain.quality import detect_untranslated_defects

    glossary = Glossary(entries=[GlossaryEntry(term="kataht", translation="kataht")])

    assert detect_untranslated_defects(
        "Condescending kataht.", "Kataht condescendiente.", glossary
    ) == []


def test_ignores_a_mapping_entry():
    """Only IDENTITY entries promise the word survives. A mapping asks for a
    change, so the source term being gone is the rule working, not breaking.
    """
    from borgesica.domain.quality import detect_untranslated_defects

    assert detect_untranslated_defects(
        "The Gleaner waited.", "El Segador esperaba.", _in_world()
    ) == []


def test_ignores_a_term_absent_from_this_chunk():
    from borgesica.domain.quality import detect_untranslated_defects

    assert detect_untranslated_defects(
        "Nothing notable here.", "Nada notable aqui.", _in_world()
    ) == []


def test_does_not_match_a_term_inside_a_longer_word():
    """"Caten" is a substring of "Catenicus". Without word boundaries a chunk
    naming only the longer term would report the shorter one as surviving —
    and, worse, a translation dropping the longer one would look clean.
    """
    from borgesica.domain.models import Glossary, GlossaryEntry
    from borgesica.domain.quality import detect_untranslated_defects

    glossary = Glossary(entries=[GlossaryEntry(term="Caten", translation="Caten")])

    defects = detect_untranslated_defects(
        "He returned to Caten.", "Regreso a Catenicus.", glossary
    )

    assert [d.term for d in defects] == ["Caten"]


def test_skips_a_term_that_is_ordinary_vocabulary():
    """"Thrum" reached the glossary as an identity entry, but the source uses
    "thrum" lowercase as a common noun — a low vibrating sound, correctly
    translated. Unfiltered it produced 10 of 12 findings on the real book,
    every one of them wrong.
    """
    from borgesica.domain.models import Glossary, GlossaryEntry
    from borgesica.domain.quality import detect_untranslated_defects

    glossary = Glossary(entries=[GlossaryEntry(term="Thrum", translation="Thrum")])

    assert detect_untranslated_defects(
        "There is a growling thrum of energy.",
        "Hay un zumbido creciente de energia.",
        glossary,
        vocabulary=frozenset({"thrum"}),
    ) == []


def test_an_all_caps_heading_does_not_disqualify_a_name():
    """The book sets chapter headings in capitals, so "CAEROR" appears all
    through the source. Treating any case variant as evidence of a common noun
    dropped Caeror, Caten, Catenicus and Livia — the very terms worth guarding.
    Only a GENUINE lowercase use disqualifies, matching
    ``character_gender_evidence``.
    """
    from borgesica.domain.quality import lowercase_vocabulary

    vocabulary = lowercase_vocabulary("CAEROR SPOKE. Caeror waited. the thrum grew.")

    assert "caeror" not in vocabulary
    assert "thrum" in vocabulary
    assert "the" in vocabulary


def test_reports_every_occurrence_count_for_triage():
    from borgesica.domain.quality import detect_untranslated_defects

    defects = detect_untranslated_defects(
        "Quintus, then Quintus again.", "Quinto, y luego Quinto otra vez.", _in_world()
    )

    assert defects[0].occurrences == 2
    assert "Quintus" in defects[0].excerpt


def test_returns_nothing_without_identity_entries():
    from borgesica.domain.models import Glossary, GlossaryEntry
    from borgesica.domain.quality import detect_untranslated_defects

    glossary = Glossary(entries=[GlossaryEntry(term="Gleaner", translation="Segador")])

    assert detect_untranslated_defects("Gleaner.", "Segador.", glossary) == []


# ---------------------------------------------------------------------------
# detect_glossary_contradictions — the glossary checked against itself
#
# A bad entry is born once and injected into every later prompt, so one first
# draw contaminates the rest of the book. Every case below is a real entry
# pair from a finished job.
# ---------------------------------------------------------------------------


def _pair(short: tuple[str, str], long: tuple[str, str]) -> "Glossary":
    from borgesica.domain.models import Glossary, GlossaryEntry

    return Glossary(
        entries=[
            GlossaryEntry(term=short[0], translation=short[1]),
            GlossaryEntry(term=long[0], translation=long[1]),
        ]
    )


def test_contradiction_rule_a_kept_alone_but_changed_inside_a_compound():
    from borgesica.domain.quality import detect_glossary_contradictions

    findings = detect_glossary_contradictions(
        _pair(("Quintus", "Quintus"), ("Quintus Darinus", "Quinto Darino"))
    )

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule == "A"
    assert (finding.short_term, finding.short_translation) == ("Quintus", "Quintus")
    assert (finding.long_term, finding.long_translation) == (
        "Quintus Darinus",
        "Quinto Darino",
    )


def test_contradiction_rule_b_translated_alone_but_kept_inside_a_compound():
    from borgesica.domain.quality import detect_glossary_contradictions

    findings = detect_glossary_contradictions(
        _pair(("Will", "Voluntad"), ("Will-carriage", "carruaje Will"))
    )

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule == "B"
    assert (finding.short_term, finding.short_translation) == ("Will", "Voluntad")
    assert (finding.long_term, finding.long_translation) == (
        "Will-carriage",
        "carruaje Will",
    )


def test_contradiction_ignores_an_article_in_the_standalone_rendering():
    """"Kept" is CONTAINMENT, not equality. Equality produced 21 false positives
    in 21 on job 9be143da, this pair among them."""
    from borgesica.domain.quality import detect_glossary_contradictions

    glossary = _pair(("Magnus", "el Magnus"), ("Magnus Quintus", "Magnus Quintus"))

    assert detect_glossary_contradictions(glossary) == []


def test_contradiction_ignores_a_gloss_in_the_standalone_rendering():
    from borgesica.domain.quality import detect_glossary_contradictions

    glossary = _pair(("ap", "ap (hijo de)"), ("Mel ap Mor", "Mel ap Mor"))

    assert detect_glossary_contradictions(glossary) == []


def test_contradiction_needs_the_short_term_as_a_whole_word():
    """"Will" inside "Willow" is a different word, not a compound of it."""
    from borgesica.domain.quality import detect_glossary_contradictions

    glossary = _pair(("Will", "Will"), ("Willow", "Sauce"))

    assert detect_glossary_contradictions(glossary) == []


def test_contradiction_matches_terms_case_insensitively():
    from borgesica.domain.quality import detect_glossary_contradictions

    findings = detect_glossary_contradictions(
        _pair(("quintus", "Quintus"), ("QUINTUS Darinus", "Quinto Darino"))
    )

    assert [f.rule for f in findings] == ["A"]


def test_contradiction_skips_entries_with_an_empty_side():
    from borgesica.domain.quality import detect_glossary_contradictions

    glossary = _pair(("Quintus", "  "), ("Quintus Darinus", "Quintus Darinus"))

    assert detect_glossary_contradictions(glossary) == []


def test_contradiction_does_not_pair_an_entry_with_its_own_case_variant():
    from borgesica.domain.quality import detect_glossary_contradictions

    glossary = _pair(("Will", "Voluntad"), ("will", "Will"))

    assert detect_glossary_contradictions(glossary) == []


# ---------------------------------------------------------------------------
# audit_chunks — every free detector over a finished job
#
# Exists so the whole-book vocabulary is built ONCE and no caller can forget
# to pass it. Forgetting is not a small mistake: on job 9be143da the filter is
# the difference between 2 findings and 12.
# ---------------------------------------------------------------------------


def _audit_glossary() -> "Glossary":
    from borgesica.domain.models import Glossary, GlossaryEntry

    return Glossary(
        entries=[
            GlossaryEntry(term="Vis", translation="Vis", gender="masculine"),
            GlossaryEntry(term="Quintus", translation="Quintus"),
        ]
    )


def test_audit_reports_both_kinds_of_defect_with_their_chunk():
    from borgesica.domain.quality import audit_chunks

    findings = audit_chunks(
        [
            (7, "There is a Quintus position.", "Hay un puesto de Quinto."),
            (9, "Easy, Vis.", "—Tranquila, Vis."),
        ],
        _audit_glossary(),
    )

    assert [(f.chunk_index, f.kind, f.term) for f in findings] == [
        (7, "untranslated", "Quintus"),
        (9, "gender", "Vis"),
    ]


def test_audit_builds_the_vocabulary_across_the_whole_book():
    """The common-noun filter needs the WHOLE source, not one chunk.

    Here the glossary carries "Thrum" as an identity entry, chunk 0 translates
    it, and chunk 1 shows the source using "thrum" lowercase — ordinary
    vocabulary. Auditing chunk 0 on its own would flag it. The book knows
    better, and that is the whole reason this function exists rather than
    leaving each caller to loop over the detectors itself.
    """
    from borgesica.domain.models import Glossary, GlossaryEntry
    from borgesica.domain.quality import audit_chunks, detect_untranslated_defects

    glossary = Glossary(entries=[GlossaryEntry(term="Thrum", translation="Thrum")])
    chunks = [
        (0, "The Thrum answered.", "El zumbido respondio."),
        (1, "a low thrum of energy", "un zumbido grave de energia"),
    ]

    # Without the book-wide vocabulary the same chunk is a finding.
    assert len(detect_untranslated_defects(chunks[0][1], chunks[0][2], glossary)) == 1
    assert audit_chunks(chunks, glossary) == []


def test_audit_is_clean_on_a_faithful_translation():
    from borgesica.domain.quality import audit_chunks

    source = "There is a Quintus position. Easy, Vis."
    translation = "Hay un puesto de Quintus. —Tranquilo, Vis."

    assert audit_chunks([(0, source, translation)], _audit_glossary()) == []


def test_audit_names_what_went_wrong_in_the_detail():
    """A bare count is not actionable — triage needs the expected/found pair."""
    from borgesica.domain.quality import audit_chunks

    findings = audit_chunks([(3, "Easy, Vis.", "—Tranquila, Vis.")], _audit_glossary())

    assert "masculine" in findings[0].detail
    assert "feminine" in findings[0].detail
    assert "tranquila" in findings[0].detail.casefold()


def test_audit_needs_no_provider():
    """The whole premise: a finished job is audited for free, as often as you
    like. Guaranteed by the SIGNATURE rather than by convention — the same
    argument ContextManager makes about prompt assembly taking no collaborator.
    A provider parameter appearing here would make a 569-chunk audit billable.
    """
    import inspect

    from borgesica.domain.quality import audit_chunks

    assert list(inspect.signature(audit_chunks).parameters) == ["chunks", "glossary"]


def test_audit_reports_glossary_contradictions_once_for_the_job():
    """A contradiction belongs to the JOB, not to a chunk: it is reported once,
    ahead of the per-chunk findings, with no chunk index — and the per-chunk
    findings are exactly what they were without it."""
    from borgesica.domain.models import Glossary, GlossaryEntry
    from borgesica.domain.quality import audit_chunks

    chunks = [
        (7, "There is a Quintus position.", "Hay un puesto de Quinto."),
        (9, "Easy, Vis.", "—Tranquila, Vis."),
    ]
    per_chunk = audit_chunks(chunks, _audit_glossary())
    glossary = Glossary(
        entries=[
            *_audit_glossary().entries,
            GlossaryEntry(term="Quintus Darinus", translation="Quinto Darino"),
        ]
    )

    findings = audit_chunks(chunks, glossary)

    assert [(f.chunk_index, f.kind, f.term) for f in findings] == [
        (None, "glossary", "Quintus"),
        (7, "untranslated", "Quintus"),
        (9, "gender", "Vis"),
    ]
    assert findings[1:] == per_chunk
    contradiction = findings[0]
    assert "rule A" in contradiction.detail
    for side in ("Quintus", "Quintus Darinus", "Quinto Darino"):
        assert side in contradiction.excerpt


def test_audit_reports_glossary_contradictions_even_with_no_chunks():
    """Called once per audit, not once per chunk — so it neither repeats per
    chunk nor disappears when nothing has been translated yet."""
    from borgesica.domain.quality import audit_chunks

    glossary = _pair(("Quintus", "Quintus"), ("Quintus Darinus", "Quinto Darino"))

    assert [(f.chunk_index, f.kind) for f in audit_chunks([], glossary)] == [
        (None, "glossary")
    ]


def test_audit_words_each_contradiction_rule_exactly():
    """What a reader of ``borgesica audit`` sees, pinned for BOTH rules — the
    wording is the finding's explanation, so a rule B finding must never be
    described as rule A or the reverse."""
    from borgesica.domain.models import Glossary, GlossaryEntry
    from borgesica.domain.quality import AuditedDefect, audit_chunks

    glossary = Glossary(
        entries=[
            GlossaryEntry(term="Quintus", translation="Quintus"),
            GlossaryEntry(term="Quintus Darinus", translation="Quinto Darino"),
            GlossaryEntry(term="Will", translation="Voluntad"),
            GlossaryEntry(term="Will-carriage", translation="carruaje Will"),
        ]
    )

    assert audit_chunks([], glossary) == [
        AuditedDefect(
            chunk_index=None,
            kind="glossary",
            term="Quintus",
            detail="rule A: 'Quintus' is kept on its own but not inside 'Quintus Darinus'",
            excerpt="Quintus -> Quintus | Quintus Darinus -> Quinto Darino",
        ),
        AuditedDefect(
            chunk_index=None,
            kind="glossary",
            term="Will",
            detail="rule B: 'Will' is translated on its own but kept inside 'Will-carriage'",
            excerpt="Will -> Voluntad | Will-carriage -> carruaje Will",
        ),
    ]


def test_audit_refuses_to_word_an_unknown_contradiction_rule(monkeypatch):
    """An unknown rule must fail loudly. Falling through to the rule B wording
    would hand the reader a confident, wrong explanation."""
    from borgesica.domain import quality
    from borgesica.domain.models import Glossary

    unknown = quality.GlossaryContradiction(
        rule="C",  # type: ignore[arg-type]
        short_term="Quintus",
        short_translation="Quintus",
        long_term="Quintus Darinus",
        long_translation="Quinto Darino",
    )
    monkeypatch.setattr(quality, "detect_glossary_contradictions", lambda _: [unknown])

    with pytest.raises(ValueError, match="'C'"):
        quality.audit_chunks([], Glossary(entries=[]))
