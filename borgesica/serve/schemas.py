"""Request/response schemas for borgesica.serve.

Request bodies are a driving-adapter concern (CreateJobRequest maps 1:1 to
JobConfig fields but omits source_type, derived from the extension in
app.py). Response schemas mirror domain entities plus the additive
best_effort_count/best_effort_indices fields (serve-api spec).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from borgesica.domain.models import (
    Chunk,
    ChunkStatus,
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


class UpdateJobRequest(BaseModel):
    """The one thing a draft lets you change after creation: its model."""

    model: str


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
    # The model the job translates with (a draft's can be changed).
    model: str
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


class JobListItem(BaseModel):
    """One row of the library: a stored job in brief. ``title`` is the source
    file's name without its extension. The source language is not stored on a
    job, so it is not here."""

    id: str
    status: JobStatus
    title: str
    source_type: str
    target_lang: str
    model: str
    total_chunks: int
    completed_chunks: int
    cost_usd: float
    failed_count: int
    best_effort_count: int


class JobListResponse(BaseModel):
    """GET /jobs: every stored job, newest first."""

    jobs: list[JobListItem] = Field(default_factory=list)


def job_to_list_item(
    job: Job, best_effort_indices: list[int], failed_indices: list[int]
) -> JobListItem:
    return JobListItem(
        id=job.id,
        status=job.status,
        title=Path(job.source_path).stem,
        source_type=str(job.config.source_type),
        target_lang=job.config.target_lang,
        model=job.config.model,
        total_chunks=job.total_chunks,
        completed_chunks=job.completed_chunks,
        cost_usd=job.cost_usd,
        failed_count=len(failed_indices),
        best_effort_count=len(best_effort_indices),
    )


def library_dir() -> Path:
    """The folder every serve-made translation lands in: BORGESICA_LIBRARY_DIR
    when set, else ``~/Borgesica``."""
    configured = os.environ.get("BORGESICA_LIBRARY_DIR")
    return Path(configured) if configured else Path.home() / "Borgesica"


def default_out_path(source_path: str, target_lang: str) -> str:
    """A file in the library folder named after the source plus the target
    language's primary subtag: ``book.epub`` translated to ``es-neutral`` ->
    ``<library>/book_es.epub``. A name already taken is numbered
    (``book_es_2.epub``, ``book_es_3.epub``...) so an earlier translation is
    never overwritten. The folder is created here."""
    source = Path(source_path)
    library = library_dir()
    library.mkdir(parents=True, exist_ok=True)
    base = f"{source.stem}_{target_lang.split('-')[0]}"
    candidate = library / f"{base}{source.suffix}"
    number = 2
    while candidate.exists():
        candidate = library / f"{base}_{number}{source.suffix}"
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
        model=job.config.model,
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


class ChunkItem(BaseModel):
    """One chunk of a job: the source text beside its translation.

    ``translation`` is null until the chunk has been translated; a FAILED
    chunk keeps whatever was stored. ``best_effort`` marks a DONE chunk that
    was accepted although it failed validation. ``chapter_key`` and
    ``chapter_title`` are null for chunks without a chapter (subtitles)."""

    index: int
    status: ChunkStatus
    source: str
    translation: str | None
    best_effort: bool
    chapter_key: str | None
    chapter_title: str | None


class ChunksResponse(BaseModel):
    """One page of a job's chunks in reading order. ``total`` counts the chunks
    matching the filter (not just this page); ``offset``/``limit`` echo the
    request."""

    total: int
    offset: int
    limit: int
    chunks: list[ChunkItem]


def chunk_to_item(chunk: Chunk, chapter: Chapter | None) -> ChunkItem:
    return ChunkItem(
        index=chunk.index,
        status=chunk.status,
        source=chunk.source_text,
        translation=chunk.translated_text,
        best_effort=is_best_effort(chunk),
        chapter_key=chapter.key if chapter else None,
        chapter_title=chapter.title if chapter else None,
    )


def is_best_effort(chunk: Chunk) -> bool:
    """Same rule as TranslatorEngine.best_effort_chunk_indices."""
    return chunk.status == ChunkStatus.DONE and not chunk.passed_validation


class DeleteJobResponse(BaseModel):
    """DELETE /jobs/{id}: the job's rows are gone (always true on 200);
    file_deleted says whether its translated file in the library was removed."""

    deleted: bool
    file_deleted: bool


class RunRequest(BaseModel):
    """Body for POST run/resume (T6): an optional destination for the
    translated output. Without one the output goes beside the source
    (default_out_path). The job payload reports the path chosen as out_path."""

    out_path: str | None = None
