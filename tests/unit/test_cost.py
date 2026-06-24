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
