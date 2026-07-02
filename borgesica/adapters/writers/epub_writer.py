"""EPUB writer adapter (M2-3).

Reinserts translated text chunks into a copy of the source EPUB, producing a
valid translated EPUB file.

Reinsertion contract (M2-2R):
  Each translated chunk carries:
    - chunk.translated_text: the translated content
    - chunk.meta["prose_nodes"]: list[{"epub_item_href": str, "node_path": str}]
      one entry per "\\n\\n"-separated segment of translated_text

  For each chunk:
    1. Split translated_text on "\\n\\n" → segments.
    2. Map segment[i] → prose_nodes[i]["node_path"] in prose_nodes[i]["epub_item_href"].
    3. Consecutive segments sharing the SAME (epub_item_href, node_path) are
       concatenated back into that single node (hard-split fragments).
    4. Each segment is parsed as an HTML/XHTML fragment and set as real child
       elements (NOT escaped text) so that <em>, <strong>, etc. survive.

Atomic write:
  Writes to ``out_path + ".tmp"`` (in the same directory as out_path), then
  calls ``os.replace(tmp, out_path)`` on success.  On ANY exception the tmp
  file is removed and out_path is left untouched.

Dependency rule:
  ebooklib and lxml are ADAPTER-only imports.  Nothing in borgesica/domain/
  imports this module.
"""
from __future__ import annotations

import logging
import os
import zipfile
from collections import defaultdict

from lxml import etree

from borgesica.adapters.readers.epub_reader import _local_tag
from borgesica.domain.models import Chunk

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_XHTML_NS = "http://www.w3.org/1999/xhtml"

# HTML5 void elements (self-closing in HTML but must have explicit close in XHTML)
_VOID_ELEMENTS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})


# ---------------------------------------------------------------------------
# Node location helper (inverse of epub_reader._node_path)
# ---------------------------------------------------------------------------


def _find_node(
    body: etree._Element,  # type: ignore[type-arg]
    node_path: str,
) -> etree._Element | None:  # type: ignore[type-arg]
    """Locate the element at ``node_path`` beneath ``body``.

    ``node_path`` is the positional XPath-like string produced by
    ``epub_reader._node_path``, e.g. ``/p[0]`` or ``/div[1]/p[2]``.

    The path is resolved the SAME way EpubReader produced it — by counting
    siblings with the same *local* tag name — so reader and writer agree on
    which element a node_path refers to.

    Returns ``None`` if the path cannot be resolved (defensive: the writer
    must not crash on a bad path, it should skip silently and log a warning).
    """
    if not node_path or node_path == "/":
        return body

    # Strip leading "/" and split into steps.
    # Each step looks like "tag[idx]" or "tag[idx]/nextStep".
    steps = [s for s in node_path.lstrip("/").split("/") if s]
    current = body

    for step in steps:
        # Parse "tagname[index]"
        if "[" not in step or not step.endswith("]"):
            logger.warning("EpubWriter: malformed node_path step %r — skipping", step)
            return None
        bracket = step.index("[")
        tag_name = step[:bracket]
        try:
            idx = int(step[bracket + 1 : -1])
        except ValueError:
            logger.warning("EpubWriter: non-integer index in node_path step %r — skipping", step)
            return None

        # Find children whose *local* tag matches (strip namespace if present)
        matching = [c for c in current if _local_tag(c) == tag_name]
        if idx >= len(matching):
            logger.warning(
                "EpubWriter: node_path step %r index %d out of range "
                "(only %d %r children) — skipping",
                step, idx, len(matching), tag_name,
            )
            return None
        current = matching[idx]

    return current


# ---------------------------------------------------------------------------
# Fragment parser
# ---------------------------------------------------------------------------


def _set_element_content(element: etree._Element, html_fragment: str) -> None:  # type: ignore[type-arg]
    """Replace *element*'s inner content with *html_fragment*.

    *html_fragment* may contain inline tags like <em>, <strong>, <a href=...>,
    etc.  These are parsed as REAL child elements (not escaped text).

    Strategy:
      1. Wrap fragment in a dummy <span> and parse with lxml's HTML parser.
      2. Strip any namespace declarations introduced by lxml.
      3. Clear the element and copy the span's children (and tail) into it.

    This ensures <em>…</em> round-trips as a real tag, not &lt;em&gt;.
    """
    # Clear existing children and text
    element.text = None
    for child in list(element):
        element.remove(child)

    if not html_fragment.strip():
        return

    # Wrap in a known root to give lxml a valid HTML document fragment
    wrapped = f"<span>{html_fragment}</span>"
    try:
        # Parse with lxml's HTML parser (tolerant of inline HTML fragments)
        parsed = etree.fromstring(wrapped, parser=etree.HTMLParser())
        # After HTML parse: html/body/span is the shape
        span = parsed.find(".//span")
        if span is None:
            # Fallback: just set text
            element.text = html_fragment
            return
    except Exception:  # noqa: BLE001
        # Last resort: treat as plain text
        element.text = html_fragment
        return

    # Copy span.text and span's children into element, stripping namespaces
    element.text = span.text

    for child in list(span):
        _copy_element_no_ns(child, element)


def _copy_element_no_ns(
    src: etree._Element,  # type: ignore[type-arg]
    parent: etree._Element,  # type: ignore[type-arg]
) -> None:
    """Recursively copy *src* into *parent*, stripping lxml HTML-parser namespaces.

    lxml's HTML parser tags elements with the XHTML namespace
    (``http://www.w3.org/1999/xhtml``) which can produce ugly output.  We
    strip the namespace from the local tag name so that the output XHTML
    contains plain ``<em>`` rather than ``<ns0:em xmlns:ns0="..."/>``.

    Attribute names get the same treatment. lxml's HTMLParser does NOT
    resolve namespace prefixes on attributes the way it does for element
    tags — a prefixed attribute like ``epub:type`` survives parsing as the
    literal string ``"epub:type"`` (no Clark-notation ``{uri}`` wrapping).
    XML's ``Element.set()`` rejects any name containing ``:`` that is not a
    real Clark-notation namespace, so passing it through as-is raises
    ``ValueError: Invalid attribute name 'epub:type'``.  We therefore strip
    BOTH forms: a Clark-notation ``{uri}`` prefix (if lxml ever does resolve
    it) and a literal ``prefix:`` prefix (the common case for HTML-parsed
    fragments). ``xmlns*`` declarations are dropped entirely — they are
    namespace declarations, not real content attributes, and are already
    implied by the document's root namespace.
    """
    local = _local_tag(src)
    # Re-create the element without namespace
    new_el = etree.SubElement(parent, local)

    # Copy attributes (skip xmlns-style; strip Clark-notation or literal
    # "prefix:" namespace prefixes from the remaining attribute names).
    for attr, val in src.attrib.items():
        if "}" in attr:
            local_attr = attr.split("}", 1)[1]
        elif ":" in attr:
            local_attr = attr.split(":", 1)[1]
        else:
            local_attr = attr
        if not local_attr.startswith("xmlns"):
            new_el.set(local_attr, val)

    new_el.text = src.text
    new_el.tail = src.tail

    for child in src:
        _copy_element_no_ns(child, new_el)


# ---------------------------------------------------------------------------
# XHTML document patcher
# ---------------------------------------------------------------------------


def _patch_xhtml_document(
    raw_content: bytes,
    patches: dict[str, str],
) -> bytes:
    """Apply *patches* to the XHTML document in *raw_content*.

    Args:
        raw_content: Raw bytes of an XHTML spine document.
        patches:     Mapping of ``node_path → translated_text_segment``.
                     Multiple segments sharing the same node_path have already
                     been concatenated by the caller.

    Returns:
        Patched XHTML bytes.  If parsing fails or the document cannot be
        modified, returns the ORIGINAL bytes unchanged (defensive).
    """
    if not patches:
        return raw_content

    try:
        # Parse preserving the original encoding declaration
        parser = etree.XMLParser(recover=True, remove_blank_text=False)
        try:
            tree = etree.fromstring(raw_content, parser=parser)
        except etree.XMLSyntaxError:
            tree = etree.fromstring(raw_content, parser=etree.HTMLParser())
    except Exception:  # noqa: BLE001
        logger.warning("EpubWriter: failed to parse XHTML document — leaving untouched")
        return raw_content

    # Locate <body>
    body = tree.find(f"{{{_XHTML_NS}}}body")
    if body is None:
        body = tree.find(".//body")
    if body is None:
        body = tree  # last resort

    for node_path, translated_text in patches.items():
        node = _find_node(body, node_path)
        if node is None:
            logger.warning(
                "EpubWriter: could not find node at path %r — skipping patch",
                node_path,
            )
            continue

        _set_element_content(node, translated_text)

    # Re-serialize preserving XML declaration
    try:
        result = etree.tostring(
            tree,
            encoding="utf-8",
            xml_declaration=True,
            pretty_print=False,
        )
        return result
    except Exception:  # noqa: BLE001
        logger.warning("EpubWriter: failed to serialize patched XHTML — returning original")
        return raw_content


# ---------------------------------------------------------------------------
# Public EpubWriter
# ---------------------------------------------------------------------------


class EpubWriter:
    """DocumentWriter implementation for .epub files.

    Implements ``borgesica.domain.ports.DocumentWriter`` via duck-typing.
    """

    def write(self, chunks: list[Chunk], src_path: str, out_path: str) -> None:
        """Reinsert translated chunks into a copy of *src_path*, write to *out_path*.

        Atomic write: writes to ``out_path + ".tmp"`` first, then renames.
        On failure the tmp is removed and out_path is left untouched.

        Args:
            chunks:   Translated Chunk objects (with translated_text set).
                      Chunks without ``meta["prose_nodes"]`` (e.g. SRT-style
                      chunks) are ignored gracefully.
            src_path: Path to the source EPUB.
            out_path: Destination path for the translated EPUB.
        """
        # Place tmp in the same directory as out_path for same-filesystem rename
        tmp_path = out_path + ".tmp"

        try:
            self._do_write(chunks, src_path, tmp_path)
            os.replace(tmp_path, out_path)
        except Exception:
            # Clean up tmp; propagate the original exception
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass  # best-effort cleanup; do not shadow the original error
            raise

    def _do_write(self, chunks: list[Chunk], src_path: str, tmp_path: str) -> None:
        """Internal: perform the actual write to *tmp_path*.

        Separated from ``write`` so tests can subclass and inject failures
        at precise points (after _do_write but before os.replace).
        """
        # -------------------------------------------------------------------
        # Step 1: Build patch map
        #   patches = {epub_item_href: {node_path: concatenated_translated_text}}
        # -------------------------------------------------------------------
        # Use a dict to concatenate segments that share (href, node_path).
        # Order matters: segments must be appended in the order they appear
        # in the chunk (which mirrors the original document order).
        patches: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))

        for chunk in chunks:
            prose_nodes = chunk.meta.get("prose_nodes")
            if not prose_nodes:
                # Not a prose chunk (e.g. SRT-style) — skip
                continue

            translated_text = chunk.translated_text
            if translated_text is None:
                # Untranslated chunk — fall back to source text
                translated_text = chunk.source_text

            segments = translated_text.split("\n\n")
            n_nodes = len(prose_nodes)
            n_segs = len(segments)

            if n_segs != n_nodes:
                # Defensive mismatch handling:
                # If we have MORE segments than nodes, concatenate excess segments
                # into the last node to avoid index errors.
                # If we have FEWER segments than nodes, leave remaining nodes
                # untouched (do not patch them).
                logger.warning(
                    "EpubWriter: segment/node count mismatch "
                    "(segments=%d, prose_nodes=%d) for chunk index=%d — "
                    "applying defensive mapping",
                    n_segs, n_nodes, chunk.index,
                )
                if n_segs > n_nodes:
                    # More segments than nodes: concatenate excess into last node
                    adjusted: list[str] = segments[: n_nodes - 1]
                    adjusted.append("\n\n".join(segments[n_nodes - 1 :]))
                    segments = adjusted
                else:
                    # Fewer segments than nodes: pad with empty strings for missing
                    segments = segments + [""] * (n_nodes - n_segs)

            for i, node_info in enumerate(prose_nodes):
                href = node_info.get("epub_item_href", "")
                np = node_info.get("node_path", "")
                if not href or not np:
                    continue
                seg = segments[i] if i < len(segments) else ""
                patches[href][np].append(seg)

        # Flatten: join multi-segment (hard-split) fragments back into one string
        flat_patches: dict[str, dict[str, str]] = {}
        for href, node_patches in patches.items():
            flat_patches[href] = {
                np: " ".join(segs).strip()  # rejoin hard-split fragments with a space
                for np, segs in node_patches.items()
            }

        # -------------------------------------------------------------------
        # Step 2: Read the source EPUB as a ZIP and copy everything,
        #         patching XHTML documents that have translation segments.
        # -------------------------------------------------------------------
        with zipfile.ZipFile(src_path, "r") as src_zip:
            with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as out_zip:
                for info in src_zip.infolist():
                    original_data = src_zip.read(info.filename)

                    # Check if this file has patches
                    # epub_item_href values are relative paths without leading slash
                    # The ZIP entry name may include "OEBPS/" or "EPUB/" prefix
                    patched_data = self._patch_entry(info.filename, original_data, flat_patches)
                    out_zip.writestr(info, patched_data)

    def _patch_entry(
        self,
        zip_entry_name: str,
        original_data: bytes,
        flat_patches: dict[str, dict[str, str]],
    ) -> bytes:
        """Return patched data for *zip_entry_name*, or *original_data* if no patch."""
        if not flat_patches:
            return original_data

        # epub_item_href values from ebooklib are like "ch1.xhtml" or
        # "OEBPS/ch1.xhtml".  The ZIP entry may be stored as "OEBPS/ch1.xhtml"
        # or just "ch1.xhtml" depending on the EPUB structure.
        # Try exact match first, then suffix match.
        patches_for_entry = flat_patches.get(zip_entry_name)

        if patches_for_entry is None:
            # Try matching on the trailing component (epub_item_href may be
            # relative to the content root rather than the ZIP root)
            for href, node_patches in flat_patches.items():
                if zip_entry_name.endswith(href) or zip_entry_name.endswith("/" + href):
                    patches_for_entry = node_patches
                    break
                # Also try the reverse: href ends with zip_entry_name's basename
                if href.endswith(os.path.basename(zip_entry_name)):
                    patches_for_entry = node_patches
                    break

        if not patches_for_entry:
            return original_data

        # Only patch XHTML/HTML documents
        if not (
            zip_entry_name.endswith(".xhtml")
            or zip_entry_name.endswith(".html")
            or zip_entry_name.endswith(".htm")
        ):
            return original_data

        return _patch_xhtml_document(original_data, patches_for_entry)
