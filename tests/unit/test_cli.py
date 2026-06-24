"""Tests for M1-13 — thin CLI (borgesica command).

Smoke tests only: argparse wiring, env var handling.
No real API key required.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Test 1: CLI module is importable and has a main() entry point
# ---------------------------------------------------------------------------


def test_cli_module_importable() -> None:
    """borgesica.__main__ is importable and exposes a main() callable."""
    import borgesica.__main__ as cli

    assert callable(getattr(cli, "main", None)), "main() must be defined"


# ---------------------------------------------------------------------------
# Test 2: --help exits 0 without crashing
# ---------------------------------------------------------------------------


def test_cli_help_exits_cleanly() -> None:
    """borgesica --help exits 0."""
    import borgesica.__main__ as cli

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--help"])
    assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# Test 3: create subcommand prints a job ID given a valid SRT path
# ---------------------------------------------------------------------------


def test_cli_create_prints_job_id(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """borgesica create <srt> --model fake exits 0 and prints a UUID-like job ID."""
    # Write a minimal SRT fixture
    srt = tmp_path / "mini.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello world.\n\n",
        encoding="utf-8",
    )

    from borgesica.__main__ import main

    # Intercept AnthropicProvider import so no API key is needed.
    # We wire a fake engine instead by monkeypatching the module-level builder.
    with patch("borgesica.__main__._build_engine") as mock_build:
        from tests.fakes import FakeTranslationProvider, InMemoryCheckpointStore
        from borgesica.api import TranslatorEngine
        from borgesica.adapters.readers.srt_reader import SrtReader
        from borgesica.adapters.writers.srt_writer import SrtWriter
        from borgesica.domain.glossary import NullGlossaryExtractor
        from borgesica.domain.models import SourceType

        fake_engine = TranslatorEngine(
            provider=FakeTranslationProvider(),
            checkpoint=InMemoryCheckpointStore(),
            readers={SourceType.SRT: SrtReader()},
            writers={SourceType.SRT: SrtWriter()},
            extractor=NullGlossaryExtractor(),
        )
        mock_build.return_value = fake_engine

        exit_code = main(["create", str(srt), "--model", "fake-model"])

    out, _ = capsys.readouterr()
    # Should print something (the job id)
    assert exit_code == 0
    assert len(out.strip()) > 0, "Expected job ID output"


# ---------------------------------------------------------------------------
# Test 4: status subcommand raises cleanly for unknown job_id
# ---------------------------------------------------------------------------


def test_cli_status_unknown_job(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """borgesica status <unknown-id> exits non-zero and prints an error."""
    from borgesica.__main__ import main

    with patch("borgesica.__main__._build_engine") as mock_build:
        from tests.fakes import FakeTranslationProvider, InMemoryCheckpointStore
        from borgesica.api import TranslatorEngine
        from borgesica.adapters.readers.srt_reader import SrtReader
        from borgesica.adapters.writers.srt_writer import SrtWriter
        from borgesica.domain.glossary import NullGlossaryExtractor
        from borgesica.domain.models import SourceType

        fake_engine = TranslatorEngine(
            provider=FakeTranslationProvider(),
            checkpoint=InMemoryCheckpointStore(),
            readers={SourceType.SRT: SrtReader()},
            writers={SourceType.SRT: SrtWriter()},
            extractor=NullGlossaryExtractor(),
        )
        mock_build.return_value = fake_engine

        exit_code = main(["status", "no-such-job-id"])

    assert exit_code != 0


# ---------------------------------------------------------------------------
# Test 5: missing ANTHROPIC_API_KEY in real _build_engine path
# ---------------------------------------------------------------------------


def test_build_engine_requires_api_key() -> None:
    """_build_engine raises or exits if ANTHROPIC_API_KEY is not set."""
    import os
    from borgesica.__main__ import _build_engine

    # Remove key from environment for this test
    env_without_key = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    with patch.dict(os.environ, env_without_key, clear=True):
        with pytest.raises((SystemExit, ValueError, KeyError)):
            _build_engine(model="fake-model", db_path=":memory:")
