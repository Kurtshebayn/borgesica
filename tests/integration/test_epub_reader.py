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
    toc_links: list[tuple[str, str, str]] | None = None,
    nav_file_name: str | None = None,
) -> bytes:
    """Build a minimal valid EPUB in memory using ebooklib.

    Args:
        chapters: list of (file_name, xhtml_bytes) tuples in spine order.
        include_encryption_xml: if True, inject META-INF/encryption.xml to simulate DRM.
        extra_image: if True, add a PNG image item to the EPUB.
        toc_links: optional list of (href, title, uid) tuples. When provided,
            populates ``book.toc`` so ebooklib emits real ``<a href>`` entries
            in the generated nav doc's ``<ol>`` and matching ``navPoint``
            entries in the ncx. Additive only — omitting this parameter keeps
            the default (empty ``book.toc``) behavior of every existing test.
        nav_file_name: optional override for the ``EpubNav`` item's filename
            (default ebooklib name is ``nav.xhtml``). Used to test that nav-doc
            detection does not rely on a "nav" filename substring.

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

    if toc_links:
        book.toc = [epub.Link(href, title, uid) for href, title, uid in toc_links]

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
    nav_item = epub.EpubNav()
    if nav_file_name:
        nav_item.file_name = nav_file_name
    book.add_item(nav_item)

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


# ---------------------------------------------------------------------------
# Test 8: DRM detection is case-insensitive (W-M2-1)
# ---------------------------------------------------------------------------


def test_drm_detection_case_insensitive() -> None:
    """DRM check must be case-insensitive: META-INF/ENCRYPTION.XML raises UnsupportedFormatError.

    W-M2-1: the old code checked for the exact string "META-INF/encryption.xml" with `in`
    on the namelist. An EPUB whose ZIP stores the entry as "META-INF/ENCRYPTION.XML" or
    "META-INF/Encryption.xml" should also raise with the DRM-specific message, not silently
    pass the check and try to translate DRM-protected content.

    RED: current code checks `"META-INF/encryption.xml" in names` (case-sensitive) → the
    uppercase variant slips through without raising.
    GREEN: fix uses case-folded comparison.
    """
    chapters = [_simple_chapter("DRM-protected content", "ch1.xhtml")]
    epub_bytes = _make_epub(chapters)

    # Inject the encryption entry with UPPERCASE name (simulates some DRM tools)
    buf_in = io.BytesIO(epub_bytes)
    buf_out = io.BytesIO()
    with (
        zipfile.ZipFile(buf_in, "r") as zin,
        zipfile.ZipFile(buf_out, "w", zipfile.ZIP_DEFLATED) as zout,
    ):
        for item in zin.infolist():
            zout.writestr(item, zin.read(item.filename))
        zout.writestr(
            "META-INF/ENCRYPTION.XML",  # uppercase — the fix must catch this
            '<?xml version="1.0"?>'
            '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
            "<EncryptedData/>"
            "</encryption>",
        )
    epub_bytes_drm = buf_out.getvalue()

    path = _write_temp_epub(epub_bytes_drm)
    try:
        reader = EpubReader()
        with pytest.raises(UnsupportedFormatError) as exc_info:
            reader.read(path, _config())
        err = exc_info.value
        assert err.path == path
        # The reason MUST mention DRM — not a generic parse error
        reason_lower = err.reason.lower()
        assert "drm" in reason_lower, (
            f"Expected DRM-specific message, got: {err.reason!r}"
        )
        # Must NOT say "not a valid EPUB" (which would mean it passed DRM check
        # and failed later — wrong code path)
        assert "not a valid epub" not in reason_lower, (
            f"Error should identify DRM, not generic parse failure: {err.reason!r}"
        )
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Test 7: chunk.meta contains chapter_index; it increases per spine document
# ---------------------------------------------------------------------------


def test_chunk_meta_contains_chapter_index() -> None:
    """Each chunk.meta must contain 'chapter_index' (int, 0-based per spine document).

    Nodes from the first spine document have chapter_index=0.
    Nodes from the second spine document have chapter_index=1, etc.
    chapter_index must be monotonically non-decreasing in spine order.
    """
    chapters = [
        ("ch1.xhtml", (
            b"<?xml version='1.0' encoding='utf-8'?>"
            b"<html xmlns='http://www.w3.org/1999/xhtml'>"
            b"<head><title>Ch1</title></head>"
            b"<body>"
            b"<p>Chapter one paragraph one.</p>"
            b"<p>Chapter one paragraph two.</p>"
            b"</body>"
            b"</html>"
        )),
        ("ch2.xhtml", (
            b"<?xml version='1.0' encoding='utf-8'?>"
            b"<html xmlns='http://www.w3.org/1999/xhtml'>"
            b"<head><title>Ch2</title></head>"
            b"<body>"
            b"<p>Chapter two paragraph one.</p>"
            b"<p>Chapter two paragraph two.</p>"
            b"</body>"
            b"</html>"
        )),
        ("ch3.xhtml", (
            b"<?xml version='1.0' encoding='utf-8'?>"
            b"<html xmlns='http://www.w3.org/1999/xhtml'>"
            b"<head><title>Ch3</title></head>"
            b"<body>"
            b"<p>Chapter three paragraph one.</p>"
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

        # Every chunk must carry chapter_index as an int
        for chunk in chunks:
            assert "chapter_index" in chunk.meta, (
                f"Missing 'chapter_index' in chunk.meta: {chunk.meta}"
            )
            assert isinstance(chunk.meta["chapter_index"], int), (
                f"chapter_index must be int, got {type(chunk.meta['chapter_index'])}"
            )

        # Gather chapter_index values per epub_item_href (spine document)
        ch1_indices = [
            c.meta["chapter_index"] for c in chunks
            if "ch1.xhtml" in c.meta.get("epub_item_href", "")
        ]
        ch2_indices = [
            c.meta["chapter_index"] for c in chunks
            if "ch2.xhtml" in c.meta.get("epub_item_href", "")
        ]
        ch3_indices = [
            c.meta["chapter_index"] for c in chunks
            if "ch3.xhtml" in c.meta.get("epub_item_href", "")
        ]

        assert ch1_indices, "Expected chunks from ch1.xhtml"
        assert ch2_indices, "Expected chunks from ch2.xhtml"
        assert ch3_indices, "Expected chunks from ch3.xhtml"

        # All nodes within the same spine doc share the same chapter_index
        assert len(set(ch1_indices)) == 1, (
            f"ch1 nodes should all share chapter_index; got {set(ch1_indices)}"
        )
        assert len(set(ch2_indices)) == 1, (
            f"ch2 nodes should all share chapter_index; got {set(ch2_indices)}"
        )
        assert len(set(ch3_indices)) == 1, (
            f"ch3 nodes should all share chapter_index; got {set(ch3_indices)}"
        )

        # chapter_index increases by spine position: ch1 < ch2 < ch3
        assert ch1_indices[0] == 0, (
            f"First spine doc should have chapter_index=0, got {ch1_indices[0]}"
        )
        assert ch2_indices[0] == 1, (
            f"Second spine doc should have chapter_index=1, got {ch2_indices[0]}"
        )
        assert ch3_indices[0] == 2, (
            f"Third spine doc should have chapter_index=2, got {ch3_indices[0]}"
        )
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Test M4-5: Non-UTF-8 chapter encoding (iso-8859-1) → correct accented chars
# ---------------------------------------------------------------------------


def _make_epub_with_latin1_chapter() -> bytes:
    """Build a minimal EPUB whose chapter content is encoded in ISO-8859-1.

    The chapter declares ``<?xml version='1.0' encoding='iso-8859-1'?>`` and
    stores accented bytes (0xe9 for é, 0xf1 for ñ, 0xf3 for ó) in ISO-8859-1.
    ebooklib stores raw bytes; ``get_content()`` forces UTF-8 parsing -> mojibake.
    The fix reads ``item.content`` directly and honours the declared encoding.
    """
    import io as _io
    import zipfile as _zipfile

    # Build the chapter in ISO-8859-1 (Latin-1).
    # "Café", "mañana", "corazón" — the accented chars are meaningful test targets.
    chapter_text_unicode = "Café mañana corazón"
    chapter_xml_bytes = (
        "<?xml version='1.0' encoding='iso-8859-1'?>"
        "<html xmlns='http://www.w3.org/1999/xhtml'>"
        "<head><title>Test</title></head>"
        "<body><p>" + chapter_text_unicode + "</p></body>"
        "</html>"
    ).encode("iso-8859-1")

    # We need to build a minimal EPUB ZIP manually because ebooklib always
    # re-encodes content as UTF-8 when writing.  The raw ZIP is the only way
    # to inject a genuine ISO-8859-1 XHTML file.
    buf = _io.BytesIO()
    with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
        # mimetype MUST be the first entry and uncompressed
        zf.writestr(
            _zipfile.ZipInfo("mimetype"),
            "application/epub+zip",
        )
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?>'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            "<rootfiles>"
            '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles>"
            "</container>",
        )
        zf.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0" encoding="utf-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0"'
            ' unique-identifier="uid">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:identifier id=\"uid\">latin1-test</dc:identifier>"
            "<dc:title>Latin-1 Test</dc:title>"
            "<dc:language>en</dc:language>"
            "</metadata>"
            "<manifest>"
            '<item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
            "</manifest>"
            '<spine toc="ncx">'
            '<itemref idref="ch1"/>'
            "</spine>"
            "</package>",
        )
        zf.writestr(
            "OEBPS/toc.ncx",
            '<?xml version="1.0" encoding="utf-8"?>'
            '<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN"'
            ' "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
            "<head><meta name=\"dtb:uid\" content=\"latin1-test\"/></head>"
            "<docTitle><text>Latin-1 Test</text></docTitle>"
            "<navMap>"
            '<navPoint id="np1" playOrder="1">'
            "<navLabel><text>Chapter 1</text></navLabel>"
            '<content src="ch1.xhtml"/>'
            "</navPoint>"
            "</navMap>"
            "</ncx>",
        )
        # Inject the ISO-8859-1 encoded chapter as raw bytes
        zf.writestr("OEBPS/ch1.xhtml", chapter_xml_bytes)
    return buf.getvalue()


def test_non_utf8_chapter_encoding_produces_correct_accented_chars() -> None:
    """EpubReader must honour the chapter's declared ISO-8859-1 encoding.

    M4-5 / S-M2-2: a chapter whose XML declaration says ``encoding='iso-8859-1'``
    and whose bytes are Latin-1 encoded should yield correct Unicode text in
    ``source_text``.  Before the fix, ``get_content()`` forces UTF-8 -> replacement
    char U+FFFD (``�``) or mojibake.  After the fix all accented chars survive.

    RED (before fix): source_text contains U+FFFD -- replacement chars.
    GREEN (after fix): source_text contains "Café", "mañana", "corazón" verbatim,
    zero replacement chars.
    """
    epub_bytes = _make_epub_with_latin1_chapter()
    path = _write_temp_epub(epub_bytes)
    try:
        reader = EpubReader()
        chunks = reader.read(path, _config())

        assert chunks, "Expected at least one chunk from the ISO-8859-1 chapter"

        # Collect all text from all chunks
        all_text = " ".join(c.source_text for c in chunks)

        # Must NOT contain the Unicode replacement character
        assert "�" not in all_text, (
            f"Replacement char U+FFFD found in source_text -- encoding not honoured. "
            f"Got: {all_text!r}"
        )

        # Must contain the correct accented characters
        assert "é" in all_text, f"Expected 'é' (from 'Café') in text, got: {all_text!r}"
        assert "ñ" in all_text, f"Expected 'ñ' (from 'mañana') in text, got: {all_text!r}"
        assert "ó" in all_text, f"Expected 'ó' (from 'corazón') in text, got: {all_text!r}"
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Test WU2-1: nav doc without "nav" filename substring must not leak (D2 gate fix)
# ---------------------------------------------------------------------------


def test_nav_doc_without_nav_substring_is_recognized_and_excluded() -> None:
    """A nav doc named ``contents.xhtml`` (no "nav" substring) must still be
    recognized as the EPUB3 navigation document via ``isinstance`` alone, and
    must NOT be treated as an ordinary body chapter.

    W-nav-toc-translation D2: the old two-level gate
    ``if item_name and ("nav" in item_name.lower()): if isinstance(...): continue``
    only routed control into the ``isinstance`` check when the filename
    happened to contain "nav". A nav doc named ``contents.xhtml`` skipped the
    ``isinstance`` check entirely (the outer ``if`` was False) and fell
    through to the general body-chapter extraction path instead — consuming
    a ``chapter_index`` slot meant for a real chapter and producing chunks
    whose ``epub_item_href`` is the nav doc itself.

    RED (before fix): a chunk exists with ``epub_item_href == "contents.xhtml"``
    (the general body-chapter path ran for the nav doc) and/or the real
    chapter's ``chapter_index`` is shifted to 1 instead of 0 because the nav
    doc consumed slot 0.
    GREEN (after fix): isinstance(item, epub.EpubNav) alone identifies and
    excludes the item from the general body-chapter traversal, regardless of
    filename — the real chapter keeps ``chapter_index == 0`` and no chunk
    carries ``epub_item_href == "contents.xhtml"``.
    """
    chapters = [_simple_chapter("Chapter one text.", "ch1.xhtml")]
    epub_bytes = _make_epub(
        chapters,
        toc_links=[("ch1.xhtml", "Chapter One", "ch1")],
        nav_file_name="contents.xhtml",
    )
    path = _write_temp_epub(epub_bytes)
    try:
        reader = EpubReader()
        chunks = reader.read(path, _config())

        # No chunk from the general body-chapter traversal may originate from
        # the nav item's href (contents.xhtml) — at this stage (WU2-1 only
        # fixes the gate; the dedicated nav walk lands in WU2-2), the branch
        # still `continue`s, so contents.xhtml must produce ZERO chunks.
        nav_href_chunks = [
            c for c in chunks if c.meta.get("epub_item_href") == "contents.xhtml"
        ]
        assert not nav_href_chunks, (
            f"Expected no chunks from contents.xhtml (nav doc), got: {nav_href_chunks}"
        )

        # The real chapter must be the FIRST body chapter (chapter_index=0) —
        # the nav doc must not have consumed the slot ahead of it.
        ch1_chunks = [c for c in chunks if c.meta.get("epub_item_href") == "ch1.xhtml"]
        assert ch1_chunks, "Expected chunks from ch1.xhtml"
        assert all(c.meta["chapter_index"] == 0 for c in ch1_chunks), (
            f"Expected ch1.xhtml chapter_index == 0 (nav doc must not consume a "
            f"chapter slot), got: {[c.meta['chapter_index'] for c in ch1_chunks]}"
        )
    finally:
        os.unlink(path)


def test_nav_doc_named_nav_xhtml_still_skipped_regression() -> None:
    """Regression: a nav doc named ``nav.xhtml`` (contains "nav") must still
    be correctly skipped from the general body-chunk traversal after the
    gate simplification to isinstance-only.
    """
    chapters = [_simple_chapter("Chapter one text.", "ch1.xhtml")]
    epub_bytes = _make_epub(
        chapters,
        toc_links=[("ch1.xhtml", "Chapter One", "ch1")],
    )
    path = _write_temp_epub(epub_bytes)
    try:
        reader = EpubReader()
        chunks = reader.read(path, _config())

        nav_href_chunks = [
            c for c in chunks if c.meta.get("epub_item_href") == "nav.xhtml"
        ]
        assert not nav_href_chunks, (
            f"Expected no chunks from nav.xhtml (nav doc), got: {nav_href_chunks}"
        )
    finally:
        os.unlink(path)
