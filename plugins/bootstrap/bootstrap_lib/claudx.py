"""claudx session propagation for nested ``claude`` children.

``scripts/claude_plugin_test.py`` (claudx) launches a session that loads the
working copy through ``--plugin-dir`` and redirects bootstrap's data root. It
exports three variables describing that session:

- ``CLAUDE_PLUGIN_TEST``: ``1``.
- ``CLAUDE_BOOTSTRAP_DATA_ROOT``: the redirected data root.
- ``CLAUDE_PLUGIN_TEST_DIRS``: the absolute plugin directories passed as
  ``--plugin-dir``, joined with :data:`os.pathsep`, in argument order.

A plugin that starts another ``claude`` session as a direct child (``claude -p``,
an llm-kit job contract command) must make that session a claudx session too. The
child inherits the environment, but ``--plugin-dir`` is a command-line flag, so
without it the child loads the PUBLISHED plugins against the redirected data
root. :func:`nested_claude_args` returns the flags.

Design: VALIDATE, THEN DERIVE FROM THE PARENT. The child environment is compared
with the parent's (``os.environ``) on all three variables and any difference
raises. A child env that kept a claudx-looking but different directory list is
refused, not honoured, so a caller-supplied env cannot point the child at other
plugins. The returned flags are built from the parent's list.

A supervised session (``claude --bg``, ``claude respawn``) runs under a
background supervisor whose environment inheritance is not established, so a
child there could load plugins without the redirect. :func:`refuse_supervised`
raises under claudx for those.

Outside claudx (neither side has ``CLAUDE_PLUGIN_TEST``) every function returns
the input unchanged and nothing is read or written.

Callers import this module lazily, only when ``CLAUDE_PLUGIN_TEST`` is set, so
production code paths never depend on it and ``requires_bootstrap`` floors do
not move.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional, Sequence

TEST_FLAG = "CLAUDE_PLUGIN_TEST"
DATA_ROOT = "CLAUDE_BOOTSTRAP_DATA_ROOT"
PLUGIN_DIRS = "CLAUDE_PLUGIN_TEST_DIRS"
PINNED_VARIABLES = (TEST_FLAG, DATA_ROOT, PLUGIN_DIRS)

# Invocations that start no session: a nested child needs no plugin flags.
_PROBE_FLAGS = frozenset({"--version", "-v", "--help", "-h"})
_SESSIONLESS_SUBCOMMANDS = frozenset(
    {"plugin", "plugins", "agents", "mcp", "config", "doctor", "update",
     "install", "stop", "logs", "rm", "setup-token", "auth"}
)
# Invocations that start a supervised session: refused under claudx.
_SUPERVISED_TOKENS = frozenset({"--bg", "respawn"})
_EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat")


class ClaudxPropagationError(RuntimeError):
    """A nested claude child cannot be made a faithful claudx session."""


def _value(env: Mapping[str, str], name: str) -> Optional[str]:
    raw = env.get(name)
    return raw if raw else None


def in_claudx(env: Optional[Mapping[str, str]] = None) -> bool:
    """True when ``env`` (default ``os.environ``) marks a claudx session."""
    return _value(os.environ if env is None else env, TEST_FLAG) is not None


def _validated_dirs(parent: Mapping[str, str]) -> list[str]:
    root = _value(parent, DATA_ROOT)
    if root is None:
        raise ClaudxPropagationError(
            f"{TEST_FLAG} is set but {DATA_ROOT} is not: a claudx session without "
            "the redirected data root would run a dev engine against the "
            "production data root. Nothing was started."
        )
    raw = _value(parent, PLUGIN_DIRS)
    if raw is None:
        raise ClaudxPropagationError(
            f"{TEST_FLAG} is set but {PLUGIN_DIRS} is not: the plugin directories "
            "of this claudx session are unknown, so a nested claude child cannot "
            "load them. Launch the session with scripts/claude_plugin_test.py. "
            "Nothing was started."
        )
    dirs = raw.split(os.pathsep)
    for entry in dirs:
        if not entry:
            raise ClaudxPropagationError(
                f"{PLUGIN_DIRS} has an empty entry: {raw!r}. Nothing was started."
            )
        if not os.path.isabs(entry):
            raise ClaudxPropagationError(
                f"{PLUGIN_DIRS} entry is not an absolute path: {entry!r}. "
                "Nothing was started."
            )
        if not (Path(entry) / ".claude-plugin" / "plugin.json").is_file():
            raise ClaudxPropagationError(
                f"{PLUGIN_DIRS} entry has no .claude-plugin/plugin.json: {entry!r}. "
                "Nothing was started."
            )
    return dirs


def nested_claude_args(
    child_env: Optional[Mapping[str, str]] = None,
    *,
    parent_env: Optional[Mapping[str, str]] = None,
) -> list[str]:
    """The ``--plugin-dir`` flags for a direct ``claude`` child.

    ``child_env`` is the environment the child will run with; ``None`` means it
    inherits the parent's. ``parent_env`` defaults to ``os.environ``.

    Returns ``[]`` when neither environment marks a claudx session. Otherwise
    raises :class:`ClaudxPropagationError` unless all of the following hold:

    - the child's ``CLAUDE_PLUGIN_TEST``, ``CLAUDE_BOOTSTRAP_DATA_ROOT`` and
      ``CLAUDE_PLUGIN_TEST_DIRS`` each equal the parent's (an absent or empty
      value equals only another absent or empty value);
    - the parent has a data root and a plugin-directory list, and every listed
      directory is absolute and holds ``.claude-plugin/plugin.json``.

    Then returns ``["--plugin-dir", d1, "--plugin-dir", d2, ...]`` built from
    the parent's list.
    """
    parent = os.environ if parent_env is None else parent_env
    child = parent if child_env is None else child_env
    if not in_claudx(parent) and not in_claudx(child):
        return []
    for name in PINNED_VARIABLES:
        if _value(parent, name) != _value(child, name):
            raise ClaudxPropagationError(
                f"the child environment differs from the claudx parent on {name}: "
                f"parent={_value(parent, name)!r}, child={_value(child, name)!r}. "
                "A child with another value would load other plugins or another "
                "data root than the session under test. Nothing was started."
            )
    args: list[str] = []
    for entry in _validated_dirs(parent):
        args.extend(["--plugin-dir", entry])
    return args


def refuse_supervised(
    what: str,
    child_env: Optional[Mapping[str, str]] = None,
    *,
    parent_env: Optional[Mapping[str, str]] = None,
) -> None:
    """Raise when a claudx session would start a supervised ``claude`` session.

    ``what`` names the invocation for the message. Returns ``None`` outside
    claudx.
    """
    parent = os.environ if parent_env is None else parent_env
    child = parent if child_env is None else child_env
    if in_claudx(parent) or in_claudx(child):
        raise ClaudxPropagationError(
            f"{what} starts a supervised claude session, which this claudx "
            f"session ({TEST_FLAG} is set) cannot make load the plugins under "
            "test: the background supervisor's environment and plugin flags are "
            "not carried over. Nothing was started. Run the workflow with a "
            "direct child (claude -p) or outside claudx."
        )


def is_claude_executable(argv0: str) -> bool:
    """True when ``argv0`` names the ``claude`` CLI (any directory, .exe/.cmd/.bat)."""
    name = argv0.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in _EXECUTABLE_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name == "claude"


def contract_command(
    command: Sequence[str],
    child_env: Optional[Mapping[str, str]] = None,
    *,
    parent_env: Optional[Mapping[str, str]] = None,
) -> tuple[str, ...]:
    """An llm-kit job contract command, made claudx-faithful when it runs ``claude``.

    Outside claudx, or when ``command[0]`` is not the claude CLI, or when the
    invocation starts no session (``--version``, ``--help``, a management
    subcommand), returns the command unchanged. A supervised invocation
    (``--bg``, ``respawn``) raises under claudx. Any other claude invocation
    gets :func:`nested_claude_args` inserted right after the executable.
    """
    argv = tuple(command)
    parent = os.environ if parent_env is None else parent_env
    child = parent if child_env is None else child_env
    if not argv or not (in_claudx(parent) or in_claudx(child)):
        return argv
    if not is_claude_executable(argv[0]):
        return argv
    rest = argv[1:]
    if any(token in _SUPERVISED_TOKENS for token in rest):
        refuse_supervised(
            f"contract command {' '.join(argv)!r}", child_env, parent_env=parent_env
        )
    if any(token in _PROBE_FLAGS for token in rest):
        return argv
    if rest and rest[0] in _SESSIONLESS_SUBCOMMANDS:
        return argv
    flags = nested_claude_args(child_env, parent_env=parent_env)
    return (argv[0], *flags, *rest)
