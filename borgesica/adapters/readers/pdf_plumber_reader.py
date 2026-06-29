"""PDF reader adapter (M3-1) — pdfplumber (MIT licence) backend.

Extracts clean text from digital (non-scanned) PDF files using ``pdfplumber``.
Applies a post-extraction cleanup pipeline:

  1. Strip repeated headers/footers — lines appearing verbatim on ≥ 80% of
     pages are considered boilerplate and removed from every page's text.
  2. Rejoin hyphenated line-breaks — ``word-\\n`` is collapsed to ``word``.
  3. Detect chapter boundaries from common heading patterns (``Chapter N``,
     ``CHAPTER``, numbered headings like ``1.``, ``2.`` etc.) so that
     downstream ``chunk_prose`` can enforce cross-chapter isolation.

Each output ``Chunk`` carries:
  - ``source_text``: cleaned paragraph text.
  - ``meta``: flat provenance dict compatible with the prose pipeline —
      ``{"pdf_page": int, "chapter_index": int, "para_index": int}``

    ``chunk_prose`` reads ``meta["chapter_index"]`` for grouping and builds
    ``meta["prose_nodes"]`` in its output using the remaining locator keys
    (``pdf_page``, ``para_index``).  This mirrors the EPUB pattern:
    EpubReader emits flat per-node meta; EpubWriter reads ``prose_nodes``
    produced by ``chunk_prose``.

    NOTE (M3-FIX / W-M3-2): earlier versions of this adapter emitted a nested
    ``prose_nodes`` list directly in each chunk's meta.  That conflicted with
    ``chunk_prose``'s own ``prose_nodes`` output key and prevented correct
    dispatch.  The reader now emits flat meta only; ``chunk_prose`` owns
    ``prose_nodes`` construction.

Dependency rule: ``pdfplumber`` is an ADAPTER-ONLY import.  It MUST NOT be
imported anywhere under ``borgesica/domain/``.

Scanned PDFs (no selectable text) are out of scope for M3 — no OCR is
performed; an empty ``list[Chunk]`` is returned for pages with no extractable
text.
"""
from __future__ import annotations

import logging
import re
from collections import Counter

import pdfplumber

from borgesica.domain.models import Chunk, ChunkStatus, JobConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Lines that appear on this fraction of pages (or more) are treated as
# repeated headers/footers and stripped.
_HEADER_FOOTER_THRESHOLD = 0.80

# Regex patterns that indicate a chapter boundary heading.
_CHAPTER_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^chapter\s+\w+", re.IGNORECASE),
    re.compile(r"^CHAPTER\s+", re.IGNORECASE),
    re.compile(r"^\d+\.\s+\w"),   # "1. Introduction"
    re.compile(r"^part\s+\w+", re.IGNORECASE),
    re.compile(r"^epilogue$", re.IGNORECASE),
    re.compile(r"^prologue$", re.IGNORECASE),
    re.compile(r"^introduction$", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_page_lines(path: str) -> list[list[str]]:
    """Open the PDF and return a list of raw line-lists, one list per page.

    Uses ``pdfplumber`` with ``layout=False`` (the default ``extract_text``
    mode) to obtain clean newline-separated text.  ``layout=True`` inserts
    large amounts of whitespace that makes line-frequency counting unreliable.

    Returns:
        A list where each element is a list of non-empty stripped lines for
        the corresponding page.  Empty pages contribute an empty list.
    """
    page_lines: list[list[str]] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            # NOTE: layout=True is intentionally NOT used here. The spec originally
            # specified layout=True but the implementation uses the default extraction
            # because layout=True inserts large amounts of positional whitespace padding
            # that makes verbatim line-frequency header/footer detection unreliable.
            # Default extraction produces clean newline-separated text in reading order.
            # See W-M3-1 in spec.md and design.md (Decision 4) for the full rationale.
            raw = page.extract_text()
            if raw:
                lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
            else:
                lines = []
            page_lines.append(lines)
    return page_lines


def _detect_boilerplate(page_lines: list[list[str]]) -> frozenset[str]:
    """Return the set of lines that appear on ≥ 80% of pages (boilerplate).

    Boilerplate detection is only meaningful for multi-page documents.
    For documents with fewer than 3 pages, no lines are considered boilerplate
    because a line appearing on 1 of 1 (or 2 of 2) pages is not a reliable
    signal — it may simply be unique content.

    Args:
        page_lines: Per-page line lists as returned by ``_extract_page_lines``.

    Returns:
        A frozenset of line strings to be stripped from every page.
    """
    total_pages = len(page_lines)
    # Require at least 3 pages before stripping anything as boilerplate.
    if total_pages < 3:
        return frozenset()

    threshold = _HEADER_FOOTER_THRESHOLD * total_pages
    freq: Counter[str] = Counter()
    for lines in page_lines:
        # Count each unique line once per page (avoid over-counting when the
        # same line appears multiple times on one page).
        for line in set(lines):
            freq[line] += 1

    return frozenset(line for line, count in freq.items() if count >= threshold)


def _rejoin_hyphens(text: str) -> str:
    """Rejoin hyphenated line-breaks.

    Transforms ``word-\\nhensible`` → ``wordensible``  (i.e. ``incompre-\\n``
    + ``hensible`` → ``incomprehensible``).

    The regex matches a word suffix ending in ``-`` immediately before a
    newline, and the continuation word on the next line.
    """
    return re.sub(r"(\w)-\n(\w)", r"\1\2", text)


def _is_chapter_heading(line: str) -> bool:
    """Return True if *line* looks like a chapter-boundary heading."""
    stripped = line.strip()
    return any(pattern.match(stripped) for pattern in _CHAPTER_PATTERNS)


def _split_into_paragraphs(text: str) -> list[str]:
    """Split page text into paragraph-sized pieces.

    Treats blank lines and isolated heading lines as paragraph boundaries.
    Returns a list of non-empty paragraph strings.
    """
    # Split on one or more blank lines
    raw_paras = re.split(r"\n{2,}", text)
    result: list[str] = []
    for para in raw_paras:
        para = para.strip()
        if para:
            result.append(para)
    return result


# ---------------------------------------------------------------------------
# Public adapter class
# ---------------------------------------------------------------------------


class PdfPlumberReader:
    """DocumentReader implementation for .pdf files using pdfplumber (MIT).

    Implements ``borgesica.domain.ports.DocumentReader`` structurally (duck-
    typing; the Protocol is not imported to keep the dependency rule clean).
    """

    def read(self, path: str, config: JobConfig) -> list[Chunk]:  # noqa: ARG002
        """Parse a PDF file into a flat list of Chunk objects.

        Steps:
          1. Extract all page text via pdfplumber.
          2. Detect and strip repeated headers/footers (≥ 80% of pages).
          3. Rejoin hyphenated line-breaks within each page.
          4. Split each page's cleaned text into paragraphs.
          5. Detect chapter boundaries from heading patterns.
          6. Emit one Chunk per paragraph with provenance metadata.

        ``config`` is accepted for Protocol compatibility but currently unused
        by the reader (font/encoding decisions belong to the writer).

        Returns:
            An ordered list of ``Chunk`` objects (may be empty for scanned
            PDFs that yield no selectable text).
        """
        # Step 1: extract raw lines per page
        page_lines = _extract_page_lines(path)

        # Step 2: detect boilerplate (headers/footers)
        boilerplate = _detect_boilerplate(page_lines)
        if boilerplate:
            logger.debug(
                "PDF reader: stripping %d boilerplate line(s): %s",
                len(boilerplate),
                boilerplate,
            )

        chunks: list[Chunk] = []
        chunk_index = 0
        chapter_index = 0

        for page_num, lines in enumerate(page_lines):
            # Step 2 (continued): remove boilerplate lines
            clean_lines = [ln for ln in lines if ln not in boilerplate]

            if not clean_lines:
                continue

            # Step 3: rejoin hyphens across lines
            page_text = "\n".join(clean_lines)
            page_text = _rejoin_hyphens(page_text)

            # Step 4: split into paragraphs
            paragraphs = _split_into_paragraphs(page_text)

            for para_idx, para in enumerate(paragraphs):
                # Step 5: chapter boundary detection
                first_line = para.split("\n")[0].strip()
                if _is_chapter_heading(first_line) and (chunks or para_idx > 0):
                    chapter_index += 1

                # Step 6: emit chunk — flat meta for chunk_prose compatibility.
                # chunk_prose groups by meta["chapter_index"] and builds its own
                # meta["prose_nodes"] from the remaining locator keys (pdf_page,
                # para_index).  This mirrors the EpubReader flat-meta pattern.
                chunks.append(
                    Chunk(
                        index=chunk_index,
                        source_text=para,
                        status=ChunkStatus.PENDING,
                        meta={
                            "pdf_page": page_num,
                            "chapter_index": chapter_index,
                            "para_index": para_idx,
                        },
                    )
                )
                chunk_index += 1

        return chunks
