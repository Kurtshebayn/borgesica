# Tasks: Continue-on-Error (continue-on-error)

Change: `continue-on-error` · Phase: tasks · Status: draft · Artifact store: openspec

Design phase: SKIPPED (user decision — the run-loop shape, prose-guard rule, and CLI
surface are fully specified in `proposal.md`). Tasks below are derived directly from
`proposal.md` + the two delta specs (`specs/job-lifecycle/spec.md`,
`specs/book-translation/spec.md`).

---

## Legend

- **[T]** = write the failing test first, then implement to green (strict TDD)
- **[I]** = implement only (pure scaffolding / doc-only, no meaningful behavior to test-drive)
- **seq** = must follow the previous task in the same work unit
- **par** = can run in parallel with other `par` tasks in the same work unit
- Spec refs use `capability/requirement-keyword` notation
- Test runner: `.venv/Scripts/python.exe -m pytest` (Windows). Baseline: 290 passed, 6 skipped — must stay green throughout.

---

## WU-1 — JobConfig.continue_on_error flag (foundation)

Everything downstream branches on this flag, so it lands first. Small, self-contained,
mechanical — one commit.

### WU1-1 [x][T] — `JobConfig.continue_on_error: bool = True`

**Depends on**: nothing
**Spec**: job-lifecycle/"JobConfig.continue_on_error gates chunk-failure handling, default ON" (scenario: default JobConfig has continue_on_error=True)
**seq**

Test first (`tests/unit/test_models.py`, extend the existing `JobConfig` defaults test or add a new one):
1. `JobConfig(source_type=SourceType.SRT, model="x")` (no explicit `continue_on_error`) → `continue_on_error is True`.
2. `JobConfig(..., continue_on_error=False)` → `continue_on_error is False`.

Implement in `borgesica/domain/models.py`: add `continue_on_error: bool = True` field to `JobConfig` (alongside `quality_mode`, `prose_chunk_tokens` — same style, no custom validator needed).

Domain purity: `models.py` is stdlib + pydantic only — no import changes required.

Deliverable: `pytest tests/unit/test_models.py` → all pass (existing + 2 new).

**Commit**: `feat(domain): add JobConfig.continue_on_error flag, default True`

---

## WU-2 — Orchestrator: prose guard + continue/strict gate

This is the dense work unit — the actual contract change. Two behaviors land together
because they share the same call site (`orchestrator.py` lines ~213-247) and the guard
must run BEFORE the gate for the tests to make sense in isolation.

### WU2-1 [x][T] — Prose guard: zero-alphabetic chunks pass through with 0 provider calls

**Depends on**: WU1-1
**Spec**: job-lifecycle/"prose guard skips provider calls for chunks with no alphabetic content" (all 3 scenarios)
**seq**

Test first (`tests/unit/test_orchestrator.py`, new section appended after the existing M2-0 tests):
1. Chunk `source_text='<img src="images/cover.jpg"/>'` (strips to empty) → `provider.call_count == 0`, chunk persisted `DONE`, `translated_text == source_text`, no cost accrued (`running_cost` unchanged — assert via `result.cost_usd == 0.0` for a single-chunk job).
2. Chunk `source_text="   \n\t  "` (whitespace only) → same assertions as test 1 (0 calls, `DONE`, pass-through).
3. Chunk `source_text` = an "OceanofPDF.com"-style watermark link whose `markup.strip` output contains letters → provider IS called (guard does NOT catch it); use a `FakeTranslationProvider` with a canned successful unit so the chunk resolves `DONE` normally — this test proves the guard does NOT over-fire, not the failure path (that belongs to WU2-2).

Implement in `borgesica/domain/orchestrator.py`, inside the per-chunk loop, BEFORE the `system_prompt = self._ctx.build_system_prompt(...)` line (~213):
- After the budget check, before building the system prompt: `stripped_text, _ = markup.strip(chunk.source_text)`. If `stripped_text.strip() == "" or not any(c.isalpha() for c in stripped_text)`: persist chunk `DONE` with `translated_text = chunk.source_text`, do NOT call the provider, do NOT update cost, `continue` to the next chunk (skip system-prompt build, translate-with-retry, and the `final_unit is None` branch entirely for this chunk).
- `markup` module is already imported in `orchestrator.py` (used by `_translate_with_retry`) — reuse the existing import, no new import needed. Confirms domain purity is unaffected (stdlib + pydantic + existing `markup.py`, which is itself domain-pure).

Deliverable: `pytest tests/unit/test_orchestrator.py` → all pass (baseline + 3 new).

**Commit**: `feat(domain): prose guard — zero-alphabetic chunks pass through without a provider call`

---

### WU2-2 [x][T] — Parametrize the existing FAILED→PAUSED test across continue_on_error

**Depends on**: WU1-1
**Spec**: job-lifecycle/"run_job applies the continue_on_error gate on chunk failure" (scenario: continue_on_error=False — job pauses immediately, remaining chunks untouched)
**seq** (independent of WU2-1's guard change; touches a different test, can be done before or after WU2-1, but ordered here for narrative flow — do NOT parallelize with WU2-3, since WU2-3 changes the exact code block this test exercises)

Locate the target test: `tests/unit/test_orchestrator.py::test_tag_mismatch_all_retries_fail_chunk_failed_job_paused` (line ~721 — the M1-8 original that asserts `ChunkStatus.FAILED` + `JobStatus.PAUSED` on retry exhaustion). This is the test named in the constraint — confirmed as the one exercising `orchestrator.py`'s `final_unit is None` block directly (not the M2-0 fallback-exhaustion test at line ~1154, which is a different scenario — both stay, only the M1-8 one gets parametrized).

Change (do NOT delete): parametrize `test_tag_mismatch_all_retries_fail_chunk_failed_job_paused` over `continue_on_error=True/False` using `@pytest.mark.parametrize("continue_on_error", [True, False])`:
- `continue_on_error=False` branch: existing assertions unchanged — chunk 1 `FAILED`, job `PAUSED`, chunk 2 (if present) untouched/`PENDING`, loop stops.
- `continue_on_error=True` branch (NEW): chunk 1 `FAILED` with `translated_text=None`, loop CONTINUES to remaining chunks, job ends `DONE`. Use a 2-chunk job (chunk 0 fails, chunk 1 has a canned-success provider response) so the "continues past" behavior is provable, not just "doesn't crash."

`make_config()` test helper: add `continue_on_error` as a parameter (default `True` to match the new `JobConfig` default) so both branches can construct a `JobConfig` with the right flag — check `make_config()`'s existing signature in the test file before touching it; extend it minimally (`continue_on_error: bool = True` kwarg passed through).

This test will be RED until WU2-3 implements the gate — write it now, watch it fail on the `continue_on_error=True` parametrization only (the `False` branch stays green throughout, since that path is unchanged behavior).

Deliverable after WU2-3 lands: `pytest tests/unit/test_orchestrator.py -k test_tag_mismatch_all_retries_fail_chunk_failed_job_paused` → 2 pass (both parametrizations).

**Commit**: bundled into WU2-3's commit (test + implementation land together — this task literally cannot go green alone).

---

### WU2-3 [x][T] — Continue/strict gate at chunk-failure exhaustion

**Depends on**: WU2-2 (failing test in place), WU2-1 (guard must run first in the loop, though the two are independent code paths)
**Spec**: job-lifecycle/"run_job applies the continue_on_error gate on chunk failure" (all scenarios), job-lifecycle/"job finishes DONE with a FAILED chunk present" + "job pauses on first FAILED chunk", book-translation/"inline EPUB tags are preserved..." (fallback-exhaustion scenarios)
**seq**

Additional tests first (`tests/unit/test_orchestrator.py`), beyond WU2-2's parametrization:
1. 5-chunk job, `continue_on_error=True`, chunk 2 exhausts all attempts → chunks 0,1,3,4 `DONE`, chunk 2 `FAILED`, job finishes `DONE` (job-lifecycle scenario: "continue_on_error=True — job continues past a FAILED chunk").
2. 3-chunk job, `continue_on_error=True`, ALL chunks fail → all 3 `FAILED` with `translated_text=None`, job finishes `DONE` (not `PAUSED`) (scenario: "all chunks fail, job still finishes DONE").
3. 5-chunk job, `continue_on_error=False`, chunk 2 exhausts → chunks 3,4 remain `PENDING` (untouched, never attempted) — strengthens WU2-2's strict-branch assertion with the "untouched" check explicitly (scenario: "continue_on_error=False — job pauses immediately, remaining chunks untouched").
4. Budget-exceeded still `PAUSED` regardless of `continue_on_error=True` — confirm the existing budget-check test (test 9 in the M1-8 suite) is unaffected; add an explicit assertion if not already covered for `continue_on_error=True` specifically (scenario: "budget-exceeded still pauses regardless of continue_on_error").

Implement in `borgesica/domain/orchestrator.py`, replacing the `if final_unit is None:` block (~235-247):
```python
if final_unit is None:
    # All retries exhausted — chunk FAILED. Whether the job PAUSES
    # is gated by JobConfig.continue_on_error.
    failed_chunk = chunk.model_copy(update={"status": ChunkStatus.FAILED})
    self._checkpoint.save_chunk(job.id, failed_chunk)
    if not config.continue_on_error:
        paused_job = job.model_copy(
            update={
                "status": JobStatus.PAUSED,
                "cost_usd": running_cost,
                "updated_at": datetime.now(UTC),
            }
        )
        self._checkpoint.save_job(paused_job)
        return paused_job
    # continue_on_error=True: do NOT pause — proceed to the next chunk.
    # Rolling summary is intentionally NOT updated for a FAILED chunk
    # (no summary_update available); next chunk reuses current_summary.
    continue
```
- After the loop ends (all chunks reached a terminal state without an early PAUSED/CANCELLED return), the existing end-of-loop code already sets `job.status = JobStatus.DONE` and calls the writer — confirm this path is reached even when `FAILED` chunks are present (no extra guard needed there; the loop simply exhausts normally). Read the tail of `orchestrator.py`'s `run()` method (after line ~269) before editing to confirm no `all(chunk.status == DONE)` assumption blocks a job with `FAILED` chunks from reaching `DONE` — if such an assumption exists, this is the one place scope may need a one-line adjustment; flag it in the PR description if found, do not silently expand scope elsewhere.

Deliverable: `pytest tests/unit/test_orchestrator.py` → all pass, including both parametrizations of the M1-8 test and the 4 new tests above. Full suite green (`pytest` from repo root).

**Commit**: `feat(domain): continue-on-error gate — FAILED chunk no longer pauses the job by default`

---

## WU-3 — Skip-report reachability (api.py, conditional)

### WU3-1 [x][T] — Check: are failed chunk indices already reachable via `TranslatorEngine`?

**Depends on**: WU2-3
**Spec**: job-lifecycle/"skip report surfaces FAILED chunk indices at end of run" (scenario: status exposes FAILED chunk indices)
**seq** — this is a decision gate, not optional busywork

Before writing any `api.py` code: confirm `TranslatorEngine.status(job_id)` returns `Job` (from `borgesica/domain/models.py`), and `Job` has NO per-chunk field (`total_chunks`/`completed_chunks` are counts, not indices). The only place `FAILED` chunk indices are currently reachable is `CheckpointStore.load_chunks(job_id) -> list[Chunk]`, which `TranslatorEngine` holds as `self._checkpoint` but does NOT expose to callers.

**Verified finding (do this check, do not skip it)**: `TranslatorEngine` in `borgesica/api.py` has no method returning `list[Chunk]` or failed indices — the CLI cannot reach this data through the engine's public surface today. Therefore `api.py` DOES need a new method. This satisfies the proposal's "ONLY if not already reachable" condition — document the one-line finding in the commit message.

Test first (`tests/unit/test_engine.py`):
1. `engine.failed_chunk_indices(job_id)` returns `[]` for a job with zero `FAILED` chunks.
2. After a `continue_on_error=True` run where chunks 2 and 5 ended `FAILED`, `engine.failed_chunk_indices(job_id)` returns `[2, 5]` (sorted, 0-based).
3. `engine.failed_chunk_indices("unknown-id")` raises `JobNotFoundError`.

Implement in `borgesica/api.py`, near `status()` (~line 241):
```python
def failed_chunk_indices(self, job_id: str) -> list[int]:
    """Return the sorted list of chunk indices currently in FAILED status."""
    self._load_job_or_raise(job_id)
    chunks = self._checkpoint.load_chunks(job_id)
    return sorted(c.index for c in chunks if c.status == ChunkStatus.FAILED)
```
Add `ChunkStatus` to the existing `from borgesica.domain.models import (...)` block in `api.py` (currently imports `CostEstimate, Glossary, GlossaryEntry, Job, JobConfig, JobStatus, SourceType` — add `ChunkStatus`).

Deliverable: `pytest tests/unit/test_engine.py` → all pass (baseline + 3 new).

**Commit**: `feat(api): expose failed_chunk_indices — no checkpoint schema change, pure pass-through`

---

## WU-4 — CLI: --strict flag, run skip-summary, status failed-index listing

Three CLI-surface changes, same file, same narrative ("make the contract change visible
to the user") — one work unit, one commit. No strict-TDD test files exist for `__main__.py`
today (M1-13 explicitly noted "no tests required for CLI itself... manual smoke test
sufficient" and the CLI regression test added in M2-0 was the first exception, for a
UnicodeEncodeError bug). Follow existing convention: add a focused regression-style test
only where the skill file's RED→GREEN discipline is practical (argument parsing + output
formatting are easily unit-testable without a real provider), consistent with strict TDD
mode being active.

### WU4-1 [x][T] — `--strict` flag on `create`

**Depends on**: WU1-1
**Spec**: job-lifecycle/"JobConfig.continue_on_error gates chunk-failure handling, default ON" (scenario: --strict flag sets continue_on_error=False)
**par** with WU4-2, WU4-3 (different code regions of the same file — argparse setup, `_cmd_run`, `_cmd_status` — safe to interleave, but land as one commit for narrative coherence)

Test first (new `tests/unit/test_cli.py` if it does not exist yet — check first; if `test_cli.py` already exists from M2-0's UTF-8 regression test, extend it):
1. `_build_parser().parse_args(["create", "book.epub", "--model", "x"])` → `args.strict is False` (default).
2. `_build_parser().parse_args(["create", "book.epub", "--model", "x", "--strict"])` → `args.strict is True`.
3. `_cmd_create` with `args.strict=True` builds a `JobConfig` with `continue_on_error=False`; with `args.strict=False` (or absent), `continue_on_error=True`.

Implement in `borgesica/__main__.py`:
- `_build_parser()`: add `p_create.add_argument("--strict", action="store_true", default=False, help="Pause the job on the first FAILED chunk instead of continuing (restores the pre-continue-on-error contract).")`.
- `_cmd_create()`: `config = JobConfig(..., continue_on_error=not args.strict)`.

Deliverable: `pytest tests/unit/test_cli.py -k strict` → all pass.

**Commit**: (bundled with WU4-2, WU4-3 below)

---

### WU4-2 [x][T] — `run` prints a skip-summary line when chunks failed

**Depends on**: WU3-1 (needs `engine.failed_chunk_indices`)
**Spec**: job-lifecycle/"skip report surfaces FAILED chunk indices at end of run" (scenario: CLI run prints a skip-summary line when chunks failed)
**par** with WU4-1, WU4-3

Test first (`tests/unit/test_cli.py`):
1. `_cmd_run` with a fake `engine` whose `run_job` returns a `DONE` job and `failed_chunk_indices` returns `[2, 5]` → captured stdout contains a line naming count (`2`) and both indices (`2`, `5`).
2. `_cmd_run` with `failed_chunk_indices` returning `[]` → no skip-summary line printed (only the existing "Done. Status=..." line).

Implement in `borgesica/__main__.py::_cmd_run` (~line 263), after the existing `print(f"Done. Status={final_job.status}  cost=${final_job.cost_usd:.5f}")` line:
```python
failed = engine.failed_chunk_indices(args.job_id)
if failed:
    print(f"WARNING: {len(failed)} chunk(s) failed and were skipped: {failed}")
```
Mirror the same addition in `_cmd_resume` (same shape — resume can also finish `DONE` with failed chunks present).

Deliverable: `pytest tests/unit/test_cli.py -k skip_summary` → all pass.

**Commit**: (bundled with WU4-1, WU4-3 below)

---

### WU4-3 [x][T] — `status` lists FAILED chunk indices when present

**Depends on**: WU3-1
**Spec**: job-lifecycle/"skip report surfaces FAILED chunk indices at end of run" (scenario: CLI status lists no failed chunks when none exist)
**par** with WU4-1, WU4-2

Test first (`tests/unit/test_cli.py`):
1. `_cmd_status` with `failed_chunk_indices` returning `[3]` → captured stdout (after the JSON job dump) contains a line listing failed chunk index `3`.
2. `_cmd_status` with `failed_chunk_indices` returning `[]` → no failed-chunk line printed, output is exactly the JSON dump (matches the existing scenario: "CLI status lists no failed chunks when none exist").

Implement in `borgesica/__main__.py::_cmd_status` (~line 287), after `print(job.model_dump_json(indent=2))`:
```python
failed = engine.failed_chunk_indices(args.job_id)
if failed:
    print(f"Failed chunks: {failed}")
```

Deliverable: `pytest tests/unit/test_cli.py` → all pass (full CLI test file green).

**Commit** (covers WU4-1 + WU4-2 + WU4-3): `feat(cli): --strict flag, run/resume skip-summary, status failed-chunk listing`

---

## WU-5 — Full-suite verification (no new code)

### WU5-1 [x][I] — Full regression pass

**Depends on**: WU1-1, WU2-1, WU2-2, WU2-3, WU3-1, WU4-1, WU4-2, WU4-3
**seq** (final gate before handing off to sdd-verify)

Run the complete suite from repo root: `.venv/Scripts/python.exe -m pytest`. Expected: baseline 290 passed, 6 skipped, PLUS all new tests from WU1 through WU4 (approx. +2 model tests, +3 guard tests, +2 parametrized-test cases replacing 1, +4 gate tests, +3 api tests, +7 CLI tests ≈ +19-21 net new passing tests). Zero regressions. Run `ruff check borgesica/` — must exit 0. Run the domain-purity test explicitly (`pytest tests/unit/test_domain_purity.py`) to confirm `orchestrator.py` and `models.py` changes introduced no adapter imports.

No commit — this is a verification checkpoint, not a code change. If any regression surfaces, fix it inside the work unit that caused it (do not create a new WU for cleanup of your own change).

---

## Work Unit → Commit Map

| Work Unit | Commit message | Files touched |
|-----------|-----------------|---------------|
| WU-1 | `feat(domain): add JobConfig.continue_on_error flag, default True` | `borgesica/domain/models.py`, `tests/unit/test_models.py` |
| WU-2 | `feat(domain): prose guard — zero-alphabetic chunks pass through without a provider call` + `feat(domain): continue-on-error gate — FAILED chunk no longer pauses the job by default` | `borgesica/domain/orchestrator.py`, `tests/unit/test_orchestrator.py` |
| WU-3 | `feat(api): expose failed_chunk_indices — no checkpoint schema change, pure pass-through` | `borgesica/api.py`, `tests/unit/test_engine.py` |
| WU-4 | `feat(cli): --strict flag, run/resume skip-summary, status failed-chunk listing` | `borgesica/__main__.py`, `tests/unit/test_cli.py` |
| WU-5 | (no commit — verification only) | none |

Each work unit is independently revertable and leaves the repo in a working, fully-green
state. WU-2 is the only unit with two internal commits (guard, then gate) because they are
separately reviewable behaviors sharing one file/section — either commit alone still leaves
a coherent, green repo.

---

## Review Workload Forecast

Estimated changed lines (implementation + tests, additions + modifications):

| Work unit | Impl LOC (est.) | Test LOC (est.) | Total |
|-----------|------------------|-------------------|-------|
| WU-1 | ~2 | ~15 | ~17 |
| WU-2 | ~25 | ~110 | ~135 |
| WU-3 | ~8 | ~30 | ~38 |
| WU-4 | ~15 | ~60 | ~75 |
| **Total** | **~50** | **~215** | **~265** |

- Estimated changed lines: **~265** (well under the 400-line budget).
- Chained PRs recommended: **No** — the whole change fits comfortably in a single PR; work-unit commits inside one PR give reviewers clean checkpoints without needing to split delivery.
- 400-line budget risk: **Low**.
- Decision needed before apply: **No** — proceed directly to `sdd-apply` under `ask-on-risk` (or any other cached `delivery_strategy`) without a chained-PR conversation; this is a single-PR change by the numbers.
