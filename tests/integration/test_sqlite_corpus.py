"""Integration tests for SQLiteCorpusStore (T3 — desktop-ui-v1).

corpus.db is a SEPARATE store from jobs.db/checkpoint (design decision #6),
write-only (spec: corpus-capture 'Write-only, best-effort, non-blocking').
Since the port is write-only (no load_* methods), these tests verify
persisted rows via a raw sqlite3 connection to the same file.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from borgesica.adapters.corpus.sqlite_corpus import SQLiteCorpusStore
from borgesica.domain.models import CorpusSample


def make_sample(
    job_id: str = "job-001",
    chunk_index: int = 0,
    passed_validation: bool = True,
    validation_errors: str | None = None,
    translated_text: str | None = "hola mundo",
) -> CorpusSample:
    return CorpusSample(
        job_id=job_id,
        chunk_index=chunk_index,
        source_text="hello world",
        translated_text=translated_text,
        provider="anthropic",
        model="claude-3-5-haiku-20241022",
        quality_mode="reflective",
        passed_validation=passed_validation,
        validation_errors=validation_errors,
    )


def _read_all_rows(db_path: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM samples ORDER BY chunk_index").fetchall()
    finally:
        conn.close()


class TestSQLiteCorpusStore:
    def setup_method(self):
        self.store = SQLiteCorpusStore(":memory:")

    # --- Schema ---

    def test_creates_samples_table_with_expected_columns(self):
        conn = sqlite3.connect(":memory:")
        # Re-derive schema via a real file so PRAGMA table_info is meaningful
        # for a fresh, non-memory store (memory-mode connections aren't
        # inspectable from a second connection).
        conn.close()
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            SQLiteCorpusStore(db_path)
            conn = sqlite3.connect(db_path)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(samples)")}
            conn.close()
            expected = {
                "job_id",
                "chunk_index",
                "source_text",
                "translated_text",
                "provider",
                "model",
                "quality_mode",
                "passed_validation",
                "validation_errors",
                "created_at",
            }
            assert expected.issubset(columns)
        finally:
            os.unlink(db_path)

    # --- save_sample basic write ---

    def test_save_sample_writes_all_captured_fields(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = SQLiteCorpusStore(db_path)
            store.save_sample(make_sample())
            rows = _read_all_rows(db_path)
            assert len(rows) == 1
            row = rows[0]
            assert row["job_id"] == "job-001"
            assert row["chunk_index"] == 0
            assert row["source_text"] == "hello world"
            assert row["translated_text"] == "hola mundo"
            assert row["provider"] == "anthropic"
            assert row["model"] == "claude-3-5-haiku-20241022"
            assert row["quality_mode"] == "reflective"
            assert bool(row["passed_validation"]) is True
            assert row["validation_errors"] is None
            assert row["created_at"] is not None
        finally:
            os.unlink(db_path)

    # --- Upsert idempotency (PK: job_id, chunk_index) ---

    def test_save_sample_upserts_on_job_id_and_chunk_index(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = SQLiteCorpusStore(db_path)
            store.save_sample(make_sample(translated_text="first draft"))
            store.save_sample(make_sample(translated_text="revised draft"))
            rows = _read_all_rows(db_path)
            assert len(rows) == 1
            assert rows[0]["translated_text"] == "revised draft"
        finally:
            os.unlink(db_path)

    def test_save_sample_distinct_rows_for_distinct_chunk_index(self):
        self.store.save_sample(make_sample(chunk_index=0))
        self.store.save_sample(make_sample(chunk_index=1))
        # Verify via a second in-memory-backed instance is not possible
        # (separate connection = separate db); use a file-backed store here.
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = SQLiteCorpusStore(db_path)
            store.save_sample(make_sample(chunk_index=0))
            store.save_sample(make_sample(chunk_index=1))
            rows = _read_all_rows(db_path)
            assert len(rows) == 2
            assert [r["chunk_index"] for r in rows] == [0, 1]
        finally:
            os.unlink(db_path)

    # --- validation_errors semantics ---

    def test_best_effort_chunk_records_validation_errors(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = SQLiteCorpusStore(db_path)
            store.save_sample(
                make_sample(
                    passed_validation=False,
                    validation_errors="tag mismatch after 3 attempts",
                )
            )
            rows = _read_all_rows(db_path)
            assert bool(rows[0]["passed_validation"]) is False
            assert rows[0]["validation_errors"] == "tag mismatch after 3 attempts"
        finally:
            os.unlink(db_path)

    def test_validated_chunk_has_null_validation_errors(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = SQLiteCorpusStore(db_path)
            store.save_sample(make_sample(passed_validation=True))
            rows = _read_all_rows(db_path)
            assert bool(rows[0]["passed_validation"]) is True
            assert rows[0]["validation_errors"] is None
        finally:
            os.unlink(db_path)

    # --- FAILED chunk handling (no translated_text) ---

    def test_save_sample_accepts_missing_translated_text(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = SQLiteCorpusStore(db_path)
            store.save_sample(
                make_sample(
                    translated_text=None,
                    passed_validation=False,
                    validation_errors="both primary and fallback exhausted",
                )
            )
            rows = _read_all_rows(db_path)
            assert rows[0]["translated_text"] is None
        finally:
            os.unlink(db_path)

    # --- Separate store: corpus.db independent of any checkpoint db ---

    def test_corpus_db_is_independent_file_from_checkpoint_db(self):
        """Instantiating SQLiteCorpusStore only touches its own db_path; it
        must not require or create any jobs.db / checkpoint file."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            corpus_path = os.path.join(tmp_dir, "corpus.db")
            SQLiteCorpusStore(corpus_path)
            created_files = set(os.listdir(tmp_dir))
            assert created_files == {"corpus.db"}

    # --- Write-failure safety: no exception may corrupt the db ---

    def test_write_failure_does_not_corrupt_previously_committed_rows(self):
        """A save_sample() call that fails mid-transaction (bad column value
        forcing a constraint/type error) must roll back cleanly — it MUST
        raise, and MUST NOT leave the previously-committed row unreadable or
        the table in a partial state."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = SQLiteCorpusStore(db_path)
            store.save_sample(make_sample(chunk_index=0, translated_text="good row"))

            # Force a failure: job_id is NOT NULL — passing None violates the
            # constraint and must raise, not silently corrupt the table.
            broken = make_sample(chunk_index=1)
            broken = broken.model_copy(update={"job_id": None})
            with pytest.raises(Exception):
                store.save_sample(broken)

            # The previously committed row must still be intact and readable.
            rows = _read_all_rows(db_path)
            assert len(rows) == 1
            assert rows[0]["translated_text"] == "good row"
        finally:
            os.unlink(db_path)

    def test_save_sample_raises_on_invalid_db_path(self):
        """The adapter propagates connection/write errors — best-effort
        semantics belong to the caller (T4), not this adapter (design
        decision #6/#10)."""
        nonexistent_dir_path = os.path.join(
            tempfile.gettempdir(), "does-not-exist-xyz", "corpus.db"
        )
        with pytest.raises(Exception):
            SQLiteCorpusStore(nonexistent_dir_path)
