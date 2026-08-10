"""Domain exceptions for the Borgésica translation engine.

All errors inherit from BorgesicaError so callers can catch at any granularity.
No EXTERNAL (provider/adapter SDK) imports — only stdlib and sibling domain
models. Usage is a domain value object, so carrying it on provider errors keeps
domain purity intact (see tests/unit/test_domain_purity.py).
"""
from __future__ import annotations

from borgesica.domain.models import GlossaryEntry, Usage


class BorgesicaError(Exception):
    """Base exception for all Borgésica domain errors."""


class BudgetExceeded(BorgesicaError):
    """Raised when the running cost exceeds the configured budget_usd.

    The job is set to PAUSED; completed chunks are safe.
    """

    def __init__(self, *, job_id: str, cost_so_far: float) -> None:
        super().__init__(f"Budget exceeded for job {job_id!r}: ${cost_so_far:.4f} spent")
        self.job_id = job_id
        self.cost_so_far = cost_so_far


class MalformedOutput(BorgesicaError):
    """Raised when the provider returns output that cannot be parsed as TranslationUnit
    after all retry tiers are exhausted.

    `usage` carries the accumulated REAL token usage of every billed-but-failed
    attempt made before giving up (any HTTP 200 whose content then failed
    parsing/validation was still billed by the provider). Defaults to zero
    Usage. Callers accrue this so the budget guard sees real spend even for
    calls that produced no usable translation.
    """

    def __init__(
        self, *, job_id: str, chunk_index: int, usage: Usage | None = None
    ) -> None:
        super().__init__(
            f"Malformed provider output for job {job_id!r}, chunk {chunk_index}"
        )
        self.job_id = job_id
        self.chunk_index = chunk_index
        self.usage = usage if usage is not None else Usage()


class JobNotFoundError(BorgesicaError):
    """Raised when a job_id is not found in the checkpoint store."""

    def __init__(self, *, job_id: str) -> None:
        super().__init__(f"Job not found: {job_id!r}")
        self.job_id = job_id


class JobStateError(BorgesicaError):
    """Raised when an operation is invalid for the job's current status."""

    def __init__(self, *, job_id: str, current_status: str) -> None:
        super().__init__(
            f"Invalid operation for job {job_id!r} in status {current_status!r}"
        )
        self.job_id = job_id
        self.current_status = current_status


class GlossaryEntryRejectedError(BorgesicaError):
    """Raised when a caller's own glossary entry contradicts the glossary.

    The glossary hygiene rules also repair contamination that was already
    stored, and that is maintenance — it happens quietly. This error is only
    for entries the CALLER just supplied: asking for something and not getting
    it is a failure, not a no-op, and the update is not persisted.

    The message names the offending terms and points at ``locked=True``, which
    is the supported way to state that the entry is intended.
    """

    def __init__(self, *, job_id: str, rejected: list[GlossaryEntry]) -> None:
        terms = ", ".join(repr(e.term) for e in rejected)
        super().__init__(
            f"Glossary entries rejected for job {job_id!r}: {terms}. "
            f"Each contradicts an existing entry by translating back into the "
            f"source language. Mark the entry locked=True to keep it anyway."
        )
        self.job_id = job_id
        self.rejected = rejected


class UnsupportedFormatError(BorgesicaError):
    """Raised when a source file format is not supported or cannot be read."""

    def __init__(self, *, path: str, reason: str) -> None:
        super().__init__(f"Unsupported format at {path!r}: {reason}")
        self.path = path
        self.reason = reason


class ProviderError(BorgesicaError):
    """Raised after all retry attempts are exhausted on a provider HTTP error.

    `usage` carries the accumulated REAL token usage of any billed-but-failed
    attempts made before giving up. For pure transport failures (429 / 5xx /
    connection errors) no response is billed, so this defaults to zero Usage.
    """

    def __init__(self, *, status_code: int | None, usage: Usage | None = None) -> None:
        super().__init__(f"Provider error (HTTP {status_code})")
        self.status_code = status_code
        self.usage = usage if usage is not None else Usage()
