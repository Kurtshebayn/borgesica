"""GlossaryExtractor — Protocol + LLM and Null strategies.

Dependency rule: only stdlib + pydantic + domain models/ports.
No I/O, no adapter imports.

Design (M1-7):
  GlossaryExtractor Protocol is already declared in ports.py.
  This module provides:
    - LlmGlossaryExtractor: calls provider.translate with a glossary-extraction
      prompt; returns a Glossary built from TranslationUnit.glossary_additions.
    - NullGlossaryExtractor: always returns Glossary() (strategy="none").
    - get_extractor(strategy, provider) -> GlossaryExtractor: factory function.

Mid-run addition staging logic:
  The orchestrator is responsible for merging mid-run glossary_additions into
  the live glossary after each chunk.  The rules are:
    1. If a locked entry with the same term already exists → discard the addition.
    2. If no existing entry with that term → add as locked=False, persist.
  This module provides the merge helper: merge_additions(glossary, additions).
"""
from __future__ import annotations

import re
from bisect import bisect_left, bisect_right

from borgesica.domain.models import (
    CharacterGender,
    Glossary,
    GlossaryEntry,
    GlossarySettlements,
    GlossaryVotes,
    JobConfig,
    normalize_term,
)
from borgesica.domain.ports import TranslationProvider

__all__ = [
    "QUORUM",
    "GlossarySettlements",
    "GlossaryVotes",
    "LlmGlossaryExtractor",
    "NullGlossaryExtractor",
    "apply_additions",
    "character_gender_evidence",
    "dedupe_glossary",
    "drop_reversed_entries",
    "get_extractor",
    "merge_additions",
    "normalize_term",
    "sanitize_glossary",
    "seed_character_gender",
    "settlement_counts",
]

# ---------------------------------------------------------------------------
# Glossary-extraction system prompt
# ---------------------------------------------------------------------------

_EXTRACTION_SYSTEM_PROMPT = """\
You are a literary terminology extractor. Given a passage of English text, \
identify proper nouns, invented terms, character names, place names, and \
domain-specific vocabulary that a translator would need to handle consistently \
across a long document.

Return your response as a JSON object with EXACTLY the following fields:
  {
    "translation": "",
    "summary_update": "Terminology extraction complete.",
    "glossary_additions": [
      {"term": "<source term>", "translation": "<suggested Spanish rendering>", \
"locked": false, "note": "<brief context or etymology>"}
    ]
  }

Rules:
- Include ONLY terms a translator needs to handle consistently.
- Do NOT include common English or Spanish vocabulary.
- If no notable terms are found, return an empty glossary_additions list.
- The "translation" field must be an empty string for this extraction task."""


# ---------------------------------------------------------------------------
# LlmGlossaryExtractor
# ---------------------------------------------------------------------------


class LlmGlossaryExtractor:
    """Extract terminology using an LLM via the TranslationProvider port.

    Calls provider.translate() exactly once with a glossary-extraction prompt.
    Returns a Glossary built from TranslationUnit.glossary_additions.

    This is the DEFAULT extractor (glossary_strategy="llm").
    It has zero install friction — no SpaCy model download required.

    Non-determinism is fully mitigated by the locked design: the seeded
    glossary is persisted immediately and user-editable before any translation
    spend. The human locks the terms, not the model.
    """

    def __init__(self, provider: TranslationProvider) -> None:  # type: ignore[type-arg]
        self._provider = provider

    def extract(self, text: str, config: JobConfig) -> Glossary:
        """Extract terminology from source text via LLM.

        Args:
            text: Source text to analyse (typically a concatenation of all
                  source chunks, or a representative sample).
            config: JobConfig (model string used for the provider call).

        Returns:
            Glossary populated with entries from the LLM response.
        """
        user_prompt = f"Extract terminology from the following text:\n\n{text}"
        result = self._provider.translate(
            system=_EXTRACTION_SYSTEM_PROMPT,
            user=user_prompt,
            model=config.model,
        )
        seeded, _duplicates = dedupe_glossary(
            Glossary(entries=list(result.unit.glossary_additions))
        )
        return seeded


# ---------------------------------------------------------------------------
# NullGlossaryExtractor
# ---------------------------------------------------------------------------


class NullGlossaryExtractor:
    """No-op extractor for glossary_strategy="none".

    Returns an empty Glossary every time.  Used when the caller explicitly
    opts out of terminology extraction.
    """

    def extract(self, text: str, config: JobConfig) -> Glossary:  # noqa: ARG002
        """Return an empty Glossary unconditionally."""
        return Glossary()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_extractor(
    strategy: str,
    provider: TranslationProvider,  # type: ignore[type-arg]
) -> "LlmGlossaryExtractor | NullGlossaryExtractor":
    """Return the appropriate GlossaryExtractor for the given strategy.

    Args:
        strategy: One of "llm", "spacy", "hybrid", "none".
                  "spacy" and "hybrid" are reserved for M4 — they fall back
                  to "llm" in this slice.
        provider: TranslationProvider used by LlmGlossaryExtractor.

    Returns:
        A GlossaryExtractor instance satisfying the Protocol.
    """
    if strategy == "none":
        return NullGlossaryExtractor()
    # "llm", "spacy" (M4), "hybrid" (M4) all use LLM extraction in this slice
    return LlmGlossaryExtractor(provider=provider)


# ---------------------------------------------------------------------------
# Term normalisation and deduplication
# ---------------------------------------------------------------------------


def _dedupe_key(term: str) -> str:
    """Return the identity of a term for duplicate detection.

    Case-insensitive: a measured 491-entry book glossary carried 12 collisions
    and every one of them was case-only ("Alupi"/"alupi", "Caer"/"caer"). Two
    spellings of one term teach the model nothing and are paid for on every
    provider call, so they are one term here.
    """
    return normalize_term(term).casefold()


def dedupe_glossary(glossary: Glossary) -> tuple[Glossary, list[GlossaryEntry]]:
    """Collapse case- and whitespace-only duplicates, preserving order.

    Returns the deduplicated glossary and the entries it discarded, matching
    ``drop_reversed_entries``. Both rules report what they removed because a
    reader has to be able to explain why a stored glossary shows fewer entries
    than were saved — on the real 491-entry glossary 14 of the 20 removals are
    duplicates, so a report that omitted them would explain almost nothing.

    Within a group of entries sharing a ``_dedupe_key``:
      - a LOCKED entry wins, because locking is an explicit human decision
        that outranks the order the model happened to emit terms in;
      - otherwise the first-seen entry wins;
      - the winner keeps its own term, translation and note, except that a
        missing note is filled from the first duplicate that has one — dedupe
        should not be the step that loses the only human-readable context.

    Entries whose normalised term is empty are dropped: they can never match
    source text, so they are pure prompt weight.
    """
    winners: dict[str, GlossaryEntry] = {}
    order: list[str] = []
    dropped: list[GlossaryEntry] = []

    for entry in glossary.entries:
        term = normalize_term(entry.term)
        if not term:
            dropped.append(entry)
            continue
        key = term.casefold()
        candidate = entry.model_copy(update={"term": term})
        incumbent = winners.get(key)

        if incumbent is None:
            winners[key] = candidate
            order.append(key)
            continue

        if candidate.locked and not incumbent.locked:
            # Locked wins, but keep any note the (unlocked) incumbent carried.
            winners[key] = candidate.model_copy(
                update={"note": candidate.note or incumbent.note}
            )
            dropped.append(incumbent)
        else:
            if incumbent.note is None and candidate.note is not None:
                winners[key] = incumbent.model_copy(update={"note": candidate.note})
            dropped.append(entry)

    return Glossary(entries=[winners[key] for key in order]), dropped


# ---------------------------------------------------------------------------
# Direction guard — reversed / contradictory entries
# ---------------------------------------------------------------------------


def drop_reversed_entries(
    glossary: Glossary,
) -> tuple[Glossary, list[GlossaryEntry]]:
    """Remove entries that point the wrong way, returning them for reporting.

    The glossary is directional: ``term`` is source text, ``translation`` is
    the target-language rendering. Nothing enforced that, and on the real
    491-entry glossary of job 13b43ac6 six entries had it backwards, four as
    outright inverse pairs ("Birthright → Derecho de Nacimiento" alongside
    "Derecho de Nacimiento → Birthright"). Rendered into the prompt they
    instruct translating INTO the source language.

    An entry is reversed when its TRANSLATION is the TERM of an
    already-accepted NON-IDENTITY entry — that is, it produces output the
    glossary itself says must be translated to something else. This needs no
    language detection: it is a contradiction visible in the data alone.

    Two exemptions keep the rule honest, both learned by running it over the
    real glossary:

    - IDENTITY entries (term == translation) are never dropped and never count
      as evidence. "la Lengua → la Lengua" only says "leave this alone", so a
      correct "The Tongue → la Lengua" does not contradict it. Without this
      exemption the rule discarded 8 valid mappings on the real glossary
      alongside the 6 real reversals.
    - LOCKED entries are never dropped. Locking is a human decision, and
      inference does not get to overrule it.

    Order decides which half of an inverse pair survives, and the data backs
    it: in all six real cases the source-language direction was recorded
    FIRST, and the rendering leaked back as a term later, after the model had
    already produced it.
    """
    mapped_terms: set[str] = set()
    kept: list[GlossaryEntry] = []
    dropped: list[GlossaryEntry] = []

    for entry in glossary.entries:
        term = normalize_term(entry.term)
        translation = normalize_term(entry.translation)
        is_identity = term == translation

        if (
            not is_identity
            and not entry.locked
            and translation.casefold() in mapped_terms
        ):
            dropped.append(entry)
            continue

        kept.append(entry)
        if not is_identity:
            mapped_terms.add(term.casefold())

    return Glossary(entries=kept), dropped


def sanitize_glossary(glossary: Glossary) -> tuple[Glossary, list[GlossaryEntry]]:
    """Apply every glossary hygiene rule, returning the entries that were removed.

    The canonical composition — deduplicate case and spacing variants, then
    drop entries that point the wrong way. Both rules are idempotent, so this
    is safe to run on every load and every save.

    Exists so there is ONE spelling of "a clean glossary". The rules are
    applied at each boundary where a glossary enters the system: on load, on
    hand edit, and on mid-run merge. Applying them only during a run was not
    enough — a FINISHED job never merges again, so its stored glossary would
    keep its duplicates and contradictions forever.
    """
    deduped, duplicates = dedupe_glossary(glossary)
    cleaned, reversed_entries = drop_reversed_entries(deduped)
    return cleaned, duplicates + reversed_entries


# ---------------------------------------------------------------------------
# Mid-run addition merge helper (used by orchestrator in M1-8)
# ---------------------------------------------------------------------------


def merge_additions(glossary: Glossary, additions: list[GlossaryEntry]) -> Glossary:
    """Merge mid-run glossary_additions into the live glossary.

    Rules (from spec context-continuity/mid-run-additions):
    1. If a LOCKED entry with the same term exists → silently discard.
    2. If an unlocked entry with the same term exists → skip (no duplicate).
    3. If the term is entirely new → add as locked=False.

    "Same term" is decided by ``_dedupe_key``, so a case or spacing variant of
    an existing term is a duplicate, not a new entry. The incoming live
    glossary is deduplicated first, and the merged result passes through
    ``drop_reversed_entries``. Both repair glossaries that were persisted
    before those rules existed: a resumed job cleans itself up on its next
    merge instead of carrying its collisions and contradictions to the end of
    the book.

    Returns a new Glossary (models are immutable Pydantic objects). Callers
    that want to report what was discarded should call ``drop_reversed_entries``
    directly — it returns the dropped entries.
    """
    deduped, _duplicates = dedupe_glossary(glossary)
    existing_locked = {_dedupe_key(e.term) for e in deduped.entries if e.locked}
    existing_terms = {_dedupe_key(e.term) for e in deduped.entries}

    new_entries = list(deduped.entries)
    for addition in additions:
        term = normalize_term(addition.term)
        if not term:
            # A blank term can never match source text.
            continue
        key = term.casefold()
        if key in existing_locked:
            # Rule 1: locked entry takes precedence — discard silently
            continue
        if key in existing_terms:
            # Rule 2: already present unlocked — skip duplicate
            continue
        # Rule 3: new term — add as unlocked
        new_entries.append(
            GlossaryEntry(
                term=term,
                translation=addition.translation,
                locked=False,
                note=addition.note,
            )
        )
        existing_terms.add(key)

    cleaned, _dropped = sanitize_glossary(Glossary(entries=new_entries))
    return cleaned


# ---------------------------------------------------------------------------
# Confirmation by repetition — provisional entries and the revision window
# ---------------------------------------------------------------------------

QUORUM = 3
"""Proposals a term collects before its rendering is settled for good.

Three, because two cannot break a disagreement: with two votes a tie has to be
resolved by taking the first, which is exactly the single unreplicated draw this
mechanism exists to replace.
"""


def _rendering_key(rendering: str) -> str:
    """Return the identity of a RENDERING, the way ``_plurality`` groups votes.

    Same operation as ``_dedupe_key`` but on the target-language side, kept
    separate because the two answer different questions: one decides whether two
    entries are the same term, this one whether two spellings are the same
    rendering.
    """
    return normalize_term(rendering).casefold()


def _plurality(proposals: tuple[str, ...]) -> str:
    """Return the most-proposed rendering, earliest proposal breaking ties.

    Grouped by ``casefold`` so "jaula de Voluntad" and "Jaula de Voluntad" are
    one rendering rather than two competing ones — on the real data that case
    split accounted for 13 of 83 proposals for a single term.
    """
    counts: dict[str, int] = {}
    spelling: dict[str, str] = {}
    for proposal in proposals:
        rendering = normalize_term(proposal)
        key = _rendering_key(rendering)
        counts[key] = counts.get(key, 0) + 1
        spelling.setdefault(key, rendering)
    # dict preserves insertion order and max() keeps the first maximum, so ties
    # resolve to the rendering proposed earliest.
    return spelling[max(counts, key=lambda key: counts[key])]


def apply_additions(
    glossary: Glossary,
    votes: GlossaryVotes,
    additions: list[GlossaryEntry],
    quorum: int = QUORUM,
) -> tuple[Glossary, GlossaryVotes]:
    """Merge additions, letting repeated proposals correct a bad first draw.

    ``merge_additions`` commits a term the first time the model emits it and
    never revisits it. That single emission is one sample at temperature > 0,
    and it decides the rendering for every remaining chunk. Measured over 422
    real calls, "Birthright" drew ten distinct renderings and the dominant one
    won 79% of first draws — so roughly one run in five pinned a minority
    rendering for the rest of the book.

    Here a new term is still committed immediately, because a term seen once
    must reach the prompt (rare terms like "Will shells" appear in only four
    chunks and would otherwise risk never being glossed at all). But it stays
    PROVISIONAL: later proposals for the same term are counted, and at
    ``quorum`` the plurality wins and replaces the committed rendering. Terms
    that never reach quorum keep their first draw, exactly as today.

    Locked entries take no votes and are never revised — locking is a human
    decision. The result passes through ``sanitize_glossary`` like every other
    glossary boundary.

    Every entry committed here records its rendering in ``first_draw``, because
    the tally is erased when a term settles: without it the draw quorum replaced
    is gone and the mechanism cannot be measured. ``settlement_counts`` reads it.

    Returns the updated glossary and the remaining provisional votes.
    """
    deduped, _duplicates = dedupe_glossary(glossary)
    locked = {_dedupe_key(e.term) for e in deduped.entries if e.locked}
    entries = list(deduped.entries)
    index = {_dedupe_key(e.term): i for i, e in enumerate(entries)}
    tally = {key: list(proposals) for key, proposals in votes.by_term.items()}

    for addition in additions:
        term = normalize_term(addition.term)
        if not term:
            # A blank term can never match source text.
            continue
        key = term.casefold()
        if key in locked:
            continue
        if key in index and key not in tally:
            # Settled: either it reached quorum or it predates this mechanism.
            continue
        rendering = normalize_term(addition.translation)
        tally.setdefault(key, []).append(rendering)
        if key not in index:
            entries.append(
                GlossaryEntry(
                    term=term,
                    translation=addition.translation,
                    locked=False,
                    note=addition.note,
                    # Normalised, so a later comparison against the plurality —
                    # which is also normalised — never reports spacing as a
                    # correction.
                    first_draw=rendering,
                )
            )
            index[key] = len(entries) - 1

    for key, proposals in list(tally.items()):
        if len(proposals) < quorum:
            continue
        position = index.get(key)
        if position is not None:
            winner = _plurality(tuple(proposals))
            entries[position] = entries[position].model_copy(
                update={"translation": winner}
            )
        del tally[key]

    cleaned, _dropped = sanitize_glossary(Glossary(entries=entries))
    return cleaned, GlossaryVotes(
        by_term={key: tuple(proposals) for key, proposals in tally.items()}
    )


def settlement_counts(
    glossary: Glossary, votes: GlossaryVotes
) -> GlossarySettlements:
    """Count the terms quorum decided, split by whether it changed them.

    Answers the question the mechanism could not answer for the 2026-08-14 run
    of job 9be143da: 32 of 549 terms settled, and nothing recorded how many of
    those 32 got a DIFFERENT rendering than the one first committed. A
    confirmation rate near 100% would mean the vote is buying nothing but
    tokens; a correction rate near the measured 21% minority-draw figure means
    it is doing the job it was built for.

    A term counts only when it is both:
      - decided — it has no votes left, so no later proposal can move it;
      - attributable — it carries a ``first_draw``, so there is something to
        compare against. Entries stored before that field existed, and entries
        a human edited, have none and are excluded rather than guessed at.

    Renderings are compared normalised and casefolded, matching how
    ``_plurality`` groups votes: two capitalisations of one rendering are the
    same rendering there, so they must not read as a correction here.
    """
    changed = 0
    confirmed = 0
    for entry in glossary.entries:
        if entry.first_draw is None:
            continue
        if _dedupe_key(entry.term) in votes.by_term:
            continue
        if _rendering_key(entry.translation) == _rendering_key(entry.first_draw):
            confirmed += 1
        else:
            changed += 1
    return GlossarySettlements(changed=changed, confirmed=confirmed)


# ---------------------------------------------------------------------------
# Character-gender seeding
# ---------------------------------------------------------------------------

# Characters searched on each side of a name for a gendered pronoun. Wide
# enough to reach the pronoun in the clause around the name, narrow enough
# that most hits belong to it — but it still catches whoever else is standing
# nearby, which is exactly why a bare majority is not enough to classify.
_GENDER_WINDOW_CHARS = 60

# A name must draw at least this many gendered pronouns before it is
# classified at all. Job 9be143da's book put every real character far above
# it (Caeror 112, Netiqret 105, Vis 68) while noise sits in single digits.
MIN_GENDER_MENTIONS = 20

# ...and at least this share of them must agree. The threshold is what keeps
# the seeder honest rather than merely accurate: Aequa (51/115) and Emissa
# (16/32) land at 66-69% feminine — obvious to a reader, under the margin, and
# therefore left unset. Guessing them right would not have been knowledge, and
# a wrong anchor is worse than no anchor because the model trusts it.
MIN_GENDER_RATIO = 0.7

_MASCULINE_PRONOUNS = frozenset({"he", "him", "his", "himself"})
_FEMININE_PRONOUNS = frozenset({"she", "her", "hers", "herself"})

_WORD = re.compile(r"[A-Za-z][A-Za-z'\u2019]*")


def _count_near(positions: list[int], anchor: int) -> int:
    """Number of ``positions`` within the window around ``anchor``."""
    low = bisect_left(positions, anchor - _GENDER_WINDOW_CHARS)
    high = bisect_right(positions, anchor + _GENDER_WINDOW_CHARS)
    return high - low


def character_gender_evidence(source_text: str) -> dict[str, tuple[int, int]]:
    """Return {casefolded name: (masculine_count, feminine_count)}.

    Separate from ``seed_character_gender`` because it is the expensive half
    and the source text never changes during a run: scanning it once and
    reusing the result costs 0.09s on a real 1.5M-character book, while
    rescanning per chunk costs 53s of the same answer 569 times over.

    One pass over the source collects the position of every gendered pronoun
    and of every capitalised word (the name candidates); each name occurrence
    is then credited with the pronouns inside its window. Built ONCE per run
    rather than per entry: the alternative rescans the whole book for every
    term, and a real book carries ~549 of them.

    A word ever seen LOWERCASE is dropped as a common noun, however strong its
    evidence. Pronoun proximity says what gender a PERSON is; a capitalised
    common noun standing near "he" is just a noun standing near a pronoun. On
    the real book this is what separated the characters from "Religion",
    "Governance", "Military" and "Concurrence" — nouns whose Spanish gender is
    grammatical, and which an anchor would have mis-declared ("la religión" is
    feminine). Capitalisation that merely opens a sentence does not disqualify
    a name: only a genuine lowercase use does.
    """
    masculine_at: list[int] = []
    feminine_at: list[int] = []
    names_at: dict[str, list[int]] = {}
    seen_lowercase: set[str] = set()

    for match in _WORD.finditer(source_text):
        word = match.group()
        lowered = word.casefold()
        if lowered in _MASCULINE_PRONOUNS:
            masculine_at.append(match.start())
        elif lowered in _FEMININE_PRONOUNS:
            feminine_at.append(match.start())
        elif word[0].isupper():
            names_at.setdefault(lowered, []).append(match.start())
        else:
            seen_lowercase.add(lowered)

    for common in seen_lowercase:
        names_at.pop(common, None)

    evidence: dict[str, tuple[int, int]] = {}
    for name, positions in names_at.items():
        masculine = sum(_count_near(masculine_at, at) for at in positions)
        feminine = sum(_count_near(feminine_at, at) for at in positions)
        if masculine or feminine:
            evidence[name] = (masculine, feminine)
    return evidence


def _classify_gender(
    masculine: int, feminine: int, min_mentions: int, min_ratio: float
) -> CharacterGender | None:
    """Return the gender the evidence supports, or None when it supports none."""
    total = masculine + feminine
    if total < min_mentions:
        return None
    if masculine >= feminine:
        return "masculine" if masculine / total >= min_ratio else None
    return "feminine" if feminine / total >= min_ratio else None


def seed_character_gender(
    glossary: Glossary,
    evidence: dict[str, tuple[int, int]],
    *,
    min_mentions: int = MIN_GENDER_MENTIONS,
    min_ratio: float = MIN_GENDER_RATIO,
) -> Glossary:
    """Classify unclassified glossary terms by gender, from the ENGLISH source.

    This is the anchored fact the summary's naming rule cannot supply. The
    rule stops the model needing to invent a gender to tell two referents
    apart; this says which gender is actually true, so a chunk whose own text
    gives no evidence about a character (chunk 19: he/his=2, both the other
    character, she/her=0) is no longer a coin flip.

    The source is the ENGLISH original, never a translation: a Spanish
    rendering already carries the fabricated agreement this exists to correct,
    so seeding from it would launder the defect into a fact.

    Only entries with no gender are touched. One already set was either seeded
    from a fuller text or chosen by a human, and re-counting pronouns must not
    overrule either.

    Only IDENTITY entries are eligible — a term whose Spanish rendering is the
    term itself. A translated noun's gender is grammatical and belongs to the
    Spanish word, so declaring "Concurrencia" masculine because the English
    "Concurrence" stood near "he" would replace a correct agreement with a
    wrong one. Characters are almost always identity entries, which is the
    same property that made a per-entry ``note`` unworkable as the channel.

    A term below the margin keeps ``gender=None`` — unclassified, never
    guessed. That is the same discipline ``first_draw`` follows for an unknown
    draw, and it costs nothing: an unanchored character is still covered by
    the naming rule, which is the part that removes the incentive to invent.
    """
    if all(entry.gender is not None for entry in glossary.entries):
        return glossary

    entries: list[GlossaryEntry] = []
    for entry in glossary.entries:
        term = normalize_term(entry.term)
        eligible = (
            entry.gender is None
            # Only a name carried into Spanish UNCHANGED. Once a term is
            # translated its Spanish gender is grammatical and belongs to the
            # Spanish noun, whatever the English referent was.
            and bool(term)
            and term == normalize_term(entry.translation)
        )
        counts = evidence.get(term.casefold()) if eligible else None
        gender = (
            _classify_gender(counts[0], counts[1], min_mentions, min_ratio)
            if counts is not None
            else None
        )
        entries.append(
            entry.model_copy(update={"gender": gender}) if gender else entry
        )
    return Glossary(entries=entries)
