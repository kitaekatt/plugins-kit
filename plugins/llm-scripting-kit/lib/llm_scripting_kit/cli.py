"""Public command-line interface for endpoint management and completions."""
from __future__ import annotations

import argparse
import dataclasses
import getpass
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from .account import AccountCheckError, validate_endpoint
from .api_key import get_api_key
from .completion import (
    ERROR,
    TIMEOUT,
    AgentTimeoutError,
    BackendOptions,
    LLMResponse,
    ResponseError,
    adapter_capabilities,
    create_backend,
    derive_dropped_params,
    derive_forwarded_params,
    utc_now_iso,
)
from .constants import USER_ENV_FILE
from .env_file import read_env_file, write_env_file
from .model_endpoints import EndpointRegistryError
from .reachability import (
    DEFAULT_VERIFY_TIMEOUT_S,
    STATUS_UNKNOWN,
    STATUS_UNREACHABLE,
    check_entry,
    check_many,
)
from .seats import discover_seats
from .request_protocol import (
    PROTOCOL_VERSION,
    ProtocolError,
    describe_request_schema,
    parse_request,
    protocol_error_envelope,
)
from .models import (
    EndpointResolveError,
    ModelResolveError,
    default_endpoint_name,
    discover_model_entries,
    load_model_config,
    resolve_endpoint,
    resolve_model,
)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_HALT = 3
EXIT_PROTOCOL = 4
"""The request could not be understood, so no call was attempted.

Separate from EXIT_USAGE (2) on purpose, even though both mean "bad input".
EXIT_USAGE is argparse's territory -- a human typing a command wrong. This code
is for a MACHINE consumer's structured request, and the two want opposite
responses: a person re-reads --help, a program fixes its serializer. Separate
from EXIT_FAILURE (1) for the reason the protocol exists at all: 1 means a call
ran and failed, and retrying it may work; 4 means nothing ran and retrying the
same bytes cannot.
"""
EXIT_INDETERMINATE = 5
"""`probe` could not determine reachability -- the check itself did not run to
a verdict (e.g. an optional dependency such as `bootstrap_lib` was
unavailable), as opposed to running and finding the target down.

Distinct from EXIT_USAGE (2) on a DIFFERENT axis than EXIT_PROTOCOL is from
EXIT_FAILURE: EXIT_USAGE there means the endpoint NAME does not resolve to
anything configured -- a config problem, decided before any check runs.
EXIT_INDETERMINATE means the name resolved fine and a check was attempted, but
that check itself could not complete. A caller gating on `probe`'s exit code
must be able to tell "not configured" (2), "checked, and it is down" (1), and
"could not check" (5) apart, and must not treat 5 as though it were 1 --
skipping a possibly-live endpoint on the strength of an unrun check is exactly
the false negative this code exists to prevent. See DEFAULT_VERIFY_TIMEOUT_S
and reachability.STATUS_UNKNOWN for the full mapping and its rationale.
"""


def _json(value: Any, *, stream: Any = None) -> None:
    print(json.dumps(value, sort_keys=True), file=stream or sys.stdout)


def _add_endpoint_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--endpoint", default=None, help="Configured endpoint name.")


def _add_project_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-root", help="Project root for layered configuration.")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-scripting-kit")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name in ("status", "which"):
        command = sub.add_parser(name)
        _add_endpoint_arg(command)
    set_key = sub.add_parser("set-key")
    _add_endpoint_arg(set_key)
    set_key.add_argument("--key")
    set_key.add_argument("--no-validate", action="store_true")

    endpoints = sub.add_parser("endpoints", help="List configured transports and harnesses.")
    _add_project_arg(endpoints)
    endpoints.add_argument(
        "--verify",
        action="store_true",
        help=(
            "Check each endpoint's reachability now and add a 'reachability' "
            "field to it. Off by default: plain `endpoints` stays instant, "
            "offline config listing -- this makes network/subprocess calls."
        ),
    )
    endpoints.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_VERIFY_TIMEOUT_S,
        help=f"Per-endpoint --verify timeout in seconds (default {DEFAULT_VERIFY_TIMEOUT_S:g}).",
    )
    probe = sub.add_parser(
        "probe",
        help="Exit-code check: is one configured endpoint reachable right now?",
    )
    _add_endpoint_arg(probe)
    _add_project_arg(probe)
    probe.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_VERIFY_TIMEOUT_S,
        help=f"Reachability check timeout in seconds (default {DEFAULT_VERIFY_TIMEOUT_S:g}).",
    )
    seats = sub.add_parser("seats", help="List reachable UP and BESIDE harness seats.")
    seats.add_argument("--self", dest="self_ref", required=True, help="Self endpoint or exact model id.")
    seats.add_argument("--json", action="store_true", help="Emit the structured result as JSON.")
    _add_project_arg(seats)
    seats.add_argument("--timeout", type=float, default=None, help="Per-seat probe timeout in seconds.")
    models = sub.add_parser("models", help="List models for an endpoint.")
    _add_endpoint_arg(models)
    _add_project_arg(models)
    resolve = sub.add_parser("resolve", help="Resolve an endpoint/model selection.")
    _add_endpoint_arg(resolve)
    _add_project_arg(resolve)
    resolve.add_argument("--model")
    resolve.add_argument("--cheap", action="store_true")

    complete = sub.add_parser("complete", help="Run one configured completion.")
    _add_endpoint_arg(complete)
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
    sub.add_parser(
        "request-schema",
        help="Print the accepted `complete --request-file` request shape.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "status":
        return _cmd_status(args.endpoint)
    if args.cmd == "set-key":
        return _cmd_set_key(args.key, validate=not args.no_validate, endpoint=args.endpoint)
    if args.cmd == "which":
        return _cmd_which(args.endpoint)
    try:
        if args.cmd == "endpoints":
            return _cmd_endpoints(args.project_root, verify=args.verify, timeout_s=args.timeout)
        if args.cmd == "probe":
            return _cmd_probe(args.endpoint, args.project_root, args.timeout)
        if args.cmd == "seats":
            return _cmd_seats(args.self_ref, args.json, args.project_root, args.timeout)
        if args.cmd == "models":
            return _cmd_models(args.endpoint, args.project_root)
        if args.cmd == "resolve":
            return _cmd_resolve(args.endpoint, args.model, args.cheap, args.project_root)
        if args.cmd == "request-schema":
            _json(describe_request_schema())
            return EXIT_OK
        if args.cmd == "complete":
            return _cmd_complete(args)
    except (EndpointResolveError, ModelResolveError, EndpointRegistryError, OSError, ValueError) as exc:
        _json({"error": {"kind": "configuration", "message": str(exc)}}, stream=sys.stderr)
        return EXIT_USAGE
    return EXIT_USAGE


def _collect_endpoint_entries(
    project_root: Optional[str],
) -> "tuple[dict, Any, dict[str, dict[str, Any]]]":
    """Build the ``endpoints`` verb's entry map. Pure config, no I/O beyond disk reads.

    Shared by ``_cmd_endpoints`` and ``_cmd_probe`` so there is exactly one
    place that turns configuration into the JSON shape a reachability check
    dispatches on.
    """
    config = load_model_config(project_root=project_root)
    discovery = discover_model_entries(config=config, project_root=project_root)
    values: dict[str, dict[str, Any]] = {}
    raw = config.get("endpoints") or {}
    for name in raw:
        entry = discovery.get(str(name))
        if entry is not None:
            values[str(name)] = _entry_json(entry)
            continue
        try:
            endpoint = resolve_endpoint(str(name), config=config, project_root=project_root)
        except EndpointResolveError:
            continue
        values[str(name)] = {
            "kind": "transport", "base_url": endpoint["base_url"],
            "key_env": endpoint["key_env"], "default_model": endpoint.get("default"),
            "adapter": "openrouter",
        }
    for name, entry in discovery.items():
        values.setdefault(name, _entry_json(entry))
    return config, discovery, values


def _cmd_endpoints(project_root: Optional[str], *, verify: bool = False, timeout_s: float = DEFAULT_VERIFY_TIMEOUT_S) -> int:
    config, discovery, values = _collect_endpoint_entries(project_root)
    if verify:
        # Opt-in only: a plain `endpoints` call must never make a network or
        # subprocess call, since callers enumerate configuration with it and
        # must not start paying for round trips silently.
        checks = check_many(values, timeout=timeout_s, project_root=project_root)
        for name, result in checks.items():
            values[name]["reachability"] = result.to_json()
    _json({
        "default": default_endpoint_name(config),
        "endpoints": values,
        # Keyed by adapter family, not by endpoint: the facts are properties of
        # adapter code, and duplicating them per endpoint would be the drift the
        # advertisement exists to remove.
        "capabilities": {
            name: cap.to_json() for name, cap in sorted(adapter_capabilities().items())
        },
        "notes": discovery.notes,
    })
    return EXIT_OK


def _cmd_probe(endpoint: Optional[str], project_root: Optional[str], timeout_s: float) -> int:
    """Exit-code check: is one configured endpoint reachable right now?

    A thin wrapper over the same :mod:`.reachability` code path `endpoints
    --verify` uses -- one implementation, two surfaces. Exit code is the
    primary interface: THREE distinguishable outcomes, not a plain
    success/failure pair -- see EXIT_INDETERMINATE for why "could not check"
    must not collapse into "checked and it is down". A short reason also goes
    to stderr on any non-zero exit, and the same reachability record rides on
    stdout as JSON for a caller that wants the detail either way.
    """
    config, _discovery, values = _collect_endpoint_entries(project_root)
    name = endpoint or default_endpoint_name(config)
    entry_json = values.get(name)
    if entry_json is None:
        raise EndpointResolveError(f"no configured endpoint named '{name}'")
    result = check_entry(entry_json, name, timeout=timeout_s, project_root=project_root)
    _json({"endpoint": name, **entry_json, "reachability": result.to_json()})
    # Exit code mapping (see EXIT_INDETERMINATE for the full rationale):
    #   0 (EXIT_OK)           reachability.STATUS_REACHABLE   -- checked, answered
    #   1 (EXIT_FAILURE)      reachability.STATUS_UNREACHABLE -- checked, did not answer
    #   5 (EXIT_INDETERMINATE) reachability.STATUS_UNKNOWN     -- could not check at all
    # 2 (EXIT_USAGE) is reserved for an endpoint NAME that does not resolve to
    # configuration at all -- raised above, before a check is ever attempted.
    if result.status == STATUS_UNREACHABLE:
        print(result.detail, file=sys.stderr)
        return EXIT_FAILURE
    if result.status == STATUS_UNKNOWN:
        print(result.detail, file=sys.stderr)
        return EXIT_INDETERMINATE
    return EXIT_OK


def _cmd_seats(
    self_ref: str,
    as_json: bool,
    project_root: Optional[str],
    timeout_s: Optional[float],
) -> int:
    """Print reachable frontier seats and preserve indeterminate probes."""
    result = discover_seats(
        self_ref, project_root=project_root, timeout=timeout_s
    )
    if as_json:
        _json(result.to_json())
    else:
        for seat in result.seats:
            print(f"{seat.relation} {seat.endpoint} ({seat.band}, {seat.harness})")
    if result.probe_unknown:
        for seat in result.probe_unknown:
            print(
                f"{seat.endpoint}: {seat.reachability.detail}", file=sys.stderr
            )
        return EXIT_INDETERMINATE
    return EXIT_OK


# Which adapter family serves an endpoint. Mirrors create_backend's harness
# dispatch; capabilities are per ADAPTER FAMILY while endpoints are per registry
# entry, and several entries share one adapter (sol and luna are both codex).
_HARNESS_ADAPTERS = {
    "claude": "claude-cli",
    "codex": "codex-cli",
    "opencode": "opencode-cli",
}


def _adapter_for(entry: Any) -> Optional[str]:
    if getattr(entry, "kind", None) == "harness":
        return _HARNESS_ADAPTERS.get((getattr(entry, "harness", "") or "").lower())
    return "openrouter"


def _entry_json(entry: Any) -> dict[str, Any]:
    result = {"kind": entry.kind, "model": entry.model, "name": entry.name}
    if entry.kind == "harness":
        result.update({"harness": entry.harness, "effort": entry.effort})
    else:
        result.update({"base_url": entry.base_url, "key_env": entry.key_env})
    adapter = _adapter_for(entry)
    if adapter is not None:
        result["adapter"] = adapter
    return result


def _cmd_models(endpoint: Optional[str], project_root: Optional[str]) -> int:
    selection = create_backend(endpoint, project_root=project_root)
    if selection.kind == "harness":
        models = {selection.endpoint: selection.model}
    else:
        config = load_model_config(project_root=project_root)
        ep = resolve_endpoint(selection.endpoint, config=config, project_root=project_root)
        models = {
            alias: value.get("slug") if isinstance(value, dict) else None
            for alias, value in (ep.get("models") or {}).items()
        }
    _json({"endpoint": selection.endpoint, "kind": selection.kind, "models": models})
    return EXIT_OK


def _cmd_resolve(endpoint: Optional[str], model: Optional[str], cheap: bool, project_root: Optional[str]) -> int:
    selection = create_backend(endpoint, model=model, cheap=cheap, project_root=project_root)
    _json({"endpoint": selection.endpoint, "kind": selection.kind, "backend": selection.backend.name,
           "model": selection.model, "effort": selection.effort})
    return EXIT_OK


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
    "effort",
    "cwd",
)

#: The flag surface's own defaults, applied in _request_from_flags rather than by
#: argparse. Keeping them here makes "unset" observable, which is what the
#: conflict check above needs. ``None`` means no temperature field is sent.
_FLAG_DEFAULTS = {"max_tokens": 4096, "temperature": None, "system": "", "cheap": False}


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
    selection = create_backend(
        args.endpoint,
        model=args.model,
        cheap=flag("cheap"),
        project_root=args.project_root,
    )
    options = BackendOptions(
        max_tokens=flag("max_tokens"),
        temperature=flag("temperature"),
        timeout_s=args.timeout,
        effort=args.effort or selection.effort,
        cwd=args.cwd,
        log_prefix=f"[{selection.endpoint}]",
    )
    return system, user, selection, options


def _request_from_protocol(
    args: argparse.Namespace,
) -> "tuple[str, str, Any, BackendOptions]":
    """Parse a versioned request, then resolve the same way the flags do."""
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
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"request is not valid JSON: {exc}") from exc

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


def _cmd_complete(args: argparse.Namespace) -> int:
    try:
        if args.request_file:
            system, user, selection, options = _request_from_protocol(args)
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
            # of a completed one.
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
            _json(_complete_envelope(selection, failed))
        # Exit codes are UNCHANGED: they stay the shell-level signal, and the
        # envelope is the machine-readable one. A caller may read either.
        return EXIT_HALT if halt else EXIT_FAILURE
    if args.format == "text":
        print(response.text)
    else:
        _json(_complete_envelope(selection, response))
    return EXIT_OK


def _dropped_for(backend: Any, options: BackendOptions) -> tuple:
    """Params this request would have had dropped, or () if unknowable.

    Read defensively: ``capabilities`` is a ClassVar on every shipped adapter,
    but the backend here is whatever the factory produced, and a caller-injected
    or test backend need not carry one.
    """
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
    capabilities = getattr(backend, "capabilities", None)
    if capabilities is None:
        return ()
    return derive_forwarded_params(capabilities, options)


def _complete_envelope(selection: Any, response: LLMResponse) -> dict[str, Any]:
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
        # OMITTED, not emptied. The CLI catches an exception, which carries no
        # record of the argv the adapter built, so what the request emitted is
        # UNKNOWN here -- and an empty list does not mean "unknown", it means
        # "this call emitted no controls". For claude, codex and opencode that
        # would be false, since each emits unconditional controls on every
        # invocation. A missing key is the only honest way to say "not known";
        # `structured` goes with it for the same reason.
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


def _resolve_endpoint_or_exit(endpoint: Optional[str]) -> Optional[dict]:
    try:
        return resolve_endpoint(endpoint)
    except EndpointResolveError as exc:
        print(f"Endpoint error: {exc}", file=sys.stderr)
        return None


def _cmd_status(endpoint: Optional[str]) -> int:
    ep = _resolve_endpoint_or_exit(endpoint)
    if ep is None:
        return EXIT_USAGE
    lookup = get_api_key(endpoint=endpoint)
    if lookup.key is None:
        print(f"No API key found for endpoint '{ep['name']}' ({ep['key_env']}).")
        return EXIT_FAILURE
    print(f"Endpoint: {ep['name']} ({ep['base_url']})")
    print(f"Source: {lookup.source}" + (f" ({lookup.source_path})" if lookup.source_path else ""))
    try:
        status = validate_endpoint(ep, lookup.key)
    except AccountCheckError as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if status is None:
        print(f"Status: key present (account_check '{ep['account_check']}' -- not validated)")
        return EXIT_OK
    if not status.ok:
        print(f"Status: REJECTED ({status.failure_reason})")
        if status.failure_reason == "auth":
            print("The key was rejected (HTTP 401).")
            if ep["account_check"] == "openrouter":
                print("Generate a new one at https://openrouter.ai/keys.")
        elif status.failure_reason == "no_credit":
            print("Account out of credit (HTTP 402). Add credit at https://openrouter.ai/credits.")
        return EXIT_FAILURE
    print("Status: OK")
    print(f"Label: {status.label or '<unlabeled>'}")
    if status.usage is not None:
        print(f"Usage: {status.usage}")
    if status.limit is not None:
        print(f"Limit: {status.limit}")
    if status.is_free_tier is not None:
        print(f"Free tier: {status.is_free_tier}")
    if status.rate_limit:
        print(f"Rate limit: {json.dumps(status.rate_limit)}")
    return EXIT_OK


def _cmd_set_key(provided: Optional[str], *, validate: bool, endpoint: Optional[str]) -> int:
    ep = _resolve_endpoint_or_exit(endpoint)
    if ep is None:
        return EXIT_USAGE
    key = provided
    if not key:
        try:
            key = getpass.getpass(f"API key for {ep['name']} (input hidden): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            return 130
    if not key:
        print("Empty key, nothing written.", file=sys.stderr)
        return EXIT_FAILURE
    if validate:
        try:
            status = validate_endpoint(ep, key)
        except AccountCheckError as exc:
            print(f"Could not validate key: {exc}", file=sys.stderr)
            print("Pass --no-validate to write the key without checking it.", file=sys.stderr)
            return EXIT_USAGE
        if status is not None and not status.ok:
            print(f"Validation failed: {status.failure_reason}", file=sys.stderr)
            return EXIT_FAILURE
    existing = read_env_file(USER_ENV_FILE)
    existing[ep["key_env"]] = key
    write_env_file(USER_ENV_FILE, existing)
    print(f"Wrote {ep['key_env']} to {USER_ENV_FILE}")
    return EXIT_OK


def _cmd_which(endpoint: Optional[str]) -> int:
    if _resolve_endpoint_or_exit(endpoint) is None:
        return EXIT_USAGE
    lookup = get_api_key(endpoint=endpoint)
    if lookup.key is None:
        print("missing")
        return EXIT_FAILURE
    print(f"{lookup.source}" + (f": {lookup.source_path}" if lookup.source_path else ""))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
