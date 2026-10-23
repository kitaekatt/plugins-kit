"""``llm-kit account``: credential status, source lookup and key storage.

Carries the legacy ``status``, ``which`` and ``set-key`` verbs. The CLI is the
composition root for credentials: it resolves them through
``llm_kit.registry`` (``resolve_endpoint``, ``get_api_key``, the user ``.env``
file) and injects the key into ``llm_kit.adapters.account``, which performs the
network check and reads no credential itself.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from llm_kit.adapters.account import AccountCheckError, validate_endpoint
from llm_kit.cli._common import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_USAGE,
    _add_endpoint_arg,
    _add_project_arg,
    guarded,
)
from llm_kit.registry.api_key import get_api_key
from llm_kit.registry.constants import USER_ENV_FILE, cli_command
from llm_kit.registry.env_file import read_env_file, write_env_file
from llm_kit.registry.models import EndpointResolveError, resolve_endpoint


def _resolve_endpoint_or_exit(endpoint: Optional[str], project_root: Optional[str] = None) -> Optional[dict]:
    try:
        return resolve_endpoint(endpoint, project_root=project_root)
    except EndpointResolveError as exc:
        print(f"Endpoint error: {exc}", file=sys.stderr)
        return None


def _root_path(project_root: Optional[str]) -> Optional[Path]:
    return Path(project_root) if project_root is not None else None


STATUS_SCHEMA = "llm-kit.account-status/v1"
"""Identifies the ``account status --json`` document; a change to its fields bumps the suffix."""

STATE_MISSING_KEY = "missing_key"
STATE_KEY_PRESENT = "key_present"
STATE_NOT_VALIDATED = "not_validated"
STATE_OK = "ok"
STATE_REJECTED_KEY = "rejected_key"
STATE_NO_CREDIT = "no_credit"
STATE_CHECK_FAILED = "check_failed"
STATES = (
    STATE_MISSING_KEY,
    STATE_KEY_PRESENT,
    STATE_NOT_VALIDATED,
    STATE_OK,
    STATE_REJECTED_KEY,
    STATE_NO_CREDIT,
    STATE_CHECK_FAILED,
)
"""Every value of the ``state`` field.

``missing_key``: no source holds a key. ``key_present``: a key resolved and the
caller asked for no validation (``--no-validate``). ``not_validated``: a key
resolved but the endpoint declares no account check. ``ok``: the endpoint
accepted the key. ``rejected_key``: HTTP 401. ``no_credit``: HTTP 402.
``check_failed``: the endpoint could not be asked (transport error or an
unexpected status), so the key's health is unknown.
"""


def _status_document(
    endpoint: Optional[str], *, validate: bool, project_root: Optional[str]
) -> dict:
    """The structured account status: one ``state`` field, never inferred from prose."""
    ep = resolve_endpoint(endpoint, project_root=project_root)
    lookup = get_api_key(_root_path(project_root), endpoint=endpoint)
    document: dict = {
        "schema": STATUS_SCHEMA,
        "state": STATE_MISSING_KEY,
        "endpoint": ep["name"],
        "base_url": ep["base_url"],
        "key_env": ep["key_env"],
        "source": lookup.source,
        "source_path": str(lookup.source_path) if lookup.source_path else None,
        "key_sha256": None,
        "label": None,
        "usage": None,
        "limit": None,
        "is_free_tier": None,
        "rate_limit": None,
        "error": None,
        "set_key_command": f"{cli_command()} account set-key",
        "user_env_file": str(USER_ENV_FILE),
    }
    if lookup.key is None:
        return document
    document["key_sha256"] = hashlib.sha256(lookup.key.encode("utf-8")).hexdigest()
    if not validate:
        document["state"] = STATE_KEY_PRESENT
        return document
    try:
        status = validate_endpoint(ep, lookup.key)
    except AccountCheckError as exc:
        document["state"] = STATE_CHECK_FAILED
        document["error"] = str(exc)
        return document
    if status is None:
        document["state"] = STATE_NOT_VALIDATED
    elif status.ok:
        document["state"] = STATE_OK
        document["label"] = status.label
        document["usage"] = status.usage
        document["limit"] = status.limit
        document["is_free_tier"] = status.is_free_tier
        document["rate_limit"] = status.rate_limit
    elif status.failure_reason == "auth":
        document["state"] = STATE_REJECTED_KEY
    elif status.failure_reason == "no_credit":
        document["state"] = STATE_NO_CREDIT
    else:
        raise ValueError(f"unrecognised account failure reason: {status.failure_reason!r}")
    return document


def _cmd_status_json(endpoint: Optional[str], *, validate: bool, project_root: Optional[str]) -> int:
    """Print the status document. Exit 0 whenever a document was produced.

    The verdict is the ``state`` field, not the exit code, so a caller never
    has to infer a credential state from a status it cannot tell apart. An
    endpoint that does not resolve is a usage error (exit 2, no document).
    """
    try:
        document = _status_document(endpoint, validate=validate, project_root=project_root)
    except EndpointResolveError as exc:
        print(f"Endpoint error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(json.dumps(document, sort_keys=True))
    return EXIT_OK


def _cmd_status(endpoint: Optional[str], *, validate: bool = True, project_root: Optional[str] = None) -> int:
    ep = _resolve_endpoint_or_exit(endpoint, project_root)
    if ep is None:
        return EXIT_USAGE
    lookup = get_api_key(_root_path(project_root), endpoint=endpoint)
    if lookup.key is None:
        print(f"No API key found for endpoint '{ep['name']}' ({ep['key_env']}).")
        return EXIT_FAILURE
    print(f"Endpoint: {ep['name']} ({ep['base_url']})")
    print(f"Source: {lookup.source}" + (f" ({lookup.source_path})" if lookup.source_path else ""))
    if not validate:
        print("Status: key present (not validated)")
        return EXIT_OK
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-kit account",
        description="Credential status, source lookup and key storage for a configured endpoint.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    status = sub.add_parser("status")
    _add_endpoint_arg(status)
    _add_project_arg(status)
    status.add_argument(
        "--json",
        action="store_true",
        help=(
            "Print one JSON document whose 'state' field is one of "
            + ", ".join(STATES)
            + ". Exit 0 whenever a document was printed."
        ),
    )
    status.add_argument(
        "--no-validate",
        action="store_true",
        help="Resolve the key and report where it came from without asking the endpoint.",
    )
    which = sub.add_parser("which")
    _add_endpoint_arg(which)
    set_key = sub.add_parser("set-key")
    _add_endpoint_arg(set_key)
    set_key.add_argument("--key")
    set_key.add_argument("--no-validate", action="store_true")
    return parser


def run(argv: Sequence[str]) -> int:
    """Run one ``account`` verb and return its exit code."""
    args = _parser().parse_args(list(argv))

    def dispatch() -> int:
        if args.cmd == "status":
            if args.json:
                return _cmd_status_json(
                    args.endpoint, validate=not args.no_validate, project_root=args.project_root
                )
            return _cmd_status(
                args.endpoint, validate=not args.no_validate, project_root=args.project_root
            )
        if args.cmd == "set-key":
            return _cmd_set_key(args.key, validate=not args.no_validate, endpoint=args.endpoint)
        if args.cmd == "which":
            return _cmd_which(args.endpoint)
        return EXIT_USAGE

    return guarded(dispatch)
