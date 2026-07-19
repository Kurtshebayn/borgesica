"""Tests for the PyInstaller hidden-import source of truth (packaging spike, T1).

PyInstaller's static import scanner cannot see the provider adapter modules
imported dynamically inside `borgesica.__main__._build_provider` (they are
imported by runtime string dispatch, not at module load time). These tests
pin `borgesica.packaging_hidden_imports` to the actual production source so
the frozen build's hidden-import list can never silently drift from the
real dynamic imports as providers are added or renamed.
"""
from __future__ import annotations

import importlib
import inspect
import re

from borgesica import __main__ as cli_main
from borgesica.packaging_hidden_imports import (
    EXTRA_HIDDEN_IMPORTS,
    PROVIDER_HIDDEN_IMPORTS,
    all_hidden_imports,
)


def _imports_in_build_provider() -> set[str]:
    """Extract the dotted provider-adapter module paths from the source of
    `_build_provider` — this is the ground truth PyInstaller cannot see."""
    source = inspect.getsource(cli_main._build_provider)
    return set(re.findall(r"from (borgesica\.adapters\.providers\.\w+) import", source))


def test_provider_hidden_imports_match_build_provider_source() -> None:
    """The pinned list must equal exactly what _build_provider dynamically imports."""
    assert set(PROVIDER_HIDDEN_IMPORTS) == _imports_in_build_provider()


def test_provider_hidden_imports_are_real_importable_modules() -> None:
    """Each pinned dotted path must resolve to a real importable module (catches typos)."""
    for module_path in PROVIDER_HIDDEN_IMPORTS:
        importlib.import_module(module_path)


def test_all_hidden_imports_appends_extras_after_providers() -> None:
    """The combined list PyInstaller consumes must keep providers first, extras after,
    and must not silently drop the lxml/ebooklib native-dependency entries."""
    combined = all_hidden_imports()
    assert combined == PROVIDER_HIDDEN_IMPORTS + EXTRA_HIDDEN_IMPORTS
    assert "ebooklib" in combined
