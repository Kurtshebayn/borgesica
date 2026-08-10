"""Tests for .env loading at CLI startup.

The CLI reads provider keys from os.environ. These tests pin the contract for
how a project-local .env file gets into that environment:

  - it is discovered relative to the CURRENT WORKING DIRECTORY, not relative to
    the borgesica package (which lives in site-packages after a pip install);
  - a variable already exported in the real environment always wins;
  - a missing .env is a no-op, and must not silently fall back to searching
    upward from the package directory.

No real API key required.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest


def test_load_dotenv_reads_env_file_from_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .env in the current working directory populates os.environ."""
    from borgesica.__main__ import _load_dotenv

    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=sk-from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with patch.dict(os.environ, {}, clear=True):
        _load_dotenv()
        assert os.environ.get("DEEPSEEK_API_KEY") == "sk-from-dotenv"


def test_exported_variable_wins_over_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An already-exported variable is NOT overwritten by the .env value.

    This is the whole reason load_dotenv() must not be called with override=True:
    an explicit `export` (or a key injected by the desktop app) has to beat a
    stale value left in a checked-out .env.
    """
    from borgesica.__main__ import _load_dotenv

    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=sk-from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-exported"}, clear=True):
        _load_dotenv()
        assert os.environ.get("DEEPSEEK_API_KEY") == "sk-exported"


def test_missing_dotenv_leaves_environment_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No .env in cwd is a no-op — and must not fall back to a package-relative search.

    Regression guard against a bare `load_dotenv()`, which walks upward from
    borgesica/__main__.py and so reaches any .env sitting above the package —
    the developer's own repository .env in a source checkout. That would make the
    CLI read secrets from a directory the user never chose, and would make this
    suite depend on an untracked file. Verified: reverting to the bare call fails
    this test with real keys leaking in from the parent checkout.
    """
    from borgesica.__main__ import _load_dotenv

    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / ".env").exists()

    with patch.dict(os.environ, {"SENTINEL": "1"}, clear=True):
        before = dict(os.environ)
        _load_dotenv()
        assert dict(os.environ) == before


def test_dotenv_is_loaded_before_provider_is_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() loads the .env before auto-detecting the provider.

    Ordering is the point: _default_provider() picks a provider by looking at which
    key is present, so a key that only exists in .env must already be in os.environ
    by the time it runs. Loading afterwards would resolve the wrong provider.
    """
    import borgesica.__main__ as cli

    calls: list[str] = []

    def fake_load_dotenv() -> None:
        calls.append("load_dotenv")

    def fake_default_provider() -> str:
        calls.append("default_provider")
        return "anthropic"

    monkeypatch.setattr(cli, "_load_dotenv", fake_load_dotenv)
    monkeypatch.setattr(cli, "_default_provider", fake_default_provider)
    monkeypatch.setattr(cli, "_build_engine", lambda **kwargs: object())
    monkeypatch.setattr(cli, "_cmd_status", lambda args, engine: 0)

    assert cli.main(["status", "nonexistent-job"]) == 0
    assert calls == ["load_dotenv", "default_provider"]
