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
        [--events <EVENTS.jsonl> --run-id <runId> --unit-id <unitId>] \\
        [--provides <NAME> --kind {schema,opaque-file} --verdict <VERDICT.json> \\
         [--schema <JSON> --schema-digest <HEX>]]

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

A node that also PROVIDES an artifact (--provides with --events) writes its
whole stream under ``plugins-kit.execution-event/v3`` instead (v3 accepts
every v1 event under the v1 rules) and records its judgment as one
``contract`` event (see ``check_artifact.py`` for the payload), which always
precedes the unit's ``terminal``: ``run`` is given a wrapper that forwards
every event except ``terminal``, which it holds until the verdict is known.
The probe then asks for v3 (absent, supports-v1-but-not-v3 and otherwise too
old get three messages), and the previous events file is removed together
with the previous verdict, before any probe. A node that provides nothing
keeps the v1 stream exactly as described above.

Typed artifacts: with --provides (which requires --kind and --verdict; --kind
schema also requires --schema and --schema-digest, which --kind opaque-file
refuses) the node provides a named artifact, and the runner records its
judgment in a ``workflow-kit.artifact-verdict/v1`` file at --verdict, the same
file ``check_artifact.py`` writes for a script node (that module owns the
writer).

- --kind schema: the runner builds llm-scripting-kit's
  ``OutputContract(id="workflow-kit.artifact.<NAME>", policy="validated-result",
  schema=<--schema>)``, refuses (exit 2, before any call) a schema outside the
  contract subset or a digest that differs from --schema-digest (the schema
  changed in transit), and sends it as ``BackendOptions(output_contract=...)``.
  The seam selects only entries that satisfy the policy, instructs the model,
  and validates the answer. On success --out receives the VALIDATED value as
  JSON (``json.dumps(structured, ensure_ascii=True, allow_nan=False)``), not
  the reply text, and the verdict is ``satisfied``. An answer that fails the
  contract (``schema-mismatch`` or ``unparseable``) is ``violated`` with its
  ``[json_pointer, keyword]`` errors; the node exits 1 and --out is not
  written. A completed call that carries no valid contract report is never
  written as validated: it is ``missing``, exit 1.
- --kind opaque-file: no contract is sent; --out is the reply text as without
  --provides, and a completed call is ``satisfied``.
- Every other outcome after the probes (a failed call, the floor, a default
  declaration that does not resolve, an unexpected exception) is ``missing``,
  with the exit code the same outcome has without --provides; an unexpected
  exception still propagates.

Lifecycle with --provides, for every execution whose arguments parse: the
previous verdict (and, with --events, the previous events file) is removed
first, before any probe (an absent file is fine; any other error exits 2
having done nothing else, creating no directory). The probes follow; with
--kind schema they add ``OutputContract`` (with ``schema_digest``),
``POLICY_VALIDATED_RESULT`` and ``BackendOptions(output_contract=)``, and an
absent and a too-old library get different messages. Everything after the
probes runs in one ``try``/``finally`` whose cleanup is three independent
layers, each attempted whatever the others did: (1) write the verdict
atomically (``missing`` when no judgment was reached); (2) with --events,
attempt exactly one ``contract`` event; (3) with --events, release the held
``terminal`` at most once. So a failure before ``run`` (the default
declaration, say) leaves a contract-only stream, and the routing floor, which
``run`` reports as ``terminal`` "unroutable" before it raises, leaves
``contract`` then ``terminal``. A cleanup failure is printed to stderr naming
its layer; with no failure in the body the first one makes the node exit 1,
and it never masks the body's own failure or exception. Without --provides,
no contract is sent and nothing here applies.
"""
from __future__ import annotations

import argparse
import importlib.util
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

#: Contract dispositions that make a provided artifact ``violated``.
_VIOLATIONS = frozenset({"schema-mismatch", "unparseable"})

_CHECKER_MODULE = "workflow_kit_check_artifact"


def _checker():
    """``check_artifact.py`` beside this script: the verdict writer, cleanup
    layers and output-contract probe both node runners share. Loaded by path,
    so no other module named ``check_artifact`` can shadow it."""
    module = sys.modules.get(_CHECKER_MODULE)
    if module is None:
        spec = importlib.util.spec_from_file_location(
            _CHECKER_MODULE, Path(__file__).resolve().parent / "check_artifact.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[_CHECKER_MODULE] = module
        spec.loader.exec_module(module)
    return module


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


class _TerminalLast:
    """The observer ``run`` gets on a provider node: every event but ``terminal``.

    ``run`` emits the unit's ``terminal`` before the runner knows the artifact's
    verdict, and the v3 stream rules refuse a ``contract`` after the unit's
    ``terminal``. So ``terminal`` is held here and forwarded by
    :meth:`release_once` after the ``contract`` event. ``seq`` is assigned when
    the real ``Emitter.emit`` runs, so the stream stays monotonic.
    """

    def __init__(self, emitter):
        self._emitter = emitter
        self._held = None

    def emit(self, event, **fields):
        if event != "terminal":
            return self._emitter.emit(event, **fields)
        if self._held is not None:
            raise RuntimeError("run emitted a second terminal event for one unit")
        self._held = fields
        return None

    def emit_contract(self, payload):
        """Record the unit's ``contract`` event on the underlying emitter."""
        return self._emitter.emit("contract", payload=payload)

    def release_once(self):
        """Forward the held ``terminal``, if any. The slot is cleared BEFORE
        forwarding, so a second call, or one after a forward that raised, is a
        no-op: the terminal is forwarded at most once."""
        held, self._held = self._held, None
        if held is not None:
            self._emitter.emit("terminal", **held)


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


def _probe_backend_options(backend_options, checker):
    """None when ``BackendOptions`` takes ``output_contract``, else the too-old message."""
    try:
        inspect.signature(backend_options).bind_partial(output_contract=None)
    except (TypeError, ValueError):
        return (
            "the linked llm_scripting_kit's completion.BackendOptions takes no "
            "`output_contract`, which --provides --kind schema sends; this requires "
            f"llm-scripting-kit >= {checker.output_contract_lsk()}. Run `claude plugin update "
            "llm-scripting-kit@plugins-kit` and restart so bootstrap re-links the newer shared "
            "lib onto workflow-kit's venv."
        )
    return None


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
    ap.add_argument(
        "--provides",
        metavar="NAME",
        help="the node provides this named artifact (its --out); requires --kind and --verdict",
    )
    ap.add_argument("--kind", choices=("schema", "opaque-file"),
                    help="the provided artifact's kind (with --provides)")
    ap.add_argument("--schema",
                    help="the artifact's JSON Schema as JSON text (with --kind schema)")
    ap.add_argument("--schema-digest",
                    help="sha256 of the schema's canonical JSON (with --kind schema)")
    ap.add_argument("--verdict",
                    help="write the workflow-kit.artifact-verdict/v1 file here (with --provides)")
    args = ap.parse_args(argv)
    if args.events and not (args.run_id and args.unit_id):
        ap.error("--events requires --run-id and --unit-id")
    if args.provides is None:
        if any(v is not None for v in (args.kind, args.schema, args.schema_digest, args.verdict)):
            ap.error("--kind, --schema, --schema-digest and --verdict require --provides")
    else:
        if not args.provides:
            ap.error("--provides needs a non-empty artifact name")
        if not (args.kind and args.verdict):
            ap.error("--provides requires --kind and --verdict")
        if args.kind == "schema" and not (args.schema and args.schema_digest):
            ap.error("--kind schema requires --schema and --schema-digest")
        if args.kind == "opaque-file" and (
            args.schema is not None or args.schema_digest is not None
        ):
            ap.error("--kind opaque-file takes no --schema or --schema-digest")

    checker = None
    # A provider node that records events writes a v3 stream holding its
    # contract event; any other node keeps the v1 stream unchanged.
    contract_events = args.provides is not None and bool(args.events)
    if args.provides is not None:
        # First action of a parsed execution: no refusal below may leave a
        # previous execution's verdict or events stream on disk.
        checker = _checker()
        message = checker.invalidate_verdict(args.verdict)
        if message is None and contract_events:
            message = checker.invalidate_events(args.events)
        if message is not None:
            print(message, file=sys.stderr)
            return 2

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

    contract = None
    if args.kind == "schema":
        api, message = checker.probe_output_contract(need_validate=False)
        if message is None:
            message = _probe_backend_options(BackendOptions, checker)
        if message is not None:
            print(message, file=sys.stderr)
            return 2
        try:
            contract, _schema = checker.build_contract(
                api, args.provides, args.schema, args.schema_digest
            )
        except checker.ContractRefusal as exc:
            print(str(exc), file=sys.stderr)
            return 2

    outcome = {"verdict": "missing", "data": None, "errors": ()}

    def event_payload():
        return checker.contract_payload(
            artifact=args.provides,
            kind=args.kind,
            verdict=outcome["verdict"],
            schema_digest=None if contract is None else contract.schema_digest,
            errors=outcome["errors"],
        )

    execution_event = None
    if args.events:
        # Every probe runs before any directory is created and before any
        # model call; a refusal leaves nothing on disk.
        if contract_events:
            execution_event, message = checker.probe_contract_events()
        else:
            execution_event, message = _probe_execution_event()
        if message is None:
            message = _probe_run_observer(run)
        if message is None and contract_events:
            message = checker.check_contract_event(
                execution_event, args.run_id, args.unit_id, event_payload()
            )
        elif message is None:
            try:
                execution_event.Emitter(
                    _EVENT_PLUGIN, args.run_id, unit_id=args.unit_id, sinks=()
                )
            except ValueError as exc:  # EventError is a ValueError
                message = f"--run-id/--unit-id are not valid event identities: {exc}"
        if message is not None:
            print(message, file=sys.stderr)
            return 2

    # One post-probe lifecycle. The body covers everything after the probes;
    # its cleanup layers run on every return and exception path. Without
    # --provides there are no layers, and the body is exactly the node's
    # behavior without --provides.
    stream = {"held": None}  # the _TerminalLast of a provider node with --events
    layers = []
    if checker is not None:
        def write_verdict_layer():
            checker.write_verdict(args.verdict, checker.verdict_record(
                artifact=args.provides,
                kind=args.kind,
                verdict=outcome["verdict"],
                path=args.out,
                data=outcome["data"],
                schema_digest=None if contract is None else contract.schema_digest,
                errors=outcome["errors"],
            ))

        layers.append(("verdict", write_verdict_layer))

    if contract_events:
        def contract_layer():
            held = stream["held"]
            if held is None:
                raise RuntimeError(f"the events stream at {args.events} was never opened")
            held.emit_contract(event_payload())

        def terminal_layer():
            if stream["held"] is not None:
                stream["held"].release_once()

        layers.append(("contract", contract_layer))
        layers.append(("terminal", terminal_layer))

    def body():
        observer = None
        if contract_events:
            stream["held"] = _TerminalLast(checker.open_contract_stream(
                execution_event, args.events, args.run_id, args.unit_id
            ))
            observer = stream["held"]
        elif args.events:
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
        if contract is not None:
            backend_kwargs["output_contract"] = contract
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
            report = getattr(result.response, "output_contract", None) if contract else None
            if report is not None and report.disposition in _VIOLATIONS:
                # An unparseable answer has no instance errors; record one.
                errors = tuple(report.errors) or checker.UNPARSEABLE_ERRORS
                outcome.update(verdict="violated", errors=errors)
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

        if contract is not None:
            # Only the seam's own report makes a completed answer validated; a
            # completion without one (a backend that ignored the contract) is
            # never written under the artifact's schema-typed name.
            report = getattr(result.response, "output_contract", None)
            if (
                report is None
                or report.disposition != "valid"
                or report.schema_digest != contract.schema_digest
            ):
                print(
                    f"openrouter node on {result.entry} completed without a validated result "
                    f"for artifact {args.provides!r}; --out is not written",
                    file=sys.stderr,
                )
                _write_status(args.status, {
                    "ok": False, "error": "output-contract", "entry": result.entry,
                })
                return 1
            text = json.dumps(result.response.structured, ensure_ascii=True, allow_nan=False)
        else:
            text = result.response.text
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        if checker is not None:
            # The verdict describes exactly the bytes now at --out.
            outcome.update(verdict="satisfied", data=out.read_bytes())
        _write_status(args.status, {
            "ok": True,
            "entry": result.entry,
            "model": result.response.model,
            "bytes": len(text.encode("utf-8")),
        })
        return 0

    rc = 1
    failures = []
    try:
        rc = body()
    finally:
        if layers:
            failures = checker.run_cleanup(layers)
            checker.report_cleanup_failures(failures)
    if failures and rc == 0:
        return 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
