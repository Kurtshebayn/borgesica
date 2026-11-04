/**
 * Pure helpers for the library screen: one "ficha" per stored job. No DOM, no
 * network; the screen feeds them `GET /jobs`.
 */
import type { JobListItem } from "./apiTypes";

export type FichaStatus = "done" | "problems" | "progress" | "draft";

/** What opening a ficha does: read a finished book, or pick up one that is
 * not finished. A draft (created, never run) is picked up at its estimate. */
export type FichaAction = "compare" | "resume";

export interface Ficha {
  job: JobListItem;
  signature: string;
  status: FichaStatus;
  label: string;
  action: FichaAction;
  meta: string;
  footer: string;
}

/** BAB-0007: a catalogue signature, numbered by age (the oldest job is 1). */
export function signature(n: number): string {
  return `BAB-${String(n).padStart(4, "0")}`;
}

export function fichaStatus(job: JobListItem): FichaStatus {
  if (job.status === "CREATED" || job.status === "ESTIMATING") return "draft";
  if (job.status === "DONE") {
    return job.failed_count + job.best_effort_count > 0 ? "problems" : "done";
  }
  return "progress";
}

function labelFor(status: FichaStatus, job: JobListItem): string {
  switch (status) {
    case "done":
      return "LISTO";
    case "problems": {
      const n = job.failed_count + job.best_effort_count;
      return `${n} CON PROBLEMAS`;
    }
    case "progress":
      return "EN CURSO";
    case "draft":
      return "BORRADOR";
  }
}

function actionFor(status: FichaStatus): FichaAction {
  return status === "done" || status === "problems" ? "compare" : "resume";
}

/** Jobs arrive newest first; the oldest gets signature 1. */
export function toFichas(jobs: JobListItem[]): Ficha[] {
  return jobs.map((job, index) => {
    const status = fichaStatus(job);
    return {
      job,
      signature: signature(jobs.length - index),
      status,
      label: labelFor(status, job),
      action: actionFor(status),
      meta: `${job.source_type} · ${job.target_lang}`,
      footer: `${job.completed_chunks}/${job.total_chunks} · $${job.cost_usd.toFixed(3)}`,
    };
  });
}

/** The signature the next job will get, for the blank ficha. */
export function nextSignature(jobs: JobListItem[]): string {
  return signature(jobs.length + 1);
}

export function librarySummary(fichas: Ficha[]): string {
  const finished = fichas.filter((f) => f.status === "done" || f.status === "problems").length;
  const noun = fichas.length === 1 ? "ficha" : "fichas";
  const done = finished === 1 ? "terminada" : "terminadas";
  return `${fichas.length} ${noun} · ${finished} ${done}`;
}

export type FichaFilter = "all" | FichaStatus;

/** The filter bar, in display order. */
export const FICHA_FILTERS: { id: FichaFilter; label: string }[] = [
  { id: "all", label: "Todas" },
  { id: "progress", label: "En curso" },
  { id: "draft", label: "Borrador" },
  { id: "done", label: "Listo" },
  { id: "problems", label: "Con problemas" },
];

export function filterFichas(fichas: Ficha[], filter: FichaFilter): Ficha[] {
  return filter === "all" ? fichas : fichas.filter((f) => f.status === filter);
}

/** How many fichas each filter would show, for the bar's counters. */
export function filterCounts(fichas: Ficha[]): Record<FichaFilter, number> {
  const counts: Record<FichaFilter, number> = {
    all: fichas.length,
    progress: 0,
    draft: 0,
    done: 0,
    problems: 0,
  };
  for (const f of fichas) counts[f.status] += 1;
  return counts;
}
