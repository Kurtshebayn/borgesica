"""Domain ports — Protocol definitions.

Every adapter in borgesica/adapters/ implements one or more of these Protocols.
Dependency rule: only stdlib + domain models allowed here.
All Protocols are @runtime_checkable so tests can use isinstance() checks.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from borgesica.domain.models import (
    Chunk,
    CorpusSample,
    Glossary,
    Job,
    JobConfig,
    Progress,
    RollingSummary,
    TranslationResult,
)

# ---------------------------------------------------------------------------
# Document I/O ports
# ---------------------------------------------------------------------------


@runtime_checkable
class DocumentReader(Protocol):
    """Parse a source file into an ordered list of Chunks.

    No I/O beyond reading the source file.
    Meta dict on each Chunk carries adapter-specific round-trip data
    (cue indices, timestamps, EPUB node paths, etc.).
    """

    def read(self, path: str, config: JobConfig) -> list[Chunk]:
        ...


@runtime_checkable
class DocumentWriter(Protocol):
    """Reassemble translated Chunks into the same format as the source file."""

    def write(self, chunks: list[Chunk], src_path: str, out_path: str) -> None:
        ...


# ---------------------------------------------------------------------------
# Provider port
# ---------------------------------------------------------------------------


@runtime_checkable
class TranslationProvider(Protocol):
    """Translation provider.  Adapters own the structured-output mechanism
    and degrade gracefully (tool-call → constrained → prompt+parse+retry).

    The domain only sees this interface — never any SDK types.
    """

    def translate(
        self, system: str, user: str, model: str, segment_count: int | None = None
    ) -> TranslationResult:
        """Return a TranslationResult containing a VALID TranslationUnit and the
        real token Usage for this call.  Adapters MUST populate usage from the
        provider response — not an estimate.  Retries and structured-output
        fallback chains are the adapter's responsibility; each retry is a separate
        call and may have its own Usage.

        segment_count (SRT cue batches): when given, the adapter requests the
        SEGMENTED output shape — TranslationUnit.translations as an array of
        exactly segment_count strings (see translation_tool_schema).  When
        None, the legacy single-string contract applies.  Callers only pass
        the argument when set, so implementations that predate it keep
        working for prose."""
        ...

    def count_tokens(self, text: str, model: str) -> int:
        """Return an approximate token count for the given text."""
        ...

    def price(self, model: str) -> tuple[float, float]:
        """Return (input_usd_per_mtok, output_usd_per_mtok) for the model."""
        ...

    def cache_price(self, model: str) -> float:
        """Return USD per Mtok for input tokens served from the prompt cache.

        Separate from ``price`` because it applies to the ``cached_input_tokens``
        subset of a call's Usage, not to all input. Implementations with no
        measured cache rate should return the ordinary input price, so an
        unknown rate can never make a job look cheaper than it is.
        """
        ...


# ---------------------------------------------------------------------------
# Checkpoint store port (8 methods per design §3)
# ---------------------------------------------------------------------------


@runtime_checkable
class CheckpointStore(Protocol):
    """Persistent state store.  All writes are idempotent (upsert semantics)."""

    def save_job(self, job: Job) -> None:
        ...

    def load_job(self, job_id: str) -> Job | None:
        ...

    def save_chunk(self, job_id: str, chunk: Chunk) -> None:
        """Idempotent upsert keyed by (job_id, chunk.index)."""
        ...

    def load_chunks(self, job_id: str) -> list[Chunk]:
        ...

    def save_glossary(self, job_id: str, g: Glossary) -> None:
        ...

    def load_glossary(self, job_id: str) -> Glossary:
        ...

    def save_summary(self, job_id: str, s: RollingSummary) -> None:
        ...

    def load_summary(self, job_id: str) -> RollingSummary:
        ...


# ---------------------------------------------------------------------------
# Corpus store port (write-only, best-effort — see design decision #6/#10)
# ---------------------------------------------------------------------------


@runtime_checkable
class CorpusStore(Protocol):
    """Write-only corpus capture store.

    Captures per-chunk translation samples for later mining/curation (out of
    scope for v1). Distinct from CheckpointStore: no load methods are
    exposed by this port — corpus.db is never read back by the engine.

    Callers (orchestrator hook, T4) are responsible for best-effort
    semantics: a raised exception from save_sample() MUST be caught and
    logged by the caller, never allowed to fail the translation job. The
    adapter itself only guarantees that a failed write does not leave the
    store in a corrupted state (atomic per-call transaction).
    """

    def save_sample(self, sample: CorpusSample) -> None:
        """Persist one corpus sample, upserting on (job_id, chunk_index)."""
        ...


# ---------------------------------------------------------------------------
# Glossary extractor port
# ---------------------------------------------------------------------------


@runtime_checkable
class GlossaryExtractor(Protocol):
    """Extract terminology from source text."""

    def extract(self, text: str, config: JobConfig) -> Glossary:
        ...


# ---------------------------------------------------------------------------
# Progress callback type alias
# ---------------------------------------------------------------------------

ProgressCallback = Callable[[Progress], None]
