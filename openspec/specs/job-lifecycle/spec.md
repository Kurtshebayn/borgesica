# Spec: job-lifecycle

Capability: `job-lifecycle` · Status: canonical

---

### Requirement: create_job reads, chunks, seeds glossary, and persists in CREATED state

`TranslatorEngine.create_job(source_path, config)` SHALL:
1. Call the appropriate `DocumentReader.read` for `config.source_type`.
2. Chunk the result via the domain chunker.
3. Seed the glossary using the configured `GlossaryExtractor` (or produce an empty glossary if `glossary_strategy="none"`).
4. Persist the `Job`, all `Chunk` objects (all with `status=PENDING`), and the `Glossary` to the `CheckpointStore`.
5. Return a `Job` with `status=JobStatus.CREATED` and `total_chunks` set to the actual chunk count.

No provider translation call SHALL be made during `create_job`.

#### Scenario: create_job returns CREATED job with correct chunk count

Given a 75-cue `.srt` file and `JobConfig(chunk_size=25)`,

When `engine.create_job(path, config)` is called,

Then the returned `Job` SHALL have `status=JobStatus.CREATED`, `total_chunks=3`, `completed_chunks=0`, and `cost_usd=0.0`.

#### Scenario: all chunks are persisted as PENDING after create_job

Given a job created from a 50-cue `.srt` file with `chunk_size=25`,

When `checkpoint.load_chunks(job_id)` is called after `create_job`,

Then the result SHALL contain exactly 2 chunks, both with `status=ChunkStatus.PENDING`.

---

### Requirement: run_job executes chunks sequentially from first PENDING chunk

`TranslatorEngine.run_job(job_id, on_progress)` SHALL:
1. Load the job and verify `status in {CREATED, PAUSED}`.
2. Set `job.status = JobStatus.RUNNING` and persist.
3. Execute the translation loop: for each chunk in index order, skip chunks with `status=DONE`, translate remaining chunks, persist each as `DONE` before moving to the next.
4. When a chunk exhausts all translation attempts, apply the `continue_on_error` gate (see "run_job applies the continue_on_error gate on chunk failure"): either persist it `FAILED` and continue the loop, or persist it `FAILED` and pause the job, depending on `JobConfig.continue_on_error`.
5. After all chunks reach a terminal per-chunk state (`DONE` or `FAILED`), call `DocumentWriter.write` and set `job.status = JobStatus.DONE` — UNLESS the loop was paused by step 4, in which case `write` is NOT called and `job.status = JobStatus.PAUSED`.
6. Call `on_progress` after each chunk completes (whether `DONE` or `FAILED`), including the final one.

#### Scenario: run_job translates all PENDING chunks in order

Given a job in `CREATED` state with 3 chunks (all `PENDING`) and a `FakeTranslationProvider`,

When `engine.run_job(job_id)` is called,

Then the provider's `translate` method SHALL be called exactly 3 times, in index order (0, 1, 2), and the job's final `status` SHALL be `JobStatus.DONE`.

#### Scenario: on_progress is called once per completed chunk

Given a job with 4 chunks and a `FakeTranslationProvider`,

When `engine.run_job(job_id, on_progress=callback)` is called,

Then `callback` SHALL be called exactly 4 times, each call with a `Progress` object where `chunk_index` matches the just-completed chunk (0, 1, 2, 3 in order).

#### Scenario: run_job on a RUNNING job raises an error

Given a job with `status=JobStatus.RUNNING` (another process has it),

When `engine.run_job(job_id)` is called,

Then a domain exception (`JobStateError` or equivalent) SHALL be raised and no provider call SHALL be made.

#### Scenario: job finishes DONE with a FAILED chunk present (continue_on_error=True, default)

Given a job with 3 chunks where chunk 1 exhausts all translation attempts, `JobConfig.continue_on_error=True` (default),

When `engine.run_job(job_id)` is called,

Then chunk 1 SHALL be persisted `FAILED` with `translated_text=None`, chunks 0 and 2 SHALL be translated normally, `DocumentWriter.write` SHALL be called, and the job's final `status` SHALL be `JobStatus.DONE`.

#### Scenario: job pauses on first FAILED chunk (continue_on_error=False, strict)

Given a job with 3 chunks where chunk 1 exhausts all translation attempts, `JobConfig.continue_on_error=False`,

When `engine.run_job(job_id)` is called,

Then chunk 1 SHALL be persisted `FAILED` with `translated_text=None`, chunk 2 SHALL NOT be translated, `DocumentWriter.write` SHALL NOT be called, and the job's final `status` SHALL be `JobStatus.PAUSED`.

---

### Requirement: JobConfig.continue_on_error gates chunk-failure handling, default ON

`JobConfig` SHALL carry a `continue_on_error: bool` field, default `True`. This flag gates the run-loop's behavior when a chunk exhausts all translation attempts (see "run_job applies the continue_on_error gate on chunk failure"). The CLI SHALL expose a `--strict` flag that sets `continue_on_error=False`; absent `--strict`, the default (`True`) applies.

#### Scenario: default JobConfig has continue_on_error=True

Given `JobConfig()` is constructed with no explicit `continue_on_error` argument,

When the resulting config is inspected,

Then `continue_on_error` SHALL be `True`.

#### Scenario: --strict flag sets continue_on_error=False

Given a CLI invocation with the `--strict` flag,

When the `JobConfig` is built from CLI arguments,

Then `continue_on_error` SHALL be `False`.

---

### Requirement: run_job applies the continue_on_error gate on chunk failure

When a chunk exhausts all translation attempts (see book-translation's inline-tags requirement for the retry/fallback sequence that precedes this point), the run loop SHALL branch on `JobConfig.continue_on_error`:
- **`True` (default)**: persist the chunk `FAILED` with `translated_text=None`; do NOT pause; proceed to the next chunk in index order. After the loop, if all chunks reached a terminal state, the job SHALL finish `DONE` even if one or more chunks are `FAILED`.
- **`False` (strict)**: persist the chunk `FAILED` with `translated_text=None`; set `job.status = JobStatus.PAUSED`; stop the loop and return immediately (exact prior contract, unchanged).

This gate applies ONLY to chunk-attempt exhaustion. `BudgetExceeded` SHALL continue to `PAUSE` the job unconditionally, regardless of `continue_on_error` (unchanged). `cancel_job` semantics are unchanged.

#### Scenario: continue_on_error=True — job continues past a FAILED chunk

Given a 5-chunk job with `continue_on_error=True` where chunk 2 exhausts all attempts,

When `engine.run_job(job_id)` is called,

Then chunks 0, 1, 3, and 4 SHALL be translated and persisted `DONE`, chunk 2 SHALL be persisted `FAILED`, and the job SHALL finish with `status=JobStatus.DONE`.

#### Scenario: continue_on_error=True — all chunks fail, job still finishes DONE

Given a 3-chunk job with `continue_on_error=True` where every chunk exhausts all translation attempts (e.g. a provider outage),

When `engine.run_job(job_id)` is called,

Then all 3 chunks SHALL be persisted `FAILED` with `translated_text=None`, `DocumentWriter.write` SHALL still be called (writers fall back to `source_text` for `None`), and the job's final `status` SHALL be `JobStatus.DONE`, with the skip report listing all 3 indices.

#### Scenario: continue_on_error=False — job pauses immediately, remaining chunks untouched

Given a 5-chunk job with `continue_on_error=False` where chunk 2 exhausts all attempts,

When `engine.run_job(job_id)` is called,

Then chunks 0 and 1 SHALL be `DONE`, chunk 2 SHALL be `FAILED`, chunks 3 and 4 SHALL remain `PENDING` (untouched), and the job's final `status` SHALL be `JobStatus.PAUSED`.

#### Scenario: budget-exceeded still pauses regardless of continue_on_error

Given a job with `continue_on_error=True` where the configured budget is exhausted mid-run,

When the orchestrator detects `BudgetExceeded`,

Then the job SHALL be set to `JobStatus.PAUSED` exactly as before this change, independent of the `continue_on_error` value.

---

### Requirement: prose guard skips provider calls for chunks with no alphabetic content

Before making any provider call for a chunk, the orchestrator SHALL apply `markup.strip` to `chunk.source_text`. If the stripped result is empty or whitespace-only, OR contains no alphabetic characters, the orchestrator SHALL mark the chunk `DONE` with `translated_text = chunk.source_text` (explicit pass-through) WITHOUT making any provider call. This guard applies identically regardless of `continue_on_error`.

The guard is intentionally conservative: it fires only on the absence of ANY alphabetic character in the stripped text. Text that survives `markup.strip` and contains at least one letter (e.g. a watermark string) is NOT caught by this guard and proceeds through the normal translation attempt sequence. `markup.strip` recognizes inline tags `i|b|u|em|strong|span|a`; it does NOT recognize void/self-closing elements such as `<img>` — a chunk whose only markup is a void element is NOT reduced to empty by `strip` and will therefore NOT be caught by this guard (it proceeds through the normal translation attempt sequence and, if untranslatable, is handled by the `continue_on_error` gate instead).

#### Scenario: bare-tag-pair chunk with no letters after strip — guard catches it, zero calls

Given a chunk whose `source_text` is `<span></span>` and `markup.strip` on it yields an empty string,

When the orchestrator processes this chunk,

Then no provider call SHALL be made, the chunk SHALL be persisted `DONE` with `translated_text` equal to the original `source_text`, and the job's incurred cost for this chunk SHALL be `0.0`.

#### Scenario: watermark chunk with letters after strip — guard does NOT catch it, rides the failure net

Given a chunk whose `source_text` is an "OceanofPDF.com" watermark link and `markup.strip` on it yields `"OceanofPDF.com"` (contains alphabetic characters),

When the orchestrator processes this chunk,

Then the prose guard SHALL NOT short-circuit it; the chunk SHALL proceed through the normal translation attempt sequence; if all attempts are exhausted, it SHALL become `FAILED` and be handled per the `continue_on_error` gate (continue past it by default, or pause the job under `--strict`).

#### Scenario: whitespace-only chunk is caught by the guard

Given a chunk whose `source_text` is `"   \n\t  "` (whitespace only),

When the orchestrator processes this chunk,

Then no provider call SHALL be made and the chunk SHALL be persisted `DONE` with `translated_text` equal to the original `source_text`.

---

### Requirement: skip report surfaces FAILED chunk indices at end of run

At the end of `run_job` (and `resume_job`), the engine SHALL make the set of `FAILED` chunk indices available to callers without any checkpoint schema change (per-chunk status is already persisted). `TranslatorEngine.status(job_id)` SHALL allow callers to derive the FAILED index list from the returned chunk statuses. The CLI `run` command SHALL print a skip-summary line when one or more chunks are `FAILED`. The CLI `status` command SHALL list `FAILED` chunk indices when present.

#### Scenario: status exposes FAILED chunk indices after a continue_on_error run

Given a job that finished `DONE` with chunks 2 and 5 persisted `FAILED`,

When `engine.status(job_id)` is called,

Then the returned chunk statuses SHALL identify chunks 2 and 5 as `FAILED`, and all other chunks as `DONE`.

#### Scenario: CLI run prints a skip-summary line when chunks failed

Given a `run` CLI invocation that completes with `continue_on_error=True` and 1 or more `FAILED` chunks,

When the run finishes and the job reaches `JobStatus.DONE`,

Then the CLI SHALL print a summary line naming the count and indices of `FAILED` chunks.

#### Scenario: CLI status lists no failed chunks when none exist

Given a job that finished `DONE` with zero `FAILED` chunks,

When the CLI `status` command is invoked for that job,

Then no failed-chunk listing SHALL be printed.

---

### Requirement: resume_job skips DONE chunks and incurs zero additional cost for them

`TranslatorEngine.resume_job(job_id, on_progress)` SHALL behave identically to `run_job` except it explicitly accepts jobs in `PAUSED` status. Chunks with `status=DONE` SHALL be skipped — no provider call is made for them. The accumulated `job.cost_usd` for skipped chunks SHALL remain unchanged (0 additional cost for already-completed work).

#### Scenario: resume skips DONE chunks — 0 provider calls for them

Given a job with 5 chunks where chunks 0–2 have `status=DONE` and chunks 3–4 have `status=PENDING`, and the job is in `PAUSED` state,

When `engine.resume_job(job_id)` is called,

Then the provider's `translate` method SHALL be called exactly 2 times (for chunks 3 and 4), and the `cost_usd` delta for the resumed run SHALL reflect only those 2 calls.

#### Scenario: resume rebuilds rolling summary from highest DONE summary

Given a job where chunks 0–2 are DONE, and summaries for chunks 0, 1, and 2 are persisted,

When `engine.resume_job(job_id)` begins translating chunk 3,

Then the rolling summary in chunk 3's system prompt SHALL be the summary from chunk index 2 (the highest DONE summary), not from chunk 0 or an empty string.

#### Scenario: resume from scratch if all chunks are PENDING

Given a job in `PAUSED` state where all chunks are `PENDING` (e.g. paused during glossary review),

When `engine.resume_job(job_id)` is called,

Then the provider SHALL be called for all chunks starting from index 0.

---

### Requirement: checkpoint is saved per-chunk before the next provider call

The orchestrator SHALL persist each chunk as `ChunkStatus.DONE` to the `CheckpointStore` BEFORE making the provider call for the next chunk. A crash AFTER persisting chunk N but BEFORE the provider call for chunk N+1 SHALL result in a resumable state where chunk N is already done.

#### Scenario: chunk N is persisted before chunk N+1 is called

Given a `FakeTranslationProvider` that raises an exception on the second call (chunk index 1),

When `engine.run_job(job_id)` is called on a 3-chunk job,

Then after the exception: chunk 0 SHALL have `status=DONE` in the checkpoint store, and chunks 1 and 2 SHALL have `status=PENDING`.

---

### Requirement: cancel_job is cooperative and preserves completed work

`TranslatorEngine.cancel_job(job_id)` SHALL set a cancellation flag that the orchestration loop checks before starting each chunk. The current chunk in progress SHALL be allowed to finish (including its checkpoint save) before the job is stopped. The job `status` SHALL be set to `JobStatus.CANCELLED`. Completed chunks SHALL retain `status=DONE`.

#### Scenario: cancel stops after the current chunk completes

Given a 5-chunk job where chunk 1 is currently being translated when `cancel_job` is called,

When the orchestrator checks the flag before chunk 2,

Then chunk 2 SHALL NOT be translated, the job status SHALL be `CANCELLED`, and chunk 1 SHALL have `status=DONE` with its translation persisted.

#### Scenario: cancelled job is resumable

Given a cancelled job with 2 DONE chunks and 3 PENDING chunks,

When `engine.resume_job(job_id)` is called,

Then the engine SHALL accept the call (resume is valid from `CANCELLED` state) and translate only the 3 PENDING chunks.

---

### Requirement: idempotent chunk save — re-saving the same (job_id, chunk_index) is a no-op

`CheckpointStore.save_chunk` SHALL implement `INSERT … ON CONFLICT(job_id, chunk_index) DO UPDATE` semantics. Calling `save_chunk` twice with the same `(job_id, chunk_index)` and identical data SHALL not raise an error and SHALL not produce duplicate rows. Calling it with different `translated_text` for the same key SHALL overwrite (update) the existing row.

#### Scenario: duplicate save of a DONE chunk does not duplicate or error

Given a chunk that has already been saved as `DONE` with `translated_text = "Hola mundo."`,

When `checkpoint.save_chunk(job_id, chunk)` is called again with the same chunk,

Then the checkpoint store SHALL contain exactly 1 row for `(job_id, chunk.index)`, and no exception SHALL be raised.

#### Scenario: re-running a completed job does not re-charge for DONE chunks

Given a job where all chunks are `DONE` (job is `DONE`),

When `engine.resume_job(job_id)` is called (e.g. user accidentally reruns),

Then 0 provider calls SHALL be made, `job.cost_usd` SHALL be unchanged, and the job status SHALL remain `DONE`.

---

### Requirement: status returns the current persisted state of a job

`TranslatorEngine.status(job_id)` SHALL return the `Job` object as loaded from the `CheckpointStore`, including the current `status`, `completed_chunks`, `total_chunks`, and `cost_usd`. It SHALL raise a `JobNotFoundError` for unknown `job_id` values.

#### Scenario: status reflects mid-run progress

Given a 10-chunk job where 6 chunks are DONE and the job is RUNNING,

When `engine.status(job_id)` is called from a different call (not the running thread),

Then the returned `Job` SHALL have `completed_chunks == 6` and `status == JobStatus.RUNNING`.

#### Scenario: status raises for unknown job_id

Given a `job_id` that does not exist in the checkpoint store,

When `engine.status(job_id)` is called,

Then `JobNotFoundError` SHALL be raised.
