"""Public command-line interface for endpoint management and completions."""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from .account import AccountCheckError, validate_endpoint
from .api_key import get_api_key
from .completion import BackendOptions, adapter_capabilities, create_backend
from .constants import USER_ENV_FILE
from .env_file import read_env_file, write_env_file
from .model_endpoints import EndpointRegistryError
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
    complete.add_argument("--cheap", action="store_true")
    complete.add_argument("--system", default="")
    complete.add_argument("--system-file", type=Path)
    prompt = complete.add_mutually_exclusive_group()
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file", type=Path)
    complete.add_argument("--max-tokens", type=int, default=4096)
    complete.add_argument("--temperature", type=float, default=0.3)
    complete.add_argument("--timeout", type=float)
    complete.add_argument("--effort")
    complete.add_argument("--cwd", type=Path)
    complete.add_argument("--format", choices=("json", "text"), default="json")
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
            return _cmd_endpoints(args.project_root)
        if args.cmd == "models":
            return _cmd_models(args.endpoint, args.project_root)
        if args.cmd == "resolve":
            return _cmd_resolve(args.endpoint, args.model, args.cheap, args.project_root)
        if args.cmd == "complete":
            return _cmd_complete(args)
    except (EndpointResolveError, ModelResolveError, EndpointRegistryError, OSError, ValueError) as exc:
        _json({"error": {"kind": "configuration", "message": str(exc)}}, stream=sys.stderr)
        return EXIT_USAGE
    return EXIT_USAGE


def _cmd_endpoints(project_root: Optional[str]) -> int:
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


def _cmd_complete(args: argparse.Namespace) -> int:
    system = _read_text(args.system_file, args.system)
    user = _read_text(args.prompt_file, args.prompt, stdin_fallback=True)
    selection = create_backend(args.endpoint, model=args.model, cheap=args.cheap, project_root=args.project_root)
    options = BackendOptions(max_tokens=args.max_tokens, temperature=args.temperature,
                             timeout_s=args.timeout, effort=args.effort or selection.effort,
                             cwd=args.cwd, log_prefix=f"[{selection.endpoint}]")
    try:
        response = selection.backend.complete(system, user, model=selection.model, options=options)
    except Exception as exc:  # transport implementations expose heterogeneous exception types
        halt = selection.backend.classify_halt(exc)
        _json({"error": {"kind": halt or "execution", "message": str(exc)},
               "endpoint": selection.endpoint, "backend": selection.backend.name}, stream=sys.stderr)
        return EXIT_HALT if halt else EXIT_FAILURE
    if args.format == "text":
        print(response.text)
    else:
        _json({"endpoint": selection.endpoint, "kind": selection.kind,
               "backend": selection.backend.name, "response": asdict(response)})
    return EXIT_OK


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
