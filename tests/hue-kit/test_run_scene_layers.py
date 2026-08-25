"""_run_scene_layers must not use exec on Windows.

CPython routes os.execv/os.execve through the CRT _execv on Windows, which
SPAWNS the replacement and terminates the caller immediately: the parent
returns exit 0 before the child has done any work, and the child is orphaned
rather than waited on. A caller reading the parent's stdout gets an empty
stream and a false success. The Windows branch therefore delegates to
_call_scene_layers, which spawns and waits.

The platform is patched through the module's _is_windows() seam, never through
os.name -- pathlib reads os.name at call time and would switch every Path() in
the module under test to WindowsPath, raising NotImplementedError on a POSIX
runner.
"""

from pathlib import Path

import pytest


class TestRunSceneLayersOnWindows:
    def test_delegates_to_subprocess_runner_and_propagates_its_code(
            self, hue_cli, tmp_path, monkeypatch):
        seen = {}

        def fake_call(flags, workdir, *, capture=False):
            seen["flags"] = list(flags)
            seen["workdir"] = Path(workdir)
            seen["capture"] = capture
            return 7, None

        def exploded(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("os.execve called on the Windows branch")

        monkeypatch.setattr(hue_cli, "_is_windows", lambda: True)
        monkeypatch.setattr(hue_cli, "_call_scene_layers", fake_call)
        monkeypatch.setattr(hue_cli.os, "execve", exploded)

        with pytest.raises(SystemExit) as excinfo:
            hue_cli._run_scene_layers(["--validate-design"], tmp_path)

        assert excinfo.value.code == 7, (
            "the child's exit code must propagate, not a bare 0"
        )
        assert seen["flags"] == ["--validate-design"]
        assert seen["workdir"] == tmp_path
        # capture must stay False so the child inherits this process's stdio and
        # the tty stays attached -- the one-verb commands depend on it.
        assert seen["capture"] is False

    def test_posix_branch_still_execs(self, hue_cli, tmp_path, monkeypatch):
        called = {}

        def fake_execve(exe, argv, env):
            called["argv"] = list(argv)
            raise SystemExit(0)  # stand in for "never returns"

        monkeypatch.setattr(hue_cli, "_is_windows", lambda: False)
        monkeypatch.setattr(hue_cli.os, "execve", fake_execve)
        monkeypatch.setattr(hue_cli.os, "chdir", lambda *a, **k: None)
        monkeypatch.setattr(hue_cli, "_scene_layers_env", lambda wd: {})

        try:
            hue_cli._run_scene_layers(["--html", "x"], tmp_path)
        except SystemExit:
            pass

        assert "--html" in called["argv"], "POSIX must still hand over via execve"
