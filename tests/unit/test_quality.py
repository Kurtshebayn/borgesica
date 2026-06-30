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
