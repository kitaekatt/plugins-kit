"""``llm-kit``: the single command-line entry point (``llm-kit = llm_kit.cli:main``).

The CLI is the composition root. It parses argv, picks a group, composes the
concrete collaborators a group needs (the configured backend factory for jobs,
registry-resolved credentials for the account adapter) and formats output. No
other component builds an adapter or reads argv.

Groups, and where each legacy command now lives:

``account``     llm-scripting-kit ``status``, ``which``, ``set-key``
``completion``  llm-scripting-kit ``complete``, ``request-schema``, ``repair-json``
``models``      llm-scripting-kit ``endpoints``, ``probe``, ``usage``,
                ``record-halt``, ``describe``, ``seats``, ``models`` (now
                ``list``), ``resolve``
``jobs``        job-kit ``run``, ``status``, ``resume``, ``resolve``, ``events``,
                ``gc``
``pipeline``    the content-pipeline command adapters (``create-run``,
                ``register-units``, ``claim``, ``renew``, ``accept``, ``fail``,
                ``halt``, ``clear-halt``, ``status``) mounted on a ledger
``frontdoor``   llm-scripting-kit ``frontdoor`` (every argument is the front
                door's own)
``local``       llm-scripting-kit ``swapper`` and ``acceptance``
``consent``     per-machine consent to the usage and spend cost classes

Each carried verb keeps its arguments, output and exit codes. The exit-code
tables differ by group because the legacy commands differed (the scripting
groups use 2 usage, 3 halt, 4 protocol, 5 indeterminate; ``jobs`` uses 3 for a
runner failure and 4 for a waiting job); each group's module documents its own.

The frozen legacy executables are unchanged and keep working; this command does
not delegate to them and they do not delegate to it.
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from llm_kit import _runtime
from llm_kit.bootstrap_guard import reexec_under_plugin_venv

EXIT_USAGE = 2

#: group name -> one-line help (also the complete, ordered list of groups).
GROUPS = {
    "account": "Credential status, source lookup and key storage.",
    "completion": "One configured completion, the request schema and JSON repair.",
    "models": "Configured endpoints, reachability, pacing verdicts and routing.",
    "jobs": "Run, inspect, resume and resolve durable agent jobs.",
    "pipeline": "Drive a leased pipeline run's execution ledger.",
    "frontdoor": "Run the OpenAI-compatible fill-first front door.",
    "local": "The local llama-swap model swapper and acceptance runs.",
    "consent": "Show or change which cost classes callers that opt in may use.",
}

#: legacy command (llm-scripting-kit, job-kit, content-pipeline adapters) -> its
#: llm-kit path. Used for the migration hint, for ``--help`` and by the tests that
#: prove every carried verb is registered.
LEGACY_VERBS = {
    "status": "account status",
    "which": "account which",
    "set-key": "account set-key",
    "complete": "completion complete",
    "request-schema": "completion request-schema",
    "repair-json": "completion repair-json",
    "endpoints": "models endpoints",
    "probe": "models probe",
    "usage": "models usage",
    "record-halt": "models record-halt",
    "describe": "models describe",
    "seats": "models seats",
    "models": "models list",
    "resolve": "models resolve",
    "swapper": "local swapper",
    "acceptance": "local acceptance",
    "run": "jobs run",
    "resume": "jobs resume",
    "events": "jobs events",
    "gc": "jobs gc",
}

# Verbs two legacy commands share by name, so a bare legacy verb is ambiguous:
# job-kit `status` / `resolve` against llm-scripting-kit `status` / `resolve`.
_AMBIGUOUS = {
    "status": ("account status", "jobs status", "pipeline status"),
    "resolve": ("models resolve", "jobs resolve"),
}


def _epilog() -> str:
    lines = ["legacy command -> llm-kit path:"]
    width = max(len(name) for name in LEGACY_VERBS)
    for name, target in LEGACY_VERBS.items():
        lines.append(f"  {name.ljust(width)}  llm-kit {target}")
    lines.append("  frontdoor  llm-kit frontdoor")
    return "\n".join(lines)


def _top_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-kit",
        description="LLM access, durable agent jobs and batch content pipelines.",
        epilog=_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="group", required=True, metavar="group")
    for name, text in GROUPS.items():
        sub.add_parser(name, help=text, add_help=False)
    return parser


def _migration_hint(verb: str) -> Optional[str]:
    options = _AMBIGUOUS.get(verb)
    if options is not None:
        return f"'{verb}' is in more than one group now: " + ", ".join(f"llm-kit {o}" for o in options)
    target = LEGACY_VERBS.get(verb)
    if target is not None:
        return f"'{verb}' is now 'llm-kit {target}'"
    return None


def _run_group(group: str, rest: Sequence[str]) -> int:
    if group == "account":
        from llm_kit.cli.account import run  # noqa: PLC0415
    elif group == "completion":
        from llm_kit.cli.completion import run  # noqa: PLC0415
    elif group == "models":
        from llm_kit.cli.models import run  # noqa: PLC0415
    elif group == "jobs":
        from llm_kit.cli.jobs import run  # noqa: PLC0415
    elif group == "pipeline":
        from llm_kit.cli.pipeline import run  # noqa: PLC0415
    elif group == "local":
        from llm_kit.cli.local import run  # noqa: PLC0415
    elif group == "consent":
        from llm_kit.cli.consent import run  # noqa: PLC0415
    elif group == "frontdoor":
        # Handed straight to the front door's own parser: argparse.REMAINDER on
        # a subparser's sole positional drops a leading option (`frontdoor
        # --check` would not parse).
        from llm_kit.frontdoor import main as run  # noqa: PLC0415
    else:
        raise AssertionError(f"unregistered group: {group}")
    return run(list(rest))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the CLI and return its exit status.

    With ``argv`` omitted (the console-script and launcher path) the plugin's
    copy first moves into the plugin's provisioned venv, or exits 2 under a
    redirected data root whose venv is missing. The installed package stays in
    the environment it was started in: it has no plugin venv to move into.
    An explicit ``argv`` is a library call and stays in the caller's interpreter.
    """
    if argv is None and _runtime.mode() == "legacy":
        # reexec_under_plugin_venv re-executes ``[target, *sys.argv]`` and
        # treats argv[0] as a script path, but argv[0] here is a console-script
        # wrapper (an .exe on Windows) or a launcher entry that Python cannot
        # run as source. Hand it the module form instead, so the replacement
        # runs ``python -m llm_kit.cli <args>``.
        entry_argv = list(sys.argv)
        sys.argv = ["-m", "llm_kit.cli", *entry_argv[1:]]
        try:
            reexec_under_plugin_venv("llm-kit")
        finally:
            sys.argv = entry_argv
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in GROUPS:
        return _run_group(raw[0], raw[1:])
    if raw and not raw[0].startswith("-"):
        hint = _migration_hint(raw[0])
        if hint is not None:
            print(f"llm-kit: {hint}", file=sys.stderr)
            return EXIT_USAGE
    _top_parser().parse_args(raw)
    return EXIT_USAGE


__all__ = ["GROUPS", "LEGACY_VERBS", "main"]
