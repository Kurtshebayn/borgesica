"""EPUB reader adapter (M2-1).

Parses an EPUB file into one Chunk per text node (paragraph/heading) using
the ``ebooklib`` and ``lxml`` libraries.

Each Chunk carries:
  - index: 0-based sequential index across all content documents
  - source_text: text content of the node (inline tags preserved)
  - meta: {
        "epub_item_href": str,   # relative href of the XHTML document in the EPUB
        "node_path": str,        # XPath-like path of the element inside <body>
        "chapter_index": int,    # 0-based per spine document
    }

Nav document (EPUB3 navigation doc, ``isinstance(item, epub.EpubNav)``):
  Handled by a dedicated nav walk (``_extract_nav_chunks``) instead of the
  general body-chapter traversal. Emits one Chunk per ``<a>``/unlinked
  ``<span>``/section heading inside ``<nav epub:type="toc">`` or
  ``<nav epub:type="landmarks">`` (``page-list`` excluded — its labels are
  page numbers, not prose). Nav-label chunks carry two additional meta keys:
        "nav_href": str | None,  # the <a>'s href; None for headings/spans
        "kind": "nav-label",     # marks the chunk for orchestrator bypass
  and share an isolated ``chapter_index = max(body chapter_index) + 1``,
  appended AFTER all body chapters in the returned chunk list.

DRM detection:
  Presence of ``META-INF/encryption.xml`` inside the EPUB ZIP → raises
  ``UnsupportedFormatError`` before any chunk is produced.

Invalid EPUB:
  If the file is not a valid ZIP or ebooklib raises on read → raises
  ``UnsupportedFormatError`` with a "not a valid EPUB" message.

Images:
  ``<img>`` elements are never emitted as chunks.  Only text content of
  block-level text elements (``<p>``, ``<h1>``–``<h6>``, ``<li>``, ``<td>``,
  ``<th>``, ``<caption>``, ``<dt>``, ``<dd>``, ``<blockquote>``, ``<pre>``,
  ``<figcaption>``, ``<label>``) is extracted.

Dependency rule: only stdlib + domain models + ebooklib + lxml allowed here.
``ebooklib`` and ``lxml`` are ADAPTER-only imports — they MUST NOT be imported
anywhere under ``borgesica/domain/``.
"""
from __future__ import annotations

import zipfile
from typing import TYPE_CHECKING

import ebooklib
from ebooklib import epub
from lxml import etree

from borgesica.domain.errors import UnsupportedFormatError
from borgesica.domain.models import Chunk, ChunkStatus, JobConfig

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# XHTML namespace — both with and without are common in EPUBs
_XHTML_NS = "http://www.w3.org/1999/xhtml"

# Block-level text elements whose text content should be extracted as chunks.
# Structural wrappers (div, section, article, header, footer, nav, aside,
# figure, table, ul, ol, dl) are traversed but NOT emitted.
_TEXT_ELEMENTS = frozenset({
    "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "dt", "dd",
    "td", "th", "caption",
    "blockquote", "pre",
    "figcaption", "label",
})

# Elements whose *entire subtree* should be skipped (no text extraction).
_SKIP_ELEMENTS = frozenset({"img", "image", "svg", "script", "style", "head", "nav"})

# EPUB3 nav ``epub:type`` values whose <a>/<span>/heading labels are
# translatable prose. ``page-list`` is deliberately excluded — its <a> labels
# are page numbers, not prose.
_NAV_TRANSLATABLE_TYPES = frozenset({"toc", "landmarks"})

# ``epub:type`` attribute, namespace-qualified (EPUB namespace).
_EPUB_NS = "http://www.idpf.org/2007/ops"
_EPUB_TYPE_ATTR = f"{{{_EPUB_NS}}}type"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_drm(path: str) -> None:
    """Raise UnsupportedFormatError if the EPUB contains META-INF/encryption.xml."""
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = zf.namelist()
    except zipfile.BadZipFile as exc:
        raise UnsupportedFormatError(
            path=path,
            reason="not a valid EPUB: file is not a valid ZIP archive",
        ) from exc
    except OSError as exc:
        raise UnsupportedFormatError(
            path=path,
            reason=f"not a valid EPUB: cannot open file ({exc})",
        ) from exc

    if any(n.lower() == "meta-inf/encryption.xml" for n in names):
        raise UnsupportedFormatError(
            path=path,
            reason="DRM-protected EPUB: META-INF/encryption.xml detected — "
            "remove DRM before translating",
        )


def _local_tag(element: etree._Element) -> str:  # type: ignore[type-arg]
    """Return the local tag name (strip namespace prefix)."""
    tag = element.tag
    if isinstance(tag, str) and "}" in tag:
        return tag.split("}", 1)[1]
    return tag if isinstance(tag, str) else ""


def _node_path(element: etree._Element, body: etree._Element) -> str:  # type: ignore[type-arg]
    """Build a simple positional XPath-like path from ``body`` to ``element``.

    Example: ``/p[1]/em[0]``
    """
    parts: list[str] = []
    current = element
    while current is not None and current is not body:
        parent = current.getparent()
        if parent is None:
            break
        local = _local_tag(current)
        # Count siblings with the same tag
        same_tag_siblings = [c for c in parent if c.tag == current.tag]
        idx = same_tag_siblings.index(current)
        parts.append(f"{local}[{idx}]")
        current = parent
    return "/" + "/".join(reversed(parts))


def _serialize_element_text(element: etree._Element) -> str:  # type: ignore[type-arg]
    """Return the full serialized text of an element, including inline child tags.

    For a paragraph like ``<p>Hello <em>world</em>!</p>`` this returns
    ``"Hello <em>world</em>!"``.
    """
    # Serialize inner XML and strip the outer element tags
    raw = etree.tostring(element, encoding="unicode", method="xml")
    # raw looks like: <p xmlns="...">Hello <em>world</em>!</p>
    # Strip the outer tag — find first '>' and last '</'
    start = raw.find(">")
    end = raw.rfind("</")
    if start == -1:
        return ""
    if end == -1 or end <= start:
        # self-closing element with no content
        return ""
    inner = raw[start + 1 : end]
    # Strip namespace declarations introduced by lxml serialization
    # e.g. <em xmlns="http://www.w3.org/1999/xhtml"> → <em>
    import re
    inner = re.sub(r'\s+xmlns(?::\w+)?="[^"]*"', "", inner)
    return inner.strip()


def _extract_chunks_from_item(
    item: epub.EpubHtml,
    start_index: int,
    chapter_index: int = 0,
) -> list[Chunk]:
    """Parse one XHTML content document and return Chunks for each text node.

    Traverses only the ``<body>`` element.  Skips ``<img>``, ``<svg>``,
    ``<script>``, ``<style>``, and ``<nav>`` subtrees entirely.
    Only elements in ``_TEXT_ELEMENTS`` produce a Chunk.

    Args:
        item:          The XHTML spine document to extract from.
        start_index:   The 0-based global chunk index to start counting from.
        chapter_index: The 0-based index of this spine document in the reading
                       order.  Every Chunk produced by this call shares the same
                       ``chapter_index`` so ``chunk_prose`` can enforce chapter
                       boundaries without knowing which ``epub_item_href`` belongs
                       to which position in the spine.

    Encoding note (M4-5 / S-M2-2):
        We read ``item.content`` (raw bytes as stored in the ZIP) rather than
        ``item.get_content()``.  ebooklib's ``get_content()`` calls
        ``parse_html_string(self.content)`` with a hard-coded
        ``html.HTMLParser(encoding='utf-8')`` that misinterprets non-UTF-8
        chapters (e.g. ISO-8859-1 / windows-1252), then re-serialises the
        tree as UTF-8 with the wrong bytes in place.  lxml's XML parser
        (``etree.fromstring``) honours the ``<?xml ... encoding='...'>``
        declaration when given raw bytes, so passing ``item.content`` directly
        is both correct and cheaper (no intermediate re-encoding round-trip).
        The HTML-parser fallback is retained for chapters that lack a valid
        XML declaration (EPUB 2 HTML4 doctype, etc.).
    """
    # Use item.content (raw bytes from ZIP) instead of item.get_content().
    # item.get_content() always re-encodes via lxml.html.HTMLParser(encoding='utf-8')
    # which corrupts non-UTF-8 content.  Raw bytes let lxml honour the declared encoding.
    raw_content: bytes = getattr(item, "content", None) or b""
    if not raw_content:
        return []

    # Parse; tolerate both namespace and namespace-free XHTML
    try:
        tree = etree.fromstring(raw_content)
    except etree.XMLSyntaxError:
        # Try with HTML parser as fallback (some EPUBs use HTML4 doctype)
        try:
            tree = etree.fromstring(raw_content, parser=etree.HTMLParser())
        except Exception:  # noqa: BLE001
            return []

    # Locate <body> — with or without XHTML namespace
    body = tree.find(f"{{{_XHTML_NS}}}body")
    if body is None:
        body = tree.find(".//body")
    if body is None:
        # Some parsers flatten; try the root itself as body
        body = tree

    chunks: list[Chunk] = []
    chunk_idx = start_index

    def _walk(node: etree._Element) -> None:  # type: ignore[type-arg]
        nonlocal chunk_idx
        local = _local_tag(node)

        if local in _SKIP_ELEMENTS:
            return  # skip entire subtree

        if local in _TEXT_ELEMENTS:
            text = _serialize_element_text(node)
            if text:
                chunks.append(
                    Chunk(
                        index=chunk_idx,
                        source_text=text,
                        status=ChunkStatus.PENDING,
                        meta={
                            "epub_item_href": item.get_name(),
                            "node_path": _node_path(node, body),
                            "chapter_index": chapter_index,
                        },
                    )
                )
                chunk_idx += 1
            # Still walk children so nested elements (e.g. <li> inside <ul>)
            # inside block containers are extracted.
            # NOTE: we do NOT recurse into text elements themselves —
            # their full serialized text already captures inner tags.
            return

        # Structural wrapper — recurse into children
        for child in node:
            _walk(child)

    _walk(body)
    return chunks


def _nav_epub_type(element: etree._Element) -> str | None:  # type: ignore[type-arg]
    """Return the ``epub:type`` attribute value of ``element``, or ``None``.

    Namespace-agnostic on the ATTRIBUTE NAME lookup: tries the namespaced
    ``{http://www.idpf.org/2007/ops}type`` key first (the normal case for
    ebooklib-generated and most real-world nav docs), then falls back to a
    bare ``type`` attribute for documents parsed without the epub namespace
    registered.
    """
    value = element.get(_EPUB_TYPE_ATTR)
    if value is not None:
        return value
    return element.get("type")


def _extract_nav_chunks(
    item: epub.EpubHtml,
    start_index: int,
    nav_chapter_index: int,
) -> list[Chunk]:
    """Parse the EPUB3 nav document and return Chunks for its translatable labels.

    Selects ``<nav>`` elements under ``<body>`` by ``epub:type`` attribute
    VALUE — ``{"toc", "landmarks"}`` only, excluding ``page-list`` (its <a>
    labels are page numbers, not prose). Within each selected ``<nav>``,
    emits one Chunk per:
      - ``<a>`` descendant — ``node_path`` at the ``<a>`` itself,
        ``nav_href`` = its ``href`` attribute value.
      - ``<span>`` descendant NOT inside an ``<a>`` (unlinked heading row) —
        ``node_path`` at the ``<span>``, ``nav_href = None``.
      - the nav's own ``<h1>``/``<h2>`` section heading — ``node_path`` at
        the heading, ``nav_href = None``.

    All emitted chunks share ``chapter_index = nav_chapter_index`` (an
    isolated bucket, distinct from every body chapter's index) and carry
    ``meta["kind"] = "nav-label"`` so the orchestrator can bypass reflective
    mode for them regardless of ``config.quality_mode``.

    Args:
        item:               The ``EpubNav`` spine item.
        start_index:        0-based global chunk index to start counting from.
        nav_chapter_index:  The isolated ``chapter_index`` bucket for nav
                             labels — ``max(body chapter_index) + 1``.

    Returns:
        A list of Chunks (possibly empty, e.g. if the nav doc has no
        toc/landmarks sections).
    """
    raw_content: bytes = getattr(item, "content", None) or b""
    if not raw_content:
        return []

    # Same encoding-safe parse path as _extract_chunks_from_item.
    try:
        tree = etree.fromstring(raw_content)
    except etree.XMLSyntaxError:
        try:
            tree = etree.fromstring(raw_content, parser=etree.HTMLParser())
        except Exception:  # noqa: BLE001
            return []

    body = tree.find(f"{{{_XHTML_NS}}}body")
    if body is None:
        body = tree.find(".//body")
    if body is None:
        body = tree

    chunks: list[Chunk] = []
    chunk_idx = start_index
    item_href = item.get_name()

    def _emit(node: etree._Element, nav_href: str | None) -> None:  # type: ignore[type-arg]
        nonlocal chunk_idx
        text = _serialize_element_text(node)
        if not text:
            return
        chunks.append(
            Chunk(
                index=chunk_idx,
                source_text=text,
                status=ChunkStatus.PENDING,
                meta={
                    "epub_item_href": item_href,
                    "node_path": _node_path(node, body),
                    "chapter_index": nav_chapter_index,
                    "nav_href": nav_href,
                    "kind": "nav-label",
                },
            )
        )
        chunk_idx += 1

    # Select <nav> elements ANYWHERE under <body> by epub:type value.
    # Descendant search, not direct children: real nav docs commonly wrap the
    # <nav> in a <section> (live-book regression: Strength_of_the_Few's
    # <body epub:type="frontmatter"><section epub:type="toc"><nav ...> shape
    # produced zero nav-label chunks with a direct-children scan).
    nav_elements = [
        el for el in body.iter()
        if _local_tag(el) == "nav" and _nav_epub_type(el) in _NAV_TRANSLATABLE_TYPES
    ]

    for nav_el in nav_elements:
        # The nav's own <h1>/<h2> section heading (namespace-agnostic: try
        # both the XHTML-namespaced and bare tag name, since nav docs are
        # commonly parsed with the default XHTML namespace registered).
        for heading_tag in ("h1", "h2"):
            heading = nav_el.find(f"{{{_XHTML_NS}}}{heading_tag}")
            if heading is None:
                heading = nav_el.find(heading_tag)
            if heading is not None:
                _emit(heading, None)
                break  # a nav section has at most one heading

        # <a> descendants (linked labels) and un-linked <span> heading rows.
        for descendant in nav_el.iter():
            local = _local_tag(descendant)
            if local == "a":
                _emit(descendant, descendant.get("href"))
            elif local == "span":
                # Only un-linked spans: skip if an <a> ancestor exists.
                if any(_local_tag(anc) == "a" for anc in descendant.iterancestors()):
                    continue
                _emit(descendant, None)

    return chunks


# ---------------------------------------------------------------------------
# Public adapter class
# ---------------------------------------------------------------------------


class EpubReader:
    """DocumentReader implementation for .epub files.

    Implements ``borgesica.domain.ports.DocumentReader`` without importing it
    (structural sub-typing via duck-typing satisfies the Protocol at runtime).
    """

    def read(self, path: str, config: JobConfig) -> list[Chunk]:  # noqa: ARG002
        """Parse an EPUB file into a flat list of Chunk objects.

        Chunks are emitted in spine order.  Each chunk corresponds to one
        text-bearing element (paragraph, heading, etc.) inside a content
        document's ``<body>``.

        ``config`` is accepted to satisfy the ``DocumentReader`` Protocol but
        currently unused by the reader (font/encoding decisions belong to the
        writer).

        Raises:
            UnsupportedFormatError: if the file is not a valid ZIP, is not a
                valid EPUB, or contains DRM (``META-INF/encryption.xml``).
        """
        # Step 1: DRM and ZIP validity check (fast, before ebooklib)
        _check_drm(path)

        # Step 2: Parse the EPUB
        try:
            book = epub.read_epub(path, options={"ignore_ncx": True})
        except epub.EpubException as exc:
            raise UnsupportedFormatError(
                path=path,
                reason=f"not a valid EPUB: {exc}",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise UnsupportedFormatError(
                path=path,
                reason=f"not a valid EPUB: unexpected error reading file ({exc})",
            ) from exc

        # Step 3: Traverse spine in OPF reading order
        chunks: list[Chunk] = []
        seen_ids: set[str] = set()
        chapter_index = 0

        # Collect NAV documents from the MANIFEST, not the spine: most
        # professionally produced EPUBs declare the nav doc as a manifest-only
        # item (properties="nav") that never appears in the reading order, so
        # a spine-driven collection walks right past it (live-book regression:
        # Strength_of_the_Few.epub — navigation menu stayed untranslated).
        # ebooklib authoritatively classifies EpubNav via the OPF
        # properties="nav" attribute, so isinstance is sufficient on its own —
        # a filename-substring pre-check would miss a nav doc named without
        # "nav" in it (e.g. contents.xhtml).
        nav_items: list[epub.EpubHtml] = [
            item for item in book.get_items() if isinstance(item, epub.EpubNav)
        ]

        for spine_id, _linear in book.spine:
            item = book.get_item_with_id(spine_id)
            if item is None:
                continue
            if item.get_type() != ebooklib.ITEM_DOCUMENT:
                continue
            if spine_id in seen_ids:
                continue
            seen_ids.add(spine_id)

            # NAV documents were already collected from the manifest above —
            # skip them here so a spine-listed nav doc is neither traversed as
            # a body chapter nor walked twice. Nav chunks are appended AFTER
            # all body chapters (see below), once every body chapter_index is
            # known.
            if isinstance(item, epub.EpubNav):
                continue

            new_chunks = _extract_chunks_from_item(
                item,
                start_index=len(chunks),
                chapter_index=chapter_index,
            )
            chunks.extend(new_chunks)
            chapter_index += 1

        # Step 4: Nav walk — isolated chapter_index bucket = max(body)+1,
        # appended LAST so nav labels see the fullest glossary/summary
        # (ordering invariant #2, design.md).
        nav_chapter_index = chapter_index  # max body chapter_index seen + 1
        for nav_item in nav_items:
            nav_chunks = _extract_nav_chunks(
                nav_item,
                start_index=len(chunks),
                nav_chapter_index=nav_chapter_index,
            )
            chunks.extend(nav_chunks)

        return chunks
