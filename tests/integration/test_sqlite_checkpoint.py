"""Integration tests for SQLiteCheckpointStore (M1-10).

All tests use SQLite :memory: — no disk files created.
Tests exercise idempotent upsert, round-trip fidelity, and restart simulation.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone

import pytest

from borgesica.adapters.checkpoints.sqlite_checkpoint import SQLiteCheckpointStore
from borgesica.domain.models import (
    Chunk,
    ChunkStatus,
    Glossary,
    GlossaryEntry,
    Job,
    JobConfig,
    JobStatus,
    RollingSummary,
    SourceType,
)


def make_job(job_id: str = "job-001") -> Job:
    now = datetime.now(tz=timezone.utc)
    return Job(
        id=job_id,
        config=JobConfig(source_type=SourceType.SRT, model="claude-3-5-haiku-20241022"),
        source_path="/tmp/test.srt",
        status=JobStatus.CREATED,
        total_chunks=3,
        completed_chunks=0,
        cost_usd=0.0,
        created_at=now,
        updated_at=now,
    )


def make_chunk(index: int, text: str = "hello world", status: ChunkStatus = ChunkStatus.PENDING) -> Chunk:
    return Chunk(
        index=index,
        source_text=text,
        status=status,
        meta={"cue_index": index + 1, "start": "00:00:01,000", "end": "00:00:03,000"},
    )


class TestSQLiteCheckpointStore:
    def setup_method(self):
        self.store = SQLiteCheckpointStore(":memory:")

    # --- Test 1: save_job + load_job round-trip ---
    def test_save_and_load_job(self):
        job = make_job()
        self.store.save_job(job)
        loaded = self.store.load_job(job.id)
        assert loaded is not None
        assert loaded.id == job.id
        assert loaded.status == JobStatus.CREATED
        assert loaded.total_chunks == 3
        assert loaded.config.model == "claude-3-5-haiku-20241022"

    # --- Test 2: save_chunk + load_chunks round-trip, meta_json preserved ---
    def test_save_and_load_chunks(self):
        job = make_job()
        self.store.save_job(job)
        chunk = make_chunk(0)
        chunk = chunk.model_copy(update={"translated_text": "hola mundo"})
        self.store.save_chunk(job.id, chunk)
        loaded = self.store.load_chunks(job.id)
        assert len(loaded) == 1
        c = loaded[0]
        assert c.index == 0
        assert c.source_text == "hello world"
        assert c.translated_text == "hola mundo"
        assert c.meta["cue_index"] == 1
        assert c.meta["start"] == "00:00:01,000"

    # --- passed_validation round-trip (T2 — design decision #8) ---
    def test_save_and_load_chunk_passed_validation_false(self):
        """A chunk persisted with passed_validation=False (best-effort accepted)
        round-trips as False, not the column default."""
        job = make_job()
        self.store.save_job(job)
        chunk = make_chunk(0)
        chunk = chunk.model_copy(
            update={
                "translated_text": "best effort",
                "status": ChunkStatus.DONE,
                "passed_validation": False,
            }
        )
        self.store.save_chunk(job.id, chunk)
        loaded = self.store.load_chunks(job.id)
        assert len(loaded) == 1
        assert loaded[0].passed_validation is False

    def test_save_and_load_chunk_passed_validation_true_default(self):
        """Triangulation: a chunk saved without overriding passed_validation
        round-trips as True (the model default)."""
        job = make_job()
        self.store.save_job(job)
        chunk = make_chunk(0)
        self.store.save_chunk(job.id, chunk)
        loaded = self.store.load_chunks(job.id)
        assert loaded[0].passed_validation is True

    def test_migrates_existing_db_without_passed_validation_column(self):
        """Opening a jobs.db created before the chunks.passed_validation column
        existed loads old chunk rows with the default (True), no error or data
        loss (spec: job-execution-core/'Backward-compatible load of existing
        jobs.db')."""
        import os
        import sqlite3

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        old_chunks_ddl = """
        CREATE TABLE chunks (
            job_id          TEXT NOT NULL,
            chunk_index     INTEGER NOT NULL,
            source_text     TEXT NOT NULL,
            translated_text TEXT,
            status          TEXT NOT NULL,
            meta_json       TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (job_id, chunk_index)
        )
        """
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(old_chunks_ddl)
            conn.execute(
                "INSERT INTO chunks VALUES (?,?,?,?,?,?)",
                ("job-old", 0, "hello world", "hola mundo", str(ChunkStatus.DONE), "{}"),
            )
            conn.commit()
            conn.close()

            store = SQLiteCheckpointStore(db_path)
            loaded = store.load_chunks("job-old")
            assert len(loaded) == 1
            assert loaded[0].passed_validation is True
            assert loaded[0].translated_text == "hola mundo"

            # New rows persist non-default values in the migrated DB.
            chunk = make_chunk(1)
            chunk = chunk.model_copy(
                update={"status": ChunkStatus.DONE, "passed_validation": False}
            )
            store.save_chunk("job-old", chunk)
            reloaded = {c.index: c for c in SQLiteCheckpointStore(db_path).load_chunks("job-old")}
            assert reloaded[1].passed_validation is False
        finally:
            os.unlink(db_path)

    # --- Test 3: save_chunk twice with same (job_id, chunk.index) → exactly 1 row, no error ---
    def test_save_chunk_idempotent_no_duplicate(self):
        job = make_job()
        self.store.save_job(job)
        chunk = make_chunk(0)
        self.store.save_chunk(job.id, chunk)
        self.store.save_chunk(job.id, chunk)  # second call, same key
        loaded = self.store.load_chunks(job.id)
        assert len(loaded) == 1

    # --- Test 4: save_chunk with different translated_text same key → overwrites ---
    def test_save_chunk_overwrites_on_conflict(self):
        job = make_job()
        self.store.save_job(job)
        chunk_v1 = make_chunk(0)
        chunk_v1 = chunk_v1.model_copy(update={"translated_text": "v1"})
        self.store.save_chunk(job.id, chunk_v1)

        chunk_v2 = make_chunk(0)
        chunk_v2 = chunk_v2.model_copy(update={"translated_text": "v2", "status": ChunkStatus.DONE})
        self.store.save_chunk(job.id, chunk_v2)

        loaded = self.store.load_chunks(job.id)
        assert len(loaded) == 1
        assert loaded[0].translated_text == "v2"
        assert loaded[0].status == ChunkStatus.DONE

    # --- Test 5: save_glossary + load_glossary round-trip, locked preserved ---
    def test_save_and_load_glossary(self):
        job = make_job()
        self.store.save_job(job)
        glossary = Glossary(entries=[
            GlossaryEntry(term="Tranca", translation="crossbar", locked=True, note="wooden beam"),
            GlossaryEntry(term="Puerta", translation="door", locked=False),
        ])
        self.store.save_glossary(job.id, glossary)
        loaded = self.store.load_glossary(job.id)
        assert len(loaded.entries) == 2
        locked = [e for e in loaded.entries if e.locked]
        assert len(locked) == 1
        assert locked[0].term == "Tranca"
        assert locked[0].note == "wooden beam"

    # --- Test 6: save_summary + load_summary round-trip ---
    def test_save_and_load_summary(self):
        job = make_job()
        self.store.save_job(job)
        summary = RollingSummary(text="Chapter 1 introduces the protagonist.", chunk_index=0)
        self.store.save_summary(job.id, summary)
        loaded = self.store.load_summary(job.id)
        assert loaded.text == "Chapter 1 introduces the protagonist."
        assert loaded.chunk_index == 0

    def test_load_summary_returns_highest_index(self):
        """load_summary returns the summary from the highest-indexed chunk."""
        job = make_job()
        self.store.save_job(job)
        self.store.save_summary(job.id, RollingSummary(text="summary-0", chunk_index=0))
        self.store.save_summary(job.id, RollingSummary(text="summary-1", chunk_index=1))
        self.store.save_summary(job.id, RollingSummary(text="summary-2", chunk_index=2))
        loaded = self.store.load_summary(job.id)
        assert loaded.chunk_index == 2
        assert loaded.text == "summary-2"

    # --- Config persistence: prose_segmentation / prose_chunk_tokens / continue_on_error ---
    def test_job_config_extra_fields_round_trip(self):
        """Non-default prose_segmentation, prose_chunk_tokens, and continue_on_error survive save/load."""
        now = datetime.now(tz=timezone.utc)
        job = Job(
            id="job-cfg",
            config=JobConfig(
                source_type=SourceType.EPUB,
                model="claude-3-5-haiku-20241022",
                prose_segmentation="paragraph",
                prose_chunk_tokens=400,
                continue_on_error=False,
            ),
            source_path="/tmp/test.epub",
            created_at=now,
            updated_at=now,
        )
        self.store.save_job(job)
        loaded = self.store.load_job(job.id)
        assert loaded is not None
        assert loaded.config.prose_segmentation == "paragraph"
        assert loaded.config.prose_chunk_tokens == 400
        assert loaded.config.continue_on_error is False

    def test_glossary_budget_tokens_round_trip(self):
        """A tuned glossary budget must survive save/load.

        It is read at RUN time (build_system_prompt), so if it does not
        persist, `resume` silently reverts an expensive-provider job to the
        default budget and quietly changes the prompt mid-book.
        """
        now = datetime.now(tz=timezone.utc)
        job = Job(
            id="job-glossary-budget",
            config=JobConfig(
                source_type=SourceType.EPUB,
                model="claude-3-5-haiku-20241022",
                glossary_budget_tokens=400,
            ),
            source_path="/tmp/test.epub",
            created_at=now,
            updated_at=now,
        )
        self.store.save_job(job)
        loaded = self.store.load_job(job.id)
        assert loaded is not None
        assert loaded.config.glossary_budget_tokens == 400

    def test_migrates_existing_db_without_config_columns(self):
        """Opening a DB created with the pre-migration schema adds the new columns
        and old rows load with JobConfig defaults."""
        import os
        import sqlite3

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        old_jobs_ddl = """
        CREATE TABLE jobs (
            id              TEXT PRIMARY KEY,
            source_type     TEXT NOT NULL,
            source_path     TEXT NOT NULL,
            target_lang     TEXT NOT NULL,
            model           TEXT NOT NULL,
            status          TEXT NOT NULL,
            budget_usd      REAL,
            chunk_size      INTEGER NOT NULL,
            line_length     INTEGER NOT NULL,
            glossary_strategy TEXT NOT NULL,
            quality_mode    TEXT NOT NULL,
            total_chunks    INTEGER NOT NULL DEFAULT 0,
            completed_chunks INTEGER NOT NULL DEFAULT 0,
            cost_usd        REAL NOT NULL DEFAULT 0.0,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )
        """
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(old_jobs_ddl)
            now = datetime.now(tz=timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("job-old", str(SourceType.SRT), "/tmp/old.srt", "es-neutral",
                 "claude-3-5-haiku-20241022", str(JobStatus.CREATED), None, 25, 42,
                 "llm", "fast", 0, 0, 0.0, now, now),
            )
            conn.commit()
            conn.close()

            store = SQLiteCheckpointStore(db_path)
            # Old row loads with defaults
            loaded = store.load_job("job-old")
            assert loaded is not None
            assert loaded.config.prose_segmentation == "batch"
            assert loaded.config.prose_chunk_tokens == 800
            assert loaded.config.continue_on_error is True
            # New rows persist non-default values in the migrated DB
            job = make_job("job-new")
            job = job.model_copy(update={
                "config": job.config.model_copy(update={
                    "prose_segmentation": "paragraph",
                    "continue_on_error": False,
                })
            })
            store.save_job(job)
            reloaded = SQLiteCheckpointStore(db_path).load_job("job-new")
            assert reloaded is not None
            assert reloaded.config.prose_segmentation == "paragraph"
            assert reloaded.config.continue_on_error is False
        finally:
            os.unlink(db_path)

    # --- Test 7: load_job with unknown job_id → returns None ---
    def test_load_unknown_job_returns_none(self):
        result = self.store.load_job("does-not-exist")
        assert result is None

    # --- Test 8: All DONE chunks load as DONE after restart (new connection, same db file) ---
    def test_chunks_persist_across_connections(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            store1 = SQLiteCheckpointStore(db_path)
            job = make_job()
            store1.save_job(job)

            chunk = make_chunk(0)
            chunk = chunk.model_copy(update={"status": ChunkStatus.DONE, "translated_text": "hola"})
            store1.save_chunk(job.id, chunk)

            # New connection simulates restart
            store2 = SQLiteCheckpointStore(db_path)
            loaded = store2.load_chunks(job.id)
            assert len(loaded) == 1
            assert loaded[0].status == ChunkStatus.DONE
            assert loaded[0].translated_text == "hola"
        finally:
            import os
            os.unlink(db_path)

    # --- Extra: multiple chunks load in order ---
    def test_load_chunks_ordered_by_index(self):
        job = make_job()
        self.store.save_job(job)
        for i in [2, 0, 1]:
            self.store.save_chunk(job.id, make_chunk(i))
        loaded = self.store.load_chunks(job.id)
        assert [c.index for c in loaded] == [0, 1, 2]

    # --- Extra: load_glossary returns empty Glossary when none saved ---
    def test_load_glossary_empty_when_none_saved(self):
        job = make_job()
        self.store.save_job(job)
        result = self.store.load_glossary(job.id)
        assert isinstance(result, Glossary)
        assert result.entries == []

    # --- Extra: load_summary returns default RollingSummary when none saved ---
    def test_load_summary_default_when_none_saved(self):
        result = self.store.load_summary("nonexistent-job")
        assert isinstance(result, RollingSummary)
        assert result.text == ""
        assert result.chunk_index == -1
