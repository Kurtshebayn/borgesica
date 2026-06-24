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
