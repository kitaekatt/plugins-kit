"""Claude CLI executable selection -- the single source of truth.

``LLM_KIT_CLAUDE_EXECUTABLE`` names the Claude CLI to run instead of ``claude``
on PATH, with the same contract as ``LLM_KIT_CODEX_EXECUTABLE``
(:mod:`llm_kit.adapters.codex_command`): a bare command name (looked up on PATH)
or an ABSOLUTE path to an executable file. Every place that starts a ``claude``
process resolves through this module -- the completion backend, the auth probe,
the reachability probe and the background-session driver -- so a verdict and a
dispatch always name the same binary.

Unset, the behaviour is unchanged: ``claude`` on PATH. Set to something that
cannot be run, it raises :class:`ClaudeExecutableError` naming the variable
instead of falling back to PATH: a user who named a specific build must never
be served a different one in silence. The module is standard library only (plus
:func:`llm_kit.adapters.codex_command.resolve_cli`), so a plain
``pip install katsoft-llm-kit`` user reaches it without any AI harness.
"""

from __future__ import annotations

import os
import shutil
from typing import Mapping, Optional, Tuple

from llm_kit.adapters.codex_command import resolve_cli

CLAUDE_EXECUTABLE = "claude"

#: Names the Claude CLI to run instead of ``claude`` on PATH: a bare command
#: name (looked up on PATH) or an ABSOLUTE path to an executable file. Set it
#: when more than one Claude build is installed and the one on PATH is not the
#: one to use.
CLAUDE_EXECUTABLE_ENV = "LLM_KIT_CLAUDE_EXECUTABLE"


class ClaudeExecutableError(RuntimeError):
    """``LLM_KIT_CLAUDE_EXECUTABLE`` is set to something that cannot be run."""


def configured_claude_executable(env: Optional[Mapping[str, str]] = None) -> str:
    """The Claude executable to resolve: the variable's value, else ``claude``.

    ``env`` defaults to ``os.environ``. Raises :class:`ClaudeExecutableError`
    when ``LLM_KIT_CLAUDE_EXECUTABLE`` is set but empty, or holds a relative
    path (a name with a directory part that is not absolute): the launcher runs
    from a different working directory than the one the user typed it in, so a
    relative path would resolve somewhere else. A bare command name stays valid.
    """
    source = os.environ if env is None else env
    raw = source.get(CLAUDE_EXECUTABLE_ENV)
    if raw is None:
        return CLAUDE_EXECUTABLE
    value = raw.strip()
    if not value:
        raise ClaudeExecutableError(
            "%s is set but empty; unset it to use `%s` from PATH, or set it to "
            "a command name or an absolute path to the Claude executable"
            % (CLAUDE_EXECUTABLE_ENV, CLAUDE_EXECUTABLE)
        )
    has_dir = os.sep in value or (os.altsep is not None and os.altsep in value)
    if has_dir and not os.path.isabs(value):
        raise ClaudeExecutableError(
            "%s=%r is a relative path; use an absolute path or a bare command "
            "name (the Claude launcher runs from a different working directory)"
            % (CLAUDE_EXECUTABLE_ENV, value)
        )
    return value


def _unrunnable(name: str) -> ClaudeExecutableError:
    return ClaudeExecutableError(
        "%s=%r is not an executable file and not a command on PATH"
        % (CLAUDE_EXECUTABLE_ENV, name)
    )


def resolve_claude_executable(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The configured Claude launcher as one path string, or None.

    With ``LLM_KIT_CLAUDE_EXECUTABLE`` unset this is ``shutil.which("claude")``:
    None when ``claude`` is not on PATH. With it set, an unresolvable value
    raises :class:`ClaudeExecutableError` naming the variable rather than
    returning None, so a misconfiguration cannot read as "Claude is not
    installed".
    """
    source = os.environ if env is None else env
    name = configured_claude_executable(source)
    resolved = shutil.which(name)
    if resolved is None and CLAUDE_EXECUTABLE_ENV in source:
        raise _unrunnable(name)
    return resolved


def resolve_claude_cli(env: Optional[Mapping[str, str]] = None) -> Optional[Tuple[str, ...]]:
    """The configured Claude launcher as an argv prefix (Windows batch shims
    wrapped in ``cmd /c``), or None; same failure contract as
    :func:`resolve_claude_executable`."""
    source = os.environ if env is None else env
    name = configured_claude_executable(source)
    prefix = resolve_cli(name)
    if prefix is None and CLAUDE_EXECUTABLE_ENV in source:
        raise _unrunnable(name)
    return prefix
