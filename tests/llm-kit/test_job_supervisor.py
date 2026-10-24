"""Real local process proofs for the opt-in supervised jobs API."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable

import psutil
import pytest

from llm_kit.jobs import _supervisor_process as processes
from llm_kit.jobs import supervisor
from llm_kit.jobs.supervisor import JobProcessSpec, ProcessHandle, SupervisorError


def eventually(predicate: Callable[[], object], timeout: float = 15.0) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(0.02)
    raise AssertionError("Expected process observation did not arrive before deadline")


@pytest.fixture
def handles() -> list[ProcessHandle]:
    owned: list[ProcessHandle] = []
    yield owned
    for handle in owned:
        supervisor.cancel(handle, "test cleanup", wait_s=10)


def spec(tmp_path: Path, body: str, **options: object) -> JobProcessSpec:
    return JobProcessSpec(run_dir=tmp_path / "run", argv=(sys.executable, "-c", body), cwd=tmp_path,
                          timeout_s=30, grace_s=0.3, **options)


def finished(handle: ProcessHandle) -> supervisor.ProcessStatus | None:
    status = supervisor.inspect(handle)
    return status if not status.alive and status.result is not None else None


def test_worker_cold_start_does_not_shadow_stdlib_select(tmp_path: Path) -> None:
    worker = Path(supervisor.__file__).with_name("_supervisor_worker.py")
    result = subprocess.run([sys.executable, str(worker), "--help"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_spec_and_handle_json_roundtrip_and_immutable_environment(tmp_path: Path) -> None:
    environment = {"TEST_VALUE": "initial"}
    value = spec(tmp_path, "pass", env=environment)
    environment["TEST_VALUE"] = "changed"
    assert value.env["TEST_VALUE"] == "initial"
    with pytest.raises(TypeError):
        value.env["TEST_VALUE"] = "changed"
    assert JobProcessSpec.from_dict(json.loads(json.dumps(value.to_dict()))) == value
    handle = ProcessHandle(tmp_path / "run", "a" * 32)
    assert ProcessHandle.from_dict(json.loads(json.dumps(handle.to_dict()))) == handle
    with pytest.raises(ValueError, match="absolute"):
        replace(value, cwd=Path("relative"))
    with pytest.raises(ValueError, match="finite"):
        replace(value, timeout_s=float("nan"))
    with pytest.raises(ValueError, match="argv sequence"):
        replace(value, argv="echo executable shell string")


def test_real_launch_logs_and_terminal_json(tmp_path: Path, handles: list[ProcessHandle]) -> None:
    handle = supervisor.start(spec(tmp_path, "import os,sys; print(os.environ['SUPERVISOR_TEST']); print('diagnostic',file=sys.stderr)",
                                   env={"SUPERVISOR_TEST": "ok"}))
    handles.append(handle)
    status = eventually(lambda: finished(handle))
    assert status.state == "finished"
    assert status.returncode == 0
    assert status.stdout_path.read_text().strip() == "ok"
    assert status.stderr_path.read_text().strip() == "diagnostic"
    assert json.loads(json.dumps(status.to_dict()))["handle"] == handle.to_dict()
    assert json.loads((handle.run_dir / "lease.json").read_text())["released"] is True
    assert (handle.run_dir / "go.json").is_file()


def test_ledger_resource_cannot_have_two_supervised_writers(tmp_path: Path, handles: list[ProcessHandle]) -> None:
    first = spec(tmp_path, "import time; time.sleep(60)", lease_path=tmp_path / "shared-ledger.lease")
    handle = supervisor.start(first)
    handles.append(handle)
    with pytest.raises(SupervisorError, match="still owned") as caught:
        supervisor.start(replace(first, run_dir=tmp_path / "second"))
    assert caught.value.handle == handle
    assert not (tmp_path / "second").exists()
    assert supervisor.adopt(ProcessHandle.from_dict(handle.to_dict())).alive


def test_nonce_mismatch_never_requests_cancellation(tmp_path: Path, handles: list[ProcessHandle]) -> None:
    handle = supervisor.start(spec(tmp_path, "import time; time.sleep(60)"))
    handles.append(handle)
    with pytest.raises(SupervisorError, match="nonce"):
        supervisor.cancel(ProcessHandle(handle.run_dir, "b" * 32), "wrong run")
    assert not (handle.run_dir / "cancel.json").exists()


def test_cancel_stops_grandchild_before_ledger_finalizer(tmp_path: Path, handles: list[ProcessHandle]) -> None:
    marker = tmp_path / "grandchild.pid"
    closed = tmp_path / "ledger.closed"
    grandchild = "import os,time; from pathlib import Path; Path(%r).write_text(str(os.getpid())); time.sleep(60)" % str(marker)
    body = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',%r]); time.sleep(60)" % grandchild
    finalizer = ("import psutil; from pathlib import Path; p=int(Path(%r).read_text()); "
                 "assert not psutil.pid_exists(p) or psutil.Process(p).status()=='zombie'; Path(%r).write_text('closed')") % (str(marker), str(closed))
    handle = supervisor.start(spec(tmp_path, body, cancel_argv=(sys.executable, "-c", finalizer)))
    handles.append(handle)
    eventually(marker.exists)
    status = supervisor.cancel(handle, "operator cancelled")
    assert status.state == "cancelled", status.to_dict()
    assert not status.alive
    assert status.result["cancel"]["returncode"] == 0
    assert closed.read_text() == "closed"
    assert not status.survivors


def test_deadline_uses_full_tree_shutdown(tmp_path: Path, handles: list[ProcessHandle]) -> None:
    value = replace(spec(tmp_path, "import time; time.sleep(60)"), timeout_s=0.5)
    handle = supervisor.start(value)
    handles.append(handle)
    status = eventually(lambda: finished(handle))
    assert status.state == "timeout"
    assert status.survivors == ()


def test_normal_parent_exit_does_not_leave_orphan(tmp_path: Path, handles: list[ProcessHandle]) -> None:
    marker = tmp_path / "orphan.pid"
    body = ("import subprocess,sys; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c',"
            "'import time; time.sleep(60)']); Path(%r).write_text(str(p.pid))") % str(marker)
    handle = supervisor.start(spec(tmp_path, body))
    handles.append(handle)
    status = eventually(lambda: finished(handle))
    pid = int(marker.read_text())
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    assert status.state == "finished"


def test_adopt_killed_owner_closes_writer_then_ledger(tmp_path: Path, handles: list[ProcessHandle]) -> None:
    marker = tmp_path / "writer.started"
    closed = tmp_path / "closed"
    body = "from pathlib import Path; import time; Path(%r).write_text('running'); time.sleep(60)" % str(marker)
    finalizer = "from pathlib import Path; Path(%r).write_text('closed')" % str(closed)
    handle = supervisor.start(spec(tmp_path, body, cancel_argv=(sys.executable, "-c", finalizer)))
    handles.append(handle)
    eventually(marker.exists)
    runner = json.loads((handle.run_dir / "runner.json").read_text())["process"]
    owner = psutil.Process(runner["pid"])
    owner.kill()
    owner.wait(timeout=5)
    assert supervisor.inspect(handle).state == "indeterminate"
    status = supervisor.adopt(handle)
    assert status.state == "cancelled", status.to_dict()
    assert not status.alive
    assert closed.read_text() == "closed"


def test_failed_ledger_finalizer_retains_resource_lease(tmp_path: Path, handles: list[ProcessHandle]) -> None:
    value = spec(tmp_path, "import time; time.sleep(60)", cancel_argv=(sys.executable, "-c", "raise SystemExit(3)"))
    handle = supervisor.start(value)
    handles.append(handle)
    status = supervisor.cancel(handle, "cancel")
    assert status.state == "indeterminate"
    assert status.result["cancel"]["returncode"] == 3
    assert json.loads(value.lease_path.read_text())["released"] is False


def test_launch_refusal_does_not_leave_claimed_resource(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("expected launch refusal")
    monkeypatch.setattr(supervisor.subprocess, "Popen", refuse)
    with pytest.raises(SupervisorError) as caught:
        supervisor.start(spec(tmp_path, "pass"))
    handle = caught.value.handle
    assert handle is not None
    assert supervisor.inspect(handle).state == "refused"
    assert json.loads((handle.run_dir / "lease.json").read_text())["released"] is True


def test_startup_timeout_cancels_late_owner_before_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = spec(tmp_path, "from pathlib import Path; Path('must-not-exist').touch()")
    with monkeypatch.context() as patch:
        patch.setattr(supervisor.subprocess, "Popen", lambda *args, **kwargs: None)
        with pytest.raises(SupervisorError, match="handshake") as caught:
            supervisor.start(value, startup_timeout_s=0.01)
    handle = caught.value.handle
    assert handle is not None
    worker = Path(supervisor.__file__).with_name("_supervisor_worker.py")
    done = subprocess.run([sys.executable, str(worker), "run", str(handle.run_dir), handle.nonce], timeout=10)
    assert done.returncode == 0
    assert supervisor.inspect(handle).state == "cancelled"
    assert not (tmp_path / "must-not-exist").exists()


def test_reused_pid_is_never_signalled(monkeypatch: pytest.MonkeyPatch) -> None:
    real = processes.identity(os.getpid())
    assert real is not None
    reused = processes.ProcessIdentity(real.pid, real.create_time - 1)
    assert processes.live(reused) is None
    assert processes.stop(None, [reused], grace_s=0.01) == []


def test_survivors_keep_lease_and_skip_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = spec(tmp_path, "pass")
    handle = ProcessHandle(value.run_dir, "a" * 32)
    value.run_dir.mkdir()
    supervisor._write(value.run_dir / "intent.json", {"nonce": handle.nonce, "spec": value.to_dict()})
    supervisor._write(value.lease_path, {"nonce": handle.nonce, "handle": handle.to_dict(), "released": False})
    survivor = processes.ProcessIdentity(999999, 1.0)
    monkeypatch.setattr(processes, "stop", lambda *args: [survivor])
    monkeypatch.setattr(supervisor, "_finish_ledger", lambda *args: pytest.fail("ledger must not be edited with survivors"))
    result = supervisor._settle(handle, value, state="cancelled", reason="test")
    assert result["state"] == "indeterminate"
    assert result["survivors"] == [survivor.to_dict()]
    assert json.loads(value.lease_path.read_text())["released"] is False


def test_missing_local_extra_refuses_before_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "psutil", None)
    with pytest.raises(supervisor.SupervisorUnsupported, match="local"):
        supervisor.start(spec(tmp_path, "pass"))
    assert not (tmp_path / "run").exists()


def test_torn_state_never_means_absent(tmp_path: Path) -> None:
    value = spec(tmp_path, "pass")
    value.run_dir.mkdir()
    (value.run_dir / "intent.json").write_text('{"schema":')
    with pytest.raises(SupervisorError, match="Cannot read"):
        supervisor.inspect(value.run_dir)


def _exclusive_windows_read(path: Path) -> tuple[Any, int]:
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
    return kernel, handle


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows exclusive file sharing")
@pytest.mark.parametrize("disappear", [False, True])
def test_state_read_sharing_denial_stays_uncertain_until_readable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                               disappear: bool) -> None:
    path = tmp_path / "state.json"
    supervisor._write(path, {"value": "observed"})
    kernel, held = _exclusive_windows_read(path)
    original = Path.read_text
    denied = False
    closed = False

    def observed_read(candidate: Path, *args: Any, **kwargs: Any) -> str:
        nonlocal denied, closed
        try:
            return original(candidate, *args, **kwargs)
        except PermissionError:
            if candidate == path and not denied:
                denied = True
                assert kernel.CloseHandle(held)
                closed = True
                if disappear:
                    path.unlink()
            raise

    monkeypatch.setattr(Path, "read_text", observed_read)
    try:
        if disappear:
            with pytest.raises(SupervisorError, match="disappeared after"):
                supervisor._read(path)
        else:
            assert supervisor._read(path)["value"] == "observed"
        assert denied
    finally:
        if not closed:
            kernel.CloseHandle(held)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows exclusive file sharing")
def test_persistent_state_denial_respects_startup_deadline_and_retains_lease(tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, handles: list[ProcessHandle]) -> None:
    value = spec(tmp_path, "raise AssertionError('must not dispatch')")
    locked: list[tuple[Any, int]] = []

    def publish_locked_go(*args: Any, **kwargs: Any) -> None:
        intent = json.loads((value.run_dir / "intent.json").read_text())
        path = value.run_dir / "go.json"
        supervisor._write(path, {"nonce": intent["nonce"]})
        locked.append(_exclusive_windows_read(path))

    try:
        with monkeypatch.context() as patch:
            patch.setattr(supervisor.subprocess, "Popen", publish_locked_go)
            started = time.monotonic()
            with pytest.raises(SupervisorError, match="Cannot read") as caught:
                supervisor.start(value, startup_timeout_s=0.05)
            elapsed = time.monotonic() - started
        assert caught.value.handle is not None
        handles.append(caught.value.handle)
        assert elapsed < 0.5, elapsed
        assert json.loads(value.lease_path.read_text())["released"] is False
        assert not (value.run_dir / "runner.json").exists()
    finally:
        for kernel, held in locked:
            kernel.CloseHandle(held)


def test_manager_death_does_not_end_detached_owner(tmp_path: Path, handles: list[ProcessHandle]) -> None:
    handle_file = tmp_path / "handle.json"
    manager_file = tmp_path / "manager.json"
    value = spec(tmp_path, "import time; time.sleep(60)")
    source_root = str(Path(supervisor.__file__).parents[2])
    manager_code = (
        "import sys,json,time,os,psutil; from pathlib import Path; sys.path.insert(0,%r); "
        "Path(%r).write_text(json.dumps({'pid':os.getpid(),'create_time':psutil.Process().create_time()})); "
        "from llm_kit.jobs.supervisor import JobProcessSpec,start; "
        "h=start(JobProcessSpec.from_dict(json.loads(%r))); Path(%r).write_text(json.dumps(h.to_dict())); time.sleep(60)"
    ) % (source_root, str(manager_file), json.dumps(value.to_dict()), str(handle_file))
    manager = subprocess.Popen([sys.executable, "-c", manager_code])
    try:
        eventually(handle_file.exists)
        handle = ProcessHandle.from_dict(json.loads(handle_file.read_text()))
        handles.append(handle)
        actual = processes.ProcessIdentity.from_dict(json.loads(manager_file.read_text()))
        live_manager = processes.live(actual)
        assert live_manager is not None
        live_manager.kill()
        live_manager.wait(timeout=5)
        manager.wait(timeout=5)
        assert processes.live(actual) is None
        assert supervisor.adopt(handle).alive
        assert supervisor.cancel(handle, "manager restarted").state == "cancelled"
    finally:
        if manager_file.exists():
            actual = processes.ProcessIdentity.from_dict(json.loads(manager_file.read_text()))
            live_manager = processes.live(actual)
            if live_manager is not None:
                live_manager.kill()
                live_manager.wait(timeout=5)
        if manager.poll() is None:
            manager.kill()
            manager.wait(timeout=5)


def test_duplicate_owner_never_dispatches_again(tmp_path: Path, handles: list[ProcessHandle]) -> None:
    marker = tmp_path / "executions"
    body = "from pathlib import Path; import time; Path(%r).write_text('one'); time.sleep(60)" % str(marker)
    handle = supervisor.start(spec(tmp_path, body))
    handles.append(handle)
    eventually(marker.exists)
    worker = Path(supervisor.__file__).with_name("_supervisor_worker.py")
    duplicate = subprocess.run([sys.executable, str(worker), "run", str(handle.run_dir), handle.nonce],
                               capture_output=True, text=True, timeout=10)
    assert duplicate.returncode == 2
    assert "already held" in duplicate.stderr
    assert not (handle.run_dir / "result.json").exists()
    assert marker.read_text() == "one"


def test_child_never_executes_after_owner_dies_before_go(tmp_path: Path) -> None:
    value = spec(tmp_path, "from pathlib import Path; Path('must-not-execute').touch()")
    handle = ProcessHandle(value.run_dir, "a" * 32)
    value.run_dir.mkdir()
    supervisor._write(handle.run_dir / "intent.json", {"nonce": handle.nonce, "spec": value.to_dict()})
    me = processes.identity(os.getpid())
    assert me is not None
    dead = processes.ProcessIdentity(me.pid, me.create_time - 1)
    supervisor._write(handle.run_dir / "runner.json", {"nonce": handle.nonce, "process": dead.to_dict()})
    worker = Path(supervisor.__file__).with_name("_supervisor_worker.py")
    child = subprocess.run([sys.executable, str(worker), "child", str(handle.run_dir), handle.nonce], timeout=10)
    assert child.returncode == 75
    assert (handle.run_dir / "child.json").is_file()
    assert not (tmp_path / "must-not-execute").exists()


def test_timed_out_ledger_finalizer_is_stopped_and_lease_retained(tmp_path: Path, handles: list[ProcessHandle]) -> None:
    marker = tmp_path / "finalizer.pid"
    finalizer = "import os,time; from pathlib import Path; Path(%r).write_text(str(os.getpid())); time.sleep(60)" % str(marker)
    value = spec(tmp_path, "import time; time.sleep(60)",
                 cancel_argv=(sys.executable, "-c", finalizer), cancel_timeout_s=0.5)
    handle = supervisor.start(value)
    handles.append(handle)
    status = supervisor.cancel(handle, "test finalizer timeout")
    status = eventually(lambda: finished(handle))
    assert status.state == "indeterminate"
    assert "timed out" in status.result["cancel"]["error"]
    pid = int(marker.read_text())
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    assert not status.alive
    assert json.loads(value.lease_path.read_text())["released"] is False


def test_adopting_history_does_not_release_a_newer_lease(tmp_path: Path, handles: list[ProcessHandle]) -> None:
    lease = tmp_path / "shared.lease"
    first = supervisor.start(spec(tmp_path, "pass", lease_path=lease))
    handles.append(first)
    eventually(lambda: finished(first))
    second = supervisor.start(replace(spec(tmp_path, "import time; time.sleep(60)", lease_path=lease),
                                     run_dir=tmp_path / "second"))
    handles.append(second)
    assert supervisor.adopt(first).state == "finished"
    document = json.loads(lease.read_text())
    assert document["nonce"] == second.nonce
    assert document["released"] is False
