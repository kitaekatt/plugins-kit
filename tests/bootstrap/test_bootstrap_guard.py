"""Tests for bootstrap_lib/bootstrap_guard.py and its vendored copies.

bootstrap_guard.py is vendored (copied byte-for-byte) into every plugin that
needs a runtime bootstrap-presence guard. Because the guard must run when
bootstrap_lib itself may be absent, each plugin ships a standalone copy rather
than importing the canonical. This test asserts every vendored copy is
byte-identical to the canonical, so the copies cannot silently drift.
"""

import filecmp
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CANON = _REPO_ROOT / "plugins" / "bootstrap" / "bootstrap_lib" / "bootstrap_guard.py"


def _vendored_copies():
    """Every bootstrap_guard.py under plugins/ and packages/ except the canonical
    and any that live inside a virtualenv / site-packages / cache dir. packages/
    is searched because the llm_kit source (and its guard copy) lives in
    packages/katsoft-llm-kit/src."""
    skip = {".venv", "site-packages", "__pycache__", "node_modules"}
    out = []
    for root in ("plugins", "packages"):
        for p in _REPO_ROOT.glob(f"{root}/**/bootstrap_guard.py"):
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


_SKIP_ENV = "_BOOTSTRAP_GUARD_SKIP_REEXEC"
_LOOP_ENV = "_BOOTSTRAP_GUARD_VENV_REEXEC"


@pytest.fixture(autouse=True)
def _no_ambient_skip_variable(monkeypatch):
    """Another test directory's conftest sets the skip variable process-wide
    (tests/awesome-kit/conftest.py is collected first in a full run). Every
    test here drives the real re-exec decision, so none may inherit it."""
    monkeypatch.delenv(_SKIP_ENV, raising=False)


def _clear_loop_flag(monkeypatch, mod):
    """Make the loop flag absent for this test AND restore it afterwards.

    ``reexec_under_plugin_venv`` writes the flag straight into ``os.environ``;
    a bare ``monkeypatch.delenv(..., raising=False)`` records nothing when the
    variable is already absent, so the value the guard writes would outlive the
    test and reach every later suite in the process. Set first, so the teardown
    has an original to restore.
    """
    monkeypatch.setenv(mod._REEXEC_GUARD_ENV, "")
    monkeypatch.delenv(mod._REEXEC_GUARD_ENV)


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

    def test_task_kit_task_system_copy_present(self):
        # The task-system CLI (task skill) re-execs via its own vendored
        # guard; assert the copy exists and is picked up by the glob so a
        # rename/move can't silently drop it from the drift check.
        vendored = (
            _REPO_ROOT / "plugins" / "task-kit" / "skills" / "task"
            / "scripts" / "bootstrap_guard.py"
        )
        assert vendored.is_file(), f"missing vendored copy: {vendored}"
        assert vendored in _vendored_copies()

    def test_llm_kit_package_copy_present(self):
        # The llm_kit guard copy lives in the katsoft-llm-kit package source,
        # outside plugins/; assert the glob reaches it so a narrowed glob cannot
        # silently drop it from the drift check.
        vendored = (
            _REPO_ROOT / "packages" / "katsoft-llm-kit" / "src" / "llm_kit"
            / "bootstrap_guard.py"
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

    def test_reexec_noop_when_skip_variable_set(self, tmp_path, monkeypatch):
        mod = _load_canon()
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(tmp_path))
        self._fake_venv_python(tmp_path, "p4-kit")
        monkeypatch.setenv(mod._REEXEC_SKIP_ENV, "1")
        called = []
        monkeypatch.setattr(mod.os, "execv", lambda *a: called.append(a))
        mod.reexec_under_plugin_venv("p4-kit")
        assert called == []

    def test_reexec_noop_when_venv_missing(self, tmp_path, monkeypatch):
        mod = _load_canon()
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(tmp_path))
        _clear_loop_flag(monkeypatch, mod)
        called = []
        monkeypatch.setattr(mod.os, "execv", lambda *a: called.append(a))
        mod.reexec_under_plugin_venv("p4-kit")
        assert called == []

    @pytest.mark.parametrize(
        "inherited_guard",
        [None, (_LOOP_ENV, "1"), (_SKIP_ENV, "1")],
        ids=["none", "legacy-loop-flag-1", "skip-variable"],
    )
    @pytest.mark.parametrize("venv_exists", [False, True])
    def test_redirected_missing_interpreter_refuses_before_shared_import(
        self, tmp_path: Path, inherited_guard: "tuple[str, str] | None", venv_exists: bool
    ) -> None:
        redirected = tmp_path / "data-dev"
        venv = redirected / "plugins-kit" / "git-kit" / ".venv"
        if venv_exists:
            venv.mkdir(parents=True)
        payload = tmp_path / "production-library"
        package = payload / "bootstrap_lib"
        package.mkdir(parents=True)
        marker = tmp_path / "production-imported"
        (package / "__init__.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
            encoding="utf-8",
        )
        script = tmp_path / "consumer.py"
        script.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(_CANON.parent)!r})\n"
            "from bootstrap_guard import reexec_under_plugin_venv\n"
            "reexec_under_plugin_venv('git-kit')\n"
            "import bootstrap_lib\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["CLAUDE_BOOTSTRAP_DATA_ROOT"] = str(redirected)
        env["PYTHONPATH"] = str(payload)
        env.pop(_LOOP_ENV, None)
        env.pop(_SKIP_ENV, None)
        if inherited_guard:
            env[inherited_guard[0]] = inherited_guard[1]
        result = subprocess.run(
            [sys.executable, "-S", str(script)], env=env,
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode != 0
        assert "git-kit" in result.stderr
        assert "CLAUDE_BOOTSTRAP_DATA_ROOT" in result.stderr
        assert str(venv) in result.stderr
        assert not marker.exists(), "production shared library executed"

    def test_redirected_existing_interpreter_reexecs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _load_canon()
        redirected = tmp_path / "data-dev"
        interpreter = redirected / "plugins-kit" / "git-kit" / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("", encoding="utf-8")
        monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(redirected))
        _clear_loop_flag(monkeypatch, mod)
        monkeypatch.setattr(mod.sys, "prefix", str(tmp_path / "outside"))
        monkeypatch.setattr(mod, "_is_windows", lambda: False)
        called: list[str] = []

        def fake_execv(path: str, args: list[str]) -> None:
            called.append(path)
            raise SystemExit(0)

        monkeypatch.setattr(mod.os, "execv", fake_execv)
        with pytest.raises(SystemExit) as exc:
            mod.reexec_under_plugin_venv("git-kit")
        assert exc.value.code == 0
        assert called == [str(interpreter)]

    def test_reexec_noop_when_already_under_venv(self, tmp_path, monkeypatch):
        """Being inside the venv is decided by sys.prefix -- the signal Python
        derives from the path the interpreter was INVOKED by."""
        mod = _load_canon()
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(tmp_path))
        py = self._fake_venv_python(tmp_path, "p4-kit")
        _clear_loop_flag(monkeypatch, mod)
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
        _clear_loop_flag(monkeypatch, mod)

        # The precondition that made the old check wrong.
        assert base_py.resolve() == venv_py.resolve()

        captured = {}

        def fake_execv(path, args):
            captured["path"] = path
            raise SystemExit(0)

        monkeypatch.setattr(mod.os, "execv", fake_execv)
        # This test pins the POSIX exec branch; Windows spawns and waits.
        monkeypatch.setattr(mod, "_is_windows", lambda: False)
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
        _clear_loop_flag(monkeypatch, mod)
        captured = {}

        def fake_execv(path, args):
            captured["path"] = path
            captured["args"] = args
            # os.execv replaces the process; simulate that the call terminates here.
            raise SystemExit(0)

        monkeypatch.setattr(mod.os, "execv", fake_execv)
        # This test pins the POSIX exec branch; Windows spawns and waits.
        monkeypatch.setattr(mod, "_is_windows", lambda: False)
        with pytest.raises(SystemExit):
            mod.reexec_under_plugin_venv("p4-kit")
        assert captured["path"] == str(py)
        assert captured["args"] == [str(py), "script.py", "152779"]
        # The loop-guard env flag is set before handing off to the new interpreter.
        assert mod.os.environ.get(mod._REEXEC_GUARD_ENV) == str(py)

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
        _clear_loop_flag(monkeypatch, mod)
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
        assert mod.os.environ.get(mod._REEXEC_GUARD_ENV) == str(py)


class TestLoopFlagNamesItsTarget:
    """The loop flag carries the interpreter it re-exec'd into, so an inherited
    flag from ANOTHER plugin's re-exec no longer suppresses this plugin's.

    Paired with tests/bootstrap/test_claudx_escapes.py (escape 3), which drives
    the same property through a real process; these pin the guard's contract
    directly, including the loop guarantee the flag exists to provide.
    """

    def _venv_python(self, root, plugin):
        py = root / plugin / ".venv" / "bin" / "python"
        py.parent.mkdir(parents=True)
        py.write_text("", encoding="utf-8")
        return py

    def _arm(self, mod, monkeypatch, tmp_path, *plugins):
        redirected = tmp_path / "data-dev"
        pythons = {p: self._venv_python(redirected / "plugins-kit", p) for p in plugins}
        monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(redirected))
        _clear_loop_flag(monkeypatch, mod)
        monkeypatch.setattr(mod.sys, "prefix", str(tmp_path / "outside"))
        monkeypatch.setattr(mod.sys, "argv", ["consumer.py"])
        monkeypatch.setattr(mod, "_is_windows", lambda: False)
        execs: list[str] = []
        # os.execv does not return in real life; here the "new process" is the
        # next call in the test, which inherits os.environ and the outside prefix.
        monkeypatch.setattr(mod.os, "execv", lambda path, args: execs.append(path))
        return pythons, execs

    def test_a_flag_naming_another_plugins_venv_does_not_suppress_the_reexec(
        self, tmp_path, monkeypatch
    ):
        mod = _load_canon()
        pythons, execs = self._arm(mod, monkeypatch, tmp_path, "git-kit", "p4-kit")
        monkeypatch.setenv(mod._REEXEC_GUARD_ENV, str(pythons["p4-kit"]))
        mod.reexec_under_plugin_venv("git-kit")
        assert execs == [str(pythons["git-kit"])]
        assert os.environ[mod._REEXEC_GUARD_ENV] == str(pythons["git-kit"])

    def test_a_flag_naming_this_plugins_venv_returns_even_off_the_venv(
        self, tmp_path, monkeypatch
    ):
        mod = _load_canon()
        pythons, execs = self._arm(mod, monkeypatch, tmp_path, "git-kit")
        monkeypatch.setenv(mod._REEXEC_GUARD_ENV, str(pythons["git-kit"]))
        mod.reexec_under_plugin_venv("git-kit")
        assert execs == []

    def test_the_skip_variable_is_the_opt_out(self, tmp_path, monkeypatch):
        """What every tests/<plugin>/conftest.py sets (see plugins/CLAUDE.md,
        "Test gotcha"): the re-exec must stay a no-op under pytest even inside
        a redirected session where the plugin venv exists. It is a separate
        variable from the loop flag, which only ever carries a target path."""
        mod = _load_canon()
        _, execs = self._arm(mod, monkeypatch, tmp_path, "git-kit")
        monkeypatch.setenv(mod._REEXEC_SKIP_ENV, "1")
        mod.reexec_under_plugin_venv("git-kit")
        assert execs == []
        assert os.environ.get(mod._REEXEC_GUARD_ENV) in (None, ""), (
            "the opt-out must not write the loop flag"
        )

    @pytest.mark.parametrize("value", ["0", "true", "yes", ""])
    def test_only_the_value_1_opts_out(self, tmp_path, monkeypatch, value):
        mod = _load_canon()
        pythons, execs = self._arm(mod, monkeypatch, tmp_path, "git-kit")
        monkeypatch.setenv(mod._REEXEC_SKIP_ENV, value)
        mod.reexec_under_plugin_venv("git-kit")
        assert execs == [str(pythons["git-kit"])]

    def test_a_legacy_1_flag_no_longer_bypasses_the_prefix_check(
        self, tmp_path, monkeypatch, capsys
    ):
        """An old guard (or an unmigrated conftest) leaves the loop flag at "1".
        That value used to return before the sys.prefix check, keeping a process
        off the plugin venv. Now it is untrusted: the process re-execs once and
        the one-line stderr note names the flag as legacy."""
        mod = _load_canon()
        pythons, execs = self._arm(mod, monkeypatch, tmp_path, "git-kit")
        monkeypatch.setenv(mod._REEXEC_GUARD_ENV, "1")
        mod.reexec_under_plugin_venv("git-kit")
        assert execs == [str(pythons["git-kit"])]
        assert os.environ[mod._REEXEC_GUARD_ENV] == str(pythons["git-kit"])
        err = capsys.readouterr().err
        assert "legacy" in err and mod._REEXEC_GUARD_ENV in err, err
        assert err.count("\n") == 1, "exactly one stderr line"

    def test_an_unrelated_untrusted_flag_value_is_silent(
        self, tmp_path, monkeypatch, capsys
    ):
        """Only the legacy literal earns the note; a foreign path just re-execs."""
        mod = _load_canon()
        pythons, execs = self._arm(mod, monkeypatch, tmp_path, "git-kit", "p4-kit")
        monkeypatch.setenv(mod._REEXEC_GUARD_ENV, str(pythons["p4-kit"]))
        mod.reexec_under_plugin_venv("git-kit")
        assert execs == [str(pythons["git-kit"])]
        assert capsys.readouterr().err == ""

    def test_the_skip_variable_never_bypasses_a_missing_redirected_interpreter(
        self, tmp_path, monkeypatch
    ):
        """Step 1 of the decision order runs before the opt-out is read."""
        mod = _load_canon()
        monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(tmp_path / "data-dev"))
        monkeypatch.setenv(mod._REEXEC_SKIP_ENV, "1")
        with pytest.raises(SystemExit) as exc:
            mod.reexec_under_plugin_venv("git-kit")
        assert exc.value.code == 2

    def test_a_legacy_1_parent_chain_is_bounded(self, tmp_path, monkeypatch):
        """An old-guard parent leaves "1"; the new child re-execs once, writes its
        token, and every later call in the tree returns: no combination loops."""
        mod = _load_canon()
        pythons, execs = self._arm(mod, monkeypatch, tmp_path, "git-kit")
        monkeypatch.setenv(mod._REEXEC_GUARD_ENV, "1")
        for _ in range(4):
            mod.reexec_under_plugin_venv("git-kit")
        assert execs == [str(pythons["git-kit"])]

    def test_a_reexec_chain_is_bounded(self, tmp_path, monkeypatch):
        """Each call stands for one process in the chain, all outside every venv
        (the worst case: sys.prefix never matches). A -> B -> A -> A must stop at
        the second A: the flag now names A, so it cannot loop."""
        mod = _load_canon()
        pythons, execs = self._arm(mod, monkeypatch, tmp_path, "git-kit", "p4-kit")
        for plugin in ("git-kit", "p4-kit", "git-kit", "git-kit", "git-kit"):
            mod.reexec_under_plugin_venv(plugin)
        assert execs == [
            str(pythons["git-kit"]), str(pythons["p4-kit"]), str(pythons["git-kit"]),
        ]
