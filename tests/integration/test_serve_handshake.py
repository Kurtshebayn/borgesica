"""Real-subprocess handshake test for the Tauri sidecar contract.

End-to-end verification found two gaps that made the desktop handshake
non-functional even though every unit test passed:

  1. `borgesica serve` exposed no `/health` route, but the Rust
     `wait_for_health` probes `GET /health` with no token before auth is
     established — a missing route (404) or an authenticated one (401) both
     fail the probe.
  2. `_cmd_serve` never emitted the ``{"event": "ready", "port": N}`` line
     that the Rust `read_ready_port` parses to discover the ephemeral port
     bound by ``--port 0``.

This test spawns the real subprocess exactly the way `sidecar.rs` does and
exercises the full handshake, so a regression in either gap fails here.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_TOKEN = "handshake-test-token"
_READY_TIMEOUT_S = 30.0


def _read_ready_port(proc: subprocess.Popen[str]) -> int:
    """Mirror Rust's read_ready_port: read stdout lines until the ready event."""
    assert proc.stdout is not None
    deadline = time.time() + _READY_TIMEOUT_S
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break  # stdout closed before a ready line arrived
        try:
            event = json.loads(line.strip())
        except json.JSONDecodeError:
            continue  # banner/log noise — ignored, exactly like parse_ready_line
        if event.get("event") == "ready":
            port = event.get("port")
            assert isinstance(port, int) and port > 0
            return port
    raise AssertionError("serve did not emit a {'event':'ready','port':N} line")


@pytest.mark.integration
def test_serve_sidecar_handshake(tmp_path: Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "borgesica", "serve", "--port", "0", "--key-stdin"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        # stderr is discarded, NOT piped: an unread stderr PIPE deadlocks the
        # child once the OS buffer fills (the RES-003 failure mode). We assert
        # nothing on stderr, so DEVNULL is both safe and correct.
        stderr=subprocess.DEVNULL,
        cwd=tmp_path,  # keep jobs.db/corpus.db out of the repo
        text=True,
        bufsize=1,
    )
    try:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"api_key": "sk-dummy-unused", "token": _TOKEN}) + "\n")
        proc.stdin.flush()

        port = _read_ready_port(proc)
        base = f"http://127.0.0.1:{port}"

        # Gap 1: /health answers 200 WITHOUT a token (unauthenticated probe).
        with urllib.request.urlopen(f"{base}/health", timeout=5) as resp:
            assert resp.status == 200
            assert json.loads(resp.read())["status"] == "ok"

        # Auth still gates everything else: no token -> 401.
        with pytest.raises(urllib.error.HTTPError) as no_token:
            urllib.request.urlopen(f"{base}/jobs/none", timeout=5)
        assert no_token.value.code == 401

        # Valid token passes the gate (404 = job not found, i.e. auth accepted).
        req = urllib.request.Request(
            f"{base}/jobs/none", headers={"X-Borgesica-Token": _TOKEN}
        )
        with pytest.raises(urllib.error.HTTPError) as with_token:
            urllib.request.urlopen(req, timeout=5)
        assert with_token.value.code == 404

        # Graceful shutdown works.
        shutdown = urllib.request.Request(
            f"{base}/shutdown", method="POST", headers={"X-Borgesica-Token": _TOKEN}
        )
        with urllib.request.urlopen(shutdown, timeout=5) as resp:
            assert resp.status == 200
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
