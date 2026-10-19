"""Unit tests for the SQLite store's glossary pre-pass state.

Covers the two things the whole-book extraction pass persists beyond the
glossary itself:
  - ``Job.glossary_extracted``: the flag that refuses a second, re-billed pass;
  - the per-term extraction proposals: every rendering each batch proposed,
    kept so a reviewer can be shown the terms whose batches disagreed.

All tests use SQLite ``:memory:`` except the migration test, which needs a
file so the schema can be reopened by a second store.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from borgesica.adapters.checkpoints.sqlite_checkpoint import SQLiteCheckpointStore
from borgesica.domain.models import Job, JobConfig, JobStatus, SourceType


def make_job(job_id: str = "job-1", **kwargs) -> Job:
    now = datetime.now(UTC)
    fields = dict(
        id=job_id,
        config=JobConfig(source_type=SourceType.EPUB, model="fake-model"),
        source_path="/tmp/book.epub",
        status=JobStatus.CREATED,
        created_at=now,
        updated_at=now,
    )
    fields.update(kwargs)
    return Job(**fields)


# ---------------------------------------------------------------------------
# Job.glossary_extracted
# ---------------------------------------------------------------------------


def test_a_new_job_has_not_extracted_its_glossary() -> None:
    assert make_job().glossary_extracted is False


def test_glossary_extracted_round_trips() -> None:
    store = SQLiteCheckpointStore(":memory:")
    store.save_job(make_job(glossary_extracted=True))

    loaded = store.load_job("job-1")

    assert loaded is not None
    assert loaded.glossary_extracted is True


def test_saving_an_existing_job_updates_glossary_extracted() -> None:
    store = SQLiteCheckpointStore(":memory:")
    job = make_job()
    store.save_job(job)

    store.save_job(job.model_copy(update={"glossary_extracted": True, "cost_usd": 0.25}))

    loaded = store.load_job("job-1")
    assert loaded is not None
    assert loaded.glossary_extracted is True
    assert loaded.cost_usd == 0.25


def test_a_database_without_the_flag_column_is_migrated_to_false(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    SQLiteCheckpointStore(db).save_job(make_job())
    # Simulate a database written before the flag existed.
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE jobs DROP COLUMN glossary_extracted")

    loaded = SQLiteCheckpointStore(db).load_job("job-1")

    assert loaded is not None
    assert loaded.glossary_extracted is False


# ---------------------------------------------------------------------------
# Extraction proposals
# ---------------------------------------------------------------------------


def test_proposals_of_an_unknown_job_are_empty() -> None:
    assert SQLiteCheckpointStore(":memory:").load_glossary_proposals("nope") == {}


def test_proposals_round_trip_in_batch_order() -> None:
    store = SQLiteCheckpointStore(":memory:")
    proposals = {
        "birthright": ("Primogenitura", "Derecho de nacimiento", "Primogenitura"),
        "vis": ("Vis",),
    }

    store.save_glossary_proposals("job-1", proposals)

    # The order inside each tuple is the batch order: it is the evidence of
    # which passage proposed what, and the plurality's tie-break.
    assert store.load_glossary_proposals("job-1") == proposals


def test_saving_proposals_replaces_the_previous_set() -> None:
    store = SQLiteCheckpointStore(":memory:")
    store.save_glossary_proposals("job-1", {"vis": ("Vis", "Visto"), "will": ("Voluntad",)})

    store.save_glossary_proposals("job-1", {"vis": ("Vis",)})

    assert store.load_glossary_proposals("job-1") == {"vis": ("Vis",)}


def test_proposals_are_kept_per_job() -> None:
    store = SQLiteCheckpointStore(":memory:")
    store.save_glossary_proposals("job-1", {"vis": ("Vis",)})
    store.save_glossary_proposals("job-2", {"will": ("Voluntad",)})

    assert store.load_glossary_proposals("job-1") == {"vis": ("Vis",)}
    assert store.load_glossary_proposals("job-2") == {"will": ("Voluntad",)}
