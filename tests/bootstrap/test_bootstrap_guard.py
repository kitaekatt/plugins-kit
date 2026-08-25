"""Tests for bootstrap_lib/bootstrap_guard.py and its vendored copies.

bootstrap_guard.py is vendored (copied byte-for-byte) into every plugin that
needs a runtime bootstrap-presence guard. Because the guard must run when
bootstrap_lib itself may be absent, each plugin ships a standalone copy rather
than importing the canonical. This test asserts every vendored copy is
byte-identical to the canonical, so the copies cannot silently drift.
"""

import filecmp
import importlib.util
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CANON = _REPO_ROOT / "plugins" / "bootstrap" / "bootstrap_lib" / "bootstrap_guard.py"


def _vendored_copies():
    """Every bootstrap_guard.py under plugins/ except the canonical and any that
    live inside a virtualenv / site-packages / cache dir."""
    skip = {".venv", "site-packages", "__pycache__", "node_modules"}
    out = []
    for p in _REPO_ROOT.glob("plugins/**/bootstrap_guard.py"):
        if p.resolve() == _CANON.resolve():
            continue
        if any(part in skip for part in p.parts):
            continue
        out.append(p)
    return out


def _load_canon():
    spec = importlib.util.spec_from_file_location("_bootstrap_guard_canon", _CANON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestCanonical:
    def test_canonical_exists(self):
        assert _CANON.is_file(), f"Canonical bootstrap_guard missing: {_CANON}"

    def test_stdlib_only_no_bootstrap_lib_import(self):
        # The guard must never IMPORT bootstrap_lib -- that's the thing it detects
        # the absence of. (Mentions in the docstring are fine; we check imports.)
        import ast
        tree = ast.parse(_CANON.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    assert not n.name.startswith("bootstrap_lib"), n.name
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("bootstrap_lib"), node.module

    def test_is_provisioned_false_for_unknown_plugin(self, tmp_path, monkeypatch):
        mod = _load_canon()
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(tmp_path))
        assert mod.is_provisioned("definitely-not-a-real-plugin") is False

    def test_is_provisioned_true_when_log_present(self, tmp_path, monkeypatch):
        mod = _load_canon()
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(tmp_path))
        d = tmp_path / ".claude" / "plugins" / "data" / "plugins-kit" / "myplugin"
        d.mkdir(parents=True)
        (d / "bootstrap.log").write_text("ok", encoding="utf-8")
        assert mod.is_provisioned("myplugin") is True

    def test_require_bootstrap_exits_when_absent(self, tmp_path, monkeypatch):
        import pytest
        mod = _load_canon()
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(tmp_path))
        with pytest.raises(SystemExit) as exc:
            mod.require_bootstrap("myplugin", feature="testing")
        assert exc.value.code == mod.EXIT_BOOTSTRAP_MISSING

    def test_require_bootstrap_force_always_exits(self, tmp_path, monkeypatch):
        import pytest
        mod = _load_canon()
        # Even when provisioned, force=True must exit (used in except-ImportError).
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(tmp_path))
        d = tmp_path / ".claude" / "plugins" / "data" / "plugins-kit" / "myplugin"
        d.mkdir(parents=True)
        (d / "bootstrap.log").write_text("ok", encoding="utf-8")
        with pytest.raises(SystemExit):
            mod.require_bootstrap("myplugin", missing="bootstrap_lib", force=True)


class TestVendoredCopies:
    def test_at_least_one_vendored_copy_exists(self):
        assert _vendored_copies(), "no vendored bootstrap_guard.py copies found"

    def test_awesome_kit_task_system_copy_present(self):
        # The task-system CLI (task skill) re-execs via its own vendored
        # guard; assert the copy exists and is picked up by the glob so a
        # rename/move can't silently drop it from the drift check.
        vendored = (
            _REPO_ROOT / "plugins" / "awesome-kit" / "skills" / "task"
            / "scripts" / "bootstrap_guard.py"
        )
        assert vendored.is_file(), f"missing vendored copy: {vendored}"
        assert vendored in _vendored_copies()

    def test_vendored_copies_match_canon(self):
        diffs = []
        for vendored in _vendored_copies():
            if not filecmp.cmp(_CANON, vendored, shallow=False):
                diffs.append(str(vendored.relative_to(_REPO_ROOT)))
        assert not diffs, f"vendored bootstrap_guard.py diverged from canonical: {diffs}"


class TestReexecUnderPluginVenv:
    """reexec_under_plugin_venv() re-execs the process under the plugin's
    bootstrap-provisioned venv so a script invoked by a bare python / uv run
    still gains the shared-lib .pth. It must be a safe no-op in every case
    where re-exec is unnecessary or impossible (loop guard, missing venv,
    already-active venv)."""

    def _fake_venv_python(self, tmp_path, plugin, marketplace="plugins-kit"):
        base = (tmp_path / ".claude" / "plugins" / "data" / marketplace / plugin
                / ".venv" / "Scripts")
        base.mkdir(parents=True)
        py = base / "python.exe"
        py.write_text("", encoding="utf-8")
        return py

    def test_plugin_venv_python_found(self, tmp_path, monkeypatch):
        mod = _load_canon()
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(tmp_path))
        py = self._fake_venv_python(tmp_path, "p4-kit")
        got = mod.plugin_venv_python("p4-kit")
        assert got is not None and got.resolve() == py.resolve()

    def test_plugin_venv_python_missing(self, tmp_path, monkeypatch):
        mod = _load_canon()
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(tmp_path))
        assert mod.plugin_venv_python("p4-kit") is None

    def test_reexec_noop_when_guard_env_set(self, tmp_path, monkeypatch):
        mod = _load_canon()
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(tmp_path))
        self._fake_venv_python(tmp_path, "p4-kit")
        monkeypatch.setenv(mod._REEXEC_GUARD_ENV, "1")
        called = []
        monkeypatch.setattr(mod.os, "execv", lambda *a: called.append(a))
        mod.reexec_under_plugin_venv("p4-kit")
        assert called == []

    def test_reexec_noop_when_venv_missing(self, tmp_path, monkeypatch):
        mod = _load_canon()
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(tmp_path))
        monkeypatch.delenv(mod._REEXEC_GUARD_ENV, raising=False)
        called = []
        monkeypatch.setattr(mod.os, "execv", lambda *a: called.append(a))
        mod.reexec_under_plugin_venv("p4-kit")
        assert called == []

    def test_reexec_noop_when_already_under_venv(self, tmp_path, monkeypatch):
        """Being inside the venv is decided by sys.prefix -- the signal Python
        derives from the path the interpreter was INVOKED by."""
        mod = _load_canon()
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(tmp_path))
        py = self._fake_venv_python(tmp_path, "p4-kit")
        monkeypatch.delenv(mod._REEXEC_GUARD_ENV, raising=False)
        monkeypatch.setattr(mod.sys, "executable", str(py))
        monkeypatch.setattr(mod.sys, "prefix", str(py.parent.parent))
        called = []
        monkeypatch.setattr(mod.os, "execv", lambda *a: called.append(a))
        mod.reexec_under_plugin_venv("p4-kit")
        assert called == []

    def test_reexec_happens_when_venv_python_symlinks_to_the_running_base(
        self, tmp_path, monkeypatch
    ):
        """Regression: uv builds `.venv/bin/python` as a SYMLINK to the base
        interpreter. Comparing resolved interpreter PATHS collapses both sides
        onto that base and reports "already provisioned" while the process is
        still running outside the venv -- with none of its site-packages. It
        misfired exactly when the caller was the standalone python every plugin
        launcher shim uses, so the common case, not a corner one. Observed as
        hue-kit reporting `zeroconf` unprovisioned when it was installed."""
        import pytest
        mod = _load_canon()
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(tmp_path))

        base = tmp_path / "python-standalone" / "bin"
        base.mkdir(parents=True)
        base_py = base / "python3"
        base_py.write_text("", encoding="utf-8")

        venv_bin = (tmp_path / ".claude" / "plugins" / "data" / "plugins-kit"
                    / "hue-kit" / ".venv" / "bin")
        venv_bin.mkdir(parents=True)
        venv_py = venv_bin / "python"
        try:
            venv_py.symlink_to(base_py)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        # Running the BASE interpreter directly: same file as venv_py once
        # resolved, but sys.prefix says we are outside the venv.
        monkeypatch.setattr(mod.sys, "executable", str(base_py))
        monkeypatch.setattr(mod.sys, "prefix", str(base_py.parent.parent))
        monkeypatch.setattr(mod.sys, "argv", ["hue_kit_cli.py", "discover"])
        monkeypatch.delenv(mod._REEXEC_GUARD_ENV, raising=False)

        # The precondition that made the old check wrong.
        assert base_py.resolve() == venv_py.resolve()

        captured = {}

        def fake_execv(path, args):
            captured["path"] = path
            raise SystemExit(0)

        monkeypatch.setattr(mod.os, "execv", fake_execv)
        with pytest.raises(SystemExit):
            mod.reexec_under_plugin_venv("hue-kit")
        assert captured["path"] == str(venv_py), (
            "must re-exec via the VENV path so pyvenv.cfg activates the venv"
        )

    def test_reexec_execs_into_venv_when_needed(self, tmp_path, monkeypatch):
        import pytest
        mod = _load_canon()
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(tmp_path))
        py = self._fake_venv_python(tmp_path, "p4-kit")
        monkeypatch.setattr(mod.sys, "executable", str(tmp_path / "other.exe"))
        monkeypatch.setattr(mod.sys, "argv", ["script.py", "152779"])
        monkeypatch.delenv(mod._REEXEC_GUARD_ENV, raising=False)
        captured = {}

        def fake_execv(path, args):
            captured["path"] = path
            captured["args"] = args
            # os.execv replaces the process; simulate that the call terminates here.
            raise SystemExit(0)

        monkeypatch.setattr(mod.os, "execv", fake_execv)
        with pytest.raises(SystemExit):
            mod.reexec_under_plugin_venv("p4-kit")
        assert captured["path"] == str(py)
        assert captured["args"] == [str(py), "script.py", "152779"]
        # The loop-guard env flag is set before handing off to the new interpreter.
        assert mod.os.environ.get(mod._REEXEC_GUARD_ENV) == "1"

    def test_reexec_on_windows_waits_and_propagates_exit_code(self, tmp_path, monkeypatch):
        """On Windows the re-exec must SPAWN-AND-WAIT, never os.execv.

        Windows has no exec: CPython's os.execv goes through the CRT _execv,
        which spawns the replacement and terminates the caller immediately.
        The parent then returns exit 0 before the child has produced anything,
        and the child is orphaned rather than waited on -- so a caller reading
        our stdout gets an empty stream and a false success. This surfaced as
        git-kit's prepare_review.py exiting 0 with no JSON on stdout while
        having correctly written bundle.json to disk.
        """
        import pytest
        mod = _load_canon()
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(tmp_path))
        py = self._fake_venv_python(tmp_path, "git-kit")
        monkeypatch.setattr(mod.sys, "executable", str(tmp_path / "other.exe"))
        monkeypatch.setattr(mod.sys, "argv", ["prepare_review.py", "--staged"])
        monkeypatch.delenv(mod._REEXEC_GUARD_ENV, raising=False)
        # Patch the seam, NOT os.name: pathlib reads os.name at call time, so
        # forcing it to "nt" on a POSIX runner makes every Path() in the module
        # raise NotImplementedError('cannot instantiate WindowsPath').
        monkeypatch.setattr(mod, "_is_windows", lambda: True)

        def forbidden_execv(path, args):  # pragma: no cover - must not run
            raise AssertionError("os.execv must not be used on Windows")

        monkeypatch.setattr(mod.os, "execv", forbidden_execv)

        import subprocess
        captured = {}

        class _Completed:
            returncode = 3

        def fake_run(args, **kwargs):
            captured["args"] = args
            return _Completed()

        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(SystemExit) as excinfo:
            mod.reexec_under_plugin_venv("git-kit")

        # The child is WAITED on, and its exit code becomes ours -- not 0.
        assert excinfo.value.code == 3, (
            "the child's exit code must propagate; returning 0 regardless is "
            "the os.execv-on-Windows bug this branch exists to avoid"
        )
        assert captured["args"] == [str(py), "prepare_review.py", "--staged"]
        assert mod.os.environ.get(mod._REEXEC_GUARD_ENV) == "1"
