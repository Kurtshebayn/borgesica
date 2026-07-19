"""Pure PASS/FAIL evaluation logic for the frozen-binary smoke check.

Spawning an actual PyInstaller-frozen `borgesica.exe` is a manual spike step
(no frozen binary exists in the CI/unit-test environment), but the decision
logic — what counts as a real, meaningful smoke pass — is a pure function
and is unit-tested here. `packaging/smoke_check.py` is the manual-run CLI
wrapper that spawns the frozen binary and hands its output to these
functions; T7a's sidecar health-check wiring can reuse the same logic.

Two checks matter for this spike:
  1. `--help` exits 0 and prints the usage banner — proves the frozen
     binary starts and its argument parser loads at all.
  2. A trivial real engine call (`estimate` on a nonexistent job id) exits
     non-zero with the expected `JobNotFoundError` message — proves the
     domain/adapters actually loaded and ran, not just argparse. A
     `ModuleNotFoundError` here is exactly the hidden-import failure mode
     this spike exists to catch.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SmokeResult:
    passed: bool
    reason: str


def evaluate_help(*, returncode: int, stdout: str) -> SmokeResult:
    """Evaluate `borgesica --help` (or bare invocation) output."""
    if returncode != 0:
        return SmokeResult(passed=False, reason=f"non-zero exit code {returncode}")
    if "usage: borgesica" not in stdout:
        return SmokeResult(passed=False, reason="usage banner missing from stdout")
    return SmokeResult(passed=True, reason="help banner present, exit 0")


def evaluate_engine_call(*, returncode: int, stderr: str) -> SmokeResult:
    """Evaluate `borgesica estimate <nonexistent-job-id>` output.

    A JobNotFoundError message proves real domain/adapter code ran to
    completion inside the frozen binary. Any other failure (import error,
    traceback, missing module) means the freeze is broken.
    """
    if returncode == 0:
        return SmokeResult(passed=False, reason="expected non-zero exit for a nonexistent job")
    if "not found" in stderr.lower():
        return SmokeResult(
            passed=True, reason="engine call executed real domain logic (JobNotFoundError path)"
        )
    return SmokeResult(passed=False, reason=f"unexpected failure mode: {stderr.strip()!r}")
