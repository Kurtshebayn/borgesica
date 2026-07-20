"""Tests for the serve API's per-session auth token (RISK-001/002/003).

`create_app(engine, session_token=...)` gates every route behind a required
`X-Borgesica-Token` header (or, for the SSE stream only — EventSource cannot
set custom headers — a `?token=` query parameter). When `session_token` is
not supplied (the default), auth is disabled: this is what lets every
pre-existing T5/T6 test in test_serve_app.py / test_serve_execution.py keep
calling `create_app(engine)` unauthenticated and stay green. Real production
boot (`__main__.py`'s `_cmd_serve`) always supplies a real session_token.
"""
from __future__ import annotations

import logging
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from borgesica.api import TranslatorEngine
from borgesica.domain.models import SourceType
from borgesica.serve.app import create_app
from tests.fakes import FakeTranslationProvider, InMemoryCheckpointStore

TOKEN = "test-session-token-abc123"


def _make_engine() -> TranslatorEngine:
    from borgesica.adapters.readers.srt_reader import SrtReader
    from borgesica.adapters.writers.srt_writer import SrtWriter
    from borgesica.domain.glossary import NullGlossaryExtractor

    return TranslatorEngine(
        provider=FakeTranslationProvider(),
        checkpoint=InMemoryCheckpointStore(),
        readers={SourceType.SRT: SrtReader()},
        writers={SourceType.SRT: SrtWriter()},
        extractor=NullGlossaryExtractor(),
    )


def _make_srt_fixture(tmp_path: Path) -> str:
    lines = ["1", "00:00:00,000 --> 00:00:01,500", "Cue text.", ""]
    srt_path = tmp_path / "test.srt"
    srt_path.write_text("\n".join(lines), encoding="utf-8")
    return str(srt_path)


def test_health_returns_ok_without_token_when_auth_configured() -> None:
    """The sidecar handshake (`wait_for_health` in sidecar.rs) probes GET
    /health with NO token before auth is established, so /health MUST be
    exempt from the session-token gate — otherwise the probe 401s and the
    sidecar never reports ready."""
    engine = _make_engine()
    client = TestClient(create_app(engine, session_token=TOKEN))

    resp = client.get("/health")

    assert resp.status_code == 200


def test_health_returns_ok_on_default_unauthenticated_app() -> None:
    engine = _make_engine()
    client = TestClient(create_app(engine))

    resp = client.get("/health")

    assert resp.status_code == 200


def test_request_without_token_rejected_when_configured(tmp_path: Path) -> None:
    engine = _make_engine()
    client = TestClient(create_app(engine, session_token=TOKEN))

    resp = client.post(
        "/jobs",
        json={"source_path": _make_srt_fixture(tmp_path), "model": "fake-model"},
    )

    assert resp.status_code == 401


def test_request_with_wrong_token_rejected(tmp_path: Path) -> None:
    engine = _make_engine()
    client = TestClient(create_app(engine, session_token=TOKEN))

    resp = client.post(
        "/jobs",
        json={"source_path": _make_srt_fixture(tmp_path), "model": "fake-model"},
        headers={"X-Borgesica-Token": "wrong-token"},
    )

    assert resp.status_code == 401


def test_request_with_correct_token_passes(tmp_path: Path) -> None:
    engine = _make_engine()
    client = TestClient(create_app(engine, session_token=TOKEN))

    resp = client.post(
        "/jobs",
        json={"source_path": _make_srt_fixture(tmp_path), "model": "fake-model"},
        headers={"X-Borgesica-Token": TOKEN},
    )

    assert resp.status_code == 201


def test_auth_disabled_when_no_session_token_configured(tmp_path: Path) -> None:
    """Backward-compat default: create_app(engine) with no session_token
    keeps every pre-existing T5/T6 test working unauthenticated."""
    engine = _make_engine()
    client = TestClient(create_app(engine))

    resp = client.post(
        "/jobs",
        json={"source_path": _make_srt_fixture(tmp_path), "model": "fake-model"},
    )

    assert resp.status_code == 201


def test_cancel_and_shutdown_also_require_the_token(tmp_path: Path) -> None:
    """RISK-002: /cancel and /shutdown take no body, so a browser page could
    blind-POST them — the required header (which a bare cross-origin POST
    cannot set without triggering a CORS preflight the server never
    approves) closes that hole."""
    engine = _make_engine()
    client = TestClient(create_app(engine, session_token=TOKEN))

    resp = client.post("/jobs/does-not-exist/cancel")
    assert resp.status_code == 401

    resp2 = client.post("/shutdown")
    assert resp2.status_code == 401


def test_sse_stream_accepts_token_via_query_param(tmp_path: Path) -> None:
    """Deviation (documented): native browser EventSource cannot set custom
    request headers, so the SSE route additionally accepts the token as a
    `?token=` query parameter. All mutating routes still require the header
    exclusively (covered above) — only this read-only stream has the
    fallback."""
    engine = _make_engine()
    client = TestClient(create_app(engine, session_token=TOKEN))
    resp = client.post(
        "/jobs",
        json={"source_path": _make_srt_fixture(tmp_path), "model": "fake-model"},
        headers={"X-Borgesica-Token": TOKEN},
    )
    job_id = resp.json()["id"]

    no_token_resp = client.get(f"/jobs/{job_id}/events")
    assert no_token_resp.status_code == 401

    with_query_resp = client.get(f"/jobs/{job_id}/events?token={TOKEN}")
    assert with_query_resp.status_code == 200


# ---------------------------------------------------------------------------
# Second correction round — ITEM B: the session token must never appear in
# emitted logs. uvicorn's default access logger writes the full request
# line INCLUDING the query string, so an unmodified `uvicorn.run` call would
# log the secret on every `GET /jobs/{id}/events?token=<SECRET>` request.
# _cmd_serve's fix (access_log=False, borgesica/__main__.py) is covered by
# the mocked test_cmd_serve_disables_uvicorn_access_log in
# test_cli_serve.py; the two tests below are the supplementary genuine
# end-to-end proof, against a REAL uvicorn server configured exactly the
# way _cmd_serve now configures it.
#
# NOTE on methodology: uvicorn's actual per-connection gate (checked at
# protocol-instantiation time — uvicorn/protocols/http/h11_impl.py:
# `self.access_log = self.access_logger.hasHandlers()`) is whether the
# "uvicorn.access" logger has ANY handlers at all — access_log=False works
# by stripping its handlers during configure_logging(). A naive test that
# attaches its OWN capture handler to that logger to "read the log" would
# itself flip `hasHandlers()` back to True and defeat the very thing being
# tested (verified empirically — see below). So instead of capturing
# output, these tests assert the real gating condition uvicorn checks.
# ---------------------------------------------------------------------------


def _run_uvicorn_server(app, *, access_log: bool) -> tuple[uvicorn.Server, threading.Thread, int]:
    config = uvicorn.Config(app, host="127.0.0.1", port=0, access_log=access_log, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.01)
    assert server.started, "uvicorn server did not start within the timeout"

    port = server.servers[0].sockets[0].getsockname()[1]
    return server, thread, port


def _stop_uvicorn_server(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=5)


def test_access_log_false_leaves_the_access_logger_without_handlers() -> None:
    """Genuine end-to-end proof, against a real uvicorn server configured
    exactly the way _cmd_serve now configures it (access_log=False): the
    "uvicorn.access" logger has no handlers once startup completes — the
    EXACT condition (`hasHandlers()`) uvicorn's h11 protocol layer checks
    per connection before ever calling `access_logger.info(...)`. No
    handlers means no access log record is ever emitted for any request —
    so a session token riding in a query string (the SSE fallback) can
    never appear in one, on any route."""
    engine = _make_engine()
    app = create_app(engine, session_token=TOKEN)

    server, thread, port = _run_uvicorn_server(app, access_log=False)
    try:
        assert logging.getLogger("uvicorn.access").hasHandlers() is False

        # Exercise a real request carrying the token in its query string —
        # confirms the server is actually live and serving, not merely that
        # it never got this far.
        url = f"http://127.0.0.1:{port}/jobs/does-not-exist/events?token={TOKEN}"
        try:
            urllib.request.urlopen(url, timeout=5)
        except urllib.error.HTTPError:
            pass  # 404 (unknown job) is expected — only the logging gate matters here

        assert logging.getLogger("uvicorn.access").hasHandlers() is False
    finally:
        _stop_uvicorn_server(server, thread)


def test_access_log_true_would_leave_the_access_logger_with_handlers() -> None:
    """Contrast/sanity check proving the assertion above is meaningful, not
    a trivial always-false: with access_log=True (uvicorn's own default —
    i.e. the pre-fix configuration), the same logger DOES have handlers,
    confirming uvicorn would have logged the token-bearing query string had
    _cmd_serve not disabled access logging."""
    engine = _make_engine()
    app = create_app(engine, session_token=TOKEN)

    server, thread, _ = _run_uvicorn_server(app, access_log=True)
    try:
        assert logging.getLogger("uvicorn.access").hasHandlers() is True
    finally:
        _stop_uvicorn_server(server, thread)
