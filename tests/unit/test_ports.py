"""M1-2 — Ports (Protocols, runtime-checkable).

Written BEFORE production code in ports.py (TDD RED phase).
"""


def test_document_reader_is_runtime_checkable() -> None:
    from borgesica.domain.ports import DocumentReader
    import typing

    assert getattr(DocumentReader, "__protocol_attrs__", None) is not None or (
        hasattr(typing, "get_protocol_members")
    )
    # The Protocol must be @runtime_checkable — isinstance must not raise.
    try:
        isinstance(object(), DocumentReader)
    except TypeError as e:
        raise AssertionError(f"DocumentReader is not @runtime_checkable: {e}") from e


def test_document_writer_is_runtime_checkable() -> None:
    from borgesica.domain.ports import DocumentWriter

    try:
        isinstance(object(), DocumentWriter)
    except TypeError as e:
        raise AssertionError(f"DocumentWriter is not @runtime_checkable: {e}") from e


def test_translation_provider_is_runtime_checkable() -> None:
    from borgesica.domain.ports import TranslationProvider

    try:
        isinstance(object(), TranslationProvider)
    except TypeError as e:
        raise AssertionError(f"TranslationProvider is not @runtime_checkable: {e}") from e


def test_checkpoint_store_is_runtime_checkable() -> None:
    from borgesica.domain.ports import CheckpointStore

    try:
        isinstance(object(), CheckpointStore)
    except TypeError as e:
        raise AssertionError(f"CheckpointStore is not @runtime_checkable: {e}") from e


def test_fake_translation_provider_satisfies_protocol() -> None:
    """FakeTranslationProvider (from tests/fakes.py) passes the Protocol check."""
    from tests.fakes import FakeTranslationProvider
    from borgesica.domain.ports import TranslationProvider

    fake = FakeTranslationProvider()
    assert isinstance(fake, TranslationProvider)


def test_in_memory_checkpoint_store_satisfies_protocol() -> None:
    """InMemoryCheckpointStore passes the CheckpointStore Protocol check."""
    from tests.fakes import InMemoryCheckpointStore
    from borgesica.domain.ports import CheckpointStore

    store = InMemoryCheckpointStore()
    assert isinstance(store, CheckpointStore)


def test_corpus_store_is_runtime_checkable() -> None:
    from borgesica.domain.ports import CorpusStore

    try:
        isinstance(object(), CorpusStore)
    except TypeError as e:
        raise AssertionError(f"CorpusStore is not @runtime_checkable: {e}") from e


def test_fake_corpus_store_satisfies_protocol() -> None:
    """FakeCorpusStore (from tests/fakes.py) passes the Protocol check."""
    from tests.fakes import FakeCorpusStore
    from borgesica.domain.ports import CorpusStore

    fake = FakeCorpusStore()
    assert isinstance(fake, CorpusStore)


def test_missing_method_does_not_satisfy_corpus_store() -> None:
    """A class with no save_sample method must NOT pass the Protocol check."""
    from borgesica.domain.ports import CorpusStore

    class Incomplete:
        pass

    assert not isinstance(Incomplete(), CorpusStore)


def test_missing_method_does_not_satisfy_translation_provider() -> None:
    """A class that is missing one required method must NOT pass the Protocol check."""
    from borgesica.domain.ports import TranslationProvider

    class Incomplete:
        def translate(self, system: str, user: str, model: str) -> object:
            return None
        # Missing: count_tokens and price

    assert not isinstance(Incomplete(), TranslationProvider)
