"""A half-written tag in a translation (for example a code listing cut in the
middle of "<code class=...>") yields attribute names that are not valid XML
names. The writer must drop them, keep the text, and not abort the book."""
from __future__ import annotations

from lxml import etree

from borgesica.adapters.writers.epub_writer import _set_element_content


def _paragraph(fragment: str) -> etree._Element:
    p = etree.Element("p")
    _set_element_content(p, fragment)
    return p


def test_invalid_attribute_names_are_dropped_and_the_text_is_kept():
    p = _paragraph('Antes <code containers: container: image: alpine:3.6>cuerpo</code> despues')
    text = "".join(p.itertext())
    assert "cuerpo" in text and "Antes" in text and "despues" in text


def test_valid_attributes_next_to_an_invalid_one_survive():
    p = _paragraph('<code class="nt" args: x>hola</code>')
    code = p.find("code")
    assert code is not None and code.get("class") == "nt"
    assert code.text == "hola"


def test_ordinary_markup_is_unchanged():
    p = _paragraph('Uno <a href="x.html" epub:type="noteref">dos</a>')
    a = p.find("a")
    assert a is not None and a.get("href") == "x.html" and a.get("type") == "noteref"
