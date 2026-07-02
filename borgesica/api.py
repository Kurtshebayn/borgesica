"""TranslatorEngine — public API for the Borgésica translation engine.

This module is the COMPOSITION ROOT: it wires concrete adapters into the
domain via constructor DI. Business logic lives exclusively in the domain
modules (orchestrator, chunker, context, cost, glossary). This file only
delegates and routes.

Dependency rule: api.py MAY import adapters and domain. Adapters never
import api.py. Domain never imports adapters or api.py.
"""
from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime

from borgesica.domain.chunking import SrtChunker, chunk_prose
from borgesica.domain.context import ContextManager
from borgesica.domain.cost import CostEstimator
from borgesica.domain.errors import JobNotFoundError, JobStateError
from borgesica.domain.glossary import NullGlossaryExtractor
from borgesica.domain.models import (
    ChunkStatus,
    CostEstimate,
    Glossary,
    GlossaryEntry,
    Job,
    JobConfig,
    JobStatus,
    SourceType,
)
from borgesica.domain.orchestrator import TranslationOrchestrator
from borgesica.domain.ports import (
    CheckpointStore,
    DocumentReader,
    DocumentWriter,
    GlossaryExtractor,
    ProgressCallback,
    TranslationProvider,
)


class TranslatorEngine:
    """Public API for the Borgésica translation engine.

    All methods delegate to domain services. This class is the only place
    where concrete adapters are named and wired together.

    Args:
        provider:   Concrete TranslationProvider (e.g. AnthropicProvider).
        checkpoint: Concrete CheckpointStore (e.g. SQLiteCheckpointStore).
        readers:    Map from SourceType → DocumentReader.
        writers:    Map from SourceType → DocumentWriter.
        extractor:  GlossaryExtractor for seeding; None → NullGlossaryExtractor.
    """

    def __init__(
        self,
        *,
        provider: TranslationProvider,
        checkpoint: CheckpointStore,
        readers: dict[SourceType, DocumentReader],
        writers: dict[SourceType, DocumentWriter],
        extractor: GlossaryExtractor | None = None,
    ) -> None:
        self._provider = provider
        self._checkpoint = checkpoint
        self._readers = readers
        self._writers = writers
        self._extractor = extractor if extractor is not None else NullGlossaryExtractor()

        # Domain services (no adapter deps)
        self._ctx = ContextManager(provider=provider)
        self._cost_est = CostEstimator(provider=provider, context_manager=self._ctx)

        # Per-job cancel flags: {job_id: threading.Event}
        self._cancel_flags: dict[str, threading.Event] = {}

    # ------------------------------------------------------------------
    # create_job
    # ------------------------------------------------------------------

    def create_job(self, source_path: str, config: JobConfig) -> Job:
        """Read + chunk the source, seed glossary, persist everything, return Job(CREATED).

        No translation provider call is made. The caller can inspect and edit
        the glossary via get_glossary / update_glossary before running.

        Args:
            source_path: Absolute (or resolvable) path to the source file.
            config:      JobConfig controlling chunking, model, budget, etc.

        Returns:
            Job with status=CREATED, total_chunks set, completed_chunks=0, cost_usd=0.
        """
        # 1. Select reader
        reader = self._readers[config.source_type]

        # 2. Read + chunk (dispatch by source type)
        cues = reader.read(source_path, config)
        if config.source_type in (SourceType.EPUB, SourceType.PDF):
            # Prose formats: chunker with per-node provenance (M2-2R / M3-FIX contract).
            # EPUB nodes carry {epub_item_href, node_path, chapter_index}.
            # PDF nodes carry {pdf_page, para_index, chapter_index}.
            # chunk_prose passes through all meta keys except chapter_index into
            # prose_nodes, making it format-agnostic.
            chunks = chunk_prose(cues, config, self._provider)
        else:
            # SRT: cue-batch chunker (meta carries cue_batches + line_length)
            chunks = SrtChunker.chunk(cues, config)

        # 3. Seed glossary (no translation for "none" strategy)
        glossary = self._extractor.extract(
            " ".join(c.source_text for c in chunks), config
        )

        # 4. Build Job entity
        now = datetime.now(UTC)
        job = Job(
            id=str(uuid.uuid4()),
            config=config,
            source_path=source_path,
            status=JobStatus.CREATED,
            total_chunks=len(chunks),
            completed_chunks=0,
            cost_usd=0.0,
            created_at=now,
            updated_at=now,
        )

        # 5. Persist everything
        self._checkpoint.save_job(job)
        for chunk in chunks:
            self._checkpoint.save_chunk(job.id, chunk)
        self._checkpoint.save_glossary(job.id, glossary)

        # 6. Initialize cancel flag for this job
        self._cancel_flags[job.id] = threading.Event()

        return job

    # ------------------------------------------------------------------
    # estimate_cost
    # ------------------------------------------------------------------

    def estimate_cost(self, job_id: str) -> CostEstimate:
        """Return a CostEstimate for the PENDING chunks of a job.

        No provider translation call is made.

        Args:
            job_id: ID of the job to estimate.

        Returns:
            CostEstimate with input_tokens, output_tokens, usd, within_budget.

        Raises:
            JobNotFoundError: if job_id is not in the checkpoint store.
        """
        job = self._load_job_or_raise(job_id)
        chunks = self._checkpoint.load_chunks(job_id)
        return self._cost_est.estimate(job, chunks, job.config)

    # ------------------------------------------------------------------
    # run_job
    # ------------------------------------------------------------------

    def run_job(
        self,
        job_id: str,
        out_path: str,
        on_progress: ProgressCallback | None = None,
    ) -> Job:
        """Execute all PENDING chunks sequentially; write output on completion.

        Args:
            job_id:      ID of the job to run (must be in CREATED or PAUSED state).
            out_path:    Destination file path for the translated output.
            on_progress: Optional callback called after each chunk completes.

        Returns:
            Final Job with status=DONE (or PAUSED if budget exceeded, etc.).

        Raises:
            JobStateError:   if the job is already RUNNING.
            JobNotFoundError: if job_id is not found.
            BudgetExceeded:  if a chunk would exceed the configured budget.
        """
        job = self._load_job_or_raise(job_id)

        # Guard: run_job only accepts CREATED or PAUSED
        if job.status == JobStatus.RUNNING:
            raise JobStateError(job_id=job_id, current_status=str(job.status))
        if job.status not in (JobStatus.CREATED, JobStatus.PAUSED):
            raise JobStateError(job_id=job_id, current_status=str(job.status))

        return self._execute_job(job, out_path=out_path, on_progress=on_progress)

    # ------------------------------------------------------------------
    # resume_job
    # ------------------------------------------------------------------

    def resume_job(
        self,
        job_id: str,
        out_path: str,
        on_progress: ProgressCallback | None = None,
    ) -> Job:
        """Resume a job from PAUSED, CANCELLED, or a crashed RUNNING state.

        Like run_job but explicitly accepts PAUSED and CANCELLED jobs.
        DONE chunks are skipped; their cost is NOT re-charged.

        A job left in RUNNING (e.g. a previous run crashed or the process was
        killed mid-translation) is treated as resumable: this is a single-user
        engine, so a persisted RUNNING status at resume time means the prior run
        did not finish. It is normalized to PAUSED and re-executed.

        Args:
            job_id:      ID of the job to resume.
            out_path:    Destination file path for the translated output.
            on_progress: Optional progress callback.

        Returns:
            Final Job.

        Raises:
            JobNotFoundError: if job_id is not found.
        """
        job = self._load_job_or_raise(job_id)

        # A stuck RUNNING job means a prior run crashed — recover it as resumable.
        if job.status == JobStatus.RUNNING:
            job = job.model_copy(update={"status": JobStatus.PAUSED})

        return self._execute_job(job, out_path=out_path, on_progress=on_progress)

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(self, job_id: str) -> Job:
        """Return the current persisted state of a job.

        Args:
            job_id: ID of the job.

        Returns:
            Job as stored in the checkpoint.

        Raises:
            JobNotFoundError: if job_id is not found.
        """
        return self._load_job_or_raise(job_id)

    # ------------------------------------------------------------------
    # failed_chunk_indices
    # ------------------------------------------------------------------

    def failed_chunk_indices(self, job_id: str) -> list[int]:
        """Return the sorted list of chunk indices currently in FAILED status.

        Pure pass-through over per-chunk status already persisted by the
        checkpoint store — no checkpoint schema change. Used by the CLI to
        surface a skip report after a continue_on_error run.

        Args:
            job_id: ID of the job.

        Returns:
            Sorted (0-based) list of FAILED chunk indices. Empty if none.

        Raises:
            JobNotFoundError: if job_id is not found.
        """
        self._load_job_or_raise(job_id)
        chunks = self._checkpoint.load_chunks(job_id)
        return sorted(c.index for c in chunks if c.status == ChunkStatus.FAILED)

    # ------------------------------------------------------------------
    # get_glossary / update_glossary
    # ------------------------------------------------------------------

    def get_glossary(self, job_id: str) -> Glossary:
        """Return the current persisted glossary for a job.

        Args:
            job_id: ID of the job.

        Returns:
            Glossary (may be empty if NullGlossaryExtractor was used).

        Raises:
            JobNotFoundError: if job_id is not found.
        """
        self._load_job_or_raise(job_id)  # validate existence
        return self._checkpoint.load_glossary(job_id)

    def update_glossary(self, job_id: str, entries: list[GlossaryEntry]) -> Glossary:
        """Replace the current glossary entries with the provided list.

        Merges: provided entries are upserted — existing entries not in the
        list are preserved. The full merged Glossary is persisted and returned.

        Args:
            job_id:  ID of the job.
            entries: List of GlossaryEntry objects to upsert.

        Returns:
            Updated Glossary as persisted.

        Raises:
            JobNotFoundError: if job_id is not found.
        """
        self._load_job_or_raise(job_id)
        existing = self._checkpoint.load_glossary(job_id)

        # Build a dict of existing entries keyed by term
        entry_map: dict[str, GlossaryEntry] = {e.term: e for e in existing.entries}

        # Upsert provided entries (caller's version wins)
        for entry in entries:
            entry_map[entry.term] = entry

        updated = Glossary(entries=list(entry_map.values()))
        self._checkpoint.save_glossary(job_id, updated)
        return updated

    # ------------------------------------------------------------------
    # cancel_job
    # ------------------------------------------------------------------

    def cancel_job(self, job_id: str) -> None:
        """Signal cooperative cancellation for a running (or not-yet-started) job.

        If the job is currently running, the orchestrator will finish the
        current chunk and then stop. If the job is not running, its status
        is set to CANCELLED immediately.

        Args:
            job_id: ID of the job to cancel.

        Raises:
            JobNotFoundError: if job_id is not found.
        """
        job = self._load_job_or_raise(job_id)

        # Ensure a cancel flag exists for this job
        if job_id not in self._cancel_flags:
            self._cancel_flags[job_id] = threading.Event()

        # Set the cooperative flag
        self._cancel_flags[job_id].set()

        # If the job is not currently RUNNING, mark it CANCELLED directly
        if job.status != JobStatus.RUNNING:
            cancelled_job = job.model_copy(
                update={"status": JobStatus.CANCELLED, "updated_at": datetime.now(UTC)}
            )
            self._checkpoint.save_job(cancelled_job)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_job_or_raise(self, job_id: str) -> Job:
        """Load job from checkpoint or raise JobNotFoundError."""
        job = self._checkpoint.load_job(job_id)
        if job is None:
            raise JobNotFoundError(job_id=job_id)
        return job

    def _execute_job(
        self,
        job: Job,
        *,
        out_path: str,
        on_progress: ProgressCallback | None,
    ) -> Job:
        """Internal: set RUNNING, delegate to orchestrator, write output.

        This is the shared implementation for run_job and resume_job.
        """
        # Create a FRESH cancel flag for each execution (clears any prior cancel).
        # This allows resume after a previous cancel_job() call.
        fresh_flag = threading.Event()
        self._cancel_flags[job.id] = fresh_flag
        cancel_flag = fresh_flag

        # Load current state
        chunks = self._checkpoint.load_chunks(job.id)
        glossary = self._checkpoint.load_glossary(job.id)

        # Persist RUNNING status so external status() calls see it.
        # The orchestrator receives the pre-RUNNING job object (it guards
        # against RUNNING itself to prevent double-entry).
        now = datetime.now(UTC)
        running_job = job.model_copy(
            update={"status": JobStatus.RUNNING, "updated_at": now}
        )
        self._checkpoint.save_job(running_job)

        # Wire orchestrator — pass the PRE-RUNNING job so the orchestrator's
        # RUNNING guard does not trip (the guard protects against a job that
        # was ALREADY running when delegated; here we are legitimately starting).
        job_for_orchestrator = job.model_copy(
            update={"updated_at": now}
        )

        # Wire orchestrator
        orchestrator = TranslationOrchestrator(
            provider=self._provider,
            checkpoint=self._checkpoint,
            context_manager=self._ctx,
            cost_estimator=self._cost_est,
        )

        # No-op progress callback if none provided
        _on_progress: ProgressCallback = (
            on_progress if on_progress is not None else (lambda p: None)
        )

        # Run the translation loop. If anything unexpected escapes the
        # orchestrator, never leave the job stranded in RUNNING — persist a
        # resumable PAUSED snapshot (checkpointed chunks are already saved) and
        # re-raise so the caller sees the real error.
        try:
            final_job = orchestrator.run(
                job=job_for_orchestrator,
                chunks=chunks,
                glossary=glossary,
                config=job.config,
                on_progress=_on_progress,
                cancel_flag=cancel_flag,
            )
        except Exception:
            latest = self._checkpoint.load_job(job.id)
            if latest is not None and latest.status == JobStatus.RUNNING:
                self._checkpoint.save_job(
                    latest.model_copy(
                        update={"status": JobStatus.PAUSED, "updated_at": datetime.now(UTC)}
                    )
                )
            raise

        # Write output if job completed successfully
        if final_job.status == JobStatus.DONE:
            writer = self._writers[job.config.source_type]
            done_chunks = self._checkpoint.load_chunks(job.id)
            writer.write(done_chunks, job.source_path, out_path)

        return final_job
