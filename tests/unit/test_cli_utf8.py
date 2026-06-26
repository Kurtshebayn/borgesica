"""CLI UTF-8 regression test (M2-0 test 8).

Commit 9440760 fixed a Windows cp1252 crash by reconfiguring stdout/stderr to
UTF-8 in borgesica/__main__.py (via stream.reconfigure(encoding='utf-8')).

This test verifies that _reconfigure_streams() (or the equivalent logic called
in main()) makes it possible to print the non-ASCII progress arrow on a
cp1252-configured stdout WITHOUT raising UnicodeEncodeError.

Design: we inject a fake BytesIO-backed TextIOWrapper configured as cp1252
(mimicking a Windows console), call the reconfigure helper, and then assert
that printing a non-ASCII character succeeds without UnicodeEncodeError.
"""
from __future__ import annotations

import codecs
import io
import sys


# ---------------------------------------------------------------------------
# Test 8 (M2-0) — CLI UTF-8 stdout reconfigure regression
# ---------------------------------------------------------------------------


def test_cli_reconfigure_enables_non_ascii_output_on_cp1252_stdout() -> None:
    """The CLI's stdout reconfigure must allow non-ASCII output on a cp1252 stream.

    Simulates the Windows environment where the console defaults to cp1252.
    After reconfiguration, writing the progress arrow (→, U+2192) must NOT
    raise UnicodeEncodeError.
    """
    from borgesica.__main__ import _reconfigure_streams

    # Build a fake cp1252-encoded BytesIO stream (simulates Windows console)
    raw = io.BytesIO()
    fake_stdout = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")

    # Verify the baseline: writing a non-ASCII char on a raw cp1252 stream raises.
    try:
        fake_stdout.write("→")
        fake_stdout.flush()
        baseline_raises = False  # some implementations may silently replace
    except (UnicodeEncodeError, UnicodeError):
        baseline_raises = True
    # Reset after baseline probe
    raw.seek(0)
    raw.truncate()

    # Now rebuild the fake stream for the real test
    raw2 = io.BytesIO()
    fake_stdout2 = io.TextIOWrapper(raw2, encoding="cp1252", errors="strict")

    # Call the reconfigure helper — it should switch encoding to utf-8
    _reconfigure_streams(fake_stdout2, fake_stdout2)

    # After reconfiguration, writing non-ASCII must succeed (no UnicodeEncodeError)
    try:
        fake_stdout2.write("→")
        fake_stdout2.flush()
    except (UnicodeEncodeError, UnicodeError) as exc:
        raise AssertionError(
            f"_reconfigure_streams did not fix encoding: writing '→' still raises {exc}"
        ) from exc
