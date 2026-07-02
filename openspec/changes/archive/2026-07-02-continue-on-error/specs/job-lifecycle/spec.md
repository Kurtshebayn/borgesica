# Delta for job-lifecycle

Change: `continue-on-error` · Capability: `job-lifecycle`
Phase: spec · Status: draft · Artifact store: openspec

---

## MODIFIED Requirements

### Requirement: run_job executes chunks sequentially from first PENDING chunk

`TranslatorEngine.run_job(job_id, on_progress)` SHALL:
1. Load the job and verify `status in {CREATED, PAUSED}`.
2. Set `job.status = JobStatus.RUNNING` and persist.
3. Execute the translation loop: for each chunk in index order, skip chunks with `status=DONE`, translate remaining chunks, persist each as `DONE` before moving to the next.
4. When a chunk exhausts all translation attempts, apply the `continue_on_error` gate (see "run_job applies the continue_on_error gate on chunk failure"): either persist it `FAILED` and continue the loop, or persist it `FAILED` and pause the job, depending on `JobConfig.continue_on_error`.
5. After all chunks reach a terminal per-chunk state (`DONE` or `FAILED`), call `DocumentWriter.write` and set `job.status = JobStatus.DONE` — UNLESS the loop was paused by step 4, in which case `write` is NOT called and `job.status = JobStatus.PAUSED`.
6. Call `on_progress` after each chunk completes (whether `DONE` or `FAILED`), including the final one.

(Previously: step 4 and the DONE/PAUSED split in step 5 did not exist — the requirement was silent on chunk-failure handling; that contract lived only in `book-translation`'s inline-tags requirement, which unconditionally set the job to `PAUSED`.)

#### Scenario: run_job translates all PENDING chunks in order

- GIVEN a job in `CREATED` state with 3 chunks (all `PENDING`) and a `FakeTranslationProvider`
- WHEN `engine.run_job(job_id)` is called
- THEN the provider's `translate` method SHALL be called exactly 3 times, in index order (0, 1, 2), and the job's final `status` SHALL be `JobStatus.DONE`

#### Scenario: on_progress is called once per completed chunk

- GIVEN a job with 4 chunks and a `FakeTranslationProvider`
- WHEN `engine.run_job(job_id, on_progress=callback)` is called
- THEN `callback` SHALL be called exactly 4 times, each call with a `Progress` object where `chunk_index` matches the just-completed chunk (0, 1, 2, 3 in order)

#### Scenario: run_job on a RUNNING job raises an error

- GIVEN a job with `status=JobStatus.RUNNING` (another process has it)
- WHEN `engine.run_job(job_id)` is called
- THEN a domain exception (`JobStateError` or equivalent) SHALL be raised and no provider call SHALL be made

#### Scenario: job finishes DONE with a FAILED chunk present (continue_on_error=True, default)

- GIVEN a job with 3 chunks where chunk 1 exhausts all translation attempts, `JobConfig.continue_on_error=True` (default)
- WHEN `engine.run_job(job_id)` is called
- THEN chunk 1 SHALL be persisted `FAILED` with `translated_text=None`, chunks 0 and 2 SHALL be translated normally, `DocumentWriter.write` SHALL be called, and the job's final `status` SHALL be `JobStatus.DONE`

#### Scenario: job pauses on first FAILED chunk (continue_on_error=False, strict)

- GIVEN a job with 3 chunks where chunk 1 exhausts all translation attempts, `JobConfig.continue_on_error=False`
- WHEN `engine.run_job(job_id)` is called
- THEN chunk 1 SHALL be persisted `FAILED` with `translated_text=None`, chunk 2 SHALL NOT be translated, `DocumentWriter.write` SHALL NOT be called, and the job's final `status` SHALL be `JobStatus.PAUSED`

---

## ADDED Requirements

### Requirement: JobConfig.continue_on_error gates chunk-failure handling, default ON

`JobConfig` SHALL carry a `continue_on_error: bool` field, default `True`. This flag gates the run-loop's behavior when a chunk exhausts all translation attempts (see "run_job applies the continue_on_error gate on chunk failure"). The CLI SHALL expose a `--strict` flag that sets `continue_on_error=False`; absent `--strict`, the default (`True`) applies.

#### Scenario: default JobConfig has continue_on_error=True

- GIVEN `JobConfig()` is constructed with no explicit `continue_on_error` argument
- WHEN the resulting config is inspected
- THEN `continue_on_error` SHALL be `True`

#### Scenario: --strict flag sets continue_on_error=False

- GIVEN a CLI invocation with the `--strict` flag
- WHEN the `JobConfig` is built from CLI arguments
- THEN `continue_on_error` SHALL be `False`

---

### Requirement: run_job applies the continue_on_error gate on chunk failure

When a chunk exhausts all translation attempts (see book-translation's inline-tags requirement for the retry/fallback sequence that precedes this point), the run loop SHALL branch on `JobConfig.continue_on_error`:
- **`True` (default)**: persist the chunk `FAILED` with `translated_text=None`; do NOT pause; proceed to the next chunk in index order. After the loop, if all chunks reached a terminal state, the job SHALL finish `DONE` even if one or more chunks are `FAILED`.
- **`False` (strict)**: persist the chunk `FAILED` with `translated_text=None`; set `job.status = JobStatus.PAUSED`; stop the loop and return immediately (exact prior contract, unchanged).

This gate applies ONLY to chunk-attempt exhaustion. `BudgetExceeded` SHALL continue to `PAUSE` the job unconditionally, regardless of `continue_on_error` (unchanged). `cancel_job` semantics are unchanged.

#### Scenario: continue_on_error=True — job continues past a FAILED chunk

- GIVEN a 5-chunk job with `continue_on_error=True` where chunk 2 exhausts all attempts
- WHEN `engine.run_job(job_id)` is called
- THEN chunks 0, 1, 3, and 4 SHALL be translated and persisted `DONE`, chunk 2 SHALL be persisted `FAILED`, and the job SHALL finish with `status=JobStatus.DONE`

#### Scenario: continue_on_error=True — all chunks fail, job still finishes DONE

- GIVEN a 3-chunk job with `continue_on_error=True` where every chunk exhausts all translation attempts (e.g. a provider outage)
- WHEN `engine.run_job(job_id)` is called
- THEN all 3 chunks SHALL be persisted `FAILED` with `translated_text=None`, `DocumentWriter.write` SHALL still be called (writers fall back to `source_text` for `None`), and the job's final `status` SHALL be `JobStatus.DONE`, with the skip report listing all 3 indices

#### Scenario: continue_on_error=False — job pauses immediately, remaining chunks untouched

- GIVEN a 5-chunk job with `continue_on_error=False` where chunk 2 exhausts all attempts
- WHEN `engine.run_job(job_id)` is called
- THEN chunks 0 and 1 SHALL be `DONE`, chunk 2 SHALL be `FAILED`, chunks 3 and 4 SHALL remain `PENDING` (untouched), and the job's final `status` SHALL be `JobStatus.PAUSED`

#### Scenario: budget-exceeded still pauses regardless of continue_on_error

- GIVEN a job with `continue_on_error=True` where the configured budget is exhausted mid-run
- WHEN the orchestrator detects `BudgetExceeded`
- THEN the job SHALL be set to `JobStatus.PAUSED` exactly as before this change, independent of the `continue_on_error` value

---

### Requirement: prose guard skips provider calls for chunks with no alphabetic content

Before making any provider call for a chunk, the orchestrator SHALL apply `markup.strip` to `chunk.source_text`. If the stripped result is empty or whitespace-only, OR contains no alphabetic characters, the orchestrator SHALL mark the chunk `DONE` with `translated_text = chunk.source_text` (explicit pass-through) WITHOUT making any provider call. This guard applies identically regardless of `continue_on_error`.

The guard is intentionally conservative: it fires only on the absence of ANY alphabetic character in the stripped text. Text that survives `markup.strip` and contains at least one letter (e.g. a watermark string) is NOT caught by this guard and proceeds through the normal translation attempt sequence.

#### Scenario: bare-tag-pair chunk with no letters after strip — guard catches it, zero calls

- GIVEN a chunk whose `source_text` is `<span></span>` and `markup.strip` on it yields an empty string
- WHEN the orchestrator processes this chunk
- THEN no provider call SHALL be made, the chunk SHALL be persisted `DONE` with `translated_text` equal to the original `source_text`, and the job's incurred cost for this chunk SHALL be `0.0`

#### Scenario: watermark chunk with letters after strip — guard does NOT catch it, rides the failure net

- GIVEN a chunk whose `source_text` is an "OceanofPDF.com" watermark link and `markup.strip` on it yields `"OceanofPDF.com"` (contains alphabetic characters)
- WHEN the orchestrator processes this chunk
- THEN the prose guard SHALL NOT short-circuit it; the chunk SHALL proceed through the normal translation attempt sequence; if all attempts are exhausted, it SHALL become `FAILED` and be handled per the `continue_on_error` gate (continue past it by default, or pause the job under `--strict`)

#### Scenario: whitespace-only chunk is caught by the guard

- GIVEN a chunk whose `source_text` is `"   \n\t  "` (whitespace only)
- WHEN the orchestrator processes this chunk
- THEN no provider call SHALL be made and the chunk SHALL be persisted `DONE` with `translated_text` equal to the original `source_text`

---

### Requirement: skip report surfaces FAILED chunk indices at end of run

At the end of `run_job` (and `resume_job`), the engine SHALL make the set of `FAILED` chunk indices available to callers without any checkpoint schema change (per-chunk status is already persisted). `TranslatorEngine.status(job_id)` SHALL allow callers to derive the FAILED index list from the returned chunk statuses. The CLI `run` command SHALL print a skip-summary line when one or more chunks are `FAILED`. The CLI `status` command SHALL list `FAILED` chunk indices when present.

#### Scenario: status exposes FAILED chunk indices after a continue_on_error run

- GIVEN a job that finished `DONE` with chunks 2 and 5 persisted `FAILED`
- WHEN `engine.status(job_id)` is called
- THEN the returned chunk statuses SHALL identify chunks 2 and 5 as `FAILED`, and all other chunks as `DONE`

#### Scenario: CLI run prints a skip-summary line when chunks failed

- GIVEN a `run` CLI invocation that completes with `continue_on_error=True` and 1 or more `FAILED` chunks
- WHEN the run finishes and the job reaches `JobStatus.DONE`
- THEN the CLI SHALL print a summary line naming the count and indices of `FAILED` chunks

#### Scenario: CLI status lists no failed chunks when none exist

- GIVEN a job that finished `DONE` with zero `FAILED` chunks
- WHEN the CLI `status` command is invoked for that job
- THEN no failed-chunk listing SHALL be printed
