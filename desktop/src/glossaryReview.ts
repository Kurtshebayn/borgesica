/**
 * Pure helpers for the glossary triage screen: how the server's review
 * (GET /jobs/{id}/glossary/review) is grouped, explained in plain words and
 * turned back into a PUT /jobs/{id}/glossary payload. No DOM, no fetch —
 * the same pure/glue split as `wizard.ts`, so all of it is unit-tested.
 *
 * The reader is not technical: every flag and gender verdict carries a plain
 * label and the evidence behind it, never an identifier.
 */
import type {
  GlossaryEntry,
  GlossaryGender,
  GlossaryReviewEntry,
  GlossaryReviewResponse,
  GlossaryRiskFlag,
  RenderingProposal,
} from "./apiTypes";

export const FLAG_TEXT: Record<GlossaryRiskFlag, { label: string; explanation: string }> = {
  conflicting_renderings: {
    label: "Traducciones distintas",
    explanation:
      "Distintas partes del libro propusieron traducciones diferentes para este término. " +
      "Conviene revisar las propuestas y dejar la correcta.",
  },
  kept_vs_translated: {
    label: "A veces quedó en inglés",
    explanation:
      "En algunas partes del libro el término se dejó sin traducir y en otras se tradujo. " +
      "Conviene decidir si es un nombre propio (se deja igual) o una palabra que se traduce.",
  },
  lowercase_identity: {
    label: "Palabra común sin traducir",
    explanation:
      "Se dejó en inglés como si fuera un nombre, pero el libro también la escribe en " +
      "minúscula como palabra corriente. Probablemente haya que traducirla.",
  },
  possible_gloss: {
    label: "Posible traducción libre (señal débil)",
    explanation:
      "La traducción no comparte ninguna palabra con el original. Casi siempre está " +
      "bien; solo hay que corregirla si un nombre inventado quedó reemplazado por una " +
      "explicación.",
  },
  undecided_gender: {
    label: "Género sin decidir",
    explanation:
      "El nombre aparece con pronombres masculinos y femeninos en proporciones parecidas, " +
      "así que no se le asignó un género. Conviene elegirlo o marcar «Sin género».",
  },
};

/** Flags that deserve a look before any automatic decision; possible_gloss is
 * deliberately not one (68 of 549 entries on the calibration book, nearly
 * all correct translations). */
const STRONG_FLAGS: ReadonlySet<GlossaryRiskFlag> = new Set([
  "conflicting_renderings",
  "kept_vs_translated",
  "lowercase_identity",
  "undecided_gender",
]);

export type TriageGroup = "attention" | "gender" | "weak" | "clear";

const TRIAGE_ORDER: TriageGroup[] = ["attention", "gender", "weak", "clear"];

export const TRIAGE_GROUP_LABELS: Record<TriageGroup, string> = {
  attention: "Revisar primero",
  gender: "Género asignado automáticamente: confirmar",
  weak: "Posibles traducciones libres (señal débil)",
  clear: "Sin observaciones",
};

/** Which triage group an entry belongs to. A seeded gender gets its own group
 * because the seeder also tags places and ranks (Caten, Princeps) from the
 * pronouns of the people near them, and only a person can tell. */
export function triageGroup(item: GlossaryReviewEntry): TriageGroup {
  if (item.flags.some((flag) => STRONG_FLAGS.has(flag))) return "attention";
  if (item.gender_source === "seeded") return "gender";
  if (item.flags.length > 0) return "weak";
  return "clear";
}

export interface IndexedReviewEntry {
  /** Position in the review, which is also the position in the editable
   * glossary (both keep the server's order). */
  index: number;
  item: GlossaryReviewEntry;
}

/** The review split into non-empty groups in triage order; inside a group
 * entries keep the server's (alphabetical) order. */
export function triageGroups(
  entries: GlossaryReviewEntry[],
): { group: TriageGroup; label: string; items: IndexedReviewEntry[] }[] {
  const indexed = entries.map((item, index) => ({ index, item }));
  return TRIAGE_ORDER.map((group) => ({
    group,
    label: TRIAGE_GROUP_LABELS[group],
    items: indexed.filter(({ item }) => triageGroup(item) === group),
  })).filter(({ items }) => items.length > 0);
}

/** The engine's `normalize_term`: Unicode-composed, whitespace collapsed,
 * case preserved. */
function normalizeTerm(text: string): string {
  return text.normalize("NFC").trim().split(/\s+/).join(" ");
}

/** Whether the entry keeps the term as is — the only kind the gender seeder
 * classifies. */
export function isVerbatim(entry: GlossaryEntry): boolean {
  const term = normalizeTerm(entry.term);
  return term !== "" && term === normalizeTerm(entry.translation);
}

export function formatPronounEvidence(item: GlossaryReviewEntry): string {
  const { masculine_mentions: masculine, feminine_mentions: feminine } = item;
  if (masculine === 0 && feminine === 0) {
    return "No se encontró ningún pronombre (he, she…) cerca del término en el original en inglés.";
  }
  return (
    `Cerca del término, en el original en inglés: ${masculine} masculinos ` +
    `(he, him, his, himself) y ${feminine} femeninos (she, her, hers, herself).`
  );
}

export function formatProposals(proposals: RenderingProposal[]): string | null {
  if (proposals.length === 0) return null;
  return proposals
    .map(
      ({ rendering, batches }) =>
        `«${rendering}» (${batches} ${batches === 1 ? "parte del libro" : "partes del libro"})`,
    )
    .join(", ");
}

const GENDER_WORDS: Record<"masculine" | "feminine", string> = {
  masculine: "Masculino",
  feminine: "Femenino",
};

/** The gender verdict the run will use, in plain words, as the server last
 * derived it (unsaved edits are shown by the gender control, not here). */
export function describeGender(
  item: GlossaryReviewEntry,
  thresholds: Pick<GlossaryReviewResponse, "min_gender_mentions" | "min_gender_ratio">,
): string {
  const rule =
    `hacen falta al menos ${thresholds.min_gender_mentions} menciones y que el ` +
    `${Math.round(thresholds.min_gender_ratio * 100)} % coincida`;
  if (item.entry.gender === "unanchored") {
    return "Sin género: se decidió que la traducción no fije un género para este término.";
  }
  if (item.gender && item.gender_source === "stored") {
    return `${GENDER_WORDS[item.gender]}: guardado en el glosario.`;
  }
  if (item.gender && item.gender_source === "seeded") {
    return (
      `${GENDER_WORDS[item.gender]}: asignado automáticamente por los pronombres del original. ` +
      "Si no es una persona (un lugar, un cargo), elegir «Sin género»."
    );
  }
  if (item.flags.includes("undecided_gender")) {
    return `Sin decidir: los pronombres no coinciden lo suficiente (${rule}).`;
  }
  if (isVerbatim(item.entry)) {
    return `Sin género asignado: hay muy pocas menciones con pronombres para decidirlo (${rule}).`;
  }
  return "No aplica: el género solo se fija para nombres que se dejan sin traducir.";
}

export type GenderChoice = "auto" | GlossaryGender;

const GENDER_CHOICE_LABELS: Record<GenderChoice, string> = {
  auto: "Automático (según el texto)",
  masculine: "Masculino",
  feminine: "Femenino",
  unanchored: "Sin género (no es una persona, o no se sabe)",
};

/** The gender options for an entry as the server stores it. "Automatic" is
 * offered only while nothing is stored: PUT treats a null gender as "keep
 * the stored one", so once saved there is no way back to it. */
export function genderChoices(original: GlossaryEntry): { value: GenderChoice; label: string }[] {
  const values: GenderChoice[] = original.gender
    ? ["masculine", "feminine", "unanchored"]
    : ["auto", "masculine", "feminine", "unanchored"];
  return values.map((value) => ({ value, label: GENDER_CHOICE_LABELS[value] }));
}

export function genderChoiceOf(entry: GlossaryEntry): GenderChoice {
  return entry.gender ?? "auto";
}

export function genderFromChoice(choice: GenderChoice): GlossaryGender | null {
  return choice === "auto" ? null : choice;
}

/** Gender only matters for names kept as is, plus any entry that already
 * stores one (so it can still be changed). */
export function showsGenderControl(entry: GlossaryEntry): boolean {
  return isVerbatim(entry) || !!entry.gender;
}

function sameEntry(a: GlossaryEntry, b: GlossaryEntry): boolean {
  return (
    a.term === b.term &&
    a.translation === b.translation &&
    a.locked === b.locked &&
    (a.note ?? null) === (b.note ?? null) &&
    (a.gender ?? null) === (b.gender ?? null)
  );
}

/** The PUT payload: only entries the person changed or added. An edit settles
 * a term (its pending votes are dropped), so sending untouched entries would
 * freeze every term the run could otherwise still correct. Without a review
 * (the plain-list fallback) the whole glossary is sent, as before. */
export function glossaryChangesToSave(
  review: GlossaryReviewResponse | null,
  current: GlossaryEntry[],
): GlossaryEntry[] {
  if (!review) return current;
  return current.filter((entry, index) => {
    const original = review.entries[index]?.entry;
    if (!original) return entry.term.trim() !== "";
    return !sameEntry(original, entry);
  });
}
