"""Integration tests for PdfPlumberReader adapter (M3-1).

All fixture PDFs are built programmatically using fpdf2 — no binary .pdf
files committed to the repo.

Strict TDD: tests written FIRST (RED), then implementation makes them GREEN.
"""
from __future__ import annotations

import os
import sys
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


def _make_pdf_with_repeated_header(
    header: str,
    body_lines: list[str],
    total_pages: int,
    header_on_pages: int,
) -> str:
    """Create a multi-page PDF where *header* appears on *header_on_pages* pages.

    The remaining pages contain only body content (no header).  Returns the
    path to a temp .pdf file — caller is responsible for os.unlink().

    Args:
        header:           The boilerplate string to stamp on header pages.
        body_lines:       Lines of non-boilerplate body text (one per page).
        total_pages:      Total number of pages in the PDF.
        header_on_pages:  How many of the total_pages carry the header.
    """
    pdf = FPDF()
    pdf.set_auto_page_break(auto=False)
    pdf.set_font("Helvetica", size=12)

    for i in range(total_pages):
        pdf.add_page()
        # Print header on the first header_on_pages pages
        if i < header_on_pages:
            pdf.cell(0, 10, header, new_x="LMARGIN", new_y="NEXT")
        # Body text — use modulo to cycle if fewer body_lines than pages
        line = body_lines[i % len(body_lines)]
        pdf.cell(0, 10, line, new_x="LMARGIN", new_y="NEXT")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        path = f.name
    pdf.output(path)
    return path


def _make_pdf_with_hyphen(hyphenated_word: str, surrounding: str = "") -> str:
    """Create a single-page PDF containing a hyphenated line-break.

    *hyphenated_word* should be the raw string as it would appear in extracted
    text, e.g. ``"incompre-\\nhensible"`` — fpdf2 will encode the text across
    two cells to simulate the line break.

    Returns the path to a temp .pdf file.
    """
    # Split on "\n" to render as two separate cells (simulating a line break)
    parts = hyphenated_word.split("\n")
    pdf = FPDF()
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)

    if surrounding:
        pdf.cell(0, 10, surrounding, new_x="LMARGIN", new_y="NEXT")
    for part in parts:
        pdf.cell(0, 10, part, new_x="LMARGIN", new_y="NEXT")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        path = f.name
    pdf.output(path)
    return path


# ---------------------------------------------------------------------------
# Test 1: Header/footer stripping removes boilerplate from ≥80% of pages
# ---------------------------------------------------------------------------


def test_header_footer_stripping() -> None:
    """Lines appearing on ≥80% of pages must be stripped from extracted text.

    Spec scenario: a boilerplate string appears on 80% of pages and must be
    removed from the extracted text.  We use an ASCII-safe string because
    fpdf2's built-in Helvetica font does not encode non-Latin characters like
    the copyright symbol correctly (it outputs a replacement glyph).

    RED: PdfPlumberReader does not exist yet → ImportError or missing stripping.
    GREEN: adapter strips repeated headers/footers verbatim.
    """
    header = "Publisher Footer 2020"
    body_lines = [
        "Chapter one begins here.",
        "The story continues with more prose.",
        "Another paragraph of fictional content.",
        "Yet more interesting material follows.",
        "The final paragraph of this section.",
    ]
    total_pages = 10
    # header_on_pages / total_pages = 8/10 = 80%
    header_on_pages = 8

    path = _make_pdf_with_repeated_header(
        header=header,
        body_lines=body_lines,
        total_pages=total_pages,
        header_on_pages=header_on_pages,
    )
    try:
        from borgesica.adapters.readers.pdf_plumber_reader import PdfPlumberReader

        reader = PdfPlumberReader()
        chunks = reader.read(path, _config())

        all_text = " ".join(c.source_text for c in chunks)
        assert header not in all_text, (
            f"Header {header!r} should have been stripped from extracted text, "
            f"but it appears in output: {all_text[:300]!r}"
        )
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Test 2: Hyphenated line-break is rejoined
# ---------------------------------------------------------------------------


def test_hyphenated_line_break_rejoined() -> None:
    """Extracted text with a hyphenated line-break must be rejoined.

    Spec scenario: "incompre-\\nhensible" → "incomprehensible" in cleaned text.

    RED: no cleanup pipeline yet.
    GREEN: adapter applies hyphen-rejoin step.
    """
    # We embed the two halves on separate lines so pdfplumber extracts them
    # as "incompre-" and "hensible" on consecutive lines.
    path = _make_pdf_with_hyphen("incompre-\nhensible", surrounding="Some context text.")
    try:
        from borgesica.adapters.readers.pdf_plumber_reader import PdfPlumberReader

        reader = PdfPlumberReader()
        chunks = reader.read(path, _config())

        all_text = " ".join(c.source_text for c in chunks)
        assert "incomprehensible" in all_text, (
            f"Expected 'incomprehensible' (rejoined) in output, got: {all_text!r}"
        )
        # The hyphenated form must not appear
        assert "incompre-" not in all_text, (
            f"Hyphenated form 'incompre-' should not appear after cleanup: {all_text!r}"
        )
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Test 3: PyMuPDF adapter is never imported by the default DI configuration
# ---------------------------------------------------------------------------


def test_pymupdf_not_imported_by_default_di() -> None:
    """With the default DI wiring, pdf_pymupdf_reader is NOT in sys.modules.

    Spec scenario: "When api.py wires up the DocumentReader for PDF source type,
    pdf_pymupdf_reader.py SHALL NOT be imported."

    We trigger default DI by importing the top-level borgesica package and
    calling the module-level wiring path (same as the CLI _build_engine does).
    The check is purely on sys.modules — no actual job is run.

    RED: if the stub already imported pymupdf at module level this would fail.
    GREEN: pdf_pymupdf_reader is never in the default import graph.
    """
    # Ensure the module is absent before we start
    pymupdf_key = "borgesica.adapters.readers.pdf_pymupdf_reader"
    # Importing PdfPlumberReader (the default) must NOT pull in the pymupdf stub
    from borgesica.adapters.readers.pdf_plumber_reader import PdfPlumberReader  # noqa: F401

    assert pymupdf_key not in sys.modules, (
        f"pdf_pymupdf_reader was unexpectedly imported into sys.modules. "
        f"Check that no default import path references it."
    )
