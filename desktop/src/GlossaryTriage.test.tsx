import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import GlossaryTriage from "./GlossaryTriage";
import type { GlossaryEntry, GlossaryReviewResponse } from "./apiTypes";

function stored(term: string, translation: string, gender: GlossaryEntry["gender"] = null) {
  return { term, translation, locked: false, note: null, gender, first_draw: null };
}

const review: GlossaryReviewResponse = {
  entries: [
    {
      entry: stored("Academy", "la Academia"),
      gender: null,
      gender_source: null,
      masculine_mentions: 0,
      feminine_mentions: 0,
      proposals: [],
      flags: ["possible_gloss"],
    },
    {
      entry: stored("Caten", "Caten"),
      gender: "masculine",
      gender_source: "seeded",
      masculine_mentions: 41,
      feminine_mentions: 3,
      proposals: [{ rendering: "Caten", batches: 7 }],
      flags: [],
    },
    {
      entry: stored("Thrum", "Thrum"),
      gender: null,
      gender_source: null,
      masculine_mentions: 0,
      feminine_mentions: 0,
      proposals: [
        { rendering: "Thrum", batches: 5 },
        { rendering: "Zumbido", batches: 2 },
      ],
      flags: ["conflicting_renderings", "kept_vs_translated", "lowercase_identity"],
    },
  ],
  min_gender_mentions: 20,
  min_gender_ratio: 0.7,
};

function render(glossary: GlossaryEntry[] = review.entries.map((e) => e.entry), disabled = false) {
  return renderToStaticMarkup(
    <GlossaryTriage review={review} glossary={glossary} disabled={disabled} onEdit={() => {}} />,
  );
}

describe("GlossaryTriage", () => {
  it("shows the groups in triage order with their counts, the decisions open and the weak signal folded", () => {
    const html = render();
    const attention = html.indexOf("Revisar primero (1)");
    const gender = html.indexOf("Género asignado automáticamente: confirmar (1)");
    const weak = html.indexOf("Posibles traducciones libres (señal débil) (1)");
    expect(attention).toBeGreaterThanOrEqual(0);
    expect(attention).toBeLessThan(gender);
    expect(gender).toBeLessThan(weak);
    expect(html.match(/<details open="">/g)).toHaveLength(2);
  });

  it("explains each flag in plain words and lists the proposals", () => {
    const html = render();
    expect(html).toContain("Palabra común sin traducir.");
    expect(html).toContain("«Thrum» (5 partes del libro), «Zumbido» (2 partes del libro)");
    expect(html).not.toContain("lowercase_identity");
  });

  it("shows the gender verdict, its evidence and a control only for names kept as is", () => {
    const html = render();
    expect(html).toContain("Masculino: asignado automáticamente");
    expect(html).toContain("41 masculinos");
    expect(html).toContain("Sin género (no es una persona, o no se sabe)");
    // Two verbatim names (Caten, Thrum) get the control; Academy does not.
    expect(html.match(/<select/g)).toHaveLength(2);
  });

  it("shows the person's unsaved edits, not the stored values", () => {
    const edited = review.entries.map((e) => e.entry);
    edited[2] = { ...edited[2], translation: "Zumbido", locked: true };
    edited[1] = { ...edited[1], gender: "unanchored" };
    const html = render(edited);
    expect(html).toContain('value="Zumbido"');
    expect(html).toMatch(/<option value="unanchored" selected="">/);
    expect(html).toMatch(/type="checkbox" checked=""/);
  });

  it("disables every control once the glossary is locked", () => {
    const html = render(undefined, true);
    const controls = html.match(/<(input|select)[^>]*>/g) ?? [];
    expect(controls.length).toBeGreaterThan(0);
    expect(controls.every((tag) => tag.includes('disabled=""'))).toBe(true);
  });
});
