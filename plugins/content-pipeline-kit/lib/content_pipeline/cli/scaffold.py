"""Arg dispatch, scope filtering, typo did-you-mean.

The reusable core of a project's CLI facade: parses a command name plus
scope-filter arguments, dispatches to the registered per-command handler,
and offers a did-you-mean suggestion on an unrecognized command name rather
than a bare error. Per-project commands register against this scaffold
instead of each project growing its own argparse tree from scratch.
"""


def dispatch(argv: list, commands: dict) -> int:
    """Parse argv, dispatch to the matching registered command, return exit code."""
    raise NotImplementedError
