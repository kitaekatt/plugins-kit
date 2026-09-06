"""Tests for npm_check.py -- node_modules validation and remediation.

Every test here stubs the npm spawn. Nothing in this file may run a real
install: these run on Windows CI as well as Ubuntu, and a network install
would make the suite slow and flaky.
"""

import json
import os

import pytest

from bootstrap_lib.npm_check import (
    check_node_modules,
    detect_other_manager,
    ensure_node_modules,
    find_npm,
    find_npm_lockfile,
)


class _Proc:
    """Popen stand-in.

    npm_check spawns with Popen and captures into a temp FILE rather than a
    PIPE (a pipe deadlocks CPython's own timeout handling when a lifecycle
    grandchild survives the kill), so the stub writes its canned output into
    whatever file handle it was handed as ``stdout``.
    """

    def __init__(self, returncode=0, stdout="", timeout=False):
        self.returncode = returncode
        self._stdout = stdout
        self._timeout = timeout
        self.pid = 4321
        self.killed = False
        self.wait_timeout = None
        self._out = None
        self._written = False

    def _attach(self, out):
        self._out = out

    def wait(self, timeout=None):
        import subprocess as sp
        if self.wait_timeout is None:
            self.wait_timeout = timeout
        if self._out is not None and not self._written:
            self._out.write(self._stdout)
            self._out.flush()
            self._written = True
        if self._timeout:
            # Cleared so the reap inside _kill_process_tree can succeed.
            self._timeout = False
            raise sp.TimeoutExpired("npm", timeout)
        return self.returncode

    def kill(self):
        self.killed = True


def _write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _make_project(tmp_path, deps=True, lockfile="package-lock.json", extra=None):
    """A project dir with package.json (+ optional lockfile)."""
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    pkg = {"name": "proj", "version": "1.0.0"}
    if deps:
        pkg["dependencies"] = {"leftpad": "0.0.1"}
    if extra:
        pkg.update(extra)
    _write(str(project / "package.json"), json.dumps(pkg))
    if lockfile:
        _write(str(project / lockfile), "{}")
    return project


def _install_node_modules(project, mtime=None):
    """Fake a completed install: npm's hidden lockfile inside node_modules."""
    hidden = project / "node_modules" / ".package-lock.json"
    _write(str(hidden), "{}")
    if mtime is not None:
        os.utime(str(hidden), (mtime, mtime))
    return hidden


class TestCheckNodeModules:
    def test_fresh_when_hidden_lockfile_newer(self, tmp_path):
        project = _make_project(tmp_path)
        lock = project / "package-lock.json"
        os.utime(str(lock), (1000, 1000))
        _install_node_modules(project, mtime=2000)

        result = check_node_modules(str(project))

        assert result.passed
        assert "up to date" in result.message
        assert result.subject == str(project / "node_modules")

    def test_fresh_when_mtimes_equal(self, tmp_path):
        """Equal mtimes count as fresh (>= not >)."""
        project = _make_project(tmp_path)
        os.utime(str(project / "package-lock.json"), (1500, 1500))
        _install_node_modules(project, mtime=1500)

        assert check_node_modules(str(project)).passed

    def test_stale_when_lockfile_newer(self, tmp_path):
        project = _make_project(tmp_path)
        _install_node_modules(project, mtime=1000)
        os.utime(str(project / "package-lock.json"), (2000, 2000))

        result = check_node_modules(str(project))

        assert not result.passed
        assert "stale" in result.message
        assert result.remediation_cmd == "npm ci"

    def test_missing_node_modules_fails(self, tmp_path):
        project = _make_project(tmp_path)

        result = check_node_modules(str(project))

        assert not result.passed
        assert "not installed" in result.message
        assert result.remediation_cmd == "npm ci"

    def test_missing_node_modules_without_lockfile_suggests_install(self, tmp_path):
        project = _make_project(tmp_path, lockfile=None)

        result = check_node_modules(str(project))

        assert not result.passed
        assert result.remediation_cmd == "npm install"

    def test_no_declared_dependencies_passes_without_node_modules(self, tmp_path):
        """Verified empirically: `npm install` on a dependency-free project
        exits 0 and creates NO node_modules. Demanding one would re-run npm
        every session and then fail the re-check forever."""
        project = _make_project(tmp_path, deps=False)

        result = check_node_modules(str(project))

        assert result.passed
        assert "no dependencies" in result.message

    def test_malformed_package_json_fails_with_parse_error(self, tmp_path):
        project = tmp_path / "broken"
        project.mkdir()
        _write(str(project / "package.json"), "{not json")

        result = check_node_modules(str(project))

        assert not result.passed
        assert "package.json parse error" in result.message

    def test_shrinkwrap_is_honored_as_lockfile(self, tmp_path):
        project = _make_project(tmp_path, lockfile="npm-shrinkwrap.json")
        _install_node_modules(project, mtime=1000)
        os.utime(str(project / "npm-shrinkwrap.json"), (2000, 2000))

        result = check_node_modules(str(project))

        assert not result.passed
        assert "npm-shrinkwrap.json" in result.message

    def test_no_lockfile_with_node_modules_is_fresh(self, tmp_path):
        project = _make_project(tmp_path, lockfile=None)
        _install_node_modules(project)

        assert check_node_modules(str(project)).passed


class TestFindNpmLockfile:
    def test_shrinkwrap_wins_over_package_lock(self, tmp_path):
        project = _make_project(tmp_path)
        _write(str(project / "npm-shrinkwrap.json"), "{}")
        assert find_npm_lockfile(str(project)).endswith("npm-shrinkwrap.json")

    def test_none_when_absent(self, tmp_path):
        project = _make_project(tmp_path, lockfile=None)
        assert find_npm_lockfile(str(project)) is None


class TestDetectOtherManager:
    @pytest.mark.parametrize("lockfile,manager", [
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("bun.lock", "bun"),
        ("bun.lockb", "bun"),
    ])
    def test_competing_lockfiles(self, tmp_path, lockfile, manager):
        project = _make_project(tmp_path, lockfile=None)
        _write(str(project / lockfile), "")
        assert detect_other_manager(str(project)) == manager

    def test_package_manager_field(self, tmp_path):
        project = _make_project(tmp_path, lockfile=None,
                                extra={"packageManager": "pnpm@8.6.0"})
        assert detect_other_manager(str(project)) == "pnpm"

    def test_package_manager_npm_is_not_other(self, tmp_path):
        project = _make_project(tmp_path, extra={"packageManager": "npm@11.0.0"})
        assert detect_other_manager(str(project)) is None

    def test_plain_npm_project(self, tmp_path):
        project = _make_project(tmp_path)
        assert detect_other_manager(str(project)) is None

    def test_malformed_package_json_is_not_other(self, tmp_path):
        project = tmp_path / "broken"
        project.mkdir()
        _write(str(project / "package.json"), "{not json")
        assert detect_other_manager(str(project)) is None


class TestFindNpm:
    def test_returns_absolute_path_when_which_finds_it(self, monkeypatch):
        """The absolute path is the whole point: CreateProcess does no PATHEXT
        resolution, so argv[0] must be the resolved .CMD/binary."""
        monkeypatch.setattr("bootstrap_lib.npm_check.shutil.which",
                            lambda name: os.path.join(os.sep, "tools", "npm.cmd"))
        found = find_npm()
        assert found is not None
        assert os.path.isabs(found)

    def test_falls_back_to_local_bin(self, tmp_path, monkeypatch):
        local_bin = tmp_path / ".local" / "bin"
        local_bin.mkdir(parents=True)
        fake = local_bin / "npm"
        fake.write_text("#!/bin/sh\n")
        monkeypatch.setattr("bootstrap_lib.npm_check.shutil.which", lambda name: None)
        monkeypatch.setattr(
            "bootstrap_lib.npm_check.os.path.expanduser",
            lambda p: str(tmp_path / p[2:]) if p.startswith("~/") else p,
        )
        assert find_npm() == os.path.abspath(str(fake))

    def test_none_when_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("bootstrap_lib.npm_check.shutil.which", lambda name: None)
        monkeypatch.setattr(
            "bootstrap_lib.npm_check.os.path.expanduser",
            lambda p: str(tmp_path / p[2:]) if p.startswith("~/") else p,
        )
        assert find_npm() is None


class TestEnsureNodeModules:
    @staticmethod
    def _stub_npm(monkeypatch, proc, calls=None, kills=None):
        monkeypatch.setattr("bootstrap_lib.npm_check.find_npm",
                            lambda: os.path.join(os.sep, "fake", "npm"))

        # Never let a test reach the real tree-killer: on Windows it shells out
        # to taskkill, and the stub pid is not ours to signal.
        def _kill(p):
            if kills is not None:
                kills.append(p)
        monkeypatch.setattr("bootstrap_lib.npm_check._kill_process_tree", _kill)

        def _popen(argv, **kwargs):
            if calls is not None:
                calls.append((argv, kwargs))
            proc._attach(kwargs.get("stdout"))
            return proc
        monkeypatch.setattr("bootstrap_lib.npm_check.subprocess.Popen", _popen)

    @staticmethod
    def _forbid_npm(monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("npm must not be spawned")
        monkeypatch.setattr("bootstrap_lib.npm_check.subprocess.Popen", _boom)

    def test_fresh_is_silent_and_spawns_nothing(self, tmp_path, monkeypatch):
        project = _make_project(tmp_path)
        os.utime(str(project / "package-lock.json"), (1000, 1000))
        _install_node_modules(project, mtime=2000)
        self._forbid_npm(monkeypatch)

        result, entries = ensure_node_modules(str(project))

        assert result.passed
        assert entries == []

    def test_skip_without_package_json(self, tmp_path, monkeypatch):
        project = tmp_path / "empty"
        project.mkdir()
        self._forbid_npm(monkeypatch)

        result, entries = ensure_node_modules(str(project))

        assert result.passed  # a skip is not a failure
        assert "no package.json" in result.message
        assert entries == []

    def test_skip_when_yarn_owns_the_tree(self, tmp_path, monkeypatch):
        """npm can READ yarn.lock and would write a competing
        package-lock.json, so this guard is mandatory, not cosmetic."""
        project = _make_project(tmp_path, lockfile=None)
        _write(str(project / "yarn.lock"), "")
        self._forbid_npm(monkeypatch)

        result, entries = ensure_node_modules(str(project))

        assert result.passed
        assert "yarn" in result.message
        assert not (project / "package-lock.json").exists()

    def test_skip_when_npm_not_on_path(self, tmp_path, monkeypatch):
        project = _make_project(tmp_path)
        monkeypatch.setattr("bootstrap_lib.npm_check.find_npm", lambda: None)
        self._forbid_npm(monkeypatch)

        result, entries = ensure_node_modules(str(project))

        assert result.passed
        assert "npm not on PATH" in result.message

    def test_uses_ci_when_lockfile_present(self, tmp_path, monkeypatch):
        project = _make_project(tmp_path)
        calls = []
        self._stub_npm(monkeypatch, _Proc(0), calls)
        monkeypatch.setattr(
            "bootstrap_lib.npm_check.check_node_modules",
            _sequence([_fail(project), _ok(project)]),
        )

        result, entries = ensure_node_modules(str(project))

        argv, kwargs = calls[0]
        assert argv[1] == "ci"
        assert "--no-audit" in argv and "--no-fund" in argv
        assert "--ignore-scripts" not in argv
        assert result.passed
        assert "created" in entries

    def test_uses_install_when_no_lockfile(self, tmp_path, monkeypatch):
        project = _make_project(tmp_path, lockfile=None)
        calls = []
        self._stub_npm(monkeypatch, _Proc(0), calls)
        monkeypatch.setattr(
            "bootstrap_lib.npm_check.check_node_modules",
            _sequence([_fail(project), _ok(project)]),
        )

        ensure_node_modules(str(project))

        assert calls[0][0][1] == "install"

    def test_spawn_is_shell_free_with_explicit_cwd(self, tmp_path, monkeypatch):
        """The cwd is the reason this phase exists: npm's local-prefix walk
        would otherwise retarget an ancestor project."""
        project = _make_project(tmp_path)
        calls = []
        proc = _Proc(0)
        self._stub_npm(monkeypatch, proc, calls)
        monkeypatch.setattr(
            "bootstrap_lib.npm_check.check_node_modules",
            _sequence([_fail(project), _ok(project)]),
        )

        ensure_node_modules(str(project))

        argv, kwargs = calls[0]
        assert os.path.isabs(argv[0])
        assert kwargs["cwd"] == str(project)
        assert kwargs["shell"] is False
        import subprocess as sp
        assert kwargs["stdin"] is sp.DEVNULL
        assert kwargs["env"]["CI"] == "1"
        # Captured to a FILE, never a PIPE -- a pipe is what deadlocks
        # CPython's timeout handling when a grandchild survives the kill.
        assert kwargs["stdout"] is not sp.PIPE
        assert hasattr(kwargs["stdout"], "write")
        assert kwargs["stderr"] is sp.STDOUT
        # The bound is on the wait, since Popen itself takes no timeout.
        assert proc.wait_timeout == 600

    def test_ignore_scripts_opt_in(self, tmp_path, monkeypatch):
        project = _make_project(tmp_path)
        calls = []
        self._stub_npm(monkeypatch, _Proc(0), calls)
        monkeypatch.setattr(
            "bootstrap_lib.npm_check.check_node_modules",
            _sequence([_fail(project), _ok(project)]),
        )

        ensure_node_modules(str(project), ignore_scripts=True)

        assert "--ignore-scripts" in calls[0][0]

    def test_ci_out_of_sync_does_not_fall_back_to_install(self, tmp_path, monkeypatch):
        """A session-start hook must never rewrite the user's tracked
        lockfile, so the EUSAGE refusal is reported, not worked around."""
        project = _make_project(tmp_path)
        out = ("npm error code EUSAGE\nnpm error `npm ci` can only install "
               "packages when your package.json and package-lock.json are in sync")
        calls = []
        self._stub_npm(monkeypatch, _Proc(1, out), calls)
        monkeypatch.setattr("bootstrap_lib.npm_check.check_node_modules",
                            _sequence([_fail(project)]))

        result, entries = ensure_node_modules(str(project))

        assert len(calls) == 1  # exactly one spawn: no second `npm install`
        assert not result.passed
        assert "out of sync" in result.message
        assert result.remediation_cmd == "npm install"
        assert any("EUSAGE" in e for e in entries)

    def test_non_one_exit_code_is_a_failure(self, tmp_path, monkeypatch):
        """npm has been observed returning raw libuv errnos (-4058), so the
        branch is zero vs non-zero, never == 1."""
        project = _make_project(tmp_path)
        out = "npm error code ENOENT\nnpm error syscall spawn"
        self._stub_npm(monkeypatch, _Proc(-4058, out))
        monkeypatch.setattr("bootstrap_lib.npm_check.check_node_modules",
                            _sequence([_fail(project)]))

        result, entries = ensure_node_modules(str(project))

        assert not result.passed
        assert "exit -4058" in result.message
        assert "[ENOENT]" in result.message
        # Full captured output survives into the entry (truncation is the
        # display layer's job).
        assert any("npm error syscall spawn" in e for e in entries)

    def test_warnings_surface_on_success(self, tmp_path, monkeypatch):
        project = _make_project(tmp_path)
        out = ("npm warn EBADENGINE Unsupported engine {package: 'x'}\n"
               "npm warn allow-scripts esbuild wants to run a script\n"
               "added 12 packages")
        self._stub_npm(monkeypatch, _Proc(0, out))
        monkeypatch.setattr(
            "bootstrap_lib.npm_check.check_node_modules",
            _sequence([_fail(project), _ok(project)]),
        )

        result, entries = ensure_node_modules(str(project))

        assert result.passed
        assert any("EBADENGINE" in e for e in entries)
        assert any("allow-scripts" in e for e in entries)

    def test_spawn_error_is_surfaced(self, tmp_path, monkeypatch):
        project = _make_project(tmp_path)
        monkeypatch.setattr("bootstrap_lib.npm_check.find_npm",
                            lambda: os.path.join(os.sep, "fake", "npm"))

        def _boom(argv, **kwargs):
            raise OSError("exec failed")
        monkeypatch.setattr("bootstrap_lib.npm_check.subprocess.Popen", _boom)
        monkeypatch.setattr("bootstrap_lib.npm_check.check_node_modules",
                            _sequence([_fail(project)]))

        result, entries = ensure_node_modules(str(project))

        assert not result.passed
        assert any("exec failed" in e for e in entries)

    def test_timeout_is_surfaced_and_kills_the_tree(self, tmp_path, monkeypatch):
        """A timeout must terminate npm's DESCENDANTS, not just the shim.

        Popen.kill ends only the direct child; a surviving lifecycle-script
        grandchild holding the output handle is what turns a timeout into a
        hung SessionStart hook.
        """
        project = _make_project(tmp_path)
        kills = []
        proc = _Proc(0, "npm warn partial output", timeout=True)
        self._stub_npm(monkeypatch, proc, kills=kills)
        monkeypatch.setattr("bootstrap_lib.npm_check.check_node_modules",
                            _sequence([_fail(project)]))

        result, entries = ensure_node_modules(str(project))

        assert not result.passed
        assert "timed out" in result.message
        assert kills == [proc], "the process tree must be killed on timeout"
        # Whatever npm managed to say before the deadline still survives.
        assert any("partial output" in e for e in entries)

    def test_workspace_subdir_sees_root_lockfile(self, tmp_path, monkeypatch):
        """A pnpm/yarn workspace package must be recognised from its ROOT.

        In a workspace the lockfile exists only at the repo root and the
        package's own package.json carries no `packageManager`, so a guard that
        looked only at the subdir would miss both signals and let npm write a
        competing package-lock.json into a tree pnpm owns.
        """
        root = tmp_path / "monorepo"
        root.mkdir()
        _write(str(root / "pnpm-lock.yaml"), "lockfileVersion: '9.0'\n")
        pkg_dir = root / "packages" / "web"
        pkg_dir.mkdir(parents=True)
        _write(str(pkg_dir / "package.json"),
               json.dumps({"name": "web", "dependencies": {"leftpad": "0.0.1"}}))
        self._forbid_npm(monkeypatch)

        result, entries = ensure_node_modules(str(pkg_dir), root=str(root))

        assert result.passed  # a skip is not a failure
        assert "pnpm" in result.message
        assert entries == []

    def test_workspace_walk_stops_at_the_declared_root(self, tmp_path, monkeypatch):
        """The walk must not escape the project: a lockfile ABOVE the declared
        root belongs to someone else and must not suppress this project."""
        outer = tmp_path / "outer"
        outer.mkdir()
        _write(str(outer / "yarn.lock"), "")
        project = _make_project(outer, deps=True)
        calls = []
        self._stub_npm(monkeypatch, _Proc(0), calls)
        monkeypatch.setattr(
            "bootstrap_lib.npm_check.check_node_modules",
            _sequence([_fail(project), _ok(project)]),
        )

        result, entries = ensure_node_modules(str(project), root=str(project))

        assert result.passed
        assert len(calls) == 1, "an unrelated ancestor lockfile must not skip us"

    def test_exit_zero_but_still_stale_is_a_failure(self, tmp_path, monkeypatch):
        project = _make_project(tmp_path)
        self._stub_npm(monkeypatch, _Proc(0, "added 0 packages"))
        monkeypatch.setattr("bootstrap_lib.npm_check.check_node_modules",
                            _sequence([_fail(project), _fail(project)]))

        result, entries = ensure_node_modules(str(project))

        assert not result.passed
        assert "exited 0" in result.message


def _ok(project):
    from bootstrap_lib.result import Result
    return Result(passed=True, subject=str(project / "node_modules"),
                  message="node_modules up to date")


def _fail(project):
    from bootstrap_lib.result import Result
    return Result(passed=False, subject=str(project / "node_modules"),
                  message="node_modules not installed",
                  remediation_cmd="npm ci")


def _sequence(results):
    """A check_node_modules stub returning each result in turn (last sticks)."""
    box = list(results)

    def _stub(project_dir):
        return box.pop(0) if len(box) > 1 else box[0]
    return _stub
