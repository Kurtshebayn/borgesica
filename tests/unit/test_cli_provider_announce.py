"""Tests for reporting which provider the CLI auto-detected.

_default_provider() scans the API keys in a fixed order and takes the first one
present, and it cannot tell a key the user exported from one loaded out of a .env.
With several keys available anthropic always wins — the most expensive entry in
the model table — so a silent auto-detection is a cost trap. These tests pin when
the CLI speaks up and, just as importantly, when it stays quiet.

No real API key required.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest


def _stub_dispatch(cli: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize engine construction and the status handler.

    These tests are about the message main() prints on the way to a command, not
    about what the command does.
    """
    monkeypatch.setattr(cli, "_build_engine", lambda **kwargs: object())
    monkeypatch.setattr(cli, "_cmd_status", lambda args, engine: 0)


def test_auto_detected_provider_is_announced_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no --provider and no BORGESICA_PROVIDER, the inferred provider is named."""
    import borgesica.__main__ as cli

    monkeypatch.chdir(tmp_path)
    _stub_dispatch(cli, monkeypatch)

    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-x"}, clear=True):
        assert cli.main(["status", "job-1"]) == 0

    captured = capsys.readouterr()
    assert "deepseek" in captured.err


def test_announcement_never_touches_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The notice goes to stderr only.

    `estimate` and `status` print JSON on stdout, and the desktop app parses that
    stream over the sidecar. A human-readable line mixed into it would break every
    consumer that json.loads() the output.
    """
    import borgesica.__main__ as cli

    monkeypatch.chdir(tmp_path)
    _stub_dispatch(cli, monkeypatch)

    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-x"}, clear=True):
        cli.main(["status", "job-1"])

    assert capsys.readouterr().out == ""


def test_explicit_provider_flag_is_not_announced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--provider was a deliberate choice; repeating it back is noise."""
    import borgesica.__main__ as cli

    monkeypatch.chdir(tmp_path)
    _stub_dispatch(cli, monkeypatch)

    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-x"}, clear=True):
        cli.main(["status", "job-1", "--provider", "deepseek"])

    assert capsys.readouterr().err == ""


def test_borgesica_provider_env_var_is_not_announced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """BORGESICA_PROVIDER is also a deliberate choice, so it stays quiet too.

    The notice exists for the one case nobody decided: a provider inferred purely
    from which keys happen to be reachable.
    """
    import borgesica.__main__ as cli

    monkeypatch.chdir(tmp_path)
    _stub_dispatch(cli, monkeypatch)

    env = {"BORGESICA_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "sk-x"}
    with patch.dict(os.environ, env, clear=True):
        cli.main(["status", "job-1"])

    assert capsys.readouterr().err == ""


def test_announcement_names_the_override_mechanisms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The notice must tell the user how to take control, not just what happened.

    Naming the provider alone leaves the reader to go hunting; the whole point is
    that the cheap fix is one flag or one env var away.
    """
    import borgesica.__main__ as cli

    monkeypatch.chdir(tmp_path)
    _stub_dispatch(cli, monkeypatch)

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-a"}, clear=True):
        cli.main(["status", "job-1"])

    err = capsys.readouterr().err
    assert "--provider" in err
    assert "BORGESICA_PROVIDER" in err
