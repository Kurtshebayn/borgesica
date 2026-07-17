"""FastAPI app factory for borgesica.serve (design decision #1).

`create_app(engine)` binds one already-constructed TranslatorEngine instance
to routes, enabling TestClient injection with a fake engine in tests. This
module MUST NOT construct adapters itself.

T5 landed create/estimate/status/glossary(get,update). T6 adds run/resume
(background thread via JobRunner), the SSE progress stream, cancellation,
and graceful shutdown.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import Request

from borgesica.api import TranslatorEngine
from borgesica.domain.errors import BorgesicaError, JobNotFoundError, JobStateError
from borgesica.domain.models import JobConfig, JobStatus, SourceType
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


def create_app(engine: TranslatorEngine, *, shutdown_join_timeout: float = 5.0) -> FastAPI:
    """Build the FastAPI app wired to *engine*.

    Args:
        engine: Already-constructed TranslatorEngine (composition root stays
            outside this module).
        shutdown_join_timeout: Seconds POST /shutdown waits for an active run
            to finish after cancelling it (design: sidecar lifecycle "kill
            after 5s" fallback). Overridable for deterministic tests.
    """
    app = FastAPI(title="Borgésica serve API")
    runner = JobRunner(engine)
    app.state.runner = runner  # exposed for tests/introspection

    # Error mapping (design endpoints table): JobNotFound->404,
    # JobStateError->409, pydantic validation->422 (FastAPI default),
    # any other BorgesicaError->500.
    @app.exception_handler(JobNotFoundError)
    async def _job_not_found_handler(_request: Request, exc: JobNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(JobStateError)
    async def _job_state_handler(_request: Request, exc: JobStateError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(BorgesicaError)
    async def _borgesica_error_handler(_request: Request, exc: BorgesicaError) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    def _status_response(job_id: str) -> JobResponse:
        job = engine.status(job_id)
        best_effort_indices = engine.best_effort_chunk_indices(job_id)
        return job_to_response(job, best_effort_indices)

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
        return EstimateResponse(**estimate.model_dump())

    @app.get("/jobs/{job_id}/glossary", response_model=GlossaryResponse)
    def get_glossary(job_id: str) -> GlossaryResponse:
        glossary = engine.get_glossary(job_id)
        return GlossaryResponse(entries=glossary.entries)

    @app.put("/jobs/{job_id}/glossary", response_model=GlossaryResponse)
    def put_glossary(job_id: str, payload: GlossaryUpdateRequest) -> GlossaryResponse:
        updated = engine.update_glossary(job_id, payload.entries)
        return GlossaryResponse(entries=updated.entries)

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

    @app.post("/jobs/{job_id}/cancel", response_model=JobResponse, status_code=202)
    def cancel_run(job_id: str) -> JobResponse:
        """Cooperative cancel — takes effect between chunks (chunk-granular)."""
        engine.cancel_job(job_id)
        return _status_response(job_id)

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
