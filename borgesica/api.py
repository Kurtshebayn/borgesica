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
from borgesica.domain.errors import (
    GlossaryEntryRejectedError,
    JobNotFoundError,
    JobStateError,
)
from borgesica.domain.glossary import (
    NullGlossaryExtractor,
    normalize_term,
    sanitize_glossary,
)
from borgesica.domain.models import (
    ChunkStatus,
    CostEstimate,
    Glossary,
    GlossaryEntry,
    GlossaryVotes,
    Job,
    JobConfig,
    JobStatus,
    SourceType,
)
from borgesica.domain.orchestrator import TranslationOrchestrator
from borgesica.domain.ports import (
    CheckpointStore,
    CorpusStore,
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
        corpus_store:  Optional CorpusStore for engine-wide corpus capture
            (design decision #6); default None → zero behavior change.
        provider_name: Provenance label passed through to the orchestrator
            (design decision #6); default "unknown".
    """

    def __init__(
        self,
        *,
        provider: TranslationProvider,
        checkpoint: CheckpointStore,
        readers: dict[SourceType, DocumentReader],
        writers: dict[SourceType, DocumentWriter],
        extractor: GlossaryExtractor | None = None,
        corpus_store: CorpusStore | None = None,  # type: ignore[type-arg]
        provider_name: str = "unknown",
    ) -> None:
        self._provider = provider
        self._checkpoint = checkpoint
        self._readers = readers
        self._writers = writers
        self._extractor = extractor if extractor is not None else NullGlossaryExtractor()
        self._corpus_store = corpus_store
        self._provider_name = provider_name

        # Domain services (no adapter deps)
        self._ctx = ContextManager()
        self._cost_est = CostEstimator(provider=provider, context_manager=self._ctx)

        # Per-job cancel flags: {job_id: threading.Event}
        self._cancel_flags: dict[str, threading.Event] = {}
        # Guards all reads/writes of self._cancel_flags (REL-002): without
        # this, a cancel_job() call racing concurrently with run_job's
        # _execute_job start can have its Event.set() silently discarded when
        # _execute_job installs a fresh Event for the same job_id.
        self._cancel_flags_lock = threading.Lock()

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

        # 2b. Extract mode: keep only the requested window of chunks. Done here
        # — before glossary seeding — so the extract is the whole job: the
        # glossary is seeded from the window alone (that is the cost saving),
        # and estimate_cost / run_job / the writer see a normal, complete job.
        # Chunks keep their ORIGINAL indices: save_chunk is keyed by
        # (job_id, chunk.index) and load_chunks orders by it, so nothing needs a
        # 0-based run, and the job records where in the book it came from.
        if config.extract_offset or config.extract_chunks is not None:
            if config.extract_offset >= len(chunks):
                raise ValueError(
                    f"extract offset {config.extract_offset} is past the last "
                    f"chunk (source has {len(chunks)})"
                )
            start = config.extract_offset
            end = (
                start + config.extract_chunks
                if config.extract_chunks is not None
                else None
            )
            chunks = chunks[start:end]

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
        # The floor bound prices the glossary/summary the job carries TODAY, so
        # a resumed job with an accumulated glossary is not estimated as if it
        # were starting from empty.
        return self._cost_est.estimate(
            job,
            chunks,
            job.config,
            glossary=self._clean_glossary(job_id),
            summary=self._checkpoint.load_summary(job_id),
        )

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
    # best_effort_chunk_indices
    # ------------------------------------------------------------------

    def best_effort_chunk_indices(self, job_id: str) -> list[int]:
        """Return the sorted list of DONE chunk indices with passed_validation=False.

        Pure pass-through over per-chunk state already persisted by the
        checkpoint store (design decision #9). Used by the CLI end-of-run
        summary and (v1) the serve API's job status payload.

        Args:
            job_id: ID of the job.

        Returns:
            Sorted (0-based) list of best-effort chunk indices. Empty if none.

        Raises:
            JobNotFoundError: if job_id is not found.
        """
        self._load_job_or_raise(job_id)
        chunks = self._checkpoint.load_chunks(job_id)
        return sorted(
            c.index
            for c in chunks
            if c.status == ChunkStatus.DONE and not c.passed_validation
        )

    # ------------------------------------------------------------------
    # get_glossary / update_glossary
    # ------------------------------------------------------------------

    def _clean_glossary(self, job_id: str) -> Glossary:
        """Load the persisted glossary and apply the hygiene rules to it.

        Glossaries written before those rules existed still hold case
        duplicates and reversed entries, and a FINISHED job never merges
        again, so nothing else would ever repair them. Cleaning on load makes
        the repair reach every reader — review, estimate and resume alike —
        without a migration.

        Deliberately does NOT write back: persisting is the job of the calls
        that already persist. See ``update_glossary``.
        """
        cleaned, _dropped = sanitize_glossary(self._checkpoint.load_glossary(job_id))
        return cleaned

    def get_glossary(self, job_id: str) -> Glossary:
        """Return the current persisted glossary for a job.

        The result is normalised and deduplicated: case-only duplicate terms
        are collapsed and entries pointing the wrong way are removed. What is
        stored is left untouched — a getter does not rewrite the database.

        Args:
            job_id: ID of the job.

        Returns:
            Glossary (may be empty if NullGlossaryExtractor was used).

        Raises:
            JobNotFoundError: if job_id is not found.
        """
        self._load_job_or_raise(job_id)  # validate existence
        return self._clean_glossary(job_id)

    def glossary_repairs(self, job_id: str) -> list[GlossaryEntry]:
        """Return the stored entries that ``get_glossary`` removes on read.

        Exists so a reader can explain the difference between what is stored
        and what it shows. Without it, ``glossary show`` printing 471 of 491
        entries looks like data loss rather than repair.

        Args:
            job_id: ID of the job.

        Returns:
            The removed entries, in the order they were stored. Empty when the
            stored glossary is already clean.

        Raises:
            JobNotFoundError: if job_id is not found.
        """
        self._load_job_or_raise(job_id)
        _cleaned, dropped = sanitize_glossary(self._checkpoint.load_glossary(job_id))
        return dropped

    def update_glossary(self, job_id: str, entries: list[GlossaryEntry]) -> Glossary:
        """Replace the current glossary entries with the provided list.

        Merges: provided entries are upserted — existing entries not in the
        list are preserved. The full merged Glossary is persisted and returned.

        This is the path a human uses to curate terminology by hand, so it is
        also the path through which every defect the glossary rules exist to
        prevent could re-enter. The upsert key is the NORMALISED term: editing
        "alupi" updates an existing "Alupi" instead of creating a second
        entry, which is exactly the bug ``merge_additions`` had before it
        matched on the same key. The result is sanitised before it is stored.

        A hand edit the direction guard would reject can still be forced by
        marking it ``locked=True`` — locking states human intent, and no
        inferred rule overrides it. Anything else the caller supplied that the
        rules discard raises ``GlossaryEntryRejectedError`` and persists
        nothing: repairing contamination already in the glossary is quiet
        maintenance, but silently dropping an edit someone just asked for is
        not.

        Args:
            job_id:  ID of the job.
            entries: List of GlossaryEntry objects to upsert.

        Returns:
            Updated Glossary as persisted.

        Raises:
            JobNotFoundError: if job_id is not found.
            GlossaryEntryRejectedError: if any supplied entry is discarded by
                the hygiene rules. Nothing is persisted in that case.
        """
        self._load_job_or_raise(job_id)
        existing = self._clean_glossary(job_id)

        # Keyed by normalised term so a case or spacing variant edits the
        # entry it refers to rather than duplicating it. Replacing a key keeps
        # its original position, so editing a term does not reshuffle the
        # glossary around it.
        entry_map: dict[str, GlossaryEntry] = {
            normalize_term(e.term).casefold(): e for e in existing.entries
        }

        # Upsert provided entries (caller's version wins)
        for entry in entries:
            entry_map[normalize_term(entry.term).casefold()] = entry

        updated, _dropped = sanitize_glossary(
            Glossary(entries=list(entry_map.values()))
        )

        # Only the caller's OWN entries count as a rejection. Compared by
        # normalised key, so collapsing two case variants the caller supplied
        # is deduplication rather than a refusal.
        surviving = {normalize_term(e.term).casefold() for e in updated.entries}
        rejected = [
            e for e in entries if normalize_term(e.term).casefold() not in surviving
        ]
        if rejected:
            raise GlossaryEntryRejectedError(job_id=job_id, rejected=rejected)

        self._checkpoint.save_glossary(job_id, updated)

        # A hand edit SETTLES the term. Leaving its votes alive would let the
        # next proposal that reaches quorum silently replace the rendering the
        # human just chose — inference does not get to overrule a person, the
        # same reason drop_reversed_entries never touches a locked entry.
        votes = self._checkpoint.load_votes(job_id)
        edited = {normalize_term(e.term).casefold() for e in entries}
        remaining = {
            term: proposals
            for term, proposals in votes.by_term.items()
            if term not in edited
        }
        if len(remaining) != len(votes.by_term):
            self._checkpoint.save_votes(job_id, GlossaryVotes(by_term=remaining))

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

        # Ensure a cancel flag exists for this job, and set it. Guarded by
        # _cancel_flags_lock so this can never interleave with _execute_job
        # installing a fresh Event for the same job_id (REL-002).
        with self._cancel_flags_lock:
            if job_id not in self._cancel_flags:
                self._cancel_flags[job_id] = threading.Event()
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
        # Install this execution's cancel flag (REL-002).
        #
        # Ordinarily a FRESH (unset) Event is installed, clearing any prior
        # cancel state — this is what lets resume_job() run to completion
        # after a previous, already-persisted CANCELLED status (deliberate
        # resume-after-cancel: `job.status == JobStatus.CANCELLED` here).
        #
        # But if the job was NOT already CANCELLED when loaded (i.e. this is
        # a fresh run/resume of a CREATED/PAUSED job) and the existing flag
        # is already set, a concurrent cancel_job() call raced with this
        # run's start. Preserve that signal instead of silently discarding
        # it by installing a brand-new unset Event over it.
        with self._cancel_flags_lock:
            existing_flag = self._cancel_flags.get(job.id)
            if job.status != JobStatus.CANCELLED and existing_flag is not None and existing_flag.is_set():
                cancel_flag = existing_flag
            else:
                cancel_flag = threading.Event()
                self._cancel_flags[job.id] = cancel_flag

        # Load current state
        chunks = self._checkpoint.load_chunks(job.id)
        # Cleaned on load so a resumed job starts from a repaired glossary
        # instead of waiting for its first mid-run merge to fix one.
        glossary = self._clean_glossary(job.id)

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
            provider_name=self._provider_name,
            corpus_store=self._corpus_store,
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
