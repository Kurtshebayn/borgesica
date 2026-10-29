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
  GlossaryExtractionEstimate,
  GlossaryReviewResponse,
  JobResponse,
  JobStatus,
  NarratorAssignment,
  NarratorFact,
  NarratorChapter,
  NarratorsResponse,
} from "./apiTypes";
import { glossaryChangesToSave } from "./glossaryReview";
import {
  applyFromChapterOnward,
  applyToFirstPerson,
  bulkNarrator,
  draftFromChapters,
  emptyNarratorBulkForm,
  narratorsPayload,
  setChapterNarrator,
  type NarratorBulkForm,
  type NarratorDraft,
} from "./narrators";

/** "preparing" is the explicit, paid glossary extraction pass, entered only
 * from the estimate screen by a user click and left only by its end (back to
 * the estimate screen, or on to the glossary screen) or a lost connection. */
export type WizardScreen =
  | "pick"
  | "estimate"
  | "preparing"
  | "glossary"
  | "run"
  | "done"
  | "error";

/** Translation backends the engine's `serve --provider` accepts. The provider
 * is chosen before the sidecar spawns (it is bound to that process); changing
 * it requires a sidecar respawn, so it lives at the App shell, not per job. */
export type Provider = "anthropic" | "deepseek" | "openai" | "ollama";

export const PROVIDERS: Provider[] = ["anthropic", "deepseek", "openai", "ollama"];

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
    case "openai":
      return "gpt-5.6-luna";
    case "ollama":
      return "";
  }
}

/** Whether the given provider needs a non-empty API key before the sidecar
 * may be spawned. anthropic/deepseek are hosted APIs that authenticate with a
 * key, so the engine's `serve --key-stdin` exits(1) if the init line carries
 * no api_key — spawning with an empty key produces a doomed sidecar and a raw
 * handshake error. Ollama runs locally and needs no key. Kept pure/tested here
 * so App.tsx stays thin glue. */
export function providerRequiresKey(provider: Provider): boolean {
  return provider !== "ollama";
}

export interface ProgressInfo {
  /** Chunks of the run finished so far, already-translated ones included.
   * Only grows, even when a parallel run finishes chunks out of order. */
  completed: number;
  totalChunks: number;
  costUsd: number;
}

export interface GlossaryProgressInfo {
  batch: number;
  totalBatches: number;
  /** The job's running cost, every batch paid so far included. */
  costUsd: number;
}

export type GlossaryExtractionStatus = "done" | "failed" | "budget_exceeded" | "stopped";

/** How a glossary preparation ended; entries/conflicts only when "done". */
export interface GlossaryExtractionResult {
  status: GlossaryExtractionStatus;
  error: string | null;
  entries: number | null;
  conflicts: number | null;
}

export interface WizardState {
  screen: WizardScreen;
  filePath: string;
  fileError: string | null;
  jobId: string | null;
  createError: string | null;
  estimate: EstimateResponse | null;
  estimateError: string | null;
  /** Progress of the glossary preparation in flight. */
  glossaryProgress: GlossaryProgressInfo | null;
  /** The user asked to stop the glossary preparation in flight. */
  stopRequested: boolean;
  /** Why the last stop request failed (the pass goes on; the stop button is
   * enabled again so the user can retry). */
  stopError: string | null;
  /** Why the estimate screen was returned to from a preparation that did not
   * finish (stopped, failed, over budget, or could not start). */
  preparationMessage: string | null;
  /** What the finished preparation extracted, shown on the glossary screen. */
  glossaryNotice: string | null;
  glossary: GlossaryEntry[];
  /** The server's triage view of the glossary as last saved: per-entry
   * evidence and flags, in the same order as `glossary`. Never edited
   * locally; reloaded after each save. */
  glossaryReview: GlossaryReviewResponse | null;
  /** Why the review could not be loaded; the screen then falls back to the
   * plain list. */
  glossaryReviewError: string | null;
  /** Why the last glossary save failed (nothing was saved, nothing locked). */
  glossarySaveError: string | null;
  glossaryLocked: boolean;
  /** The chapters and narrators as last loaded or saved; null until loaded.
   * Empty for jobs without chapters (subtitles): no narrator section. */
  narratorChapters: NarratorChapter[] | null;
  /** The editable narrators, by chapter key (see `narrators.ts`). */
  narratorDraft: NarratorDraft;
  /** The "write it once for the first-person chapters" form. */
  narratorBulk: NarratorBulkForm;
  /** Why the chapters could not be loaded (blocks nothing else). */
  narratorLoadError: string | null;
  /** Why the last save failed; the edits are kept. */
  narratorSaveError: string | null;
  narratorSaving: boolean;
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
  glossaryProgress: null,
  stopRequested: false,
  stopError: null,
  preparationMessage: null,
  glossaryNotice: null,
  glossary: [],
  glossaryReview: null,
  glossaryReviewError: null,
  glossarySaveError: null,
  glossaryLocked: false,
  narratorChapters: null,
  narratorDraft: {},
  narratorBulk: emptyNarratorBulkForm,
  narratorLoadError: null,
  narratorSaveError: null,
  narratorSaving: false,
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

/** Fetches the glossary triage review and reports it, or why it failed (the
 * screen then falls back to the plain list). `getReview` is the API call. */
export async function loadGlossaryReview(
  getReview: () => Promise<GlossaryReviewResponse>,
  dispatch: (action: WizardAction) => void,
): Promise<void> {
  try {
    dispatch({ type: "GLOSSARY_REVIEW_LOADED", review: await getReview() });
  } catch (err) {
    dispatch({ type: "GLOSSARY_REVIEW_FAILED", message: String(err) });
  }
}

/** The seams a glossary save needs, injected for the same reason as
 * `RecoveryActions`: the decision stays here, tested, and WizardScreen.tsx
 * only wires the API calls. */
export interface GlossarySaveSeams {
  /** PUT /jobs/{id}/glossary with these entries (an upsert). */
  put: (entries: GlossaryEntry[]) => Promise<unknown>;
  /** Reload the review after a save (see `loadGlossaryReview`). */
  loadReview: () => Promise<void>;
  dispatch: (action: WizardAction) => void;
}

/** "save" stores the changed entries and reloads the review, so flags and
 * gender verdicts reflect them; "lock" stores them and locks the glossary for
 * the run. A refused PUT (HTTP 422: the glossary rules rejected an entry,
 * nothing was stored) is reported and never followed by a lock — locking a
 * glossary that was not saved would send the person to pay for a
 * translation without their fixes. */
export async function saveGlossary(
  state: WizardState,
  mode: "save" | "lock",
  seams: GlossarySaveSeams,
): Promise<void> {
  const changes = glossaryChangesToSave(state.glossaryReview, state.glossary);
  if (changes.length > 0) {
    try {
      await seams.put(changes);
    } catch (err) {
      seams.dispatch({ type: "GLOSSARY_SAVE_FAILED", message: String(err) });
      return;
    }
  }
  if (mode === "lock") {
    seams.dispatch({ type: "GLOSSARY_LOCKED" });
  } else if (changes.length > 0) {
    await seams.loadReview();
  }
}

/** Fetches the chapters and their narrators and reports them, or why they
 * could not be loaded (the rest of the screen works without them). */
export async function loadNarrators(
  jobId: string,
  getNarrators: () => Promise<NarratorsResponse>,
  dispatch: (action: WizardAction) => void,
): Promise<void> {
  try {
    dispatch({ type: "NARRATORS_LOADED", jobId, response: await getNarrators() });
  } catch (err) {
    dispatch({ type: "NARRATORS_LOAD_FAILED", jobId, message: String(err) });
  }
}

export interface NarratorSaveSeams {
  /** PUT /jobs/{id}/narrators with this full list (an overwrite). */
  put: (narrators: NarratorAssignment[]) => Promise<NarratorsResponse>;
  dispatch: (action: WizardAction) => void;
}

/** Sends every chapter that has a narrator (the PUT replaces them all) and
 * shows what the server stored; a refused save keeps the edits. */
export async function saveNarrators(state: WizardState, seams: NarratorSaveSeams): Promise<void> {
  const { jobId } = state;
  if (!state.narratorChapters || !jobId) return;
  const payload = narratorsPayload(state.narratorChapters, state.narratorDraft);
  seams.dispatch({ type: "NARRATORS_SAVE_STARTED" });
  try {
    seams.dispatch({ type: "NARRATORS_LOADED", jobId, response: await seams.put(payload) });
  } catch (err) {
    seams.dispatch({ type: "NARRATORS_SAVE_FAILED", jobId, message: String(err) });
  }
}

// Serve-api spec: only .epub/.srt are accepted; PDF is rejected with a
// clear message both here (client-side, before any request is sent) and by
// the server's own 422 (surfaced via JOB_CREATE_FAILED if it is ever hit).
const ACCEPTED_EXTENSIONS = [".epub", ".srt"];

export function validateFilePath(path: string): string | null {
  const lower = path.toLowerCase();
  if (lower.endsWith(".pdf")) {
    return "Los archivos PDF no se admiten como entrada de traducción. Elegí un archivo EPUB o SRT.";
  }
  if (!ACCEPTED_EXTENSIONS.some((ext) => lower.endsWith(ext))) {
    return "Solo se admiten archivos .epub y .srt.";
  }
  return null;
}

export function formatBestEffortSummary(count: number): string | null {
  if (count <= 0) return null;
  return count === 1
    ? "1 fragmento quedó en modo best-effort"
    : `${count} fragmentos quedaron en modo best-effort`;
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
    `${failedCount} de ${totalChunks} fragmentos no se pudieron traducir — ` +
    `el archivo de salida contiene texto original sin traducir para ` +
    `${failedCount === 1 ? "ese fragmento" : "esos fragmentos"}.`
  );
}

/** The done screen's "where is my file" line. The engine writes the output
 * beside the source only when the run finishes (DONE), so a stopped run, or
 * one whose write failed (runError), names no file. */
export function formatOutputLocation(
  status: JobStatus,
  outPath: string | null,
  runError: string | null,
): string | null {
  if (status !== "DONE" || runError || !outPath) return null;
  return `La traducción quedó en: ${outPath}`;
}

export function formatProgress(progress: ProgressInfo): string {
  return `Fragmento ${progress.completed}/${progress.totalChunks} — $${progress.costUsd.toFixed(4)}`;
}

/** The estimate screen's headline: what translating the whole book costs,
 * glossary preparation included while it is still ahead. */
export function formatBookEstimate(estimate: EstimateResponse): string {
  return (
    `Costo estimado del libro: $${estimate.book_usd_low.toFixed(2)} – ` +
    `$${estimate.book_usd_high.toFixed(2)}`
  );
}

/** Shown beside the button that starts the glossary preparation, so a
 * person knows that clicking it starts spending on the book. */
export const START_TRANSLATION_NOTE =
  "Traducir el libro empieza por preparar su glosario: es el primer paso pago. " +
  "Después podrá revisarlo, y el texto se traduce cuando usted lo confirme.";

/** The glossary's share of the book estimate. Preparing it is not optional:
 * it is the first paid step of the translation. */
export function formatGlossaryExtractionLine(estimate: GlossaryExtractionEstimate): string {
  const calls = estimate.batches === 1 ? "1 llamada" : `${estimate.batches} llamadas`;
  return (
    `Incluye la preparación del glosario: $${estimate.usd.toFixed(2)} – ` +
    `$${estimate.usd_high.toFixed(2)}, ${calls}`
  );
}

export function formatGlossaryProgress(progress: GlossaryProgressInfo): string {
  return `Lote ${progress.batch}/${progress.totalBatches} — $${progress.costUsd.toFixed(4)}`;
}

/** What a finished preparation extracted. Conflicts are terms whose batches
 * proposed different renderings; only their count is shown for now. */
export function formatGlossaryExtractionSummary(entries: number, conflicts: number): string {
  const extracted = entries === 1 ? "1 término extraído" : `${entries} términos extraídos`;
  if (conflicts <= 0) return extracted;
  return `${extracted}, ${conflicts} con traducciones distintas a lo largo del libro`;
}

/** Why the estimate screen is shown again after a preparation that did not
 * finish. *spentUsd* is the job's running cost from the last progress event
 * (null when no batch reported one): batches already paid for stay paid. */
export function formatGlossaryExtractionEnd(
  status: Exclude<GlossaryExtractionStatus, "done">,
  error: string | null,
  spentUsd: number | null,
): string {
  let headline: string;
  switch (status) {
    case "stopped":
      headline = "La preparación del glosario se detuvo.";
      break;
    case "budget_exceeded":
      headline =
        "La preparación del glosario se detuvo: el siguiente lote podía superar el presupuesto.";
      break;
    case "failed":
      headline = `La preparación del glosario falló: ${error ?? "error desconocido"}`;
      break;
  }
  const spent =
    spentUsd === null ? "" : ` Costo acumulado del trabajo: $${spentUsd.toFixed(4)}.`;
  return `${headline}${spent} Volver a prepararlo procesa el libro completo de nuevo.`;
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
  /** The chunk's place in the run. Out of order on a parallel run. */
  position: number;
  /** Chunks of the run finished so far — what the progress bar follows. */
  completed: number;
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

/** Extracts the `data:` payload from one raw SSE frame (the text between two
 * blank lines). The fetch-stream client hand-parses frames because it reads
 * the token-authenticated stream via `fetch` (which, unlike `EventSource`,
 * can set the `X-Borgesica-Token` header — keeping the token out of the URL).
 *
 * Follows the SSE wire format: lines starting with `data:` contribute the
 * payload (optional single leading space stripped), multiple `data:` lines
 * are joined with `\n`, and a frame with no `data:` line (e.g. a
 * `: heartbeat` comment) yields `null` so the caller ignores it. Never
 * throws — pure string handling only. */
export function extractSseData(frame: string): string | null {
  const dataLines = frame
    .split("\n")
    .map((line) => (line.endsWith("\r") ? line.slice(0, -1) : line))
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).replace(/^ /, ""));
  if (dataLines.length === 0) return null;
  return dataLines.join("\n");
}

/** Parses one SSE `data:` payload (already extracted by `extractSseData`).
 * Never throws — malformed or unrecognized payloads (e.g. transient noise)
 * become `null` so the caller can safely ignore them instead of crashing the
 * stream. */
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

export interface SseGlossaryProgressEvent {
  type: "glossary_progress";
  job_id: string;
  batch: number;
  total_batches: number;
  cost_usd: number;
}

export interface SseGlossaryTerminalEvent {
  type: "glossary_terminal";
  status: GlossaryExtractionStatus;
  error: string | null;
  entries: number | null;
  conflicts: number | null;
  cost_usd: number | null;
}

/** What the event stream can carry while a glossary preparation is watched.
 * A plain `terminal` arrives when the server knows of no extraction for the
 * job (e.g. the sidecar restarted, or the job left CREATED). */
export type SseGlossaryParsedEvent =
  | SseGlossaryProgressEvent
  | SseGlossaryTerminalEvent
  | SseTerminalEvent;

/** `parseSseData`'s counterpart for a glossary preparation stream. Never
 * throws; translation progress and malformed payloads become `null`. */
export function parseGlossarySseData(raw: string): SseGlossaryParsedEvent | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const candidate = parsed as { type?: unknown };
  if (
    candidate.type === "glossary_progress" ||
    candidate.type === "glossary_terminal" ||
    candidate.type === "terminal"
  ) {
    return parsed as SseGlossaryParsedEvent;
  }
  return null;
}

/** How the watched preparation ended. A plain `terminal` means the stream
 * has no extraction outcome to give, which the wizard treats as a failure. */
export function glossaryResultFromEvent(
  event: SseGlossaryTerminalEvent | SseTerminalEvent,
): GlossaryExtractionResult {
  if (event.type === "glossary_terminal") {
    return {
      status: event.status,
      error: event.error,
      entries: event.entries,
      conflicts: event.conflicts,
    };
  }
  return {
    status: "failed",
    error: `la preparación terminó sin resultado (estado del trabajo: ${event.status})`,
    entries: null,
    conflicts: null,
  };
}

export function sseEventToAction(event: SseParsedEvent): WizardAction {
  if (event.type === "progress") {
    return {
      type: "PROGRESS_RECEIVED",
      progress: {
        completed: event.completed,
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
  /** The user clicked "prepare glossary" (dispatched before the POST, so the
   * paid action cannot be clicked twice). */
  | { type: "GLOSSARY_EXTRACTION_STARTED" }
  | { type: "GLOSSARY_EXTRACTION_START_FAILED"; message: string }
  | { type: "GLOSSARY_PROGRESS_RECEIVED"; progress: GlossaryProgressInfo }
  | { type: "GLOSSARY_STOP_REQUESTED" }
  /** The stop request itself failed (network error, or the server refused
   * it); the pass may still be running. */
  | { type: "GLOSSARY_STOP_FAILED"; message: string }
  | {
      type: "GLOSSARY_EXTRACTION_ENDED";
      result: GlossaryExtractionResult;
      /** The glossary fetched after a "done" preparation, when available. */
      glossary?: GlossaryEntry[];
      /** The estimate re-fetched after any other end, when available. */
      estimate?: EstimateResponse;
    }
  | { type: "GLOSSARY_LOADED"; entries: GlossaryEntry[] }
  | { type: "GLOSSARY_CHANGED"; entries: GlossaryEntry[] }
  /** The triage review arrived (on entering the screen, or after a save). */
  | { type: "GLOSSARY_REVIEW_LOADED"; review: GlossaryReviewResponse }
  | { type: "GLOSSARY_REVIEW_FAILED"; message: string }
  /** One entry's rendering, gender or lock changed on the triage screen. */
  | {
      type: "GLOSSARY_ENTRY_EDITED";
      index: number;
      patch: Partial<Pick<GlossaryEntry, "translation" | "gender" | "locked">>;
    }
  | { type: "GLOSSARY_SAVE_FAILED"; message: string }
  | { type: "GLOSSARY_LOCKED" }
  /** The chapters arrived (on entering the screen, or as a save's reply).
   * Every narrator reply names the job it was asked for: one that arrives
   * after the person moved to another job is dropped. */
  | { type: "NARRATORS_LOADED"; jobId: string; response: NarratorsResponse }
  | { type: "NARRATORS_LOAD_FAILED"; jobId: string; message: string }
  | { type: "NARRATOR_BULK_CHANGED"; form: NarratorBulkForm }
  /** The bulk form's narrator goes to every chapter marked first-person. */
  | { type: "NARRATORS_APPLIED_TO_FIRST_PERSON" }
  /** One chapter's narrator changed; null clears it. */
  | { type: "NARRATOR_CHAPTER_EDITED"; key: string; narrator: NarratorFact | null }
  /** This chapter's narrator goes to every following first-person chapter. */
  | { type: "NARRATOR_APPLIED_ONWARD"; key: string }
  | { type: "NARRATORS_SAVE_STARTED" }
  | { type: "NARRATORS_SAVE_FAILED"; jobId: string; message: string }
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

type NarratorState = Pick<
  WizardState,
  | "narratorChapters"
  | "narratorDraft"
  | "narratorBulk"
  | "narratorLoadError"
  | "narratorSaveError"
  | "narratorSaving"
>;

/** The narrator fields to carry into `jobId`: kept for the same job, reset
 * for any other, so one book's chapters never show on another. */
function narratorStateFor(state: WizardState, jobId: string): NarratorState {
  const source = jobId === state.jobId ? state : initialWizardState;
  return {
    narratorChapters: source.narratorChapters,
    narratorDraft: source.narratorDraft,
    narratorBulk: source.narratorBulk,
    narratorLoadError: source.narratorLoadError,
    narratorSaveError: source.narratorSaveError,
    narratorSaving: source.narratorSaving,
  };
}

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
      return {
        ...state,
        ...narratorStateFor(state, action.jobId),
        jobId: action.jobId,
        createError: null,
        screen: "estimate",
      };
    case "JOB_CREATE_FAILED":
      return { ...state, createError: action.message };
    case "ESTIMATE_RECEIVED":
      return { ...state, estimate: action.estimate, estimateError: null };
    case "ESTIMATE_FAILED":
      return { ...state, estimateError: action.message };
    case "PROCEED_TO_GLOSSARY":
      // Guard (spec: glossary strictly pre-run, always after an estimate).
      if (!state.estimate) return state;
      // Guard: a preparation in flight ends only through its own terminal.
      if (state.screen === "preparing") return state;
      return { ...state, screen: "glossary" };
    case "GLOSSARY_EXTRACTION_STARTED":
      // Guard: the paid pass starts only from the estimate screen, and only
      // while that screen shows its cost line (null once extracted).
      if (state.screen !== "estimate" || !state.estimate?.glossary_extraction) return state;
      return {
        ...state,
        screen: "preparing",
        glossaryProgress: null,
        stopRequested: false,
        stopError: null,
        preparationMessage: null,
      };
    case "GLOSSARY_EXTRACTION_START_FAILED":
      if (state.screen !== "preparing") return state;
      return {
        ...state,
        screen: "estimate",
        glossaryProgress: null,
        stopRequested: false,
        stopError: null,
        preparationMessage: `No se pudo iniciar la preparación del glosario: ${action.message}`,
      };
    case "GLOSSARY_PROGRESS_RECEIVED":
      if (state.screen !== "preparing") return state;
      return { ...state, glossaryProgress: action.progress };
    case "GLOSSARY_STOP_REQUESTED":
      if (state.screen !== "preparing") return state;
      return { ...state, stopRequested: true, stopError: null };
    case "GLOSSARY_STOP_FAILED":
      // Re-enable the stop button: otherwise it stays disabled while the
      // paid pass keeps going.
      if (state.screen !== "preparing") return state;
      return {
        ...state,
        stopRequested: false,
        stopError: `No se pudo detener la preparación: ${action.message}`,
      };
    case "GLOSSARY_EXTRACTION_ENDED": {
      if (state.screen !== "preparing") return state;
      const { result } = action;
      const cleared = {
        ...state,
        glossaryProgress: null,
        stopRequested: false,
        stopError: null,
      };
      if (result.status === "done") {
        return {
          ...cleared,
          screen: "glossary",
          glossary: action.glossary ?? state.glossary,
          glossaryNotice: formatGlossaryExtractionSummary(
            result.entries ?? 0,
            result.conflicts ?? 0,
          ),
          // The job's glossary is extracted now; the line must not be offered
          // again even if this estimate is shown before a re-fetch.
          estimate: state.estimate ? { ...state.estimate, glossary_extraction: null } : null,
        };
      }
      return {
        ...cleared,
        screen: "estimate",
        estimate: action.estimate ?? state.estimate,
        preparationMessage: formatGlossaryExtractionEnd(
          result.status,
          result.error,
          state.glossaryProgress?.costUsd ?? null,
        ),
      };
    }
    case "GLOSSARY_LOADED":
      return { ...state, glossary: action.entries };
    case "GLOSSARY_CHANGED":
      // Guard (spec: glossary edit/lock strictly pre-run — once locked, no
      // further edits are accepted by this reducer).
      if (state.glossaryLocked) return state;
      return { ...state, glossary: action.entries };
    case "GLOSSARY_REVIEW_LOADED":
      // A review replaces the editable entries with what was saved, so it
      // must not land after the glossary is locked for the run.
      if (state.glossaryLocked) return state;
      return {
        ...state,
        glossaryReview: action.review,
        glossary: action.review.entries.map((item) => item.entry),
        glossaryReviewError: null,
        glossarySaveError: null,
      };
    case "GLOSSARY_REVIEW_FAILED":
      return { ...state, glossaryReviewError: `No se pudo cargar la revisión: ${action.message}` };
    case "GLOSSARY_ENTRY_EDITED": {
      if (state.glossaryLocked) return state;
      const current = state.glossary[action.index];
      if (!current) return state;
      const glossary = state.glossary.map((entry, i) =>
        i === action.index ? { ...entry, ...action.patch } : entry,
      );
      return { ...state, glossary };
    }
    case "GLOSSARY_SAVE_FAILED":
      return {
        ...state,
        glossarySaveError: `No se pudieron guardar los cambios del glosario: ${action.message}`,
      };
    case "GLOSSARY_LOCKED":
      return { ...state, glossaryLocked: true };
    case "NARRATORS_LOADED":
      if (action.jobId !== state.jobId) return state;
      return {
        ...state,
        narratorChapters: action.response.chapters,
        narratorDraft: draftFromChapters(action.response.chapters),
        narratorLoadError: null,
        narratorSaveError: null,
        narratorSaving: false,
      };
    case "NARRATORS_LOAD_FAILED":
      if (action.jobId !== state.jobId) return state;
      return {
        ...state,
        narratorLoadError: `No se pudieron cargar los capítulos para indicar el narrador: ${action.message}`,
      };
    case "NARRATOR_BULK_CHANGED":
      return { ...state, narratorBulk: action.form };
    case "NARRATORS_APPLIED_TO_FIRST_PERSON": {
      const fact = bulkNarrator(state.narratorBulk);
      if (!fact || !state.narratorChapters) return state;
      return {
        ...state,
        narratorDraft: applyToFirstPerson(state.narratorChapters, state.narratorDraft, fact),
      };
    }
    case "NARRATOR_CHAPTER_EDITED":
      return {
        ...state,
        narratorDraft: setChapterNarrator(state.narratorDraft, action.key, action.narrator),
      };
    case "NARRATOR_APPLIED_ONWARD": {
      const fact = state.narratorDraft[action.key];
      if (!fact || !state.narratorChapters) return state;
      return {
        ...state,
        narratorDraft: applyFromChapterOnward(
          state.narratorChapters,
          state.narratorDraft,
          action.key,
          fact,
        ),
      };
    }
    case "NARRATORS_SAVE_STARTED":
      return { ...state, narratorSaving: true, narratorSaveError: null };
    case "NARRATORS_SAVE_FAILED":
      if (action.jobId !== state.jobId) return state;
      return {
        ...state,
        narratorSaving: false,
        narratorSaveError: `No se pudieron guardar los narradores: ${action.message}`,
      };
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
        ...narratorStateFor(state, action.job.id),
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
      // Guard: only a run or a glossary preparation in flight can "lose
      // connection" in the dead-end sense (RES-001) — no-op elsewhere so this
      // can never clobber an unrelated screen.
      if (state.screen !== "run" && state.screen !== "preparing") return state;
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
