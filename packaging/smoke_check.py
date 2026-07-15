"""Manual smoke check for a PyInstaller-frozen borgesica binary (T1 spike).

Not part of the automated test suite — no frozen binary exists in CI. Run
this by hand after `pyinstaller packaging/borgesica.spec` to confirm the
freeze actually works before starting T7a (desktop sidecar) work:

    python packaging/smoke_check.py dist/borgesica/borgesica.exe

The PASS/FAIL decision logic itself is pure and unit-tested in
tests/unit/test_packaging_smoke_check.py via
borgesica.packaging_smoke_check.{evaluate_help,evaluate_engine_call}.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from borgesica.packaging_smoke_check import evaluate_engine_call, evaluate_help  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("Usage: python packaging/smoke_check.py <path-to-frozen-binary>", file=sys.stderr)
        return 2
    binary = argv[0]

    help_run = subprocess.run([binary, "--help"], capture_output=True, text=True, timeout=30)
    help_result = evaluate_help(returncode=help_run.returncode, stdout=help_run.stdout)
    print(f"[help]   {'PASS' if help_result.passed else 'FAIL'} — {help_result.reason}")

    # --provider ollama needs no API key, so a clean run proves the ollama
    # adapter module loaded and a real engine call executed inside the
    # frozen binary, without requiring credentials for this smoke check.
    engine_run = subprocess.run(
        [binary, "estimate", "nonexistent-job-id", "--provider", "ollama"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    engine_result = evaluate_engine_call(returncode=engine_run.returncode, stderr=engine_run.stderr)
    print(f"[engine] {'PASS' if engine_result.passed else 'FAIL'} — {engine_result.reason}")

    return 0 if (help_result.passed and engine_result.passed) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
