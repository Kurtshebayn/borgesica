/**
 * Pure logic for the narrator review (who the first-person "I" is, per
 * chapter): the editable draft, the bulk and "from here onward" rules, and
 * the PUT payload. `NarratorReview.tsx` only displays it; the wizard reducer
 * holds the draft. No fetch, no DOM — unit tested in node.
 */
import type {
  NarratorAssignment,
  NarratorChapter,
  NarratorFact,
  NarratorGender,
} from "./apiTypes";

/** The editable narrators, by chapter key; null means no narrator. Names are
 * kept as typed (spaces included) and normalised only when saved. */
export type NarratorDraft = Record<string, NarratorFact | null>;

/** The "write it once" form: a name (optional) and a gender to choose. */
export interface NarratorBulkForm {
  name: string;
  gender: NarratorGender | null;
}

export const emptyNarratorBulkForm: NarratorBulkForm = { name: "", gender: null };

function normalizedName(name: string | null): string | null {
  const trimmed = (name ?? "").trim();
  return trimmed === "" ? null : trimmed;
}

function normalized(fact: NarratorFact | null): NarratorFact | null {
  return fact ? { name: normalizedName(fact.name), gender: fact.gender } : null;
}

export function draftFromChapters(chapters: NarratorChapter[]): NarratorDraft {
  return Object.fromEntries(chapters.map((c) => [c.key, c.narrator]));
}

/** The narrator the bulk form describes, or null until a gender is chosen. */
export function bulkNarrator(form: NarratorBulkForm): NarratorFact | null {
  return form.gender ? { name: normalizedName(form.name), gender: form.gender } : null;
}

/** Every chapter marked first-person gets *fact*; the others keep theirs. */
export function applyToFirstPerson(
  chapters: NarratorChapter[],
  draft: NarratorDraft,
  fact: NarratorFact,
): NarratorDraft {
  const next = { ...draft };
  for (const c of chapters) if (c.first_person) next[c.key] = fact;
  return next;
}

/** One chapter's narrator changes; null clears it. */
export function setChapterNarrator(
  draft: NarratorDraft,
  key: string,
  fact: NarratorFact | null,
): NarratorDraft {
  if (!(key in draft)) return draft;
  return { ...draft, [key]: fact };
}

/** The chapter *key* and every following chapter marked first-person get
 * *fact*; chapters before it, and following unmarked ones, keep theirs. */
export function applyFromChapterOnward(
  chapters: NarratorChapter[],
  draft: NarratorDraft,
  key: string,
  fact: NarratorFact,
): NarratorDraft {
  const start = chapters.findIndex((c) => c.key === key);
  if (start < 0) return draft;
  const next = { ...draft };
  chapters.forEach((c, i) => {
    if (i === start || (i > start && c.first_person)) next[c.key] = fact;
  });
  return next;
}

/** A chapter's gender selector value: a gender, or "" for no narrator. */
export type NarratorGenderChoice = NarratorGender | "";

/** The chapter's narrator after its gender selector changed: the name is
 * kept; "" clears the narrator. */
export function withGenderChoice(
  current: NarratorFact | null,
  choice: NarratorGenderChoice,
): NarratorFact | null {
  return choice === "" ? null : { name: current?.name ?? null, gender: choice };
}

/** The chapter's narrator after its name changed; a chapter without a
 * narrator needs a gender first. */
export function withName(current: NarratorFact | null, name: string): NarratorFact | null {
  return current ? { ...current, name } : null;
}

export function chapterLabel(chapter: NarratorChapter): string {
  return chapter.title.trim() || chapter.key;
}

/** The PUT body's list: every chapter with a narrator, in chapter order. The
 * PUT is a full overwrite, so a cleared chapter is simply left out. */
export function narratorsPayload(
  chapters: NarratorChapter[],
  draft: NarratorDraft,
): NarratorAssignment[] {
  const payload: NarratorAssignment[] = [];
  for (const c of chapters) {
    const fact = normalized(draft[c.key] ?? null);
    if (fact) payload.push({ key: c.key, name: fact.name, gender: fact.gender });
  }
  return payload;
}

/** Whether saving the draft would change what the server has stored. */
export function hasNarratorChanges(chapters: NarratorChapter[], draft: NarratorDraft): boolean {
  return chapters.some((c) => {
    const stored = normalized(c.narrator);
    const edited = normalized(draft[c.key] ?? null);
    return stored?.name !== edited?.name || stored?.gender !== edited?.gender;
  });
}
