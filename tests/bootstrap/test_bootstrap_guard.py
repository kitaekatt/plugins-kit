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
        mod = _load_canon()
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(tmp_path))
        py = self._fake_venv_python(tmp_path, "p4-kit")
        monkeypatch.delenv(mod._REEXEC_GUARD_ENV, raising=False)
        monkeypatch.setattr(mod.sys, "executable", str(py))
        called = []
        monkeypatch.setattr(mod.os, "execv", lambda *a: called.append(a))
        mod.reexec_under_plugin_venv("p4-kit")
        assert called == []

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
