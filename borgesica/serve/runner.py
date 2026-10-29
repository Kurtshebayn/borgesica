"""JobRunner — background-thread execution + progress bridging for
borgesica.serve (design decisions #3/#4).

One threading.Thread per run/resume or glossary extraction; ONE active slot
globally — a second run/resume/extraction attempt while any of them is in
flight is rejected with JobStateError (409 via app.py's existing error
mapping), regardless of job id. This mirrors the single-user desktop
constraint already relied on elsewhere (sqlite checkpoint pattern is
sequential), and it is what keeps an extraction from overlapping a
translation run: the engine's extraction pass re-checks the job between
batches but the store has no compare-and-set, so the runner is the lock.

Progress is bridged from the worker thread to a queue.Queue of plain dicts
(the SSE event shape), consumed by sse.py's sync generator.

Translation run/resume:
    {"type": "progress", "job_id": ..., "chunk_index": ..., "position": ...,
     "completed": ..., "total_chunks": ..., "cost_usd": ..., "status": ...}
Consumers must build the completion fraction from "completed", not
"chunk_index" or "position": an extract job keeps the original book indices,
so chunk_index can exceed total_chunks, and a parallel run finishes chunks
out of order, so position moves back and forth. "completed" (chunks of the
run finished so far, already-DONE ones included) only grows.
    {"type": "terminal", "status": ..., "error": str | None}

Glossary extraction (a different terminal type, so a client can tell the
end of the pre-translation pass from the end of a translation run):
    {"type": "glossary_progress", "job_id": ..., "batch": int,
     "total_batches": int, "cost_usd": float}
    {"type": "glossary_terminal",
     "status": "done" | "failed" | "budget_exceeded" | "stopped",
     "error": str | None, "entries": int | None, "conflicts": int | None,
     "cost_usd": float | None}
"cost_usd" in glossary_progress is the job's running total; in
glossary_terminal it is what the pass spent. The outcome fields are null
unless status is "done"; after a failure or a stop the job's own cost_usd
(GET /jobs/{id}) still includes every batch already paid for.

An extraction is stopped cooperatively (``request_extraction_stop``, used by
POST /jobs/{id}/glossary/extract/stop and by shutdown): the pass checks a
stop flag before each batch call and ends with status "stopped", leaving the
job CREATED.
"""
from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from borgesica.api import TranslatorEngine
from borgesica.domain.errors import BudgetExceeded, ExtractionStopped, JobStateError
from borgesica.domain.models import Progress

# RunHandle.kind values.
TRANSLATION = "translation"
GLOSSARY_EXTRACTION = "glossary_extraction"

# Event types that end a stream (sse.py stops reading after one).
TERMINAL_EVENT_TYPES = frozenset({"terminal", "glossary_terminal"})


@dataclass
class RunHandle:
    """One background execution's thread + its progress/terminal event queue."""

    thread: threading.Thread
    events: "queue.Queue[dict]"
    kind: str = TRANSLATION
    # The terminal event once the worker has finished, kept so a client that
    # subscribes after the end can still be told how it ended.
    terminal: dict | None = None
    # Set to ask a glossary extraction to stop before its next batch call.
    stop: threading.Event = field(default_factory=threading.Event)
    # Where a translation run writes its output; None for an extraction.
    out_path: str | None = None


class JobRunner:
    """Registry of background job-execution threads, at most one active."""

    def __init__(self, engine: TranslatorEngine) -> None:
        self._engine = engine
        self._handles: dict[str, RunHandle] = {}
        self._lock = threading.Lock()
        self._active_job_id: str | None = None

    def _spawn(
        self,
        job_id: str,
        kind: str,
        work: Callable[[RunHandle], dict],
        out_path: str | None = None,
    ) -> None:
        """Take the single active slot for *job_id* and run *work* on a thread;
        *work* returns the terminal event to emit.

        Claiming the slot, registering the new handle and starting its thread
        happen under one lock hold, so whoever reads the slot (shutdown, the
        stop request) always finds the handle of THIS execution — never the
        job's previous one — and a thread that has started.

        The slot is released in the worker's finally-block, AFTER the terminal
        event is queued and recorded on the handle.

        Raises:
            JobStateError: if the slot is already taken, whichever job holds it.
        """
        handle: RunHandle

        def _worker() -> None:
            try:
                terminal = work(handle)
                handle.terminal = terminal
                handle.events.put(terminal)
            finally:
                with self._lock:
                    if self._active_job_id == job_id:
                        self._active_job_id = None

        handle = RunHandle(
            thread=threading.Thread(target=_worker, daemon=True),
            events=queue.Queue(),
            kind=kind,
            out_path=out_path,
        )
        with self._lock:
            if self._active_job_id is not None:
                raise JobStateError(job_id=job_id, current_status="RUNNING")
            self._active_job_id = job_id
            self._handles[job_id] = handle
            # Starting under the lock is safe: the worker takes the lock only
            # in its finally-block, which simply waits for this to return.
            handle.thread.start()

    def start(self, job_id: str, out_path: str, *, resume: bool = False) -> None:
        """Spawn a background thread running (or resuming) *job_id*.

        Raises:
            JobStateError: if another run or extraction is already active
                (design decision #3: single active slot), regardless of which
                job_id.
        """

        def _work(handle: RunHandle) -> dict:
            def _on_progress(progress: Progress) -> None:
                handle.events.put({"type": "progress", **progress.model_dump()})

            try:
                method = self._engine.resume_job if resume else self._engine.run_job
                final_job = method(job_id, out_path=out_path, on_progress=_on_progress)
                return {"type": "terminal", "status": str(final_job.status), "error": None}
            except Exception as exc:  # noqa: BLE001 — surface any failure as a terminal event
                return {"type": "terminal", "status": "FAILED", "error": str(exc)}

        self._spawn(job_id, TRANSLATION, _work, out_path=out_path)

    def out_path_for(self, job_id: str) -> str | None:
        """Where job_id's latest translation run in this process writes its
        output; None if none has started (an extraction has no output)."""
        handle = self._handles.get(job_id)
        return handle.out_path if handle is not None else None

    def start_glossary_extraction(self, job_id: str) -> None:
        """Spawn a background thread running the glossary pass for *job_id*.

        Shares the single active slot with translation runs, so the two can
        never overlap. The caller validates the job's state first (app.py
        maps a refusal to 409 before any thread exists); a refusal the engine
        still raises here ends as a "failed" glossary_terminal. A stop asked
        for with ``request_extraction_stop`` ends it as "stopped".

        Raises:
            JobStateError: if a run or another extraction is already active.
        """

        def _work(handle: RunHandle) -> dict:
            def _on_progress(done: int, total: int, cost_usd: float) -> None:
                handle.events.put(
                    {
                        "type": "glossary_progress",
                        "job_id": job_id,
                        "batch": done,
                        "total_batches": total,
                        "cost_usd": cost_usd,
                    }
                )

            def _ended(status: str, error: str) -> dict:
                return {
                    "type": "glossary_terminal",
                    "status": status,
                    "error": error,
                    "entries": None,
                    "conflicts": None,
                    "cost_usd": None,
                }

            try:
                outcome = self._engine.extract_glossary(
                    job_id, on_progress=_on_progress, should_stop=handle.stop.is_set
                )
            except BudgetExceeded as exc:
                return _ended("budget_exceeded", str(exc))
            except ExtractionStopped as exc:
                return _ended("stopped", str(exc))
            except Exception as exc:  # noqa: BLE001 — surface any failure as a terminal event
                return _ended("failed", str(exc))
            return {
                "type": "glossary_terminal",
                "status": "done",
                "error": None,
                "entries": outcome.entries,
                "conflicts": outcome.conflicts,
                "cost_usd": outcome.cost_usd,
            }

        self._spawn(job_id, GLOSSARY_EXTRACTION, _work)

    def request_extraction_stop(self, job_id: str) -> bool:
        """Ask job_id's glossary extraction to stop before its next batch call.

        Returns:
            True if a glossary extraction of job_id holds the active slot (the
            stop is then on its way; the pass ends with a "stopped"
            glossary_terminal, or "done" if its last call was already made).
            False if it does not (no execution, a translation run, or a pass
            that already released the slot) — nothing is touched.
        """
        with self._lock:
            if self._active_job_id != job_id:
                return False
            handle = self._handles[job_id]
            if handle.kind != GLOSSARY_EXTRACTION:
                return False
            handle.stop.set()
            return True

    def is_active(self, job_id: str) -> bool:
        """True while job_id's background run is registered as active.

        Set synchronously in start() BEFORE the worker thread is started (and
        therefore before the worker has had a chance to persist RUNNING), and
        cleared only once the worker's finally-block runs. This is the
        authoritative "is a run currently in flight for this job_id" signal —
        unlike a freshly-read persisted status, it cannot lag behind
        start()'s caller (REL-001).
        """
        with self._lock:
            return self._active_job_id == job_id

    def queue_for(self, job_id: str) -> "queue.Queue[dict]":
        """Return the progress/terminal event queue for a currently-active run.

        Raises:
            JobStateError: if job_id has no active run registered.
        """
        handle = self._handles.get(job_id)
        if handle is None:
            raise JobStateError(job_id=job_id, current_status="NOT_RUNNING")
        return handle.events

    def finished_glossary_terminal(self, job_id: str) -> dict | None:
        """The terminal event of job_id's latest execution, if that was a
        glossary extraction that has finished; None otherwise.

        The job stays CREATED whatever the pass did, so its stored status
        cannot tell a late subscriber how the pass ended — this can.
        """
        handle = self._handles.get(job_id)
        if handle is None or handle.kind != GLOSSARY_EXTRACTION:
            return None
        return handle.terminal

    def shutdown(self, join_timeout: float = 5.0) -> bool:
        """Cancel the active run (if any) and wait up to *join_timeout* for
        it to finish (sidecar-lifecycle design: "sets cancel flag, joins
        thread"). Cancellation only takes effect between chunks, so a run
        blocked mid-chunk may not finish within the timeout.

        An active glossary extraction is NOT cancelled but stopped (the same
        flag as ``request_extraction_stop``): ``cancel_job`` on a job that is
        not RUNNING marks it CANCELLED, which would take a CREATED job out of
        the wizard for good. The stop keeps the pass from starting a batch
        the sidecar's own kill fallback would then lose after paying for it;
        batches already paid for are persisted as they finish.

        Returns:
            True if no run was active, or it finished within the timeout.
            False if it is still finishing (caller/Tauri falls back to its
            own 5s force-kill).
        """
        # The slot and its handle are read together: _spawn registers both
        # under the same lock, so this handle is the active execution's own.
        with self._lock:
            job_id = self._active_job_id
            if job_id is None:
                return True
            handle = self._handles[job_id]
            if handle.kind == GLOSSARY_EXTRACTION:
                handle.stop.set()
        if handle.kind == TRANSLATION:
            self._engine.cancel_job(job_id)
        handle.thread.join(timeout=join_timeout)
        return not handle.thread.is_alive()
