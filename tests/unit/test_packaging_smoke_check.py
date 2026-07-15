"""Tests for the pure evaluation logic behind the frozen-binary smoke check
(packaging spike, T1). The actual subprocess spawn of a frozen exe is a
manual spike step (no frozen binary exists in CI); these tests pin the
PASS/FAIL decision logic so it is real, assertable, and reusable by T7a's
sidecar health-check wiring.
"""
from __future__ import annotations

from borgesica.packaging_smoke_check import evaluate_engine_call, evaluate_help


def test_evaluate_help_passes_on_zero_exit_and_usage_banner() -> None:
    result = evaluate_help(returncode=0, stdout="usage: borgesica <command> ...\n")
    assert result.passed is True
    assert result.reason == "help banner present, exit 0"


def test_evaluate_help_fails_on_nonzero_exit() -> None:
    result = evaluate_help(returncode=1, stdout="usage: borgesica <command> ...\n")
    assert result.passed is False
    assert "exit code 1" in result.reason


def test_evaluate_engine_call_passes_on_expected_not_found_error() -> None:
    result = evaluate_engine_call(
        returncode=1,
        stderr="ERROR: Job 'nonexistent-job' not found.\n",
    )
    assert result.passed is True
    assert result.reason == "engine call executed real domain logic (JobNotFoundError path)"


def test_evaluate_engine_call_fails_when_engine_never_ran() -> None:
    # A traceback/import failure means the frozen provider modules never
    # loaded — the exact failure mode this spike exists to catch.
    result = evaluate_engine_call(
        returncode=1,
        stderr=(
            "ModuleNotFoundError: No module named "
            "'borgesica.adapters.providers.ollama_provider'\n"
        ),
    )
    assert result.passed is False
    assert "not found" not in result.reason
