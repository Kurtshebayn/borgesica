"""Tests for T5 — `serve` subcommand registration in borgesica.__main__.

Covers: localhost-only binding (non-loopback host can never be configured),
key intake via the existing --key-stdin mechanism (never argv), and that the
subcommand is wired into main()'s dispatch using the existing pattern.

No live server is ever started: _run_server() (the seam _cmd_serve delegates
the uvicorn boot + ready-line announce to) is mocked in every test that
reaches _cmd_serve.
"""
from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Parser wiring
# ---------------------------------------------------------------------------


def test_serve_subcommand_registered() -> None:
    from borgesica.__main__ import _build_parser

    args = _build_parser().parse_args(["serve", "--port", "0"])
    assert args.command == "serve"


def test_serve_accepts_key_stdin_flag() -> None:
    from borgesica.__main__ import _build_parser

    args = _build_parser().parse_args(["serve", "--key-stdin"])
    assert args.key_stdin is True


def test_serve_default_port_is_zero_ephemeral() -> None:
    """Design decision #5: --port default 0 (ephemeral) to avoid collisions."""
    from borgesica.__main__ import _build_parser

    args = _build_parser().parse_args(["serve"])
    assert args.port == 0


# ---------------------------------------------------------------------------
# Localhost-only binding — non-loopback can never be configured (RED test
# from the design's threat matrix)
# ---------------------------------------------------------------------------


def test_serve_parser_has_no_host_flag() -> None:
    """No --host argument exists at all: a non-loopback bind can never be
    configured via the CLI, not merely validated at runtime."""
    from borgesica.__main__ import _build_parser

    with pytest.raises(SystemExit):
        _build_parser().parse_args(["serve", "--host", "0.0.0.0"])


def test_cmd_serve_binds_loopback_host_to_uvicorn() -> None:
    from borgesica.__main__ import main

    with patch("borgesica.__main__._build_engine") as mock_build, patch(
        "borgesica.__main__._run_server"
    ) as mock_run:
        mock_build.return_value = MagicMock()
        with patch("sys.stdin", io.StringIO('{"api_key": "sk-x"}\n')):
            main(["serve", "--port", "0", "--provider", "anthropic", "--key-stdin"])

    # _cmd_serve builds the uvicorn.Config and hands it to _run_server; the
    # loopback host is on that config (a non-loopback bind is unconfigurable).
    config = mock_run.call_args.args[0]
    assert config.host == "127.0.0.1"


def test_serve_host_constant_is_loopback() -> None:
    from borgesica.serve.app import SERVE_HOST

    assert SERVE_HOST == "127.0.0.1"


# ---------------------------------------------------------------------------
# Session token must never reach the uvicorn access log (RISK-001/002/003,
# second correction round). uvicorn's default access logger writes the full
# request line, INCLUDING the query string — so an unmodified `uvicorn.run`
# call would log the secret on every `GET /jobs/{id}/events?token=<SECRET>`
# request (the SSE query-param fallback, since EventSource cannot set
# custom headers).
# ---------------------------------------------------------------------------


def test_cmd_serve_disables_uvicorn_access_log() -> None:
    """_cmd_serve must build the uvicorn.Config with access_log=False — the
    minimal, complete fix: no access log line is ever emitted, so the token
    can never appear in one, regardless of route or query string."""
    from borgesica.__main__ import main

    with patch("borgesica.__main__._build_engine") as mock_build, patch(
        "borgesica.__main__._run_server"
    ) as mock_run:
        mock_build.return_value = MagicMock()
        with patch("sys.stdin", io.StringIO('{"api_key": "sk-x"}\n')):
            main(["serve", "--port", "0", "--provider", "anthropic", "--key-stdin"])

    config = mock_run.call_args.args[0]
    assert config.access_log is False


# ---------------------------------------------------------------------------
# Key intake at boot — never via argv
# ---------------------------------------------------------------------------


def test_serve_key_never_appears_in_argv() -> None:
    """The API key travels only over stdin; the argv list passed to main()
    for the serve subcommand never contains the key value itself (design
    threat matrix: "key absent from process argv")."""
    from borgesica.__main__ import main

    secret = "sk-serve-secret-value"
    argv = ["serve", "--port", "0", "--provider", "anthropic", "--key-stdin"]
    assert not any(secret in tok for tok in argv)

    with patch("borgesica.__main__._build_engine") as mock_build, patch(
        "borgesica.__main__._run_server"
    ):
        mock_build.return_value = MagicMock()
        with patch("sys.stdin", io.StringIO(f'{{"api_key": "{secret}"}}\n')):
            main(argv)

    assert mock_build.call_args.kwargs.get("key_stdin") is True
    assert not any(secret in tok for tok in argv)


# ---------------------------------------------------------------------------
# Session auth token intake — RISK-001/002/003: the same trusted stdin init
# line that carries the API key also carries the serve API's per-session
# auth token (see borgesica.__main__._read_key_from_stdin /
# _stdin_session_token). _cmd_serve reads that module-level value and passes
# it to create_app — tested directly here (isolated from the unrelated
# complexity of mocking a full _build_engine/_build_provider call chain,
# which is exercised separately by the _read_key_from_stdin tests in
# test_cli.py).
# ---------------------------------------------------------------------------


def test_cmd_serve_passes_stdin_token_to_create_app() -> None:
    """A "token" captured from the --key-stdin init line reaches
    create_app's session_token, gating every route behind it."""
    import argparse

    import borgesica.__main__ as main_module

    original = main_module._stdin_session_token
    main_module._stdin_session_token = "session-tok-abc"
    try:
        with patch("borgesica.__main__._run_server") as mock_run, patch(
            "borgesica.serve.app.create_app"
        ) as mock_create_app:
            args = argparse.Namespace(port=0)
            main_module._cmd_serve(args, MagicMock())
    finally:
        main_module._stdin_session_token = original

    assert mock_create_app.call_args.kwargs.get("session_token") == "session-tok-abc"
    assert mock_run.called


def test_cmd_serve_without_stdin_token_disables_auth() -> None:
    """No "token" was ever captured (plain CLI/env-var flow, or --key-stdin
    not used) — create_app is called with session_token=None, the
    documented default/backward-compat behavior."""
    import argparse

    import borgesica.__main__ as main_module

    original = main_module._stdin_session_token
    main_module._stdin_session_token = None
    try:
        with patch("borgesica.__main__._run_server"), patch(
            "borgesica.serve.app.create_app"
        ) as mock_create_app:
            args = argparse.Namespace(port=0)
            main_module._cmd_serve(args, MagicMock())
    finally:
        main_module._stdin_session_token = original

    assert mock_create_app.call_args.kwargs.get("session_token") is None


def test_serve_parser_has_no_token_flag() -> None:
    """Mirrors test_serve_parser_has_no_host_flag: the token can never be
    supplied via argv/CLI flags — it only ever arrives over the stdin
    handshake, structurally impossible to leak into process argv."""
    from borgesica.__main__ import _build_parser

    with pytest.raises(SystemExit):
        _build_parser().parse_args(["serve", "--token", "whatever"])
