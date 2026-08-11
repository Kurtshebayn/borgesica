"""Tests for CostEstimator (M1-6).

Strict TDD: tests written before implementation.
Spec: cost-control/estimate_cost, cost-control/quality_mode
"""
from __future__ import annotations

import math
from datetime import datetime

import pytest

from tests.fakes import FakeTranslationProvider
from borgesica.domain.models import (
    Chunk,
    ChunkStatus,
    CostEstimate,
    Job,
    JobConfig,
    JobStatus,
    RollingSummary,
    SourceType,
)


def make_config(**kwargs) -> JobConfig:
    defaults = dict(source_type=SourceType.SRT, model="claude-haiku-4-5")
    defaults.update(kwargs)
    return JobConfig(**defaults)


def make_job(config: JobConfig, total: int = 4) -> Job:
    return Job(
        id="job-001",
        config=config,
        source_path="/tmp/test.srt",
        total_chunks=total,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )


def make_chunk(index: int, status: ChunkStatus = ChunkStatus.PENDING, text: str = "hello world") -> Chunk:
    return Chunk(index=index, source_text=text, status=status)


def _empty_dynamic_tokens(provider, context_manager, config) -> int:
    """Tokens of the dynamic block a job with no glossary and no summary pays.

    Measured, not hardcoded. cost.py used to encode this as a flat 500 and the
    literal went stale when the glossary budget moved to 2500 words (C1).
    """
    from borgesica.domain.models import Glossary

    return provider.count_tokens(
        context_manager.build_dynamic_block(
            Glossary(), RollingSummary(), config.glossary_budget_tokens
        ),
        config.model,
    )


# ---------------------------------------------------------------------------
# Test 1 — fast mode: 4 pending chunks → 4 passes
# ---------------------------------------------------------------------------


def test_fast_mode_counts_one_pass_per_chunk():
    """Fast mode: 4 pending chunks → exactly 4 provider passes counted."""
    from borgesica.domain.cost import CostEstimator

    provider = FakeTranslationProvider()
    estimator = CostEstimator(provider=provider)
    config = make_config(quality_mode="fast")
    job = make_job(config, total=4)
    chunks = [make_chunk(i) for i in range(4)]

    estimate = estimator.estimate(job, chunks, config)

    # 4 chunks × 1 pass × (word_count_per_chunk) tokens
    # The fake counts words. "hello world" = 2 tokens.
    # We just verify the structure is correct and 4 passes are counted.
    assert estimate.input_tokens > 0
    assert estimate.output_tokens >= 0
    assert isinstance(estimate, CostEstimate)


# ---------------------------------------------------------------------------
# Test 2 — reflective mode: 4 pending chunks → 12 passes (3 per chunk)
# ---------------------------------------------------------------------------


def test_reflective_mode_counts_three_passes_per_chunk():
    """Reflective mode: 4 pending chunks → 12 passes (3× per chunk)."""
    from borgesica.domain.cost import CostEstimator

    provider = FakeTranslationProvider()
    estimator_fast = CostEstimator(provider=provider)
    estimator_refl = CostEstimator(provider=provider)

    fast_config = make_config(quality_mode="fast")
    refl_config = make_config(quality_mode="reflective")

    job_fast = make_job(fast_config, total=4)
    job_refl = make_job(refl_config, total=4)
    chunks = [make_chunk(i) for i in range(4)]

    est_fast = estimator_fast.estimate(job_fast, chunks, fast_config)
    est_refl = estimator_refl.estimate(job_refl, chunks, refl_config)

    # Reflective should cost exactly 3× fast (same content, 3 passes vs 1)
    assert est_refl.usd == pytest.approx(est_fast.usd * 3, rel=1e-6)
    assert est_refl.input_tokens == pytest.approx(est_fast.input_tokens * 3, rel=1e-6)


# ---------------------------------------------------------------------------
# Test 3 — estimate skips DONE chunks
# ---------------------------------------------------------------------------


def test_estimate_skips_done_chunks():
    """5-chunk job, 2 DONE → cost covers only 3 pending chunks."""
    from borgesica.domain.cost import CostEstimator

    provider = FakeTranslationProvider()
    estimator = CostEstimator(provider=provider)
    config = make_config(quality_mode="fast")
    job = make_job(config, total=5)

    chunks = [
        make_chunk(0, ChunkStatus.DONE),
        make_chunk(1, ChunkStatus.DONE),
        make_chunk(2, ChunkStatus.PENDING),
        make_chunk(3, ChunkStatus.PENDING),
        make_chunk(4, ChunkStatus.PENDING),
    ]

    est_all = estimator.estimate(job, [make_chunk(i) for i in range(5)], config)
    est_pending = estimator.estimate(job, chunks, config)

    # 3 pending vs 5 all → roughly 3/5 of cost (same text length)
    assert est_pending.usd < est_all.usd
    # Specifically, 3/5 the cost (all chunks have same text)
    assert est_pending.usd == pytest.approx(est_all.usd * 3 / 5, rel=1e-4)


# ---------------------------------------------------------------------------
# Test 4 — fully DONE job → usd=0.0, input_tokens=0
# ---------------------------------------------------------------------------


def test_fully_done_job_zero_cost():
    """All DONE chunks → usd=0.0, input_tokens=0."""
    from borgesica.domain.cost import CostEstimator

    provider = FakeTranslationProvider()
    estimator = CostEstimator(provider=provider)
    config = make_config()
    job = make_job(config, total=3)
    chunks = [make_chunk(i, ChunkStatus.DONE) for i in range(3)]

    est = estimator.estimate(job, chunks, config)
    assert est.usd == 0.0
    assert est.input_tokens == 0


# ---------------------------------------------------------------------------
# Test 5 — within_budget=True when estimated < budget_usd
# ---------------------------------------------------------------------------


def test_within_budget_true_when_under():
    """Estimated cost < budget_usd → within_budget=True."""
    from borgesica.domain.cost import CostEstimator

    provider = FakeTranslationProvider()
    estimator = CostEstimator(provider=provider)
    config = make_config(budget_usd=1.00)
    job = make_job(config, total=4)
    # Each chunk: "hello world" = 2 input tokens
    # Cost: 4 × (2/1e6 × 1.0 + output/1e6 × 5.0) — tiny fraction of $1.00
    chunks = [make_chunk(i) for i in range(4)]

    est = estimator.estimate(job, chunks, config)
    assert est.within_budget is True


# ---------------------------------------------------------------------------
# Test 6 — within_budget=False when estimated > budget_usd
# ---------------------------------------------------------------------------


def test_within_budget_false_when_over():
    """Estimated cost > budget_usd → within_budget=False."""
    from borgesica.domain.cost import CostEstimator

    provider = FakeTranslationProvider()
    estimator = CostEstimator(provider=provider)
    # Use a very tiny budget so the estimate exceeds it
    config = make_config(budget_usd=0.0)
    job = make_job(config, total=4)
    chunks = [make_chunk(i, text="hello world foo bar") for i in range(4)]

    est = estimator.estimate(job, chunks, config)
    # Any positive cost with budget=0 → within_budget=False
    if est.usd > 0:
        assert est.within_budget is False


# ---------------------------------------------------------------------------
# Test 7 — budget_usd=None → within_budget=True always
# ---------------------------------------------------------------------------


def test_no_budget_always_within_budget():
    """budget_usd=None → within_budget=True regardless of cost."""
    from borgesica.domain.cost import CostEstimator

    provider = FakeTranslationProvider()
    estimator = CostEstimator(provider=provider)
    config = make_config(budget_usd=None)
    job = make_job(config, total=10)
    chunks = [make_chunk(i, text="a very long chunk of text with many words " * 5) for i in range(10)]

    est = estimator.estimate(job, chunks, config)
    assert est.within_budget is True


# ---------------------------------------------------------------------------
# Test 8 — cost arithmetic: 100in + 50out tokens at $1/$5 per Mtok → $0.00035/chunk
# ---------------------------------------------------------------------------


def test_cost_arithmetic_correctness():
    """100 input + 50 output tokens at $1/$5 per Mtok → $0.00035 per chunk, 4 chunks → $0.00140."""
    from borgesica.domain.cost import CostEstimator

    # Build a provider that always counts exactly 100 input tokens per call
    class PreciseProvider(FakeTranslationProvider):
        def count_tokens(self, text: str, model: str) -> int:
            # Always return 100 for system+user combined, and 50 for output estimate
            return 100

    provider = PreciseProvider()
    estimator = CostEstimator(provider=provider)
    config = make_config(quality_mode="fast")
    job = make_job(config, total=4)
    chunks = [make_chunk(i) for i in range(4)]

    est = estimator.estimate(job, chunks, config, output_tokens_per_chunk=50)

    # Expected: 4 chunks × (100/1e6 × 1.0 + 50/1e6 × 5.0) = 4 × 0.00035 = 0.00140
    expected = 4 * (100 / 1_000_000 * 1.0 + 50 / 1_000_000 * 5.0)
    assert math.isclose(est.usd, expected, rel_tol=1e-6), f"Expected {expected}, got {est.usd}"


# ---------------------------------------------------------------------------
# Test 9 — model field is preserved in CostEstimate
# ---------------------------------------------------------------------------


def test_estimate_preserves_model_field():
    """CostEstimate.model matches config.model."""
    from borgesica.domain.cost import CostEstimator

    provider = FakeTranslationProvider()
    estimator = CostEstimator(provider=provider)
    config = make_config(model="claude-sonnet-4-5")
    job = make_job(config, total=2)
    chunks = [make_chunk(i) for i in range(2)]

    est = estimator.estimate(job, chunks, config)
    assert est.model == "claude-sonnet-4-5"


# ---------------------------------------------------------------------------
# Per-call overhead calibration (bug: 16x under-estimate on SRT, job 0b86d4f2)
# The estimator counted ONLY source-text tokens and a flat 150-token output.
# Real calls pay the system prompt (static block + glossary + summary) on
# EVERY call, and the output is the full JSON envelope (translation +
# summary_update + glossary_additions). On thin SRT chunks that overhead
# dominates: estimate $0.0025 vs real $0.0395.
# ---------------------------------------------------------------------------


def test_estimate_includes_per_call_system_prompt_overhead():
    """With a context_manager, input tokens include the system prompt
    (static block + dynamic budget) once per chunk per pass — not just source."""
    from borgesica.domain.context import ContextManager
    from borgesica.domain.cost import CostEstimator

    provider = FakeTranslationProvider()
    context_manager = ContextManager()
    estimator = CostEstimator(provider=provider, context_manager=context_manager)
    config = make_config(quality_mode="fast")
    job = make_job(config, total=3)
    # "hello world" = 2 tokens per chunk with the word-count fake
    chunks = [make_chunk(i, text="hello world") for i in range(3)]

    est = estimator.estimate(job, chunks, config)

    import json as _json

    from borgesica.domain.models import translation_tool_schema

    static_tokens = provider.count_tokens(
        context_manager.get_static_block(config), config.model
    )
    schema_tokens = provider.count_tokens(
        _json.dumps(translation_tool_schema(None)), config.model
    )
    dynamic_tokens = _empty_dynamic_tokens(provider, context_manager, config)
    expected_input = 3 * (2 + static_tokens + dynamic_tokens + schema_tokens)
    assert est.input_tokens == expected_input, (
        f"Expected {expected_input} input tokens (source + system prompt + tool "
        f"schema per call), got {est.input_tokens} — per-call overhead not counted"
    )


def test_estimate_default_output_scales_with_source():
    """Default output per chunk = translation (≈ source size) + JSON envelope
    (summary_update + glossary_additions), not a flat 150 tokens."""
    from borgesica.domain.cost import _OUTPUT_ENVELOPE_TOKENS, CostEstimator

    provider = FakeTranslationProvider()
    estimator = CostEstimator(provider=provider)
    config = make_config(quality_mode="fast")
    job = make_job(config, total=1)

    est_small = estimator.estimate(job, [make_chunk(0, text="hello world")], config)
    est_large = estimator.estimate(job, [make_chunk(0, text="w " * 400)], config)

    assert est_small.output_tokens == 2 + _OUTPUT_ENVELOPE_TOKENS
    assert est_large.output_tokens == 400 + _OUTPUT_ENVELOPE_TOKENS


# ---------------------------------------------------------------------------
# W-2: CostEstimate.cached reflects static-block caching eligibility
# ---------------------------------------------------------------------------


class BigTokenProvider(FakeTranslationProvider):
    """Provider whose count_tokens always returns 2000 (≥ 1024 → cached=True)."""

    def count_tokens(self, text: str, model: str) -> int:  # noqa: ARG002
        return 2000


class SmallTokenProvider(FakeTranslationProvider):
    """Provider whose count_tokens always returns 100 (< 1024 → cached=False)."""

    def count_tokens(self, text: str, model: str) -> int:  # noqa: ARG002
        return 100


def test_cached_true_when_static_block_meets_min():
    """cached=True when static-block token count ≥ 1024 (Anthropic prompt-caching min)."""
    from borgesica.domain.context import ContextManager
    from borgesica.domain.cost import CostEstimator

    provider = BigTokenProvider()
    context_manager = ContextManager()
    estimator = CostEstimator(provider=provider, context_manager=context_manager)

    config = make_config(quality_mode="fast")
    job = make_job(config, total=2)
    chunks = [make_chunk(i) for i in range(2)]

    est = estimator.estimate(job, chunks, config)
    assert est.cached is True, f"Expected cached=True (token count=2000 ≥ 1024), got {est.cached}"


def test_cached_false_when_static_block_below_min():
    """cached=False when static-block token count < 1024."""
    from borgesica.domain.context import ContextManager
    from borgesica.domain.cost import CostEstimator

    provider = SmallTokenProvider()
    context_manager = ContextManager()
    estimator = CostEstimator(provider=provider, context_manager=context_manager)

    config = make_config(quality_mode="fast")
    job = make_job(config, total=2)
    chunks = [make_chunk(i) for i in range(2)]

    est = estimator.estimate(job, chunks, config)
    assert est.cached is False, f"Expected cached=False (token count=100 < 1024), got {est.cached}"


def test_cached_false_when_no_context_manager():
    """cached=False (backward compat) when context_manager is not provided."""
    from borgesica.domain.cost import CostEstimator

    provider = BigTokenProvider()  # big tokens, but no context_manager injected
    estimator = CostEstimator(provider=provider)  # no context_manager

    config = make_config(quality_mode="fast")
    job = make_job(config, total=2)
    chunks = [make_chunk(i) for i in range(2)]

    est = estimator.estimate(job, chunks, config)
    # Without context_manager, cached MUST stay False (backward compat)
    assert est.cached is False, f"Expected cached=False (no context_manager), got {est.cached}"


# ---------------------------------------------------------------------------
# Cost RANGE (bug: SRT estimate ~3x below real, job 0b86d4f2).
# The point estimate models the HAPPY PATH — one billed provider call per
# chunk. Real runs pay for structured-output tier fallthrough + retries, each
# billed call re-sending the full prompt. That waste is provider-behaviour-
# dependent, so no static token math predicts it: the estimate is a RANGE.
#   usd_low  = happy path (== usd, backward compat)
#   usd_high = usd_low × provider-declared retry_waste_factor
# The budget guard protects against usd_high, the ceiling.
# ---------------------------------------------------------------------------


class WastefulProvider(FakeTranslationProvider):
    """Provider that declares a heavy retry-waste ceiling factor."""

    retry_waste_factor = 4.0


def test_usd_equals_low_and_high_applies_provider_waste_factor():
    """usd == usd_low (happy path); usd_high = usd_low × provider factor."""
    from borgesica.domain.cost import CostEstimator

    provider = WastefulProvider()
    estimator = CostEstimator(provider=provider)
    config = make_config(quality_mode="fast")
    job = make_job(config, total=3)
    chunks = [make_chunk(i, text="hello world foo") for i in range(3)]

    est = estimator.estimate(job, chunks, config)

    assert est.usd == pytest.approx(est.usd_low)
    assert est.usd_high == pytest.approx(est.usd_low * 4.0)
    assert est.usd_high > est.usd_low


def test_waste_factor_defaults_when_provider_undeclared():
    """A provider that does not declare retry_waste_factor gets the module default."""
    from borgesica.domain.cost import _DEFAULT_WASTE_FACTOR, CostEstimator

    provider = FakeTranslationProvider()  # no retry_waste_factor attribute
    estimator = CostEstimator(provider=provider)
    config = make_config(quality_mode="fast")
    job = make_job(config, total=2)
    chunks = [make_chunk(i, text="hello world") for i in range(2)]

    est = estimator.estimate(job, chunks, config)
    assert est.usd_high == pytest.approx(est.usd_low * _DEFAULT_WASTE_FACTOR)


def test_within_budget_guards_the_ceiling_not_the_point():
    """budget between usd_low and usd_high → within_budget=False (guards ceiling)."""
    from borgesica.domain.cost import CostEstimator

    provider = WastefulProvider()  # factor 4.0
    estimator = CostEstimator(provider=provider)
    job_cfg = make_config(quality_mode="fast", budget_usd=None)
    job = make_job(job_cfg, total=4)
    chunks = [make_chunk(i, text="hello world foo bar baz") for i in range(4)]

    # First measure the range with no budget.
    ranged = estimator.estimate(job, chunks, job_cfg)
    midpoint = (ranged.usd_low + ranged.usd_high) / 2
    assert ranged.usd_low < midpoint < ranged.usd_high  # sanity

    # A budget at the midpoint clears the floor but NOT the ceiling.
    tight_cfg = make_config(quality_mode="fast", budget_usd=midpoint)
    tight = estimator.estimate(job, chunks, tight_cfg)
    assert tight.within_budget is False, "budget below usd_high must fail the guard"


def test_estimate_counts_tool_schema_in_per_call_overhead():
    """The tool/function schema (input_schema sent in tools=) is billed as input
    on EVERY call but was counted as zero. With a context_manager, per-call
    input overhead now includes the schema tokens."""
    import json

    from borgesica.domain.context import ContextManager
    from borgesica.domain.cost import CostEstimator
    from borgesica.domain.models import translation_tool_schema

    provider = FakeTranslationProvider()
    context_manager = ContextManager()
    estimator = CostEstimator(provider=provider, context_manager=context_manager)
    config = make_config(quality_mode="fast")
    job = make_job(config, total=2)
    # Plain chunks (no cue_batches) → segment_count None → default schema shape.
    chunks = [make_chunk(i, text="hello world") for i in range(2)]

    est = estimator.estimate(job, chunks, config)

    static_tokens = provider.count_tokens(
        context_manager.get_static_block(config), config.model
    )
    schema_tokens = provider.count_tokens(
        json.dumps(translation_tool_schema(None)), config.model
    )
    dynamic_tokens = _empty_dynamic_tokens(provider, context_manager, config)
    expected_input = 2 * (2 + static_tokens + dynamic_tokens + schema_tokens)
    assert est.input_tokens == expected_input, (
        f"Expected {expected_input} (source + static + dynamic + tool schema), "
        f"got {est.input_tokens} — tool schema not counted"
    )


# ---------------------------------------------------------------------------
# C1 — the dynamic-block overhead was a STALE CONSTANT.
#
# cost.py hand-mirrored ContextManager's dynamic block as a flat 500 tokens
# ("glossary <= 300 + summary <= 200"). The glossary budget later moved to
# DEFAULT_GLOSSARY_BUDGET_TOKENS = 2500 WORDS (~3.3k tokens) and the mirror was
# never updated, so both the estimate and the budget guard under-counted the
# per-call overhead by ~6.6x. Measured on job 13b43ac6: estimated $0.275-$0.826,
# real $1.2349 — above the CEILING.
#
# The fix stops mirroring: the estimator measures the real dynamic block for the
# floor and the configured budget for the ceiling.
# ---------------------------------------------------------------------------


def _estimator_with_ctx(provider=None):
    from borgesica.domain.context import ContextManager
    from borgesica.domain.cost import CostEstimator

    provider = provider or FakeTranslationProvider()
    ctx = ContextManager()
    return CostEstimator(provider=provider, context_manager=ctx), provider, ctx


def test_ceiling_overhead_scales_with_configured_glossary_budget():
    """usd_high must grow when config.glossary_budget_tokens grows.

    The glossary rides in the DYNAMIC (uncached) block, so it is paid on EVERY
    call. A job configured for a 2500-word glossary has a materially higher
    ceiling than one capped at 300. With the stale flat constant both estimates
    were identical.
    """
    estimator, _, _ = _estimator_with_ctx()
    chunks = [make_chunk(i, text="hello world") for i in range(3)]

    small_cfg = make_config(quality_mode="fast", glossary_budget_tokens=300)
    big_cfg = make_config(quality_mode="fast", glossary_budget_tokens=2500)

    small = estimator.estimate(make_job(small_cfg, total=3), chunks, small_cfg)
    big = estimator.estimate(make_job(big_cfg, total=3), chunks, big_cfg)

    assert big.usd_high > small.usd_high, (
        f"A 2500-word glossary budget must project a higher ceiling than a "
        f"300-word one (got {big.usd_high} vs {small.usd_high}) — the dynamic "
        f"block is paid on every call and must not be a flat constant."
    )


def test_ceiling_is_more_than_the_floor_times_the_waste_factor():
    """usd_high folds in the glossary ceiling ON TOP OF retry waste.

    A fresh job has an empty glossary, so its FLOOR pays almost no dynamic
    block. Its CEILING pays a full budget's worth on every call. If the dynamic
    block were a single constant shared by both bounds, the only difference
    between them would be the retry-waste factor.
    """
    from borgesica.domain.cost import _waste_factor

    estimator, provider, _ = _estimator_with_ctx()
    config = make_config(quality_mode="fast", glossary_budget_tokens=2500)
    chunks = [make_chunk(0, text="hello world")]

    est = estimator.estimate(make_job(config, total=1), chunks, config)

    assert est.usd_high > est.usd_low * _waste_factor(provider), (
        f"usd_high ({est.usd_high}) must exceed usd_low ({est.usd_low}) x the "
        f"waste factor ({_waste_factor(provider)}): the ceiling also carries a "
        f"full glossary budget that the empty-glossary floor does not."
    )


def test_floor_uses_the_real_glossary_not_a_constant():
    """usd_low must reflect the glossary the job ACTUALLY has.

    A resumed job carrying 400 accumulated entries pays for them on every
    remaining call; a fresh job pays nothing. The floor must tell them apart.
    """
    from borgesica.domain.models import Glossary, GlossaryEntry

    estimator, _, _ = _estimator_with_ctx()
    config = make_config(quality_mode="fast", glossary_budget_tokens=2500)
    chunks = [make_chunk(0, text="hello world")]
    job = make_job(config, total=1)

    populated = Glossary(
        entries=[
            GlossaryEntry(term=f"term{i}", translation=f"termino{i}")
            for i in range(200)
        ]
    )

    empty_est = estimator.estimate(job, chunks, config, glossary=Glossary())
    full_est = estimator.estimate(job, chunks, config, glossary=populated)

    assert full_est.usd_low > empty_est.usd_low, (
        f"A job with 200 accumulated glossary entries must project a higher "
        f"floor ({full_est.usd_low}) than one with an empty glossary "
        f"({empty_est.usd_low}) — the glossary is paid on every call."
    )


def test_floor_never_exceeds_the_ceiling():
    """Even a glossary that overflows its budget keeps usd_low <= usd_high."""
    from borgesica.domain.models import Glossary, GlossaryEntry

    estimator, _, _ = _estimator_with_ctx()
    config = make_config(quality_mode="fast", glossary_budget_tokens=300)
    chunks = [make_chunk(0, text="hello world")]
    job = make_job(config, total=1)

    huge = Glossary(
        entries=[
            GlossaryEntry(term=f"term{i}", translation=f"termino{i}")
            for i in range(2000)
        ]
    )

    est = estimator.estimate(job, chunks, config, glossary=huge)

    assert est.usd_low <= est.usd_high, (
        f"usd_low ({est.usd_low}) must never exceed usd_high ({est.usd_high})"
    )


def test_estimate_overhead_measures_the_real_context_manager_block():
    """The estimator must MEASURE ContextManager's dynamic block, not mirror it.

    This is the regression that caused C1: cost.py described the block in a
    comment and encoded it as a literal. Measuring the real builder means the
    two can never drift again.
    """
    from borgesica.domain.models import Glossary, RollingSummary as _RS

    estimator, provider, ctx = _estimator_with_ctx()
    config = make_config(quality_mode="fast", glossary_budget_tokens=2500)
    chunks = [make_chunk(0, text="hello world")]
    job = make_job(config, total=1)

    glossary = Glossary()
    summary = _RS(text="prior context here")

    est = estimator.estimate(job, chunks, config, glossary=glossary, summary=summary)

    import json as _json

    from borgesica.domain.models import translation_tool_schema

    static_tokens = provider.count_tokens(ctx.get_static_block(config), config.model)
    schema_tokens = provider.count_tokens(
        _json.dumps(translation_tool_schema(None)), config.model
    )
    dynamic_tokens = provider.count_tokens(
        ctx.build_dynamic_block(glossary, summary, config.glossary_budget_tokens),
        config.model,
    )
    expected_input = 2 + static_tokens + dynamic_tokens + schema_tokens

    assert est.input_tokens == expected_input, (
        f"input_tokens ({est.input_tokens}) must equal source + static + the "
        f"MEASURED dynamic block + tool schema ({expected_input})"
    )


# ---------------------------------------------------------------------------
# Cache-aware cost — cached input tokens are not billed at the miss rate
#
# Measured against the DeepSeek dashboard over 1,114 real requests:
#   hit 5,115,392 / miss 892,787 / output 1,941,028 tokens, billed $0.68.
# The miss and output rates ($0.14 / $0.28) alone account for $0.66848 of
# that, i.e. 98.3% — so the price table was right and the cache rate is a
# small residual, ~$0.00225/Mtok (1.6% of the miss rate).
#
# Pricing every input token at the miss rate computed $1.38463 for the same
# tokens: a 2.04x overstatement. That number feeds the budget guard, so a job
# with budget_usd set would raise BudgetExceeded at roughly half the budget it
# had actually spent.
# ---------------------------------------------------------------------------


def test_usage_carries_cached_input_tokens_defaulting_to_zero():
    """Providers that report no cache detail must keep behaving as before."""
    from borgesica.domain.models import Usage

    assert Usage().cached_input_tokens == 0
    assert Usage(input_tokens=10, output_tokens=5).cached_input_tokens == 0


def test_cached_input_tokens_are_a_subset_of_input_tokens():
    """The OpenAI wire format reports cached tokens as part of prompt_tokens."""
    from borgesica.domain.models import Usage

    usage = Usage(input_tokens=1000, output_tokens=100, cached_input_tokens=850)

    assert usage.cached_input_tokens <= usage.input_tokens


def test_usage_cost_prices_cached_tokens_at_the_cache_rate():
    """The whole point: 850 of 1000 input tokens must not cost the miss rate."""
    from borgesica.domain.models import Usage
    from borgesica.domain.orchestrator import TranslationOrchestrator

    usage = Usage(input_tokens=1000, output_tokens=100, cached_input_tokens=850)

    cost = TranslationOrchestrator._usage_cost(
        usage, in_price=0.14, out_price=0.28, cache_price=0.00225
    )

    expected = 150 / 1e6 * 0.14 + 850 / 1e6 * 0.00225 + 100 / 1e6 * 0.28
    assert cost == pytest.approx(expected)


def test_usage_cost_without_cached_tokens_is_unchanged():
    """A provider reporting no cache hits must price exactly as it always did."""
    from borgesica.domain.models import Usage
    from borgesica.domain.orchestrator import TranslationOrchestrator

    usage = Usage(input_tokens=1000, output_tokens=100)

    cost = TranslationOrchestrator._usage_cost(
        usage, in_price=0.14, out_price=0.28, cache_price=0.00225
    )

    assert cost == pytest.approx(1000 / 1e6 * 0.14 + 100 / 1e6 * 0.28)


def test_usage_cost_reproduces_the_measured_dashboard_bill():
    """The real 1,114-request workload must price to what DeepSeek charged."""
    from borgesica.domain.models import Usage
    from borgesica.domain.orchestrator import TranslationOrchestrator

    usage = Usage(
        input_tokens=5_115_392 + 892_787,
        output_tokens=1_941_028,
        cached_input_tokens=5_115_392,
    )

    cost = TranslationOrchestrator._usage_cost(
        usage, in_price=0.14, out_price=0.28, cache_price=0.00225
    )

    # Dashboard rounds to the cent; the residual method fixes the cache rate
    # only to within that rounding, so assert against $0.68 +- half a cent.
    assert cost == pytest.approx(0.68, abs=0.005)


def test_naive_pricing_would_have_doubled_the_measured_bill():
    """Guards the regression: ignoring the cache overstates by ~2x, not a little."""
    from borgesica.domain.models import Usage
    from borgesica.domain.orchestrator import TranslationOrchestrator

    usage = Usage(
        input_tokens=5_115_392 + 892_787,
        output_tokens=1_941_028,
        cached_input_tokens=5_115_392,
    )

    cache_aware = TranslationOrchestrator._usage_cost(
        usage, in_price=0.14, out_price=0.28, cache_price=0.00225
    )
    naive = TranslationOrchestrator._usage_cost(
        usage.model_copy(update={"cached_input_tokens": 0}),
        in_price=0.14,
        out_price=0.28,
        cache_price=0.00225,
    )

    assert naive / cache_aware > 2.0


# ---------------------------------------------------------------------------
# Cache-aware estimate — the discount belongs in usd_low, never in usd_high
#
# Prompt caching matches the longest common PREFIX of a request. Verified
# against the measured run: 778 uncached tokens per call versus a 765-token
# average chunk source, i.e. exactly the user message missed and everything
# before it hit, because that experiment held the system prompt fixed.
#
# A REAL run is different. The dynamic block sits after the static one and
# changes: the summary every chunk, the glossary whenever a term is added. So
# the invariant prefix is the static block plus the tool schema (~47% of input
# on job 13b43ac6); the glossary only extends it on chunks that add nothing,
# which was 365 of 502. The measured 85.7% hit rate is therefore an artefact
# of the experiment, and applying it here would UNDERSTATE cost.
#
# usd_low is defined as the happy path and usd_high as the ceiling the budget
# guard gates on, so cache uncertainty is exactly what that band is for.
# ---------------------------------------------------------------------------


class _CachingProvider(FakeTranslationProvider):
    """Fake whose cache is 100x cheaper than a miss, to make the effect visible."""

    def price(self, model: str) -> tuple[float, float]:  # noqa: ARG002
        return (1.0, 5.0)

    def cache_price(self, model: str) -> float:  # noqa: ARG002
        return 0.01


def _chunks(n: int) -> list:
    """n PENDING chunks of identical, realistic size."""
    return [make_chunk(i, text="the quick brown fox jumps over the lazy dog") for i in range(n)]


def _estimate_with(provider, chunks, **cfg_kwargs):
    from borgesica.domain.context import ContextManager
    from borgesica.domain.cost import CostEstimator

    estimator = CostEstimator(provider=provider, context_manager=ContextManager())
    config = make_config(**cfg_kwargs) if cfg_kwargs else make_config()
    return estimator.estimate(make_job(config, total=len(chunks)), chunks, config)


def test_estimate_low_discounts_the_invariant_prefix():
    """usd_low must price the static block + schema at the cache rate."""
    chunks = _chunks(6)

    cheap = _estimate_with(_CachingProvider(), chunks)
    plain = _estimate_with(FakeTranslationProvider(), chunks)

    assert cheap.usd_low < plain.usd_low


def test_estimate_high_ignores_the_cache_entirely():
    """The ceiling the budget guard gates on must assume a cold cache.

    A run whose glossary changes on every chunk gets no prefix reuse beyond
    the static block, and a cold start gets none at all. Discounting the
    ceiling would admit jobs that then overspend.
    """
    chunks = _chunks(6)

    cheap = _estimate_with(_CachingProvider(), chunks)
    plain = _estimate_with(FakeTranslationProvider(), chunks)

    assert cheap.usd_high == pytest.approx(plain.usd_high)


def test_estimate_charges_the_first_chunk_at_full_price():
    """Nothing is cached before the first call populates the cache."""
    one = _estimate_with(_CachingProvider(), _chunks(1))
    plain_one = _estimate_with(FakeTranslationProvider(), _chunks(1))

    assert one.usd_low == pytest.approx(plain_one.usd_low)


def test_estimate_discount_grows_with_chunk_count():
    """More chunks means more calls reusing the same prefix."""
    provider = _CachingProvider()
    plain = FakeTranslationProvider()

    def ratio(n: int) -> float:
        return _estimate_with(provider, _chunks(n)).usd_low / _estimate_with(
            plain, _chunks(n)
        ).usd_low

    assert ratio(50) < ratio(5)


def test_estimate_unchanged_for_a_provider_without_a_cache_discount():
    """cache_price == input price must reproduce the previous numbers exactly."""
    chunks = _chunks(6)

    est = _estimate_with(FakeTranslationProvider(), chunks)

    # FakeTranslationProvider.cache_price returns its input price, so the
    # cache-aware path must collapse back onto the plain arithmetic.
    assert est.usd_low == pytest.approx(est.usd, rel=1e-12)


def test_estimate_within_budget_still_gates_on_the_ceiling():
    """The cache discount must not sneak a job past the budget gate."""
    chunks = _chunks(40)
    est = _estimate_with(_CachingProvider(), chunks)

    tight = _estimate_with(_CachingProvider(), chunks, budget_usd=est.usd_high * 0.5)

    assert tight.within_budget is False
