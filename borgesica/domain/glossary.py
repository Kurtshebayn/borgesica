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
)
from borgesica.domain.ports import TranslationProvider

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
        return Glossary(entries=list(result.unit.glossary_additions))


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
# Mid-run addition merge helper (used by orchestrator in M1-8)
# ---------------------------------------------------------------------------


def merge_additions(glossary: Glossary, additions: list[GlossaryEntry]) -> Glossary:
    """Merge mid-run glossary_additions into the live glossary.

    Rules (from spec context-continuity/mid-run-additions):
    1. If a LOCKED entry with the same term exists → silently discard.
    2. If an unlocked entry with the same term exists → skip (no duplicate).
    3. If the term is entirely new → add as locked=False.

    Returns a new Glossary (models are immutable Pydantic objects).
    """
    existing_locked = {e.term for e in glossary.entries if e.locked}
    existing_terms = {e.term for e in glossary.entries}

    new_entries = list(glossary.entries)
    for addition in additions:
        if addition.term in existing_locked:
            # Rule 1: locked entry takes precedence — discard silently
            continue
        if addition.term in existing_terms:
            # Rule 2: already present unlocked — skip duplicate
            continue
        # Rule 3: new term — add as unlocked
        new_entries.append(
            GlossaryEntry(
                term=addition.term,
                translation=addition.translation,
                locked=False,
                note=addition.note,
            )
        )
        existing_terms.add(addition.term)

    return Glossary(entries=new_entries)
