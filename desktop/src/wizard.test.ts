import { describe, expect, it, vi } from "vitest";
import {
  defaultModelForProvider,
  formatBestEffortSummary,
  formatProgress,
  initialWizardState,
  isPersistentConnectionFailure,
  isRunningStatus,
  MAX_CONSECUTIVE_CONNECTION_FAILURES,
  parseSseData,
  PROVIDERS,
  recoveryRequiresRespawn,
  runRecoveryAction,
  sseEventToAction,
  validateFilePath,
  wizardReducer,
  type WizardState,
} from "./wizard";

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
    expect(formatBestEffortSummary(1)).toBe("1 chunk remained best-effort");
  });

  it("reports multiple chunks in plural form", () => {
    expect(formatBestEffortSummary(3)).toBe("3 chunks remained best-effort");
  });
});

describe("provider selection", () => {
  it("offers the three engine-supported providers", () => {
    expect(PROVIDERS).toEqual(["anthropic", "deepseek", "ollama"]);
  });

  it("defaults Anthropic to a current (non-retired) model id", () => {
    expect(defaultModelForProvider("anthropic")).toBe("claude-sonnet-5");
  });

  it("defaults DeepSeek to a DeepSeek model id, not an Anthropic one", () => {
    const model = defaultModelForProvider("deepseek");
    expect(model).toMatch(/deepseek/);
    expect(model).not.toMatch(/claude/);
  });

  it("leaves the model blank for Ollama (models are user-local, not fixed)", () => {
    expect(defaultModelForProvider("ollama")).toBe("");
  });
});

describe("formatProgress", () => {
  it("renders chunk counter and cost", () => {
    expect(
      formatProgress({ chunkIndex: 4, totalChunks: 10, costUsd: 1.5 }),
    ).toBe("Chunk 4/10 — $1.5000");
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
