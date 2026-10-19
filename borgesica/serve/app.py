"""FastAPI app factory for borgesica.serve (design decision #1).

`create_app(engine)` binds one already-constructed TranslatorEngine instance
to routes, enabling TestClient injection with a fake engine in tests. This
module MUST NOT construct adapters itself.

T5 landed create/estimate/status/glossary(get,update). T6 adds run/resume
(background thread via JobRunner), the SSE progress stream, cancellation,
and graceful shutdown. The glossary extraction pass (POST
/jobs/{id}/glossary/extract) runs through the same JobRunner slot.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import Request

from borgesica.api import TranslatorEngine
from borgesica.domain.errors import (
    BorgesicaError,
    GlossaryEntryRejectedError,
    JobNotFoundError,
    JobStateError,
)
from borgesica.domain.models import GlossaryReview, JobConfig, JobStatus, SourceType
from borgesica.serve.runner import JobRunner
from borgesica.serve.schemas import (
    CreateJobRequest,
    EstimateResponse,
    GlossaryResponse,
    GlossaryUpdateRequest,
    JobResponse,
    RunRequest,
    job_to_response,
)
from borgesica.serve.sse import sse_event_stream

# Design decision #5: bind 127.0.0.1 only. Shared by __main__.py's serve
# subcommand and tests — the CLI parser also has no --host flag, so a
# non-loopback bind can never be configured (design threat matrix).
SERVE_HOST = "127.0.0.1"

# Serve-api spec: "Reject PDF" — enforced independently of the CLI (which
# still allows PDF input with a warning). PDF is intentionally absent here.
_EXT_TO_SOURCE_TYPE: dict[str, SourceType] = {
    ".srt": SourceType.SRT,
    ".epub": SourceType.EPUB,
}


def _source_type_for_serve(source_path: str) -> SourceType | None:
    """SourceType for source_path's extension, or None if unsupported/rejected."""
    return _EXT_TO_SOURCE_TYPE.get(Path(source_path).suffix.lower())


def create_app(
    engine: TranslatorEngine,
    *,
    shutdown_join_timeout: float = 5.0,
    session_token: str | None = None,
) -> FastAPI:
    """Build the FastAPI app wired to *engine*.

    Args:
        engine: Already-constructed TranslatorEngine (composition root stays
            outside this module).
        shutdown_join_timeout: Seconds POST /shutdown waits for an active run
            to finish after cancelling it (design: sidecar lifecycle "kill
            after 5s" fallback). Overridable for deterministic tests.
        session_token: Per-session shared secret (RISK-001/002/003) required
            on every request via the `X-Borgesica-Token` header — including
            the SSE stream, which the desktop app reads with `fetch` (not
            native `EventSource`) precisely so the token rides the header and
            never appears in a URL/query string. `None` (the default) disables
            auth entirely: this preserves the unauthenticated contract every
            pre-existing T5/T6 test relies on. Real production boot
            (`__main__.py`'s `serve` subcommand with `--key-stdin`) always
            supplies a real token derived from the same trusted stdin
            handshake as the provider API key.
    """

    async def _require_session_token(
        request: Request,
        x_borgesica_token: str | None = Header(default=None, alias="X-Borgesica-Token"),
    ) -> None:
        # /health is an unauthenticated liveness probe: the sidecar handshake
        # (wait_for_health in sidecar.rs) hits it with no token before auth is
        # established. It exposes no state and has no side effects, so exempting
        # it does not weaken RISK-001/002/003.
        if request.url.path == "/health":
            return
        if session_token is None:
            return
        # Header only — no query-parameter fallback. Every route, the SSE
        # stream included, authenticates via the X-Borgesica-Token header, so
        # the secret is never carried in a URL where it could be logged,
        # cached, or land in browser history.
        if x_borgesica_token != session_token:
            raise HTTPException(status_code=401, detail="Missing or invalid session token")

    app = FastAPI(
        title="Borgésica serve API",
        dependencies=[Depends(_require_session_token)],
    )
    # CORS for the Tauri webview: its fetch() calls carry X-Borgesica-Token +
    # a JSON body, so they are non-simple cross-origin requests and the
    # browser preflights them. Without this the preflight is unanswered and
    # the app fails with "Failed to fetch". Origins are restricted to the
    # app's own webview (dev: http://localhost:<port>; bundled Tauri:
    # tauri://localhost and http(s)://tauri.localhost) — never "*" — and the
    # per-session token still gates every route, so RISK-001/002/003 hold.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^(https?://localhost(:\d+)?|https?://tauri\.localhost|tauri://localhost)$",
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["Content-Type", "X-Borgesica-Token"],
    )

    runner = JobRunner(engine)
    app.state.runner = runner  # exposed for tests/introspection

    # Error mapping (design endpoints table): JobNotFound->404,
    # JobStateError->409, GlossaryEntryRejected->409, pydantic
    # validation->422 (FastAPI default), any other BorgesicaError->500.
    @app.exception_handler(JobNotFoundError)
    async def _job_not_found_handler(_request: Request, exc: JobNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(JobStateError)
    async def _job_state_handler(_request: Request, exc: JobStateError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(GlossaryEntryRejectedError)
    async def _glossary_rejected_handler(
        _request: Request, exc: GlossaryEntryRejectedError
    ) -> JSONResponse:
        # 409: the submitted entry conflicts with what the glossary already
        # states, and nothing was persisted.
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(BorgesicaError)
    async def _borgesica_error_handler(_request: Request, exc: BorgesicaError) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    @app.get("/health")
    def health() -> dict[str, str]:
        """Unauthenticated liveness probe consumed by the Tauri sidecar
        handshake (wait_for_health). Returns 200 as soon as the app is up."""
        return {"status": "ok"}

    def _status_response(job_id: str) -> JobResponse:
        job = engine.status(job_id)
        best_effort_indices = engine.best_effort_chunk_indices(job_id)
        failed_indices = engine.failed_chunk_indices(job_id)
        return job_to_response(job, best_effort_indices, failed_indices)

    @app.post("/jobs", response_model=JobResponse, status_code=201)
    def create_job(payload: CreateJobRequest) -> JobResponse:
        source_type = _source_type_for_serve(payload.source_path)
        if source_type is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Unsupported or rejected source format: {payload.source_path!r} "
                    "(only .srt/.epub are accepted by the serve API)"
                ),
            )
        config = JobConfig(
            source_type=source_type,
            model=payload.model,
            target_lang=payload.target_lang,
            budget_usd=payload.budget_usd,
            chunk_size=payload.chunk_size,
            line_length=payload.line_length,
            glossary_strategy=payload.glossary_strategy,
            quality_mode=payload.quality_mode,
            prose_chunk_tokens=payload.prose_chunk_tokens,
            prose_segmentation=payload.prose_segmentation,
            continue_on_error=payload.continue_on_error,
        )
        job = engine.create_job(payload.source_path, config)
        return job_to_response(job, best_effort_indices=[])

    @app.get("/jobs/{job_id}", response_model=JobResponse)
    def get_status(job_id: str) -> JobResponse:
        return _status_response(job_id)

    @app.get("/jobs/{job_id}/estimate", response_model=EstimateResponse)
    def get_estimate(job_id: str) -> EstimateResponse:
        estimate = engine.estimate_cost(job_id)
        return EstimateResponse(
            **estimate.model_dump(),
            glossary_extraction=engine.estimate_glossary_extraction(job_id),
        )

    @app.get("/jobs/{job_id}/glossary", response_model=GlossaryResponse)
    def get_glossary(job_id: str) -> GlossaryResponse:
        glossary = engine.get_glossary(job_id)
        return GlossaryResponse(entries=glossary.entries)

    @app.put("/jobs/{job_id}/glossary", response_model=GlossaryResponse)
    def put_glossary(job_id: str, payload: GlossaryUpdateRequest) -> GlossaryResponse:
        updated = engine.update_glossary(job_id, payload.entries)
        return GlossaryResponse(entries=updated.entries)

    @app.get("/jobs/{job_id}/glossary/review", response_model=GlossaryReview)
    def get_glossary_review(job_id: str) -> GlossaryReview:
        """Every glossary entry with its gender evidence and risk flags, for
        the pre-translation review screen. Derived on each request, free (no
        provider call) and never persisted; GlossaryReview documents the
        shape."""
        return engine.review_glossary(job_id)

    @app.post("/jobs/{job_id}/run", response_model=JobResponse, status_code=202)
    def start_run(job_id: str, payload: RunRequest) -> JobResponse:
        """Launch run_job on a background thread; returns immediately with a
        RUNNING acknowledgement (spec: "Run returns immediately"). Actual
        progress is persisted asynchronously — this response reflects the
        synthetic ack, not a racy read of the checkpoint store."""
        job = engine.status(job_id)  # JobNotFoundError -> 404 before spawning a thread
        runner.start(job_id, payload.out_path, resume=False)
        return job_to_response(
            job.model_copy(update={"status": JobStatus.RUNNING}), best_effort_indices=[]
        )

    @app.post("/jobs/{job_id}/resume", response_model=JobResponse, status_code=202)
    def start_resume(job_id: str, payload: RunRequest) -> JobResponse:
        """Same background-thread contract as run, via resume_job."""
        job = engine.status(job_id)
        runner.start(job_id, payload.out_path, resume=True)
        return job_to_response(
            job.model_copy(update={"status": JobStatus.RUNNING}), best_effort_indices=[]
        )

    @app.post(
        "/jobs/{job_id}/glossary/extract", response_model=JobResponse, status_code=202
    )
    def start_glossary_extraction(job_id: str) -> JobResponse:
        """Run the explicit, paid pre-translation glossary pass in the
        background; progress arrives on GET /jobs/{id}/events as
        glossary_progress events and one glossary_terminal. POST
        /jobs/{id}/glossary/extract/stop stops it before its next batch call.

        Shares the runner's single active slot with run/resume, so a busy
        runner answers 409 here exactly as it does for a second run. The
        job's state is checked HERE, before any thread exists, so a job the
        pass would refuse is a 409 and not a failed stream. The response is
        the job as it stands (still CREATED: the pass never changes status).
        """
        job = engine.status(job_id)  # JobNotFoundError -> 404
        if job.status != JobStatus.CREATED:
            raise JobStateError(job_id=job_id, current_status=str(job.status))
        if job.glossary_extracted:
            raise JobStateError(
                job_id=job_id,
                current_status=str(job.status),
                reason="glossary already extracted",
            )
        runner.start_glossary_extraction(job_id)
        return job_to_response(job, best_effort_indices=[])

    @app.post("/jobs/{job_id}/cancel", response_model=JobResponse, status_code=202)
    def cancel_run(job_id: str) -> JobResponse:
        """Cooperative cancel — takes effect between chunks (chunk-granular).

        Always cancels the job. To stop a glossary extraction and keep the
        job, use POST /jobs/{id}/glossary/extract/stop.
        """
        engine.cancel_job(job_id)
        return _status_response(job_id)

    @app.post(
        "/jobs/{job_id}/glossary/extract/stop",
        response_model=JobResponse,
        status_code=202,
    )
    def stop_glossary_extraction(job_id: str) -> JobResponse:
        """Stop this job's glossary extraction before its next batch call.

        Only ever requests the cooperative stop — it NEVER cancels the job:
        the job stays CREATED (the wizard keeps it, the pass may be started
        again) and the stream ends with a glossary_terminal of status
        "stopped" ("done" if the last call was already made). Kept apart from
        /cancel so a stop that arrives just after the pass released the
        runner's slot cannot fall back to cancelling the job and strand the
        glossary that was just paid for: with no active extraction of this
        job it answers 409 and touches nothing.
        """
        job = engine.status(job_id)  # JobNotFoundError -> 404
        if not runner.request_extraction_stop(job_id):
            raise JobStateError(
                job_id=job_id,
                current_status=str(job.status),
                reason="no active glossary extraction",
            )
        return job_to_response(job, best_effort_indices=[])

    @app.get("/jobs/{job_id}/events")
    def stream_events(job_id: str) -> StreamingResponse:
        engine.status(job_id)  # JobNotFoundError -> 404, validated in route scope
        return StreamingResponse(
            sse_event_stream(engine, runner, job_id), media_type="text/event-stream"
        )

    @app.post("/shutdown")
    def shutdown() -> dict[str, object]:
        """Cancel any active run and join it (bounded); returns whether the
        run finished cleanly within the timeout (design: sidecar lifecycle).
        """
        clean = runner.shutdown(join_timeout=shutdown_join_timeout)
        return {"status": "shutting_down", "clean": clean}

    return app
