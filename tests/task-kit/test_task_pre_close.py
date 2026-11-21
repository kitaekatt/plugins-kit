"""Configured close/archive command contracts.

Run: uv run --extra dev pytest tests/task-kit/test_task_pre_close.py -q
Trigger: a configured pre-close checker protects parked work before mutations.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from task_system import location_ops, state_ops
from task_system.state_ops import StateOpError


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(tmp_path / "plugin-data"))
    root = tmp_path / "project"
    root.mkdir()
    remote = tmp_path / "remote.git"
    for args in (["init", "--bare", str(remote)], ["init", "-b", "main", str(root)]):
        subprocess.run(["git", *args], check=True, capture_output=True)
    for args in (["config", "user.name", "Test"], ["config", "user.email", "test@example.com"],
                 ["remote", "add", "origin", str(remote)]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    folder = root / "dev/tasks/recover"
    folder.mkdir(parents=True)
    (folder / "task.yaml").write_text(yaml.safe_dump({"task": {"status": "active", "title": "Recover"}}))
    (folder / "log.md").write_text("preserve log\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "task"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "push", "-u", "origin", "main"], check=True, capture_output=True)
    return root


def configure(project: Path, command: list[str] | dict[str, object]) -> None:
    config = project / ".claude/bootstrap.json"
    config.parent.mkdir(exist_ok=True)
    config.write_text(json.dumps({"task_kit": {"pre_close": command}}))


def snapshot(project: Path) -> dict[str, bytes]:
    return {str(path.relative_to(project)): path.read_bytes() for path in (project / "dev/tasks").rglob("*") if path.is_file()}


@pytest.mark.parametrize("operation", [state_ops.close, location_ops.archive_task])
def test_parked_blocks_close_and_archive(project: Path, operation) -> None:
    configure(project, [sys.executable, "-c",
        "import json,sys; from pathlib import Path; assert sys.argv[1:] == ['dev/tasks/recover','recover',str(Path.cwd())]; "
        "print(json.dumps({'allowed':False,'parked':[{'id':'w-waiting','task':'recover'}]}))",
        "{task}", "{task_id}", "{repo}"])
    before = snapshot(project)
    head = subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"])
    with pytest.raises(StateOpError, match="w-waiting"):
        operation("dev/tasks/recover", project)
    assert snapshot(project) == before
    assert subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"]) == head


@pytest.mark.parametrize("output,code", [("not JSON", 0), ('{"allowed":"yes","parked":[]}', 0),
    ('{"allowed":true,"parked":[{"id":"w-waiting"}]}', 0), ('{"allowed":true,"parked":[]}', 7)])
def test_invalid_pre_close_fails_before_write(project: Path, output: str, code: int) -> None:
    configure(project, [sys.executable, "-c", f"print({output!r}); raise SystemExit({code})"])
    before = snapshot(project)
    with pytest.raises(StateOpError, match="pre-close"):
        state_ops.close("dev/tasks/recover", project)
    assert snapshot(project) == before


@pytest.mark.parametrize("configured", [False, True])
def test_absent_pre_close_keeps_existing_behavior(project: Path, configured: bool, capsys: pytest.CaptureFixture) -> None:
    if configured:
        configure(project, [str(project / "absent-checker")])
    result = state_ops.close("dev/tasks/recover", project)
    assert result == "dev/tasks/recover"
    assert yaml.safe_load((project / result / "task.yaml").read_text())["task"]["status"] == "closed"
    assert ("pre-close" in capsys.readouterr().err) is configured


def test_absent_plugin_script_keeps_existing_behavior(project: Path, capsys: pytest.CaptureFixture) -> None:
    missing_script = project / "absent-plugin/check.py"
    configure(project, {"argv": [sys.executable, str(missing_script)], "required_paths": [str(missing_script)]})
    assert state_ops.close("dev/tasks/recover", project) == "dev/tasks/recover"
    assert yaml.safe_load((project / "dev/tasks/recover/task.yaml").read_text())["task"]["status"] == "closed"
    assert "check omitted" in capsys.readouterr().err


@pytest.mark.parametrize("command", [{"argv": [sys.executable], "unknown": True},
    {"argv": [sys.executable], "required_paths": ["relative.py"]},
    {"argv": [sys.executable], "required_paths": None},
    {"required_paths": []}])
def test_invalid_pre_close_config_blocks(project: Path, command: dict[str, object]) -> None:
    configure(project, command)
    before = snapshot(project)
    with pytest.raises(StateOpError, match="pre-close"):
        state_ops.close("dev/tasks/recover", project)
    assert snapshot(project) == before


def test_present_plugin_script_runs_check(project: Path) -> None:
    script = project / "checker.py"
    script.write_text("print('{\"allowed\":false,\"parked\":[{\"id\":\"w-waiting\"}]}')\n")
    configure(project, {"argv": [sys.executable, str(script)], "required_paths": [str(script)]})
    before = snapshot(project)
    with pytest.raises(StateOpError, match="w-waiting"):
        state_ops.close("dev/tasks/recover", project)
    assert snapshot(project) == before


@pytest.mark.parametrize("operation", [state_ops.close, location_ops.archive_task])
def test_allowed_pre_close_runs_operation(project: Path, operation) -> None:
    configure(project, [sys.executable, "-c", "print('{\"allowed\":true,\"parked\":[]}')"])
    operation("dev/tasks/recover", project)
    folder = project / "dev/tasks/recover"
    assert not folder.exists() or yaml.safe_load((folder / "task.yaml").read_text())["task"]["status"] != "active"


def test_pre_close_blocks_archive_split_repair(project: Path) -> None:
    folder = project / "dev/tasks/recover"
    (folder / "task.yaml").write_text(yaml.safe_dump({"task": {"status": "archived"}}))
    shutil.copytree(folder, project / "dev/tasks/archived-tasks/recover")
    configure(project, [sys.executable, "-c", "print('{\"allowed\":false,\"parked\":[{\"id\":\"w-waiting\"}]}')"])
    before = snapshot(project)
    with pytest.raises(StateOpError, match="w-waiting"):
        location_ops.archive_task("dev/tasks/recover", project)
    assert snapshot(project) == before
