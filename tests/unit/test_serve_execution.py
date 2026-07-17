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
from borgesica.domain.models import SourceType
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
