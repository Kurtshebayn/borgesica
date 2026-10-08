import { afterEach, describe, expect, it, vi } from "vitest";
import { extractGlossary, stopGlossaryExtraction } from "./apiClient";

const BASE = "http://127.0.0.1:8765";

const JOB = {
  id: "job-1",
  status: "CREATED",
  total_chunks: 3,
  completed_chunks: 0,
  cost_usd: 0,
  best_effort_count: 0,
  best_effort_indices: [],
  failed_count: 0,
  failed_indices: [],
  glossary_extracted: false,
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("extractGlossary", () => {
  it("POSTs the job's extract route with the session token and returns the job", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(JOB), { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);

    const job = await extractGlossary(BASE, "secret", "job-1");

    expect(job).toEqual(JOB);
    expect(fetchMock).toHaveBeenCalledWith(`${BASE}/jobs/job-1/glossary/extract`, {
      method: "POST",
      headers: { "X-Borgesica-Token": "secret" },
    });
  });

  it("rejects with the status and body on a 409 (busy runner, or nothing to extract)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response('{"detail":"already extracted"}', { status: 409 })),
    );

    await expect(extractGlossary(BASE, "secret", "job-1")).rejects.toThrow(
      /HTTP 409: .*already extracted/,
    );
  });
});

describe("stopGlossaryExtraction", () => {
  it("POSTs the job's stop route (never /cancel) with the session token", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(JOB), { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);

    const job = await stopGlossaryExtraction(BASE, "secret", "job-1");

    expect(job).toEqual(JOB);
    expect(fetchMock).toHaveBeenCalledWith(`${BASE}/jobs/job-1/glossary/extract/stop`, {
      method: "POST",
      headers: { "X-Borgesica-Token": "secret" },
    });
  });

  it("rejects on a 409 (no active extraction; the job was left untouched)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response('{"detail":"no active glossary extraction"}', { status: 409 })),
    );

    await expect(stopGlossaryExtraction(BASE, "secret", "job-1")).rejects.toThrow(/HTTP 409/);
  });
});
