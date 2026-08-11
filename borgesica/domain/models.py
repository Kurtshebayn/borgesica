"""Domain models — Pydantic v2 entities and enums.

This module is the contract language for the entire engine.
Dependency rule: only stdlib + pydantic allowed here.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_term(term: str) -> str:
    """Return the canonical display form of a glossary term.

    Canonicalises the two ways the same term can be spelled without anyone
    meaning anything different by it:
      - surrounding and repeated internal whitespace (models emit both
        "Ddram cyfraith" and "Ddram  cyfraith ");
      - Unicode composition, so a precomposed "ó" and an "o" followed by a
        combining acute compare equal — they render identically and would
        otherwise persist as two entries in an accented-Spanish glossary.

    Casing is deliberately preserved: it is not part of the identity of a term
    (see ``borgesica.domain.glossary._dedupe_key``), but it *is* how the entry
    is shown to the model and to the human editing the glossary.

    Lives here rather than in ``glossary.py`` because ``Glossary.render`` needs
    it to decide which entries are identity entries, and ``glossary.py``
    imports from this module.
    """
    return _WHITESPACE_RUN.sub(" ", unicodedata.normalize("NFC", term)).strip()

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


# Glossary prompt budget, in WORDS (see Glossary.render). Sized from a REAL
# book: a finished 502-chunk run accumulated 491 entries, which render to 1868
# words once notes are excluded (~3.8 words each). 2500 fits that with room for
# a larger glossary. At 300 the renderer stopped at 18 of those 491 entries,
# which is how a term at alphabetical position 153 became invisible to the
# model in every chunk.
#
# The glossary rides in the DYNAMIC (uncached) prompt block, so it is paid on
# every call: ~2500 words ≈ 3.3k tokens × 502 chunks ≈ $0.23 per book on
# deepseek-v4-flash, ~$5 on claude-sonnet-5. Tune JobConfig.
# glossary_budget_tokens down on expensive providers. It is a ceiling, not a
# floor: a small glossary renders small regardless.
DEFAULT_GLOSSARY_BUDGET_TOKENS = 2500


class GlossaryEntry(BaseModel):
    term: str
    translation: str
    locked: bool = False
    note: str | None = None


class Glossary(BaseModel):
    entries: list[GlossaryEntry] = Field(default_factory=list)

    def render(self, budget_tokens: int = DEFAULT_GLOSSARY_BUDGET_TOKENS) -> str:
        """Return a compact table of entries for prompt injection.

        Entries are split into two kinds, because they carry different
        instructions and cost very different amounts to express:

        - MAPPINGS (term != translation) render as "term → translation" lines.
          These are the only entries that teach the model a rendering it would
          not otherwise produce, so they get first claim on the budget.
        - IDENTITY entries (term == translation once normalised) collapse into
          a single trailing DO NOT TRANSLATE line. On a real 491-entry book
          glossary 337 entries (69%) were of this kind — proper nouns that
          correctly must not be translated. Spelling each one out as
          "Aaru → Aaru" spent two thirds of the per-call glossary budget
          repeating every term back to itself; naming them once in a list says
          the same thing for roughly a third of the words.

        Locked entries are ALWAYS included in full, of either kind — a locked
        term is one the user explicitly pinned, so dropping it would defeat the
        purpose of locking. Unlocked entries are trimmed once the budget is
        exhausted. Identity entries carry no [LOCKED] marker: "do not
        translate" is already absolute, so the marker would be paid for on
        every call while adding nothing the line does not already say.

        The budget is measured in WORDS (``len(line.split())``), which
        under-counts real tokens by roughly 1.7x for accented Spanish plus the
        arrow separator. The default is expressed in those same units and sized
        so that a full novel's glossary reaches the prompt intact; at 300 the
        renderer capped out near 67 entries no matter how large the glossary
        grew, and everything past that was invisible to the model in every
        chunk.
        """
        mappings: list[GlossaryEntry] = []
        identities: list[GlossaryEntry] = []
        for e in self.entries:
            term = normalize_term(e.term)
            if term and term == normalize_term(e.translation):
                identities.append(e)
            else:
                mappings.append(e)

        def entry_line(e: GlossaryEntry) -> str:
            # `note` is deliberately NOT rendered. It is explanatory prose for
            # the human reviewing the glossary; the model only needs the
            # term → translation pair to stay consistent. On a real 491-entry
            # book glossary every entry carried one, and the notes were 78% of
            # the rendered weight (8482 words with them, 1868 without) — they
            # crowded out the very mappings the glossary exists to enforce.
            suffix = " [LOCKED]" if e.locked else ""
            return f"  {e.term} → {e.translation}{suffix}"

        lines: list[str] = []
        used_tokens = 0

        # Always include all locked mappings, then spend what is left of the
        # budget on unlocked ones.
        for e in mappings:
            if e.locked:
                line = entry_line(e)
                lines.append(line)
                used_tokens += len(line.split())
        for e in mappings:
            if e.locked:
                continue
            line = entry_line(e)
            cost = len(line.split())
            if used_tokens + cost > budget_tokens:
                break
            lines.append(line)
            used_tokens += cost

        identity_line = self._render_identity_line(
            identities, budget_tokens - used_tokens
        )
        if identity_line:
            lines.append(identity_line)

        return "\n".join(lines)

    @staticmethod
    def _render_identity_line(
        identities: list[GlossaryEntry], remaining_tokens: int
    ) -> str:
        """Return the compact do-not-translate line, or "" if there is nothing to say.

        Locked terms are always named, even when ``remaining_tokens`` is
        already spent; unlocked ones are appended while they fit. The header is
        charged against the budget like any other content, but it is never the
        reason a locked term goes missing.
        """
        if not identities:
            return ""

        header = "  DO NOT TRANSLATE (copy verbatim):"
        used = len(header.split())
        terms: list[str] = []

        for e in identities:
            if e.locked:
                term = normalize_term(e.term)
                terms.append(term)
                used += len(term.split())
        for e in identities:
            if e.locked:
                continue
            term = normalize_term(e.term)
            cost = len(term.split())
            if used + cost > remaining_tokens:
                break
            terms.append(term)
            used += cost

        if not terms:
            return ""
        return f"{header} {', '.join(terms)}"


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
    # Validation failure detail (JSON list of issue-message strings),
    # populated whenever passed_validation is False (best-effort or FAILED
    # paths — see orchestrator.py _translate_with_retry); None when
    # validation passed cleanly. In-memory only — NOT persisted to the
    # checkpoint schema (T4b amendment: the corpus hook fires at DONE
    # within the same run, so in-memory threading suffices; no jobs.db
    # column needed).
    validation_errors: str | None = None


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
    cached_input_tokens: int = 0
    """The SUBSET of ``input_tokens`` that hit the provider's prompt cache.

    Defaults to 0 so a provider that reports no cache detail prices exactly as
    it always did. Measured on a real DeepSeek bill of 1,114 requests: 5.12M of
    6.01M input tokens were cache hits, billed at roughly 1.6% of the miss
    rate. Pricing them as misses overstated that bill 2.04x — and the same
    figure feeds the budget guard, which would then pause a job at about half
    the budget it had really spent."""


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
    # Output is a range for the same reason cost is. It is not a rescaling of
    # input: the translation expands against its source, and one measured call
    # in twenty emitted 3.6x the floor's projection for a single (non-retried)
    # call. output_tokens reports the FLOOR, matching input_tokens and usd.
    output_tokens_high: int | None = None
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
        if self.output_tokens_high is None:
            self.output_tokens_high = self.output_tokens
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
    # Extract mode: keep only the first N chunks at create time, for cheap model
    # comparison and translation-quality iteration without paying for a full book.
    # None (default) = translate everything. Truncation happens BEFORE glossary
    # seeding, so the job simply IS the extract — estimate, run, and writer need
    # no special-casing. Values above the chunk total clamp to the total.
    extract_chunks: int | None = Field(default=None, ge=1)
    # Where the extract window starts. Defects tend to surface deep into a long
    # book, so an extract pinned to the opening cannot reproduce them. Combined:
    # chunks[extract_offset : extract_offset + extract_chunks]. With no
    # extract_chunks, the window runs to the end. Extracted chunks KEEP their
    # original indices, so the job records where in the book it came from.
    extract_offset: int = Field(default=0, ge=0)
    # Word budget for the glossary block of the per-chunk system prompt. Tunable
    # so expensive providers can trade glossary coverage against cost; see
    # DEFAULT_GLOSSARY_BUDGET_TOKENS for why the default is what it is.
    glossary_budget_tokens: int = Field(
        default=DEFAULT_GLOSSARY_BUDGET_TOKENS, ge=1
    )
    # Output-token cap per provider call. None (default) lets the adapter pick
    # its own. Raise it only for a model whose reasoning trace must fit in the
    # SAME budget as the answer: reasoning tokens are billed as output and are
    # drawn from this cap, so a trace larger than the cap truncates the call
    # before it emits anything (deepseek-v4-flash, measured 2026-08-06, spends
    # ~20k). Prefer disabling reasoning at the adapter over raising this — a
    # reasoning run cost ~23x the output tokens and ~100x the wall time.
    max_output_tokens: int | None = Field(default=None, ge=1)


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
    # Position of this chunk in the SOURCE, which for an extract job is its
    # original book index (--extract 20 --from 380 yields 380-399). Keep it:
    # the writer and the checkpoint both key off it.
    chunk_index: int
    # 1-based position within THIS run, so `position/total_chunks` is a real
    # fraction. chunk_index is not: on an extract it made the CLI print
    # "chunk 387/20 (1935%)". Defaults to 0 for callers that build a Progress
    # without one.
    position: int = 0
    total_chunks: int
    cost_usd: float
    status: JobStatus


class CorpusSample(BaseModel):
    """A single captured corpus entry (write-only; see CorpusStore port).

    Captured at the orchestrator's chunk-DONE point (design decision #6),
    for every job execution path (CLI and served/UI). One CorpusSample maps
    to one row in corpus.db's ``samples`` table, upserted on
    (job_id, chunk_index).
    """

    job_id: str
    chunk_index: int
    source_text: str
    translated_text: str | None = None
    provider: str
    model: str
    quality_mode: str
    # Provenance mirror of Chunk.passed_validation — DONE does not imply
    # validation passed (see Chunk.passed_validation docstring).
    passed_validation: bool = True
    # JSON/text detail of validation failures. Populated when
    # passed_validation is False; empty/null when True.
    validation_errors: str | None = None


class QualityScore(BaseModel):
    """Rubric score produced by the LLM-as-judge harness (M4-2).

    Each dimension is rated 1–5 (1 = poor, 5 = excellent).
    All four fields are required; out-of-range values raise ValidationError.
    """

    accuracy: int = Field(..., ge=1, le=5)
    fluency: int = Field(..., ge=1, le=5)
    neutral_register: int = Field(..., ge=1, le=5)
    glossary_consistency: int = Field(..., ge=1, le=5)
