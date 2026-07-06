"""Smoke tests for the test doubles (FakeTranslationProvider + InMemoryCheckpointStore).

M0-3 deliverable: 3 tests pass.
"""
from tests.fakes import FakeTranslationProvider, InMemoryCheckpointStore
from borgesica.domain.ports import TranslationProvider, CheckpointStore


def test_fake_translation_provider_satisfies_protocol() -> None:
    """FakeTranslationProvider must pass isinstance check for TranslationProvider Protocol."""
    fake = FakeTranslationProvider()
    assert isinstance(fake, TranslationProvider)


def test_in_memory_checkpoint_store_satisfies_protocol() -> None:
    """InMemoryCheckpointStore must pass isinstance check for CheckpointStore Protocol."""
    store = InMemoryCheckpointStore()
    assert isinstance(store, CheckpointStore)


def test_save_chunk_is_idempotent() -> None:
    """save_chunk called twice with the same (job_id, chunk.index) produces exactly one row."""
    from borgesica.domain.models import Chunk, ChunkStatus

    store = InMemoryCheckpointStore()
    chunk = Chunk(index=0, source_text="Hello", status=ChunkStatus.DONE, translated_text="Hola")

    store.save_chunk("job-1", chunk)
    store.save_chunk("job-1", chunk)

    chunks = store.load_chunks("job-1")
    assert len(chunks) == 1
    assert chunks[0].translated_text == "Hola"


def test_fake_provider_segment_count_returns_translations_array() -> None:
    """With segment_count=N (SRT contract), the default echo fake returns a
    per-segment translations array aligned with the source segments."""
    fake = FakeTranslationProvider()

    result = fake.translate("system", "cue one\n\ncue two\n\ncue three", "fake", segment_count=3)

    assert result.unit.translations == [
        "[translated] cue one",
        "[translated] cue two",
        "[translated] cue three",
    ]
    assert fake.segment_count_log == [3]


def test_fake_provider_without_segment_count_keeps_string_echo() -> None:
    fake = FakeTranslationProvider()

    result = fake.translate("system", "plain prose", "fake")

    assert result.unit.translations is None
    assert result.unit.translation == "[translated] plain prose"
    assert fake.segment_count_log == [None]
