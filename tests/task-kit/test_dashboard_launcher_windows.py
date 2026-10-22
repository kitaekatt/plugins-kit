"""Windows process creation, run on every OS.

``task_system.procspawn`` is the one place task-kit reads the platform for process
creation, and its callers pass ``windows=`` explicitly. So the Windows
composition and its wiring are tested here with the Windows constants
monkeypatched in (absent on POSIX), and the ``child_exec`` Windows branch is
run for real by forcing the parameter.

What this does NOT prove: the Windows APIs themselves (``CREATE_NO_WINDOW``
hiding consoles, the kill-on-close Job Object, ``msvcrt`` locks). Those need a
Windows host; see ``test_dashboard_process_tree.py`` for the one that exists.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from dashboard_support import ENTRYPOINT

from task_system import procspawn
from task_system.dashboard import child_exec, runfiles
from task_system.dashboard import launcher as L
from task_system.dashboard import runner
from task_system.dashboard.scheduler import Scheduler

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
SENTINEL = {"sentinel": True}


@pytest.fixture
def windows_constants(monkeypatch):
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", CREATE_NO_WINDOW, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", CREATE_NEW_PROCESS_GROUP, raising=False)


def test_w1_the_exact_flags_for_both_platforms(windows_constants):
    """Red: add ``| 0x00000008`` to the Windows branch of ``detached``."""
    assert procspawn.detached(windows=False) == {"start_new_session": True}
    assert procspawn.hidden(windows=False) == {"start_new_session": True}
    detached = procspawn.detached(windows=True)
    assert set(detached) == {"creationflags"}
    assert detached["creationflags"] == CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    assert not detached["creationflags"] & DETACHED_PROCESS  # it would make CREATE_NO_WINDOW a no-op
    assert procspawn.hidden(windows=True) == {"creationflags": CREATE_NO_WINDOW}


class RecordingPopen:
    """Records what it was started with; never starts anything."""

    calls: list = []

    def __init__(self, command, **kwargs):
        type(self).calls.append((command, kwargs))
        raise OSError("recording popen: nothing started")


@pytest.fixture
def recording(monkeypatch):
    RecordingPopen.calls = []
    monkeypatch.setattr(subprocess, "Popen", RecordingPopen)
    return RecordingPopen.calls


@pytest.mark.parametrize("windows", [False, True], ids=["posix", "windows"])
def test_w2_every_launch_passes_exactly_what_procspawn_returned(monkeypatch, tmp_path, recording, windows):
    """Red: give ``Scheduler._default_spawn`` its own ``sys.platform`` / ``getattr`` flags again."""
    asked = []

    def spy(name):
        def inner(*, windows):
            asked.append((name, windows))
            return dict(SENTINEL)
        return inner

    monkeypatch.setattr(procspawn, "WINDOWS", windows)
    monkeypatch.setattr(procspawn, "detached", spy("detached"))
    monkeypatch.setattr(procspawn, "hidden", spy("hidden"))

    # the server (launcher)
    with pytest.raises(OSError):
        L.spawn_server(ENTRYPOINT, tmp_path / "data", tmp_path / "data" / "dashboard" / "server.log")
    # the runner (scheduler)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(OSError):
        Scheduler._default_spawn([sys.executable, "-c", "pass"], run_dir)
    # the runner's command
    command_dir = tmp_path / "command-run"
    command_dir.mkdir()
    runfiles.write_json(command_dir / runfiles.SPEC_FILE, {
        "project_root": str(tmp_path), "runner": {"argv": [sys.executable, "-c", "pass"], "deadline_s": 5}})
    assert runner.run(command_dir) == runner.EXIT_REFUSED  # the recording Popen raised OSError: cannot start

    assert [(name, w) for name, w in asked] == [("detached", windows), ("detached", windows), ("hidden", windows)]
    assert len(recording) == 3
    for command, kwargs in recording:
        assert {k: kwargs[k] for k in SENTINEL} == SENTINEL, command
        assert "start_new_session" not in kwargs and "creationflags" not in kwargs  # no flags beside procspawn's


def test_w3_the_windows_branch_of_become_returns_the_exit_code_and_keeps_the_runner_parentage(tmp_path):
    """The command stays a DESCENDANT of the recorded pid (recovery depends on it).

    Red: change the Windows branch to ``subprocess.Popen(command); return 0``.
    """
    parent_file = tmp_path / "ppid"
    code = f"import os, sys; open({str(parent_file)!r}, 'w').write(str(os.getppid())); sys.exit(7)"
    assert child_exec.become([sys.executable, "-c", code], windows=True) == 7
    assert parent_file.read_text() == str(os.getpid())


def test_the_production_paths_hold_no_assert_for_the_process_identity():
    """``assert`` is stripped under ``-O``; an unreadable own identity must still fail loudly."""
    for module in (child_exec, runner):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "assert me is not None" not in source
