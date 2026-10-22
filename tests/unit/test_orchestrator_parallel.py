"""Tests for TranslationOrchestrator.run(workers=N): chunks translated
concurrently.

Workers only make provider calls; every checkpoint write, the budget
reservation and the progress callback stay on the thread that called run().
A parallel run freezes the glossary: mid-run additions are not applied.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from borgesica.domain.context import ContextManager
from borgesica.domain.cost import CostEstimator
from borgesica.domain.errors import BudgetExceeded, ProviderError
from borgesica.domain.models import (
    Chunk,
    ChunkStatus,
    Glossary,
    GlossaryEntry,
    Job,
    JobStatus,
    Progress,
    TranslationResult,
    TranslationUnit,
)
from borgesica.domain.orchestrator import TranslationOrchestrator
from tests.fakes import FakeTranslationProvider, InMemoryCheckpointStore
from tests.unit.test_orchestrator import make_chunks, make_config, make_job

# Generous: a test only waits this long when the code under test is wrong.
_TIMEOUT = 5.0


@dataclass
class HookedProvider(FakeTranslationProvider):
    """FakeTranslationProvider that runs ``before(user)`` ahead of each call
    and records every prompt it was sent (``started``, even when ``before``
    raises) and how many calls were in flight at once."""

    before: Callable[[str], None] = lambda user: None
    started: list[str] = field(default_factory=list)
    in_flight: int = 0
    max_in_flight: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def translate(
        self, system: str, user: str, model: str, segment_count: int | None = None
    ) -> TranslationResult:
        with self._lock:
            self.started.append(user)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            self.before(user)
            return super().translate(system, user, model, segment_count)
        finally:
            with self._lock:
                self.in_flight -= 1


def _orchestrator(
    provider: FakeTranslationProvider, store: InMemoryCheckpointStore
) -> TranslationOrchestrator:
    return TranslationOrchestrator(
        provider=provider,
        checkpoint=store,
        context_manager=ContextManager(),
        cost_estimator=CostEstimator(provider),
    )


def _run(
    orch: TranslationOrchestrator,
    store: InMemoryCheckpointStore,
    job: Job,
    chunks: list[Chunk],
    *,
    workers: int,
    glossary: Glossary | None = None,
    cancel_flag: threading.Event | None = None,
    on_progress: Callable[[Progress], None] | None = None,
) -> Job:
    glossary = glossary if glossary is not None else Glossary()
    store.save_job(job)
    for c in chunks:
        store.save_chunk(job.id, c)
    store.save_glossary(job.id, glossary)
    return orch.run(
        job=job,
        chunks=chunks,
        glossary=glossary,
        config=job.config,
        on_progress=on_progress or (lambda p: None),
        cancel_flag=cancel_flag or threading.Event(),
        workers=workers,
    )


def _chunk_of(user: str) -> int:
    """The chunk index a fake prompt belongs to ("Chunk {i} text.")."""
    return int(user.split()[1])


def _statuses(store: InMemoryCheckpointStore, job_id: str) -> dict[int, ChunkStatus]:
    return {c.index: c.status for c in store.load_chunks(job_id)}


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_parallel_run_keeps_up_to_workers_calls_in_flight():
    """Three calls must be in flight together, or none of them can pass the
    barrier: a sequential run would time out on the first call."""
    barrier = threading.Barrier(3, timeout=_TIMEOUT)
    provider = HookedProvider(before=lambda user: barrier.wait())
    store = InMemoryCheckpointStore()
    job = make_job(make_config(), total=6)

    result = _run(_orchestrator(provider, store), store, job, make_chunks(6), workers=3)

    assert result.status == JobStatus.DONE
    assert provider.max_in_flight == 3
    assert set(_statuses(store, job.id).values()) == {ChunkStatus.DONE}
    assert sorted(_chunk_of(u) for _, u, _ in provider.call_log) == list(range(6))


def test_parallel_run_never_exceeds_the_worker_count():
    provider = HookedProvider()
    store = InMemoryCheckpointStore()
    job = make_job(make_config(), total=12)

    _run(_orchestrator(provider, store), store, job, make_chunks(12), workers=4)

    assert 1 <= provider.max_in_flight <= 4
    assert provider.call_count == 12


def test_parallel_run_accrues_the_cost_of_every_call():
    sequential_provider = FakeTranslationProvider()
    sequential_store = InMemoryCheckpointStore()
    sequential = _run(
        _orchestrator(sequential_provider, sequential_store),
        sequential_store,
        make_job(make_config(), total=5),
        make_chunks(5),
        workers=1,
    )

    provider = FakeTranslationProvider()
    store = InMemoryCheckpointStore()
    parallel = _run(
        _orchestrator(provider, store),
        store,
        make_job(make_config(), total=5),
        make_chunks(5),
        workers=3,
    )

    assert parallel.cost_usd == pytest.approx(sequential.cost_usd)
    assert parallel.cost_usd > 0
    assert store.load_job(parallel.id).cost_usd == pytest.approx(parallel.cost_usd)


def test_workers_below_one_is_rejected():
    provider = FakeTranslationProvider()
    store = InMemoryCheckpointStore()
    with pytest.raises(ValueError, match="workers"):
        _run(
            _orchestrator(provider, store),
            store,
            make_job(make_config(), total=1),
            make_chunks(1),
            workers=0,
        )
    assert provider.call_count == 0


# ---------------------------------------------------------------------------
# Frozen glossary
# ---------------------------------------------------------------------------


def _proposing_provider() -> FakeTranslationProvider:
    return FakeTranslationProvider(
        canned_unit=TranslationUnit(
            translation="[translated] Chunk text.",
            summary_update="Fake summary.",
            glossary_additions=[GlossaryEntry(term="Mystara", translation="Mistara")],
        )
    )


def test_parallel_run_does_not_apply_mid_run_glossary_additions():
    provider = _proposing_provider()
    store = InMemoryCheckpointStore()
    reviewed = Glossary(entries=[GlossaryEntry(term="Vis", translation="Vis", locked=True)])
    job = make_job(make_config(), total=4)

    _run(
        _orchestrator(provider, store), store, job, make_chunks(4),
        workers=2, glossary=reviewed,
    )

    assert provider.call_count == 4
    for system, _, _ in provider.call_log:
        assert "Mystara" not in system
        assert "Vis" in system
    assert store.load_glossary(job.id) == reviewed


def test_sequential_run_still_applies_mid_run_glossary_additions():
    provider = _proposing_provider()
    store = InMemoryCheckpointStore()
    job = make_job(make_config(), total=2)

    _run(_orchestrator(provider, store), store, job, make_chunks(2), workers=1)

    assert "Mystara" not in provider.call_log[0][0]
    assert "Mystara" in provider.call_log[1][0]
    assert [e.term for e in store.load_glossary(job.id).entries] == ["Mystara"]


# ---------------------------------------------------------------------------
# Budget: the projection of every in-flight chunk is reserved
# ---------------------------------------------------------------------------


def _projection(orch: TranslationOrchestrator, job: Job) -> float:
    return orch._project_chunk_cost(make_chunks(1)[0], job.config, Glossary())


def test_budget_reserves_in_flight_projections_before_starting_a_chunk():
    """Room for one projection but not two: the run never has two calls in
    flight, yet finishes, because each call costs less than projected."""
    provider = HookedProvider()
    store = InMemoryCheckpointStore()
    orch = _orchestrator(provider, store)
    projected = _projection(orch, make_job(make_config(), total=1))
    # Every chunk's real cost is far below its projection (the projection
    # carries the retry-waste factor), so the budget fits all four calls
    # made one at a time, but never two projections at once.
    job = make_job(make_config(budget_usd=projected * 1.9), total=4)

    result = _run(orch, store, job, make_chunks(4), workers=4)

    assert result.status == JobStatus.DONE
    assert provider.max_in_flight == 1
    assert provider.call_count == 4
    assert result.cost_usd <= job.config.budget_usd


def test_budget_pauses_once_nothing_in_flight_and_no_room_is_left():
    provider = HookedProvider()
    store = InMemoryCheckpointStore()
    orch = _orchestrator(provider, store)
    projected = _projection(orch, make_job(make_config(), total=1))
    job = make_job(make_config(budget_usd=projected * 1.01), total=4)

    with pytest.raises(BudgetExceeded):
        _run(orch, store, job, make_chunks(4), workers=4)

    assert provider.call_count == 1
    statuses = _statuses(store, job.id)
    assert statuses[0] == ChunkStatus.DONE
    assert [statuses[i] for i in (1, 2, 3)] == [ChunkStatus.PENDING] * 3
    paused = store.load_job(job.id)
    assert paused.status == JobStatus.PAUSED
    assert 0 < paused.cost_usd <= job.config.budget_usd


# ---------------------------------------------------------------------------
# Cancel, strict failure, unexpected errors: in-flight chunks are saved
# ---------------------------------------------------------------------------


def test_cancel_lets_in_flight_chunks_finish_and_starts_no_more():
    cancel = threading.Event()
    barrier = threading.Barrier(2, timeout=_TIMEOUT)

    def before(user: str) -> None:
        barrier.wait()  # both chunks are in flight before the cancel
        if _chunk_of(user) == 0:
            cancel.set()
        # Neither call returns before the cancel, so no free worker can
        # start a third chunk ahead of it.
        assert cancel.wait(_TIMEOUT)

    provider = HookedProvider(before=before)
    store = InMemoryCheckpointStore()
    job = make_job(make_config(), total=6)

    result = _run(
        _orchestrator(provider, store), store, job, make_chunks(6),
        workers=2, cancel_flag=cancel,
    )

    assert result.status == JobStatus.CANCELLED
    assert provider.call_count == 2
    statuses = _statuses(store, job.id)
    assert statuses[0] == statuses[1] == ChunkStatus.DONE
    assert [statuses[i] for i in range(2, 6)] == [ChunkStatus.PENDING] * 4
    saved = store.load_job(job.id)
    assert saved.status == JobStatus.CANCELLED
    assert saved.cost_usd == pytest.approx(result.cost_usd)
    assert result.cost_usd > 0


def test_strict_failure_drains_in_flight_chunks_then_pauses():
    chunk0_done = threading.Event()
    calls_for_chunk0 = 0

    def before(user: str) -> None:
        nonlocal calls_for_chunk0
        if _chunk_of(user) == 0:
            calls_for_chunk0 += 1
            if calls_for_chunk0 == 4:  # 3 attempts + the strip/reinsert fallback
                chunk0_done.set()
            raise ProviderError(status_code=500)
        # Chunk 1 stays in flight until chunk 0 has failed for good.
        assert chunk0_done.wait(_TIMEOUT)

    provider = HookedProvider(before=before)
    store = InMemoryCheckpointStore()
    job = make_job(make_config(continue_on_error=False), total=5)

    result = _run(_orchestrator(provider, store), store, job, make_chunks(5), workers=2)

    assert result.status == JobStatus.PAUSED
    statuses = _statuses(store, job.id)
    assert statuses[0] == ChunkStatus.FAILED
    assert statuses[1] == ChunkStatus.DONE
    assert [statuses[i] for i in (2, 3, 4)] == [ChunkStatus.PENDING] * 3
    assert sorted({_chunk_of(u) for u in provider.started}) == [0, 1]
    assert store.load_job(job.id).status == JobStatus.PAUSED


def test_unexpected_error_saves_in_flight_chunks_then_propagates():
    chunk1_started = threading.Event()
    chunk0_raised = threading.Event()

    def before(user: str) -> None:
        if _chunk_of(user) == 1:
            chunk1_started.set()
            # Still in flight when chunk 0 fails, so no worker frees up early.
            assert chunk0_raised.wait(_TIMEOUT)
            return
        assert chunk1_started.wait(_TIMEOUT)
        chunk0_raised.set()
        raise RuntimeError("a bug, not a provider failure")

    provider = HookedProvider(before=before)
    store = InMemoryCheckpointStore()
    job = make_job(make_config(), total=2)

    with pytest.raises(RuntimeError, match="a bug"):
        _run(_orchestrator(provider, store), store, job, make_chunks(2), workers=2)

    statuses = _statuses(store, job.id)
    assert statuses[1] == ChunkStatus.DONE
    assert statuses[0] == ChunkStatus.PENDING
    assert sorted({_chunk_of(u) for u in provider.started}) == [0, 1]


# ---------------------------------------------------------------------------
# Resume, checkpoint thread, progress
# ---------------------------------------------------------------------------


def test_resume_after_an_out_of_order_stop_translates_only_unfinished_chunks():
    provider = FakeTranslationProvider()
    store = InMemoryCheckpointStore()
    job = make_job(make_config(), total=5)
    chunks = make_chunks(5)
    for i in (1, 3):
        chunks[i] = chunks[i].model_copy(
            update={"status": ChunkStatus.DONE, "translated_text": f"Done {i}."}
        )

    result = _run(_orchestrator(provider, store), store, job, chunks, workers=3)

    assert result.status == JobStatus.DONE
    assert sorted(_chunk_of(u) for _, u, _ in provider.call_log) == [0, 2, 4]
    done = {c.index: c.translated_text for c in store.load_chunks(job.id)}
    assert done[1] == "Done 1." and done[3] == "Done 3."


class _ThreadRecordingStore(InMemoryCheckpointStore):
    def __init__(self) -> None:
        super().__init__()
        self.writer_threads: set[int] = set()

    def save_chunk(self, job_id: str, chunk: Chunk) -> None:
        self.writer_threads.add(threading.get_ident())
        super().save_chunk(job_id, chunk)

    def save_job(self, job: Job) -> None:
        self.writer_threads.add(threading.get_ident())
        super().save_job(job)


def test_checkpoint_writes_stay_on_the_calling_thread():
    provider = HookedProvider()
    store = _ThreadRecordingStore()
    job = make_job(make_config(), total=8)

    _run(_orchestrator(provider, store), store, job, make_chunks(8), workers=4)

    assert store.writer_threads == {threading.get_ident()}


def test_parallel_progress_reports_each_chunk_once_on_the_calling_thread():
    events: list[Progress] = []
    threads: set[int] = set()

    def on_progress(p: Progress) -> None:
        threads.add(threading.get_ident())
        events.append(p)

    store = InMemoryCheckpointStore()
    job = make_job(make_config(), total=6)
    _run(
        _orchestrator(HookedProvider(), store), store, job, make_chunks(6),
        workers=3, on_progress=on_progress,
    )

    assert sorted(p.chunk_index for p in events) == list(range(6))
    assert all(p.position == p.chunk_index + 1 for p in events)
    assert threads == {threading.get_ident()}
