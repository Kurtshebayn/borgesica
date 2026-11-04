"""GET /jobs: every stored job, newest first, with its problem counts.

Read-only view for the desktop library screen.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from borgesica.api import TranslatorEngine
from borgesica.domain.glossary import NullGlossaryExtractor
from borgesica.domain.models import Chunk, ChunkStatus, Job, JobConfig, JobStatus, SourceType
from borgesica.serve.app import create_app
from tests.fakes import FakeTranslationProvider, InMemoryCheckpointStore

TOKEN = "list-token"
ITEM_KEYS = {
    "id", "status", "title", "source_type", "target_lang", "model",
    "total_chunks", "completed_chunks", "cost_usd", "failed_count", "best_effort_count",
}


def _engine() -> tuple[TranslatorEngine, InMemoryCheckpointStore]:
    store = InMemoryCheckpointStore()
    engine = TranslatorEngine(
        provider=FakeTranslationProvider(),
        checkpoint=store,
        readers={},
        writers={},
        extractor=NullGlossaryExtractor(),
    )
    return engine, store


def _job(job_id: str, age_minutes: int, source_path: str = "book.epub", **kw) -> Job:
    when = datetime(2026, 1, 1, tzinfo=UTC) - timedelta(minutes=age_minutes)
    return Job(
        id=job_id,
        config=JobConfig(
            source_type=kw.pop("source_type", SourceType.EPUB),
            model="fake-model",
            target_lang="es-neutral",
        ),
        source_path=source_path,
        status=kw.pop("status", JobStatus.DONE),
        created_at=when,
        updated_at=when,
        **kw,
    )


def test_empty_store_lists_no_jobs():
    engine, _ = _engine()

    resp = TestClient(create_app(engine)).get("/jobs")

    assert resp.status_code == 200
    assert resp.json() == {"jobs": []}


def test_jobs_come_newest_first_with_the_documented_fields():
    engine, store = _engine()
    # Saved oldest-first and out of order: creation time decides, not insertion.
    store.save_job(_job("old", 30, "C:/libros/viejo.epub", total_chunks=4, completed_chunks=4, cost_usd=0.5))
    store.save_job(_job("new", 0, "sub.srt", source_type=SourceType.SRT, status=JobStatus.RUNNING))
    store.save_job(_job("mid", 10, "otro.epub"))

    resp = TestClient(create_app(engine)).get("/jobs")

    assert resp.status_code == 200
    jobs = resp.json()["jobs"]
    assert [j["id"] for j in jobs] == ["new", "mid", "old"]
    assert all(set(j) == ITEM_KEYS for j in jobs)
    new, _, old = jobs
    assert new["title"] == "sub"
    assert new["source_type"] == "SRT"  # the domain value, like status
    assert new["status"] == "RUNNING"
    assert old["title"] == "viejo"
    assert (old["target_lang"], old["model"]) == ("es-neutral", "fake-model")
    assert (old["total_chunks"], old["completed_chunks"], old["cost_usd"]) == (4, 4, 0.5)


def test_counts_failed_and_best_effort_chunks_per_job():
    engine, store = _engine()
    store.save_job(_job("a", 5))
    store.save_job(_job("b", 0))
    store.save_chunk("a", Chunk(index=0, source_text="s", status=ChunkStatus.DONE, translated_text="t", passed_validation=True))
    store.save_chunk("a", Chunk(index=1, source_text="s", status=ChunkStatus.DONE, translated_text="t", passed_validation=False))
    store.save_chunk("a", Chunk(index=2, source_text="s", status=ChunkStatus.FAILED))
    store.save_chunk("a", Chunk(index=3, source_text="s", status=ChunkStatus.FAILED))
    store.save_chunk("b", Chunk(index=0, source_text="s", status=ChunkStatus.DONE, translated_text="t", passed_validation=True))

    by_id = {j["id"]: j for j in TestClient(create_app(engine)).get("/jobs").json()["jobs"]}

    assert (by_id["a"]["failed_count"], by_id["a"]["best_effort_count"]) == (2, 1)
    assert (by_id["b"]["failed_count"], by_id["b"]["best_effort_count"]) == (0, 0)


def test_listing_requires_the_session_token_like_other_routes():
    engine, store = _engine()
    store.save_job(_job("a", 0))
    client = TestClient(create_app(engine, session_token=TOKEN))

    assert client.get("/jobs").status_code == 401
    assert client.get("/jobs", headers={"X-Borgesica-Token": "wrong"}).status_code == 401
    ok = client.get("/jobs", headers={"X-Borgesica-Token": TOKEN})
    assert ok.status_code == 200
    assert [j["id"] for j in ok.json()["jobs"]] == ["a"]
