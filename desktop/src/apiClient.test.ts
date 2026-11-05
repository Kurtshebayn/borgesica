import { afterEach, describe, expect, it, vi } from "vitest";
import {
  extractGlossary,
  getChunks,
  getGlossaryReview,
  getNarrators,
  getOllamaModels,
  deleteJob,
  updateJobModel,
  listJobs,
  putGlossary,
  putNarrators,
  resumeJob,
  runJob,
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
  out_path: "C:\\libros\\book_es.epub",
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("runJob / resumeJob", () => {
  it.each([
    ["run", runJob],
    ["resume", resumeJob],
  ] as const)("POSTs %s with no out_path, so the engine writes beside the source", async (route, start) => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(JOB), { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);

    const job = await start(BASE, "secret", "job-1");

    expect(job).toEqual(JOB);
    expect(fetchMock).toHaveBeenCalledWith(`${BASE}/jobs/job-1/${route}`, {
      method: "POST",
      headers: { "X-Borgesica-Token": "secret", "Content-Type": "application/json" },
      body: "{}",
    });
  });
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

describe("getChunks", () => {
  const body = { total: 0, offset: 0, limit: 50, chunks: [] };

  it("GETs the chunks route with no query by default", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    expect(await getChunks(BASE, "secret", "job-1")).toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(`${BASE}/jobs/job-1/chunks`, {
      headers: { "X-Borgesica-Token": "secret" },
    });
  });

  it("sends offset, limit and only_problems when given", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await getChunks(BASE, "secret", "job-1", { offset: 100, limit: 25, onlyProblems: true });

    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/jobs/job-1/chunks?offset=100&limit=25&only_problems=true`,
      { headers: { "X-Borgesica-Token": "secret" } },
    );
  });

  it("rejects with the server's reason for an unknown job", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response('{"detail":"Job not found: nope"}', { status: 404 })),
    );

    await expect(getChunks(BASE, "secret", "nope")).rejects.toThrow(/HTTP 404/);
  });
});

describe("listJobs", () => {
  it("GETs /jobs with the token and returns the body", async () => {
    const body = {
      jobs: [
        {
          id: "job-1",
          status: "DONE",
          title: "book",
          source_type: "epub",
          target_lang: "es-neutral",
          model: "m",
          total_chunks: 3,
          completed_chunks: 3,
          cost_usd: 0.1,
          failed_count: 0,
          best_effort_count: 1,
        },
      ],
    };
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    expect(await listJobs(BASE, "secret")).toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(`${BASE}/jobs`, {
      headers: { "X-Borgesica-Token": "secret" },
    });
  });

  it("rejects with the status on a refused token", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response('{"detail":"Missing or invalid session token"}', { status: 401 })),
    );

    await expect(listJobs(BASE, "bad")).rejects.toThrow(/HTTP 401/);
  });
});

describe("getNarrators", () => {
  it("GETs the job's narrators route with the session token and returns the chapters", async () => {
    const body = {
      chapters: [
        {
          key: "ch1.xhtml",
          title: "One",
          opening: "I woke",
          first_chunk: 0,
          last_chunk: 3,
          first_person: true,
          first_person_per_1k: 31.5,
          narrator: { name: "Vis", gender: "masculine" },
        },
      ],
    };
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    expect(await getNarrators(BASE, "secret", "job-1")).toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(`${BASE}/jobs/job-1/narrators`, {
      headers: { "X-Borgesica-Token": "secret" },
    });
  });
});

describe("putNarrators", () => {
  it("PUTs the full list as {narrators} with the session token and returns the GET shape", async () => {
    const narrators = [{ key: "ch1.xhtml", name: null, gender: "feminine" as const }];
    const body = { chapters: [] };
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    expect(await putNarrators(BASE, "secret", "job-1", narrators)).toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(`${BASE}/jobs/job-1/narrators`, {
      method: "PUT",
      headers: { "Content-Type": "application/json", "X-Borgesica-Token": "secret" },
      body: JSON.stringify({ narrators }),
    });
  });

  it("rejects with the server's reason on a 422 (unknown key, bad gender, duplicate)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response('{"detail":"unknown chapter key"}', { status: 422 })),
    );

    await expect(putNarrators(BASE, "secret", "job-1", [])).rejects.toThrow(
      /HTTP 422: .*unknown chapter key/,
    );
  });
});

describe("deleteJob", () => {
  it("DELETEs /jobs/{id} with the token and returns the body", async () => {
    const body = { deleted: true, file_deleted: false };
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => body });
    vi.stubGlobal("fetch", fetchMock);

    expect(await deleteJob(BASE, "secret", "job-1")).toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(`${BASE}/jobs/job-1`, {
      method: "DELETE",
      headers: { "X-Borgesica-Token": "secret" },
    });
  });

  it("throws with the status and body when the engine refuses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 409, text: async () => "running" }),
    );

    await expect(deleteJob(BASE, "secret", "job-1")).rejects.toThrow("HTTP 409: running");
  });
});

describe("updateJobModel", () => {
  it("PATCHes /jobs/{id} with the model as JSON and the token", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => JOB });
    vi.stubGlobal("fetch", fetchMock);

    expect(await updateJobModel(BASE, "secret", "job-1", "deepseek-v4-pro")).toEqual(JOB);
    expect(fetchMock).toHaveBeenCalledWith(`${BASE}/jobs/job-1`, {
      method: "PATCH",
      headers: { "X-Borgesica-Token": "secret", "Content-Type": "application/json" },
      body: JSON.stringify({ model: "deepseek-v4-pro" }),
    });
  });

  it("throws with the status and body when the engine refuses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 409, text: async () => "started" }),
    );

    await expect(updateJobModel(BASE, "secret", "job-1", "m")).rejects.toThrow("HTTP 409: started");
  });
});

describe("getOllamaModels", () => {
  it("GETs /ollama/models with the session token", async () => {
    const body = { models: ["qwen3.6-27b-tr:latest"], error: null };
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    expect(await getOllamaModels(BASE, "secret")).toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(`${BASE}/ollama/models`, {
      headers: { "X-Borgesica-Token": "secret" },
    });
  });

  it("rejects on a refused request", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("no", { status: 401 })));
    await expect(getOllamaModels(BASE, "bad")).rejects.toThrow("HTTP 401");
  });
});
