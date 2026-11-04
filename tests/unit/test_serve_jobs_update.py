"""PATCH /jobs/{id}: a draft (CREATED) job can change its model, nothing else."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from borgesica.adapters.checkpoints.sqlite_checkpoint import SQLiteCheckpointStore
from borgesica.adapters.readers.srt_reader import SrtReader
from borgesica.adapters.writers.srt_writer import SrtWriter
from borgesica.api import TranslatorEngine
from borgesica.domain.glossary import NullGlossaryExtractor
from borgesica.domain.models import SourceType
from borgesica.serve.app import create_app
from tests.fakes import FakeTranslationProvider

_SRT = "1\n00:00:00,000 --> 00:00:01,500\nCue 1 text.\n"


def _client(tmp_path: Path) -> TestClient:
    engine = TranslatorEngine(
        provider=FakeTranslationProvider(),
        checkpoint=SQLiteCheckpointStore(str(tmp_path / "jobs.db")),
        readers={SourceType.SRT: SrtReader()},
        writers={SourceType.SRT: SrtWriter()},
        extractor=NullGlossaryExtractor(),
    )
    return TestClient(create_app(engine))


def _create(client: TestClient, tmp_path: Path) -> str:
    src = tmp_path / "test.srt"
    src.write_text(_SRT, encoding="utf-8")
    resp = client.post("/jobs", json={"source_path": str(src), "model": "model-a"})
    assert resp.json()["model"] == "model-a"
    return resp.json()["id"]


def test_patch_changes_the_model_of_a_draft_and_persists_it(tmp_path: Path) -> None:
    client = _client(tmp_path)
    job_id = _create(client, tmp_path)

    resp = client.patch(f"/jobs/{job_id}", json={"model": "model-b"})

    assert resp.status_code == 200
    assert resp.json()["model"] == "model-b"
    assert resp.json()["status"] == "CREATED"
    assert client.get(f"/jobs/{job_id}").json()["model"] == "model-b"
    # A new engine over the same database sees it: it was stored, not cached.
    assert _client(tmp_path).get(f"/jobs/{job_id}").json()["model"] == "model-b"


def test_patch_changes_nothing_else_about_the_job(tmp_path: Path) -> None:
    client = _client(tmp_path)
    job_id = _create(client, tmp_path)
    before = client.get(f"/jobs/{job_id}").json()

    after = client.patch(f"/jobs/{job_id}", json={"model": "model-b"}).json()

    assert {**after, "model": "x"} == {**before, "model": "x"}


def test_patch_unknown_job_is_404(tmp_path: Path) -> None:
    assert _client(tmp_path).patch("/jobs/nope", json={"model": "m"}).status_code == 404


def test_patch_blank_model_is_422_and_keeps_the_old_one(tmp_path: Path) -> None:
    client = _client(tmp_path)
    job_id = _create(client, tmp_path)

    assert client.patch(f"/jobs/{job_id}", json={"model": "  "}).status_code == 422
    assert client.get(f"/jobs/{job_id}").json()["model"] == "model-a"


def test_patch_after_the_job_ran_is_409(tmp_path: Path) -> None:
    client = _client(tmp_path)
    job_id = _create(client, tmp_path)
    client.post(f"/jobs/{job_id}/run", json={})
    for _ in range(200):
        if client.get(f"/jobs/{job_id}").json()["status"] != "RUNNING":
            break
    assert client.get(f"/jobs/{job_id}").json()["status"] != "CREATED"

    resp = client.patch(f"/jobs/{job_id}", json={"model": "model-b"})

    assert resp.status_code == 409
    assert client.get(f"/jobs/{job_id}").json()["model"] == "model-a"
