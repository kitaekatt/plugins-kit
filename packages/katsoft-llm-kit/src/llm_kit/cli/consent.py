"""``llm-kit consent``: show and change the optional consent layer for cost classes.

The layer is OPTIONAL. The library makes no LLM calls on its own and never
consults this file unless a caller opts in: protocol 2 request field ``consent``,
``llm-kit models describe --consent [--consent-scope X]``, or the API call
:func:`llm_kit.registry.consent.policy`. A caller may build its own layer instead.
The cost class of an entry is information that is always reported; this layer is
the policy a caller may apply to it.

``consent show [--scope X] [--json]`` prints the effective policy: per class
(``local``, ``subscription``, ``metered``) whether it is allowed and which layer
decided it (``default``, ``global`` or ``scope``). A scope ``on`` that the global
policy blocks is shown as blocked, with the global command that lifts the block.
``path`` is the file read, ``write_path`` the file ``consent set`` writes (the
``LLM_KIT_CONSENT_FILE`` override, else the neutral ``consent.json``).

``consent set [--scope X] [--local on|off] [--subscription on|off] [--metered on|off]``
is a partial update: a flag that is not given keeps its value, scopes are
preserved by a global change, and a scope can only restrict the global policy.
With no class flag it changes nothing and exits 2.

Exit codes follow the scripting groups: 0 success, 2 usage or configuration
(including a consent file that cannot be read, is malformed, or cannot be written).
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from llm_kit.cli._common import EXIT_OK, EXIT_USAGE, _json, guarded
from llm_kit.registry import consent as consent_store

#: Printed by ``consent set`` every time, so the effect of switching a class on
#: is never left implicit.
UNREDACTED_NOTICE = (
    "With subscription or metered on, operations that opt in send their inputs unredacted "
    "to the providers of those models."
)

OPTIONAL_NOTICE = (
    "The consent layer is optional: nothing applies it unless a caller opts in "
    "(protocol 2 \"consent\", `llm-kit models describe --consent`, "
    "llm_kit.registry.consent.policy())."
)


def _flag(value: bool) -> str:
    return "on" if value else "off"


def _class_line(policy: consent_store.Policy, cost_class: str) -> str:
    state = policy.classes[cost_class]
    line = f"{cost_class}: {_flag(state.allowed)} ({state.provenance})"
    if state.blocked_by_global:
        line += (
            f"; scope {policy.scope!r} states it on but the global policy blocks it: "
            f"run `{state.commands[0]}`"
        )
    elif not state.allowed:
        line += "; to allow: " + " and ".join(f"`{c}`" for c in state.commands)
    return line


def _print_policy(policy: consent_store.Policy) -> None:
    print(f"scope: {policy.scope if policy.scope is not None else '(global)'}")
    print(f"source: {policy.source}")
    print(f"path: {policy.path}")
    print(f"write_path: {consent_store.default_consent_path()}")
    for cost_class in consent_store.CLASSES:
        print(_class_line(policy, cost_class))


def _cmd_show(scope: Optional[str], as_json: bool) -> int:
    policy = consent_store.policy(scope)
    if as_json:
        document = policy.to_json()
        document["write_path"] = str(consent_store.default_consent_path())
        document["layer"] = consent_store.layer_description()
        _json(document)
        return EXIT_OK
    _print_policy(policy)
    print(OPTIONAL_NOTICE)
    return EXIT_OK


def _cmd_set(scope: Optional[str], changes: "dict[str, bool]") -> int:
    if not changes:
        print(
            "llm-kit consent set: give --local, --subscription or --metered, each on|off.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    written, config = consent_store.update_config(changes, scope=scope)
    print(f"Consent written to {written}")
    _print_policy(consent_store.effective(config, scope))
    print(UNREDACTED_NOTICE)
    print(OPTIONAL_NOTICE)
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-kit consent",
        description=(
            "Show or change the optional consent layer: which cost classes (local, subscription, "
            "metered) callers that opt in may use on this machine. Nothing applies it unless a "
            "caller opts in."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    show = sub.add_parser("show", help="Print the effective policy and where each value comes from.")
    show.add_argument("--scope", help="Show the named scope's effective policy (default: the global policy).")
    show.add_argument("--json", action="store_true", help="Print one JSON object.")
    setter = sub.add_parser("set", help="Change the policy; a flag not given keeps its value.")
    setter.add_argument("--scope", help="Change the named scope instead of the global policy (a scope can only restrict).")
    setter.add_argument("--local", choices=("on", "off"), help="Allow local (declared unmetered and self-owned endpoint) operations.")
    setter.add_argument("--subscription", choices=("on", "off"), help="Allow subscription (claude or codex session pool) operations.")
    setter.add_argument("--metered", choices=("on", "off"), help="Allow metered (paid or unverified billing) operations.")
    return parser


def run(argv: Sequence[str]) -> int:
    """Run one ``consent`` verb and return its exit code."""
    args = _parser().parse_args(list(argv))

    def dispatch() -> int:
        if args.cmd == "show":
            return _cmd_show(args.scope, args.json)
        if args.cmd == "set":
            changes = {
                name: getattr(args, name) == "on"
                for name in consent_store.CLASSES
                if getattr(args, name) is not None
            }
            return _cmd_set(args.scope, changes)
        return EXIT_USAGE

    return guarded(dispatch)
