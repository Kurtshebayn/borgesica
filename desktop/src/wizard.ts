/**
 * Pure state machine + formatting/parsing helpers for the T7b translation
 * wizard. Deliberately has no dependency on `@tauri-apps/api`, `fetch`, the
 * DOM, or `EventSource` so it is unit-testable in isolation (vitest, node
 * environment) — mirrors the pure/glue split established in T7a's
 * `sidecarStatus.ts`. All actual network/IPC calls live in `apiClient.ts`
 * and `sseClient.ts`.
 */
import type {
  EstimateResponse,
  GlossaryEntry,
  JobResponse,
  JobStatus,
} from "./apiTypes";

export type WizardScreen = "pick" | "estimate" | "glossary" | "run" | "done";

export interface ProgressInfo {
  chunkIndex: number;
  totalChunks: number;
  costUsd: number;
}

export interface WizardState {
  screen: WizardScreen;
  filePath: string;
  fileError: string | null;
  jobId: string | null;
  createError: string | null;
  estimate: EstimateResponse | null;
  estimateError: string | null;
  glossary: GlossaryEntry[];
  glossaryLocked: boolean;
  progress: ProgressInfo | null;
  job: JobResponse | null;
  runError: string | null;
  cancelRequested: boolean;
}

export const initialWizardState: WizardState = {
  screen: "pick",
  filePath: "",
  fileError: null,
  jobId: null,
  createError: null,
  estimate: null,
  estimateError: null,
  glossary: [],
  glossaryLocked: false,
  progress: null,
  job: null,
  runError: null,
  cancelRequested: false,
};

// Serve-api spec: only .epub/.srt are accepted; PDF is rejected with a
// clear message both here (client-side, before any request is sent) and by
// the server's own 422 (surfaced via JOB_CREATE_FAILED if it is ever hit).
const ACCEPTED_EXTENSIONS = [".epub", ".srt"];

export function validateFilePath(path: string): string | null {
  const lower = path.toLowerCase();
  if (lower.endsWith(".pdf")) {
    return "PDF files are not supported for translation input. Please choose an EPUB or SRT file.";
  }
  if (!ACCEPTED_EXTENSIONS.some((ext) => lower.endsWith(ext))) {
    return "Only .epub and .srt files are supported.";
  }
  return null;
}

export function formatBestEffortSummary(count: number): string | null {
  if (count <= 0) return null;
  return `${count} chunk${count === 1 ? "" : "s"} remained best-effort`;
}

export function formatProgress(progress: ProgressInfo): string {
  return `Chunk ${progress.chunkIndex}/${progress.totalChunks} — $${progress.costUsd.toFixed(4)}`;
}

/** True only while the job is actively RUNNING (used to decide whether SSE
 * polling fallback should keep going). */
export function isRunningStatus(status: JobStatus): boolean {
  return status === "RUNNING";
}

export interface SseProgressEvent {
  type: "progress";
  job_id: string;
  chunk_index: number;
  total_chunks: number;
  cost_usd: number;
  status: string;
}

export interface SseTerminalEvent {
  type: "terminal";
  status: string;
  error: string | null;
}

export type SseParsedEvent = SseProgressEvent | SseTerminalEvent;

/** Parses one SSE `data:` payload (already extracted by EventSource/the
 * fetch-stream glue). Never throws — malformed or unrecognized payloads
 * (e.g. transient noise) become `null` so the caller can safely ignore
 * them instead of crashing the stream. */
export function parseSseData(raw: string): SseParsedEvent | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const candidate = parsed as { type?: unknown };
  if (candidate.type === "progress" || candidate.type === "terminal") {
    return parsed as SseParsedEvent;
  }
  return null;
}

export function sseEventToAction(event: SseParsedEvent): WizardAction {
  if (event.type === "progress") {
    return {
      type: "PROGRESS_RECEIVED",
      progress: {
        chunkIndex: event.chunk_index,
        totalChunks: event.total_chunks,
        costUsd: event.cost_usd,
      },
    };
  }
  return {
    type: "RUN_TERMINAL",
    status: event.status as JobStatus,
    error: event.error,
  };
}

export type WizardAction =
  | { type: "FILE_PATH_CHANGED"; path: string }
  | { type: "JOB_CREATED"; jobId: string }
  | { type: "JOB_CREATE_FAILED"; message: string }
  | { type: "ESTIMATE_RECEIVED"; estimate: EstimateResponse }
  | { type: "ESTIMATE_FAILED"; message: string }
  | { type: "PROCEED_TO_GLOSSARY" }
  | { type: "GLOSSARY_LOADED"; entries: GlossaryEntry[] }
  | { type: "GLOSSARY_CHANGED"; entries: GlossaryEntry[] }
  | { type: "GLOSSARY_LOCKED" }
  | { type: "RUN_STARTED"; job: JobResponse }
  | { type: "RUN_START_FAILED"; message: string }
  | { type: "PROGRESS_RECEIVED"; progress: ProgressInfo }
  | { type: "RUN_TERMINAL"; status: JobStatus; error: string | null }
  | { type: "CANCEL_REQUESTED" }
  | { type: "RESUME_JOB"; job: JobResponse }
  | { type: "RESET" };

export function wizardReducer(state: WizardState, action: WizardAction): WizardState {
  switch (action.type) {
    case "FILE_PATH_CHANGED": {
      const error = validateFilePath(action.path);
      return { ...state, filePath: action.path, fileError: error, createError: null };
    }
    case "JOB_CREATED":
      // Guard: never surface a job as created while the current file path
      // is still rejected (defensive — the UI should not call this action
      // in that case, but the reducer must not trust callers blindly).
      if (state.fileError) return state;
      return { ...state, jobId: action.jobId, createError: null, screen: "estimate" };
    case "JOB_CREATE_FAILED":
      return { ...state, createError: action.message };
    case "ESTIMATE_RECEIVED":
      return { ...state, estimate: action.estimate, estimateError: null };
    case "ESTIMATE_FAILED":
      return { ...state, estimateError: action.message };
    case "PROCEED_TO_GLOSSARY":
      // Guard (spec: glossary strictly pre-run, always after an estimate).
      if (!state.estimate) return state;
      return { ...state, screen: "glossary" };
    case "GLOSSARY_LOADED":
      return { ...state, glossary: action.entries };
    case "GLOSSARY_CHANGED":
      // Guard (spec: glossary edit/lock strictly pre-run — once locked, no
      // further edits are accepted by this reducer).
      if (state.glossaryLocked) return state;
      return { ...state, glossary: action.entries };
    case "GLOSSARY_LOCKED":
      return { ...state, glossaryLocked: true };
    case "RUN_STARTED":
      // Hard guard: never run without an estimate having been shown first.
      if (!state.estimate) return state;
      return {
        ...state,
        screen: "run",
        job: action.job,
        runError: null,
        progress: null,
        cancelRequested: false,
      };
    case "RUN_START_FAILED":
      return { ...state, runError: action.message };
    case "PROGRESS_RECEIVED":
      return { ...state, progress: action.progress };
    case "RUN_TERMINAL":
      return {
        ...state,
        screen: "done",
        job: state.job ? { ...state.job, status: action.status } : state.job,
        runError: action.error,
      };
    case "CANCEL_REQUESTED":
      return { ...state, cancelRequested: true };
    case "RESUME_JOB":
      return {
        ...state,
        jobId: action.job.id,
        job: action.job,
        estimate: state.estimate,
        screen: "run",
        progress: null,
        cancelRequested: false,
      };
    case "RESET":
      return { ...initialWizardState };
    default:
      return state;
  }
}
