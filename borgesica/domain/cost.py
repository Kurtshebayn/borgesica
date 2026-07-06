"""CostEstimator — token math to CostEstimate.

Dependency rule: only stdlib + pydantic + domain models/ports.
No I/O, no adapter imports.

Design (M1-6):
  CostEstimator.estimate(job, chunks, config, output_tokens_per_chunk?) -> CostEstimate
  - Counts PENDING chunks only (skips DONE).
  - Multiplies by 3 if quality_mode="reflective" (translate + critique + revise).
  - Computes USD from provider.price(model).
  - Sets within_budget per config.budget_usd.
  - cached=False (cache hint lives in SystemPrompt, not in the estimate by default).

NOTE: Prompt cache-write cost is NOT included in the estimate.
Cache writes are one-time amortized costs; omitting them keeps estimates
conservative and predictable for the user. The cached field on CostEstimate
signals whether the static block qualifies for caching — the adapter decides
whether to apply it and what discount to apply at runtime.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from borgesica.domain.models import (
    Chunk,
    ChunkStatus,
    CostEstimate,
    Job,
    JobConfig,
)
from borgesica.domain.ports import TranslationProvider

if TYPE_CHECKING:
    from borgesica.domain.context import ContextManager

# Dynamic system-prompt budget paid on EVERY call: rendered glossary (≤ 300)
# + rolling summary (≤ 200). Mirrors ContextManager._build_dynamic_block.
_DYNAMIC_BLOCK_BUDGET_TOKENS = 500

# Output tokens BEYOND the translation itself: the provider returns the full
# JSON envelope — summary_update (3-5 sentences) + glossary_additions + keys.
_OUTPUT_ENVELOPE_TOKENS = 150

# reflective mode runs 3 passes: translate (draft) + critique + revise
_REFLECTIVE_PASSES = 3
_FAST_PASSES = 1


class CostEstimator:
    """Estimate translation cost for pending chunks.

    Pure domain — no I/O, no adapter imports.  The provider is injected
    only for count_tokens() and price() capabilities.

    Args:
        provider: A TranslationProvider used for count_tokens() and price().
                  No translate() calls are made here.
    """

    def __init__(
        self,
        provider: TranslationProvider,  # type: ignore[type-arg]
        context_manager: "ContextManager | None" = None,
    ) -> None:
        self._provider = provider
        self._context_manager = context_manager

    def estimate(
        self,
        job: Job,
        chunks: list[Chunk],
        config: JobConfig,
        output_tokens_per_chunk: int | None = None,
    ) -> CostEstimate:
        """Compute a CostEstimate covering only PENDING chunks.

        Args:
            job: The Job (used for model and budget info via config).
            chunks: All chunks for the job (DONE chunks are skipped).
            config: JobConfig (model, quality_mode, budget_usd).
            output_tokens_per_chunk: Override for estimated output token count
                per provider call. Default (None) models the real JSON
                envelope: source-sized translation + _OUTPUT_ENVELOPE_TOKENS.
                Pass an explicit value in tests for deterministic arithmetic.

        Returns:
            CostEstimate with input_tokens, output_tokens, usd, model,
            cached (always False — cache hint is in SystemPrompt, not here),
            and within_budget.

        NOTE: Prompt cache-write cost is NOT included. See module docstring.
        """
        # Determine whether the static instruction block qualifies for prompt caching.
        # This is independent of whether there are pending chunks.
        cached: bool = False
        if self._context_manager is not None:
            static_block = self._context_manager.get_static_block(config)
            token_count = self._provider.count_tokens(static_block, config.model)
            cached = token_count >= 1024  # Anthropic prompt-cache minimum

        pending = [c for c in chunks if c.status != ChunkStatus.DONE]

        if not pending:
            return CostEstimate(
                input_tokens=0,
                output_tokens=0,
                usd=0.0,
                model=config.model,
                cached=cached,
                within_budget=True,
            )

        passes = _REFLECTIVE_PASSES if config.quality_mode == "reflective" else _FAST_PASSES

        # Per-call input overhead: the system prompt (static block + glossary
        # + summary) is sent on EVERY provider call. On thin chunks (SRT cue
        # batches ≈ 176 tokens) this overhead DOMINATES the call cost —
        # omitting it under-estimated a full-movie job 16x (job 0b86d4f2).
        # Without a context_manager the static block is unknown; overhead
        # stays 0 (backward compat, same contract as `cached`).
        overhead_tokens = 0
        if self._context_manager is not None:
            overhead_tokens = token_count + _DYNAMIC_BLOCK_BUDGET_TOKENS

        total_input = 0
        total_output = 0

        for chunk in pending:
            chunk_input = self._provider.count_tokens(chunk.source_text, config.model)
            chunk_output = (
                output_tokens_per_chunk
                if output_tokens_per_chunk is not None
                else chunk_input + _OUTPUT_ENVELOPE_TOKENS
            )
            total_input += (chunk_input + overhead_tokens) * passes
            total_output += chunk_output * passes

        in_usd_per_mtok, out_usd_per_mtok = self._provider.price(config.model)
        usd = (
            total_input / 1_000_000 * in_usd_per_mtok
            + total_output / 1_000_000 * out_usd_per_mtok
        )

        within_budget: bool
        if config.budget_usd is None:
            within_budget = True
        else:
            within_budget = usd <= config.budget_usd

        return CostEstimate(
            input_tokens=total_input,
            output_tokens=total_output,
            usd=usd,
            model=config.model,
            cached=cached,
            within_budget=within_budget,
        )
