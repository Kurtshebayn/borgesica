"""SQLite corpus store adapter (T3 — desktop-ui-v1).

Implements the CorpusStore Protocol using Python's stdlib sqlite3.
Schema (design 'corpus.db (SQLiteCorpusStore, adapters/corpus/sqlite_corpus.py)'):
  - samples(job_id, chunk_index, source_text, translated_text, provider,
            model, quality_mode, passed_validation, validation_errors,
            created_at, PRIMARY KEY(job_id, chunk_index))

corpus.db is a SEPARATE file from jobs.db/checkpoint (design decision #6) —
this module knows nothing about CheckpointStore and never touches its file.

Write-only: no load methods are exposed (spec: corpus-capture 'Write-only,
best-effort, non-blocking'). Best-effort semantics (never let a write
failure fail the translation job) belong to the CALLER (T4's orchestrator
hook), not this adapter — save_sample() raises normally on failure. This
adapter's own safety guarantee is narrower: the per-call transaction
(commit-on-success / rollback-on-exception, mirroring sqlite_checkpoint's
_connect() pattern) ensures a failed write never leaves the table in a
partially-written state.

save_sample uses INSERT … ON CONFLICT(job_id, chunk_index) DO UPDATE —
idempotent, mirroring SQLiteCheckpointStore.save_chunk.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator

from borgesica.domain.models import CorpusSample

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_CREATE_SAMPLES = """
CREATE TABLE IF NOT EXISTS samples (
    job_id            TEXT NOT NULL,
    chunk_index       INTEGER NOT NULL,
    source_text       TEXT NOT NULL,
    translated_text   TEXT,
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    quality_mode      TEXT NOT NULL,
    passed_validation INTEGER NOT NULL DEFAULT 1,
    validation_errors TEXT,
    created_at        TEXT NOT NULL,
    PRIMARY KEY (job_id, chunk_index)
)
"""

# Columns added after the initial release; existing samples tables are
# migrated in-place via ALTER TABLE guarded by PRAGMA table_info (same
# pattern as sqlite_checkpoint's _JOBS_MIGRATIONS / _CHUNKS_MIGRATIONS).
# Empty today — the initial schema already carries every column the design
# specifies — kept as an extension point for future additive columns.
_SAMPLES_MIGRATIONS: dict[str, str] = {}


# ---------------------------------------------------------------------------
# SQLiteCorpusStore
# ---------------------------------------------------------------------------


class SQLiteCorpusStore:
    """CorpusStore backed by a local SQLite database, distinct from jobs.db.

    Args:
        db_path: Path to the SQLite database file, or ":memory:" for in-memory.

    NOTE on :memory: mode: mirrors SQLiteCheckpointStore — a single
    persistent connection is kept alive for the lifetime of this object so
    all operations see the same in-memory database. File-based databases use
    per-call connections (safe for single-user sequential writes).
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._is_memory = db_path == ":memory:"
        self._mem_conn: sqlite3.Connection | None = None
        if self._is_memory:
            self._mem_conn = sqlite3.connect(":memory:")
            self._mem_conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_CREATE_SAMPLES)
            existing = {row[1] for row in conn.execute("PRAGMA table_info(samples)")}
            for column, ddl in _SAMPLES_MIGRATIONS.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE samples ADD COLUMN {column} {ddl}")

    def __del__(self) -> None:
        if self._mem_conn is not None:
            try:
                self._mem_conn.close()
            except Exception:
                pass

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        """Yield a connection.

        For :memory: databases, reuse the persistent connection and commit
        after each operation (but don't close it). For file-based databases,
        open a fresh connection per call. Commit only on success; any
        exception rolls back the transaction before propagating — this is
        the adapter's sole corruption guard (see module docstring).
        """
        if self._is_memory:
            assert self._mem_conn is not None
            try:
                yield self._mem_conn
                self._mem_conn.commit()
            except Exception:
                self._mem_conn.rollback()
                raise
        else:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # --- CorpusStore Protocol ---

    def save_sample(self, sample: CorpusSample) -> None:
        """Persist one corpus sample, upserting on (job_id, chunk_index).

        Raises whatever sqlite3 raises on failure (e.g. IntegrityError for a
        NOT NULL violation, OperationalError for a locked/unwritable file).
        Never caught here — best-effort handling is the caller's
        responsibility (T4)."""
        sql = """
        INSERT INTO samples (
            job_id, chunk_index, source_text, translated_text, provider,
            model, quality_mode, passed_validation, validation_errors,
            created_at
        ) VALUES (
            :job_id, :chunk_index, :source_text, :translated_text, :provider,
            :model, :quality_mode, :passed_validation, :validation_errors,
            :created_at
        )
        ON CONFLICT(job_id, chunk_index) DO UPDATE SET
            source_text=excluded.source_text,
            translated_text=excluded.translated_text,
            provider=excluded.provider,
            model=excluded.model,
            quality_mode=excluded.quality_mode,
            passed_validation=excluded.passed_validation,
            validation_errors=excluded.validation_errors,
            created_at=excluded.created_at
        """
        with self._connect() as conn:
            conn.execute(sql, {
                "job_id": sample.job_id,
                "chunk_index": sample.chunk_index,
                "source_text": sample.source_text,
                "translated_text": sample.translated_text,
                "provider": sample.provider,
                "model": sample.model,
                "quality_mode": sample.quality_mode,
                "passed_validation": 1 if sample.passed_validation else 0,
                "validation_errors": sample.validation_errors,
                "created_at": datetime.now(tz=timezone.utc).isoformat(),
            })
