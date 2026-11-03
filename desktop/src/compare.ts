/**
 * Pure helpers for the compare screen (original beside translation). No DOM,
 * no network: the screen feeds them pages from `GET /jobs/{id}/chunks`.
 */
import type { ChunkItem } from "./apiTypes";

export const COMPARE_PAGE_SIZE = 8;

/** How a chunk should read on screen.
 * - failed: the provider never translated it; the engine ships the source.
 * - effort: translated, but accepted although it failed validation.
 * - pending: not translated yet (a run in progress).
 * - ok: translated and valid. */
export type ChunkKind = "ok" | "failed" | "effort" | "pending";

export function chunkKind(chunk: ChunkItem): ChunkKind {
  if (chunk.status === "FAILED") return "failed";
  if (chunk.status === "PENDING" || chunk.status === "TRANSLATING") return "pending";
  if (chunk.best_effort) return "effort";
  return "ok";
}

export const KIND_LABELS: Record<ChunkKind, string | null> = {
  ok: null,
  pending: "SIN TRADUCIR AÚN",
  failed: "FALLIDO · SE ENTREGA EL ORIGINAL",
  effort: "MEJOR ESFUERZO",
};

/** What the translation column shows: a failed chunk falls back to the
 * source, as the shipped book does; a pending one has nothing yet. */
export function translationText(chunk: ChunkItem): string {
  if (chunk.status === "FAILED") return chunk.source;
  return chunk.translation ?? "";
}

/** The chunk number a person sees (the engine counts from 0). */
export function displayNumber(chunk: ChunkItem): number {
  return chunk.index + 1;
}

/** "from–to of total" for the pager, 1-based; null when there is nothing. */
export function pageSummary(
  total: number,
  offset: number,
  shown: number,
): { from: number; to: number; total: number } | null {
  if (total === 0 || shown === 0) return null;
  return { from: offset + 1, to: offset + shown, total };
}

export function nextOffset(total: number, offset: number, limit: number): number | null {
  const next = offset + limit;
  return next < total ? next : null;
}

export function previousOffset(offset: number, limit: number): number | null {
  return offset > 0 ? Math.max(0, offset - limit) : null;
}

/** The offset of the page that holds chunk `index` when nothing is filtered. */
export function offsetForChunk(index: number, limit: number): number {
  return Math.floor(index / limit) * limit;
}

/** A run of chunk text with the emphasis that applies to it. */
export interface InlineSegment {
  text: string;
  bold: boolean;
  italic: boolean;
}

const TAG = /<(\/?)([a-zA-Z][\w-]*)\b[^>]*?(\/?)>/g;
const BOLD_TAGS = new Set(["b", "strong"]);
const ITALIC_TAGS = new Set(["i", "em"]);

/**
 * Reads the inline markup an EPUB chunk carries (`<em>`, `<b>`…) so the viewer
 * shows the emphasis instead of the tag. Only bold and italic are kept; every
 * other tag (spans, page-break anchors) is dropped, `<br>` becomes a line
 * break, and a tag left open just runs to the end of the chunk. Output is
 * plain text segments, never HTML.
 */
export function parseInline(text: string): InlineSegment[] {
  const segments: InlineSegment[] = [];
  let bold = 0;
  let italic = 0;
  let last = 0;
  const push = (chunk: string) => {
    if (chunk) segments.push({ text: chunk, bold: bold > 0, italic: italic > 0 });
  };
  for (const m of text.matchAll(TAG)) {
    push(text.slice(last, m.index));
    last = m.index + m[0].length;
    const [, closing, rawName, selfClosing] = m;
    const name = rawName.toLowerCase();
    if (name === "br") {
      push("\n");
    } else if (selfClosing) {
      continue;
    } else if (BOLD_TAGS.has(name)) {
      bold = Math.max(0, bold + (closing ? -1 : 1));
    } else if (ITALIC_TAGS.has(name)) {
      italic = Math.max(0, italic + (closing ? -1 : 1));
    }
  }
  push(text.slice(last));
  return segments;
}
