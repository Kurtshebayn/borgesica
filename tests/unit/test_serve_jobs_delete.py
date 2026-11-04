"""DELETE /jobs/{id} and the persisted out_path: deleting a ficha removes its
rows and, only when it is safe, its translated file in the library folder."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from borgesica.adapters.checkpoints.sqlite_checkpoint import SQLiteCheckpointStore
from borgesica.adapters.readers.srt_reader import SrtReader
from borgesica.adapters.writers.srt_writer import SrtWriter
from borgesica.api import TranslatorEngine
from borgesica.domain.glossary import NullGlossaryExtractor
from borgesica.domain.models import JobStatus, SourceType
from borgesica.serve.app import create_app
from tests.fakes import FakeTranslationProvider

_SRT = "1\n00:00:00,000 --> 00:00:01,500\nCue 1 text.\n"


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    lib = tmp_path / "library"
    monkeypatch.setenv("BORGESICA_LIBRARY_DIR", str(lib))
    return lib


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "jobs.db")


def _client(db: str, provider=None) -> TestClient:
    engine = TranslatorEngine(
        provider=provider or FakeTranslationProvider(),
        checkpoint=SQLiteCheckpointStore(db),
        readers={SourceType.SRT: SrtReader()},
        writers={SourceType.SRT: SrtWriter()},
        extractor=NullGlossaryExtractor(),
    )
    return TestClient(create_app(engine))


def _source(tmp_path: Path) -> Path:
    src = tmp_path / "src" / "test.srt"
    src.parent.mkdir(exist_ok=True)
    src.write_text(_SRT, encoding="utf-8")
    return src


def _create(client: TestClient, src: Path) -> str:
    return client.post("/jobs", json={"source_path": str(src), "model": "fake-model"}).json()["id"]


def _translate(client: TestClient, src: Path, body: dict | None = None) -> str:
    job_id = _create(client, src)
    client.post(f"/jobs/{job_id}/run", json=body or {})
    client.app.state.runner._handles[job_id].thread.join(timeout=5)
    return job_id


def _rows(db: str, job_id: str) -> dict[str, int]:
    tables = {
        "jobs": "id", "chunks": "job_id", "glossary": "job_id", "glossary_votes": "job_id",
        "glossary_proposals": "job_id", "summaries": "job_id", "narrators": "job_id",
    }
    with sqlite3.connect(db) as conn:
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE {c}=?", (job_id,)).fetchone()[0]
            for t, c in tables.items()
        }


def _set_out_path(db: str, job_id: str, out_path: str) -> None:
    store = SQLiteCheckpointStore(db)
    store.save_job(store.load_job(job_id).model_copy(update={"out_path": out_path}))


def test_the_run_persists_where_the_output_went(tmp_path, library, db) -> None:
    client = _client(db)
    job_id = _translate(client, _source(tmp_path))

    expected = str(library / "test_es.srt")
    assert SQLiteCheckpointStore(db).load_job(job_id).out_path == expected
    # A fresh process (no runner handle) still reports it.
    assert _client(db).get(f"/jobs/{job_id}").json()["out_path"] == expected


def test_delete_removes_the_rows_and_the_library_file_but_not_the_source(
    tmp_path, library, db
) -> None:
    client = _client(db)
    src = _source(tmp_path)
    job_id = _translate(client, src)
    out = library / "test_es.srt"
    assert out.is_file() and _rows(db, job_id)["chunks"] > 0

    resp = client.delete(f"/jobs/{job_id}")

    assert resp.status_code == 200
    assert resp.json() == {"deleted": True, "file_deleted": True}
    assert not out.exists()
    assert src.read_text(encoding="utf-8") == _SRT
    assert all(n == 0 for n in _rows(db, job_id).values())
    assert client.get(f"/jobs/{job_id}").status_code == 404
    assert client.get("/jobs").json()["jobs"] == []


def test_delete_of_an_unknown_job_is_404(db, library) -> None:
    assert _client(db).delete("/jobs/nope").status_code == 404


def test_delete_leaves_a_file_outside_the_library_alone(tmp_path, library, db) -> None:
    client = _client(db)
    outside = tmp_path / "elsewhere" / "mine.srt"
    outside.parent.mkdir()
    job_id = _translate(client, _source(tmp_path), {"out_path": str(outside)})
    assert outside.is_file()

    resp = client.delete(f"/jobs/{job_id}")

    assert resp.status_code == 200
    assert resp.json() == {"deleted": True, "file_deleted": False}
    assert outside.is_file()
    assert all(n == 0 for n in _rows(db, job_id).values())


def test_delete_never_removes_the_source_even_inside_the_library(tmp_path, library, db) -> None:
    client = _client(db)
    library.mkdir()
    src = library / "test.srt"
    src.write_text(_SRT, encoding="utf-8")
    job_id = _create(client, src)
    _set_out_path(db, job_id, str(src))

    resp = client.delete(f"/jobs/{job_id}")

    assert resp.json() == {"deleted": True, "file_deleted": False}
    assert src.is_file()


def test_delete_ignores_a_stored_path_that_is_a_directory(tmp_path, library, db) -> None:
    client = _client(db)
    job_id = _create(client, _source(tmp_path))
    folder = library / "adir"
    folder.mkdir(parents=True)
    _set_out_path(db, job_id, str(folder))

    resp = client.delete(f"/jobs/{job_id}")

    assert resp.json() == {"deleted": True, "file_deleted": False}
    assert folder.is_dir()


def test_delete_rejects_a_dotdot_path_that_escapes_the_library(tmp_path, library, db) -> None:
    client = _client(db)
    job_id = _create(client, _source(tmp_path))
    library.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    _set_out_path(db, job_id, str(library / ".." / "victim.txt"))

    resp = client.delete(f"/jobs/{job_id}")

    assert resp.json() == {"deleted": True, "file_deleted": False}
    assert victim.read_text() == "keep"


def test_a_job_without_a_stored_out_path_is_deleted_without_touching_files(
    tmp_path, library, db
) -> None:
    client = _client(db)
    src = _source(tmp_path)
    job_id = _create(client, src)
    library.mkdir()
    bystander = library / "test_es.srt"
    bystander.write_text("not yours")

    resp = client.delete(f"/jobs/{job_id}")

    assert resp.json() == {"deleted": True, "file_deleted": False}
    assert bystander.read_text() == "not yours"
    assert src.is_file()
    assert _rows(db, job_id)["jobs"] == 0


def test_a_file_that_cannot_be_removed_does_not_fail_the_request(
    tmp_path, library, db, monkeypatch
) -> None:
    client = _client(db)
    job_id = _translate(client, _source(tmp_path))

    def _boom(self, *args, **kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "unlink", _boom)
    resp = client.delete(f"/jobs/{job_id}")

    assert resp.status_code == 200
    assert resp.json() == {"deleted": True, "file_deleted": False}
    assert all(n == 0 for n in _rows(db, job_id).values())


def test_a_running_job_is_refused_with_its_rows_intact(tmp_path, library, db) -> None:
    started, release = threading.Event(), threading.Event()

    class _Blocking(FakeTranslationProvider):
        def translate(self, system, user, model, segment_count=None):
            started.set()
            release.wait(timeout=5)
            return super().translate(system, user, model, segment_count)

    client = _client(db, _Blocking())
    job_id = _create(client, _source(tmp_path))
    client.post(f"/jobs/{job_id}/run", json={})
    assert started.wait(timeout=5)
    try:
        resp = client.delete(f"/jobs/{job_id}")
        assert resp.status_code == 409
        assert _rows(db, job_id)["jobs"] == 1
        assert _rows(db, job_id)["chunks"] > 0
    finally:
        release.set()
        client.app.state.runner._handles[job_id].thread.join(timeout=5)
    assert SQLiteCheckpointStore(db).load_job(job_id) is not None


def test_a_persisted_running_job_is_refused_even_without_a_live_runner(
    tmp_path, library, db
) -> None:
    client = _client(db)
    job_id = _translate(client, _source(tmp_path))
    store = SQLiteCheckpointStore(db)
    store.save_job(store.load_job(job_id).model_copy(update={"status": JobStatus.RUNNING}))

    resp = _client(db).delete(f"/jobs/{job_id}")

    assert resp.status_code == 409
    assert (library / "test_es.srt").is_file()
    assert _rows(db, job_id)["jobs"] == 1
