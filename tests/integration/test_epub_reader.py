"""Integration tests for EpubReader adapter (M2-1).

All fixtures are built programmatically using ebooklib — no binary .epub files committed.
Tests follow strict TDD: written FIRST to be RED, then implementation makes them GREEN.
"""
from __future__ import annotations

import io
import os
import tempfile
import zipfile

import pytest
from ebooklib import epub

from borgesica.adapters.readers.epub_reader import EpubReader
from borgesica.domain.errors import UnsupportedFormatError
from borgesica.domain.models import JobConfig, SourceType


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_epub(
    chapters: list[tuple[str, bytes]],
    *,
    include_encryption_xml: bool = False,
    extra_image: bool = False,
) -> bytes:
    """Build a minimal valid EPUB in memory using ebooklib.

    Args:
        chapters: list of (file_name, xhtml_bytes) tuples in spine order.
        include_encryption_xml: if True, inject META-INF/encryption.xml to simulate DRM.
        extra_image: if True, add a PNG image item to the EPUB.

    Returns:
        Raw bytes of the .epub file.
    """
    book = epub.EpubBook()
    book.set_identifier("test-id-001")
    book.set_title("Test Book")
    book.set_language("en")
    book.add_author("Test Author")

    epub_items: list[epub.EpubHtml] = []
    for idx, (fname, content) in enumerate(chapters):
        item = epub.EpubHtml(title=f"Chapter {idx + 1}", file_name=fname, lang="en")
        item.content = content
        book.add_item(item)
        epub_items.append(item)

    if extra_image:
        img_item = epub.EpubImage()
        img_item.uid = "img-001"
        img_item.file_name = "images/test.png"
        img_item.media_type = "image/png"
        # Minimal 1x1 transparent PNG bytes
        img_item.content = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
            b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
            b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        book.add_item(img_item)

    book.spine = ["nav"] + epub_items
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    # Write to a BytesIO buffer via a temp file
    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
        tmp_path = f.name
    try:
        epub.write_epub(tmp_path, book, {})
        with open(tmp_path, "rb") as f:
            epub_bytes = f.read()
    finally:
        os.unlink(tmp_path)

    if include_encryption_xml:
        # Re-open the ZIP and inject META-INF/encryption.xml
        buf_in = io.BytesIO(epub_bytes)
        buf_out = io.BytesIO()
        with zipfile.ZipFile(buf_in, "r") as zin, zipfile.ZipFile(buf_out, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                zout.writestr(item, zin.read(item.filename))
            zout.writestr(
                "META-INF/encryption.xml",
                '<?xml version="1.0"?>'
                '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
                '<EncryptedData/>'
                "</encryption>",
            )
        epub_bytes = buf_out.getvalue()

    return epub_bytes


def _write_temp_epub(content: bytes) -> str:
    """Write bytes to a temporary .epub file and return its path."""
    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
        f.write(content)
        return f.name


def _simple_chapter(text: str, file_name: str = "ch.xhtml") -> tuple[str, bytes]:
    """Return a (file_name, xhtml_bytes) tuple for a chapter with one paragraph."""
    content = (
        f"<?xml version='1.0' encoding='utf-8'?>"
        f"<html xmlns='http://www.w3.org/1999/xhtml'>"
        f"<head><title>Chapter</title></head>"
        f"<body><p>{text}</p></body>"
        f"</html>"
    ).encode()
    return file_name, content


def _config() -> JobConfig:
    return JobConfig(source_type=SourceType.EPUB, model="claude-haiku-4-5")


# ---------------------------------------------------------------------------
# Test 1: Valid non-DRM EPUB → list of chunks, no exception
# ---------------------------------------------------------------------------


def test_valid_epub_returns_chunks() -> None:
    """A valid non-DRM EPUB produces a list of Chunk objects with no exception."""
    chapters = [
        _simple_chapter("Hello, world!", "ch1.xhtml"),
    ]
    epub_bytes = _make_epub(chapters)
    path = _write_temp_epub(epub_bytes)
    try:
        reader = EpubReader()
        chunks = reader.read(path, _config())
        assert isinstance(chunks, list)
        assert len(chunks) >= 1
        # Each chunk has non-empty source_text
        for chunk in chunks:
            assert isinstance(chunk.source_text, str)
            assert chunk.source_text.strip() != ""
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Test 2: encryption.xml present → UnsupportedFormatError (DRM message)
# ---------------------------------------------------------------------------


def test_drm_epub_raises_unsupported_format_error() -> None:
    """An EPUB with META-INF/encryption.xml raises UnsupportedFormatError (DRM)."""
    chapters = [_simple_chapter("Secret content", "ch1.xhtml")]
    epub_bytes = _make_epub(chapters, include_encryption_xml=True)
    path = _write_temp_epub(epub_bytes)
    try:
        reader = EpubReader()
        with pytest.raises(UnsupportedFormatError) as exc_info:
            reader.read(path, _config())
        err = exc_info.value
        assert err.path == path
        # The reason must mention DRM
        assert "DRM" in err.reason or "drm" in err.reason.lower()
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Test 3: Invalid ZIP with .epub extension → UnsupportedFormatError
# ---------------------------------------------------------------------------


def test_invalid_zip_epub_raises_unsupported_format_error() -> None:
    """A file with .epub extension that is not a valid ZIP raises UnsupportedFormatError."""
    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as f:
        f.write(b"This is not a ZIP file, just garbage bytes")
        path = f.name
    try:
        reader = EpubReader()
        with pytest.raises(UnsupportedFormatError) as exc_info:
            reader.read(path, _config())
        err = exc_info.value
        assert err.path == path
        assert "valid EPUB" in err.reason or "valid epub" in err.reason.lower()
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Test 4: Spine order respected (3 chapters → ch1 before ch2 before ch3)
# ---------------------------------------------------------------------------


def test_spine_order_is_respected() -> None:
    """Chunks are emitted in spine order: all ch1 chunks before ch2 before ch3."""
    chapters = [
        ("ch1.xhtml", (
            b"<?xml version='1.0' encoding='utf-8'?>"
            b"<html xmlns='http://www.w3.org/1999/xhtml'>"
            b"<head><title>Ch1</title></head>"
            b"<body><p>Chapter one paragraph one.</p><p>Chapter one paragraph two.</p></body>"
            b"</html>"
        )),
        ("ch2.xhtml", (
            b"<?xml version='1.0' encoding='utf-8'?>"
            b"<html xmlns='http://www.w3.org/1999/xhtml'>"
            b"<head><title>Ch2</title></head>"
            b"<body><p>Chapter two paragraph one.</p></body>"
            b"</html>"
        )),
        ("ch3.xhtml", (
            b"<?xml version='1.0' encoding='utf-8'?>"
            b"<html xmlns='http://www.w3.org/1999/xhtml'>"
            b"<head><title>Ch3</title></head>"
            b"<body><p>Chapter three paragraph one.</p></body>"
            b"</html>"
        )),
    ]
    epub_bytes = _make_epub(chapters)
    path = _write_temp_epub(epub_bytes)
    try:
        reader = EpubReader()
        chunks = reader.read(path, _config())

        # Find chunks from each chapter by their meta href
        ch1_indices = [i for i, c in enumerate(chunks) if "ch1.xhtml" in c.meta.get("epub_item_href", "")]
        ch2_indices = [i for i, c in enumerate(chunks) if "ch2.xhtml" in c.meta.get("epub_item_href", "")]
        ch3_indices = [i for i, c in enumerate(chunks) if "ch3.xhtml" in c.meta.get("epub_item_href", "")]

        assert ch1_indices, "Expected chunks from ch1.xhtml"
        assert ch2_indices, "Expected chunks from ch2.xhtml"
        assert ch3_indices, "Expected chunks from ch3.xhtml"

        # All ch1 chunks come before all ch2 chunks; all ch2 before all ch3
        assert max(ch1_indices) < min(ch2_indices), (
            f"ch1 max index {max(ch1_indices)} should be before ch2 min index {min(ch2_indices)}"
        )
        assert max(ch2_indices) < min(ch3_indices), (
            f"ch2 max index {max(ch2_indices)} should be before ch3 min index {min(ch3_indices)}"
        )
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Test 5: Images not extracted as chunks (no binary data in source_text)
# ---------------------------------------------------------------------------


def test_images_not_extracted_as_chunks() -> None:
    """No chunk.source_text contains binary image data."""
    chapters = [
        ("ch1.xhtml", (
            b"<?xml version='1.0' encoding='utf-8'?>"
            b"<html xmlns='http://www.w3.org/1999/xhtml'>"
            b"<head><title>Ch with image</title></head>"
            b"<body>"
            b"<p>Text before image.</p>"
            b'<img src="../images/test.png" alt="test"/>'
            b"<p>Text after image.</p>"
            b"</body>"
            b"</html>"
        )),
    ]
    epub_bytes = _make_epub(chapters, extra_image=True)
    path = _write_temp_epub(epub_bytes)
    try:
        reader = EpubReader()
        chunks = reader.read(path, _config())

        assert len(chunks) >= 1, "Expected at least one text chunk"
        for chunk in chunks:
            # source_text must be a valid str (no binary/decoded garbage)
            assert isinstance(chunk.source_text, str)
            # PNG magic bytes must not appear in any chunk
            assert b"\x89PNG" not in chunk.source_text.encode("utf-8", errors="replace")
            # source_text must not be empty binary-looking garbage
            text = chunk.source_text.strip()
            assert text, "source_text must not be empty"
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Test 6: chunk.meta contains epub_item_href and node_path
# ---------------------------------------------------------------------------


def test_chunk_meta_contains_required_keys() -> None:
    """Each chunk.meta must contain 'epub_item_href' and 'node_path' keys."""
    chapters = [
        ("ch1.xhtml", (
            b"<?xml version='1.0' encoding='utf-8'?>"
            b"<html xmlns='http://www.w3.org/1999/xhtml'>"
            b"<head><title>Meta Test</title></head>"
            b"<body>"
            b"<h1>Heading One</h1>"
            b"<p>First paragraph.</p>"
            b"<p>Second paragraph.</p>"
            b"</body>"
            b"</html>"
        )),
    ]
    epub_bytes = _make_epub(chapters)
    path = _write_temp_epub(epub_bytes)
    try:
        reader = EpubReader()
        chunks = reader.read(path, _config())

        assert chunks, "Expected at least one chunk"
        for chunk in chunks:
            assert "epub_item_href" in chunk.meta, (
                f"Missing 'epub_item_href' in chunk.meta: {chunk.meta}"
            )
            assert "node_path" in chunk.meta, (
                f"Missing 'node_path' in chunk.meta: {chunk.meta}"
            )
            # Both values must be non-empty strings
            assert isinstance(chunk.meta["epub_item_href"], str) and chunk.meta["epub_item_href"]
            assert isinstance(chunk.meta["node_path"], str) and chunk.meta["node_path"]
    finally:
        os.unlink(path)
