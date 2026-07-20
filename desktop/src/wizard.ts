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

export type WizardScreen = "pick" | "estimate" | "glossary" | "run" | "done" | "error";

/** Translation backends the engine's `serve --provider` accepts. The provider
 * is chosen before the sidecar spawns (it is bound to that process); changing
 * it requires a sidecar respawn, so it lives at the App shell, not per job. */
export type Provider = "anthropic" | "deepseek" | "ollama";

export const PROVIDERS: Provider[] = ["anthropic", "deepseek", "ollama"];

/** A sensible default model id for a freshly selected provider, so the user
 * is not left with an Anthropic model id after switching to DeepSeek (which
 * would fail at create/estimate). Ollama returns "" on purpose: local models
 * are user-specific, so the user must type whichever tag they have pulled. */
export function defaultModelForProvider(provider: Provider): string {
  switch (provider) {
    case "anthropic":
      return "claude-sonnet-5";
    case "deepseek":
      return "deepseek-v4-flash";
    case "ollama":
      return "";
  }
}

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
  /** RES-001: set when the sidecar connection is judged persistently lost
   * (see `isPersistentConnectionFailure`) while a run is in flight. Reaching
   * the "error" screen is the recovery path out of what used to be a silent
   * dead end (SSE/polling looping forever with no user-visible feedback). */
  connectionError: string | null;
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
  connectionError: null,
};

// RES-001: number of consecutive status-poll failures (e.g. sidecar
// connection refused) before the wizard treats the connection as
// persistently lost rather than a transient network blip. Kept as a named
// constant + pure predicate so the threshold is unit-testable without any
// network/timer glue.
export const MAX_CONSECUTIVE_CONNECTION_FAILURES = 3;

/** Pure classification: transient (keep silently retrying) vs. persistent
 * (surface a recoverable error to the user) connection failure. Used by the
 * SSE/polling glue (`sseClient.ts`) so failure-counting logic itself stays
 * testable here rather than buried in untested DOM/network code. */
export function isPersistentConnectionFailure(consecutiveFailures: number): boolean {
  return consecutiveFailures >= MAX_CONSECUTIVE_CONNECTION_FAILURES;
}

/** RES-001 (second correction round): whether recovering from the CURRENT
 * state requires an actual sidecar respawn rather than merely resetting the
 * wizard screen. Only the "error" screen (reached exclusively via a
 * judged-persistent connection loss — see `isPersistentConnectionFailure`)
 * genuinely implies the underlying sidecar process may be dead; recovering
 * from any other screen is a normal navigation, never a respawn trigger. */
export function recoveryRequiresRespawn(state: WizardState): boolean {
  return state.screen === "error";
}

/** The seams a real recovery action needs, injected so the decision stays
 * pure/testable here instead of buried in WizardScreen.tsx's untested glue.
 * `restartSidecar` is the actual stop+start sidecar-lifecycle seam (owned by
 * App.tsx, which alone holds the API key + Tauri invoke calls) — called
 * ONLY when `recoveryRequiresRespawn` is true. */
export interface RecoveryActions {
  closeSubscription: () => void;
  dispatch: (action: WizardAction) => void;
  restartSidecar: () => void;
}

/** RES-001 (second correction round): the recovery action, made real. A
 * cosmetic version merely reset the wizard screen while the underlying
 * sidecar process (and its stale baseUrl/token) stayed exactly as dead as
 * before — the next create/estimate call would just hit the same corpse.
 * This closes the subscription, resets the wizard state, and — ONLY when
 * recovering from a genuine persistent-failure "error" screen, never a
 * plain screen navigation — triggers an actual sidecar respawn via the
 * injected `restartSidecar` seam. */
export function runRecoveryAction(state: WizardState, actions: RecoveryActions): void {
  actions.closeSubscription();
  actions.dispatch({ type: "RECOVER_FROM_ERROR" });
  if (recoveryRequiresRespawn(state)) {
    actions.restartSidecar();
  }
}

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

/** Error-surfacing: a run with FAILED chunks (provider auth/model error, bad
 * key, etc.) still ends DONE at $0 under continue_on_error=true, but the
 * output file holds untranslated SOURCE text for those chunks. Returns a
 * user-facing alert when any chunk failed, or null for a clean run so a real
 * success shows no false alarm. Kept pure/tested here rather than inlined in
 * WizardScreen.tsx's untested render glue. */
export function formatFailureSummary(
  failedCount: number,
  totalChunks: number,
): string | null {
  if (failedCount <= 0) return null;
  return (
    `${failedCount} of ${totalChunks} chunks failed to translate — ` +
    `the output file contains untranslated source text for ` +
    `${failedCount === 1 ? "it" : "them"}.`
  );
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
  | {
      type: "RUN_TERMINAL";
      status: JobStatus;
      error: string | null;
      /** Refreshed final job status fetched on terminal (carries accurate
       * failed_count/best_effort_count that the RUNNING run-ack lacked). When
       * omitted (e.g. the status re-fetch failed), the reducer falls back to
       * updating only the status on the existing job. */
      job?: JobResponse;
    }
  | { type: "CANCEL_REQUESTED" }
  | { type: "RESUME_JOB"; job: JobResponse }
  | { type: "RESET" }
  | { type: "CONNECTION_LOST"; message: string }
  | { type: "RECOVER_FROM_ERROR" };

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
        // Prefer the refreshed job (accurate failed/best-effort counts); fall
        // back to stamping just the final status onto the run-ack when the
        // status re-fetch was unavailable.
        job: action.job ?? (state.job ? { ...state.job, status: action.status } : state.job),
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
    case "CONNECTION_LOST":
      // Guard: only a run in flight can "lose connection" in the dead-end
      // sense (RES-001) — no-op elsewhere so this can never clobber an
      // unrelated screen.
      if (state.screen !== "run") return state;
      return { ...state, screen: "error", connectionError: action.message };
    case "RECOVER_FROM_ERROR":
      // Guard: only meaningful from the error screen. This resets the pure
      // wizard state back to "pick" only — it deliberately does NOT decide
      // whether the underlying sidecar process needs restarting (that
      // decision is `recoveryRequiresRespawn`/`runRecoveryAction`, driven by
      // the glue layer so the actual stop+start seam stays out of this pure
      // reducer). Dispatching this action alone is NOT a complete recovery.
      if (state.screen !== "error") return state;
      return { ...initialWizardState, filePath: state.filePath };
    default:
      return state;
  }
}
