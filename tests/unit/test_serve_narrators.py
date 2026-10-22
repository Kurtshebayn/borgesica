"""GET/PUT /jobs/{id}/narrators and the engine path that feeds the prompt.

The narrator fact is set per chapter by a person in the desktop review; the
server lists the chapters (pre-marking the first-person ones) and stores what
the person decides. A run then gives each chunk the fact of its own chapter.
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from borgesica.api import TranslatorEngine
from borgesica.domain.models import Chunk, Job, JobConfig, JobStatus, SourceType
from borgesica.domain.narrators import NarratorFact
from tests.fakes import FakeTranslationProvider, InMemoryCheckpointStore

FIRST_PERSON = "I keep my gaze fixed on Tash. I wait, and I tell myself it is over. " * 20
THIRD_PERSON = "He keeps his gaze fixed on Tash. He waits, and he tells himself so."


class _RecordingWriter:
    def __init__(self) -> None:
        self.written: list[Chunk] = []

    def write(self, chunks, source_path, out_path, *, target_lang="es-neutral") -> None:
        self.written = list(chunks)


def _chunk(index: int, href: str, text: str) -> Chunk:
    return Chunk(
        index=index,
        source_text=text,
        meta={"prose_nodes": [{"epub_item_href": href, "node_path": f"/p[{index}]"}]},
    )


BOOK = [
    _chunk(0, "chapter01.xhtml", "I\n\n" + FIRST_PERSON),
    _chunk(1, "chapter01.xhtml", FIRST_PERSON),
    _chunk(2, "chapter02.xhtml", "II\n\n" + FIRST_PERSON),
    _chunk(3, "major_characters.xhtml", "MAJOR CHARACTERS\n\n" + THIRD_PERSON),
]


def _setup(
    chunks: list[Chunk] = BOOK, source_type: SourceType = SourceType.EPUB
) -> tuple[TranslatorEngine, InMemoryCheckpointStore, FakeTranslationProvider, str]:
    from borgesica.domain.glossary import NullGlossaryExtractor

    store = InMemoryCheckpointStore()
    provider = FakeTranslationProvider()
    engine = TranslatorEngine(
        provider=provider,
        checkpoint=store,
        readers={},
        writers={source_type: _RecordingWriter()},
        extractor=NullGlossaryExtractor(),
    )
    now = datetime.now(UTC)
    job = Job(
        id="job-n",
        config=JobConfig(source_type=source_type, model="fake-model"),
        source_path="book.epub",
        status=JobStatus.CREATED,
        total_chunks=len(chunks),
        created_at=now,
        updated_at=now,
    )
    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    return engine, store, provider, job.id


def _client(engine: TranslatorEngine) -> TestClient:
    from borgesica.serve.app import create_app

    return TestClient(create_app(engine))


VIS = {"name": "Vis", "gender": "masculine"}


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------


def test_get_lists_chapters_with_first_person_premarked():
    engine, _, _, job_id = _setup()
    resp = _client(engine).get(f"/jobs/{job_id}/narrators")

    assert resp.status_code == 200
    chapters = resp.json()["chapters"]
    assert [c["key"] for c in chapters] == [
        "chapter01.xhtml",
        "chapter02.xhtml",
        "major_characters.xhtml",
    ]
    first = chapters[0]
    assert first["title"] == "I"
    assert first["opening"].startswith("I keep my gaze")
    assert (first["first_chunk"], first["last_chunk"]) == (0, 1)
    assert [c["first_person"] for c in chapters] == [True, True, False]
    assert chapters[2]["first_person_per_1k"] == 0
    assert all(c["narrator"] is None for c in chapters)


def test_get_unknown_job_is_404():
    engine, _, _, _ = _setup()
    assert _client(engine).get("/jobs/nope/narrators").status_code == 404


def test_get_for_a_subtitle_job_lists_no_chapters():
    srt = [Chunk(index=0, source_text="Hello", meta={"cue_batches": []})]
    engine, _, _, job_id = _setup(srt, SourceType.SRT)
    resp = _client(engine).get(f"/jobs/{job_id}/narrators")

    assert resp.status_code == 200
    assert resp.json() == {"chapters": []}


# ---------------------------------------------------------------------------
# PUT
# ---------------------------------------------------------------------------


def test_put_stores_narrators_and_returns_the_chapter_list():
    engine, store, _, job_id = _setup()
    client = _client(engine)

    resp = client.put(
        f"/jobs/{job_id}/narrators",
        json={
            "narrators": [
                {"key": "chapter01.xhtml", **VIS},
                {"key": "chapter02.xhtml", "name": None, "gender": "feminine"},
            ]
        },
    )

    assert resp.status_code == 200
    by_key = {c["key"]: c["narrator"] for c in resp.json()["chapters"]}
    assert by_key == {
        "chapter01.xhtml": VIS,
        "chapter02.xhtml": {"name": None, "gender": "feminine"},
        "major_characters.xhtml": None,
    }
    assert client.get(f"/jobs/{job_id}/narrators").json() == resp.json()
    assert store.load_narrators(job_id) == {
        "chapter01.xhtml": NarratorFact(name="Vis", gender="masculine"),
        "chapter02.xhtml": NarratorFact(gender="feminine"),
    }


def test_put_replaces_the_whole_set():
    engine, store, _, job_id = _setup()
    client = _client(engine)
    client.put(
        f"/jobs/{job_id}/narrators",
        json={"narrators": [{"key": "chapter01.xhtml", **VIS}, {"key": "chapter02.xhtml", **VIS}]},
    )

    client.put(f"/jobs/{job_id}/narrators", json={"narrators": [{"key": "chapter01.xhtml", **VIS}]})

    assert list(store.load_narrators(job_id)) == ["chapter01.xhtml"]


def test_put_an_empty_list_clears_every_narrator():
    engine, store, _, job_id = _setup()
    client = _client(engine)
    client.put(f"/jobs/{job_id}/narrators", json={"narrators": [{"key": "chapter01.xhtml", **VIS}]})

    resp = client.put(f"/jobs/{job_id}/narrators", json={"narrators": []})

    assert resp.status_code == 200
    assert store.load_narrators(job_id) == {}


def test_put_unknown_chapter_is_422_and_stores_nothing():
    engine, store, _, job_id = _setup()
    resp = _client(engine).put(
        f"/jobs/{job_id}/narrators",
        json={"narrators": [{"key": "chapter01.xhtml", **VIS}, {"key": "nope.xhtml", **VIS}]},
    )

    assert resp.status_code == 422
    assert "nope.xhtml" in resp.json()["detail"]
    assert store.load_narrators(job_id) == {}


def test_put_duplicate_chapter_is_422_and_stores_nothing():
    engine, store, _, job_id = _setup()
    resp = _client(engine).put(
        f"/jobs/{job_id}/narrators",
        json={
            "narrators": [
                {"key": "chapter01.xhtml", **VIS},
                {"key": "chapter01.xhtml", "name": "Emissa", "gender": "feminine"},
            ]
        },
    )

    assert resp.status_code == 422
    assert "chapter01.xhtml" in resp.json()["detail"]
    assert store.load_narrators(job_id) == {}


def test_put_bad_gender_is_422():
    engine, store, _, job_id = _setup()
    resp = _client(engine).put(
        f"/jobs/{job_id}/narrators",
        json={"narrators": [{"key": "chapter01.xhtml", "name": "Vis", "gender": "unanchored"}]},
    )

    assert resp.status_code == 422
    assert store.load_narrators(job_id) == {}


def test_put_unknown_job_is_404():
    engine, _, _, _ = _setup()
    resp = _client(engine).put("/jobs/nope/narrators", json={"narrators": []})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Engine: a run gives each chunk its own chapter's narrator
# ---------------------------------------------------------------------------


def test_run_job_sends_the_stored_narrator_only_to_its_chapters(tmp_path):
    engine, store, provider, job_id = _setup()
    store.save_narrators(
        job_id, {"chapter01.xhtml": NarratorFact(name="Vis", gender="masculine")}
    )

    final = engine.run_job(job_id, out_path=str(tmp_path / "out.epub"))

    assert final.status == JobStatus.DONE
    systems = [system for system, _user, _model in provider.call_log]
    assert len(systems) == 4
    assert ["[NARRATOR]" in s for s in systems] == [True, True, False, False]
    assert "Vis, male" in systems[0]
