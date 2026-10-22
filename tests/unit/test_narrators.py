"""Tests for the per-chapter first-person narrator fact.

A book can have several narrators, so the fact belongs to a chapter, never to
the book. A chapter is an EPUB spine item: chunk_prose never lets a chunk cross
one, so every chunk maps to exactly one chapter through its first prose node.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from borgesica.domain.context import ContextManager
from borgesica.domain.models import (
    Chunk,
    Glossary,
    GlossaryEntry,
    JobConfig,
    SourceType,
)
from borgesica.domain.narrators import (
    NarratorFact,
    chapter_key,
    first_person_per_1k,
    list_chapters,
)


def epub_chunk(index: int, href: str, text: str, **meta) -> Chunk:
    nodes = [{"epub_item_href": href, "node_path": f"/body/p[{index}]", **meta}]
    extra = {"kind": meta["kind"]} if "kind" in meta else {}
    return Chunk(index=index, source_text=text, meta={"prose_nodes": nodes, **extra})


FIRST_PERSON = (
    "I keep my gaze fixed on Tash, and my hand on his shoulder. I wait, and I "
    "listen, and I tell myself that the worst is over. My heart will not slow. "
) * 8  # long enough (> FIRST_PERSON_MIN_WORDS) to be judged
THIRD_PERSON = (
    "He keeps his gaze fixed on Tash, and his hand on her shoulder. He waits, "
    "and he listens, and he tells himself that the worst is over."
)


# ---------------------------------------------------------------------------
# chapter_key
# ---------------------------------------------------------------------------


def test_chapter_key_is_the_spine_item_of_the_first_prose_node():
    assert chapter_key(epub_chunk(3, "chapter02.xhtml", "text")) == "chapter02.xhtml"


def test_chapter_key_is_none_for_a_chunk_without_an_epub_locator():
    srt = Chunk(index=0, source_text="Hello", meta={"cue_batches": []})
    pdf = Chunk(
        index=0, source_text="Hello", meta={"prose_nodes": [{"pdf_page": 1, "para_index": 0}]}
    )
    assert chapter_key(srt) is None
    assert chapter_key(pdf) is None


# ---------------------------------------------------------------------------
# first_person_per_1k
# ---------------------------------------------------------------------------


def test_first_person_density_is_high_for_first_person_narration():
    assert first_person_per_1k(FIRST_PERSON) > 100


def test_first_person_density_is_zero_for_third_person_narration():
    assert first_person_per_1k(THIRD_PERSON) == 0


def test_first_person_density_ignores_dialogue():
    # A third-person chapter is full of "I" inside quotes; that "I" is a
    # character speaking, not the narrator.
    text = (
        "He turned to her. “I know what I said, and I meant it,” he told her. "
        'She laughed. "I do not believe you, and I never will," she said.'
    )
    assert first_person_per_1k(text) == 0


def test_first_person_density_ignores_markup():
    tagged = "<em>I</em> keep <i>my</i> word."
    assert first_person_per_1k(tagged) == first_person_per_1k("I keep my word.")


def test_first_person_density_of_empty_text_is_zero():
    assert first_person_per_1k("") == 0
    assert first_person_per_1k("“All dialogue.”") == 0


# ---------------------------------------------------------------------------
# list_chapters
# ---------------------------------------------------------------------------


def test_list_chapters_groups_consecutive_chunks_by_spine_item():
    chunks = [
        epub_chunk(8, "chapter01.xhtml", "I\n\nFEAR, MY FATHER ONCE TOLD ME, is a lack."),
        epub_chunk(9, "chapter01.xhtml", FIRST_PERSON),
        epub_chunk(10, "chapter02.xhtml", "II\n\n" + THIRD_PERSON),
    ]
    chapters = list_chapters(chunks)

    assert [c.key for c in chapters] == ["chapter01.xhtml", "chapter02.xhtml"]
    assert (chapters[0].first_chunk, chapters[0].last_chunk) == (8, 9)
    assert (chapters[1].first_chunk, chapters[1].last_chunk) == (10, 10)


def test_list_chapters_titles_a_chapter_by_its_first_paragraph():
    chunks = [
        epub_chunk(8, "chapter01.xhtml", "I\n\nFEAR, MY FATHER <em>ONCE</em> TOLD ME, is a lack."),
    ]
    chapter = list_chapters(chunks)[0]

    assert chapter.title == "I"
    assert chapter.opening == "FEAR, MY FATHER ONCE TOLD ME, is a lack."


def test_list_chapters_opening_is_truncated():
    chunks = [epub_chunk(0, "c.xhtml", "Title\n\n" + "word " * 200)]
    assert len(list_chapters(chunks)[0].opening) <= 160


def test_list_chapters_premarks_first_person_chapters():
    chunks = [
        epub_chunk(0, "chapter01.xhtml", FIRST_PERSON),
        epub_chunk(1, "chapter02.xhtml", THIRD_PERSON),
    ]
    first, third = list_chapters(chunks)

    assert first.first_person is True
    assert first.first_person_per_1k > 100
    assert third.first_person is False
    assert third.first_person_per_1k == 0


def test_list_chapters_counts_density_over_the_whole_chapter():
    # One dialogue-heavy chunk must not hide a chapter that is first-person
    # overall: the count runs over every chunk of the chapter together.
    chunks = [
        epub_chunk(0, "c.xhtml", FIRST_PERSON),
        epub_chunk(1, "c.xhtml", "“Go.”"),
    ]
    assert list_chapters(chunks)[0].first_person is True


def test_roman_numeral_headings_are_not_the_pronoun():
    # A table of contents is a column of "I", "II", "III"...: measured on the
    # real book it scored 30 per 1k and was pre-marked first-person.
    toc = "\n\n".join(["CONTENTS", "I", "II", "III", "IV", "V", "Part One"])
    assert first_person_per_1k(toc) == 0
    assert first_person_per_1k("I\n\n" + THIRD_PERSON) == 0


def test_a_chapter_too_short_to_judge_is_not_premarked():
    # A dedication ("For my son") or a page of review blurbs is first person
    # by the count, but not the narrator's: too little text to suggest it.
    chunks = [epub_chunk(0, "dedication.xhtml", "For my son")]
    chapter = list_chapters(chunks)[0]

    assert chapter.first_person_per_1k > 100
    assert chapter.first_person is False


def test_list_chapters_sorts_by_chunk_index():
    chunks = [
        epub_chunk(5, "b.xhtml", THIRD_PERSON),
        epub_chunk(1, "a.xhtml", THIRD_PERSON),
    ]
    assert [c.key for c in list_chapters(chunks)] == ["a.xhtml", "b.xhtml"]


def test_list_chapters_skips_navigation_labels_and_unlocated_chunks():
    chunks = [
        epub_chunk(0, "chapter01.xhtml", FIRST_PERSON),
        epub_chunk(1, "nav.xhtml", "Chapter One", kind="nav-label"),
        Chunk(index=2, source_text="Hello", meta={"cue_batches": []}),
    ]
    assert [c.key for c in list_chapters(chunks)] == ["chapter01.xhtml"]


def test_list_chapters_of_a_subtitle_job_is_empty():
    chunks = [Chunk(index=0, source_text="Hello", meta={"cue_batches": []})]
    assert list_chapters(chunks) == []


# ---------------------------------------------------------------------------
# NarratorFact
# ---------------------------------------------------------------------------


def test_narrator_fact_accepts_a_name_and_a_gender():
    fact = NarratorFact(name="Vis", gender="masculine")
    assert (fact.name, fact.gender) == ("Vis", "masculine")


def test_narrator_fact_name_is_optional():
    assert NarratorFact(gender="feminine").name is None


@pytest.mark.parametrize("gender", ["unanchored", "male", "", None])
def test_narrator_fact_rejects_anything_but_masculine_or_feminine(gender):
    with pytest.raises(ValidationError):
        NarratorFact(name="Vis", gender=gender)


def test_narrator_fact_treats_a_blank_name_as_no_name():
    assert NarratorFact(name="   ", gender="masculine").name is None
    assert NarratorFact(name="  Vis ", gender="masculine").name == "Vis"


# ---------------------------------------------------------------------------
# The [NARRATOR] block in the prompt
# ---------------------------------------------------------------------------


def make_config() -> JobConfig:
    return JobConfig(source_type=SourceType.EPUB, model="m", target_lang="es-neutral")


def build(narrator: NarratorFact | None) -> str:
    glossary = Glossary(entries=[GlossaryEntry(term="Vis", translation="Vis")])
    return (
        ContextManager()
        .build_system_prompt(make_config(), glossary, narrator=narrator)
        .text
    )


def test_prompt_without_a_narrator_has_no_narrator_block():
    assert "[NARRATOR]" not in build(None)


def test_prompt_with_a_narrator_names_them_and_their_agreement():
    text = build(NarratorFact(name="Vis", gender="masculine"))

    block = text[text.index("[NARRATOR]") : text.index("[GLOSSARY]")]
    assert "Vis" in block
    assert "male" in block
    assert "masculine agreement" in block


def test_narrator_block_sits_right_before_the_glossary():
    text = build(NarratorFact(name="Vis", gender="masculine"))
    assert text.index("[NARRATOR]") < text.index("[GLOSSARY]")
    # Only whitespace and the block's own text between them: nothing else
    # was slotted in between.
    between = text[text.index("[NARRATOR]") : text.index("[GLOSSARY]")]
    assert between.count("[") == 1


def test_feminine_narrator_gets_feminine_agreement():
    text = build(NarratorFact(name="Emissa", gender="feminine"))
    block = text[text.index("[NARRATOR]") : text.index("[GLOSSARY]")]
    assert "Emissa, female" in block
    assert "feminine agreement" in block
    assert "masculine" not in block


def test_unnamed_narrator_still_carries_the_gender():
    text = build(NarratorFact(gender="feminine"))
    block = text[text.index("[NARRATOR]") : text.index("[GLOSSARY]")]
    assert "female" in block
    assert "None" not in block


def test_narrator_block_leaves_the_static_block_untouched():
    # The static block is the prompt-caching boundary: a per-chapter fact
    # must never move it.
    with_fact = build(NarratorFact(name="Vis", gender="masculine"))
    without = build(None)
    static = ContextManager().get_static_block(make_config())
    assert with_fact.startswith(static)
    assert without.startswith(static)
