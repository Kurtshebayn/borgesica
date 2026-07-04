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
from lxml import etree

from borgesica.adapters.readers.epub_reader import EpubReader
from borgesica.adapters.writers.epub_writer import EpubWriter
from borgesica.domain.chunking import chunk_prose
from borgesica.domain.models import Chunk, ChunkStatus, JobConfig, SourceType

# ---------------------------------------------------------------------------
# Raw-ZIP fixture helper (mirrors tests/integration/test_epub_reader.py's
# _replace_zip_entry pattern) — used by nav/ncx writer tests below to inject
# hand-built nav docs and ncx documents that ebooklib's public API cannot
# easily produce (e.g. landmarks/page-list siblings, fragment-bearing content
# src, unmatched navPoints).
# ---------------------------------------------------------------------------


def _replace_zip_entry(epub_bytes: bytes, entry_name: str, new_content: bytes) -> bytes:
    """Return a copy of ``epub_bytes`` with ``entry_name``'s content replaced."""
    buf_in = io.BytesIO(epub_bytes)
    buf_out = io.BytesIO()
    with (
        zipfile.ZipFile(buf_in, "r") as zin,
        zipfile.ZipFile(buf_out, "w", zipfile.ZIP_DEFLATED) as zout,
    ):
        for item in zin.infolist():
            data = new_content if item.filename == entry_name else zin.read(item.filename)
            zout.writestr(item, data)
    return buf_out.getvalue()


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


def _chapter_xhtml_plain(text: str) -> bytes:
    """Build a minimal XHTML chapter with one plain <p> (no inline tags)."""
    return (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<html xmlns='http://www.w3.org/1999/xhtml'>"
        "<head><title>Chapter</title></head>"
        f"<body><p>{text}</p></body>"
        "</html>"
    ).encode("utf-8")


def _make_epub_bytes(
    chapters: list[tuple[str, bytes]],
    *,
    include_png: bool = False,
    include_jpeg: bool = False,
    include_css: bool = False,
    toc_links: list[tuple[str, str, str]] | None = None,
) -> bytes:
    """Build a minimal valid EPUB in memory using ebooklib.

    Args:
        toc_links: optional list of (href, title, uid) tuples. When provided,
            populates ``book.toc`` so ebooklib emits real ``<a href>`` entries
            in the generated nav doc and matching ``navPoint`` entries in the
            ncx. Additive only — omitting this parameter keeps the default
            (empty ``book.toc``) behavior of every existing test.
    """
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

    if toc_links:
        book.toc = [epub.Link(href, title, uid) for href, title, uid in toc_links]

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
# Test 6b: namespaced attribute (e.g. epub:type) in a reinserted fragment must
# not crash the writer.
#
# Regression test: exporting a real book, a translated fragment re-parsed with
# lxml's HTMLParser kept the literal prefixed attribute name "epub:type" (the
# HTML parser does not resolve namespaces — it leaves the colon in the raw
# attribute string). _copy_element_no_ns then called new_el.set("epub:type",
# ...), and lxml's XML .set() rejects any name containing ":" that isn't a
# real Clark-notation namespace, raising:
#   ValueError: Invalid attribute name 'epub:type'
# Full traceback path: write -> _do_write -> _patch_entry ->
# _patch_xhtml_document -> _set_element_content -> _copy_element_no_ns.
#
# Chosen behavior (documented here): the namespace prefix is stripped and only
# the local attribute name survives (epub:type -> type). This mirrors how
# _local_tag already strips namespaces from element tag names — attributes
# get the same treatment for consistency. A true xmlns:* declaration (e.g.
# xmlns:epub) is dropped entirely, never copied as a literal attribute.
# ---------------------------------------------------------------------------


def test_namespaced_attribute_in_fragment_does_not_crash_writer() -> None:
    """A reinserted fragment containing epub:type=... must not raise ValueError.

    Reproduces the live crash: translated_text contains a <span epub:type="...">
    fragment (as produced when lxml's HTMLParser re-parses reinserted content
    without resolving the epub: namespace). The writer must survive and the
    attribute must land as its local name ("type"), not crash on ':'.
    """
    chapters = [("ch1.xhtml", _chapter_xhtml_plain("Hello, world!"))]
    src_bytes = _make_epub_bytes(chapters)
    src_path = _write_temp_epub(src_bytes)
    out_path = src_path.replace(".epub", "_out.epub")

    try:
        reader = EpubReader()
        node_chunks = reader.read(src_path, _config())
        assert node_chunks, "Expected at least one chunk from plain chapter"

        provider = _FakeProvider()
        chunks = chunk_prose(node_chunks, _config(), provider)

        # Fake translation: inject a namespaced attribute exactly like the
        # live crash — a fragment with epub:type on a span-like element.
        translated_chunks = []
        for chunk in chunks:
            segments = chunk.source_text.split("\n\n")
            translated_segments = [
                f'[ES] <aside epub:type="footnote">nota</aside> {seg}' for seg in segments
            ]
            translated_chunks.append(
                chunk.model_copy(
                    update={
                        "translated_text": "\n\n".join(translated_segments),
                        "status": ChunkStatus.DONE,
                    }
                )
            )

        writer = EpubWriter()
        # Must NOT raise ValueError: Invalid attribute name 'epub:type'
        writer.write(translated_chunks, src_path, out_path)

        with zipfile.ZipFile(out_path, "r") as zf:
            xhtml_files = [n for n in zf.namelist() if n.endswith(".xhtml") or n.endswith(".html")]
            content_files = [n for n in xhtml_files if "nav" not in n.lower()]
            assert content_files, f"No content XHTML files found; got: {xhtml_files}"

            raw = zf.read(content_files[0]).decode("utf-8", errors="replace")
            assert "epub:type" not in raw, (
                "Namespace prefix must be stripped from the attribute name; "
                f"found literal 'epub:type' in output: {raw}"
            )
            assert "<aside" in raw and 'type="footnote"' in raw, (
                f"Expected local attribute name 'type' to survive; got: {raw}"
            )
    finally:
        os.unlink(src_path)
        if os.path.exists(out_path):
            os.unlink(out_path)


# ---------------------------------------------------------------------------
# Test 6c: _patch_entry filename matching must respect path boundaries.
#
# Regression tests: translating a real book, the EPUB3 nav doc "toc.xhtml"
# received the patches destined for "content-toc.xhtml" because the reverse
# fuzzy match used a bare suffix check:
#   'content-toc.xhtml'.endswith(os.path.basename('toc.xhtml'))  → True
# producing 15 misdirected "skipping patch" warnings per run (and, had the
# node paths existed in the nav doc, silently overwritten navigation labels).
# The forward direction had the same latent bug:
#   'OEBPS/content-toc.xhtml'.endswith('toc.xhtml')  → True
#
# Chosen behavior: a suffix match is only valid at a "/" path boundary, in
# BOTH directions. Exact matches and prefix-directory matches keep working.
# ---------------------------------------------------------------------------

_PATCH_ENTRY_XHTML = (
    "<?xml version='1.0' encoding='utf-8'?>"
    "<html xmlns='http://www.w3.org/1999/xhtml'>"
    "<head><title>Doc</title></head>"
    "<body><p>Original paragraph.</p></body>"
    "</html>"
).encode("utf-8")


def test_patch_entry_reverse_suffix_does_not_misdirect() -> None:
    """Patches for 'content-toc.xhtml' must NOT be applied to 'toc.xhtml'."""
    writer = EpubWriter()
    flat_patches = {"content-toc.xhtml": {"/p[0]": "ÍNDICE"}}
    result = writer._patch_entry("toc.xhtml", _PATCH_ENTRY_XHTML, flat_patches)
    assert result == _PATCH_ENTRY_XHTML, (
        "toc.xhtml was modified by patches destined for content-toc.xhtml "
        "(reverse bare-suffix match misdirected the patch)"
    )


def test_patch_entry_forward_suffix_requires_path_boundary() -> None:
    """Patches for 'toc.xhtml' must NOT be applied to 'OEBPS/content-toc.xhtml'."""
    writer = EpubWriter()
    flat_patches = {"toc.xhtml": {"/p[0]": "ÍNDICE"}}
    result = writer._patch_entry(
        "OEBPS/content-toc.xhtml", _PATCH_ENTRY_XHTML, flat_patches
    )
    assert result == _PATCH_ENTRY_XHTML, (
        "content-toc.xhtml was modified by patches destined for toc.xhtml "
        "(forward bare-suffix match misdirected the patch)"
    )


def test_patch_entry_exact_match_patches() -> None:
    """Exact href == zip entry name must keep patching."""
    writer = EpubWriter()
    flat_patches = {"toc.xhtml": {"/p[0]": "ÍNDICE"}}
    result = writer._patch_entry("toc.xhtml", _PATCH_ENTRY_XHTML, flat_patches)
    assert "ÍNDICE".encode("utf-8") in result


def test_patch_entry_prefixed_zip_entry_patches() -> None:
    """href 'ch1.xhtml' must match zip entry 'OEBPS/ch1.xhtml' (boundary '/')."""
    writer = EpubWriter()
    flat_patches = {"ch1.xhtml": {"/p[0]": "Hola mundo"}}
    result = writer._patch_entry("OEBPS/ch1.xhtml", _PATCH_ENTRY_XHTML, flat_patches)
    assert b"Hola mundo" in result


def test_patch_entry_prefixed_href_patches() -> None:
    """href 'OEBPS/ch1.xhtml' must match zip entry 'ch1.xhtml' (boundary '/')."""
    writer = EpubWriter()
    flat_patches = {"OEBPS/ch1.xhtml": {"/p[0]": "Hola mundo"}}
    result = writer._patch_entry("ch1.xhtml", _PATCH_ENTRY_XHTML, flat_patches)
    assert b"Hola mundo" in result


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

        # Re-read output and inspect XHTML content per chapter.
        # S-M2-3: per-chapter assertions replace the weak "anywhere in output" check.
        # A bug that dumps all translations into one node or swaps chapters must fail.
        with zipfile.ZipFile(out_path, "r") as zf:
            all_names = zf.namelist()

            def _read_xhtml(fname: str) -> str:
                return zf.read(fname).decode("utf-8", errors="replace")

            # Find chapter files by name suffix (ebooklib may prefix with OEBPS/ etc.)
            def _find_chapter(suffix: str) -> str | None:
                for name in all_names:
                    if name.endswith(suffix) and "nav" not in name.lower():
                        return name
                return None

            ch1_file = _find_chapter("ch1.xhtml")
            ch2_file = _find_chapter("ch2.xhtml")
            ch3_file = _find_chapter("ch3.xhtml")

            assert ch1_file is not None, f"ch1.xhtml not found in output; files: {all_names}"
            assert ch2_file is not None, f"ch2.xhtml not found in output; files: {all_names}"
            assert ch3_file is not None, f"ch3.xhtml not found in output; files: {all_names}"

            ch1_text = _read_xhtml(ch1_file)
            ch2_text = _read_xhtml(ch2_file)
            ch3_text = _read_xhtml(ch3_file)

        # Chapter-1 source: "The quick brown fox." → translated: "[ES] The quick brown fox."
        # The chapter-1 translation must be in ch1.xhtml, NOT only in some other chapter.
        assert "[ES] The quick brown fox." in ch1_text, (
            "Chapter-1 translation must appear in ch1.xhtml. "
            f"ch1 content snippet: {ch1_text[:300]!r}"
        )
        # Chapter-2 source: "Jumps over the lazy dog." → translated: "[ES] Jumps over the lazy dog."
        assert "[ES] Jumps over the lazy dog." in ch2_text, (
            "Chapter-2 translation must appear in ch2.xhtml. "
            f"ch2 content snippet: {ch2_text[:300]!r}"
        )
        # Chapter-3 source: "The end of the story." → translated: "[ES] The end of the story."
        assert "[ES] The end of the story." in ch3_text, (
            "Chapter-3 translation must appear in ch3.xhtml. "
            f"ch3 content snippet: {ch3_text[:300]!r}"
        )

        # Cross-chapter guard: ch1's specific text must NOT appear in ch2 or ch3
        # (catches a bug where all translations dump into a single document)
        assert "[ES] The quick brown fox." not in ch2_text, (
            "Chapter-1 translation must NOT appear in ch2.xhtml — wrong chapter placement"
        )
        assert "[ES] Jumps over the lazy dog." not in ch1_text, (
            "Chapter-2 translation must NOT appear in ch1.xhtml — wrong chapter placement"
        )
    finally:
        os.unlink(src_path)
        if os.path.exists(out_path):
            os.unlink(out_path)


# ---------------------------------------------------------------------------
# WU5-1: nav <a>/<span>/heading patch regression guard (D5 xhtml path).
#
# These are REGRESSION-GUARD tests: _do_write/_patch_entry's existing .xhtml
# branch already patches by epub_item_href + node_path and never touches
# element.attrib. Confirms the existing mechanism correctly handles nav-doc
# input now that the reader's nav walk (WU2-2) produces real nav-label
# chunks feeding it.
# ---------------------------------------------------------------------------

_NAV_XHTML_SIMPLE = (
    b"<?xml version='1.0' encoding='utf-8'?>"
    b'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
    b"<head><title>Nav</title></head>"
    b"<body>"
    b'<nav epub:type="toc" id="toc">'
    b"<h2>Contents</h2>"
    b'<ol><li><a href="ch1.xhtml">Chapter One</a></li></ol>'
    b"</nav>"
    b"</body>"
    b"</html>"
)

_NAV_XHTML_SIBLINGS = (
    b"<?xml version='1.0' encoding='utf-8'?>"
    b'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
    b"<head><title>Nav</title></head>"
    b"<body>"
    b'<nav epub:type="landmarks">'
    b"<h2>Guide</h2>"
    b'<ol><li><a href="ch1.xhtml">Cover</a></li></ol>'
    b"</nav>"
    b'<nav epub:type="toc" id="toc">'
    b"<h2>Contents</h2>"
    b'<ol><li><a href="ch1.xhtml">Chapter One</a></li></ol>'
    b"</nav>"
    b'<nav epub:type="page-list" hidden="hidden">'
    b"<h2>Pages</h2>"
    b'<ol><li><a href="ch1.xhtml#page1">1</a></li></ol>'
    b"</nav>"
    b"</body>"
    b"</html>"
)

_NAV_XHTML_NESTED = (
    b"<?xml version='1.0' encoding='utf-8'?>"
    b'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
    b"<head><title>Nav</title></head>"
    b"<body>"
    b'<nav epub:type="toc" id="toc">'
    b"<h2>Contents</h2>"
    b"<ol>"
    b'<li><a href="ch1.xhtml">Chapter One</a>'
    b"<ol><li><a href=\"ch1.xhtml#sec1\">Section One</a></li></ol>"
    b"</li>"
    b'<li><a href="ch2.xhtml">Chapter Two</a></li>'
    b"</ol>"
    b"</nav>"
    b"</body>"
    b"</html>"
)


def _make_nav_epub_bytes(nav_xhtml: bytes, chapters: list[tuple[str, bytes]]) -> bytes:
    """Build an ebooklib EPUB with toc_links, then swap in a hand-built nav doc.

    ebooklib's public API cannot express landmarks/page-list siblings or
    nested <ol> depth directly, so we generate a normal EPUB (with a
    populated book.toc so the manifest/spine/ncx are wired correctly) and
    then replace the nav.xhtml entry's raw bytes — mirroring the reader
    test suite's `_replace_zip_entry` pattern.
    """
    toc_links = [(fname, f"Label for {fname}", f"uid-{i}") for i, (fname, _) in enumerate(chapters)]
    src_bytes = _make_epub_bytes(chapters, toc_links=toc_links)
    return _replace_zip_entry(src_bytes, "EPUB/nav.xhtml", nav_xhtml)


def _find_nav_entry(zf: zipfile.ZipFile) -> str:
    names = [n for n in zf.namelist() if n.endswith("nav.xhtml")]
    assert names, f"nav.xhtml not found in output; got: {zf.namelist()}"
    return names[0]


def test_nav_a_label_patched_href_preserved_byte_for_byte() -> None:
    """Translated nav-label chunk patches the <a> text; href stays byte-identical."""
    chapters = [("ch1.xhtml", _chapter_xhtml("Chapter one body text."))]
    src_bytes = _make_nav_epub_bytes(_NAV_XHTML_SIMPLE, chapters)
    src_path = _write_temp_epub(src_bytes)
    out_path = src_path.replace(".epub", "_out.epub")

    try:
        reader = EpubReader()
        node_chunks = reader.read(src_path, _config())
        nav_label_chunk = next(
            c for c in node_chunks
            if c.meta.get("nav_href") == "ch1.xhtml" and c.source_text == "Chapter One"
        )

        provider = _FakeProvider()
        chunks = chunk_prose(node_chunks, _config(), provider)

        translated_chunks = []
        for chunk in chunks:
            prose_nodes = chunk.meta.get("prose_nodes", [])
            is_nav_batch = any(
                pn.get("node_path") == nav_label_chunk.meta["node_path"]
                and pn.get("epub_item_href") == nav_label_chunk.meta["epub_item_href"]
                for pn in prose_nodes
            )
            if is_nav_batch:
                segments = chunk.source_text.split("\n\n")
                translated_segments = [
                    "Capítulo Uno" if seg == "Chapter One" else "[ES] " + seg
                    for seg in segments
                ]
                translated_chunks.append(
                    chunk.model_copy(
                        update={
                            "translated_text": "\n\n".join(translated_segments),
                            "status": ChunkStatus.DONE,
                        }
                    )
                )
            else:
                translated_chunks.extend(_fake_translate([chunk]))

        writer = EpubWriter()
        writer.write(translated_chunks, src_path, out_path)

        with zipfile.ZipFile(out_path, "r") as zf:
            nav_name = _find_nav_entry(zf)
            raw = zf.read(nav_name).decode("utf-8", errors="replace")

        assert "Capítulo Uno" in raw, f"Expected translated label in nav doc, got: {raw}"
        assert 'href="ch1.xhtml"' in raw, (
            f"Expected byte-identical href='ch1.xhtml', got: {raw}"
        )
    finally:
        os.unlink(src_path)
        if os.path.exists(out_path):
            os.unlink(out_path)


def test_nav_sibling_node_path_stability_landmarks_toc_pagelist() -> None:
    """Writer patches the correct <a> inside toc, not landmarks/page-list siblings."""
    chapters = [("ch1.xhtml", _chapter_xhtml("Chapter one body text."))]
    src_bytes = _make_nav_epub_bytes(_NAV_XHTML_SIBLINGS, chapters)
    src_path = _write_temp_epub(src_bytes)
    out_path = src_path.replace(".epub", "_out.epub")

    try:
        reader = EpubReader()
        node_chunks = reader.read(src_path, _config())

        toc_label_chunk = next(
            c for c in node_chunks
            if c.meta.get("nav_href") == "ch1.xhtml" and c.source_text == "Chapter One"
        )
        cover_label_chunk = next(
            c for c in node_chunks
            if c.meta.get("nav_href") == "ch1.xhtml" and c.source_text == "Cover"
        )
        # The two labels share the same href but MUST have distinct node_path
        # (they live under different <nav> siblings) — otherwise this test
        # can't distinguish which one got patched.
        assert toc_label_chunk.meta["node_path"] != cover_label_chunk.meta["node_path"]

        provider = _FakeProvider()
        chunks = chunk_prose(node_chunks, _config(), provider)

        translated_chunks = []
        for chunk in chunks:
            prose_nodes = chunk.meta.get("prose_nodes", [])
            is_toc_batch = any(
                pn.get("node_path") == toc_label_chunk.meta["node_path"]
                and pn.get("epub_item_href") == toc_label_chunk.meta["epub_item_href"]
                for pn in prose_nodes
            )
            if is_toc_batch:
                segments = chunk.source_text.split("\n\n")
                translated_segments = [
                    "Capítulo Uno" if seg == "Chapter One" else seg
                    for seg in segments
                ]
                translated_chunks.append(
                    chunk.model_copy(
                        update={
                            "translated_text": "\n\n".join(translated_segments),
                            "status": ChunkStatus.DONE,
                        }
                    )
                )
            else:
                translated_chunks.extend(_fake_translate([chunk]))

        writer = EpubWriter()
        writer.write(translated_chunks, src_path, out_path)

        with zipfile.ZipFile(out_path, "r") as zf:
            nav_name = _find_nav_entry(zf)
            raw = zf.read(nav_name).decode("utf-8", errors="replace")

        # The toc label must be translated...
        assert "Capítulo Uno" in raw, f"Expected translated toc label, got: {raw}"
        # ...and it must NOT have replaced the landmarks "Cover" label (which
        # should remain "[ES] Cover" via the fallback fake-translate path, or
        # at minimum must NOT read "Capítulo Uno" instead of "Cover").
        assert "Cover" in raw or "[ES] Cover" in raw, (
            f"Expected landmarks 'Cover' label untouched by the toc patch, got: {raw}"
        )
    finally:
        os.unlink(src_path)
        if os.path.exists(out_path):
            os.unlink(out_path)


def test_nav_ol_li_nesting_and_epub_type_survive_patching() -> None:
    """Nested <ol><li> depth/count and epub:type value survive nav-label patching."""
    chapters = [
        ("ch1.xhtml", _chapter_xhtml("Chapter one body text.")),
        ("ch2.xhtml", _chapter_xhtml("Chapter two body text.")),
    ]
    src_bytes = _make_nav_epub_bytes(_NAV_XHTML_NESTED, chapters)
    src_path = _write_temp_epub(src_bytes)
    out_path = src_path.replace(".epub", "_out.epub")

    try:
        reader = EpubReader()
        node_chunks = reader.read(src_path, _config())
        provider = _FakeProvider()
        chunks = chunk_prose(node_chunks, _config(), provider)
        translated_chunks = _fake_translate(chunks)

        writer = EpubWriter()
        writer.write(translated_chunks, src_path, out_path)

        with zipfile.ZipFile(out_path, "r") as zf:
            nav_name = _find_nav_entry(zf)
            raw = zf.read(nav_name)

        parsed = etree.fromstring(raw)
        body = parsed.find(".//{http://www.w3.org/1999/xhtml}body")
        if body is None:
            body = parsed.find(".//body")
        nav_el = body.find(".//{http://www.w3.org/1999/xhtml}nav")
        if nav_el is None:
            nav_el = body.find(".//nav")

        epub_type = nav_el.get("{http://www.idpf.org/2007/ops}type") or nav_el.get("type")
        assert epub_type == "toc", "Expected epub:type='toc' to survive patching"

        # Depth/count: outer <ol> has 2 <li>, first <li> has a nested <ol><li>.
        outer_ol = nav_el.find("{http://www.w3.org/1999/xhtml}ol")
        if outer_ol is None:
            outer_ol = nav_el.find("ol")
        outer_lis = outer_ol.findall("{http://www.w3.org/1999/xhtml}li")
        if not outer_lis:
            outer_lis = outer_ol.findall("li")
        assert len(outer_lis) == 2, f"Expected 2 top-level <li>, got {len(outer_lis)}"

        nested_ol = outer_lis[0].find("{http://www.w3.org/1999/xhtml}ol")
        if nested_ol is None:
            nested_ol = outer_lis[0].find("ol")
        assert nested_ol is not None, "Expected nested <ol> to survive patching"
        nested_lis = nested_ol.findall("{http://www.w3.org/1999/xhtml}li")
        if not nested_lis:
            nested_lis = nested_ol.findall("li")
        assert len(nested_lis) == 1, f"Expected 1 nested <li>, got {len(nested_lis)}"
    finally:
        os.unlink(src_path)
        if os.path.exists(out_path):
            os.unlink(out_path)


# ---------------------------------------------------------------------------
# WU6-1 / WU6-2: ncx-copy lookup + patch branch, href match (D4).
#
# The nav_label_lookup is built (per D4) from the SAME per-node loop that
# builds flat_patches, BEFORE the ZIP-copy loop — so ncx-copy does not depend
# on nav.xhtml appearing before toc.ncx in ZIP iteration order.
# ---------------------------------------------------------------------------


def _make_nav_and_ncx_epub_bytes(
    nav_xhtml: bytes,
    ncx_xml: bytes,
    chapters: list[tuple[str, bytes]],
) -> bytes:
    """Build an ebooklib EPUB, then swap in BOTH a hand-built nav doc and ncx.

    Mirrors ``_make_nav_epub_bytes`` but also replaces the ``toc.ncx`` entry
    so the fixture can express fragment-bearing ``content src`` and
    unmatched ``navPoint`` shapes the ebooklib API cannot produce directly.
    """
    toc_links = [(fname, f"Label for {fname}", f"uid-{i}") for i, (fname, _) in enumerate(chapters)]
    src_bytes = _make_epub_bytes(chapters, toc_links=toc_links)
    src_bytes = _replace_zip_entry(src_bytes, "EPUB/nav.xhtml", nav_xhtml)
    src_bytes = _replace_zip_entry(src_bytes, "EPUB/toc.ncx", ncx_xml)
    return src_bytes


def _find_ncx_entry(zf: zipfile.ZipFile) -> str:
    names = [n for n in zf.namelist() if n.endswith(".ncx")]
    assert names, f"toc.ncx not found in output; got: {zf.namelist()}"
    return names[0]


def _translate_nav_label(
    chunks: list[Chunk],
    nav_node_path: str,
    nav_item_href: str,
    source_label: str,
    translated_label: str,
) -> list[Chunk]:
    """Translate only the batch containing the nav-label node at nav_node_path;
    fall back to the ordinary `_fake_translate` prefix for every other chunk.
    """
    translated_chunks: list[Chunk] = []
    for chunk in chunks:
        prose_nodes = chunk.meta.get("prose_nodes", [])
        is_target_batch = any(
            pn.get("node_path") == nav_node_path and pn.get("epub_item_href") == nav_item_href
            for pn in prose_nodes
        )
        if is_target_batch:
            segments = chunk.source_text.split("\n\n")
            translated_segments = [
                translated_label if seg == source_label else "[ES] " + seg
                for seg in segments
            ]
            translated_chunks.append(
                chunk.model_copy(
                    update={
                        "translated_text": "\n\n".join(translated_segments),
                        "status": ChunkStatus.DONE,
                    }
                )
            )
        else:
            translated_chunks.extend(_fake_translate([chunk]))
    return translated_chunks


_NCX_SIMPLE = (
    b"<?xml version='1.0' encoding='utf-8'?>"
    b'<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
    b"<head><meta name=\"dtb:uid\" content=\"t\"/></head>"
    b"<docTitle><text>T</text></docTitle>"
    b"<navMap>"
    b'<navPoint id="np1" playOrder="1">'
    b"<navLabel><text>Chapter One</text></navLabel>"
    b'<content src="ch1.xhtml"/>'
    b"</navPoint>"
    b"</navMap>"
    b"</ncx>"
)

_NCX_FRAGMENT = (
    b"<?xml version='1.0' encoding='utf-8'?>"
    b'<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
    b"<head><meta name=\"dtb:uid\" content=\"t\"/></head>"
    b"<docTitle><text>T</text></docTitle>"
    b"<navMap>"
    b'<navPoint id="np1" playOrder="1">'
    b"<navLabel><text>Chapter One</text></navLabel>"
    b'<content src="ch1.xhtml#sec2"/>'
    b"</navPoint>"
    b"</navMap>"
    b"</ncx>"
)


def test_ncx_navlabel_replaced_by_href_match_no_provider_call() -> None:
    """ncx navLabel text is replaced by href match; content src untouched; no extra translate call."""
    chapters = [("ch1.xhtml", _chapter_xhtml("Chapter one body text."))]
    src_bytes = _make_nav_and_ncx_epub_bytes(_NAV_XHTML_SIMPLE, _NCX_SIMPLE, chapters)
    src_path = _write_temp_epub(src_bytes)
    out_path = src_path.replace(".epub", "_out.epub")

    try:
        reader = EpubReader()
        node_chunks = reader.read(src_path, _config())
        nav_label_chunk = next(
            c for c in node_chunks
            if c.meta.get("nav_href") == "ch1.xhtml" and c.source_text == "Chapter One"
        )

        class _CountingProvider(_FakeProvider):
            def __init__(self) -> None:
                self.translate_calls = 0

            def translate(self, system: str, user: str, model: str):  # noqa: ARG002
                self.translate_calls += 1
                raise NotImplementedError("not used — writer never calls translate directly")

        provider = _CountingProvider()
        chunks = chunk_prose(node_chunks, _config(), provider)

        translated_chunks = _translate_nav_label(
            chunks,
            nav_label_chunk.meta["node_path"],
            nav_label_chunk.meta["epub_item_href"],
            "Chapter One",
            "Capítulo Uno",
        )

        writer = EpubWriter()
        writer.write(translated_chunks, src_path, out_path)

        # The writer must NEVER call provider.translate (it only reinserts
        # already-translated text) — assert zero additional calls were made
        # for the ncx-copy step specifically (the writer takes no provider
        # argument at all, so this also proves no such call is even possible).
        assert provider.translate_calls == 0, (
            "EpubWriter must not call TranslationProvider.translate for ncx-copy"
        )

        with zipfile.ZipFile(out_path, "r") as zf:
            ncx_name = _find_ncx_entry(zf)
            raw = zf.read(ncx_name).decode("utf-8", errors="replace")

        assert "Capítulo Uno" in raw, f"Expected translated ncx navLabel, got: {raw}"
        assert "Chapter One" not in raw, (
            f"Expected original label replaced, not left alongside translation: {raw}"
        )
        assert 'src="ch1.xhtml"' in raw, f"Expected content src unchanged, got: {raw}"
    finally:
        os.unlink(src_path)
        if os.path.exists(out_path):
            os.unlink(out_path)


def test_ncx_fragment_bearing_href_matches_fragment_less_nav_href() -> None:
    """content src='ch1.xhtml#sec2' normalizes and matches nav_href='ch1.xhtml'."""
    chapters = [("ch1.xhtml", _chapter_xhtml("Chapter one body text."))]
    src_bytes = _make_nav_and_ncx_epub_bytes(_NAV_XHTML_SIMPLE, _NCX_FRAGMENT, chapters)
    src_path = _write_temp_epub(src_bytes)
    out_path = src_path.replace(".epub", "_out.epub")

    try:
        reader = EpubReader()
        node_chunks = reader.read(src_path, _config())
        nav_label_chunk = next(
            c for c in node_chunks
            if c.meta.get("nav_href") == "ch1.xhtml" and c.source_text == "Chapter One"
        )

        provider = _FakeProvider()
        chunks = chunk_prose(node_chunks, _config(), provider)
        translated_chunks = _translate_nav_label(
            chunks,
            nav_label_chunk.meta["node_path"],
            nav_label_chunk.meta["epub_item_href"],
            "Chapter One",
            "Capítulo Uno",
        )

        writer = EpubWriter()
        writer.write(translated_chunks, src_path, out_path)

        with zipfile.ZipFile(out_path, "r") as zf:
            ncx_name = _find_ncx_entry(zf)
            raw = zf.read(ncx_name).decode("utf-8", errors="replace")

        assert "Capítulo Uno" in raw, (
            f"Expected fragment-bearing content src to match fragment-less nav_href, got: {raw}"
        )
        assert 'src="ch1.xhtml#sec2"' in raw, f"Expected content src unchanged, got: {raw}"
    finally:
        os.unlink(src_path)
        if os.path.exists(out_path):
            os.unlink(out_path)


# ---------------------------------------------------------------------------
# WU7-1: ncx-copy defensive fallback paths — unmatched navPoint, empty
# lookup, ncx-only-book no-op (D4).
# ---------------------------------------------------------------------------

_NCX_UNMATCHED = (
    b"<?xml version='1.0' encoding='utf-8'?>"
    b'<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
    b"<head><meta name=\"dtb:uid\" content=\"t\"/></head>"
    b"<docTitle><text>T</text></docTitle>"
    b"<navMap>"
    b'<navPoint id="np1" playOrder="1">'
    b"<navLabel><text>Appendix</text></navLabel>"
    b'<content src="appendix.xhtml"/>'
    b"</navPoint>"
    b"</navMap>"
    b"</ncx>"
)


def test_ncx_unmatched_navpoint_left_untranslated_no_crash() -> None:
    """A navPoint whose content src has no lookup entry keeps its source label."""
    chapters = [("ch1.xhtml", _chapter_xhtml("Chapter one body text."))]
    src_bytes = _make_nav_and_ncx_epub_bytes(_NAV_XHTML_SIMPLE, _NCX_UNMATCHED, chapters)
    src_path = _write_temp_epub(src_bytes)
    out_path = src_path.replace(".epub", "_out.epub")

    try:
        reader = EpubReader()
        node_chunks = reader.read(src_path, _config())
        provider = _FakeProvider()
        chunks = chunk_prose(node_chunks, _config(), provider)
        translated_chunks = _fake_translate(chunks)

        writer = EpubWriter()
        # Must not raise.
        writer.write(translated_chunks, src_path, out_path)

        with zipfile.ZipFile(out_path, "r") as zf:
            ncx_name = _find_ncx_entry(zf)
            raw = zf.read(ncx_name).decode("utf-8", errors="replace")

        assert "Appendix" in raw, (
            f"Expected unmatched navPoint's original label preserved, got: {raw}"
        )
    finally:
        os.unlink(src_path)
        if os.path.exists(out_path):
            os.unlink(out_path)


def test_book_with_no_ncx_item_is_unaffected() -> None:
    """An EPUB with no .ncx entry: ncx-copy step no-ops, nav translation proceeds."""
    chapters = [("ch1.xhtml", _chapter_xhtml("Chapter one body text."))]
    src_bytes = _make_nav_epub_bytes(_NAV_XHTML_SIMPLE, chapters)

    # Remove the .ncx entry AND its OPF manifest/spine references entirely,
    # to simulate an EPUB3-only, nav-doc-only book (ebooklib's read_epub
    # raises if the manifest references a ZIP entry that doesn't exist).
    buf_in = io.BytesIO(src_bytes)
    buf_out = io.BytesIO()
    with (
        zipfile.ZipFile(buf_in, "r") as zin,
        zipfile.ZipFile(buf_out, "w", zipfile.ZIP_DEFLATED) as zout,
    ):
        for item in zin.infolist():
            if item.filename.endswith(".ncx"):
                continue
            data = zin.read(item.filename)
            if item.filename.endswith("content.opf"):
                text = data.decode("utf-8")
                text = text.replace(
                    '<item href="toc.ncx" id="ncx" media-type="application/x-dtbncx+xml"/>',
                    "",
                )
                text = text.replace(' toc="ncx"', "")
                data = text.encode("utf-8")
            zout.writestr(item, data)
    src_bytes = buf_out.getvalue()

    src_path = _write_temp_epub(src_bytes)
    out_path = src_path.replace(".epub", "_out.epub")

    try:
        reader = EpubReader()
        node_chunks = reader.read(src_path, _config())
        nav_label_chunk = next(
            c for c in node_chunks
            if c.meta.get("nav_href") == "ch1.xhtml" and c.source_text == "Chapter One"
        )

        provider = _FakeProvider()
        chunks = chunk_prose(node_chunks, _config(), provider)
        translated_chunks = _translate_nav_label(
            chunks,
            nav_label_chunk.meta["node_path"],
            nav_label_chunk.meta["epub_item_href"],
            "Chapter One",
            "Capítulo Uno",
        )

        writer = EpubWriter()
        # Must not raise despite no .ncx entry present.
        writer.write(translated_chunks, src_path, out_path)

        with zipfile.ZipFile(out_path, "r") as zf:
            names = zf.namelist()
            assert not any(n.endswith(".ncx") for n in names), (
                f"Expected no .ncx entry in output, got: {names}"
            )
            nav_name = _find_nav_entry(zf)
            raw = zf.read(nav_name).decode("utf-8", errors="replace")

        assert "Capítulo Uno" in raw, (
            f"Expected nav-doc translation to proceed even without a .ncx entry, got: {raw}"
        )
    finally:
        os.unlink(src_path)
        if os.path.exists(out_path):
            os.unlink(out_path)


def test_ncx_but_no_nav_doc_left_untranslated_no_crash() -> None:
    """EPUB2-style book: toc.ncx present, no EpubNav item — ncx stays untranslated."""
    epub_bytes = _make_epub_ncx_only_no_nav_writer()
    src_path = _write_temp_epub(epub_bytes)
    out_path = src_path.replace(".epub", "_out.epub")

    try:
        reader = EpubReader()
        node_chunks = reader.read(src_path, _config())
        nav_label_chunks = [
            c for c in node_chunks
            if c.meta.get("kind") == "nav-label" or c.meta.get("nav_href") is not None
        ]
        assert not nav_label_chunks, (
            f"Expected no nav-label chunks (no EpubNav item), got: {nav_label_chunks}"
        )

        provider = _FakeProvider()
        chunks = chunk_prose(node_chunks, _config(), provider)
        translated_chunks = _fake_translate(chunks)

        writer = EpubWriter()
        # Must not raise.
        writer.write(translated_chunks, src_path, out_path)

        with zipfile.ZipFile(out_path, "r") as zf:
            ncx_name = _find_ncx_entry(zf)
            raw = zf.read(ncx_name).decode("utf-8", errors="replace")

        assert "Chapter 1" in raw, (
            f"Expected ncx label to remain untranslated (no nav doc to copy from), got: {raw}"
        )
    finally:
        os.unlink(src_path)
        if os.path.exists(out_path):
            os.unlink(out_path)


def _make_epub_ncx_only_no_nav_writer() -> bytes:
    """Build a minimal EPUB2-style book with toc.ncx but NO EpubNav item.

    Mirrors ``tests/integration/test_epub_reader.py``'s
    ``_make_epub_ncx_only_no_nav`` (same manual-ZIP construction — reused
    here rather than imported to keep this test module import-independent
    of the reader test module, matching the existing convention where both
    test files build their own fixtures).
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
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
            "<dc:identifier id=\"uid\">ncx-only-writer-test</dc:identifier>"
            "<dc:title>NCX Only Writer Test</dc:title>"
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
            "<head><meta name=\"dtb:uid\" content=\"ncx-only-writer-test\"/></head>"
            "<docTitle><text>NCX Only Writer Test</text></docTitle>"
            "<navMap>"
            '<navPoint id="np1" playOrder="1">'
            "<navLabel><text>Chapter 1</text></navLabel>"
            '<content src="ch1.xhtml"/>'
            "</navPoint>"
            "</navMap>"
            "</ncx>",
        )
        zf.writestr(
            "OEBPS/ch1.xhtml",
            "<?xml version='1.0' encoding='utf-8'?>"
            "<html xmlns='http://www.w3.org/1999/xhtml'>"
            "<head><title>Ch1</title></head>"
            "<body><p>Chapter one paragraph.</p></body>"
            "</html>",
        )
    return buf.getvalue()
