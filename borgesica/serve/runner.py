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
     "total_chunks": ..., "cost_usd": ..., "status": ...}
Consumers must build the completion fraction from "position", not
"chunk_index": an extract job keeps the original book indices, so chunk_index
can exceed total_chunks.
    {"type": "terminal", "status": ..., "error": str | None}

Glossary extraction (a different terminal type, so a client can tell the
end of the pre-translation pass from the end of a translation run):
    {"type": "glossary_progress", "job_id": ..., "batch": int,
     "total_batches": int, "cost_usd": float}
    {"type": "glossary_terminal", "status": "done" | "failed" | "budget_exceeded",
     "error": str | None, "entries": int | None, "conflicts": int | None,
     "cost_usd": float | None}
"cost_usd" in glossary_progress is the job's running total; in
glossary_terminal it is what the pass spent. The outcome fields are null
unless status is "done"; after a failure the job's own cost_usd (GET
/jobs/{id}) still includes every batch already paid for.
"""
from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass

from borgesica.api import TranslatorEngine
from borgesica.domain.errors import BudgetExceeded, JobStateError
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


class JobRunner:
    """Registry of background job-execution threads, at most one active."""

    def __init__(self, engine: TranslatorEngine) -> None:
        self._engine = engine
        self._handles: dict[str, RunHandle] = {}
        self._lock = threading.Lock()
        self._active_job_id: str | None = None

    def _claim(self, job_id: str) -> None:
        """Take the single active slot for *job_id* or raise if it is taken."""
        with self._lock:
            if self._active_job_id is not None:
                raise JobStateError(job_id=job_id, current_status="RUNNING")
            self._active_job_id = job_id

    def _spawn(
        self, job_id: str, kind: str, work: Callable[["queue.Queue[dict]"], dict]
    ) -> None:
        """Run *work* on a thread; it returns the terminal event to emit.

        The slot is released in the worker's finally-block, AFTER the terminal
        event is queued and recorded on the handle.
        """
        events: "queue.Queue[dict]" = queue.Queue()
        handle: RunHandle

        def _worker() -> None:
            try:
                terminal = work(events)
                handle.terminal = terminal
                events.put(terminal)
            finally:
                with self._lock:
                    if self._active_job_id == job_id:
                        self._active_job_id = None

        thread = threading.Thread(target=_worker, daemon=True)
        handle = RunHandle(thread=thread, events=events, kind=kind)
        self._handles[job_id] = handle
        thread.start()

    def start(self, job_id: str, out_path: str, *, resume: bool = False) -> None:
        """Spawn a background thread running (or resuming) *job_id*.

        Raises:
            JobStateError: if another run or extraction is already active
                (design decision #3: single active slot), regardless of which
                job_id.
        """
        self._claim(job_id)

        def _work(events: "queue.Queue[dict]") -> dict:
            def _on_progress(progress: Progress) -> None:
                events.put({"type": "progress", **progress.model_dump()})

            try:
                method = self._engine.resume_job if resume else self._engine.run_job
                final_job = method(job_id, out_path=out_path, on_progress=_on_progress)
                return {"type": "terminal", "status": str(final_job.status), "error": None}
            except Exception as exc:  # noqa: BLE001 — surface any failure as a terminal event
                return {"type": "terminal", "status": "FAILED", "error": str(exc)}

        self._spawn(job_id, TRANSLATION, _work)

    def start_glossary_extraction(self, job_id: str) -> None:
        """Spawn a background thread running the glossary pass for *job_id*.

        Shares the single active slot with translation runs, so the two can
        never overlap. The caller validates the job's state first (app.py
        maps a refusal to 409 before any thread exists); a refusal the engine
        still raises here ends as a "failed" glossary_terminal.

        Raises:
            JobStateError: if a run or another extraction is already active.
        """
        self._claim(job_id)

        def _work(events: "queue.Queue[dict]") -> dict:
            def _on_progress(done: int, total: int, cost_usd: float) -> None:
                events.put(
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
                outcome = self._engine.extract_glossary(job_id, on_progress=_on_progress)
            except BudgetExceeded as exc:
                return _ended("budget_exceeded", str(exc))
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

        An active glossary extraction is NOT cancelled, only joined:
        ``cancel_job`` on a job that is not RUNNING marks it CANCELLED, which
        would take a CREATED job out of the wizard for good. Batches already
        paid for are persisted as they finish, so the sidecar's own kill
        fallback loses at most the call in flight.

        Returns:
            True if no run was active, or it finished within the timeout.
            False if it is still finishing (caller/Tauri falls back to its
            own 5s force-kill).
        """
        with self._lock:
            job_id = self._active_job_id
        if job_id is None:
            return True
        handle = self._handles.get(job_id)
        if handle is None or handle.kind == TRANSLATION:
            self._engine.cancel_job(job_id)
        if handle is None:
            return True
        handle.thread.join(timeout=join_timeout)
        return not handle.thread.is_alive()
