import { afterEach, describe, expect, it, vi } from "vitest";
import {
  extractGlossary,
  getGlossaryReview,
  putGlossary,
  stopGlossaryExtraction,
} from "./apiClient";

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

describe("getGlossaryReview", () => {
  it("GETs the job's review route with the session token and returns the review", async () => {
    const review = { entries: [], min_gender_mentions: 20, min_gender_ratio: 0.7 };
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(review), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    expect(await getGlossaryReview(BASE, "secret", "job-1")).toEqual(review);
    expect(fetchMock).toHaveBeenCalledWith(`${BASE}/jobs/job-1/glossary/review`, {
      headers: { "X-Borgesica-Token": "secret" },
    });
  });
});

describe("putGlossary", () => {
  it("rejects with the server's reason when an edit is refused (422)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response('{"detail":"entry rejected: Caten"}', { status: 422 })),
    );

    await expect(
      putGlossary(BASE, "secret", "job-1", [{ term: "Caten", translation: "", locked: false }]),
    ).rejects.toThrow(/HTTP 422: .*entry rejected/);
  });
});
