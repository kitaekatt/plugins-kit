#!/usr/bin/env python
"""workflow-kit openrouter node-strategy runner.

Make ONE non-Claude chat-completion call for a workflow node and write the
reply text to --out. The node's ``--model`` is a model DECLARATION: one or more
llm-scripting-kit TRANSPORT entry ids (e.g. ``or-qwen``), as a comma list. It
is dispatched through llm-scripting-kit's one declaration API,
``llm_scripting_kit.declaration.run``: the first usable entry runs, a classified
halt moves to the next usable entry, and an id this node cannot dispatch (a
harness entry, or one that resolves to nothing) is skipped silently. When no
usable entry is left, the typed floor (``NoUsableRoutingTarget``) is reported
with every declared id and its disposition.

With no ``--model`` the node runs the configured default declaration
(llm-scripting-kit's ``default_endpoint``); ``--cheap`` selects each entry's
``defaultCheap`` model. A model alias (``qwen``) or raw slug
(``qwen/qwen3-32b``) is not an entry id: it resolves to no entry and reaches
the floor like any other unresolved id.

Run this with WORKFLOW-KIT's OWN venv python, which bootstrap provisions with:
  - `llm_scripting_kit` and `bootstrap_lib` -- shared libraries linked onto this
    venv via the bootstrap shared-libs .pth because workflow-kit declares both
    in `shared_lib_imports`. llm-scripting-kit is REQUIRED (an `install: auto`
    plugin edge in bootstrap.json); an absent copy and a too-old one are
    diagnosed apart.
  - the `openai` SDK -- a declared workflow-kit dependency, used lazily inside
    llm-scripting-kit's OpenRouterBackend.

    <workflow-kit-venv-python> scripts/openrouter_run.py \\
        [--model <entry>[,<entry>...]] [--cheap] --prompt-file <req.txt> --out <OUT> \\
        [--system <s>] [--status <STATUS>] \\
        [--events <EVENTS.jsonl> --run-id <runId> --unit-id <unitId>]

Contract: writes the reply to --out and exits 0 on success. Exit 2 is a
resolution failure (the floor, an invalid declaration, a missing or too-old
shared library); exit 1 is a call that ran and failed. The optional --status
JSON names the entry that ran or, on failure, the error kind, the halt kind
when one was classified, and the floor's dispositions. The seam classifies
transport, HTTP and halt failures; this script reports them.

Execution events: with --events (which requires --run-id and --unit-id), the
call records a ``plugins-kit.execution-event/v1`` JSONL stream at that path.
An ``Emitter`` from ``bootstrap_lib.execution_event``, bound to
``source.plugin`` ``workflow-kit`` and the given run and unit ids, is passed
as ``observer=`` to ``declaration.run``, which emits ``dispatch-selected``,
``call-started``, ``usage`` and ``result`` per attempt and one ``terminal``.
The file is truncated on each execution, so it records the node's LAST
execution, as --out does. Before any model call, the runner probes
``bootstrap_lib.execution_event`` (the v1 schema and the exact ``Emitter`` and
``JsonlSink`` calls) and ``run``'s ``observer`` keyword; an absent or too-old
library exits 2 and creates nothing. Only then is the events file's parent
directory created.
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
from pathlib import Path

#: The llm-scripting-kit release that shipped create_transport_backend, the
#: newest symbol this runner uses (with default_declaration and the
#: declaration module's run/NoUsableRoutingTarget).
_LLM_SCRIPTING_KIT_MIN = "0.46.0"

#: The llm-scripting-kit release that shipped ``run(..., observer=...)``, the
#: call --events makes.
_LLM_SCRIPTING_KIT_OBSERVER = "0.56.0"

#: The execution-event schema this runner writes. Its own literal, never read
#: from the (possibly stale) linked module.
_REQUIRED_SCHEMA = "plugins-kit.execution-event/v1"

#: ``source.plugin`` of every event this runner records.
_EVENT_PLUGIN = "workflow-kit"

_OBSERVER_KINDS = (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)


def _write_status(path, obj):
    if not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj), encoding="utf-8")


def _split(value):
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _execution_event_bootstrap():
    """workflow-kit's own constant for the bootstrap release --events needs."""
    try:
        from workflow_kit_lib.declarations import EXECUTION_EVENT_BOOTSTRAP  # noqa: PLC0415
    except ImportError:
        # workflow_kit_lib ships beside this script; use that copy when the
        # interpreter has none installed.
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from workflow_kit_lib.declarations import EXECUTION_EVENT_BOOTSTRAP  # noqa: PLC0415
    return EXECUTION_EVENT_BOOTSTRAP


def _probe_execution_event():
    """Return ``(module, None)`` when usable, else ``(None, message)``.

    Absent and too-old are different repairs (install vs. update), so they get
    different messages. The version named is workflow-kit's own constant,
    never a value read from the linked module.
    """
    try:
        import bootstrap_lib  # noqa: F401, PLC0415
    except ImportError:
        return None, (
            "--events needs bootstrap_lib.execution_event, but bootstrap_lib is not "
            "importable: the plugins-kit:bootstrap plugin has not provisioned "
            "workflow-kit here. Run `claude plugin install bootstrap@plugins-kit`, start "
            "a new session, and run this with workflow-kit's venv python."
        )
    try:
        from bootstrap_lib import execution_event  # noqa: PLC0415
    except ImportError:
        execution_event = None
    reason = None
    if execution_event is None:
        reason = "no bootstrap_lib.execution_event"
    elif _REQUIRED_SCHEMA not in (getattr(execution_event, "SUPPORTED_SCHEMAS", None) or ()):
        reason = f"schema {_REQUIRED_SCHEMA} is not supported"
    elif not callable(getattr(execution_event, "Emitter", None)) or not callable(
        getattr(execution_event, "JsonlSink", None)
    ):
        reason = "Emitter or JsonlSink is missing"
    else:
        try:
            inspect.signature(execution_event.Emitter).bind(
                _EVENT_PLUGIN, "r", unit_id="u", sinks=()
            )
            inspect.signature(execution_event.JsonlSink).bind("p", mode="truncate")
        except (TypeError, ValueError):
            reason = "Emitter(...) or JsonlSink(..., mode='truncate') does not bind"
    if reason is None:
        return execution_event, None
    return None, (
        f"the linked bootstrap_lib predates the execution-event call --events makes ({reason}); "
        f"this requires bootstrap >= {_execution_event_bootstrap()}. Run `claude plugin update "
        "bootstrap@plugins-kit` and restart so bootstrap re-links the newer shared lib onto "
        "workflow-kit's venv."
    )


def _probe_run_observer(run):
    """Return None when ``run`` accepts ``observer=`` as a keyword, else a message."""
    try:
        param = inspect.signature(run).parameters.get("observer")
    except (TypeError, ValueError):
        param = None
    if param is not None and param.kind in _OBSERVER_KINDS:
        return None
    return (
        "the linked llm_scripting_kit's declaration.run takes no `observer` keyword, which "
        f"--events passes; this requires llm-scripting-kit >= {_LLM_SCRIPTING_KIT_OBSERVER}. "
        "Run `claude plugin update llm-scripting-kit@plugins-kit` and restart so bootstrap "
        "re-links the newer shared lib onto workflow-kit's venv."
    )


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="workflow-kit: one non-Claude model call via llm-scripting-kit's declaration API."
    )
    ap.add_argument(
        "--model",
        default=None,
        help="A model declaration: one or more llm-scripting-kit transport entry ids, "
        "comma-separated (e.g. or-qwen,or-gpt-mini). If omitted, the configured default "
        "declaration is used.",
    )
    ap.add_argument(
        "--cheap",
        action="store_true",
        help="Select each entry's configured 'defaultCheap' model.",
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--prompt", help="user prompt text")
    g.add_argument("--prompt-file", help="path to a file holding the user prompt")
    ap.add_argument("--system", help="optional system message")
    ap.add_argument("--temperature", type=float)
    ap.add_argument("--max-tokens", type=int)
    ap.add_argument("--out", required=True, help="write the reply text here ($OUT)")
    ap.add_argument("--status", help="optional path for a small JSON status object ($STATUS)")
    ap.add_argument(
        "--events",
        help="record a plugins-kit.execution-event/v1 JSONL stream here (replaced on each "
        "run); requires --run-id and --unit-id",
    )
    ap.add_argument("--run-id", help="the workflow run id the events carry (with --events)")
    ap.add_argument("--unit-id", help="the node's unit id the events carry (with --events)")
    args = ap.parse_args(argv)
    if args.events and not (args.run_id and args.unit_id):
        ap.error("--events requires --run-id and --unit-id")

    # The package and the newest symbol are probed separately: a .pth links no
    # version, so this venv can resolve an llm-scripting-kit predating the
    # declaration API. An absent package and a too-old one are different
    # repairs (enable the plugin vs. update it), so they get different messages.
    try:
        import llm_scripting_kit  # noqa: F401
    except ImportError:
        print(
            "llm_scripting_kit not importable. Enable the llm-scripting-kit plugin and run this "
            "with workflow-kit's venv python (bootstrap links llm_scripting_kit onto it via "
            "the shared-libs .pth).",
            file=sys.stderr,
        )
        return 2
    try:
        from llm_scripting_kit import default_declaration
        from llm_scripting_kit.completion import BackendOptions, create_transport_backend
        from llm_scripting_kit.declaration import (
            DeclarationSupportError,
            NoUsableRoutingTarget,
            RUN_COMPLETED,
            RunRequest,
            run,
        )
    except ImportError:
        print(
            "the linked llm_scripting_kit predates the model-declaration API "
            "(create_transport_backend). This requires llm-scripting-kit >= "
            f"{_LLM_SCRIPTING_KIT_MIN}. Run `claude plugin update llm-scripting-kit@plugins-kit` "
            "and restart so bootstrap re-links the newer shared lib onto workflow-kit's venv.",
            file=sys.stderr,
        )
        return 2

    observer = None
    if args.events:
        # Every probe runs before any directory is created and before any
        # model call; a refusal leaves nothing on disk.
        execution_event, message = _probe_execution_event()
        if message is None:
            message = _probe_run_observer(run)
        if message is None:
            try:
                execution_event.Emitter(
                    _EVENT_PLUGIN, args.run_id, unit_id=args.unit_id, sinks=()
                )
            except ValueError as exc:  # EventError is a ValueError
                message = f"--run-id/--unit-id are not valid event identities: {exc}"
        if message is not None:
            print(message, file=sys.stderr)
            return 2
        events_path = Path(args.events)
        # A fresh run has no ./.workflow-kit/<runId>/ directory, and --out's
        # parent is created only after the call returns.
        events_path.parent.mkdir(parents=True, exist_ok=True)
        observer = execution_event.Emitter(
            _EVENT_PLUGIN,
            args.run_id,
            unit_id=args.unit_id,
            sinks=[execution_event.JsonlSink(events_path, mode="truncate")],
        )

    project_root = os.getcwd()
    names = _split(args.model)
    try:
        if not names:
            names = default_declaration(project_root=project_root)
    except (DeclarationSupportError, ValueError) as exc:  # DeclarationError is a ValueError
        print(str(exc), file=sys.stderr)
        _write_status(args.status, {"ok": False, "error": type(exc).__name__})
        return 2

    prompt = args.prompt if args.prompt is not None else Path(args.prompt_file).read_text(encoding="utf-8")
    backend_kwargs = {}
    if args.temperature is not None:
        backend_kwargs["temperature"] = args.temperature
    if args.max_tokens is not None:
        backend_kwargs["max_tokens"] = args.max_tokens
    request = RunRequest(system=args.system or "", prompt=prompt, options=BackendOptions(**backend_kwargs))

    def factory(name, project_root=None):
        return create_transport_backend(name, cheap=args.cheap, project_root=project_root)

    try:
        run_kwargs = {} if observer is None else {"observer": observer}
        result = run(
            names, request, project_root=project_root, backend_factory=factory,
            max_attempts=len(names), **run_kwargs,
        )
    except NoUsableRoutingTarget as exc:
        print(str(exc), file=sys.stderr)
        _write_status(args.status, {
            "ok": False,
            "error": "NoUsableRoutingTarget",
            "dispositions": exc.to_json()["dispositions"],
        })
        return 2
    except (DeclarationSupportError, ValueError) as exc:  # structurally invalid declaration
        print(str(exc), file=sys.stderr)
        _write_status(args.status, {"ok": False, "error": type(exc).__name__})
        return 2

    if result.status != RUN_COMPLETED:
        print(f"openrouter node failed on {result.entry}: {result.detail}", file=sys.stderr)
        status = {
            "ok": False,
            "error": result.status,
            "entry": result.entry,
            "detail": result.detail,
            "attempts": [attempt.to_json() for attempt in result.attempts],
        }
        halt = result.attempts[-1].halt if result.attempts else None
        if halt:
            status["halt"] = halt
        _write_status(args.status, status)
        return 1

    text = result.response.text
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    _write_status(args.status, {
        "ok": True,
        "entry": result.entry,
        "model": result.response.model,
        "bytes": len(text.encode("utf-8")),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
