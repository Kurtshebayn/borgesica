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
  /** null means "not classified yet" (a run may seed it) and, on PUT,
   * "keep the stored value"; "unanchored" is a person's decision that the
   * term carries no character gender. */
  gender?: GlossaryGender | null;
  /** The rendering the vote committed; PUT always clears it. */
  first_draw?: string | null;
}

export type CharacterGender = "masculine" | "feminine";

/** What an entry can store: a character gender or a decision for none. */
export type GlossaryGender = CharacterGender | "unanchored";

/** Deterministic reasons an entry deserves a reviewer's look.
 * kept_vs_translated always comes with conflicting_renderings;
 * possible_gloss is a weak signal (it also fires on correct translations of
 * capitalised English words such as Academy -> la Academia). */
export type GlossaryRiskFlag =
  | "conflicting_renderings"
  | "kept_vs_translated"
  | "lowercase_identity"
  | "possible_gloss"
  | "undecided_gender";

/** One distinct rendering the extraction batches proposed for a term. */
export interface RenderingProposal {
  rendering: string;
  batches: number;
}

export interface GlossaryReviewEntry {
  /** The stored entry, unchanged; edit it through PUT /glossary. */
  entry: GlossaryEntry;
  /** The gender a translation run will anchor, or null (also for an entry
   * stored as "unanchored"). */
  gender: CharacterGender | null;
  /** "stored": set on the entry; "seeded": derived from the pronoun counts. */
  gender_source: "stored" | "seeded" | null;
  /** Gendered pronouns near the term in the English source. */
  masculine_mentions: number;
  feminine_mentions: number;
  /** Most-proposed first; empty when no extraction batch proposed the term. */
  proposals: RenderingProposal[];
  flags: GlossaryRiskFlag[];
}

/** GET /jobs/{id}/glossary/review — derived on each request, never stored.
 * Entries come in the same order as GET /jobs/{id}/glossary. */
export interface GlossaryReviewResponse {
  entries: GlossaryReviewEntry[];
  /** A verbatim entry is classified only with at least this many mentions... */
  min_gender_mentions: number;
  /** ...of which at least this share agree; otherwise undecided_gender. */
  min_gender_ratio: number;
}

/** The first-person narrator's gender (a narrator always has one). */
export type NarratorGender = "masculine" | "feminine";

/** Who the first-person "I" of a chapter is; the name is optional. */
export interface NarratorFact {
  name: string | null;
  gender: NarratorGender;
}

/** One chapter (an EPUB spine item) as GET /jobs/{id}/narrators lists it. */
export interface NarratorChapter {
  /** Stable chapter id (the spine item's href); PUT refers to it. */
  key: string;
  title: string;
  /** The first words of the chapter, to recognise it. */
  opening: string;
  first_chunk: number;
  last_chunk: number;
  /** Pre-marked by the server from its pronoun counts. */
  first_person: boolean;
  first_person_per_1k: number;
  /** The stored narrator, or null: the chapter has no narrator fact. */
  narrator: NarratorFact | null;
}

/** GET (and PUT) /jobs/{id}/narrators. Empty for jobs without chapters
 * (subtitles). */
export interface NarratorsResponse {
  chapters: NarratorChapter[];
}

/** One element of the PUT body: a chapter with a narrator. The PUT is a full
 * overwrite, so a chapter left out has no narrator afterwards. */
export interface NarratorAssignment {
  key: string;
  name: string | null;
  gender: NarratorGender;
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
  /** True once the explicit pre-translation glossary pass has run for the
   * job, so an empty glossary that was never extracted can be told apart
   * from one that was. */
  glossary_extracted: boolean;
}

/** Cost of the explicit, paid pre-translation glossary pass (one provider
 * call per batch), kept apart from the translation estimate. */
export interface GlossaryExtractionEstimate {
  batches: number;
  input_tokens: number;
  output_tokens: number;
  output_tokens_high: number;
  usd: number;
  usd_high: number;
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
  /** Null when the pass cannot run: the job is not CREATED, or its glossary
   * was already extracted. */
  glossary_extraction: GlossaryExtractionEstimate | null;
  /** The whole book: the translation plus the glossary preparation while it
   * is still ahead. `within_budget` checks `book_usd_high`. */
  book_usd_low: number;
  book_usd_high: number;
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
