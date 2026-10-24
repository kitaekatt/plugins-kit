"""LLM_KIT_CLAUDE_EXECUTABLE: one setting names the Claude build for every spawn site.

Same contract as LLM_KIT_CODEX_EXECUTABLE (see test_codex_executable_setting.py):
an absolute path or a command name; a value that cannot be run raises an error
naming the variable instead of falling back to ``claude`` on PATH; unset keeps
``claude`` on PATH. Sites covered: the resolver, the completion backend, the auth
probe, the reachability probe and the background-session driver. No harness,
bootstrap or plugin is involved and no real model runs.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from llm_kit.adapters import claude_command
from llm_kit.adapters.backends import ClaudeCliBackend
from llm_kit.adapters.claude_command import ClaudeExecutableError
from llm_kit.adapters.harness_auth import claude_auth
from llm_kit.dispatch import reachability
from llm_kit.pipeline.execution.drivers import claude_bg
from llm_kit.registry.cost import SUBSCRIPTION, UNKNOWN

ENV = claude_command.CLAUDE_EXECUTABLE_ENV

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="the fake CLI is a POSIX shell script"
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)


def _fake_cli(directory: Path, name: str = "claude-app") -> Path:
    path = directory / name
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "auth" ]; then\n'
        '  echo \'{"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty"}\'\n'
        "else\n"
        "  echo '2.1.0 (Claude Code)'\n"
        "fi\n",
        encoding="ascii",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _fake_which(mapping):
    return lambda name, *a, **k: mapping.get(name)


# -- the resolver ----------------------------------------------------------


def test_variable_name_is_the_documented_one():
    assert ENV == "LLM_KIT_CLAUDE_EXECUTABLE"


def test_unset_uses_claude_from_path(monkeypatch):
    seen = []

    def fake_which(name):
        seen.append(name)
        return "/usr/local/bin/claude"

    monkeypatch.setattr(claude_command.shutil, "which", fake_which)
    assert claude_command.resolve_claude_executable() == "/usr/local/bin/claude"
    assert claude_command.resolve_claude_cli() == ("/usr/local/bin/claude",)
    assert seen == ["claude", "claude"]


def test_unset_and_absent_is_none_not_an_error(monkeypatch):
    monkeypatch.setattr(claude_command.shutil, "which", lambda name: None)
    assert claude_command.resolve_claude_executable() is None
    assert claude_command.resolve_claude_cli() is None


def test_command_name_is_looked_up_on_path(monkeypatch):
    monkeypatch.setenv(ENV, "claude-app")
    monkeypatch.setattr(
        claude_command.shutil, "which", _fake_which({"claude-app": "/opt/app/claude-app"})
    )
    assert claude_command.resolve_claude_executable() == "/opt/app/claude-app"
    assert claude_command.resolve_claude_cli() == ("/opt/app/claude-app",)


@posix_only
def test_absolute_path_is_used(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv(ENV, str(cli))
    assert claude_command.resolve_claude_executable() == str(cli)
    assert claude_command.resolve_claude_cli() == (str(cli),)


def test_env_argument_overrides_the_process_environment(monkeypatch):
    monkeypatch.setenv(ENV, "from-process")
    assert claude_command.configured_claude_executable({ENV: "from-mapping"}) == "from-mapping"
    assert claude_command.configured_claude_executable({}) == "claude"


def test_empty_value_is_refused(monkeypatch):
    monkeypatch.setenv(ENV, "   ")
    with pytest.raises(ClaudeExecutableError, match=ENV):
        claude_command.configured_claude_executable()


def test_relative_path_is_refused(monkeypatch):
    monkeypatch.setenv(ENV, os.path.join("bin", "claude"))
    with pytest.raises(ClaudeExecutableError, match="relative"):
        claude_command.resolve_claude_executable()


def test_nonexistent_absolute_path_fails_loudly_naming_the_variable(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, str(tmp_path / "no-such-claude"))
    for resolve in (claude_command.resolve_claude_executable, claude_command.resolve_claude_cli):
        with pytest.raises(ClaudeExecutableError) as err:
            resolve()
        assert ENV in str(err.value)
        assert "no-such-claude" in str(err.value)


def test_bare_name_not_on_path_fails_loudly(monkeypatch):
    monkeypatch.setenv(ENV, "no-such-claude-command")
    monkeypatch.setattr(claude_command.shutil, "which", lambda name: None)
    with pytest.raises(ClaudeExecutableError, match=ENV):
        claude_command.resolve_claude_executable()


@posix_only
def test_non_executable_file_fails_loudly(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.write_text("not a program", encoding="ascii")
    plain.chmod(0o644)
    monkeypatch.setenv(ENV, str(plain))
    with pytest.raises(ClaudeExecutableError, match=ENV):
        claude_command.resolve_claude_executable()


def test_error_is_a_runtime_error():
    assert issubclass(ClaudeExecutableError, RuntimeError)


# -- spawn site: completion backend ---------------------------------------


class _Runner:
    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, cmd, request, cwd, **kwargs):
        self.calls.append(list(cmd))
        return json.dumps({"result": "ok", "is_error": False, "usage": {}}), "", 0


@posix_only
def test_completion_backend_dispatches_the_configured_binary(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv(ENV, str(cli))
    runner = _Runner()
    ClaudeCliBackend(runner=runner).complete("s", "u", model="m")
    assert runner.calls[0][0] == str(cli)


def test_completion_backend_unset_uses_claude_on_path(monkeypatch):
    monkeypatch.setattr(claude_command.shutil, "which", lambda name: "/usr/bin/claude")
    runner = _Runner()
    ClaudeCliBackend(runner=runner).complete("s", "u", model="m")
    assert runner.calls[0][0] == "/usr/bin/claude"


def test_completion_backend_bad_value_fails_loudly_before_the_runner(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, str(tmp_path / "no-such-claude"))
    runner = _Runner()
    with pytest.raises(ClaudeExecutableError, match=ENV):
        ClaudeCliBackend(runner=runner).complete("s", "u", model="m")
    assert runner.calls == []


@posix_only
def test_completion_backend_explicit_executable_beats_the_variable(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, str(tmp_path / "no-such-claude"))
    runner = _Runner()
    ClaudeCliBackend(runner=runner, executable="/x/claude").complete("s", "u", model="m")
    assert runner.calls[0][0] == "/x/claude"


# -- spawn site: auth probe ------------------------------------------------


class _Spy:
    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, argv, **kwargs):
        import subprocess

        self.calls.append(list(argv))
        out = json.dumps({"authMethod": "claude.ai", "apiProvider": "firstParty"})
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


def test_auth_unset_runs_the_bare_name():
    spy = _Spy()
    assert claude_auth({}, runner=spy).state == SUBSCRIPTION
    assert spy.calls == [["claude", "auth", "status"]]


@posix_only
def test_auth_runs_the_configured_binary(tmp_path):
    cli = _fake_cli(tmp_path)
    spy = _Spy()
    assert claude_auth({ENV: str(cli)}, runner=spy).state == SUBSCRIPTION
    assert spy.calls == [[str(cli), "auth", "status"]]


def test_auth_bad_value_is_unknown_naming_the_variable_and_runs_nothing(tmp_path):
    spy = _Spy()
    auth = claude_auth({ENV: str(tmp_path / "no-such-claude")}, runner=spy)
    assert auth.state == UNKNOWN
    assert ENV in auth.reason
    assert spy.calls == []


# -- spawn site: reachability probe ---------------------------------------


@posix_only
def test_reachability_probes_the_configured_binary(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv(ENV, str(cli))
    verdict = reachability.check_harness("claude")
    assert verdict.status == reachability.STATUS_REACHABLE
    assert "2.1.0" in verdict.detail


def test_reachability_bad_value_raises_naming_the_variable(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, str(tmp_path / "no-such-claude"))
    with pytest.raises(ClaudeExecutableError, match=ENV):
        reachability.check_harness("claude")


def test_reachability_unset_and_absent_is_unreachable(monkeypatch):
    monkeypatch.setattr(claude_command.shutil, "which", lambda name: None)
    verdict = reachability.check_harness("claude")
    assert verdict.status == reachability.STATUS_UNREACHABLE


# -- spawn site: background-session driver ---------------------------------


@posix_only
def test_bg_driver_resolves_the_configured_binary(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv(ENV, str(cli))
    assert claude_bg.ClaudeCli().resolve_executable() == str(cli)


def test_bg_driver_unset_uses_claude_on_path(monkeypatch):
    monkeypatch.setattr(claude_command.shutil, "which", lambda name: "/usr/bin/claude")
    assert claude_bg.ClaudeCli().resolve_executable() == "/usr/bin/claude"


def test_bg_driver_bad_value_is_a_not_found_error_naming_the_variable(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, str(tmp_path / "no-such-claude"))
    with pytest.raises(claude_bg.ClaudeExecutableNotFoundError, match=ENV):
        claude_bg.ClaudeCli().resolve_executable()


def test_bg_driver_explicit_executable_beats_the_variable(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, str(tmp_path / "no-such-claude"))
    assert claude_bg.ClaudeCli(executable="/x/claude").resolve_executable() == "/x/claude"
