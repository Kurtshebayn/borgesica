"""Tests for T5 — borgesica.serve app factory + CRUD/estimate/glossary endpoints.

All tests build a real TranslatorEngine wired with FakeTranslationProvider +
InMemoryCheckpointStore (tests/fakes.py precedent, mirrors test_engine.py's
_make_engine helper) and exercise the FastAPI app via TestClient. No network,
no live provider calls, no uvicorn server actually bound to a port.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from borgesica.api import TranslatorEngine
from borgesica.domain.models import Chunk, ChunkStatus, GlossaryEntry, SourceType
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


def _make_engine() -> tuple[TranslatorEngine, InMemoryCheckpointStore]:
    from borgesica.adapters.readers.srt_reader import SrtReader
    from borgesica.adapters.writers.srt_writer import SrtWriter
    from borgesica.domain.glossary import NullGlossaryExtractor

    checkpoint = InMemoryCheckpointStore()
    engine = TranslatorEngine(
        provider=FakeTranslationProvider(),
        checkpoint=checkpoint,
        readers={SourceType.SRT: SrtReader()},
        writers={SourceType.SRT: SrtWriter()},
        extractor=NullGlossaryExtractor(),
    )
    return engine, checkpoint


def _client(engine: TranslatorEngine) -> TestClient:
    from borgesica.serve.app import create_app

    return TestClient(create_app(engine))


# ---------------------------------------------------------------------------
# create_app factory
# ---------------------------------------------------------------------------


def test_create_app_returns_fastapi_instance() -> None:
    from fastapi import FastAPI

    from borgesica.serve.app import create_app

    engine, _ = _make_engine()
    app = create_app(engine)
    assert isinstance(app, FastAPI)


# ---------------------------------------------------------------------------
# POST /jobs — create
# ---------------------------------------------------------------------------


def test_create_job_endpoint_creates_job(tmp_path: Path) -> None:
    engine, checkpoint = _make_engine()
    srt_path = _make_srt_fixture(tmp_path)
    client = _client(engine)

    resp = client.post("/jobs", json={"source_path": srt_path, "model": "fake-model"})

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "CREATED"
    assert body["total_chunks"] >= 1
    assert body["completed_chunks"] == 0
    assert body["best_effort_count"] == 0
    assert body["best_effort_indices"] == []
    assert checkpoint.load_job(body["id"]) is not None


def test_create_job_rejects_pdf(tmp_path: Path) -> None:
    engine, checkpoint = _make_engine()
    pdf_path = tmp_path / "book.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    client = _client(engine)

    resp = client.post("/jobs", json={"source_path": str(pdf_path), "model": "fake-model"})

    assert resp.status_code == 422
    assert checkpoint._jobs == {}


# ---------------------------------------------------------------------------
# GET /jobs/{id} — status
# ---------------------------------------------------------------------------


def test_status_endpoint_unknown_job_404() -> None:
    engine, _ = _make_engine()
    client = _client(engine)

    resp = client.get("/jobs/does-not-exist")

    assert resp.status_code == 404


def test_status_endpoint_reports_best_effort_chunks(tmp_path: Path) -> None:
    engine, checkpoint = _make_engine()
    srt_path = _make_srt_fixture(tmp_path, num_cues=2)
    client = _client(engine)

    create_resp = client.post("/jobs", json={"source_path": srt_path, "model": "fake-model"})
    job_id = create_resp.json()["id"]

    # Simulate a chunk that completed as best-effort (passed_validation=False).
    chunks = checkpoint.load_chunks(job_id)
    best_effort_chunk = chunks[0].model_copy(
        update={
            "status": ChunkStatus.DONE,
            "translated_text": "[translated] best effort",
            "passed_validation": False,
        }
    )
    checkpoint.save_chunk(job_id, best_effort_chunk)

    resp = client.get(f"/jobs/{job_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["best_effort_count"] == 1
    assert body["best_effort_indices"] == [best_effort_chunk.index]


# ---------------------------------------------------------------------------
# GET /jobs/{id}/estimate
# ---------------------------------------------------------------------------


def test_estimate_endpoint_returns_cost_fields(tmp_path: Path) -> None:
    engine, _ = _make_engine()
    srt_path = _make_srt_fixture(tmp_path)
    client = _client(engine)

    job_id = client.post("/jobs", json={"source_path": srt_path, "model": "fake-model"}).json()["id"]
    resp = client.get(f"/jobs/{job_id}/estimate")

    assert resp.status_code == 200
    body = resp.json()
    assert "usd_low" in body
    assert "usd_high" in body
    assert "within_budget" in body


def test_estimate_endpoint_unknown_job_404() -> None:
    engine, _ = _make_engine()
    client = _client(engine)

    resp = client.get("/jobs/does-not-exist/estimate")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET/PUT /jobs/{id}/glossary
# ---------------------------------------------------------------------------


def test_glossary_get_and_update_roundtrip(tmp_path: Path) -> None:
    engine, _ = _make_engine()
    srt_path = _make_srt_fixture(tmp_path)
    client = _client(engine)

    job_id = client.post("/jobs", json={"source_path": srt_path, "model": "fake-model"}).json()["id"]

    get_resp = client.get(f"/jobs/{job_id}/glossary")
    assert get_resp.status_code == 200
    assert get_resp.json()["entries"] == []

    put_resp = client.put(
        f"/jobs/{job_id}/glossary",
        json={"entries": [{"term": "Aleph", "translation": "Aleph", "locked": True}]},
    )
    assert put_resp.status_code == 200
    entries = put_resp.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["term"] == "Aleph"
    assert entries[0]["locked"] is True

    # Persisted — a subsequent GET reflects the update.
    get_again = client.get(f"/jobs/{job_id}/glossary")
    assert get_again.json()["entries"][0]["term"] == "Aleph"


def test_glossary_update_unknown_job_404() -> None:
    engine, _ = _make_engine()
    client = _client(engine)

    resp = client.put(
        "/jobs/does-not-exist/glossary",
        json={"entries": [{"term": "x", "translation": "y"}]},
    )

    assert resp.status_code == 404
