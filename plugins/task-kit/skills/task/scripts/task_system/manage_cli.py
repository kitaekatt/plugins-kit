"""One-document JSON CLI for task-kit manage. No service or LLM dependency."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import traceback
from typing import Any, TextIO

from . import authoring, export, export_cli, manage, mutation, oslock, sessions

EXITS = {"internal": 1, "publication_failed": 1, "usage": 2, "unknown_task": 3,
         "unknown_item": 3, "protocol_unsupported": 4, "conflict": 5,
         "refused": 6, "held": 6, "too_large": 6, "busy": 7}


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise manage.ManageError("usage", message)


def _parser() -> Parser:
    parser = Parser(prog="task-kit manage", add_help=False, allow_abbrev=False)
    parser.add_argument("kind", choices=("protocols", "input", "mutate"))
    parser.add_argument("--format", required=True, choices=("json",))
    parser.add_argument("--protocol", type=int)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--task")
    parser.add_argument("--item")
    return parser


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise manage.ManageError("usage", f"duplicate JSON key: {key}")
        result[key] = value
    return result


def document(argv: list[str], *, stdin: TextIO | None = None, version: str | None = None,
             **inject: Any) -> tuple[int, dict[str, Any]]:
    protocol = max(manage.PROTOCOLS)
    resolved_version = version or export_cli.UNKNOWN_VERSION
    try:
        if version is None:
            resolved_version = export_cli.task_kit_version()
        args = _parser().parse_args(argv)
        if args.protocol is not None:
            if args.protocol not in manage.PROTOCOLS:
                raise manage.ManageError("protocol_unsupported", f"supported protocols: {list(manage.PROTOCOLS)}")
            protocol = args.protocol
        if args.kind == "protocols":
            if any(getattr(args, field) is not None for field in ("protocol", "project", "task", "item")):
                raise manage.ManageError("usage", "protocols accepts only --format json")
            payload = manage.protocols()
        else:
            if args.protocol is None or args.project is None:
                raise manage.ManageError("usage", "input and mutate require --protocol and --project")
            if args.kind == "input":
                if args.task is None:
                    raise manage.ManageError("usage", "input requires --task")
                with export_cli._no_optional_locks():
                    payload = manage.input_payload(args.project, args.task, args.item,
                                                   **{key: value for key, value in inject.items() if key == "resolve_machine"})
            else:
                if args.task is not None or args.item is not None:
                    raise manage.ManageError("usage", "mutate takes task and item in the JSON request, not flags")
                text = (stdin or sys.stdin).read(manage.INPUT_LIMIT + 1)
                if len(text.encode("utf-8")) > manage.INPUT_LIMIT:
                    raise manage.ManageError("too_large", "mutation request exceeds the input limit")
                try:
                    request = json.loads(text, object_pairs_hook=_object,
                                         parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
                except (ValueError, RecursionError) as exc:
                    raise manage.ManageError("usage", f"invalid JSON request: {exc}") from exc
                payload = manage.mutate_payload(args.project, request, **inject)
    except mutation.MutationError as exc:
        code = "conflict" if isinstance(exc, mutation.StaleRevision) else "busy" if exc.code == "lock-timeout" else "refused"
        return EXITS[code], {"protocol": protocol, "task_kit_version": resolved_version,
                             "error": {"code": code, "message": str(exc), "written": list(exc.written)}}
    except (manage.ManageError, export.ExportError) as exc:
        return EXITS.get(exc.code, 1), {"protocol": protocol, "task_kit_version": resolved_version,
                                       "error": {"code": exc.code, "message": str(exc),
                                                 "written": list(getattr(exc, "written", ()))}}
    except (authoring.AuthoringRefused, sessions.SessionsError, oslock.LockTimeout) as exc:
        code = "busy" if isinstance(exc, oslock.LockTimeout) else "refused"
        return EXITS[code], {"protocol": protocol, "task_kit_version": resolved_version,
                             "error": {"code": code, "message": str(exc), "written": []}}
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        return 1, {"protocol": protocol, "task_kit_version": resolved_version,
                   "error": {"code": "internal", "message": f"{type(exc).__name__}: {exc}", "written": []}}
    return 0, {"protocol": protocol, "task_kit_version": resolved_version,
               "kind": f"manage-{args.kind}", "payload": payload}


def main(argv: list[str] | None = None) -> int:
    code, result = document(list(sys.argv[1:] if argv is None else argv))
    if "error" in result:
        print(f"task-kit manage: {result['error']['code']}: {result['error']['message']}", file=sys.stderr)
    sys.stdout.write(export_cli.serialize(result))
    sys.stdout.flush()
    return code
