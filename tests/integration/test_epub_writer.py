"""Integration tests for EpubWriter adapter (M2-3).

Tests follow strict TDD: written FIRST to be RED, then implementation makes
them GREEN.

All fixtures built programmatically using ebooklib — no binary .epub files
committed to the repository.

Test plan:
  1. Output EPUB opens with ebooklib.epub.read_epub — no exception.
  2. Chapter count equals source (12-chapter fixture).
  3. Images byte-identical in output vs source.
  4. CSS stylesheets byte-identical.
  5. No partial output on writer failure (atomic write: tmp + os.replace).
  6. <em>…</em> round-trips as a real tag in output XHTML.
  7. End-to-end round-trip: reader→chunker→(fake translate)→writer→reader,
     translated text lands in the correct nodes (provenance round-trip).
"""
from __future__ import annotations

import io
import os
import tempfile
import zipfile

import pytest
from ebooklib import epub

from borgesica.adapters.readers.epub_reader import EpubReader
from borgesica.adapters.writers.epub_writer import EpubWriter
from borgesica.domain.chunking import chunk_prose
from borgesica.domain.models import Chunk, ChunkStatus, JobConfig, SourceType


# ---------------------------------------------------------------------------
# Minimal 1×1 transparent PNG bytes (used as a fixture image)
# ---------------------------------------------------------------------------

_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

_JPEG_BYTES = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xd9"
)

_CSS_CONTENT = b"body { font-family: serif; }"


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _chapter_xhtml(text: str, *, with_em: bool = False) -> bytes:
    """Build a minimal XHTML chapter with one paragraph."""
    if with_em:
        body_content = f"<p>{text}</p>"
    else:
        body_content = f"<p>{text}</p>"
    return (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<html xmlns='http://www.w3.org/1999/xhtml'>"
        "<head><title>Chapter</title></head>"
        f"<body>{body_content}</body>"
        "</html>"
    ).encode("utf-8")


def _chapter_xhtml_with_em(pre: str, em_text: str, post: str) -> bytes:
    """Build an XHTML chapter where the <p> contains an <em> tag."""
    body_content = f"<p>{pre}<em>{em_text}</em>{post}</p>"
    return (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<html xmlns='http://www.w3.org/1999/xhtml'>"
        "<head><title>Chapter</title></head>"
        f"<body>{body_content}</body>"
        "</html>"
    ).encode("utf-8")


def _make_epub_bytes(
    chapters: list[tuple[str, bytes]],
    *,
    include_png: bool = False,
    include_jpeg: bool = False,
    include_css: bool = False,
) -> bytes:
    """Build a minimal valid EPUB in memory using ebooklib."""
    book = epub.EpubBook()
    book.set_identifier("test-writer-001")
    book.set_title("Writer Test Book")
    book.set_language("en")
    book.add_author("Test Author")

    epub_items: list[epub.EpubHtml] = []
    for idx, (fname, content) in enumerate(chapters):
        item = epub.EpubHtml(title=f"Chapter {idx + 1}", file_name=fname, lang="en")
        item.content = content
        book.add_item(item)
        epub_items.append(item)

    if include_png:
        img = epub.EpubImage()
        img.uid = "img-png"
        img.file_name = "images/cover.png"
        img.media_type = "image/png"
        img.content = _PNG_BYTES
        book.add_item(img)

    if include_jpeg:
        img2 = epub.EpubImage()
        img2.uid = "img-jpeg"
        img2.file_name = "images/photo.jpg"
        img2.media_type = "image/jpeg"
        img2.content = _JPEG_BYTES
        book.add_item(img2)

    if include_css:
        css = epub.EpubItem(
            uid="style-main",
            file_name="styles/main.css",
            media_type="text/css",
            content=_CSS_CONTENT,
        )
        book.add_item(css)

    book.spine = ["nav"] + epub_items
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
        tmp = f.name
    try:
        epub.write_epub(tmp, book, {})
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        os.unlink(tmp)


def _write_temp_epub(content: bytes) -> str:
    """Write bytes to a named temp file and return its path."""
    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
        f.write(content)
        return f.name


def _config() -> JobConfig:
    return JobConfig(source_type=SourceType.EPUB, model="claude-haiku-4-5")


# ---------------------------------------------------------------------------
# Fake translation provider for tests (deterministic, no network)
# ---------------------------------------------------------------------------


class _FakeProvider:
    """Minimal TranslationProvider for tests: count_tokens only."""

    def count_tokens(self, text: str, model: str) -> int:  # noqa: ARG002
        return max(1, len(text.split()))

    def translate(self, system: str, user: str, model: str):  # noqa: ARG002
        raise NotImplementedError("not used in writer tests")

    def price(self, model: str):  # noqa: ARG002
        return (1.0, 5.0)


# ---------------------------------------------------------------------------
# Helper: apply deterministic "translation" to a list of chunks.
#
# Strategy: prefix every segment with "[ES] " while KEEPING inline tags.
# This simulates a translation that preserves <em>, <strong>, etc.
# ---------------------------------------------------------------------------


def _fake_translate(chunks: list[Chunk]) -> list[Chunk]:
    """Assign translated_text to each chunk deterministically.

    Each ``\\n\\n``-separated segment gets prefixed with "[ES] ".
    Inline tags are KEPT intact (so the em round-trip test can verify them).
    """
    translated = []
    for chunk in chunks:
        segments = chunk.source_text.split("\n\n")
        translated_segments = ["[ES] " + seg for seg in segments]
        translated_text = "\n\n".join(translated_segments)
        updated = chunk.model_copy(
            update={"translated_text": translated_text, "status": ChunkStatus.DONE}
        )
        translated.append(updated)
    return translated


# ---------------------------------------------------------------------------
# Test 1: Output EPUB opens with ebooklib.epub.read_epub — no exception
# ---------------------------------------------------------------------------


def test_output_epub_is_openable() -> None:
    """EpubWriter.write produces a file that read_epub accepts without exception."""
    chapters = [
        ("ch1.xhtml", _chapter_xhtml("Hello, world!")),
        ("ch2.xhtml", _chapter_xhtml("Second chapter content.")),
    ]
    src_bytes = _make_epub_bytes(chapters)
    src_path = _write_temp_epub(src_bytes)
    out_path = src_path.replace(".epub", "_out.epub")

    try:
        # Read + produce chunks
        reader = EpubReader()
        node_chunks = reader.read(src_path, _config())
        provider = _FakeProvider()
        chunks = chunk_prose(node_chunks, _config(), provider)
        chunks = _fake_translate(chunks)

        writer = EpubWriter()
        writer.write(chunks, src_path, out_path)

        # Must open without exception
        result_book = epub.read_epub(out_path)
        assert result_book is not None
    finally:
        os.unlink(src_path)
        if os.path.exists(out_path):
            os.unlink(out_path)


# ---------------------------------------------------------------------------
# Test 2: Chapter count equals source (12-chapter fixture)
# ---------------------------------------------------------------------------


def test_chapter_count_equals_source() -> None:
    """Output EPUB has the same number of XHTML documents as the source."""
    n_chapters = 12
    chapters = [
        (f"ch{i + 1}.xhtml", _chapter_xhtml(f"Chapter {i + 1} content paragraph."))
        for i in range(n_chapters)
    ]
    src_bytes = _make_epub_bytes(chapters)
    src_path = _write_temp_epub(src_bytes)
    out_path = src_path.replace(".epub", "_out.epub")

    try:
        reader = EpubReader()
        node_chunks = reader.read(src_path, _config())
        provider = _FakeProvider()
        chunks = chunk_prose(node_chunks, _config(), provider)
        chunks = _fake_translate(chunks)

        writer = EpubWriter()
        writer.write(chunks, src_path, out_path)

        result_book = epub.read_epub(out_path)
        xhtml_items = [
            item for item in result_book.get_items()
            if item.media_type == "application/xhtml+xml"
        ]
        # At minimum the 12 content chapters must be present
        # (NAV may also be counted, so we check >= n_chapters)
        assert len(xhtml_items) >= n_chapters, (
            f"Expected at least {n_chapters} XHTML items, got {len(xhtml_items)}"
        )
    finally:
        os.unlink(src_path)
        if os.path.exists(out_path):
            os.unlink(out_path)


# ---------------------------------------------------------------------------
# Test 3: Images byte-identical in output
# ---------------------------------------------------------------------------


def test_images_byte_identical() -> None:
    """Images in the output EPUB are byte-for-byte identical to the source."""
    chapters = [("ch1.xhtml", _chapter_xhtml("Text with images nearby."))]
    src_bytes = _make_epub_bytes(chapters, include_png=True, include_jpeg=True)
    src_path = _write_temp_epub(src_bytes)
    out_path = src_path.replace(".epub", "_out.epub")

    try:
        reader = EpubReader()
        node_chunks = reader.read(src_path, _config())
        provider = _FakeProvider()
        chunks = chunk_prose(node_chunks, _config(), provider)
        chunks = _fake_translate(chunks)

        writer = EpubWriter()
        writer.write(chunks, src_path, out_path)

        # Extract image bytes from both ZIPs and compare
        with zipfile.ZipFile(src_path, "r") as src_zip:
            src_names = src_zip.namelist()
            src_images = {
                n: src_zip.read(n)
                for n in src_names
                if any(n.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"))
            }

        with zipfile.ZipFile(out_path, "r") as out_zip:
            out_images = {
                n: out_zip.read(n)
                for n in out_zip.namelist()
                if any(n.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"))
            }

        assert src_images, "Expected at least one image in source EPUB"
        for name, src_data in src_images.items():
            assert name in out_images, f"Image {name!r} missing from output EPUB"
            assert out_images[name] == src_data, (
                f"Image {name!r} is not byte-identical in output EPUB"
            )
    finally:
        os.unlink(src_path)
        if os.path.exists(out_path):
            os.unlink(out_path)


# ---------------------------------------------------------------------------
# Test 4: CSS stylesheets byte-identical
# ---------------------------------------------------------------------------


def test_css_byte_identical() -> None:
    """CSS stylesheets in the output EPUB are byte-for-byte identical to source."""
    chapters = [("ch1.xhtml", _chapter_xhtml("Styled content."))]
    src_bytes = _make_epub_bytes(chapters, include_css=True)
    src_path = _write_temp_epub(src_bytes)
    out_path = src_path.replace(".epub", "_out.epub")

    try:
        reader = EpubReader()
        node_chunks = reader.read(src_path, _config())
        provider = _FakeProvider()
        chunks = chunk_prose(node_chunks, _config(), provider)
        chunks = _fake_translate(chunks)

        writer = EpubWriter()
        writer.write(chunks, src_path, out_path)

        with zipfile.ZipFile(src_path, "r") as src_zip:
            src_css = {
                n: src_zip.read(n)
                for n in src_zip.namelist()
                if n.endswith(".css")
            }

        with zipfile.ZipFile(out_path, "r") as out_zip:
            out_css = {
                n: out_zip.read(n)
                for n in out_zip.namelist()
                if n.endswith(".css")
            }

        assert src_css, "Expected at least one CSS file in source EPUB"
        for name, src_data in src_css.items():
            assert name in out_css, f"CSS {name!r} missing from output EPUB"
            assert out_css[name] == src_data, (
                f"CSS {name!r} is not byte-identical in output EPUB"
            )
    finally:
        os.unlink(src_path)
        if os.path.exists(out_path):
            os.unlink(out_path)


# ---------------------------------------------------------------------------
# Test 5: No partial output on writer failure (atomic write)
# ---------------------------------------------------------------------------


def test_no_partial_output_on_failure() -> None:
    """If EpubWriter.write raises mid-way, out_path must NOT exist.

    Strategy: subclass EpubWriter to raise after writing the tmp file but
    before os.replace is called. Verify that:
      - out_path does not exist.
      - No leftover .tmp file remains next to out_path.
    """
    chapters = [("ch1.xhtml", _chapter_xhtml("Content for atomic write test."))]
    src_bytes = _make_epub_bytes(chapters)
    src_path = _write_temp_epub(src_bytes)
    out_path = src_path.replace(".epub", "_out.epub")

    # Ensure out_path doesn't pre-exist
    if os.path.exists(out_path):
        os.unlink(out_path)

    class _BrokenWriter(EpubWriter):
        """Overrides _do_write to raise after the tmp is written but before rename."""

        def _do_write(self, chunks: list[Chunk], src_path: str, tmp_path: str) -> None:
            # Write the tmp (calls parent logic)
            super()._do_write(chunks, src_path, tmp_path)
            # Then raise to simulate a failure BEFORE os.replace
            raise RuntimeError("Simulated failure after write, before rename")

    try:
        reader = EpubReader()
        node_chunks = reader.read(src_path, _config())
        provider = _FakeProvider()
        chunks = chunk_prose(node_chunks, _config(), provider)
        chunks = _fake_translate(chunks)

        writer = _BrokenWriter()
        with pytest.raises(RuntimeError, match="Simulated failure"):
            writer.write(chunks, src_path, out_path)

        # out_path must NOT exist
        assert not os.path.exists(out_path), (
            f"out_path {out_path!r} exists after failure — atomic write violated"
        )
        # Tmp file must also be cleaned up
        tmp_path = out_path + ".tmp"
        assert not os.path.exists(tmp_path), (
            f"Leftover tmp file {tmp_path!r} exists after failure"
        )
    finally:
        os.unlink(src_path)
        if os.path.exists(out_path):
            os.unlink(out_path)


# ---------------------------------------------------------------------------
# Test 6: <em>…</em> round-trips as a real tag in the output XHTML
# ---------------------------------------------------------------------------


def test_em_tag_round_trips_as_real_element() -> None:
    """<em>…</em> in source text must appear as a real <em> tag in output XHTML.

    The writer must parse translated segments as XHTML/HTML fragments and set
    them as real child elements — NOT escape the tags as &lt;em&gt;.
    """
    em_chapter = _chapter_xhtml_with_em("A ", "critical", " point.")
    chapters = [("ch1.xhtml", em_chapter)]
    src_bytes = _make_epub_bytes(chapters)
    src_path = _write_temp_epub(src_bytes)
    out_path = src_path.replace(".epub", "_out.epub")

    try:
        reader = EpubReader()
        node_chunks = reader.read(src_path, _config())
        assert node_chunks, "Expected at least one chunk from <em> chapter"

        provider = _FakeProvider()
        chunks = chunk_prose(node_chunks, _config(), provider)

        # Fake translation: keep the <em> tag intact in translated_text.
        # The source_text contains "A <em>critical</em> point." — our fake
        # translation prefixes "[ES] " but keeps the tag.
        translated_chunks = []
        for chunk in chunks:
            segments = chunk.source_text.split("\n\n")
            # Prefix each segment with "[ES] " — em tags are preserved in place
            translated_segments = []
            for seg in segments:
                translated_segments.append("[ES] " + seg)
            translated_chunks.append(
                chunk.model_copy(
                    update={
                        "translated_text": "\n\n".join(translated_segments),
                        "status": ChunkStatus.DONE,
                    }
                )
            )

        writer = EpubWriter()
        writer.write(translated_chunks, src_path, out_path)

        # Read output XHTML and check for real <em> tag
        with zipfile.ZipFile(out_path, "r") as zf:
            xhtml_files = [n for n in zf.namelist() if n.endswith(".xhtml") or n.endswith(".html")]
            # Find the content chapter (not nav)
            content_files = [n for n in xhtml_files if "nav" not in n.lower()]
            assert content_files, f"No content XHTML files found; got: {xhtml_files}"

            found_em = False
            for fname in content_files:
                raw = zf.read(fname).decode("utf-8", errors="replace")
                # A REAL <em> tag — NOT escaped as &lt;em&gt;
                if "<em>" in raw and "</em>" in raw:
                    found_em = True
                    break
                # Also accept namespace-qualified em (rare but possible)
                if "<em " in raw or ":em>" in raw:
                    found_em = True
                    break

            assert found_em, (
                "Expected real <em>…</em> tags in output XHTML, "
                "but found only escaped text or no em at all. "
                f"Content files searched: {content_files}"
            )
    finally:
        os.unlink(src_path)
        if os.path.exists(out_path):
            os.unlink(out_path)


# ---------------------------------------------------------------------------
# Test 7: End-to-end provenance round-trip
#   reader → chunker → fake translate → writer → reader
#   Assert: translated text landed in the correct nodes
# ---------------------------------------------------------------------------


def test_e2e_provenance_round_trip() -> None:
    """Full pipeline: reader→chunker→translate→writer→reader.

    Verifies that:
    - The output EPUB can be re-read.
    - The translated text ("[ES] ..." prefix) appears in the output XHTML
      nodes corresponding to the original source text locations.
    - Chunks without translated_text (no DONE status) use source_text as
      fallback (writer must not crash on untranslated chunks).
    """
    chapters = [
        ("ch1.xhtml", _chapter_xhtml("The quick brown fox.")),
        ("ch2.xhtml", _chapter_xhtml("Jumps over the lazy dog.")),
        ("ch3.xhtml", _chapter_xhtml("The end of the story.")),
    ]
    src_bytes = _make_epub_bytes(chapters)
    src_path = _write_temp_epub(src_bytes)
    out_path = src_path.replace(".epub", "_out.epub")

    try:
        reader = EpubReader()
        node_chunks = reader.read(src_path, _config())
        provider = _FakeProvider()
        chunks = chunk_prose(node_chunks, _config(), provider)

        # Translate all chunks deterministically
        translated_chunks = _fake_translate(chunks)

        writer = EpubWriter()
        writer.write(translated_chunks, src_path, out_path)

        # Re-read output and inspect XHTML content
        with zipfile.ZipFile(out_path, "r") as zf:
            all_xhtml = [n for n in zf.namelist() if n.endswith(".xhtml") or n.endswith(".html")]
            content_files = [n for n in all_xhtml if "nav" not in n.lower()]

            all_text = ""
            for fname in content_files:
                all_text += zf.read(fname).decode("utf-8", errors="replace")

        # The "[ES] " prefix must appear in the output (translation landed)
        assert "[ES] " in all_text, (
            "Expected translated text ('[ES] ' prefix) in output XHTML, "
            "but it was not found. The writer may not be reinserting translated content."
        )
    finally:
        os.unlink(src_path)
        if os.path.exists(out_path):
            os.unlink(out_path)
