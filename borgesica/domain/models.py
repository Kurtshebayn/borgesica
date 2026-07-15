"""Domain models — Pydantic v2 entities and enums.

This module is the contract language for the entire engine.
Dependency rule: only stdlib + pydantic allowed here.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

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
    # Provenance: whether the persisted translation passed tag/segment
    # validation. DONE does NOT imply True — a prose chunk can be accepted
    # as best-effort after exhausting all retries (see orchestrator.py
    # _translate_with_retry). Defaults True so legacy rows (and passthrough/
    # no-translation chunks) are backward-compatible.
    passed_validation: bool = True


class TranslationUnit(BaseModel):
    """Structured LLM result — the per-chunk contract the provider MUST fulfill.

    Two output shapes, one model:
      - Prose (EPUB/PDF): ``translation`` — a single string whose "\\n\\n"
        segment count mirrors the source (writers map segments positionally).
      - Segmented (SRT): ``translations`` — one string PER source cue.  Blank
        lines are a typographic convention models "correct" on unpunctuated
        speech-to-text fragments; an array is structural, so cue boundaries
        survive.  ``translation`` is derived (the "\\n\\n" join) so every
        legacy consumer (checkpoint, reflective prompts) keeps working.

    At least one of the two fields must be present — a payload with neither
    is malformed output and must fail validation so providers fall through
    their tier chain.
    """

    translation: str = ""
    translations: list[str] | None = None
    summary_update: str  # 3-5 sentences, REPLACES prior summary
    glossary_additions: list[GlossaryEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_content_and_derive_translation(self) -> "TranslationUnit":
        if not self.translation:
            if self.translations is None:
                raise ValueError(
                    "TranslationUnit requires 'translation' or 'translations'"
                )
            self.translation = "\n\n".join(self.translations)
        return self


def translation_tool_schema(segment_count: int | None = None) -> dict[str, Any]:
    """JSON schema for the provider tool call, per output shape.

    segment_count=None (prose): legacy contract — ``translation`` (string)
    required, no ``translations`` offered (models must not be tempted to
    restructure prose output).

    segment_count=N (SRT): ``translations`` required with minItems ==
    maxItems == N and ``translation`` removed — the schema itself enforces
    the cue-boundary contract instead of a typographic "\\n\\n" convention.

    Adapters (the only schema consumers) pass the result verbatim as the
    tool/function parameter schema; pydantic-generated pieces (summary_update,
    glossary_additions, $defs) are reused as-is.
    """
    schema = TranslationUnit.model_json_schema()
    props = schema["properties"]
    props.pop("translations", None)

    if segment_count is None:
        props["translation"].pop("default", None)
        schema["required"] = ["translation", "summary_update"]
        return schema

    props.pop("translation")
    schema["properties"] = {
        "translations": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": segment_count,
            "maxItems": segment_count,
            "description": (
                f"Exactly {segment_count} translated segments — one per source "
                "segment, in source order. Never merge, split, or reorder "
                "segments."
            ),
        },
        **props,
    }
    schema["required"] = ["translations", "summary_update"]
    return schema


class Usage(BaseModel):
    """Real token usage returned by a provider call.

    Both fields default to 0 so callers can construct Usage() safely when
    a provider does not expose usage (e.g. local Ollama with usage disabled).
    """

    input_tokens: int = 0
    output_tokens: int = 0


class TranslationResult(BaseModel):
    """Value object wrapping TranslationUnit + real per-call token Usage.

    Returned by TranslationProvider.translate() (M4-6 protocol change).
    The orchestrator uses .unit for all content logic and .usage to accrue
    real cost for every provider call (including reflective passes, retries,
    and fallback calls — fixes debt #289 / S-2).
    """

    unit: TranslationUnit
    usage: Usage = Field(default_factory=Usage)


class CostEstimate(BaseModel):
    input_tokens: int
    output_tokens: int
    usd: float  # backward-compat point estimate; equals usd_low (happy path).
    # Cost is a RANGE, not a point: usd_low is the best case (1 billed provider
    # call per chunk), usd_high folds in the retry / structured-output tier
    # fallthrough waste that no static token math can predict (provider-declared
    # factor). The budget guard protects against usd_high, the ceiling.
    usd_low: float | None = None
    usd_high: float | None = None
    model: str
    cached: bool = False
    within_budget: bool = True

    @model_validator(mode="after")
    def _default_range_to_point(self) -> "CostEstimate":
        """A bare point estimate is a degenerate range: low = high = usd."""
        if self.usd_low is None:
            self.usd_low = self.usd
        if self.usd_high is None:
            self.usd_high = self.usd
        return self


class JobConfig(BaseModel):
    source_type: SourceType
    model: str  # required; no engine-level default — caller decides
    target_lang: str = "es-neutral"
    budget_usd: float | None = None
    chunk_size: int = 25  # SRT cues per batch
    line_length: int = 42  # SRT reflow limit
    glossary_strategy: Literal["llm", "spacy", "hybrid", "none"] = "llm"
    quality_mode: Literal["fast", "reflective"] = "fast"
    # EPUB/PDF prose token budget per chunk — distinct from chunk_size (SRT cue-batch control).
    prose_chunk_tokens: int = 800
    # "batch" (default): accumulate nodes up to prose_chunk_tokens per chunk.
    # "paragraph": one node per chunk — for small local models that translate
    # well but cannot follow the multi-segment output contract; segment→node
    # alignment becomes structural instead of model-dependent.
    prose_segmentation: Literal["batch", "paragraph"] = "batch"
    # continue-on-error: when True (default), a chunk that exhausts all translation
    # attempts is persisted FAILED and the run CONTINUES; the job still finishes
    # DONE. When False (--strict), the prior contract holds: FAILED chunk pauses
    # the job immediately.
    continue_on_error: bool = True


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


class QualityScore(BaseModel):
    """Rubric score produced by the LLM-as-judge harness (M4-2).

    Each dimension is rated 1–5 (1 = poor, 5 = excellent).
    All four fields are required; out-of-range values raise ValidationError.
    """

    accuracy: int = Field(..., ge=1, le=5)
    fluency: int = Field(..., ge=1, le=5)
    neutral_register: int = Field(..., ge=1, le=5)
    glossary_consistency: int = Field(..., ge=1, le=5)
