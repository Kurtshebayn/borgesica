"""JobRunner — background-thread execution + progress bridging for
borgesica.serve (design decisions #3/#4).

One threading.Thread per run/resume; single active run globally — a second
run/resume attempt while one is in flight is rejected with JobStateError
(409 via app.py's existing error mapping), regardless of job id. This
mirrors the single-user desktop constraint already relied on elsewhere
(sqlite checkpoint pattern is sequential).

Progress is bridged from the worker thread to a queue.Queue of plain dicts
(the SSE event shape), consumed by sse.py's sync generator:
    {"type": "progress", "job_id": ..., "chunk_index": ..., "position": ...,
     "total_chunks": ..., "cost_usd": ..., "status": ...}
Consumers must build the completion fraction from "position", not
"chunk_index": an extract job keeps the original book indices, so chunk_index
can exceed total_chunks.
    {"type": "terminal", "status": ..., "error": str | None}
"""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass

from borgesica.api import TranslatorEngine
from borgesica.domain.errors import JobStateError
from borgesica.domain.models import Progress


@dataclass
class RunHandle:
    """One background execution's thread + its progress/terminal event queue."""

    thread: threading.Thread
    events: "queue.Queue[dict]"


class JobRunner:
    """Registry of background job-execution threads, at most one active."""

    def __init__(self, engine: TranslatorEngine) -> None:
        self._engine = engine
        self._handles: dict[str, RunHandle] = {}
        self._lock = threading.Lock()
        self._active_job_id: str | None = None

    def start(self, job_id: str, out_path: str, *, resume: bool = False) -> None:
        """Spawn a background thread running (or resuming) *job_id*.

        Raises:
            JobStateError: if another run is already active (design decision
                #3: single active run), regardless of which job_id.
        """
        with self._lock:
            if self._active_job_id is not None:
                raise JobStateError(job_id=job_id, current_status="RUNNING")
            self._active_job_id = job_id

        events: "queue.Queue[dict]" = queue.Queue()

        def _on_progress(progress: Progress) -> None:
            events.put({"type": "progress", **progress.model_dump()})

        def _worker() -> None:
            try:
                method = self._engine.resume_job if resume else self._engine.run_job
                final_job = method(job_id, out_path=out_path, on_progress=_on_progress)
                events.put(
                    {"type": "terminal", "status": str(final_job.status), "error": None}
                )
            except Exception as exc:  # noqa: BLE001 — surface any failure as a terminal event
                events.put({"type": "terminal", "status": "FAILED", "error": str(exc)})
            finally:
                with self._lock:
                    if self._active_job_id == job_id:
                        self._active_job_id = None

        thread = threading.Thread(target=_worker, daemon=True)
        self._handles[job_id] = RunHandle(thread=thread, events=events)
        thread.start()

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

    def shutdown(self, join_timeout: float = 5.0) -> bool:
        """Cancel the active run (if any) and wait up to *join_timeout* for
        it to finish (sidecar-lifecycle design: "sets cancel flag, joins
        thread"). Cancellation only takes effect between chunks, so a run
        blocked mid-chunk may not finish within the timeout.

        Returns:
            True if no run was active, or it finished within the timeout.
            False if it is still finishing (caller/Tauri falls back to its
            own 5s force-kill).
        """
        with self._lock:
            job_id = self._active_job_id
        if job_id is None:
            return True
        self._engine.cancel_job(job_id)
        handle = self._handles.get(job_id)
        if handle is None:
            return True
        handle.thread.join(timeout=join_timeout)
        return not handle.thread.is_alive()
