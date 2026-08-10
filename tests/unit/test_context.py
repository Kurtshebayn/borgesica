"""Tests for ContextManager (M1-5).

Strict TDD: all tests written before implementation.
Spec: context-continuity + translation-quality + cost-control/prompt-caching
"""
from __future__ import annotations

import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

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

    cm = ContextManager()
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

    cm = ContextManager()
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
# B1a — Glossary budget: the default must fit a real book's glossary
#
# Measured before the fix: at budget_tokens=300 the renderer hit a HARD ceiling
# at ~67 entries regardless of glossary size, so a 400-term novel delivered 67
# terms and silently dropped 333. Because the cut is a `break` over insertion
# order, every term past the ceiling was invisible in EVERY chunk, permanently
# — which is why term drift showed up late in a long book, not early.
# ---------------------------------------------------------------------------


def _novel_glossary(n: int) -> Glossary:
    """n realistic proper-noun entries, shaped like extractor output."""
    return Glossary(
        entries=[
            GlossaryEntry(term=f"Gleaners{i:03d}", translation=f"Espigadores{i:03d}")
            for i in range(n)
        ]
    )


def test_glossary_render_default_budget_fits_a_full_novel_glossary():
    """300 unlocked entries must ALL survive render() at its default budget."""
    glossary = _novel_glossary(300)

    rendered = glossary.render()

    kept = len([ln for ln in rendered.split("\n") if ln.strip()])
    assert kept == 300, f"only {kept}/300 entries reached the prompt"


def test_glossary_render_still_truncates_when_budget_is_explicitly_small():
    """The budget still works — raising the default did not disable trimming."""
    glossary = _novel_glossary(300)

    rendered = glossary.render(budget_tokens=30)

    kept = len([ln for ln in rendered.split("\n") if ln.strip()])
    assert 0 < kept < 300


def test_glossary_render_omits_notes():
    """Notes are for the human reviewing the glossary, not for the prompt.

    Measured on a real 491-entry book glossary: every entry carried a note,
    and the notes were 78% of the rendered weight (8482 words with them, 1868
    without). Repeating explanatory prose to the model on all 502 chunks
    crowded out the term->translation pairs that actually enforce consistency.
    """
    glossary = Glossary(
        entries=[
            GlossaryEntry(
                term="Aaru",
                translation="Aaru",
                note="El Campo de Juncos; concepto de más allá en la tradición de las tumbas.",
            )
        ]
    )

    rendered = glossary.render()

    assert "Aaru" in rendered
    assert "Campo de Juncos" not in rendered
    assert "más allá" not in rendered


def test_glossary_render_default_budget_fits_a_real_book_glossary():
    """491 note-bearing entries — the real shape — must all reach the prompt.

    The earlier synthetic fixture used 3-word entries and badly overstated
    coverage: with notes rendered, real entries averaged 17.3 words and only
    92 of 491 survived at a 1500 budget.
    """
    glossary = Glossary(
        entries=[
            GlossaryEntry(
                term=f"Gleaners{i:03d}",
                translation=f"Segadores{i:03d}",
                note="Explicación larga que no debe llegar nunca al prompt del modelo.",
            )
            for i in range(491)
        ]
    )

    rendered = glossary.render()

    kept = len([ln for ln in rendered.split("\n") if ln.strip()])
    assert kept == 491, f"only {kept}/491 entries reached the prompt"


def test_build_system_prompt_honors_config_glossary_budget():
    """The per-chunk prompt uses config.glossary_budget_tokens, not a constant.

    Before the fix context.py hardcoded budget_tokens=300, so the budget was
    unreachable from JobConfig and could not be tuned per job or per provider.
    """
    from borgesica.domain.context import ContextManager

    manager = ContextManager()
    glossary = _novel_glossary(300)
    summary = RollingSummary()

    generous = manager.build_system_prompt(
        make_config(glossary_budget_tokens=5000), glossary, summary
    )
    stingy = manager.build_system_prompt(
        make_config(glossary_budget_tokens=30), glossary, summary
    )

    assert "Gleaners299" in generous.text
    assert "Gleaners299" not in stingy.text


# ---------------------------------------------------------------------------
# Test 6 — rolling summary from chunk N-1 is in chunk N's system prompt
# ---------------------------------------------------------------------------


def test_system_prompt_includes_prior_rolling_summary():
    """Summary from chunk N-1 must be detectable in chunk N's system prompt."""
    from borgesica.domain.context import ContextManager

    cm = ContextManager()
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

    cm = ContextManager()
    config = make_config()
    glossary = Glossary()
    summary = RollingSummary()  # default: text="", chunk_index=-1

    # Must not raise
    sp = cm.build_system_prompt(config, glossary, summary)
    # Must contain either empty summary or placeholder
    assert "No prior context." in sp.text or sp.text.count("[SUMMARY]") >= 0


# ---------------------------------------------------------------------------
# SystemPrompt.cached removed — it was computed and never consumed
#
# No adapter ever read the hint: AnthropicProvider.translate passes `system`
# through as a plain string and nothing anywhere sets cache_control. Computing
# it cost a count_tokens call on every chunk to produce a boolean no caller
# looked at. CostEstimate.cached is a separate thing and stays: it is surfaced
# to the user by `estimate` and by the HTTP schema.
# ---------------------------------------------------------------------------


def test_system_prompt_exposes_text_only():
    """SystemPrompt carries the prompt text and nothing else."""
    from borgesica.domain.context import ContextManager, SystemPrompt

    cm = ContextManager()

    sp = cm.build_system_prompt(make_config(), Glossary(), RollingSummary())

    assert isinstance(sp, SystemPrompt)
    assert isinstance(sp.text, str)
    assert not hasattr(sp, "cached")


def test_context_manager_needs_no_provider():
    """Prompt assembly is pure string work — it holds no provider.

    The provider existed solely for the count_tokens call behind the deleted
    `cached` hint. Dropping the dependency makes "assembling a prompt costs
    nothing" a property of the type rather than something a test must guard.
    """
    from borgesica.domain.context import ContextManager

    cm = ContextManager()

    sp = cm.build_system_prompt(make_config(), Glossary(), RollingSummary())

    assert sp.text
    assert not hasattr(cm, "_provider")


def test_cost_estimate_still_reports_cached():
    """The user-facing hint survives: `estimate` and the HTTP schema expose it."""
    from borgesica.domain.models import CostEstimate

    estimate = CostEstimate(
        input_tokens=1, output_tokens=1, usd=0.0, model="m", cached=True
    )

    assert estimate.cached is True


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

    cm = ContextManager()
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

    cm = ContextManager()
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

    cm = ContextManager()
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

    cm = ContextManager()
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

    cm = ContextManager()
    static = cm.get_static_block(make_config(source_type=SourceType.EPUB))

    assert '"translation"' in static
    assert '"translations"' not in static


def test_srt_static_block_still_cacheable_and_stable():
    """The SRT static block must not vary per chunk (caching boundary):
    two calls with the same config return the identical string."""
    from borgesica.domain.context import ContextManager

    cm = ContextManager()
    config = make_config(source_type=SourceType.SRT)

    assert cm.get_static_block(config) == cm.get_static_block(config)


# ---------------------------------------------------------------------------
# B1d — identity entries (term == translation) are compacted
#
# Measured on the real 491-entry book glossary of job 13b43ac6: 337 entries
# (69%) had term == translation. Those are proper nouns that correctly must
# not be translated, but rendering each as "Aaru -> Aaru" spent two thirds of
# the per-call glossary budget repeating every term back to itself. They are
# now collapsed into a single do-not-translate line.
# ---------------------------------------------------------------------------


def _identity_terms_line(rendered: str) -> str:
    """Return the compact do-not-translate line, or "" if absent."""
    for line in rendered.splitlines():
        if "DO NOT TRANSLATE" in line:
            return line
    return ""


def test_render_compacts_identity_entries_into_one_line():
    """term == translation entries share a single line instead of one each."""
    glossary = Glossary(
        entries=[
            GlossaryEntry(term="Aaru", translation="Aaru"),
            GlossaryEntry(term="Alupi", translation="Alupi"),
            GlossaryEntry(term="Crannog", translation="Crannog"),
        ]
    )

    rendered = glossary.render()

    line = _identity_terms_line(rendered)
    assert line, "expected a do-not-translate line"
    for term in ("Aaru", "Alupi", "Crannog"):
        assert term in line
    assert "→" not in rendered, "identity entries must not render as arrow pairs"


def test_render_keeps_real_mappings_as_arrow_pairs():
    """A term that genuinely changes still needs its explicit mapping."""
    glossary = Glossary(
        entries=[
            GlossaryEntry(term="Gleaner", translation="Segador"),
            GlossaryEntry(term="Aaru", translation="Aaru"),
        ]
    )

    rendered = glossary.render()

    assert "Gleaner → Segador" in rendered
    assert "Aaru" in _identity_terms_line(rendered)


def test_render_compaction_is_cheaper_than_arrow_pairs():
    """The whole point: the compact form must cost materially fewer words."""
    terms = [f"Propio{i:03d}" for i in range(337)]
    glossary = Glossary(
        entries=[GlossaryEntry(term=t, translation=t) for t in terms]
    )

    compact_words = len(glossary.render().split())
    arrow_words = len(" ".join(f"{t} → {t}" for t in terms).split())

    assert compact_words < arrow_words / 2, (
        f"compact form cost {compact_words} words vs {arrow_words} as arrow pairs"
    )


def test_render_identity_detection_ignores_whitespace_and_composition():
    """Normalisation decides identity, so stray spacing does not force a pair."""
    glossary = Glossary(entries=[GlossaryEntry(term="Caer  Dathyl", translation="Caer Dathyl ")])

    rendered = glossary.render()

    assert "Caer Dathyl" in _identity_terms_line(rendered)
    assert "→" not in rendered


def test_render_treats_a_case_difference_as_a_real_mapping():
    """"Alupi -> alupi" instructs a change; it is not a do-not-translate term."""
    glossary = Glossary(entries=[GlossaryEntry(term="Alupi", translation="alupi")])

    rendered = glossary.render()

    assert "Alupi → alupi" in rendered
    assert _identity_terms_line(rendered) == ""


def test_render_always_includes_locked_identity_terms():
    """The locked guarantee holds in the compact form too."""
    locked = [
        GlossaryEntry(term=f"Pinned{i:02d}", translation=f"Pinned{i:02d}", locked=True)
        for i in range(10)
    ]
    filler = [
        GlossaryEntry(term=f"Relleno{i:03d}", translation=f"Traducido{i:03d}")
        for i in range(200)
    ]
    glossary = Glossary(entries=locked + filler)

    rendered = glossary.render(budget_tokens=40)

    for i in range(10):
        assert f"Pinned{i:02d}" in rendered, f"locked Pinned{i:02d} must always appear"


def test_render_trims_unlocked_identity_terms_when_the_budget_runs_out():
    """Compaction lowers the cost of identity terms; it does not exempt them."""
    glossary = Glossary(
        entries=[
            GlossaryEntry(term=f"Propio{i:03d}", translation=f"Propio{i:03d}")
            for i in range(300)
        ]
    )

    rendered = glossary.render(budget_tokens=30)

    line = _identity_terms_line(rendered)
    kept = [t for t in line.split() if t.startswith("Propio")]
    assert 0 < len(kept) < 300


def test_render_spends_the_budget_on_real_mappings_before_identity_terms():
    """A mapping teaches a translation; an identity term only withholds one."""
    mappings = [
        GlossaryEntry(term=f"Ingles{i:02d}", translation=f"Espanol{i:02d}")
        for i in range(10)
    ]
    identities = [
        GlossaryEntry(term=f"Propio{i:03d}", translation=f"Propio{i:03d}")
        for i in range(300)
    ]
    glossary = Glossary(entries=identities + mappings)

    rendered = glossary.render(budget_tokens=45)

    for i in range(10):
        assert f"Ingles{i:02d}" in rendered, f"mapping Ingles{i:02d} was crowded out"


def test_render_respects_the_budget_with_a_mixed_glossary():
    """The compact line is charged against the budget like everything else."""
    identities = [
        GlossaryEntry(term=f"Propio{i:03d}", translation=f"Propio{i:03d}")
        for i in range(300)
    ]
    mappings = [
        GlossaryEntry(term=f"Ingles{i:03d}", translation=f"Espanol{i:03d}")
        for i in range(100)
    ]
    glossary = Glossary(entries=identities + mappings)

    rendered = glossary.render(budget_tokens=200)

    assert len(rendered.split()) <= 200


def test_render_emits_no_do_not_translate_line_without_identity_entries():
    """No identity entries means no wasted header."""
    glossary = Glossary(entries=[GlossaryEntry(term="Gleaner", translation="Segador")])

    assert _identity_terms_line(glossary.render()) == ""


def test_render_of_an_empty_glossary_is_still_empty():
    """An empty glossary must not grow a header."""
    assert Glossary().render() == ""
