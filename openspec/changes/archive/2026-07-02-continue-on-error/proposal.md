# Proposal: Continue-on-Error (one bad chunk must not block an entire book)

Change: `continue-on-error`
Phase: proposal · Status: draft · Artifact store: openspec

---

## 1. Problem

One untranslatable chunk can block an ENTIRE book, forever.

Today's run-loop contract is all-or-nothing: in `TranslationOrchestrator.run` (`borgesica/domain/orchestrator.py:235-247`), when `_translate_with_retry` exhausts every attempt (3 tags-in-text attempts + 1 deterministic strip/reinsert fallback), the chunk is marked `FAILED` and the WHOLE JOB is set to `PAUSED`. The run stops and returns.

That is fine when the failure is a flaky model call — you resume and it recovers. It is NOT fine when a chunk has **no translatable prose to begin with**. An EPUB cover chunk (`<img src="images/cover.jpg"/>` plus an "OceanofPDF.com" watermark link) can never yield a valid `TranslationUnit`, so it fails on every attempt, on every resume, forever. One structurally-doomed chunk holds the whole book hostage.

**Verified live**: job `512f79d4` (Haiku, real EPUB smoke test) paused at chunk 0 = the cover. The remaining ~N chapters — all perfectly translatable — never ran.

Why now: EPUB is the primary book format (M2, shipped and archived) and real EPUBs routinely carry a cover/watermark chunk. The engine is otherwise complete (290 passed, 6 skipped) — this is the one contract that makes real books fail on the first page.

Success looks like: a real EPUB with a non-prose cover translates end-to-end to a valid ebook by default; the cover rides through as source-text pass-through; the run finishes `DONE`; the user sees exactly which chunks (if any) were skipped.

---

## 2. What changes

The core contract shift, stated plainly:

> **A `FAILED` chunk no longer implies a `PAUSED` job — by default.**

Behavior is gated on a new config flag, default ON (resilient by default, strict on request):

| # | Change | Where |
|---|--------|-------|
| 1 | New `JobConfig.continue_on_error: bool = True`. | `borgesica/domain/models.py` |
| 2 | **Continue path** (`continue_on_error=True`): when a chunk exhausts all attempts, save it `FAILED` with `translated_text=None` (unchanged), do NOT pause, CONTINUE to the next chunk. After the loop, the job finishes `DONE` even if some chunks are `FAILED`. | `borgesica/domain/orchestrator.py` |
| 3 | **Strict path** (`continue_on_error=False`): preserves the EXACT current contract — `FAILED` → `PAUSED` → stop and return. | `borgesica/domain/orchestrator.py` |
| 4 | **Prose guard** (before any provider call for a chunk): if `markup.strip(chunk.source_text)` (`borgesica/domain/markup.py:39`) leaves text that is empty/whitespace OR contains **no alphabetic characters**, mark the chunk `DONE` with `translated_text = chunk.source_text` (explicit pass-through). Zero provider calls, zero cost. | `borgesica/domain/orchestrator.py` |
| 5 | **Skip report** (pass-through): at end of run, surface which chunk indices ended `FAILED`. No checkpoint schema change — per-chunk status is already persisted. | `borgesica/domain/orchestrator.py`, `borgesica/api.py` |
| 6 | CLI opt-out flag `--strict` (sets `continue_on_error=False`); `run` prints a skip-summary line; `status` lists failed chunk indices. | `borgesica/__main__.py` |

### Design invariants (do not redesign these)

- **Writers need NO changes.** `epub_writer.py:316-319` and `srt_writer.py:128` already fall back to `source_text` when `translated_text is None` — verified. A `FAILED` chunk emits its original text.
- **Resume semantics intact.** `FAILED` + `translated_text=None` means a later `run` re-attempts ONLY those chunks (`run` skips `DONE` only). Re-running a `continue_on_error` job that finished `DONE` with failed chunks will retry those chunks — same idempotent, skip-DONE loop as today.
- **Budget-exceeded still PAUSES.** Money exhaustion is a deliberate stop, not a flaky chunk — `BudgetExceeded` behavior is unchanged (`orchestrator.py:210-211`). Cancel is unchanged.
- **Prose guard is deliberately conservative.** No real prose has zero alphabetic characters, so the guard has **zero false positives** — it never silently skips a translatable sentence. Note that `markup.strip` keeps the text *between* tags, so the actual cover (watermark text "OceanofPDF.com" survives `strip`, and it contains letters) is NOT caught by the guard. That is intended: the cover rides the continue-on-error net instead. We add NO URL/non-prose heuristics — each heuristic risks silently leaving a real sentence untranslated, which is worse than a few wasted cents. We iterate later with real skip-report data.

### Contract table (before → after)

| Situation | Before (today) | After — default (`continue_on_error=True`) | After — `--strict` |
|-----------|----------------|--------------------------------------------|--------------------|
| Chunk exhausts all attempts | `FAILED` → job `PAUSED`, stop | `FAILED` (text=None), CONTINUE; job ends `DONE` | `FAILED` → job `PAUSED`, stop (unchanged) |
| Chunk has no alphabetic content | (attempts made, likely fails) | Prose guard: `DONE` pass-through, 0 calls, 0 cost | same guard applies |
| Budget exceeded | `PAUSED` | `PAUSED` (unchanged) | `PAUSED` (unchanged) |
| Cancel | `CANCELLED` | `CANCELLED` (unchanged) | `CANCELLED` (unchanged) |

---

## 3. Impact — affected specs (deltas)

Two canonical specs describe the run-loop / job lifecycle and carry the FAILED→PAUSED contract. Both get a delta.

| Spec file | Requirement getting a delta | Nature of delta |
|-----------|-----------------------------|-----------------|
| `openspec/specs/job-lifecycle/spec.md` | **"run_job executes chunks sequentially from first PENDING chunk"** (lines 36-51). Also touches the surrounding run-loop model. | ADD: `continue_on_error` gate on the run-loop. When `True`, an exhausted chunk is saved `FAILED` and the loop CONTINUES; job ends `DONE` with failed chunks present. When `False`, the current `FAILED`→`PAUSED` contract holds. ADD: prose-guard requirement (non-alphabetic chunk → `DONE` pass-through, zero provider calls). ADD: skip-report requirement (failed chunk indices surfaced at end of run). New scenarios for each; new default value in `JobConfig`. |
| `openspec/specs/book-translation/spec.md` | **"inline EPUB tags are preserved in place (tags-in-text), with strip/reinsert fallback"** (line 132) — currently ends: *"only if the fallback also fails does the chunk become `ChunkStatus.FAILED` and the job `JobStatus.PAUSED`."* | AMEND that sentence: the fallback-failure outcome is now `FAILED`; the job becomes `PAUSED` ONLY when `continue_on_error=False`, otherwise the run continues and the chunk stays `FAILED` with source-text pass-through on write. |

Note: `job-lifecycle`'s existing per-chunk persistence, resume-skips-DONE, idempotency, cancel, and budget requirements are UNCHANGED — the delta is additive and gated. The existing M1-8 test that asserts FAILED→PAUSED gets **parametrized** across both flag values, not deleted.

### Affected code (surgical)

- `borgesica/domain/models.py` — add `JobConfig.continue_on_error: bool = True`.
- `borgesica/domain/orchestrator.py` — prose guard + continue/strict branch at the current `final_unit is None` block (lines 235-247); collect failed indices for the skip report.
- `borgesica/__main__.py` — `--strict` flag; skip-summary in `run`; failed-index listing in `status`.
- `borgesica/api.py` — surface failed chunk indices (skip report pass-through), if not already reachable via `status`.
- Specs deltas (above) + tests (parametrize M1-8; add prose-guard, continue-path, and skip-report cases).

---

## 4. Out of scope

- **Non-prose / URL / cover heuristics.** No content classification beyond the zero-alphabetic guard. Each heuristic risks silently dropping a real sentence — deferred until real skip-report data justifies it.
- **Writers and readers.** No changes to `epub_writer.py`, `srt_writer.py`, or any `DocumentReader` — the `translated_text=None` → `source_text` fallback already exists and is verified.
- **Providers / DeepSeek robustness.** Making weak/local models fail less often is a separate concern; this change is about surviving failures gracefully, not preventing them.
- **Checkpoint schema.** Per-chunk status is already persisted; the skip report is pure pass-through — no migration, no new columns.
- **Budget and cancel semantics.** Deliberately unchanged.

---

## 5. Risks & mitigations

| # | Risk | Mitigation |
|---|------|------------|
| 1 | Silent degradation — a book "succeeds" (`DONE`) while chunks are secretly untranslated. | Skip report is mandatory: `run` prints a summary line and `status` lists failed indices. Failure is visible, not hidden. |
| 2 | Prose guard skips real prose (false positive). | Guard fires ONLY on zero alphabetic characters — mathematically impossible for real prose. Conservative by design. |
| 3 | Default flip (ON) changes existing behavior for current users. | Contract change is called out explicitly here; `--strict` restores the exact prior contract; M1-8 test parametrized to prove both paths. |
| 4 | Re-running a `DONE`-with-failures job retries failed chunks (may surprise/cost). | Documented as intended resume semantics; failed chunks are the ONLY ones re-attempted (skip-DONE loop), so the retry is minimal and idempotent. |

---

## 6. Next phase

`sdd-spec` — author the `job-lifecycle` and `book-translation` spec deltas (new scenarios for continue path, strict path, prose guard, skip report). `sdd-design` may run in parallel if any run-loop restructuring warrants it, though this change is small enough that spec → tasks may suffice.
