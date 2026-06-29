"""PyMuPDF4LLM PDF reader adapter — AGPL-3.0 opt-in stub.

WARNING — LICENCE INCOMPATIBILITY
==================================
PyMuPDF (and its wrapper pymupdf4llm) is distributed under the GNU Affero
General Public License v3 (AGPL-3.0).  This is a COPYLEFT licence that
requires ALL code that links to it — including the entire application — to be
released under AGPL-3.0 as well.  This is INCOMPATIBLE with closed-source or
proprietary use.

DO NOT import this module from api.py, __main__.py, or any default wiring
path.  It may only be instantiated by an operator who:
  a) has obtained a commercial PyMuPDF licence, OR
  b) is distributing their entire application under AGPL-3.0.

The Borgésica default DI uses PdfPlumberReader (MIT) instead.  See
design.md Decision 4 and pyproject.toml [project.optional-dependencies]
pdf-fast.

HOW TO OPT IN
=============
Install the optional extra:
    pip install borgesica[pdf-fast]

Then wire manually in your own entry point (NOT in api.py):
    from borgesica.adapters.readers.pdf_pymupdf_reader import PdfMuPdfReader
    engine = TranslatorEngine(..., readers={SourceType.PDF: PdfMuPdfReader()})

M3 STATUS
=========
This file is a COMMENTED STUB only.  The actual implementation (wrapping
pymupdf4llm.to_markdown + splitting on headings) is deferred to a future
milestone when AGPL acceptability is confirmed.
"""

# from __future__ import annotations
#
# import pymupdf4llm  # AGPL-3.0 — never imported in default paths
#
# from borgesica.domain.models import Chunk, ChunkStatus, JobConfig
#
#
# class PdfMuPdfReader:
#     """DocumentReader using PyMuPDF4LLM (AGPL-3.0, opt-in only).
#
#     DO NOT instantiate from api.py or any default DI wiring.
#     See module docstring for licence requirements.
#     """
#
#     def read(self, path: str, config: JobConfig) -> list[Chunk]:
#         raise NotImplementedError(
#             "PdfMuPdfReader is an opt-in stub. "
#             "Install borgesica[pdf-fast] and wire manually. "
#             "See borgesica/adapters/readers/pdf_pymupdf_reader.py."
#         )
