import { describe, expect, it } from "vitest";
import {
  describeGender,
  FLAG_TEXT,
  formatPronounEvidence,
  formatProposals,
  genderChoiceOf,
  genderChoices,
  genderFromChoice,
  glossaryChangesToSave,
  isVerbatim,
  showsGenderControl,
  triageGroup,
  triageGroups,
} from "./glossaryReview";
import type {
  GlossaryEntry,
  GlossaryReviewEntry,
  GlossaryReviewResponse,
  GlossaryRiskFlag,
} from "./apiTypes";

const THRESHOLDS = { min_gender_mentions: 20, min_gender_ratio: 0.7 };

function entry(term: string, translation: string, extra: Partial<GlossaryEntry> = {}): GlossaryEntry {
  return { term, translation, locked: false, note: null, gender: null, first_draw: null, ...extra };
}

function item(
  term: string,
  translation: string,
  extra: Partial<Omit<GlossaryReviewEntry, "entry">> & { stored?: Partial<GlossaryEntry> } = {},
): GlossaryReviewEntry {
  const { stored, ...rest } = extra;
  return {
    entry: entry(term, translation, stored),
    gender: null,
    gender_source: null,
    masculine_mentions: 0,
    feminine_mentions: 0,
    proposals: [],
    flags: [],
    ...rest,
  };
}

describe("FLAG_TEXT", () => {
  it("gives every risk flag a plain label and an explanation", () => {
    const flags: GlossaryRiskFlag[] = [
      "conflicting_renderings",
      "kept_vs_translated",
      "lowercase_identity",
      "possible_gloss",
      "undecided_gender",
    ];
    for (const flag of flags) {
      expect(FLAG_TEXT[flag].label).not.toMatch(/_/);
      expect(FLAG_TEXT[flag].explanation.length).toBeGreaterThan(40);
    }
  });

  it("tells the reader the possible-gloss flag is weak and usually a correct translation", () => {
    expect(FLAG_TEXT.possible_gloss.label).toMatch(/débil/);
    expect(FLAG_TEXT.possible_gloss.explanation).toMatch(/casi siempre está bien/i);
    // Shown under every flagged entry, so no fixed example term (it read as
    // being about that entry).
    expect(FLAG_TEXT.possible_gloss.explanation).not.toMatch(/Academ/);
  });
});

describe("triageGroup", () => {
  it("puts any strong flag in the attention group, even with a weak flag beside it", () => {
    expect(triageGroup(item("Thrum", "Thrum", { flags: ["lowercase_identity"] }))).toBe(
      "attention",
    );
    expect(
      triageGroup(item("Plá", "plaga", { flags: ["conflicting_renderings", "possible_gloss"] })),
    ).toBe("attention");
    expect(triageGroup(item("Aequa", "Aequa", { flags: ["undecided_gender"] }))).toBe("attention");
  });

  it("asks to confirm a gender the seeder assigned (it also tags places such as Caten)", () => {
    const caten = item("Caten", "Caten", { gender: "masculine", gender_source: "seeded" });
    expect(triageGroup(caten)).toBe("gender");
  });

  it("keeps an entry whose only flag is the weak possible-gloss apart from the strong ones", () => {
    expect(triageGroup(item("Academy", "la Academia", { flags: ["possible_gloss"] }))).toBe(
      "weak",
    );
  });

  it("files a stored gender and an unflagged entry as clear", () => {
    const vis = item("Vis", "Vis", {
      gender: "masculine",
      gender_source: "stored",
      stored: { gender: "masculine" },
    });
    expect(triageGroup(vis)).toBe("clear");
    expect(triageGroup(item("Aaru", "Aaru"))).toBe("clear");
  });
});

describe("triageGroups", () => {
  it("orders groups attention, gender, weak, clear, keeps the API's order inside each and its index", () => {
    const entries = [
      item("Academy", "la Academia", { flags: ["possible_gloss"] }),
      item("Aequa", "Aequa", { flags: ["undecided_gender"] }),
      item("Caten", "Caten", { gender: "masculine", gender_source: "seeded" }),
      item("Senate", "el Senado", { flags: ["possible_gloss"] }),
      item("Thrum", "Thrum", { flags: ["lowercase_identity"] }),
    ];

    const groups = triageGroups(entries);

    expect(groups.map((g) => g.group)).toEqual(["attention", "gender", "weak"]);
    expect(groups[0].items.map(({ index, item: it }) => [index, it.entry.term])).toEqual([
      [1, "Aequa"],
      [4, "Thrum"],
    ]);
    expect(groups[2].items.map(({ item: it }) => it.entry.term)).toEqual(["Academy", "Senate"]);
    expect(groups.every((g) => g.label.length > 0)).toBe(true);
  });
});

describe("isVerbatim", () => {
  it("compares the term and its rendering the way the seeder does: whitespace-normalised, case-sensitive", () => {
    expect(isVerbatim(entry("Ddram  cyfraith ", "Ddram cyfraith"))).toBe(true);
    expect(isVerbatim(entry("Vis", "vis"))).toBe(false);
    expect(isVerbatim(entry("Academy", "la Academia"))).toBe(false);
  });
});

describe("formatPronounEvidence", () => {
  it("shows the raw counts and which English pronouns were counted", () => {
    const text = formatPronounEvidence(
      item("Aequa", "Aequa", { masculine_mentions: 95, feminine_mentions: 84 }),
    );
    expect(text).toMatch(/95 masculinos/);
    expect(text).toMatch(/84 femeninos/);
    expect(text).toMatch(/he, him, his/);
    expect(text).toMatch(/she, her/);
  });

  it("says plainly when no pronoun was found near the term", () => {
    expect(formatPronounEvidence(item("Aaru", "Aaru"))).toMatch(/ningún pronombre/);
  });
});

describe("formatProposals", () => {
  it("lists each proposed rendering with how many parts of the book proposed it", () => {
    expect(
      formatProposals([
        { rendering: "Thrum", batches: 12 },
        { rendering: "Zumbido", batches: 1 },
      ]),
    ).toBe("«Thrum» (12 partes del libro), «Zumbido» (1 parte del libro)");
  });

  it("returns null when the extraction never proposed the term", () => {
    expect(formatProposals([])).toBeNull();
  });
});

describe("describeGender", () => {
  it("explains a seeded gender as automatic and offers the way out for non-persons", () => {
    const text = describeGender(
      item("Caten", "Caten", { gender: "masculine", gender_source: "seeded" }),
      THRESHOLDS,
    );
    expect(text).toMatch(/^Masculino/);
    expect(text).toMatch(/automáticamente/);
    expect(text).toMatch(/Sin género/);
  });

  it("explains a stored gender as saved", () => {
    const text = describeGender(
      item("Lanistia", "Lanistia", {
        gender: "feminine",
        gender_source: "stored",
        stored: { gender: "feminine" },
      }),
      THRESHOLDS,
    );
    expect(text).toMatch(/^Femenino/);
    expect(text).toMatch(/guardado/);
  });

  it("explains a stored decision to anchor nothing", () => {
    const text = describeGender(
      item("Caten", "Caten", { gender_source: "stored", stored: { gender: "unanchored" } }),
      THRESHOLDS,
    );
    expect(text).toMatch(/^Sin género/);
  });

  it("explains an undecided name with the thresholds the seeder used", () => {
    const text = describeGender(
      item("Aequa", "Aequa", {
        masculine_mentions: 95,
        feminine_mentions: 84,
        flags: ["undecided_gender"],
      }),
      THRESHOLDS,
    );
    expect(text).toMatch(/^Sin decidir/);
    expect(text).toMatch(/20 menciones/);
    expect(text).toMatch(/70 %/);
  });

  it("explains a verbatim name with too few mentions to decide", () => {
    const text = describeGender(item("Tara", "Tara", { masculine_mentions: 3 }), THRESHOLDS);
    expect(text).toMatch(/^Sin género asignado/);
    expect(text).toMatch(/20/);
  });

  it("says gender does not apply to a translated term", () => {
    expect(describeGender(item("Academy", "la Academia"), THRESHOLDS)).toMatch(/^No aplica/);
  });
});

describe("gender choices", () => {
  it("offers automatic only while nothing is stored (the API cannot go back to it)", () => {
    expect(genderChoices(entry("Caten", "Caten")).map((c) => c.value)).toEqual([
      "auto",
      "masculine",
      "feminine",
      "unanchored",
    ]);
    expect(
      genderChoices(entry("Vis", "Vis", { gender: "masculine" })).map((c) => c.value),
    ).toEqual(["masculine", "feminine", "unanchored"]);
    expect(genderChoices(entry("Caten", "Caten")).every((c) => c.label.length > 0)).toBe(true);
  });

  it("maps the stored value to a choice and back", () => {
    expect(genderChoiceOf(entry("Caten", "Caten"))).toBe("auto");
    expect(genderChoiceOf(entry("Caten", "Caten", { gender: "unanchored" }))).toBe("unanchored");
    expect(genderFromChoice("auto")).toBeNull();
    expect(genderFromChoice("feminine")).toBe("feminine");
    expect(genderFromChoice("unanchored")).toBe("unanchored");
  });

  it("shows the control on verbatim names and on any entry that already stores a gender", () => {
    expect(showsGenderControl(entry("Caten", "Caten"))).toBe(true);
    expect(showsGenderControl(entry("Academy", "la Academia"))).toBe(false);
    expect(showsGenderControl(entry("Princeps", "Príncipe", { gender: "masculine" }))).toBe(true);
  });
});

describe("glossaryChangesToSave", () => {
  const review: GlossaryReviewResponse = {
    entries: [item("Caten", "Caten"), item("Thrum", "Thrum"), item("Vis", "Vis")],
    min_gender_mentions: 20,
    min_gender_ratio: 0.7,
  };

  it("sends only the entries the person changed, so the rest stay open to the run's vote", () => {
    const current = [
      entry("Caten", "Caten", { gender: "unanchored" }),
      entry("Thrum", "Zumbido", { locked: true }),
      entry("Vis", "Vis"),
    ];

    expect(glossaryChangesToSave(review, current)).toEqual([current[0], current[1]]);
  });

  it("sends nothing when nothing changed", () => {
    expect(glossaryChangesToSave(review, review.entries.map((e) => e.entry))).toEqual([]);
  });

  it("adds new entries with a term and skips blank ones", () => {
    const current = [
      ...review.entries.map((e) => e.entry),
      entry("Duat", "Duat"),
      entry("  ", ""),
    ];
    expect(glossaryChangesToSave(review, current)).toEqual([entry("Duat", "Duat")]);
  });

  it("sends the whole glossary when no review was loaded (the plain list fallback)", () => {
    const current = [entry("Caten", "Caten")];
    expect(glossaryChangesToSave(null, current)).toEqual(current);
  });
});
