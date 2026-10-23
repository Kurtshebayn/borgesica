"""Worker-count wiring for parallel translation: the CLI flag and env var,
the serve default, and the engine rule that only a job with a prepared
glossary is translated in parallel (a parallel run freezes the glossary).
"""
from __future__ import annotations

import io
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from borgesica.api import TranslatorEngine
from borgesica.domain.models import (
    Chunk,
    Job,
    JobConfig,
    JobStatus,
    SourceType,
)
from borgesica.domain.orchestrator import TranslationOrchestrator
from tests.fakes import FakeTranslationProvider, InMemoryCheckpointStore

# ---------------------------------------------------------------------------
# CLI: --workers, BORGESICA_WORKERS, serve default
# ---------------------------------------------------------------------------


def _built_workers(argv: list[str], monkeypatch: pytest.MonkeyPatch, env: str | None) -> int:
    from borgesica.__main__ import main

    if env is None:
        monkeypatch.delenv("BORGESICA_WORKERS", raising=False)
    else:
        monkeypatch.setenv("BORGESICA_WORKERS", env)
    with patch("borgesica.__main__._build_engine") as mock_build, patch(
        "borgesica.__main__._run_server"
    ):
        engine = MagicMock()
        engine.status.return_value = MagicMock(
            config=MagicMock(source_type="SRT"), glossary_extracted=True
        )
        engine.run_job.return_value = MagicMock(status=JobStatus.DONE, cost_usd=0.0)
        engine.resume_job.return_value = MagicMock(status=JobStatus.DONE, cost_usd=0.0)
        engine.failed_chunk_indices.return_value = []
        engine.best_effort_chunk_indices.return_value = []
        mock_build.return_value = engine
        with patch("sys.stdin", io.StringIO('{"api_key": "sk-x"}\n')):
            main(argv)
    return mock_build.call_args.kwargs["workers"]


def test_run_defaults_to_one_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _built_workers(["run", "job-1", "--out", "o.srt"], monkeypatch, None) == 1


def test_run_and_resume_take_workers_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    run = ["run", "job-1", "--out", "o.srt", "--workers", "6"]
    resume = ["resume", "job-1", "--out", "o.srt", "--workers", "3"]
    assert _built_workers(run, monkeypatch, None) == 6
    assert _built_workers(resume, monkeypatch, None) == 3


def test_env_var_sets_workers_and_the_flag_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    argv = ["run", "job-1", "--out", "o.srt"]
    assert _built_workers(argv, monkeypatch, "5") == 5
    assert _built_workers([*argv, "--workers", "2"], monkeypatch, "5") == 2


def test_serve_defaults_to_eight_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    serve = ["serve", "--port", "0", "--provider", "anthropic", "--key-stdin"]
    assert _built_workers(serve, monkeypatch, None) == 8
    assert _built_workers(serve, monkeypatch, "4") == 4
    assert _built_workers([*serve, "--workers", "2"], monkeypatch, "4") == 2


@pytest.mark.parametrize("bad", ["0", "-1", "many"])
def test_invalid_workers_flag_is_rejected(bad: str) -> None:
    from borgesica.__main__ import _build_parser

    with pytest.raises(SystemExit):
        _build_parser().parse_args(["run", "job-1", "--workers", bad])


def test_invalid_workers_env_var_is_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from borgesica.__main__ import main

    monkeypatch.setenv("BORGESICA_WORKERS", "zero")
    with patch("borgesica.__main__._build_engine") as mock_build:
        code = main(["run", "job-1", "--out", "o.srt"])

    assert code == 1
    assert "BORGESICA_WORKERS" in capsys.readouterr().err
    mock_build.assert_not_called()


def _cli_stderr(
    argv: list[str],
    *,
    glossary_extracted: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> str:
    from borgesica.__main__ import main

    monkeypatch.delenv("BORGESICA_WORKERS", raising=False)
    with patch("borgesica.__main__._build_engine") as mock_build:
        engine = MagicMock()
        engine.status.return_value = MagicMock(
            config=MagicMock(source_type="SRT"), glossary_extracted=glossary_extracted
        )
        engine.run_job.return_value = MagicMock(status=JobStatus.DONE, cost_usd=0.0)
        engine.resume_job.return_value = MagicMock(status=JobStatus.DONE, cost_usd=0.0)
        engine.failed_chunk_indices.return_value = []
        engine.best_effort_chunk_indices.return_value = []
        mock_build.return_value = engine
        assert main(argv) == 0
    return capsys.readouterr().err


@pytest.mark.parametrize("command", ["run", "resume"])
def test_cli_says_when_a_job_without_glossary_runs_one_chunk_at_a_time(
    command: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    err = _cli_stderr(
        [command, "job-1", "--out", "o.srt", "--workers", "4"],
        glossary_extracted=False, monkeypatch=monkeypatch, capsys=capsys,
    )
    assert "one chunk at a time" in err
    assert "borgesica glossary extract job-1" in err


@pytest.mark.parametrize(
    ("argv", "glossary_extracted"),
    [
        (["resume", "job-1", "--out", "o.srt", "--workers", "4"], True),
        (["run", "job-1", "--out", "o.srt", "--workers", "4"], True),
        (["resume", "job-1", "--out", "o.srt"], False),
    ],
)
def test_cli_says_nothing_when_the_run_is_what_was_asked(
    argv: list[str],
    glossary_extracted: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A prepared glossary runs in parallel as asked, and a sequential run was
    never asked to be parallel: neither gets the NOTE."""
    err = _cli_stderr(
        argv, glossary_extracted=glossary_extracted, monkeypatch=monkeypatch, capsys=capsys
    )
    assert "one chunk at a time" not in err


# ---------------------------------------------------------------------------
# Engine: parallel only with a prepared glossary
# ---------------------------------------------------------------------------


def _engine_run_workers(*, workers: int, glossary_extracted: bool) -> int:
    from borgesica.adapters.writers.srt_writer import SrtWriter

    store = InMemoryCheckpointStore()
    now = datetime.now(UTC)
    job = Job(
        id="job-w",
        config=JobConfig(source_type=SourceType.SRT, model="fake-model"),
        source_path="in.srt",
        status=JobStatus.CREATED,
        total_chunks=1,
        created_at=now,
        updated_at=now,
        glossary_extracted=glossary_extracted,
    )
    store.save_job(job)
    store.save_chunk(job.id, Chunk(index=0, source_text="Hello there."))
    engine = TranslatorEngine(
        provider=FakeTranslationProvider(),
        checkpoint=store,
        readers={},
        writers={SourceType.SRT: MagicMock(spec=SrtWriter)},
        workers=workers,
    )
    seen: list[int] = []
    real_run = TranslationOrchestrator.run

    def spy(self, *args, **kwargs):
        seen.append(kwargs["workers"])
        return real_run(self, *args, **kwargs)

    with patch.object(TranslationOrchestrator, "run", spy):
        engine.run_job(job.id, out_path="out.srt")
    return seen[0]


def test_engine_runs_a_job_with_a_prepared_glossary_in_parallel() -> None:
    assert _engine_run_workers(workers=4, glossary_extracted=True) == 4


def test_engine_runs_a_job_without_a_prepared_glossary_sequentially() -> None:
    assert _engine_run_workers(workers=4, glossary_extracted=False) == 1


def test_engine_rejects_workers_below_one() -> None:
    with pytest.raises(ValueError, match="workers"):
        TranslatorEngine(
            provider=FakeTranslationProvider(),
            checkpoint=InMemoryCheckpointStore(),
            readers={},
            writers={},
            workers=0,
        )
