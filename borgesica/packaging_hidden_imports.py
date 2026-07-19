"""Hidden-import source of truth for freezing the borgesica CLI with PyInstaller.

`borgesica.__main__._build_provider` imports each provider adapter module
dynamically inside an `if`/`elif` branch selected by a runtime string
(`--provider anthropic|deepseek|ollama`), never at module load time.
PyInstaller's static import scanner only follows module-level imports, so
without an explicit hidden-import list the frozen binary silently omits
whichever provider(s) weren't exercised at analysis time.

`packaging/borgesica.spec` imports this module directly so the spec and the
real dynamic-import sites can never drift apart — `test_packaging_hidden_imports.py`
pins `PROVIDER_HIDDEN_IMPORTS` to the actual source of `_build_provider`.

This module intentionally lives inside the `borgesica` package (not inside
`packaging/`) so it stays importable both by pytest and by the frozen spec
without ever making a top-level `packaging` package that would shadow the
third-party `packaging` distribution (used by pip/setuptools/pytest for
version parsing).
"""
from __future__ import annotations

# Provider adapters imported dynamically inside _build_provider() by name,
# not at module load time — PyInstaller's import scanner cannot discover them.
PROVIDER_HIDDEN_IMPORTS: list[str] = [
    "borgesica.adapters.providers.anthropic_provider",
    "borgesica.adapters.providers.openai_compatible_provider",
    "borgesica.adapters.providers.ollama_provider",
]

# Native/compiled or plugin-style dependencies whose import-time side effects
# PyInstaller's static analyzer misses or under-collects when frozen.
EXTRA_HIDDEN_IMPORTS: list[str] = [
    "lxml.etree",
    "lxml._elementpath",
    "ebooklib",
]


def all_hidden_imports() -> list[str]:
    """Return the full hidden-import list consumed by packaging/borgesica.spec."""
    return PROVIDER_HIDDEN_IMPORTS + EXTRA_HIDDEN_IMPORTS
