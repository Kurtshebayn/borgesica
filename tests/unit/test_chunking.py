"""Tests for borgesica.domain.chunking — M1-4 (strict TDD).

All test scenarios driven from spec: subtitle-translation/cues-batched.
Cue Chunk objects are constructed IN MEMORY — no file I/O.
"""
from __future__ import annotations

from borgesica.domain.chunking import SrtChunker
from borgesica.domain.models import Chunk, ChunkStatus, JobConfig, SourceType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_cue(index: int, start: str = "00:00:00,000", end: str = "00:00:01,000") -> Chunk:
    """Build an individual cue Chunk (as SrtReader would produce)."""
    return Chunk(
        index=index,
        source_text=f"Cue text {index}",
        status=ChunkStatus.PENDING,
        meta={
            "cue_index": index + 1,  # SRT cues are 1-based
            "start": start,
            "end": end,
        },
    )


def make_config(chunk_size: int) -> JobConfig:
    return JobConfig(source_type=SourceType.SRT, model="claude-haiku-4-5", chunk_size=chunk_size)


# ---------------------------------------------------------------------------
# Spec scenario: 60 cues, chunk_size=20 → exactly 3 chunks of 20
# ---------------------------------------------------------------------------


def test_60_cues_chunk_size_20_produces_3_chunks() -> None:
    cues = [make_cue(i) for i in range(60)]
    config = make_config(chunk_size=20)
    batches = SrtChunker.chunk(cues, config)
    assert len(batches) == 3


def test_60_cues_chunk_size_20_each_batch_has_20_cues() -> None:
    cues = [make_cue(i) for i in range(60)]
    config = make_config(chunk_size=20)
    batches = SrtChunker.chunk(cues, config)
    for batch in batches:
        cue_batch_items = batch.meta["cue_batches"]
        assert len(cue_batch_items) == 20


# ---------------------------------------------------------------------------
# Spec scenario: 55 cues, chunk_size=25 → chunks of 25, 25, 5
# ---------------------------------------------------------------------------


def test_55_cues_chunk_size_25_produces_3_chunks() -> None:
    cues = [make_cue(i) for i in range(55)]
    config = make_config(chunk_size=25)
    batches = SrtChunker.chunk(cues, config)
    assert len(batches) == 3


def test_55_cues_chunk_size_25_last_batch_has_5_cues() -> None:
    cues = [make_cue(i) for i in range(55)]
    config = make_config(chunk_size=25)
    batches = SrtChunker.chunk(cues, config)
    assert len(batches[0].meta["cue_batches"]) == 25
    assert len(batches[1].meta["cue_batches"]) == 25
    assert len(batches[2].meta["cue_batches"]) == 5


# ---------------------------------------------------------------------------
# Spec scenario: 1 cue → 1 chunk
# ---------------------------------------------------------------------------


def test_single_cue_produces_one_chunk() -> None:
    cues = [make_cue(0)]
    config = make_config(chunk_size=25)
    batches = SrtChunker.chunk(cues, config)
    assert len(batches) == 1


# ---------------------------------------------------------------------------
# Spec: chunk.meta contains cue_indices for each cue in the batch
# ---------------------------------------------------------------------------


def test_chunk_meta_contains_cue_indices() -> None:
    """Each batch Chunk.meta has 'cue_batches' list with cue_index per cue."""
    cues = [make_cue(i) for i in range(3)]
    config = make_config(chunk_size=10)
    batches = SrtChunker.chunk(cues, config)
    assert len(batches) == 1
    cue_batches = batches[0].meta["cue_batches"]
    assert len(cue_batches) == 3
    # Each item must carry cue_index, start, end, text
    for i, item in enumerate(cue_batches):
        assert "cue_index" in item
        assert "start" in item
        assert "end" in item
        assert "text" in item


# ---------------------------------------------------------------------------
# Spec: no cue is ever split across chunks
# ---------------------------------------------------------------------------


def test_no_cue_split_across_chunks() -> None:
    """Every cue index appears in exactly one batch."""
    cues = [make_cue(i) for i in range(7)]
    config = make_config(chunk_size=3)
    batches = SrtChunker.chunk(cues, config)
    seen: set[int] = set()
    for batch in batches:
        for item in batch.meta["cue_batches"]:
            cue_idx = item["cue_index"]
            assert cue_idx not in seen, f"Cue {cue_idx} appears in more than one batch"
            seen.add(cue_idx)
    assert len(seen) == 7


# ---------------------------------------------------------------------------
# source_text is cues joined by double newline
# ---------------------------------------------------------------------------


def test_batch_source_text_is_double_newline_joined() -> None:
    cues = [make_cue(i) for i in range(3)]
    config = make_config(chunk_size=10)
    batches = SrtChunker.chunk(cues, config)
    expected = "\n\n".join(c.source_text for c in cues)
    assert batches[0].source_text == expected


# ---------------------------------------------------------------------------
# Chunk.index is 0-based batch index
# ---------------------------------------------------------------------------


def test_chunk_index_is_zero_based_batch_index() -> None:
    cues = [make_cue(i) for i in range(6)]
    config = make_config(chunk_size=2)
    batches = SrtChunker.chunk(cues, config)
    for expected_idx, batch in enumerate(batches):
        assert batch.index == expected_idx


# ---------------------------------------------------------------------------
# Empty cue list returns empty list
# ---------------------------------------------------------------------------


def test_empty_cues_returns_empty_list() -> None:
    batches = SrtChunker.chunk([], make_config(chunk_size=25))
    assert batches == []


# ---------------------------------------------------------------------------
# chunk_prose (M2-2) — stub test removed; full behaviour tested in
# tests/unit/test_prose_chunker.py
# ---------------------------------------------------------------------------
