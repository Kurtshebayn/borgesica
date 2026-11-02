/**
 * Thin `fetch` glue over the borgesica serve API (`borgesica/serve/app.py`,
 * T5/T6). Kept separate from `wizard.ts` (pure logic, unit tested) so the
 * untestable network boundary is not disguised as tested logic — mirrors
 * `sidecarClient.ts`'s split from `sidecarStatus.ts` in T7a.
 *
 * RISK-001/002/003: every route requires the per-session auth token
 * (`X-Borgesica-Token` header) — every exported function here takes it as
 * an explicit parameter and attaches it, mirroring how baseUrl is already
 * threaded through explicitly rather than hidden in module state.
 */
import type {
  ChunksResponse,
  CreateJobRequest,
  EstimateResponse,
  GlossaryEntry,
  GlossaryReviewResponse,
  JobListResponse,
  JobResponse,
  NarratorAssignment,
  NarratorsResponse,
} from "./apiTypes";

const TOKEN_HEADER = "X-Borgesica-Token";

async function asJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new Error(`HTTP ${response.status}${body ? `: ${body}` : ""}`);
  }
  return (await response.json()) as T;
}

function authHeaders(token: string, withJsonBody = false): HeadersInit {
  return withJsonBody
    ? { "Content-Type": "application/json", [TOKEN_HEADER]: token }
    : { [TOKEN_HEADER]: token };
}

export function createJob(
  baseUrl: string,
  token: string,
  payload: CreateJobRequest,
): Promise<JobResponse> {
  return fetch(`${baseUrl}/jobs`, {
    method: "POST",
    headers: authHeaders(token, true),
    body: JSON.stringify(payload),
  }).then(asJson<JobResponse>);
}

/** Every stored job, newest first, with its failed/best-effort counts. */
export function listJobs(baseUrl: string, token: string): Promise<JobListResponse> {
  return fetch(`${baseUrl}/jobs`, { headers: authHeaders(token) }).then(
    asJson<JobListResponse>,
  );
}

export function getStatus(
  baseUrl: string,
  token: string,
  jobId: string,
): Promise<JobResponse> {
  return fetch(`${baseUrl}/jobs/${jobId}`, { headers: authHeaders(token) }).then(
    asJson<JobResponse>,
  );
}

export function getEstimate(
  baseUrl: string,
  token: string,
  jobId: string,
): Promise<EstimateResponse> {
  return fetch(`${baseUrl}/jobs/${jobId}/estimate`, {
    headers: authHeaders(token),
  }).then(asJson<EstimateResponse>);
}

export function getGlossary(
  baseUrl: string,
  token: string,
  jobId: string,
): Promise<{ entries: GlossaryEntry[] }> {
  return fetch(`${baseUrl}/jobs/${jobId}/glossary`, {
    headers: authHeaders(token),
  }).then(asJson<{ entries: GlossaryEntry[] }>);
}

/** Every glossary entry with its gender evidence, extraction proposals and
 * risk flags — derived by the server on each call, free, never stored. */
export function getGlossaryReview(
  baseUrl: string,
  token: string,
  jobId: string,
): Promise<GlossaryReviewResponse> {
  return fetch(`${baseUrl}/jobs/${jobId}/glossary/review`, {
    headers: authHeaders(token),
  }).then(asJson<GlossaryReviewResponse>);
}

/** Upserts *entries* (the rest of the glossary is kept). A 422 means the
 * glossary rules rejected one of them and nothing was saved. */
export function putGlossary(
  baseUrl: string,
  token: string,
  jobId: string,
  entries: GlossaryEntry[],
): Promise<{ entries: GlossaryEntry[] }> {
  return fetch(`${baseUrl}/jobs/${jobId}/glossary`, {
    method: "PUT",
    headers: authHeaders(token, true),
    body: JSON.stringify({ entries }),
  }).then(asJson<{ entries: GlossaryEntry[] }>);
}

/** Every chapter with its first-person mark and stored narrator. Empty
 * `chapters` for jobs without chapters (subtitles). */
export function getNarrators(
  baseUrl: string,
  token: string,
  jobId: string,
): Promise<NarratorsResponse> {
  return fetch(`${baseUrl}/jobs/${jobId}/narrators`, {
    headers: authHeaders(token),
  }).then(asJson<NarratorsResponse>);
}

/** Replaces every stored narrator with *narrators* (a full overwrite: a
 * chapter left out has none afterwards) and returns the GET shape. A 422
 * (unknown key, bad gender, duplicate key) saves nothing. */
export function putNarrators(
  baseUrl: string,
  token: string,
  jobId: string,
  narrators: NarratorAssignment[],
): Promise<NarratorsResponse> {
  return fetch(`${baseUrl}/jobs/${jobId}/narrators`, {
    method: "PUT",
    headers: authHeaders(token, true),
    body: JSON.stringify({ narrators }),
  }).then(asJson<NarratorsResponse>);
}

/** One page of the job's chunks (source beside translation), in reading
 * order. The server defaults to offset 0, limit 50 (max 200). `onlyProblems`
 * keeps FAILED and best-effort chunks; `total` counts the filtered set. */
export function getChunks(
  baseUrl: string,
  token: string,
  jobId: string,
  options: { offset?: number; limit?: number; onlyProblems?: boolean } = {},
): Promise<ChunksResponse> {
  const query = new URLSearchParams();
  if (options.offset !== undefined) query.set("offset", String(options.offset));
  if (options.limit !== undefined) query.set("limit", String(options.limit));
  if (options.onlyProblems) query.set("only_problems", "true");
  const qs = query.toString();
  return fetch(`${baseUrl}/jobs/${jobId}/chunks${qs ? `?${qs}` : ""}`, {
    headers: authHeaders(token),
  }).then(asJson<ChunksResponse>);
}

/** Starts the translation. No out_path is sent: the engine writes beside the
 * source (the job's out_path). */
export function runJob(baseUrl: string, token: string, jobId: string): Promise<JobResponse> {
  return fetch(`${baseUrl}/jobs/${jobId}/run`, {
    method: "POST",
    headers: authHeaders(token, true),
    body: "{}",
  }).then(asJson<JobResponse>);
}

export function resumeJob(baseUrl: string, token: string, jobId: string): Promise<JobResponse> {
  return fetch(`${baseUrl}/jobs/${jobId}/resume`, {
    method: "POST",
    headers: authHeaders(token, true),
    body: "{}",
  }).then(asJson<JobResponse>);
}

/** Starts the explicit, PAID pre-translation glossary pass (202; progress
 * arrives on the job's event stream). A 409 — runner busy, job not CREATED,
 * or glossary already extracted — rejects like every other call here. */
export function extractGlossary(
  baseUrl: string,
  token: string,
  jobId: string,
): Promise<JobResponse> {
  return fetch(`${baseUrl}/jobs/${jobId}/glossary/extract`, {
    method: "POST",
    headers: authHeaders(token),
  }).then(asJson<JobResponse>);
}

/** Stops the job's glossary preparation before its next batch; the job stays
 * CREATED and the event stream reports the end. Never cancels the job: with
 * no active preparation (e.g. it just ended) the server answers 409 and
 * touches nothing, which rejects here. */
export function stopGlossaryExtraction(
  baseUrl: string,
  token: string,
  jobId: string,
): Promise<JobResponse> {
  return fetch(`${baseUrl}/jobs/${jobId}/glossary/extract/stop`, {
    method: "POST",
    headers: authHeaders(token),
  }).then(asJson<JobResponse>);
}

/** Cancels the job (a run stops between chunks). Not for a glossary
 * preparation: that is `stopGlossaryExtraction`, which keeps the job. */
export function cancelJob(
  baseUrl: string,
  token: string,
  jobId: string,
): Promise<JobResponse> {
  return fetch(`${baseUrl}/jobs/${jobId}/cancel`, {
    method: "POST",
    headers: authHeaders(token),
  }).then(asJson<JobResponse>);
}
