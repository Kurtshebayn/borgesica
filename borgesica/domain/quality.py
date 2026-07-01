"""Domain quality harness — LLM-as-judge evaluation (M4-2).

Dependency rule: only stdlib + pydantic allowed here.  No anthropic/openai/nltk/sacrebleu.

Tier structure:
    Tier-2 (evaluate):  One judge LLM call per translation. Returns a clean QualityScore.
    Tier-3 (back_translation_similarity): One back-translation call per invocation. Returns
        a float in [0.0, 1.0] computed by stdlib difflib. Called separately — callers opt in
        by calling this method explicitly; skipping it costs zero extra provider calls.

Design note (TranslationUnit-as-carrier):
    The TranslationProvider Protocol exposes only
    `translate(system, user, model) → TranslationUnit`.
    The judge needs a QualityScore, not a TranslationUnit — a contract mismatch.

    # NOTE: Deliberate, CONTAINED contract reuse.  The QualityHarness calls
    # provider.translate() with a JUDGE system prompt that instructs the model to return
    # its evaluation as a JSON object with the four QualityScore fields.  The model
    # serialises that JSON into the `translation` field of the returned TranslationUnit;
    # the harness then parses it with QualityScore.model_validate_json().
    # The `summary_update` field carries a one-line rationale (ignored by the harness).
    #
    # This is intentional and DOES NOT modify the TranslationProvider Protocol, which
    # would ripple to AnthropicProvider and the upcoming OpenAICompatibleProvider.
    #
    # TODO: if LLM-as-judge becomes first-class, introduce a generic
    #       structured_completion(system, user, schema, model) method on the port
    #       instead of reusing translate().

Back-translation similarity (Tier-3):
    back_translation_similarity(source, translation, model) translates the Spanish output
    back to English via one provider call, then computes a stdlib similarity ratio via
    difflib.SequenceMatcher.  No external NLP library is required.
    This is a SEPARATE method from evaluate() — call it independently when Tier-3 is desired.
"""
from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field

from pydantic import ValidationError

from borgesica.domain.errors import MalformedOutput
from borgesica.domain.models import Glossary, QualityScore
from borgesica.domain.ports import TranslationProvider

# ---------------------------------------------------------------------------
# Advisory result
# ---------------------------------------------------------------------------


@dataclass
class AdvisoryResult:
    """Result of advisory_gate().

    `passed` is True iff ALL four dimensions score >= 4.
    `failing_dimensions` lists the names of dimensions that scored < 4.
    This is ADVISORY — it never raises; the caller decides how to act.
    """

    passed: bool
    failing_dimensions: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure advisory helper
# ---------------------------------------------------------------------------

_PASS_THRESHOLD = 4  # all dims must be >= this to pass


def advisory_gate(score: QualityScore) -> AdvisoryResult:
    """Pure function: evaluate a QualityScore against the CI advisory pass threshold.

    Returns an AdvisoryResult indicating PASS or FAIL (advisory only — never raises).
    Any dimension < 4 produces a FAIL advisory for that dimension.

    Spec reference: quality-evaluation / "CI advisory gate fails when any dimension < 4"
    """
    failing: list[str] = []
    dims = {
        "accuracy": score.accuracy,
        "fluency": score.fluency,
        "neutral_register": score.neutral_register,
        "glossary_consistency": score.glossary_consistency,
    }
    for dim_name, value in dims.items():
        if value < _PASS_THRESHOLD:
            failing.append(dim_name)

    return AdvisoryResult(passed=len(failing) == 0, failing_dimensions=failing)


# ---------------------------------------------------------------------------
# Judge prompt builders
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM_PROMPT = """\
You are an expert translation quality judge for English → Spanish (neutral register) translation.

Your task is to evaluate a Spanish translation against the original English source.

Score the translation on EXACTLY these four dimensions, each on a scale of 1–5:
  1  = very poor
  3  = acceptable
  5  = excellent

Dimensions:
1. accuracy           — semantic fidelity to the source; meaning, intent, and nuance preserved.
2. fluency            — grammatical correctness and natural flow in Spanish; reads like a native.
3. neutral_register   — absence of voseo, regional slang (che, tío, órale, etc.), and leísmo;
                        register is neutral and consistent throughout.
4. glossary_consistency — locked glossary terms appear VERBATIM in the translated text exactly
                          as specified. Score 1 if any locked term is missing or altered.

Output ONLY a JSON object with these four keys and integer values — NO prose, NO markdown fences.
Example: {"accuracy": 4, "fluency": 5, "neutral_register": 4, "glossary_consistency": 5}

Use the `summary_update` field for a ONE-LINE rationale (required by the output schema).
""".strip()

_BACK_TRANSLATE_SYSTEM_PROMPT = """\
Translate the following Spanish text back to English.
Return ONLY the translated English text, nothing else.
""".strip()


def _build_judge_user_message(
    source: str,
    translation: str,
    glossary: Glossary,
) -> str:
    """Build the user turn for the judge prompt."""
    parts: list[str] = []
    parts.append(f"[SOURCE (English)]\n{source.strip()}")
    parts.append(f"[TRANSLATION (Spanish)]\n{translation.strip()}")

    glossary_text = glossary.render(budget_tokens=300)
    if glossary_text.strip():
        parts.append(f"[GLOSSARY — locked terms must appear VERBATIM]\n{glossary_text}")
    else:
        parts.append("[GLOSSARY]\n(none)")

    parts.append(
        'Evaluate the translation and return ONLY the JSON object with keys: '
        '"accuracy", "fluency", "neutral_register", "glossary_consistency".'
    )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# QualityHarness
# ---------------------------------------------------------------------------


class QualityHarness:
    """LLM-as-judge quality harness.

    Tier-2 (evaluate): makes exactly ONE judge provider call per invocation and returns
    a clean QualityScore.  The provider is reused via the TranslationUnit-as-carrier
    pattern (see module docstring).

    Tier-3 (back_translation_similarity): makes exactly ONE back-translation provider call
    and returns a stdlib difflib similarity ratio in [0.0, 1.0].  Call this method
    independently when Tier-3 back-translation is desired; simply not calling it costs
    zero extra provider calls.
    """

    def __init__(self, provider: TranslationProvider) -> None:
        self._provider = provider

    def evaluate(
        self,
        source: str,
        translation: str,
        glossary: Glossary,
        model: str,
    ) -> QualityScore:
        """Evaluate a translation and return a QualityScore (Tier-2 judge).

        Makes exactly ONE provider call (the judge call).  No back-translation is
        performed here — call back_translation_similarity() separately for Tier-3.

        Args:
            source:      The original English source text.
            translation: The Spanish translation to evaluate.
            glossary:    The job glossary (locked terms injected into judge prompt).
            model:       The model to use for the judge call.

        Returns:
            A valid QualityScore instance (pure 4-field rubric).

        Raises:
            MalformedOutput: if the judge returns non-JSON, invalid JSON, or out-of-range
                             values that fail Pydantic validation.
        """
        # --- Judge call (Tier-2) ---
        system_prompt = self._build_judge_system_prompt(glossary)
        user_message = _build_judge_user_message(source, translation, glossary)

        result = self._provider.translate(system_prompt, user_message, model)

        # Parse QualityScore from the TranslationUnit.translation field (the carrier).
        return self._parse_score(result.unit.translation)

    def back_translation_similarity(
        self,
        source: str,
        translation: str,
        model: str,
    ) -> float:
        """Compute back-translation similarity ratio (Tier-3).

        Makes exactly ONE provider call: translates the Spanish `translation` back to
        English, then computes a stdlib difflib.SequenceMatcher ratio against `source`.

        Args:
            source:      The original English source text.
            translation: The Spanish translation to back-translate.
            model:       The model to use for the back-translation call.

        Returns:
            A float in [0.0, 1.0] where 1.0 means identical strings.
        """
        back_translated = self._back_translate(translation, model)
        return self._compute_similarity(source, back_translated)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_judge_system_prompt(self, glossary: Glossary) -> str:
        """Combine the static judge prompt with glossary context."""
        prompt_parts = [_JUDGE_SYSTEM_PROMPT]

        # Inject locked glossary terms explicitly into the system prompt so the
        # judge knows exactly what terms are locked.
        locked = [e for e in glossary.entries if e.locked]
        if locked:
            locked_lines = "\n".join(f"  - {e.term} → {e.translation}" for e in locked)
            prompt_parts.append(
                f"[LOCKED GLOSSARY TERMS — must appear VERBATIM in the translation]\n"
                f"{locked_lines}"
            )

        return "\n\n".join(prompt_parts)

    def _parse_score(self, raw: str) -> QualityScore:
        """Parse a JSON string into QualityScore; raise MalformedOutput on failure."""
        # Strip potential markdown fences that a weak model might add.
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            # Drop opening ``` line and closing ``` line if present.
            lines = [ln for ln in lines if not ln.strip().startswith("```")]
            cleaned = "\n".join(lines).strip()

        try:
            score = QualityScore.model_validate_json(cleaned)
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise MalformedOutput(
                job_id="quality-harness",
                chunk_index=-1,
            ) from exc

        return score

    def _back_translate(self, spanish_text: str, model: str) -> str:
        """Translate the Spanish text back to English via the provider."""
        result = self._provider.translate(
            _BACK_TRANSLATE_SYSTEM_PROMPT,
            spanish_text.strip(),
            model,
        )
        return result.unit.translation

    @staticmethod
    def _compute_similarity(original: str, back_translated: str) -> float:
        """Compute a stdlib SequenceMatcher similarity ratio between two strings.

        Returns a float in [0.0, 1.0] where 1.0 is identical.
        Uses difflib.SequenceMatcher — stdlib only, no nltk/sacrebleu.
        """
        return difflib.SequenceMatcher(
            None,
            original.strip().lower(),
            back_translated.strip().lower(),
        ).ratio()
