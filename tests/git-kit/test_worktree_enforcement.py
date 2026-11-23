"""Enabled creation gates. Run: uv run --extra dev pytest tests/git-kit/test_worktree_enforcement.py -q.

Triggers: Agent isolation, raw shell creation, forged builder evidence, session start.
Every Git operation and home/data write stays in the lifecycle fixture's temp tree.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from test_worktree_lifecycle import lifecycle, write_json  # noqa: F401


PLUGIN = Path(__file__).parents[2] / "plugins/git-kit"


def run_hook(repo: Path, tool: str, data: dict) -> dict:
    registration = json.loads((PLUGIN / "hooks/hooks.json").read_text())
    hooks = registration["hooks"].get("PreToolUse", [])
    assert hooks, "PreToolUse lifecycle enforcement is not registered"
    command = hooks[0]["hooks"][0]["command"].replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN))
    result = subprocess.run(command, shell=True, cwd=repo, text=True, capture_output=True,
        input=json.dumps({"hook_event_name": "PreToolUse", "cwd": str(repo),
                          "tool_name": tool, "tool_input": data}),
        env={**os.environ, "BOOTSTRAP_PYTHON": sys.executable}, check=True)
    return json.loads(result.stdout) if result.stdout.strip() else {}


@pytest.mark.parametrize("command", [
    "git worktree add ../raw",
    "git -C {repo} worktree add ../raw",
    "git -c color.ui=false --no-pager -C {repo} worktree add --detach ../raw HEAD",
    "env GIT_KIT_POOL_BUILD=1 command git -C {repo} worktree add ../raw",
    "timeout 30s git -C {repo} worktree add ../raw",
    "nice -n 5 nohup git -C {repo} worktree add ../raw",
    "bash -lc 'git -C {repo} worktree add ../raw'",
    "bash -O extglob -c 'git worktree add ../raw'",
    "bash +O extglob -c 'git worktree add ../raw'",
    "sh -c 'command git worktree add ../raw'",
    "eval 'git worktree add ../raw'",
    "cd {repo}/.claude && git worktree add ../../raw",
    "cd -- {repo}/.claude && git worktree add ../../raw",
    "(cd {repo} && git worktree add ../raw)",
    "git status; git worktree add ../raw",
    "git worktree list && git worktree add ../raw",
    "git status\ngit worktree add ../raw",
    "git \\\nworktree add ../raw",
    "git status # inspect state\ngit worktree add ../raw",
    "bash <<EOF\ngit worktree add ../raw\nEOF",
    "bash <<'EOF'\ngit worktree add ../raw\nEOF",
    "env bash <<EOF\ngit worktree add ../raw\nEOF",
    "cat <<'EOF'\nordinary text\nEOF\ngit worktree add ../raw",
    "cat <<EOF\n$(git worktree add ../raw)\nEOF",
    "cat <<'EOF' | bash\ngit worktree add ../raw\nEOF",
])
def test_enabled_creation_paths_block(lifecycle, command):
    obj = lifecycle
    for tool, data in [("Agent", {"isolation": "worktree"}),
                       ("Bash", {"command": command.format(repo=obj.repo)})]:
        response = run_hook(obj.repo, tool, data)["hookSpecificOutput"]
        assert response["hookEventName"] == "PreToolUse"
        assert response["permissionDecision"] == "deny"
        reason = response["permissionDecisionReason"]
        # The reason is a runnable command for the installed version, and it
        # says the agent ID is a unique claim ID that names the folder.
        command = re.search(r'"\$\{?BOOTSTRAP_PYTHON[^"]*" "\$\{?GIT_KIT_ROOT[^"]*/(scripts/worktrees\.py)" take --agent ',
                            reason)
        assert command and (PLUGIN / command[1]).is_file(), reason
        assert "--session" in reason and "unique" in reason and "folder" in reason
    assert not (obj.repo.parent / "raw").exists()


@pytest.mark.parametrize("mode", ["disabled", "unconfigured", "untracked", "null"])
def test_disabled_is_unchanged(lifecycle, mode):
    obj = lifecycle
    manifest = obj.repo / ".claude/bootstrap.json"
    if mode == "disabled":
        config = obj.settings.raw.copy()
        config["enabled"] = False
        write_json(manifest, {"git_kit": {"worktrees": config}})
    elif mode == "unconfigured":
        manifest.unlink()
    elif mode == "null":
        write_json(manifest, {"git_kit": {"worktrees": None}})
    else:
        # A local-only file cannot opt the repository into enforcement.
        subprocess.run(["git", "rm", "--cached", str(manifest)], cwd=obj.repo, check=True, capture_output=True)
    assert run_hook(obj.repo, "Agent", {"isolation": "worktree"}) == {}
    assert run_hook(obj.repo, "Bash", {"command": "git worktree add ../raw"}) == {}
    assert run_hook(obj.repo, "Bash", {"command": "echo 'unclosed quote"}) == {}


def test_target_repository_controls_gate(lifecycle, tmp_path):
    obj = lifecycle
    other = tmp_path / "disabled"
    subprocess.run(["git", "clone", str(obj.repo), str(other)], check=True, capture_output=True)
    (other / ".claude/bootstrap.json").unlink()
    assert run_hook(obj.repo, "Bash", {"command": f"git -C {other} worktree add ../raw"}) == {}
    assert run_hook(obj.repo, "Bash", {"command": f"cd {other} && git worktree add ../raw"}) == {}
    decision = run_hook(other, "Bash", {"command": f"git -C {obj.repo} worktree add ../raw"})
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    for command in [f"env -C {obj.repo} git worktree add ../raw",
                    f"env --chdir={obj.repo} git worktree add ../raw",
                    f"git --git-dir={obj.repo}/.git worktree add ../raw",
                    f"git --git-dir {obj.repo}/.git worktree add ../raw"]:
        assert run_hook(other, "Bash", {"command": command})["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert run_hook(obj.repo, "Bash", {"command": "echo 'git worktree add ../raw'"}) == {}
    assert run_hook(obj.repo, "Bash", {"command": "git worktree list"}) == {}
    assert run_hook(obj.repo, "Bash", {"command": "command -v git worktree add"}) == {}
    assert run_hook(obj.repo, "Agent", {"description": "ordinary agent"}) == {}


@pytest.mark.parametrize("command", [
    "cat <<'EOF'\ngit worktree add ../raw\nEOF",
    "cat <<'EOF'\nIt's ordinary text.\nEOF",
    'cat <<"EOF"\nIt\'s ordinary text.\nEOF',
    "cat <<EOF\ngit worktree add ../raw\nEOF",
    "cat <<-'EOF'\n\tgit worktree add ../raw\n\tEOF",
    "cat <<'EOF'\n$(git worktree add ../raw)\nEOF",
    "git status # ; git worktree add ../raw\ngit worktree list",
    "bash -n <<'EOF'\ngit worktree add ../raw\nEOF",
    "bash script.sh <<'EOF'\ngit worktree add ../raw\nEOF",
    "cat <<END-OF-FILE\nIt's ordinary text.\nEND-OF-FILE",
    "echo $((1 << N))",
])
def test_literal_heredoc_and_comment_data_is_unchanged(lifecycle, command):
    assert run_hook(lifecycle.repo, "Bash", {"command": command}) == {}


def test_forged_builder_refused(lifecycle, monkeypatch):
    import worktree_safety as safety

    obj = lifecycle
    obj.initialize()
    record = obj.new_record()
    obj.transition(record, "build", "git worktree add")
    write_json(obj.state / "maintenance.json", {"outcome": "running", **safety.process_identity(os.getpid())})
    monkeypatch.setenv("GIT_KIT_POOL_BUILD", "1")
    monkeypatch.setenv("GIT_KIT_MANAGED_BUILD", "1")
    with pytest.raises(ValueError, match="lock|ownership"):
        obj.prepare(record, recycle=False)
    assert not Path(record["path"]).exists()


def test_managed_builder_requires_worker_and_record(lifecycle):
    from worktree_enforcement import maintenance_lock, require_managed_build
    import worktree_safety as safety

    obj = lifecycle
    obj.initialize()
    record = obj.new_record()
    obj.transition(record, "build", "git worktree add")
    worker = {"outcome": "running", **safety.process_identity(os.getpid())}
    write_json(obj.state / "maintenance.json", worker)
    with maintenance_lock(obj) as held:
        assert held
        require_managed_build(obj, record)
        record["worker"] = {"pid": os.getpid(), "start": "forged"}
        obj.save(record)
        with pytest.raises(ValueError, match="worker|ownership"):
            require_managed_build(obj, record)


def test_other_lock_owner_cannot_forge_builder(lifecycle):
    from worktree_enforcement import require_managed_build
    from worktree_pool import _file_lock
    import worktree_safety as safety

    obj = lifecycle
    obj.initialize()
    record = obj.new_record()
    obj.transition(record, "build", "git worktree add")
    write_json(obj.state / "maintenance.json", {"outcome": "running", **safety.process_identity(os.getpid())})
    with _file_lock(obj.state / "maintenance.lock", blocking=False) as held:
        assert held
        with pytest.raises(ValueError, match="ownership"):
            require_managed_build(obj, record)


def test_sessionstart_no_maintenance():
    hooks = json.loads((PLUGIN / "hooks/hooks.json").read_text())["hooks"]
    assert "SessionStart" not in hooks
    assert not (PLUGIN / "hooks/sessionstart/pool-refill.sh").exists()
