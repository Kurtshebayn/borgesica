"""CORS for the Tauri webview (functional gap found running the real GUI).

The desktop app's `fetch` calls carry the `X-Borgesica-Token` header and a
JSON body, making them non-simple cross-origin requests: the webview
(origin http://localhost:1420 in dev, tauri://localhost or
https://tauri.localhost when bundled) issues a CORS preflight before the
real request. Without CORS the preflight goes unanswered and the browser
fails the call with "TypeError: Failed to fetch" — even though the sidecar
is up (the Rust health probe is not a browser and is unaffected).

Security note: this does NOT reopen RISK-001/002/003. Origins are restricted
to the app's own webview (not "*"), and the per-session token still gates
every route — a foreign origin that clears CORS still gets 401 without it.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from borgesica.api import TranslatorEngine
from borgesica.domain.models import SourceType
from borgesica.serve.app import create_app
from tests.fakes import FakeTranslationProvider, InMemoryCheckpointStore

_DEV_ORIGIN = "http://localhost:1420"
_TOKEN = "cors-test-token"


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


def _client() -> TestClient:
    return TestClient(create_app(_make_engine(), session_token=_TOKEN))


def test_preflight_from_webview_origin_is_allowed() -> None:
    resp = _client().options(
        "/jobs",
        headers={
            "Origin": _DEV_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-borgesica-token,content-type",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == _DEV_ORIGIN
    allowed = resp.headers.get("access-control-allow-headers", "").lower()
    assert "x-borgesica-token" in allowed


def test_actual_request_carries_allow_origin_for_webview() -> None:
    resp = _client().get("/health", headers={"Origin": _DEV_ORIGIN})
    assert resp.headers.get("access-control-allow-origin") == _DEV_ORIGIN


def test_bundled_tauri_origin_is_allowed() -> None:
    resp = _client().get("/health", headers={"Origin": "https://tauri.localhost"})
    assert resp.headers.get("access-control-allow-origin") == "https://tauri.localhost"


def test_foreign_origin_gets_no_allow_origin_header() -> None:
    resp = _client().get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in resp.headers
