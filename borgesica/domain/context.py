"""ContextManager — system prompt assembly for the translation engine.

Dependency rule: only stdlib + pydantic + domain models/ports allowed.
No I/O, no adapter imports.

Design (M1-5):
  build_system_prompt(config, glossary, summary) -> SystemPrompt
    SystemPrompt.text  = static_block + dynamic_block
    SystemPrompt.cached = True iff provider.count_tokens(static_block) >= 1024

The static block is cacheable (Anthropic prompt-caching boundary).
The dynamic block appends glossary + rolling summary (changes per chunk).
"""
from __future__ import annotations

from dataclasses import dataclass

from borgesica.domain.models import Glossary, JobConfig, RollingSummary, SourceType
from borgesica.domain.ports import TranslationProvider  # noqa: I001

# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SystemPrompt:
    """Assembled system prompt for a single translation call.

    Attributes:
        text: Full system prompt string (static + dynamic).
        cached: True iff the static block meets the provider's minimum cacheable
                size (≥ 1024 tokens by Anthropic convention). Adapters honor or
                ignore this hint; no exception is raised if they ignore it.
    """

    text: str
    cached: bool


# ---------------------------------------------------------------------------
# Translation-philosophy constants
# ---------------------------------------------------------------------------

_PHILOSOPHY = """\
## Translation Philosophy

Translate the MEANING and the physical IMAGE conveyed by the source, not a \
word-for-word mapping. When a source phrase describes a physical action or \
object, carry over what that object DOES or IS in context — do not translate \
its components literally if a literal rendering would confuse a Spanish reader.

Avoid literal calques (calcos literales): do not render English phrases by \
substituting each word with its Spanish dictionary equivalent when the result \
is unnatural or misleading. For example, "a locking wooden beam" across a door \
is a "tranca" — a bar that itself IS the locking mechanism — not "una viga con \
cerradura". Translate MEANING, not words.

Prioritize naturalness while remaining faithful to the source. The output must \
read as if originally written in fluent, professional Spanish — never as a \
translation artifact."""


# ---------------------------------------------------------------------------
# Neutral-Spanish rules constants
# All 5 rules are detectable as substrings in the prompt text.
# ---------------------------------------------------------------------------

_NEUTRAL_SPANISH = """\
## Neutral Spanish Rules (target_lang = es-neutral)

These rules apply to ALL translations in this job. Violating any of them is \
an error.

1. No voseo: Do NOT use "vos" as the second-person singular pronoun or its \
   conjugations. Use "tú" or impersonal constructions instead. \
   (Banned: "vos sabés", "hacé". Correct: "tú sabes", "haz".)

2. No regional slang or localisms: Do NOT use region-specific slang such as \
   "che", "tío", "órale", "pata", "bacán", "macanudo", or any other localism \
   not universally understood across Latin American Spanish-speaking countries.

3. No leísmo: Do NOT use "le" as a direct-object pronoun when the referent is \
   a person who is the direct object of the verb. Follow RAE standard: \
   "lo/la" for direct object, "le/les" only for indirect object.

4. Consistent register: Maintain the register (formal or informal) established \
   at the start of the job. Do NOT allow register drift within a single job. \
   If the source is informal dialogue, the target must be consistently informal \
   in neutral Spanish; if formal prose, consistently formal.

5. Consistent number/unit style: Use the same format for spelled-out numbers, \
   ordinals, and units of measure throughout the job, consistent with the \
   register. Do not mix "5 kilómetros" and "cinco kilómetros" in the same \
   document without a stylistic reason."""


# ---------------------------------------------------------------------------
# Output format / task description
# ---------------------------------------------------------------------------

_INLINE_TAG_RULES = """\
## Inline Tag Preservation Rules

The source text may contain inline formatting tags such as <i>, </i>, <b>, \
</b>, <u>, </u>, <em>, </em>, <strong>, </strong>, <span ...>, </span>, \
<a ...>, </a>.

You MUST preserve every inline tag exactly as it appears:
1. Keep every inline tag in the output — do NOT drop or add any tag.
2. Move each tag WITH the word(s) it wraps: if <i>word</i> translates to \
   <i>palabra</i>, keep the tag pair around the translated word.
3. Preserve the EXACT tag count: the number of tags in the output MUST equal \
   the number of tags in the source. Any mismatch is an error.
4. Preserve nesting order: if tags are nested in the source, maintain the \
   same nesting in the translation.

Failure to preserve inline tags is a critical translation error."""


_TASK_DESCRIPTION = """\
## Task

You are a professional literary translator. Your task is to translate the \
provided subtitle or prose chunk from English to neutral, pan-Latin-American \
Spanish (es-neutral).

Return your response as a valid JSON object with EXACTLY the following fields:
  {
    "translation": "<translated text>",
    "summary_update": "<3–5 sentence narrative summary of what happened or \
was discussed in this chunk — REPLACES the prior summary, does NOT append>",
    "glossary_additions": [
      {"term": "<source term>", "translation": "<Spanish equivalent>", \
"locked": false, "note": "<optional context>"}
    ]
  }

Rules for summary_update:
- Write 3–5 sentences maximum.
- Capture narrative arc, tone, setting, and any new characters.
- This summary REPLACES the previous one — do NOT include prior context verbatim.
- Keep it under 200 tokens.

Rules for glossary_additions:
- Add ONLY proper nouns, invented terms, or domain-specific vocabulary first \
encountered in this chunk.
- Do NOT add common Spanish vocabulary.
- Leave the list empty ([]) if no new terms appear.

Rules for locked glossary terms (marked [LOCKED] below):
- You MUST use locked translations verbatim. They are NOT open to \
reinterpretation."""


_TASK_DESCRIPTION_SRT = """\
## Task

You are a professional literary translator. Your task is to translate the \
provided subtitle segments from English to neutral, pan-Latin-American \
Spanish (es-neutral).

The source is a batch of subtitle segments separated by blank lines. Each \
segment is one subtitle cue — often a mid-sentence fragment without \
punctuation (speech-to-text timing, NOT a formatting error). Together the \
segments form CONTINUOUS SPEECH: a sentence frequently starts in one segment \
and finishes in the next. First read the whole batch to understand the full \
passage, then translate it so meaning flows coherently across segment \
boundaries — never translate a fragment in isolation as if it were a complete \
sentence, while still preserving the fragment boundaries exactly in your \
output.

Example — a sentence split across two segments:
  Source segments:
    "I bought her a brand new"
    "watch for her birthday"
  WRONG (each fragment translated in isolation — "watch" misread as an \
imperative):
    ["le compré algo totalmente nuevo", "mira su cumpleaños"]
  RIGHT (whole sentence understood first, boundaries preserved):
    ["le compré un reloj", "nuevo para su cumpleaños"]

Return your response as a valid JSON object with EXACTLY the following fields:
  {
    "translations": ["<segment 1 translated>", "<segment 2 translated>", ...],
    "summary_update": "<3–5 sentence narrative summary of what happened or \
was discussed in this chunk — REPLACES the prior summary, does NOT append>",
    "glossary_additions": [
      {"term": "<source term>", "translation": "<Spanish equivalent>", \
"locked": false, "note": "<optional context>"}
    ]
  }

Rules for translations:
- Return EXACTLY one string per source segment, in the same order.
- NEVER merge, split, or reorder segments — even when a sentence clearly \
continues across segments, each fragment must stay its own array item.
- Do not add or remove segments to "fix" the formatting; the segmentation \
carries subtitle timing and is not negotiable.

Rules for summary_update:
- Write 3–5 sentences maximum.
- Capture narrative arc, tone, setting, and any new characters.
- This summary REPLACES the previous one — do NOT include prior context verbatim.
- Keep it under 200 tokens.

Rules for glossary_additions:
- Add ONLY proper nouns, invented terms, or domain-specific vocabulary first \
encountered in this chunk.
- Do NOT add common Spanish vocabulary.
- Leave the list empty ([]) if no new terms appear.

Rules for locked glossary terms (marked [LOCKED] below):
- You MUST use locked translations verbatim. They are NOT open to \
reinterpretation."""


# ---------------------------------------------------------------------------
# ContextManager
# ---------------------------------------------------------------------------

_ANTHROPIC_CACHE_MIN_TOKENS = 1024


class ContextManager:
    """Assembles the system prompt for each translation call.

    Pure domain — no I/O, no adapter imports.  The provider is injected
    only for its count_tokens capability (token budget calculation).

    Args:
        provider: A TranslationProvider used solely for count_tokens().
                  No translate() calls are made here.
    """

    def __init__(self, provider: "TranslationProvider") -> None:  # type: ignore[type-arg]
        self._provider = provider

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def build_system_prompt(
        self,
        config: JobConfig,
        glossary: Glossary,
        summary: RollingSummary,
    ) -> SystemPrompt:
        """Assemble the full system prompt for a single chunk.

        Structure:
          [STATIC — cacheable]
            Task description + output format
            Translation philosophy (meaning+image, no calques, naturalness)
            Neutral-Spanish rules (all 5, verbatim)
          [DYNAMIC — changes per chunk]
            [GLOSSARY] rendered table (≤ 300 tokens)
            [SUMMARY] rolling summary from chunk N-1 (≤ 200 tokens)

        Returns:
            SystemPrompt with .text and .cached fields.
        """
        static_block = self.get_static_block(config)
        dynamic_block = self._build_dynamic_block(glossary, summary)
        full_text = static_block + "\n\n" + dynamic_block

        token_count = self._provider.count_tokens(static_block, config.model)
        cached = token_count >= _ANTHROPIC_CACHE_MIN_TOKENS

        return SystemPrompt(text=full_text, cached=cached)

    def get_static_block(self, config: JobConfig) -> str:
        """Return the cacheable static instruction block.

        The static block never changes for a given config, so it is the
        caching boundary for Anthropic prompt caching.

        SRT jobs get the SEGMENTED task description (the "translations" array,
        one string per cue); prose jobs keep the legacy single-string contract.
        """
        task = (
            _TASK_DESCRIPTION_SRT
            if config.source_type == SourceType.SRT
            else _TASK_DESCRIPTION
        )
        parts = [task, _PHILOSOPHY, _INLINE_TAG_RULES]
        if config.target_lang == "es-neutral":
            parts.append(_NEUTRAL_SPANISH)
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_dynamic_block(
        self,
        glossary: Glossary,
        summary: RollingSummary,
    ) -> str:
        """Build the per-chunk dynamic section."""
        rendered_glossary = glossary.render(budget_tokens=300)
        summary_text = summary.text if summary.text else "No prior context."

        return (
            f"[GLOSSARY]\n{rendered_glossary}\n\n"
            f"[SUMMARY]\n{summary_text}"
        )
