"""Integration test — W-M3-2 fix: PDF create_job must use chunk_prose, not SrtChunker.

Strict TDD: this test was written FIRST (RED), then the api.py dispatch fix makes it GREEN.

RED: api.py dispatches PDF to SrtChunker.chunk() whose meta carries
     {"cue_batches": [...], "line_length": 42} — not prose provenance.
GREEN: api.py dispatches PDF to chunk_prose(); meta carries
       {"prose_nodes": [...]} with pdf_page/para_index locators;
       "cue_batches" is absent.
"""
from __future__ import annotations

import os
import tempfile

import pytest
from fpdf import FPDF

from borgesica.domain.models import JobConfig, SourceType
from tests.fakes import FakeTranslationProvider, InMemoryCheckpointStore

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_simple_pdf(pages: int = 4, lines_per_page: int = 3) -> str:
    """Create a simple multi-page PDF with distinct body lines.

    Returns the path to a temp .pdf file — caller must os.unlink() it.
    """
    pdf = FPDF()
    pdf.set_auto_page_break(auto=False)
    pdf.set_font("Helvetica", size=12)

    for page_num in range(pages):
        pdf.add_page()
        for line_num in range(lines_per_page):
            text = f"Page {page_num} line {line_num} content for testing."
            pdf.cell(0, 10, text, new_x="LMARGIN", new_y="NEXT")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        path = f.name
    pdf.output(path)
    return path


# ---------------------------------------------------------------------------
# W-M3-2: PDF create_job produces prose-chunked chunks (not SrtChunker cue-batches)
# ---------------------------------------------------------------------------


def test_pdf_create_job_uses_chunk_prose_not_srt_chunker() -> None:
    """api.create_job with SourceType.PDF must route through chunk_prose, not SrtChunker.

    RED (before fix):
      - api.py falls into the ``else`` branch → SrtChunker.chunk()
      - Each chunk meta carries "cue_batches" and "line_length" (SRT format)
      - "prose_nodes" is absent — provenance from PdfPlumberReader is discarded

    GREEN (after fix):
      - api.py dispatches PDF to chunk_prose()
      - Each chunk meta carries "prose_nodes" (list of source-locator dicts)
      - "cue_batches" is absent
    """
    from borgesica.adapters.readers.pdf_plumber_reader import PdfPlumberReader
    from borgesica.adapters.writers.pdf_writer import PdfWriter
    from borgesica.api import TranslatorEngine

    path = _make_simple_pdf(pages=4, lines_per_page=3)
    try:
        provider = FakeTranslationProvider()
        checkpoint = InMemoryCheckpointStore()

        engine = TranslatorEngine(
            provider=provider,
            checkpoint=checkpoint,
            readers={SourceType.PDF: PdfPlumberReader()},
            writers={SourceType.PDF: PdfWriter()},
        )

        config = JobConfig(
            source_type=SourceType.PDF,
            model="claude-haiku-4-5",
            prose_chunk_tokens=800,
        )
        job = engine.create_job(path, config)

        # Load persisted chunks
        chunks = checkpoint.load_chunks(job.id)

        assert chunks, "No chunks produced — PDF read produced no content"

        for chunk in chunks:
            # Must carry prose_nodes (chunk_prose output)
            assert "prose_nodes" in chunk.meta, (
                f"Chunk {chunk.index} meta is missing 'prose_nodes'. "
                f"Got meta keys: {list(chunk.meta.keys())}. "
                f"If 'cue_batches' is present, PDF is still going through SrtChunker."
            )
            prose_nodes = chunk.meta["prose_nodes"]
            assert isinstance(prose_nodes, list), (
                f"prose_nodes must be a list, got {type(prose_nodes)}"
            )
            assert len(prose_nodes) > 0, (
                f"Chunk {chunk.index} has empty prose_nodes list"
            )

            # Each prose_node must carry PDF-specific locator keys (pdf_page / para_index)
            for pn in prose_nodes:
                assert isinstance(pn, dict), f"prose_node entry is not a dict: {pn!r}"
                assert "pdf_page" in pn, (
                    f"prose_node missing 'pdf_page' key. Got: {pn!r}"
                )
                assert "para_index" in pn, (
                    f"prose_node missing 'para_index' key. Got: {pn!r}"
                )

            # Must NOT carry SrtChunker artifacts
            assert "cue_batches" not in chunk.meta, (
                f"Chunk {chunk.index} carries 'cue_batches' — PDF is routed through SrtChunker. "
                f"Fix: add SourceType.PDF to the chunk_prose dispatch branch in api.create_job."
            )

    finally:
        os.unlink(path)


def test_pdf_chunk_prose_nodes_alignment() -> None:
    """Each PDF chunk: source_text.split('\\n\\n') length == len(meta['prose_nodes']).

    This verifies the provenance contract: segment i in source_text maps to prose_nodes[i].
    """
    from borgesica.adapters.readers.pdf_plumber_reader import PdfPlumberReader
    from borgesica.adapters.writers.pdf_writer import PdfWriter
    from borgesica.api import TranslatorEngine

    path = _make_simple_pdf(pages=4, lines_per_page=3)
    try:
        provider = FakeTranslationProvider()
        checkpoint = InMemoryCheckpointStore()

        engine = TranslatorEngine(
            provider=provider,
            checkpoint=checkpoint,
            readers={SourceType.PDF: PdfPlumberReader()},
            writers={SourceType.PDF: PdfWriter()},
        )

        config = JobConfig(
            source_type=SourceType.PDF,
            model="claude-haiku-4-5",
            prose_chunk_tokens=800,
        )
        job = engine.create_job(path, config)

        chunks = checkpoint.load_chunks(job.id)

        for chunk in chunks:
            if "prose_nodes" not in chunk.meta:
                # Already asserted in the other test — skip alignment check here
                continue
            segments = chunk.source_text.split("\n\n")
            prose_nodes = chunk.meta["prose_nodes"]
            assert len(segments) == len(prose_nodes), (
                f"Chunk {chunk.index}: {len(segments)} source_text segments "
                f"but {len(prose_nodes)} prose_nodes entries. "
                f"Provenance alignment contract violated."
            )

    finally:
        os.unlink(path)
