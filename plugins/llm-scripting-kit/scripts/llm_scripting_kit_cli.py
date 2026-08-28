"""Compatibility launcher for the installed :mod:`llm_scripting_kit.cli`.

Subcommands (all accept ``--endpoint NAME``; default is the config's
``default_endpoint``, i.e. ``openrouter``):

    status      Resolve the key and validate it for the endpoint. For an
                OpenRouter endpoint prints label, usage, limit, free-tier flag,
                and rate limit; other endpoints print a simpler ok/skipped.
    set-key     Prompt for a new key (via getpass), validate it before
                writing, and store it under the endpoint's key_env in the
                user-scoped .env file. Pass --no-validate to skip the network
                round-trip.
    which       Print the resolved key's source path (or "missing").

Runs under the plugin venv when bootstrap has provisioned it (PyYAML there lets
the layered config.yaml be read), and degrades to stdlib-only otherwise: without
PyYAML the shipped model baseline is used and a warning goes to stderr, but key
management keeps working. The shims in ``bin/`` pick the interpreter.
"""

import argparse
import getpass
import json
import sys
from pathlib import Path

# Make the bundled lib/ importable when invoked directly.
_HERE = Path(__file__).resolve().parent
_LIB_DIR = _HERE.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from llm_scripting_kit.account import AccountCheckError, validate_endpoint  # noqa: E402
from llm_scripting_kit.api_key import get_api_key  # noqa: E402
from llm_scripting_kit.constants import USER_ENV_FILE  # noqa: E402
from llm_scripting_kit.env_file import read_env_file, write_env_file  # noqa: E402
from llm_scripting_kit.models import EndpointResolveError, resolve_endpoint  # noqa: E402


def _add_endpoint_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--endpoint",
        default=None,
        help="Named endpoint (default: the config's default_endpoint, 'openrouter').",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="llm-scripting-kit",
        description="Manage LLM endpoint API keys for plugins-kit consumers.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="Validate the resolved key and print account info.")
    _add_endpoint_arg(p_status)

    p_set = sub.add_parser("set-key", help="Store a new API key in the user .env file.")
    p_set.add_argument(
        "--key",
        help="Key value. Omit to prompt securely via getpass.",
    )
    p_set.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip the validation round-trip.",
    )
    _add_endpoint_arg(p_set)

    p_which = sub.add_parser("which", help="Print the source path of the resolved key.")
    _add_endpoint_arg(p_which)

    args = parser.parse_args(argv)

    if args.cmd == "status":
        return _cmd_status(args.endpoint)
    if args.cmd == "set-key":
        return _cmd_set_key(args.key, validate=not args.no_validate, endpoint=args.endpoint)
    if args.cmd == "which":
        return _cmd_which(args.endpoint)
    parser.error(f"unknown command: {args.cmd}")
    return 2


def _resolve_endpoint_or_exit(endpoint: str | None):
    try:
        return resolve_endpoint(endpoint)
    except EndpointResolveError as e:
        print(f"Endpoint error: {e}", file=sys.stderr)
        return None


def _cmd_status(endpoint: str | None) -> int:
    ep = _resolve_endpoint_or_exit(endpoint)
    if ep is None:
        return 2
    lookup = get_api_key(endpoint=endpoint)
    if lookup.key is None:
        print(f"No API key found for endpoint '{ep['name']}' ({ep['key_env']}).")
        print(f"Set one with `llm-scripting-kit set-key --endpoint {ep['name']}`.")
        return 1

    print(f"Endpoint: {ep['name']} ({ep['base_url']})")
    print(f"Source: {lookup.source}", end="")
    if lookup.source_path is not None:
        print(f" ({lookup.source_path})")
    else:
        print()

    try:
        status = validate_endpoint(ep, lookup.key)
    except AccountCheckError as e:
        print(f"Validation failed: {e}", file=sys.stderr)
        return 2

    if status is None:
        print(f"Status: key present (account_check '{ep['account_check']}' -- not validated)")
        return 0

    if not status.ok:
        print(f"Status: REJECTED ({status.failure_reason})")
        if status.failure_reason == "auth":
            print("The key was rejected (HTTP 401).")
            if ep["account_check"] == "openrouter":
                print("Generate a new one at https://openrouter.ai/keys.")
        elif status.failure_reason == "no_credit":
            print("Account out of credit (HTTP 402). Add credit at https://openrouter.ai/credits.")
        return 1

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
    return 0


def _cmd_set_key(provided: str | None, *, validate: bool, endpoint: str | None) -> int:
    ep = _resolve_endpoint_or_exit(endpoint)
    if ep is None:
        return 2
    key_env = ep["key_env"]

    key = provided
    if not key:
        try:
            key = getpass.getpass(f"API key for {ep['name']} (input hidden): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            return 130

    if not key:
        print("Empty key, nothing written.", file=sys.stderr)
        return 1

    if validate:
        try:
            status = validate_endpoint(ep, key)
        except AccountCheckError as e:
            print(f"Could not validate key: {e}", file=sys.stderr)
            print("Pass --no-validate to write the key without checking it.", file=sys.stderr)
            return 2
        if status is not None and not status.ok:
            print(f"Validation failed: {status.failure_reason}", file=sys.stderr)
            return 1

    # Preserve any existing keys in the file (keys for other endpoints coexist
    # as separate KEY=VALUE lines).
    existing = read_env_file(USER_ENV_FILE)
    existing[key_env] = key
    write_env_file(USER_ENV_FILE, existing)
    print(f"Wrote {key_env} to {USER_ENV_FILE}")
    return 0


def _cmd_which(endpoint: str | None) -> int:
    ep = _resolve_endpoint_or_exit(endpoint)
    if ep is None:
        return 2
    lookup = get_api_key(endpoint=endpoint)
    if lookup.key is None:
        print("missing")
        return 1
    if lookup.source_path is not None:
        print(f"{lookup.source}: {lookup.source_path}")
    else:
        print(f"{lookup.source}")
    return 0


if __name__ == "__main__":
    from llm_scripting_kit.cli import main as package_main

    sys.exit(package_main())
