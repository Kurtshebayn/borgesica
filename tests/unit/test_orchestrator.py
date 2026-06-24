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
    TranslationUnit,
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
        def translate(self, system: str, user: str, model: str) -> TranslationUnit:
            n = len(self.call_log)
            self.call_log.append((system, user, model))
            # call 0 = draft, call 1 = critique, call 2 = revise
            step = n % 3
            if step == 0:
                return TranslationUnit(
                    translation="DRAFT_TEXT",
                    summary_update="Draft summary.",
                )
            elif step == 1:
                return TranslationUnit(
                    translation="CRITIQUE_TEXT",
                    summary_update="Critique summary.",
                )
            else:
                return TranslationUnit(
                    translation="REVISED_TEXT",
                    summary_update="Revised summary.",
                )

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
        def translate(self, system: str, user: str, model: str) -> TranslationUnit:
            n = len(self.call_log)
            self.call_log.append((system, user, model))
            if n == 0:
                # First call: translation has mismatched tags (extra <i>)
                return TranslationUnit(
                    translation="<i>Texto</i> <i>extra</i>",
                    summary_update="Summary.",
                )
            else:
                # Second call: correct — no tags (original source also has no tags)
                return TranslationUnit(
                    translation="Texto correcto.",
                    summary_update="Summary.",
                )

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
        def translate(self, system: str, user: str, model: str) -> TranslationUnit:
            self.call_log.append((system, user, model))
            # Always return extra tags — source has no tags
            return TranslationUnit(
                translation="<i>Always mismatched</i>",
                summary_update="Summary.",
            )

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

    # 3 attempts for chunk 0 (initial + 2 retries)
    assert provider.call_count == 3
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
        def translate(self, system: str, user: str, model: str) -> TranslationUnit:
            n = len(self.call_log)
            self.call_log.append((system, user, model))
            if n == 2:  # 3rd call = chunk index 2
                return TranslationUnit(
                    translation="Texto con Zorblax.",
                    summary_update="Summary with Zorblax.",
                    glossary_additions=[new_term],
                )
            return TranslationUnit(
                translation=f"[translated] {user}",
                summary_update="Summary.",
            )

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
        def translate(self, system: str, user: str, model: str) -> TranslationUnit:
            self.call_log.append((system, user, model))
            return TranslationUnit(
                translation="Texto con Mystara.",
                summary_update="Summary.",
                glossary_additions=[conflict_entry],
            )

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
        def translate(self, system: str, user: str, model: str) -> TranslationUnit:
            self.call_log.append((system, user, model))
            summaries_seen.append(system)
            n = len(self.call_log) - 1
            return TranslationUnit(
                translation=f"Translation {n}.",
                summary_update=f"Summary of chunk {n}.",
            )

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
