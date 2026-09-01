"""Arg dispatch, scope filtering, typo did-you-mean.

The reusable core of a project's CLI facade -- the shape both source systems
grew a multi-thousand-line monolith around. Per-project commands register a
thin handler against this scaffold instead of each project growing its own
argparse tree from scratch:

- **Subcommand registry -> thin handlers.** :func:`dispatch` looks up the first
  argv token in a ``{name: handler}`` registry and calls the handler with the
  remaining args; the handler returns a result the scaffold renders uniformly.
- **Did-you-mean on a miss.** An unrecognized command (or a scope-filter value
  that matches nothing) yields a ``difflib`` suggestion rather than a bare
  error -- the recovery affordance from the source facade.
- **Uniform YAML output + exit codes.** A handler returns any YAML-able value;
  the scaffold serializes it and maps the outcome to a stable exit code
  (0 success, 2 usage/unknown-command, 1 handler error).

Stdlib + ``pyyaml`` only; imports nothing else from ``content_pipeline`` (the
LLM ``PipelineHaltError`` handling lives in ``cli.budget``, which this scaffold's
handlers compose -- it is not wired in here).
"""

from __future__ import annotations

import difflib
import sys
from dataclasses import dataclass
from typing import Any, Callable, List, Mapping, Optional, Sequence, TextIO, Tuple

# A command handler: (remaining argv) -> a YAML-able result (or None).
Handler = Callable[[List[str]], Any]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


@dataclass(frozen=True)
class Command:
    """A registered subcommand: a handler plus a one-line help string."""

    name: str
    handler: Handler
    help: str = ""


def did_you_mean(
    name: str,
    candidates: Sequence[str],
    *,
    n: int = 5,
    cutoff: float = 0.5,
) -> List[str]:
    """Return up to ``n`` close matches for ``name`` among ``candidates``.

    A thin wrapper over ``difflib.get_close_matches`` so every did-you-mean
    site (unknown command, unknown scope value) uses one tuned cutoff.
    """
    return difflib.get_close_matches(name, list(candidates), n=n, cutoff=cutoff)


def filter_scope(
    items: Sequence[Any],
    value: Optional[str],
    *,
    match: Callable[[Any, str], bool],
    universe: Optional[Sequence[str]] = None,
    n: int = 5,
    cutoff: float = 0.5,
) -> Tuple[List[Any], List[str]]:
    """Filter ``items`` by a scope ``value``, returning ``(kept, suggestions)``.

    ``match(item, value)`` decides membership. When ``value`` is falsy, every
    item is kept (an empty scope is a legitimate "whole corpus" ask). When a
    non-empty ``value`` matches nothing AND ``universe`` (the set of valid scope
    values) is given, ``suggestions`` carries the did-you-mean list so the
    caller can surface the canonical spelling; ``kept`` is then empty. On a
    successful filter ``suggestions`` is empty.
    """
    if not value:
        return list(items), []
    kept = [item for item in items if match(item, value)]
    if kept:
        return kept, []
    suggestions = (
        did_you_mean(value, universe, n=n, cutoff=cutoff) if universe else []
    )
    return [], suggestions


def emit_yaml(result: Any) -> str:
    """Serialize a handler result to a YAML string (empty for ``None``).

    ``sort_keys=False`` preserves a handler's intended key order;
    ``allow_unicode=False`` keeps the output ASCII (this scaffold carries no
    localization payloads).
    """
    if result is None:
        return ""
    import yaml  # noqa: PLC0415

    return yaml.safe_dump(result, sort_keys=False, allow_unicode=False).rstrip("\n")


def dispatch(
    argv: Sequence[str],
    commands: Mapping[str, Any],
    *,
    out: Optional[TextIO] = None,
    err: Optional[TextIO] = None,
    render: Callable[[Any], str] = emit_yaml,
    cutoff: float = 0.5,
) -> int:
    """Dispatch ``argv[0]`` to a registered command; return an exit code.

    ``commands`` maps a name to either a bare handler callable or a
    :class:`Command`. With no argv, or an unknown command, a usage / did-you-
    mean message is written to ``err`` and :data:`EXIT_USAGE` returned. On a
    match, the handler runs with ``argv[1:]``; its rendered result is written to
    ``out`` and :data:`EXIT_OK` returned. A ``SystemExit`` from the handler
    propagates its code; any other exception writes its message to ``err`` and
    returns :data:`EXIT_ERROR`.
    """
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    argv = list(argv)

    names = sorted(commands)
    if not argv:
        err.write(f"usage: <command> [args]; commands: {', '.join(names)}\n")
        return EXIT_USAGE

    name, rest = argv[0], argv[1:]
    if name not in commands:
        err.write(f"error: unknown command {name!r}.\n")
        suggestions = did_you_mean(name, names, cutoff=cutoff)
        if suggestions:
            err.write(f"       Did you mean: {', '.join(suggestions)}?\n")
        else:
            err.write(f"       Available commands: {', '.join(names)}\n")
        return EXIT_USAGE

    entry = commands[name]
    handler = entry.handler if isinstance(entry, Command) else entry
    try:
        result = handler(rest)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else (EXIT_OK if code is None else EXIT_ERROR)
    except Exception as exc:  # noqa: BLE001 -- report and map to an exit code
        err.write(f"error: {name}: {exc}\n")
        return EXIT_ERROR

    rendered = render(result)
    if rendered:
        out.write(rendered + "\n")
    return EXIT_OK


__all__ = [
    "Handler",
    "EXIT_OK",
    "EXIT_ERROR",
    "EXIT_USAGE",
    "Command",
    "did_you_mean",
    "filter_scope",
    "emit_yaml",
    "dispatch",
]
