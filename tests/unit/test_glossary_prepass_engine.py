"""Tests for the explicit pre-translation glossary pass on TranslatorEngine.

Covers ``estimate_glossary_extraction`` and ``extract_glossary``: the pass
runs only when asked (``create_job`` spends nothing), over every stored chunk
in batches, accrues each batch's real cost as it happens, stops before a call
that could break the budget, keeps human decisions already in the glossary,
persists every proposed rendering, and refuses to run (and bill) twice.

No network: a scripted provider returns one list of glossary additions per
call, with usage the test controls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from borgesica.api import TranslatorEngine
from borgesica.domain.errors import (
    BudgetExceeded,
    ExtractionStopped,
    JobStateError,
    MalformedOutput,
    ProviderError,
)
from borgesica.domain.glossary import LlmGlossaryExtractor
from borgesica.domain.models import (
    Chunk,
    ChunkStatus,
    Glossary,
    GlossaryEntry,
    GlossaryExtractionEstimate,
    GlossaryExtractionOutcome,
    Job,
    JobConfig,
    JobStatus,
    SourceType,
    TranslationResult,
    TranslationUnit,
    Usage,
)
from tests.fakes import InMemoryCheckpointStore

# Price of the scripted provider: $1/Mtok in, $5/Mtok out, cache hits $0.10.
IN_PRICE, OUT_PRICE, CACHE_PRICE = 1.0, 5.0, 0.1
CALL_USAGE = Usage(input_tokens=1_000_000, output_tokens=200_000, cached_input_tokens=0)
# 1M input at $1 + 0.2M output at $5.
CALL_COST = 2.0


def entry(term: str, translation: str, **kwargs) -> GlossaryEntry:
    return GlossaryEntry(term=term, translation=translation, **kwargs)


@dataclass
class ScriptedProvider:
    """One scripted list of glossary additions per call, in call order.

    ``fail_on`` maps a call index to the exception that call raises instead.
    Token counting is one token per whitespace-separated word.
    """

    additions_per_call: list[list[GlossaryEntry]] = field(default_factory=list)
    usage: Usage = field(default_factory=lambda: CALL_USAGE)
    fail_on: dict[int, Exception] = field(default_factory=dict)
    call_log: list[tuple[str, str, str]] = field(default_factory=list)

    def translate(
        self, system: str, user: str, model: str, segment_count: int | None = None
    ) -> TranslationResult:
        index = len(self.call_log)
        self.call_log.append((system, user, model))
        if index in self.fail_on:
            raise self.fail_on[index]
        unit = TranslationUnit(
            translation="",
            translations=[],
            summary_update="Terminology extraction complete.",
            glossary_additions=self.additions_per_call[index],
        )
        return TranslationResult(unit=unit, usage=self.usage)

    def count_tokens(self, text: str, model: str) -> int:  # noqa: ARG002
        return len(text.split())

    def price(self, model: str) -> tuple[float, float]:  # noqa: ARG002
        return (IN_PRICE, OUT_PRICE)

    def cache_price(self, model: str) -> float:  # noqa: ARG002
        return CACHE_PRICE


def words(n: int, tag: str = "w") -> str:
    return " ".join([tag] * n)


def setup_job(
    store: InMemoryCheckpointStore,
    texts: list[str],
    *,
    budget_usd: float | None = None,
    status: JobStatus = JobStatus.CREATED,
    glossary: Glossary | None = None,
    cost_usd: float = 0.0,
    glossary_extracted: bool = False,
) -> Job:
    now = datetime.now(UTC)
    job = Job(
        id="job-1",
        config=JobConfig(
            source_type=SourceType.EPUB, model="fake-model", budget_usd=budget_usd
        ),
        source_path="/tmp/book.epub",
        status=status,
        total_chunks=len(texts),
        cost_usd=cost_usd,
        created_at=now,
        updated_at=now,
        glossary_extracted=glossary_extracted,
    )
    store.save_job(job)
    # Saved in reverse to prove the pass orders by index, not by insertion.
    for index in reversed(range(len(texts))):
        store.save_chunk(
            job.id,
            Chunk(index=index, source_text=texts[index], status=ChunkStatus.PENDING),
        )
    store.save_glossary(job.id, glossary or Glossary())
    return job


def make_engine(
    provider: ScriptedProvider, store: InMemoryCheckpointStore, **kwargs
) -> TranslatorEngine:
    return TranslatorEngine(
        provider=provider, checkpoint=store, readers={}, writers={}, **kwargs
    )


# Three chunks of 3000 words: the first two fill one 6000-token batch, the
# third opens a second one.
THREE_CHUNKS = [words(3000, "a"), words(3000, "b"), words(3000, "c")]


# ---------------------------------------------------------------------------
# estimate_glossary_extraction
# ---------------------------------------------------------------------------


def test_estimate_is_the_domain_estimate_over_every_stored_chunk() -> None:
    store = InMemoryCheckpointStore()
    provider = ScriptedProvider()
    job = setup_job(store, THREE_CHUNKS)
    engine = make_engine(provider, store)

    estimate = engine.estimate_glossary_extraction(job.id)

    assert isinstance(estimate, GlossaryExtractionEstimate)
    assert estimate == engine._cost_est.estimate_glossary_extraction(
        sorted(store.load_chunks(job.id), key=lambda c: c.index), job.config
    )
    assert estimate.batches == 2
    assert provider.call_log == []


def test_estimate_is_none_once_the_glossary_was_extracted() -> None:
    store = InMemoryCheckpointStore()
    job = setup_job(store, THREE_CHUNKS, glossary_extracted=True)

    assert make_engine(ScriptedProvider(), store).estimate_glossary_extraction(job.id) is None


@pytest.mark.parametrize(
    "status", [JobStatus.RUNNING, JobStatus.PAUSED, JobStatus.DONE, JobStatus.CANCELLED]
)
def test_estimate_is_none_for_a_job_the_pass_would_refuse(status: JobStatus) -> None:
    # The line advertises a paid action; offering one that extract_glossary
    # would refuse invites a click that can only fail.
    store = InMemoryCheckpointStore()
    job = setup_job(store, THREE_CHUNKS, status=status)

    assert make_engine(ScriptedProvider(), store).estimate_glossary_extraction(job.id) is None


# ---------------------------------------------------------------------------
# extract_glossary — the happy path
# ---------------------------------------------------------------------------


def test_extract_calls_one_batch_at_a_time_in_index_order() -> None:
    store = InMemoryCheckpointStore()
    provider = ScriptedProvider([[], []])
    job = setup_job(store, THREE_CHUNKS)

    make_engine(provider, store).extract_glossary(job.id)

    users = [user for _system, user, _model in provider.call_log]
    assert len(users) == 2
    assert users[0].endswith(THREE_CHUNKS[0] + "\n\n" + THREE_CHUNKS[1])
    assert users[1].endswith(THREE_CHUNKS[2])


def test_extract_persists_the_glossary_proposals_flag_and_cost() -> None:
    store = InMemoryCheckpointStore()
    provider = ScriptedProvider(
        [
            [entry("Birthright", "Primogenitura"), entry("Vis", "Vis")],
            [entry("Birthright", "Derecho de nacimiento")],
        ]
    )
    job = setup_job(store, THREE_CHUNKS, cost_usd=0.5)

    outcome = make_engine(provider, store).extract_glossary(job.id)

    saved = {e.term: e for e in store.load_glossary(job.id).entries}
    assert saved["Birthright"].translation == "Primogenitura"
    assert saved["Vis"].translation == "Vis"
    # Not quorum draws: settlement counts must never see them.
    assert all(e.first_draw is None for e in saved.values())
    assert store.load_glossary_proposals(job.id) == {
        "birthright": ("Primogenitura", "Derecho de nacimiento"),
        "vis": ("Vis",),
    }
    final = store.load_job(job.id)
    assert final.glossary_extracted is True
    assert final.status == JobStatus.CREATED
    assert final.cost_usd == pytest.approx(0.5 + 2 * CALL_COST)
    assert isinstance(outcome, GlossaryExtractionOutcome)
    assert (outcome.batches, outcome.entries, outcome.conflicts) == (2, 2, 1)
    # What THIS pass spent, not the job's running total.
    assert outcome.cost_usd == pytest.approx(2 * CALL_COST)


def test_extract_prices_cached_input_at_the_cache_rate() -> None:
    store = InMemoryCheckpointStore()
    usage = Usage(input_tokens=1_000_000, output_tokens=0, cached_input_tokens=400_000)
    provider = ScriptedProvider([[]], usage=usage)
    job = setup_job(store, [words(10)])

    outcome = make_engine(provider, store).extract_glossary(job.id)

    assert outcome.cost_usd == pytest.approx(0.6 * IN_PRICE + 0.4 * CACHE_PRICE)


def test_conflicts_compare_renderings_as_the_plurality_groups_them() -> None:
    store = InMemoryCheckpointStore()
    provider = ScriptedProvider(
        [
            [entry("Will cage", "Jaula de Voluntad"), entry("Vis", "Vis")],
            [entry("Will cage", "jaula de  voluntad"), entry("Vis", "Visto")],
        ]
    )
    job = setup_job(store, THREE_CHUNKS)

    outcome = make_engine(provider, store).extract_glossary(job.id)

    # Case and spacing are the same rendering; only "Vis" really disagrees.
    assert outcome.conflicts == 1


def test_extract_reports_progress_after_each_batch_with_cost_persisted() -> None:
    store = InMemoryCheckpointStore()
    provider = ScriptedProvider([[], []])
    job = setup_job(store, THREE_CHUNKS)
    seen: list[tuple[int, int, float, float]] = []

    def on_progress(done: int, total: int, cost: float) -> None:
        seen.append((done, total, cost, store.load_job(job.id).cost_usd))

    make_engine(provider, store).extract_glossary(job.id, on_progress=on_progress)

    assert seen == [
        (1, 2, pytest.approx(CALL_COST), pytest.approx(CALL_COST)),
        (2, 2, pytest.approx(2 * CALL_COST), pytest.approx(2 * CALL_COST)),
    ]


def test_existing_entries_win_over_extraction_output() -> None:
    store = InMemoryCheckpointStore()
    provider = ScriptedProvider(
        [[entry("Vis", "Visto"), entry("Will", "Albedrío"), entry("Caeror", "Caeror")], []]
    )
    existing = Glossary(
        entries=[
            entry("Vis", "Vis", locked=True),
            entry("Will", "Voluntad"),  # hand-edited, unlocked
        ]
    )
    job = setup_job(store, THREE_CHUNKS, glossary=existing)

    make_engine(provider, store).extract_glossary(job.id)

    saved = {e.term: e for e in store.load_glossary(job.id).entries}
    assert saved["Vis"].translation == "Vis"
    assert saved["Vis"].locked is True
    assert saved["Will"].translation == "Voluntad"
    assert saved["Caeror"].translation == "Caeror"


def test_extract_resolves_reversed_pairs_against_the_source() -> None:
    store = InMemoryCheckpointStore()
    # The English side is what the source says: "Will" appears, "Voluntad"
    # does not, so the reversed entry is the one to drop.
    texts = ["The Will binds them. " + words(10)]
    provider = ScriptedProvider([[entry("Voluntad", "Will"), entry("Will", "Voluntad")]])
    job = setup_job(store, texts)

    make_engine(provider, store).extract_glossary(job.id)

    saved = [(e.term, e.translation) for e in store.load_glossary(job.id).entries]
    assert saved == [("Will", "Voluntad")]


def test_extract_uses_an_injected_batch_extractor() -> None:
    store = InMemoryCheckpointStore()
    engine_provider = ScriptedProvider()
    extractor_provider = ScriptedProvider([[entry("Vis", "Vis")]])
    job = setup_job(store, [words(10)])

    make_engine(
        engine_provider,
        store,
        batch_extractor=LlmGlossaryExtractor(extractor_provider),
    ).extract_glossary(job.id)

    assert len(extractor_provider.call_log) == 1
    assert engine_provider.call_log == []


def test_create_job_still_makes_no_provider_call(tmp_path) -> None:
    # The pass is an explicit paid action: an engine that can run it must
    # still create jobs without spending anything.
    from borgesica.adapters.readers.srt_reader import SrtReader

    srt = tmp_path / "test.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,500\nThe Hierarchy rules.\n", encoding="utf-8"
    )
    store = InMemoryCheckpointStore()
    provider = ScriptedProvider()
    engine = TranslatorEngine(
        provider=provider,
        checkpoint=store,
        readers={SourceType.SRT: SrtReader()},
        writers={},
    )

    job = engine.create_job(
        str(srt), JobConfig(source_type=SourceType.SRT, model="fake-model")
    )

    assert provider.call_log == []
    assert job.glossary_extracted is False


# ---------------------------------------------------------------------------
# extract_glossary — refusals and failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status", [JobStatus.RUNNING, JobStatus.PAUSED, JobStatus.DONE, JobStatus.CANCELLED]
)
def test_extract_refuses_a_job_that_is_not_created(status: JobStatus) -> None:
    store = InMemoryCheckpointStore()
    provider = ScriptedProvider([[]])
    job = setup_job(store, [words(10)], status=status)

    with pytest.raises(JobStateError):
        make_engine(provider, store).extract_glossary(job.id)

    assert provider.call_log == []


def test_extract_refuses_to_run_twice() -> None:
    store = InMemoryCheckpointStore()
    provider = ScriptedProvider([[entry("Vis", "Vis")], [entry("Vis", "Visto")]])
    job = setup_job(store, [words(10)])
    engine = make_engine(provider, store)
    engine.extract_glossary(job.id)

    with pytest.raises(JobStateError, match="already extracted"):
        engine.extract_glossary(job.id)

    assert len(provider.call_log) == 1
    assert store.load_job(job.id).cost_usd == pytest.approx(CALL_COST)


def test_budget_guard_stops_before_a_call_that_could_break_the_budget() -> None:
    store = InMemoryCheckpointStore()
    provider = ScriptedProvider([[entry("Vis", "Vis")], []])
    # Each batch's ceiling is a few cents, so the first call is allowed; it
    # really costs $2, which leaves less than the second batch's ceiling.
    job = setup_job(store, [words(3000), words(4000)], budget_usd=2.01)

    with pytest.raises(BudgetExceeded):
        make_engine(provider, store).extract_glossary(job.id)

    assert len(provider.call_log) == 1
    final = store.load_job(job.id)
    assert final.cost_usd == pytest.approx(CALL_COST)
    assert final.status == JobStatus.CREATED
    assert final.glossary_extracted is False
    assert store.load_glossary(job.id) == Glossary()


def test_budget_guard_counts_what_the_job_already_spent() -> None:
    store = InMemoryCheckpointStore()
    provider = ScriptedProvider([[]])
    job = setup_job(store, [words(3000)], budget_usd=1.0, cost_usd=1.0)

    with pytest.raises(BudgetExceeded):
        make_engine(provider, store).extract_glossary(job.id)

    assert provider.call_log == []


def test_budget_guard_projects_the_batch_ceiling() -> None:
    store = InMemoryCheckpointStore()
    provider = ScriptedProvider([[]])
    job = setup_job(store, [words(3000)])
    engine = make_engine(provider, store)
    ceiling = engine.estimate_glossary_extraction(job.id).usd_high
    store.save_job(
        store.load_job(job.id).model_copy(
            update={"config": job.config.model_copy(update={"budget_usd": ceiling * 0.99})}
        )
    )

    with pytest.raises(BudgetExceeded):
        engine.extract_glossary(job.id)

    assert provider.call_log == []


def test_a_failed_batch_keeps_the_spend_and_writes_no_glossary() -> None:
    store = InMemoryCheckpointStore()
    wasted = Usage(input_tokens=500_000, output_tokens=0)
    provider = ScriptedProvider(
        [[entry("Vis", "Vis")], []],
        fail_on={1: MalformedOutput(job_id="unknown", chunk_index=-1, usage=wasted)},
    )
    job = setup_job(store, THREE_CHUNKS)

    with pytest.raises(MalformedOutput):
        make_engine(provider, store).extract_glossary(job.id)

    final = store.load_job(job.id)
    # The successful batch AND the billed-but-failed attempts are real money.
    assert final.cost_usd == pytest.approx(CALL_COST + 0.5 * IN_PRICE)
    assert final.glossary_extracted is False
    assert store.load_glossary(job.id) == Glossary()
    assert store.load_glossary_proposals(job.id) == {}


def test_a_provider_error_charges_its_billed_usage_and_propagates() -> None:
    store = InMemoryCheckpointStore()
    wasted = Usage(input_tokens=300_000, output_tokens=100_000)
    provider = ScriptedProvider(
        [[entry("Vis", "Vis")], []],
        fail_on={1: ProviderError(status_code=500, usage=wasted)},
    )
    job = setup_job(store, THREE_CHUNKS)

    with pytest.raises(ProviderError):
        make_engine(provider, store).extract_glossary(job.id)

    final = store.load_job(job.id)
    assert final.cost_usd == pytest.approx(
        CALL_COST + 0.3 * IN_PRICE + 0.1 * OUT_PRICE
    )
    assert final.status == JobStatus.CREATED
    assert final.glossary_extracted is False
    assert store.load_glossary(job.id) == Glossary()
    assert store.load_glossary_proposals(job.id) == {}


# ---------------------------------------------------------------------------
# extract_glossary — a cooperative stop keeps the job CREATED
# ---------------------------------------------------------------------------


def test_a_stop_request_ends_the_pass_before_the_next_call() -> None:
    store = InMemoryCheckpointStore()
    provider = ScriptedProvider([[entry("Vis", "Vis")], []])
    job = setup_job(store, THREE_CHUNKS, cost_usd=0.5)
    stop = {"requested": False}

    def on_progress(done: int, total: int, cost: float) -> None:  # noqa: ARG001
        stop["requested"] = True

    with pytest.raises(ExtractionStopped) as excinfo:
        make_engine(provider, store).extract_glossary(
            job.id, on_progress=on_progress, should_stop=lambda: stop["requested"]
        )

    assert (excinfo.value.batches_done, excinfo.value.total_batches) == (1, 2)
    assert len(provider.call_log) == 1
    final = store.load_job(job.id)
    # The batch already paid for stays on the job; nothing else is written.
    assert final.cost_usd == pytest.approx(0.5 + CALL_COST)
    assert final.status == JobStatus.CREATED
    assert final.glossary_extracted is False
    assert store.load_glossary(job.id) == Glossary()
    assert store.load_glossary_proposals(job.id) == {}


def test_a_stop_requested_before_the_first_batch_makes_no_call() -> None:
    store = InMemoryCheckpointStore()
    provider = ScriptedProvider([[entry("Vis", "Vis")], []])
    job = setup_job(store, THREE_CHUNKS)

    with pytest.raises(ExtractionStopped):
        make_engine(provider, store).extract_glossary(job.id, should_stop=lambda: True)

    assert provider.call_log == []
    assert store.load_job(job.id).cost_usd == 0.0


def test_a_stopped_pass_can_be_started_again() -> None:
    store = InMemoryCheckpointStore()
    provider = ScriptedProvider([[entry("Vis", "Vis")], [], [entry("Vis", "Vis")], []])
    job = setup_job(store, THREE_CHUNKS)
    engine = make_engine(provider, store)
    with pytest.raises(ExtractionStopped):
        engine.extract_glossary(
            job.id, should_stop=lambda: len(provider.call_log) == 1
        )

    outcome = engine.extract_glossary(job.id)

    assert outcome.batches == 2
    final = store.load_job(job.id)
    assert final.glossary_extracted is True
    # The stopped pass's batch and both batches of the retry were paid for.
    assert final.cost_usd == pytest.approx(3 * CALL_COST)


def test_a_stop_after_the_last_call_keeps_the_paid_result() -> None:
    # Every batch is already paid for; writing the glossary costs nothing,
    # so a stop that arrives during the last call does not throw it away.
    store = InMemoryCheckpointStore()
    provider = ScriptedProvider([[entry("Vis", "Vis")], []])
    job = setup_job(store, THREE_CHUNKS)

    outcome = make_engine(provider, store).extract_glossary(
        job.id, should_stop=lambda: len(provider.call_log) == 2
    )

    assert outcome.batches == 2
    assert store.load_job(job.id).glossary_extracted is True


# ---------------------------------------------------------------------------
# extract_glossary — a concurrent change to the job is never reverted
# ---------------------------------------------------------------------------


def concurrent_run(store: InMemoryCheckpointStore, job_id: str) -> Job:
    """What a translation run started meanwhile would persist: RUNNING, one
    chunk done and its own spend on top of what it loaded."""
    current = store.load_job(job_id)
    started = current.model_copy(
        update={
            "status": JobStatus.RUNNING,
            "completed_chunks": 1,
            "cost_usd": current.cost_usd + 7.0,
        }
    )
    store.save_job(started)
    return started


def test_a_job_that_leaves_created_between_batches_stops_the_pass() -> None:
    store = InMemoryCheckpointStore()
    provider = ScriptedProvider([[entry("Vis", "Vis")], []])
    job = setup_job(store, THREE_CHUNKS)

    def on_progress(done: int, total: int, cost: float) -> None:  # noqa: ARG001
        if done == 1:
            concurrent_run(store, job.id)

    with pytest.raises(JobStateError):
        make_engine(provider, store).extract_glossary(job.id, on_progress=on_progress)

    # No call is made for a job the pass no longer owns.
    assert len(provider.call_log) == 1
    final = store.load_job(job.id)
    assert final.status == JobStatus.RUNNING
    assert final.completed_chunks == 1
    assert final.cost_usd == pytest.approx(CALL_COST + 7.0)
    assert final.glossary_extracted is False
    assert store.load_glossary(job.id) == Glossary()
    assert store.load_glossary_proposals(job.id) == {}


@dataclass
class RacingProvider(ScriptedProvider):
    """Scripted provider that lets a concurrent run start during call ``race_on``."""

    store: InMemoryCheckpointStore | None = None
    job_id: str = ""
    race_on: int = 0

    def translate(
        self, system: str, user: str, model: str, segment_count: int | None = None
    ) -> TranslationResult:
        if len(self.call_log) == self.race_on:
            concurrent_run(self.store, self.job_id)
        return super().translate(system, user, model, segment_count)


@pytest.mark.parametrize("race_on", [0, 1])
def test_spend_lands_on_the_current_job_without_reverting_a_concurrent_run(
    race_on: int,
) -> None:
    # Saving the job loaded at the start would put it back to CREATED and
    # erase the run's progress and cost. The call's spend is still real, so
    # it is added to the CURRENT job before the pass stops.
    store = InMemoryCheckpointStore()
    job = setup_job(store, THREE_CHUNKS, cost_usd=0.5)
    provider = RacingProvider(
        [[entry("Vis", "Vis")], []], store=store, job_id=job.id, race_on=race_on
    )

    with pytest.raises(JobStateError):
        make_engine(provider, store).extract_glossary(job.id)

    assert len(provider.call_log) == race_on + 1
    final = store.load_job(job.id)
    assert final.status == JobStatus.RUNNING
    assert final.completed_chunks == 1
    assert final.cost_usd == pytest.approx(0.5 + 7.0 + (race_on + 1) * CALL_COST)
    assert final.glossary_extracted is False
    assert store.load_glossary(job.id) == Glossary()
    assert store.load_glossary_proposals(job.id) == {}


def test_the_final_save_keeps_fields_changed_during_the_pass() -> None:
    # Only cost_usd and glossary_extracted belong to the pass; anything else
    # written meanwhile (here, a budget edit) survives the final save.
    store = InMemoryCheckpointStore()
    provider = ScriptedProvider([[entry("Vis", "Vis")], []])
    job = setup_job(store, THREE_CHUNKS)
    edited = job.config.model_copy(update={"budget_usd": 99.0})

    def on_progress(done: int, total: int, cost: float) -> None:  # noqa: ARG001
        if done == total:
            store.save_job(store.load_job(job.id).model_copy(update={"config": edited}))

    make_engine(provider, store).extract_glossary(job.id, on_progress=on_progress)

    final = store.load_job(job.id)
    assert final.config.budget_usd == 99.0
    assert final.glossary_extracted is True
    assert final.cost_usd == pytest.approx(2 * CALL_COST)


# ---------------------------------------------------------------------------
# review_glossary
# ---------------------------------------------------------------------------


def test_review_attaches_the_stored_proposals_and_the_whole_source_evidence() -> None:
    store = InMemoryCheckpointStore()
    gap = " " + "." * 80 + " "
    # Evidence spread over chunks that are all still PENDING: the review runs
    # before anything is translated, so it must read the source, not output.
    texts = [gap.join(["Vis said he was ready."] * 3), gap + "Vis nodded, he agreed."]
    glossary = Glossary(
        entries=[entry("Vis", "Vis"), entry("Birthright", "Primogenitura")]
    )
    job = setup_job(store, texts, glossary=glossary)
    store.save_glossary_proposals(
        job.id, {"birthright": ("Primogenitura", "Derecho de nacimiento")}
    )

    reviewed = {
        item.entry.term: item
        for item in make_engine(ScriptedProvider(), store).review_glossary(job.id).entries
    }

    assert (reviewed["Vis"].masculine_mentions, reviewed["Vis"].feminine_mentions) == (4, 0)
    assert "conflicting_renderings" in reviewed["Birthright"].flags
    assert [p.rendering for p in reviewed["Birthright"].proposals] == [
        "Primogenitura",
        "Derecho de nacimiento",
    ]


def test_review_persists_nothing() -> None:
    store = InMemoryCheckpointStore()
    gap = " " + "." * 80 + " "
    texts = [gap.join(["Vis said he was ready."] * 25)]
    job = setup_job(store, texts, glossary=Glossary(entries=[entry("Vis", "Vis")]))

    reviewed = make_engine(ScriptedProvider(), store).review_glossary(job.id).entries

    assert reviewed[0].gender == "masculine"
    assert reviewed[0].gender_source == "seeded"
    assert store.load_glossary(job.id).entries[0].gender is None


def test_review_of_an_unknown_job_raises() -> None:
    from borgesica.domain.errors import JobNotFoundError

    engine = make_engine(ScriptedProvider(), InMemoryCheckpointStore())

    with pytest.raises(JobNotFoundError):
        engine.review_glossary("missing")


def test_review_lists_exactly_the_entries_get_glossary_shows() -> None:
    # The stored glossary holds what the read path repairs: a reversed pair
    # and a case duplicate. The review must show the same entries, or the
    # screen would offer to triage entries the glossary no longer has.
    store = InMemoryCheckpointStore()
    stored = Glossary(
        entries=[
            entry("Voluntad", "Will"),
            entry("Will", "Voluntad"),
            entry("alupi", "alupi"),
            entry("Alupi", "Alupi"),
        ]
    )
    job = setup_job(store, ["The Will answered him. The alupi waited."], glossary=stored)
    engine = make_engine(ScriptedProvider(), store)

    reviewed = engine.review_glossary(job.id)

    assert [item.entry for item in reviewed.entries] == engine.get_glossary(job.id).entries
    assert len(reviewed.entries) < len(stored.entries)
