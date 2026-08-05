"""Borgésica CLI — thin command-line interface for the translation engine.

This module is a testing aid and simple end-user entry point, not the
product UI. It wires concrete adapters into TranslatorEngine and exposes
subcommands for the full job lifecycle.

Usage:
    python -m borgesica <subcommand> [options]
    borgesica <subcommand> [options]          # after pip install

Subcommands:
    create  <source_file> --model <model> [--provider anthropic|deepseek|openai|ollama]
             [--chunk-size N] [--budget USD] [--quality-mode fast|reflective] [--strict]
             [--extract N] [--from M]
             (source format auto-detected from .srt/.epub/.pdf extension)
             By default (no --strict), a chunk that exhausts all translation
             attempts is skipped (FAILED) and the run continues; the job still
             finishes DONE. Pass --strict to restore the pre-continue-on-error
             contract: the job PAUSES immediately on the first FAILED chunk.
             --extract N keeps only N chunks, for comparing models or iterating
             on translation quality without paying for a full book. --from M
             starts that window at chunk M instead of the beginning, to reach
             defects that only surface deep into a long book. Extracted chunks
             keep their original indices.
    estimate <job_id>
    run     <job_id> [--out <path>] [--provider ...]
             Prints a skip-summary line if any chunks ended FAILED.
    resume  <job_id> [--out <path>] [--provider ...]
             Prints a skip-summary line if any chunks ended FAILED.
    status  <job_id>
             Lists FAILED chunk indices (if any) after the JSON job dump.
    cancel  <job_id>
    glossary show   <job_id>
    glossary update <job_id> <term> <translation> [--lock]

Provider selection:
    Pass --provider on any command, or let it auto-detect: BORGESICA_PROVIDER env
    var if set, otherwise whichever key is present (anthropic → deepseek → openai → ollama).

Environment (per provider; only the selected provider's key is required):
    ANTHROPIC_API_KEY  — for --provider anthropic
    DEEPSEEK_API_KEY   — for --provider deepseek
    OPENAI_API_KEY     — for --provider openai
    OLLAMA_HOST        — for --provider ollama (optional; defaults to localhost:11434)
    BORGESICA_PROVIDER — optional; sets the default provider when --provider is omitted

API key from stdin (--key-stdin):
    Instead of an environment variable, pass --key-stdin on any provider command
    to read the selected provider's key from a single newline-delimited JSON line
    on stdin: {"api_key": "..."}. This is how the desktop app hands the key (read
    from the OS keyring) to this subprocess — the secret never touches argv (which
    is world-readable via ps/Task Manager) or a persisted env var. Only the key
    travels this channel; the subcommand, job_id, provider and model stay on argv.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from borgesica.api import TranslatorEngine
from borgesica.domain.errors import BorgesicaError, JobNotFoundError, UnsupportedFormatError
from borgesica.domain.models import GlossaryEntry, JobConfig, Progress, SourceType

# Map file extension → SourceType and back (extension → format detection).
_EXT_TO_SOURCE_TYPE: dict[str, SourceType] = {
    ".srt": SourceType.SRT,
    ".epub": SourceType.EPUB,
    ".pdf": SourceType.PDF,
}
_SOURCE_TYPE_TO_EXT: dict[SourceType, str] = {v: k for k, v in _EXT_TO_SOURCE_TYPE.items()}


def _source_type_for(path: str) -> SourceType:
    """Detect the SourceType from a file's extension (case-insensitive).

    Raises:
        UnsupportedFormatError: if the extension is not one of .srt/.epub/.pdf.
    """
    ext = Path(path).suffix.lower()
    try:
        return _EXT_TO_SOURCE_TYPE[ext]
    except KeyError:
        raise UnsupportedFormatError(
            path=path,
            reason=f"unsupported extension {ext!r}; supported formats: .srt, .epub, .pdf",
        ) from None


def _ext_for(source_type: SourceType) -> str:
    """Return the output file extension for a SourceType (defaults to .txt)."""
    return _SOURCE_TYPE_TO_EXT.get(source_type, ".txt")

# ---------------------------------------------------------------------------
# Engine builder — the ONLY place concrete adapters are instantiated from CLI
# Separated so tests can mock it cleanly.
# ---------------------------------------------------------------------------


def _default_provider() -> str:
    """Resolve the provider when --provider is not given.

    Precedence: explicit BORGESICA_PROVIDER env var → auto-detect from whichever
    provider key is present (anthropic → deepseek → openai → ollama) → 'anthropic'
    (which then errors clearly about its missing key).
    """
    explicit = os.environ.get("BORGESICA_PROVIDER")
    if explicit:
        return explicit.lower()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("DEEPSEEK_API_KEY"):
        return "deepseek"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("OLLAMA_HOST"):
        return "ollama"
    return "anthropic"


def _require_env(var: str, provider: str) -> str:
    """Return env var *var* or exit(1) with a clear message naming the *provider*."""
    value = os.environ.get(var)
    if not value:
        print(
            f"ERROR: {var} environment variable is not set (required for --provider {provider}).",
            file=sys.stderr,
        )
        sys.exit(1)
    return value


# RISK-001/002/003: the serve API's per-session auth token, captured as a
# side effect of reading the SAME trusted stdin init line as the provider
# API key (see _read_key_from_stdin). Module-level because the token must
# reach _cmd_serve (called later, from main()'s dispatch table) without
# threading a new parameter through every _build_engine/_build_provider
# caller — none of which otherwise need to know about serve-layer auth.
# None when --key-stdin was never used (plain CLI/env-var flow) or the init
# line carried no "token" field: create_app() then runs with auth disabled,
# same as it always has.
_stdin_session_token: str | None = None


def _read_key_from_stdin(provider: str) -> str:
    """Read the provider API key from a single JSON init line on stdin.

    The desktop shell (Tauri/Rust) reads the key from the OS keyring and writes
    one newline-delimited JSON message — {"api_key": "...", "token": "..."} —
    to this subprocess's stdin. Keeping the key off argv (visible via ps/Task
    Manager to any process) and out of a persisted env var is the whole point:
    only secrets travel this channel. Newline-delimited JSON is the
    forward-compatible framing for any richer init message added later.

    As of RISK-001/002/003, the same init line also carries the serve API's
    per-session auth "token" (captured into the module-level
    `_stdin_session_token` for `_cmd_serve` to pick up — see that global's
    docstring for why it is not threaded through as a return value here).

    Exits(1) with a clear message if the line is missing, malformed, or has no
    non-empty api_key — same failure contract as _require_env.
    """
    global _stdin_session_token
    _stdin_session_token = None  # reset each read — no stale token across calls
    line = sys.stdin.readline()
    key: str | None = None
    try:
        message = json.loads(line)
        if isinstance(message, dict):
            raw = message.get("api_key")
            key = raw if isinstance(raw, str) and raw else None
            raw_token = message.get("token")
            if isinstance(raw_token, str) and raw_token:
                _stdin_session_token = raw_token
    except json.JSONDecodeError:
        key = None
    if not key:
        print(
            'ERROR: --key-stdin expects an init line {"api_key": "..."} on stdin '
            f"(required for --provider {provider}).",
            file=sys.stderr,
        )
        sys.exit(1)
    return key


def _resolve_key(var: str, provider: str, *, key_stdin: bool) -> str:
    """Resolve the provider API key from stdin (desktop app) or the environment (CLI)."""
    if key_stdin:
        return _read_key_from_stdin(provider)
    return _require_env(var, provider)


def _build_provider(provider: str, *, key_stdin: bool = False) -> Any:
    """Instantiate a TranslationProvider by name.

    Supported: 'anthropic' (ANTHROPIC_API_KEY), 'deepseek' (DEEPSEEK_API_KEY),
    'openai' (OPENAI_API_KEY), 'ollama' (local; OLLAMA_HOST optional, no key needed).

    key_stdin: when True, the provider's API key is read from a JSON init line
    on stdin (desktop-app mode) instead of the environment. ollama needs no key,
    so the flag is inert there.

    Raises:
        SystemExit: if an unknown provider is given or a required key is missing.
    """
    name = provider.lower()
    if name == "anthropic":
        from borgesica.adapters.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(
            api_key=_resolve_key("ANTHROPIC_API_KEY", "anthropic", key_stdin=key_stdin)
        )
    if name == "deepseek":
        from borgesica.adapters.providers.openai_compatible_provider import (
            OpenAICompatibleProvider,
        )

        key = _resolve_key("DEEPSEEK_API_KEY", "deepseek", key_stdin=key_stdin)
        return OpenAICompatibleProvider.deepseek(api_key=key)
    if name == "openai":
        from borgesica.adapters.providers.openai_compatible_provider import (
            OpenAICompatibleProvider,
        )

        key = _resolve_key("OPENAI_API_KEY", "openai", key_stdin=key_stdin)
        return OpenAICompatibleProvider.openai(api_key=key)
    if name == "ollama":
        from borgesica.adapters.providers.ollama_provider import OllamaProvider

        return OllamaProvider()  # base_url resolved from OLLAMA_HOST; no API key needed
    print(
        f"ERROR: unknown provider {provider!r} (choose: anthropic, deepseek, openai, ollama).",
        file=sys.stderr,
    )
    sys.exit(1)


def _build_engine(
    *, provider: str = "anthropic", model: str = "", db_path: str = "", key_stdin: bool = False
) -> TranslatorEngine:
    """Construct a TranslatorEngine wired with real adapters for all formats.

    Args:
        provider: Provider name — 'anthropic' (default), 'deepseek', 'openai', or 'ollama'.
        model:    Model string (persisted in JobConfig; not validated here).
        db_path:  SQLite checkpoint DB path. Defaults to ~/.borgesica/jobs.db.
                  Pass ":memory:" for ephemeral use.

    Returns:
        Fully wired TranslatorEngine (SRT/EPUB/PDF readers + writers).

    Raises:
        SystemExit: if the selected provider's API key env var is not set.
    """
    translation_provider = _build_provider(provider, key_stdin=key_stdin)

    # Lazy imports — keep CLI startup fast and avoid dependency errors
    # for people who only use the test helpers.
    from borgesica.adapters.checkpoints.sqlite_checkpoint import SQLiteCheckpointStore
    from borgesica.adapters.readers.epub_reader import EpubReader
    from borgesica.adapters.readers.pdf_plumber_reader import PdfPlumberReader
    from borgesica.adapters.readers.srt_reader import SrtReader
    from borgesica.adapters.writers.epub_writer import EpubWriter
    from borgesica.adapters.writers.pdf_writer import PdfWriter
    from borgesica.adapters.writers.srt_writer import SrtWriter
    from borgesica.domain.glossary import NullGlossaryExtractor

    if not db_path:
        db_dir = Path.home() / ".borgesica"
        db_dir.mkdir(parents=True, exist_ok=True)
        db_path = str(db_dir / "jobs.db")

    # Corpus capture is engine-wide (design decision #6/#10): CLI runs get it
    # from day one, in a separate corpus.db next to jobs.db. Mirrors db_path's
    # ":memory:" escape hatch so tests exercising the real _build_engine path
    # never touch the filesystem.
    from borgesica.adapters.corpus.sqlite_corpus import SQLiteCorpusStore

    corpus_db_path = (
        ":memory:" if db_path == ":memory:" else str(Path(db_path).parent / "corpus.db")
    )

    return TranslatorEngine(
        provider=translation_provider,
        checkpoint=SQLiteCheckpointStore(db_path=db_path),
        readers={
            SourceType.SRT: SrtReader(),
            SourceType.EPUB: EpubReader(),
            SourceType.PDF: PdfPlumberReader(),
        },
        writers={
            SourceType.SRT: SrtWriter(),
            SourceType.EPUB: EpubWriter(),
            SourceType.PDF: PdfWriter(),
        },
        extractor=NullGlossaryExtractor(),  # default: no LLM glossary seed on CLI
        corpus_store=SQLiteCorpusStore(corpus_db_path),
        provider_name=provider,
    )


# ---------------------------------------------------------------------------
# Progress printer
# ---------------------------------------------------------------------------


def _print_progress(progress: Progress) -> None:
    pct = (
        int(100 * (progress.chunk_index + 1) / progress.total_chunks)
        if progress.total_chunks > 0
        else 0
    )
    print(
        f"  chunk {progress.chunk_index + 1}/{progress.total_chunks}"
        f"  ({pct}%)  cost=${progress.cost_usd:.5f}",
        flush=True,
    )


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _cmd_create(args: argparse.Namespace, engine: TranslatorEngine) -> int:
    try:
        source_type = _source_type_for(args.source_file)
    except UnsupportedFormatError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if source_type is SourceType.PDF:
        print(
            "WARNING: PDF input can be read/translated, but PDF output is not yet "
            "supported (the run step will fail at write). Use .epub or .srt for a full round-trip.",
            file=sys.stderr,
        )

    config = JobConfig(
        source_type=source_type,
        model=args.model,
        chunk_size=getattr(args, "chunk_size", 25),
        budget_usd=getattr(args, "budget", None),
        quality_mode=getattr(args, "quality_mode", "fast"),  # type: ignore[arg-type]
        continue_on_error=not getattr(args, "strict", False),
        prose_segmentation=(
            "paragraph" if getattr(args, "per_paragraph", False) else "batch"
        ),
        extract_chunks=getattr(args, "extract_chunks", None),
        extract_offset=getattr(args, "extract_offset", 0),
    )
    try:
        job = engine.create_job(args.source_file, config)
    except ValueError as exc:
        # e.g. an extract window that starts past the last chunk — a user
        # mistake, not a crash.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(job.id)
    return 0


def _cmd_estimate(args: argparse.Namespace, engine: TranslatorEngine) -> int:
    try:
        estimate = engine.estimate_cost(args.job_id)
        print(estimate.model_dump_json(indent=2))
        return 0
    except JobNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _resolve_out(args: argparse.Namespace, engine: TranslatorEngine) -> str:
    """Return the explicit --out, or a default that matches the job's source format."""
    if getattr(args, "out", None):
        return str(args.out)
    job = engine.status(args.job_id)  # raises JobNotFoundError if unknown
    return _default_out(args.job_id, job.config.source_type)


def _print_skip_summary(args: argparse.Namespace, engine: TranslatorEngine) -> None:
    """Print a skip summary naming the FAILED chunks, their consequence
    (segments keep the untranslated source text in the output) and the
    remedy (`resume` retries only failed chunks). No output when there
    are none (continue-on-error skip report)."""
    failed = engine.failed_chunk_indices(args.job_id)
    if failed:
        print(
            f"WARNING: {len(failed)} chunk(s) failed and were skipped: {failed}\n"
            f"Their segments keep the untranslated source text in the output.\n"
            f"Run `borgesica resume {args.job_id}` to retry only the failed chunks."
        )


def _print_best_effort_summary(args: argparse.Namespace, engine: TranslatorEngine) -> None:
    """Print an end-of-run best-effort summary, analogous to
    _print_skip_summary (spec: "End-of-run best-effort summary" — v1). Prints
    nothing when there are zero best-effort chunks (no noise on the happy
    path). Best-effort chunks ARE translated (unlike FAILED skips) but did
    not pass tag/segment validation on any attempt."""
    best_effort = engine.best_effort_chunk_indices(args.job_id)
    if best_effort:
        print(
            f"NOTE: {len(best_effort)} chunk(s) remained best-effort "
            f"(translated, but did not pass validation): {best_effort}"
        )


def _cmd_run(args: argparse.Namespace, engine: TranslatorEngine) -> int:
    try:
        out_path = _resolve_out(args, engine)
        print(f"Running job {args.job_id} → {out_path}")
        final_job = engine.run_job(args.job_id, out_path=out_path, on_progress=_print_progress)
        print(f"Done. Status={final_job.status}  cost=${final_job.cost_usd:.5f}")
        _print_skip_summary(args, engine)
        _print_best_effort_summary(args, engine)
        return 0
    except BorgesicaError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _cmd_resume(args: argparse.Namespace, engine: TranslatorEngine) -> int:
    try:
        out_path = _resolve_out(args, engine)
        print(f"Resuming job {args.job_id} → {out_path}")
        final_job = engine.resume_job(args.job_id, out_path=out_path, on_progress=_print_progress)
        print(f"Done. Status={final_job.status}  cost=${final_job.cost_usd:.5f}")
        _print_skip_summary(args, engine)
        _print_best_effort_summary(args, engine)
        return 0
    except BorgesicaError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _cmd_status(args: argparse.Namespace, engine: TranslatorEngine) -> int:
    try:
        job = engine.status(args.job_id)
        print(job.model_dump_json(indent=2))
        failed = engine.failed_chunk_indices(args.job_id)
        if failed:
            print(f"Failed chunks: {failed}")
        return 0
    except JobNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _cmd_cancel(args: argparse.Namespace, engine: TranslatorEngine) -> int:
    try:
        engine.cancel_job(args.job_id)
        print(f"Cancel signal sent for job {args.job_id}.")
        return 0
    except JobNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _cmd_glossary_show(args: argparse.Namespace, engine: TranslatorEngine) -> int:
    try:
        glossary = engine.get_glossary(args.job_id)
        data = [e.model_dump() for e in glossary.entries]
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    except JobNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _cmd_serve(args: argparse.Namespace, engine: TranslatorEngine) -> int:
    """Boot the local HTTP API (borgesica.serve) bound to loopback only.

    main() builds *engine* eagerly for every subcommand (including
    --key-stdin key intake), so the key is consumed once at process boot,
    before serving any request (spec: "Key at process boot").

    RISK-001/002/003: when --key-stdin's init line carried a "token", every
    route is gated behind it (create_app's session_token). Without
    --key-stdin (plain CLI/manual `borgesica serve`), no token was ever read
    and auth stays disabled — a known, documented scope boundary (see
    _stdin_session_token).

    access_log=False: defense-in-depth. The session token now travels in the
    X-Borgesica-Token header on every route (the SSE stream is read with
    `fetch`, not native EventSource, so it no longer needs a `?token=` query
    fallback), so it never appears in a request line to begin with. Disabling
    the access log keeps it that way even if a URL ever carried a secret again,
    and cuts per-request log noise for a loopback-only local API.
    """
    import uvicorn

    from borgesica.serve.app import SERVE_HOST, create_app

    app = create_app(engine, session_token=_stdin_session_token)
    config = uvicorn.Config(app, host=SERVE_HOST, port=args.port, access_log=False)
    _run_server(config)
    return 0


def _run_server(config: Any) -> None:
    """Run a uvicorn server, announcing the actually-bound port on stdout.

    With ``--port 0`` (the default, and what the Tauri sidecar spawns) the OS
    assigns an ephemeral port; the Rust handshake (``read_ready_port`` in
    sidecar.rs) discovers it by parsing this exact line from stdout:
    ``{"event": "ready", "port": N}``. ``flush=True`` is mandatory — the
    parent reads the pipe line-by-line and would otherwise block on buffering.

    Isolated behind this seam so unit tests can drive ``_cmd_serve`` (and
    assert on the ``uvicorn.Config`` it builds) without booting a real server.
    """
    import asyncio

    import uvicorn

    server = uvicorn.Server(config)

    async def _serve_and_announce() -> None:
        serve_task = asyncio.ensure_future(server.serve())
        try:
            while not server.started:
                await asyncio.sleep(0.02)
            bound_port = server.servers[0].sockets[0].getsockname()[1]
            print(json.dumps({"event": "ready", "port": bound_port}), flush=True)
            await serve_task
        finally:
            server.should_exit = True

    asyncio.run(_serve_and_announce())


def _cmd_glossary_update(args: argparse.Namespace, engine: TranslatorEngine) -> int:
    try:
        entry = GlossaryEntry(
            term=args.term,
            translation=args.translation,
            locked=getattr(args, "lock", False),
        )
        engine.update_glossary(args.job_id, [entry])
        print(f"Updated glossary entry: {args.term!r} → {args.translation!r}")
        return 0
    except JobNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _default_out(job_id: str, source_type: SourceType) -> str:
    return str(Path.home() / ".borgesica" / f"{job_id}_translated{_ext_for(source_type)}")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="borgesica",
        description="Borgésica — literary translation engine (SRT / EPUB / PDF)",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    provider_choices = ["anthropic", "deepseek", "openai", "ollama"]
    provider_help = (
        "Provider: anthropic/deepseek/openai/ollama. Default: auto-detect from "
        "BORGESICA_PROVIDER env or whichever API key is set."
    )

    def _add_provider(p: argparse.ArgumentParser) -> None:
        p.add_argument("--provider", choices=provider_choices, default=None, help=provider_help)
        p.add_argument(
            "--key-stdin",
            action="store_true",
            default=False,
            dest="key_stdin",
            help=(
                'Read the provider API key from a JSON init line {"api_key": "..."} '
                "on stdin instead of the environment. Used by the desktop app to keep "
                "the key out of argv and env vars."
            ),
        )

    # create
    p_create = sub.add_parser(
        "create", help="Create a translation job from a source file (.srt/.epub/.pdf)"
    )
    p_create.add_argument(
        "source_file", help="Path to the source file (.srt, .epub, or .pdf — format auto-detected)"
    )
    p_create.add_argument("--model", required=True, help="Model string (e.g. deepseek-v4-flash)")
    _add_provider(p_create)
    p_create.add_argument("--chunk-size", type=int, default=25, dest="chunk_size")
    p_create.add_argument("--budget", type=float, default=None, help="Budget in USD")
    p_create.add_argument(
        "--quality-mode",
        choices=["fast", "reflective"],
        default="fast",
        dest="quality_mode",
    )
    p_create.add_argument(
        "--strict",
        action="store_true",
        default=False,
        help=(
            "Pause the job on the first FAILED chunk instead of continuing "
            "(restores the pre-continue-on-error contract)."
        ),
    )
    p_create.add_argument(
        "--extract",
        type=int,
        default=None,
        dest="extract_chunks",
        metavar="N",
        help=(
            "Translate only the first N chunks. For comparing models or "
            "iterating on translation quality without paying for a full book. "
            "The glossary is seeded from the extract alone."
        ),
    )
    p_create.add_argument(
        "--from",
        type=int,
        default=0,
        dest="extract_offset",
        metavar="M",
        help=(
            "Start the extract at chunk M instead of the beginning. Translation "
            "defects often surface deep into a long book, where an extract of "
            "the opening cannot reach them. Extracted chunks keep their "
            "original indices."
        ),
    )
    p_create.add_argument(
        "--per-paragraph",
        action="store_true",
        default=False,
        dest="per_paragraph",
        help=(
            "One paragraph per translation call (EPUB/PDF only). Slower, but "
            "segment alignment is guaranteed by structure — recommended for "
            "small local models that cannot follow the multi-segment contract."
        ),
    )

    # estimate
    p_estimate = sub.add_parser("estimate", help="Estimate cost for a job's pending chunks")
    p_estimate.add_argument("job_id", help="Job ID")
    _add_provider(p_estimate)  # estimate uses the provider's count_tokens + price

    # run
    p_run = sub.add_parser("run", help="Run (translate) a job")
    p_run.add_argument("job_id", help="Job ID")
    p_run.add_argument("--out", default=None, help="Output file path")
    _add_provider(p_run)

    # resume
    p_resume = sub.add_parser("resume", help="Resume a paused or cancelled job")
    p_resume.add_argument("job_id", help="Job ID")
    p_resume.add_argument("--out", default=None, help="Output file path")
    _add_provider(p_resume)

    # status
    p_status = sub.add_parser("status", help="Show current job status as JSON")
    p_status.add_argument("job_id", help="Job ID")
    _add_provider(p_status)

    # cancel
    p_cancel = sub.add_parser("cancel", help="Cancel a job (cooperative)")
    p_cancel.add_argument("job_id", help="Job ID")
    _add_provider(p_cancel)

    # glossary (sub-sub-commands)
    p_glossary = sub.add_parser("glossary", help="Inspect or edit the job glossary")
    gsub = p_glossary.add_subparsers(dest="glossary_command", metavar="<action>")

    p_gshow = gsub.add_parser("show", help="Print glossary as JSON")
    p_gshow.add_argument("job_id")
    _add_provider(p_gshow)

    p_gupdate = gsub.add_parser("update", help="Add or update a glossary entry")
    p_gupdate.add_argument("job_id")
    p_gupdate.add_argument("term")
    p_gupdate.add_argument("translation")
    p_gupdate.add_argument("--lock", action="store_true", default=False)
    _add_provider(p_gupdate)

    # serve — local HTTP API (design decision #1/#5). No --host flag: the
    # server always binds loopback only (borgesica.serve.app.SERVE_HOST);
    # non-loopback binding can never be configured via this CLI.
    p_serve = sub.add_parser(
        "serve", help="Start the local HTTP API (127.0.0.1 only) for the desktop app"
    )
    p_serve.add_argument(
        "--port", type=int, default=0, help="Port to listen on (default: 0, ephemeral)"
    )
    _add_provider(p_serve)

    return parser


# ---------------------------------------------------------------------------
# main() — entry point
# ---------------------------------------------------------------------------


def _reconfigure_streams(*streams: Any) -> None:
    """Reconfigure text streams to use UTF-8 encoding where supported.

    On Windows, console streams default to cp1252, which raises UnicodeEncodeError
    when printing non-ASCII characters (e.g. the progress arrow →, accented Spanish).
    This function calls stream.reconfigure(encoding='utf-8') on any stream that
    supports it (TextIOWrapper-like objects with a reconfigure() method).

    Args:
        *streams: One or more text streams to reconfigure (e.g. sys.stdout, sys.stderr).
                  Streams that do not have reconfigure() are silently skipped.
    """
    for _stream in streams:
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Parse args and dispatch to subcommand handlers.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        Exit code (0 = success, non-zero = error).
    """
    # Windows consoles default to cp1252, which raises UnicodeEncodeError on
    # non-ASCII output (arrows, accented Spanish). Force UTF-8 where supported.
    _reconfigure_streams(sys.stdout, sys.stderr)

    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    # Resolve provider: explicit --provider wins, else auto-detect (env / keys).
    engine = _build_engine(
        provider=getattr(args, "provider", None) or _default_provider(),
        model=getattr(args, "model", ""),
        key_stdin=getattr(args, "key_stdin", False),
    )

    dispatch: dict[str, Any] = {
        "create": _cmd_create,
        "estimate": _cmd_estimate,
        "run": _cmd_run,
        "resume": _cmd_resume,
        "status": _cmd_status,
        "cancel": _cmd_cancel,
        "serve": _cmd_serve,
    }

    if args.command == "glossary":
        if not hasattr(args, "glossary_command") or not args.glossary_command:
            parser.parse_args(["glossary", "--help"])
            return 1
        if args.glossary_command == "show":
            return _cmd_glossary_show(args, engine)
        if args.glossary_command == "update":
            return _cmd_glossary_update(args, engine)
        return 1

    handler = dispatch.get(args.command)
    if handler is None:
        parser.print_help()
        return 1

    return handler(args, engine)


if __name__ == "__main__":
    sys.exit(main())
