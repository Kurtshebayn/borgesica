/**
 * Shared request/response types mirroring `borgesica/serve/schemas.py`.
 * Kept as plain interfaces (no runtime validation) — the sidecar is the
 * source of truth for validation; this file exists so `apiClient.ts` and
 * `wizard.ts` share one vocabulary instead of ad-hoc inline shapes.
 */

export type JobStatus =
  | "CREATED"
  | "ESTIMATING"
  | "RUNNING"
  | "PAUSED"
  | "DONE"
  | "FAILED"
  | "CANCELLED";

export interface GlossaryEntry {
  term: string;
  translation: string;
  locked: boolean;
  note?: string | null;
}

export interface JobResponse {
  id: string;
  status: JobStatus;
  total_chunks: number;
  completed_chunks: number;
  cost_usd: number;
  best_effort_count: number;
  best_effort_indices: number[];
  /** Chunks the provider could never translate (all retries + fallback
   * failed). Under continue_on_error=true the job still ends DONE at $0
   * shipping SOURCE text, so a non-zero failed_count is the only signal that
   * a "DONE" run actually produced untranslated output. */
  failed_count: number;
  failed_indices: number[];
}

export interface EstimateResponse {
  input_tokens: number;
  output_tokens: number;
  usd: number;
  usd_low: number;
  usd_high: number;
  model: string;
  cached: boolean;
  within_budget: boolean;
}

export interface CreateJobRequest {
  source_path: string;
  model: string;
  target_lang?: string;
  budget_usd?: number | null;
  chunk_size?: number;
  line_length?: number;
  glossary_strategy?: "llm" | "spacy" | "hybrid" | "none";
  quality_mode?: "fast" | "reflective";
  prose_chunk_tokens?: number;
  prose_segmentation?: "batch" | "paragraph";
  continue_on_error?: boolean;
}
