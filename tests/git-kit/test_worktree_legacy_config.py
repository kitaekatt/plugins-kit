"""Legacy settings recovery. Run: uv run --extra dev pytest tests/git-kit/test_worktree_legacy_config.py -q.

Triggers: propose/apply, creation enforcement and task closure with invalid settings.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from test_worktree_enforcement import run_hook
from test_worktree_lifecycle import git, lifecycle, write_json  # noqa: F401
from worktree_cli import main
from worktree_config import validate_config


@pytest.fixture
def legacy_repo(lifecycle: Any) -> Path:
    repo = lifecycle.repo
    write_json(repo / ".claude/bootstrap.json", {"git_kit": {"worktrees": {"enabled": True}}})
    git(repo, "add", ".claude/bootstrap.json")
    git(repo, "commit", "-qm", "legacy enablement without pool")
    return repo


@pytest.mark.parametrize("settings", [{"enabled": True}, {"enabled": True, "pool": "old"},
                                      {"enabled": True, "root": "relative"}, "broken JSON"])
def test_propose_recovers_invalid_settings(legacy_repo: Path, capsys: Any, settings: Any) -> None:
    manifest = legacy_repo / ".claude/bootstrap.json"
    if isinstance(settings, str):
        manifest.write_text(settings, encoding="utf-8")
    else:
        write_json(manifest, {"git_kit": {"worktrees": settings}})
    before = manifest.read_bytes()
    assert main(["configure", "--propose", "--repo", str(legacy_repo)]) == 0
    out, err = capsys.readouterr()
    assert not err
    result = json.loads(out)
    validate_config(result["proposal"], legacy_repo)
    warning = result["current_settings"]
    assert warning["status"] == "invalid"
    assert "replaced" in warning["message"] and "--apply" in warning["message"]
    assert manifest.read_bytes() == before
    assert "maintenance" not in result


def test_propose_needs_no_remote_or_user_disk_policy(legacy_repo: Path, capsys: Any) -> None:
    git(legacy_repo, "remote", "remove", "origin")
    write_json(Path.home() / ".claude/bootstrap.json", {"git_kit": {"worktrees": {"disk_floor_percent": "old"}}})
    assert main(["--repo", str(legacy_repo), "configure", "--propose"]) == 0
    assert json.loads(capsys.readouterr().out)["proposal"]["enabled"] is True


def test_propose_still_requires_user_install(legacy_repo: Path, capsys: Any) -> None:
    write_json(Path.home() / ".claude/settings.json", {"enabledPlugins": {"git-kit@plugins-kit": False}})
    assert main(["configure", "--propose", "--repo", str(legacy_repo)]) == 2
    out, err = capsys.readouterr()
    assert not out and "user-level" in err


def test_apply_replaces_legacy_settings(legacy_repo: Path, tmp_path: Path, capsys: Any) -> None:
    manifest = legacy_repo / ".claude/bootstrap.json"
    write_json(manifest, {"other": {"preserved": True}, "git_kit": {"worktrees": {"enabled": True}}})
    approved = {"enabled": True, "pool": {"setup": [{"id": "setup", "argv": ["bootstrap", "run"]}]}}
    approval = tmp_path / "approved.json"
    write_json(approval, approved)
    assert main(["configure", "--apply", str(approval), "--repo", str(legacy_repo)]) == 0
    assert json.loads(capsys.readouterr().out)["settings"] == approved
    assert json.loads(manifest.read_text()) == {"other": {"preserved": True}, "git_kit": {"worktrees": approved}}


def test_legacy_enabled_creation_is_denied(legacy_repo: Path) -> None:
    for tool, arguments in [("Bash", {"command": "git worktree add ../raw"}),
                            ("Agent", {"isolation": "worktree"})]:
        output = run_hook(legacy_repo, tool, arguments)["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "configure --propose" in output["permissionDecisionReason"]
    assert not (legacy_repo.parent / "raw").exists()


def test_legacy_task_close_returns_actionable_denial(legacy_repo: Path, capsys: Any) -> None:
    assert main(["check-task-close", "--task", "task", "--json", "--repo", str(legacy_repo)]) == 2
    out, err = capsys.readouterr()
    result = json.loads(out)
    assert result["allowed"] is False and result["parked"] == []
    assert "configure --propose" in result["error"]
    assert "Traceback" not in err
