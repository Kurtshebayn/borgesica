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

from borgesica.domain.models import (
    Glossary,
    GlossaryEntry,
    JobConfig,
    normalize_term,
)
from borgesica.domain.ports import TranslationProvider

__all__ = [
    "LlmGlossaryExtractor",
    "NullGlossaryExtractor",
    "dedupe_glossary",
    "drop_reversed_entries",
    "get_extractor",
    "merge_additions",
    "normalize_term",
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
        return dedupe_glossary(Glossary(entries=list(result.unit.glossary_additions)))


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


def dedupe_glossary(glossary: Glossary) -> Glossary:
    """Collapse case- and whitespace-only duplicates, preserving order.

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

    for entry in glossary.entries:
        term = normalize_term(entry.term)
        if not term:
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
        elif incumbent.note is None and candidate.note is not None:
            winners[key] = incumbent.model_copy(update={"note": candidate.note})

    return Glossary(entries=[winners[key] for key in order])


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
    deduped = dedupe_glossary(glossary)
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

    cleaned, _dropped = drop_reversed_entries(Glossary(entries=new_entries))
    return cleaned
