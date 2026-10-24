"""``llm-kit completion``: one configured completion, the request schema, JSON repair.

Carries the legacy ``complete``, ``request-schema`` and ``repair-json`` verbs.
Exit codes and the JSON envelope (``protocol`` 1) are the legacy contract:
0 completed, 1 failed, 3 a classified halt, 4 an unreadable request.

``complete --request-file`` also speaks ``protocol`` 2
(:mod:`llm_kit.completion.invoke_protocol`): the request names a row or selection
of the project model file and the result carries requested and served model,
effort, cost class, usage and, for a ``workspace`` call, the files written. The
exit codes are the same table (4 = refused before any spawn, on stderr).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional, Sequence

from llm_kit.cli._common import (
    EXIT_FAILURE,
    EXIT_HALT,
    EXIT_OK,
    EXIT_PROTOCOL,
    EXIT_USAGE,
    _add_models_arg,
    _add_project_arg,
    _add_requirements_arg,
    _backend_factory,
    _declared_models,
    _first_usable,
    _json,
    _load_requirements,
    guarded,
)
from llm_kit.cli._compose import create_backend
from llm_kit.completion.contract import OutputContractViolation
from llm_kit.completion.halt import AgentTimeoutError, halt_payload
from llm_kit.completion import invoke_protocol
from llm_kit.completion.request_protocol import (
    PROTOCOL_VERSION,
    ProtocolError,
    describe_request_schema,
    parse_request,
    protocol_error_envelope,
)
from llm_kit.completion.results import (
    derive_dropped_params,
    derive_forwarded_params,
    utc_now_iso,
)
from llm_kit.completion.types import (
    ERROR,
    TIMEOUT,
    BackendOptions,
    LLMResponse,
    ResponseError,
)
from llm_kit.dispatch.declaration import (
    RUN_ATTEMPT_LIMIT,
    RUN_COMPLETED,
    NoUsableRoutingTarget,
    RunRequest,
)
from llm_kit.dispatch.declaration import run as declaration_run
from llm_kit.dispatch import invoke as dispatch_invoke
from llm_kit.registry.model_endpoints import EndpointRegistryError
from llm_kit.registry.models import EndpointResolveError, ModelResolveError
from llm_kit.schema.json_repair import STATUS_REPAIRED, repair_json_structure
from llm_kit.schema.json_schema import check_schema


def _read_text(path: Optional[Path], inline: Optional[str], *, stdin_fallback: bool = False) -> str:
    if path is not None:
        return path.read_text(encoding="utf-8")
    if inline is not None:
        return inline
    return sys.stdin.read() if stdin_fallback else ""


#: `complete` flags that describe the call itself. Naming one alongside
#: --request-file is refused rather than merged: a precedence rule is invisible
#: at the call site, so a flag silently overriding a request field would be a
#: lie of exactly the kind this contract exists to remove. --format and
#: --project-root are absent because they describe the CLI's own behaviour
#: rather than the request, so they compose with either surface.
_COMPLETE_CALL_FLAGS = (
    "models",
    "requirements",
    "max_attempts",
    "endpoint",
    "model",
    "cheap",
    "system",
    "system_file",
    "prompt",
    "prompt_file",
    "max_tokens",
    "temperature",
    "timeout",
    "max_retries",
    "effort",
    "cwd",
)


#: The flag surface's own defaults, applied in _request_from_flags rather than by
#: argparse. Keeping them here makes "unset" observable, which is what the
#: conflict check above needs. ``None`` means no temperature field is sent.
_FLAG_DEFAULTS = {"max_tokens": None, "temperature": None, "system": "", "cheap": False}


def _request_from_flags(args: argparse.Namespace) -> "tuple[str, str, Any, BackendOptions]":
    """The original flag surface, unchanged.

    Kept whole rather than re-expressed through the protocol parser: every
    existing caller is a shell script, and routing them through a new code path
    to save duplication would risk their behaviour for no benefit they asked
    for.
    """
    def flag(name: str) -> Any:
        value = getattr(args, name)
        return _FLAG_DEFAULTS[name] if value is None else value

    system = _read_text(args.system_file, flag("system"))
    user = _read_text(args.prompt_file, args.prompt, stdin_fallback=True)
    declared = _declared_models(args)
    requirements = _load_requirements(args.requirements)
    if requirements is not None and not declared:
        raise ValueError("--requirements needs --models: it filters a declaration")
    selection = create_backend(
        _first_usable(declared, args.project_root) if declared and requirements is None
        else _first_usable(declared, args.project_root, requirements) if declared else None,
        model=args.model,
        cheap=flag("cheap"),
        project_root=args.project_root,
    )
    options = BackendOptions(
        max_tokens=flag("max_tokens"),
        temperature=flag("temperature"),
        timeout_s=args.timeout,
        max_retries=args.max_retries,
        effort=args.effort or selection.effort,
        cwd=args.cwd,
        log_prefix=f"[{selection.endpoint}]",
    )
    return system, user, selection, options


def _read_request_payload(args: argparse.Namespace) -> Any:
    """The decoded ``--request-file`` JSON; flags that describe the call are refused beside it."""
    # `is not None` rather than a falsy test: `0 in (None, False, "")` is True
    # in Python, so a falsy comparison would let `--timeout 0` through as though
    # it had never been named.
    named = [
        flag for flag in _COMPLETE_CALL_FLAGS if getattr(args, flag, None) is not None
    ]
    if named:
        raise ProtocolError(
            "--request-file cannot be combined with "
            + ", ".join("--" + flag.replace("_", "-") for flag in sorted(named))
            + "; the request carries those values itself"
        )
    raw = sys.stdin.read() if args.request_file == "-" else Path(
        args.request_file
    ).read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"request is not valid JSON: {exc}") from exc


def _is_protocol_2(payload: Any) -> bool:
    version = payload.get("protocol") if isinstance(payload, dict) else None
    return type(version) is int and version == invoke_protocol.PROTOCOL_VERSION


def _request_from_protocol(
    args: argparse.Namespace, payload: Any
) -> "tuple[str, str, Any, BackendOptions]":
    """Parse a versioned (protocol 1) request, then resolve the same way the flags do."""
    request = parse_request(payload)
    selection = create_backend(
        request.endpoint,
        model=request.model,
        cheap=request.cheap,
        project_root=args.project_root,
    )
    # The two values the CLI owns rather than the caller: log_prefix is a
    # diagnostic tag (the parser refuses it outright), and an unset effort falls
    # back to the endpoint's configured one exactly as the flag surface does.
    options = dataclasses.replace(
        request.options,
        effort=request.options.effort or selection.effort,
        log_prefix=f"[{selection.endpoint}]",
    )
    return request.system, request.prompt, selection, options


def _refuse_v2(kind: str, message: str) -> int:
    """A protocol 2 refusal: nothing ran, so stderr carries it and the exit is EXIT_PROTOCOL."""
    _json(invoke_protocol.refusal_envelope(kind, message), stream=sys.stderr)
    return EXIT_PROTOCOL


def _cmd_invoke(args: argparse.Namespace, payload: Any) -> int:
    """``complete --request-file`` with ``"protocol": 2``: one call from the project model file.

    Exit 0 completed, 1 the call ran and failed (an unmet ``expect_files``
    included), 3 it ran and halted, 4 it was refused before anything was
    spawned (the refusal is on stderr, ``error.kind`` says what to fix).
    """
    try:
        if args.format == "text":
            raise invoke_protocol.InvokeRefusal(
                invoke_protocol.KIND_PROTOCOL, "protocol 2 answers in JSON only; drop --format text"
            )
        request = invoke_protocol.parse_invoke_request(payload)
        if (
            args.project_root is not None
            and request.project_root is not None
            and Path(args.project_root).resolve() != Path(request.project_root).resolve()
        ):
            raise invoke_protocol.InvokeRefusal(
                invoke_protocol.KIND_PATH,
                "--project-root and the request's project_root name different directories",
            )
        result = dispatch_invoke.invoke(
            request,
            default_project_root=args.project_root,
            backend_factory=_backend_factory,
        )
    except invoke_protocol.InvokeRefusal as exc:
        return _refuse_v2(exc.kind, str(exc))
    except ProtocolError as exc:
        return _refuse_v2(invoke_protocol.KIND_PROTOCOL, str(exc))
    except (EndpointRegistryError, EndpointResolveError, ModelResolveError) as exc:
        return _refuse_v2(invoke_protocol.KIND_CONFIGURATION, str(exc))
    _json(result.document)
    if result.outcome == dispatch_invoke.OUTCOME_COMPLETED:
        return EXIT_OK
    return EXIT_HALT if result.outcome == dispatch_invoke.OUTCOME_HALTED else EXIT_FAILURE


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid positive integer: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def _non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid non-negative integer: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {value}")
    return value


def _routed(args: argparse.Namespace) -> bool:
    """True when ``complete`` goes through ``declaration.run`` (selection + fallback).

    A multi-entry ``--models`` or any ``--requirements``. A single entry with no
    requirements keeps the original one-call path byte for byte.
    """
    if args.request_file:
        return False
    declared = _declared_models(args) or []
    return len(declared) > 1 or args.requirements is not None


def _failed_response(
    entry: str, attempt: Any, selection: Any, options: BackendOptions
) -> LLMResponse:
    """The failed response a routed run reports when no response exists."""
    halted = attempt is not None and attempt.outcome == "halted"
    return LLMResponse(
        text="",
        model=getattr(selection, "model", "") or "",
        status=ERROR,
        error=ResponseError(
            code=(attempt.halt if halted and attempt.halt else "execution"),
            message=(attempt.error if attempt is not None and attempt.error else "no entry served"),
        ),
        dropped_params=_dropped_for(selection.backend, options) if selection else (),
        forwarded_params=_forwarded_for(selection.backend, options) if selection else (),
    )


def _cmd_complete_routed(args: argparse.Namespace) -> int:
    """``complete`` over a declaration: first usable entry serves, halts fall back.

    The envelope is the single-call envelope for the entry that served (or the
    last one tried) plus ``entry``, ``run_status``, ``declaration`` and
    ``attempts`` (one object per execution, ``declaration.Attempt.to_json``).
    """
    declared = _declared_models(args) or []
    requirements = _load_requirements(args.requirements)
    if not declared:
        raise ValueError("--requirements needs --models: it filters a declaration")
    system = _read_text(args.system_file, _FLAG_DEFAULTS["system"] if args.system is None else args.system)
    user = _read_text(args.prompt_file, args.prompt, stdin_fallback=True)
    options = BackendOptions(
        max_tokens=_FLAG_DEFAULTS["max_tokens"] if args.max_tokens is None else args.max_tokens,
        temperature=args.temperature,
        timeout_s=args.timeout,
        max_retries=args.max_retries,
        effort=args.effort,
        cwd=args.cwd,
    )
    cheap = bool(args.cheap)
    selections: dict[str, Any] = {}

    def factory(name: str, **kwargs: Any) -> Any:
        kwargs.setdefault("model", args.model)
        kwargs.setdefault("cheap", cheap)
        selection = _backend_factory(name, **kwargs)
        selection = dataclasses.replace(selection, backend=_Recording(selection.backend)) \
            if dataclasses.is_dataclass(selection) else selection
        selections[name] = selection
        return selection

    attempts: list[Any] = []
    attempt_halts: list[Any] = []
    last_exc: list[Any] = [None]

    class _Recording:
        """Backend proxy remembering the exception behind each classified halt."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def classify_halt(self, exc: BaseException) -> Any:
            last_exc[0] = exc
            return self._inner.classify_halt(exc)

        def with_text_only(self) -> Any:
            # Defined here, not reached through __getattr__: delegating would
            # return the inner backend armed but UNWRAPPED, and the halt
            # recording above would silently stop for the armed call.
            return _Recording(self._inner.with_text_only())

    def on_attempt(attempt: Any) -> None:
        attempts.append(attempt)
        attempt_halts.append(
            halt_payload(attempt.halt, last_exc[0]) if attempt.halt else None
        )
        last_exc[0] = None

    max_attempts = args.max_attempts or len(declared)
    floor: Optional[NoUsableRoutingTarget] = None
    result = None
    try:
        result = declaration_run(
            declared,
            RunRequest(system=system, prompt=user, options=options),
            project_root=args.project_root,
            requirements=requirements,
            max_attempts=max_attempts,
            on_attempt=on_attempt,
            backend_factory=factory,
        )
    except NoUsableRoutingTarget as exc:
        if not attempts:
            raise  # nothing ran: the ordinary floor
        floor = exc
    last = attempts[-1] if attempts else None
    entry = result.entry if result is not None else (last.entry if last else None)
    selection = selections.get(entry) if entry else None
    status = result.status if result is not None else "no-usable-entry"
    extras = {
        "entry": entry,
        "run_status": status,
        "declaration": declared,
        "attempts": [
            {**a.to_json(), "halt_payload": h} for a, h in zip(attempts, attempt_halts)
        ],
    }
    if attempt_halts and attempt_halts[-1] is not None and (
        result is None or result.status != RUN_COMPLETED
    ):
        extras["halt"] = attempt_halts[-1]
    if floor is not None:
        extras["floor"] = floor.to_json()
    ran_contract = False
    if result is not None and status == RUN_COMPLETED:
        response = result.response
    elif result is not None and result.response is not None:
        response, ran_contract = result.response, True
    else:
        response = _failed_response(entry or "", last, selection, options)
    if args.format == "text":
        if status == RUN_COMPLETED:
            print(_answer_text(response))
        else:
            print(f"{response.error.code}: {response.error.message}", file=sys.stderr)
    else:
        envelope = _complete_envelope(selection, response, call_ran=ran_contract)
        envelope.update(extras)
        _json(envelope)
    if status == RUN_COMPLETED:
        return EXIT_OK
    halted = status == RUN_ATTEMPT_LIMIT or floor is not None
    return EXIT_HALT if halted else EXIT_FAILURE


def _cmd_complete(args: argparse.Namespace) -> int:
    if _routed(args):
        return _cmd_complete_routed(args)
    if args.max_attempts is not None and not args.request_file:
        # The single-call path makes exactly one execution; accepting the flag
        # and ignoring it would hide a limit the caller believes is in force.
        raise ValueError(
            "--max-attempts needs a multi-entry --models or --requirements: "
            "a single-entry call makes exactly one execution"
        )
    try:
        if args.request_file:
            payload = _read_request_payload(args)
            if _is_protocol_2(payload):
                return _cmd_invoke(args, payload)
            system, user, selection, options = _request_from_protocol(args, payload)
        else:
            system, user, selection, options = _request_from_flags(args)
    except ProtocolError as exc:
        # A protocol error never reached an endpoint, so it gets neither the
        # result envelope (there is no call to describe) nor an endpoint exit
        # code. Emitted on stderr because stdout is the result channel and a
        # consumer parsing it must not find a different shape there.
        _json(protocol_error_envelope(str(exc)), stream=sys.stderr)
        return EXIT_PROTOCOL
    # Bracket the call HERE too. A failure never reaches an adapter's own
    # timestamping, and a failed call still ran -- a timeout by definition
    # burned the whole budget -- so the CLI has to record what it can see.
    started_at = utc_now_iso()
    started_monotonic = time.monotonic()
    try:
        response = selection.backend.complete(system, user, model=selection.model, options=options)
    except OutputContractViolation as exc:
        # The call RAN: the adapter holds the full failed response (raw text,
        # the controls it emitted, the contract report), so the envelope
        # renders that rather than a rebuilt empty one. Not a halt.
        if args.format == "text":
            print(f"{exc.response.error.code}: {exc.response.error.message}", file=sys.stderr)
        else:
            _json(_complete_envelope(selection, exc.response, call_ran=True))
        return EXIT_FAILURE
    except Exception as exc:  # transport implementations expose heterogeneous exception types
        # ERROR-AS-DATA, and only here. The package API keeps RAISING -- every
        # existing consumer branches on typed exceptions, and returning a
        # failure there would make it read as a success at call sites that never
        # asked for this contract. The CLI is a protocol surface with no such
        # history, so a failure comes back in the SAME envelope shape as a
        # success and a caller parses one thing instead of two.
        halt = selection.backend.classify_halt(exc)
        failed = LLMResponse(
            text="",
            model=selection.model,
            status=TIMEOUT if isinstance(exc, AgentTimeoutError) else ERROR,
            error=ResponseError(code=halt or "execution", message=str(exc)),
            # An EmptyCompletionError carries the diagnostic the caller most
            # needs and cannot reconstruct -- the thinking block behind an empty
            # answer, and the stop token that says why it was empty. The
            # envelope already serializes both fields, so a failure that drops
            # them reports less than it holds. Every other exception leaves the
            # defaults.
            reasoning=getattr(exc, "reasoning", "") or "",
            finish_reason=getattr(exc, "finish_reason", None),
            output_tokens=getattr(exc, "output_tokens", 0) or 0,
            wall_ms=int((time.monotonic() - started_monotonic) * 1000),
            # Dropped params do not depend on the outcome: the adapter would not
            # have read them either way, so this is as true of a failed call as
            # of a completed one. An adapter with a per-call report (the
            # effort plan) answers for itself, so an overridden, suppressed or
            # translated effort is reported exactly as a completed call would.
            dropped_params=_dropped_for(selection.backend, options),
            forwarded_params=_forwarded_for(selection.backend, options),
            started_at=started_at,
            ended_at=utc_now_iso(),
        )
        if args.format == "text":
            # the text format carries no envelope, so the diagnostic channel is
            # the only place a failure can be stated
            print(f"{failed.error.code}: {failed.error.message}", file=sys.stderr)
        else:
            envelope = _complete_envelope(selection, failed)
            if halt:
                envelope["halt"] = halt_payload(halt, exc)
            _json(envelope)
        # Exit codes are UNCHANGED: they stay the shell-level signal, and the
        # envelope is the machine-readable one. A caller may read either.
        return EXIT_HALT if halt else EXIT_FAILURE
    if args.format == "text":
        print(_answer_text(response))
    else:
        _json(_complete_envelope(selection, response))
    return EXIT_OK


def _answer_text(response: LLMResponse) -> str:
    """The text ``--format text`` prints as the answer.

    A structurally repaired contract answer keeps the RAW model text in
    ``response.text`` (provenance), which is not parseable JSON. Printing it as
    the answer would turn a formerly failing call into exit 0 with broken JSON,
    so the repaired value is printed, serialized as JSON. The JSON envelope
    carries both: raw ``text`` and the validated ``structured`` value, with
    ``output_contract.repair`` saying a repair was applied.
    """
    report = response.output_contract
    if report is not None and report.repaired and response.structured is not None:
        return json.dumps(response.structured, ensure_ascii=False)
    return response.text


def _params_report(backend: Any, options: BackendOptions) -> Optional[tuple]:
    """The adapter's own ``(dropped, forwarded)`` report for ``options``, if it has one.

    ``OpenRouterBackend.params_report`` derives both from the call's effort
    plan -- the same derivation its successful response uses -- so the family
    record's blanket "effort is dropped" never contradicts an endpoint that
    delivered it, and a suppressed extras key is never reported as forwarded.
    """
    report = getattr(backend, "params_report", None)
    return report(options) if callable(report) else None


def _dropped_for(backend: Any, options: BackendOptions) -> tuple:
    """Params this request would have had dropped, or () if unknowable.

    Read defensively: ``capabilities`` is a ClassVar on every shipped adapter,
    but the backend here is whatever the factory produced, and a caller-injected
    or test backend need not carry one.
    """
    report = _params_report(backend, options)
    if report is not None:
        return report[0]
    capabilities = getattr(backend, "capabilities", None)
    if capabilities is None:
        return ()
    return derive_dropped_params(capabilities, options)


def _forwarded_for(backend: Any, options: BackendOptions) -> tuple:
    """Params this request would have forwarded unvalidated, or () if unknowable.

    Read defensively for the same reason as :func:`_dropped_for`, and true of a
    failed call for the same reason: whether the adapter validates a param does
    not depend on how the call came out.
    """
    report = _params_report(backend, options)
    if report is not None:
        return report[1]
    capabilities = getattr(backend, "capabilities", None)
    if capabilities is None:
        return ()
    return derive_forwarded_params(capabilities, options)


def _complete_envelope(
    selection: Any, response: LLMResponse, *, call_ran: bool = False
) -> dict[str, Any]:
    """One result shape for a completed, timed-out, or failed call.

    ``error`` is rendered through :meth:`ResponseError.to_json` rather than
    ``asdict``'s nested dict so the payload stays the record's own declared
    shape, and it is omitted entirely when the call completed -- a null error
    beside a "completed" status is noise a consumer has to branch on twice.
    """
    payload = asdict(response)
    payload.pop("error", None)
    if response.error is not None:
        payload["error"] = response.error.to_json()
        # call_ran: an output-contract violation. The adapter built this
        # response and knows what it emitted, so the controls stay as it
        # reported them; the omission below is for exceptions the CLI caught.
        # OMITTED, not emptied. The CLI catches an exception, which carries no
        # record of the argv the adapter built, so what the request emitted is
        # UNKNOWN here -- and an empty list does not mean "unknown", it means
        # "this call emitted no controls". For claude, codex and opencode that
        # would be false, since each emits unconditional controls on every
        # invocation. A missing key is the only honest way to say "not known";
        # `structured` goes with it for the same reason.
        if not call_ran:
            payload.pop("execution_controls_applied", None)
            payload.pop("structured", None)
    return {
        # Versioned so a consumer can tell which payload shape it is holding.
        # The other keys are unchanged from the unversioned envelope: adding a
        # field cannot break a reader, while renaming one would, and there is no
        # reason to spend a compatibility break on tidiness.
        "protocol": PROTOCOL_VERSION,
        "endpoint": selection.endpoint,
        "kind": selection.kind,
        "backend": selection.backend.name,
        "response": payload,
    }


def _cmd_repair_json(schema_file: Path) -> int:
    """Repair the UTF-8 answer on stdin under one validated schema."""
    if not schema_file.is_absolute():
        raise ValueError("--schema-file must be an absolute path")
    schema = json.loads(schema_file.read_text(encoding="utf-8"))
    check_schema(schema)
    raw = sys.stdin.buffer.read().decode("utf-8")
    result = repair_json_structure(schema, raw)
    repaired = result.status == STATUS_REPAIRED
    _json({
        "protocol": 1,
        "status": result.status,
        "text": result.text if repaired else raw,
        "edits": [edit.to_json() for edit in result.edits] if repaired else [],
        "reason": result.reason,
    })
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-kit completion",
        description="One configured completion, the request schema, and structural JSON repair.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    repair_json = sub.add_parser(
        "repair-json",
        help="Repair one raw model answer structurally, without calling a model.",
    )
    repair_json.add_argument(
        "--schema-file",
        type=Path,
        required=True,
        help="Absolute path to a JSON schema in the supported subset.",
    )

    complete = sub.add_parser(
        "complete",
        help=(
            "Run one configured completion. With several --models (or "
            "--requirements) the first usable entry serves and a classified halt "
            "falls back to the next."
        ),
    )
    _add_models_arg(complete)
    _add_requirements_arg(complete)
    complete.add_argument(
        "--max-attempts",
        type=_positive_int,
        default=None,
        help=(
            "Executions allowed across the declaration (default: one per declared "
            "entry; refused with exit 2 on a single-entry call without --requirements)."
        ),
    )
    _add_project_arg(complete)
    complete.add_argument("--model")
    # Every call-describing flag defaults to None so "unset" is distinguishable
    # from "set to a falsy value". Their real defaults live in _FLAG_DEFAULTS --
    # keeping them here would make `--max-tokens 4096` indistinguishable from
    # silence, and the --request-file conflict check below would then let a
    # named flag be silently discarded.
    complete.add_argument("--cheap", action="store_true", default=None)
    complete.add_argument("--system", default=None)
    complete.add_argument("--system-file", type=Path)
    prompt = complete.add_mutually_exclusive_group()
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file", type=Path)
    complete.add_argument("--max-tokens", type=int)
    complete.add_argument("--temperature", type=float)
    complete.add_argument("--timeout", type=float)
    complete.add_argument(
        "--max-retries",
        type=_non_negative_int,
        help="OpenAI-compatible endpoints: SDK retries per call (default 2; 0 disables).",
    )
    complete.add_argument("--effort")
    complete.add_argument("--cwd", type=Path)
    complete.add_argument("--format", choices=("json", "text"), default="json")
    # The protocol surface. `-` reads stdin, which is unambiguous here because a
    # request carries its own prompt -- the stdin-as-prompt fallback below
    # applies only to the flag surface.
    complete.add_argument(
        "--request-file",
        help="Read a versioned JSON request (see `request-schema`); - for stdin.",
    )
    request_schema = sub.add_parser(
        "request-schema",
        help="Print the accepted `complete --request-file` request shape.",
    )
    request_schema.add_argument(
        "--protocol",
        type=int,
        choices=(PROTOCOL_VERSION, invoke_protocol.PROTOCOL_VERSION),
        default=PROTOCOL_VERSION,
        help="Which request protocol to describe (default 1; 2 selects from the project model file).",
    )
    return parser


def run(argv: Sequence[str]) -> int:
    """Run one ``completion`` verb and return its exit code."""
    args = _parser().parse_args(list(argv))

    def dispatch() -> int:
        if args.cmd == "repair-json":
            return _cmd_repair_json(args.schema_file)
        if args.cmd == "request-schema":
            _json(
                invoke_protocol.describe_invoke_schema()
                if args.protocol == invoke_protocol.PROTOCOL_VERSION
                else describe_request_schema()
            )
            return EXIT_OK
        if args.cmd == "complete":
            return _cmd_complete(args)
        return EXIT_USAGE

    return guarded(dispatch)
