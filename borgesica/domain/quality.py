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
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import ValidationError

from borgesica.domain.errors import MalformedOutput
from borgesica.domain.models import (
    DEFAULT_GLOSSARY_BUDGET_TOKENS,
    Glossary,
    QualityScore,
    normalize_term,
)
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
    glossary_budget_tokens: int = DEFAULT_GLOSSARY_BUDGET_TOKENS,
) -> str:
    """Build the user turn for the judge prompt."""
    parts: list[str] = []
    parts.append(f"[SOURCE (English)]\n{source.strip()}")
    parts.append(f"[TRANSLATION (Spanish)]\n{translation.strip()}")

    glossary_text = glossary.render(budget_tokens=glossary_budget_tokens)
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
        glossary_budget_tokens: int = DEFAULT_GLOSSARY_BUDGET_TOKENS,
    ) -> QualityScore:
        """Evaluate a translation and return a QualityScore (Tier-2 judge).

        Makes exactly ONE provider call (the judge call).  No back-translation is
        performed here — call back_translation_similarity() separately for Tier-3.

        Args:
            source:      The original English source text.
            translation: The Spanish translation to evaluate.
            glossary:    The job glossary (locked terms injected into judge prompt).
            model:       The model to use for the judge call.
            glossary_budget_tokens: Word budget for the glossary block. Must match
                         the budget the TRANSLATOR ran with (JobConfig.
                         glossary_budget_tokens) — the judge scores a
                         glossary_consistency dimension, so a more aggressively
                         trimmed view here would penalise the model for terms it
                         was never shown.

        Returns:
            A valid QualityScore instance (pure 4-field rubric).

        Raises:
            MalformedOutput: if the judge returns non-JSON, invalid JSON, or out-of-range
                             values that fail Pydantic validation.
        """
        # --- Judge call (Tier-2) ---
        system_prompt = self._build_judge_system_prompt(glossary)
        user_message = _build_judge_user_message(
            source, translation, glossary, glossary_budget_tokens
        )

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


# ---------------------------------------------------------------------------
# Deterministic character-gender detector
# ---------------------------------------------------------------------------
#
# Post-hoc, free, and makes ZERO provider calls. Everything it needs is
# already persisted — chunks.source_text, chunks.translated_text and the
# summaries table — so a finished job can be audited without paying to run it
# again.
#
# It FLAGS; it never retries. A retry would feed the model the same poisoned
# summary and reproduce the same output. The order that fixes anything is
# anchor -> re-pass -> re-run this detector to confirm it reaches zero.

# Agreement is checked only against a word standing DIRECTLY beside the name.
# That is the shape of the defect that shipped ("—Tranquila, Vis") and it is
# the only position where attribution is safe: at any distance the gendered
# word usually belongs to another character on stage, and a detector whose
# findings are mostly wrong is one nobody reads. Recall is deliberately traded
# for precision — distant first-person narration is out of reach, because
# narrator and dialogue cannot be separated reliably.
_AGREEMENT_WINDOW_TOKENS = 1

# Participle endings, which mark agreement reliably.
_FEMININE_SUFFIXES = ("ada", "adas", "ida", "idas")
_MASCULINE_SUFFIXES = ("ado", "ados", "ido", "idos")

# Plus the handful of plain adjectives common enough in vocative dialogue to
# be worth naming outright. "tranquilo/a" is here because it is the exact word
# the book got wrong.
_GENDERED_ADJECTIVES = {
    "tranquilo": "masculine", "tranquila": "feminine",
    "quieto": "masculine", "quieta": "feminine",
    "listo": "masculine", "lista": "feminine",
    "seguro": "masculine", "segura": "feminine",
    # "solo" is absent on purpose: it is overwhelmingly the adverb "only" and
    # marks no agreement, which is what produced the last surviving false
    # positive across a full book's summaries. "sola" has no adverbial sense.
    "sola": "feminine",
    "muerto": "masculine", "muerta": "feminine",
    "vivo": "masculine", "viva": "feminine",
    "loco": "masculine", "loca": "feminine",
    "viejo": "masculine", "vieja": "feminine",
    "nuevo": "masculine", "nueva": "feminine",
}

# Nouns and verb forms carrying a participle ending without being agreement.
# "—Nada, Vis" is not a feminine Vis. The list is deliberately short: every
# entry is a word measured or expected to sit next to a name in dialogue, and
# it is meant to grow from observed false positives rather than from guesses.
_NOT_AGREEMENT = frozenset({
    "nada", "cada", "vida", "comida", "salida", "entrada", "mirada",
    "llamada", "espada", "jornada", "manada", "partida", "medida",
    "ruido", "sonido", "sentido", "olvido", "vestido", "pedido",
})

_SENTENCE_BREAK = re.compile(r"[.!?;:\n\u2026]")
_SPANISH_WORD = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+")

# Prepositions that make the name a complement rather than the thing being
# described, so a following adjective agrees with whatever came before it.
_COMPLEMENT_MARKERS = frozenset({"de", "del", "en"})


def _opens_clause(text: str, tokens: list[re.Match[str]], index: int) -> bool:
    """True when the token at ``index`` is the first word of its clause.

    Anything in front of it must be punctuation — the raya opening a spoken
    turn, a quote, an opening question mark — never another word.
    """
    if index == 0:
        return True
    gap = text[tokens[index - 1].end():tokens[index].start()]
    return bool(_SENTENCE_BREAK.search(gap))


@dataclass(frozen=True)
class GenderDefect:
    """One place a translation disagrees with a character's anchored gender."""

    name: str
    expected: str
    found: str
    marker: str
    excerpt: str


def _agreement_gender(word: str) -> str | None:
    """Return the gender a word marks, or None when it marks none."""
    lowered = word.casefold()
    if lowered in _NOT_AGREEMENT:
        return None
    if lowered in _GENDERED_ADJECTIVES:
        return _GENDERED_ADJECTIVES[lowered]
    if lowered.endswith(_FEMININE_SUFFIXES):
        return "feminine"
    if lowered.endswith(_MASCULINE_SUFFIXES):
        return "masculine"
    return None


def detect_gender_defects(text: str, glossary: Glossary) -> list[GenderDefect]:
    """Return every disagreement between ``text`` and the glossary's anchors.

    Deterministic and free: no provider call, no persisted verdict. A verdict
    is worth storing only when recomputing it costs money (the LLM judge,
    back-translation); this one is cheaper to redo than to look up.

    Characters with no anchored gender are skipped. Unclassified means
    unknown, and an unknown expectation cannot be violated — flagging one
    would invent exactly the fact the seeder declined to guess.

    A ZERO RESULT IS NOT A CLEAN BILL OF HEALTH. Run over the finished book
    this returns 4 findings in 569 chunks and 0 across all 569 summaries —
    yet 156 of those summaries carry a fabricated feminine marker. The
    fabrication is mostly a bare "ella" standing nowhere near a name, and
    adjacency is exactly what this refuses to guess past. It measures the
    defect that reaches the READER, never the contamination in the context;
    the latter is counted by looking for feminine markers in summaries that
    name no female character, which is a different question.
    """
    expected_by_name = {
        normalize_term(e.term).casefold(): e.gender
        for e in glossary.entries
        if e.gender is not None
    }
    if not expected_by_name:
        return []

    tokens = list(_SPANISH_WORD.finditer(text))
    defects: list[GenderDefect] = []

    for position, token in enumerate(tokens):
        expected = expected_by_name.get(token.group().casefold())
        if expected is None:
            continue

        before = tokens[position - 1] if position else None
        # A name introduced by a preposition is a complement, and any
        # agreement that follows belongs to the head noun in front of it:
        # "la voz de Caeror, apagada" describes the voz. Measured on the real
        # book, this was the single largest false-positive class.
        takes_trailing = before is None or before.group().casefold() not in _COMPLEMENT_MARKERS

        candidates = []
        if position and _opens_clause(text, tokens, position - 1):
            # Vocative agreement opens its clause ("—Tranquila, Vis"). A
            # marker with words in front of it inside the same clause belongs
            # to that phrase and merely lands beside the name: "alguien
            # llamado Netiqret", "En un momento dado, Kiya", "de nuevo, Vis".
            candidates.append(tokens[position - 1])
        if takes_trailing and position + 1 < len(tokens):
            candidates.append(tokens[position + 1])

        for neighbour in candidates:
            between = (
                text[neighbour.end():token.start()]
                if neighbour.start() < token.start()
                else text[token.end():neighbour.start()]
            )
            if _SENTENCE_BREAK.search(between):
                continue
            found = _agreement_gender(neighbour.group())
            if found is None or found == expected:
                continue
            start = max(0, min(token.start(), neighbour.start()) - 40)
            end = max(token.end(), neighbour.end()) + 40
            defects.append(
                GenderDefect(
                    name=token.group(),
                    expected=expected,
                    found=found,
                    marker=neighbour.group(),
                    excerpt=" ".join(text[start:end].split()),
                )
            )
    return defects


# ---------------------------------------------------------------------------
# Deterministic do-not-translate detector
# ---------------------------------------------------------------------------
#
# Same shape as the gender detector above: post-hoc, free, ZERO provider calls,
# reading only what is already persisted (chunks.source_text and
# chunks.translated_text).
#
# It exists because the KEEP INVENTED LANGUAGE VERBATIM prompt rule is an
# INSTRUCTION, not a guarantee. "Catenicus → Catenicus" sat in the glossary of
# every job as an identity entry — which renders as a DO NOT TRANSLATE line —
# and two runs still emitted "Catenico" anyway. An identity entry makes a
# checkable promise; this is the check.

# Word characters on either side of a term, so "Caten" does not match inside
# "Catenicus". Matching is CASE-INSENSITIVE: Spanish word order routinely moves
# a term to the front of its sentence and capitalises it, and a capital is not
# an erasure. Case-sensitive matching cut precision on the real book from one
# finding in two to one in four.
_TERM_BOUNDARY = (r"(?<!\w)", r"(?!\w)")

_SOURCE_WORD = re.compile(r"[A-Za-z][A-Za-z'\u2019]*")


def lowercase_vocabulary(source_text: str) -> frozenset[str]:
    """Return every word the source ever uses in genuine lowercase.

    Separate from the detector for the reason ``character_gender_evidence`` is
    separate from ``seed_character_gender``: it is the expensive half, the
    source never changes during a run, and the answer is the same for all 569
    chunks.

    Only a GENUINE lowercase spelling counts. Treating any case variant as
    evidence looked equivalent and was not: the book sets chapter headings in
    capitals, so "CAEROR" appears throughout, and an upper-case test dropped
    Caeror, Caten, Catenicus and Livia from the guarded set — the exact terms
    worth guarding. This is the same rule, and the same reasoning, as the
    common-noun filter in ``character_gender_evidence``.
    """
    return frozenset(
        match.group()
        for match in _SOURCE_WORD.finditer(source_text)
        if match.group().islower()
    )


@dataclass(frozen=True)
class UntranslatedDefect:
    """One place a do-not-translate term failed to survive into the output."""

    term: str
    occurrences: int
    excerpt: str


def detect_untranslated_defects(
    source: str,
    translation: str,
    glossary: Glossary,
    *,
    vocabulary: frozenset[str] = frozenset(),
) -> list[UntranslatedDefect]:
    """Return every identity glossary term that vanished from the translation.

    Only IDENTITY entries (term == translation once normalised) are checked.
    Those are the entries that promise the word is carried over unchanged; a
    MAPPING asks for a change, so the source term being absent is that rule
    working rather than breaking.

    ``vocabulary`` is the whole book's lowercase words, from
    ``lowercase_vocabulary``. A capitalised term whose lowercase spelling the
    source also uses is ordinary vocabulary that reached the glossary by
    mistake, and translating it is correct. Passing nothing skips the filter
    and is much noisier: on the real book "Thrum" alone — a low vibrating
    sound, used lowercase throughout — produced 10 of 12 findings, every one
    of them wrong.

    Measured on job 9be143da (569 chunks, 278 identity entries of 549): TWO
    findings.
      - ch32  "Quintus" rendered "Quinto" — the defect this exists for, the
        same hispanicisation that produced "Catenico" in two earlier runs.
      - ch224 "iunctus" rendered "iunctii" — a Latin plural, and the known
        false-positive class. INFLECTION is not erasure, but separating the
        two needs a stemmer this domain module has no business carrying, and
        one false positive in a whole book is cheap to dismiss by eye.

    Like the gender detector, this FLAGS and never retries: a retry would feed
    the model the same glossary and reproduce the same output. It also shares
    that detector's limit — a zero result means no identity term disappeared,
    not that the translation is faithful.
    """
    before, after = _TERM_BOUNDARY
    defects: list[UntranslatedDefect] = []

    for entry in glossary.entries:
        term = normalize_term(entry.term)
        if not term or term != normalize_term(entry.translation):
            continue
        if term.lower() != term and term.lower() in vocabulary:
            continue

        pattern = re.compile(before + re.escape(term) + after, re.IGNORECASE)
        matches = pattern.findall(source)
        if not matches or pattern.search(translation):
            continue

        first = pattern.search(source)
        assert first is not None  # `matches` is non-empty
        start = max(0, first.start() - 40)
        defects.append(
            UntranslatedDefect(
                term=term,
                occurrences=len(matches),
                excerpt=" ".join(source[start:first.end() + 40].split()),
            )
        )
    return defects


ContradictionRule = Literal["kept_alone_changed_inside", "changed_alone_kept_inside"]

# What each rule means, worded for the reader of ``borgesica audit``. Keyed by
# rule so a rule without wording is a lookup failure, never a fall-through to
# another rule's explanation.
_CONTRADICTION_WORDING: dict[ContradictionRule, str] = {
    "kept_alone_changed_inside": "{short!r} is kept on its own but not inside {long!r}",
    "changed_alone_kept_inside": "{short!r} is translated on its own but kept inside {long!r}",
}


@dataclass(frozen=True)
class GlossaryContradiction:
    """Two glossary entries that disagree about whether a term is carried over.

    ``short_term`` occurs as whole words inside ``long_term``. The rules are
    worded in ``_CONTRADICTION_WORDING``: "kept_alone_changed_inside" keeps the
    term alone but drops it inside the compound, "changed_alone_kept_inside"
    the reverse.
    """

    rule: ContradictionRule
    short_term: str
    short_translation: str
    long_term: str
    long_translation: str

    @property
    def detail(self) -> str:
        """The rule this pair breaks, in words. Raises on an unknown rule."""
        wording = _CONTRADICTION_WORDING.get(self.rule)
        if wording is None:
            raise ValueError(f"unknown glossary contradiction rule {self.rule!r}")
        return wording.format(short=self.short_term, long=self.long_term)


def detect_glossary_contradictions(glossary: Glossary) -> list[GlossaryContradiction]:
    """Return every pair of entries that contradict each other about a term.

    For every ordered pair (short, long) of entries with case-insensitively
    distinct terms, where ``long.term`` contains ``short.term`` as whole words,
    the short term is "kept" when ``short.translation`` contains it as whole
    words. "kept_alone_changed_inside" fires when it is kept but
    ``long.translation`` does not contain it; "changed_alone_kept_inside" when
    it is not kept but ``long.translation`` does.

    This checks the glossary against ITSELF, which no per-chunk detector can
    do. A bad entry is born once and injected into every later prompt: five
    repeated extracts of the chunk introducing "Quintus Darinus" produced
    ``Quintus Darinus -> Quinto Darino`` beside ``Quintus -> Quintus`` in one
    first draw of six, and that one draw steers the rest of the book.

    "Kept" is CONTAINMENT, never equality. Equality produced 21 false positives
    in 21 findings on job 9be143da — ``Magnus -> el Magnus`` and
    ``ap -> ap (hijo de)`` keep the term and merely add an article or a gloss.

    Measured on real glossaries before implementation: 9 of 9 findings real on
    the two tuning jobs, 8 of 11 on four held-out jobs. The three false
    positives were two HTML tags a local model extracted as terms and one
    reversed entry. They are deliberately NOT filtered: a filter built from the
    held-out misses would tune on the held-out set and hide the precision the
    rule really has.

    Pairwise over the entries, so O(n^2); a substring pre-check keeps the
    regex off almost every pair.
    """
    before, after = _TERM_BOUNDARY
    entries = [
        (term, translation, re.compile(before + re.escape(term) + after, re.IGNORECASE))
        for term, translation in (
            (entry.term.strip(), entry.translation.strip())
            for entry in glossary.entries
        )
        if term and translation
    ]
    findings: list[GlossaryContradiction] = []

    for short_term, short_translation, pattern in entries:
        folded = short_term.lower()
        kept = pattern.search(short_translation) is not None
        for long_term, long_translation, _ in entries:
            if long_term.lower() == folded or folded not in long_term.lower():
                continue
            if not pattern.search(long_term):
                continue
            in_long = pattern.search(long_translation) is not None
            if kept == in_long:
                continue
            findings.append(
                GlossaryContradiction(
                    rule="kept_alone_changed_inside" if kept else "changed_alone_kept_inside",
                    short_term=short_term,
                    short_translation=short_translation,
                    long_term=long_term,
                    long_translation=long_translation,
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Whole-job audit — every free detector, one pass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditedDefect:
    """One finding, tagged with the chunk and the detector that produced it.

    The detectors report different shapes (a gender disagreement names an
    expectation and a marker; a vanished term names a count). They are
    flattened into one record because the consumer is a reader triaging a
    finished book, and a single ordered list is what that reader wants. `kind`
    keeps the distinction that matters.

    ``chunk_index`` is None for a finding about the JOB rather than a chunk —
    a glossary contradiction lives in the glossary every chunk was given.
    None rather than the -1 sentinel used elsewhere, because this record is
    printed as JSON for a reader, and -1 there reads as a chunk number.
    """

    chunk_index: int | None
    kind: str  # "glossary" | "untranslated" | "gender"
    term: str
    detail: str
    excerpt: str


def audit_chunks(
    chunks: Sequence[tuple[int, str, str]],
    glossary: Glossary,
) -> list[AuditedDefect]:
    """Run every FREE detector over a finished job's chunks.

    Takes ``(chunk_index, source_text, translated_text)`` triples. Glossary
    contradictions come first, once for the whole job and with no chunk index;
    then the per-chunk findings in chunk order, untranslated terms before
    gender defects within a chunk.

    This exists to own the one thing a caller cannot get right by looping:
    ``lowercase_vocabulary`` must be built from the WHOLE source, once. A
    per-chunk vocabulary sees far too little text to recognise a common noun,
    and skipping it entirely takes job 9be143da from 2 findings to 12 — ten of
    them the word "thrum". Building it here makes the correct usage the only
    usage, and costs one pass over the source rather than 569.

    Makes ZERO provider calls, which is the premise: a finished 569-chunk book
    can be audited as often as you like, including after a hand edit. The LLM
    judge is the opposite trade and is not called here.

    A ZERO RESULT IS NOT A CLEAN BILL OF HEALTH — every detector buys
    precision with recall, and none reads meaning. See their own docstrings for
    what each one cannot see.
    """
    vocabulary = lowercase_vocabulary("\n".join(source for _, source, _ in chunks))
    findings: list[AuditedDefect] = [
        AuditedDefect(
            chunk_index=None,
            kind="glossary",
            term=contradiction.short_term,
            detail=contradiction.detail,
            excerpt=(
                f"{contradiction.short_term} -> {contradiction.short_translation}"
                f" | {contradiction.long_term} -> {contradiction.long_translation}"
            ),
        )
        for contradiction in detect_glossary_contradictions(glossary)
    ]

    for index, source, translation in chunks:
        for term_defect in detect_untranslated_defects(
            source, translation, glossary, vocabulary=vocabulary
        ):
            findings.append(
                AuditedDefect(
                    chunk_index=index,
                    kind="untranslated",
                    term=term_defect.term,
                    detail=(
                        f"{term_defect.occurrences} occurrence(s) in the source, "
                        f"none in the translation"
                    ),
                    excerpt=term_defect.excerpt,
                )
            )
        for gender_defect in detect_gender_defects(translation, glossary):
            findings.append(
                AuditedDefect(
                    chunk_index=index,
                    kind="gender",
                    term=gender_defect.name,
                    detail=(
                        f"expected {gender_defect.expected}, found "
                        f"{gender_defect.found} ({gender_defect.marker})"
                    ),
                    excerpt=gender_defect.excerpt,
                )
            )
    return findings
