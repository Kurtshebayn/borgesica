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


def test_default_provider_resolution_selects_openai_when_only_key_present() -> None:
    """_default_provider: OPENAI_API_KEY alone (no Anthropic/DeepSeek keys, no
    BORGESICA_PROVIDER) auto-selects openai (spec: 'OpenAI selected when it is
    the only lower-precedence key present')."""
    import os

    from borgesica.__main__ import _default_provider

    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-oa"}, clear=True):
        assert _default_provider() == "openai"


def test_default_provider_resolution_anthropic_beats_openai() -> None:
    """_default_provider: ANTHROPIC_API_KEY still wins over OPENAI_API_KEY —
    existing precedence unaffected by adding OpenAI (spec scenario)."""
    import os

    from borgesica.__main__ import _default_provider

    with patch.dict(
        os.environ, {"ANTHROPIC_API_KEY": "sk-a", "OPENAI_API_KEY": "sk-oa"}, clear=True
    ):
        assert _default_provider() == "anthropic"


def test_build_engine_openai_requires_openai_key() -> None:
    """provider=openai requires OPENAI_API_KEY (not ANTHROPIC_API_KEY)."""
    import os

    from borgesica.__main__ import _build_engine

    env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises((SystemExit, ValueError, KeyError)):
            _build_engine(provider="openai", model="gpt-5.6-luna", db_path=":memory:")


def test_build_provider_openai_resolves_key_and_targets_openai_base_url() -> None:
    """_build_provider('openai', ...) resolves OPENAI_API_KEY and dispatches to
    OpenAICompatibleProvider.openai() (spec: 'Explicit selection dispatches to
    OpenAI')."""
    import os

    from borgesica.__main__ import _build_provider

    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-oa"}, clear=True):
        provider = _build_provider("openai")

    assert provider.base_url == "https://api.openai.com/v1"
    assert provider.default_model == "gpt-5.6-luna"


def test_estimate_resolves_provider_from_env_not_hardcoded_anthropic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`estimate <id>` with only DEEPSEEK_API_KEY set must NOT demand ANTHROPIC_API_KEY.

    Regression: estimate/status/cancel/glossary previously defaulted to anthropic
    and failed when the user only had a DeepSeek key.

    Runs from an empty directory on purpose. main() loads a project-local .env, so
    "only DEEPSEEK_API_KEY is set" only holds where no .env can contribute other
    keys — otherwise this test would pass or fail depending on whether the person
    running it happens to have an untracked .env above the checkout.
    """
    import os
    from unittest.mock import MagicMock

    from borgesica.__main__ import main

    monkeypatch.chdir(tmp_path)

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


# ---------------------------------------------------------------------------
# continue-on-error WU4-1 — `--strict` flag on `create`
# Spec: job-lifecycle/"JobConfig.continue_on_error gates chunk-failure
# handling, default ON" (scenario: --strict flag sets continue_on_error=False)
# ---------------------------------------------------------------------------


def test_strict_flag_defaults_to_false() -> None:
    """--strict is absent by default → args.strict is False."""
    from borgesica.__main__ import _build_parser

    args = _build_parser().parse_args(["create", "book.epub", "--model", "x"])
    assert args.strict is False


def test_strict_flag_parses_true_when_given() -> None:
    """--strict sets args.strict to True."""
    from borgesica.__main__ import _build_parser

    args = _build_parser().parse_args(["create", "book.epub", "--model", "x", "--strict"])
    assert args.strict is True


def test_cmd_create_strict_sets_continue_on_error_false() -> None:
    """`create --strict` builds a JobConfig with continue_on_error=False."""
    from unittest.mock import MagicMock

    from borgesica.__main__ import main

    with patch("borgesica.__main__._build_engine") as mock_build:
        engine = MagicMock()
        engine.create_job.return_value = MagicMock(id="job-strict-1")
        mock_build.return_value = engine

        code = main(["create", "book.epub", "--model", "x", "--strict"])

    assert code == 0
    _, passed_config = engine.create_job.call_args.args
    assert passed_config.continue_on_error is False


def test_cmd_create_without_strict_sets_continue_on_error_true() -> None:
    """`create` without --strict builds a JobConfig with continue_on_error=True."""
    from unittest.mock import MagicMock

    from borgesica.__main__ import main

    with patch("borgesica.__main__._build_engine") as mock_build:
        engine = MagicMock()
        engine.create_job.return_value = MagicMock(id="job-default-1")
        mock_build.return_value = engine

        code = main(["create", "book.epub", "--model", "x"])

    assert code == 0
    _, passed_config = engine.create_job.call_args.args
    assert passed_config.continue_on_error is True


# ---------------------------------------------------------------------------
# A1 — Extract mode: `create --extract N` translates only the first N chunks
# ---------------------------------------------------------------------------


def test_extract_flag_parses_int_when_given() -> None:
    """--extract sets args.extract_chunks to the given int."""
    from borgesica.__main__ import _build_parser

    args = _build_parser().parse_args(
        ["create", "book.epub", "--model", "x", "--extract", "5"]
    )
    assert args.extract_chunks == 5


def test_extract_flag_defaults_to_none() -> None:
    """Without --extract, extract_chunks is None (translate everything)."""
    from borgesica.__main__ import _build_parser

    args = _build_parser().parse_args(["create", "book.epub", "--model", "x"])
    assert args.extract_chunks is None


def test_cmd_create_extract_sets_extract_chunks_on_config() -> None:
    """`create --extract 3` builds a JobConfig with extract_chunks=3."""
    from unittest.mock import MagicMock

    from borgesica.__main__ import main

    with patch("borgesica.__main__._build_engine") as mock_build:
        engine = MagicMock()
        engine.create_job.return_value = MagicMock(id="job-extract-1")
        mock_build.return_value = engine

        code = main(["create", "book.epub", "--model", "x", "--extract", "3"])

    assert code == 0
    _, passed_config = engine.create_job.call_args.args
    assert passed_config.extract_chunks == 3


def test_from_flag_parses_int_and_defaults_to_zero() -> None:
    """--from sets args.extract_offset; absent, it is 0."""
    from borgesica.__main__ import _build_parser

    with_flag = _build_parser().parse_args(
        ["create", "book.epub", "--model", "x", "--extract", "20", "--from", "380"]
    )
    without = _build_parser().parse_args(["create", "book.epub", "--model", "x"])

    assert with_flag.extract_offset == 380
    assert without.extract_offset == 0


def test_cmd_create_from_sets_extract_offset_on_config() -> None:
    """`create --extract 20 --from 380` reaches JobConfig intact."""
    from unittest.mock import MagicMock

    from borgesica.__main__ import main

    with patch("borgesica.__main__._build_engine") as mock_build:
        engine = MagicMock()
        engine.create_job.return_value = MagicMock(id="job-extract-3")
        mock_build.return_value = engine

        code = main(
            ["create", "book.epub", "--model", "x", "--extract", "20", "--from", "380"]
        )

    assert code == 0
    _, passed_config = engine.create_job.call_args.args
    assert passed_config.extract_chunks == 20
    assert passed_config.extract_offset == 380


def test_cmd_create_reports_out_of_range_offset_without_a_traceback() -> None:
    """An offset past the last chunk is a user error, not a crash."""
    from unittest.mock import MagicMock

    from borgesica.__main__ import main

    with patch("borgesica.__main__._build_engine") as mock_build:
        engine = MagicMock()
        engine.create_job.side_effect = ValueError("extract offset 99 is past the last chunk")
        mock_build.return_value = engine

        code = main(["create", "book.epub", "--model", "x", "--from", "99"])

    assert code == 1


def test_cmd_create_without_extract_leaves_extract_chunks_none() -> None:
    """`create` without --extract builds a JobConfig with extract_chunks=None."""
    from unittest.mock import MagicMock

    from borgesica.__main__ import main

    with patch("borgesica.__main__._build_engine") as mock_build:
        engine = MagicMock()
        engine.create_job.return_value = MagicMock(id="job-extract-2")
        mock_build.return_value = engine

        code = main(["create", "book.epub", "--model", "x"])

    assert code == 0
    _, passed_config = engine.create_job.call_args.args
    assert passed_config.extract_chunks is None


# ---------------------------------------------------------------------------
# continue-on-error WU4-2 — `run` prints a skip-summary line when chunks failed
# Spec: job-lifecycle/"skip report surfaces FAILED chunk indices at end of run"
# (scenario: CLI run prints a skip-summary line when chunks failed)
# ---------------------------------------------------------------------------


def test_cmd_run_prints_skip_summary_when_chunks_failed(capsys: pytest.CaptureFixture) -> None:
    """run prints a WARNING line naming the count and indices of FAILED chunks."""
    from unittest.mock import MagicMock

    from borgesica.__main__ import main
    from borgesica.domain.models import JobStatus

    with patch("borgesica.__main__._build_engine") as mock_build:
        engine = MagicMock()
        engine.status.return_value = MagicMock(config=MagicMock(source_type="SRT"))
        done_job = MagicMock(status=JobStatus.DONE, cost_usd=0.001)
        engine.run_job.return_value = done_job
        engine.failed_chunk_indices.return_value = [2, 5]
        mock_build.return_value = engine

        code = main(["run", "job-1", "--out", "out.srt"])

    out, _ = capsys.readouterr()
    assert code == 0
    assert "2" in out
    assert "[2, 5]" in out


def test_cmd_run_skip_summary_offers_resume(capsys: pytest.CaptureFixture) -> None:
    """The skip summary must state the consequence (failed chunks keep their
    source-language text in the output) and the remedy: `resume` retries
    ONLY the failed chunks."""
    from unittest.mock import MagicMock

    from borgesica.__main__ import main
    from borgesica.domain.models import JobStatus

    with patch("borgesica.__main__._build_engine") as mock_build:
        engine = MagicMock()
        engine.status.return_value = MagicMock(config=MagicMock(source_type="SRT"))
        done_job = MagicMock(status=JobStatus.DONE, cost_usd=0.001)
        engine.run_job.return_value = done_job
        engine.failed_chunk_indices.return_value = [2, 5]
        mock_build.return_value = engine

        code = main(["run", "job-1", "--out", "out.srt"])

    out, _ = capsys.readouterr()
    assert code == 0
    assert "untranslated" in out
    assert "borgesica resume job-1" in out


def test_cmd_run_no_skip_summary_when_no_failures(capsys: pytest.CaptureFixture) -> None:
    """run prints no skip-summary line when failed_chunk_indices returns []."""
    from unittest.mock import MagicMock

    from borgesica.__main__ import main
    from borgesica.domain.models import JobStatus

    with patch("borgesica.__main__._build_engine") as mock_build:
        engine = MagicMock()
        done_job = MagicMock(status=JobStatus.DONE, cost_usd=0.001)
        engine.run_job.return_value = done_job
        engine.failed_chunk_indices.return_value = []
        mock_build.return_value = engine

        code = main(["run", "job-1", "--out", "out.srt"])

    out, _ = capsys.readouterr()
    assert code == 0
    assert "WARNING" not in out
    assert "Done. Status=" in out


# ---------------------------------------------------------------------------
# T4 — `run`/`resume` print a best-effort summary line when N>0 chunks have
# passed_validation=False, and print nothing (no noise) when N=0.
# Spec: "End-of-run best-effort summary" (rev 2).
# ---------------------------------------------------------------------------


def test_cmd_run_prints_best_effort_summary_when_present(capsys: pytest.CaptureFixture) -> None:
    """run prints a NOTE line naming the count and indices of best-effort
    chunks (translated but not passing validation)."""
    from unittest.mock import MagicMock

    from borgesica.__main__ import main
    from borgesica.domain.models import JobStatus

    with patch("borgesica.__main__._build_engine") as mock_build:
        engine = MagicMock()
        engine.status.return_value = MagicMock(config=MagicMock(source_type="SRT"))
        done_job = MagicMock(status=JobStatus.DONE, cost_usd=0.001)
        engine.run_job.return_value = done_job
        engine.failed_chunk_indices.return_value = []
        engine.best_effort_chunk_indices.return_value = [1, 4]
        mock_build.return_value = engine

        code = main(["run", "job-1", "--out", "out.srt"])

    out, _ = capsys.readouterr()
    assert code == 0
    assert "2" in out
    assert "[1, 4]" in out


def test_cmd_run_no_best_effort_summary_when_zero(capsys: pytest.CaptureFixture) -> None:
    """run prints no best-effort NOTE line when best_effort_chunk_indices
    returns [] (no noise on the happy path)."""
    from unittest.mock import MagicMock

    from borgesica.__main__ import main
    from borgesica.domain.models import JobStatus

    with patch("borgesica.__main__._build_engine") as mock_build:
        engine = MagicMock()
        done_job = MagicMock(status=JobStatus.DONE, cost_usd=0.001)
        engine.run_job.return_value = done_job
        engine.failed_chunk_indices.return_value = []
        engine.best_effort_chunk_indices.return_value = []
        mock_build.return_value = engine

        code = main(["run", "job-1", "--out", "out.srt"])

    out, _ = capsys.readouterr()
    assert code == 0
    assert "NOTE" not in out
    assert "Done. Status=" in out


def test_cmd_resume_prints_best_effort_summary_when_present(
    capsys: pytest.CaptureFixture,
) -> None:
    """resume also prints the best-effort NOTE line when present."""
    from unittest.mock import MagicMock

    from borgesica.__main__ import main
    from borgesica.domain.models import JobStatus

    with patch("borgesica.__main__._build_engine") as mock_build:
        engine = MagicMock()
        engine.status.return_value = MagicMock(config=MagicMock(source_type="SRT"))
        done_job = MagicMock(status=JobStatus.DONE, cost_usd=0.001)
        engine.resume_job.return_value = done_job
        engine.failed_chunk_indices.return_value = []
        engine.best_effort_chunk_indices.return_value = [0]
        mock_build.return_value = engine

        code = main(["resume", "job-1", "--out", "out.srt"])

    out, _ = capsys.readouterr()
    assert code == 0
    assert "1" in out
    assert "[0]" in out


# ---------------------------------------------------------------------------
# continue-on-error WU4-3 — `status` lists FAILED chunk indices when present
# Spec: job-lifecycle/"skip report surfaces FAILED chunk indices at end of run"
# (scenario: CLI status lists no failed chunks when none exist)
# ---------------------------------------------------------------------------


def test_cmd_status_lists_failed_chunks_when_present(capsys: pytest.CaptureFixture) -> None:
    """status prints a line listing FAILED chunk index 3 when present."""
    from unittest.mock import MagicMock

    from borgesica.__main__ import main

    with patch("borgesica.__main__._build_engine") as mock_build:
        engine = MagicMock()
        engine.status.return_value = MagicMock(model_dump_json=lambda **k: "{}")
        engine.failed_chunk_indices.return_value = [3]
        mock_build.return_value = engine

        code = main(["status", "job-1"])

    out, _ = capsys.readouterr()
    assert code == 0
    assert "3" in out
    assert "Failed chunks" in out


def test_cmd_status_no_failed_chunk_line_when_none(capsys: pytest.CaptureFixture) -> None:
    """status output is exactly the JSON dump when no chunks are FAILED."""
    from unittest.mock import MagicMock

    from borgesica.__main__ import main

    with patch("borgesica.__main__._build_engine") as mock_build:
        engine = MagicMock()
        engine.status.return_value = MagicMock(model_dump_json=lambda **k: "{}")
        engine.failed_chunk_indices.return_value = []
        mock_build.return_value = engine

        code = main(["status", "job-1"])

    out, _ = capsys.readouterr()
    assert code == 0
    assert out.strip() == "{}"
    assert "Failed chunks" not in out


# ---------------------------------------------------------------------------
# API key from stdin (priority #1: public desktop app).
# The desktop shell (Tauri/Rust) reads the key from the OS keyring and hands it
# to this subprocess as a single JSON init line on stdin — never on argv (visible
# in ps) nor a persisted env var. Only the SECRET moves to stdin; the subcommand,
# job_id, provider and model stay on argv. Framing is newline-delimited JSON.
# ---------------------------------------------------------------------------


def test_read_key_from_stdin_returns_api_key() -> None:
    """_read_key_from_stdin parses {"api_key": "..."} from a single stdin line."""
    import io

    from borgesica.__main__ import _read_key_from_stdin

    with patch("sys.stdin", io.StringIO('{"api_key": "sk-from-stdin"}\n')):
        assert _read_key_from_stdin("anthropic") == "sk-from-stdin"


def test_read_key_from_stdin_missing_key_exits() -> None:
    """A JSON line without api_key exits(1) — the shell must supply the key."""
    import io

    from borgesica.__main__ import _read_key_from_stdin

    with patch("sys.stdin", io.StringIO("{}\n")):
        with pytest.raises(SystemExit) as exc:
            _read_key_from_stdin("anthropic")
    assert exc.value.code == 1


def test_read_key_from_stdin_malformed_json_exits() -> None:
    """Non-JSON on stdin exits(1) rather than passing garbage as a key."""
    import io

    from borgesica.__main__ import _read_key_from_stdin

    with patch("sys.stdin", io.StringIO("not json at all\n")):
        with pytest.raises(SystemExit) as exc:
            _read_key_from_stdin("anthropic")
    assert exc.value.code == 1


def test_read_key_from_stdin_captures_session_token() -> None:
    """RISK-001/002/003: a "token" field on the same init line is captured
    into the module-level _stdin_session_token for _cmd_serve to pick up."""
    import io

    import borgesica.__main__ as main_module

    with patch(
        "sys.stdin",
        io.StringIO('{"api_key": "sk-from-stdin", "token": "session-tok-xyz"}\n'),
    ):
        main_module._read_key_from_stdin("anthropic")

    assert main_module._stdin_session_token == "session-tok-xyz"


def test_read_key_from_stdin_resets_token_when_absent() -> None:
    """A subsequent read whose init line carries no "token" resets the
    module-level value to None rather than leaking a stale token from a
    prior read."""
    import io

    import borgesica.__main__ as main_module

    with patch(
        "sys.stdin",
        io.StringIO('{"api_key": "sk-1", "token": "stale-token"}\n'),
    ):
        main_module._read_key_from_stdin("anthropic")
    assert main_module._stdin_session_token == "stale-token"

    with patch("sys.stdin", io.StringIO('{"api_key": "sk-2"}\n')):
        main_module._read_key_from_stdin("anthropic")

    assert main_module._stdin_session_token is None


def test_build_engine_key_stdin_bypasses_env() -> None:
    """With key_stdin=True the key comes from stdin — a missing env var must
    NOT abort (the whole point: no env dependency in the desktop app)."""
    import io
    import os

    from borgesica.__main__ import _build_engine
    from borgesica.api import TranslatorEngine

    env_without_key = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    with patch.dict(os.environ, env_without_key, clear=True):
        with patch("sys.stdin", io.StringIO('{"api_key": "sk-stdin"}\n')):
            engine = _build_engine(
                provider="anthropic", model="claude-haiku-4-5", db_path=":memory:", key_stdin=True
            )
    assert isinstance(engine, TranslatorEngine)


def test_build_engine_deepseek_key_stdin_bypasses_env() -> None:
    """key_stdin path works for deepseek too (OpenAI-compatible provider)."""
    import io
    import os

    from borgesica.__main__ import _build_engine
    from borgesica.api import TranslatorEngine

    env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
    with patch.dict(os.environ, env, clear=True):
        with patch("sys.stdin", io.StringIO('{"api_key": "sk-ds-stdin"}\n')):
            engine = _build_engine(
                provider="deepseek", model="deepseek-v4-flash", db_path=":memory:", key_stdin=True
            )
    assert isinstance(engine, TranslatorEngine)


def test_run_command_accepts_key_stdin_flag() -> None:
    """The --key-stdin flag is wired on provider commands and threaded into
    _build_engine as key_stdin=True."""
    import io
    from unittest.mock import MagicMock

    from borgesica.__main__ import main

    with patch("borgesica.__main__._build_engine") as mock_build:
        engine = MagicMock()
        engine.run_job.return_value = MagicMock(status="DONE", cost_usd=0.0)
        engine.failed_chunk_indices.return_value = []
        engine.status.return_value = MagicMock(config=MagicMock(source_type=None))
        mock_build.return_value = engine
        with patch("sys.stdin", io.StringIO('{"api_key": "sk-x"}\n')):
            main(["run", "job-1", "--out", "out.srt", "--provider", "anthropic", "--key-stdin"])

    assert mock_build.call_args.kwargs.get("key_stdin") is True


class TestProgressPrinter:
    """Extracted chunks keep their original book indices, so the printer must
    use Progress.position. Using chunk_index printed "chunk 387/20 (1935%)"
    on a --extract 20 --from 380 run."""

    def _line(self, capsys, **kwargs) -> str:
        from borgesica.__main__ import _print_progress
        from borgesica.domain.models import JobStatus, Progress

        payload = dict(
            job_id="j",
            chunk_index=386,
            position=7,
            total_chunks=20,
            cost_usd=0.00526,
            status=JobStatus.RUNNING,
        )
        payload.update(kwargs)
        _print_progress(Progress(**payload))
        return capsys.readouterr().out.strip()

    def test_uses_the_run_position_not_the_book_index(self, capsys):
        line = self._line(capsys)

        assert "7/20" in line, f"expected the run position, got: {line}"
        assert "387/20" not in line, f"book index leaked into progress: {line}"
        assert "(35%)" in line, f"expected 7/20 = 35%, got: {line}"

    def test_percentage_never_exceeds_one_hundred(self, capsys):
        line = self._line(capsys, position=20, chunk_index=399)

        assert "20/20" in line
        assert "(100%)" in line, line

    def test_zero_total_does_not_divide_by_zero(self, capsys):
        line = self._line(capsys, position=0, total_chunks=0)

        assert "(0%)" in line, line
