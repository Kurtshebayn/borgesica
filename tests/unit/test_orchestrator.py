"""Tests for TranslationOrchestrator (M1-8).

Strict TDD — all 21 spec scenarios.

Test categories:
  1-4:   Job lifecycle (basic run, progress callbacks, state guard, persist-before-next)
  5-8:   Resume (skip DONE, rebuild summary, all-done short-circuit, resume-from-CANCELLED)
  9-10:  Budget hard-stop
  11-12: Cancel cooperative flag
  13-15: Reflection mode (fast=1 call, reflective=3 calls, revise text persisted)
  16-17: Tag-mismatch retry (succeed on 2nd, fail all 3 -> FAILED + PAUSED)
  18-19: Glossary mid-run merge (new term staged, locked entry not overridden)
  20-21: Rolling summary threading (chunk N uses N-1 summary, first chunk uses placeholder)
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

import pytest

from borgesica.domain.context import ContextManager
from borgesica.domain.cost import CostEstimator
from borgesica.domain.errors import BudgetExceeded, JobStateError, MalformedOutput
from borgesica.domain.models import (
    Chunk,
    ChunkStatus,
    Glossary,
    GlossaryEntry,
    Job,
    JobConfig,
    JobStatus,
    Progress,
    RollingSummary,
    SourceType,
    TranslationResult,
    TranslationUnit,
    Usage,
)
from borgesica.domain.orchestrator import TranslationOrchestrator
from tests.fakes import FakeCorpusStore, FakeTranslationProvider, InMemoryCheckpointStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(
    quality_mode: str = "fast",
    budget_usd: float | None = None,
    continue_on_error: bool = True,
) -> JobConfig:
    return JobConfig(
        source_type=SourceType.SRT,
        model="claude-test",
        quality_mode=quality_mode,  # type: ignore[arg-type]
        budget_usd=budget_usd,
        continue_on_error=continue_on_error,
    )


def make_job(config: JobConfig, total: int = 3) -> Job:
    now = datetime.now(UTC)
    return Job(
        id="job-001",
        config=config,
        source_path="test.srt",
        status=JobStatus.CREATED,
        total_chunks=total,
        completed_chunks=0,
        cost_usd=0.0,
        created_at=now,
        updated_at=now,
    )


def make_chunks(n: int, status: ChunkStatus = ChunkStatus.PENDING) -> list[Chunk]:
    return [Chunk(index=i, source_text=f"Chunk {i} text.", status=status) for i in range(n)]


def make_orchestrator(
    provider: FakeTranslationProvider | None = None,
    store: InMemoryCheckpointStore | None = None,
    corpus_store: FakeCorpusStore | None = None,
    provider_name: str = "unknown",
) -> tuple[TranslationOrchestrator, FakeTranslationProvider, InMemoryCheckpointStore]:
    if provider is None:
        provider = FakeTranslationProvider()
    if store is None:
        store = InMemoryCheckpointStore()
    ctx = ContextManager(provider)
    cost_est = CostEstimator(provider)
    orch = TranslationOrchestrator(
        provider=provider,
        checkpoint=store,
        context_manager=ctx,
        cost_estimator=cost_est,
        provider_name=provider_name,
        corpus_store=corpus_store,
    )
    return orch, provider, store


def run_job(
    orch: TranslationOrchestrator,
    job: Job,
    chunks: list[Chunk],
    glossary: Glossary | None = None,
    config: JobConfig | None = None,
    on_progress: Callable[[Progress], None] | None = None,
    cancel_flag: threading.Event | None = None,
    store: InMemoryCheckpointStore | None = None,
) -> Job:
    """Helper that seeds the store and calls orch.run()."""
    if glossary is None:
        glossary = Glossary()
    if config is None:
        config = job.config
    if cancel_flag is None:
        cancel_flag = threading.Event()
    if store is not None:
        store.save_job(job)
        for c in chunks:
            store.save_chunk(job.id, c)
        store.save_glossary(job.id, glossary)
    return orch.run(
        job=job,
        chunks=chunks,
        glossary=glossary,
        config=config,
        on_progress=on_progress or (lambda p: None),
        cancel_flag=cancel_flag,
    )


# ===========================================================================
# 1. run_job with 3 chunks — provider called exactly 3 times, job ends DONE
# ===========================================================================


def test_run_job_basic_3_chunks_done():
    orch, provider, store = make_orchestrator()
    config = make_config()
    job = make_job(config, total=3)
    chunks = make_chunks(3)

    result = run_job(orch, job, chunks, store=store)

    assert provider.call_count == 3
    assert result.status == JobStatus.DONE


# ===========================================================================
# 2. on_progress called once per completed chunk
# ===========================================================================


def test_on_progress_called_per_chunk():
    orch, provider, store = make_orchestrator()
    config = make_config()
    job = make_job(config, total=4)
    chunks = make_chunks(4)

    progress_events: list[Progress] = []
    run_job(orch, job, chunks, on_progress=progress_events.append, store=store)

    assert len(progress_events) == 4
    # Each progress has the correct chunk_index (in order)
    for i, p in enumerate(progress_events):
        assert p.chunk_index == i


# ===========================================================================
# 3. run_job on RUNNING job raises JobStateError, 0 provider calls
# ===========================================================================


def test_run_job_on_running_raises_state_error():
    orch, provider, store = make_orchestrator()
    config = make_config()
    job = make_job(config, total=3)
    job = job.model_copy(update={"status": JobStatus.RUNNING})
    chunks = make_chunks(3)

    with pytest.raises(JobStateError):
        run_job(orch, job, chunks, store=store)

    assert provider.call_count == 0


# ===========================================================================
# 4. Chunk N persisted DONE before chunk N+1 called
#    Provider raises MalformedOutput on call 2 → chunk 0 DONE, chunks 1-2 PENDING
# ===========================================================================


def test_transient_provider_error_is_retried_and_job_completes():
    """A transient provider failure on one chunk is RETRIED (not fatal): the job
    completes and no exception propagates. Chunk 0 is persisted DONE before the
    failing chunk is attempted.

    Resilience contract (post-live-DeepSeek): a single flaky chunk must never
    crash the whole run or strand the job in RUNNING.
    """
    store = InMemoryCheckpointStore()
    provider = FakeTranslationProvider(fail_on={1})  # 2nd provider call raises once
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=3)
    chunks = make_chunks(3)  # no tags → validate_tags always passes

    result = run_job(orch, job, chunks, store=store)  # must NOT raise

    assert result.status == JobStatus.DONE
    saved = {c.index: c for c in store.load_chunks(job.id)}
    assert saved[0].status == ChunkStatus.DONE
    assert saved[1].status == ChunkStatus.DONE  # transient failure healed by retry
    # chunk0(call0) + chunk1 fail(call1)+retry(call2) + chunk2(call3) = 4 calls.
    assert provider.call_count == 4


def test_provider_error_exhausted_pauses_job_not_raises():
    """When the provider fails ALL primary attempts AND the fallback call, the
    chunk is FAILED and the job PAUSED (resumable) — never a raw exception that
    would strand the job in RUNNING. Uses continue_on_error=False (strict) —
    this test exercises the pre-continue-on-error PAUSE contract specifically;
    the continue-on-error=True gate behavior is covered separately.
    """
    from borgesica.domain.errors import MalformedOutput

    class AlwaysRaiseProvider(FakeTranslationProvider):
        def translate(self, system: str, user: str, model: str):
            self.call_log.append((system, user, model))
            raise MalformedOutput(job_id="x", chunk_index=-1)

    store = InMemoryCheckpointStore()
    provider = AlwaysRaiseProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config(continue_on_error=False)
    job = make_job(config, total=2)
    chunks = make_chunks(2)

    result = run_job(orch, job, chunks, store=store)  # must NOT raise

    assert result.status == JobStatus.PAUSED
    saved = {c.index: c for c in store.load_chunks(job.id)}
    assert saved[0].status == ChunkStatus.FAILED
    assert saved[1].status == ChunkStatus.PENDING  # never reached
    # chunk 0: 3 primary attempts + 1 fallback call = 4, then stop.
    assert provider.call_count == 4


# ===========================================================================
# 5. Resume: 3 DONE + 2 PENDING → provider called exactly 2 times
# ===========================================================================


def test_resume_skips_done_chunks():
    store = InMemoryCheckpointStore()
    provider = FakeTranslationProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=5)
    chunks = make_chunks(5)

    # Mark first 3 as DONE with translated text
    for i in range(3):
        chunks[i] = chunks[i].model_copy(
            update={"status": ChunkStatus.DONE, "translated_text": f"Done {i}."}
        )
        store.save_summary(job.id, RollingSummary(text=f"Summary {i}", chunk_index=i))

    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, Glossary())

    result = orch.run(
        job=job,
        chunks=chunks,
        glossary=Glossary(),
        config=config,
        on_progress=lambda p: None,
        cancel_flag=threading.Event(),
    )

    assert provider.call_count == 2
    assert result.status == JobStatus.DONE


# ===========================================================================
# 6. Resume rebuilds rolling summary from highest DONE summary
#    Chunk 2's summary must appear in chunk 3's system prompt
# ===========================================================================


def test_resume_rebuilds_summary_from_highest_done():
    store = InMemoryCheckpointStore()
    provider = FakeTranslationProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=4)
    chunks = make_chunks(4)

    # Mark chunks 0-2 as DONE; chunk 2 has a known summary
    for i in range(3):
        chunks[i] = chunks[i].model_copy(
            update={"status": ChunkStatus.DONE, "translated_text": f"Done {i}."}
        )
        summary_text = f"Summary from chunk {i}"
        store.save_summary(job.id, RollingSummary(text=summary_text, chunk_index=i))

    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, Glossary())

    orch.run(
        job=job,
        chunks=chunks,
        glossary=Glossary(),
        config=config,
        on_progress=lambda p: None,
        cancel_flag=threading.Event(),
    )

    # The system prompt for chunk 3 (index 3) must contain "Summary from chunk 2"
    assert provider.call_count == 1
    system_for_chunk3 = provider.call_log[0][0]  # system is first element
    assert "Summary from chunk 2" in system_for_chunk3


# ===========================================================================
# 7. Resume with all chunks DONE → 0 provider calls, job status DONE
# ===========================================================================


def test_resume_all_done_no_provider_calls():
    store = InMemoryCheckpointStore()
    provider = FakeTranslationProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=3)
    chunks = make_chunks(3)

    for i in range(3):
        chunks[i] = chunks[i].model_copy(
            update={"status": ChunkStatus.DONE, "translated_text": f"Done {i}."}
        )

    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, Glossary())

    result = orch.run(
        job=job,
        chunks=chunks,
        glossary=Glossary(),
        config=config,
        on_progress=lambda p: None,
        cancel_flag=threading.Event(),
    )

    assert provider.call_count == 0
    assert result.status == JobStatus.DONE


# ===========================================================================
# 8. Resume from CANCELLED state is accepted — translates PENDING chunks
# ===========================================================================


def test_resume_from_cancelled_accepted():
    store = InMemoryCheckpointStore()
    provider = FakeTranslationProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=3)
    job = job.model_copy(update={"status": JobStatus.CANCELLED})
    chunks = make_chunks(3)

    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, Glossary())

    result = orch.run(
        job=job,
        chunks=chunks,
        glossary=Glossary(),
        config=config,
        on_progress=lambda p: None,
        cancel_flag=threading.Event(),
    )

    assert provider.call_count == 3
    assert result.status == JobStatus.DONE


# ===========================================================================
# 9. Budget hard-stop BEFORE the call that would exceed
#    8 chunks done at $0.93, chunk 9 projected at $0.10 → BudgetExceeded
# ===========================================================================


def test_budget_hard_stop_before_exceeding_call():
    """Budget $1.00; cost so far = $0.93; next chunk would cost ~$0.10 → stop."""
    store = InMemoryCheckpointStore()
    provider = FakeTranslationProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config(budget_usd=1.00)
    job = make_job(config, total=9)
    job = job.model_copy(update={"cost_usd": 0.93})

    # 8 DONE chunks + 1 PENDING
    # Budget remaining = $1.00 - $0.93 = $0.07
    # FakeProvider: $1/Mtok in, $5/Mtok out, output_tokens=150
    # To trigger stop: projected input cost alone must exceed $0.07
    # Need > 70_000 input tokens → use 80_000 words
    chunks = []
    for i in range(8):
        chunks.append(
            Chunk(
                index=i,
                source_text="x " * 100,  # 100 words → 100 tokens input
                status=ChunkStatus.DONE,
                translated_text="done",
            )
        )
    # chunk 8 is PENDING with 80_000 tokens → projected input cost = $0.08 > $0.07 remaining
    chunks.append(
        Chunk(
            index=8,
            source_text="word " * 80000,  # 80_000 tokens → $0.08 input cost
            status=ChunkStatus.PENDING,
        )
    )

    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, Glossary())

    with pytest.raises(BudgetExceeded) as exc_info:
        orch.run(
            job=job,
            chunks=chunks,
            glossary=Glossary(),
            config=config,
            on_progress=lambda p: None,
            cancel_flag=threading.Event(),
        )

    exc = exc_info.value
    # Completed cost preserved, 0 extra provider calls
    assert provider.call_count == 0
    assert exc.cost_so_far == pytest.approx(0.93)
    assert exc.job_id == "job-001"


# ===========================================================================
# 10. budget_usd=None → no budget check, job completes
# ===========================================================================


def test_no_budget_check_when_none():
    orch, provider, store = make_orchestrator()
    config = make_config(budget_usd=None)
    job = make_job(config, total=5)
    chunks = make_chunks(5)

    result = run_job(orch, job, chunks, store=store)

    assert result.status == JobStatus.DONE
    assert provider.call_count == 5


# ===========================================================================
# 11. cancel_job: after chunk 1 starts → chunk 1 completes+persists,
#     chunk 2 never called, job CANCELLED
# ===========================================================================


def test_cancel_after_first_chunk():
    store = InMemoryCheckpointStore()
    cancel_flag = threading.Event()

    progress_received: list[Progress] = []

    def on_progress(p: Progress) -> None:
        progress_received.append(p)
        # Set cancel after chunk 0 completes
        if p.chunk_index == 0:
            cancel_flag.set()

    provider = FakeTranslationProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=3)
    chunks = make_chunks(3)

    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, Glossary())

    result = orch.run(
        job=job,
        chunks=chunks,
        glossary=Glossary(),
        config=config,
        on_progress=on_progress,
        cancel_flag=cancel_flag,
    )

    # Chunk 0 completes (1 provider call); chunk 1 never called
    assert provider.call_count == 1
    assert result.status == JobStatus.CANCELLED

    # Chunk 0 must be persisted as DONE
    saved = {c.index: c for c in store.load_chunks(job.id)}
    assert saved[0].status == ChunkStatus.DONE


# ===========================================================================
# 12. Cooperative flag checked per chunk (not mid-chunk)
# ===========================================================================


def test_cancel_flag_checked_per_chunk_not_mid_chunk():
    """Cancel set before run → zero chunks processed."""
    store = InMemoryCheckpointStore()
    cancel_flag = threading.Event()
    cancel_flag.set()  # already set before run starts

    provider = FakeTranslationProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=3)
    chunks = make_chunks(3)

    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, Glossary())

    result = orch.run(
        job=job,
        chunks=chunks,
        glossary=Glossary(),
        config=config,
        on_progress=lambda p: None,
        cancel_flag=cancel_flag,
    )

    assert provider.call_count == 0
    assert result.status == JobStatus.CANCELLED


# ===========================================================================
# 13. quality_mode="fast" → exactly 1 provider call per chunk
# ===========================================================================


def test_fast_mode_one_call_per_chunk():
    orch, provider, store = make_orchestrator()
    config = make_config(quality_mode="fast")
    job = make_job(config, total=3)
    chunks = make_chunks(3)

    run_job(orch, job, chunks, store=store)

    assert provider.call_count == 3


# ===========================================================================
# 14. quality_mode="reflective" → exactly 3 provider calls per chunk
# ===========================================================================


def test_reflective_mode_three_calls_per_chunk():
    # Canned single-segment output: the default echo fake would return the
    # composite critique/revise PROMPT as the translation, violating the
    # "\n\n" segment contract and triggering retries — an artifact no real
    # model exhibits (a real translation mirrors the source's segments).
    canned = FakeTranslationProvider(
        canned_unit=TranslationUnit(translation="Texto traducido.", summary_update="Summary.")
    )
    orch, provider, store = make_orchestrator(provider=canned)
    config = make_config(quality_mode="reflective")
    job = make_job(config, total=3)
    chunks = make_chunks(3)

    run_job(orch, job, chunks, store=store)

    assert provider.call_count == 9  # 3 chunks × 3 calls


# ===========================================================================
# 15. Reflective: persisted translated_text is the REVISE step's output
# ===========================================================================


def test_reflective_persisted_text_is_revise_output():
    """The REVISE step's translation must be what ends up in chunk.translated_text."""
    store = InMemoryCheckpointStore()

    call_sequence: list[str] = []

    class SequencedProvider(FakeTranslationProvider):
        def translate(self, system: str, user: str, model: str) -> TranslationResult:
            n = len(self.call_log)
            self.call_log.append((system, user, model))
            # call 0 = draft, call 1 = critique, call 2 = revise
            step = n % 3
            if step == 0:
                unit = TranslationUnit(
                    translation="DRAFT_TEXT",
                    summary_update="Draft summary.",
                )
            elif step == 1:
                unit = TranslationUnit(
                    translation="CRITIQUE_TEXT",
                    summary_update="Critique summary.",
                )
            else:
                unit = TranslationUnit(
                    translation="REVISED_TEXT",
                    summary_update="Revised summary.",
                )
            in_tok = self.count_tokens(system + " " + user, model)
            out_tok = self.count_tokens(unit.translation, model)
            return TranslationResult(unit=unit, usage=Usage(input_tokens=in_tok, output_tokens=out_tok))

    provider = SequencedProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config(quality_mode="reflective")
    job = make_job(config, total=1)
    chunks = make_chunks(1)

    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, Glossary())

    orch.run(
        job=job,
        chunks=chunks,
        glossary=Glossary(),
        config=config,
        on_progress=lambda p: None,
        cancel_flag=threading.Event(),
    )

    saved = store.load_chunks(job.id)
    assert saved[0].translated_text == "REVISED_TEXT"


# ===========================================================================
# 15b. REL-003: reflective partial cost must survive an exception mid-sequence
# ===========================================================================


@dataclass
class ReflectiveStepFailProvider(FakeTranslationProvider):
    """Reflective provider whose Nth call (0=draft, 1=critique, 2=revise)
    raises MalformedOutput. Every SUCCESSFUL call bills a fixed Usage(10, 5)
    so the accrued cost of prior steps is known exactly."""

    fail_step: int = 1

    def translate(  # type: ignore[override]
        self, system: str, user: str, model: str, segment_count: int | None = None
    ) -> TranslationResult:
        n = len(self.call_log)
        self.call_log.append((system, user, model))
        self.segment_count_log.append(segment_count)
        if n == self.fail_step:
            # Transport-style failure: the exception itself carries ZERO usage,
            # so any usage on it must come from the prior successful step(s).
            raise MalformedOutput(job_id="fake-job", chunk_index=n)
        unit = TranslationUnit(translation=f"step{n} text", summary_update="s")
        return TranslationResult(unit=unit, usage=Usage(input_tokens=10, output_tokens=5))


def test_reflective_carries_draft_cost_when_critique_raises():
    """Draft succeeds, critique (2nd call) raises: the draft call was billed,
    so its Usage MUST be carried on the propagated exception. Otherwise the
    caller accrues exc.usage (zero here) and the draft's real spend is lost,
    undercounting running_cost."""
    provider = ReflectiveStepFailProvider(fail_step=1)
    orch, _, _ = make_orchestrator(provider=provider)
    config = make_config(quality_mode="reflective")

    with pytest.raises(MalformedOutput) as excinfo:
        orch._translate_reflective(
            system="sys",
            user="hello world",
            config=config,
            in_price=1.0,
            out_price=5.0,
        )

    # Draft billed Usage(10, 5); nothing else succeeded.
    assert excinfo.value.usage == Usage(input_tokens=10, output_tokens=5)


def test_reflective_carries_draft_and_critique_cost_when_revise_raises():
    """Draft + critique succeed, revise (3rd call) raises: both prior billed
    calls' Usage MUST be carried on the propagated exception."""
    provider = ReflectiveStepFailProvider(fail_step=2)
    orch, _, _ = make_orchestrator(provider=provider)
    config = make_config(quality_mode="reflective")

    with pytest.raises(MalformedOutput) as excinfo:
        orch._translate_reflective(
            system="sys",
            user="hello world",
            config=config,
            in_price=1.0,
            out_price=5.0,
        )

    # Draft + critique each billed Usage(10, 5) → summed.
    assert excinfo.value.usage == Usage(input_tokens=20, output_tokens=10)


# ===========================================================================
# 16. Tag mismatch: fail on 1st, succeed on 2nd → 2 provider calls, chunk DONE
# ===========================================================================


def test_tag_mismatch_retry_succeeds_on_second():
    """Provider returns mismatched tags on 1st call, correct tags on 2nd."""
    store = InMemoryCheckpointStore()

    call_count_ref: list[int] = [0]

    class TagMismatchProvider(FakeTranslationProvider):
        def translate(self, system: str, user: str, model: str) -> TranslationResult:
            n = len(self.call_log)
            self.call_log.append((system, user, model))
            if n == 0:
                # First call: translation has mismatched tags (extra <i>)
                unit = TranslationUnit(
                    translation="<i>Texto</i> <i>extra</i>",
                    summary_update="Summary.",
                )
            else:
                # Second call: correct — no tags (original source also has no tags)
                unit = TranslationUnit(
                    translation="Texto correcto.",
                    summary_update="Summary.",
                )
            in_tok = self.count_tokens(system + " " + user, model)
            out_tok = self.count_tokens(unit.translation, model)
            return TranslationResult(unit=unit, usage=Usage(input_tokens=in_tok, output_tokens=out_tok))

    provider = TagMismatchProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=1)
    # Chunk with NO tags in source so mismatch is detectable
    chunks = [Chunk(index=0, source_text="Plain text without tags.")]

    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, Glossary())

    result = orch.run(
        job=job,
        chunks=chunks,
        glossary=Glossary(),
        config=config,
        on_progress=lambda p: None,
        cancel_flag=threading.Event(),
    )

    assert provider.call_count == 2
    saved = store.load_chunks(job.id)
    assert saved[0].status == ChunkStatus.DONE
    assert result.status == JobStatus.DONE


# ===========================================================================
# 16b. Segment-count mismatch: the model merged/split "\n\n" paragraphs.
#
# The "\n\n" segment count is part of the output contract: writers map
# segments back to document nodes positionally, so a merged paragraph
# desynchronizes every node after the divergence point (observed live:
# DeepSeek chunks 0/8/11/13/15, one real paragraph-join in human review).
#
# Contract: retry for a compliant output; if every attempt is tag-valid but
# segment-mismatched, ACCEPT the last attempt (the writer's defensive mapping
# is the safety net) — the chunk must NOT fail and the strip/reinsert
# fallback (which exists for TAG failures) must NOT be invoked.
# ===========================================================================


def test_segment_mismatch_retry_succeeds_on_second():
    """Provider merges paragraphs on 1st call, respects "\n\n" on 2nd."""
    store = InMemoryCheckpointStore()

    class SegmentMergeOnceProvider(FakeTranslationProvider):
        def translate(self, system: str, user: str, model: str) -> TranslationResult:
            n = len(self.call_log)
            self.call_log.append((system, user, model))
            if n == 0:
                # First call: two source paragraphs merged into ONE segment
                unit = TranslationUnit(
                    translation="Párrafo uno. Párrafo dos.",
                    summary_update="Summary.",
                )
            else:
                # Second call: segment count matches the source (2)
                unit = TranslationUnit(
                    translation="Párrafo uno.\n\nPárrafo dos.",
                    summary_update="Summary.",
                )
            in_tok = self.count_tokens(system + " " + user, model)
            out_tok = self.count_tokens(unit.translation, model)
            return TranslationResult(unit=unit, usage=Usage(input_tokens=in_tok, output_tokens=out_tok))

    provider = SegmentMergeOnceProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=1)
    chunks = [Chunk(index=0, source_text="Paragraph one.\n\nParagraph two.")]

    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, Glossary())

    result = orch.run(
        job=job,
        chunks=chunks,
        glossary=Glossary(),
        config=config,
        on_progress=lambda p: None,
        cancel_flag=threading.Event(),
    )

    assert provider.call_count == 2
    saved = store.load_chunks(job.id)
    assert saved[0].status == ChunkStatus.DONE
    assert saved[0].translated_text == "Párrafo uno.\n\nPárrafo dos."
    assert result.status == JobStatus.DONE


def test_segment_mismatch_all_attempts_accepts_best_effort():
    """Provider ALWAYS merges paragraphs: after 3 tag-valid attempts the last
    one is accepted — chunk DONE (not FAILED), no strip/reinsert fallback call."""
    store = InMemoryCheckpointStore()

    class AlwaysMergeProvider(FakeTranslationProvider):
        def translate(self, system: str, user: str, model: str) -> TranslationResult:
            self.call_log.append((system, user, model))
            unit = TranslationUnit(
                translation="Párrafo uno. Párrafo dos.",
                summary_update="Summary.",
            )
            in_tok = self.count_tokens(system + " " + user, model)
            out_tok = self.count_tokens(unit.translation, model)
            return TranslationResult(unit=unit, usage=Usage(input_tokens=in_tok, output_tokens=out_tok))

    provider = AlwaysMergeProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=1)
    chunks = [Chunk(index=0, source_text="Paragraph one.\n\nParagraph two.")]

    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, Glossary())

    result = orch.run(
        job=job,
        chunks=chunks,
        glossary=Glossary(),
        config=config,
        on_progress=lambda p: None,
        cancel_flag=threading.Event(),
    )

    # 3 primary attempts (all tag-valid, all segment-mismatched) and NOTHING
    # else: accepting best effort must not trigger the strip/reinsert fallback.
    assert provider.call_count == 3
    saved = store.load_chunks(job.id)
    assert saved[0].status == ChunkStatus.DONE
    assert saved[0].translated_text == "Párrafo uno. Párrafo dos."
    assert result.status == JobStatus.DONE


# ===========================================================================
# 17. Tag mismatch all 3 attempts → chunk FAILED; job PAUSED (strict) or
#     CONTINUES (default) depending on JobConfig.continue_on_error.
# Spec: job-lifecycle/"run_job applies the continue_on_error gate on chunk
# failure" — parametrized across both flag values (continue-on-error WU2-2).
# ===========================================================================


class _AlwaysMismatchProvider(FakeTranslationProvider):
    """First chunk always returns a tag-mismatched translation (source has no
    tags, translation always has one). Any subsequent chunk is translated
    normally via a canned success response."""

    def translate(self, system: str, user: str, model: str) -> TranslationResult:
        self.call_log.append((system, user, model))
        if user == "No tags here.":
            # Always return extra tags — source has no tags → mismatch forever.
            unit = TranslationUnit(
                translation="<i>Always mismatched</i>",
                summary_update="Summary.",
            )
        else:
            unit = TranslationUnit(
                translation=f"[translated] {user}",
                summary_update="Summary.",
            )
        in_tok = self.count_tokens(system + " " + user, model)
        out_tok = self.count_tokens(unit.translation, model)
        return TranslationResult(unit=unit, usage=Usage(input_tokens=in_tok, output_tokens=out_tok))


@pytest.mark.parametrize("continue_on_error", [True, False])
def test_tag_mismatch_all_retries_fail_chunk_failed_job_paused(continue_on_error: bool):
    store = InMemoryCheckpointStore()
    provider = _AlwaysMismatchProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config(continue_on_error=continue_on_error)
    job = make_job(config, total=2)
    chunks = [
        Chunk(index=0, source_text="No tags here."),
        Chunk(index=1, source_text="Also no tags."),
    ]

    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, Glossary())

    result = orch.run(
        job=job,
        chunks=chunks,
        glossary=Glossary(),
        config=config,
        on_progress=lambda p: None,
        cancel_flag=threading.Event(),
    )

    saved = {c.index: c for c in store.load_chunks(job.id)}
    assert saved[0].status == ChunkStatus.FAILED
    assert saved[0].translated_text is None

    if continue_on_error is False:
        # Strict path (unchanged prior contract): job PAUSES immediately,
        # chunk 1 is never attempted (stays PENDING).
        # 3 primary (tags-in-text) attempts + 1 fallback (strip→translate→reinsert)
        # = 4 total provider calls before chunk 0 is marked FAILED.
        assert provider.call_count == 4
        assert result.status == JobStatus.PAUSED
        assert saved[1].status == ChunkStatus.PENDING
    else:
        # Continue path (default): loop proceeds past the FAILED chunk;
        # chunk 1 is translated normally and the job still finishes DONE.
        assert saved[1].status == ChunkStatus.DONE
        assert result.status == JobStatus.DONE


# ===========================================================================
# continue-on-error WU2-3: additional gate scenarios beyond the parametrized
# M1-8 test above.
# Spec: job-lifecycle/"run_job applies the continue_on_error gate on chunk
# failure" (all scenarios), job-lifecycle/"job finishes DONE with a FAILED
# chunk present" + "job pauses on first FAILED chunk".
# ===========================================================================


def _always_mismatch_except(failing_texts: set[str]) -> type[FakeTranslationProvider]:
    """Build a provider class where any chunk whose source_text is in
    *failing_texts* always returns a tag-mismatched translation (forcing
    FAILED); every other chunk translates successfully."""

    class _Provider(FakeTranslationProvider):
        def translate(self, system: str, user: str, model: str) -> TranslationResult:
            self.call_log.append((system, user, model))
            if user in failing_texts:
                unit = TranslationUnit(
                    translation="<i>Always mismatched</i>",
                    summary_update="Summary.",
                )
            else:
                unit = TranslationUnit(
                    translation=f"[translated] {user}",
                    summary_update="Summary.",
                )
            in_tok = self.count_tokens(system + " " + user, model)
            out_tok = self.count_tokens(unit.translation, model)
            return TranslationResult(unit=unit, usage=Usage(input_tokens=in_tok, output_tokens=out_tok))

    return _Provider


def test_continue_on_error_true_5_chunks_one_failure_job_finishes_done():
    """5-chunk job, continue_on_error=True, chunk 2 exhausts all attempts →
    chunks 0,1,3,4 DONE, chunk 2 FAILED, job finishes DONE."""
    store = InMemoryCheckpointStore()
    chunks = [Chunk(index=i, source_text=f"Chunk {i} text.") for i in range(5)]
    provider = _always_mismatch_except({"Chunk 2 text."})()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config(continue_on_error=True)
    job = make_job(config, total=5)

    result = run_job(orch, job, chunks, store=store)

    saved = {c.index: c for c in store.load_chunks(job.id)}
    for i in (0, 1, 3, 4):
        assert saved[i].status == ChunkStatus.DONE
    assert saved[2].status == ChunkStatus.FAILED
    assert result.status == JobStatus.DONE


def test_continue_on_error_true_all_chunks_fail_job_still_done():
    """3-chunk job, continue_on_error=True, ALL chunks fail → all 3 FAILED with
    translated_text=None, job finishes DONE (not PAUSED)."""
    store = InMemoryCheckpointStore()
    chunks = [Chunk(index=i, source_text=f"Chunk {i} text.") for i in range(3)]
    provider = _always_mismatch_except({c.source_text for c in chunks})()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config(continue_on_error=True)
    job = make_job(config, total=3)

    result = run_job(orch, job, chunks, store=store)

    saved = {c.index: c for c in store.load_chunks(job.id)}
    for i in range(3):
        assert saved[i].status == ChunkStatus.FAILED
        assert saved[i].translated_text is None
    assert result.status == JobStatus.DONE


def test_continue_on_error_false_5_chunks_remaining_untouched():
    """5-chunk job, continue_on_error=False, chunk 2 exhausts → chunks 3,4
    remain PENDING (untouched, never attempted)."""
    store = InMemoryCheckpointStore()
    chunks = [Chunk(index=i, source_text=f"Chunk {i} text.") for i in range(5)]
    provider = _always_mismatch_except({"Chunk 2 text."})()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config(continue_on_error=False)
    job = make_job(config, total=5)

    result = run_job(orch, job, chunks, store=store)

    saved = {c.index: c for c in store.load_chunks(job.id)}
    assert saved[0].status == ChunkStatus.DONE
    assert saved[1].status == ChunkStatus.DONE
    assert saved[2].status == ChunkStatus.FAILED
    assert saved[3].status == ChunkStatus.PENDING
    assert saved[4].status == ChunkStatus.PENDING
    assert result.status == JobStatus.PAUSED


def test_budget_exceeded_still_pauses_regardless_of_continue_on_error_true():
    """Budget-exceeded still PAUSES the job even when continue_on_error=True —
    money exhaustion is unaffected by the continue-on-error gate."""
    store = InMemoryCheckpointStore()
    provider = FakeTranslationProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config(budget_usd=1.00, continue_on_error=True)
    job = make_job(config, total=9)
    job = job.model_copy(update={"cost_usd": 0.93})

    chunks = []
    for i in range(8):
        chunks.append(
            Chunk(
                index=i,
                source_text="x " * 100,
                status=ChunkStatus.DONE,
                translated_text="done",
            )
        )
    chunks.append(
        Chunk(
            index=8,
            source_text="word " * 80000,
            status=ChunkStatus.PENDING,
        )
    )

    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, Glossary())

    with pytest.raises(BudgetExceeded) as exc_info:
        orch.run(
            job=job,
            chunks=chunks,
            glossary=Glossary(),
            config=config,
            on_progress=lambda p: None,
            cancel_flag=threading.Event(),
        )

    exc = exc_info.value
    assert provider.call_count == 0
    assert exc.cost_so_far == pytest.approx(0.93)
    saved_job = store.load_job(job.id)
    assert saved_job.status == JobStatus.PAUSED


# ===========================================================================
# 18. Glossary mid-run: chunk 3 adds new term → chunk 4 system prompt contains it
# ===========================================================================


def test_glossary_midrun_new_term_in_next_chunk_prompt():
    store = InMemoryCheckpointStore()
    provider = FakeTranslationProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    new_term = GlossaryEntry(term="Zorblax", translation="Zorblax", locked=False)

    # Override canned unit for chunk 2 to return a new glossary term
    call_counter: list[int] = [0]

    class GlossaryAddingProvider(FakeTranslationProvider):
        def translate(self, system: str, user: str, model: str) -> TranslationResult:
            n = len(self.call_log)
            self.call_log.append((system, user, model))
            if n == 2:  # 3rd call = chunk index 2
                unit = TranslationUnit(
                    translation="Texto con Zorblax.",
                    summary_update="Summary with Zorblax.",
                    glossary_additions=[new_term],
                )
            else:
                unit = TranslationUnit(
                    translation=f"[translated] {user}",
                    summary_update="Summary.",
                )
            in_tok = self.count_tokens(system + " " + user, model)
            out_tok = self.count_tokens(unit.translation, model)
            return TranslationResult(unit=unit, usage=Usage(input_tokens=in_tok, output_tokens=out_tok))

    provider2 = GlossaryAddingProvider()
    orch2, _, _ = make_orchestrator(provider=provider2, store=store)

    config = make_config()
    job = make_job(config, total=4)
    chunks = make_chunks(4)

    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, Glossary())

    orch2.run(
        job=job,
        chunks=chunks,
        glossary=Glossary(),
        config=config,
        on_progress=lambda p: None,
        cancel_flag=threading.Event(),
    )

    # chunk 4 is index 3; its system prompt is the 4th call (index 3)
    assert provider2.call_count == 4
    system_for_chunk3 = provider2.call_log[3][0]
    assert "Zorblax" in system_for_chunk3


# ===========================================================================
# 19. New mid-run term does NOT override a locked entry
# ===========================================================================


def test_glossary_midrun_locked_entry_not_overridden():
    store = InMemoryCheckpointStore()

    locked_entry = GlossaryEntry(term="Mystara", translation="Mystara Original", locked=True)
    initial_glossary = Glossary(entries=[locked_entry])

    # Provider returns an addition that conflicts with the locked entry
    conflict_entry = GlossaryEntry(term="Mystara", translation="WRONG OVERRIDE", locked=False)

    class ConflictProvider(FakeTranslationProvider):
        def translate(self, system: str, user: str, model: str) -> TranslationResult:
            self.call_log.append((system, user, model))
            unit = TranslationUnit(
                translation="Texto con Mystara.",
                summary_update="Summary.",
                glossary_additions=[conflict_entry],
            )
            in_tok = self.count_tokens(system + " " + user, model)
            out_tok = self.count_tokens(unit.translation, model)
            return TranslationResult(unit=unit, usage=Usage(input_tokens=in_tok, output_tokens=out_tok))

    provider = ConflictProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=2)
    chunks = make_chunks(2)

    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, initial_glossary)

    orch.run(
        job=job,
        chunks=chunks,
        glossary=initial_glossary,
        config=config,
        on_progress=lambda p: None,
        cancel_flag=threading.Event(),
    )

    # Chunk 2 system prompt must still contain "Mystara Original", not "WRONG OVERRIDE"
    system_for_chunk1 = provider.call_log[1][0]
    assert "Mystara Original" in system_for_chunk1
    assert "WRONG OVERRIDE" not in system_for_chunk1


# ===========================================================================
# 20. Rolling summary: chunk N's system prompt contains chunk N-1's summary
# ===========================================================================


def test_rolling_summary_chunk_n_uses_n1_summary():
    store = InMemoryCheckpointStore()

    summaries_seen: list[str] = []

    class SummaryTrackingProvider(FakeTranslationProvider):
        def translate(self, system: str, user: str, model: str) -> TranslationResult:
            self.call_log.append((system, user, model))
            summaries_seen.append(system)
            n = len(self.call_log) - 1
            unit = TranslationUnit(
                translation=f"Translation {n}.",
                summary_update=f"Summary of chunk {n}.",
            )
            in_tok = self.count_tokens(system + " " + user, model)
            out_tok = self.count_tokens(unit.translation, model)
            return TranslationResult(unit=unit, usage=Usage(input_tokens=in_tok, output_tokens=out_tok))

    provider = SummaryTrackingProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=3)
    chunks = make_chunks(3)

    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, Glossary())

    orch.run(
        job=job,
        chunks=chunks,
        glossary=Glossary(),
        config=config,
        on_progress=lambda p: None,
        cancel_flag=threading.Event(),
    )

    # Chunk 1's system prompt must contain "Summary of chunk 0"
    assert "Summary of chunk 0" in summaries_seen[1]
    # Chunk 2's system prompt must contain "Summary of chunk 1"
    assert "Summary of chunk 1" in summaries_seen[2]


# ===========================================================================
# 21. First chunk uses empty/placeholder summary (no prior context)
# ===========================================================================


def test_first_chunk_uses_placeholder_summary():
    store = InMemoryCheckpointStore()
    provider = FakeTranslationProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=1)
    chunks = make_chunks(1)

    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, Glossary())

    orch.run(
        job=job,
        chunks=chunks,
        glossary=Glossary(),
        config=config,
        on_progress=lambda p: None,
        cancel_flag=threading.Event(),
    )

    system_for_chunk0 = provider.call_log[0][0]
    # Either empty or the standard placeholder text from ContextManager
    assert "No prior context." in system_for_chunk0 or "[SUMMARY]" in system_for_chunk0


# ===========================================================================
# M2-0 Tests — Tag-rework: tags-in-text primary, strip/reinsert fallback
# ===========================================================================


# ===========================================================================
# M2-0 Test 2: Tags-in-text primary: source is sent WITH tags, strip NOT applied
# ===========================================================================


def test_tags_in_text_primary_strip_not_applied():
    """Tags-in-text primary path: provider receives raw source WITH tags in the user
    message. markup.strip must NOT have been applied before the provider call.
    Spec: subtitle-translation/inline-tags-in-text scenario 'tags travel with translated words'.
    """
    store = InMemoryCheckpointStore()
    # Provider returns a translation that KEEPS the tags and passes validate_tags
    canned = TranslationUnit(
        translation="El zorro <i>rápido</i>.",
        summary_update="Summary.",
    )
    provider = FakeTranslationProvider(canned_unit=canned)
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=1)
    source_with_tags = "The <i>quick</i> fox."
    chunks = [Chunk(index=0, source_text=source_with_tags)]

    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, Glossary())

    result = orch.run(
        job=job,
        chunks=chunks,
        glossary=Glossary(),
        config=config,
        on_progress=lambda p: None,
        cancel_flag=threading.Event(),
    )

    assert result.status == JobStatus.DONE
    assert provider.call_count == 1

    # The user message (second element of call_log tuple) must CONTAIN the raw tags.
    # If strip had been applied, the user message would be plain text without tags.
    user_message = provider.call_log[0][1]
    assert "<i>" in user_message, (
        "User message must contain raw <i> tag — strip must NOT have been applied before call"
    )
    assert "</i>" in user_message, (
        "User message must contain raw </i> tag — strip must NOT have been applied before call"
    )


# ===========================================================================
# M2-0 Test 3: Mismatch on attempt 1, valid on attempt 2 → 2 provider calls, DONE
# ===========================================================================


def test_tags_in_text_mismatch_retry_succeeds_on_second():
    """When validate_tags fails on attempt 1 but succeeds on attempt 2, the chunk
    completes as DONE with exactly 2 provider calls (retry behavior preserved under
    the new tags-in-text path).
    Spec: subtitle-translation/inline-tags-in-text scenario 'tag count mismatch triggers retry'.
    """
    store = InMemoryCheckpointStore()

    class MismatchThenOkProvider(FakeTranslationProvider):
        def translate(self, system: str, user: str, model: str) -> TranslationResult:
            n = len(self.call_log)
            self.call_log.append((system, user, model))
            if n == 0:
                # First attempt: tags missing in output → validate_tags will fail
                # (source has <i>...</i> but translation has none)
                unit = TranslationUnit(
                    translation="El zorro rápido.",  # missing tags
                    summary_update="Summary.",
                )
            else:
                # Second attempt: tags preserved → validate_tags passes
                unit = TranslationUnit(
                    translation="El zorro <i>rápido</i>.",
                    summary_update="Summary.",
                )
            in_tok = self.count_tokens(system + " " + user, model)
            out_tok = self.count_tokens(unit.translation, model)
            return TranslationResult(unit=unit, usage=Usage(input_tokens=in_tok, output_tokens=out_tok))

    provider = MismatchThenOkProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=1)
    source_with_tags = "The <i>quick</i> fox."
    chunks = [Chunk(index=0, source_text=source_with_tags)]

    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, Glossary())

    result = orch.run(
        job=job,
        chunks=chunks,
        glossary=Glossary(),
        config=config,
        on_progress=lambda p: None,
        cancel_flag=threading.Event(),
    )

    assert provider.call_count == 2
    assert result.status == JobStatus.DONE
    saved = store.load_chunks(job.id)
    assert saved[0].status == ChunkStatus.DONE


# ===========================================================================
# M2-0 Test 4: All 3 tags-in-text attempts fail → strip/reinsert fallback used
# ===========================================================================


def test_all_tags_in_text_fail_falls_back_to_strip_reinsert():
    """When all 3 tags-in-text attempts fail validation, the engine falls back to
    the deterministic strip→translate-plain→reinsert path. If the fallback passes
    validate_tags, the chunk is DONE using the fallback output.
    Spec: subtitle-translation/inline-tags-in-text scenario 'retries exhausted — deterministic fallback'.
    """
    store = InMemoryCheckpointStore()

    call_count_ref: list[int] = [0]

    class AllTagsFailThenPlainProvider(FakeTranslationProvider):
        def translate(self, system: str, user: str, model: str) -> TranslationResult:
            n = len(self.call_log)
            self.call_log.append((system, user, model))
            if n < 3:
                # First 3 calls (tags-in-text attempts): drop a tag → validate_tags fails
                unit = TranslationUnit(
                    translation="El zorro rápido.",  # missing <i>...</i>
                    summary_update="Summary.",
                )
            else:
                # 4th call (fallback — plain text translate): return plain translation
                # strip() will have removed tags; plain text in → plain text out → valid
                unit = TranslationUnit(
                    translation="El zorro rápido.",
                    summary_update="Summary.",
                )
            in_tok = self.count_tokens(system + " " + user, model)
            out_tok = self.count_tokens(unit.translation, model)
            return TranslationResult(unit=unit, usage=Usage(input_tokens=in_tok, output_tokens=out_tok))

    provider = AllTagsFailThenPlainProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=1)
    source_with_tags = "The <i>quick</i> fox."
    chunks = [Chunk(index=0, source_text=source_with_tags)]

    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, Glossary())

    result = orch.run(
        job=job,
        chunks=chunks,
        glossary=Glossary(),
        config=config,
        on_progress=lambda p: None,
        cancel_flag=threading.Event(),
    )

    # 3 tags-in-text calls + 1 fallback (plain text) call = 4 total
    assert provider.call_count == 4
    assert result.status == JobStatus.DONE
    saved = store.load_chunks(job.id)
    assert saved[0].status == ChunkStatus.DONE


# ===========================================================================
# M2-0 Test 5: Both tags-in-text (3) and fallback fail → FAILED, PAUSED
# ===========================================================================


def test_both_tags_in_text_and_fallback_fail_chunk_failed_job_paused():
    """When all 3 tags-in-text attempts AND the strip/reinsert fallback all fail
    validate_tags, chunk is FAILED and job is PAUSED (continue_on_error=False).
    Spec: book-translation/"inline EPUB tags are preserved..." scenario
    'fallback exhaustion with continue_on_error=False — chunk FAILED, job
    PAUSED (unchanged prior contract)'.
    """
    store = InMemoryCheckpointStore()

    class AlwaysMismatchProvider(FakeTranslationProvider):
        def translate(self, system: str, user: str, model: str) -> TranslationResult:
            self.call_log.append((system, user, model))
            # Always produce a translation that has DIFFERENT tag count from source.
            # Source has 2 tags (<i> and </i>), we return 0 tags from the first 3
            # (tags-in-text path fails), then on fallback we return extra tags to force
            # fallback validate_tags to also fail.
            n = len(self.call_log) - 1
            if n < 3:
                # Tags-in-text: return 0 tags (source has 2) → mismatch
                unit = TranslationUnit(
                    translation="El zorro rápido.",
                    summary_update="Summary.",
                )
                in_tok = self.count_tokens(system + " " + user, model)
                out_tok = self.count_tokens(unit.translation, model)
                return TranslationResult(unit=unit, usage=Usage(input_tokens=in_tok, output_tokens=out_tok))
            else:
                # Fallback call (plain text): return EXTRA tags → mismatch again
                # After reinsert, the result will have the original tags reinserted
                # so validate_tags will pass (2 tags in, 2 tags out).
                # We need to make the fallback fail differently — return a response
                # whose reinserted form still mismatches.
                # Actually: reinsert always puts EXACTLY the source tags back in
                # So fallback validate_tags should ALWAYS pass.
                # To force fallback failure we need reinsert to produce a different count.
                # This is not possible with the current reinsert — reinsert always
                # re-adds the exact tags stripped. So the only way fallback fails is if
                # validate_tags check on (source, reinserted) mismatches — it can't
                # because reinsert puts EXACTLY the stripped tags back.
                # Conclusion: per the spec, fallback FAILS only if the provider call itself
                # raises or returns something the orchestrator marks as failed.
                # For this test: simulate the fallback failing by making the fallback
                # provider call raise MalformedOutput so no TranslationUnit is obtained.
                # Actually the spec says "validate_tags fails" but reinsert guarantees tag count.
                # Therefore: the ONLY way to fail fallback is if the provider raises.
                # We raise MalformedOutput on the 4th call to simulate a broken fallback call.
                from borgesica.domain.errors import MalformedOutput as MO
                raise MO(job_id="fake-job", chunk_index=n)

    provider = AlwaysMismatchProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config(continue_on_error=False)
    job = make_job(config, total=2)
    chunks = [
        Chunk(index=0, source_text="The <i>quick</i> fox."),
        Chunk(index=1, source_text="Second chunk."),
    ]

    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, Glossary())

    result = orch.run(
        job=job,
        chunks=chunks,
        glossary=Glossary(),
        config=config,
        on_progress=lambda p: None,
        cancel_flag=threading.Event(),
    )

    # 3 tags-in-text calls + 1 fallback (raises) = 4 total
    assert provider.call_count == 4
    saved = {c.index: c for c in store.load_chunks(job.id)}
    assert saved[0].status == ChunkStatus.FAILED
    assert result.status == JobStatus.PAUSED


# ===========================================================================
# M2-0 Review fix: unexpected (non-provider) exception in fallback propagates.
# A bare `except Exception` in the fallback would mask real bugs (a strip/reinsert
# defect, an AttributeError) as a clean tag-failure. Only provider errors
# (MalformedOutput / ProviderError) become FAILED/PAUSED; anything else propagates.
# ===========================================================================


def test_unexpected_exception_in_fallback_propagates():
    """A non-provider exception raised during the fallback path must NOT be
    swallowed and mislabeled as a tag failure — it must propagate so real bugs
    surface instead of producing a silent FAILED/PAUSED chunk.
    """
    store = InMemoryCheckpointStore()

    class BoomOnFallbackProvider(FakeTranslationProvider):
        def translate(self, system: str, user: str, model: str) -> TranslationResult:
            n = len(self.call_log)
            self.call_log.append((system, user, model))
            if n < 3:
                # Tags-in-text attempts: drop the tags → validate_tags fails.
                unit = TranslationUnit(
                    translation="El zorro rápido.",  # 0 tags vs 2 in source
                    summary_update="Summary.",
                )
                in_tok = self.count_tokens(system + " " + user, model)
                out_tok = self.count_tokens(unit.translation, model)
                return TranslationResult(unit=unit, usage=Usage(input_tokens=in_tok, output_tokens=out_tok))
            # Fallback call: raise an UNEXPECTED (non-provider) error.
            raise RuntimeError("unexpected boom in fallback")

    provider = BoomOnFallbackProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=1)
    chunks = [Chunk(index=0, source_text="The <i>quick</i> fox.")]

    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, Glossary())

    with pytest.raises(RuntimeError, match="unexpected boom"):
        orch.run(
            job=job,
            chunks=chunks,
            glossary=Glossary(),
            config=config,
            on_progress=lambda p: None,
            cancel_flag=threading.Event(),
        )

    # 3 tags-in-text + 1 fallback call that raised = 4 total.
    assert provider.call_count == 4


# ===========================================================================
# M2-0 Test 6 (REGRESSION): Cue-spanning tag regression
# A 2-cue chunk where cue 1 = "We don't have <i>much</i> time"
# After translation and SrtWriter "\n\n" split, <i>...</i> must stay in cue 1.
# ===========================================================================


def test_cue_spanning_tag_regression():
    """Regression: a 2-cue chunk where cue 1 has <i>...</i> tags.
    After the tags-in-text path and SrtWriter split on '\\n\\n', the <i>...</i>
    pair must remain WITHIN cue 1's text — no tag may leak into cue 2.

    This is the exact bug from the 2026-06-25 live test with proportional reinsert.
    Using the tags-in-text primary path, the model is given the full source WITH tags
    and told to carry them — so translation output keeps <i>...</i> in place.
    The SrtWriter split on '\\n\\n' then correctly places them in cue 1.
    """
    from borgesica.domain.markup import validate_tags

    # Source: 2-cue chunk joined by \n\n
    # Cue 1: has inline tags
    # Cue 2: plain text
    cue1_source = "We don't have <i>much</i> time"
    cue2_source = "before they arrive."
    source_text = f"{cue1_source}\n\n{cue2_source}"

    # The provider is given the raw source WITH tags (tags-in-text primary path).
    # In a real translation, the model returns something like:
    # "No tenemos <i>mucho</i> tiempo\n\nantes de que lleguen."
    # We simulate this with a canned TranslationUnit that keeps tags in cue 1.
    canned_translation = "No tenemos <i>mucho</i> tiempo\n\nantes de que lleguen."
    canned = TranslationUnit(
        translation=canned_translation,
        summary_update="Summary.",
    )
    provider = FakeTranslationProvider(canned_unit=canned)

    store = InMemoryCheckpointStore()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=1)
    chunks = [Chunk(index=0, source_text=source_text, meta={"cue_batches": [
        {"cue_index": 1, "start": "00:00:01,000", "end": "00:00:03,000", "text": cue1_source},
        {"cue_index": 2, "start": "00:00:04,000", "end": "00:00:06,000", "text": cue2_source},
    ]})]

    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, Glossary())

    result = orch.run(
        job=job,
        chunks=chunks,
        glossary=Glossary(),
        config=config,
        on_progress=lambda p: None,
        cancel_flag=threading.Event(),
    )

    assert result.status == JobStatus.DONE
    saved = store.load_chunks(job.id)
    translated_text = saved[0].translated_text
    assert translated_text is not None

    # Split the translated chunk on "\n\n" to get per-cue text (as SrtWriter does)
    cue_texts = translated_text.split("\n\n")
    assert len(cue_texts) == 2, f"Expected 2 cue texts after split, got {len(cue_texts)}: {cue_texts!r}"

    cue1_translated = cue_texts[0]
    cue2_translated = cue_texts[1]

    # Tags must be entirely in cue 1 — none may leak into cue 2
    assert "<i>" in cue1_translated, f"<i> must be in cue 1, got: {cue1_translated!r}"
    assert "</i>" in cue1_translated, f"</i> must be in cue 1, got: {cue1_translated!r}"
    assert "<i>" not in cue2_translated, f"<i> must NOT be in cue 2, got: {cue2_translated!r}"
    assert "</i>" not in cue2_translated, f"</i> must NOT be in cue 2, got: {cue2_translated!r}"

    # Verify overall validate_tags still passes
    assert validate_tags(source_text, translated_text), (
        "validate_tags must pass: tag count in source and translation must match"
    )

    # S-M2-1: hardening assertion — the provider must have received the tags in
    # the user message (tags-in-text primary path). call_log entries are
    # (system, user, model); index [1] is the user prompt.
    # Against the OLD strip-before-call flow, source_text would have been
    # stripped of tags before being sent, and this assertion would FAIL.
    assert "<i>" in provider.call_log[0][1], (
        "Provider user-message must contain inline tags (tags-in-text path). "
        f"Got user message: {provider.call_log[0][1][:200]!r}"
    )


# ===========================================================================
# M4-6 — Real token-usage cost accounting (regression guards)
# ===========================================================================
#
# FakeTranslationProvider.price() = (1.0, 5.0) — $1/Mtok in, $5/Mtok out.
# FakeTranslationProvider.translate() returns TranslationResult with usage
# derived from count_tokens(system + " " + user, model) for input and
# count_tokens(unit.translation, model) for output.
#
# chunk source_text = "Chunk 0 text." etc.  (from make_chunks)
# count_tokens("Chunk N text.", model) = len("Chunk N text.".split()) = 3 words = 3 tokens
# System prompt is generated by ContextManager — we need to measure it
# deterministically.  But rather than hard-coding the prompt length we use
# FakeTranslationProvider.count_tokens which just counts words.
#
# The key assertions are relative/directional, not exact-USD bets, EXCEPT for
# the BUG-1 (reflective-cost: 3× calls must produce 3× cost vs 1× call) and
# BUG-2 (failed-chunk cost is > 0).
# ===========================================================================


def test_fast_mode_cost_is_real_usage_not_estimate():
    """M4-6 / BUG-0 regression: job.cost_usd is based on TranslationResult.usage
    (real per-call tokens), NOT the old flat estimate.

    FakeTranslationProvider returns TranslationResult with:
      - input_tokens  = count_tokens(system + " " + user, model)  [word count]
      - output_tokens = count_tokens(unit.translation, model)      [word count]

    For a 1-chunk job with source_text="Chunk 0 text." (3 words) and the system
    prompt generated by ContextManager (measurable word count), the expected cost
    is: (in_tok/1e6)*1.0 + (out_tok/1e6)*5.0 — summed over all calls.

    We verify: cost > 0 AND cost for 3 chunks > cost for 1 chunk (real accrual).
    """
    # 1-chunk job
    orch1, _, store1 = make_orchestrator()
    config = make_config(quality_mode="fast")
    job1 = make_job(config, total=1)
    chunks1 = make_chunks(1)
    result1 = run_job(orch1, job1, chunks1, store=store1)
    assert result1.cost_usd > 0.0, "1-chunk fast-mode cost must be > 0"

    # 3-chunk job — cost must be ~3× the 1-chunk cost (same prompts, 3 calls)
    orch3, _, store3 = make_orchestrator()
    job3 = make_job(config, total=3)
    chunks3 = make_chunks(3)
    result3 = run_job(orch3, job3, chunks3, store=store3)
    assert result3.cost_usd > result1.cost_usd, (
        "3-chunk cost must exceed 1-chunk cost (real usage accrued per call)."
    )
    # 3 chunks should cost roughly 3× 1 chunk (within 50% tolerance for prompt variation)
    ratio = result3.cost_usd / result1.cost_usd
    assert 2.0 <= ratio <= 4.0, (
        f"3-chunk cost ratio {ratio:.2f}× unexpected. Expected 2–4× of 1-chunk cost."
    )


def test_reflective_mode_cost_reflects_three_calls_per_chunk():
    """BUG-1 regression guard: reflective mode makes 3 provider calls per chunk.
    With REAL per-call usage, reflective cost > fast cost because critique and
    revise prompts are much longer (they include the original text + draft + context).

    Old code bug: _actual_chunk_cost used _project_chunk_cost which correctly
    multiplied passes×3 but used a FLAT 150 output tokens. With real usage,
    the reflective mode's critique+revise prompts are substantially longer than
    the draft prompt, so actual cost >> old estimate.

    This test uses a TrackingProvider that asserts it was called exactly 3× per chunk,
    AND that the accrued cost accounts for all 3 calls (not just 1).
    """
    call_costs: list[float] = []

    class TrackingProvider(FakeTranslationProvider):
        """Returns TranslationResult with tracked usage to enable per-call cost checks."""
        def translate(self, system: str, user: str, model: str) -> "TranslationResult":  # type: ignore[override]
            from borgesica.domain.models import TranslationResult, TranslationUnit, Usage
            self.call_log.append((system, user, model))
            n = len(self.call_log) - 1
            step = n % 3
            if step == 0:
                text = f"[translated] {user}"
            elif step == 1:
                text = "critique notes"
            else:
                # Single-segment revise output: echoing the composite revise
                # PROMPT would violate the "\n\n" segment contract and trigger
                # retries (the revise output is what gets validated).
                text = "[revised] chunk translation."
            unit = TranslationUnit(translation=text, summary_update="Summary.")
            in_tok = self.count_tokens(system + " " + user, model)
            out_tok = self.count_tokens(text, model)
            call_costs.append((in_tok / 1e6) * 1.0 + (out_tok / 1e6) * 5.0)
            return TranslationResult(unit=unit, usage=Usage(input_tokens=in_tok, output_tokens=out_tok))

    provider = TrackingProvider()
    store = InMemoryCheckpointStore()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config(quality_mode="reflective")
    job = make_job(config, total=1)
    chunks = make_chunks(1)

    result = run_job(orch, job, chunks, config=config, store=store)

    # Exactly 3 calls for 1 reflective chunk
    assert provider.call_count == 3, f"Expected 3 calls for reflective, got {provider.call_count}"

    # BUG-1 guard: cost must equal sum of ALL 3 calls' usage, not just 1 call
    expected_cost = sum(call_costs)
    assert result.cost_usd == pytest.approx(expected_cost, rel=1e-6), (
        f"Reflective cost {result.cost_usd:.8f} must equal sum of all 3 call costs "
        f"{expected_cost:.8f}. Bug-1: old code charged only 1 pass."
    )


def test_failed_chunk_cost_is_nonzero():
    """BUG-2 regression guard: a chunk that fails all attempts accrues COST > 0.
    Old code: failed chunk cost was ZERO because _actual_chunk_cost was only called
    in the DONE branch, never for FAILED/PAUSED chunks.
    New code: real usage from every call is accrued into running_cost before
    the failure is recorded — so failed chunks' calls ARE paid for."""
    store = InMemoryCheckpointStore()

    accumulated_call_costs: list[float] = []

    class AlwaysMismatchProvider(FakeTranslationProvider):
        """Always returns mismatched tags; tracks individual call costs."""
        def translate(self, system: str, user: str, model: str) -> "TranslationResult":  # type: ignore[override]
            from borgesica.domain.models import TranslationResult, TranslationUnit, Usage
            self.call_log.append((system, user, model))
            unit = TranslationUnit(
                translation="<i>Always mismatched</i>",
                summary_update="Summary.",
            )
            in_tok = self.count_tokens(system + " " + user, model)
            out_tok = self.count_tokens(unit.translation, model)
            call_cost = (in_tok / 1e6) * 1.0 + (out_tok / 1e6) * 5.0
            accumulated_call_costs.append(call_cost)
            return TranslationResult(unit=unit, usage=Usage(input_tokens=in_tok, output_tokens=out_tok))

    provider = AlwaysMismatchProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    # continue_on_error=False: this test's job-status assertion (PAUSED) is
    # about the pre-continue-on-error contract; it captures the failed-chunk
    # cost snapshot on the PAUSED job. The BUG-2 cost-accrual fix itself is
    # orthogonal to continue_on_error (cost is accrued the same way on both
    # the PAUSED and the continue-past-FAILED paths).
    config = make_config(continue_on_error=False)
    job = make_job(config, total=1)
    # Plain text source (no tags) — provider returns tags → tag mismatch for all 3+1 calls
    chunks = [Chunk(index=0, source_text="No tags here.")]

    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, Glossary())

    result = orch.run(
        job=job,
        chunks=chunks,
        glossary=Glossary(),
        config=config,
        on_progress=lambda p: None,
        cancel_flag=threading.Event(),
    )

    assert result.status == JobStatus.PAUSED

    # BUG-2 guard: failed chunk must have cost > 0 (4 calls were made and charged)
    assert result.cost_usd > 0.0, (
        "Failed-chunk cost must be > 0. Calls were made and tokens were consumed. "
        "Bug-2: old code set cost=0 for failed chunks because _actual_chunk_cost "
        "was only called in the DONE branch."
    )
    # The accrued cost must equal the sum of all call costs (4 calls: 3 primary + 1 fallback)
    expected_total = sum(accumulated_call_costs)
    assert result.cost_usd == pytest.approx(expected_total, rel=1e-6), (
        f"Failed chunk cost {result.cost_usd:.8f} must equal total of all call costs "
        f"{expected_total:.8f} ({len(accumulated_call_costs)} calls made)."
    )


# ===========================================================================
# continue-on-error WU2-1: prose guard — zero-alphabetic chunks pass through
# with 0 provider calls.
# Spec: job-lifecycle/"prose guard skips provider calls for chunks with no
# alphabetic content" (all 3 scenarios).
# ===========================================================================


def test_prose_guard_empty_after_strip_skips_provider_zero_cost():
    """A chunk that strips to an empty string (a bare inline-tag pair with no
    text content — the markup-only shape of a non-prose node such as an EPUB
    cover placeholder) is passed through as DONE with zero provider calls and
    zero cost."""
    store = InMemoryCheckpointStore()
    provider = FakeTranslationProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=1)
    chunks = [Chunk(index=0, source_text='<span></span>')]

    result = run_job(orch, job, chunks, store=store)

    assert provider.call_count == 0
    saved = {c.index: c for c in store.load_chunks(job.id)}
    assert saved[0].status == ChunkStatus.DONE
    assert saved[0].translated_text == chunks[0].source_text
    assert result.cost_usd == 0.0


def test_prose_guard_whitespace_only_skips_provider_zero_cost():
    """A whitespace-only chunk is passed through as DONE with zero provider calls."""
    store = InMemoryCheckpointStore()
    provider = FakeTranslationProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=1)
    chunks = [Chunk(index=0, source_text="   \n\t  ")]

    result = run_job(orch, job, chunks, store=store)

    assert provider.call_count == 0
    saved = {c.index: c for c in store.load_chunks(job.id)}
    assert saved[0].status == ChunkStatus.DONE
    assert saved[0].translated_text == chunks[0].source_text
    assert result.cost_usd == 0.0


def test_prose_guard_does_not_catch_watermark_with_letters():
    """A watermark-style chunk whose stripped text contains letters (e.g.
    'OceanofPDF.com') is NOT caught by the guard — the provider IS called."""
    store = InMemoryCheckpointStore()
    provider = FakeTranslationProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=1)
    chunks = [Chunk(index=0, source_text='<a href="x">OceanofPDF.com</a>')]

    result = run_job(orch, job, chunks, store=store)

    assert provider.call_count == 1
    saved = {c.index: c for c in store.load_chunks(job.id)}
    assert saved[0].status == ChunkStatus.DONE


# ===========================================================================
# Guard miss on nested <img> (backlog obs #318, WARNING-2 of continue-on-error
# verify): EpubReader skips <img> only as a standalone structural node; nested
# in <p>/<figcaption> it serializes INTO source_text. markup.strip() only
# knows i/b/u/em/strong/span/a, so '<img src="images/cover.jpg"/>' keeps its
# alphabetic attribute characters and the guard never fires — a real cover
# burned provider calls (up to 4: 3 primary + fallback) translating a tag.
#
# Contract: the guard's alphabetic check must ignore ALL markup (known inline
# tags, void tags, unknown tags). A tags-only chunk passes through verbatim
# (bit-perfect <img> preservation, 0 calls, $0). A chunk with an <img> AND
# real prose must still be translated.
# ===========================================================================


def test_prose_guard_skips_img_only_chunk_zero_calls():
    """A chunk whose source is ONLY a nested-<img> serialization (real cover
    shape) passes through verbatim with ZERO provider calls and zero cost."""
    store = InMemoryCheckpointStore()
    provider = FakeTranslationProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=1)
    chunks = [
        Chunk(index=0, source_text='<img src="images/cover.jpg" alt="Cover art"/>')
    ]

    result = run_job(orch, job, chunks, store=store)

    assert provider.call_count == 0
    saved = {c.index: c for c in store.load_chunks(job.id)}
    assert saved[0].status == ChunkStatus.DONE
    assert saved[0].translated_text == chunks[0].source_text
    assert result.cost_usd == 0.0


def test_prose_guard_skips_figure_wrapped_img_zero_calls():
    """Unknown wrapper tags (<figure>/<figcaption> shells) around an <img>
    with no text content must also pass through with zero calls."""
    store = InMemoryCheckpointStore()
    provider = FakeTranslationProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=1)
    chunks = [
        Chunk(index=0, source_text='<figure><img src="cover.png"/></figure>')
    ]

    result = run_job(orch, job, chunks, store=store)

    assert provider.call_count == 0
    saved = {c.index: c for c in store.load_chunks(job.id)}
    assert saved[0].status == ChunkStatus.DONE
    assert saved[0].translated_text == chunks[0].source_text
    assert result.cost_usd == 0.0


# ===========================================================================
# Guard miss on roman-numeral/page-number nodes (job 34d5d0a7, per-paragraph
# mode, qwen3:14b): a front-matter node like "i ii iii 1 2 3" has alphabetic
# characters (roman numerals), so the zero-alphabetic check never fired and
# the fragment went to the LLM — small local models reply with meta-commentary
# ("No hay narrativa ni diálogo en este fragmento...") that leaked into the
# book as the "translation".
#
# Contract: chunks whose stripped text is ONLY digits, strict roman numerals,
# and punctuation pass through verbatim (0 calls, $0). Real prose — including
# single-word headings like "Prologue" — must still be translated.
# ===========================================================================


def test_prose_guard_skips_roman_numeral_page_list_zero_calls():
    """A front-matter page-list chunk (roman numerals + arabic page numbers)
    passes through verbatim with ZERO provider calls and zero cost."""
    store = InMemoryCheckpointStore()
    provider = FakeTranslationProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=1)
    chunks = [
        Chunk(index=0, source_text="i ii iii iv v vi vii 1 2 3 4 5 6 7 8 9")
    ]

    result = run_job(orch, job, chunks, store=store)

    assert provider.call_count == 0
    saved = {c.index: c for c in store.load_chunks(job.id)}
    assert saved[0].status == ChunkStatus.DONE
    assert saved[0].translated_text == chunks[0].source_text
    assert result.cost_usd == 0.0


def test_prose_guard_skips_tagged_roman_numeral_node_zero_calls():
    """The same page-list shape wrapped in markup (as EPUB readers serialize
    it) must also pass through — tags are stripped before the prose check."""
    store = InMemoryCheckpointStore()
    provider = FakeTranslationProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=1)
    chunks = [Chunk(index=0, source_text="<span>xiv</span> <span>xv</span> 210")]

    result = run_job(orch, job, chunks, store=store)

    assert provider.call_count == 0
    saved = {c.index: c for c in store.load_chunks(job.id)}
    assert saved[0].status == ChunkStatus.DONE
    assert saved[0].translated_text == chunks[0].source_text
    assert result.cost_usd == 0.0


def test_prose_guard_does_not_catch_single_word_heading():
    """A single-word heading ('Prologue') is real prose — the provider IS
    called (zero-false-positives contract of the guard)."""
    store = InMemoryCheckpointStore()
    provider = FakeTranslationProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=1)
    chunks = [Chunk(index=0, source_text="Prologue")]

    result = run_job(orch, job, chunks, store=store)

    assert provider.call_count == 1
    saved = {c.index: c for c in store.load_chunks(job.id)}
    assert saved[0].status == ChunkStatus.DONE


def test_prose_guard_does_not_catch_img_with_prose():
    """An <img> accompanied by real prose must NOT be skipped — the provider
    IS called (zero-false-positives contract of the guard)."""
    store = InMemoryCheckpointStore()
    provider = FakeTranslationProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=1)
    chunks = [
        Chunk(index=0, source_text='<img src="map.png"/> The journey begins here.')
    ]

    result = run_job(orch, job, chunks, store=store)

    assert provider.call_count == 1
    saved = {c.index: c for c in store.load_chunks(job.id)}
    assert saved[0].status == ChunkStatus.DONE


# ===========================================================================
# Billed-usage accrual on failed provider calls (budget guard regression).
#
# Root cause: a real book run billed $1.68 on Anthropic's console but
# Borgésica recorded $0.196 — 13 failed chunks x up to 12 billed calls each
# had their usage silently dropped because `except (MalformedOutput,
# ProviderError): continue` / `pass` discarded the exception (and its usage)
# without adding anything to total_call_cost. Both MalformedOutput and
# ProviderError now carry a `usage` field; the orchestrator must add
# `_usage_cost(exc.usage, in_price, out_price)` to total_call_cost in both the
# PRIMARY-loop except and the FALLBACK except.
# ===========================================================================


def test_primary_loop_exception_usage_is_accrued_into_cost():
    """PRIMARY loop: every attempt raises MalformedOutput carrying non-zero
    billed usage. Even though every attempt "fails" (continue's past it), the
    orchestrator must accrue each attempt's usage cost — previously this cost
    was silently dropped (except: continue, no cost added).

    FakeTranslationProvider.price() = (1.0, 5.0) — $1/Mtok in, $5/Mtok out.
    """
    from borgesica.domain.errors import MalformedOutput
    from borgesica.domain.models import Usage

    class AlwaysRaiseWithUsageProvider(FakeTranslationProvider):
        def translate(self, system: str, user: str, model: str):
            self.call_log.append((system, user, model))
            raise MalformedOutput(
                job_id="x", chunk_index=-1, usage=Usage(input_tokens=1000, output_tokens=500)
            )

    store = InMemoryCheckpointStore()
    provider = AlwaysRaiseWithUsageProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config(continue_on_error=False)
    job = make_job(config, total=1)
    chunks = make_chunks(1)

    result = run_job(orch, job, chunks, store=store)

    assert result.status == JobStatus.PAUSED
    # 3 primary attempts + 1 fallback attempt = 4 calls, each billed
    # (1000 in, 500 out) tokens at ($1/Mtok, $5/Mtok):
    # per-call cost = (1000/1e6)*1.0 + (500/1e6)*5.0 = 0.001 + 0.0025 = 0.0035
    # 4 calls => 0.014
    assert provider.call_count == 4
    expected_cost = 4 * ((1000 / 1_000_000) * 1.0 + (500 / 1_000_000) * 5.0)
    assert result.cost_usd == pytest.approx(expected_cost, rel=1e-6), (
        f"Expected accrued cost {expected_cost:.6f} from billed-but-failed usage, "
        f"got {result.cost_usd:.6f}. Regression: exceptions' usage was silently "
        f"dropped (except: continue with no cost accrual)."
    )
    assert result.cost_usd > 0.0


def test_fallback_exception_usage_is_accrued_into_cost():
    """FALLBACK except path: the fallback call raises MalformedOutput carrying
    non-zero billed usage after all PRIMARY attempts also raised with billed
    usage. Total accrued cost must equal the sum of all 4 calls' usage costs
    (3 primary + 1 fallback), not zero.
    """
    from borgesica.domain.errors import MalformedOutput
    from borgesica.domain.models import Usage

    class PrimaryFreeFallbackBilledProvider(FakeTranslationProvider):
        """PRIMARY attempts raise MalformedOutput with zero usage (default);
        the FALLBACK call (4th) raises MalformedOutput with non-zero usage —
        isolates the FALLBACK except-block accrual path specifically."""

        def translate(self, system: str, user: str, model: str):
            call_index = len(self.call_log)
            self.call_log.append((system, user, model))
            if call_index < 3:
                # PRIMARY attempts: billed but zero usage (default Usage()).
                raise MalformedOutput(job_id="x", chunk_index=-1)
            # FALLBACK call (4th, index 3): billed with real usage.
            raise MalformedOutput(
                job_id="x", chunk_index=-1, usage=Usage(input_tokens=2000, output_tokens=800)
            )

    store = InMemoryCheckpointStore()
    provider = PrimaryFreeFallbackBilledProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config(continue_on_error=False)
    job = make_job(config, total=1)
    chunks = make_chunks(1)

    result = run_job(orch, job, chunks, store=store)

    assert result.status == JobStatus.PAUSED
    assert provider.call_count == 4
    # Only the fallback call (index 3) carries non-zero usage.
    expected_cost = (2000 / 1_000_000) * 1.0 + (800 / 1_000_000) * 5.0
    assert result.cost_usd == pytest.approx(expected_cost, rel=1e-6), (
        f"Expected accrued cost {expected_cost:.6f} from the fallback call's billed "
        f"usage, got {result.cost_usd:.6f}. Regression: the FALLBACK except-block "
        f"dropped exc.usage entirely (except: pass with no cost accrual)."
    )
    assert result.cost_usd > 0.0


# ===========================================================================
# WU4-1 — nav-label chunks always single-pass, bypassing reflective mode
# (translation-quality/"quality_mode controls how many model passes run per
# chunk" — scenario "nav-label chunk bypasses reflective mode even when the
# job is reflective"; translation-quality/"reflection is orchestrator-level
# and provider-agnostic" — scenario "reflective mode works with any provider
# via the same port" (mixed body + nav-label job))
# ===========================================================================


def make_nav_label_chunk(index: int, text: str = "Chapter One") -> Chunk:
    """Build a nav-label chunk as chunk_prose would emit it (D3a: top-level
    meta["kind"] == "nav-label", lifted from the isolated nav chapter_index
    bucket's per-node marker set by EpubReader's nav walk)."""
    return Chunk(
        index=index,
        source_text=text,
        status=ChunkStatus.PENDING,
        meta={"kind": "nav-label", "prose_nodes": [{"epub_item_href": "nav.xhtml", "node_path": "/nav[0]/a[0]"}]},
    )


def test_nav_label_chunk_bypasses_reflective_mode_single_call():
    """quality_mode='reflective' job, mixed nav-label + body chunks: the
    nav-label chunk gets exactly 1 provider call (no critique/revise), while
    body chunks in the SAME job still receive the full 3-call reflective
    sequence."""
    canned = FakeTranslationProvider(
        canned_unit=TranslationUnit(translation="Texto traducido.", summary_update="Summary.")
    )
    orch, provider, store = make_orchestrator(provider=canned)
    config = make_config(quality_mode="reflective")
    job = make_job(config, total=2)

    nav_chunk = make_nav_label_chunk(0)
    body_chunk = Chunk(index=1, source_text="Body chunk text.", status=ChunkStatus.PENDING)
    chunks = [nav_chunk, body_chunk]

    run_job(orch, job, chunks, store=store)

    # 1 call for the nav-label chunk + 3 calls for the body chunk = 4 total.
    assert provider.call_count == 4, (
        f"Expected 4 total calls (1 nav-label + 3 reflective body), got {provider.call_count}"
    )

    # The FIRST call must be the nav-label chunk's single pass; calls 2-4 are
    # the body chunk's draft/critique/revise sequence (chunks processed in
    # index order).
    nav_call_user = provider.call_log[0][1]
    assert nav_chunk.source_text in nav_call_user

    body_call_users = [c[1] for c in provider.call_log[1:]]
    assert any(body_chunk.source_text in u for u in body_call_users), (
        "Body chunk's draft call must appear among calls 2-4"
    )


def test_nav_label_chunk_cost_projection_uses_single_pass_even_when_reflective():
    """_project_chunk_cost must use passes=1 for a nav-label chunk even under
    quality_mode='reflective' — the budget guard must not over-project 3x
    cost for a chunk that will actually only make 1 call."""
    orch, provider, _ = make_orchestrator()
    config = make_config(quality_mode="reflective")

    nav_chunk = make_nav_label_chunk(0, text="Chapter One")
    body_chunk = Chunk(index=1, source_text="Chapter One", status=ChunkStatus.PENDING)

    nav_projection = orch._project_chunk_cost(nav_chunk, config)
    body_projection = orch._project_chunk_cost(body_chunk, config)

    # Same source_text, same config — the ONLY difference is nav_chunk's
    # meta["kind"]. Body (ordinary) chunk projects reflective (3x); nav-label
    # chunk projects fast (1x) — nav_projection must be exactly 1/3 of body's.
    assert body_projection == pytest.approx(nav_projection * 3, rel=1e-9), (
        f"nav-label projection ({nav_projection}) must be 1/3 of the reflective "
        f"body projection ({body_projection}) — nav-label chunks must project "
        f"passes=1 even under quality_mode='reflective'."
    )


def test_project_chunk_cost_includes_system_prompt_overhead():
    """The budget-guard projection must include the per-call system prompt
    (static block + dynamic budget), the tool schema, and the JSON-envelope
    output — not just source tokens + a flat 150 (bug: 16x under-estimate on
    SRT chunks). It also applies the provider retry-waste factor so the guard
    protects against the CEILING."""
    import json as _json

    from borgesica.domain.cost import (
        _DYNAMIC_BLOCK_BUDGET_TOKENS,
        _OUTPUT_ENVELOPE_TOKENS,
        _waste_factor,
    )
    from borgesica.domain.models import translation_tool_schema

    orch, provider, _ = make_orchestrator()
    config = make_config(quality_mode="fast")
    chunk = Chunk(index=0, source_text="hello world", status=ChunkStatus.PENDING)

    projection = orch._project_chunk_cost(chunk, config)

    static_tokens = provider.count_tokens(
        orch._ctx.get_static_block(config), config.model
    )
    schema_tokens = provider.count_tokens(
        _json.dumps(translation_tool_schema(None)), config.model
    )
    src_tokens = 2  # "hello world" with the word-count fake
    in_price, out_price = provider.price(config.model)
    base = (
        (src_tokens + static_tokens + _DYNAMIC_BLOCK_BUDGET_TOKENS + schema_tokens)
        / 1_000_000 * in_price
        + (src_tokens + _OUTPUT_ENVELOPE_TOKENS) / 1_000_000 * out_price
    )
    expected = base * _waste_factor(provider)
    assert projection == pytest.approx(expected, rel=1e-9), (
        f"Projection {projection} must include system-prompt + tool-schema "
        f"overhead and the retry-waste ceiling factor (expected {expected})"
    )


def test_project_chunk_cost_scales_with_provider_waste_factor():
    """A provider that declares a heavier retry_waste_factor projects a
    proportionally higher ceiling for the same chunk."""
    reliable, _, _ = make_orchestrator()

    wasteful_provider = FakeTranslationProvider()
    wasteful_provider.retry_waste_factor = 6.0  # declare a heavy ceiling
    wasteful, _, _ = make_orchestrator(provider=wasteful_provider)

    config = make_config(quality_mode="fast")
    chunk = Chunk(index=0, source_text="hello world foo", status=ChunkStatus.PENDING)

    from borgesica.domain.cost import _waste_factor

    ratio = _waste_factor(wasteful_provider) / _waste_factor(reliable._provider)
    assert wasteful._project_chunk_cost(chunk, config) == pytest.approx(
        reliable._project_chunk_cost(chunk, config) * ratio, rel=1e-9
    )


def test_nav_label_chunk_fast_mode_unchanged_single_pass():
    """Regression: quality_mode='fast' job with a nav-label chunk → unchanged
    (still 1 pass — this path was already 1 pass; confirm the new branch
    doesn't accidentally special-case fast mode differently)."""
    orch, provider, store = make_orchestrator()
    config = make_config(quality_mode="fast")
    job = make_job(config, total=1)

    nav_chunk = make_nav_label_chunk(0)
    chunks = [nav_chunk]

    run_job(orch, job, chunks, store=store)

    assert provider.call_count == 1, (
        f"Expected exactly 1 call for a nav-label chunk under fast mode, got {provider.call_count}"
    )


# ===========================================================================
# 22. SRT segmented contract: chunks with meta["cue_batches"] request the
#     translations ARRAY (segment_count=N) instead of the "\n\n" convention.
#
# Root fix for jobs 0b86d4f2 / 80a1ad82 (Backrooms, DeepSeek): on
# unpunctuated speech-to-text fragments the model "corrects" blank-line
# separators and regroups 25 cues into 5 (or 1) semantic paragraphs; retries
# never help because that is trained instinct about well-formed text. An
# array of exactly N strings is structural — models fill arrays reliably.
# ===========================================================================


def make_srt_chunk(cue_texts: list[str], index: int = 0) -> Chunk:
    """Build a batch Chunk exactly as SrtChunker produces it."""
    return Chunk(
        index=index,
        source_text="\n\n".join(cue_texts),
        meta={
            "cue_batches": [
                {
                    "cue_index": i + 1,
                    "start": "00:00:01,000",
                    "end": "00:00:02,000",
                    "text": t,
                }
                for i, t in enumerate(cue_texts)
            ],
            "line_length": 42,
        },
    )


def test_srt_chunk_passes_segment_count_and_joins_array():
    """An SRT batch of 3 cues requests segment_count=3; the compliant array
    is joined with "\n\n" for the checkpoint (writer contract unchanged)."""
    store = InMemoryCheckpointStore()
    orch, provider, _ = make_orchestrator(store=store)
    config = make_config()
    job = make_job(config, total=1)
    cues = ["and then we went", "down the hallway that", "never seems to end"]
    chunks = [make_srt_chunk(cues)]

    result = run_job(orch, job, chunks, store=store)

    assert provider.segment_count_log == [3]
    assert provider.call_count == 1
    saved = store.load_chunks(job.id)
    assert saved[0].status == ChunkStatus.DONE
    assert saved[0].translated_text == "\n\n".join(f"[translated] {c}" for c in cues)
    assert result.status == JobStatus.DONE


def test_srt_system_prompt_declares_segment_count():
    """The per-chunk system prompt must state the exact segment count."""
    store = InMemoryCheckpointStore()
    orch, provider, _ = make_orchestrator(store=store)
    config = make_config()
    job = make_job(config, total=1)
    chunks = [make_srt_chunk(["one", "two", "three", "four"])]

    run_job(orch, job, chunks, store=store)

    system_sent = provider.call_log[0][0]
    assert "exactly 4" in system_sent
    assert "translations" in system_sent


def test_prose_chunk_does_not_pass_segment_count():
    """Chunks without cue_batches keep the legacy call — segment_count None."""
    store = InMemoryCheckpointStore()
    orch, provider, _ = make_orchestrator(store=store)
    config = make_config()
    job = make_job(config, total=1)
    chunks = [Chunk(index=0, source_text="Plain prose paragraph.")]

    run_job(orch, job, chunks, store=store)

    assert provider.segment_count_log == [None]


def test_srt_array_segment_internal_blank_lines_normalized():
    """A blank line INSIDE one array item would desynchronize the writer's
    "\n\n" arithmetic — it must be collapsed to a single newline on join."""
    store = InMemoryCheckpointStore()

    class BlankLineInsideSegmentProvider(FakeTranslationProvider):
        def translate(self, system, user, model, segment_count=None):
            self.call_log.append((system, user, model))
            self.segment_count_log.append(segment_count)
            unit = TranslationUnit(
                translations=["primera\n\n\nlínea", "segunda"],
                summary_update="Summary.",
            )
            return TranslationResult(unit=unit, usage=Usage())

    provider = BlankLineInsideSegmentProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)
    config = make_config()
    job = make_job(config, total=1)
    chunks = [make_srt_chunk(["cue a", "cue b"])]

    run_job(orch, job, chunks, store=store)

    saved = store.load_chunks(job.id)
    assert saved[0].status == ChunkStatus.DONE
    assert saved[0].translated_text == "primera\nlínea\n\nsegunda"
    assert len(saved[0].translated_text.split("\n\n")) == 2


def test_srt_wrong_array_length_retries_then_succeeds():
    """Wrong array length on attempt 1, aligned on attempt 2 → 2 calls, DONE."""
    store = InMemoryCheckpointStore()

    class WrongLengthOnceProvider(FakeTranslationProvider):
        def translate(self, system, user, model, segment_count=None):
            n = len(self.call_log)
            self.call_log.append((system, user, model))
            self.segment_count_log.append(segment_count)
            if n == 0:
                unit = TranslationUnit(
                    translations=["todo junto en uno"],
                    summary_update="Summary.",
                )
            else:
                unit = TranslationUnit(
                    translations=["uno", "dos", "tres"],
                    summary_update="Summary.",
                )
            return TranslationResult(unit=unit, usage=Usage())

    provider = WrongLengthOnceProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)
    config = make_config()
    job = make_job(config, total=1)
    chunks = [make_srt_chunk(["a", "b", "c"])]

    result = run_job(orch, job, chunks, store=store)

    assert len(provider.call_log) == 2
    saved = store.load_chunks(job.id)
    assert saved[0].status == ChunkStatus.DONE
    assert saved[0].translated_text == "uno\n\ndos\n\ntres"
    assert result.status == JobStatus.DONE


def test_srt_wrong_array_length_marks_chunk_failed():
    """Persistent misalignment on a cue-batch chunk: 3 attempts, then the
    chunk ends FAILED — NOT silently accepted as best effort. A best-effort
    misaligned batch is useless to the SrtWriter (it falls back to source
    text, shipping untranslated cues), so FAILED is honest: it surfaces in
    the skip summary and `resume` retries exactly these chunks. The
    strip/reinsert fallback must NOT fire (a 4th call cannot fix
    segmentation)."""
    store = InMemoryCheckpointStore()

    class AlwaysWrongLengthProvider(FakeTranslationProvider):
        def translate(self, system, user, model, segment_count=None):
            self.call_log.append((system, user, model))
            self.segment_count_log.append(segment_count)
            unit = TranslationUnit(
                translations=["todo el batch en un solo bloque"],
                summary_update="Summary.",
            )
            return TranslationResult(unit=unit, usage=Usage())

    provider = AlwaysWrongLengthProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)
    config = make_config()
    job = make_job(config, total=1)
    chunks = [make_srt_chunk(["a", "b", "c"])]

    result = run_job(orch, job, chunks, store=store)

    assert len(provider.call_log) == 3
    saved = store.load_chunks(job.id)
    assert saved[0].status == ChunkStatus.FAILED
    assert saved[0].translated_text is None
    # continue_on_error=True: the job still finishes DONE.
    assert result.status == JobStatus.DONE


def test_srt_user_prompt_numbers_segments():
    """Cue-batch chunks send the segments with explicit [k] index markers so
    segment boundaries are unambiguous (a model splitting a two-line cue into
    two items caused chunk-wide fallback to source text). The checkpoint
    format stays marker-free."""
    store = InMemoryCheckpointStore()
    provider = FakeTranslationProvider(
        canned_unit=TranslationUnit(
            translations=["uno\ndos", "tres"], summary_update="Summary."
        )
    )
    orch, _, _ = make_orchestrator(provider=provider, store=store)
    config = make_config()
    job = make_job(config, total=1)
    chunks = [make_srt_chunk(["a\nb", "c"])]

    run_job(orch, job, chunks, store=store)

    _, user, _ = provider.call_log[0]
    assert user == "[1]\na\nb\n\n[2]\nc"
    saved = store.load_chunks(job.id)
    assert saved[0].translated_text == "uno\ndos\n\ntres"


def test_srt_leading_index_markers_stripped_from_translations():
    """If the model echoes the [k] markers back in the translations array,
    they are stripped — markers are transport, never content."""
    store = InMemoryCheckpointStore()
    provider = FakeTranslationProvider(
        canned_unit=TranslationUnit(
            translations=["[1] uno", "[2]\ndos"], summary_update="Summary."
        )
    )
    orch, _, _ = make_orchestrator(provider=provider, store=store)
    config = make_config()
    job = make_job(config, total=1)
    chunks = [make_srt_chunk(["a", "b"])]

    run_job(orch, job, chunks, store=store)

    saved = store.load_chunks(job.id)
    assert saved[0].translated_text == "uno\n\ndos"


def test_srt_legacy_string_with_matching_segments_still_accepted():
    """A model that ignores the array but returns a "\n\n" string with the
    right segment count is still accepted (graceful degradation, 1 call)."""
    store = InMemoryCheckpointStore()

    class LegacyStringProvider(FakeTranslationProvider):
        def translate(self, system, user, model, segment_count=None):
            self.call_log.append((system, user, model))
            self.segment_count_log.append(segment_count)
            unit = TranslationUnit(
                translation="uno\n\ndos",
                summary_update="Summary.",
            )
            return TranslationResult(unit=unit, usage=Usage())

    provider = LegacyStringProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)
    config = make_config()
    job = make_job(config, total=1)
    chunks = [make_srt_chunk(["a", "b"])]

    run_job(orch, job, chunks, store=store)

    assert len(provider.call_log) == 1
    saved = store.load_chunks(job.id)
    assert saved[0].status == ChunkStatus.DONE
    assert saved[0].translated_text == "uno\n\ndos"


def test_srt_reflective_passes_segment_count_to_draft_and_revise_only():
    """Reflective SRT: draft and revise request the array; the critique call
    (notes, not a translation) must NOT carry segment_count."""
    store = InMemoryCheckpointStore()

    class ReflectiveSegmentedProvider(FakeTranslationProvider):
        def translate(self, system, user, model, segment_count=None):
            n = len(self.call_log)
            self.call_log.append((system, user, model))
            self.segment_count_log.append(segment_count)
            step = n % 3
            if step == 1:
                unit = TranslationUnit(
                    translation="Critique notes.", summary_update="Critique complete."
                )
            else:
                unit = TranslationUnit(
                    translations=["uno", "dos"], summary_update="Summary."
                )
            return TranslationResult(unit=unit, usage=Usage())

    provider = ReflectiveSegmentedProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)
    config = make_config(quality_mode="reflective")
    job = make_job(config, total=1)
    chunks = [make_srt_chunk(["a", "b"])]

    result = run_job(orch, job, chunks, store=store)

    assert provider.segment_count_log == [2, None, 2]
    saved = store.load_chunks(job.id)
    assert saved[0].status == ChunkStatus.DONE
    assert saved[0].translated_text == "uno\n\ndos"
    assert result.status == JobStatus.DONE


# ===========================================================================
# T2 — passed_validation provenance threading through _translate_with_retry's
# four return paths (spec: job-execution-core/"passed_validation persisted
# per chunk"; design decision #7, orchestrator.py:612-621).
# ===========================================================================


def test_passed_validation_true_on_first_try_success():
    """Return path 1 (happy): translation passes tag+segment validation on the
    FIRST attempt → persisted chunk.passed_validation is True."""
    orch, provider, store = make_orchestrator()
    config = make_config()
    job = make_job(config, total=1)
    chunks = make_chunks(1)

    run_job(orch, job, chunks, store=store)

    assert provider.call_count == 1
    saved = store.load_chunks(job.id)
    assert saved[0].status == ChunkStatus.DONE
    assert saved[0].passed_validation is True


def test_passed_validation_false_on_best_effort_segment_mismatch():
    """Return path 2 (best-effort accepted): a prose chunk that never produces
    a segment-compliant output is accepted DONE after 3 attempts (design
    decision #7 / orchestrator.py:612-621) with passed_validation=False."""
    store = InMemoryCheckpointStore()

    class AlwaysMergeProvider(FakeTranslationProvider):
        def translate(self, system: str, user: str, model: str) -> TranslationResult:
            self.call_log.append((system, user, model))
            unit = TranslationUnit(
                translation="Párrafo uno. Párrafo dos.",
                summary_update="Summary.",
            )
            in_tok = self.count_tokens(system + " " + user, model)
            out_tok = self.count_tokens(unit.translation, model)
            return TranslationResult(unit=unit, usage=Usage(input_tokens=in_tok, output_tokens=out_tok))

    provider = AlwaysMergeProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=1)
    chunks = [Chunk(index=0, source_text="Paragraph one.\n\nParagraph two.")]

    result = run_job(orch, job, chunks, store=store)

    assert provider.call_count == 3
    saved = store.load_chunks(job.id)
    assert saved[0].status == ChunkStatus.DONE
    assert saved[0].passed_validation is False
    assert result.status == JobStatus.DONE


def test_passed_validation_true_on_fallback_success():
    """Return path 4 (strip/reinsert fallback): all 3 tags-in-text attempts
    fail, but the deterministic fallback call passes validate_tags → chunk
    DONE with passed_validation=True (fallback success IS a validation pass,
    distinct from the best-effort segment-mismatch acceptance)."""
    store = InMemoryCheckpointStore()

    class AllTagsFailThenPlainProvider(FakeTranslationProvider):
        def translate(self, system: str, user: str, model: str) -> TranslationResult:
            n = len(self.call_log)
            self.call_log.append((system, user, model))
            unit = TranslationUnit(
                translation="El zorro rápido.",
                summary_update="Summary.",
            )
            in_tok = self.count_tokens(system + " " + user, model)
            out_tok = self.count_tokens(unit.translation, model)
            return TranslationResult(unit=unit, usage=Usage(input_tokens=in_tok, output_tokens=out_tok))

    provider = AllTagsFailThenPlainProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=1)
    chunks = [Chunk(index=0, source_text="The <i>quick</i> fox.")]

    result = run_job(orch, job, chunks, store=store)

    assert provider.call_count == 4  # 3 primary + 1 fallback
    saved = store.load_chunks(job.id)
    assert saved[0].status == ChunkStatus.DONE
    assert saved[0].passed_validation is True
    assert result.status == JobStatus.DONE


def test_passed_validation_false_on_chunk_failed_tag_mismatch():
    """Return path 3 (FAILED): both primary and fallback exhaust without a
    tag-valid output → chunk FAILED with passed_validation=False."""
    store = InMemoryCheckpointStore()
    provider = _always_mismatch_except({"Chunk 0 text."})()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config(continue_on_error=True)
    job = make_job(config, total=1)
    chunks = [Chunk(index=0, source_text="Chunk 0 text.")]

    result = run_job(orch, job, chunks, store=store)

    saved = store.load_chunks(job.id)
    assert saved[0].status == ChunkStatus.FAILED
    assert saved[0].passed_validation is False
    assert result.status == JobStatus.DONE


def test_passed_validation_false_on_segmented_misalignment_failed():
    """Return path 3 (FAILED), segmented-cue variant: a translations array
    that never aligns with the cue count ends the chunk FAILED (not
    best-effort — SRT cue batches cannot absorb a misalignment) with
    passed_validation=False."""
    store = InMemoryCheckpointStore()

    class MisalignedSegmentsProvider(FakeTranslationProvider):
        def translate(self, system, user, model, segment_count=None):
            self.call_log.append((system, user, model))
            self.segment_count_log.append(segment_count)
            # Always return ONE fewer translation than requested — permanently
            # misaligned with the cue count.
            unit = TranslationUnit(
                translations=["uno"] if segment_count else ["uno", "dos"],
                summary_update="Summary.",
            )
            return TranslationResult(unit=unit, usage=Usage())

    provider = MisalignedSegmentsProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config(continue_on_error=True)
    job = make_job(config, total=1)
    chunks = [make_srt_chunk(["a", "b"])]

    result = run_job(orch, job, chunks, store=store)

    saved = store.load_chunks(job.id)
    assert saved[0].status == ChunkStatus.FAILED
    assert saved[0].passed_validation is False
    assert result.status == JobStatus.DONE


def test_orchestrator_accepts_provider_name_default_unknown():
    """Orchestrator ctor gains an additive provider_name kwarg (design decision
    #6) defaulting to 'unknown' so existing call sites are unaffected."""
    provider = FakeTranslationProvider()
    store = InMemoryCheckpointStore()
    ctx = ContextManager(provider)
    cost_est = CostEstimator(provider)
    orch = TranslationOrchestrator(
        provider=provider,
        checkpoint=store,
        context_manager=ctx,
        cost_estimator=cost_est,
    )
    assert orch._provider_name == "unknown"


def test_orchestrator_accepts_provider_name_explicit_value():
    """Triangulation: explicit provider_name is stored verbatim."""
    provider = FakeTranslationProvider()
    store = InMemoryCheckpointStore()
    ctx = ContextManager(provider)
    cost_est = CostEstimator(provider)
    orch = TranslationOrchestrator(
        provider=provider,
        checkpoint=store,
        context_manager=ctx,
        cost_estimator=cost_est,
        provider_name="anthropic",
    )
    assert orch._provider_name == "anthropic"


# ===========================================================================
# T4 — corpus capture hook (design decision #6/#10): a CorpusSample is
# written at the real chunk-DONE point (translated, not passthrough/FAILED).
# Best-effort: a raising CorpusStore never fails the job. No store → no
# behavior change.
# ===========================================================================


def test_corpus_store_receives_sample_on_chunk_done():
    """A DONE (translated) chunk writes a CorpusSample carrying provenance
    (source/translated text, provider, model, quality_mode, passed_validation)."""
    store = InMemoryCheckpointStore()
    corpus = FakeCorpusStore()
    orch, provider, _ = make_orchestrator(store=store, corpus_store=corpus, provider_name="anthropic")
    config = make_config()
    job = make_job(config, total=1)
    chunks = make_chunks(1)

    run_job(orch, job, chunks, config=config, store=store)

    sample = corpus.samples[(job.id, 0)]
    assert sample.source_text == chunks[0].source_text
    assert sample.translated_text is not None
    assert sample.provider == "anthropic"
    assert sample.model == config.model
    assert sample.quality_mode == config.quality_mode
    assert sample.passed_validation is True


def test_corpus_store_failure_does_not_fail_job():
    """A raising CorpusStore.save_sample() MUST NOT fail, pause, or otherwise
    affect the translation job (design decision #10 — silent best-effort)."""
    store = InMemoryCheckpointStore()
    corpus = FakeCorpusStore(raise_on_save=True)
    orch, provider, _ = make_orchestrator(store=store, corpus_store=corpus)
    config = make_config()
    job = make_job(config, total=3)
    chunks = make_chunks(3)

    result = run_job(orch, job, chunks, config=config, store=store)

    assert result.status == JobStatus.DONE
    assert provider.call_count == 3
    assert corpus.samples == {}


def test_no_corpus_store_no_writes():
    """Omitting corpus_store (default None) is a pure no-op — no attribute
    error, job completes exactly as before T4."""
    orch, provider, store = make_orchestrator()
    config = make_config()
    job = make_job(config, total=2)
    chunks = make_chunks(2)

    result = run_job(orch, job, chunks, config=config, store=store)

    assert result.status == JobStatus.DONE
    assert provider.call_count == 2


def test_corpus_store_skips_passthrough_chunk():
    """A pass-through chunk (no translatable prose) makes zero provider calls
    and MUST NOT be captured to the corpus store (design decision #10)."""
    store = InMemoryCheckpointStore()
    corpus = FakeCorpusStore()
    orch, provider, _ = make_orchestrator(store=store, corpus_store=corpus)
    config = make_config()
    job = make_job(config, total=1)
    chunks = [Chunk(index=0, source_text="<span></span>")]

    run_job(orch, job, chunks, config=config, store=store)

    assert provider.call_count == 0
    assert corpus.samples == {}


def test_corpus_store_skips_failed_chunk():
    """A chunk that ends FAILED (no translated_text) MUST NOT be captured to
    the corpus store (design decision #10)."""
    store = InMemoryCheckpointStore()
    provider = _AlwaysMismatchProvider()
    corpus = FakeCorpusStore()
    orch, _, _ = make_orchestrator(provider=provider, store=store, corpus_store=corpus)
    config = make_config(continue_on_error=True)
    job = make_job(config, total=1)
    chunks = [Chunk(index=0, source_text="No tags here.")]

    result = run_job(orch, job, chunks, config=config, store=store)

    assert result.status == JobStatus.DONE
    saved = {c.index: c for c in store.load_chunks(job.id)}
    assert saved[0].status == ChunkStatus.FAILED
    assert corpus.samples == {}


# ===========================================================================
# T4b amendment — validation failure detail (spec-conformance gap): a
# best-effort corpus sample MUST carry the actual validator issue messages
# in validation_errors; a cleanly-passing sample MUST have validation_errors
# None; a FAILED chunk still makes zero corpus writes (unchanged from T4).
# ===========================================================================


def test_corpus_store_best_effort_chunk_carries_validator_messages():
    """A prose chunk accepted as best-effort (segment-count mismatch on every
    attempt) is captured with validation_errors populated with the actual
    validator issue messages (JSON list of strings)."""
    import json

    store = InMemoryCheckpointStore()
    corpus = FakeCorpusStore()

    class AlwaysMergeProvider(FakeTranslationProvider):
        def translate(self, system: str, user: str, model: str) -> TranslationResult:
            self.call_log.append((system, user, model))
            unit = TranslationUnit(
                translation="Párrafo uno. Párrafo dos.",
                summary_update="Summary.",
            )
            in_tok = self.count_tokens(system + " " + user, model)
            out_tok = self.count_tokens(unit.translation, model)
            return TranslationResult(unit=unit, usage=Usage(input_tokens=in_tok, output_tokens=out_tok))

    provider = AlwaysMergeProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store, corpus_store=corpus)
    config = make_config()
    job = make_job(config, total=1)
    chunks = [Chunk(index=0, source_text="Paragraph one.\n\nParagraph two.")]

    run_job(orch, job, chunks, config=config, store=store)

    saved = store.load_chunks(job.id)[0]
    assert saved.status == ChunkStatus.DONE
    assert saved.passed_validation is False

    sample = corpus.samples[(job.id, 0)]
    assert sample.passed_validation is False
    assert sample.validation_errors is not None
    issues = json.loads(sample.validation_errors)
    assert isinstance(issues, list) and len(issues) > 0
    assert any("segment" in issue.lower() for issue in issues)


def test_corpus_store_passing_chunk_validation_errors_none():
    """A chunk that passes validation cleanly (first attempt) is captured
    with validation_errors=None."""
    store = InMemoryCheckpointStore()
    corpus = FakeCorpusStore()
    orch, _, _ = make_orchestrator(store=store, corpus_store=corpus)
    config = make_config()
    job = make_job(config, total=1)
    chunks = make_chunks(1)

    run_job(orch, job, chunks, config=config, store=store)

    sample = corpus.samples[(job.id, 0)]
    assert sample.passed_validation is True
    assert sample.validation_errors is None
