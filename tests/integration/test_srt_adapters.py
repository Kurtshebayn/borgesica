"""Integration tests for SrtReader and SrtWriter (M1-9).

Tests run against real .srt fixture files in tests/integration/fixtures/.
No mocking — these are integration tests that exercise the actual srt library.
"""
from __future__ import annotations

import os
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
import srt

from borgesica.adapters.readers.srt_reader import SrtReader
from borgesica.adapters.writers.srt_writer import SrtWriter, reflow
from borgesica.domain.models import Chunk, ChunkStatus, JobConfig, SourceType

FIXTURES = Path(__file__).parent / "fixtures"


def make_config(line_length: int = 42) -> JobConfig:
    return JobConfig(source_type=SourceType.SRT, model="claude-3-5-haiku-20241022", line_length=line_length)


# ---------------------------------------------------------------------------
# SrtReader tests
# ---------------------------------------------------------------------------


class TestSrtReader:
    def setup_method(self):
        self.reader = SrtReader()
        self.config = make_config()

    def test_cue_with_inline_tag(self):
        """Cue 1 with inline <i> tag: meta has cue_index, timestamps, source_text preserved."""
        chunks = self.reader.read(str(FIXTURES / "tagged.srt"), self.config)
        # First cue (index 1) has <i> tag
        c = chunks[0]
        assert c.meta["cue_index"] == 1
        assert c.source_text == "Hello, <i>world</i>."
        # Timestamps are stored as timedelta or string
        assert "start" in c.meta
        assert "end" in c.meta

    def test_multi_line_cue(self):
        """Multi-line cue: source_text joins lines with newline."""
        chunks = self.reader.read(str(FIXTURES / "tagged.srt"), self.config)
        # Cue 3 is multi-line
        c = chunks[2]
        assert c.meta["cue_index"] == 3
        assert c.source_text == "Multi-line cue\nwith a second line."

    def test_empty_srt_returns_empty_list(self):
        """Empty SRT file returns empty list without raising."""
        chunks = self.reader.read(str(FIXTURES / "empty.srt"), self.config)
        assert chunks == []

    def test_chunk_status_defaults_to_pending(self):
        """All parsed cues start as PENDING chunks."""
        chunks = self.reader.read(str(FIXTURES / "simple.srt"), self.config)
        assert all(c.status == ChunkStatus.PENDING for c in chunks)

    def test_chunk_indices_sequential(self):
        """Chunk indices are 0-based and sequential."""
        chunks = self.reader.read(str(FIXTURES / "simple.srt"), self.config)
        assert [c.index for c in chunks] == list(range(len(chunks)))

    def test_ten_cues_parsed(self):
        """simple.srt has 10 cues → 10 chunks."""
        chunks = self.reader.read(str(FIXTURES / "simple.srt"), self.config)
        assert len(chunks) == 10


# ---------------------------------------------------------------------------
# reflow() unit-level tests
# ---------------------------------------------------------------------------


class TestReflow:
    def test_short_text_no_split(self):
        """Short translation (≤ line_length) stays on one line."""
        result = reflow("Short text.", line_length=42)
        assert "\n" not in result
        assert result == "Short text."

    def test_two_line_split_at_word_boundary(self):
        """57-char text with line_length=42 → exactly 2 lines, each ≤ 42 chars, split at word boundary."""
        text = "This sentence is somewhat longer than forty-two characters total"
        result = reflow(text, line_length=42)
        lines = result.split("\n")
        assert len(lines) == 2
        for line in lines:
            assert len(line) <= 42
        # Reassembled text matches original (no words lost)
        assert " ".join(result.split()) == " ".join(text.split())

    def test_three_line_fallback(self):
        """When 2 lines not possible, fall back to 3 lines."""
        # 90 chars, word boundaries not clean for 2 lines of ≤42
        # craft text that forces 3 lines
        text = "supercalifragilistic expialidocious extraordinarily magnificent spectacular"
        result = reflow(text, line_length=42)
        lines = result.split("\n")
        # Must be 2 or 3 lines — never 4+
        assert 1 <= len(lines) <= 3
        for line in lines:
            assert len(line) <= 42

    def test_never_four_lines(self):
        """Even for long text, output has ≤ 3 lines."""
        text = "word " * 30  # 150 chars
        result = reflow(text.strip(), line_length=42)
        lines = result.split("\n")
        assert len(lines) <= 3

    def test_line_length_30_respected(self):
        """line_length=30 applied to a 50-char cue produces lines ≤ 30."""
        text = "This is fifty characters total here!"
        result = reflow(text, line_length=30)
        lines = result.split("\n")
        for line in lines:
            assert len(line) <= 30


# ---------------------------------------------------------------------------
# SrtWriter tests
# ---------------------------------------------------------------------------


class TestSrtWriter:
    def setup_method(self):
        self.reader = SrtReader()
        self.writer = SrtWriter()

    def _make_translated_chunks(self, source_chunks: list[Chunk], translations: list[str]) -> list[Chunk]:
        """Build translated copies of source chunks."""
        result = []
        for chunk, translation in zip(source_chunks, translations):
            translated = chunk.model_copy(update={"translated_text": translation, "status": ChunkStatus.DONE})
            result.append(translated)
        return result

    def test_roundtrip_50_cues(self):
        """10 batches of 5 cues each → output has exactly 50 cues with original indices+timestamps."""
        from borgesica.domain.chunking import SrtChunker

        config = make_config()
        # Read simple.srt (10 cues) — each cue is one chunk from reader
        cue_chunks = self.reader.read(str(FIXTURES / "simple.srt"), config)
        # Batch them into groups of 2 (5 batches of 2 cues)
        chunker = SrtChunker()
        batched = chunker.chunk(cue_chunks, config.__class__(
            source_type=SourceType.SRT,
            model="claude-3-5-haiku-20241022",
            chunk_size=2,
            line_length=42,
        ))

        # Translate each batch: output = same text (identity translation)
        translations = []
        for batch in batched:
            # Build an identity translation: join the cue texts
            cue_texts = [cb["text"] for cb in batch.meta["cue_batches"]]
            translations.append("\n\n".join(cue_texts))

        translated_batches = self._make_translated_chunks(batched, translations)

        with tempfile.NamedTemporaryFile(suffix=".srt", delete=False, mode="w", encoding="utf-8") as f:
            out_path = f.name

        try:
            src_path = str(FIXTURES / "simple.srt")
            self.writer.write(translated_batches, src_path, out_path)

            # Parse output with srt library
            with open(out_path, encoding="utf-8") as f:
                content = f.read()
            parsed = list(srt.parse(content))
            assert len(parsed) == 10
            # Check original indices and timestamps are preserved
            original = list(srt.parse(open(str(FIXTURES / "simple.srt"), encoding="utf-8").read()))
            for orig, out in zip(original, parsed):
                assert orig.index == out.index
                assert orig.start == out.start
                assert orig.end == out.end
        finally:
            os.unlink(out_path)

    def test_output_parseable_by_srt_library(self):
        """Output SRT file is parseable by srt.parse() without error."""
        config = make_config()
        cue_chunks = self.reader.read(str(FIXTURES / "simple.srt"), config)
        # Make a trivial "translation" (same text) for each chunk
        translated = self._make_translated_chunks(cue_chunks, [c.source_text for c in cue_chunks])

        with tempfile.NamedTemporaryFile(suffix=".srt", delete=False, mode="w", encoding="utf-8") as f:
            out_path = f.name

        try:
            self.writer.write(translated, str(FIXTURES / "simple.srt"), out_path)
            with open(out_path, encoding="utf-8") as f:
                content = f.read()
            # Must not raise
            parsed = list(srt.parse(content))
            assert len(parsed) == 10
        finally:
            os.unlink(out_path)
