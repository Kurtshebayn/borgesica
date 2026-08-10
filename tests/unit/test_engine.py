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
from pydantic import ValidationError

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
from tests.fakes import FakeCorpusStore, FakeTranslationProvider, InMemoryCheckpointStore


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
    continue_on_error: bool = True,
    extract_chunks: int | None = None,
    extract_offset: int = 0,
) -> JobConfig:
    return JobConfig(
        source_type=source_type,
        model=model,
        chunk_size=chunk_size,
        quality_mode=quality_mode,  # type: ignore[arg-type]
        budget_usd=budget_usd,
        continue_on_error=continue_on_error,
        extract_chunks=extract_chunks,
        extract_offset=extract_offset,
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
# A1 — Extract mode: create_job truncates to the first N chunks
#
# Purpose (backlog A1): make model comparison and quality iteration cheap.
# Truncation happens at CREATE time, before glossary seeding, so the job IS
# the extract: total_chunks, cost estimate, run, and writer all operate on
# the truncated set with no downstream special-casing.
# ---------------------------------------------------------------------------


class _SpyGlossaryExtractor:
    """Records the text passed to extract() so tests can assert on its scope."""

    def __init__(self) -> None:
        self.seen_text: str | None = None

    def extract(self, text: str, config: JobConfig) -> Glossary:  # noqa: ARG002
        self.seen_text = text
        return Glossary(entries=[])


def test_create_job_extract_chunks_truncates_to_first_n(tmp_path: Path) -> None:
    """75 cues / chunk_size=25 → 3 chunks; extract_chunks=2 keeps the first 2."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=75)
    engine, _, checkpoint = _make_engine()
    config = _make_config(chunk_size=25, extract_chunks=2)

    job = engine.create_job(srt_path, config)

    assert job.total_chunks == 2
    chunks = checkpoint.load_chunks(job.id)
    assert len(chunks) == 2
    # The first N, in order — not an arbitrary subset.
    assert [c.index for c in chunks] == [0, 1]


def test_create_job_without_extract_chunks_keeps_all(tmp_path: Path) -> None:
    """Default (None) is a no-op: the full job is created, as before."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=75)
    engine, _, checkpoint = _make_engine()
    config = _make_config(chunk_size=25)

    job = engine.create_job(srt_path, config)

    assert job.total_chunks == 3
    assert len(checkpoint.load_chunks(job.id)) == 3


def test_create_job_extract_chunks_above_total_keeps_all(tmp_path: Path) -> None:
    """Asking for more chunks than exist clamps to the total — not an error."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=75)
    engine, _, checkpoint = _make_engine()
    config = _make_config(chunk_size=25, extract_chunks=99)

    job = engine.create_job(srt_path, config)

    assert job.total_chunks == 3
    assert len(checkpoint.load_chunks(job.id)) == 3


def test_create_job_extract_chunks_scopes_glossary_seeding(tmp_path: Path) -> None:
    """Glossary is seeded from the EXTRACT only — that is the cost saving.

    Seeding from the full source would defeat the purpose: the extractor call
    carries the whole book as input (see api.create_job step 3).
    """
    srt_path = _make_srt_fixture(tmp_path, num_cues=75)
    spy = _SpyGlossaryExtractor()
    provider = FakeTranslationProvider()
    checkpoint = InMemoryCheckpointStore()
    from borgesica.adapters.readers.srt_reader import SrtReader
    from borgesica.adapters.writers.srt_writer import SrtWriter

    engine = TranslatorEngine(
        provider=provider,
        checkpoint=checkpoint,
        readers={SourceType.SRT: SrtReader()},
        writers={SourceType.SRT: SrtWriter()},
        extractor=spy,
    )
    config = _make_config(chunk_size=25, extract_chunks=1)

    engine.create_job(srt_path, config)

    assert spy.seen_text is not None
    # Cue 1 is in the first chunk; cue 75 is in the third and must be excluded.
    assert "Cue 1 text." in spy.seen_text
    assert "Cue 75 text." not in spy.seen_text


def test_extract_chunks_must_be_at_least_one() -> None:
    """extract_chunks=0 is meaningless — reject at the config boundary."""
    with pytest.raises(ValidationError):
        JobConfig(source_type=SourceType.SRT, model="fake-model", extract_chunks=0)


# ---------------------------------------------------------------------------
# A1b — Extract offset: reach an arbitrary window, not just the opening
#
# Translation defects surface deep into a long book (a term first rendered
# consistently can drift once the glossary outgrows its prompt budget), so an
# extract pinned to the first N chunks cannot reproduce or verify them.
# ---------------------------------------------------------------------------


def test_create_job_extract_offset_preserves_original_chunk_indices(
    tmp_path: Path,
) -> None:
    """The window keeps its real position in the book — indices are NOT rebased.

    save_chunk is keyed by (job_id, chunk.index) and load_chunks orders by it,
    neither of which requires a 0-based run; keeping the original index is what
    makes `status` show where in the book the extract actually came from.
    """
    srt_path = _make_srt_fixture(tmp_path, num_cues=75)
    engine, _, checkpoint = _make_engine()
    config = _make_config(chunk_size=25, extract_chunks=1, extract_offset=2)

    job = engine.create_job(srt_path, config)

    assert job.total_chunks == 1
    chunks = checkpoint.load_chunks(job.id)
    assert [c.index for c in chunks] == [2]


def test_create_job_extract_offset_without_count_runs_to_the_end(
    tmp_path: Path,
) -> None:
    """An offset with no count means "from here to the end"."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=75)
    engine, _, checkpoint = _make_engine()
    config = _make_config(chunk_size=25, extract_offset=1)

    job = engine.create_job(srt_path, config)

    assert job.total_chunks == 2
    assert [c.index for c in checkpoint.load_chunks(job.id)] == [1, 2]


def test_create_job_extract_offset_beyond_total_is_rejected(tmp_path: Path) -> None:
    """An offset past the last chunk would create an empty, unrunnable job."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=75)
    engine, _, _ = _make_engine()
    config = _make_config(chunk_size=25, extract_offset=99)

    with pytest.raises(ValueError, match="offset"):
        engine.create_job(srt_path, config)


def test_create_job_extract_offset_scopes_glossary_to_the_window(
    tmp_path: Path,
) -> None:
    """The glossary is seeded from the WINDOW, not from the start of the book."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=75)
    spy = _SpyGlossaryExtractor()
    from borgesica.adapters.readers.srt_reader import SrtReader
    from borgesica.adapters.writers.srt_writer import SrtWriter

    engine = TranslatorEngine(
        provider=FakeTranslationProvider(),
        checkpoint=InMemoryCheckpointStore(),
        readers={SourceType.SRT: SrtReader()},
        writers={SourceType.SRT: SrtWriter()},
        extractor=spy,
    )
    config = _make_config(chunk_size=25, extract_chunks=1, extract_offset=2)

    engine.create_job(srt_path, config)

    assert spy.seen_text is not None
    assert "Cue 75 text." in spy.seen_text
    assert "Cue 1 text." not in spy.seen_text


def test_extract_offset_must_be_non_negative() -> None:
    """A negative offset would silently wrap around the chunk list."""
    with pytest.raises(ValidationError):
        JobConfig(source_type=SourceType.SRT, model="fake-model", extract_offset=-1)


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
# T4 — corpus_store/provider_name ctor kwargs are threaded through to the
# orchestrator during run_job (design decision #6: engine-wide capture, CLI
# included, since api.py is the CLI's driving adapter's composition root).
# ---------------------------------------------------------------------------


def test_run_job_threads_corpus_store_to_orchestrator(tmp_path: Path) -> None:
    """Injecting corpus_store + provider_name into TranslatorEngine results in
    corpus samples being captured during run_job, with provenance carrying
    the configured provider_name."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=1)
    provider = FakeTranslationProvider()
    checkpoint = InMemoryCheckpointStore()
    corpus = FakeCorpusStore()
    from borgesica.adapters.readers.srt_reader import SrtReader
    from borgesica.adapters.writers.srt_writer import SrtWriter
    from borgesica.domain.glossary import NullGlossaryExtractor

    engine = TranslatorEngine(
        provider=provider,
        checkpoint=checkpoint,
        readers={SourceType.SRT: SrtReader()},
        writers={SourceType.SRT: SrtWriter()},
        extractor=NullGlossaryExtractor(),
        corpus_store=corpus,
        provider_name="anthropic",
    )
    config = _make_config(chunk_size=1)

    job = engine.create_job(srt_path, config)
    out_path = str(tmp_path / "out.srt")
    engine.run_job(job.id, out_path=out_path)

    assert corpus.samples
    sample = next(iter(corpus.samples.values()))
    assert sample.provider == "anthropic"


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


def test_resume_recovers_crashed_running_job(tmp_path: Path) -> None:
    """A job stuck in RUNNING (a prior run crashed) is resumable, not rejected.

    Regression from a live run: a provider error left the job in RUNNING with no
    terminal state, and resume_job rejected RUNNING → the job was unrecoverable.
    """
    srt_path = _make_srt_fixture(tmp_path, num_cues=3)
    engine, provider, checkpoint = _make_engine()
    config = _make_config(chunk_size=1)

    job = engine.create_job(srt_path, config)

    # Simulate a crashed run: persist the job as RUNNING with no terminal state.
    crashed = checkpoint.load_job(job.id).model_copy(update={"status": JobStatus.RUNNING})
    checkpoint.save_job(crashed)
    assert engine.status(job.id).status == JobStatus.RUNNING

    # resume_job must recover it (normalize RUNNING → PAUSED) and finish.
    out_path = str(tmp_path / "out.srt")
    final_job = engine.resume_job(job.id, out_path=out_path)
    assert final_job.status == JobStatus.DONE


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


# ---------------------------------------------------------------------------
# REL-002 — cancel_job racing concurrently with run_job()'s _execute_job start
# ---------------------------------------------------------------------------


def test_cancel_race_before_execute_installs_fresh_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel_job() landing in the window between run_job() loading the job
    and _execute_job installing its fresh cancel Event must not be silently
    lost. Regression: _execute_job used to unconditionally overwrite
    self._cancel_flags[job.id] with a brand-new (unset) Event, discarding any
    concurrent cancel signal — the job would then run to completion instead
    of observing the cancellation."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=2)
    engine, provider, checkpoint = _make_engine()
    config = _make_config(chunk_size=1)
    job = engine.create_job(srt_path, config)

    entered = threading.Event()
    proceed = threading.Event()
    original_execute = engine._execute_job

    def _blocking_execute(job_arg, *, out_path, on_progress):
        entered.set()
        proceed.wait(timeout=5)
        return original_execute(job_arg, out_path=out_path, on_progress=on_progress)

    monkeypatch.setattr(engine, "_execute_job", _blocking_execute)

    out_path = str(tmp_path / "out.srt")
    result: dict[str, Job] = {}

    def _run() -> None:
        result["job"] = engine.run_job(job.id, out_path=out_path)

    thread = threading.Thread(target=_run)
    thread.start()
    assert entered.wait(timeout=2)

    # Concurrent cancel lands here — before the real _execute_job body runs.
    engine.cancel_job(job.id)

    proceed.set()
    thread.join(timeout=5)

    assert result["job"].status == JobStatus.CANCELLED
    assert provider.call_count == 0


def test_cancel_then_resume_still_reaches_done(tmp_path: Path) -> None:
    """Companion to test_cancel_then_resume: guards against a fix for the
    concurrent-cancel race (REL-002) accidentally making a fresh, deliberate
    resume-after-cancel inherit a stale set cancel-flag from the prior
    cancelled run."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=3)
    engine, provider, checkpoint = _make_engine()
    config = _make_config(chunk_size=1)

    job = engine.create_job(srt_path, config)
    engine.cancel_job(job.id)
    assert engine.status(job.id).status == JobStatus.CANCELLED

    out_path = str(tmp_path / "out.srt")
    final_job = engine.resume_job(job.id, out_path=out_path)

    assert final_job.status == JobStatus.DONE
    assert provider.call_count == 3


# ---------------------------------------------------------------------------
# continue-on-error WU3-1 — TranslatorEngine.failed_chunk_indices(job_id)
# Spec: job-lifecycle/"skip report surfaces FAILED chunk indices at end of run"
# ---------------------------------------------------------------------------


def test_failed_chunk_indices_empty_for_job_with_no_failures(tmp_path: Path) -> None:
    """failed_chunk_indices returns [] for a job with zero FAILED chunks."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=2)
    engine, _, _ = _make_engine()
    config = _make_config(chunk_size=1)

    job = engine.create_job(srt_path, config)
    out_path = str(tmp_path / "out.srt")
    engine.run_job(job.id, out_path=out_path)

    assert engine.failed_chunk_indices(job.id) == []


def test_failed_chunk_indices_returns_sorted_failed_indices(tmp_path: Path) -> None:
    """After a continue_on_error=True run where chunks 2 and 5 end FAILED,
    failed_chunk_indices returns [2, 5] (sorted, 0-based)."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=6)

    class MismatchOn25Provider(FakeTranslationProvider):
        def translate(self, system: str, user: str, model: str, segment_count: int | None = None):
            self.call_log.append((system, user, model))
            if "Cue 3 " in user or "Cue 6 " in user:  # chunk indices 2 and 5 (1-based cues 3, 6)
                unit = TranslationUnit(
                    translation="<i>Always mismatched</i>",
                    summary_update="Summary.",
                )
            else:
                unit = TranslationUnit(
                    translation=f"[translated] {user}",
                    summary_update="Summary.",
                )
            from borgesica.domain.models import TranslationResult, Usage

            in_tok = self.count_tokens(system + " " + user, model)
            out_tok = self.count_tokens(unit.translation, model)
            return TranslationResult(unit=unit, usage=Usage(input_tokens=in_tok, output_tokens=out_tok))

    provider = MismatchOn25Provider()
    engine, _, _ = _make_engine(provider=provider)
    config = _make_config(chunk_size=1, continue_on_error=True)

    job = engine.create_job(srt_path, config)
    out_path = str(tmp_path / "out.srt")
    final_job = engine.run_job(job.id, out_path=out_path)

    assert final_job.status == JobStatus.DONE
    assert engine.failed_chunk_indices(job.id) == [2, 5]


def test_failed_chunk_indices_raises_for_unknown_job_id() -> None:
    """failed_chunk_indices raises JobNotFoundError for an unknown job_id."""
    engine, _, _ = _make_engine()
    with pytest.raises(JobNotFoundError):
        engine.failed_chunk_indices("unknown-id")


# ---------------------------------------------------------------------------
# T4 — TranslatorEngine.best_effort_chunk_indices(job_id)
# Spec: "End-of-run best-effort summary" — DONE chunks with
# passed_validation=False, sorted 0-based (design decision #9).
# ---------------------------------------------------------------------------


def test_best_effort_chunk_indices_empty_when_all_validated(tmp_path: Path) -> None:
    """best_effort_chunk_indices returns [] when every DONE chunk validated."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=2)
    engine, _, _ = _make_engine()
    config = _make_config(chunk_size=1)

    job = engine.create_job(srt_path, config)
    out_path = str(tmp_path / "out.srt")
    engine.run_job(job.id, out_path=out_path)

    assert engine.best_effort_chunk_indices(job.id) == []


def test_best_effort_chunk_indices_returns_sorted_indices(tmp_path: Path) -> None:
    """DONE chunks persisted with passed_validation=False are surfaced,
    sorted 0-based; a DONE chunk with passed_validation=True is excluded."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=2)
    engine, _, checkpoint = _make_engine()
    config = _make_config(chunk_size=1)

    job = engine.create_job(srt_path, config)
    chunks = {c.index: c for c in checkpoint.load_chunks(job.id)}
    checkpoint.save_chunk(
        job.id,
        chunks[0].model_copy(
            update={"status": ChunkStatus.DONE, "passed_validation": False}
        ),
    )
    checkpoint.save_chunk(
        job.id,
        chunks[1].model_copy(
            update={"status": ChunkStatus.DONE, "passed_validation": True}
        ),
    )

    assert engine.best_effort_chunk_indices(job.id) == [0]


def test_best_effort_chunk_indices_raises_for_unknown_job_id() -> None:
    """best_effort_chunk_indices raises JobNotFoundError for an unknown job_id."""
    engine, _, _ = _make_engine()
    with pytest.raises(JobNotFoundError):
        engine.best_effort_chunk_indices("unknown-id")


# ---------------------------------------------------------------------------
# B1d — the API is the other door into the glossary
#
# merge_additions cleans the glossary during a run, but a FINISHED job never
# merges again, and update_glossary is the path a human uses to curate terms
# by hand. Both bypassed every normalisation rule: update_glossary keyed its
# upsert map on the exact term string, so it reintroduced the very case
# duplicates that merge_additions had just been taught to reject.
# ---------------------------------------------------------------------------


def _engine_with_glossary(tmp_path: Path, entries: list[GlossaryEntry]):
    """Create a job and persist `entries` verbatim, bypassing the API guards."""
    srt_path = _make_srt_fixture(tmp_path, num_cues=3)
    engine, _, checkpoint = _make_engine()
    job = engine.create_job(srt_path, _make_config())
    checkpoint.save_glossary(job.id, Glossary(entries=entries))
    return engine, job


def test_get_glossary_collapses_case_duplicates_persisted_before_the_rule(
    tmp_path: Path,
) -> None:
    """A finished job never merges again, so the repair must happen on read."""
    engine, job = _engine_with_glossary(
        tmp_path,
        [
            GlossaryEntry(term="Alupi", translation="Alupi"),
            GlossaryEntry(term="alupi", translation="alupi"),
        ],
    )

    glossary = engine.get_glossary(job.id)

    assert [e.term for e in glossary.entries] == ["Alupi"]


def test_get_glossary_drops_reversed_entries(tmp_path: Path) -> None:
    """Reviewing a contaminated glossary must not show entries pointing backwards."""
    engine, job = _engine_with_glossary(
        tmp_path,
        [
            GlossaryEntry(term="Will", translation="Voluntad"),
            GlossaryEntry(term="Voluntad", translation="Will"),
        ],
    )

    glossary = engine.get_glossary(job.id)

    assert [e.term for e in glossary.entries] == ["Will"]


def test_get_glossary_does_not_rewrite_what_is_stored(tmp_path: Path) -> None:
    """A getter stays a getter — cleaning on read must not silently persist."""
    engine, job = _engine_with_glossary(
        tmp_path,
        [
            GlossaryEntry(term="Alupi", translation="Alupi"),
            GlossaryEntry(term="alupi", translation="alupi"),
        ],
    )

    engine.get_glossary(job.id)

    stored = engine._checkpoint.load_glossary(job.id)
    assert len(stored.entries) == 2


def test_update_glossary_does_not_reintroduce_a_case_duplicate(tmp_path: Path) -> None:
    """The exact-string upsert key was the same bug merge_additions already had."""
    engine, job = _engine_with_glossary(
        tmp_path, [GlossaryEntry(term="Alupi", translation="Alupi")]
    )

    updated = engine.update_glossary(
        job.id, [GlossaryEntry(term="alupi", translation="alupi tribe")]
    )

    assert len(updated.entries) == 1
    assert updated.entries[0].translation == "alupi tribe"


def test_update_glossary_lets_the_callers_version_win(tmp_path: Path) -> None:
    """Upsert semantics are unchanged: a hand edit overrides what was stored."""
    engine, job = _engine_with_glossary(
        tmp_path, [GlossaryEntry(term="Gleaner", translation="Espigador")]
    )

    updated = engine.update_glossary(
        job.id, [GlossaryEntry(term="Gleaner", translation="Segador", locked=True)]
    )

    assert len(updated.entries) == 1
    assert updated.entries[0].translation == "Segador"
    assert updated.entries[0].locked is True


def test_update_glossary_preserves_the_position_of_an_upserted_entry(
    tmp_path: Path,
) -> None:
    """Editing a term must not reshuffle the glossary around it."""
    engine, job = _engine_with_glossary(
        tmp_path,
        [
            GlossaryEntry(term="Aaru", translation="Aaru"),
            GlossaryEntry(term="Gleaner", translation="Espigador"),
            GlossaryEntry(term="Draoi", translation="Draoi"),
        ],
    )

    updated = engine.update_glossary(
        job.id, [GlossaryEntry(term="Gleaner", translation="Segador")]
    )

    assert [e.term for e in updated.entries] == ["Aaru", "Gleaner", "Draoi"]


def test_update_glossary_honours_a_locked_entry_the_guard_would_reject(
    tmp_path: Path,
) -> None:
    """Locking is the escape hatch: it states intent the guard must not override."""
    engine, job = _engine_with_glossary(
        tmp_path, [GlossaryEntry(term="Will", translation="Voluntad")]
    )

    updated = engine.update_glossary(
        job.id, [GlossaryEntry(term="Voluntad", translation="Will", locked=True)]
    )

    assert [e.term for e in updated.entries] == ["Will", "Voluntad"]


def test_update_glossary_persists_the_cleaned_glossary(tmp_path: Path) -> None:
    """Unlike the getter, a write heals what is stored."""
    engine, job = _engine_with_glossary(
        tmp_path,
        [
            GlossaryEntry(term="Alupi", translation="Alupi"),
            GlossaryEntry(term="alupi", translation="alupi"),
        ],
    )

    engine.update_glossary(job.id, [GlossaryEntry(term="Aaru", translation="Aaru")])

    stored = engine._checkpoint.load_glossary(job.id)
    assert [e.term for e in stored.entries] == ["Alupi", "Aaru"]


# ---------------------------------------------------------------------------
# B1d — a rejected hand edit must be reported, never silently discarded
#
# The direction guard runs on update_glossary, so a caller's own entry could
# be dropped with no trace. The CLI printed "Updated glossary entry: ..."
# unconditionally, so the user was told the edit had landed when it had not.
# ---------------------------------------------------------------------------


def test_update_glossary_raises_when_the_callers_entry_is_rejected(
    tmp_path: Path,
) -> None:
    """Asking for something and not getting it is an error, not a no-op."""
    from borgesica.domain.errors import GlossaryEntryRejectedError

    engine, job = _engine_with_glossary(
        tmp_path, [GlossaryEntry(term="Will", translation="Voluntad")]
    )

    with pytest.raises(GlossaryEntryRejectedError) as exc_info:
        engine.update_glossary(
            job.id, [GlossaryEntry(term="Voluntad", translation="Will")]
        )

    assert [e.term for e in exc_info.value.rejected] == ["Voluntad"]


def test_rejected_entry_error_names_the_term_and_the_way_out(tmp_path: Path) -> None:
    """The message has to be actionable — locking is the documented override."""
    from borgesica.domain.errors import GlossaryEntryRejectedError

    engine, job = _engine_with_glossary(
        tmp_path, [GlossaryEntry(term="Will", translation="Voluntad")]
    )

    with pytest.raises(GlossaryEntryRejectedError) as exc_info:
        engine.update_glossary(
            job.id, [GlossaryEntry(term="Voluntad", translation="Will")]
        )

    message = str(exc_info.value)
    assert "Voluntad" in message
    assert "locked" in message


def test_update_glossary_does_not_persist_a_rejected_edit(tmp_path: Path) -> None:
    """A rejected update must leave the stored glossary exactly as it was."""
    from borgesica.domain.errors import GlossaryEntryRejectedError

    engine, job = _engine_with_glossary(
        tmp_path, [GlossaryEntry(term="Will", translation="Voluntad")]
    )

    with pytest.raises(GlossaryEntryRejectedError):
        engine.update_glossary(
            job.id, [GlossaryEntry(term="Voluntad", translation="Will")]
        )

    stored = engine._checkpoint.load_glossary(job.id)
    assert [e.term for e in stored.entries] == ["Will"]


def test_update_glossary_does_not_raise_for_a_locked_override(tmp_path: Path) -> None:
    """The escape hatch must actually work — locking is not rejected."""
    engine, job = _engine_with_glossary(
        tmp_path, [GlossaryEntry(term="Will", translation="Voluntad")]
    )

    updated = engine.update_glossary(
        job.id, [GlossaryEntry(term="Voluntad", translation="Will", locked=True)]
    )

    assert [e.term for e in updated.entries] == ["Will", "Voluntad"]


def test_update_glossary_does_not_raise_when_only_old_contamination_is_cleaned(
    tmp_path: Path,
) -> None:
    """Repairing what was already stored is maintenance, not a rejected request."""
    engine, job = _engine_with_glossary(
        tmp_path,
        [
            GlossaryEntry(term="Will", translation="Voluntad"),
            GlossaryEntry(term="Voluntad", translation="Will"),
        ],
    )

    updated = engine.update_glossary(
        job.id, [GlossaryEntry(term="Aaru", translation="Aaru")]
    )

    assert [e.term for e in updated.entries] == ["Will", "Aaru"]


def test_update_glossary_accepts_two_case_variants_in_one_call(tmp_path: Path) -> None:
    """Collapsing a caller's own duplicate is deduplication, not rejection."""
    engine, job = _engine_with_glossary(tmp_path, [])

    updated = engine.update_glossary(
        job.id,
        [
            GlossaryEntry(term="Alupi", translation="Alupi"),
            GlossaryEntry(term="alupi", translation="Alupi"),
        ],
    )

    assert len(updated.entries) == 1


def test_glossary_repairs_reports_what_cleaning_would_remove(tmp_path: Path) -> None:
    """`glossary show` must be able to say why it prints fewer entries than are stored."""
    engine, job = _engine_with_glossary(
        tmp_path,
        [
            GlossaryEntry(term="Alupi", translation="Alupi"),
            GlossaryEntry(term="alupi", translation="alupi"),
            GlossaryEntry(term="Will", translation="Voluntad"),
            GlossaryEntry(term="Voluntad", translation="Will"),
        ],
    )

    repairs = engine.glossary_repairs(job.id)

    assert [e.term for e in repairs] == ["alupi", "Voluntad"]


def test_glossary_repairs_is_empty_for_a_clean_glossary(tmp_path: Path) -> None:
    """Nothing to repair means nothing to report."""
    engine, job = _engine_with_glossary(
        tmp_path, [GlossaryEntry(term="Gleaner", translation="Segador")]
    )

    assert engine.glossary_repairs(job.id) == []
