"""Borgésica CLI — thin command-line interface for the translation engine.

This module is a testing aid and simple end-user entry point, not the
product UI. It wires concrete adapters into TranslatorEngine and exposes
subcommands for the full job lifecycle.

Usage:
    python -m borgesica <subcommand> [options]
    borgesica <subcommand> [options]          # after pip install

Subcommands:
    create  <source_file> --model <model> [--provider anthropic|deepseek|ollama]
             [--chunk-size N] [--budget USD] [--quality-mode fast|reflective]
             (source format auto-detected from .srt/.epub/.pdf extension)
    estimate <job_id>
    run     <job_id> [--out <path>] [--provider ...]
    resume  <job_id> [--out <path>] [--provider ...]
    status  <job_id>
    cancel  <job_id>
    glossary show   <job_id>
    glossary update <job_id> <term> <translation> [--lock]

Provider selection:
    Pass --provider on any command, or let it auto-detect: BORGESICA_PROVIDER env
    var if set, otherwise whichever key is present (anthropic → deepseek → ollama).

Environment (per provider; only the selected provider's key is required):
    ANTHROPIC_API_KEY  — for --provider anthropic
    DEEPSEEK_API_KEY   — for --provider deepseek
    OLLAMA_HOST        — for --provider ollama (optional; defaults to localhost:11434)
    BORGESICA_PROVIDER — optional; sets the default provider when --provider is omitted
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from borgesica.api import TranslatorEngine
from borgesica.domain.errors import BorgésicaError, JobNotFoundError, UnsupportedFormatError
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
    provider key is present (anthropic → deepseek → ollama) → 'anthropic' (which
    then errors clearly about its missing key).
    """
    explicit = os.environ.get("BORGESICA_PROVIDER")
    if explicit:
        return explicit.lower()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("DEEPSEEK_API_KEY"):
        return "deepseek"
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


def _build_provider(provider: str) -> Any:
    """Instantiate a TranslationProvider by name.

    Supported: 'anthropic' (ANTHROPIC_API_KEY), 'deepseek' (DEEPSEEK_API_KEY),
    'ollama' (local; OLLAMA_HOST optional, no key needed).

    Raises:
        SystemExit: if an unknown provider is given or a required key is missing.
    """
    name = provider.lower()
    if name == "anthropic":
        from borgesica.adapters.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(api_key=_require_env("ANTHROPIC_API_KEY", "anthropic"))
    if name == "deepseek":
        from borgesica.adapters.providers.openai_compatible_provider import (
            OpenAICompatibleProvider,
        )

        key = _require_env("DEEPSEEK_API_KEY", "deepseek")
        return OpenAICompatibleProvider.deepseek(api_key=key)
    if name == "ollama":
        from borgesica.adapters.providers.ollama_provider import OllamaProvider

        return OllamaProvider()  # base_url resolved from OLLAMA_HOST; no API key needed
    print(
        f"ERROR: unknown provider {provider!r} (choose: anthropic, deepseek, ollama).",
        file=sys.stderr,
    )
    sys.exit(1)


def _build_engine(
    *, provider: str = "anthropic", model: str = "", db_path: str = ""
) -> TranslatorEngine:
    """Construct a TranslatorEngine wired with real adapters for all formats.

    Args:
        provider: Provider name — 'anthropic' (default), 'deepseek', or 'ollama'.
        model:    Model string (persisted in JobConfig; not validated here).
        db_path:  SQLite checkpoint DB path. Defaults to ~/.borgesica/jobs.db.
                  Pass ":memory:" for ephemeral use.

    Returns:
        Fully wired TranslatorEngine (SRT/EPUB/PDF readers + writers).

    Raises:
        SystemExit: if the selected provider's API key env var is not set.
    """
    translation_provider = _build_provider(provider)

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
    )
    job = engine.create_job(args.source_file, config)
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


def _cmd_run(args: argparse.Namespace, engine: TranslatorEngine) -> int:
    try:
        out_path = _resolve_out(args, engine)
        print(f"Running job {args.job_id} → {out_path}")
        final_job = engine.run_job(args.job_id, out_path=out_path, on_progress=_print_progress)
        print(f"Done. Status={final_job.status}  cost=${final_job.cost_usd:.5f}")
        return 0
    except BorgésicaError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _cmd_resume(args: argparse.Namespace, engine: TranslatorEngine) -> int:
    try:
        out_path = _resolve_out(args, engine)
        print(f"Resuming job {args.job_id} → {out_path}")
        final_job = engine.resume_job(args.job_id, out_path=out_path, on_progress=_print_progress)
        print(f"Done. Status={final_job.status}  cost=${final_job.cost_usd:.5f}")
        return 0
    except BorgésicaError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _cmd_status(args: argparse.Namespace, engine: TranslatorEngine) -> int:
    try:
        job = engine.status(args.job_id)
        print(job.model_dump_json(indent=2))
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

    provider_choices = ["anthropic", "deepseek", "ollama"]
    provider_help = (
        "Provider: anthropic/deepseek/ollama. Default: auto-detect from "
        "BORGESICA_PROVIDER env or whichever API key is set."
    )

    def _add_provider(p: argparse.ArgumentParser) -> None:
        p.add_argument("--provider", choices=provider_choices, default=None, help=provider_help)

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
    )

    dispatch: dict[str, Any] = {
        "create": _cmd_create,
        "estimate": _cmd_estimate,
        "run": _cmd_run,
        "resume": _cmd_resume,
        "status": _cmd_status,
        "cancel": _cmd_cancel,
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
