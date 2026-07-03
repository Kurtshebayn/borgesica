"""Domain exceptions for the Borgésica translation engine.

All errors inherit from BorgésicaError so callers can catch at any granularity.
No EXTERNAL (provider/adapter SDK) imports — only stdlib and sibling domain
models. Usage is a domain value object, so carrying it on provider errors keeps
domain purity intact (see tests/unit/test_domain_purity.py).
"""
from __future__ import annotations

from borgesica.domain.models import Usage


class BorgésicaError(Exception):
    """Base exception for all Borgésica domain errors."""


class BudgetExceeded(BorgésicaError):
    """Raised when the running cost exceeds the configured budget_usd.

    The job is set to PAUSED; completed chunks are safe.
    """

    def __init__(self, *, job_id: str, cost_so_far: float) -> None:
        super().__init__(f"Budget exceeded for job {job_id!r}: ${cost_so_far:.4f} spent")
        self.job_id = job_id
        self.cost_so_far = cost_so_far


class MalformedOutput(BorgésicaError):
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


class JobNotFoundError(BorgésicaError):
    """Raised when a job_id is not found in the checkpoint store."""

    def __init__(self, *, job_id: str) -> None:
        super().__init__(f"Job not found: {job_id!r}")
        self.job_id = job_id


class JobStateError(BorgésicaError):
    """Raised when an operation is invalid for the job's current status."""

    def __init__(self, *, job_id: str, current_status: str) -> None:
        super().__init__(
            f"Invalid operation for job {job_id!r} in status {current_status!r}"
        )
        self.job_id = job_id
        self.current_status = current_status


class UnsupportedFormatError(BorgésicaError):
    """Raised when a source file format is not supported or cannot be read."""

    def __init__(self, *, path: str, reason: str) -> None:
        super().__init__(f"Unsupported format at {path!r}: {reason}")
        self.path = path
        self.reason = reason


class ProviderError(BorgésicaError):
    """Raised after all retry attempts are exhausted on a provider HTTP error.

    `usage` carries the accumulated REAL token usage of any billed-but-failed
    attempts made before giving up. For pure transport failures (429 / 5xx /
    connection errors) no response is billed, so this defaults to zero Usage.
    """

    def __init__(self, *, status_code: int | None, usage: Usage | None = None) -> None:
        super().__init__(f"Provider error (HTTP {status_code})")
        self.status_code = status_code
        self.usage = usage if usage is not None else Usage()
