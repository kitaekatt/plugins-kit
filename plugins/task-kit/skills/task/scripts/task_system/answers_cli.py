"""``task-kit answers``: the command line of the task answers engine.

Contract: ``skills/task/references/task-export-protocol.md``, section "Answers commands" (normative). The
stdout, stderr, exit-code and error-code rules are the export's (``export_cli``): every outcome this module
handles writes exactly one JSON document and a newline to stdout, and stderr carries human diagnostics only.

Verbs (``--format json --protocol N`` on each)::

    task-kit answers refresh [--parent DIR]... [--allow CLASSES] [--batch N] [--stop-file PATH] [--deadline-s N]
        kind "answers-pass": one engine pass per task repository (``answer_engine.run_pass``)
    task-kit answers status [--parent DIR]...
        kind "answers-status": coverage and pending work; writes nothing, takes no lock
    task-kit answers prune [--parent DIR]...
        kind "answers-prune": delete this machine's answer files of questions the catalog no longer holds

``--allow`` names the cost classes the caller allows (``free``, ``usage``, ``spend``); without it the classes
are llm-kit's per-machine consent, read once, on the first pass that needs a model.
"""

from __future__ import annotations

import argparse
import datetime
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, TextIO

from . import (
    answer_chain, answer_engine, answer_scripts, answers_config, datadir, export, export_cli, machine as machine_mod,
    questions, sessions_config,
)
from .answers_types import Catalog
from .validate import validate_ref

VERBS = {"refresh": "answers-pass", "status": "answers-status", "prune": "answers-prune"}
EXIT_CODES = {**export_cli.EXIT_CODES, "config_invalid": 1}
CLASSIFY_WORKERS = 8
_PER_VERB = {
    "refresh": ("parent", "allow", "batch", "stop_file", "deadline_s"),
    "status": ("parent",),
    "prune": ("parent",),
}
_OPTIONS = ("parent", "allow", "batch", "stop_file", "deadline_s")


def _parser() -> export_cli._Parser:
    parser = export_cli._Parser(prog="task-kit answers", add_help=False, allow_abbrev=False)
    parser.add_argument("verb", choices=tuple(VERBS))
    parser.add_argument("--format", required=True, choices=("json",))
    parser.add_argument("--protocol", type=int)
    parser.add_argument("--parent", action="append", default=[], type=Path)
    parser.add_argument("--allow")
    parser.add_argument("--batch", type=int)
    parser.add_argument("--stop-file", dest="stop_file", type=Path)
    parser.add_argument("--deadline-s", dest="deadline_s", type=float)
    return parser


def _check_flags(args: argparse.Namespace) -> None:
    if args.protocol is None:
        raise export.ExportError("usage", f"answers {args.verb} requires --protocol")
    allowed = _PER_VERB[args.verb]
    for name in _OPTIONS:
        if name not in allowed and getattr(args, name) not in (None, []):
            raise export.ExportError("usage", f"answers {args.verb} takes no --{name.replace('_', '-')}")
    if args.batch is not None and args.batch < 1:
        raise export.ExportError("usage", "--batch must be at least 1")
    if args.deadline_s is not None and args.deadline_s < 0:
        raise export.ExportError("usage", "--deadline-s must not be negative")


# -- the world ----------------------------------------------------------------------------------


def _catalog(inject: dict) -> Catalog:
    if inject.get("catalog") is not None:
        return inject["catalog"]
    try:
        return questions.load_catalog(questions.default_shipped_dir(), questions.default_fleet_dir())
    except questions.QuestionError as exc:
        raise export.ExportError("config_invalid", f"the question catalog cannot be loaded: {exc}") from exc


def _config(inject: dict) -> answers_config.AnswersConfig:
    if inject.get("config") is not None:
        return inject["config"]
    try:
        return answers_config.load()
    except answers_config.AnswersConfigError as exc:
        raise export.ExportError("config_invalid", str(exc)) from exc


def _machine(inject: dict):
    resolver = inject.get("resolve_machine")
    try:
        if resolver is not None:
            return resolver()
        return export.current_machine(inject.get("config_path"))
    except (sessions_config.SessionsConfigError, machine_mod.MachineError) as exc:
        raise export.ExportError("config_invalid", f"this machine cannot be identified: {exc}") from exc


def _repositories(
    parents: tuple[Path, ...] | None, inject: dict, errors: list[dict]
) -> dict[Path, list[answer_engine.TaskTarget]]:
    """The task folders export discovery finds, grouped by the repository that holds them."""
    owners, _bases = export._owners(export.resolve_parents(parents, inject.get("config_path")), errors)  # noqa: SLF001

    def classify(owner) -> str:
        return validate_ref(owner.path, owner.project.root).classification

    grouped: dict[Path, list[answer_engine.TaskTarget]] = {}
    if not owners:
        return grouped
    with ThreadPoolExecutor(max_workers=CLASSIFY_WORKERS) as pool:
        futures = [(owner, pool.submit(classify, owner)) for owner in owners.values()]
        for owner, future in futures:
            try:
                top = answer_scripts.repo_top_of(owner.folder)
            except answer_scripts.AnswerScriptError as exc:
                errors.append(export._error("answers", str(exc), owner.project.root))  # noqa: SLF001
                continue
            if top is None:
                errors.append(export._error(  # noqa: SLF001
                    "answers", f"{owner.folder} is not inside a git work tree, so it has no answers", owner.project.root
                ))
                continue
            grouped.setdefault(top.resolve(), []).append(answer_engine.TaskTarget(owner.folder, future.result()))
    return {top: sorted(targets, key=lambda t: str(t.folder)) for top, targets in sorted(grouped.items())}


# -- verbs --------------------------------------------------------------------------------------


def _allow_payload(lazy: answer_engine.LazyAllow) -> dict | None:
    resolved = lazy.resolved
    if resolved is None:
        return None
    return {"classes": sorted(resolved.classes), "source": resolved.source, "note": resolved.note}


def _refresh(args: argparse.Namespace, inject: dict) -> dict:
    parents = tuple(args.parent) or None
    allow_names = None
    if args.allow is not None:
        try:
            allow_names = answer_chain.parse_allow(args.allow)
        except ValueError as exc:
            raise export.ExportError("usage", str(exc)) from exc
    catalog, config, machine = _catalog(inject), _config(inject), _machine(inject)
    errors: list[dict] = []
    repositories = _repositories(parents, inject, errors)
    clock: Callable[[], float] = inject.get("clock", time.monotonic)
    deadline = None if args.deadline_s is None else clock() + args.deadline_s
    budget = answer_engine.Budget(
        batch_size=args.batch if args.batch is not None else answer_engine.DEFAULT_BATCH_SIZE, deadline=deadline,
        **inject.get("budget", {}),
    )
    stop_file = args.stop_file
    should_stop = (lambda: stop_file.exists()) if stop_file is not None else (lambda: False)
    lazy = answer_engine.LazyAllow(allow_names)
    reports: list[dict] = []
    stopped: str | None = None
    for top, targets in repositories.items():
        report = answer_engine.run_pass(
            top, machine, config, budget=budget, should_stop=should_stop, targets=targets, catalog=catalog,
            allow=lazy, run=inject.get("run"), data_dir=inject.get("data_dir"), clock=clock,
            now=inject.get("local_now", datetime.datetime.now), publish=inject.get("publish", True),
        )
        reports.append(report.as_dict())
        if report.stop is not None:
            stopped = report.stop
            break
    return {"allow": _allow_payload(lazy), "stopped": stopped, "repositories": reports, "errors": errors}


def _model_answering() -> dict:
    try:
        answer_chain.probe_llm_kit()
    except answer_chain.ModelAnsweringUnavailable as exc:
        return {"available": False, "state": exc.state, "message": str(exc)}
    return {"available": True, "state": "ok", "message": None}


def _status(args: argparse.Namespace, inject: dict) -> dict:
    catalog, machine = _catalog(inject), _machine(inject)
    errors: list[dict] = []
    repositories = _repositories(tuple(args.parent) or None, inject, errors)
    reports = [
        answer_engine.status(
            top, machine, targets=targets, catalog=catalog,
            now=inject.get("local_now", datetime.datetime.now),
        )
        for top, targets in repositories.items()
    ]
    tasks = [task for report in reports for task in report.tasks]
    covered = [task.coverage for task in tasks if task.coverage is not None]
    return {
        "catalog_version": catalog.version,
        "questions": list(catalog.ids),
        "model_answering": _model_answering(),
        "totals": {
            "tasks": len(tasks),
            "applicable": sum(c.applicable for c in covered),
            "current": sum(c.current for c in covered),
            "pending_tasks": sum(1 for task in tasks if task.pending),
            "pending_questions": sum(len(task.pending) for task in tasks),
            "unpublished": sum(len(report.unpublished) for report in reports),
        },
        "repositories": [report.as_dict() for report in reports],
        "errors": errors,
    }


def _prune(args: argparse.Namespace, inject: dict) -> dict:
    catalog, machine = _catalog(inject), _machine(inject)
    errors: list[dict] = []
    repositories = _repositories(tuple(args.parent) or None, inject, errors)
    reports = [
        answer_engine.prune(
            top, machine, catalog, targets=targets, data_dir=inject.get("data_dir"),
            publish=inject.get("publish", True),
        ).as_dict()
        for top, targets in repositories.items()
    ]
    return {"repositories": reports, "errors": errors}


def document(argv: list[str], *, version: str | None = None, **inject) -> tuple[int, dict]:
    """The exit code and the one document ``task-kit answers <argv>`` answers with.

    ``inject`` (``config_path``, ``resolve_machine``, ``catalog``, ``config``, ``run``, ``data_dir``,
    ``local_now``, ``clock``, ``budget``, ``publish``) is for tests; the command line passes nothing.
    """
    protocol = max(export.PROTOCOLS)
    try:
        resolved_version = export_cli.task_kit_version() if version is None else version
    except (OSError, ValueError, KeyError) as exc:
        return EXIT_CODES["internal"], export_cli._error_document(  # noqa: SLF001
            protocol, export_cli.UNKNOWN_VERSION, "internal",
            f"cannot read task-kit's version from {export_cli.PLUGIN_MANIFEST}: {exc}",
        )
    kind = None
    try:
        args = _parser().parse_args(argv)
        kind = VERBS[args.verb]
        _check_flags(args)
        if args.protocol not in export.PROTOCOLS:
            raise export.ExportError(
                "protocol_unsupported",
                f"protocol {args.protocol} is not supported; this task-kit ({resolved_version}) serves "
                f"protocols {list(export.PROTOCOLS)}",
            )
        protocol = args.protocol
        if args.verb == "status":
            with export_cli._no_optional_locks():  # noqa: SLF001
                payload = _status(args, inject)
        elif args.verb == "refresh":
            payload = _refresh(args, inject)
        else:
            payload = _prune(args, inject)
    except export.ExportError as exc:
        return EXIT_CODES[exc.code], export_cli._error_document(protocol, resolved_version, exc.code, exc.message)  # noqa: SLF001
    except datadir.DataDirError as exc:
        return EXIT_CODES["config_invalid"], export_cli._error_document(  # noqa: SLF001
            protocol, resolved_version, "config_invalid", str(exc)
        )
    except Exception as exc:  # noqa: BLE001 -- every outcome is a document; the traceback goes to stderr
        traceback.print_exc(file=sys.stderr)
        return EXIT_CODES["internal"], export_cli._error_document(  # noqa: SLF001
            protocol, resolved_version, "internal", f"{type(exc).__name__}: {exc}"
        )
    return export_cli.EXIT_OK, {
        "protocol": protocol, "task_kit_version": resolved_version, "kind": kind, "payload": payload,
    }


def _diagnostics(doc: dict) -> list[str]:
    """Human lines for stderr: an error, a consent note, a refusal to answer with a model."""
    if "error" in doc:
        return [f"task-kit answers: {doc['error']['code']}: {doc['error']['message']}"]
    payload = doc.get("payload", {})
    lines = []
    allow = payload.get("allow")
    if allow and allow.get("note"):
        lines.append(f"task-kit answers: {allow['note']}")
    for repo in payload.get("repositories", []):
        refusal = repo.get("refusal")
        if refusal:
            lines.append(f"task-kit answers: model answering unavailable ({refusal['state']}): {refusal['message']}")
    return lines


def run(argv: list[str], *, stdout: TextIO | None = None, stderr: TextIO | None = None, **inject) -> int:
    code, doc = document(argv, **inject)
    for line in _diagnostics(doc):
        print(line, file=stderr or sys.stderr)
    out = stdout or sys.stdout
    out.write(export_cli.serialize(doc))
    out.flush()
    return code


def main(argv: list[str] | None = None) -> int:
    return run(list(sys.argv[1:] if argv is None else argv))
