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


def test_create_response_reports_zero_failed_chunks(tmp_path: Path) -> None:
    engine, _ = _make_engine()
    srt_path = _make_srt_fixture(tmp_path)
    client = _client(engine)

    resp = client.post("/jobs", json={"source_path": srt_path, "model": "fake-model"})

    assert resp.status_code == 201
    body = resp.json()
    assert body["failed_count"] == 0
    assert body["failed_indices"] == []


def test_status_endpoint_reports_failed_chunks(tmp_path: Path) -> None:
    """Error-surfacing: a FAILED chunk (e.g. provider auth/model error under
    continue_on_error=True, which ends the job DONE at $0 shipping source
    text) MUST be visible in the job status payload — otherwise the desktop
    UI reports a silent success on untranslated output."""
    engine, checkpoint = _make_engine()
    srt_path = _make_srt_fixture(tmp_path, num_cues=2)
    client = _client(engine)

    create_resp = client.post("/jobs", json={"source_path": srt_path, "model": "fake-model"})
    job_id = create_resp.json()["id"]

    # Simulate a chunk the provider could never translate (all retries + the
    # fallback raised) — the orchestrator persists it FAILED.
    chunks = checkpoint.load_chunks(job_id)
    failed_chunk = chunks[0].model_copy(update={"status": ChunkStatus.FAILED})
    checkpoint.save_chunk(job_id, failed_chunk)

    resp = client.get(f"/jobs/{job_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["failed_count"] == 1
    assert body["failed_indices"] == [failed_chunk.index]


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


def test_estimate_endpoint_reports_the_glossary_extraction_line(tmp_path: Path) -> None:
    engine, _ = _make_engine()
    srt_path = _make_srt_fixture(tmp_path)
    client = _client(engine)

    created = client.post("/jobs", json={"source_path": srt_path, "model": "fake-model"})
    job_id = created.json()["id"]
    body = client.get(f"/jobs/{job_id}/estimate").json()

    expected = engine.estimate_glossary_extraction(job_id)
    assert body["glossary_extraction"] == expected.model_dump()
    assert set(body["glossary_extraction"]) == {
        "batches",
        "input_tokens",
        "output_tokens",
        "output_tokens_high",
        "usd",
        "usd_high",
    }


def test_estimate_endpoint_glossary_extraction_is_null_once_extracted(
    tmp_path: Path,
) -> None:
    engine, checkpoint = _make_engine()
    srt_path = _make_srt_fixture(tmp_path)
    client = _client(engine)
    created = client.post("/jobs", json={"source_path": srt_path, "model": "fake-model"})
    job_id = created.json()["id"]
    checkpoint.save_job(
        checkpoint.load_job(job_id).model_copy(update={"glossary_extracted": True})
    )

    body = client.get(f"/jobs/{job_id}/estimate").json()

    assert body["glossary_extraction"] is None
    # The translation estimate itself is unchanged.
    assert "usd_high" in body


def test_status_endpoint_reports_whether_the_glossary_was_extracted(
    tmp_path: Path,
) -> None:
    engine, checkpoint = _make_engine()
    srt_path = _make_srt_fixture(tmp_path)
    client = _client(engine)
    created = client.post("/jobs", json={"source_path": srt_path, "model": "fake-model"}).json()
    job_id = created["id"]

    assert created["glossary_extracted"] is False
    assert client.get(f"/jobs/{job_id}").json()["glossary_extracted"] is False

    checkpoint.save_job(
        checkpoint.load_job(job_id).model_copy(update={"glossary_extracted": True})
    )

    assert client.get(f"/jobs/{job_id}").json()["glossary_extracted"] is True


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


def test_glossary_update_takes_a_gender_anchor_away(tmp_path: Path) -> None:
    """The review screen's "no gender" choice: a stored decision that a later
    edit sending ``gender: null`` (meaning "unchanged") does not undo."""
    engine, _ = _make_engine()
    srt_path = _make_srt_fixture(tmp_path)
    client = _client(engine)
    job_id = client.post("/jobs", json={"source_path": srt_path, "model": "fake-model"}).json()["id"]
    url = f"/jobs/{job_id}/glossary"

    client.put(url, json={"entries": [{"term": "Caten", "translation": "Caten", "gender": "masculine"}]})
    put_resp = client.put(
        url, json={"entries": [{"term": "Caten", "translation": "Caten", "gender": "unanchored"}]}
    )
    client.put(url, json={"entries": [{"term": "Caten", "translation": "Caten", "locked": True}]})

    assert put_resp.status_code == 200
    assert put_resp.json()["entries"][0]["gender"] == "unanchored"
    stored = client.get(url).json()["entries"][0]
    assert (stored["gender"], stored["locked"]) == ("unanchored", True)
    reviewed = client.get(f"{url}/review").json()["entries"][0]
    assert (reviewed["gender"], reviewed["gender_source"]) == (None, "stored")


def test_glossary_update_unknown_job_404() -> None:
    engine, _ = _make_engine()
    client = _client(engine)

    resp = client.put(
        "/jobs/does-not-exist/glossary",
        json={"entries": [{"term": "x", "translation": "y"}]},
    )

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /jobs/{id}/glossary/review
# ---------------------------------------------------------------------------


def test_glossary_review_returns_every_entry_with_evidence_and_flags(
    tmp_path: Path,
) -> None:
    engine, checkpoint = _make_engine()
    srt_path = _make_srt_fixture(tmp_path)
    client = _client(engine)
    job_id = client.post("/jobs", json={"source_path": srt_path, "model": "fake-model"}).json()["id"]
    client.put(
        f"/jobs/{job_id}/glossary",
        json={"entries": [{"term": "Cue", "translation": "Cue"}]},
    )
    checkpoint.save_glossary_proposals(job_id, {"cue": ("Cue", "Señal")})

    resp = client.get(f"/jobs/{job_id}/glossary/review")

    assert resp.status_code == 200
    body = resp.json()
    # The seeder's thresholds travel with the data, so the review screen can
    # explain "undecided" without hard-coding them.
    assert body["min_gender_mentions"] == 20
    assert body["min_gender_ratio"] == 0.7
    assert body["entries"] == [
        {
            "entry": {
                "term": "Cue",
                "translation": "Cue",
                "locked": False,
                "note": None,
                "gender": None,
                "first_draw": None,
            },
            "gender": None,
            "gender_source": None,
            "masculine_mentions": 0,
            "feminine_mentions": 0,
            "proposals": [
                {"rendering": "Cue", "batches": 1},
                {"rendering": "Señal", "batches": 1},
            ],
            "flags": ["conflicting_renderings", "kept_vs_translated"],
        }
    ]


def test_glossary_review_unknown_job_404() -> None:
    engine, _ = _make_engine()

    resp = _client(engine).get("/jobs/does-not-exist/glossary/review")

    assert resp.status_code == 404
