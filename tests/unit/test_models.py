"""M1-1 — Domain models, enums, and errors (TDD: tests written first).

All tests in this file must be GREEN before any production code is verified.
"""
import pytest
from datetime import datetime, timezone


# --- Enums ---

def test_job_status_has_all_7_values() -> None:
    from borgesica.domain.models import JobStatus

    values = {s.value for s in JobStatus}
    assert values == {"CREATED", "ESTIMATING", "RUNNING", "PAUSED", "DONE", "FAILED", "CANCELLED"}


def test_chunk_status_has_4_values() -> None:
    from borgesica.domain.models import ChunkStatus

    values = {s.value for s in ChunkStatus}
    assert values == {"PENDING", "TRANSLATING", "DONE", "FAILED"}


def test_source_type_has_3_values() -> None:
    from borgesica.domain.models import SourceType

    values = {s.value for s in SourceType}
    assert values == {"SRT", "EPUB", "PDF"}


# --- GlossaryEntry ---

def test_glossary_entry_locked_defaults_to_false() -> None:
    from borgesica.domain.models import GlossaryEntry

    entry = GlossaryEntry(term="tranca", translation="bar bolt")
    assert entry.locked is False


def test_glossary_entry_accepts_locked_true() -> None:
    from borgesica.domain.models import GlossaryEntry

    entry = GlossaryEntry(term="viga", translation="beam", locked=True)
    assert entry.locked is True


# --- Glossary ---

def test_glossary_defaults_to_empty_entries() -> None:
    from borgesica.domain.models import Glossary

    g = Glossary()
    assert g.entries == []


# --- RollingSummary ---

def test_rolling_summary_defaults() -> None:
    from borgesica.domain.models import RollingSummary

    s = RollingSummary()
    assert s.text == ""
    assert s.chunk_index == -1


# --- Chunk ---

def test_chunk_status_defaults_to_pending() -> None:
    from borgesica.domain.models import Chunk, ChunkStatus

    c = Chunk(index=0, source_text="Hello world")
    assert c.status == ChunkStatus.PENDING


# --- TranslationUnit ---

def test_translation_unit_requires_translation_and_summary() -> None:
    from borgesica.domain.models import TranslationUnit
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TranslationUnit()  # type: ignore[call-arg]


def test_translation_unit_glossary_additions_defaults_to_empty() -> None:
    from borgesica.domain.models import TranslationUnit

    unit = TranslationUnit(translation="Hola mundo", summary_update="Simple greeting.")
    assert unit.glossary_additions == []


# --- CostEstimate ---

def test_cost_estimate_within_budget_defaults_to_true() -> None:
    from borgesica.domain.models import CostEstimate

    est = CostEstimate(input_tokens=100, output_tokens=50, usd=0.001, model="claude-haiku-4-5")
    assert est.within_budget is True


def test_cost_estimate_has_all_5_fields() -> None:
    from borgesica.domain.models import CostEstimate

    est = CostEstimate(
        input_tokens=100,
        output_tokens=50,
        usd=0.001,
        model="claude-haiku-4-5",
        cached=True,
        within_budget=False,
    )
    assert est.input_tokens == 100
    assert est.output_tokens == 50
    assert est.usd == 0.001
    assert est.model == "claude-haiku-4-5"
    assert est.cached is True
    assert est.within_budget is False


# --- JobConfig ---

def test_job_config_defaults() -> None:
    from borgesica.domain.models import JobConfig, SourceType

    config = JobConfig(source_type=SourceType.SRT, model="claude-haiku-4-5")
    assert config.target_lang == "es-neutral"
    assert config.chunk_size == 25
    assert config.line_length == 42
    assert config.glossary_strategy == "llm"
    assert config.quality_mode == "fast"


def test_job_config_prose_chunk_tokens_default() -> None:
    """M2-2: prose_chunk_tokens is a distinct field with default 800 (not reusing chunk_size)."""
    from borgesica.domain.models import JobConfig, SourceType

    config = JobConfig(source_type=SourceType.SRT, model="claude-haiku-4-5")
    # New field must exist and default to 800.
    assert config.prose_chunk_tokens == 800
    # Must be independent of chunk_size (the SRT cue-batch control).
    assert config.chunk_size == 25
    assert config.prose_chunk_tokens != config.chunk_size


# --- Job ---

def test_job_accepts_all_required_fields() -> None:
    from borgesica.domain.models import Job, JobConfig, SourceType, JobStatus

    now = datetime.now(tz=timezone.utc)
    config = JobConfig(source_type=SourceType.SRT, model="claude-haiku-4-5")
    job = Job(
        id="job-123",
        config=config,
        source_path="/tmp/test.srt",
        created_at=now,
        updated_at=now,
    )
    assert job.id == "job-123"
    assert job.status == JobStatus.CREATED


# --- Progress ---

def test_progress_has_all_5_fields() -> None:
    from borgesica.domain.models import Progress, JobStatus

    p = Progress(
        job_id="job-1",
        chunk_index=2,
        total_chunks=10,
        cost_usd=0.05,
        status=JobStatus.RUNNING,
    )
    assert p.job_id == "job-1"
    assert p.chunk_index == 2
    assert p.total_chunks == 10
    assert p.cost_usd == 0.05
    assert p.status == JobStatus.RUNNING


# --- Errors ---

def test_budget_exceeded_is_borgesica_error() -> None:
    from borgesica.domain.errors import BudgetExceeded, BorgésicaError

    err = BudgetExceeded(job_id="job-1", cost_so_far=5.0)
    assert isinstance(err, BorgésicaError)
    assert err.job_id == "job-1"
    assert err.cost_so_far == 5.0


def test_malformed_output_carries_expected_attrs() -> None:
    from borgesica.domain.errors import MalformedOutput, BorgésicaError

    err = MalformedOutput(job_id="job-1", chunk_index=3)
    assert isinstance(err, BorgésicaError)
    assert err.chunk_index == 3


def test_job_not_found_error() -> None:
    from borgesica.domain.errors import JobNotFoundError, BorgésicaError

    err = JobNotFoundError(job_id="x")
    assert isinstance(err, BorgésicaError)
    assert err.job_id == "x"


def test_job_state_error() -> None:
    from borgesica.domain.errors import JobStateError, BorgésicaError

    err = JobStateError(job_id="y", current_status="RUNNING")
    assert isinstance(err, BorgésicaError)
    assert err.current_status == "RUNNING"


def test_unsupported_format_error() -> None:
    from borgesica.domain.errors import UnsupportedFormatError, BorgésicaError

    err = UnsupportedFormatError(path="/tmp/x.doc", reason="unknown format")
    assert isinstance(err, BorgésicaError)
    assert err.path == "/tmp/x.doc"
    assert err.reason == "unknown format"


def test_provider_error() -> None:
    from borgesica.domain.errors import ProviderError, BorgésicaError

    err = ProviderError(status_code=429)
    assert isinstance(err, BorgésicaError)
    assert err.status_code == 429


def test_provider_error_status_code_can_be_none() -> None:
    from borgesica.domain.errors import ProviderError

    err = ProviderError(status_code=None)
    assert err.status_code is None
