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
  CreateJobRequest,
  EstimateResponse,
  GlossaryEntry,
  GlossaryReviewResponse,
  JobResponse,
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

export function runJob(
  baseUrl: string,
  token: string,
  jobId: string,
  outPath: string,
): Promise<JobResponse> {
  return fetch(`${baseUrl}/jobs/${jobId}/run`, {
    method: "POST",
    headers: authHeaders(token, true),
    body: JSON.stringify({ out_path: outPath }),
  }).then(asJson<JobResponse>);
}

export function resumeJob(
  baseUrl: string,
  token: string,
  jobId: string,
  outPath: string,
): Promise<JobResponse> {
  return fetch(`${baseUrl}/jobs/${jobId}/resume`, {
    method: "POST",
    headers: authHeaders(token, true),
    body: JSON.stringify({ out_path: outPath }),
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
