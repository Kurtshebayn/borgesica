import { afterEach, describe, expect, it, vi } from "vitest";
import { subscribeToGlossaryExtraction, subscribeToJobEvents } from "./sseClient";
import { MAX_CONSECUTIVE_CONNECTION_FAILURES, type WizardAction } from "./wizard";

const BASE = "http://127.0.0.1:8765";
const EVENTS_URL = `${BASE}/jobs/job-1/events`;

const ESTIMATE = {
  input_tokens: 1,
  output_tokens: 1,
  usd: 1,
  usd_low: 0.5,
  usd_high: 1.5,
  model: "m",
  cached: false,
  within_budget: true,
  glossary_extraction: {
    batches: 2,
    input_tokens: 10,
    output_tokens: 5,
    output_tokens_high: 8,
    usd: 0.1,
    usd_high: 0.2,
  },
};

const ENTRIES = [{ term: "Vis", translation: "Vis", locked: false, note: null }];

const JOB = {
  id: "job-1",
  status: "DONE",
  total_chunks: 2,
  completed_chunks: 2,
  cost_usd: 0.3,
  best_effort_count: 0,
  best_effort_indices: [],
  failed_count: 0,
  failed_indices: [],
  glossary_extracted: true,
};

function sse(...events: object[]): string {
  return events.map((event) => `: heartbeat\n\ndata: ${JSON.stringify(event)}\n\n`).join("");
}

const progress = (batch: number) => ({
  type: "glossary_progress",
  job_id: "job-1",
  batch,
  total_batches: 2,
  cost_usd: 0.01 * batch,
});

const terminal = (status: string, extra: object = {}) => ({
  type: "glossary_terminal",
  status,
  error: null,
  entries: null,
  conflicts: null,
  cost_usd: null,
  ...extra,
});

/** Fake fetch: each events request takes the next scripted stream body (or
 * a rejection, for an unreachable sidecar); other routes answer JSON. */
function stubServer(streams: (string | Error)[], routes: Record<string, Response> = {}) {
  const queue = [...streams];
  const fetchMock = vi.fn(async (url: string) => {
    if (url === EVENTS_URL) {
      const next = queue.shift();
      if (next === undefined || next instanceof Error) throw next ?? new Error("no stream");
      return new Response(next, { status: 200 });
    }
    const route = routes[url];
    if (!route) return new Response("not found", { status: 404 });
    return route.clone();
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const json = (body: object, status = 200) => new Response(JSON.stringify(body), { status });

function ended(dispatch: ReturnType<typeof vi.fn>): WizardAction[] {
  return dispatch.mock.calls
    .map(([action]) => action as WizardAction)
    .filter((action) => action.type === "GLOSSARY_EXTRACTION_ENDED");
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("subscribeToGlossaryExtraction", () => {
  it("dispatches batch progress, then the done end with the fetched glossary", async () => {
    stubServer([sse(progress(1), progress(2), terminal("done", { entries: 1, conflicts: 0 }))], {
      [`${BASE}/jobs/job-1/glossary`]: json({ entries: ENTRIES }),
    });
    const dispatch = vi.fn();

    subscribeToGlossaryExtraction(BASE, "secret", "job-1", dispatch);

    await vi.waitFor(() => expect(ended(dispatch)).toHaveLength(1));
    expect(dispatch.mock.calls.map(([action]) => action)).toEqual([
      {
        type: "GLOSSARY_PROGRESS_RECEIVED",
        progress: { batch: 1, totalBatches: 2, costUsd: 0.01 },
      },
      {
        type: "GLOSSARY_PROGRESS_RECEIVED",
        progress: { batch: 2, totalBatches: 2, costUsd: 0.02 },
      },
      {
        type: "GLOSSARY_EXTRACTION_ENDED",
        result: { status: "done", error: null, entries: 1, conflicts: 0 },
        glossary: ENTRIES,
      },
    ]);
  });

  it.each(["stopped", "failed", "budget_exceeded"])(
    "re-fetches the estimate after a %s end",
    async (status) => {
      stubServer([sse(progress(1), terminal(status, { error: "boom" }))], {
        [`${BASE}/jobs/job-1/estimate`]: json(ESTIMATE),
      });
      const dispatch = vi.fn();

      subscribeToGlossaryExtraction(BASE, "secret", "job-1", dispatch);

      await vi.waitFor(() => expect(ended(dispatch)).toHaveLength(1));
      expect(ended(dispatch)[0]).toEqual({
        type: "GLOSSARY_EXTRACTION_ENDED",
        result: { status, error: "boom", entries: null, conflicts: null },
        estimate: ESTIMATE,
      });
    },
  );

  it("still ends the preparation when the follow-up fetch fails", async () => {
    stubServer([sse(terminal("done", { entries: 1, conflicts: 0 }))], {
      [`${BASE}/jobs/job-1/glossary`]: json({ detail: "boom" }, 500),
    });
    const dispatch = vi.fn();

    subscribeToGlossaryExtraction(BASE, "secret", "job-1", dispatch);

    await vi.waitFor(() => expect(ended(dispatch)).toHaveLength(1));
    expect(ended(dispatch)[0]).toEqual({
      type: "GLOSSARY_EXTRACTION_ENDED",
      result: { status: "done", error: null, entries: 1, conflicts: 0 },
    });
  });

  it("treats a plain terminal (no extraction known) as a failed preparation", async () => {
    stubServer([sse({ type: "terminal", status: "CREATED", error: null })], {
      [`${BASE}/jobs/job-1/estimate`]: json(ESTIMATE),
    });
    const dispatch = vi.fn();

    subscribeToGlossaryExtraction(BASE, "secret", "job-1", dispatch);

    await vi.waitFor(() => expect(ended(dispatch)).toHaveLength(1));
    const [action] = ended(dispatch);
    expect(action.type === "GLOSSARY_EXTRACTION_ENDED" && action.result.status).toBe("failed");
  });

  it("reconnects after a dropped stream and takes the replayed end", async () => {
    const fetchMock = stubServer(
      [sse(progress(1)), new Error("connection reset"), sse(terminal("stopped"))],
      { [`${BASE}/jobs/job-1/estimate`]: json(ESTIMATE) },
    );
    const dispatch = vi.fn();

    subscribeToGlossaryExtraction(BASE, "secret", "job-1", dispatch, { reconnectDelayMs: 0 });

    await vi.waitFor(() => expect(ended(dispatch)).toHaveLength(1));
    const eventsCalls = fetchMock.mock.calls.filter(([url]) => url === EVENTS_URL);
    expect(eventsCalls).toHaveLength(3);
    expect(dispatch).not.toHaveBeenCalledWith(expect.objectContaining({ type: "CONNECTION_LOST" }));
  });

  it("reports a lost connection once reconnecting keeps failing", async () => {
    const failures = Array.from(
      { length: MAX_CONSECUTIVE_CONNECTION_FAILURES + 2 },
      () => new Error("connection refused"),
    );
    const fetchMock = stubServer(failures);
    const dispatch = vi.fn();

    subscribeToGlossaryExtraction(BASE, "secret", "job-1", dispatch, { reconnectDelayMs: 0 });

    await vi.waitFor(() =>
      expect(dispatch).toHaveBeenCalledWith(expect.objectContaining({ type: "CONNECTION_LOST" })),
    );
    expect(fetchMock).toHaveBeenCalledTimes(MAX_CONSECUTIVE_CONNECTION_FAILURES);
    expect(ended(dispatch)).toEqual([]);
  });

  it("dispatches nothing once closed", async () => {
    stubServer([sse(terminal("done", { entries: 1, conflicts: 0 }))], {
      [`${BASE}/jobs/job-1/glossary`]: json({ entries: ENTRIES }),
    });
    const dispatch = vi.fn();

    subscribeToGlossaryExtraction(BASE, "secret", "job-1", dispatch).close();

    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(dispatch).not.toHaveBeenCalled();
  });
});

describe("subscribeToJobEvents", () => {
  it("dispatches run progress, then the terminal with the refreshed job", async () => {
    stubServer(
      [
        sse(
          {
            type: "progress",
            job_id: "job-1",
            chunk_index: 0,
            position: 1,
            total_chunks: 2,
            cost_usd: 0.1,
            status: "RUNNING",
          },
          { type: "terminal", status: "DONE", error: null },
        ),
      ],
      { [`${BASE}/jobs/job-1`]: json(JOB) },
    );
    const dispatch = vi.fn();

    subscribeToJobEvents(BASE, "secret", "job-1", dispatch);

    await vi.waitFor(() =>
      expect(dispatch).toHaveBeenCalledWith(expect.objectContaining({ type: "RUN_TERMINAL" })),
    );
    expect(dispatch.mock.calls.map(([action]) => action)).toEqual([
      {
        type: "PROGRESS_RECEIVED",
        progress: { chunkIndex: 0, totalChunks: 2, costUsd: 0.1 },
      },
      { type: "RUN_TERMINAL", status: "DONE", error: null, job: JOB },
    ]);
  });
});
