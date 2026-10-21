"""Pre-translation glossary review — per-entry gender evidence and risk flags.

Dependency rule: only stdlib + pydantic + domain.

The whole-book extraction pass produces a glossary a human still has to
triage before translating: on "The Strength of the Few" (gpt-6-luna, 688
entries) 65 terms drew conflicting renderings across batches, 42 were split
between kept and translated, and the gender seeder left high-mention
characters such as Aequa unset. ``review_glossary`` attaches to every entry the
evidence and flags that let a reviewer find those entries without reading all
of them.

Everything here is deterministic and free, so it is recomputed on every read
instead of stored: the pronoun scan and the lowercase vocabulary cost 0.10s
and 0.07s on that book's 1.5M characters, and a stored verdict would age out
of agreement with an entry a human edits.
"""
from __future__ import annotations

import re
import unicodedata

from borgesica.domain.glossary import (
    MIN_GENDER_MENTIONS,
    MIN_GENDER_RATIO,
    character_gender_evidence,
    gender_eligible,
    rendering_counts,
    renderings_disagree,
    seed_character_gender,
)
from borgesica.domain.models import (
    Glossary,
    GlossaryEntry,
    GlossaryReview,
    GlossaryReviewEntry,
    GlossaryRiskFlag,
    RenderingProposal,
    anchored_gender,
    normalize_term,
)
from borgesica.domain.quality import lowercase_vocabulary

__all__ = ["review_glossary"]

# Letters in any script. The source vocabulary is ASCII-tokenised, so an
# ASCII pattern here would read "Artán" as "Art" + "n" — and "n" is lowercase
# somewhere in any book. On job 9be143da that split alone flagged 8 Irish
# names (Artán, Pádraig, Rónán...) as ordinary vocabulary; matching whole
# Unicode words leaves a non-ASCII word simply unseen in lowercase.
_TERM_WORD = re.compile(r"[^\W\d_][^\W\d_'’]*")


def _words(text: str) -> list[str]:
    return _TERM_WORD.findall(text)


def _fold(word: str) -> str:
    """Casefold and strip accents, so "Ká" and "Ka" are the same word."""
    decomposed = unicodedata.normalize("NFD", word.casefold())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def _seen_lowercase(term: str, vocabulary: frozenset[str]) -> bool:
    """True when the source writes every word of ``term`` in lowercase.

    A single-word test for a single-word term; a multi-word term counts only
    when all of its words are ordinary vocabulary ("Will shells").
    """
    words = _words(term)
    return bool(words) and all(word.lower() in vocabulary for word in words)


def _is_identity(entry: GlossaryEntry) -> bool:
    """True when the entry keeps its term verbatim, ignoring case."""
    return not renderings_disagree((entry.term, entry.translation))


def _shares_a_word(term: str, rendering: str) -> bool:
    return bool({_fold(w) for w in _words(term)} & {_fold(w) for w in _words(rendering)})


def _flags(
    entry: GlossaryEntry,
    proposals: tuple[str, ...],
    vocabulary: frozenset[str],
    undecided: bool,
) -> list[GlossaryRiskFlag]:
    term = normalize_term(entry.term)
    identity = _is_identity(entry)
    flags: list[GlossaryRiskFlag] = []

    if renderings_disagree(proposals):
        flags.append("conflicting_renderings")
        kept = [not renderings_disagree((term, rendering)) for rendering in proposals]
        if any(kept) and not all(kept):
            flags.append("kept_vs_translated")

    seen_lowercase = _seen_lowercase(term, vocabulary)
    # Capitalised only. A term the glossary itself spells lowercase is the
    # extractor recognising an in-world word the book writes that way (alupi,
    # draoi, kataht): on job 9be143da 28 of the 31 lowercase-vocabulary
    # identity entries were those, all correctly kept. The capitalised three
    # were Thrum, Vek and a PDF watermark — the defect this flag is for.
    if identity and term[:1].isupper() and seen_lowercase:
        flags.append("lowercase_identity")

    # Weak by design: on job 9be143da it fires on 68 of 549 entries, nearly all
    # correct translations of capitalised English words (Academy -> la
    # Academia, Senate -> el Senado). A cognate test only halved that (37)
    # without finding anything the plain rule missed, so it was not added.
    # A term with no word at all ("1984") makes both tests vacuous, so it is
    # never flagged by default.
    if (
        not identity
        and _words(term)
        and not seen_lowercase
        and not _shares_a_word(term, entry.translation)
    ):
        flags.append("possible_gloss")

    if undecided:
        flags.append("undecided_gender")
    return flags


def review_glossary(
    glossary: Glossary,
    proposals: dict[str, tuple[str, ...]],
    source_text: str,
    *,
    min_mentions: int = MIN_GENDER_MENTIONS,
    min_ratio: float = MIN_GENDER_RATIO,
) -> GlossaryReview:
    """Return every glossary entry with its gender evidence and risk flags.

    Args:
        glossary: the glossary to review, in the order to report it.
        proposals: the extraction pass's per-term renderings, keyed by the
            casefolded normalised term, one per batch that proposed it (what
            ``merge_extraction_batches`` returns and the store persists).
        source_text: the ENGLISH source of the whole job. Never a
            translation: a Spanish rendering already carries the agreement the
            gender anchor exists to correct.
        min_mentions, min_ratio: the seeder's thresholds, so the reported
            gender is the one a translation run would anchor.

    Entries keep the glossary's order; flags keep the ``GlossaryRiskFlag``
    order; the thresholds used are reported beside them. Nothing is mutated
    or persisted.
    """
    evidence = character_gender_evidence(source_text)
    vocabulary = lowercase_vocabulary(source_text)
    seeded = seed_character_gender(
        glossary, evidence, min_mentions=min_mentions, min_ratio=min_ratio
    )

    reviewed: list[GlossaryReviewEntry] = []
    for entry, anchored in zip(glossary.entries, seeded.entries, strict=True):
        key = normalize_term(entry.term).casefold()
        masculine, feminine = evidence.get(key, (0, 0))
        term_proposals = proposals.get(key, ())
        if entry.gender is not None:
            gender_source = "stored"
        elif anchored.gender is not None:
            gender_source = "seeded"
        else:
            gender_source = None
        undecided = (
            gender_eligible(entry)
            and anchored.gender is None
            and masculine + feminine >= min_mentions
        )
        reviewed.append(
            GlossaryReviewEntry(
                entry=entry,
                gender=anchored_gender(anchored.gender),
                gender_source=gender_source,
                masculine_mentions=masculine,
                feminine_mentions=feminine,
                proposals=[
                    RenderingProposal(rendering=rendering, batches=batches)
                    for rendering, batches in rendering_counts(term_proposals)
                ],
                flags=_flags(entry, term_proposals, vocabulary, undecided),
            )
        )
    return GlossaryReview(
        entries=reviewed, min_gender_mentions=min_mentions, min_gender_ratio=min_ratio
    )
