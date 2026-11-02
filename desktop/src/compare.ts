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
