"""Contracts for bootstrap's Codex SessionStart adapter."""

import json
import importlib.util
import os
import subprocess
from pathlib import Path

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
    def test_writes_session_start_hook_and_preserves_existing_hooks(self, tmp_path, monkeypatch):
        project = _git_project(tmp_path)
        codex_dir = project / ".codex"
        codex_dir.mkdir()
        hooks_path = codex_dir / "hooks.json"
        hooks_path.write_text(json.dumps({
            "hooks": {
                "SessionStart": [{
                    "matcher": "^(startup|resume)$",
                    "hooks": [{"type": "command", "command": "other-hook"}],
                }],
            },
        }), encoding="utf-8")
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        result = codex_hook.ensure_codex_hook(str(project))

        assert result.changed is True
        body = json.loads(hooks_path.read_text(encoding="utf-8"))
        session_hooks = body["hooks"]["SessionStart"]
        commands = [
            hook["command"]
            for group in session_hooks
            for hook in group["hooks"]
        ]
        assert "other-hook" in commands
        assert any(c.endswith("bootstrap codex-hook") for c in commands)
        assert any(group["matcher"] == "^(startup|resume)$"
                   for group in session_hooks)

    def test_rerun_is_idempotent(self, tmp_path):
        project = _git_project(tmp_path)

        first = codex_hook.ensure_codex_hook(str(project))
        second = codex_hook.ensure_codex_hook(str(project))

        assert first.changed is True
        assert second.changed is False

    def test_marker_like_text_in_unrelated_command_is_preserved(self, tmp_path):
        project = _git_project(tmp_path)
        hooks_path = project / ".codex" / "hooks.json"
        hooks_path.parent.mkdir()
        hooks_path.write_text(json.dumps({
            "hooks": {"SessionStart": [{
                "hooks": [{"type": "command", "command": "echo bootstrap codex-hook"}],
            }]},
        }), encoding="utf-8")

        codex_hook.ensure_codex_hook(str(project))

        body = json.loads(hooks_path.read_text(encoding="utf-8"))
        commands = [
            hook["command"]
            for group in body["hooks"]["SessionStart"]
            for hook in group["hooks"]
        ]
        assert "echo bootstrap codex-hook" in commands

    def test_existing_hook_permissions_are_preserved(self, tmp_path):
        project = _git_project(tmp_path)
        hooks_path = project / ".codex" / "hooks.json"
        hooks_path.parent.mkdir()
        hooks_path.write_text("{}\n", encoding="utf-8")
        os.chmod(hooks_path, 0o640)

        codex_hook.ensure_codex_hook(str(project))

        assert (os.stat(hooks_path).st_mode & 0o777) == 0o640


class TestCodexIgnoreContext:
    def test_gitignore_rule_is_accepted(self, tmp_path):
        project = _git_project(tmp_path)
        (project / ".gitignore").write_text("/.codex/\n", encoding="utf-8")

        assert codex_hook.codex_ignore_context(str(project)) == ""

    def test_missing_gitignore_instructs_codex_to_add_codex_rule(self, tmp_path):
        project = _git_project(tmp_path)

        context = codex_hook.codex_ignore_context(str(project))

        assert ".gitignore" in context
        assert "modify" in context.lower()
        assert "/.codex/" in context

    def test_tracked_codex_is_reported_as_an_ownership_conflict(self, tmp_path):
        project = _git_project(tmp_path)
        hooks_path = project / ".codex" / "hooks.json"
        hooks_path.parent.mkdir()
        hooks_path.write_text("{}\n", encoding="utf-8")
        _git("add", ".codex/hooks.json", cwd=project)

        context = codex_hook.codex_ignore_context(str(project))

        assert "already tracked" in context
        assert "Do not remove" in context

    def test_p4ignore_remediation_requires_p4_edit(self, tmp_path, monkeypatch):
        project = _git_project(tmp_path)
        (project / ".gitignore").write_text("/.codex/\n", encoding="utf-8")
        (project / ".p4ignore").write_text("*.tmp\n", encoding="utf-8")
        monkeypatch.setattr(codex_hook.shutil, "which", lambda name: "/usr/bin/p4")
        monkeypatch.setattr(
            codex_hook,
            "_run_p4",
            lambda *args, **kwargs: codex_hook._Process(
                stdout="/tmp/project/.codex not ignored\n", returncode=0),
        )

        context = codex_hook.codex_ignore_context(str(project))

        assert ".p4ignore" in context
        assert "p4 edit .p4ignore" in context
        assert "/.codex/" in context


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
            codex_hook,
            "ensure_codex_hook",
            lambda project: codex_hook.CodexHookInstallResult(True, project + "/.codex/hooks.json"),
        )

        actions, oks, failures = engine._run_codex_hook_setup(str(tmp_path))

        assert actions == ["codex hook: installed %s/.codex/hooks.json" % tmp_path]
        assert oks == []
        assert failures == []

    def test_codex_unavailable_skips_hook_without_remediation(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(
            codex,
            "detect_codex",
            lambda: codex.CodexDetection(
                available=False, reason="`codex` not found on PATH"
            ),
        )
        monkeypatch.setattr(
            codex_hook,
            "ensure_codex_hook",
            lambda project: calls.append(project),
        )

        assert engine._run_codex_hook_setup(str(tmp_path)) == ([], [], [])
        assert calls == []

    def test_hook_is_not_created_after_an_incomplete_or_console_pass(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(
            codex_hook,
            "ensure_codex_hook",
            lambda project: calls.append(project),
        )

        assert engine._run_codex_hook_setup(
            str(tmp_path), existing_failures=[{"type": "tool"}]
        ) == ([], [], [])
        assert engine._run_codex_hook_setup(str(tmp_path), console=True) == (
            [], [], []
        )
        assert calls == []

    def test_missing_gitignore_does_not_block_hook_install(self, tmp_path, monkeypatch):
        project = _git_project(tmp_path)
        monkeypatch.setattr(
            codex,
            "detect_codex",
            lambda: codex.CodexDetection(available=True, reason="fake codex"),
        )
        which = codex_hook.shutil.which
        monkeypatch.setattr(
            codex_hook.shutil, "which",
            lambda name: None if name == "p4" else which(name),
        )
        monkeypatch.setattr(
            codex_hook,
            "ensure_codex_hook",
            lambda project: codex_hook.CodexHookInstallResult(
                True, project + "/.codex/hooks.json"
            ),
        )

        actions, oks, failures = engine._run_codex_hook_setup(str(project))

        assert actions == ["codex hook: installed %s/.codex/hooks.json" % project]
        assert oks == []
        assert failures == []


class TestCodexCli:
    def test_codex_hook_runs_engine_and_appends_ignore_context(self, tmp_path, monkeypatch, capsys):
        cli = _load_cli()
        project = _git_project(tmp_path)
        response = {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "bootstrap context",
            },
        }

        monkeypatch.chdir(project)
        monkeypatch.setattr(cli, "marketplaces", lambda: ["plugins-kit"])
        monkeypatch.setattr(cli, "plugin_data_dir", lambda _: str(tmp_path / "data"))
        monkeypatch.setattr(cli, "find_plugin_root", lambda *_: str(tmp_path / "plugin"))
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda *args, **kwargs: type(
                "Result", (), {"stdout": json.dumps(response), "stderr": "", "returncode": 0}
            )(),
        )
        monkeypatch.setattr(
            codex_hook,
            "codex_ignore_context",
            lambda _: "ignore context",
        )

        assert cli.main(["--plugin-root", str(tmp_path / "plugin"), "codex-hook"]) == 0
        output = json.loads(capsys.readouterr().out)
        assert output["hookSpecificOutput"]["additionalContext"] == (
            "bootstrap context\n\nignore context"
        )
