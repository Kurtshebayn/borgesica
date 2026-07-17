"""SSE formatting + progress-to-stream bridging for borgesica.serve
(design decision #4).

`on_progress` (worker thread) -> queue.Queue (JobRunner) -> this sync
generator -> StreamingResponse (SSE). Heartbeat via `get(timeout=15)` keeps
idle connections alive without emitting noise events; a terminal event
`{type, status, error}` closes the stream.

Reconnection (spec: "Reconnection after drop"): if the job is not currently
RUNNING when a client connects (already finished, or never started), a
single terminal event reflecting the persisted status is emitted
immediately — equivalent to a status poll, without restarting the job or
hanging on an empty queue.
"""
from __future__ import annotations

import json
import queue as _queue
from collections.abc import Iterator

from borgesica.api import TranslatorEngine
from borgesica.domain.models import JobStatus
from borgesica.serve.runner import JobRunner

_HEARTBEAT_TIMEOUT = 15.0


def format_sse(event: dict) -> str:
    """Format one JSON-serializable event dict as an SSE `data:` line."""
    return f"data: {json.dumps(event)}\n\n"


def sse_event_stream(engine: TranslatorEngine, runner: JobRunner, job_id: str) -> Iterator[str]:
    """Yield SSE lines for job_id's progress + terminal event.

    Caller (app.py route) MUST validate job_id exists (JobNotFoundError ->
    404) BEFORE calling this — exceptions raised lazily inside a streaming
    generator are not caught by FastAPI's exception_handler machinery.
    """
    # Check the runner's authoritative in-memory active-run registry FIRST
    # (REL-001): runner.start() marks a job active synchronously, before its
    # worker thread is started, so this can never lag behind a freshly-read
    # persisted status the way `engine.status(job_id)` can. Without this
    # check-order, a client connecting in the window after POST /run returns
    # but before the worker has persisted RUNNING would see a stale
    # non-RUNNING status and get a premature terminal event instead of the
    # job's real progress/terminal stream.
    if runner.is_active(job_id):
        events = runner.queue_for(job_id)
    else:
        job = engine.status(job_id)
        if job.status != JobStatus.RUNNING:
            yield format_sse({"type": "terminal", "status": str(job.status), "error": None})
            return
        events = runner.queue_for(job_id)
    while True:
        try:
            item = events.get(timeout=_HEARTBEAT_TIMEOUT)
        except _queue.Empty:
            yield ": heartbeat\n\n"
            continue
        yield format_sse(item)
        if item["type"] == "terminal":
            break
