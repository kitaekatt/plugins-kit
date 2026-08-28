"""Harness-compatible CLI rendering for declarative resources."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence, TextIO

from .model import Declaration, Operation, Resources, Status
from .runner import run


def run_cli(
    resources: Declaration | Resources,
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Execute an explicit operation and return a process exit code.

    Project ``bootstrap.py`` files can be only ``raise SystemExit(run_cli([...]))``.
    """
    parser = argparse.ArgumentParser(description="Inspect or converge managed state")
    parser.add_argument("operation", choices=[item.value for item in Operation])
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    output = stdout if stdout is not None else sys.stdout
    errors = stderr if stderr is not None else sys.stderr

    report = run(resources, args.operation)
    if args.json_output:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True), file=output)
    else:
        for result in report.results:
            stream = errors if result.status in {Status.BLOCKED, Status.FAILED} else output
            print(f"{result.status.value}: {result.name}: {result.detail}", file=stream)
            if result.backup is not None:
                print(f"  backup: {result.backup}", file=stream)
            if result.rollback is not None:
                print(f"  rollback: {result.rollback}", file=stream)
    return 0 if report.ok else 1
