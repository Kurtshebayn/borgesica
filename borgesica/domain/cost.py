"""CostEstimator — token math to CostEstimate.

Dependency rule: only stdlib + pydantic + domain models/ports.
No I/O, no adapter imports.

Design (M1-6):
  CostEstimator.estimate(job, chunks, config, output_tokens_per_chunk?) -> CostEstimate
  - Counts PENDING chunks only (skips DONE).
  - Multiplies by 3 if quality_mode="reflective" (translate + critique + revise).
  - Computes USD from provider.price(model).
  - Sets within_budget per config.budget_usd.
  - Sets cached=True when the static block clears Anthropic's 1024-token
    prompt-cache minimum (requires a context_manager; False without one).

NOTE: Prompt cache-write cost is NOT included in the estimate.
Cache writes are one-time amortized costs; omitting them keeps estimates
conservative and predictable for the user. The cached field on CostEstimate
reports whether the static block QUALIFIES for caching; no adapter applies
caching today, so it is informational only — `estimate` surfaces it to the
user and the HTTP schema carries it.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from borgesica.domain.models import (
    Chunk,
    ChunkStatus,
    CostEstimate,
    Glossary,
    Job,
    JobConfig,
    RollingSummary,
    translation_tool_schema,
)
from borgesica.domain.ports import TranslationProvider

if TYPE_CHECKING:
    from borgesica.domain.context import ContextManager

# Rolling-summary allowance, in WORDS, used only for the CEILING bound. The
# summary a future chunk will carry cannot be known before the run, and unlike
# the glossary it has no configured cap, so the ceiling assumes a summary of
# this size on every call. The FLOOR uses the job's actual summary.
_SUMMARY_BUDGET_WORDS = 200

# Filler token used to price a WORD budget. Glossary budgets are expressed in
# words (Glossary.render counts len(line.split())) but cost math needs tokens,
# so a budget of N words is priced by asking the provider to count N words.
_BUDGET_FILLER_WORD = "palabra"

# Output tokens per token of SOURCE. The old model was `chunk_input + envelope`,
# which assumed PARITY: that the Spanish translation costs the same tokens as
# the English it renders. It does not, for two independent reasons that
# multiply:
#
#   Spanish prose runs 1.710 tok/word against English's 1.384  → 1.24x
#   the JSON envelope tokenizes at 3.295 chars/token, not the 3.853
#   `count_tokens` assumes for prose                            → 1.17x
#
# and count_tokens(source) is an English-prose count, so the first factor
# applies against a ~1.01 word ratio: 1.06 x 1.17 = 1.24.
#
# MEASURED 2026-08-11, deepseek-v4-flash, EPUB en->es: 20 instrumented calls,
# one per chunk, sampled across a 502-chunk book with the real accumulated
# glossary and each chunk's real inbound summary, pairing
# usage.completion_tokens with the exact bytes returned:
#
#   translation tokens / count_tokens(source)   1.254 median, 1.475 p90
#
# The HIGH figure is the p90, not the max. One call in twenty emitted 4,799
# tokens for 4,692 chars (0.98 chars/token — a degenerate repetition, not a
# retry); that tail is what _waste_factor and the band absorb, and calibrating
# the ceiling on it would price every job for the worst chunk in the book.
_OUTPUT_EXPANSION = 1.25
_OUTPUT_EXPANSION_HIGH = 1.48

# Output tokens BEYOND the translation itself: the provider returns the full
# JSON envelope — summary_update (3-5 sentences) + glossary_additions + keys.
#
# This was 150, which contradicted _SUMMARY_BUDGET_WORDS in this same file: the
# SAME rolling summary was priced at 200 WORDS on the input side but expected
# to fit in 150 TOKENS on the output side, alongside glossary_additions and
# every JSON key. At the calibrated Spanish rate those 200 words are ~342
# tokens on their own. Measured on the 20 calls above: 260 median, 271 mean,
# 351 p90 (summary ~215, JSON scaffolding ~45).
_OUTPUT_ENVELOPE_TOKENS = 260

# The non-summary part of the envelope: glossary_additions plus the JSON keys,
# braces, and escaping. Split out from _OUTPUT_ENVELOPE_TOKENS so the CEILING
# can price the summary through _word_budget_tokens at the same
# _SUMMARY_BUDGET_WORDS the input block already uses, instead of restating that
# budget as a second invented token constant — the exact mistake that produced
# the 150-vs-200 contradiction above.
_OUTPUT_JSON_SCAFFOLDING_TOKENS = 45

# reflective mode runs 3 passes: translate (draft) + critique + revise
_REFLECTIVE_PASSES = 3
_FAST_PASSES = 1

# Anthropic will not cache a prompt prefix below this many tokens. Only the
# static block is a caching candidate (it is identical for every chunk of a
# job); the glossary and rolling summary change per chunk. Used solely to fill
# the informational CostEstimate.cached — no adapter applies prompt caching, so
# nothing behavioural depends on this number.
_ANTHROPIC_CACHE_MIN_TOKENS = 1024

# Retry-waste ceiling: the point estimate models the HAPPY PATH (1 billed call
# per chunk). Real runs pay for structured-output tier fallthrough + malformed
# retries — each billed call re-sends the full prompt. That waste is provider-
# behaviour-dependent (a reliable tool-caller rarely falls through; an endpoint
# that 400s tool-calls and JSON mode pays 2-3 billed calls per chunk), so a
# provider DECLARES its own ceiling factor via `retry_waste_factor`. Providers
# that don't declare one (fakes, minimal adapters) fall back to this default.
_DEFAULT_WASTE_FACTOR = 1.5


def _waste_factor(provider: object) -> float:
    """Return the provider-declared retry-waste ceiling factor, or the default.

    A plain attribute lookup (not a Protocol method) keeps every existing
    provider and test double working unchanged: only providers that opt in by
    declaring `retry_waste_factor` change the ceiling.
    """
    return float(getattr(provider, "retry_waste_factor", _DEFAULT_WASTE_FACTOR))


def _word_budget_tokens(
    provider: TranslationProvider,  # type: ignore[type-arg]
    words: int,
    model: str,
) -> int:
    """Price a WORD budget in tokens using the provider's own counter.

    Glossary budgets are word counts, not token counts (see Glossary.render).
    Routing the conversion through `count_tokens` instead of hardcoding a ratio
    means a provider that ships a real tokenizer corrects this for free.

    NOTE: every adapter currently approximates count_tokens as words x 1.3, so
    this conversion inherits that approximation. Calibrating it against real
    `usage.input_tokens` is tracked separately (C1, cause 2); it is deliberately
    NOT papered over with a second invented constant here.
    """
    if words <= 0:
        return 0
    return provider.count_tokens(" ".join([_BUDGET_FILLER_WORD] * words), model)


def chunk_output_tokens(source_tokens: int) -> int:
    """Projected output tokens for ONE provider call on a chunk this size.

    PUBLIC because two callers need it — CostEstimator's pre-flight floor and
    the orchestrator's runtime budget guard — and they must not each keep their
    own copy. They already drifted once: the guard mirrored
    `source_tokens + _OUTPUT_ENVELOPE_TOKENS` and kept the source-PARITY
    assumption after this module dropped it.

    That drift was hard to see precisely BECAUSE the two shared the envelope
    constant: the 150 -> 260 correction propagated to the guard on its own,
    while the 1.25x expansion did not, leaving a guard that looked synchronised
    and under-projected output by ~16% on prose. Sharing a constant is not
    sharing a model. Callers that need the size of a chunk's output must call
    this — never restate its shape.
    """
    return round(source_tokens * _OUTPUT_EXPANSION) + _OUTPUT_ENVELOPE_TOKENS


def _tool_schema_tokens(
    provider: TranslationProvider,  # type: ignore[type-arg]
    chunk: Chunk,
    model: str,
) -> int:
    """Tokens for the tool/function schema sent in `tools=` on EVERY call.

    Billed as input by every provider but historically counted as zero.
    SRT cue-batch chunks request the SEGMENTED schema (translations array);
    prose chunks use the default single-string shape.
    """
    cue_batches = chunk.meta.get("cue_batches")
    segment_count = len(cue_batches) if cue_batches else None
    return provider.count_tokens(json.dumps(translation_tool_schema(segment_count)), model)


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
        glossary: Glossary | None = None,
        summary: RollingSummary | None = None,
    ) -> CostEstimate:
        """Compute a CostEstimate covering only PENDING chunks.

        The two bounds differ in more than retry waste. The glossary rides in
        the DYNAMIC (uncached) prompt block and is paid on EVERY call, but it
        GROWS during a run — a fresh job starts empty and a real 502-chunk book
        finished with 491 entries. No pre-flight measurement can predict that
        growth, so:

          usd_low  — the dynamic block the job has RIGHT NOW (empty on a fresh
                     job, the accumulated glossary on a resumed one).
          usd_high — a full `config.glossary_budget_tokens` glossary plus a
                     summary on every call, the p90 output expansion, and a
                     full-budget summary EMITTED, times the retry-waste factor.

        The band is therefore genuinely wide on a fresh job. That is honest:
        the floor is what the first chunk costs, the ceiling is what the last
        one costs.

        Args:
            job: The Job (used for model and budget info via config).
            chunks: All chunks for the job (DONE chunks are skipped).
            config: JobConfig (model, quality_mode, budget_usd).
            output_tokens_per_chunk: Override for estimated output token count
                per provider call. Default (None) models the real JSON
                envelope: the translation EXPANDED against its source
                (_OUTPUT_EXPANSION — Spanish costs more tokens than the English
                it renders) plus _OUTPUT_ENVELOPE_TOKENS. An explicit value
                pins both bounds, for deterministic arithmetic in tests.
            glossary: The job's current glossary, used for the FLOOR bound.
                None is treated as empty (a job that has not run yet).
            summary: The job's current rolling summary, used for the FLOOR
                bound. None is treated as empty.

        Returns:
            CostEstimate with input_tokens, output_tokens, usd, model,
            cached (whether the static block qualifies for prompt caching —
            informational; no adapter applies it), and within_budget.
            input_tokens reports the FLOOR, matching `usd` (= usd_low).

        NOTE: Prompt cache-write cost is NOT included. See module docstring.
        """
        # Determine whether the static instruction block qualifies for prompt caching.
        # This is independent of whether there are pending chunks.
        cached: bool = False
        # 0 without a context manager: the static block is unknown, so nothing
        # is known to be cacheable either (same backward-compat contract as
        # `cached` and the per-call overhead).
        token_count = 0
        if self._context_manager is not None:
            static_block = self._context_manager.get_static_block(config)
            token_count = self._provider.count_tokens(static_block, config.model)
            cached = token_count >= _ANTHROPIC_CACHE_MIN_TOKENS

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
        #
        # The dynamic block is MEASURED, never mirrored: cost.py once restated
        # its shape as a literal 500 tokens, the glossary budget moved to 2500
        # words, and every estimate under-counted by ~6.6x (job 13b43ac6:
        # estimated $0.275-$0.826, real $1.2349).
        overhead_low = 0
        overhead_high = 0
        if self._context_manager is not None:
            dynamic_block = self._context_manager.build_dynamic_block(
                glossary if glossary is not None else Glossary(),
                summary if summary is not None else RollingSummary(),
                config.glossary_budget_tokens,
            )
            dynamic_low = self._provider.count_tokens(dynamic_block, config.model)
            # Ceiling: the same block structure, but with the glossary and the
            # summary each filled to their budget. Measuring the empty block
            # keeps the [GLOSSARY]/[SUMMARY] scaffolding counted exactly once.
            scaffolding = self._provider.count_tokens(
                self._context_manager.build_dynamic_block(
                    Glossary(), RollingSummary(), config.glossary_budget_tokens
                ),
                config.model,
            )
            dynamic_high = (
                scaffolding
                + _word_budget_tokens(
                    self._provider, config.glossary_budget_tokens, config.model
                )
                + _word_budget_tokens(
                    self._provider, _SUMMARY_BUDGET_WORDS, config.model
                )
            )
            # A glossary already over budget must not push the floor past the
            # ceiling (render() always emits locked entries, budget or not).
            dynamic_low = min(dynamic_low, dynamic_high)
            overhead_low = token_count + dynamic_low
            overhead_high = token_count + dynamic_high

        total_input = 0
        total_input_high = 0
        total_output = 0
        total_output_high = 0
        # Ceiling allowance for the summary the model EMITS, priced through the
        # same helper and the same word budget the input side uses for the
        # summary it RECEIVES. One budget, two sides, one conversion.
        output_envelope_high = (
            _word_budget_tokens(self._provider, _SUMMARY_BUDGET_WORDS, config.model)
            + _OUTPUT_JSON_SCAFFOLDING_TOKENS
        )
        # Input tokens the provider can serve from its prompt cache on the
        # happy path. Caching matches the longest common PREFIX of a request,
        # and only two parts of a call are invariant across a whole job: the
        # static block and the tool schema. The dynamic block sits after them
        # and changes — the summary every chunk, the glossary whenever a term
        # is added — so it truncates the shared prefix and is never counted
        # here.
        #
        # Measured on job 13b43ac6: static ~1,124 tok and schema ~1,400 tok
        # against ~5,400 tok of input per call, so this is roughly 47%. A
        # fixed-prompt experiment showed 85.7% cached, but that held the
        # system prompt constant across every call and is NOT what a real run
        # looks like; using that figure here would UNDERSTATE cost.
        cacheable_input = 0

        for chunk in pending:
            chunk_input = self._provider.count_tokens(chunk.source_text, config.model)
            if output_tokens_per_chunk is not None:
                chunk_output = chunk_output_high = output_tokens_per_chunk
            else:
                chunk_output = chunk_output_tokens(chunk_input)
                chunk_output_high = (
                    round(chunk_input * _OUTPUT_EXPANSION_HIGH) + output_envelope_high
                )
                # The ceiling envelope is a WORD budget converted by the
                # provider's own counter, while the floor is a measured TOKEN
                # constant; a provider whose counter reads low enough can price
                # the 200-word budget under the measured floor and invert the
                # band. Same guard, same reason, as dynamic_low above.
                chunk_output_high = max(chunk_output_high, chunk_output)
            # The tool schema is per-call input overhead like the system prompt,
            # so it is gated on context_manager presence (bare token-math mode
            # counts neither — same backward-compat contract as `cached`).
            schema_tokens = (
                _tool_schema_tokens(self._provider, chunk, config.model)
                if self._context_manager is not None
                else 0
            )
            total_input += (chunk_input + overhead_low + schema_tokens) * passes
            total_input_high += (chunk_input + overhead_high + schema_tokens) * passes
            total_output += chunk_output * passes
            total_output_high += chunk_output_high * passes
            # One cached prefix per chunk after the first: the first call of
            # the job populates the cache, and in reflective mode the critique
            # and revise passes carry their own system prompts, so they share
            # no prefix with the translate pass. Counting a single pass is the
            # conservative reading — it can only leave the estimate high.
            if chunk is not pending[0]:
                cacheable_input += token_count + schema_tokens

        in_usd_per_mtok, out_usd_per_mtok = self._provider.price(config.model)
        cache_usd_per_mtok = self._provider.cache_price(config.model)
        # usd_low: happy path — one billed call per chunk (× passes), carrying
        # only the dynamic block the job has today, and reusing the invariant
        # prefix from the provider's prompt cache on every chunk after the
        # first.
        cached_input = min(cacheable_input, total_input)
        usd_low = (
            (total_input - cached_input) / 1_000_000 * in_usd_per_mtok
            + cached_input / 1_000_000 * cache_usd_per_mtok
            + total_output / 1_000_000 * out_usd_per_mtok
        )
        # usd_high: ceiling — a full glossary budget on every call, PLUS the
        # provider's retry / tier-fallthrough waste that no static token math
        # can predict.
        #
        # Deliberately assumes a COLD cache: no discount at all. A job whose
        # glossary grows on every chunk reuses nothing beyond the static block,
        # a first run starts cold, and the budget guard gates on this number.
        # Discounting it would admit jobs that then overspend. Cache behaviour
        # is uncertainty, and the low/high band is where uncertainty belongs.
        usd_high = (
            total_input_high / 1_000_000 * in_usd_per_mtok
            + total_output_high / 1_000_000 * out_usd_per_mtok
        ) * _waste_factor(self._provider)

        # The budget guard protects against the CEILING, not the optimistic
        # point. A public user who is promised a number must not blow past it
        # because the model struggled with structured output.
        within_budget: bool
        if config.budget_usd is None:
            within_budget = True
        else:
            within_budget = usd_high <= config.budget_usd

        return CostEstimate(
            input_tokens=total_input,
            output_tokens=total_output,
            output_tokens_high=total_output_high,
            usd=usd_low,
            usd_low=usd_low,
            usd_high=usd_high,
            model=config.model,
            cached=cached,
            within_budget=within_budget,
        )
