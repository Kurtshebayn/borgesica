/**
 * Thin `fetch` glue over the borgesica serve API (`borgesica/serve/app.py`,
 * T5/T6). Kept separate from `wizard.ts` (pure logic, unit tested) so the
 * untestable network boundary is not disguised as tested logic — mirrors
 * `sidecarClient.ts`'s split from `sidecarStatus.ts` in T7a.
 */
import type {
  CreateJobRequest,
  EstimateResponse,
  GlossaryEntry,
  JobResponse,
} from "./apiTypes";

async function asJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new Error(`HTTP ${response.status}${body ? `: ${body}` : ""}`);
  }
  return (await response.json()) as T;
}

export function createJob(
  baseUrl: string,
  payload: CreateJobRequest,
): Promise<JobResponse> {
  return fetch(`${baseUrl}/jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }).then(asJson<JobResponse>);
}

export function getStatus(baseUrl: string, jobId: string): Promise<JobResponse> {
  return fetch(`${baseUrl}/jobs/${jobId}`).then(asJson<JobResponse>);
}

export function getEstimate(
  baseUrl: string,
  jobId: string,
): Promise<EstimateResponse> {
  return fetch(`${baseUrl}/jobs/${jobId}/estimate`).then(asJson<EstimateResponse>);
}

export function getGlossary(
  baseUrl: string,
  jobId: string,
): Promise<{ entries: GlossaryEntry[] }> {
  return fetch(`${baseUrl}/jobs/${jobId}/glossary`).then(
    asJson<{ entries: GlossaryEntry[] }>,
  );
}

export function putGlossary(
  baseUrl: string,
  jobId: string,
  entries: GlossaryEntry[],
): Promise<{ entries: GlossaryEntry[] }> {
  return fetch(`${baseUrl}/jobs/${jobId}/glossary`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ entries }),
  }).then(asJson<{ entries: GlossaryEntry[] }>);
}

export function runJob(
  baseUrl: string,
  jobId: string,
  outPath: string,
): Promise<JobResponse> {
  return fetch(`${baseUrl}/jobs/${jobId}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ out_path: outPath }),
  }).then(asJson<JobResponse>);
}

export function resumeJob(
  baseUrl: string,
  jobId: string,
  outPath: string,
): Promise<JobResponse> {
  return fetch(`${baseUrl}/jobs/${jobId}/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ out_path: outPath }),
  }).then(asJson<JobResponse>);
}

export function cancelJob(baseUrl: string, jobId: string): Promise<JobResponse> {
  return fetch(`${baseUrl}/jobs/${jobId}/cancel`, { method: "POST" }).then(
    asJson<JobResponse>,
  );
}
