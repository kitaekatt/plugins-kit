"""task-kit's statusline segment: the bootstrap installer and segment.sh itself."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

STATUSLINE = Path(__file__).resolve().parents[2] / "plugins" / "task-kit" / "statusline"
sys.path.insert(0, str(STATUSLINE))

import install_segment  # noqa: E402  (red until the installer exists)


class FakeCtx:
    def __init__(self, data_dir):
        self.data_dir = str(data_dir)
        self.project_dir = None
        self.logs, self.ok_logs = [], []

    def log(self, msg):
        self.logs.append(msg)

    def log_ok(self, msg):
        self.ok_logs.append(msg)


@pytest.fixture(autouse=True)
def isolated_config(tmp_path_factory, monkeypatch):
    monkeypatch.setattr(install_segment, "CONFIG_PATH", tmp_path_factory.mktemp("cfg") / "task.local.yaml")


def rig(tmp_path):
    root = tmp_path / "data" / "plugins-kit"
    task_data = root / "task-kit"
    (task_data / "statusline").mkdir(parents=True)
    shutil.copy(STATUSLINE / "segment.sh", task_data / "statusline" / "segment.sh")
    return root, task_data


def test_installer_copies_the_segment_as_99(tmp_path):
    root, task_data = rig(tmp_path)
    target = root / "claude-ui-kit" / "segments" / "99-task.sh"
    (root / "claude-ui-kit").mkdir()
    ctx = FakeCtx(task_data)
    install_segment.install(ctx)
    assert target.read_text() == (STATUSLINE / "segment.sh").read_text()
    assert ctx.logs and not ctx.ok_logs


def test_installer_is_a_logged_noop_when_current(tmp_path):
    root, task_data = rig(tmp_path)
    (root / "claude-ui-kit").mkdir()
    install_segment.install(FakeCtx(task_data))
    target = root / "claude-ui-kit" / "segments" / "99-task.sh"
    before = target.stat().st_mtime_ns
    ctx = FakeCtx(task_data)
    install_segment.install(ctx)
    assert target.stat().st_mtime_ns == before
    assert ctx.ok_logs and not ctx.logs


def test_installer_skips_loudly_without_claude_ui_kit(tmp_path):
    root, task_data = rig(tmp_path)
    ctx = FakeCtx(task_data)
    install_segment.install(ctx)
    assert not (root / "claude-ui-kit").exists()
    assert ctx.ok_logs


needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq")


def run_segment(tmp_path, sid, stdin_sid, extra_env=None):
    data_root = tmp_path / "data"
    task = tmp_path / "proj" / "better-statusbar"
    task.mkdir(parents=True)
    (task / "task.yaml").write_text("task:\n  _schema_version: '1'\n  title: x\n  status: active\n")
    ptr = data_root / "plugins-kit" / "task-kit" / "statusline"
    ptr.mkdir(parents=True)
    (ptr / sid).write_text(str(task) + "\n")
    env = dict(os.environ, CLAUDE_BOOTSTRAP_DATA_ROOT=str(data_root))
    env.pop("BOOTSTRAP_BIN_JQ", None)
    env.update(extra_env or {})
    return subprocess.run(["bash", str(STATUSLINE / "segment.sh")], input=json.dumps({"session_id": stdin_sid}),
                          capture_output=True, text=True, env=env, timeout=10)


def test_segment_is_silent_when_jq_is_declared_absent(tmp_path):
    # set-but-empty BOOTSTRAP_BIN_JQ declares "this machine has no jq"; the cell must vanish quietly.
    r = run_segment(tmp_path, "s1", "s1", {"BOOTSTRAP_BIN_JQ": ""})
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""


@needs_jq
def test_segment_uses_the_jq_override(tmp_path):
    r = run_segment(tmp_path, "s1", "s1", {"BOOTSTRAP_BIN_JQ": shutil.which("jq")})
    assert "better-statusbar [active]" in r.stdout


@needs_jq
def test_segment_prints_folder_and_status(tmp_path):
    r = run_segment(tmp_path, "s1", "s1")
    assert r.returncode == 0
    assert "better-statusbar [active]" in r.stdout


@needs_jq
def test_segment_prints_nothing_for_an_unknown_session(tmp_path):
    r = run_segment(tmp_path, "s1", "other")
    assert r.returncode == 0 and r.stdout == ""


def cfg(tmp_path, text):
    path = tmp_path / "task.local.yaml"
    path.write_text(text)
    return path


def test_cell_defaults_on_without_a_config_file(tmp_path):
    assert install_segment.cell_enabled(tmp_path / "absent.yaml") is True


def test_cell_off_removes_the_owned_segment_and_logs(tmp_path, monkeypatch):
    root, task_data = rig(tmp_path)
    (root / "claude-ui-kit").mkdir()
    install_segment.install(FakeCtx(task_data))
    target = root / "claude-ui-kit" / "segments" / "99-task.sh"
    assert target.is_file()
    monkeypatch.setattr(install_segment, "CONFIG_PATH", cfg(tmp_path, "statusline:\n  cell: false\n"))
    ctx = FakeCtx(task_data)
    install_segment.install(ctx)
    assert not target.exists()
    assert any("removed" in m for m in ctx.logs)
    ctx = FakeCtx(task_data)
    install_segment.install(ctx)
    assert not target.exists() and ctx.ok_logs and not ctx.logs


def test_cell_off_leaves_a_foreign_segment_alone(tmp_path, monkeypatch):
    root, task_data = rig(tmp_path)
    target = root / "claude-ui-kit" / "segments" / "99-task.sh"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\necho mine\n")
    monkeypatch.setattr(install_segment, "CONFIG_PATH", cfg(tmp_path, "statusline:\n  cell: false\n"))
    install_segment.install(FakeCtx(task_data))
    assert target.read_text() == "#!/bin/sh\necho mine\n"


def test_cell_on_installs_as_before(tmp_path, monkeypatch):
    root, task_data = rig(tmp_path)
    (root / "claude-ui-kit").mkdir()
    monkeypatch.setattr(install_segment, "CONFIG_PATH", cfg(tmp_path, "statusline:\n  cell: true\n"))
    install_segment.install(FakeCtx(task_data))
    assert (root / "claude-ui-kit" / "segments" / "99-task.sh").is_file()


@pytest.mark.parametrize("text", ["statusline:\n  cell: maybe\n", "statusline:\n  bogus: 1\n", "statusline: 3\n"])
def test_bad_config_fails_loudly_and_changes_nothing(tmp_path, monkeypatch, text):
    root, task_data = rig(tmp_path)
    (root / "claude-ui-kit").mkdir()
    monkeypatch.setattr(install_segment, "CONFIG_PATH", cfg(tmp_path, text))
    ctx = FakeCtx(task_data)
    install_segment.install(ctx)
    assert any("FAILED" in m for m in ctx.logs)
    assert not (root / "claude-ui-kit" / "segments").exists()
