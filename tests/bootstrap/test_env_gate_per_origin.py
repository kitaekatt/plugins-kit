"""The env gate is keyed per origin, and a plain console pass writes no stamp.

Gap B: the env gate used to be ONE machine-wide stamp. Origin A's failing pass
stamped it "failed"; a clean pass from origin B with the same merged manifest
stamped it "clean"; A's next pass then skipped the env phase, reported no
env failures, and its queue rewrite dropped A's still-failing elevated fix.

Gap C: a plain `--console` pass (no --fix-all) ran the env phase and wrote the
stamp but skipped the queue step, so it closed the gate without touching the
queue. A plain console pass now writes no env stamp at all; the post-fix-all
re-check pass (`--recheck`) rewrites the queue AND its origin's stamp.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import bootstrap_lib.engine as engine
import bootstrap_lib.fix_queue as fq
from bootstrap.link_compat import link_tree
from bootstrap_lib.engine import _process_env_pass
from bootstrap_lib.env_manifest import ENV_STATE_STAMP, read_env_state

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_ROOT = REPO_ROOT / "plugins" / "bootstrap"
ENGINE_SCRIPT = BOOTSTRAP_ROOT / "engine" / "bootstrap_engine.py"
RESET_SCRIPT = BOOTSTRAP_ROOT / "scripts" / "env-reset-cooldown.sh"


def _find_bash():
    candidates = []
    if os.name == "nt":
        candidates += [r"C:\Program Files\Git\usr\bin\bash.exe",
                       r"C:\Program Files\Git\bin\bash.exe"]
    candidates.append(shutil.which("bash"))
    for c in candidates:
        if c and Path(c).exists() and "WindowsApps" not in c \
                and "System32" not in c:
            return c
    return None


BASH = _find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")


def _write_json(path: Path, content) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content))


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


# --------------------------------------------------------------------------- #
# 6. Gap B: one origin's clean pass cannot close another origin's gate
# --------------------------------------------------------------------------- #

class TestPerOriginGate:
    def test_clean_pass_elsewhere_keeps_a_failing_origins_gate_open(
            self, tmp_path, isolated_home, monkeypatch):
        monkeypatch.setattr(engine, "_privileges_available", lambda os_: False)
        marker = isolated_home / "configured"
        _write_json(isolated_home / ".claude" / "env.json", {
            "machines": {"testhost": {"os": "ubuntu"}},
            "env_checks": [{"name": "x",
                            "check": f"test -f {marker.as_posix()}",
                            "fix": "true", "elevated": True}],
        })
        data = tmp_path / "data"
        plugin = tmp_path / "plugin"
        data.mkdir()
        plugin.mkdir()
        project_a = tmp_path / "a"
        project_b = tmp_path / "b"
        project_a.mkdir()
        project_b.mkdir()

        def run(project):
            ok_entries: list = []
            failures = _process_env_pass(
                str(project), "ubuntu", str(data), str(plugin), [], ok_entries,
                engine_version="1.0.0", hostname="testhost")
            return failures, ok_entries

        first, _ = run(project_a)
        assert [f["elevation"]["id"] for f in first] == ["env_check:x"]

        # Origin B passes cleanly while the check happens to pass.
        marker.write_text("")
        clean, _ = run(project_b)
        assert clean == []
        marker.unlink()

        # A's next pass must still run the phase and keep the deferral.
        third, ok_entries = run(project_a)
        assert any(e.startswith("running (") for e in ok_entries), ok_entries
        assert [f["elevation"]["id"] for f in third] == ["env_check:x"]

        tasks = fq.queue_from_failures(third, "ubuntu", origin=str(project_a))
        monkeypatch.setattr(fq, "resolve_bash", lambda: "/usr/bin/bash")
        qpath = fq.write_or_clear_queue(tasks, str(data), "ubuntu",
                                        origin=str(project_a))
        body = json.loads(Path(qpath).read_text())
        assert [t["id"] for t in body["tasks"]] == ["env_check:x"]
        assert body["tasks"][0].get("entry_sha256")

    def test_stamps_are_recorded_per_key(self, tmp_path):
        from bootstrap_lib.env_manifest import write_env_state
        data = tmp_path / "data"
        data.mkdir()
        write_env_state(str(data), "h", "1.0.0", "failed", project_key="a")
        write_env_state(str(data), "h", "1.0.0", "clean", project_key="b")

        assert read_env_state(str(data), project_key="a")["last_result"] == "failed"
        assert read_env_state(str(data), project_key="b")["last_result"] == "clean"
        assert read_env_state(str(data), project_key="c") is None

    def test_old_machine_wide_stamp_reads_as_no_stamp(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        (data / ENV_STATE_STAMP).write_text(json.dumps({
            "manifest_sha256": "h", "engine_version": "1.0.0",
            "last_result": "clean"}))

        assert read_env_state(str(data), project_key="a") is None


# --------------------------------------------------------------------------- #
# 7. Gap C: console writes no stamp; the re-check pass writes queue AND stamp
# --------------------------------------------------------------------------- #

class TestConsoleAndRecheck:
    def _setup(self, tmp_path):
        from bootstrap_lib.platform_detect import detect_os

        fake_root = tmp_path / "plugins" / "bootstrap"
        fake_root.mkdir(parents=True)
        link_tree(fake_root / "bootstrap_lib", str(BOOTSTRAP_ROOT / "bootstrap_lib"))
        link_tree(fake_root / "engine", str(BOOTSTRAP_ROOT / "engine"))
        defaults = fake_root / "defaults"
        defaults.mkdir()
        (defaults / "config.json").write_text(json.dumps({
            "schema_version": 5, "no_bootstrap": [], "bootstrap_cache": [],
            "log_success_shell": False, "log_success_checks": False,
            "self_setup": {},
        }))
        (fake_root / "bootstrap.json").write_text(json.dumps({}))

        home = tmp_path / "_home"
        _write_json(home / ".claude" / "env.json", {
            "machines": {socket.gethostname(): {"os": detect_os()}},
            "env_checks": [{"name": "ok", "check": "true"}],
        })
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        # A stale record queued by another project for a check that no
        # longer exists anywhere.
        qpath = Path(fq.queue_path(str(data_dir)))
        _write_json(qpath, {"version": 1, "os": detect_os(),
                            "bash": "/usr/bin/bash", "tasks": [{
                                "id": "env_check:deleted", "kind": "command",
                                "label": "deleted", "origin": "/gone-project",
                                "elevated": True, "command": "harmful"}]})
        env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
        env.pop("CLAUDE_ENV_FILE", None)
        return fake_root, data_dir, qpath, env

    def _run(self, fake_root, data_dir, env, *flags):
        return subprocess.run(
            [sys.executable, str(ENGINE_SCRIPT),
             "--plugin-root", str(fake_root),
             "--data-dir", str(data_dir), *flags],
            capture_output=True, text=True, env=env, timeout=300,
        )

    def test_plain_console_writes_no_env_stamp(self, tmp_path):
        fake_root, data_dir, qpath, env = self._setup(tmp_path)

        result = self._run(fake_root, data_dir, env, "--console")

        assert result.returncode == 0, result.stderr
        assert not (data_dir / ENV_STATE_STAMP).exists()
        # Consistent with the queue, which a plain console pass never writes.
        assert qpath.exists()

    def test_recheck_pass_rewrites_queue_and_stamp(self, tmp_path):
        fake_root, data_dir, qpath, env = self._setup(tmp_path)

        result = self._run(fake_root, data_dir, env, "--console", "--recheck")

        assert result.returncode == 0, result.stderr
        assert not qpath.exists(), result.stdout
        state = read_env_state(str(data_dir), project_key="_global_")
        assert state is not None and state["last_result"] == "clean"


# --------------------------------------------------------------------------- #
# 10. The reset lever clears every origin's stamp
# --------------------------------------------------------------------------- #

@needs_bash
class TestResetLever:
    def test_reset_clears_every_per_key_stamp(self, tmp_path):
        home = tmp_path / "home"
        data = home / ".claude" / "plugins" / "data" / "plugins-kit" / "bootstrap"
        data.mkdir(parents=True)
        from bootstrap_lib.env_manifest import write_env_state
        write_env_state(str(data), "h", "1.0.0", "clean", project_key="a")
        write_env_state(str(data), "h", "1.0.0", "failed", project_key="b")
        env = dict(os.environ, HOME=str(home))
        env.pop("CLAUDE_BOOTSTRAP_DATA_ROOT", None)
        env.pop("BOOTSTRAP_MARKETPLACE", None)

        result = subprocess.run([BASH, str(RESET_SCRIPT)],
                                capture_output=True, text=True, env=env)

        assert result.returncode == 0, result.stderr
        assert read_env_state(str(data), project_key="a") is None
        assert read_env_state(str(data), project_key="b") is None
