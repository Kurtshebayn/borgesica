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
from typing import TYPE_CHECKING, NamedTuple

from borgesica.domain.glossary import extraction_prompts, plan_extraction_batches
from borgesica.domain.models import (
    Chunk,
    ChunkStatus,
    CostEstimate,
    Glossary,
    GlossaryExtractionEstimate,
    Job,
    JobConfig,
    SourceType,
    Usage,
    translation_tool_schema,
)
from borgesica.domain.ports import TranslationProvider

if TYPE_CHECKING:
    from borgesica.domain.context import ContextManager

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

# SRT cue batches are NOT prose and must not borrow its numbers. Same
# instrumentation, 2026-08-11, 20 cue-batch chunks of job 0b86d4f2 (25 segments
# each), 21 billed calls:
#
#   translation / count_tokens(source)   1.325 median, 1.508 p90
#   envelope                             310 median, 311 mean, 458 p90
#   output density                       2.826 chars/token (prose: 3.295)
#
# Two effects pull in OPPOSITE directions and the density wins. Spanish
# subtitles are SHORTER in words than their English source — 0.91-0.94 across
# five stored SRT jobs, against 1.009 for prose — because subtitling compresses
# and `line_length` caps the result. But the reply is a `translations` array of
# 25 quoted strings, so brackets, quotes and commas make it denser than one
# prose blob, and the expansion ends up HIGHER than prose (1.32 vs 1.25).
#
# It matters more here than in prose: a cue batch is thin (~185 source tokens),
# so the envelope is over half the projected output instead of a fifth. Pricing
# SRT with the prose model came to 0.885x of measured; these come to 0.998x.
_SRT_OUTPUT_EXPANSION = 1.32
_SRT_OUTPUT_EXPANSION_HIGH = 1.51

# Output tokens BEYOND the translation itself: the JSON envelope —
# glossary_additions plus the keys, braces and escaping.
#
# Measured on the 20 calls above: the envelope was 260 median, 271 mean,
# 351 p90, of which the rolling summary_update was ~215 and the JSON
# scaffolding ~45. The summary is no longer requested (removed 2026-10-23,
# a day after it stopped being read back), so the envelope is the scaffolding
# that was measured alongside it. It prices BOTH bounds: no uncalibrated
# headroom is invented for the ceiling, whose band now comes from the p90
# expansion and the retry-waste factor. Re-measure it on the next
# instrumented run (see cost instrumentation in project memory).
_OUTPUT_ENVELOPE_TOKENS = 45

# SRT counterpart, measured on the same 20 cue-batch calls (310 median with
# the summary, 89 without it). Double prose's because a `translations` array
# of 25 strings pays brackets, quotes and commas 25 times over, where prose
# pays them once around a single blob.
_SRT_OUTPUT_ENVELOPE_TOKENS = 89


class _OutputModel(NamedTuple):
    """How a chunk's source size maps to the tokens the model emits back.

    Selected by SourceType because a cue-batch reply and a prose reply are
    different shapes, not the same shape at different scales — see the
    constants above for the measurement that separates them.
    """

    expansion: float
    expansion_high: float
    envelope: int


_PROSE_OUTPUT_MODEL = _OutputModel(
    expansion=_OUTPUT_EXPANSION,
    expansion_high=_OUTPUT_EXPANSION_HIGH,
    envelope=_OUTPUT_ENVELOPE_TOKENS,
)
_SRT_OUTPUT_MODEL = _OutputModel(
    expansion=_SRT_OUTPUT_EXPANSION,
    expansion_high=_SRT_OUTPUT_EXPANSION_HIGH,
    envelope=_SRT_OUTPUT_ENVELOPE_TOKENS,
)


def output_model(source_type: SourceType) -> _OutputModel:
    """Return the measured output model for this source type.

    PDF rides with EPUB on the prose model: both send prose blobs through the
    single-string schema, which is what was measured. Only SRT requests the
    segmented `translations` array, and only SRT was measured separately.
    """
    return _SRT_OUTPUT_MODEL if source_type == SourceType.SRT else _PROSE_OUTPUT_MODEL


def usage_cost(
    usage: Usage, in_price: float, out_price: float, cache_price: float
) -> float:
    """Compute the USD cost of one provider call from its REAL Usage.

    The one formula for money actually spent — the translation run's budget
    guard and the glossary extraction pass both accrue with it, so a dollar
    counted by one is the same dollar counted by the other.

    ``cached_input_tokens`` is the subset of ``input_tokens`` the provider
    served from its prompt cache, and it is billed at ``cache_price``, not at
    ``in_price``. Pricing all input at the miss rate overstated a real
    DeepSeek bill 2.04x, and because this figure is what the budget guard
    accrues, a job with ``budget_usd`` set would raise BudgetExceeded at
    roughly half the budget it had actually spent.
    """
    cached = min(usage.cached_input_tokens, usage.input_tokens)
    missed = usage.input_tokens - cached
    return (
        missed / 1_000_000 * in_price
        + cached / 1_000_000 * cache_price
        + usage.output_tokens / 1_000_000 * out_price
    )


# Output tokens per SOURCE token of one glossary-extraction call. Measured on
# job 9be143da split into 10-chunk batches: each batch's glossary entries,
# serialized as the extractor's JSON and tokenized at 3.853 chars/token, came
# to an output/input ratio of mean 0.326, median 0.308, p90 0.404, max 0.997
# across batches, where "input" is the batch's source tokens without prompt
# overhead. The expected figure uses the mean; the ceiling uses the worst
# batch, applied to the whole book. Measured on the glossary entries the
# TRANSLATION run produced (no extraction output existed yet), so both are to
# be recalibrated against real extraction output.
_EXTRACTION_OUTPUT_RATIO = 0.326
_EXTRACTION_OUTPUT_RATIO_HIGH = 0.997

# reflective mode runs 3 passes: translate (draft) + critique + revise
_REFLECTIVE_PASSES = 3
_FAST_PASSES = 1

# Anthropic will not cache a prompt prefix below this many tokens. Only the
# static block is a caching candidate (it is identical for every chunk of a
# job); the narrator and glossary change per chunk. Used solely to fill
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

    NOTE: this conversion inherits whatever `count_tokens` approximates, and
    that approximation is now MEASURED rather than guessed at. (It is no longer
    "words x 1.3" — 7e1bec8 moved every OpenAI-compatible adapter to
    characters / 3.853; this note said otherwise for several commits.)

    The residual is a real UNDER-count, and it is worst exactly here. Measured
    2026-08-11 against `usage.prompt_tokens` on 20 instrumented prose calls:

      whole prompt, count_tokens vs real   -15.9%
      of which, the rendered glossary      2020 counted vs ~2757 real (-27%)
      chat-template overhead (unseeable)   ~5% on every call, both source types

    A glossary is a dense arrow table (~2.8 chars/token) being priced at prose
    density (3.853), so a 2500-WORD budget prices at 5191 tokens against ~6460
    real — 24% low, on every call. On a 502-chunk book that is ~11% of usd_high.

    It is still NOT papered over with a second invented constant here: the fix
    belongs in the counter, which is the provider's job, not in a domain-side
    correction that every future provider would inherit whether or not its
    tokenizer needs it. Until then the gap lives in the band, and the band is
    wide enough to hold it — usd_high already sits ~2.9x above measured spend.
    """
    if words <= 0:
        return 0
    return provider.count_tokens(" ".join([_BUDGET_FILLER_WORD] * words), model)


def chunk_output_tokens(source_tokens: int, source_type: SourceType) -> int:
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
    model = output_model(source_type)
    return round(source_tokens * model.expansion) + model.envelope


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
    ) -> CostEstimate:
        """Compute a CostEstimate covering only PENDING chunks.

        The two bounds differ in more than retry waste. The glossary rides in
        the DYNAMIC (uncached) prompt block and is paid on EVERY call, but it
        GROWS during a run — a fresh job starts empty and a real 502-chunk book
        finished with 491 entries. No pre-flight measurement can predict that
        growth, so:

          usd_low  — the dynamic block the job has RIGHT NOW (empty on a fresh
                     job, the accumulated glossary on a resumed one).
          usd_high — a full `config.glossary_budget_tokens` glossary on every
                     call and the p90 output expansion, times the retry-waste
                     factor.

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

        # Per-call input overhead: the system prompt (static block + narrator
        # + glossary) is sent on EVERY provider call. On thin chunks (SRT cue
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
                config.glossary_budget_tokens,
            )
            dynamic_low = self._provider.count_tokens(dynamic_block, config.model)
            # Ceiling: the same block structure, but with the glossary filled
            # to its budget. Measuring the empty block keeps the [GLOSSARY]
            # scaffolding counted exactly once.
            scaffolding = self._provider.count_tokens(
                self._context_manager.build_dynamic_block(
                    Glossary(), config.glossary_budget_tokens
                ),
                config.model,
            )
            dynamic_high = scaffolding + _word_budget_tokens(
                self._provider, config.glossary_budget_tokens, config.model
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
        out_model = output_model(config.source_type)
        # Input tokens the provider can serve from its prompt cache on the
        # happy path. Caching matches the longest common PREFIX of a request,
        # and only two parts of a call are invariant across a whole job: the
        # static block and the tool schema. The dynamic block sits after them
        # and changes — the narrator per chapter, the glossary whenever a term
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
                chunk_output = chunk_output_tokens(chunk_input, config.source_type)
                # Same measured envelope as the floor; the p90 expansion is
                # what widens the band, so it can never invert.
                chunk_output_high = (
                    round(chunk_input * out_model.expansion_high) + out_model.envelope
                )
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

    def estimate_glossary_extraction(
        self, chunks: list[Chunk], config: JobConfig
    ) -> GlossaryExtractionEstimate:
        """Project the cost of the whole-book glossary extraction pass.

        A line of its own, separate from ``estimate``: extraction is an
        explicit paid action the user chooses to run, and it covers EVERY
        chunk exactly once whatever its translation status — the glossary is
        about the whole text, not about what is left to translate.

        Batches are planned exactly as the pass plans them
        (``plan_extraction_batches`` over the provider's own token count), and
        every batch pays the extraction system prompt and user-prompt prefix on
        top of its source, measured from ``extraction_prompts`` rather than
        restated.

        Output is a ratio of input — the extractor emits glossary entries, not
        a translation, so the translation output model does not apply. ``usd``
        prices the expected ratio, ``usd_high`` the worst measured batch. Both
        assume a cold cache (no discount): a first pass reuses nothing but the
        system prompt, and an optimistic number is the wrong one to show
        before a spend the user has to approve.
        """
        model = config.model
        texts = [chunk.source_text for chunk in chunks]
        # Each distinct text is counted once and the count reused for both the
        # plan and the sum: on a real provider a count can be a round trip.
        counts: dict[str, int] = {}

        def count_once(text: str) -> int:
            if text not in counts:
                counts[text] = self._provider.count_tokens(text, model)
            return counts[text]

        batches = plan_extraction_batches(texts, count_once)
        system, prefix = extraction_prompts("")
        overhead = self._provider.count_tokens(
            system, model
        ) + self._provider.count_tokens(prefix, model)

        # Every chunk lands in exactly one batch, and every batch pays the
        # prompt overhead once.
        source_tokens = sum(count_once(text) for text in texts)
        input_tokens = len(batches) * overhead + source_tokens
        # The ratios were measured against batch SOURCE tokens, so they project
        # output from the source alone; the overhead is priced as input only.
        output_tokens = round(source_tokens * _EXTRACTION_OUTPUT_RATIO)
        output_tokens_high = round(source_tokens * _EXTRACTION_OUTPUT_RATIO_HIGH)

        in_usd_per_mtok, out_usd_per_mtok = self._provider.price(model)
        input_usd = input_tokens / 1_000_000 * in_usd_per_mtok
        return GlossaryExtractionEstimate(
            batches=len(batches),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            output_tokens_high=output_tokens_high,
            usd=input_usd + output_tokens / 1_000_000 * out_usd_per_mtok,
            usd_high=input_usd + output_tokens_high / 1_000_000 * out_usd_per_mtok,
        )
