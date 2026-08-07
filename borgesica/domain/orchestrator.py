"""TranslationOrchestrator — sequential per-chunk translation loop.

Dependency rule: only stdlib + pydantic + domain models/ports/helpers.
No I/O, no adapter imports.

Design (continue-on-error — replaces M2-0 per-chunk flow):
  TranslationOrchestrator.run(job, chunks, glossary, config, on_progress, cancel_flag)
    For each PENDING chunk (in index order):
      1. Check cancel_flag BEFORE the chunk (cooperative, not mid-chunk).
      2. Budget check: if cost_so_far + projected_cost > budget_usd → raise BudgetExceeded
         (PAUSES the job unconditionally — unaffected by continue_on_error).
      3. Prose guard: strip(source_text); if the stripped result is empty/whitespace-only
         OR contains no alphabetic characters, persist the chunk DONE with
         translated_text = source_text (pass-through), make ZERO provider calls, and
         move to the next chunk. Applies identically regardless of continue_on_error.
      4. Build system prompt via ContextManager (using current glossary + last summary).
      5. PRIMARY (tags-in-text): send source_text WITH inline tags to the provider.
         After translate, validate_tags(source, translated):
           - pass → chunk DONE.
           - mismatch → retry (≤2 additional, 3 total).
      6. If quality_mode="reflective": critique + revise on the tagged user prompt.
         Persisted text = REVISE step output.
      7. FALLBACK (only after 3 tags-in-text attempts all fail validation):
         strip(source) → translate plain text (fresh provider call) → reinsert(tags)
         → validate_tags:
           - pass → chunk DONE using fallback output.
           - fail (or provider raises) → chunk FAILED. Whether the job PAUSES is
             gated by JobConfig.continue_on_error:
               - continue_on_error=True (default): job does NOT pause — proceed
                 to the next chunk; the job still finishes DONE with this chunk
                 FAILED (translated_text=None; writers fall back to source_text).
               - continue_on_error=False (--strict): job PAUSES immediately, stop.
      8. checkpoint.save_chunk(DONE or FAILED) — idempotent.
      9. summary = unit.summary_update; checkpoint.save_summary(N) — skipped for
         a FAILED chunk (no summary_update available); the next chunk reuses the
         current rolling summary.
     10. Merge glossary_additions (locked wins; new terms added as unlocked).
     11. job.cost_usd += chunk cost; emit on_progress(Progress(...)).
    After loop: job.status = DONE — even if one or more chunks ended FAILED (only
    reached when continue_on_error=True or no chunk failed; a strict-mode PAUSE
    returns early and never reaches this point).

Resume semantics:
  - DONE chunks are skipped (0 provider calls for them).
  - Rolling summary rebuilt from highest-index DONE summary row.
  - Continue from first non-DONE chunk.
  - CANCELLED / PAUSED / CREATED are all valid entry statuses for run().
  - RUNNING is the only invalid entry status → raises JobStateError.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import UTC, datetime

from borgesica.domain.context import ContextManager
from borgesica.domain.cost import (
    _OUTPUT_ENVELOPE_TOKENS,
    _tool_schema_tokens,
    _waste_factor,
    CostEstimator,
)
from borgesica.domain.errors import (
    BudgetExceeded,
    JobStateError,
    MalformedOutput,
    ProviderError,
)
from borgesica.domain.glossary import merge_additions
from borgesica.domain.markup import (
    reinsert,
    strip,
    strip_all_tags,
    validate_segments,
    validate_tags,
)
from borgesica.domain.language import detect_unexpected_script
from borgesica.domain.prose import has_translatable_prose
from borgesica.domain.models import (
    Chunk,
    ChunkStatus,
    CorpusSample,
    Glossary,
    Job,
    JobConfig,
    JobStatus,
    Progress,
    RollingSummary,
    TranslationUnit,
    Usage,
)
from borgesica.domain.ports import (
    CheckpointStore,
    CorpusStore,
    ProgressCallback,
    TranslationProvider,
)

logger = logging.getLogger(__name__)

# Maximum retries for tag-mismatch (initial attempt + 2 retries = 3 total)
_MAX_TAG_RETRIES = 2

# Reflective mode calls: translate (draft) → critique → revise
_REFLECTIVE_PASSES = 3

# System prompt used for the critique step in reflective mode
_CRITIQUE_SYSTEM = """\
You are a translation quality reviewer. Review the following translation for:
1. Literal calques or unnatural phrasing
2. Register inconsistency
3. Any loss of meaning or image from the source

Return a JSON object:
  {
    "translation": "<your critique notes, NOT the translation>",
    "summary_update": "Critique complete.",
    "glossary_additions": []
  }"""

# System prompt used for the revise step in reflective mode
_REVISE_SYSTEM = """\
You are a professional literary translator. You have received a draft translation
and a critique of its weaknesses. Produce a revised translation that addresses
the critique while remaining faithful to the source.

Return a JSON object:
  {
    "translation": "<revised translation>",
    "summary_update": "<3-5 sentence narrative summary — REPLACES prior summary>",
    "glossary_additions": []
  }"""

# Appended to the system prompt for SRT cue-batch chunks (segmented contract).
# The exact count is stated per chunk because "one per segment" alone is what
# models silently violate on unpunctuated speech-to-text fragments. Segments
# carry explicit [k] index markers because blank-line separation alone is
# ambiguous to the model: a two-line cue looks like two segments, and one
# miscounted chunk desynchronizes the whole positional mapping.
_SEGMENT_SYSTEM_INSTRUCTION = """\
[SEGMENTS]
This chunk contains exactly {n} subtitle segments. Each source segment \
begins with an index marker line "[k]" (from [1] to [{n}]); everything until \
the next marker belongs to that ONE segment, even across line breaks. Fill \
the "translations" array with EXACTLY {n} strings — item k is the \
translation of segment [k]. Do NOT copy the index markers into the \
translations. Never merge, split, or reorder segments; a segment that ends \
mid-sentence must stay mid-sentence."""

# Leading "[k]" echo of the index markers in a returned translation item.
_INDEX_MARKER_RE = re.compile(r"^\[\d+\]\s*")


def _add_usage(a: Usage, b: Usage) -> Usage:
    """Sum two token-Usage value objects field-wise (immutable — returns a new
    Usage). Used to fold prior-step usage onto a mid-sequence exception so no
    billed spend is dropped."""
    return Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
    )


def _indexed_segments(cue_batches: list[dict]) -> str:
    """Render cue batch texts as the indexed user prompt: "[k]\\n<text>"
    blocks separated by blank lines. Markers are transport only — they never
    enter chunk.source_text or the checkpoint format."""
    return "\n\n".join(
        f"[{i}]\n{cb['text']}" for i, cb in enumerate(cue_batches, start=1)
    )


def _segmented_text(
    unit: TranslationUnit, fallback_text: str, segment_count: int
) -> str:
    """Join an aligned translations array into the "\\n\\n" checkpoint format.

    A leading "[k]" marker echoed back by the model is stripped (markers are
    transport, never content). Blank lines INSIDE one segment are collapsed
    to a single newline — they would desynchronize the writer's positional
    split. A missing or misaligned array falls back to the legacy string so
    the existing segment validation drives the retry flow.
    """
    if unit.translations is not None and len(unit.translations) == segment_count:
        return "\n\n".join(
            re.sub(r"\n\s*\n", "\n", _INDEX_MARKER_RE.sub("", seg.strip()))
            for seg in unit.translations
        )
    return fallback_text


class TranslationOrchestrator:
    """Sequential per-chunk translation loop.

    Pure domain — knows only ports and domain helpers.
    No I/O, no adapter imports.

    Args:
        provider:        TranslationProvider for translate() calls.
        checkpoint:      CheckpointStore for persisting job/chunk state.
        context_manager: ContextManager for system prompt assembly.
        cost_estimator:  CostEstimator for per-chunk budget projection.
    """

    def __init__(
        self,
        provider: TranslationProvider,  # type: ignore[type-arg]
        checkpoint: CheckpointStore,  # type: ignore[type-arg]
        context_manager: ContextManager,
        cost_estimator: CostEstimator,
        provider_name: str = "unknown",
        corpus_store: CorpusStore | None = None,  # type: ignore[type-arg]
    ) -> None:
        self._provider = provider
        self._checkpoint = checkpoint
        self._ctx = context_manager
        self._cost_est = cost_estimator
        # Provenance: the adapter's provider name, available in the same
        # scope as chunk.source_text/final_text at chunk-DONE (design
        # decision #6). Additive with a safe default — no required new
        # constructor argument for existing call sites.
        self._provider_name = provider_name
        # Optional corpus capture collaborator (design decision #6). Default
        # None → zero behavior change when absent. Writes are best-effort:
        # a raising CorpusStore never fails the job (decision #10).
        self._corpus_store = corpus_store

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run(
        self,
        job: Job,
        chunks: list[Chunk],
        glossary: Glossary,
        config: JobConfig,
        on_progress: ProgressCallback,
        cancel_flag: threading.Event,
    ) -> Job:
        """Execute (or resume) the sequential translation loop for *job*.

        Args:
            job:         The Job entity (status must NOT be RUNNING).
            chunks:      All chunks for the job (DONE ones are skipped).
            glossary:    Current live glossary (may be pre-populated).
            config:      JobConfig (model, quality_mode, budget_usd, …).
            on_progress: Callback invoked after each chunk completes.
            cancel_flag: threading.Event; checked BEFORE each chunk.

        Returns:
            Updated Job with final status (DONE, PAUSED, or CANCELLED).

        Raises:
            JobStateError:  if job.status == RUNNING on entry.
            BudgetExceeded: if projected cost for the next chunk would exceed budget.
        """
        # Guard: RUNNING is the only invalid entry state.
        if job.status == JobStatus.RUNNING:
            raise JobStateError(
                job_id=job.id,
                current_status=str(job.status),
            )

        # Check cancel before touching anything.
        if cancel_flag.is_set():
            updated_job = job.model_copy(
                update={"status": JobStatus.CANCELLED, "updated_at": datetime.now(UTC)}
            )
            self._checkpoint.save_job(updated_job)
            return updated_job

        # Sort chunks by index for deterministic ordering.
        ordered_chunks = sorted(chunks, key=lambda c: c.index)

        # Rebuild rolling summary from the highest-index DONE chunk.
        current_summary = self._rebuild_summary(job.id, ordered_chunks)

        # Track running cost (may already be non-zero from previous runs).
        running_cost = job.cost_usd

        # Mutable glossary reference — may grow via mid-run additions.
        live_glossary = glossary

        # Process each chunk in order.
        for chunk in ordered_chunks:
            # Skip already-DONE chunks (resume semantics).
            if chunk.status == ChunkStatus.DONE:
                continue

            # Cooperative cancel check — BEFORE the chunk.
            if cancel_flag.is_set():
                updated_job = job.model_copy(
                    update={
                        "status": JobStatus.CANCELLED,
                        "cost_usd": running_cost,
                        "updated_at": datetime.now(UTC),
                    }
                )
                self._checkpoint.save_job(updated_job)
                return updated_job

            # Budget check — BEFORE the provider call.
            if config.budget_usd is not None:
                projected = self._project_chunk_cost(
                    chunk, config, live_glossary, current_summary
                )
                if running_cost + projected > config.budget_usd:
                    paused_job = job.model_copy(
                        update={
                            "status": JobStatus.PAUSED,
                            "cost_usd": running_cost,
                            "updated_at": datetime.now(UTC),
                        }
                    )
                    self._checkpoint.save_job(paused_job)
                    raise BudgetExceeded(job_id=job.id, cost_so_far=running_cost)

            # Prose guard: chunks with no translatable prose (empty/whitespace-only
            # after stripping ALL markup, or made only of digits, strict roman
            # numerals, and punctuation — front-matter page lists like "i ii iii
            # 1 2 3") pass through verbatim with ZERO provider calls and ZERO cost.
            # strip_all_tags (not strip) so void/unknown tags don't count as prose:
            # an <img> nested in a <p> serializes into source_text and its src/alt
            # letters used to defeat the guard — a real cover burned provider calls.
            # This is deliberately conservative — one prose token anywhere makes the
            # chunk translatable, so this guard has zero false positives (never
            # silently skips a translatable sentence). Applies identically
            # regardless of continue_on_error.
            stripped_text = strip_all_tags(chunk.source_text)
            if not has_translatable_prose(stripped_text):
                passthrough_chunk = chunk.model_copy(
                    update={
                        "status": ChunkStatus.DONE,
                        "translated_text": chunk.source_text,
                    }
                )
                self._checkpoint.save_chunk(job.id, passthrough_chunk)
                on_progress(
                    Progress(
                        job_id=job.id,
                        chunk_index=chunk.index,
                        total_chunks=len(ordered_chunks),
                        cost_usd=running_cost,
                        status=JobStatus.RUNNING,
                    )
                )
                continue

            # Build system prompt for this chunk.
            system_prompt = self._ctx.build_system_prompt(config, live_glossary, current_summary)

            # Fetch price once per chunk (same model throughout — no need to re-fetch per call).
            in_price, out_price = self._provider.price(config.model)

            # PRIMARY: send source_text WITH tags (do NOT strip up front).
            # The system prompt instructs the model to carry every tag with its word.
            # FALLBACK (if primary exhausted): strip → translate plain → reinsert.
            (
                final_unit,
                final_text,
                call_cost,
                passed_validation,
                validation_errors,
            ) = self._translate_with_retry(
                chunk=chunk,
                system=system_prompt.text,
                config=config,
                in_price=in_price,
                out_price=out_price,
            )

            # Accrue the real cost of ALL calls made (regardless of success or failure).
            # This fixes both Bug-1 (reflective charges 3 calls) and Bug-2 (failed
            # chunks charged 0 — now they accrue the cost of every attempt made).
            running_cost += call_cost

            if final_unit is None:
                # All retries exhausted — chunk FAILED. Whether the job PAUSES
                # is gated by JobConfig.continue_on_error.
                failed_chunk = chunk.model_copy(
                    update={
                        "status": ChunkStatus.FAILED,
                        "passed_validation": passed_validation,
                        "validation_errors": validation_errors,
                    }
                )
                self._checkpoint.save_chunk(job.id, failed_chunk)
                if not config.continue_on_error:
                    paused_job = job.model_copy(
                        update={
                            "status": JobStatus.PAUSED,
                            "cost_usd": running_cost,
                            "updated_at": datetime.now(UTC),
                        }
                    )
                    self._checkpoint.save_job(paused_job)
                    return paused_job
                # continue_on_error=True: do NOT pause — proceed to the next chunk.
                # Rolling summary is intentionally NOT updated for a FAILED chunk
                # (no summary_update available); next chunk reuses current_summary.
                on_progress(
                    Progress(
                        job_id=job.id,
                        chunk_index=chunk.index,
                        total_chunks=len(ordered_chunks),
                        cost_usd=running_cost,
                        status=JobStatus.RUNNING,
                    )
                )
                continue

            # Persist chunk as DONE (idempotent upsert).
            done_chunk = chunk.model_copy(
                update={
                    "status": ChunkStatus.DONE,
                    "translated_text": final_text,
                    "passed_validation": passed_validation,
                    "validation_errors": validation_errors,
                }
            )
            self._checkpoint.save_chunk(job.id, done_chunk)

            # Corpus capture (design decisions #6/#10): engine-wide, best-effort,
            # write-only. Only real translated DONE chunks are captured — never
            # pass-through (source==translated, zero provider calls) or FAILED.
            self._capture_corpus(job.id, done_chunk, config, passed_validation)

            # Update rolling summary (REPLACES prior summary).
            current_summary = RollingSummary(
                text=final_unit.summary_update,
                chunk_index=chunk.index,
            )
            self._checkpoint.save_summary(job.id, current_summary)

            # Merge glossary additions (locked wins; new terms added as unlocked).
            if final_unit.glossary_additions:
                live_glossary = merge_additions(live_glossary, final_unit.glossary_additions)
                self._checkpoint.save_glossary(job.id, live_glossary)

            # Emit progress callback.
            on_progress(
                Progress(
                    job_id=job.id,
                    chunk_index=chunk.index,
                    total_chunks=len(ordered_chunks),
                    cost_usd=running_cost,
                    status=JobStatus.RUNNING,
                )
            )

        # All chunks processed (or all were already DONE).
        done_job = job.model_copy(
            update={
                "status": JobStatus.DONE,
                "cost_usd": running_cost,
                "updated_at": datetime.now(UTC),
            }
        )
        self._checkpoint.save_job(done_job)
        return done_job

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _rebuild_summary(self, job_id: str, ordered_chunks: list[Chunk]) -> RollingSummary:
        """Return the rolling summary from the highest-index DONE chunk.

        If no DONE chunks exist yet, return an empty RollingSummary.
        """
        done_indices = [c.index for c in ordered_chunks if c.status == ChunkStatus.DONE]
        if not done_indices:
            return RollingSummary()
        # load_summary returns the summary from the highest-index chunk.
        # The CheckpointStore contract guarantees this for both the in-memory
        # and SQLite implementations (see design section 5).
        return self._checkpoint.load_summary(job_id)

    def _project_chunk_cost(
        self,
        chunk: Chunk,
        config: JobConfig,
        glossary: Glossary | None = None,
        summary: RollingSummary | None = None,
    ) -> float:
        """Estimate the cost of translating a single chunk (pre-call budget GUARD only).

        This is a CONSERVATIVE PROJECTION used before the provider call to decide
        whether to proceed.  It is NOT used to accrue actual cost — that comes from
        TranslationResult.usage after each real call.  Do NOT remove this method;
        the budget guard requires it.

        Unlike the pre-flight CostEstimator, this runs mid-loop with the LIVE
        glossary and summary in hand — the very objects that build the next
        prompt — so the dynamic block is measured exactly rather than budgeted.
        The projection therefore RISES as the glossary accumulates, which is
        when the real per-call cost rises too. A job may now pause partway
        through where it previously blew past its budget in silence.
        """
        source_tokens = self._provider.count_tokens(chunk.source_text, config.model)
        # Per-call system prompt (static block + dynamic block) is paid on
        # EVERY call — on thin SRT chunks it dominates the call cost.
        static_tokens = self._provider.count_tokens(
            self._ctx.get_static_block(config), config.model
        )
        # Measured, not mirrored: this is the same builder that produces the
        # prompt a few lines later in run(). See cost.py for the stale-constant
        # regression that made mirroring it a bug.
        dynamic_tokens = self._provider.count_tokens(
            self._ctx.build_dynamic_block(
                glossary if glossary is not None else Glossary(),
                summary if summary is not None else RollingSummary(),
                config.glossary_budget_tokens,
            ),
            config.model,
        )
        # Tool schema (input_schema in tools=) is billed as input on every call.
        schema_tokens = _tool_schema_tokens(self._provider, chunk, config.model)
        input_tokens = source_tokens + static_tokens + dynamic_tokens + schema_tokens
        # Output = source-sized translation + JSON envelope (same as CostEstimator).
        output_tokens = source_tokens + _OUTPUT_ENVELOPE_TOKENS
        # Nav-label chunks always single-pass, regardless of quality_mode (D3):
        # the critique/revise cycle yields no quality gain for short factual
        # nav labels, and the orchestrator must not over-project 3x budget for
        # a chunk that will only ever make 1 real call.
        is_nav_label = chunk.meta.get("kind") == "nav-label"
        passes = 1 if is_nav_label else (3 if config.quality_mode == "reflective" else 1)
        in_price, out_price = self._provider.price(config.model)
        happy_path = (
            input_tokens * passes / 1_000_000 * in_price
            + output_tokens * passes / 1_000_000 * out_price
        )
        # The budget guard protects against the CEILING: apply the provider's
        # retry / tier-fallthrough waste factor so a chunk that could realistically
        # cost 2-3x its happy-path price does not silently blow the budget.
        return happy_path * _waste_factor(self._provider)

    def _capture_corpus(
        self,
        job_id: str,
        done_chunk: Chunk,
        config: JobConfig,
        passed_validation: bool,
    ) -> None:
        """Best-effort corpus capture at the real chunk-DONE point.

        No-op when no corpus_store is injected (default None — zero behavior
        change). A raised exception from the store is logged and swallowed —
        it MUST NOT fail, pause, or otherwise affect the translation job
        (design decision #10: silent best-effort).
        """
        if self._corpus_store is None:
            return
        try:
            sample = CorpusSample(
                job_id=job_id,
                chunk_index=done_chunk.index,
                source_text=done_chunk.source_text,
                translated_text=done_chunk.translated_text,
                provider=self._provider_name,
                model=config.model,
                quality_mode=config.quality_mode,
                passed_validation=passed_validation,
                validation_errors=done_chunk.validation_errors,
            )
            self._corpus_store.save_sample(sample)
        except Exception:
            logger.warning(
                "Corpus capture failed for job %s chunk %d — dropping sample "
                "(best-effort, does not affect the translation job)",
                job_id,
                done_chunk.index,
                exc_info=True,
            )

    @staticmethod
    def _usage_cost(usage: Usage, in_price: float, out_price: float) -> float:
        """Compute the USD cost for a single provider call's real Usage."""
        return (
            usage.input_tokens / 1_000_000 * in_price
            + usage.output_tokens / 1_000_000 * out_price
        )

    def _translate_with_retry(
        self,
        chunk: Chunk,
        system: str,
        config: JobConfig,
        in_price: float,
        out_price: float,
    ) -> tuple[TranslationUnit | None, str | None, float, bool, str | None]:
        """Attempt translation using the tags-in-text PRIMARY path, with up to
        _MAX_TAG_RETRIES retries on tag-count mismatch, then a deterministic
        FALLBACK (strip → translate plain → reinsert) if all primary attempts fail.

        PRIMARY (tags-in-text):
          - Send chunk.source_text WITH inline tags as the user prompt.
          - validate_tags(source, translated_text) on the raw output.
          - validate_segments(source, translated_text): the "\\n\\n" segment
            count must match the source (writers map segments to document
            nodes positionally). Mismatch retries like a tag mismatch, but if
            every attempt is tag-valid and only segment-mismatched, the LAST
            such attempt is accepted (never FAILED, no fallback call) — the
            writer's defensive mapping absorbs it.
          - Retry up to _MAX_TAG_RETRIES times (3 total attempts) on mismatch.

        FALLBACK (strip/reinsert deterministic path):
          - Applied only after all primary attempts fail.
          - strip(source) → translate plain text (fresh provider call) → reinsert(tags).
          - validate_tags again; if pass → chunk DONE using fallback output.
          - If provider raises or validate_tags fails → return (None, None, total_cost, False).

        Returns:
            (TranslationUnit, translated_text, total_call_cost, passed_validation,
            validation_errors) on success. passed_validation is True for the
            happy-path first-try match AND the strip/reinsert fallback success
            (both are full validate_tags/validate_segments passes) — False for
            the segment-mismatch best-effort acceptance (design decision #7:
            DONE does NOT imply validation passed) and False when both primary
            and fallback are exhausted (chunk FAILED — caller ignores the
            flag's semantic weight there since translated_text is None, but it
            is still threaded consistently).
            (None, None, total_call_cost, False, validation_errors) if both
            primary and fallback fail.
            total_call_cost is the SUM of real usage costs from ALL calls made
            (including failed attempts and the fallback call).
            validation_errors (T4b amendment) is a JSON-serialized list of the
            validator issue messages collected across every failed/mismatched
            attempt — None whenever passed_validation ends up True (a clean
            pass discards prior transient mismatches from earlier attempts).
        """
        user_prompt = chunk.source_text  # PRIMARY: send WITH tags
        total_call_cost = 0.0
        # T4b: validator issue messages collected across every mismatched or
        # failed attempt (tag mismatch, segment mismatch, fallback failure).
        # Serialized to JSON only when the chunk ends up NOT passing validation.
        validation_issues: list[str] = []

        # Segmented (SRT) contract: cue-batch chunks request the translations
        # ARRAY — cue boundaries as structured data, not blank-line convention
        # (models "correct" blank lines between unpunctuated speech-to-text
        # fragments; they fill a fixed-length array reliably). The user prompt
        # carries explicit [k] index markers; validation and checkpointing
        # keep using the marker-free chunk.source_text.
        cue_batches = chunk.meta.get("cue_batches")
        segment_count = len(cue_batches) if cue_batches else None
        if segment_count is not None:
            user_prompt = _indexed_segments(cue_batches)
            system = (
                f"{system}\n\n"
                + _SEGMENT_SYSTEM_INSTRUCTION.format(n=segment_count)
            )
        # Passed as **kwargs so providers (and test fakes) that predate
        # segment_count keep working on the prose path.
        segment_kwargs = (
            {} if segment_count is None else {"segment_count": segment_count}
        )

        # Nav-label chunks always single-pass, regardless of quality_mode (D3):
        # short factual nav labels gain nothing from critique/revise, and the
        # orchestrator has no other per-chunk-kind branch — this is the ONLY one.
        is_nav_label = chunk.meta.get("kind") == "nav-label"

        # Last tag-valid attempt whose "\n\n" segment count diverged from the
        # source — accepted after the loop if no compliant output arrives.
        best_effort: tuple[TranslationUnit, str] | None = None

        # --- PRIMARY: tags-in-text attempts ---
        for attempt in range(_MAX_TAG_RETRIES + 1):  # 0, 1, 2
            try:
                if config.quality_mode == "reflective" and not is_nav_label:
                    unit, translated_text, call_cost = self._translate_reflective(
                        system=system,
                        user=user_prompt,
                        config=config,
                        in_price=in_price,
                        out_price=out_price,
                        segment_count=segment_count,
                    )
                    total_call_cost += call_cost
                else:
                    result = self._provider.translate(
                        system=system,
                        user=user_prompt,
                        model=config.model,
                        **segment_kwargs,
                    )
                    unit = result.unit
                    translated_text = result.unit.translation
                    total_call_cost += self._usage_cost(result.usage, in_price, out_price)
            except (MalformedOutput, ProviderError) as exc:
                # The provider gave up on this attempt (after its own tiers/retries).
                # Treat it as a failed attempt: retry, or fall through to the
                # deterministic fallback after the last attempt. A single flaky
                # chunk must NOT crash the whole run — worst case the chunk ends
                # FAILED and the job PAUSED (resumable), never stranded RUNNING.
                # Cost fix: the provider's internal billed-but-failed calls carry
                # real usage on the exception — accrue it so the budget guard sees
                # the true spend of failed chunks (previously charged $0).
                total_call_cost += self._usage_cost(exc.usage, in_price, out_price)
                continue

            # Segmented contract: an aligned translations array becomes the
            # canonical "\n\n" join (validate_segments then passes by
            # construction); anything else keeps the legacy string so the
            # existing mismatch retry / best-effort flow applies.
            if segment_count is not None:
                translated_text = _segmented_text(unit, translated_text, segment_count)

            # Validate tag counts in the raw translation (tags-in-text path).
            if validate_tags(chunk.source_text, translated_text):
                if validate_segments(chunk.source_text, translated_text):
                    # Structure can be perfect while the LANGUAGE is wrong: a
                    # real run shipped one chunk translated into Chinese
                    # because every check above reasons about shape only.
                    # Deterministic and local — no extra provider call.
                    foreign = detect_unexpected_script(
                        translated_text, config.target_lang
                    )
                    if foreign is None:
                        return unit, translated_text, total_call_cost, True, None
                    validation_issues.append(
                        f"attempt {attempt + 1}: output is in {foreign} script, "
                        f"not {config.target_lang}"
                    )
                    best_effort = (unit, translated_text)
                    continue
                # Segment-count mismatch (model merged/split "\n\n" paragraphs,
                # which desynchronizes the writer's positional node mapping):
                # keep this tag-valid attempt and retry for a compliant one.
                src_segs = len(chunk.source_text.split("\n\n"))
                got_segs = len(translated_text.split("\n\n"))
                validation_issues.append(
                    f"attempt {attempt + 1}: segment count mismatch "
                    f"(expected {src_segs}, got {got_segs})"
                )
                best_effort = (unit, translated_text)
                continue

            # Tag mismatch — retry unless this was the last attempt.
            validation_issues.append(f"attempt {attempt + 1}: tag count mismatch")

        # All attempts exhausted with at least one tag-valid but
        # segment-mismatched output. Two different consequences:
        #
        # PROSE: accept the last one — the writer's defensive mapping absorbs
        # the mismatch and the text is still shown translated.
        #
        # SRT cue batches: a misaligned batch is USELESS to the SrtWriter (it
        # cannot map k translations onto n cues without desynchronizing
        # timing, so it falls back to source text — untranslated cues shipped
        # silently). End the chunk FAILED instead: it surfaces in the CLI
        # skip summary and `resume` retries exactly these chunks. The
        # strip/reinsert fallback below exists for TAG failures and would
        # cost another call without fixing segmentation — skip it.
        if best_effort is not None:
            if segment_count is None:
                logger.warning(
                    "Chunk %d: segment-count mismatch persisted after %d attempts — "
                    "accepting best effort (writer applies defensive mapping)",
                    chunk.index,
                    _MAX_TAG_RETRIES + 1,
                )
                best_unit, best_text = best_effort
                return (
                    best_unit,
                    best_text,
                    total_call_cost,
                    False,
                    json.dumps(validation_issues),
                )
            logger.warning(
                "Chunk %d: translations array misaligned with the %d cues after "
                "%d attempts — chunk FAILED (resume retries only failed chunks)",
                chunk.index,
                segment_count,
                _MAX_TAG_RETRIES + 1,
            )
            return None, None, total_call_cost, False, json.dumps(validation_issues)

        # --- FALLBACK: strip → translate plain → reinsert ---
        # All primary (tags-in-text) attempts exhausted.
        # Apply the OLD deterministic path as a last resort.
        try:
            plain_source, tags = strip(chunk.source_text)
            fallback_result = self._provider.translate(
                system=system,
                user=plain_source,
                model=config.model,
            )
            total_call_cost += self._usage_cost(fallback_result.usage, in_price, out_price)
            fallback_unit = fallback_result.unit
            fallback_text = reinsert(fallback_unit.translation, tags, plain_source)
            if validate_tags(chunk.source_text, fallback_text):
                return fallback_unit, fallback_text, total_call_cost, True, None
            validation_issues.append("fallback: tag count mismatch after strip/reinsert")
        except (MalformedOutput, ProviderError) as exc:
            # Provider failed during the fallback call — treat as total failure
            # (chunk FAILED, job PAUSED). Any OTHER exception (a real bug in
            # strip/reinsert, etc.) is intentionally NOT caught here so it surfaces
            # instead of being silently mislabeled as a tag failure.
            # Cost fix: accrue the billed-but-failed usage carried on the exception
            # so the fallback call's real cost is not dropped.
            total_call_cost += self._usage_cost(exc.usage, in_price, out_price)
            validation_issues.append(f"fallback: provider error ({exc})")

        # Both primary and fallback failed.
        return None, None, total_call_cost, False, json.dumps(validation_issues)

    def _translate_reflective(
        self,
        system: str,
        user: str,
        config: JobConfig,
        in_price: float,
        out_price: float,
        segment_count: int | None = None,
    ) -> tuple[TranslationUnit, str, float]:
        """Execute the translate → critique → revise loop.

        As of M2-0, the user prompt is the source text WITH inline tags (tags-in-text
        primary path). The REVISE step's output is returned as the translated_text;
        the caller validates tags against chunk.source_text.

        Each of the 3 provider calls accrues real usage; the total is returned
        as the third element so the orchestrator can add it to running_cost
        regardless of whether this reflective pass ultimately passed validation.

        Returns (final_unit, translated_text, total_cost) where:
          - final_unit: the REVISE step's TranslationUnit
          - translated_text: the REVISE step's translation (caller validates tags)
          - total_cost: sum of real usage costs for all 3 calls (draft+critique+revise)
        """
        total_cost = 0.0

        # Real token usage of the calls that have ALREADY succeeded in this
        # reflective sequence. If a later step (critique/revise) raises after an
        # earlier step was billed, the exception must carry this accrued usage so
        # the caller's `except (MalformedOutput, ProviderError)` handler accrues
        # the FULL spend — not just the single failing call's usage. Without
        # this, the draft (and critique) spend is discarded with the unwinding
        # local `total_cost`, undercounting running_cost and silently weakening
        # the budget guard (REL-003).
        accrued_usage = Usage()

        # Segmented (SRT) contract applies to the DRAFT and REVISE calls — the
        # calls whose output is a translation. The CRITIQUE call returns notes,
        # not a translation, so it keeps the plain single-string contract.
        segment_kwargs = (
            {} if segment_count is None else {"segment_count": segment_count}
        )

        try:
            # Step 1: Draft translation (user prompt has tags)
            draft_result = self._provider.translate(
                system=system, user=user, model=config.model, **segment_kwargs
            )
            accrued_usage = _add_usage(accrued_usage, draft_result.usage)
            total_cost += self._usage_cost(draft_result.usage, in_price, out_price)
            draft_unit = draft_result.unit
            draft_text = draft_unit.translation

            # Step 2: Critique
            critique_prompt = (
                f"Source text:\n{user}\n\n"
                f"Draft translation:\n{draft_text}\n\n"
                "Please critique the draft translation above."
            )
            critique_result = self._provider.translate(
                system=_CRITIQUE_SYSTEM,
                user=critique_prompt,
                model=config.model,
            )
            accrued_usage = _add_usage(accrued_usage, critique_result.usage)
            total_cost += self._usage_cost(critique_result.usage, in_price, out_price)
            critique_text = critique_result.unit.translation

            # Step 3: Revise
            revise_prompt = (
                f"Source text:\n{user}\n\n"
                f"Draft translation:\n{draft_text}\n\n"
                f"Critique:\n{critique_text}\n\n"
                "Please produce a revised translation."
            )
            revise_system = _REVISE_SYSTEM
            if segment_count is not None:
                revise_system = (
                    f"{_REVISE_SYSTEM}\n\n"
                    + _SEGMENT_SYSTEM_INSTRUCTION.format(n=segment_count)
                )
            revised_result = self._provider.translate(
                system=revise_system,
                user=revise_prompt,
                model=config.model,
                **segment_kwargs,
            )
            accrued_usage = _add_usage(accrued_usage, revised_result.usage)
            total_cost += self._usage_cost(revised_result.usage, in_price, out_price)
            revised_unit = revised_result.unit
        except (MalformedOutput, ProviderError) as exc:
            # Fold the already-billed usage of prior successful steps into the
            # exception's own usage so no reflective spend is lost as the local
            # frame unwinds. `exc.usage` already carries the failing call's real
            # billed-but-failed usage; adding `accrued_usage` makes it the true
            # total for the whole partial sequence. All calls use config.model,
            # so the caller's single (in_price, out_price) converts it correctly.
            exc.usage = _add_usage(exc.usage, accrued_usage)
            raise

        # Return the REVISE output directly; caller validates tags.
        return revised_unit, revised_unit.translation, total_cost
