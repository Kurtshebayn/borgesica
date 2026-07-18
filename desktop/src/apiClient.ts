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
