"""``task-kit export``: the command line of the task export.

Contract: ``skills/task/references/task-export-protocol.md`` (normative). Every
outcome this module handles -- success, a usage error, an unknown id, an
unsupported protocol, a discovery failure, an unexpected exception -- writes
exactly one JSON document and a newline to stdout; stderr carries human
diagnostics only. A consumer therefore never sees "exit 2 with empty stdout"
from a task-kit that has this command, which is how it tells a task-kit too
old to have ``export`` from a failing one.

Imports nothing under ``task_system.dashboard``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Iterator, TextIO

from . import export

KINDS = ("protocols", "tasks", "item")
EXIT_OK = 0
EXIT_CODES = {
    "internal": 1,
    "discovery_failed": 1,
    "usage": 2,
    "unknown_checkout": 3,
    "unknown_task": 3,
    "unknown_item": 3,
    "protocol_unsupported": 4,
}
PLUGIN_MANIFEST = Path(__file__).resolve().parents[4] / ".claude-plugin" / "plugin.json"
UNKNOWN_VERSION = "unknown"


def task_kit_version(manifest: Path = PLUGIN_MANIFEST) -> str:
    """This task-kit's version, from its plugin manifest."""
    value = json.loads(manifest.read_text(encoding="utf-8"))["version"]
    if not isinstance(value, str):
        raise ValueError(f"{manifest}: version is not a string")
    return value


class _Parser(argparse.ArgumentParser):
    """argparse that reports a usage error as an ExportError instead of printing and exiting 2."""

    def error(self, message: str):  # type: ignore[override]
        raise export.ExportError("usage", message)


def _parser() -> _Parser:
    parser = _Parser(prog="task-kit export", add_help=False, allow_abbrev=False)
    parser.add_argument("kind", choices=KINDS)
    parser.add_argument("--format", required=True, choices=("json",))
    parser.add_argument("--protocol", type=int)
    parser.add_argument("--parent", action="append", default=[], type=Path)
    parser.add_argument("--checkout")
    parser.add_argument("--task")
    parser.add_argument("--item")
    return parser


def _require(args: argparse.Namespace, kind: str, required: tuple[str, ...], allowed: tuple[str, ...]) -> None:
    for name in required:
        if getattr(args, name) in (None, []):
            raise export.ExportError("usage", f"export {kind} requires --{name}")
    for name in ("protocol", "parent", "checkout", "task", "item"):
        if name not in required + allowed and getattr(args, name) not in (None, []):
            raise export.ExportError("usage", f"export {kind} takes no --{name}")


@contextlib.contextmanager
def _no_optional_locks() -> Iterator[None]:
    """Child git processes take no optional locks, so validation's ``git status`` writes no index refresh."""
    previous = os.environ.get("GIT_OPTIONAL_LOCKS")
    os.environ["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("GIT_OPTIONAL_LOCKS", None)
        else:
            os.environ["GIT_OPTIONAL_LOCKS"] = previous


def _payload(args: argparse.Namespace, **inject) -> dict:
    parents = tuple(args.parent) or None
    if args.kind == "protocols":
        _require(args, "protocols", (), ())
        return export.protocols_payload()
    if args.kind == "tasks":
        _require(args, "tasks", ("protocol",), ("parent",))
        return export.tasks_payload(parents, **inject)
    _require(args, "item", ("protocol", "checkout", "task", "item"), ("parent",))
    return export.item_payload(args.checkout, args.task, args.item, parents, config_path=inject.get("config_path"))


def document(argv: list[str], *, version: str | None = None, **inject) -> tuple[int, dict]:
    """The exit code and the one document ``task-kit export <argv>`` answers with.

    ``inject`` (``config_path``, ``resolve_machine``, ``now``) is for tests;
    the command line passes nothing.
    """
    protocol = max(export.PROTOCOLS)
    try:
        resolved_version = task_kit_version() if version is None else version
    except (OSError, ValueError, KeyError) as exc:
        return EXIT_CODES["internal"], _error_document(
            protocol, UNKNOWN_VERSION, "internal", f"cannot read task-kit's version from {PLUGIN_MANIFEST}: {exc}"
        )
    try:
        args = _parser().parse_args(argv)
        if args.protocol is not None:
            if args.protocol not in export.PROTOCOLS:
                raise export.ExportError(
                    "protocol_unsupported",
                    f"protocol {args.protocol} is not supported; this task-kit ({resolved_version}) serves "
                    f"protocols {list(export.PROTOCOLS)}",
                )
            protocol = args.protocol
        with _no_optional_locks():
            payload = _payload(args, **inject)
    except export.ExportError as exc:
        return EXIT_CODES[exc.code], _error_document(protocol, resolved_version, exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001 -- every outcome is a document; the traceback goes to stderr
        traceback.print_exc(file=sys.stderr)
        return EXIT_CODES["internal"], _error_document(
            protocol, resolved_version, "internal", f"{type(exc).__name__}: {exc}"
        )
    return EXIT_OK, {"protocol": protocol, "task_kit_version": resolved_version, "kind": args.kind, "payload": payload}


def _error_document(protocol: int, version: str, code: str, message: str) -> dict:
    return {"protocol": protocol, "task_kit_version": version, "error": {"code": code, "message": message}}


def serialize(doc: dict) -> str:
    """The exact stdout text of one document: compact, ASCII-only, sorted keys, one trailing newline."""
    return json.dumps(doc, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"


def run(argv: list[str], *, stdout: TextIO | None = None, stderr: TextIO | None = None, **inject) -> int:
    code, doc = document(argv, **inject)
    if "error" in doc:
        print(f"task-kit export: {doc['error']['code']}: {doc['error']['message']}", file=stderr or sys.stderr)
    out = stdout or sys.stdout
    out.write(serialize(doc))
    out.flush()
    return code


def main(argv: list[str] | None = None) -> int:
    return run(list(sys.argv[1:] if argv is None else argv))
