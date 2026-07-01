# Spec: job-lifecycle

Change: `translation-engine` · Capability: `job-lifecycle`
Phase: spec · Status: draft · Artifact store: openspec

---

## ADDED Requirements

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
4. After all chunks are `DONE`, call `DocumentWriter.write` and set `job.status = JobStatus.DONE`.
5. Call `on_progress` after each chunk completes, including the final one.

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
