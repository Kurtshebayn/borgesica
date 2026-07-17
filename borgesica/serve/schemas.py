"""Request/response schemas for borgesica.serve.

Request bodies are a driving-adapter concern (CreateJobRequest maps 1:1 to
JobConfig fields but omits source_type, derived from the extension in
app.py). Response schemas mirror domain entities plus the additive
best_effort_count/best_effort_indices fields (serve-api spec).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from borgesica.domain.models import GlossaryEntry, Job, JobStatus


class CreateJobRequest(BaseModel):
    """Maps 1:1 to JobConfig fields (design decision #1); source_type is
    derived from source_path's extension in app.py, not accepted here."""

    source_path: str
    model: str
    target_lang: str = "es-neutral"
    budget_usd: float | None = None
    chunk_size: int = 25
    line_length: int = 42
    glossary_strategy: Literal["llm", "spacy", "hybrid", "none"] = "llm"
    quality_mode: Literal["fast", "reflective"] = "fast"
    prose_chunk_tokens: int = 800
    prose_segmentation: Literal["batch", "paragraph"] = "batch"
    continue_on_error: bool = True


class JobResponse(BaseModel):
    """Job status payload + best-effort count/indices (spec: best-effort
    count exposed in job status)."""

    id: str
    status: JobStatus
    total_chunks: int
    completed_chunks: int
    cost_usd: float
    best_effort_count: int
    best_effort_indices: list[int]


def job_to_response(job: Job, best_effort_indices: list[int]) -> JobResponse:
    """Pure mapping from Job + best-effort indices to JobResponse."""
    return JobResponse(
        id=job.id,
        status=job.status,
        total_chunks=job.total_chunks,
        completed_chunks=job.completed_chunks,
        cost_usd=job.cost_usd,
        best_effort_count=len(best_effort_indices),
        best_effort_indices=best_effort_indices,
    )


class EstimateResponse(BaseModel):
    input_tokens: int
    output_tokens: int
    usd: float
    usd_low: float
    usd_high: float
    model: str
    cached: bool
    within_budget: bool


class GlossaryResponse(BaseModel):
    entries: list[GlossaryEntry] = Field(default_factory=list)


class GlossaryUpdateRequest(BaseModel):
    entries: list[GlossaryEntry]


class RunRequest(BaseModel):
    """Body for POST run/resume (T6): destination for the translated output,
    chosen by the UI before the run starts (design decision #11)."""

    out_path: str
