import { describe, expect, it } from "vitest";
import type { ChunkItem } from "./apiTypes";
import {
  chunkKind,
  displayNumber,
  nextOffset,
  offsetForChunk,
  pageSummary,
  previousOffset,
  parseInline,
  translationText,
} from "./compare";

function chunk(overrides: Partial<ChunkItem> = {}): ChunkItem {
  return {
    index: 0,
    status: "DONE",
    source: "source text",
    translation: "texto traducido",
    best_effort: false,
    chapter_key: null,
    chapter_title: null,
    ...overrides,
  };
}

describe("chunkKind", () => {
  it("marks a translated valid chunk as ok", () => {
    expect(chunkKind(chunk())).toBe("ok");
  });

  it("marks FAILED as failed even when best_effort is set", () => {
    expect(chunkKind(chunk({ status: "FAILED", best_effort: true }))).toBe("failed");
  });

  it("marks a DONE best-effort chunk as effort", () => {
    expect(chunkKind(chunk({ best_effort: true }))).toBe("effort");
  });

  it("marks PENDING and TRANSLATING as pending", () => {
    expect(chunkKind(chunk({ status: "PENDING", translation: null }))).toBe("pending");
    expect(chunkKind(chunk({ status: "TRANSLATING", translation: null }))).toBe("pending");
  });
});

describe("translationText", () => {
  it("shows the translation of a done chunk", () => {
    expect(translationText(chunk())).toBe("texto traducido");
  });

  it("falls back to the source for a failed chunk, as the shipped book does", () => {
    expect(translationText(chunk({ status: "FAILED", translation: null }))).toBe("source text");
  });

  it("is empty for a pending chunk", () => {
    expect(translationText(chunk({ status: "PENDING", translation: null }))).toBe("");
  });
});

describe("paging", () => {
  it("numbers chunks from 1 for the reader", () => {
    expect(displayNumber(chunk({ index: 0 }))).toBe(1);
    expect(displayNumber(chunk({ index: 11 }))).toBe(12);
  });

  it("summarises the visible range", () => {
    expect(pageSummary(41, 8, 8)).toEqual({ from: 9, to: 16, total: 41 });
    expect(pageSummary(41, 40, 1)).toEqual({ from: 41, to: 41, total: 41 });
  });

  it("has no summary for an empty result", () => {
    expect(pageSummary(0, 0, 0)).toBeNull();
  });

  it("finds the next page offset and stops at the end", () => {
    expect(nextOffset(41, 0, 8)).toBe(8);
    expect(nextOffset(41, 40, 8)).toBeNull();
    expect(nextOffset(16, 8, 8)).toBeNull();
  });

  it("finds the previous page offset and stops at the start", () => {
    expect(previousOffset(16, 8)).toBe(8);
    expect(previousOffset(3, 8)).toBe(0);
    expect(previousOffset(0, 8)).toBeNull();
  });

  it("locates the page that holds a chunk", () => {
    expect(offsetForChunk(0, 8)).toBe(0);
    expect(offsetForChunk(7, 8)).toBe(0);
    expect(offsetForChunk(8, 8)).toBe(8);
    expect(offsetForChunk(30, 8)).toBe(24);
  });
});

describe("parseInline", () => {
  it("returns plain text untouched", () => {
    expect(parseInline("Hola")).toEqual([{ text: "Hola", bold: false, italic: false }]);
  });

  it("turns em and b into emphasis instead of showing the tag", () => {
    expect(parseInline("a <em>b</em> <b>c</b>")).toEqual([
      { text: "a ", bold: false, italic: false },
      { text: "b", bold: false, italic: true },
      { text: " ", bold: false, italic: false },
      { text: "c", bold: true, italic: false },
    ]);
  });

  it("drops other tags, including the page-break anchor, and keeps their text", () => {
    const text = '<span aria-label="3" id="page3" role="doc-pagebreak"/>I <span class="grey">FEAR</span>';
    expect(parseInline(text).map((s) => s.text).join("")).toBe("I FEAR");
  });

  it("ignores a closing tag that was never opened", () => {
    expect(parseInline("x</em>y")).toEqual([
      { text: "x", bold: false, italic: false },
      { text: "y", bold: false, italic: false },
    ]);
  });

  it("lets an unclosed tag run to the end and nests emphasis", () => {
    expect(parseInline("<b>a <i>b")).toEqual([
      { text: "a ", bold: true, italic: false },
      { text: "b", bold: true, italic: true },
    ]);
  });

  it("does not take a bare less-than for a tag", () => {
    expect(parseInline("si x < 5 y 6 > 2")).toEqual([
      { text: "si x < 5 y 6 > 2", bold: false, italic: false },
    ]);
  });
});
