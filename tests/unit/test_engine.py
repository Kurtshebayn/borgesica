"""Tests for M1-12 — TranslatorEngine public API.

All tests use FakeTranslationProvider + InMemoryCheckpointStore.
No network, no real SRT files (we build Chunks directly in fixtures).
"""
from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from borgesica.api import TranslatorEngine
from borgesica.domain.errors import JobNotFoundError, JobStateError
from borgesica.domain.models import (
    Chunk,
    ChunkStatus,
    CostEstimate,
    Glossary,
    GlossaryEntry,
    Job,
    JobConfig,
    JobStatus,
    Progress,
    SourceType,
    TranslationUnit,
)
from tests.fakes import FakeTranslationProvider, InMemoryCheckpointStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    *,
    source_type: SourceType = SourceType.SRT,
    model: str = "fake-model",
    chunk_size: int = 25,
    quality_mode: str = "fast",
    budget_usd: float | None = None,
) -> JobConfig:
    return JobConfig(
        source_type=source_type,
        model=model,
        chunk_size=chunk_size,
        quality_mode=quality_mode,  # type: ignore[arg-type]
        budget_usd=budget_usd,
    )


def _make_srt_fixture(tmp_path: Path, num_cues: int = 3) -> str:
    """Write a minimal SRT file to tmp_path and return its path string."""
    lines = []
    for i in range(1, num_cues + 1):
        start_ms = (i - 1) * 2000
        end_ms = start_ms + 1500
        start = f"00:00:{start_ms // 1000:02d},{start_ms % 1000:03d}"
        end = f"00:00:{end_ms // 1000:02d},{end_ms % 1000:03d}"
        lines += [str(i), f"{start} --> {end}", f"Cue {i} text.", ""]
    srt_path = tmp_path / "test.srt"
    srt_path.write_text("\n".join(lines), encoding="utf-8")
    return str(srt_path)


def _make_engine(
    *,
    provider: FakeTranslationProvider | None = None,
    checkpoint: InMemoryCheckpointStore | None = None,
) -> tuple[TranslatorEngine, FakeTranslationProvider, InMemoryCheckpointStore]:
    provider = provider or FakeTranslationProvider()
    checkpoint = checkpoint or InMemoryCheckpointStore()
    from borgesica.adapters.readers.srt_reader import SrtReader
    from borgesica.adapters.writers.srt_writer import SrtWriter
    from borgesica.domain.glossary import NullGlossaryExtractor

    engine = TranslatorEngine(
        provider=provider,
        checkpoint=checkpoint,
        readers={SourceType.SRT: SrtReader()},
        writers={SourceType.SRT: SrtWriter()},
        extractor=NullGlossaryExtractor(),
    )
    return engine, provider, checkpoint


# ---------------------------------------------------------------------------
# M1-12 — Test 1: create_job returns CREATED job with correct chunk count
# 75-cue SRT, chunk_size=25 → 3 chunks
# ---------------------------------------------------------------------------


def test_create_job_returns_created_with_correct_chunk_count(tmp_path: Path) -> None:
    """create_job returns Job with status=CREATED, total_chunks=3, completed_chunks=0, cost=0."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=75)
    engine, provider, _ = _make_engine()
    config = _make_config(chunk_size=25)

    job = engine.create_job(srt_path, config)

    assert job.status == JobStatus.CREATED
    assert job.total_chunks == 3
    assert job.completed_chunks == 0
    assert job.cost_usd == 0.0
    # No translation calls during create_job
    assert provider.call_count == 0


# ---------------------------------------------------------------------------
# M1-12 — Test 2: all chunks persisted as PENDING after create_job
# ---------------------------------------------------------------------------


def test_create_job_persists_all_chunks_as_pending(tmp_path: Path) -> None:
    """50 cues, chunk_size=25 → 2 PENDING chunks in checkpoint."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=50)
    engine, provider, checkpoint = _make_engine()
    config = _make_config(chunk_size=25)

    job = engine.create_job(srt_path, config)
    chunks = checkpoint.load_chunks(job.id)

    assert len(chunks) == 2
    for chunk in chunks:
        assert chunk.status == ChunkStatus.PENDING
    # No translation calls
    assert provider.call_count == 0


# ---------------------------------------------------------------------------
# M1-12 — Test 3: get_glossary returns Glossary immediately after create_job
# ---------------------------------------------------------------------------


def test_get_glossary_available_after_create_job(tmp_path: Path) -> None:
    """get_glossary returns a Glossary (may be empty) right after create_job."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=3)
    engine, _, _ = _make_engine()
    config = _make_config()

    job = engine.create_job(srt_path, config)
    glossary = engine.get_glossary(job.id)

    assert isinstance(glossary, Glossary)
    # NullGlossaryExtractor → empty
    assert isinstance(glossary.entries, list)


# ---------------------------------------------------------------------------
# M1-12 — Test 4: update_glossary locks an entry; get_glossary reflects it
# ---------------------------------------------------------------------------


def test_update_glossary_locks_entry(tmp_path: Path) -> None:
    """update_glossary → subsequent get_glossary returns the entry locked."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=3)
    engine, _, _ = _make_engine()
    config = _make_config()

    job = engine.create_job(srt_path, config)
    engine.update_glossary(
        job.id, [GlossaryEntry(term="Riverdale", translation="Riverdale", locked=True)]
    )
    glossary = engine.get_glossary(job.id)

    entry = next((e for e in glossary.entries if e.term == "Riverdale"), None)
    assert entry is not None
    assert entry.locked is True


# ---------------------------------------------------------------------------
# M1-12 — Test 5: provider.translate called 0 times after create_job only
# ---------------------------------------------------------------------------


def test_create_job_makes_no_provider_calls(tmp_path: Path) -> None:
    """No translation calls during create_job."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=10)
    engine, provider, _ = _make_engine()
    config = _make_config()

    engine.create_job(srt_path, config)

    assert provider.call_count == 0


# ---------------------------------------------------------------------------
# M1-12 — Test 6: run_job completes; provider called total_chunks times; DONE
# ---------------------------------------------------------------------------


def test_run_job_completes_and_calls_provider_per_chunk(
    tmp_path: Path,
) -> None:
    """run_job calls provider exactly total_chunks times; job ends DONE."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=3)
    engine, provider, _ = _make_engine()
    config = _make_config(chunk_size=1)  # 1 cue per chunk → 3 chunks

    job = engine.create_job(srt_path, config)
    out_path = str(tmp_path / "out.srt")
    final_job = engine.run_job(job.id, out_path=out_path)

    assert final_job.status == JobStatus.DONE
    assert provider.call_count == 3


# ---------------------------------------------------------------------------
# M1-12 — Test 7: status(job_id) returns Job matching checkpoint state
# ---------------------------------------------------------------------------


def test_status_returns_current_job(tmp_path: Path) -> None:
    """status(job_id) returns persisted Job; reflects completed state after run."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=2)
    engine, _, _ = _make_engine()
    config = _make_config(chunk_size=1)

    job = engine.create_job(srt_path, config)
    assert engine.status(job.id).status == JobStatus.CREATED

    out_path = str(tmp_path / "out.srt")
    engine.run_job(job.id, out_path=out_path)
    assert engine.status(job.id).status == JobStatus.DONE


# ---------------------------------------------------------------------------
# M1-12 — Test 8: status("unknown-id") raises JobNotFoundError
# ---------------------------------------------------------------------------


def test_status_raises_for_unknown_job_id() -> None:
    """status raises JobNotFoundError for a job_id not in checkpoint."""
    engine, _, _ = _make_engine()
    with pytest.raises(JobNotFoundError):
        engine.status("does-not-exist")


# ---------------------------------------------------------------------------
# M1-12 — Test 9: estimate_cost covers only PENDING chunks
# ---------------------------------------------------------------------------


def test_estimate_cost_covers_only_pending_chunks(tmp_path: Path) -> None:
    """estimate_cost returns CostEstimate with usd == 0 for a fully DONE job,
    and non-zero for a PENDING job."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=3)
    engine, _, _ = _make_engine()
    config = _make_config(chunk_size=1)

    job = engine.create_job(srt_path, config)
    estimate_before = engine.estimate_cost(job.id)
    assert isinstance(estimate_before, CostEstimate)
    assert estimate_before.input_tokens > 0
    assert estimate_before.usd > 0.0

    out_path = str(tmp_path / "out.srt")
    engine.run_job(job.id, out_path=out_path)
    estimate_after = engine.estimate_cost(job.id)
    assert estimate_after.usd == 0.0
    assert estimate_after.input_tokens == 0


# ---------------------------------------------------------------------------
# M1-12 — Test 10: cancel_job → resume_job continues from PENDING
# ---------------------------------------------------------------------------


def test_cancel_then_resume(tmp_path: Path) -> None:
    """cancel_job sets CANCELLED; resume_job from CANCELLED translates PENDING chunks."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=3)
    engine, provider, checkpoint = _make_engine()
    config = _make_config(chunk_size=1)

    job = engine.create_job(srt_path, config)

    # Cancel before any translation
    engine.cancel_job(job.id)
    assert engine.status(job.id).status == JobStatus.CANCELLED

    # resume_job should accept CANCELLED
    out_path = str(tmp_path / "out.srt")
    final_job = engine.resume_job(job.id, out_path=out_path)
    assert final_job.status == JobStatus.DONE
    assert provider.call_count == 3


# ---------------------------------------------------------------------------
# M1-12 — Test 11: run_job on RUNNING job raises JobStateError
# ---------------------------------------------------------------------------


def test_run_job_on_running_job_raises_job_state_error(tmp_path: Path) -> None:
    """run_job on a RUNNING job raises JobStateError, 0 additional provider calls."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=1)
    engine, provider, checkpoint = _make_engine()
    config = _make_config(chunk_size=1)

    job = engine.create_job(srt_path, config)
    # Manually force RUNNING state in checkpoint
    running_job = job.model_copy(
        update={"status": JobStatus.RUNNING, "updated_at": datetime.now(UTC)}
    )
    checkpoint.save_job(running_job)

    with pytest.raises(JobStateError):
        engine.run_job(job.id, out_path=str(tmp_path / "out.srt"))

    assert provider.call_count == 0


# ---------------------------------------------------------------------------
# M1-12 — Test 12: model string passed unchanged to provider
# ---------------------------------------------------------------------------


def test_model_string_passed_to_provider(tmp_path: Path) -> None:
    """The model string from config reaches provider.translate unchanged."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=1)
    engine, provider, _ = _make_engine()
    config = _make_config(model="claude-opus-4-5", chunk_size=1)

    job = engine.create_job(srt_path, config)
    out_path = str(tmp_path / "out.srt")
    engine.run_job(job.id, out_path=out_path)

    assert len(provider.call_log) >= 1
    _, _, model_used = provider.call_log[0]
    assert model_used == "claude-opus-4-5"


# ---------------------------------------------------------------------------
# M1-12 — Test 13: on_progress callback receives Progress with accurate cost_usd
# ---------------------------------------------------------------------------


def test_on_progress_callback_receives_progress(tmp_path: Path) -> None:
    """on_progress is called once per chunk; Progress.cost_usd accumulates."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=3)
    engine, _, _ = _make_engine()
    config = _make_config(chunk_size=1)

    job = engine.create_job(srt_path, config)
    progress_list: list[Progress] = []

    out_path = str(tmp_path / "out.srt")
    engine.run_job(job.id, out_path=out_path, on_progress=progress_list.append)

    assert len(progress_list) == 3
    # cost_usd should be non-negative and non-decreasing
    for i, prog in enumerate(progress_list):
        assert isinstance(prog, Progress)
        assert prog.chunk_index == i
        assert prog.cost_usd >= 0.0
    # Later callbacks have higher or equal cost
    assert progress_list[2].cost_usd >= progress_list[1].cost_usd >= progress_list[0].cost_usd
