"""M2-2 — Prose chunker tests (TDD: written before implementation).

All tests use FakeTranslationProvider.count_tokens, which returns len(text.split())
(i.e. word count).  Fixtures are therefore sized in WORDS, not characters.

Chapter-boundary signature chosen:
    chunk_prose(paragraphs_by_chapter: list[list[str]], config, provider) -> list[Chunk]

The caller (EpubReader) groups paragraphs per chapter into a list of lists.
chunk_prose flattens them, inserting a chapter-boundary sentinel between chapters so
that no chunk ever spans a chapter boundary.  This is the simplest possible API that
makes the constraint explicit in the type rather than in a convention.

Alternatively: a single flat list[str] with a sentinel string (e.g. "\x00CHAPTER\x00")
to mark chapter breaks — but two separate collections is cleaner.
"""
from __future__ import annotations

import logging

import pytest

from tests.fakes import FakeTranslationProvider


# ---------------------------------------------------------------------------
# Test 1 — paragraph that fits within budget → single chunk, unsplit
# ---------------------------------------------------------------------------


def test_paragraph_within_budget_stays_single_chunk() -> None:
    """400-token paragraph, budget 800 → 1 chunk, unsplit."""
    from borgesica.domain.chunking import chunk_prose
    from borgesica.domain.models import JobConfig, SourceType

    provider = FakeTranslationProvider()
    # 400 words — count_tokens returns len(text.split()) so 400 tokens exactly.
    paragraph = " ".join(f"word{i}" for i in range(400))
    config = JobConfig(
        source_type=SourceType.EPUB,
        model="fake-model",
        prose_chunk_tokens=800,
    )

    # Call with list-of-chapters convention: [[paragraph]]
    chunks = chunk_prose([[paragraph]], config, provider)

    assert len(chunks) == 1
    assert paragraph in chunks[0].source_text


# ---------------------------------------------------------------------------
# Test 2 — over-long paragraph splits at sentence boundaries
# ---------------------------------------------------------------------------


def test_overlong_paragraph_splits_at_sentence_boundaries() -> None:
    """3-sentence paragraph (1,200 tokens total), budget 800 → ≥2 chunks, each ≤800 tokens."""
    from borgesica.domain.chunking import chunk_prose
    from borgesica.domain.models import JobConfig, SourceType

    provider = FakeTranslationProvider()
    # Each sentence is 400 words (400 tokens via FakeProvider) — total 1,200 tokens.
    # Budget is 800, so the 3-sentence block must be split.
    # Sentence 1 (400 tokens) fits alone; sentences 2+3 combined (800 tokens) fit together.
    # So we expect 2 chunks.
    def make_sentence(n: int, start: int) -> str:
        words = " ".join(f"word{i}" for i in range(start, start + n))
        return words + "."

    s1 = make_sentence(400, 0)
    s2 = make_sentence(399, 400)  # 399 words + "." = 400 tokens
    s3 = make_sentence(399, 799)  # 399 words + "." = 400 tokens

    paragraph = f"{s1} {s2} {s3}"
    config = JobConfig(
        source_type=SourceType.EPUB,
        model="fake-model",
        prose_chunk_tokens=800,
    )

    chunks = chunk_prose([[paragraph]], config, provider)

    assert len(chunks) >= 2, f"Expected ≥2 chunks, got {len(chunks)}"
    for chunk in chunks:
        token_count = provider.count_tokens(chunk.source_text, config.model)
        assert token_count <= 800, (
            f"Chunk {chunk.index} has {token_count} tokens (limit 800): {chunk.source_text[:80]}"
        )


# ---------------------------------------------------------------------------
# Test 3 — single over-long sentence → hard-split + WARNING logged
# ---------------------------------------------------------------------------


def test_overlong_sentence_hard_split_and_warning_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Single 1,500-token sentence, budget 800 → hard-split + WARNING in caplog."""
    from borgesica.domain.chunking import chunk_prose
    from borgesica.domain.models import JobConfig, SourceType

    provider = FakeTranslationProvider()
    # One sentence of 1,500 words (no mid-sentence periods).
    sentence = " ".join(f"word{i}" for i in range(1500)) + "."
    config = JobConfig(
        source_type=SourceType.EPUB,
        model="fake-model",
        prose_chunk_tokens=800,
    )

    with caplog.at_level(logging.WARNING, logger="borgesica.domain.chunking"):
        chunks = chunk_prose([[sentence]], config, provider)

    # Each chunk must be within budget.
    for chunk in chunks:
        token_count = provider.count_tokens(chunk.source_text, config.model)
        assert token_count <= 800, f"Chunk {chunk.index} exceeded budget: {token_count}"

    # At least one WARNING must have been emitted.
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) >= 1, "Expected at least one WARNING for oversized sentence"

    # The warning message must contain the sentence char length.
    char_len = len(sentence)
    assert any(str(char_len) in r.message for r in warnings), (
        f"WARNING must mention sentence char length ({char_len}); "
        f"got: {[r.message for r in warnings]}"
    )


# ---------------------------------------------------------------------------
# Test 4 — paragraphs from different chapters are NEVER merged
# ---------------------------------------------------------------------------


def test_different_chapter_paragraphs_never_merged() -> None:
    """Paragraphs from different chapters must not appear in the same chunk."""
    from borgesica.domain.chunking import chunk_prose
    from borgesica.domain.models import JobConfig, SourceType

    provider = FakeTranslationProvider()
    config = JobConfig(
        source_type=SourceType.EPUB,
        model="fake-model",
        prose_chunk_tokens=800,
    )

    # Chapter A: one paragraph of 100 words (well under budget).
    ch_a_para = " ".join(f"chA_word{i}" for i in range(100)) + "."
    # Chapter B: one paragraph of 100 words (well under budget).
    ch_b_para = " ".join(f"chB_word{i}" for i in range(100)) + "."

    # If merging were allowed, 200 tokens < 800 would normally create 1 chunk.
    chunks = chunk_prose([[ch_a_para], [ch_b_para]], config, provider)

    # With the chapter-boundary contract we must get ≥ 2 chunks.
    assert len(chunks) >= 2, (
        f"Expected ≥2 chunks (one per chapter), got {len(chunks)}: {[c.source_text[:40] for c in chunks]}"
    )

    # No single chunk must contain text from both chapters.
    for chunk in chunks:
        has_cha = "chA_word" in chunk.source_text
        has_chb = "chB_word" in chunk.source_text
        assert not (has_cha and has_chb), (
            f"Chunk {chunk.index} mixes chapter-A and chapter-B paragraphs"
        )


# ---------------------------------------------------------------------------
# Test 5 — prose_chunk_tokens is a DISTINCT field with default 800
# ---------------------------------------------------------------------------


def test_prose_chunk_tokens_is_distinct_field_with_default_800() -> None:
    """JobConfig.prose_chunk_tokens defaults to 800 and is independent of chunk_size."""
    from borgesica.domain.models import JobConfig, SourceType

    config = JobConfig(source_type=SourceType.SRT, model="any-model")

    # The new field must exist and default to 800.
    assert hasattr(config, "prose_chunk_tokens"), "JobConfig must have prose_chunk_tokens field"
    assert config.prose_chunk_tokens == 800

    # chunk_size (the SRT cue-batch control) must remain 25 and be independent.
    assert config.chunk_size == 25
    assert config.prose_chunk_tokens != config.chunk_size
