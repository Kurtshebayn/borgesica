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
  formatProgress,
  glossaryResultFromEvent,
  initialWizardState,
  isPersistentConnectionFailure,
  isRunningStatus,
  MAX_CONSECUTIVE_CONNECTION_FAILURES,
  parseGlossarySseData,
  parseSseData,
  providerRequiresKey,
  PROVIDERS,
  recoveryRequiresRespawn,
  runRecoveryAction,
  sseEventToAction,
  validateFilePath,
  wizardReducer,
  type WizardState,
} from "./wizard";
import type {
  EstimateResponse,
  GlossaryExtractionEstimate,
  JobResponse,
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
      formatProgress({ chunkIndex: 4, totalChunks: 10, costUsd: 1.5 }),
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
  it("maps a progress event to PROGRESS_RECEIVED", () => {
    expect(
      sseEventToAction({
        type: "progress",
        job_id: "abc",
        chunk_index: 2,
        total_chunks: 5,
        cost_usd: 0.2,
        status: "RUNNING",
      }),
    ).toEqual({
      type: "PROGRESS_RECEIVED",
      progress: { chunkIndex: 2, totalChunks: 5, costUsd: 0.2 },
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
  it("shows the cost range and the number of calls, apart from the translation", () => {
    expect(formatGlossaryExtractionLine(EXTRACTION_LINE)).toBe(
      "Preparación del glosario: $0.12 – $0.57, 57 llamadas",
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
