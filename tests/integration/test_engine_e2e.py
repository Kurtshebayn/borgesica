"""End-to-end test for TranslatorEngine (M1-12 + M1-13 gate).

Proves the full M1 walking skeleton works end-to-end:
  English SRT → TranslatorEngine (FakeTranslationProvider) → valid Spanish-shaped SRT

What this test proves:
- create_job reads the SRT, chunks it, seeds an empty glossary, persists state
- run_job calls FakeTranslationProvider for each chunk, assembles output SRT
- Output is a valid SRT (parseable by the `srt` library)
- Cue indices and timestamps are preserved exactly from the source
- The job is resumable: a second run_job on DONE chunks makes 0 provider calls
- The glossary is editable between create and run, and survives the round-trip

This is NOT a live API test. It requires no network or API key.
"""
from __future__ import annotations

import srt

from borgesica.api import TranslatorEngine
from borgesica.adapters.readers.srt_reader import SrtReader
from borgesica.adapters.writers.srt_writer import SrtWriter
from borgesica.domain.glossary import NullGlossaryExtractor
from borgesica.domain.models import (
    GlossaryEntry,
    JobConfig,
    JobStatus,
    SourceType,
)
from tests.fakes import FakeTranslationProvider, InMemoryCheckpointStore


# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------


def _write_srt(path, num_cues: int) -> None:
    """Write a deterministic SRT with num_cues cues."""
    from datetime import timedelta

    subtitles = []
    for i in range(1, num_cues + 1):
        start = timedelta(seconds=(i - 1) * 2)
        end = timedelta(seconds=(i - 1) * 2 + 1)
        subtitles.append(
            srt.Subtitle(
                index=i,
                start=start,
                end=end,
                content=f"English cue {i}.",
            )
        )
    path.write_text(srt.compose(subtitles), encoding="utf-8")


def _make_engine(provider=None, checkpoint=None):
    provider = provider or FakeTranslationProvider()
    checkpoint = checkpoint or InMemoryCheckpointStore()
    engine = TranslatorEngine(
        provider=provider,
        checkpoint=checkpoint,
        readers={SourceType.SRT: SrtReader()},
        writers={SourceType.SRT: SrtWriter()},
        extractor=NullGlossaryExtractor(),
    )
    return engine, provider, checkpoint


# ---------------------------------------------------------------------------
# E2E Test 1: 10-cue SRT runs end-to-end; output is valid SRT
# ---------------------------------------------------------------------------


def test_engine_e2e_srt_basic(tmp_path):
    """10 cues, chunk_size=5 → 2 chunks; output is a valid 10-cue SRT."""
    src = tmp_path / "source.srt"
    _write_srt(src, num_cues=10)
    out = str(tmp_path / "output.srt")

    engine, provider, checkpoint = _make_engine()
    config = JobConfig(source_type=SourceType.SRT, model="fake", chunk_size=5)

    # create → run
    job = engine.create_job(str(src), config)
    assert job.status == JobStatus.CREATED
    assert job.total_chunks == 2
    assert provider.call_count == 0  # no provider call during create

    final_job = engine.run_job(job.id, out_path=out)
    assert final_job.status == JobStatus.DONE
    assert provider.call_count == 2  # 1 call per chunk

    # Output is valid SRT
    out_text = (tmp_path / "output.srt").read_text(encoding="utf-8")
    parsed = list(srt.parse(out_text))

    assert len(parsed) == 10, f"Expected 10 cues, got {len(parsed)}"


# ---------------------------------------------------------------------------
# E2E Test 2: cue indices and timestamps are preserved exactly
# ---------------------------------------------------------------------------


def test_engine_e2e_timestamps_preserved(tmp_path):
    """Timestamps from source SRT are present in the output SRT."""
    from datetime import timedelta

    src = tmp_path / "source.srt"
    _write_srt(src, num_cues=5)
    out = str(tmp_path / "output.srt")

    # Read source cues to know expected timestamps
    source_subs = list(srt.parse(src.read_text(encoding="utf-8")))

    engine, _, _ = _make_engine()
    config = JobConfig(source_type=SourceType.SRT, model="fake", chunk_size=5)

    job = engine.create_job(str(src), config)
    engine.run_job(job.id, out_path=out)

    out_subs = sorted(
        srt.parse((tmp_path / "output.srt").read_text(encoding="utf-8")),
        key=lambda s: s.index,
    )

    assert len(out_subs) == len(source_subs)
    for src_sub, out_sub in zip(source_subs, out_subs):
        assert out_sub.start == src_sub.start, f"Start mismatch at cue {src_sub.index}"
        assert out_sub.end == src_sub.end, f"End mismatch at cue {src_sub.index}"


# ---------------------------------------------------------------------------
# E2E Test 3: glossary editable between create and run; survives round-trip
# ---------------------------------------------------------------------------


def test_engine_e2e_glossary_edit_before_run(tmp_path):
    """update_glossary before run_job is visible after job completes."""
    src = tmp_path / "source.srt"
    _write_srt(src, num_cues=3)
    out = str(tmp_path / "output.srt")

    engine, _, _ = _make_engine()
    config = JobConfig(source_type=SourceType.SRT, model="fake", chunk_size=3)

    job = engine.create_job(str(src), config)

    # Edit glossary before running
    engine.update_glossary(
        job.id,
        [GlossaryEntry(term="Thornwood", translation="Thornwood", locked=True)],
    )

    # Verify edit was persisted
    glossary_before = engine.get_glossary(job.id)
    assert any(e.term == "Thornwood" for e in glossary_before.entries)

    engine.run_job(job.id, out_path=out)

    # Glossary still accessible after run
    glossary_after = engine.get_glossary(job.id)
    entry = next((e for e in glossary_after.entries if e.term == "Thornwood"), None)
    assert entry is not None
    assert entry.locked is True


# ---------------------------------------------------------------------------
# E2E Test 4: resumability — second run on DONE job = 0 provider calls
# ---------------------------------------------------------------------------


def test_engine_e2e_resumable_done_job(tmp_path):
    """Calling resume_job on a DONE job triggers 0 provider calls."""
    src = tmp_path / "source.srt"
    _write_srt(src, num_cues=4)
    out = str(tmp_path / "output.srt")

    engine, provider, _ = _make_engine()
    config = JobConfig(source_type=SourceType.SRT, model="fake", chunk_size=2)

    job = engine.create_job(str(src), config)
    engine.run_job(job.id, out_path=out)

    calls_after_first_run = provider.call_count  # should be 2

    # resume on a DONE job → 0 extra calls
    engine.resume_job(job.id, out_path=out)
    assert provider.call_count == calls_after_first_run, (
        "resume_job on DONE job should make 0 additional provider calls"
    )


# ---------------------------------------------------------------------------
# E2E Test 5: 50-cue SRT → output has exactly 50 cues
# ---------------------------------------------------------------------------


def test_engine_e2e_large_srt(tmp_path):
    """50 cues, chunk_size=10 → 5 chunks; output has exactly 50 cues."""
    src = tmp_path / "large.srt"
    _write_srt(src, num_cues=50)
    out = str(tmp_path / "output.srt")

    engine, provider, _ = _make_engine()
    config = JobConfig(source_type=SourceType.SRT, model="fake", chunk_size=10)

    job = engine.create_job(str(src), config)
    assert job.total_chunks == 5

    final_job = engine.run_job(job.id, out_path=out)
    assert final_job.status == JobStatus.DONE
    assert provider.call_count == 5

    out_text = (tmp_path / "output.srt").read_text(encoding="utf-8")
    parsed = list(srt.parse(out_text))
    assert len(parsed) == 50
