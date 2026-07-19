"""SQLite checkpoint store adapter (M1-10).

Implements the CheckpointStore Protocol using Python's stdlib sqlite3.
Schema from design section 5:
  - jobs(id PK, source_type, source_path, target_lang, model, status,
         budget_usd, chunk_size, line_length, glossary_strategy, quality_mode,
         total_chunks, completed_chunks, cost_usd, created_at, updated_at,
         prose_chunk_tokens, prose_segmentation, continue_on_error)
  - chunks(job_id, chunk_index, source_text, translated_text, status, meta_json,
           PRIMARY KEY(job_id, chunk_index))
  - glossary(job_id, term, translation, locked, note,
             PRIMARY KEY(job_id, term))
  - summaries(job_id, chunk_index, text, PRIMARY KEY(job_id, chunk_index))

save_chunk uses INSERT … ON CONFLICT(job_id, chunk_index) DO UPDATE — idempotent.
Each method opens/closes its own connection (stateless, safe for sequential use).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Generator

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

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_CREATE_JOBS = """
CREATE TABLE IF NOT EXISTS jobs (
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
    updated_at      TEXT NOT NULL,
    prose_chunk_tokens INTEGER NOT NULL DEFAULT 800,
    prose_segmentation TEXT NOT NULL DEFAULT 'batch',
    continue_on_error INTEGER NOT NULL DEFAULT 1
)
"""

# Columns added after the initial release; existing databases are migrated
# in-place via ALTER TABLE guarded by PRAGMA table_info.
_JOBS_MIGRATIONS = {
    "prose_chunk_tokens": "INTEGER NOT NULL DEFAULT 800",
    "prose_segmentation": "TEXT NOT NULL DEFAULT 'batch'",
    "continue_on_error": "INTEGER NOT NULL DEFAULT 1",
}

_CREATE_CHUNKS = """
CREATE TABLE IF NOT EXISTS chunks (
    job_id          TEXT NOT NULL,
    chunk_index     INTEGER NOT NULL,
    source_text     TEXT NOT NULL,
    translated_text TEXT,
    status          TEXT NOT NULL,
    meta_json       TEXT NOT NULL DEFAULT '{}',
    passed_validation INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (job_id, chunk_index)
)
"""

# Columns added after the initial release; existing chunks tables are
# migrated in-place via ALTER TABLE guarded by PRAGMA table_info (same
# pattern as _JOBS_MIGRATIONS). Default 1 (True) so pre-existing rows —
# persisted before validation provenance was tracked — are treated as
# validated (design decision #8).
_CHUNKS_MIGRATIONS = {
    "passed_validation": "INTEGER NOT NULL DEFAULT 1",
}

_CREATE_GLOSSARY = """
CREATE TABLE IF NOT EXISTS glossary (
    job_id          TEXT NOT NULL,
    term            TEXT NOT NULL,
    translation     TEXT NOT NULL,
    locked          INTEGER NOT NULL DEFAULT 0,
    note            TEXT,
    PRIMARY KEY (job_id, term)
)
"""

_CREATE_SUMMARIES = """
CREATE TABLE IF NOT EXISTS summaries (
    job_id          TEXT NOT NULL,
    chunk_index     INTEGER NOT NULL,
    text            TEXT NOT NULL,
    PRIMARY KEY (job_id, chunk_index)
)
"""


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


@contextmanager
def _connect(db_path: str) -> Generator[sqlite3.Connection, None, None]:
    """Context manager: open, yield, commit on success, close always."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _dt_to_iso(dt: datetime) -> str:
    return dt.isoformat()


def _iso_to_dt(s: str) -> datetime:
    # Python 3.11+ supports Z suffix
    return datetime.fromisoformat(s)


# ---------------------------------------------------------------------------
# SQLiteCheckpointStore
# ---------------------------------------------------------------------------


class SQLiteCheckpointStore:
    """CheckpointStore backed by a local SQLite database.

    Args:
        db_path: Path to the SQLite database file, or ":memory:" for in-memory.

    NOTE on :memory: mode: SQLite's in-memory database lives only for the
    lifetime of a single connection.  When db_path is ":memory:", we keep a
    single persistent connection open for the lifetime of this object so that
    all operations see the same in-memory database.  File-based databases use
    per-call connections (one connection per method call) which is safe for
    single-user sequential use.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._is_memory = db_path == ":memory:"
        # For :memory: keep one connection alive for this object's lifetime.
        self._mem_conn: sqlite3.Connection | None = None
        if self._is_memory:
            self._mem_conn = sqlite3.connect(":memory:")
            self._mem_conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_CREATE_JOBS)
            conn.execute(_CREATE_CHUNKS)
            conn.execute(_CREATE_GLOSSARY)
            conn.execute(_CREATE_SUMMARIES)
            existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
            for column, ddl in _JOBS_MIGRATIONS.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {ddl}")
            existing_chunk_cols = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
            for column, ddl in _CHUNKS_MIGRATIONS.items():
                if column not in existing_chunk_cols:
                    conn.execute(f"ALTER TABLE chunks ADD COLUMN {column} {ddl}")

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
        after each operation (but don't close it).
        For file-based databases, open a fresh connection per call.
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

    # --- CheckpointStore Protocol: Job ---

    def save_job(self, job: Job) -> None:
        sql = """
        INSERT INTO jobs (
            id, source_type, source_path, target_lang, model, status,
            budget_usd, chunk_size, line_length, glossary_strategy, quality_mode,
            total_chunks, completed_chunks, cost_usd, created_at, updated_at,
            prose_chunk_tokens, prose_segmentation, continue_on_error
        ) VALUES (
            :id, :source_type, :source_path, :target_lang, :model, :status,
            :budget_usd, :chunk_size, :line_length, :glossary_strategy, :quality_mode,
            :total_chunks, :completed_chunks, :cost_usd, :created_at, :updated_at,
            :prose_chunk_tokens, :prose_segmentation, :continue_on_error
        )
        ON CONFLICT(id) DO UPDATE SET
            status=excluded.status,
            total_chunks=excluded.total_chunks,
            completed_chunks=excluded.completed_chunks,
            cost_usd=excluded.cost_usd,
            updated_at=excluded.updated_at
        """
        with self._connect() as conn:
            conn.execute(sql, {
                "id": job.id,
                "source_type": str(job.config.source_type),
                "source_path": job.source_path,
                "target_lang": job.config.target_lang,
                "model": job.config.model,
                "status": str(job.status),
                "budget_usd": job.config.budget_usd,
                "chunk_size": job.config.chunk_size,
                "line_length": job.config.line_length,
                "glossary_strategy": job.config.glossary_strategy,
                "quality_mode": job.config.quality_mode,
                "total_chunks": job.total_chunks,
                "completed_chunks": job.completed_chunks,
                "cost_usd": job.cost_usd,
                "created_at": _dt_to_iso(job.created_at),
                "updated_at": _dt_to_iso(job.updated_at),
                "prose_chunk_tokens": job.config.prose_chunk_tokens,
                "prose_segmentation": job.config.prose_segmentation,
                "continue_on_error": 1 if job.config.continue_on_error else 0,
            })

    def load_job(self, job_id: str) -> Job | None:
        sql = "SELECT * FROM jobs WHERE id = ?"
        with self._connect() as conn:
            row = conn.execute(sql, (job_id,)).fetchone()

        if row is None:
            return None

        config = JobConfig(
            source_type=SourceType(row["source_type"]),
            model=row["model"],
            target_lang=row["target_lang"],
            budget_usd=row["budget_usd"],
            chunk_size=row["chunk_size"],
            line_length=row["line_length"],
            glossary_strategy=row["glossary_strategy"],
            quality_mode=row["quality_mode"],
            prose_chunk_tokens=row["prose_chunk_tokens"],
            prose_segmentation=row["prose_segmentation"],
            continue_on_error=bool(row["continue_on_error"]),
        )
        return Job(
            id=row["id"],
            config=config,
            source_path=row["source_path"],
            status=JobStatus(row["status"]),
            total_chunks=row["total_chunks"],
            completed_chunks=row["completed_chunks"],
            cost_usd=row["cost_usd"],
            created_at=_iso_to_dt(row["created_at"]),
            updated_at=_iso_to_dt(row["updated_at"]),
        )

    # --- CheckpointStore Protocol: Chunk ---

    def save_chunk(self, job_id: str, chunk: Chunk) -> None:
        """Idempotent upsert keyed by (job_id, chunk.index)."""
        sql = """
        INSERT INTO chunks (
            job_id, chunk_index, source_text, translated_text, status, meta_json,
            passed_validation
        )
        VALUES (
            :job_id, :chunk_index, :source_text, :translated_text, :status, :meta_json,
            :passed_validation
        )
        ON CONFLICT(job_id, chunk_index) DO UPDATE SET
            source_text=excluded.source_text,
            translated_text=excluded.translated_text,
            status=excluded.status,
            meta_json=excluded.meta_json,
            passed_validation=excluded.passed_validation
        """
        with self._connect() as conn:
            conn.execute(sql, {
                "job_id": job_id,
                "chunk_index": chunk.index,
                "source_text": chunk.source_text,
                "translated_text": chunk.translated_text,
                "status": str(chunk.status),
                "meta_json": json.dumps(chunk.meta),
                "passed_validation": 1 if chunk.passed_validation else 0,
            })

    def load_chunks(self, job_id: str) -> list[Chunk]:
        sql = "SELECT * FROM chunks WHERE job_id = ? ORDER BY chunk_index"
        with self._connect() as conn:
            rows = conn.execute(sql, (job_id,)).fetchall()

        return [
            Chunk(
                index=row["chunk_index"],
                source_text=row["source_text"],
                translated_text=row["translated_text"],
                status=ChunkStatus(row["status"]),
                meta=json.loads(row["meta_json"]),
                passed_validation=bool(row["passed_validation"]),
            )
            for row in rows
        ]

    # --- CheckpointStore Protocol: Glossary ---

    def save_glossary(self, job_id: str, g: Glossary) -> None:
        """Replace all glossary entries for this job (full overwrite)."""
        delete_sql = "DELETE FROM glossary WHERE job_id = ?"
        insert_sql = """
        INSERT INTO glossary (job_id, term, translation, locked, note)
        VALUES (:job_id, :term, :translation, :locked, :note)
        ON CONFLICT(job_id, term) DO UPDATE SET
            translation=excluded.translation,
            locked=excluded.locked,
            note=excluded.note
        """
        with self._connect() as conn:
            conn.execute(delete_sql, (job_id,))
            for entry in g.entries:
                conn.execute(insert_sql, {
                    "job_id": job_id,
                    "term": entry.term,
                    "translation": entry.translation,
                    "locked": 1 if entry.locked else 0,
                    "note": entry.note,
                })

    def load_glossary(self, job_id: str) -> Glossary:
        sql = "SELECT * FROM glossary WHERE job_id = ? ORDER BY term"
        with self._connect() as conn:
            rows = conn.execute(sql, (job_id,)).fetchall()

        entries = [
            GlossaryEntry(
                term=row["term"],
                translation=row["translation"],
                locked=bool(row["locked"]),
                note=row["note"],
            )
            for row in rows
        ]
        return Glossary(entries=entries)

    # --- CheckpointStore Protocol: Summary ---

    def save_summary(self, job_id: str, s: RollingSummary) -> None:
        sql = """
        INSERT INTO summaries (job_id, chunk_index, text)
        VALUES (:job_id, :chunk_index, :text)
        ON CONFLICT(job_id, chunk_index) DO UPDATE SET text=excluded.text
        """
        with self._connect() as conn:
            conn.execute(sql, {
                "job_id": job_id,
                "chunk_index": s.chunk_index,
                "text": s.text,
            })

    def load_summary(self, job_id: str) -> RollingSummary:
        """Return the summary from the highest-indexed chunk, or a default."""
        sql = "SELECT * FROM summaries WHERE job_id = ? ORDER BY chunk_index DESC LIMIT 1"
        with self._connect() as conn:
            row = conn.execute(sql, (job_id,)).fetchone()

        if row is None:
            return RollingSummary()

        return RollingSummary(text=row["text"], chunk_index=row["chunk_index"])
