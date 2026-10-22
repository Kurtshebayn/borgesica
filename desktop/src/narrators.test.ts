import { describe, expect, it } from "vitest";
import {
  applyFromChapterOnward,
  applyToFirstPerson,
  bulkNarrator,
  chapterLabel,
  draftFromChapters,
  withGenderChoice,
  withName,
  hasNarratorChanges,
  narratorsPayload,
  setChapterNarrator,
} from "./narrators";
import type { NarratorChapter, NarratorFact } from "./apiTypes";

function chapter(
  key: string,
  firstPerson: boolean,
  narrator: NarratorFact | null = null,
): NarratorChapter {
  return {
    key,
    title: `Title ${key}`,
    opening: `Opening of ${key}`,
    first_chunk: 0,
    last_chunk: 0,
    first_person: firstPerson,
    first_person_per_1k: firstPerson ? 30 : 0,
    narrator,
  };
}

const vis: NarratorFact = { name: "Vis", gender: "masculine" };
const sira: NarratorFact = { name: "Sira", gender: "feminine" };

// ack (unmarked), ch1 (1st), ch2 (3rd person), ch3 (1st), ch4 (1st)
const chapters = [
  chapter("ack.xhtml", false),
  chapter("ch1.xhtml", true),
  chapter("ch2.xhtml", false),
  chapter("ch3.xhtml", true),
  chapter("ch4.xhtml", true, sira),
];

describe("draftFromChapters", () => {
  it("maps every chapter key to its stored narrator, or null", () => {
    expect(draftFromChapters(chapters)).toEqual({
      "ack.xhtml": null,
      "ch1.xhtml": null,
      "ch2.xhtml": null,
      "ch3.xhtml": null,
      "ch4.xhtml": sira,
    });
  });
});

describe("bulkNarrator", () => {
  it("needs a gender; the name is optional and blank means no name", () => {
    expect(bulkNarrator({ name: "Vis", gender: null })).toBeNull();
    expect(bulkNarrator({ name: "  Vis ", gender: "masculine" })).toEqual(vis);
    expect(bulkNarrator({ name: "   ", gender: "feminine" })).toEqual({
      name: null,
      gender: "feminine",
    });
  });
});

describe("applyToFirstPerson", () => {
  it("gives the narrator to every first-person chapter and leaves the others as they are", () => {
    const draft = { ...draftFromChapters(chapters), "ch2.xhtml": sira };
    expect(applyToFirstPerson(chapters, draft, vis)).toEqual({
      "ack.xhtml": null,
      "ch1.xhtml": vis,
      "ch2.xhtml": sira,
      "ch3.xhtml": vis,
      "ch4.xhtml": vis,
    });
  });
});

describe("setChapterNarrator", () => {
  it("changes a single chapter's narrator", () => {
    const draft = draftFromChapters(chapters);
    const next = setChapterNarrator(draft, "ch3.xhtml", vis);
    expect(next["ch3.xhtml"]).toEqual(vis);
    expect(next["ch4.xhtml"]).toEqual(sira);
    expect(next["ch1.xhtml"]).toBeNull();
  });

  it("clears a single chapter (no narrator, e.g. acknowledgments by the author)", () => {
    const draft = applyToFirstPerson(chapters, draftFromChapters(chapters), vis);
    const next = setChapterNarrator(draft, "ch1.xhtml", null);
    expect(next["ch1.xhtml"]).toBeNull();
    expect(next["ch3.xhtml"]).toEqual(vis);
  });

  it("ignores a key that is not a chapter of this job", () => {
    const draft = draftFromChapters(chapters);
    expect(setChapterNarrator(draft, "nope.xhtml", vis)).toBe(draft);
  });
});

describe("applyFromChapterOnward", () => {
  it("gives the narrator to this chapter and every following first-person chapter only", () => {
    const draft = { ...draftFromChapters(chapters), "ch1.xhtml": vis };
    expect(applyFromChapterOnward(chapters, draft, "ch2.xhtml", sira)).toEqual({
      "ack.xhtml": null,
      "ch1.xhtml": vis, // before: untouched
      "ch2.xhtml": sira, // this chapter, even though it is not marked
      "ch3.xhtml": sira,
      "ch4.xhtml": sira,
    });
  });

  it("leaves following unmarked chapters as they are", () => {
    const draft = draftFromChapters(chapters);
    const next = applyFromChapterOnward(chapters, draft, "ch1.xhtml", vis);
    expect(next["ch2.xhtml"]).toBeNull();
    expect(next["ack.xhtml"]).toBeNull();
    expect(next["ch3.xhtml"]).toEqual(vis);
  });

  it("ignores a key that is not a chapter of this job", () => {
    const draft = draftFromChapters(chapters);
    expect(applyFromChapterOnward(chapters, draft, "nope.xhtml", vis)).toBe(draft);
  });
});

describe("narratorsPayload", () => {
  it("lists only the chapters with a narrator, in chapter order, blank names as null", () => {
    const draft = {
      ...draftFromChapters(chapters),
      "ch3.xhtml": { name: " ", gender: "masculine" as const },
      "ch1.xhtml": { name: " Vis ", gender: "masculine" as const },
    };
    expect(narratorsPayload(chapters, draft)).toEqual([
      { key: "ch1.xhtml", name: "Vis", gender: "masculine" },
      { key: "ch3.xhtml", name: null, gender: "masculine" },
      { key: "ch4.xhtml", name: "Sira", gender: "feminine" },
    ]);
  });

  it("is empty when every chapter was cleared (a full overwrite removes them all)", () => {
    const draft = setChapterNarrator(draftFromChapters(chapters), "ch4.xhtml", null);
    expect(narratorsPayload(chapters, draft)).toEqual([]);
  });
});

describe("hasNarratorChanges", () => {
  it("is false for the draft as loaded and true after an edit", () => {
    const draft = draftFromChapters(chapters);
    expect(hasNarratorChanges(chapters, draft)).toBe(false);
    expect(hasNarratorChanges(chapters, setChapterNarrator(draft, "ch1.xhtml", vis))).toBe(true);
    expect(hasNarratorChanges(chapters, setChapterNarrator(draft, "ch4.xhtml", null))).toBe(true);
  });

  it("ignores edits that save to the same thing (surrounding spaces in a name)", () => {
    const draft = setChapterNarrator(draftFromChapters(chapters), "ch4.xhtml", {
      name: " Sira ",
      gender: "feminine",
    });
    expect(hasNarratorChanges(chapters, draft)).toBe(false);
  });
});

describe("per-chapter edits", () => {
  it("choosing a gender keeps the name; choosing none clears the narrator", () => {
    expect(withGenderChoice(null, "feminine")).toEqual({ name: null, gender: "feminine" });
    expect(withGenderChoice(vis, "feminine")).toEqual({ name: "Vis", gender: "feminine" });
    expect(withGenderChoice(vis, "")).toBeNull();
  });

  it("a name edit keeps the gender and is ignored without a narrator", () => {
    expect(withName(vis, "Kell")).toEqual({ name: "Kell", gender: "masculine" });
    expect(withName(null, "Kell")).toBeNull();
  });
});

describe("chapterLabel", () => {
  it("is the title, or the chapter key when the title is blank", () => {
    expect(chapterLabel(chapter("ch1.xhtml", true))).toBe("Title ch1.xhtml");
    expect(chapterLabel({ ...chapter("ch1.xhtml", true), title: "  " })).toBe("ch1.xhtml");
  });
});
