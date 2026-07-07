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

    def test_cue_content_blank_lines_normalized(self):
        """Blank lines inside a cue's content must be normalized away.

        The srt parser absorbs trailing blank lines at EOF into the last
        cue's content (e.g. 'Ahhh\\n\\n'). SrtChunker joins cue texts with
        '\\n\\n', so an internal blank line makes segments outnumber cues and
        the chunk can NEVER pass segment validation — it burns every retry
        on every run (chunk 36 of jobs 0b86d4f2 / 80a1ad82, cue 913).
        """
        from borgesica.domain.chunking import SrtChunker

        chunks = self.reader.read(str(FIXTURES / "trailing_blank.srt"), self.config)

        # The last cue's raw content is 'Ahhh\n\n' — must come out clean.
        assert chunks[-1].source_text == "Ahhh"
        for c in chunks:
            assert "\n\n" not in c.source_text

        # Batch invariant: segments == cues by construction.
        batched = SrtChunker().chunk(chunks, self.config)
        for batch in batched:
            segments = batch.source_text.split("\n\n")
            assert len(segments) == len(batch.meta["cue_batches"])


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
        """When 2 lines are not possible, reflow must produce EXACTLY 3 lines.

        Construct text that provably cannot fit into 2 lines of ≤ line_length:
          - line_length = 20
          - five words of 7 chars each = "aaaaaaa bbbbbbbb ccccccc ddddddd eeeeeee"
            total = 5×7 + 4 spaces = 39 chars
          - No 2-line split works: the shortest first-line is one word (7 chars)
            leaving 4 words = 7+1+7+1+7+1+7 = 31 chars > 20; the longest
            first-line is four words = 7+1+7+1+7+1+7 = 31 > 20.
            → impossible to split into 2 lines of ≤ 20 → MUST fall back to 3.
        """
        # Five 7-char words; no 2-line split can satisfy line_length=20
        text = "aaaaaaa bbbbbbb ccccccc ddddddd eeeeeee"
        result = reflow(text, line_length=20)
        lines = result.split("\n")

        # Must be EXACTLY 3 lines (not 1 or 2, which would mean 2-line split worked
        # when it shouldn't, and not 4+ which would violate the spec)
        assert len(lines) == 3, (
            f"Expected exactly 3 lines but got {len(lines)}: {lines!r}"
        )
        # Each line must be ≤ line_length (except potentially an over-long single word
        # which S-3 handles; here all words are exactly 7 chars ≤ 20)
        for line in lines:
            assert len(line) <= 20, f"Line {line!r} exceeds line_length=20"

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

    def test_overlong_single_word_never_produces_four_lines(self):
        """A single token longer than line_length must NOT cause 4+ lines.

        Spec S-3: reflow is graceful when a single word exceeds line_length.
        The over-long word is allowed to overflow on its own line (cannot be
        split without hyphenation), but the total output must still be ≤ 3 lines.
        A warning is logged; no exception is raised.
        """
        import logging

        # A single word of 50 chars with line_length=20: textwrap produces
        # ['supercalifragilistic...'] (one over-long line) — must stay ≤ 3 lines.
        overlong_word = "a" * 50  # 50 chars, line_length=20 → cannot split
        text = f"{overlong_word} short end"

        # Should not raise even with an overlong token
        result = reflow(text, line_length=20)
        lines = result.split("\n")

        assert len(lines) <= 3, (
            f"reflow produced {len(lines)} lines (> 3) with over-long token: {lines!r}"
        )

    def test_overlong_single_word_logs_warning(self):
        """reflow logs a WARNING when a single token exceeds line_length."""
        import logging

        overlong_word = "b" * 50
        text = f"{overlong_word} short"

        with self._assert_logs_warning():
            reflow(text, line_length=20)

    @staticmethod
    def _assert_logs_warning():
        """Context manager that asserts at least one WARNING was emitted."""
        import logging
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            with __import__("unittest.mock", fromlist=["patch"]).patch(
                "borgesica.adapters.writers.srt_writer.logger"
            ) as mock_logger:
                yield mock_logger
                assert mock_logger.warning.called, (
                    "Expected a logger.warning() call for over-long token"
                )

        return _ctx()


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

    def test_cue_count_mismatch_falls_back_to_per_cue_source_text(self):
        """On cue-count mismatch, each cue falls back to ITS OWN source text.

        Regression for job 0b86d4f2: when translated_text splits into fewer
        parts than cue_batches, the writer must NOT duplicate the whole batch
        text into every cue (walls of identical text, line_length violations).
        It must degrade per cue using meta["cue_batches"][i]["text"].
        """
        from borgesica.domain.chunking import SrtChunker

        config = make_config()
        cue_chunks = self.reader.read(str(FIXTURES / "simple.srt"), config)
        chunker = SrtChunker()
        batched = chunker.chunk(cue_chunks, config.__class__(
            source_type=SourceType.SRT,
            model="claude-3-5-haiku-20241022",
            chunk_size=5,
            line_length=42,
        ))

        batch = batched[0]
        cue_batches = batch.meta["cue_batches"]
        assert len(cue_batches) >= 3, "fixture must yield a multi-cue batch"

        # Mismatched translation: one segment fewer than the cue count
        bad_translation = "\n\n".join(
            f"Traducción {i}" for i in range(len(cue_batches) - 1)
        )
        translated = self._make_translated_chunks([batch], [bad_translation])

        with tempfile.NamedTemporaryFile(suffix=".srt", delete=False, mode="w", encoding="utf-8") as f:
            out_path = f.name

        try:
            self.writer.write(translated, str(FIXTURES / "simple.srt"), out_path)
            with open(out_path, encoding="utf-8") as f:
                parsed = list(srt.parse(f.read()))

            assert len(parsed) == len(cue_batches)
            full_batch_norm = " ".join(bad_translation.split())
            for cue_meta, sub in zip(cue_batches, parsed):
                content_norm = " ".join(sub.content.split())
                source_norm = " ".join(cue_meta["text"].split())
                # Each cue carries its OWN source text…
                assert content_norm == source_norm, (
                    f"cue {cue_meta['cue_index']}: expected per-cue source "
                    f"fallback {source_norm!r}, got {content_norm!r}"
                )
                # …and never the whole batch duplicated.
                assert content_norm != full_batch_norm
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

    def test_non_monotonic_timestamps_preserve_original_cue_order(self):
        """A cue whose timestamp jumps BACKWARDS (e.g. a post-credits/bonus
        scene appended with a restarted timestamp track — real case: Backrooms
        (2026) cue 897) must NOT get globally reshuffled.

        Root cause: srt.compose() defaults to reindex=True, which re-SORTS
        BY START TIME and renumbers 1..N, silently discarding the writer's
        own `subtitles.sort(key=lambda s: s.index)` line. On a real 913-cue
        file this corrupted 889 cues (97%) downstream of the single
        non-monotonic timestamp. The writer must pass reindex=False — it
        already establishes the correct order itself.
        """
        def make_batch(cue_index: int, start: str, end: str, text: str, translated: str) -> Chunk:
            return Chunk(
                index=cue_index - 1,
                source_text=text,
                translated_text=translated,
                status=ChunkStatus.DONE,
                meta={
                    "cue_batches": [
                        {"cue_index": cue_index, "start": start, "end": end, "text": text}
                    ],
                    "line_length": 42,
                },
            )

        chunks = [
            make_batch(1, "00:00:01,000", "00:00:02,000", "First line.", "Primera linea."),
            make_batch(2, "00:00:02,000", "00:00:03,000", "Second line.", "Segunda linea."),
            # Cue 3's timestamp jumps BACK before cues 1 and 2 (bonus-scene style).
            make_batch(3, "00:00:00,100", "00:00:00,900", "Bonus scene line.", "Linea de escena bonus."),
        ]

        with tempfile.NamedTemporaryFile(suffix=".srt", delete=False, mode="w", encoding="utf-8") as f:
            out_path = f.name

        try:
            self.writer.write(chunks, str(FIXTURES / "simple.srt"), out_path)
            with open(out_path, encoding="utf-8") as f:
                parsed = sorted(srt.parse(f.read()), key=lambda s: s.index)

            assert len(parsed) == 3
            assert [s.index for s in parsed] == [1, 2, 3]
            assert parsed[0].content == "Primera linea."
            assert parsed[1].content == "Segunda linea."
            assert parsed[2].content == "Linea de escena bonus."
            assert parsed[2].start == timedelta(seconds=0, milliseconds=100)
        finally:
            os.unlink(out_path)
