"""GET /ollama/models: the tags pulled on the local Ollama, for the wizard's
model dropdown. The Ollama HTTP call is mocked; no Ollama is needed."""
from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

from fastapi.testclient import TestClient

from borgesica.api import TranslatorEngine
from borgesica.domain.glossary import NullGlossaryExtractor
from borgesica.serve.app import create_app
from tests.fakes import FakeTranslationProvider, InMemoryCheckpointStore

TOKEN = "ollama-token"
URLOPEN = "urllib.request.urlopen"


def _client() -> TestClient:
    engine = TranslatorEngine(
        provider=FakeTranslationProvider(),
        checkpoint=InMemoryCheckpointStore(),
        readers={},
        writers={},
        extractor=NullGlossaryExtractor(),
    )
    return TestClient(create_app(engine, session_token=TOKEN))


def _tags(payload: object) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode())


def test_lists_the_pulled_model_names(monkeypatch) -> None:
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    body = {
        "models": [
            {"name": "qwen3.6-27b-tr:latest", "size": 1},
            {"name": "hf.co/unsloth/Qwen3.6-27B-GGUF:UD-Q3_K_XL"},
        ]
    }
    with patch(URLOPEN, return_value=_tags(body)) as urlopen:
        res = _client().get("/ollama/models", headers={"X-Borgesica-Token": TOKEN})

    assert res.status_code == 200
    assert res.json() == {
        "models": ["qwen3.6-27b-tr:latest", "hf.co/unsloth/Qwen3.6-27B-GGUF:UD-Q3_K_XL"],
        "error": None,
    }
    assert urlopen.call_args.args[0] == "http://localhost:11434/api/tags"


def test_honours_ollama_host(monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_HOST", "192.168.1.42:11434")
    with patch(URLOPEN, return_value=_tags({"models": []})) as urlopen:
        res = _client().get("/ollama/models", headers={"X-Borgesica-Token": TOKEN})

    assert res.json() == {"models": [], "error": None}
    assert urlopen.call_args.args[0] == "http://192.168.1.42:11434/api/tags"


def test_unreachable_ollama_is_reported_not_raised() -> None:
    with patch(URLOPEN, side_effect=urllib.error.URLError("connection refused")):
        res = _client().get("/ollama/models", headers={"X-Borgesica-Token": TOKEN})

    assert res.status_code == 200
    body = res.json()
    assert body["models"] == []
    assert "connection refused" in body["error"]


def test_unexpected_document_is_reported() -> None:
    with patch(URLOPEN, return_value=_tags({"nope": 1})):
        res = _client().get("/ollama/models", headers={"X-Borgesica-Token": TOKEN})

    assert res.status_code == 200
    assert res.json()["models"] == []
    assert res.json()["error"]


def test_requires_the_session_token() -> None:
    with patch(URLOPEN) as urlopen:
        res = _client().get("/ollama/models")

    assert res.status_code == 401
    urlopen.assert_not_called()
