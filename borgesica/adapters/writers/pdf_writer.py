"""PDF writer adapter stub (M3).

PDF output is not supported in M3.  Attempting to write a translated PDF
raises ``NotImplementedError`` with a clear message directing the user to
export as plain text or use an EPUB source instead.

A full PDF writer (recreating typesetting, fonts, layout) is a significant
undertaking beyond M3 scope and is deferred to a future milestone.
"""
from __future__ import annotations

from borgesica.domain.models import Chunk


class PdfWriter:
    """DocumentWriter stub for .pdf output.

    Implements the ``DocumentWriter`` Protocol structurally (duck-typing).
    Always raises ``NotImplementedError`` — PDF output is not supported in M3.
    """

    def write(self, chunks: list[Chunk], src_path: str, out_path: str) -> None:
        """Raise NotImplementedError — PDF output is not supported in M3.

        Args:
            chunks:   Translated chunks (unused).
            src_path: Source PDF path (unused).
            out_path: Intended output path (unused).

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError(
            "PDF output not supported in M3 — export as text or use source EPUB"
        )
