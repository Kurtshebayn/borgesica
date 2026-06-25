# Verify Report — translation-engine (M1)

Change: translation-engine  
Phase: verify  
Milestone: M1 (SRT walking skeleton, tasks M0-1 through M1-13)  
Mode: Strict TDD  
Artifact store: openspec  
Date: 2026-06-25  

---

## Pytest Result


platform win32 -- Python 3.13.2, pytest-9.1.1
collected 169 items

tests/integration/test_engine_e2e.py          .....       5 passed
tests/integration/test_sqlite_checkpoint.py   ............  12 passed
tests/integration/test_srt_adapters.py        .............  13 passed
tests/unit/test_anthropic_provider.py         ..........s  10 passed, 1 skipped
tests/unit/test_chunking.py                   ...........  11 passed
tests/unit/test_cli.py                        .....        5 passed
tests/unit/test_context.py                    ..........  10 passed
tests/unit/test_cost.py                       .........    9 passed
tests/unit/test_domain_purity.py              .           1 passed
tests/unit/test_engine.py                     .............  13 passed
tests/unit/test_fakes.py                      ...          3 passed
tests/unit/test_glossary.py                   .......      7 passed
tests/unit/test_markup.py                     ...................  19 passed
tests/unit/test_models.py                     ......................  22 passed
tests/unit/test_orchestrator.py               .....................  21 passed
tests/unit/test_ports.py                      .......      7 passed

168 passed, 1 skipped in 1.53s


1 skipped = AnthropicProvider live integration test, gated by ANTHROPIC_API_KEY; correct behavior.  
ruff check borgesica/ -- All checks passed.

---

## Task Completion

All 16 M0+M1 tasks marked complete in tasks.md; confirmed by apply-progress (obs #274, slices 1-6).

| Task | Status |
|------|--------|
| M0-1 pyproject + skeleton | DONE |
| M0-2 pytest + ruff config | DONE |
| M0-3 Test doubles | DONE |
| M1-1 Domain models + errors | DONE |
| M1-2 Ports (Protocols) | DONE |
| M1-3 Markup strip/reinsert/validate | DONE |
| M1-4 SRT chunker | DONE |
| M1-5 ContextManager | DONE |
| M1-6 CostEstimator | DONE |
| M1-7 GlossaryExtractor | DONE |
| M1-8 TranslationOrchestrator | DONE |
| M1-9 SrtReader + SrtWriter | DONE |
| M1-10 SQLiteCheckpointStore | DONE |
| M1-11 AnthropicProvider | DONE |
| M1-12 TranslatorEngine public API | DONE |
| M1-13 Thin CLI | DONE |

---

## Issues

### WARNING

**W-1 — SrtWriter ignores non-default line_length from JobConfig**

Files: borgesica/domain/chunking.py (line 66) and borgesica/adapters/writers/srt_writer.py (lines 120, 133)  
Spec: subtitle-translation / Scenario: configurable line_length is respected

SrtChunker.chunk() builds batch meta as {cue_batches: [...]} but never stores config.line_length. SrtWriter.write() reads meta.get(line_length, 42) and always gets 42 regardless of what JobConfig.line_length was set to. The isolated unit test test_line_length_30_respected passes because it calls reflow(text, 30) directly, bypassing the pipeline. Any real job with JobConfig(line_length=30) silently produces 42-character lines.

Fix: One-line change in SrtChunker.chunk(): change meta={cue_batches: cue_batches} to meta={cue_batches: cue_batches, line_length: config.line_length}.

**W-2 — CostEstimate.cached is always False; spec requires it to reflect prompt-caching eligibility**

File: borgesica/domain/cost.py (line 117)  
Spec: cost-control / Scenario: CostEstimate.cached == True when estimate_cost is called and static block >= 1024 tokens

CostEstimator.estimate() hardcodes cached=False. ContextManager correctly computes SystemPrompt.cached but the signal is never propagated to CostEstimate. The spec explicitly equates the estimate cached flag with static-block caching eligibility.

Fix: estimate() should call context_manager.build_system_prompt() or expose cached via a helper and pass it to CostEstimate.

**W-3 — Tier-2 structured output not implemented; test label misleading**

File: borgesica/adapters/providers/anthropic_provider.py  
Spec: model-provider / three-tier fallback: Tier 1 tool-calling, Tier 2 constrained decoding/JSON mode, Tier 3 prompt-and-parse

The implementation goes from Tier-1 (tool-calling) directly to Tier-3 (text JSON parse + retry). Tier-2 (Anthropic JSON mode) is not implemented. The apply-progress notes this as a deliberate choice (tool-use is sufficient, avoids extra dependency) but it is not documented as a known spec deviation. Behavior is functionally correct.

### SUGGESTION

**S-1** — test_three_line_fallback asserts 1<=lines<=3, not exactly 3. Should be len(lines)==3 to prove the fallback fires.

**S-2** — Orchestrator uses cost projection for actual chunk cost, not real token counts from response.usage. Will diverge from Anthropic billing in production. Track for M2.

**S-3** — Reflow 3-line fallback may exceed line_length on the merged last line when textwrap produces 4+ lines. Not tested. Acknowledged in code comment.

---

## Deferred by Design

| Capability | Spec File | Milestone |
|------------|-----------|-----------|
| EPUB round-trip (EpubReader, EpubWriter) | book-translation | M2 |
| PDF support (PdfPlumberReader) | book-translation | M3 |
| Prose chunker (chunk_prose) | book-translation | M2 |
| Golden fixtures (tests/golden/) | quality-evaluation | M4 |
| LLM-as-judge harness | quality-evaluation | M4 |
| Back-translation regression check | quality-evaluation | M4 |
| OllamaProvider adapter | quality-evaluation | M4 |
| Eval harness scores neutral-register | context-continuity | M4 |
| Calque golden sample | translation-quality | M4 |
| Ollama/LiteLLM adapters | model-provider | M4 |

---

## M1 Verdict: PASS WITH WARNINGS

CRITICAL: 0  
WARNING: 3  
SUGGESTION: 3  

W-1 is a silent behavioral bug (non-42 line_length ignored in full pipeline; 1-line fix).  
W-2 is a spec-compliance gap (CostEstimate.cached always False).  
W-3 is a documentation gap; adapter behavior is functionally correct.  

None of the warnings cause test suite failures. The M1 walking skeleton is functionally complete.  
Next recommended: sdd-archive (if W-1 and W-2 accepted as known issues) or targeted apply for W-1 fix first.