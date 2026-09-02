"""Command-line entry point for job-kit run, status, resume and gc."""

from __future__ import annotations

import argparse
import re
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from .model import JobState, RunSnapshot
from .run import (
    DEFAULT_TIMEOUT_S,
    default_store_path,
    resume_run,
    run_job_file,
)
from .store import JobStore


EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_RUNNER_FAILURE = 3

_EXIT_EPILOG = """Exit codes:
  0 -- every job accepted, or the verb succeeded (GC refusals are reported)
  1 -- the verb ran but a job was not accepted (rejected / failed / halted / unroutable)
  2 -- usage error (argparse exits with this code)
  3 -- the runner itself failed (unreadable jobs file, missing store, or unexpected exception)
"""


def _parser() -> argparse.ArgumentParser:
    """Build the job-kit argument parser."""
    parser = argparse.ArgumentParser(
        prog="job-kit",
        epilog=_EXIT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    run = subcommands.add_parser("run", help="run the jobs in a YAML file")
    run.add_argument("jobs", type=Path)
    run.add_argument("--store", type=Path)
    run.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    run.add_argument(
        "--run-id",
        type=_run_id_argument,
        help="preassign the run id (letters, digits, . _ -) so a caller can resume it later",
    )

    status = subcommands.add_parser("status", help="show a durable run")
    status.add_argument("run")
    status.add_argument("--store", type=Path)

    resume = subcommands.add_parser("resume", help="resume non-terminal jobs")
    resume.add_argument("run")
    resume.add_argument("--store", type=Path)
    resume.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)

    gc = subcommands.add_parser("gc", help="reclaim eligible attempt worktrees")
    gc.add_argument("run", nargs="?")
    gc.add_argument("--store", type=Path)
    gc.add_argument("--accepted-only", action="store_true")
    gc.add_argument("--force", action="store_true")
    return parser


_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def _run_id_argument(value: str) -> str:
    """Validate a caller-preassigned run id."""
    if not _RUN_ID_PATTERN.match(value):
        raise argparse.ArgumentTypeError(
            "run id must be non-empty and use only letters, digits, '.', '_' or '-'"
        )
    return value


def _store_path(explicit: Optional[Path]) -> Path:
    """Resolve an explicit store or the default project store."""
    if explicit is not None:
        return explicit.expanduser().resolve()
    return default_store_path()


def _emit(snapshot: RunSnapshot, store_path: Path) -> None:
    """Write one JSON status payload."""
    payload = snapshot.to_mapping()
    payload["store"] = str(store_path)
    print(json.dumps(payload, sort_keys=True))


def _exit_for_snapshot(snapshot: RunSnapshot) -> int:
    """Return success only when every declared job was accepted."""
    return (
        EXIT_OK
        if all(job.state is JobState.ACCEPTED for job in snapshot.jobs)
        else EXIT_FAILURE
    )


def _run(args: argparse.Namespace) -> int:
    """Handle the run subcommand."""
    snapshot = run_job_file(
        args.jobs,
        store_path=args.store,
        timeout_s=args.timeout,
        run_id=args.run_id,
    )
    store_path = (
        args.store.expanduser().resolve()
        if args.store is not None
        else default_store_path()
    )
    _emit(snapshot, store_path)
    return _exit_for_snapshot(snapshot)


def _status(args: argparse.Namespace) -> int:
    """Handle the status subcommand."""
    store_path = _store_path(args.store)
    snapshot = JobStore(store_path, create=False).snapshot(args.run)
    _emit(snapshot, store_path)
    return EXIT_OK


def _resume(args: argparse.Namespace) -> int:
    """Handle the resume subcommand."""
    store_path = _store_path(args.store)
    snapshot = resume_run(args.run, store_path, timeout_s=args.timeout)
    _emit(snapshot, store_path)
    return _exit_for_snapshot(snapshot)


def _gc(args: argparse.Namespace) -> int:
    """Handle the conservative workspace garbage collector."""
    from .workspace import gc_workspaces

    store_path = _store_path(args.store)
    report = gc_workspaces(
        JobStore(store_path, create=False),
        args.run,
        accepted_only=args.accepted_only,
        force=args.force,
    )
    payload = report.to_mapping()
    payload["store"] = str(store_path)
    print(json.dumps(payload, sort_keys=True))
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the CLI and return its documented exit status.

    Returns 0 when every job is accepted or a verb succeeds, 1 when a run job
    is rejected, failed, halted, or unroutable, and 3 when the runner itself
    fails. Argparse exits with 2 for usage errors.
    """
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return _run(args)
        if args.command == "status":
            return _status(args)
        if args.command == "resume":
            return _resume(args)
        if args.command == "gc":
            return _gc(args)
    except Exception as exc:
        print(f"job-kit: {exc}", file=sys.stderr)
        return EXIT_RUNNER_FAILURE
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "EXIT_OK",
    "EXIT_FAILURE",
    "EXIT_USAGE",
    "EXIT_RUNNER_FAILURE",
    "main",
]
