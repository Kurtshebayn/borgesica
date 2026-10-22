"""Per-chapter first-person narrator fact.

English "I" carries no gender, so Spanish agreement for a first-person
narrator ("No estoy segura" vs "seguro") is a coin flip unless the prompt says
who "I" is. The per-name CHARACTER GENDER line cannot say it: an anchored
chunk still wrote feminine agreement for the male narrator, because nothing
tied "I" to the name.

The fact belongs to a CHAPTER, never to the book: a book can have several
narrators (POV changing by chapter), and even a single-narrator book has
other "I"s (an author's acknowledgments). A chapter is an EPUB spine item —
``chunk_prose`` never lets a chunk cross one, so each chunk belongs to exactly
one chapter through its first prose node.

A person decides the narrator in the review step. This module only suggests
which chapters are first-person, with a free pronoun count over the English
source; it never guesses who the narrator is or their gender.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, field_validator

from borgesica.domain.markup import strip_all_tags
from borgesica.domain.models import CharacterGender, Chunk

# First-person singular pronouns per 1000 narrative words at or above which a
# chapter is pre-marked first-person. Measured on Strength of the Few: every
# one of its 80 first-person chapters scores 40-91 once dialogue is removed,
# while the back matter (character list, glossary, copyright) scores 0.
FIRST_PERSON_MIN_PER_1K = 20.0
# Narrative words below which a chapter is not pre-marked at all. On the same
# book the dedication ("For my son", 4 words), the review blurbs (156) and the
# back-cover text (169) all count as first person, but none is long enough to
# tell whose "I" it is; the shortest real chapter has 828 words.
FIRST_PERSON_MIN_WORDS = 200

_TITLE_MAX = 80
_OPENING_MAX = 160

# Dialogue in double quotes, curly or straight. Single quotes are left alone:
# a British-style ‘…’ cannot be told apart from an apostrophe (don’t) by a
# regex, and a missed quote only inflates a suggestion the person reviews.
_DIALOGUE = re.compile(r"“[^”]*”|\"[^\"]*\"")
# "I" is matched case-sensitively (a lowercase "i" is not the pronoun); the
# others case-insensitively, because chapter openings are often set in
# capitals ("FEAR, MY FATHER ONCE TOLD ME").
_FIRST_PERSON = re.compile(r"\bI\b|\b(?i:me|my|mine|myself)\b")
# A paragraph that is only a Roman numeral is a heading ("I", "XIV"). A table
# of contents is a column of them, and "I" would count as the pronoun.
_ROMAN_HEADING = re.compile(r"^[IVXLCDM]+\.?$")
_GENDER_WORD = {"masculine": "male", "feminine": "female"}


class NarratorFact(BaseModel):
    """Who the first-person narrator of a chapter is, as a person set it."""

    name: str | None = None
    gender: CharacterGender

    @field_validator("name")
    @classmethod
    def _blank_name_is_no_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class NarratorAssignment(NarratorFact):
    """A narrator fact addressed to one chapter, as the review sends it."""

    key: str


class Chapter(BaseModel):
    """One chapter as the review shows it, with the narrator stored for it."""

    key: str
    title: str
    opening: str
    first_chunk: int
    last_chunk: int
    first_person: bool
    first_person_per_1k: float
    narrator: NarratorFact | None = None


def chapter_key(chunk: Chunk) -> str | None:
    """Return the spine item a chunk belongs to, or None when it has none.

    Subtitle and PDF chunks carry no ``epub_item_href`` and so no chapter.
    """
    nodes = chunk.meta.get("prose_nodes") or []
    if not nodes:
        return None
    href = nodes[0].get("epub_item_href")
    return href if isinstance(href, str) and href else None


def first_person_per_1k(text: str) -> float:
    """First-person singular pronouns per 1000 words of NARRATION.

    Dialogue is removed first: a third-person chapter is full of "I" spoken
    by its characters, and that "I" says nothing about who narrates.
    """
    narration = _narration(text)
    words = len(narration.split())
    if not words:
        return 0.0
    return 1000 * len(_FIRST_PERSON.findall(narration)) / words


def _narration(text: str) -> str:
    """Text without markup, Roman-numeral headings or double-quoted dialogue."""
    paragraphs = strip_all_tags(text).split("\n\n")
    kept = [p for p in paragraphs if not _ROMAN_HEADING.match(p.strip())]
    return _DIALOGUE.sub(" ", "\n\n".join(kept))


def list_chapters(chunks: list[Chunk]) -> list[Chapter]:
    """Group chunks into chapters in reading order, pre-marking first person.

    Navigation labels (the table of contents) and chunks without a chapter
    are skipped: no narrator speaks in them.
    """
    groups: dict[str, list[Chunk]] = {}
    for chunk in sorted(chunks, key=lambda c: c.index):
        if chunk.meta.get("kind") == "nav-label":
            continue
        key = chapter_key(chunk)
        if key is not None:
            groups.setdefault(key, []).append(chunk)

    chapters: list[Chapter] = []
    for key, members in groups.items():
        text = "\n\n".join(c.source_text for c in members)
        density = first_person_per_1k(text)
        long_enough = len(_narration(text).split()) >= FIRST_PERSON_MIN_WORDS
        title, opening = _title_and_opening(members[0].source_text)
        chapters.append(
            Chapter(
                key=key,
                title=title,
                opening=opening,
                first_chunk=members[0].index,
                last_chunk=members[-1].index,
                first_person=long_enough and density >= FIRST_PERSON_MIN_PER_1K,
                first_person_per_1k=round(density, 1),
            )
        )
    return chapters


def render_narrator(fact: NarratorFact) -> str:
    """Return the body of the prompt's [NARRATOR] block."""
    who = f"{fact.name}, " if fact.name else ""
    return (
        f'The first-person narrator ("I") of this passage is {who}'
        f"{_GENDER_WORD[fact.gender]}. Use {fact.gender} agreement for every "
        "adjective and participle that refers to the narrator."
    )


def _title_and_opening(source_text: str) -> tuple[str, str]:
    paragraphs = [
        " ".join(strip_all_tags(p).split())
        for p in source_text.split("\n\n")
    ]
    paragraphs = [p for p in paragraphs if p]
    if not paragraphs:
        return "", ""
    title = _truncate(paragraphs[0], _TITLE_MAX)
    opening = _truncate(" ".join(paragraphs[1:]), _OPENING_MAX)
    return title, opening


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
