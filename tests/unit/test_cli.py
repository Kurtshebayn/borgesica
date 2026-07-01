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
    """_build_engine raises or exits if ANTHROPIC_API_KEY is not set (default provider)."""
    import os
    from borgesica.__main__ import _build_engine

    # Remove key from environment for this test
    env_without_key = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    with patch.dict(os.environ, env_without_key, clear=True):
        with pytest.raises((SystemExit, ValueError, KeyError)):
            _build_engine(model="fake-model", db_path=":memory:")


# ---------------------------------------------------------------------------
# CLI wiring for EPUB/PDF + provider selection (post-M4 CLI wiring)
# ---------------------------------------------------------------------------


def test_source_type_for_maps_extensions() -> None:
    """_source_type_for detects SRT/EPUB/PDF from the file extension (case-insensitive)."""
    from borgesica.__main__ import _source_type_for
    from borgesica.domain.models import SourceType

    assert _source_type_for("subs.srt") == SourceType.SRT
    assert _source_type_for("SUBS.SRT") == SourceType.SRT
    assert _source_type_for("book.epub") == SourceType.EPUB
    assert _source_type_for("/path/to/Doc.PDF") == SourceType.PDF


def test_source_type_for_unknown_extension_raises() -> None:
    """An unsupported extension raises UnsupportedFormatError with a clear message."""
    from borgesica.__main__ import _source_type_for
    from borgesica.domain.errors import UnsupportedFormatError

    with pytest.raises(UnsupportedFormatError):
        _source_type_for("notes.txt")


def test_cli_create_epub_sets_epub_source_type() -> None:
    """`create book.epub` builds a JobConfig with source_type=EPUB (not hardcoded SRT)."""
    from unittest.mock import MagicMock

    from borgesica.__main__ import main
    from borgesica.domain.models import SourceType

    with patch("borgesica.__main__._build_engine") as mock_build:
        engine = MagicMock()
        engine.create_job.return_value = MagicMock(id="job-epub-1")
        mock_build.return_value = engine

        code = main(
            ["create", "book.epub", "--model", "deepseek-v4-flash", "--provider", "deepseek"]
        )

    assert code == 0
    # create_job(path, config) — inspect the config that was passed.
    passed_path, passed_config = engine.create_job.call_args.args
    assert passed_path == "book.epub"
    assert passed_config.source_type == SourceType.EPUB


def test_build_engine_deepseek_requires_deepseek_key() -> None:
    """provider=deepseek requires DEEPSEEK_API_KEY (not ANTHROPIC_API_KEY)."""
    import os

    from borgesica.__main__ import _build_engine

    env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises((SystemExit, ValueError, KeyError)):
            _build_engine(provider="deepseek", model="deepseek-v4-flash", db_path=":memory:")


def test_default_provider_resolution() -> None:
    """_default_provider: explicit BORGESICA_PROVIDER wins, else auto-detect from keys."""
    import os

    from borgesica.__main__ import _default_provider

    with patch.dict(os.environ, {"BORGESICA_PROVIDER": "ollama"}, clear=True):
        assert _default_provider() == "ollama"
    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "x"}, clear=True):
        assert _default_provider() == "deepseek"
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}, clear=True):
        assert _default_provider() == "anthropic"


def test_estimate_resolves_provider_from_env_not_hardcoded_anthropic() -> None:
    """`estimate <id>` with only DEEPSEEK_API_KEY set must NOT demand ANTHROPIC_API_KEY.

    Regression: estimate/status/cancel/glossary previously defaulted to anthropic
    and failed when the user only had a DeepSeek key.
    """
    import os
    from unittest.mock import MagicMock

    from borgesica.__main__ import main

    with (
        patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-x"}, clear=True),
        patch("borgesica.__main__._build_engine") as mock_build,
    ):
        engine = MagicMock()
        engine.estimate_cost.return_value = MagicMock(model_dump_json=lambda **k: "{}")
        mock_build.return_value = engine
        code = main(["estimate", "job-1"])

    assert code == 0
    assert mock_build.call_args.kwargs["provider"] == "deepseek"
