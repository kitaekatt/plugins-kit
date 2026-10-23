"""``llm-kit consent``: show and change the per-machine cost-class consent.

``consent show [--json]`` prints what :func:`llm_kit.registry.consent.load_consent`
reports: ``usage``, ``spend``, ``source`` (``file``, ``absent`` or ``unreadable``),
``path`` (the file read), ``write_path`` (the file ``consent set`` writes: the
``LLM_KIT_CONSENT_FILE`` override, else the neutral ``consent.json``) and ``note``. ``consent set [--usage on|off] [--spend on|off]`` is a
partial update: a flag that is not given keeps its current value. With neither
flag it changes nothing and exits 2.

Exit codes follow the scripting groups: 0 success, 2 usage or configuration
(including a consent file that cannot be written).

The CLI only reads and writes the consent file. Which operations consult it, and
whether they opt in, is decided by their callers.
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
    "With usage or spend on, operations that opt in send their inputs unredacted "
    "to the providers of those models."
)


def _flag(value: bool) -> str:
    return "on" if value else "off"


def _cmd_show(as_json: bool) -> int:
    loaded = consent_store.load_consent()
    if as_json:
        _json({
            "usage": loaded.usage,
            "spend": loaded.spend,
            "source": loaded.source,
            "path": str(loaded.path),
            "write_path": str(consent_store.default_consent_path()),
            "note": loaded.note,
        })
        return EXIT_OK
    print(f"usage: {_flag(loaded.usage)}")
    print(f"spend: {_flag(loaded.spend)}")
    print(f"source: {loaded.source}")
    print(f"path: {loaded.path}")
    print(f"write_path: {consent_store.default_consent_path()}")
    print(f"note: {loaded.note}")
    return EXIT_OK


def _cmd_set(usage: Optional[str], spend: Optional[str]) -> int:
    if usage is None and spend is None:
        print("llm-kit consent set: give --usage on|off, --spend on|off, or both.", file=sys.stderr)
        return EXIT_USAGE
    current = consent_store.load_consent()
    new_usage = current.usage if usage is None else usage == "on"
    new_spend = current.spend if spend is None else spend == "on"
    written = consent_store.save_consent(usage=new_usage, spend=new_spend)
    print(f"Consent written to {written}")
    print(f"usage: {_flag(new_usage)}")
    print(f"spend: {_flag(new_spend)}")
    print(UNREDACTED_NOTICE)
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-kit consent",
        description=(
            "Show or change which non-free cost classes (usage, spend) callers that opt in "
            "may use on this machine. Free is always allowed."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    show = sub.add_parser("show", help="Print the current consent.")
    show.add_argument("--json", action="store_true", help="Print one JSON object.")
    setter = sub.add_parser("set", help="Change consent; a flag not given keeps its value.")
    setter.add_argument("--usage", choices=("on", "off"), help="Allow subscription-usage operations.")
    setter.add_argument("--spend", choices=("on", "off"), help="Allow metered-spend operations.")
    return parser


def run(argv: Sequence[str]) -> int:
    """Run one ``consent`` verb and return its exit code."""
    args = _parser().parse_args(list(argv))

    def dispatch() -> int:
        if args.cmd == "show":
            return _cmd_show(args.json)
        if args.cmd == "set":
            return _cmd_set(args.usage, args.spend)
        return EXIT_USAGE

    return guarded(dispatch)
