"""Tests for T5 — `serve` subcommand registration in borgesica.__main__.

Covers: localhost-only binding (non-loopback host can never be configured),
key intake via the existing --key-stdin mechanism (never argv), and that the
subcommand is wired into main()'s dispatch using the existing pattern.

No live server is ever started: uvicorn.run() is mocked in every test that
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
        "uvicorn.run"
    ) as mock_run:
        mock_build.return_value = MagicMock()
        with patch("sys.stdin", io.StringIO('{"api_key": "sk-x"}\n')):
            main(["serve", "--port", "0", "--provider", "anthropic", "--key-stdin"])

    assert mock_run.call_args.kwargs["host"] == "127.0.0.1"


def test_serve_host_constant_is_loopback() -> None:
    from borgesica.serve.app import SERVE_HOST

    assert SERVE_HOST == "127.0.0.1"


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

    with patch("borgesica.__main__._build_engine") as mock_build, patch("uvicorn.run"):
        mock_build.return_value = MagicMock()
        with patch("sys.stdin", io.StringIO(f'{{"api_key": "{secret}"}}\n')):
            main(argv)

    assert mock_build.call_args.kwargs.get("key_stdin") is True
    assert not any(secret in tok for tok in argv)
