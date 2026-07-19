# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the borgesica CLI engine (packaging spike, T1).

Freezes `borgesica.__main__:main` into a standalone binary — the sidecar
process the desktop app (T7a) will spawn. This spike proves the freeze
works BEFORE any UI work starts.

Hidden imports: the three provider adapter modules are imported dynamically
inside `_build_provider()` (selected by a runtime `--provider` string), so
PyInstaller's static scanner cannot see them. `borgesica.packaging_hidden_imports`
is the single source of truth for that list and is unit-tested (see
tests/unit/test_packaging_hidden_imports.py) to stay pinned to the real
dynamic-import sites.

Native deps: lxml (via ebooklib's EPUB support) ships compiled extensions
that PyInstaller's analyzer can under-collect; ebooklib itself is imported
lazily inside `_build_engine()`.

Build:
    pyinstaller packaging/borgesica.spec --distpath dist --workpath build

Smoke-check the frozen binary:
    python packaging/smoke_check.py dist/borgesica/borgesica.exe
"""
from __future__ import annotations

import sys
from pathlib import Path

# SPECPATH is injected into the exec globals by PyInstaller itself.
REPO_ROOT = Path(SPECPATH).parent  # noqa: F821
sys.path.insert(0, str(REPO_ROOT))

from borgesica.packaging_hidden_imports import all_hidden_imports  # noqa: E402

block_cipher = None

a = Analysis(  # noqa: F821
    [str(REPO_ROOT / "borgesica" / "__main__.py")],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=all_hidden_imports(),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="borgesica",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="borgesica",
)
