"""claude-cli text-only mode: argv and record.

The mode removes every tool, MCP server and skill and drops the permission
bypass. Arming and selection are in test_text_only_arming.py, codex in
test_text_only_codex.py. No real model runs: the backend is driven through its
runner seam and the assertions read the argv it built.
"""
from __future__ import annotations

import json

import pytest

from llm_scripting_kit.completion.adapter_capabilities import (
    _CLAUDE_TEXT_ONLY_ARGS,
    CLAUDE_CAPABILITIES,
)
from llm_scripting_kit.completion.backends import ClaudeCliBackend
from llm_scripting_kit.completion.capabilities import (
    DENY,
    FILESYSTEM_WRITE,
    REQUEST,
    SHELL_EXEC,
    SUBAGENT_SPAWN,
    TEXT_ONLY_MODE,
    TEXT_ONLY_PARAMETER,
)
from llm_scripting_kit.completion.types import BackendOptions


# -- seams -----------------------------------------------------------------


class _ClaudeRunner:
    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, cmd, request, cwd, **kwargs):
        self.calls.append(list(cmd))
        return json.dumps({"result": "ok", "is_error": False, "usage": {}}), "", 0


def _claude(text_only: bool = False):
    runner = _ClaudeRunner()
    return ClaudeCliBackend(runner=runner, executable="claude", text_only=text_only), runner


def _contains_run(argv, run) -> bool:
    run = list(run)
    return any(argv[i : i + len(run)] == run for i in range(len(argv) - len(run) + 1))


# -- claude argv -----------------------------------------------------------


def test_claude_text_only_argv_replaces_the_bypass_and_the_allow_list():
    backend, runner = _claude(text_only=True)
    response = backend.complete("sys", "usr", model="m")
    argv = runner.calls[0]
    assert argv[-len(_CLAUDE_TEXT_ONLY_ARGS):] == list(_CLAUDE_TEXT_ONLY_ARGS)
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "--strict-mcp-config" in argv and "--disable-slash-commands" in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "bypassPermissions" not in argv
    assert "--allowedTools" not in argv
    assert response.execution_controls_applied == ("no-session-persistence", "text-only-mode")


def test_claude_default_argv_is_unchanged():
    """A caller that requires nothing gets the pre-existing argv exactly."""
    backend, runner = _claude()
    response = backend.complete("sys", "usr", model="m")
    assert runner.calls[0] == [
        "claude", "-p", "--model", "m", "--system-prompt", "sys",
        "--output-format", "json", "--no-session-persistence",
        "--permission-mode", "bypassPermissions", "--allowedTools", "",
    ]
    assert response.execution_controls_applied == (
        "allowed-tools", "permission-bypass", "no-session-persistence",
    )


def test_claude_text_only_keeps_a_caller_deny_list_and_effort():
    backend, runner = _claude(text_only=True)
    response = backend.complete(
        "sys", "usr", model="m", options=BackendOptions(disallowed_tools="Bash", effort="low")
    )
    argv = runner.calls[0]
    assert argv[argv.index("--disallowedTools") + 1] == "Bash"
    assert argv[argv.index("--effort") + 1] == "low"
    assert "disallowed-tools" in response.execution_controls_applied


def test_claude_text_only_refuses_an_allow_list_before_dispatch():
    backend, runner = _claude(text_only=True)
    with pytest.raises(ValueError, match="allowed_tools"):
        backend.complete("sys", "usr", model="m", options=BackendOptions(allowed_tools="Read"))
    assert runner.calls == []


def test_claude_text_only_accepts_an_empty_allow_list():
    backend, runner = _claude(text_only=True)
    backend.complete("sys", "usr", model="m", options=BackendOptions(allowed_tools=""))
    assert "--allowedTools" not in runner.calls[0]


# -- claude record ---------------------------------------------------------


def _control(record, control_id):
    return next(c for c in record.execution_controls if c.id == control_id)


def test_claude_record_declares_the_three_guarantees_for_text_only_mode():
    control = _control(CLAUDE_CAPABILITIES, TEXT_ONLY_MODE)
    assert control.effect == DENY
    assert control.subjects == (FILESYSTEM_WRITE, SHELL_EXEC, SUBAGENT_SPAWN)
    assert control.source == REQUEST
    assert control.parameter == TEXT_ONLY_PARAMETER
    assert control.when_value == "true"
    assert control.emits == (
        '--tools "" --strict-mcp-config --mcp-config {"mcpServers":{}} '
        "--disable-slash-commands --permission-mode dontAsk"
    )
    param = CLAUDE_CAPABILITIES.params[TEXT_ONLY_PARAMETER]
    assert param.type == "boolean" and param.default is False
    assert param.emits == control.emits
    # Structural guarantees stay empty: the mode is a flag set, not an absence.
    assert CLAUDE_CAPABILITIES.guarantees == ()


def test_claude_permission_bypass_is_advertised_as_absent_in_text_only_mode():
    control = _control(CLAUDE_CAPABILITIES, "permission-bypass")
    assert control.source == REQUEST
    assert control.parameter == TEXT_ONLY_PARAMETER
    assert control.when_value == "false"


def test_claude_text_only_emission_is_what_the_argv_carries():
    backend, runner = _claude(text_only=True)
    backend.complete("sys", "usr", model="m")
    assert _contains_run(runner.calls[0], _CLAUDE_TEXT_ONLY_ARGS)
