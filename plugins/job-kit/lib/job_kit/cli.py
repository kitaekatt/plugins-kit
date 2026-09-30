"""Command-line entry point for job-kit run, status, resume, resolve, events and gc."""

from __future__ import annotations

import argparse
import re
import json
import sys
import uuid
from pathlib import Path
from typing import Optional, Sequence

from .model import TERMINAL_STATES, JobState, RunSnapshot, validate_max_parallel
from .run import (
    DEFAULT_TIMEOUT_S,
    default_store_path,
    resume_run,
    run_job_file,
)
from .store import (
    InterruptExpiredError,
    JobStore,
    LedgerReader,
    ResolutionConflictError,
    ResolutionInputError,
)


EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_RUNNER_FAILURE = 3
EXIT_WAITING = 4

_EXIT_EPILOG = """Exit codes:
  0 -- every job accepted, or the verb succeeded (GC refusals are reported)
  1 -- the verb ran but a job was not accepted (rejected / failed / halted / unroutable /
       operator_rejected / expired), or `resolve` refused the resolution
  2 -- usage error (argparse exits with this code)
  3 -- the runner itself failed (unreadable jobs file, missing store, unknown run or
       interrupt, or unexpected exception), or `events` refused (a run without an
       event log, or an existing --out file)
  4 -- `run` or `resume` ended with no failure but at least one job waiting on an
       interrupt: healthy, not done; `resolve` it, then `resume`
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
        "--max-parallel",
        type=_max_parallel_argument,
        help="override the jobs file's max_parallel; the override is recorded in the ledger",
    )
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
    resume.add_argument(
        "--max-parallel",
        type=_max_parallel_argument,
        help="pool width for this pass only; the ledger's recorded value is not rewritten",
    )

    resolve = subcommands.add_parser(
        "resolve",
        help="resolve a waiting job's interrupt (answer or reject it)",
        description=(
            "Record the immutable resolution of one interrupt. An answer is "
            "validated against the request schema the interrupt declared. "
            "Replaying an equal resolution is a no-op that exits 0; a different "
            "one is refused. resolve never resumes the run: run "
            "`job-kit resume <run-id>` afterwards."
        ),
    )
    resolve.add_argument("run")
    resolve.add_argument("interrupt_id")
    decision = resolve.add_mutually_exclusive_group(required=True)
    decision.add_argument("--input", help="the answer, as JSON text")
    decision.add_argument(
        "--input-file", type=Path, help="read the answer as JSON from this file"
    )
    decision.add_argument(
        "--reject", action="store_true", help="reject the request instead of answering it"
    )
    resolve.add_argument("--reason", help="the operator's reason; only with --reject")
    resolve.add_argument("--store", type=Path)

    events = subcommands.add_parser(
        "events",
        help="export a run's execution events as JSONL",
        description=(
            "Write the run's execution events (plugins-kit.execution-event/v1, "
            "and /v2 for interrupt events) "
            "as one JSON object per line, in seq order: to stdout, or to a new "
            "file with --out. A run created before job-kit recorded events is "
            "refused, because its stream would be partial."
        ),
    )
    events.add_argument("run")
    events.add_argument("--store", type=Path)
    events.add_argument(
        "--out",
        type=Path,
        help="write the stream to this new file; an existing file is refused",
    )

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


def _max_parallel_argument(value: str) -> int:
    """Validate a caller-supplied worker-pool bound."""
    try:
        return validate_max_parallel(int(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


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
    """Map a run's jobs to an exit code: a failure dominates a wait.

    1 when any job is terminal and not accepted; otherwise 4 when any job is
    waiting on an interrupt; otherwise 0 when every job was accepted;
    otherwise 1.
    """
    states = [job.state for job in snapshot.jobs]
    if any(state in TERMINAL_STATES and state is not JobState.ACCEPTED for state in states):
        return EXIT_FAILURE
    if any(state is JobState.WAITING for state in states):
        return EXIT_WAITING
    if all(state is JobState.ACCEPTED for state in states):
        return EXIT_OK
    return EXIT_FAILURE


def _run(args: argparse.Namespace) -> int:
    """Handle the run subcommand."""
    if args.run_id is None:
        args.run_id = uuid.uuid4().hex
    snapshot = run_job_file(
        args.jobs,
        store_path=args.store,
        timeout_s=args.timeout,
        run_id=args.run_id,
        max_parallel=args.max_parallel,
    )
    store_path = (
        args.store.expanduser().resolve()
        if args.store is not None
        else default_store_path()
    )
    _emit(snapshot, store_path)
    return _exit_for_snapshot(snapshot)


def _emit_interrupted_run(args: argparse.Namespace) -> None:
    """Emit a durable snapshot when a run is interrupted after creation."""
    if args.command != "run" or args.run_id is None:
        return
    store_path = (
        args.store.expanduser().resolve()
        if args.store is not None
        else default_store_path()
    )
    try:
        snapshot = JobStore(store_path, create=False).snapshot(args.run_id)
    except Exception:
        return
    _emit(snapshot, store_path)


def _status(args: argparse.Namespace) -> int:
    """Handle the status subcommand through the read-only ledger reader.

    The reader never migrates a ledger or changes its journal mode, so
    `status` can be pointed at any ledger without upgrading it.
    """
    store_path = _store_path(args.store)
    snapshot = LedgerReader(store_path).snapshot(args.run)
    _emit(snapshot, store_path)
    return EXIT_OK


def _resume(args: argparse.Namespace) -> int:
    """Handle the resume subcommand."""
    store_path = _store_path(args.store)
    snapshot = resume_run(
        args.run,
        store_path,
        timeout_s=args.timeout,
        max_parallel=args.max_parallel,
    )
    _emit(snapshot, store_path)
    return _exit_for_snapshot(snapshot)


def _parse_input_text(text: str) -> object:
    """Parse resolution input JSON, refusing NaN, infinity and repeated keys."""

    def refuse_constant(name: str) -> object:
        raise ValueError(f"{name} is not JSON")

    def refuse_duplicates(pairs: list) -> dict:
        result: dict = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"the key {key!r} is repeated")
            result[key] = value
        return result

    return json.loads(
        text, parse_constant=refuse_constant, object_pairs_hook=refuse_duplicates
    )


def _refusal(args: argparse.Namespace, code: str, message: str, errors: Sequence = ()) -> int:
    """Print the JSON body of a refused resolution and return the refusal exit."""
    print(
        json.dumps(
            {
                "run": args.run,
                "interrupt_id": args.interrupt_id,
                "refused": code,
                "message": message,
                "errors": [
                    {"pointer": pointer, "keyword": keyword} for pointer, keyword in errors
                ],
            },
            sort_keys=True,
        )
    )
    return EXIT_FAILURE


def _resolve(args: argparse.Namespace) -> int:
    """Handle the resolve subcommand.

    The json_schema probe runs before the ledger is opened, so a machine that
    cannot validate an answer never migrates or writes the ledger.
    """
    from . import interrupts

    interrupts._schema_validator()
    store_path = _store_path(args.store)
    value: object = None
    if not args.reject:
        text = args.input
        if args.input_file is not None:
            text = args.input_file.expanduser().read_text(encoding="utf-8")
        try:
            value = _parse_input_text(text)
        except ValueError as exc:
            return _refusal(args, "invalid_json", f"the input is not valid JSON: {exc}")
    store = JobStore(store_path, create=False)
    decision = "reject" if args.reject else "answer"
    try:
        resolution = store.resolve_interrupt(
            args.run,
            args.interrupt_id,
            decision=decision,
            input=value,
            reason=args.reason,
        )
    except ResolutionInputError as exc:
        return _refusal(args, "schema" if exc.errors else "input", str(exc), exc.errors)
    except ResolutionConflictError as exc:
        return _refusal(args, "conflict", str(exc))
    except InterruptExpiredError as exc:
        return _refusal(args, "expired", str(exc))
    record = next(
        item
        for item in store.list_interrupts(args.run)
        if item.id == resolution.interrupt_id
    )
    job = store.get_job(args.run, record.job_id)
    print(
        json.dumps(
            {
                "run": args.run,
                "interrupt_id": resolution.interrupt_id,
                "job_id": record.job_id,
                "outcome": "replayed" if resolution.replayed else "recorded",
                "decision": decision,
                "state": job.state.value if job is not None else None,
                "store": str(store_path),
            },
            sort_keys=True,
        )
    )
    return EXIT_OK


def _events(args: argparse.Namespace) -> int:
    """Handle the events subcommand."""
    from . import events as event_support

    store_path = _store_path(args.store)
    stream = JobStore(store_path, create=False).list_events(args.run)
    if args.out is None:
        for event in stream:
            print(json.dumps(event, sort_keys=True, ensure_ascii=True, separators=(",", ":")))
        return EXIT_OK
    out_path = args.out.expanduser().resolve()
    try:
        sink = event_support.jsonl_sink(out_path)
    except FileExistsError:
        print(
            f"job-kit: --out file already exists: {out_path}; events are only "
            "written to a new file",
            file=sys.stderr,
        )
        return EXIT_RUNNER_FAILURE
    for event in stream:
        sink.write(event)
    print(
        json.dumps(
            {"events": len(stream), "out": str(out_path), "run": args.run, "store": str(store_path)},
            sort_keys=True,
        )
    )
    return EXIT_OK


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
    is rejected, failed, halted, unroutable, operator-rejected or expired (or
    `resolve` refuses), 3 when the runner itself fails, and 4 when `run` or
    `resume` leaves a job waiting on an interrupt and none failed. Argparse
    exits with 2 for usage errors.
    """
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "resolve" and args.reason is not None and not args.reject:
        parser.error("resolve: --reason is only valid with --reject")
    try:
        if args.command == "run":
            return _run(args)
        if args.command == "status":
            return _status(args)
        if args.command == "resume":
            return _resume(args)
        if args.command == "resolve":
            return _resolve(args)
        if args.command == "events":
            return _events(args)
        if args.command == "gc":
            return _gc(args)
    except KeyboardInterrupt:
        _emit_interrupted_run(args)
        raise
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
    "EXIT_WAITING",
    "main",
]
