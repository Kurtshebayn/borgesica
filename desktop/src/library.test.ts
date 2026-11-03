import { describe, expect, it } from "vitest";
import type { JobListItem } from "./apiTypes";
import { fichaStatus, librarySummary, nextSignature, signature, toFichas } from "./library";

function job(overrides: Partial<JobListItem> = {}): JobListItem {
  return {
    id: "j1",
    status: "DONE",
    title: "Meridiano 47",
    source_type: "EPUB",
    target_lang: "es-neutral",
    model: "deepseek-v4-flash",
    total_chunks: 26,
    completed_chunks: 26,
    cost_usd: 0.017,
    failed_count: 0,
    best_effort_count: 0,
    ...overrides,
  };
}

describe("signature", () => {
  it("pads to four digits", () => {
    expect(signature(7)).toBe("BAB-0007");
    expect(signature(123)).toBe("BAB-0123");
  });
});

describe("fichaStatus", () => {
  it("is done for a clean finished job", () => {
    expect(fichaStatus(job())).toBe("done");
  });

  it("is problems for a finished job with failed or best-effort chunks", () => {
    expect(fichaStatus(job({ failed_count: 3 }))).toBe("problems");
    expect(fichaStatus(job({ best_effort_count: 1 }))).toBe("problems");
  });

  it("is progress for an interrupted job, whatever its stored status", () => {
    for (const status of ["RUNNING", "PAUSED", "FAILED", "CANCELLED"] as const) {
      expect(fichaStatus(job({ status }))).toBe("progress");
    }
  });

  it("is draft for a job that was never estimated", () => {
    expect(fichaStatus(job({ status: "CREATED" }))).toBe("draft");
    expect(fichaStatus(job({ status: "ESTIMATING" }))).toBe("draft");
  });
});

describe("toFichas", () => {
  const jobs = [
    job({ id: "new", status: "RUNNING", completed_chunks: 17 }),
    job({ id: "mid", failed_count: 3 }),
    job({ id: "old" }),
    job({ id: "draft", status: "CREATED", completed_chunks: 0, cost_usd: 0 }),
  ];
  const fichas = toFichas(jobs);

  it("numbers by age: the oldest job is 1 and the newest is last", () => {
    expect(fichas.map((f) => f.signature)).toEqual([
      "BAB-0004",
      "BAB-0003",
      "BAB-0002",
      "BAB-0001",
    ]);
  });

  it("reads a finished book and resumes an interrupted one", () => {
    expect(fichas.map((f) => f.action)).toEqual(["resume", "compare", "compare", "resume"]);
  });

  it("never lets a click start a job that was only created", () => {
    expect(fichas[3].status).toBe("draft");
    expect(fichas[3].action).toBe("resume");
  });

  it("labels and formats the footer", () => {
    expect(fichas[0].label).toBe("EN CURSO");
    expect(fichas[1].label).toBe("3 CON PROBLEMAS");
    expect(fichas[0].footer).toBe("17/26 · $0.017");
    expect(fichas[0].meta).toBe("EPUB · es-neutral");
  });
});

describe("library summary", () => {
  it("names the next signature after the existing jobs", () => {
    expect(nextSignature([job(), job()])).toBe("BAB-0003");
    expect(nextSignature([])).toBe("BAB-0001");
  });

  it("counts finished fichas", () => {
    const fichas = toFichas([job({ status: "RUNNING" }), job(), job({ failed_count: 1 })]);
    expect(librarySummary(fichas)).toBe("3 fichas · 2 terminadas");
    expect(librarySummary(toFichas([job()]))).toBe("1 ficha · 1 terminada");
  });
});
