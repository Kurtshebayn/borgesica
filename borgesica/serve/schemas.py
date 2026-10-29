"""Request/response schemas for borgesica.serve.

Request bodies are a driving-adapter concern (CreateJobRequest maps 1:1 to
JobConfig fields but omits source_type, derived from the extension in
app.py). Response schemas mirror domain entities plus the additive
best_effort_count/best_effort_indices fields (serve-api spec).
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from borgesica.domain.models import (
    GlossaryEntry,
    GlossaryExtractionEstimate,
    Job,
    JobStatus,
)
from borgesica.domain.narrators import Chapter, NarratorAssignment


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
    """Job status payload + best-effort and FAILED count/indices.

    best_effort_* (spec: best-effort count exposed in job status) reports DONE
    chunks that were accepted despite failing validation. failed_* reports
    chunks the provider could never translate (all retries + fallback failed):
    critically, under continue_on_error=True a run with FAILED chunks still
    ends DONE at $0 shipping SOURCE text, so failed_count is the only signal
    that distinguishes a genuine success from a silent untranslated one."""

    id: str
    status: JobStatus
    total_chunks: int
    completed_chunks: int
    cost_usd: float
    best_effort_count: int
    best_effort_indices: list[int]
    failed_count: int
    failed_indices: list[int]
    # Each FAILED chunk's validator messages (why it failed), keyed by index;
    # [] when none were recorded.
    failed_errors: dict[int, list[str]]
    # True once the explicit pre-translation glossary pass has run, so the
    # wizard can tell an empty glossary that was never extracted from one
    # that was.
    glossary_extracted: bool
    # Where this sidecar's latest run/resume of the job writes its translation
    # (see default_out_path); null until one has started in this process.
    out_path: str | None


def default_out_path(source_path: str, target_lang: str) -> str:
    """The source's own folder and name, plus the target language's primary
    subtag: ``book.epub`` translated to ``es-neutral`` -> ``book_es.epub``.
    A name already taken is numbered (``book_es_2.epub``, ``book_es_3.epub``…)
    so an earlier translation is never overwritten."""
    source = Path(source_path)
    base = f"{source.stem}_{target_lang.split('-')[0]}"
    candidate = source.with_name(f"{base}{source.suffix}")
    number = 2
    while candidate.exists():
        candidate = source.with_name(f"{base}_{number}{source.suffix}")
        number += 1
    return str(candidate)


def job_to_response(
    job: Job,
    best_effort_indices: list[int],
    failed_indices: list[int] | None = None,
    failed_errors: dict[int, list[str]] | None = None,
    out_path: str | None = None,
) -> JobResponse:
    """Pure mapping from Job + best-effort/failed indices to JobResponse."""
    failed_indices = failed_indices or []
    failed_errors = failed_errors or {}
    return JobResponse(
        id=job.id,
        status=job.status,
        total_chunks=job.total_chunks,
        completed_chunks=job.completed_chunks,
        cost_usd=job.cost_usd,
        best_effort_count=len(best_effort_indices),
        best_effort_indices=best_effort_indices,
        failed_count=len(failed_indices),
        failed_indices=failed_indices,
        failed_errors=failed_errors,
        glossary_extracted=job.glossary_extracted,
        out_path=out_path,
    )


class EstimateResponse(BaseModel):
    """Translation estimate, plus the glossary extraction line beside it.

    ``glossary_extraction`` is the cost of the explicit pre-translation pass
    (``POST /jobs/{id}/glossary/extract``). Null when the pass cannot run: the
    job is not CREATED, or its glossary was already extracted.

    ``book_usd_low``/``book_usd_high`` price the whole book: what the job has
    already spent (``cost_usd`` — a prepared glossary, an earlier partial run)
    plus what is still ahead, the extraction included while it has not run.
    ``within_budget`` checks ``book_usd_high`` against the job's budget — the
    same budget both passes spend from, and where the run's guard starts.
    """

    input_tokens: int
    output_tokens: int
    usd: float
    usd_low: float
    usd_high: float
    model: str
    cached: bool
    within_budget: bool
    glossary_extraction: GlossaryExtractionEstimate | None = None
    book_usd_low: float
    book_usd_high: float


class GlossaryResponse(BaseModel):
    entries: list[GlossaryEntry] = Field(default_factory=list)


class GlossaryUpdateRequest(BaseModel):
    entries: list[GlossaryEntry]


class NarratorsResponse(BaseModel):
    """Every chapter of the job, with its first-person pre-mark and the
    narrator a person stored for it (null when none)."""

    chapters: list[Chapter] = Field(default_factory=list)


class NarratorsUpdateRequest(BaseModel):
    """The full set of narrators: chapters left out end up with none."""

    narrators: list[NarratorAssignment]


class RunRequest(BaseModel):
    """Body for POST run/resume (T6): an optional destination for the
    translated output. Without one the output goes beside the source
    (default_out_path). The job payload reports the path chosen as out_path."""

    out_path: str | None = None
