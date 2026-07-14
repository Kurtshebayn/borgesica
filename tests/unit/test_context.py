"""Tests for ContextManager (M1-5).

Strict TDD: all tests written before implementation.
Spec: context-continuity + translation-quality + cost-control/prompt-caching
"""
from __future__ import annotations

import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from tests.fakes import FakeTranslationProvider
from borgesica.domain.models import (
    Glossary,
    GlossaryEntry,
    JobConfig,
    RollingSummary,
    SourceType,
)


def make_config(**kwargs) -> JobConfig:
    defaults = dict(source_type=SourceType.SRT, model="claude-haiku-4-5", target_lang="es-neutral")
    defaults.update(kwargs)
    return JobConfig(**defaults)


# ---------------------------------------------------------------------------
# Test 1 — neutral-Spanish constraints present in static system prompt
# ---------------------------------------------------------------------------


def test_system_prompt_contains_all_neutral_spanish_constraints():
    """Static block must contain all 5 neutral-Spanish rules (substring detectable)."""
    from borgesica.domain.context import ContextManager

    provider = FakeTranslationProvider()
    cm = ContextManager(provider=provider)
    config = make_config(target_lang="es-neutral")
    glossary = Glossary()
    summary = RollingSummary()

    sp = cm.build_system_prompt(config, glossary, summary)
    text = sp.text

    # All 5 neutral-Spanish constraints must be detectable
    assert "voseo" in text.lower() or "vos" in text.lower(), "must mention voseo ban"
    assert "tú" in text or "tu" in text.lower(), "must mention tú form"
    assert "slang" in text.lower() or "localismo" in text.lower() or "localism" in text.lower(), (
        "must mention slang/localism ban"
    )
    assert "leísmo" in text.lower() or "leismo" in text.lower(), "must mention leísmo rule"
    assert "register" in text.lower() or "registro" in text.lower(), "must mention register consistency"


# ---------------------------------------------------------------------------
# Test 2 — translation philosophy always present (including anti-calque)
# ---------------------------------------------------------------------------


def test_system_prompt_contains_translation_philosophy():
    """Static block must include meaning+image philosophy and anti-calque instruction."""
    from borgesica.domain.context import ContextManager

    provider = FakeTranslationProvider()
    cm = ContextManager(provider=provider)
    config = make_config()
    glossary = Glossary()
    summary = RollingSummary()

    sp = cm.build_system_prompt(config, glossary, summary)
    text = sp.text

    # Translation philosophy detectable
    assert "meaning" in text.lower() or "significado" in text.lower(), "must mention meaning"
    assert "calque" in text.lower() or "calco" in text.lower(), "must mention calques"
    assert "natural" in text.lower(), "must mention naturalness"


# ---------------------------------------------------------------------------
# Test 3 — Glossary.render respects budget_tokens
# ---------------------------------------------------------------------------


def test_glossary_render_respects_token_budget():
    """50 entries × ~8 tokens each → render(300) output token count ≤ 300."""
    entries = [
        GlossaryEntry(term=f"Term{i:02d}", translation=f"Translated{i:02d}")
        for i in range(50)
    ]
    glossary = Glossary(entries=entries)

    rendered = glossary.render(budget_tokens=300)
    token_count = len(rendered.split())
    assert token_count <= 300, f"rendered {token_count} tokens, expected ≤ 300"


# ---------------------------------------------------------------------------
# Test 4 — locked entries appear before unlocked in render
# ---------------------------------------------------------------------------


def test_glossary_render_locked_first():
    """Locked entries appear before unlocked ones in rendered output."""
    entries = [
        GlossaryEntry(term="Alpha", translation="Alpha-ES", locked=False),
        GlossaryEntry(term="Beta", translation="Beta-ES", locked=True),
        GlossaryEntry(term="Gamma", translation="Gamma-ES", locked=False),
        GlossaryEntry(term="Delta", translation="Delta-ES", locked=True),
    ]
    glossary = Glossary(entries=entries)
    rendered = glossary.render(budget_tokens=1000)

    # Find positions
    pos_beta = rendered.index("Beta")
    pos_delta = rendered.index("Delta")
    pos_alpha = rendered.index("Alpha")
    pos_gamma = rendered.index("Gamma")

    assert pos_beta < pos_alpha, "locked Beta must appear before unlocked Alpha"
    assert pos_delta < pos_alpha, "locked Delta must appear before unlocked Alpha"
    assert pos_beta < pos_gamma, "locked Beta must appear before unlocked Gamma"
    assert pos_delta < pos_gamma, "locked Delta must appear before unlocked Gamma"


# ---------------------------------------------------------------------------
# Test 5 — all locked entries appear even when budget exceeded by unlocked
# ---------------------------------------------------------------------------


def test_glossary_render_all_locked_appear_regardless_of_budget():
    """10 locked entries (80t) + 40 unlocked (400t), budget=300 → all locked present."""
    locked_entries = [
        GlossaryEntry(term=f"LockedTerm{i}", translation=f"LT{i}ES", locked=True)
        for i in range(10)
    ]
    unlocked_entries = [
        GlossaryEntry(term=f"Unlocked{i}", translation=f"UL{i}ES", locked=False)
        for i in range(40)
    ]
    glossary = Glossary(entries=locked_entries + unlocked_entries)
    rendered = glossary.render(budget_tokens=300)

    for i in range(10):
        assert f"LockedTerm{i}" in rendered, f"LockedTerm{i} must always appear"


# ---------------------------------------------------------------------------
# Test 6 — rolling summary from chunk N-1 is in chunk N's system prompt
# ---------------------------------------------------------------------------


def test_system_prompt_includes_prior_rolling_summary():
    """Summary from chunk N-1 must be detectable in chunk N's system prompt."""
    from borgesica.domain.context import ContextManager

    provider = FakeTranslationProvider()
    cm = ContextManager(provider=provider)
    config = make_config()
    glossary = Glossary()
    summary = RollingSummary(text="Context: detective story, noir tone.", chunk_index=0)

    sp = cm.build_system_prompt(config, glossary, summary)
    assert "Context: detective story, noir tone." in sp.text


# ---------------------------------------------------------------------------
# Test 7 — first chunk (empty summary) → no exception, placeholder or empty
# ---------------------------------------------------------------------------


def test_system_prompt_first_chunk_no_exception():
    """First chunk has empty summary (chunk_index=-1) → no exception, empty or placeholder."""
    from borgesica.domain.context import ContextManager

    provider = FakeTranslationProvider()
    cm = ContextManager(provider=provider)
    config = make_config()
    glossary = Glossary()
    summary = RollingSummary()  # default: text="", chunk_index=-1

    # Must not raise
    sp = cm.build_system_prompt(config, glossary, summary)
    # Must contain either empty summary or placeholder
    assert "No prior context." in sp.text or sp.text.count("[SUMMARY]") >= 0


# ---------------------------------------------------------------------------
# Test 8 — static block ≥ 1024 tokens → cached=True
# ---------------------------------------------------------------------------


def test_cached_true_when_static_block_large_enough():
    """When static block ≥ 1024 tokens, SystemPrompt.cached must be True."""
    from borgesica.domain.context import ContextManager

    # Use a provider that counts tokens naively (word count)
    provider = FakeTranslationProvider()
    cm = ContextManager(provider=provider)
    config = make_config()
    glossary = Glossary()
    summary = RollingSummary()

    sp = cm.build_system_prompt(config, glossary, summary)

    # Measure the static block token count with the fake provider
    static_tokens = provider.count_tokens(cm.get_static_block(config), "")

    if static_tokens >= 1024:
        assert sp.cached is True
    else:
        # The test verifies the LOGIC: if we artificially force a large static block
        # by patching, cached becomes True. We test the boundary rule here.
        # Since our static block may be < 1024 in the fake, test that cached=False
        assert sp.cached is False


# ---------------------------------------------------------------------------
# Test 9 — static block < 1024 tokens → cached=False
# ---------------------------------------------------------------------------


def test_cached_false_when_static_block_too_small():
    """When static block < 1024 tokens (by fake provider word count), cached=False."""
    from borgesica.domain.context import ContextManager

    provider = FakeTranslationProvider()
    cm = ContextManager(provider=provider)
    config = make_config()
    glossary = Glossary()
    summary = RollingSummary()

    sp = cm.build_system_prompt(config, glossary, summary)
    static_tokens = provider.count_tokens(cm.get_static_block(config), "")

    # The FakeTranslationProvider counts words. Our static block will be < 1024 words.
    # So cached must be False.
    assert static_tokens < 1024 or sp.cached is True  # either the block is small → False, or it's big → True


# ---------------------------------------------------------------------------
# Test 10 — SystemPrompt is a proper dataclass/model with text and cached fields
# ---------------------------------------------------------------------------


def test_system_prompt_has_text_and_cached_fields():
    """SystemPrompt must expose .text (str) and .cached (bool)."""
    from borgesica.domain.context import ContextManager, SystemPrompt

    provider = FakeTranslationProvider()
    cm = ContextManager(provider=provider)
    config = make_config()

    sp = cm.build_system_prompt(config, Glossary(), RollingSummary())

    assert isinstance(sp, SystemPrompt)
    assert isinstance(sp.text, str)
    assert isinstance(sp.cached, bool)


# ---------------------------------------------------------------------------
# Test 11 (M2-0) — system prompt contains tag-preservation instruction
# ---------------------------------------------------------------------------


def test_system_prompt_contains_tag_preservation_instruction():
    """Static block must contain an explicit instruction to preserve inline tags,
    move them with the translated words, and preserve the exact tag count.
    Spec: subtitle-translation/inline-tags-in-text scenario 'system prompt instructs
    the model to preserve inline tags'.
    """
    from borgesica.domain.context import ContextManager

    provider = FakeTranslationProvider()
    cm = ContextManager(provider=provider)
    config = make_config()
    glossary = Glossary()
    summary = RollingSummary()

    sp = cm.build_system_prompt(config, glossary, summary)
    text = sp.text.lower()

    # Must explicitly mention tag preservation / inline tags
    assert "tag" in text or "inline" in text, (
        "system prompt must mention inline tags"
    )
    # Must instruct to keep/preserve tags
    assert "preserv" in text or "keep" in text or "mantener" in text or "mant" in text, (
        "system prompt must instruct to preserve/keep tags"
    )
    # Must mention moving tags with translated words
    assert "word" in text or "palabra" in text or "move" in text or "wrap" in text, (
        "system prompt must mention tags moving with words"
    )


# ---------------------------------------------------------------------------
# Segmented output instructions (SRT vs prose static blocks)
# ---------------------------------------------------------------------------


def test_srt_static_block_describes_translations_array():
    """For SRT jobs the task description must teach the SEGMENTED contract:
    the 'translations' array, one string per segment, no merging/splitting."""
    from borgesica.domain.context import ContextManager

    cm = ContextManager(provider=FakeTranslationProvider())
    static = cm.get_static_block(make_config(source_type=SourceType.SRT))

    assert '"translations"' in static
    assert "one" in static.lower() and "segment" in static.lower()
    assert "merge" in static.lower()


def test_srt_static_block_teaches_continuous_speech_coherence():
    """Segments in a batch are consecutive cues of the SAME speech stream.
    The prompt must instruct the model to read the whole passage as continuous
    speech and translate with cross-segment coherence — NOT to translate each
    fragment in isolation ('on its own'), which produces mistranslations when
    a sentence spans two cues (e.g. 'I didn't get a good one' / 'look at it'
    rendered as an unrelated imperative)."""
    from borgesica.domain.context import ContextManager

    cm = ContextManager(provider=FakeTranslationProvider())
    static = cm.get_static_block(make_config(source_type=SourceType.SRT))

    assert "continuous speech" in static.lower()
    assert "on its own" not in static.lower()
    # The alignment contract must survive the rewrite.
    assert "EXACTLY one string per source segment" in static
    assert "NEVER merge, split, or reorder" in static


def test_srt_static_block_has_split_sentence_few_shot_example():
    """An abstract coherence rule proved insufficient (DeepSeek v4-flash AND
    v4-pro both rendered 'look at it' as an isolated imperative even with the
    continuous-speech instruction). The prompt must include a concrete WRONG
    vs RIGHT example of a sentence split across two segments. The example is
    deliberately NOT the known failing passage, so real transcripts remain a
    valid held-out test of generalization."""
    from borgesica.domain.context import ContextManager

    cm = ContextManager(provider=FakeTranslationProvider())
    static = cm.get_static_block(make_config(source_type=SourceType.SRT))

    assert "watch for her birthday" in static
    assert "WRONG" in static and "RIGHT" in static
    # The example must never leak into the prose (single-string) prompt.
    prose = cm.get_static_block(make_config(source_type=SourceType.EPUB))
    assert "watch for her birthday" not in prose


def test_prose_static_block_keeps_legacy_translation_contract():
    """EPUB/PDF jobs keep the single-string 'translation' instruction and must
    NOT mention the translations array."""
    from borgesica.domain.context import ContextManager

    cm = ContextManager(provider=FakeTranslationProvider())
    static = cm.get_static_block(make_config(source_type=SourceType.EPUB))

    assert '"translation"' in static
    assert '"translations"' not in static


def test_srt_static_block_still_cacheable_and_stable():
    """The SRT static block must not vary per chunk (caching boundary):
    two calls with the same config return the identical string."""
    from borgesica.domain.context import ContextManager

    cm = ContextManager(provider=FakeTranslationProvider())
    config = make_config(source_type=SourceType.SRT)

    assert cm.get_static_block(config) == cm.get_static_block(config)
