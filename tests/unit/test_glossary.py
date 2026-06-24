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
