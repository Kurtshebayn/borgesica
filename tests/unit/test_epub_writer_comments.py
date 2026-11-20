"""A paragraph carrying HTML comments (publishers leave "<!--PROD: ...-->" notes
in copyright pages) must not crash the EPUB writer: the comment is dropped and
the text around it is kept."""
from __future__ import annotations

from lxml import etree

from borgesica.adapters.writers.epub_writer import _set_element_content


def _paragraph(fragment: str) -> etree._Element:
    p = etree.Element("p")
    _set_element_content(p, fragment)
    return p


def test_trailing_comments_are_dropped_and_text_kept():
    p = _paragraph("Texto. <!--PROD: add a sentence--> <!--legal notice-->")
    assert "<!--" not in etree.tostring(p, encoding="unicode")
    assert "".join(p.itertext()).startswith("Texto.")


def test_text_after_a_comment_stays_in_the_paragraph():
    p = _paragraph("Uno <!--x-->dos <em>tres</em><!--y--> cuatro")
    assert etree.tostring(p, encoding="unicode") == "<p>Uno dos <em>tres</em> cuatro</p>"


def test_a_leading_comment_keeps_the_text_after_it():
    p = _paragraph("<!--x-->Hola")
    assert etree.tostring(p, encoding="unicode") == "<p>Hola</p>"
