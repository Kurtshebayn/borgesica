"""Hand-written test doubles for the Borgésica test suite.

NO mocking framework.  NO network.  All doubles are deterministic.

Provided doubles:
  - FakeTranslationProvider: implements TranslationProvider Protocol
  - InMemoryCheckpointStore: implements CheckpointStore Protocol
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from borgesica.domain.errors import MalformedOutput
from borgesica.domain.models import (
    Chunk,
    Glossary,
    GlossaryEntry,
    Job,
    RollingSummary,
    TranslationResult,
    TranslationUnit,
    Usage,
)


# ---------------------------------------------------------------------------
# FakeTranslationProvider
# ---------------------------------------------------------------------------


@dataclass
class FakeTranslationProvider:
    """Deterministic TranslationProvider for unit tests.

    Attributes:
        canned_unit: The TranslationUnit returned by translate().
            If None, a default unit is constructed from the user text.
        fail_on: Set of 0-based call counts that should raise MalformedOutput.
        call_log: Appended with (system, user, model) on every translate() call.
    """

    canned_unit: TranslationUnit | None = None
    fail_on: set[int] = field(default_factory=set)
    call_log: list[tuple[str, str, str]] = field(default_factory=list)
    segment_count_log: list[int | None] = field(default_factory=list)

    # --- TranslationProvider Protocol methods ---

    def translate(
        self, system: str, user: str, model: str, segment_count: int | None = None
    ) -> TranslationResult:
        call_index = len(self.call_log)
        self.call_log.append((system, user, model))
        self.segment_count_log.append(segment_count)

        if call_index in self.fail_on:
            raise MalformedOutput(job_id="fake-job", chunk_index=call_index)

        if self.canned_unit is not None:
            unit = self.canned_unit
        elif segment_count is not None:
            # Segmented (SRT) contract: echo one translated string per source
            # segment — a compliant model filling the translations array. The
            # orchestrator prefixes each segment with a "[k]" marker line; a
            # compliant model does not copy the markers into its output.
            unit = TranslationUnit(
                translations=[
                    "[translated] " + re.sub(r"^\[\d+\]\n?", "", seg)
                    for seg in user.split("\n\n")
                ],
                summary_update="Fake summary.",
                glossary_additions=[],
            )
        else:
            unit = TranslationUnit(
                translation=f"[translated] {user}",
                summary_update="Fake summary.",
                glossary_additions=[],
            )

        # Deterministic usage: count_tokens on the actual input and output text
        # so cost-based tests get exact, reproducible numbers.
        in_tok = self.count_tokens(system + " " + user, model)
        out_tok = self.count_tokens(unit.translation, model)
        return TranslationResult(unit=unit, usage=Usage(input_tokens=in_tok, output_tokens=out_tok))

    def count_tokens(self, text: str, model: str) -> int:  # noqa: ARG002
        """Deterministic approximation: token ≈ word."""
        return len(text.split())

    def price(self, model: str) -> tuple[float, float]:  # noqa: ARG002
        """Fixed price: $1/Mtok in, $5/Mtok out."""
        return (1.0, 5.0)

    # --- Helpers ---

    @property
    def call_count(self) -> int:
        return len(self.call_log)

    def reset(self) -> None:
        self.call_log.clear()


# ---------------------------------------------------------------------------
# InMemoryCheckpointStore
# ---------------------------------------------------------------------------


class InMemoryCheckpointStore:
    """In-memory implementation of CheckpointStore for unit tests.

    All data lives in plain Python dicts.  No SQLite.
    Idempotent: save_chunk with the same (job_id, chunk.index) upserts.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        # {job_id: {chunk_index: Chunk}}
        self._chunks: dict[str, dict[int, Chunk]] = {}
        self._glossaries: dict[str, Glossary] = {}
        self._summaries: dict[str, dict[int, RollingSummary]] = {}

    # --- CheckpointStore Protocol methods ---

    def save_job(self, job: Job) -> None:
        self._jobs[job.id] = job

    def load_job(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def save_chunk(self, job_id: str, chunk: Chunk) -> None:
        """Idempotent upsert by (job_id, chunk.index)."""
        if job_id not in self._chunks:
            self._chunks[job_id] = {}
        self._chunks[job_id][chunk.index] = chunk

    def load_chunks(self, job_id: str) -> list[Chunk]:
        bucket = self._chunks.get(job_id, {})
        return [bucket[k] for k in sorted(bucket)]

    def save_glossary(self, job_id: str, g: Glossary) -> None:
        self._glossaries[job_id] = g

    def load_glossary(self, job_id: str) -> Glossary:
        return self._glossaries.get(job_id, Glossary())

    def save_summary(self, job_id: str, s: RollingSummary) -> None:
        if job_id not in self._summaries:
            self._summaries[job_id] = {}
        self._summaries[job_id][s.chunk_index] = s

    def load_summary(self, job_id: str) -> RollingSummary:
        bucket = self._summaries.get(job_id, {})
        if not bucket:
            return RollingSummary()
        # Return the summary from the highest-index chunk.
        max_index = max(bucket)
        return bucket[max_index]
