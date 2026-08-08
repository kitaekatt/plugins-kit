"""Tests for project-local Git config convergence without invoking Git."""

from __future__ import annotations

import subprocess
from pathlib import Path

from bootstrap_lib import engine, git_config_check
from bootstrap_lib.manifest_merge import merge_manifests


def completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_check_git_config_passes_exact_single_value(monkeypatch):
    monkeypatch.setattr(
        git_config_check, "_run_git", lambda *args: completed(0, ".githooks\n")
    )
    result = git_config_check.check_git_config(
        Path("C:/project"), "core.hooksPath", ".githooks"
    )
    assert result.passed is True


def test_check_git_config_rejects_missing_or_multiple_values(monkeypatch):
    monkeypatch.setattr(
        git_config_check,
        "_run_git",
        lambda *args: completed(0, ".githooks\nother-hooks\n"),
    )
    result = git_config_check.check_git_config(
        Path("C:/project"), "core.hooksPath", ".githooks"
    )
    assert result.passed is False


def test_write_git_config_uses_replace_all_then_rechecks(monkeypatch):
    calls = []

    def fake_run(project_dir, *args):
        calls.append(args)
        if args[0] == "--replace-all":
            return completed(0)
        return completed(0, ".githooks\n")

    monkeypatch.setattr(git_config_check, "_run_git", fake_run)
    result = git_config_check.write_git_config(
        Path("C:/project"), "core.hooksPath", ".githooks"
    )
    assert result.passed is True
    assert calls == [
        ("--replace-all", "core.hooksPath", ".githooks"),
        ("--get-all", "core.hooksPath"),
    ]


def test_manifest_phase_converges_project_config(monkeypatch):
    monkeypatch.setattr(
        git_config_check,
        "check_git_config",
        lambda *args: git_config_check.GitConfigResult(False, args[1], "missing"),
    )
    monkeypatch.setattr(
        git_config_check,
        "write_git_config",
        lambda *args: git_config_check.GitConfigResult(True, args[1], "set"),
    )
    actions = []
    failures = engine._process_manifest(
        {"git_config": [{"key": "core.hooksPath", "value": ".githooks"}]},
        "windows",
        "C:/project/tmp/data",
        "C:/project/plugins/bootstrap",
        actions,
        [],
        project_dir="C:/project",
    )
    assert failures == []
    assert actions == ["git config core.hooksPath: set to .githooks"]


def test_git_config_layers_merge_by_key():
    merged = merge_manifests(
        {"git_config": [{"key": "core.hooksPath", "value": "old"}]},
        {"git_config": [{"key": "core.hooksPath", "value": ".githooks"}]},
    )
    assert merged["git_config"] == [
        {"key": "core.hooksPath", "value": ".githooks"}
    ]
