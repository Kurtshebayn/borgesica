"""M2-2R — Prose chunker provenance rework tests.

All tests follow strict TDD: written FIRST to be RED, then implementation makes them GREEN.

All tests use FakeTranslationProvider.count_tokens (word count).
Fixtures are sized in WORDS.

New signature:
    chunk_prose(node_chunks: list[Chunk], config: JobConfig, provider: TranslationProvider) -> list[Chunk]

Each input Chunk is ONE text node with:
    meta = {"epub_item_href": str, "node_path": str, "chapter_index": int}
    source_text = the node's raw text (may contain inline tags)

Each output Chunk:
    source_text = batched node texts joined with "\\n\\n"
    meta["prose_nodes"] = list[{"epub_item_href": str, "node_path": str}]  — one per segment
    index = 0-based output index
    status = PENDING
"""
from __future__ import annotations

import logging

import pytest

from borgesica.domain.models import Chunk, ChunkStatus, JobConfig, SourceType
from tests.fakes import FakeTranslationProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_node(
    index: int,
    text: str,
    chapter_index: int,
    href: str = "ch1.xhtml",
    node_path: str | None = None,
) -> Chunk:
    """Build a single per-node Chunk as EpubReader would produce it (after M2-2R)."""
    if node_path is None:
        node_path = f"/p[{index}]"
    return Chunk(
        index=index,
        source_text=text,
        status=ChunkStatus.PENDING,
        meta={
            "epub_item_href": href,
            "node_path": node_path,
            "chapter_index": chapter_index,
        },
    )


def _words(n: int, prefix: str = "word") -> str:
    """Return a string of n unique words (count_tokens == n via FakeProvider)."""
    return " ".join(f"{prefix}{i}" for i in range(n))


# ---------------------------------------------------------------------------
# Test 1 — nodes that together fit budget → single output chunk; prose_nodes correct
# ---------------------------------------------------------------------------


def test_nodes_within_budget_produce_single_chunk() -> None:
    """Two 200-word nodes, budget 800 → 1 output chunk; prose_nodes lists both in order."""
    from borgesica.domain.chunking import chunk_prose

    provider = FakeTranslationProvider()
    config = JobConfig(source_type=SourceType.EPUB, model="fake-model", prose_chunk_tokens=800)

    node_a = _make_node(0, _words(200, "aaa"), chapter_index=0, href="ch1.xhtml", node_path="/p[0]")
    node_b = _make_node(1, _words(200, "bbb"), chapter_index=0, href="ch1.xhtml", node_path="/p[1]")

    chunks = chunk_prose([node_a, node_b], config, provider)

    assert len(chunks) == 1, f"Expected 1 chunk, got {len(chunks)}"
    chunk = chunks[0]

    # source_text is both node texts joined with "\n\n"
    assert "aaa0" in chunk.source_text
    assert "bbb0" in chunk.source_text
    assert "\n\n" in chunk.source_text

    # prose_nodes: one entry per segment
    prose_nodes = chunk.meta["prose_nodes"]
    assert len(prose_nodes) == 2
    assert prose_nodes[0]["epub_item_href"] == "ch1.xhtml"
    assert prose_nodes[0]["node_path"] == "/p[0]"
    assert prose_nodes[1]["epub_item_href"] == "ch1.xhtml"
    assert prose_nodes[1]["node_path"] == "/p[1]"

    # index and status
    assert chunk.index == 0
    assert chunk.status == ChunkStatus.PENDING


# ---------------------------------------------------------------------------
# Test 2 — chapter whose nodes exceed budget → multiple chunks, prose_nodes correct per chunk
# ---------------------------------------------------------------------------


def test_over_budget_chapter_splits_into_multiple_chunks() -> None:
    """Chapter with 3 × 400-word nodes, budget 800 → ≥2 chunks, each ≤800 tokens, prose_nodes correct."""
    from borgesica.domain.chunking import chunk_prose

    provider = FakeTranslationProvider()
    config = JobConfig(source_type=SourceType.EPUB, model="fake-model", prose_chunk_tokens=800)

    # 3 nodes of 400 words each = 1200 total — must split into ≥2 chunks
    nodes = [
        _make_node(i, _words(400, f"n{i}w"), chapter_index=0, href="ch1.xhtml", node_path=f"/p[{i}]")
        for i in range(3)
    ]

    chunks = chunk_prose(nodes, config, provider)

    assert len(chunks) >= 2, f"Expected ≥2 chunks for 1200-word chapter, got {len(chunks)}"

    for chunk in chunks:
        token_count = provider.count_tokens(chunk.source_text, config.model)
        assert token_count <= 800, (
            f"Chunk {chunk.index} has {token_count} tokens (budget 800)"
        )

        # prose_nodes must exist and be non-empty
        prose_nodes = chunk.meta["prose_nodes"]
        assert prose_nodes, f"Chunk {chunk.index} has empty prose_nodes"

        # source_text.split("\n\n") length must equal len(prose_nodes)
        segments = chunk.source_text.split("\n\n")
        assert len(segments) == len(prose_nodes), (
            f"Chunk {chunk.index}: {len(segments)} segments but {len(prose_nodes)} prose_nodes"
        )

        # each node_path referenced must be from the input
        input_paths = {n.meta["node_path"] for n in nodes}
        for pn in prose_nodes:
            assert pn["node_path"] in input_paths, (
                f"prose_nodes references unknown node_path {pn['node_path']!r}"
            )


# ---------------------------------------------------------------------------
# Test 3 — single over-budget node → hard-split + exactly ONE WARNING per sentence
# ---------------------------------------------------------------------------


def test_single_over_budget_node_hard_splits_and_logs_exactly_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One 1500-word node (single sentence), budget 800 → hard-split, exactly 1 WARNING."""
    from borgesica.domain.chunking import chunk_prose

    provider = FakeTranslationProvider()
    config = JobConfig(source_type=SourceType.EPUB, model="fake-model", prose_chunk_tokens=800)

    # Single sentence of 1500 words → exceeds budget, triggers hard-split
    giant_sentence = _words(1500, "giant") + "."
    node = _make_node(0, giant_sentence, chapter_index=0, href="ch1.xhtml", node_path="/p[0]")

    with caplog.at_level(logging.WARNING, logger="borgesica.domain.chunking"):
        chunks = chunk_prose([node], config, provider)

    # All chunks within budget
    for chunk in chunks:
        token_count = provider.count_tokens(chunk.source_text, config.model)
        assert token_count <= 800, f"Chunk {chunk.index} exceeded budget: {token_count}"

    # Exactly ONE WARNING (not one per fragment)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, (
        f"Expected exactly 1 WARNING for the oversized sentence, got {len(warnings)}: "
        f"{[r.message for r in warnings]}"
    )

    # WARNING must mention the sentence char length
    char_len = len(giant_sentence)
    assert str(char_len) in warnings[0].message, (
        f"WARNING must mention sentence char length ({char_len}); got: {warnings[0].message}"
    )

    # Each hard-split chunk carries meta["hard_split"] = True and prose_nodes
    for chunk in chunks:
        assert chunk.meta.get("hard_split") is True, (
            f"Chunk {chunk.index} missing hard_split=True in meta"
        )
        assert "prose_nodes" in chunk.meta, f"Chunk {chunk.index} missing prose_nodes"
        for pn in chunk.meta["prose_nodes"]:
            assert pn["node_path"] == "/p[0]"


# ---------------------------------------------------------------------------
# Test 4 — chapter isolation: nodes from different chapter_index are NEVER merged
# ---------------------------------------------------------------------------


def test_chapter_isolation_never_merges_across_chapters() -> None:
    """Nodes from chapter_index=0 and chapter_index=1 must never appear in the same chunk.

    MUST FAIL if chapters were merged — assert == exact count, NOT >=.
    """
    from borgesica.domain.chunking import chunk_prose

    provider = FakeTranslationProvider()
    config = JobConfig(source_type=SourceType.EPUB, model="fake-model", prose_chunk_tokens=800)

    # Each chapter has one 100-word node — together 200 tokens < 800, would merge if unconstrained.
    node_ch0 = _make_node(0, _words(100, "alpha"), chapter_index=0, href="ch1.xhtml", node_path="/p[0]")
    node_ch1 = _make_node(1, _words(100, "beta"), chapter_index=1, href="ch2.xhtml", node_path="/p[0]")

    chunks = chunk_prose([node_ch0, node_ch1], config, provider)

    # EXACT count: 2 (one per chapter) — using == not >= to catch any regression
    assert len(chunks) == 2, (
        f"Expected exactly 2 chunks (one per chapter), got {len(chunks)}: "
        f"{[c.source_text[:40] for c in chunks]}"
    )

    # No chunk mixes both chapters
    for chunk in chunks:
        has_alpha = "alpha0" in chunk.source_text
        has_beta = "beta0" in chunk.source_text
        assert not (has_alpha and has_beta), (
            f"Chunk {chunk.index} mixes chapter-0 and chapter-1 nodes"
        )

    # Verify prose_nodes chapter isolation at the meta level too
    assert all(
        pn["epub_item_href"] == "ch1.xhtml"
        for pn in chunks[0].meta["prose_nodes"]
    ), "First output chunk must only reference ch1.xhtml nodes"
    assert all(
        pn["epub_item_href"] == "ch2.xhtml"
        for pn in chunks[1].meta["prose_nodes"]
    ), "Second output chunk must only reference ch2.xhtml nodes"


# ---------------------------------------------------------------------------
# Test 5 — provenance: split("\n\n") aligns to prose_nodes one-to-one
# ---------------------------------------------------------------------------


def test_provenance_segments_align_to_prose_nodes() -> None:
    """Multi-node output chunk: source_text.split('\\n\\n') length == len(meta['prose_nodes'])
    and segment i aligns to prose_nodes[i].node_path (the segment came from that node).
    """
    from borgesica.domain.chunking import chunk_prose

    provider = FakeTranslationProvider()
    config = JobConfig(source_type=SourceType.EPUB, model="fake-model", prose_chunk_tokens=800)

    # Three 100-word nodes all in chapter 0 — total 300 tokens < 800 → single output chunk
    texts = [_words(100, f"seg{i}") for i in range(3)]
    paths = [f"/p[{i}]" for i in range(3)]
    nodes = [
        _make_node(i, texts[i], chapter_index=0, href="ch1.xhtml", node_path=paths[i])
        for i in range(3)
    ]

    chunks = chunk_prose(nodes, config, provider)

    assert len(chunks) == 1, f"Expected 1 chunk for 300-token input, got {len(chunks)}"
    chunk = chunks[0]

    segments = chunk.source_text.split("\n\n")
    prose_nodes = chunk.meta["prose_nodes"]

    # Exact alignment
    assert len(segments) == len(prose_nodes), (
        f"segments ({len(segments)}) != prose_nodes ({len(prose_nodes)})"
    )
    assert len(segments) == 3, f"Expected 3 segments, got {len(segments)}"

    # Each segment i must contain words from texts[i] and prose_nodes[i] must point to paths[i]
    for i, (segment, pn) in enumerate(zip(segments, prose_nodes)):
        # The segment text should be from the matching node
        assert f"seg{i}0" in segment, (
            f"Segment {i} does not contain expected text from node {i}: {segment[:60]}"
        )
        assert pn["node_path"] == paths[i], (
            f"prose_nodes[{i}].node_path = {pn['node_path']!r}, expected {paths[i]!r}"
        )
        assert pn["epub_item_href"] == "ch1.xhtml"


# ---------------------------------------------------------------------------
# Test 6 — empty / whitespace-only nodes are skipped
# ---------------------------------------------------------------------------


def test_empty_and_whitespace_nodes_are_skipped() -> None:
    """Nodes with empty or whitespace-only source_text are not emitted as chunks."""
    from borgesica.domain.chunking import chunk_prose

    provider = FakeTranslationProvider()
    config = JobConfig(source_type=SourceType.EPUB, model="fake-model", prose_chunk_tokens=800)

    nodes = [
        _make_node(0, "", chapter_index=0, href="ch1.xhtml", node_path="/p[0]"),          # empty
        _make_node(1, "   ", chapter_index=0, href="ch1.xhtml", node_path="/p[1]"),       # whitespace
        _make_node(2, _words(50, "real"), chapter_index=0, href="ch1.xhtml", node_path="/p[2]"),
        _make_node(3, "\t\n", chapter_index=0, href="ch1.xhtml", node_path="/p[3]"),      # tab+newline
    ]

    chunks = chunk_prose(nodes, config, provider)

    # Only the real node should produce a chunk
    assert len(chunks) == 1, f"Expected 1 chunk (only real text), got {len(chunks)}"
    assert "real0" in chunks[0].source_text
    # prose_nodes should only reference the real node
    assert len(chunks[0].meta["prose_nodes"]) == 1
    assert chunks[0].meta["prose_nodes"][0]["node_path"] == "/p[2]"
