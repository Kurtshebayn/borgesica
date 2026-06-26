"""TranslationOrchestrator — sequential per-chunk translation loop.

Dependency rule: only stdlib + pydantic + domain models/ports/helpers.
No I/O, no adapter imports.

Design (M2-0 tag-rework — replaces M1-8 per-chunk flow):
  TranslationOrchestrator.run(job, chunks, glossary, config, on_progress, cancel_flag)
    For each PENDING chunk (in index order):
      1. Check cancel_flag BEFORE the chunk (cooperative, not mid-chunk).
      2. Budget check: if cost_so_far + projected_cost > budget_usd → raise BudgetExceeded.
      3. Build system prompt via ContextManager (using current glossary + last summary).
      4. PRIMARY (tags-in-text): send source_text WITH inline tags to the provider.
         After translate, validate_tags(source, translated):
           - pass → chunk DONE.
           - mismatch → retry (≤2 additional, 3 total).
      5. If quality_mode="reflective": critique + revise on the tagged user prompt.
         Persisted text = REVISE step output.
      6. FALLBACK (only after 3 tags-in-text attempts all fail validation):
         strip(source) → translate plain text (fresh provider call) → reinsert(tags)
         → validate_tags:
           - pass → chunk DONE using fallback output.
           - fail (or provider raises) → chunk FAILED, job PAUSED, stop.
      7. checkpoint.save_chunk(DONE) — idempotent.
      8. summary = unit.summary_update; checkpoint.save_summary(N).
      9. Merge glossary_additions (locked wins; new terms added as unlocked).
     10. job.cost_usd += chunk cost; emit on_progress(Progress(...)).
    After loop: job.status = DONE (if all completed normally).

Resume semantics:
  - DONE chunks are skipped (0 provider calls for them).
  - Rolling summary rebuilt from highest-index DONE summary row.
  - Continue from first non-DONE chunk.
  - CANCELLED / PAUSED / CREATED are all valid entry statuses for run().
  - RUNNING is the only invalid entry status → raises JobStateError.
"""
from __future__ import annotations

import threading
from datetime import UTC, datetime

from borgesica.domain.context import ContextManager
from borgesica.domain.cost import CostEstimator
from borgesica.domain.errors import (
    BudgetExceeded,
    JobStateError,
    MalformedOutput,
    ProviderError,
)
from borgesica.domain.glossary import merge_additions
from borgesica.domain.markup import reinsert, strip, validate_tags
from borgesica.domain.models import (
    Chunk,
    ChunkStatus,
    Glossary,
    Job,
    JobConfig,
    JobStatus,
    Progress,
    RollingSummary,
    TranslationUnit,
)
from borgesica.domain.ports import CheckpointStore, ProgressCallback, TranslationProvider

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
    ) -> None:
        self._provider = provider
        self._checkpoint = checkpoint
        self._ctx = context_manager
        self._cost_est = cost_estimator

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
                projected = self._project_chunk_cost(chunk, config)
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

            # Build system prompt for this chunk.
            system_prompt = self._ctx.build_system_prompt(config, live_glossary, current_summary)

            # PRIMARY: send source_text WITH tags (do NOT strip up front).
            # The system prompt instructs the model to carry every tag with its word.
            # FALLBACK (if primary exhausted): strip → translate plain → reinsert.
            final_unit, final_text = self._translate_with_retry(
                chunk=chunk,
                system=system_prompt.text,
                config=config,
            )

            if final_unit is None:
                # All retries exhausted — chunk FAILED, job PAUSED.
                failed_chunk = chunk.model_copy(update={"status": ChunkStatus.FAILED})
                self._checkpoint.save_chunk(job.id, failed_chunk)
                paused_job = job.model_copy(
                    update={
                        "status": JobStatus.PAUSED,
                        "cost_usd": running_cost,
                        "updated_at": datetime.now(UTC),
                    }
                )
                self._checkpoint.save_job(paused_job)
                return paused_job

            # Persist chunk as DONE (idempotent upsert).
            done_chunk = chunk.model_copy(
                update={
                    "status": ChunkStatus.DONE,
                    "translated_text": final_text,
                }
            )
            self._checkpoint.save_chunk(job.id, done_chunk)

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

            # Accrue cost.
            chunk_cost = self._actual_chunk_cost(chunk, config)
            running_cost += chunk_cost

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

    def _project_chunk_cost(self, chunk: Chunk, config: JobConfig) -> float:
        """Estimate the cost of translating a single chunk (for budget guard)."""
        input_tokens = self._provider.count_tokens(chunk.source_text, config.model)
        output_tokens = 150  # conservative default (same as CostEstimator)
        passes = 3 if config.quality_mode == "reflective" else 1
        in_price, out_price = self._provider.price(config.model)
        return (
            input_tokens * passes / 1_000_000 * in_price
            + output_tokens * passes / 1_000_000 * out_price
        )

    def _actual_chunk_cost(self, chunk: Chunk, config: JobConfig) -> float:
        """Compute the actual cost charged for a completed chunk.

        Uses the same projection formula as _project_chunk_cost for consistency.
        A real adapter would return actual token counts; here we use the estimate.
        """
        return self._project_chunk_cost(chunk, config)

    def _translate_with_retry(
        self,
        chunk: Chunk,
        system: str,
        config: JobConfig,
    ) -> tuple[TranslationUnit | None, str | None]:
        """Attempt translation using the tags-in-text PRIMARY path, with up to
        _MAX_TAG_RETRIES retries on tag-count mismatch, then a deterministic
        FALLBACK (strip → translate plain → reinsert) if all primary attempts fail.

        PRIMARY (tags-in-text):
          - Send chunk.source_text WITH inline tags as the user prompt.
          - validate_tags(source, translated_text) on the raw output.
          - Retry up to _MAX_TAG_RETRIES times (3 total attempts) on mismatch.

        FALLBACK (strip/reinsert deterministic path):
          - Applied only after all primary attempts fail.
          - strip(source) → translate plain text (fresh provider call) → reinsert(tags).
          - validate_tags again; if pass → chunk DONE using fallback output.
          - If provider raises or validate_tags fails → return (None, None).

        Returns:
            (TranslationUnit, translated_text) on success.
            (None, None) if both primary and fallback fail.
        """
        user_prompt = chunk.source_text  # PRIMARY: send WITH tags

        # --- PRIMARY: tags-in-text attempts ---
        for attempt in range(_MAX_TAG_RETRIES + 1):  # 0, 1, 2
            if config.quality_mode == "reflective":
                unit, translated_text = self._translate_reflective(
                    system=system,
                    user=user_prompt,
                    config=config,
                )
            else:
                unit = self._provider.translate(
                    system=system,
                    user=user_prompt,
                    model=config.model,
                )
                translated_text = unit.translation

            # Validate tag counts in the raw translation (tags-in-text path).
            if validate_tags(chunk.source_text, translated_text):
                return unit, translated_text

            # Tag mismatch — retry unless this was the last attempt.

        # --- FALLBACK: strip → translate plain → reinsert ---
        # All primary (tags-in-text) attempts exhausted.
        # Apply the OLD deterministic path as a last resort.
        try:
            plain_source, tags = strip(chunk.source_text)
            fallback_unit = self._provider.translate(
                system=system,
                user=plain_source,
                model=config.model,
            )
            fallback_text = reinsert(fallback_unit.translation, tags, plain_source)
            if validate_tags(chunk.source_text, fallback_text):
                return fallback_unit, fallback_text
        except (MalformedOutput, ProviderError):
            # Provider failed during the fallback call — treat as total failure
            # (chunk FAILED, job PAUSED). Any OTHER exception (a real bug in
            # strip/reinsert, etc.) is intentionally NOT caught here so it surfaces
            # instead of being silently mislabeled as a tag failure.
            pass

        # Both primary and fallback failed.
        return None, None

    def _translate_reflective(
        self,
        system: str,
        user: str,
        config: JobConfig,
    ) -> tuple[TranslationUnit, str]:
        """Execute the translate → critique → revise loop.

        As of M2-0, the user prompt is the source text WITH inline tags (tags-in-text
        primary path). The REVISE step's output is returned as the translated_text;
        the caller validates tags against chunk.source_text.

        Returns (final_unit, translated_text) where translated_text is
        the REVISE step's output (raw — caller handles validate_tags).
        """
        # Step 1: Draft translation (user prompt has tags)
        draft_unit = self._provider.translate(system=system, user=user, model=config.model)
        draft_text = draft_unit.translation

        # Step 2: Critique
        critique_prompt = (
            f"Source text:\n{user}\n\n"
            f"Draft translation:\n{draft_text}\n\n"
            "Please critique the draft translation above."
        )
        critique_unit = self._provider.translate(
            system=_CRITIQUE_SYSTEM,
            user=critique_prompt,
            model=config.model,
        )
        critique_text = critique_unit.translation

        # Step 3: Revise
        revise_prompt = (
            f"Source text:\n{user}\n\n"
            f"Draft translation:\n{draft_text}\n\n"
            f"Critique:\n{critique_text}\n\n"
            "Please produce a revised translation."
        )
        revised_unit = self._provider.translate(
            system=_REVISE_SYSTEM,
            user=revise_prompt,
            model=config.model,
        )
        # Return the REVISE output directly; caller validates tags.
        return revised_unit, revised_unit.translation
