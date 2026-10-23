"""``llm-kit pipeline``: drive a leased pipeline run's ledger from the command line.

Mounts the store-only command set of :func:`llm_kit.cli.run.build_commands`
(``create-run``, ``register-units``, ``claim``, ``renew``, ``accept``, ``fail``,
``halt``, ``clear-halt`` and ``status``) on the execution ledger named by
``--store``, and renders each result through :func:`llm_kit.scaffold.dispatch`
(YAML on stdout; exit 0 ok, 1 error, 2 usage).

The legacy content-pipeline library shipped these adapters without an entry
point: a consumer wired its own. This group is that wiring for the commands that
need nothing beyond a ledger. The ``protocol`` command needs a consumer's run
adapter and handlers, so it stays a library seam: a consumer mount calls
``build_commands(store, adapter=...)`` itself.
"""
from __future__ import annotations

import argparse
from typing import Sequence

from llm_kit.cli.run import build_commands
from llm_kit.pipeline.execution.store import ExecutionStore
from llm_kit.scaffold import dispatch

_COMMANDS = (
    "create-run", "register-units", "claim", "renew", "accept", "fail", "halt", "clear-halt", "status",
)


def _unreachable(args: Sequence[str]) -> None:
    raise AssertionError("dispatch reached a command it was not given")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-kit pipeline",
        description=(
            "Drive a leased pipeline run's execution ledger. Name the ledger with "
            "--store, before the command; everything after the command is that "
            "command's own arguments (positionals plus --key=value flags)."
        ),
        epilog="commands: " + ", ".join(_COMMANDS),
    )
    parser.add_argument(
        "--store",
        required=True,
        help="Path of the execution ledger (a SQLite file; created on first use).",
    )
    parser.add_argument(
        "command_args",
        nargs=argparse.REMAINDER,
        metavar="command [args...]",
        help="The command to run and its arguments.",
    )
    return parser


def run(argv: Sequence[str]) -> int:
    """Run one ``pipeline`` command and return its exit code."""
    args = _parser().parse_args(list(argv))
    if not args.command_args or args.command_args[0] not in _COMMANDS:
        # No command or a misspelt one: let dispatch print the usage and the
        # did-you-mean list, and do not open (and so create) the ledger first.
        names = {name: _unreachable for name in _COMMANDS}
        return dispatch(args.command_args, names)
    store = ExecutionStore(args.store)
    return dispatch(args.command_args, build_commands(store))


__all__ = ["run"]
