"""GET /jobs/{id}/chunks: the stored source/translation pairs, paged.

Read-only view for the side-by-side screen: pagination with a `total`, the
`only_problems` filter (FAILED + best-effort), untranslated chunks, chapters.
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from borgesica.api import TranslatorEngine
from borgesica.domain.glossary import NullGlossaryExtractor
from borgesica.domain.models import Chunk, ChunkStatus, Job, JobConfig, JobStatus, SourceType
from borgesica.serve.app import MAX_CHUNKS_LIMIT, create_app
from tests.fakes import FakeTranslationProvider, InMemoryCheckpointStore


def _setup(chunks: list[Chunk], source_type: SourceType = SourceType.EPUB):
    store = InMemoryCheckpointStore()
    engine = TranslatorEngine(
        provider=FakeTranslationProvider(),
        checkpoint=store,
        readers={},
        writers={},
        extractor=NullGlossaryExtractor(),
    )
    now = datetime.now(UTC)
    store.save_job(
        Job(
            id="job-c",
            config=JobConfig(source_type=source_type, model="fake-model"),
            source_path="book.epub",
            status=JobStatus.DONE,
            total_chunks=len(chunks),
            created_at=now,
            updated_at=now,
        )
    )
    for chunk in chunks:
        store.save_chunk("job-c", chunk)
    return TestClient(create_app(engine))


def _chunk(index: int, status=ChunkStatus.DONE, *, passed=True, href="ch1.xhtml", text=None):
    return Chunk(
        index=index,
        source_text=text if text is not None else f"source {index}",
        status=status,
        translated_text=f"traducción {index}" if status == ChunkStatus.DONE else None,
        passed_validation=passed,
        meta={"prose_nodes": [{"epub_item_href": href, "node_path": f"/p[{index}]"}]},
    )


def _indices(resp) -> list[int]:
    return [c["index"] for c in resp.json()["chunks"]]


def test_pages_in_reading_order_with_total():
    # Saved out of order: the store's order is not a contract.
    client = _setup([_chunk(i) for i in (4, 0, 3, 1, 2)])

    resp = client.get("/jobs/job-c/chunks", params={"offset": 1, "limit": 2})

    assert resp.status_code == 200
    body = resp.json()
    assert (body["total"], body["offset"], body["limit"]) == (5, 1, 2)
    assert _indices(resp) == [1, 2]


def test_defaults_to_the_first_fifty():
    client = _setup([_chunk(i) for i in range(60)])

    body = client.get("/jobs/job-c/chunks").json()

    assert (body["total"], body["offset"], body["limit"]) == (60, 0, 50)
    assert len(body["chunks"]) == 50


def test_offset_past_the_end_is_an_empty_page():
    client = _setup([_chunk(i) for i in range(3)])

    body = client.get("/jobs/job-c/chunks", params={"offset": 10}).json()

    assert body["total"] == 3
    assert body["chunks"] == []


def test_out_of_range_paging_is_422():
    client = _setup([_chunk(0)])
    for params in (
        {"offset": -1},
        {"limit": 0},
        {"limit": MAX_CHUNKS_LIMIT + 1},
    ):
        assert client.get("/jobs/job-c/chunks", params=params).status_code == 422


def test_item_carries_source_translation_and_chapter():
    client = _setup([_chunk(0, text="I\n\nHe walked."), _chunk(1, href="ch2.xhtml", text="II\n\nShe ran.")])

    first, second = client.get("/jobs/job-c/chunks").json()["chunks"]

    assert first == {
        "index": 0,
        "status": "DONE",
        "source": "I\n\nHe walked.",
        "translation": "traducción 0",
        "best_effort": False,
        "chapter_key": "ch1.xhtml",
        "chapter_title": "I",
    }
    assert (second["chapter_key"], second["chapter_title"]) == ("ch2.xhtml", "II")


def test_chunks_without_a_chapter_have_null_chapter_fields():
    cue = Chunk(index=0, source_text="Hello", meta={"cue_batches": []})
    client = _setup([cue], SourceType.SRT)

    item = client.get("/jobs/job-c/chunks").json()["chunks"][0]

    assert item["chapter_key"] is None
    assert item["chapter_title"] is None


def test_untranslated_chunks_have_a_null_translation():
    client = _setup(
        [_chunk(0), _chunk(1, ChunkStatus.PENDING), _chunk(2, ChunkStatus.TRANSLATING)]
    )

    items = client.get("/jobs/job-c/chunks").json()["chunks"]

    assert [(c["status"], c["translation"], c["best_effort"]) for c in items] == [
        ("DONE", "traducción 0", False),
        ("PENDING", None, False),
        ("TRANSLATING", None, False),
    ]


def test_best_effort_flag_marks_done_chunks_that_failed_validation():
    client = _setup([_chunk(0), _chunk(1, passed=False), _chunk(2, ChunkStatus.FAILED, passed=False)])

    items = client.get("/jobs/job-c/chunks").json()["chunks"]

    assert [c["best_effort"] for c in items] == [False, True, False]


def test_only_problems_keeps_failed_and_best_effort_and_totals_the_filter():
    chunks = [
        _chunk(0),
        _chunk(1, ChunkStatus.FAILED, passed=False),
        _chunk(2),
        _chunk(3, passed=False),
        _chunk(4, ChunkStatus.PENDING),
        _chunk(5, ChunkStatus.FAILED, passed=False),
    ]
    client = _setup(chunks)

    resp = client.get("/jobs/job-c/chunks", params={"only_problems": "true"})
    assert (resp.json()["total"], _indices(resp)) == (3, [1, 3, 5])

    page = client.get("/jobs/job-c/chunks", params={"only_problems": "true", "offset": 1, "limit": 1})
    assert (page.json()["total"], _indices(page)) == (3, [3])


def test_only_problems_with_none_is_an_empty_list():
    client = _setup([_chunk(0), _chunk(1)])

    body = client.get("/jobs/job-c/chunks", params={"only_problems": "true"}).json()

    assert (body["total"], body["chunks"]) == (0, [])


def test_unknown_job_is_404():
    client = _setup([_chunk(0)])
    assert client.get("/jobs/nope/chunks").status_code == 404


def test_requires_the_session_token_when_one_is_set():
    store = InMemoryCheckpointStore()
    engine = TranslatorEngine(
        provider=FakeTranslationProvider(),
        checkpoint=store,
        readers={},
        writers={},
        extractor=NullGlossaryExtractor(),
    )
    client = TestClient(create_app(engine, session_token="s3cret"))

    assert client.get("/jobs/x/chunks").status_code == 401
    assert client.get("/jobs/x/chunks", headers={"X-Borgesica-Token": "s3cret"}).status_code == 404
