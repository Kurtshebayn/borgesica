"""POST /jobs/{id}/chunks/{index}/accept: keep a failed chunk's candidate."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from borgesica.api import TranslatorEngine
from borgesica.domain.glossary import NullGlossaryExtractor
from borgesica.domain.models import Chunk, ChunkStatus, Job, JobConfig, JobStatus, SourceType
from borgesica.serve.app import create_app
from tests.fakes import FakeTranslationProvider, InMemoryCheckpointStore


class _RecordingWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[list[Chunk], str]] = []

    def write(self, chunks, source_path, out_path, target_lang="es"):  # type: ignore[no-untyped-def]
        self.calls.append((list(chunks), out_path))


def _setup(candidate, source_type=SourceType.EPUB):
    store = InMemoryCheckpointStore()
    writer = _RecordingWriter()
    engine = TranslatorEngine(
        provider=FakeTranslationProvider(),
        checkpoint=store,
        readers={},
        writers={source_type: writer},
        extractor=NullGlossaryExtractor(),
    )
    now = datetime.now(UTC)
    store.save_job(
        Job(
            id="job-a",
            config=JobConfig(source_type=source_type, model="fake-model"),
            source_path="book.epub",
            status=JobStatus.DONE,
            total_chunks=2,
            created_at=now,
            updated_at=now,
            out_path="out/book_es.epub",
        )
    )
    store.save_chunk(
        "job-a", Chunk(index=0, source_text="a", translated_text="A", status=ChunkStatus.DONE)
    )
    meta = {"rejected_candidate": candidate} if candidate else {}
    store.save_chunk(
        "job-a", Chunk(index=1, source_text="b\n\nc", status=ChunkStatus.FAILED, meta=meta)
    )
    return TestClient(create_app(engine)), store, writer


def test_chunks_payload_exposes_the_candidate_and_its_alignment():
    client, _, _ = _setup({"text": "B C", "aligned": False})
    failed = client.get("/jobs/job-a/chunks").json()["chunks"][1]
    assert failed["candidate"] == "B C"
    assert failed["candidate_aligned"] is False


def test_accepting_keeps_the_candidate_and_rewrites_the_output():
    client, store, writer = _setup({"text": "B C", "aligned": False})

    resp = client.post("/jobs/job-a/chunks/1/accept")

    assert resp.status_code == 200
    assert resp.json()["status"] == "DONE"
    saved = store.load_chunks("job-a")[1]
    assert saved.translated_text == "B C" and saved.passed_validation is False
    assert "rejected_candidate" not in saved.meta
    assert writer.calls and writer.calls[0][1] == "out/book_es.epub"
    assert client.get("/jobs/job-a").json()["failed_count"] == 0


def test_a_chunk_without_a_candidate_is_refused_and_nothing_changes():
    client, store, writer = _setup(None)
    resp = client.post("/jobs/job-a/chunks/1/accept")
    assert resp.status_code == 422
    assert store.load_chunks("job-a")[1].status == ChunkStatus.FAILED
    assert not writer.calls


def test_a_chunk_that_did_not_fail_is_refused():
    client, _, _ = _setup({"text": "x", "aligned": True})
    assert client.post("/jobs/job-a/chunks/0/accept").status_code == 422


def test_misaligned_candidate_is_refused_outside_epub():
    client, store, _ = _setup({"text": "B C", "aligned": False}, SourceType.SRT)
    assert client.post("/jobs/job-a/chunks/1/accept").status_code == 422
    assert store.load_chunks("job-a")[1].status == ChunkStatus.FAILED


def test_a_failing_writer_leaves_the_chunk_failed():
    client, store, writer = _setup({"text": "B C", "aligned": False})

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise ValueError("disk full")

    writer.write = boom  # type: ignore[method-assign]
    assert client.post("/jobs/job-a/chunks/1/accept").status_code == 422
    saved = store.load_chunks("job-a")[1]
    assert saved.status == ChunkStatus.FAILED and saved.translated_text is None


def test_a_chunk_the_person_kept_is_no_longer_a_problem():
    client, store, _ = _setup({"text": "B C", "aligned": False})
    assert client.post("/jobs/job-a/chunks/1/accept").status_code == 200

    # It still records that validation rejected it...
    assert store.load_chunks("job-a")[1].passed_validation is False
    # ...but the person reviewed it: not best effort, not a problem to list.
    assert client.get("/jobs/job-a/chunks").json()["chunks"][1]["best_effort"] is False
    job = client.get("/jobs/job-a").json()
    assert job["best_effort_count"] == 0 and job["best_effort_indices"] == []
    assert client.get("/jobs/job-a/chunks?only_problems=true").json()["total"] == 0
    listed = client.get("/jobs").json()["jobs"][0]
    assert listed["best_effort_count"] == 0 and listed["failed_count"] == 0


def test_an_unreviewed_best_effort_chunk_still_counts_as_a_problem():
    client, store, _ = _setup({"text": "B C", "aligned": False})
    store.save_chunk(
        "job-a",
        Chunk(
            index=1,
            source_text="b",
            translated_text="B",
            status=ChunkStatus.DONE,
            passed_validation=False,
        ),
    )
    assert client.get("/jobs/job-a").json()["best_effort_count"] == 1


def test_completed_count_comes_from_the_chunks_not_the_stored_number():
    # The job was stored with completed_chunks=0 (finished before it was kept).
    client, _, _ = _setup({"text": "B C", "aligned": False})
    assert client.get("/jobs/job-a").json()["completed_chunks"] == 1
    assert client.get("/jobs").json()["jobs"][0]["completed_chunks"] == 1

    assert client.post("/jobs/job-a/chunks/1/accept").status_code == 200
    assert client.get("/jobs/job-a").json()["completed_chunks"] == 2
    assert client.get("/jobs").json()["jobs"][0]["completed_chunks"] == 2
