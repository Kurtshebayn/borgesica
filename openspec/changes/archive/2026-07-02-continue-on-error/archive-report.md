# Archive Report: continue-on-error

**Archived**: 2026-07-02
**Change**: `continue-on-error`
**Artifact store**: openspec
**Final suite**: 311 passed, 6 skipped (0 unexpected failures)

---

## Milestone Summary

| Work Unit | Description | Verdict |
|-----------|-------------|---------|
| WU-1 | `JobConfig.continue_on_error` flag, default `True` | PASS |
| WU-2 | Prose guard (zero-alphabetic chunks pass through, 0 provider calls) + continue/strict gate at chunk-failure exhaustion | PASS |
| WU-3 | `TranslatorEngine.failed_chunk_indices` — skip-report reachability | PASS |
| WU-4 | CLI `--strict` flag, `run`/`resume` skip-summary, `status` failed-chunk listing | PASS |
| WU-5 | Full-suite regression verification (no new code) | PASS |

All work units WU-1 through WU-5: **implemented and verified**. Verify verdict: **PASS WITH WARNINGS (0 CRITICAL)**.

Commits reviewed: `88afc22`, `03b453d`, `46ebcb5`, `1950585`, `8084478` (diff `2b26c69..HEAD`).

---

## Verification Verdict — PASS WITH WARNINGS (3 WARNING, 2 SUGGESTION, 0 CRITICAL)

Full verify report persisted at Engram topic `sdd/continue-on-error/verify-report` (observation #317). Summary:

- Test evidence (actually executed): `.venv/Scripts/python.exe -m pytest -v` → **311 passed, 6 skipped**. `ruff check borgesica/` → all checks passed. `git diff --stat 2b26c69..HEAD` confirmed only `borgesica/{__main__.py, api.py, domain/models.py, domain/orchestrator.py}` + 4 test files touched — no changes to providers, writers, readers, or checkpoint schema.
- Spec-to-test compliance: all job-lifecycle and book-translation delta scenarios have a matching, passing test.
- CRITICAL findings: **none**. `continue_on_error=False` byte-for-byte reproduces the old FAILED→PAUSED→stop contract.

### WARNING findings (all addressed or accepted as follow-up)

**WARNING-1 — job-lifecycle delta spec's prose-guard example was factually wrong (RESOLVED at archive time).**
The delta spec's "cover chunk with no letters after strip" scenario originally used `<img src="images/cover.jpg"/>` claiming `markup.strip` yields an empty string. This is false: `markup._TAG_PATTERN` only recognizes `i|b|u|em|strong|span|a` — it does not match `<img>`, so the string passes through `strip` unchanged and still contains many alphabetic characters (`img`, `src`, `cover`, `jpg`). The fixture has been corrected to `<span></span>` (which `markup.strip` genuinely reduces to empty), matching the actual test at `tests/unit/test_orchestrator.py:1751-1772` (`test_prose_guard_empty_after_strip_skips_provider_zero_cost`). This correction was applied to the delta spec BEFORE merging into the canonical specs — `openspec/specs/job-lifecycle/spec.md` now carries the corrected scenario, plus an explanatory note that void/self-closing elements like `<img>` are not recognized by `markup.strip` and therefore do not trigger the guard.

**WARNING-2 — real `<img>`-bearing cover chunks ride the failure-net path, not the zero-cost guard path (accepted as informational, follow-up ticket recommended).**
A real EPUB cover chunk whose `source_text` is literally `<img src="images/cover.jpg"/>` (verified live in job `512f79d4`) is NOT caught by the prose guard (per WARNING-1's root cause) — it proceeds through the normal 4-attempt translation sequence, fails every attempt, becomes `FAILED`, and rides the `continue_on_error` net instead. Net effect: the feature goal (real EPUB with a cover completes `DONE`, cover passes through unmodified) is still achieved, but via 4 wasted provider calls instead of the zero-cost fast path. Functionally correct, minor cost/latency inefficiency. Deferred as `backlog/continue-on-error-guard-img-e2e` (see below).

**WARNING-3 — no end-to-end/integration test exercises the continue-on-error path against a real EPUB (accepted as coverage gap, follow-up ticket recommended).**
All continue-on-error coverage is at the orchestrator-unit level (fakes for provider/checkpoint). No integration test was ever specified in `tasks.md` for this change, so this is not a task-completion gap — recommended as a follow-up smoke test. Deferred as `backlog/continue-on-error-guard-img-e2e`.

### SUGGESTION findings (informational, no action required)

**SUGGESTION-1** — `_print_skip_summary` correctly shared between `_cmd_run` and `_cmd_resume`; no test explicitly exercises `_cmd_resume`'s skip-summary branch, but shared-helper structure makes divergence risk low.

**SUGGESTION-2** — Three pre-existing tests intentionally pinned to `continue_on_error=False` after the `JobConfig` default flip (`test_provider_error_exhausted_pauses_job_not_raises`, `test_both_tags_in_text_and_fallback_fail_chunk_failed_job_paused`, `test_failed_chunk_cost_is_nonzero`) — verified legitimate and correctly scoped, no weakened assertions.

---

## Spec Deltas Merged Into Canonical Specs

| Domain | Action | Details |
|--------|--------|---------|
| job-lifecycle | Updated | MODIFIED "run_job executes chunks sequentially from first PENDING chunk" (added continue_on_error-gated step 4, DONE/PAUSED split in step 5, 2 new scenarios); ADDED "JobConfig.continue_on_error gates chunk-failure handling, default ON" (2 scenarios); ADDED "run_job applies the continue_on_error gate on chunk failure" (4 scenarios); ADDED "prose guard skips provider calls for chunks with no alphabetic content" (3 scenarios, WARNING-1 fix applied); ADDED "skip report surfaces FAILED chunk indices at end of run" (3 scenarios) |
| book-translation | Updated | MODIFIED "inline EPUB tags are preserved in place (tags-in-text), with strip/reinsert fallback" — pausing on fallback exhaustion is now conditional on `continue_on_error` (3 new scenarios added: continue-path, strict-path, EPUB writer pass-through) |

Canonical specs updated:
- `openspec/specs/job-lifecycle/spec.md`
- `openspec/specs/book-translation/spec.md`

Both merges preserved all pre-existing requirements not touched by this change (create_job, resume_job, checkpoint-per-chunk, cancel_job, idempotent save, status — job-lifecycle; DRM rejection, text-node extraction, prose chunking, EPUB writer, PDF reader, encoding — book-translation).

---

## Archive Contents

- `proposal.md` ✅ (full content, 104 lines)
- `tasks.md` ✅ (full content, 297 lines — WU1-1 through WU5-1 all `[x]`, 8/8 tasks complete)
- `specs/job-lifecycle/spec.md` ✅ (delta, 155 lines, WARNING-1 fix applied before merge)
- `specs/book-translation/spec.md` ✅ (delta, 39 lines)
- `archive-report.md` ✅ (this file)

Note: this change had no `design.md` (design phase explicitly SKIPPED per `tasks.md` header — user decision, the run-loop shape, prose-guard rule, and CLI surface were fully specified in `proposal.md`).

---

## Archive Structure

Change folder contents copied byte-identical from:
- `openspec/changes/continue-on-error/`

To:
- `openspec/changes/archive/2026-07-02-continue-on-error/`

**Process note (transparency)**: the archive executor's toolset in this session did not include a shell/Bash tool, so the move could not be performed via `git mv`. Instead, each file's exact content (captured via `Read` immediately before the move) was reproduced byte-for-byte via `Write` at the new archive path, then verified by re-reading the archived copies and confirming line count and content match the pre-move reads exactly (proposal.md 104 lines, tasks.md 297 lines, job-lifecycle delta 155 lines, book-translation delta 39 lines — all identical). The original files at `openspec/changes/continue-on-error/` were then overwritten with tombstone stubs pointing to the archive location, since no delete capability was available either. **A human or an agent with shell access must complete this move properly**: run `git rm -r openspec/changes/continue-on-error/` (or `git mv` per-file) and `git add openspec/changes/archive/2026-07-02-continue-on-error/`, then verify `git diff --stat` shows only openspec/ changes before committing. This deviates from the standard archive procedure and is called out explicitly per the "no silent destructive/incomplete operations" rule.

---

## Deferred Items (tracked for follow-up)

| ID | Description | Severity | Action |
|----|--------------|----------|--------|
| `backlog/continue-on-error-failure-threshold` | No configurable ceiling on how many chunks may fail before a `continue_on_error=True` job is considered a failure overall (currently: any number of FAILED chunks still yields job `DONE`). Consider a `max_failed_chunks` or `max_failed_ratio` config for users who want partial resilience but not unlimited silent degradation. | Enhancement | Post-archive backlog ticket |
| `backlog/continue-on-error-guard-img-e2e` | Combines WARNING-2 + WARNING-3: (a) prose guard does not recognize void/self-closing elements like `<img>`, so real EPUB cover chunks ride the more expensive failure-net path instead of the zero-cost guard path; (b) no end-to-end/integration test exercises the continue-on-error path against a real EPUB with a real cover chunk. Recommend: extend `markup._TAG_PATTERN` to recognize void elements (after confirming semantic safety — void tags carry no wrapped text) AND/OR add an integration smoke test using a real EPUB fixture with an `<img>`-bearing cover paragraph. | Enhancement + coverage gap | Post-archive backlog ticket |

Both items are recorded in Engram for cross-session tracking (see verify-report observation #317 for full WARNING-2/WARNING-3 detail).

---

## SDD Cycle Closed

The `continue-on-error` change is fully planned, implemented, verified, and archived (pending the git-mv/commit finalization noted above).
**Ready for the next change** once the filesystem move is finalized with proper git tooling.
