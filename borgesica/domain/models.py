"""Domain models — Pydantic v2 entities and enums.

This module is the contract language for the entire engine.
Dependency rule: only stdlib + pydantic allowed here.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class JobStatus(StrEnum):
    CREATED = "CREATED"
    ESTIMATING = "ESTIMATING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ChunkStatus(StrEnum):
    PENDING = "PENDING"
    TRANSLATING = "TRANSLATING"
    DONE = "DONE"
    FAILED = "FAILED"


class SourceType(StrEnum):
    SRT = "SRT"
    EPUB = "EPUB"
    PDF = "PDF"


# ---------------------------------------------------------------------------
# Domain entities
# ---------------------------------------------------------------------------


class GlossaryEntry(BaseModel):
    term: str
    translation: str
    locked: bool = False
    note: str | None = None


class Glossary(BaseModel):
    entries: list[GlossaryEntry] = Field(default_factory=list)

    def render(self, budget_tokens: int = 300) -> str:
        """Return a compact table of entries for prompt injection.

        Locked entries appear first.  Unlocked entries are trimmed once the
        estimated token budget is exhausted (token ~= word count).
        """
        locked = [e for e in self.entries if e.locked]
        unlocked = [e for e in self.entries if not e.locked]

        def entry_line(e: GlossaryEntry, mark: str = "") -> str:
            suffix = f" [LOCKED]{mark}" if e.locked else mark
            note_part = f" ({e.note})" if e.note else ""
            return f"  {e.term} → {e.translation}{note_part}{suffix}"

        lines: list[str] = []
        used_tokens = 0

        # Always include all locked entries.
        for e in locked:
            line = entry_line(e)
            lines.append(line)
            used_tokens += len(line.split())

        # Add unlocked entries until token budget is exhausted.
        for e in unlocked:
            line = entry_line(e)
            cost = len(line.split())
            if used_tokens + cost > budget_tokens:
                break
            lines.append(line)
            used_tokens += cost

        return "\n".join(lines)


class RollingSummary(BaseModel):
    text: str = ""
    chunk_index: int = -1  # index of the last chunk that produced this summary


class Chunk(BaseModel):
    index: int
    source_text: str
    status: ChunkStatus = ChunkStatus.PENDING
    translated_text: str | None = None
    meta: dict = Field(default_factory=dict)  # adapter round-trip data


class TranslationUnit(BaseModel):
    """Structured LLM result — the per-chunk contract the provider MUST fulfill."""

    translation: str
    summary_update: str  # 3-5 sentences, REPLACES prior summary
    glossary_additions: list[GlossaryEntry] = Field(default_factory=list)


class CostEstimate(BaseModel):
    input_tokens: int
    output_tokens: int
    usd: float
    model: str
    cached: bool = False
    within_budget: bool = True


class JobConfig(BaseModel):
    source_type: SourceType
    model: str  # required; no engine-level default — caller decides
    target_lang: str = "es-neutral"
    budget_usd: float | None = None
    chunk_size: int = 25  # SRT cues per batch
    line_length: int = 42  # SRT reflow limit
    glossary_strategy: Literal["llm", "spacy", "hybrid", "none"] = "llm"
    quality_mode: Literal["fast", "reflective"] = "fast"


class Job(BaseModel):
    id: str
    config: JobConfig
    source_path: str
    status: JobStatus = JobStatus.CREATED
    total_chunks: int = 0
    completed_chunks: int = 0
    cost_usd: float = 0.0
    created_at: datetime
    updated_at: datetime


class Progress(BaseModel):
    """Progress update pushed to the caller after each chunk completes."""

    job_id: str
    chunk_index: int
    total_chunks: int
    cost_usd: float
    status: JobStatus
