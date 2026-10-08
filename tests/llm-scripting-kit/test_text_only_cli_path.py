"""Text-only mode through the REAL ``complete --requirements`` path.

The CLI, ``declaration.run``, ``describe``, the core entries and the real
``create_backend`` all run unchanged. Only the subprocess layer is faked: each
harness backend the factory builds gets a recording runner, and the
reachability probe (a ``--version`` subprocess) reports reachable. This is the
path a caller uses, and the one the CLI's recording proxy sits on.
"""
from __future__ import annotations

import functools
import io
import json
from pathlib import Path

import pytest

from llm_scripting_kit import cli
from llm_scripting_kit import declaration as decl
from llm_scripting_kit.completion import factory
from llm_scripting_kit.completion.adapter_capabilities import _CLAUDE_TEXT_ONLY_ARGS
from llm_scripting_kit.completion.backends import ClaudeCliBackend
from llm_scripting_kit.completion.codex_backend import CodexCliBackend
from llm_scripting_kit.reachability import STATUS_REACHABLE, Reachability

GUARANTEES = json.dumps({"guarantees": ["filesystem-write", "shell-exec", "subagent-spawn"]})


class _Runner:
    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, cmd, request, cwd, **kwargs):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[1:3] == ["debug", "models"]:
            model = self.model
            return json.dumps({"models": [{"slug": model, "tool_mode": "code_mode_only"}]}), "", 0
        if "-o" in cmd:  # codex exec: the answer goes to the -o file
            Path(cmd[cmd.index("-o") + 1]).write_text("NO_TOOLS", encoding="utf-8")
            return "", "tokens used: 10", 0
        return json.dumps({"result": "NO_TOOLS", "is_error": False, "usage": {}}), "", 0


@pytest.fixture
def runners(monkeypatch, tmp_path):
    claude, codex = _Runner(), _Runner()
    monkeypatch.setattr(
        factory, "ClaudeCliBackend",
        functools.partial(ClaudeCliBackend, runner=claude, executable="claude"),
    )
    monkeypatch.setattr(
        factory, "CodexCliBackend",
        functools.partial(CodexCliBackend, runner=codex, argv_prefix=("codex",)),
    )

    def reachable(entries, **_kw):
        return {name: Reachability(status=STATUS_REACHABLE, checked="t", detail="ok")
                for name in entries}

    monkeypatch.setattr(decl, "check_many", reachable)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    return claude, codex


def _complete(capsys, tmp_path, model, *extra):
    code = cli.main([
        "complete", "--models", model, *extra, "--prompt", "Reply NO_TOOLS",
        "--cwd", str(tmp_path), "--format", "json",
    ])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_claude_runs_text_only_through_the_cli(runners, capsys, tmp_path):
    claude, _ = runners
    code, out, err = _complete(capsys, tmp_path, "haiku", "--requirements", GUARANTEES)
    assert code == 0, err + out
    env = json.loads(out)
    assert env["response"]["text"] == "NO_TOOLS"
    argv = claude.calls[0]
    assert any(argv[i:i + len(_CLAUDE_TEXT_ONLY_ARGS)] == list(_CLAUDE_TEXT_ONLY_ARGS)
               for i in range(len(argv)))
    assert "bypassPermissions" not in argv


def test_codex_runs_text_only_through_the_cli(runners, capsys, tmp_path):
    _, codex = runners
    codex.model = factory.create_backend("luna").model
    code, out, err = _complete(capsys, tmp_path, "luna", "--requirements", GUARANTEES)
    assert code == 0, err + out
    assert json.loads(out)["response"]["text"] == "NO_TOOLS"
    exec_argv = next(c for c in codex.calls if c[1] == "exec")
    assert "--ignore-user-config" in exec_argv
    assert exec_argv[exec_argv.index("-s") + 1] == "read-only"


def test_without_requirements_the_cli_keeps_the_bypass_argv(runners, capsys, tmp_path):
    claude, _ = runners
    code, out, err = _complete(capsys, tmp_path, "haiku")
    assert code == 0, err + out
    assert "bypassPermissions" in claude.calls[0]
