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
from datetime import UTC, datetime
from typing import Callable

import pytest

from borgesica.domain.context import ContextManager
from borgesica.domain.cost import CostEstimator
from borgesica.domain.errors import BudgetExceeded, JobStateError
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
from tests.fakes import FakeTranslationProvider, InMemoryCheckpointStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(
    quality_mode: str = "fast",
    budget_usd: float | None = None,
) -> JobConfig:
    return JobConfig(
        source_type=SourceType.SRT,
        model="claude-test",
        quality_mode=quality_mode,  # type: ignore[arg-type]
        budget_usd=budget_usd,
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


def test_chunk_persisted_done_before_next_call(monkeypatch):
    """Chunk 0 must be saved DONE before provider.translate is called for chunk 1."""
    from borgesica.domain.errors import MalformedOutput

    store = InMemoryCheckpointStore()
    provider = FakeTranslationProvider(fail_on={1})  # call index 1 fails
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
    job = make_job(config, total=3)
    chunks = make_chunks(3)
    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, Glossary())

    with pytest.raises(Exception):  # MalformedOutput bubbles up after retries
        orch.run(
            job=job,
            chunks=chunks,
            glossary=Glossary(),
            config=config,
            on_progress=lambda p: None,
            cancel_flag=threading.Event(),
        )

    saved = {c.index: c for c in store.load_chunks(job.id)}
    assert saved[0].status == ChunkStatus.DONE
    # chunks 1 and 2 may still be PENDING or FAILED depending on retry behaviour;
    # the key invariant is that chunk 0 IS done.


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
    orch, provider, store = make_orchestrator()
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
# 17. Tag mismatch all 3 attempts → chunk FAILED, job PAUSED
# ===========================================================================


def test_tag_mismatch_all_retries_fail_chunk_failed_job_paused():
    store = InMemoryCheckpointStore()

    class AlwaysMismatchProvider(FakeTranslationProvider):
        def translate(self, system: str, user: str, model: str) -> TranslationResult:
            self.call_log.append((system, user, model))
            # Always return extra tags — source has no tags
            unit = TranslationUnit(
                translation="<i>Always mismatched</i>",
                summary_update="Summary.",
            )
            in_tok = self.count_tokens(system + " " + user, model)
            out_tok = self.count_tokens(unit.translation, model)
            return TranslationResult(unit=unit, usage=Usage(input_tokens=in_tok, output_tokens=out_tok))

    provider = AlwaysMismatchProvider()
    orch, _, _ = make_orchestrator(provider=provider, store=store)

    config = make_config()
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

    # M2-0: 3 primary (tags-in-text) attempts + 1 fallback (strip→translate→reinsert) call
    # = 4 total provider calls before chunk is marked FAILED.
    assert provider.call_count == 4
    saved = {c.index: c for c in store.load_chunks(job.id)}
    assert saved[0].status == ChunkStatus.FAILED
    assert result.status == JobStatus.PAUSED


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
    validate_tags, chunk is FAILED and job is PAUSED.
    Spec: subtitle-translation/inline-tags-in-text scenario 'fallback also fails'.
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

    config = make_config()
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
                text = f"[revised] {user}"
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

    config = make_config()
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
