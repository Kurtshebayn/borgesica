"""Borgésica CLI — thin command-line interface for the translation engine.

This module is a testing aid and simple end-user entry point, not the
product UI. It wires concrete adapters into TranslatorEngine and exposes
subcommands for the full job lifecycle.

Usage:
    python -m borgesica <subcommand> [options]
    borgesica <subcommand> [options]          # after pip install

Subcommands:
    create  <srt_file> --model <model> [--chunk-size N] [--budget USD]
             [--quality-mode fast|reflective]
    estimate <job_id>
    run     <job_id> --out <path>
    resume  <job_id> --out <path>
    status  <job_id>
    cancel  <job_id>
    glossary show   <job_id>
    glossary update <job_id> <term> <translation> [--lock]

Environment:
    ANTHROPIC_API_KEY  — required for real runs (not needed in tests via _build_engine mock)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from borgesica.api import TranslatorEngine
from borgesica.domain.errors import BorgésicaError, JobNotFoundError
from borgesica.domain.models import GlossaryEntry, JobConfig, Progress, SourceType

# ---------------------------------------------------------------------------
# Engine builder — the ONLY place concrete adapters are instantiated from CLI
# Separated so tests can mock it cleanly.
# ---------------------------------------------------------------------------


def _build_engine(*, model: str, db_path: str = "") -> TranslatorEngine:
    """Construct a TranslatorEngine wired with real adapters.

    Reads ANTHROPIC_API_KEY from the environment.

    Args:
        model:   Model string to pass to the provider (not validated here).
        db_path: Path to the SQLite checkpoint DB. Defaults to ~/.borgesica/jobs.db.
                 Pass ":memory:" for ephemeral use (testing / one-shot runs).

    Returns:
        Fully wired TranslatorEngine.

    Raises:
        SystemExit: if ANTHROPIC_API_KEY is not set.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    # Lazy imports — keep CLI startup fast and avoid dependency errors
    # for people who only use the test helpers.
    from borgesica.adapters.checkpoints.sqlite_checkpoint import SQLiteCheckpointStore
    from borgesica.adapters.providers.anthropic_provider import AnthropicProvider
    from borgesica.adapters.readers.srt_reader import SrtReader
    from borgesica.adapters.writers.srt_writer import SrtWriter
    from borgesica.domain.glossary import NullGlossaryExtractor

    if not db_path:
        db_dir = Path.home() / ".borgesica"
        db_dir.mkdir(parents=True, exist_ok=True)
        db_path = str(db_dir / "jobs.db")

    return TranslatorEngine(
        provider=AnthropicProvider(api_key=api_key),
        checkpoint=SQLiteCheckpointStore(db_path=db_path),
        readers={SourceType.SRT: SrtReader()},
        writers={SourceType.SRT: SrtWriter()},
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
    config = JobConfig(
        source_type=SourceType.SRT,
        model=args.model,
        chunk_size=getattr(args, "chunk_size", 25),
        budget_usd=getattr(args, "budget", None),
        quality_mode=getattr(args, "quality_mode", "fast"),  # type: ignore[arg-type]
    )
    job = engine.create_job(args.srt_file, config)
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


def _cmd_run(args: argparse.Namespace, engine: TranslatorEngine) -> int:
    try:
        out_path = args.out if hasattr(args, "out") and args.out else _default_out(args.job_id)
        print(f"Running job {args.job_id} → {out_path}")
        final_job = engine.run_job(args.job_id, out_path=out_path, on_progress=_print_progress)
        print(f"Done. Status={final_job.status}  cost=${final_job.cost_usd:.5f}")
        return 0
    except BorgésicaError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _cmd_resume(args: argparse.Namespace, engine: TranslatorEngine) -> int:
    try:
        out_path = args.out if hasattr(args, "out") and args.out else _default_out(args.job_id)
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


def _default_out(job_id: str) -> str:
    return str(Path.home() / ".borgesica" / f"{job_id}_translated.srt")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="borgesica",
        description="Borgésica — literary SRT translation engine",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # create
    p_create = sub.add_parser("create", help="Create a translation job from an SRT file")
    p_create.add_argument("srt_file", help="Path to the source .srt file")
    p_create.add_argument("--model", required=True, help="Model string (e.g. claude-haiku-4-5)")
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

    # run
    p_run = sub.add_parser("run", help="Run (translate) a job")
    p_run.add_argument("job_id", help="Job ID")
    p_run.add_argument("--out", default=None, help="Output file path")

    # resume
    p_resume = sub.add_parser("resume", help="Resume a paused or cancelled job")
    p_resume.add_argument("job_id", help="Job ID")
    p_resume.add_argument("--out", default=None, help="Output file path")

    # status
    p_status = sub.add_parser("status", help="Show current job status as JSON")
    p_status.add_argument("job_id", help="Job ID")

    # cancel
    p_cancel = sub.add_parser("cancel", help="Cancel a job (cooperative)")
    p_cancel.add_argument("job_id", help="Job ID")

    # glossary (sub-sub-commands)
    p_glossary = sub.add_parser("glossary", help="Inspect or edit the job glossary")
    gsub = p_glossary.add_subparsers(dest="glossary_command", metavar="<action>")

    p_gshow = gsub.add_parser("show", help="Print glossary as JSON")
    p_gshow.add_argument("job_id")

    p_gupdate = gsub.add_parser("update", help="Add or update a glossary entry")
    p_gupdate.add_argument("job_id")
    p_gupdate.add_argument("term")
    p_gupdate.add_argument("translation")
    p_gupdate.add_argument("--lock", action="store_true", default=False)

    return parser


# ---------------------------------------------------------------------------
# main() — entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Parse args and dispatch to subcommand handlers.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        Exit code (0 = success, non-zero = error).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    # For glossary commands, build engine lazily (tests mock _build_engine)
    engine = _build_engine(model=getattr(args, "model", ""))

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
