import { describe, expect, it, vi } from "vitest";
import {
  defaultModelForProvider,
  extractSseData,
  formatBestEffortSummary,
  formatFailureSummary,
  formatGlossaryExtractionEnd,
  formatGlossaryExtractionLine,
  formatGlossaryExtractionSummary,
  formatGlossaryProgress,
  formatOutputLocation,
  formatProgress,
  glossaryResultFromEvent,
  initialWizardState,
  isPersistentConnectionFailure,
  isRunningStatus,
  loadGlossaryReview,
  MAX_CONSECUTIVE_CONNECTION_FAILURES,
  parseGlossarySseData,
  parseSseData,
  providerRequiresKey,
  PROVIDERS,
  recoveryRequiresRespawn,
  runRecoveryAction,
  saveGlossary,
  loadNarrators,
  saveNarrators,
  sseEventToAction,
  validateFilePath,
  wizardReducer,
  type WizardState,
} from "./wizard";
import type {
  EstimateResponse,
  GlossaryExtractionEstimate,
  GlossaryReviewResponse,
  JobResponse,
  NarratorFact,
  NarratorsResponse,
} from "./apiTypes";

describe("validateFilePath", () => {
  it("rejects .pdf with a clear message (spec: PDF rejected in picker)", () => {
    expect(validateFilePath("book.pdf")).toMatch(/pdf/i);
  });

  it("rejects .PDF case-insensitively", () => {
    expect(validateFilePath("BOOK.PDF")).toMatch(/pdf/i);
  });

  it("accepts .epub", () => {
    expect(validateFilePath("novel.epub")).toBeNull();
  });

  it("accepts .srt", () => {
    expect(validateFilePath("subtitles.srt")).toBeNull();
  });

  it("rejects unknown extensions", () => {
    expect(validateFilePath("notes.txt")).toMatch(/epub|srt/i);
  });
});

describe("formatBestEffortSummary", () => {
  it("is null when count is zero (no noise per spec)", () => {
    expect(formatBestEffortSummary(0)).toBeNull();
  });

  it("reports a single chunk in singular form", () => {
    expect(formatBestEffortSummary(1)).toBe("1 fragmento quedó en modo best-effort");
  });

  it("reports multiple chunks in plural form", () => {
    expect(formatBestEffortSummary(3)).toBe("3 fragmentos quedaron en modo best-effort");
  });
});

describe("formatOutputLocation", () => {
  const out = "C:\\libros\\book_es.epub";

  it("names the file a finished run wrote beside the source", () => {
    expect(formatOutputLocation("DONE", out, null)).toBe(`La traducción quedó en: ${out}`);
  });

  it("says nothing when writing the file failed (it was never written)", () => {
    expect(formatOutputLocation("DONE", out, "[WinError 5] Acceso denegado")).toBeNull();
  });

  it("says nothing when the run stopped before the end (no file is written)", () => {
    expect(formatOutputLocation("PAUSED", out, null)).toBeNull();
    expect(formatOutputLocation("CANCELLED", out, null)).toBeNull();
  });

  it("says nothing when the engine reports no path (no run started in this session)", () => {
    expect(formatOutputLocation("DONE", null, null)).toBeNull();
  });
});

describe("formatFailureSummary", () => {
  it("is null when no chunks failed (a clean success shows no alarm)", () => {
    expect(formatFailureSummary(0, 10)).toBeNull();
  });

  it("warns that the output contains untranslated text when chunks failed", () => {
    const msg = formatFailureSummary(3, 10);
    expect(msg).toMatch(/3 de 10/);
    expect(msg).toMatch(/no se pudieron traducir/i);
    expect(msg).toMatch(/sin traducir/i);
  });

  it("reports a single failed chunk too (one failure is still not a success)", () => {
    expect(formatFailureSummary(1, 4)).toMatch(/1 de 4/);
  });
});

describe("provider selection", () => {
  it("offers the four engine-supported providers", () => {
    expect(PROVIDERS).toEqual(["anthropic", "deepseek", "openai", "ollama"]);
  });

  it("defaults Anthropic to a current (non-retired) model id", () => {
    expect(defaultModelForProvider("anthropic")).toBe("claude-sonnet-5");
  });

  it("defaults DeepSeek to a DeepSeek model id, not an Anthropic one", () => {
    const model = defaultModelForProvider("deepseek");
    expect(model).toMatch(/deepseek/);
    expect(model).not.toMatch(/claude/);
  });

  it("defaults OpenAI to gpt-5.6-luna", () => {
    expect(defaultModelForProvider("openai")).toBe("gpt-5.6-luna");
  });

  it("leaves the model blank for Ollama (models are user-local, not fixed)", () => {
    expect(defaultModelForProvider("ollama")).toBe("");
  });
});

describe("providerRequiresKey", () => {
  it("requires a key for anthropic (hosted, key-authenticated)", () => {
    expect(providerRequiresKey("anthropic")).toBe(true);
  });

  it("requires a key for deepseek (hosted, key-authenticated)", () => {
    expect(providerRequiresKey("deepseek")).toBe(true);
  });

  it("requires a key for openai (hosted, key-authenticated)", () => {
    expect(providerRequiresKey("openai")).toBe(true);
  });

  it("does not require a key for ollama (runs locally)", () => {
    expect(providerRequiresKey("ollama")).toBe(false);
  });
});

describe("formatProgress", () => {
  it("renders chunk counter and cost", () => {
    expect(
      formatProgress({ completed: 4, totalChunks: 10, costUsd: 1.5 }),
    ).toBe("Fragmento 4/10 — $1.5000");
  });
});

describe("isRunningStatus", () => {
  it("is true only for RUNNING", () => {
    expect(isRunningStatus("RUNNING")).toBe(true);
    expect(isRunningStatus("PAUSED")).toBe(false);
    expect(isRunningStatus("DONE")).toBe(false);
  });
});

describe("parseSseData", () => {
  it("parses a progress event", () => {
    const raw = JSON.stringify({
      type: "progress",
      job_id: "abc",
      chunk_index: 2,
      total_chunks: 5,
      cost_usd: 0.2,
      status: "RUNNING",
    });
    expect(parseSseData(raw)).toEqual({
      type: "progress",
      job_id: "abc",
      chunk_index: 2,
      total_chunks: 5,
      cost_usd: 0.2,
      status: "RUNNING",
    });
  });

  it("parses a terminal event", () => {
    const raw = JSON.stringify({ type: "terminal", status: "DONE", error: null });
    expect(parseSseData(raw)).toEqual({
      type: "terminal",
      status: "DONE",
      error: null,
    });
  });

  it("returns null on malformed JSON (never throws into the caller)", () => {
    expect(parseSseData("not json")).toBeNull();
  });

  it("returns null on an unrecognized event type", () => {
    expect(parseSseData(JSON.stringify({ type: "mystery" }))).toBeNull();
  });
});

describe("extractSseData", () => {
  it("returns the payload of a single data: line (leading space stripped)", () => {
    expect(extractSseData("data: {\"type\":\"terminal\"}")).toBe('{"type":"terminal"}');
  });

  it("tolerates a data: line with no space after the colon", () => {
    expect(extractSseData("data:{\"x\":1}")).toBe('{"x":1}');
  });

  it("ignores comment/heartbeat frames (no data field)", () => {
    expect(extractSseData(": heartbeat")).toBeNull();
  });

  it("joins multiple data: lines with newlines (SSE spec)", () => {
    expect(extractSseData("data: a\ndata: b")).toBe("a\nb");
  });

  it("tolerates CRLF line endings", () => {
    expect(extractSseData("data: {\"ok\":true}\r")).toBe('{"ok":true}');
  });
});

describe("sseEventToAction", () => {
  it("maps a progress event to PROGRESS_RECEIVED, counting finished chunks", () => {
    // A parallel run finishes chunks out of order: chunk 7 can be the third
    // one done. The bar follows `completed`, never the chunk's own index.
    expect(
      sseEventToAction({
        type: "progress",
        job_id: "abc",
        chunk_index: 7,
        position: 8,
        completed: 3,
        total_chunks: 10,
        cost_usd: 0.2,
        status: "RUNNING",
      }),
    ).toEqual({
      type: "PROGRESS_RECEIVED",
      progress: { completed: 3, totalChunks: 10, costUsd: 0.2 },
    });
  });

  it("maps a terminal event to RUN_TERMINAL", () => {
    expect(
      sseEventToAction({ type: "terminal", status: "FAILED", error: "boom" }),
    ).toEqual({ type: "RUN_TERMINAL", status: "FAILED", error: "boom" });
  });
});

describe("wizardReducer", () => {
  it("FILE_PATH_CHANGED sets a fileError for a rejected extension", () => {
    const next = wizardReducer(initialWizardState, {
      type: "FILE_PATH_CHANGED",
      path: "book.pdf",
    });
    expect(next.fileError).toMatch(/pdf/i);
    expect(next.filePath).toBe("book.pdf");
  });

  it("FILE_PATH_CHANGED clears fileError for an accepted extension", () => {
    const withError = wizardReducer(initialWizardState, {
      type: "FILE_PATH_CHANGED",
      path: "book.pdf",
    });
    const next = wizardReducer(withError, {
      type: "FILE_PATH_CHANGED",
      path: "book.epub",
    });
    expect(next.fileError).toBeNull();
  });

  it("JOB_CREATED advances to the estimate screen", () => {
    const next = wizardReducer(initialWizardState, {
      type: "JOB_CREATED",
      jobId: "job-1",
    });
    expect(next.screen).toBe("estimate");
    expect(next.jobId).toBe("job-1");
  });

  it("JOB_CREATED is a no-op guard when a file error is present", () => {
    const withError = wizardReducer(initialWizardState, {
      type: "FILE_PATH_CHANGED",
      path: "book.pdf",
    });
    const next = wizardReducer(withError, {
      type: "JOB_CREATED",
      jobId: "job-1",
    });
    expect(next.screen).toBe("pick");
    expect(next.jobId).toBeNull();
  });

  it("PROCEED_TO_GLOSSARY is a no-op guard without an estimate (no run without estimate)", () => {
    const next = wizardReducer(initialWizardState, {
      type: "PROCEED_TO_GLOSSARY",
    });
    expect(next.screen).toBe("pick");
  });

  it("PROCEED_TO_GLOSSARY advances once an estimate exists", () => {
    const withEstimate: WizardState = {
      ...initialWizardState,
      estimate: {
        input_tokens: 1,
        output_tokens: 1,
        usd: 1,
        usd_low: 0.5,
        usd_high: 1.5,
        model: "m",
        cached: false,
        within_budget: true,
        glossary_extraction: null,
        book_usd_low: 0.5,
        book_usd_high: 1.5,
      },
    };
    const next = wizardReducer(withEstimate, { type: "PROCEED_TO_GLOSSARY" });
    expect(next.screen).toBe("glossary");
  });

  it("RUN_STARTED is a hard guard no-op without an estimate", () => {
    const next = wizardReducer(initialWizardState, {
      type: "RUN_STARTED",
      job: {
        id: "job-1",
        status: "RUNNING",
        total_chunks: 10,
        completed_chunks: 0,
        cost_usd: 0,
        best_effort_count: 0,
        best_effort_indices: [],
        failed_count: 0,
        failed_indices: [],
        glossary_extracted: false,
        out_path: "C:\\libros\\book_es.epub",
      },
    });
    expect(next.screen).toBe("pick");
    expect(next.job).toBeNull();
  });

  it("GLOSSARY_CHANGED is a no-op once locked", () => {
    const locked: WizardState = { ...initialWizardState, glossaryLocked: true };
    const next = wizardReducer(locked, {
      type: "GLOSSARY_CHANGED",
      entries: [{ term: "a", translation: "b", locked: false }],
    });
    expect(next.glossary).toEqual([]);
  });

  it("RUN_TERMINAL moves to the done screen and records the final status", () => {
    const running: WizardState = {
      ...initialWizardState,
      screen: "run",
      job: {
        id: "job-1",
        status: "RUNNING",
        total_chunks: 10,
        completed_chunks: 3,
        cost_usd: 0.4,
        best_effort_count: 0,
        best_effort_indices: [],
        failed_count: 0,
        failed_indices: [],
        glossary_extracted: false,
        out_path: "C:\\libros\\book_es.epub",
      },
    };
    const next = wizardReducer(running, {
      type: "RUN_TERMINAL",
      status: "DONE",
      error: null,
    });
    expect(next.screen).toBe("done");
    expect(next.job?.status).toBe("DONE");
  });

  it("RUN_TERMINAL replaces the job with the refreshed status when provided " +
    "(so failed/best-effort counts reach the done screen, not the stale run ack)", () => {
    const running: WizardState = {
      ...initialWizardState,
      screen: "run",
      job: {
        id: "job-1",
        status: "RUNNING",
        total_chunks: 5,
        completed_chunks: 0,
        cost_usd: 0,
        best_effort_count: 0,
        best_effort_indices: [],
        failed_count: 0,
        failed_indices: [],
        glossary_extracted: false,
        out_path: "C:\\libros\\book_es.epub",
      },
    };
    const refreshed: JobResponse = {
      id: "job-1",
      status: "DONE",
      total_chunks: 5,
      completed_chunks: 0,
      cost_usd: 0,
      best_effort_count: 0,
      best_effort_indices: [],
      failed_count: 5,
      failed_indices: [0, 1, 2, 3, 4],
      glossary_extracted: false,
      out_path: "C:\\libros\\book_es.epub",
    };
    const next = wizardReducer(running, {
      type: "RUN_TERMINAL",
      status: "DONE",
      error: null,
      job: refreshed,
    });
    expect(next.screen).toBe("done");
    expect(next.job).toEqual(refreshed);
    expect(next.job?.failed_count).toBe(5);
  });

  it("RESET returns to the initial state", () => {
    const dirty: WizardState = { ...initialWizardState, screen: "glossary" };
    expect(wizardReducer(dirty, { type: "RESET" })).toEqual(initialWizardState);
  });

  // -------------------------------------------------------------------
  // RES-001 — sidecar-death dead-end: CONNECTION_LOST / RECOVER_FROM_ERROR
  // -------------------------------------------------------------------

  it("CONNECTION_LOST moves the run screen to the error screen with a message", () => {
    const running: WizardState = {
      ...initialWizardState,
      screen: "run",
      jobId: "job-1",
    };
    const next = wizardReducer(running, {
      type: "CONNECTION_LOST",
      message: "Lost connection to the translation sidecar.",
    });
    expect(next.screen).toBe("error");
    expect(next.connectionError).toBe("Lost connection to the translation sidecar.");
  });

  it("CONNECTION_LOST is a no-op guard outside the run screen", () => {
    const next = wizardReducer(initialWizardState, {
      type: "CONNECTION_LOST",
      message: "Lost connection to the translation sidecar.",
    });
    expect(next.screen).toBe("pick");
    expect(next.connectionError).toBeNull();
  });

  it("RECOVER_FROM_ERROR returns from the error screen to pick, clearing the error", () => {
    const errored: WizardState = {
      ...initialWizardState,
      screen: "error",
      connectionError: "Lost connection to the translation sidecar.",
      jobId: "job-1",
    };
    const next = wizardReducer(errored, { type: "RECOVER_FROM_ERROR" });
    expect(next.screen).toBe("pick");
    expect(next.connectionError).toBeNull();
  });

  it("RECOVER_FROM_ERROR is a no-op guard outside the error screen", () => {
    const running: WizardState = { ...initialWizardState, screen: "run" };
    const next = wizardReducer(running, { type: "RECOVER_FROM_ERROR" });
    expect(next.screen).toBe("run");
  });
});

describe("isPersistentConnectionFailure", () => {
  it("is false below the consecutive-failure threshold", () => {
    expect(isPersistentConnectionFailure(1)).toBe(false);
    expect(isPersistentConnectionFailure(MAX_CONSECUTIVE_CONNECTION_FAILURES - 1)).toBe(
      false,
    );
  });

  it("is true at and above the consecutive-failure threshold", () => {
    expect(isPersistentConnectionFailure(MAX_CONSECUTIVE_CONNECTION_FAILURES)).toBe(true);
    expect(isPersistentConnectionFailure(MAX_CONSECUTIVE_CONNECTION_FAILURES + 5)).toBe(
      true,
    );
  });

  it("treats a single transient failure as non-persistent (spec: keep retrying blips)", () => {
    expect(isPersistentConnectionFailure(0)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// RES-001 (second correction round) — recovery must be REAL, not cosmetic:
// a permanent connection failure requires an actual sidecar respawn, not
// just a screen reset back to "pick".
// ---------------------------------------------------------------------------

describe("recoveryRequiresRespawn", () => {
  it("is true only from the error screen (a genuine permanent-failure recovery)", () => {
    const errored: WizardState = { ...initialWizardState, screen: "error" };
    expect(recoveryRequiresRespawn(errored)).toBe(true);
  });

  it("is false from every other screen — a transient blip never reaches 'error' " +
    "in the first place, so recovering from it must never force a respawn", () => {
    expect(recoveryRequiresRespawn({ ...initialWizardState, screen: "pick" })).toBe(false);
    expect(recoveryRequiresRespawn({ ...initialWizardState, screen: "run" })).toBe(false);
    expect(recoveryRequiresRespawn({ ...initialWizardState, screen: "done" })).toBe(false);
  });
});

describe("runRecoveryAction", () => {
  it("invokes the sidecar restart seam when recovering from a genuine connection loss", () => {
    const errored: WizardState = {
      ...initialWizardState,
      screen: "error",
      connectionError: "Lost connection to the translation sidecar.",
    };
    const closeSubscription = vi.fn();
    const dispatch = vi.fn();
    const restartSidecar = vi.fn();

    runRecoveryAction(errored, { closeSubscription, dispatch, restartSidecar });

    expect(closeSubscription).toHaveBeenCalledOnce();
    expect(dispatch).toHaveBeenCalledWith({ type: "RECOVER_FROM_ERROR" });
    expect(restartSidecar).toHaveBeenCalledOnce();
  });

  it("does NOT invoke the restart seam when there was no permanent failure to recover from " +
    "(a transient blip must never trigger a full respawn)", () => {
    const running: WizardState = { ...initialWizardState, screen: "run" };
    const restartSidecar = vi.fn();

    runRecoveryAction(running, {
      closeSubscription: vi.fn(),
      dispatch: vi.fn(),
      restartSidecar,
    });

    expect(restartSidecar).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Pre-translation glossary preparation (the explicit, paid extraction pass)
// ---------------------------------------------------------------------------

const EXTRACTION_LINE: GlossaryExtractionEstimate = {
  batches: 57,
  input_tokens: 400_000,
  output_tokens: 130_000,
  output_tokens_high: 160_000,
  usd: 0.1234,
  usd_high: 0.5678,
};

function estimateWith(
  glossary_extraction: GlossaryExtractionEstimate | null,
): EstimateResponse {
  return {
    input_tokens: 1,
    output_tokens: 1,
    usd: 1,
    usd_low: 0.5,
    usd_high: 1.5,
    model: "m",
    cached: false,
    within_budget: true,
    glossary_extraction,
    book_usd_low: 0.5,
    book_usd_high: 1.5,
  };
}

const onEstimate: WizardState = {
  ...initialWizardState,
  screen: "estimate",
  jobId: "job-1",
  estimate: estimateWith(EXTRACTION_LINE),
};

const preparing: WizardState = {
  ...onEstimate,
  screen: "preparing",
  glossaryProgress: { batch: 3, totalBatches: 57, costUsd: 0.0421 },
};

describe("formatGlossaryExtractionLine", () => {
  it("shows the glossary's share of the book: cost range and number of calls", () => {
    expect(formatGlossaryExtractionLine(EXTRACTION_LINE)).toBe(
      "Incluye la preparación del glosario: $0.12 – $0.57, 57 llamadas",
    );
  });

  it("uses the singular for a single call", () => {
    expect(formatGlossaryExtractionLine({ ...EXTRACTION_LINE, batches: 1 })).toMatch(
      /, 1 llamada$/,
    );
  });
});

describe("formatGlossaryProgress", () => {
  it("renders the batch counter and the job's cost so far", () => {
    expect(formatGlossaryProgress({ batch: 3, totalBatches: 57, costUsd: 0.0421 })).toBe(
      "Lote 3/57 — $0.0421",
    );
  });
});

describe("formatGlossaryExtractionSummary", () => {
  it("counts the extracted terms and the conflicting ones", () => {
    expect(formatGlossaryExtractionSummary(120, 7)).toBe(
      "120 términos extraídos, 7 con traducciones distintas a lo largo del libro",
    );
  });

  it("leaves the conflicts out when there are none", () => {
    expect(formatGlossaryExtractionSummary(1, 0)).toBe("1 término extraído");
  });
});

describe("formatGlossaryExtractionEnd", () => {
  it("says the preparation was stopped and what the job already spent", () => {
    const message = formatGlossaryExtractionEnd("stopped", null, 0.0421);
    expect(message).toMatch(/se detuvo/i);
    expect(message).toContain("$0.0421");
  });

  it("shows the error of a failed preparation", () => {
    expect(formatGlossaryExtractionEnd("failed", "Provider error (HTTP 500)", null)).toContain(
      "Provider error (HTTP 500)",
    );
  });

  it("names the budget when the next batch could exceed it", () => {
    expect(formatGlossaryExtractionEnd("budget_exceeded", "Budget exceeded", null)).toMatch(
      /presupuesto/i,
    );
  });

  it("omits the spend when no batch reported one", () => {
    expect(formatGlossaryExtractionEnd("stopped", null, null)).not.toContain("$");
  });
});

describe("parseGlossarySseData", () => {
  it("parses a glossary_progress event", () => {
    const raw = JSON.stringify({
      type: "glossary_progress",
      job_id: "job-1",
      batch: 2,
      total_batches: 5,
      cost_usd: 0.01,
    });
    expect(parseGlossarySseData(raw)).toEqual(JSON.parse(raw));
  });

  it("parses a glossary_terminal event", () => {
    const raw = JSON.stringify({
      type: "glossary_terminal",
      status: "stopped",
      error: "Glossary extraction stopped",
      entries: null,
      conflicts: null,
      cost_usd: null,
    });
    expect(parseGlossarySseData(raw)).toEqual(JSON.parse(raw));
  });

  it("parses a plain terminal event (the stream knows of no extraction)", () => {
    const raw = JSON.stringify({ type: "terminal", status: "CREATED", error: null });
    expect(parseGlossarySseData(raw)).toEqual(JSON.parse(raw));
  });

  it("returns null for translation progress and malformed payloads", () => {
    expect(parseGlossarySseData(JSON.stringify({ type: "progress" }))).toBeNull();
    expect(parseGlossarySseData("{not json")).toBeNull();
  });
});

describe("glossaryResultFromEvent", () => {
  it("carries a glossary_terminal's outcome as is", () => {
    expect(
      glossaryResultFromEvent({
        type: "glossary_terminal",
        status: "done",
        error: null,
        entries: 12,
        conflicts: 2,
        cost_usd: 0.3,
      }),
    ).toEqual({ status: "done", error: null, entries: 12, conflicts: 2 });
  });

  it("turns a plain terminal into a failed preparation naming the job status", () => {
    const result = glossaryResultFromEvent({
      type: "terminal",
      status: "CANCELLED",
      error: null,
    });
    expect(result.status).toBe("failed");
    expect(result.error).toContain("CANCELLED");
    expect(result.entries).toBeNull();
    expect(result.conflicts).toBeNull();
  });
});

describe("wizardReducer — glossary preparation", () => {
  it("GLOSSARY_EXTRACTION_STARTED moves the estimate screen to preparing", () => {
    const next = wizardReducer(
      { ...onEstimate, preparationMessage: "old message" },
      { type: "GLOSSARY_EXTRACTION_STARTED" },
    );
    expect(next.screen).toBe("preparing");
    expect(next.glossaryProgress).toBeNull();
    expect(next.stopRequested).toBe(false);
    expect(next.preparationMessage).toBeNull();
  });

  it("GLOSSARY_EXTRACTION_STARTED is a no-op guard when no extraction line was shown", () => {
    const extracted = { ...onEstimate, estimate: estimateWith(null) };
    expect(wizardReducer(extracted, { type: "GLOSSARY_EXTRACTION_STARTED" })).toBe(extracted);
  });

  it("GLOSSARY_EXTRACTION_STARTED is a no-op guard outside the estimate screen", () => {
    const onGlossary: WizardState = { ...onEstimate, screen: "glossary" };
    expect(wizardReducer(onGlossary, { type: "GLOSSARY_EXTRACTION_STARTED" })).toBe(onGlossary);
  });

  it("GLOSSARY_EXTRACTION_START_FAILED returns to the estimate screen with the error", () => {
    const next = wizardReducer(
      { ...preparing, glossaryProgress: null },
      { type: "GLOSSARY_EXTRACTION_START_FAILED", message: "HTTP 409: busy" },
    );
    expect(next.screen).toBe("estimate");
    expect(next.preparationMessage).toContain("HTTP 409: busy");
  });

  it("GLOSSARY_PROGRESS_RECEIVED records the batch counter while preparing", () => {
    const progress = { batch: 4, totalBatches: 57, costUsd: 0.05 };
    const next = wizardReducer(preparing, { type: "GLOSSARY_PROGRESS_RECEIVED", progress });
    expect(next.glossaryProgress).toEqual(progress);
  });

  it("GLOSSARY_PROGRESS_RECEIVED is ignored outside the preparing screen", () => {
    const next = wizardReducer(onEstimate, {
      type: "GLOSSARY_PROGRESS_RECEIVED",
      progress: { batch: 1, totalBatches: 2, costUsd: 0 },
    });
    expect(next).toBe(onEstimate);
  });

  it("GLOSSARY_STOP_REQUESTED marks the stop while preparing", () => {
    expect(wizardReducer(preparing, { type: "GLOSSARY_STOP_REQUESTED" }).stopRequested).toBe(
      true,
    );
  });

  it("PROCEED_TO_GLOSSARY is a no-op guard while preparing (no skipping a pass in flight)", () => {
    expect(wizardReducer(preparing, { type: "PROCEED_TO_GLOSSARY" })).toBe(preparing);
  });

  it("GLOSSARY_EXTRACTION_ENDED done shows the glossary screen with the fetched entries", () => {
    const entries = [{ term: "Vis", translation: "Vis", locked: false }];
    const next = wizardReducer(preparing, {
      type: "GLOSSARY_EXTRACTION_ENDED",
      result: { status: "done", error: null, entries: 12, conflicts: 2 },
      glossary: entries,
    });
    expect(next.screen).toBe("glossary");
    expect(next.glossary).toEqual(entries);
    expect(next.glossaryNotice).toBe(formatGlossaryExtractionSummary(12, 2));
    // The pass cannot be offered again: the job's glossary is extracted.
    expect(next.estimate?.glossary_extraction).toBeNull();
    expect(next.glossaryProgress).toBeNull();
  });

  it.each(["stopped", "failed", "budget_exceeded"] as const)(
    "GLOSSARY_EXTRACTION_ENDED %s returns to the estimate screen with the re-fetched estimate",
    (status) => {
      const refreshed = estimateWith({ ...EXTRACTION_LINE, usd: 0.2 });
      const next = wizardReducer(
        { ...preparing, stopRequested: status === "stopped" },
        {
          type: "GLOSSARY_EXTRACTION_ENDED",
          result: { status, error: "boom", entries: null, conflicts: null },
          estimate: refreshed,
        },
      );
      expect(next.screen).toBe("estimate");
      expect(next.estimate).toEqual(refreshed);
      expect(next.preparationMessage).toBe(formatGlossaryExtractionEnd(status, "boom", 0.0421));
      expect(next.stopRequested).toBe(false);
      expect(next.glossaryProgress).toBeNull();
    },
  );

  it("GLOSSARY_EXTRACTION_ENDED keeps the shown estimate when the re-fetch failed", () => {
    const next = wizardReducer(preparing, {
      type: "GLOSSARY_EXTRACTION_ENDED",
      result: { status: "failed", error: "boom", entries: null, conflicts: null },
    });
    expect(next.estimate).toBe(preparing.estimate);
  });

  it("GLOSSARY_EXTRACTION_ENDED is a no-op guard outside the preparing screen", () => {
    const next = wizardReducer(onEstimate, {
      type: "GLOSSARY_EXTRACTION_ENDED",
      result: { status: "done", error: null, entries: 1, conflicts: 0 },
    });
    expect(next).toBe(onEstimate);
  });

  it("GLOSSARY_STOP_FAILED re-enables the stop button and says why, while preparing", () => {
    const stopping: WizardState = { ...preparing, stopRequested: true };
    const next = wizardReducer(stopping, {
      type: "GLOSSARY_STOP_FAILED",
      message: "Error: HTTP 500",
    });
    expect(next.screen).toBe("preparing");
    expect(next.stopRequested).toBe(false);
    expect(next.stopError).toBe("No se pudo detener la preparación: Error: HTTP 500");
  });

  it("GLOSSARY_STOP_FAILED is ignored outside the preparing screen", () => {
    expect(
      wizardReducer(onEstimate, { type: "GLOSSARY_STOP_FAILED", message: "late" }),
    ).toBe(onEstimate);
  });

  it("GLOSSARY_STOP_REQUESTED clears the message of an earlier failed stop", () => {
    const failedStop: WizardState = { ...preparing, stopError: "No se pudo detener" };
    expect(wizardReducer(failedStop, { type: "GLOSSARY_STOP_REQUESTED" }).stopError).toBeNull();
  });

  it("GLOSSARY_EXTRACTION_ENDED clears the message of a failed stop", () => {
    const next = wizardReducer(
      { ...preparing, stopError: "No se pudo detener" },
      {
        type: "GLOSSARY_EXTRACTION_ENDED",
        result: { status: "done", error: null, entries: 1, conflicts: 0 },
      },
    );
    expect(next.stopError).toBeNull();
  });

  it("CONNECTION_LOST moves the preparing screen to the error screen", () => {
    const next = wizardReducer(preparing, { type: "CONNECTION_LOST", message: "gone" });
    expect(next.screen).toBe("error");
    expect(next.connectionError).toBe("gone");
  });
});

describe("glossary triage", () => {
  const review: GlossaryReviewResponse = {
    entries: [
      {
        entry: { term: "Caten", translation: "Caten", locked: false, note: null, gender: null },
        gender: "masculine",
        gender_source: "seeded",
        masculine_mentions: 40,
        feminine_mentions: 2,
        proposals: [],
        flags: [],
      },
      {
        entry: { term: "Thrum", translation: "Thrum", locked: false, note: null, gender: null },
        gender: null,
        gender_source: null,
        masculine_mentions: 0,
        feminine_mentions: 0,
        proposals: [{ rendering: "Thrum", batches: 3 }],
        flags: ["lowercase_identity"],
      },
    ],
    min_gender_mentions: 20,
    min_gender_ratio: 0.7,
  };
  const onGlossary: WizardState = { ...initialWizardState, screen: "glossary" };

  it("GLOSSARY_REVIEW_LOADED keeps the review and makes its entries the editable glossary", () => {
    const next = wizardReducer(onGlossary, { type: "GLOSSARY_REVIEW_LOADED", review });
    expect(next.glossaryReview).toBe(review);
    expect(next.glossary).toEqual(review.entries.map((e) => e.entry));
    expect(next.glossaryReviewError).toBeNull();
  });

  it("GLOSSARY_REVIEW_LOADED clears a previous save error (it follows a successful save)", () => {
    const failed = { ...onGlossary, glossarySaveError: "HTTP 422" };
    const next = wizardReducer(failed, { type: "GLOSSARY_REVIEW_LOADED", review });
    expect(next.glossarySaveError).toBeNull();
  });

  it("GLOSSARY_REVIEW_FAILED records why, so the screen can fall back to the plain list", () => {
    const next = wizardReducer(onGlossary, { type: "GLOSSARY_REVIEW_FAILED", message: "HTTP 500" });
    expect(next.glossaryReviewError).toMatch(/HTTP 500/);
    expect(next.glossaryReview).toBeNull();
  });

  it("GLOSSARY_ENTRY_EDITED patches one entry: rendering, gender and lock", () => {
    const loaded = wizardReducer(onGlossary, { type: "GLOSSARY_REVIEW_LOADED", review });
    let next = wizardReducer(loaded, {
      type: "GLOSSARY_ENTRY_EDITED",
      index: 1,
      patch: { translation: "Zumbido", locked: true },
    });
    next = wizardReducer(next, {
      type: "GLOSSARY_ENTRY_EDITED",
      index: 0,
      patch: { gender: "unanchored" },
    });
    expect(next.glossary[1]).toMatchObject({ term: "Thrum", translation: "Zumbido", locked: true });
    expect(next.glossary[0]).toMatchObject({ term: "Caten", gender: "unanchored" });
    // The evidence stays what the server derived; edits never rewrite it.
    expect(next.glossaryReview).toBe(review);
  });

  it("GLOSSARY_ENTRY_EDITED is a no-op once the glossary is locked or out of range", () => {
    const loaded = wizardReducer(onGlossary, { type: "GLOSSARY_REVIEW_LOADED", review });
    const locked = { ...loaded, glossaryLocked: true };
    const edit = { type: "GLOSSARY_ENTRY_EDITED", index: 0, patch: { locked: true } } as const;
    expect(wizardReducer(locked, edit)).toBe(locked);
    expect(wizardReducer(loaded, { ...edit, index: 5 })).toBe(loaded);
  });

  it("GLOSSARY_REVIEW_LOADED is ignored once the glossary is locked", () => {
    const locked = { ...onGlossary, glossaryLocked: true };
    expect(wizardReducer(locked, { type: "GLOSSARY_REVIEW_LOADED", review })).toBe(locked);
  });

  it("GLOSSARY_SAVE_FAILED surfaces the error and leaves the glossary unlocked", () => {
    const next = wizardReducer(onGlossary, { type: "GLOSSARY_SAVE_FAILED", message: "HTTP 422: rejected" });
    expect(next.glossarySaveError).toMatch(/HTTP 422: rejected/);
    expect(next.glossaryLocked).toBe(false);
  });
});

describe("glossary save and review load seams", () => {
  const caten = { term: "Caten", translation: "Caten", locked: false, note: null, gender: null };
  const thrum = { term: "Thrum", translation: "Thrum", locked: false, note: null, gender: null };
  const review: GlossaryReviewResponse = {
    entries: [caten, thrum].map((entry) => ({
      entry,
      gender: null,
      gender_source: null,
      masculine_mentions: 0,
      feminine_mentions: 0,
      proposals: [],
      flags: [],
    })),
    min_gender_mentions: 20,
    min_gender_ratio: 0.7,
  };
  const reviewed: WizardState = {
    ...initialWizardState,
    screen: "glossary",
    glossaryReview: review,
    glossary: [caten, thrum],
  };
  const edited: WizardState = {
    ...reviewed,
    glossary: [{ ...caten, gender: "unanchored" as const }, thrum],
  };

  function seams(put: () => Promise<unknown> = async () => ({})) {
    return { put: vi.fn(put), loadReview: vi.fn(async () => {}), dispatch: vi.fn() };
  }

  it("loadGlossaryReview dispatches the review, or why it failed", async () => {
    const dispatch = vi.fn();
    await loadGlossaryReview(async () => review, dispatch);
    expect(dispatch).toHaveBeenCalledWith({ type: "GLOSSARY_REVIEW_LOADED", review });

    await loadGlossaryReview(async () => Promise.reject(new Error("HTTP 500")), dispatch);
    expect(dispatch).toHaveBeenLastCalledWith({
      type: "GLOSSARY_REVIEW_FAILED",
      message: "Error: HTTP 500",
    });
  });

  it("save sends only the changed entries, then reloads the review", async () => {
    const s = seams();
    await saveGlossary(edited, "save", s);
    expect(s.put).toHaveBeenCalledWith([{ ...caten, gender: "unanchored" }]);
    expect(s.loadReview).toHaveBeenCalledTimes(1);
    expect(s.dispatch).not.toHaveBeenCalled();
  });

  it("save does nothing when nothing changed", async () => {
    const s = seams();
    await saveGlossary(reviewed, "save", s);
    expect(s.put).not.toHaveBeenCalled();
    expect(s.loadReview).not.toHaveBeenCalled();
  });

  it("a refused save reports why and does not reload", async () => {
    const s = seams(async () => Promise.reject(new Error("HTTP 422: rejected")));
    await saveGlossary(edited, "save", s);
    expect(s.dispatch).toHaveBeenCalledWith({
      type: "GLOSSARY_SAVE_FAILED",
      message: "Error: HTTP 422: rejected",
    });
    expect(s.loadReview).not.toHaveBeenCalled();
  });

  it("lock saves the changes first, then locks", async () => {
    const s = seams();
    await saveGlossary(edited, "lock", s);
    expect(s.put).toHaveBeenCalledWith([{ ...caten, gender: "unanchored" }]);
    expect(s.dispatch).toHaveBeenCalledWith({ type: "GLOSSARY_LOCKED" });
  });

  it("lock with nothing changed locks without a request", async () => {
    const s = seams();
    await saveGlossary(reviewed, "lock", s);
    expect(s.put).not.toHaveBeenCalled();
    expect(s.dispatch).toHaveBeenCalledWith({ type: "GLOSSARY_LOCKED" });
  });

  it("lock without a review sends the whole plain list, as before", async () => {
    const s = seams();
    const plain = { ...initialWizardState, screen: "glossary" as const, glossary: [caten] };
    await saveGlossary(plain, "lock", s);
    expect(s.put).toHaveBeenCalledWith([caten]);
    expect(s.dispatch).toHaveBeenCalledWith({ type: "GLOSSARY_LOCKED" });
  });

  it("a refused lock reports why and does not lock", async () => {
    const s = seams(async () => Promise.reject(new Error("HTTP 422: rejected")));
    await saveGlossary(edited, "lock", s);
    expect(s.dispatch).toHaveBeenCalledTimes(1);
    expect(s.dispatch).toHaveBeenCalledWith({
      type: "GLOSSARY_SAVE_FAILED",
      message: "Error: HTTP 422: rejected",
    });
  });
});

describe("narrator review", () => {
  const vis = { name: "Vis", gender: "masculine" as const };
  const sira = { name: "Sira", gender: "feminine" as const };
  function chapter(key: string, first_person: boolean, narrator: NarratorFact | null = null) {
    return {
      key,
      title: key,
      opening: "",
      first_chunk: 0,
      last_chunk: 0,
      first_person,
      first_person_per_1k: first_person ? 25 : 0,
      narrator,
    };
  }
  const response: NarratorsResponse = {
    chapters: [
      chapter("ack.xhtml", false),
      chapter("ch1.xhtml", true),
      chapter("ch2.xhtml", false),
      chapter("ch3.xhtml", true, sira),
    ],
  };
  const onGlossary: WizardState = {
    ...initialWizardState,
    screen: "glossary",
    jobId: "job-1",
    estimate: estimateWith(null),
  };
  const loaded = wizardReducer(onGlossary, { type: "NARRATORS_LOADED", jobId: "job-1", response });
  const RUNNING_JOB: JobResponse = {
    id: "job-1",
    status: "RUNNING",
    total_chunks: 4,
    completed_chunks: 0,
    cost_usd: 0,
    best_effort_count: 0,
    best_effort_indices: [],
    failed_count: 0,
    failed_indices: [],
    glossary_extracted: false,
    out_path: "C:\\libros\\book_es.epub",
  };

  it("starts with nothing loaded", () => {
    expect(initialWizardState.narratorChapters).toBeNull();
    expect(initialWizardState.narratorLoadError).toBeNull();
    expect(initialWizardState.narratorSaveError).toBeNull();
  });

  it("NARRATORS_LOADED keeps the chapters and makes their narrators the draft", () => {
    expect(loaded.narratorChapters).toBe(response.chapters);
    expect(loaded.narratorDraft).toEqual({
      "ack.xhtml": null,
      "ch1.xhtml": null,
      "ch2.xhtml": null,
      "ch3.xhtml": sira,
    });
  });

  it("applies the bulk form's narrator to every first-person chapter", () => {
    let next = wizardReducer(loaded, {
      type: "NARRATOR_BULK_CHANGED",
      form: { name: "Vis", gender: "masculine" },
    });
    next = wizardReducer(next, { type: "NARRATORS_APPLIED_TO_FIRST_PERSON" });
    expect(next.narratorDraft).toEqual({
      "ack.xhtml": null,
      "ch1.xhtml": vis,
      "ch2.xhtml": null,
      "ch3.xhtml": vis,
    });
  });

  it("applying the bulk form without a gender does nothing", () => {
    const named = wizardReducer(loaded, {
      type: "NARRATOR_BULK_CHANGED",
      form: { name: "Vis", gender: null },
    });
    expect(wizardReducer(named, { type: "NARRATORS_APPLIED_TO_FIRST_PERSON" })).toBe(named);
  });

  it("changes or clears a single chapter", () => {
    let next = wizardReducer(loaded, {
      type: "NARRATOR_CHAPTER_EDITED",
      key: "ack.xhtml",
      narrator: vis,
    });
    next = wizardReducer(next, { type: "NARRATOR_CHAPTER_EDITED", key: "ch3.xhtml", narrator: null });
    expect(next.narratorDraft).toMatchObject({ "ack.xhtml": vis, "ch3.xhtml": null, "ch1.xhtml": null });
  });

  it("applies a chapter's narrator from here onward, skipping unmarked chapters", () => {
    let next = wizardReducer(loaded, { type: "NARRATOR_CHAPTER_EDITED", key: "ch1.xhtml", narrator: vis });
    next = wizardReducer(next, { type: "NARRATOR_APPLIED_ONWARD", key: "ch1.xhtml" });
    expect(next.narratorDraft).toEqual({
      "ack.xhtml": null,
      "ch1.xhtml": vis,
      "ch2.xhtml": null,
      "ch3.xhtml": vis,
    });
  });

  it("applying onward from a chapter with no narrator does nothing", () => {
    expect(wizardReducer(loaded, { type: "NARRATOR_APPLIED_ONWARD", key: "ch1.xhtml" })).toBe(loaded);
  });

  it("a failed save shows an error and keeps the edits", () => {
    const edited = wizardReducer(loaded, {
      type: "NARRATOR_CHAPTER_EDITED",
      key: "ch1.xhtml",
      narrator: vis,
    });
    const saving = wizardReducer(edited, { type: "NARRATORS_SAVE_STARTED" });
    expect(saving.narratorSaving).toBe(true);
    const failed = wizardReducer(saving, {
      type: "NARRATORS_SAVE_FAILED",
      jobId: "job-1",
      message: "HTTP 422: bad",
    });
    expect(failed.narratorSaveError).toMatch(/HTTP 422: bad/);
    expect(failed.narratorSaving).toBe(false);
    expect(failed.narratorDraft).toEqual(edited.narratorDraft);
  });

  it("the server's response after a save replaces the draft and clears the error", () => {
    const failed = { ...loaded, narratorSaveError: "x", narratorSaving: true };
    const saved: NarratorsResponse = {
      chapters: response.chapters.map((c) => ({ ...c, narrator: c.first_person ? vis : null })),
    };
    const next = wizardReducer(failed, { type: "NARRATORS_LOADED", jobId: "job-1", response: saved });
    expect(next.narratorSaveError).toBeNull();
    expect(next.narratorSaving).toBe(false);
    expect(next.narratorDraft).toMatchObject({ "ch1.xhtml": vis, "ch3.xhtml": vis });
  });

  it("a failed load records a short error and blocks neither the glossary lock nor the run", () => {
    const failed = wizardReducer(onGlossary, {
      type: "NARRATORS_LOAD_FAILED",
      jobId: "job-1",
      message: "HTTP 500",
    });
    expect(failed.narratorLoadError).toMatch(/HTTP 500/);
    const locked = wizardReducer(failed, { type: "GLOSSARY_LOCKED" });
    expect(locked.glossaryLocked).toBe(true);
    const running = wizardReducer(locked, { type: "RUN_STARTED", job: RUNNING_JOB });
    expect(running.screen).toBe("run");
  });

  it("ignores a load, a failed load or a failed save that belongs to another job", () => {
    // A response for the job the person left behind must never show its
    // chapters, or its errors, on the job now open.
    expect(wizardReducer(onGlossary, { type: "NARRATORS_LOADED", jobId: "job-0", response })).toBe(
      onGlossary,
    );
    expect(
      wizardReducer(loaded, { type: "NARRATORS_LOAD_FAILED", jobId: "job-0", message: "HTTP 500" }),
    ).toBe(loaded);
    expect(
      wizardReducer(loaded, { type: "NARRATORS_SAVE_FAILED", jobId: "job-0", message: "HTTP 409" }),
    ).toBe(loaded);
  });

  it("a new job starts with no narrator state from the previous one", () => {
    const edited = {
      ...wizardReducer(loaded, { type: "NARRATOR_CHAPTER_EDITED", key: "ch1.xhtml", narrator: vis }),
      narratorBulk: { name: "Vis", gender: "masculine" as const },
      narratorSaveError: "x",
      narratorLoadError: "y",
      narratorSaving: true,
    };
    const narratorFields = {
      narratorChapters: initialWizardState.narratorChapters,
      narratorDraft: initialWizardState.narratorDraft,
      narratorBulk: initialWizardState.narratorBulk,
      narratorLoadError: initialWizardState.narratorLoadError,
      narratorSaveError: initialWizardState.narratorSaveError,
      narratorSaving: initialWizardState.narratorSaving,
    };

    const created = wizardReducer(edited, { type: "JOB_CREATED", jobId: "job-2" });
    expect(created).toMatchObject({ jobId: "job-2", ...narratorFields });

    const resumed = wizardReducer(edited, { type: "RESUME_JOB", job: { ...RUNNING_JOB, id: "job-2" } });
    expect(resumed).toMatchObject({ jobId: "job-2", ...narratorFields });
  });

  it("resuming the same job keeps its narrator state", () => {
    const resumed = wizardReducer(loaded, { type: "RESUME_JOB", job: RUNNING_JOB });
    expect(resumed.narratorChapters).toBe(loaded.narratorChapters);
    expect(resumed.narratorDraft).toBe(loaded.narratorDraft);
  });

  it("leaves the glossary review flow untouched (and the glossary review leaves narrators alone)", () => {
    const review: GlossaryReviewResponse = {
      entries: [],
      min_gender_mentions: 20,
      min_gender_ratio: 0.7,
    };
    const edited = wizardReducer(loaded, {
      type: "NARRATOR_CHAPTER_EDITED",
      key: "ch1.xhtml",
      narrator: vis,
    });
    const withReview = wizardReducer(edited, { type: "GLOSSARY_REVIEW_LOADED", review });
    expect(withReview.narratorDraft).toBe(edited.narratorDraft);
    expect(withReview.narratorChapters).toBe(edited.narratorChapters);
    expect(withReview.glossaryReview).toBe(review);
    expect(edited.glossary).toBe(onGlossary.glossary);
    expect(edited.glossaryReview).toBe(onGlossary.glossaryReview);
    expect(edited.glossaryLocked).toBe(false);
  });
});

describe("narrator load and save seams", () => {
  const vis = { name: " Vis ", gender: "masculine" as const };
  const response: NarratorsResponse = {
    chapters: [
      {
        key: "ack.xhtml",
        title: "Agradecimientos",
        opening: "",
        first_chunk: 0,
        last_chunk: 0,
        first_person: true,
        first_person_per_1k: 12,
        narrator: null,
      },
      {
        key: "ch1.xhtml",
        title: "Uno",
        opening: "",
        first_chunk: 1,
        last_chunk: 4,
        first_person: true,
        first_person_per_1k: 30,
        narrator: null,
      },
    ],
  };
  const edited: WizardState = {
    ...initialWizardState,
    screen: "glossary",
    jobId: "job-1",
    narratorChapters: response.chapters,
    narratorDraft: { "ack.xhtml": null, "ch1.xhtml": vis },
  };

  it("loadNarrators dispatches the chapters, or why they could not be loaded", async () => {
    const dispatch = vi.fn();
    await loadNarrators("job-1", async () => response, dispatch);
    expect(dispatch).toHaveBeenCalledWith({ type: "NARRATORS_LOADED", jobId: "job-1", response });

    await loadNarrators("job-1", async () => Promise.reject(new Error("HTTP 500")), dispatch);
    expect(dispatch).toHaveBeenLastCalledWith({
      type: "NARRATORS_LOAD_FAILED",
      jobId: "job-1",
      message: "Error: HTTP 500",
    });
  });

  it("save PUTs only the chapters with a narrator and reflects the server's response", async () => {
    const saved = { chapters: [] };
    const put = vi.fn(async () => saved);
    const dispatch = vi.fn();
    await saveNarrators(edited, { put, dispatch });
    expect(put).toHaveBeenCalledWith([{ key: "ch1.xhtml", name: "Vis", gender: "masculine" }]);
    expect(dispatch.mock.calls).toEqual([
      [{ type: "NARRATORS_SAVE_STARTED" }],
      [{ type: "NARRATORS_LOADED", jobId: "job-1", response: saved }],
    ]);
  });

  it("a refused save reports why", async () => {
    const put = vi.fn(async () => Promise.reject(new Error("HTTP 422: unknown key")));
    const dispatch = vi.fn();
    await saveNarrators(edited, { put, dispatch });
    expect(dispatch).toHaveBeenLastCalledWith({
      type: "NARRATORS_SAVE_FAILED",
      jobId: "job-1",
      message: "Error: HTTP 422: unknown key",
    });
  });

  it("does nothing before the chapters are loaded", async () => {
    const put = vi.fn(async () => response);
    const dispatch = vi.fn();
    await saveNarrators({ ...initialWizardState }, { put, dispatch });
    expect(put).not.toHaveBeenCalled();
    expect(dispatch).not.toHaveBeenCalled();
  });
});
