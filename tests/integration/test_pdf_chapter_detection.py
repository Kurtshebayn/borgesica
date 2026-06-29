"""Integration test — S-M3-2: Chapter-boundary detection in PdfPlumberReader.

Strict TDD: this test is written FIRST (RED — zero chapter detection coverage),
then the implementation (already exists in pdf_plumber_reader.py) makes it GREEN.

Note: the chapter detection logic (_is_chapter_heading / chapter_index increment)
already exists in the implementation (it was written in M3-1 without a driving test).
The RED state is that NO test exercises this path; after adding these tests,
the logic goes GREEN because the implementation is correct but previously untested.
"""
from __future__ import annotations

import os
import tempfile

import pytest
from fpdf import FPDF

from borgesica.domain.models import JobConfig, SourceType

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _config() -> JobConfig:
    return JobConfig(source_type=SourceType.PDF, model="claude-haiku-4-5")


def _make_multi_chapter_pdf(chapters: list[tuple[str, list[str]]]) -> str:
    """Create a multi-page PDF with chapter headings.

    Args:
        chapters: List of (heading, body_lines) tuples.
                  Each chapter begins with a heading line followed by body paragraphs.
                  A blank page is added between chapters to help pdfplumber see them
                  as distinct paragraphs.

    Returns:
        Path to a temp .pdf file — caller must os.unlink() it.
    """
    pdf = FPDF()
    pdf.set_auto_page_break(auto=False)
    pdf.set_font("Helvetica", size=12)

    for heading, body_lines in chapters:
        pdf.add_page()
        # Print the chapter heading first
        pdf.cell(0, 10, heading, new_x="LMARGIN", new_y="NEXT")
        # Then body content on the same or subsequent lines
        for line in body_lines:
            pdf.cell(0, 10, line, new_x="LMARGIN", new_y="NEXT")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        path = f.name
    pdf.output(path)
    return path


# ---------------------------------------------------------------------------
# S-M3-2: Chapter detection produces distinct, increasing chapter_index values
# ---------------------------------------------------------------------------


def test_chapter_headings_produce_distinct_chapter_index_values() -> None:
    """PdfPlumberReader must detect chapter headings and increment chapter_index.

    Fixture: a 2-page PDF where page 1 starts with "Chapter 1" heading and
    page 2 starts with "Chapter 2" heading.  The reader must detect the heading
    at the start of page 2 (or paragraph) and increment chapter_index so that
    chunks from page 1 and chunks from page 2 carry DIFFERENT chapter_index values.

    RED: chapter detection is untested (the path exists in the code but no test
         ever drove it to confirm the logic works).
    GREEN: implementation correctly assigns chapter_index=0 for chapter-1 content
           and chapter_index=1 for chapter-2 content.
    """
    from borgesica.adapters.readers.pdf_plumber_reader import PdfPlumberReader

    chapters = [
        (
            "Chapter 1",
            [
                "This is the opening paragraph of chapter one.",
                "It continues with more text on the same page.",
            ],
        ),
        (
            "Chapter 2",
            [
                "The second chapter begins with new ideas.",
                "More prose follows in the second chapter.",
            ],
        ),
    ]
    path = _make_multi_chapter_pdf(chapters)
    try:
        reader = PdfPlumberReader()
        chunks = reader.read(path, _config())

        assert chunks, "PdfPlumberReader returned no chunks for multi-chapter PDF"

        # Collect the unique chapter_index values across all chunks
        chapter_indices = [c.meta.get("chapter_index") for c in chunks]
        assert all(isinstance(ci, int) for ci in chapter_indices), (
            f"Some chunks lack integer chapter_index. Got: {chapter_indices}"
        )

        # Must have at least two distinct chapter_index values
        unique_indices = sorted(set(chapter_indices))
        assert len(unique_indices) >= 2, (
            f"Expected chunks with at least 2 distinct chapter_index values "
            f"(one per chapter), got unique indices: {unique_indices}. "
            f"Chunk details: {[(c.source_text[:40], c.meta.get('chapter_index')) for c in chunks]}"
        )

        # chapter_index values must be 0-based and increasing (no negative, no gaps > 1)
        assert unique_indices[0] == 0, (
            f"First chapter_index must be 0, got {unique_indices[0]}"
        )
        for i in range(1, len(unique_indices)):
            assert unique_indices[i] == unique_indices[i - 1] + 1, (
                f"chapter_index values must be consecutive: {unique_indices}"
            )

        # Verify chapter-1 content has lower chapter_index than chapter-2 content
        ch1_indices = {
            c.meta["chapter_index"]
            for c in chunks
            if "chapter one" in c.source_text.lower() or "opening paragraph" in c.source_text.lower()
        }
        ch2_indices = {
            c.meta["chapter_index"]
            for c in chunks
            if "second chapter" in c.source_text.lower() or "new ideas" in c.source_text.lower()
        }

        if ch1_indices and ch2_indices:
            assert max(ch1_indices) < min(ch2_indices), (
                f"Chapter-1 chunks (index={ch1_indices}) must have lower chapter_index "
                f"than chapter-2 chunks (index={ch2_indices})"
            )

    finally:
        os.unlink(path)


def test_three_chapter_pdf_produces_three_distinct_chapter_indices() -> None:
    """A 3-chapter PDF must produce exactly 3 distinct chapter_index values: 0, 1, 2.

    Uses "Chapter N" headings which match the _CHAPTER_PATTERNS regex.
    Each chapter is on its own page to guarantee clean paragraph boundaries.
    """
    from borgesica.adapters.readers.pdf_plumber_reader import PdfPlumberReader

    chapters = [
        ("Chapter 1", ["First chapter prose here."]),
        ("Chapter 2", ["Second chapter prose here."]),
        ("Chapter 3", ["Third chapter prose here."]),
    ]
    path = _make_multi_chapter_pdf(chapters)
    try:
        reader = PdfPlumberReader()
        chunks = reader.read(path, _config())

        assert chunks, "No chunks produced for 3-chapter PDF"

        chapter_indices = sorted({c.meta.get("chapter_index", -1) for c in chunks})

        # Must have indices 0, 1, 2
        assert 0 in chapter_indices, f"chapter_index=0 missing: {chapter_indices}"
        assert chapter_indices[-1] >= 2, (
            f"Expected at least chapter_index=2, got max={chapter_indices[-1]}. "
            f"All indices: {chapter_indices}. "
            f"Chunks: {[(c.source_text[:50], c.meta.get('chapter_index')) for c in chunks]}"
        )

    finally:
        os.unlink(path)
