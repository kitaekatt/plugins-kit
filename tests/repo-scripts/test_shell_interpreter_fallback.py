"""BOOTSTRAP_PYTHON / BOOTSTRAP_PROJECT_PYTHON fallback behavior for the shell
launchers routed through the interpreter standard (U5a-r).

Every launcher below already resolves a DETERMINISTIC candidate (a plugin venv
path or the bootstrap-provisioned standalone install path) before falling
through to a bare PATH lookup -- the Windows Store stub trap
(host_python_via_plugin_venv / manifest_changes_need_version_bump lineage in
root CLAUDE.md). This adds exactly one fallback tier in between: BOOTSTRAP_PYTHON
is accepted only when the deterministic candidate is absent AND
BOOTSTRAP_PYTHON's own realpath resolves inside the standalone install
directory (``.local/share/python-standalone``, interpreter_env.STANDALONE_DIR_REL).
A value pointing anywhere else is ignored, falling through to the existing PATH
chain unchanged -- never trusted blind, which would let an attacker- or
misconfiguration-controlled BOOTSTRAP_PYTHON substitute an arbitrary
interpreter.

Each test proves the fallback fires (or is correctly rejected) by making the
candidate executables identifiable shell stubs that write a distinct marker,
rather than mocking the resolution logic -- so a revert of the real script
(back to no BOOTSTRAP_PYTHON tier, or back to a bug in the tier) shows up here
as a wrong marker.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BASH = shutil.which("bash")


def _write_stub(path: Path, marker: str) -> None:
    """A minimal POSIX shell stub that identifies itself by printing `marker`
    and does nothing else -- safe to "run" in place of a real interpreter."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho {marker}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run(script: Path, home: Path, extra_env: dict, *, stdin: str = "") -> subprocess.CompletedProcess:
    assert _BASH, "bash not found on PATH"
    env = dict(os.environ)
    env.pop("BOOTSTRAP_PYTHON", None)
    env.pop("BOOTSTRAP_PROJECT_PYTHON", None)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env.update(extra_env)
    return subprocess.run(
        [_BASH, str(script)], input=stdin, env=env,
        capture_output=True, text=True, timeout=30,
    )


@pytest.fixture
def home(tmp_path):
    """A throwaway HOME with no deterministic standalone/venv Python anywhere,
    plus one candidate placed INSIDE the standalone directory and one placed
    OUTSIDE it, so a test only has to pick which candidate BOOTSTRAP_PYTHON
    names."""
    h = tmp_path / "home"
    inside = h / ".local" / "share" / "python-standalone" / "alt" / "python3"
    outside = h / "elsewhere" / "python3"
    _write_stub(inside, "MARKER_INSIDE_STANDALONE")
    _write_stub(outside, "MARKER_OUTSIDE_STANDALONE")
    return h, inside, outside


class TestHueKitAndSecretsKitShape:
    """hue-kit and secrets-kit share one deterministic-candidate-then-PATH
    shape; hue-kit is exercised directly, secrets-kit's identical shape is
    pinned via the parametrized second file."""

    @pytest.mark.parametrize("rel", [
        "plugins/hue-kit/bin/hue-kit",
        "plugins/secrets-kit/bin/secrets-kit",
    ])
    def test_uses_validated_bootstrap_python_when_deterministic_absent(self, home, rel):
        h, inside, _outside = home
        result = _run(_REPO_ROOT / rel, h, {"BOOTSTRAP_PYTHON": str(inside)})
        assert "MARKER_INSIDE_STANDALONE" in result.stdout, result.stderr

    @pytest.mark.parametrize("rel", [
        "plugins/hue-kit/bin/hue-kit",
        "plugins/secrets-kit/bin/secrets-kit",
    ])
    def test_ignores_bootstrap_python_outside_standalone_dir(self, home, rel):
        h, _inside, outside = home
        result = _run(_REPO_ROOT / rel, h, {"BOOTSTRAP_PYTHON": str(outside)})
        assert "MARKER_OUTSIDE_STANDALONE" not in result.stdout


class TestVenvChainShims:
    """job-kit and llm-scripting-kit insert the same validated fallback
    between their venv/standalone deterministic tiers and the PATH tier."""

    @pytest.mark.parametrize("rel", [
        "plugins/job-kit/bin/job-kit",
        "plugins/llm-scripting-kit/bin/llm-scripting-kit",
    ])
    def test_uses_validated_bootstrap_python_when_deterministic_absent(self, home, rel):
        h, inside, _outside = home
        result = _run(_REPO_ROOT / rel, h, {"BOOTSTRAP_PYTHON": str(inside)})
        assert "MARKER_INSIDE_STANDALONE" in result.stdout, result.stderr

    @pytest.mark.parametrize("rel", [
        "plugins/job-kit/bin/job-kit",
        "plugins/llm-scripting-kit/bin/llm-scripting-kit",
    ])
    def test_ignores_bootstrap_python_outside_standalone_dir(self, home, rel):
        h, _inside, outside = home
        result = _run(_REPO_ROOT / rel, h, {"BOOTSTRAP_PYTHON": str(outside)})
        assert "MARKER_OUTSIDE_STANDALONE" not in result.stdout


class TestRepairRegistryStaysDependencyFree:
    """bootstrap-stuck-fix's repair-registry.sh must still work when
    BOOTSTRAP_PYTHON is absent (its whole point is to run on a machine
    bootstrap cannot reach) and must accept it only when validated."""

    _SCRIPT = _REPO_ROOT / "plugins" / "bootstrap-stuck-fix" / "hooks" / "sessionstart" / "repair-registry.sh"

    def test_uses_validated_bootstrap_python(self, home, tmp_path):
        h, inside, _outside = home
        plugin_root = tmp_path / "plugin"
        scripts = plugin_root / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "repair_registry.py").write_text("# real script would go here\n")
        result = _run(
            self._SCRIPT, h,
            {"BOOTSTRAP_PYTHON": str(inside), "CLAUDE_PLUGIN_ROOT": str(plugin_root)},
        )
        assert "MARKER_INSIDE_STANDALONE" in result.stdout, result.stderr
        assert result.returncode == 0

    def test_ignores_bootstrap_python_outside_standalone_dir(self, home, tmp_path):
        h, _inside, outside = home
        plugin_root = tmp_path / "plugin"
        scripts = plugin_root / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "repair_registry.py").write_text("# real script would go here\n")
        result = _run(
            self._SCRIPT, h,
            {"BOOTSTRAP_PYTHON": str(outside), "CLAUDE_PLUGIN_ROOT": str(plugin_root)},
        )
        assert "MARKER_OUTSIDE_STANDALONE" not in result.stdout
        # Still never fails the session even with nothing usable on PATH either.
        assert result.returncode == 0


class TestPreCommitScriptsPreferProjectPython:
    """check-staged-version-bump.sh and pre-commit-version-check.sh keep their
    deterministic .venv chain but prefer BOOTSTRAP_PROJECT_PYTHON -- the
    already-resolved answer for this checkout -- ahead of it."""

    def _publish_repo(self, tmp_path):
        repo = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
        plugin = repo / "plugins" / "foo" / ".claude-plugin"
        plugin.mkdir(parents=True)
        (plugin / "plugin.json").write_text('{"name": "foo", "version": "1.0.0"}\n')
        (repo / "plugins" / "foo" / "code.py").write_text("x = 1\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
        (repo / "scripts").mkdir()
        shutil.copy(_REPO_ROOT / "scripts" / "publish.py", repo / "scripts" / "publish.py")
        subprocess.run(["git", "-C", str(repo), "add", "scripts"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "scripts"], check=True)
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()
        subprocess.run(
            ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", head],
            check=True)
        return repo

    def test_check_staged_version_bump_uses_project_python(self, tmp_path):
        repo = self._publish_repo(tmp_path)
        stub = tmp_path / "projpy" / "python3"
        _write_stub(stub, "PROJECT_PY_BASE_abc123")
        (repo / "plugins" / "foo" / "code.py").write_text("x = 2\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)

        env = dict(os.environ)
        env.pop("PLUGINS_KIT_SKIP_BUMP_CHECK", None)
        env["BOOTSTRAP_PROJECT_PYTHON"] = str(stub)
        result = subprocess.run(
            [_BASH, str(_REPO_ROOT / "scripts" / "check-staged-version-bump.sh")],
            cwd=repo, env=env, capture_output=True, text=True,
        )
        # The stub's stdout ("PROJECT_PY_BASE_abc123") is threaded through as
        # the resolved publish-point ref, which cannot resolve in this throwaway
        # clone -- proving BOOTSTRAP_PROJECT_PYTHON, not a real interpreter,
        # answered `scripts/publish.py --print-range-base`.
        assert "PROJECT_PY_BASE_abc123" in result.stderr, result.stderr

    def test_pre_commit_version_check_uses_project_python(self, tmp_path):
        repo = self._publish_repo(tmp_path)
        stub = tmp_path / "projpy2" / "python3"
        _write_stub(stub, "PROJECT_PY_PCVC_MARKER")

        env = dict(os.environ)
        env["BOOTSTRAP_PROJECT_PYTHON"] = str(stub)
        result = subprocess.run(
            [_BASH, str(_REPO_ROOT / "scripts" / "pre-commit-version-check.sh")],
            cwd=repo, env=env, capture_output=True, text=True,
        )
        assert "PROJECT_PY_PCVC_MARKER" in result.stdout, result.stderr


def _write_marker_stub(path: Path, marker_file: Path) -> None:
    """A stub whose invocation is provable even when the caller redirects its
    own stdout/stderr to /dev/null -- it writes to an explicitly named file
    instead of relying on its own stdout."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho invoked >> "{marker_file.as_posix()}"\n')
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class TestBootstrapDisplayHarvestInterpreter:
    """bootstrap-display.sh's per-prompt harvest invokes the same
    deterministic-then-validated-BOOTSTRAP_PYTHON-then-PATH chain; its own
    stdout/stderr are redirected to /dev/null, so invocation is observed
    through a marker FILE the stub writes instead."""

    _SCRIPT = _REPO_ROOT / "plugins" / "bootstrap" / "hooks" / "userpromptsubmit" / "bootstrap-display.sh"

    def test_uses_validated_bootstrap_python_for_the_harvest(self, home, tmp_path):
        h, inside, _outside = home
        marker = tmp_path / "harvest-invoked.txt"
        _write_marker_stub(inside, marker)
        result = _run(
            self._SCRIPT, h,
            {"BOOTSTRAP_PYTHON": str(inside), "CLAUDE_BOOTSTRAP_DATA_ROOT": str(tmp_path / "data")},
            stdin="",
        )
        assert marker.is_file(), result.stderr

    def test_ignores_bootstrap_python_outside_standalone_dir(self, home, tmp_path):
        h, _inside, outside = home
        marker = tmp_path / "harvest-invoked-outside.txt"
        _write_marker_stub(outside, marker)
        _run(
            self._SCRIPT, h,
            {"BOOTSTRAP_PYTHON": str(outside), "CLAUDE_BOOTSTRAP_DATA_ROOT": str(tmp_path / "data")},
            stdin="",
        )
        assert not marker.exists()


class TestDiagnosePythonVenvInterpreter:
    """diagnose-python-venv.sh's registry-parsing step (section 6) used to
    shell out to `uv run --no-project python`; it now follows the same
    deterministic-then-validated-fallback-then-PATH chain, with no uv
    dependency for this step at all."""

    _SCRIPT = _REPO_ROOT / "plugins" / "bootstrap" / "scripts" / "diagnose-python-venv.sh"

    @staticmethod
    def _prep_registry(home_dir: Path) -> None:
        registry_dir = home_dir / ".claude" / "plugins"
        registry_dir.mkdir(parents=True, exist_ok=True)
        (registry_dir / "installed_plugins.json").write_text('{"plugins": {}}\n')

    def test_uses_validated_bootstrap_python(self, home):
        h, inside, _outside = home
        self._prep_registry(h)
        result = _run(self._SCRIPT, h, {"BOOTSTRAP_PYTHON": str(inside)})
        assert "MARKER_INSIDE_STANDALONE" in result.stdout, result.stderr

    def test_ignores_bootstrap_python_outside_standalone_dir(self, home):
        h, _inside, outside = home
        self._prep_registry(h)
        result = _run(self._SCRIPT, h, {"BOOTSTRAP_PYTHON": str(outside)})
        assert "MARKER_OUTSIDE_STANDALONE" not in result.stdout


# check-editor-build-fresh.sh's detector-resolution chain (identical shape to
# the classes above) is deliberately NOT pytest-covered here: it spawns the
# detector in a doubly-backgrounded, fully-detached subshell (stdout/stderr to
# /dev/null) so the <30ms foreground latency budget is never touched, and that
# detachment is not reliably observable through Python's subprocess process
# tree on this host -- the orphaned grandchild does not survive past the
# parent bash process exiting under subprocess.run on Windows, even though it
# does survive a direct interactive bash invocation. Verified manually instead
# (isolated scratchpad HOME, a marker-file stub in place of BOOTSTRAP_PYTHON,
# both the accepted-inside-the-standalone-dir and rejected-outside-it cases),
# during this unit's implementation.
