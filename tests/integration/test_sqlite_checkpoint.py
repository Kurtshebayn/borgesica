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
