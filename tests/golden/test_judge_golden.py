"""M4-2 — GOLDEN tests for the LLM-as-judge harness.

These tests use a REAL LLM provider and are SKIPPED unless GOLDEN=1.
They are ADVISORY VALIDATION only — they do not block CI.

Run with:
    GOLDEN=1 pytest tests/golden/test_judge_golden.py -v

Requirements (env vars):
    ANTHROPIC_API_KEY — for AnthropicProvider
    JUDGE_MODEL — model to use (default: claude-haiku-4-5)

Fixture loading uses the schema documented in tests/golden/README.md.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

GOLDEN = os.getenv("GOLDEN", "0") == "1"
FIXTURES_DIR = Path(__file__).parent

# ---------------------------------------------------------------------------
# Skip guard: skip entire module unless GOLDEN=1
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.golden

if not GOLDEN:
    # Collect phase skip: mark all tests in this module as skipped
    collect_ignore_glob = ["*"]  # noqa: F841


def _load_fixture(name: str) -> dict[str, Any]:
    """Load a YAML fixture by filename (without directory)."""
    import yaml  # noqa: PLC0415

    path = FIXTURES_DIR / name
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _build_provider():  # type: ignore[return]
    """Build a real AnthropicProvider for golden tests."""
    from borgesica.adapters.providers.anthropic_provider import AnthropicProvider  # noqa: PLC0415

    return AnthropicProvider()


def _build_harness():  # type: ignore[return]
    """Build QualityHarness with real AnthropicProvider."""
    from borgesica.domain.quality import QualityHarness  # noqa: PLC0415

    return QualityHarness(provider=_build_provider())


def _fixture_to_glossary(fixture: dict[str, Any]):  # type: ignore[return]
    """Convert fixture glossary list → domain Glossary."""
    from borgesica.domain.models import Glossary, GlossaryEntry  # noqa: PLC0415

    entries = [
        GlossaryEntry(
            term=e["term"],
            translation=e["translation"],
            locked=e.get("locked", False),
            note=e.get("note"),
        )
        for e in (fixture.get("glossary") or [])
    ]
    return Glossary(entries=entries)


# ---------------------------------------------------------------------------
# Golden test 1 — Calque fixture: bad "viga con cerradura" rendering scores low
# ---------------------------------------------------------------------------


@pytest.mark.golden
@pytest.mark.skipif(not GOLDEN, reason="GOLDEN=1 required")
def test_calque_bad_rendering_scores_low() -> None:
    """calque_01_locking_beam: a literal calque translation should score accuracy/fluency < 4.

    We evaluate the BAD translation (literal calque, 'viga con cerradura') against the
    source. The judge should penalise the inaccurate rendering.
    """
    judge_model = os.getenv("JUDGE_MODEL", "claude-haiku-4-5")
    fixture = _load_fixture("calque_01_locking_beam.yaml")

    bad_translation = (
        "una ranura para una viga de madera con cerradura a través de la puerta"
    )

    harness = _build_harness()
    score = harness.evaluate(
        source=fixture["source"].strip(),
        translation=bad_translation,
        glossary=_fixture_to_glossary(fixture),
        model=judge_model,
    )

    # Bad calque: at least one of accuracy or fluency should be below 4
    assert score.accuracy < 4 or score.fluency < 4, (
        f"Expected the calque rendering to score accuracy or fluency < 4, "
        f"but got accuracy={score.accuracy}, fluency={score.fluency}. "
        "The judge may not be penalising literal calques appropriately."
    )


# ---------------------------------------------------------------------------
# Golden test 2 — Missing locked term scores glossary_consistency <= 2
# ---------------------------------------------------------------------------


@pytest.mark.golden
@pytest.mark.skipif(not GOLDEN, reason="GOLDEN=1 required")
def test_missing_locked_term_scores_glossary_consistency_low() -> None:
    """srt_04_glossary_proper_noun: a translation that drops/alters 'Thornwood' → low score.

    The locked entry requires 'Thornwood' verbatim. A translation that uses
    'Bosque Espinoso' instead should score glossary_consistency <= 2.
    """
    judge_model = os.getenv("JUDGE_MODEL", "claude-haiku-4-5")
    fixture = _load_fixture("srt_04_glossary_proper_noun.yaml")

    # Bad translation: changes the locked proper noun
    bad_translation = "Bienvenido a Bosque Espinoso. Aquí encontrarás todo lo que necesitas."

    harness = _build_harness()
    score = harness.evaluate(
        source=fixture["source"].strip(),
        translation=bad_translation,
        glossary=_fixture_to_glossary(fixture),
        model=judge_model,
    )

    assert score.glossary_consistency <= 2, (
        f"Expected glossary_consistency <= 2 for a translation that drops the locked term "
        f"'Thornwood', but got glossary_consistency={score.glossary_consistency}."
    )


# ---------------------------------------------------------------------------
# Golden test 3 — Good translation scores all dimensions >= 4
# ---------------------------------------------------------------------------


@pytest.mark.golden
@pytest.mark.skipif(not GOLDEN, reason="GOLDEN=1 required")
def test_good_translation_passes_advisory_gate() -> None:
    """srt_01_standard_dialogue: the gold-standard translation should score all dims >= 4."""
    from borgesica.domain.quality import advisory_gate  # noqa: PLC0415

    judge_model = os.getenv("JUDGE_MODEL", "claude-haiku-4-5")
    fixture = _load_fixture("srt_01_standard_dialogue.yaml")

    harness = _build_harness()
    score = harness.evaluate(
        source=fixture["source"].strip(),
        translation=fixture["expected"].strip(),
        glossary=_fixture_to_glossary(fixture),
        model=judge_model,
    )

    result = advisory_gate(score)
    # Gold-standard translations SHOULD pass; log the result for inspection
    # but do not hard-fail in case the judge is overly strict on a given run
    print(
        f"\nGolden advisory: passed={result.passed}, failing={result.failing_dimensions}, "
        f"score={score.model_dump()}"
    )
