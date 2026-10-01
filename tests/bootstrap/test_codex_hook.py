"""Contracts for bootstrap's Codex SessionStart adapter."""

import json
import importlib.util
import io
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from bootstrap_lib import codex, codex_hook, engine


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_cli():
    path = REPO_ROOT / "plugins" / "bootstrap" / "scripts" / "bootstrap_cli.py"
    spec = importlib.util.spec_from_file_location("bootstrap_codex_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _git_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _git("init", "-q", cwd=project)
    return project


class TestCodexHookInstall:
    def test_windows_command_is_portable_single_line_with_terminal_calls(
        self, monkeypatch, tmp_path
    ):
        home = tmp_path / "user home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))

        command = codex_hook._hook_entry()["commandWindows"]

        assert "\n" not in command
        assert "\r" not in command
        assert command.startswith("cmd.exe /d /c ")
        assert "%USERPROFILE%\\.local\\bin\\bootstrap.cmd" in command
        assert str(home) not in command
        assert "&&" not in command
        assert "||" not in command
        assert re.search(r"else call bootstrap\.cmd codex-hook\"$", command)
        assert re.search(
            r"else exit /b 0\) else call bootstrap\.cmd codex-hook\"$", command
        )

    @pytest.mark.skipif(os.name != "nt", reason="Windows shell execution contract")
    @pytest.mark.parametrize("home_name", ["home", "home with spaces"])
    @pytest.mark.parametrize("shell", ["powershell.exe", "cmd.exe"])
    def test_windows_command_executes_launcher(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        home_name: str, shell: str,
    ) -> None:
        executable = shutil.which(shell)
        if executable is None:
            pytest.skip(f"{shell} is unavailable")
        home = tmp_path / home_name
        launcher = home / ".local" / "bin" / "bootstrap.cmd"
        launcher.parent.mkdir(parents=True)
        launcher.write_text(
            "@echo off\necho hook-argument:%1\nexit /b 0\n", encoding="ascii",
        )
        monkeypatch.setenv("HOME", str(home))
        command = codex_hook._hook_entry()["commandWindows"]
        arguments = (
            [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
            if shell == "powershell.exe" else f'"{executable}" /d /s /c "{command}"'
        )

        result = subprocess.run(
            arguments, capture_output=True, text=True, timeout=15,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "hook-argument:codex-hook"

    def test_writes_user_session_start_hook_and_preserves_existing_hooks(self, tmp_path, monkeypatch):
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        hooks_path = codex_home / "hooks.json"
        hooks_path.write_text(json.dumps({
            "hooks": {
                "SessionStart": [{
                    "matcher": "^(startup|resume)$",
                    "hooks": [{"type": "command", "command": "other-hook"}],
                }],
            },
        }), encoding="utf-8")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))

        result = codex_hook.ensure_user_codex_hook()

        assert result.changed is True
        body = json.loads(hooks_path.read_text(encoding="utf-8"))
        session_hooks = body["hooks"]["SessionStart"]
        commands = [
            hook["command"]
            for group in session_hooks
            for hook in group["hooks"]
        ]
        assert "other-hook" in commands
        assert codex_hook.POSIX_HOOK_COMMAND in commands
        assert any(group["matcher"] == "^(startup|resume)$"
                   for group in session_hooks)

    def test_rerun_is_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
        first = codex_hook.ensure_user_codex_hook()
        first_content = Path(first.path).read_bytes()
        second = codex_hook.ensure_user_codex_hook()

        assert first.changed is True
        assert second.changed is False
        assert Path(second.path).read_bytes() == first_content

    def test_codex_home_symlink_is_resolved_and_commands_have_no_user_paths(
        self, tmp_path, monkeypatch
    ):
        real_home = tmp_path / "real-codex-home"
        real_home.mkdir()
        link_home = tmp_path / "linked-codex-home"
        link_home.symlink_to(real_home, target_is_directory=True)
        monkeypatch.setenv("CODEX_HOME", str(link_home))
        monkeypatch.setenv("HOME", str(tmp_path / "private home"))

        result = codex_hook.ensure_user_codex_hook()
        body = json.loads(Path(result.path).read_text(encoding="utf-8"))
        hook = body["hooks"]["SessionStart"][0]["hooks"][0]

        assert Path(result.path).parent == real_home
        assert hook["command"] == codex_hook.POSIX_HOOK_COMMAND
        assert str(tmp_path) not in hook["command"]
        assert "%USERPROFILE%" in hook["commandWindows"]
        assert str(tmp_path) not in hook["commandWindows"]

    def test_hooks_json_symlink_is_refused(self, tmp_path, monkeypatch):
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        target = tmp_path / "target.json"
        target.write_text("{}\n", encoding="utf-8")
        (codex_home / "hooks.json").symlink_to(target)
        monkeypatch.setenv("CODEX_HOME", str(codex_home))

        with pytest.raises(codex_hook.CodexHookError):
            codex_hook.ensure_user_codex_hook()

    @pytest.mark.parametrize("legacy", [False, True])
    def test_windows_only_hook_is_replaced_and_rerun_is_idempotent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: bool,
    ) -> None:
        codex_home = tmp_path / "home with spaces"
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        windows_command = codex_hook._hook_entry()["commandWindows"]
        old_command = 'cmd.exe /d /c call "C:\\old\\bootstrap.cmd" codex-hook' if legacy else windows_command
        hooks_path = codex_home / "hooks.json"
        hooks_path.parent.mkdir()
        unrelated_hook = {
            "type": "command", "commandWindows": "echo bootstrap codex-hook",
        }
        hooks_path.write_text(json.dumps({
            "hooks": {"SessionStart": [{"hooks": [
                {"type": "command", "commandWindows": old_command}, unrelated_hook,
            ]}]},
        }), encoding="utf-8")

        first = codex_hook.ensure_user_codex_hook()
        second = codex_hook.ensure_user_codex_hook()

        hooks = [
            hook for group in json.loads(hooks_path.read_text(encoding="utf-8"))[
                "hooks"]["SessionStart"] for hook in group["hooks"]
        ]
        assert first.changed is True
        assert second.changed is False
        assert hooks == [unrelated_hook, codex_hook._hook_entry()]

    def test_marker_like_text_in_unrelated_command_is_preserved(self, tmp_path, monkeypatch):
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        hooks_path = codex_home / "hooks.json"
        hooks_path.write_text(json.dumps({
            "hooks": {"SessionStart": [{
                "hooks": [{"type": "command", "command": "echo bootstrap codex-hook"}],
            }]},
        }), encoding="utf-8")

        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        codex_hook.ensure_user_codex_hook()

        body = json.loads(hooks_path.read_text(encoding="utf-8"))
        commands = [
            hook["command"]
            for group in body["hooks"]["SessionStart"]
            for hook in group["hooks"]
        ]
        assert "echo bootstrap codex-hook" in commands

    def test_existing_hook_permissions_are_preserved(self, tmp_path, monkeypatch):
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        hooks_path = codex_home / "hooks.json"
        hooks_path.write_text("{}\n", encoding="utf-8")
        os.chmod(hooks_path, 0o640)
        original_mode = os.stat(hooks_path).st_mode & 0o777
        monkeypatch.setenv("CODEX_HOME", str(codex_home))

        codex_hook.ensure_user_codex_hook()

        assert (os.stat(hooks_path).st_mode & 0o777) == original_mode


class TestProjectHookStrip:
    def test_strip_keeps_team_hooks_and_removes_owned_groups(self, tmp_path):
        project = _git_project(tmp_path)
        hooks_path = project / ".codex" / "hooks.json"
        hooks_path.parent.mkdir()
        hooks_path.write_text(json.dumps({"hooks": {
            "SessionStart": [
                {"matcher": "startup", "hooks": [
                    codex_hook._hook_entry(),
                    {"type": "command", "command": "team-hook"},
                ]},
                {"matcher": "resume", "hooks": [
                    {"type": "command", "command": "/opt/bootstrap codex-hook"},
                ]},
            ],
            "Other": [{"hooks": [{"command": "team-other"}]}],
        }}), encoding="utf-8")

        result = codex_hook.strip_project_codex_hook(str(project))

        assert result.changed is True
        assert json.loads(hooks_path.read_text(encoding="utf-8"))["hooks"] == {
            "SessionStart": [{"matcher": "startup", "hooks": [
                {"type": "command", "command": "team-hook"}
            ]}],
            "Other": [{"hooks": [{"command": "team-other"}]}],
        }
        assert not hooks_path.with_name("hooks.json.lock").exists()

    def test_team_only_project_hook_does_not_create_lock_file(self, tmp_path):
        project = _git_project(tmp_path)
        hooks_path = project / ".codex" / "hooks.json"
        hooks_path.parent.mkdir()
        hooks_path.write_text(json.dumps({"hooks": {
            "SessionStart": [{"hooks": [
                {"type": "command", "command": "team-hook"},
            ]}],
        }}), encoding="utf-8")

        result = codex_hook.strip_project_codex_hook(str(project))

        assert result.changed is False
        assert hooks_path.exists()
        assert not hooks_path.with_name("hooks.json.lock").exists()

    def test_untracked_empty_strip_removes_file_lock_and_empty_codex_dir(self, tmp_path):
        project = _git_project(tmp_path)
        hooks_path = project / ".codex" / "hooks.json"
        hooks_path.parent.mkdir()
        hooks_path.write_text(json.dumps({"hooks": {"SessionStart": [
            {"hooks": [codex_hook._hook_entry()]}
        ]}}), encoding="utf-8")
        hooks_path.with_name("hooks.json.lock").write_text("", encoding="ascii")

        codex_hook.strip_project_codex_hook(str(project))

        assert not hooks_path.exists()
        assert not hooks_path.with_name("hooks.json.lock").exists()
        assert not hooks_path.parent.exists()

    def test_tracked_empty_strip_keeps_reduced_file(self, tmp_path):
        project = _git_project(tmp_path)
        hooks_path = project / ".codex" / "hooks.json"
        hooks_path.parent.mkdir()
        hooks_path.write_text(json.dumps({"hooks": {"SessionStart": [
            {"hooks": [codex_hook._hook_entry()]}
        ]}}), encoding="utf-8")
        _git("add", ".codex/hooks.json", cwd=project)

        codex_hook.strip_project_codex_hook(str(project))

        assert hooks_path.exists()
        assert json.loads(hooks_path.read_text(encoding="utf-8")) == {"hooks": {}}


class TestCodexResponse:
    def test_additional_context_is_appended_to_session_start_response(self):
        response = {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "bootstrap context",
            },
        }

        result = codex_hook.add_additional_context(response, "ignore context")

        assert result["hookSpecificOutput"]["additionalContext"] == (
            "bootstrap context\n\nignore context"
        )


class TestEngineWiring:
    def test_clean_automatic_pass_installs_hook(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            codex,
            "detect_codex",
            lambda: codex.CodexDetection(available=True, reason="fake codex"),
        )
        monkeypatch.setattr(
            codex_hook, "ensure_user_codex_hook",
            lambda: codex_hook.CodexHookInstallResult(True, "user/hooks.json"),
        )
        monkeypatch.setattr(codex_hook, "strip_project_codex_hook", lambda project: None)

        actions, oks, failures = engine._run_codex_hook_setup(str(tmp_path))

        assert actions == ["codex hook: installed user/hooks.json; Codex needs /hooks review"]
        assert oks == []
        assert failures == []

    def test_codex_unavailable_skips_hook_without_remediation(self, tmp_path, monkeypatch):
        calls = []
        stripped = []
        monkeypatch.setattr(
            codex,
            "detect_codex",
            lambda: codex.CodexDetection(
                available=False, reason="`codex` not found on PATH"
            ),
        )
        monkeypatch.setattr(
            codex_hook,
            "ensure_user_codex_hook",
            lambda: calls.append(True),
        )
        monkeypatch.setattr(codex_hook, "strip_project_codex_hook", stripped.append)

        assert engine._run_codex_hook_setup(str(tmp_path)) == ([], [], [])
        assert calls == []
        assert stripped == [str(tmp_path)]

    def test_hook_is_not_created_after_an_incomplete_or_console_pass(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(
            codex_hook,
            "ensure_user_codex_hook",
            lambda project: calls.append(project),
        )

        assert engine._run_codex_hook_setup(
            str(tmp_path), existing_failures=[{"type": "tool"}]
        ) == ([], [], [])
        assert engine._run_codex_hook_setup(str(tmp_path), console=True) == (
            [], [], []
        )
        assert calls == []

class TestNoCodexTreeInArbitraryCwd:
    def test_pass_from_subdirectory_writes_only_user_codex_home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        codex_home = home / ".codex"
        sub = tmp_path / "project" / "tmp" / "run" / "audit"
        sub.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.chdir(sub)
        monkeypatch.setattr(
            codex, "detect_codex",
            lambda: codex.CodexDetection(available=True, reason="fake codex"),
        )

        actions, _oks, failures = engine._run_codex_hook_setup(str(sub))

        assert failures == []
        assert (codex_home / "hooks.json").is_file()
        stray = [p for p in (tmp_path / "project").rglob(".codex")]
        assert stray == []


class TestCodexCli:
    def test_codex_hook_uses_stdin_cwd_and_does_not_add_ignore_context(
        self, tmp_path, monkeypatch, capsys
    ):
        cli = _load_cli()
        project = _git_project(tmp_path)
        response = {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "bootstrap context",
            },
        }

        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.chdir(other)
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps({
            "cwd": str(project), "session_id": "session-one"
        })))
        monkeypatch.setattr(cli, "marketplaces", lambda: ["plugins-kit"])
        monkeypatch.setattr(cli, "plugin_data_dir", lambda _: str(tmp_path / "data"))
        monkeypatch.setattr(cli, "find_plugin_root", lambda *_: str(tmp_path / "plugin"))
        seen = {}
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda *args, **kwargs: seen.update(kwargs) or type(
                "Result", (), {"stdout": json.dumps(response), "stderr": "", "returncode": 0}
            )(),
        )
        assert cli.main(["--plugin-root", str(tmp_path / "plugin"), "codex-hook"]) == 0
        output = json.loads(capsys.readouterr().out)
        assert output["hookSpecificOutput"]["additionalContext"] == "bootstrap context"
        assert seen["cwd"] == str(project)

    def test_duplicate_session_is_a_quiet_noop(self, tmp_path, monkeypatch, capsys):
        cli = _load_cli()
        project = _git_project(tmp_path)
        monkeypatch.chdir(project)
        monkeypatch.setattr(cli, "marketplaces", lambda: ["plugins-kit"])
        monkeypatch.setattr(cli, "plugin_data_dir", lambda _: str(tmp_path / "data"))
        monkeypatch.setattr(cli, "find_plugin_root", lambda *_: str(tmp_path / "plugin"))
        calls = []
        monkeypatch.setattr(
            cli.subprocess, "run",
            lambda *args, **kwargs: calls.append(args) or type(
                "Result", (), {"stdout": "", "stderr": "", "returncode": 0}
            )(),
        )

        for _ in range(2):
            monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps({
                "cwd": str(project), "session_id": "same-session"
            })))
            assert cli.main(["--plugin-root", str(tmp_path / "plugin"), "codex-hook"]) == 0

        assert len(calls) == 1
        assert capsys.readouterr().out == ""
