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

from pathlib import Path

import pytest
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
