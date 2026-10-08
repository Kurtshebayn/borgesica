"""Tests for T6 — borgesica.serve run/resume execution, SSE progress stream,
cancellation, and graceful shutdown.

All tests build a real TranslatorEngine wired with FakeTranslationProvider (or
a small blocking subclass for chunk-boundary synchronization) + InMemory
CheckpointStore, and exercise the FastAPI app via TestClient. No network, no
live provider calls, no real uvicorn server bound to a port, and no
arbitrary/flaky real-time sleeps — synchronization uses threading.Event and
bounded thread.join() only.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from borgesica.api import TranslatorEngine
from borgesica.domain.models import (
    GlossaryEntry,
    GlossaryExtractionOutcome,
    JobStatus,
    SourceType,
    TranslationUnit,
)
from tests.fakes import FakeTranslationProvider, InMemoryCheckpointStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_srt_fixture(tmp_path: Path, num_cues: int = 2) -> str:
    lines = []
    for i in range(1, num_cues + 1):
        start_ms = (i - 1) * 2000
        end_ms = start_ms + 1500
        start = f"00:00:{start_ms // 1000:02d},{start_ms % 1000:03d}"
        end = f"00:00:{end_ms // 1000:02d},{end_ms % 1000:03d}"
        lines += [str(i), f"{start} --> {end}", f"Cue {i} text.", ""]
    srt_path = tmp_path / "test.srt"
    srt_path.write_text("\n".join(lines), encoding="utf-8")
    return str(srt_path)


class _BlockingOnFirstCallProvider(FakeTranslationProvider):
    """Blocks the FIRST translate() call until *release* is set, signalling
    *started* right before blocking. Lets tests synchronize on "chunk 0 is
    currently being translated" without any real-time sleep."""

    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self._started = started
        self._release = release

    def translate(self, system: str, user: str, model: str, segment_count: int | None = None):
        if self.call_count == 0:
            self._started.set()
            self._release.wait(timeout=5)
        return super().translate(system, user, model, segment_count)


def _make_engine(provider: FakeTranslationProvider | None = None):
    from borgesica.adapters.readers.srt_reader import SrtReader
    from borgesica.adapters.writers.srt_writer import SrtWriter
    from borgesica.domain.glossary import NullGlossaryExtractor

    checkpoint = InMemoryCheckpointStore()
    engine = TranslatorEngine(
        provider=provider if provider is not None else FakeTranslationProvider(),
        checkpoint=checkpoint,
        readers={SourceType.SRT: SrtReader()},
        writers={SourceType.SRT: SrtWriter()},
        extractor=NullGlossaryExtractor(),
    )
    return engine, checkpoint


def _create_job(
    client: TestClient, tmp_path: Path, num_cues: int = 2, chunk_size: int = 25
) -> str:
    srt_path = _make_srt_fixture(tmp_path, num_cues=num_cues)
    resp = client.post(
        "/jobs",
        json={"source_path": srt_path, "model": "fake-model", "chunk_size": chunk_size},
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def _app_and_client(engine, **kwargs) -> TestClient:
    from borgesica.serve.app import create_app

    return TestClient(create_app(engine, **kwargs))


# ---------------------------------------------------------------------------
# POST /jobs/{id}/run — background execution + immediate RUNNING ack
# ---------------------------------------------------------------------------


def test_run_returns_immediately_with_running_ack(tmp_path: Path) -> None:
    engine, checkpoint = _make_engine()
    client = _app_and_client(engine)
    job_id = _create_job(client, tmp_path)
    out_path = str(tmp_path / "out.srt")

    resp = client.post(f"/jobs/{job_id}/run", json={"out_path": out_path})

    assert resp.status_code == 202
    assert resp.json()["status"] == "RUNNING"

    runner = client.app.state.runner
    runner._handles[job_id].thread.join(timeout=5)
    assert Path(out_path).exists()


def test_run_unknown_job_404(tmp_path: Path) -> None:
    engine, _ = _make_engine()
    client = _app_and_client(engine)

    resp = client.post("/jobs/does-not-exist/run", json={"out_path": str(tmp_path / "out.srt")})

    assert resp.status_code == 404


def test_run_conflict_when_already_running(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    provider = _BlockingOnFirstCallProvider(started, release)
    engine, checkpoint = _make_engine(provider=provider)
    client = _app_and_client(engine)
    job_id = _create_job(client, tmp_path)
    out_path = str(tmp_path / "out.srt")

    resp1 = client.post(f"/jobs/{job_id}/run", json={"out_path": out_path})
    assert resp1.status_code == 202
    assert started.wait(timeout=2)

    resp2 = client.post(f"/jobs/{job_id}/run", json={"out_path": out_path})
    assert resp2.status_code == 409

    release.set()
    runner = client.app.state.runner
    runner._handles[job_id].thread.join(timeout=5)


# ---------------------------------------------------------------------------
# POST /jobs/{id}/cancel — cooperative, chunk-granular
# ---------------------------------------------------------------------------


def test_cancel_running_job_stops_before_next_chunk(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    provider = _BlockingOnFirstCallProvider(started, release)
    engine, checkpoint = _make_engine(provider=provider)
    client = _app_and_client(engine)
    job_id = _create_job(client, tmp_path, num_cues=2, chunk_size=1)
    out_path = str(tmp_path / "out.srt")

    client.post(f"/jobs/{job_id}/run", json={"out_path": out_path})
    assert started.wait(timeout=2)

    cancel_resp = client.post(f"/jobs/{job_id}/cancel")
    assert cancel_resp.status_code == 202

    release.set()
    runner = client.app.state.runner
    runner._handles[job_id].thread.join(timeout=5)

    final = client.get(f"/jobs/{job_id}").json()
    assert final["status"] == "CANCELLED"


# ---------------------------------------------------------------------------
# GET /jobs/{id}/events — SSE progress stream
# ---------------------------------------------------------------------------


def test_sse_stream_emits_progress_then_terminal_event(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    provider = _BlockingOnFirstCallProvider(started, release)
    engine, checkpoint = _make_engine(provider=provider)
    client = _app_and_client(engine)
    job_id = _create_job(client, tmp_path, num_cues=2)
    out_path = str(tmp_path / "out.srt")

    client.post(f"/jobs/{job_id}/run", json={"out_path": out_path})
    assert started.wait(timeout=2)

    events: list[dict] = []
    with client.stream("GET", f"/jobs/{job_id}/events") as stream_resp:
        assert stream_resp.status_code == 200
        release.set()
        for line in stream_resp.iter_lines():
            if not line or line.startswith(":"):
                continue
            assert line.startswith("data: ")
            events.append(json.loads(line[len("data: ") :]))
            if events[-1]["type"] == "terminal":
                break

    assert any(e["type"] == "progress" for e in events)
    assert events[-1] == {"type": "terminal", "status": "DONE", "error": None}

    runner = client.app.state.runner
    runner._handles[job_id].thread.join(timeout=5)


def test_sse_stream_connects_before_running_persisted_gets_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REL-001: the SSE stream must not read a premature terminal event when
    a client connects in the window after POST /run returns (worker thread
    started, registered as active in JobRunner) but BEFORE the worker has
    persisted RUNNING. Regression: sse_event_stream used to read a fresh
    `engine.status(job_id)` first and, seeing it still CREATED, emit a
    terminal event and return without ever consuming the runner's live
    progress queue."""
    engine, checkpoint = _make_engine()
    client = _app_and_client(engine)
    job_id = _create_job(client, tmp_path, num_cues=1)
    out_path = str(tmp_path / "out.srt")

    entered = threading.Event()
    proceed = threading.Event()
    original_execute = engine._execute_job

    def _blocking_execute(job_arg, *, out_path, on_progress):
        entered.set()
        proceed.wait(timeout=5)
        return original_execute(job_arg, out_path=out_path, on_progress=on_progress)

    monkeypatch.setattr(engine, "_execute_job", _blocking_execute)

    resp = client.post(f"/jobs/{job_id}/run", json={"out_path": out_path})
    assert resp.status_code == 202
    assert entered.wait(timeout=2)

    # Worker thread started (registered active in JobRunner) but blocked
    # before persisting RUNNING — a fresh status read still shows CREATED.
    assert engine.status(job_id).status == JobStatus.CREATED

    events: list[dict] = []
    with client.stream("GET", f"/jobs/{job_id}/events") as stream_resp:
        assert stream_resp.status_code == 200
        proceed.set()
        for line in stream_resp.iter_lines():
            if not line or line.startswith(":"):
                continue
            assert line.startswith("data: ")
            events.append(json.loads(line[len("data: ") :]))
            if events[-1]["type"] == "terminal":
                break

    assert any(e["type"] == "progress" for e in events)
    assert events[-1] == {"type": "terminal", "status": "DONE", "error": None}

    runner = client.app.state.runner
    runner._handles[job_id].thread.join(timeout=5)


def test_sse_stream_unknown_job_404() -> None:
    engine, _ = _make_engine()
    client = _app_and_client(engine)

    resp = client.get("/jobs/does-not-exist/events")

    assert resp.status_code == 404


def test_sse_stream_reconnect_after_completion_returns_terminal_only(tmp_path: Path) -> None:
    """Reconnection fallback (spec): connecting after the job already
    finished yields one terminal event reflecting persisted status, no
    restart, no hang."""
    engine, checkpoint = _make_engine()
    client = _app_and_client(engine)
    job_id = _create_job(client, tmp_path)
    out_path = str(tmp_path / "out.srt")

    client.post(f"/jobs/{job_id}/run", json={"out_path": out_path})
    runner = client.app.state.runner
    runner._handles[job_id].thread.join(timeout=5)

    events: list[dict] = []
    with client.stream("GET", f"/jobs/{job_id}/events") as stream_resp:
        assert stream_resp.status_code == 200
        for line in stream_resp.iter_lines():
            if not line or line.startswith(":"):
                continue
            events.append(json.loads(line[len("data: ") :]))

    assert events == [{"type": "terminal", "status": "DONE", "error": None}]


# ---------------------------------------------------------------------------
# POST /shutdown — graceful shutdown with a running job
# ---------------------------------------------------------------------------


def test_shutdown_cancels_running_job(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    provider = _BlockingOnFirstCallProvider(started, release)
    engine, checkpoint = _make_engine(provider=provider)
    client = _app_and_client(engine, shutdown_join_timeout=0.2)
    job_id = _create_job(client, tmp_path, num_cues=2, chunk_size=1)
    out_path = str(tmp_path / "out.srt")

    client.post(f"/jobs/{job_id}/run", json={"out_path": out_path})
    assert started.wait(timeout=2)

    shutdown_resp = client.post("/shutdown")
    assert shutdown_resp.status_code == 200
    # Still blocked in translate() — the bounded join could not finish yet.
    assert shutdown_resp.json()["clean"] is False

    release.set()
    runner = client.app.state.runner
    handle = runner._handles[job_id]
    assert handle.thread.join(timeout=5) is None
    assert not handle.thread.is_alive()

    final = client.get(f"/jobs/{job_id}").json()
    assert final["status"] == "CANCELLED"


def test_shutdown_with_no_active_run_is_clean() -> None:
    engine, _ = _make_engine()
    client = _app_and_client(engine)

    resp = client.post("/shutdown")

    assert resp.status_code == 200
    assert resp.json() == {"status": "shutting_down", "clean": True}


# ---------------------------------------------------------------------------
# POST /jobs/{id}/glossary/extract — the explicit, paid pre-translation pass
# ---------------------------------------------------------------------------

# What the scripted extraction call proposes. "Cue" occurs in every fixture
# cue, so the source-text hygiene rules keep it.
_EXTRACTED = TranslationUnit(
    translation="",
    translations=[],
    summary_update="Terminology extraction complete.",
    glossary_additions=[GlossaryEntry(term="Cue", translation="Señal")],
)


def _read_events(stream_resp, terminal_types=("terminal", "glossary_terminal")) -> list[dict]:
    events: list[dict] = []
    for line in stream_resp.iter_lines():
        if not line or line.startswith(":"):
            continue
        assert line.startswith("data: ")
        events.append(json.loads(line[len("data: ") :]))
        if events[-1]["type"] in terminal_types:
            break
    return events


def _join(client: TestClient, job_id: str) -> None:
    client.app.state.runner._handles[job_id].thread.join(timeout=5)


def _hold_extraction_before_its_first_batch(
    monkeypatch: pytest.MonkeyPatch, engine: TranslatorEngine
) -> tuple[threading.Event, threading.Event]:
    """Make the background pass wait, before any batch call, until the
    returned *proceed* event is set; *entered* is set once it is waiting."""
    entered = threading.Event()
    proceed = threading.Event()
    original_extract = engine.extract_glossary

    def _blocking_extract(job_id_arg, on_progress=None, should_stop=None):
        entered.set()
        proceed.wait(timeout=5)
        return original_extract(
            job_id_arg, on_progress=on_progress, should_stop=should_stop
        )

    monkeypatch.setattr(engine, "extract_glossary", _blocking_extract)
    return entered, proceed


def test_glossary_extract_returns_202_and_runs_the_pass_in_background(
    tmp_path: Path,
) -> None:
    provider = FakeTranslationProvider(canned_unit=_EXTRACTED)
    engine, checkpoint = _make_engine(provider=provider)
    client = _app_and_client(engine)
    job_id = _create_job(client, tmp_path)

    resp = client.post(f"/jobs/{job_id}/glossary/extract")

    assert resp.status_code == 202
    assert resp.json()["status"] == "CREATED"
    _join(client, job_id)
    assert provider.call_count == 1
    status = client.get(f"/jobs/{job_id}").json()
    assert status["status"] == "CREATED"
    assert status["glossary_extracted"] is True
    assert status["cost_usd"] > 0
    glossary = client.get(f"/jobs/{job_id}/glossary").json()["entries"]
    assert [(e["term"], e["translation"]) for e in glossary] == [("Cue", "Señal")]


def test_glossary_extract_unknown_job_404() -> None:
    engine, _ = _make_engine()
    client = _app_and_client(engine)

    resp = client.post("/jobs/does-not-exist/glossary/extract")

    assert resp.status_code == 404
    # The engine's 404, not the router's "Not Found" for a missing route.
    assert "Job not found" in resp.json()["detail"]


@pytest.mark.parametrize(
    "update",
    [
        {"status": JobStatus.PAUSED},
        {"status": JobStatus.DONE},
        {"glossary_extracted": True},
    ],
)
def test_glossary_extract_conflicts_for_a_job_the_pass_refuses(
    tmp_path: Path, update: dict
) -> None:
    provider = FakeTranslationProvider(canned_unit=_EXTRACTED)
    engine, checkpoint = _make_engine(provider=provider)
    client = _app_and_client(engine)
    job_id = _create_job(client, tmp_path)
    checkpoint.save_job(checkpoint.load_job(job_id).model_copy(update=update))

    resp = client.post(f"/jobs/{job_id}/glossary/extract")

    assert resp.status_code == 409
    # Refused before any thread or call: nothing is spent.
    assert job_id not in client.app.state.runner._handles
    assert provider.call_count == 0


def test_an_active_extraction_makes_run_resume_and_extract_busy(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    provider = _BlockingOnFirstCallProvider(started, release)
    provider.canned_unit = _EXTRACTED
    engine, checkpoint = _make_engine(provider=provider)
    client = _app_and_client(engine)
    job_id = _create_job(client, tmp_path)
    other_id = _create_job(client, tmp_path)
    out_path = str(tmp_path / "out.srt")

    assert client.post(f"/jobs/{job_id}/glossary/extract").status_code == 202
    assert started.wait(timeout=2)

    assert client.post(f"/jobs/{job_id}/run", json={"out_path": out_path}).status_code == 409
    assert client.post(f"/jobs/{job_id}/resume", json={"out_path": out_path}).status_code == 409
    assert client.post(f"/jobs/{other_id}/run", json={"out_path": out_path}).status_code == 409
    assert client.post(f"/jobs/{other_id}/glossary/extract").status_code == 409

    release.set()
    _join(client, job_id)
    # Only the extraction call was ever made; the busy requests spent nothing.
    assert provider.call_count == 1
    assert checkpoint.load_job(other_id).cost_usd == 0.0


def test_an_active_run_makes_extract_busy(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    provider = _BlockingOnFirstCallProvider(started, release)
    engine, checkpoint = _make_engine(provider=provider)
    client = _app_and_client(engine)
    job_id = _create_job(client, tmp_path)
    other_id = _create_job(client, tmp_path)

    client.post(f"/jobs/{job_id}/run", json={"out_path": str(tmp_path / "out.srt")})
    assert started.wait(timeout=2)

    assert client.post(f"/jobs/{other_id}/glossary/extract").status_code == 409

    release.set()
    _join(client, job_id)
    assert checkpoint.load_job(other_id).glossary_extracted is False


def test_sse_stream_emits_glossary_progress_then_glossary_terminal(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    provider = _BlockingOnFirstCallProvider(started, release)
    provider.canned_unit = _EXTRACTED
    engine, checkpoint = _make_engine(provider=provider)
    client = _app_and_client(engine)
    job_id = _create_job(client, tmp_path)

    client.post(f"/jobs/{job_id}/glossary/extract")
    assert started.wait(timeout=2)

    with client.stream("GET", f"/jobs/{job_id}/events") as stream_resp:
        assert stream_resp.status_code == 200
        release.set()
        events = _read_events(stream_resp)
    _join(client, job_id)

    cost = checkpoint.load_job(job_id).cost_usd
    assert events == [
        {
            "type": "glossary_progress",
            "job_id": job_id,
            "batch": 1,
            "total_batches": 1,
            "cost_usd": pytest.approx(cost),
        },
        {
            "type": "glossary_terminal",
            "status": "done",
            "error": None,
            "entries": 1,
            "conflicts": 0,
            "cost_usd": pytest.approx(cost),
        },
    ]


def test_sse_stream_subscribed_before_the_pass_starts_gets_its_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The REL-001 window for extraction: the client connects after POST
    returns but before the worker has made its first call. It must get the
    pass's events, not a terminal built from the still-CREATED status."""
    provider = FakeTranslationProvider(canned_unit=_EXTRACTED)
    engine, _ = _make_engine(provider=provider)
    client = _app_and_client(engine)
    job_id = _create_job(client, tmp_path)
    entered, proceed = _hold_extraction_before_its_first_batch(monkeypatch, engine)

    assert client.post(f"/jobs/{job_id}/glossary/extract").status_code == 202
    assert entered.wait(timeout=2)

    with client.stream("GET", f"/jobs/{job_id}/events") as stream_resp:
        proceed.set()
        events = _read_events(stream_resp)
    _join(client, job_id)

    assert [e["type"] for e in events] == ["glossary_progress", "glossary_terminal"]
    assert events[-1]["status"] == "done"


def test_sse_stream_after_a_finished_extraction_replays_its_terminal(
    tmp_path: Path,
) -> None:
    # The job stays CREATED after the pass, so the stored status alone could
    # not tell a late subscriber whether the pass succeeded.
    provider = FakeTranslationProvider(canned_unit=_EXTRACTED)
    engine, _ = _make_engine(provider=provider)
    client = _app_and_client(engine)
    job_id = _create_job(client, tmp_path)
    client.post(f"/jobs/{job_id}/glossary/extract")
    _join(client, job_id)

    with client.stream("GET", f"/jobs/{job_id}/events") as stream_resp:
        events = _read_events(stream_resp)

    assert len(events) == 1
    assert events[0]["type"] == "glossary_terminal"
    assert events[0]["status"] == "done"
    assert events[0]["entries"] == 1


def test_a_failed_extraction_ends_with_a_failed_glossary_terminal(tmp_path: Path) -> None:
    provider = FakeTranslationProvider(canned_unit=_EXTRACTED, fail_on={0})
    engine, checkpoint = _make_engine(provider=provider)
    client = _app_and_client(engine)
    job_id = _create_job(client, tmp_path)

    client.post(f"/jobs/{job_id}/glossary/extract")
    _join(client, job_id)
    with client.stream("GET", f"/jobs/{job_id}/events") as stream_resp:
        events = _read_events(stream_resp)

    terminal = events[-1]
    assert terminal["type"] == "glossary_terminal"
    assert terminal["status"] == "failed"
    assert "Malformed provider output" in terminal["error"]
    assert (terminal["entries"], terminal["conflicts"], terminal["cost_usd"]) == (
        None,
        None,
        None,
    )
    job = checkpoint.load_job(job_id)
    assert (job.status, job.glossary_extracted) == (JobStatus.CREATED, False)


def test_a_budget_stop_ends_with_a_budget_exceeded_glossary_terminal(
    tmp_path: Path,
) -> None:
    provider = FakeTranslationProvider(canned_unit=_EXTRACTED)
    engine, _ = _make_engine(provider=provider)
    client = _app_and_client(engine)
    srt_path = _make_srt_fixture(tmp_path)
    job_id = client.post(
        "/jobs",
        json={"source_path": srt_path, "model": "fake-model", "budget_usd": 1e-9},
    ).json()["id"]

    client.post(f"/jobs/{job_id}/glossary/extract")
    _join(client, job_id)
    with client.stream("GET", f"/jobs/{job_id}/events") as stream_resp:
        events = _read_events(stream_resp)

    terminal = events[-1]
    assert terminal["type"] == "glossary_terminal"
    assert terminal["status"] == "budget_exceeded"
    assert "Budget exceeded" in terminal["error"]
    assert terminal["entries"] is None
    assert provider.call_count == 0


def test_shutdown_during_an_extraction_does_not_cancel_the_job(tmp_path: Path) -> None:
    # Cancelling would mark the job CANCELLED: it could never be extracted
    # again, and the wizard would lose its CREATED job. The pass is left to
    # finish (or to be killed by the sidecar's own fallback); spend already
    # made stays recorded either way.
    started = threading.Event()
    release = threading.Event()
    provider = _BlockingOnFirstCallProvider(started, release)
    provider.canned_unit = _EXTRACTED
    engine, checkpoint = _make_engine(provider=provider)
    client = _app_and_client(engine, shutdown_join_timeout=0.2)
    job_id = _create_job(client, tmp_path)

    client.post(f"/jobs/{job_id}/glossary/extract")
    assert started.wait(timeout=2)

    assert client.post("/shutdown").json()["clean"] is False

    release.set()
    _join(client, job_id)
    job = checkpoint.load_job(job_id)
    assert job.status == JobStatus.CREATED
    assert job.glossary_extracted is True


# ---------------------------------------------------------------------------
# POST /jobs/{id}/cancel during an extraction — a cooperative stop
# ---------------------------------------------------------------------------


def test_cancel_during_an_extraction_stops_it_and_keeps_the_job_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A whole-book pass is dozens of sequential calls, so the user must be
    # able to stop it. Marking the job CANCELLED would take it out of the
    # wizard for good; the pass is stopped instead and the job stays CREATED.
    provider = FakeTranslationProvider(canned_unit=_EXTRACTED)
    engine, checkpoint = _make_engine(provider=provider)
    client = _app_and_client(engine)
    job_id = _create_job(client, tmp_path)
    entered, proceed = _hold_extraction_before_its_first_batch(monkeypatch, engine)
    assert client.post(f"/jobs/{job_id}/glossary/extract").status_code == 202
    assert entered.wait(timeout=2)

    cancel_resp = client.post(f"/jobs/{job_id}/cancel")

    assert cancel_resp.status_code == 202
    assert cancel_resp.json()["status"] == "CREATED"
    with client.stream("GET", f"/jobs/{job_id}/events") as stream_resp:
        proceed.set()
        events = _read_events(stream_resp)
    _join(client, job_id)

    terminal = events[-1]
    assert terminal["type"] == "glossary_terminal"
    assert terminal["status"] == "stopped"
    assert "stopped" in terminal["error"]
    assert (terminal["entries"], terminal["conflicts"]) == (None, None)
    assert provider.call_count == 0
    job = checkpoint.load_job(job_id)
    assert (job.status, job.glossary_extracted) == (JobStatus.CREATED, False)


def test_a_stopped_extraction_can_be_started_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeTranslationProvider(canned_unit=_EXTRACTED)
    engine, checkpoint = _make_engine(provider=provider)
    client = _app_and_client(engine)
    job_id = _create_job(client, tmp_path)
    entered, proceed = _hold_extraction_before_its_first_batch(monkeypatch, engine)
    client.post(f"/jobs/{job_id}/glossary/extract")
    assert entered.wait(timeout=2)
    client.post(f"/jobs/{job_id}/cancel")
    proceed.set()
    _join(client, job_id)

    assert client.post(f"/jobs/{job_id}/glossary/extract").status_code == 202
    _join(client, job_id)

    assert checkpoint.load_job(job_id).glossary_extracted is True
    assert provider.call_count == 1


def test_cancel_of_a_created_job_with_no_active_extraction_still_cancels_it(
    tmp_path: Path,
) -> None:
    # The stop applies only to a pass in flight; otherwise cancel keeps its
    # existing meaning.
    provider = FakeTranslationProvider(canned_unit=_EXTRACTED)
    engine, _ = _make_engine(provider=provider)
    client = _app_and_client(engine)
    job_id = _create_job(client, tmp_path)
    client.post(f"/jobs/{job_id}/glossary/extract")
    _join(client, job_id)

    resp = client.post(f"/jobs/{job_id}/cancel")

    assert resp.status_code == 202
    assert resp.json()["status"] == "CANCELLED"


def test_shutdown_during_an_extraction_stops_it_before_the_next_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The sidecar is about to be killed: a batch started now would be paid
    # for and lost, so the pass is asked to stop, never cancelled.
    provider = FakeTranslationProvider(canned_unit=_EXTRACTED)
    engine, checkpoint = _make_engine(provider=provider)
    client = _app_and_client(engine, shutdown_join_timeout=0.2)
    job_id = _create_job(client, tmp_path)
    entered, proceed = _hold_extraction_before_its_first_batch(monkeypatch, engine)
    client.post(f"/jobs/{job_id}/glossary/extract")
    assert entered.wait(timeout=2)

    assert client.post("/shutdown").json()["clean"] is False

    proceed.set()
    _join(client, job_id)
    assert provider.call_count == 0
    assert client.app.state.runner.finished_glossary_terminal(job_id)["status"] == "stopped"
    assert checkpoint.load_job(job_id).status == JobStatus.CREATED


class _RecordingEngine:
    """Engine stand-in for the runner alone: a run that fails at once, an
    extraction that succeeds at once, and a record of cancel_job calls."""

    def __init__(self) -> None:
        self.cancelled: list[str] = []

    def run_job(self, job_id, out_path, on_progress=None):  # noqa: ARG002
        raise RuntimeError("the run failed before touching the job")

    def extract_glossary(self, job_id, on_progress=None, should_stop=None):  # noqa: ARG002
        return GlossaryExtractionOutcome(batches=1, entries=0, conflicts=0, cost_usd=0.0)

    def cancel_job(self, job_id) -> None:
        self.cancelled.append(job_id)


def test_shutdown_never_acts_on_the_previous_execution_of_a_job_being_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The slot and the new execution's handle are registered together: a
    # shutdown can never see the slot claimed for a job while the handle
    # stored for it is still the job's previous execution. Here the previous
    # execution was a run and the new one an extraction, so reading the stale
    # handle would cancel_job the CREATED job and take it out of the wizard.
    from borgesica.serve import runner as runner_module

    engine = _RecordingEngine()
    runner = runner_module.JobRunner(engine)
    runner.start("job-1", "out.srt")
    runner._handles["job-1"].thread.join(timeout=5)
    real_thread = threading.Thread

    class _ShutdownWhileBuilt(real_thread):
        """Lands a shutdown while the new execution is being set up."""

        def __init__(self, *args, **kwargs) -> None:
            runner.shutdown(join_timeout=0)
            super().__init__(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(runner_module.threading, "Thread", _ShutdownWhileBuilt)
        runner.start_glossary_extraction("job-1")
    runner._handles["job-1"].thread.join(timeout=5)

    assert engine.cancelled == []
    assert runner.finished_glossary_terminal("job-1")["status"] == "done"
