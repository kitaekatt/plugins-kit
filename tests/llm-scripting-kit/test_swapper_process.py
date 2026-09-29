"""Tests for llm_scripting_kit._swapper_process (the psutil adapter).

Most tests run against a fake ``psutil`` module. The real-psutil tests signal
only a ``sleep`` child this test spawned itself.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from collections import namedtuple

import pytest

from llm_scripting_kit import _swapper_process as spm
from llm_scripting_kit.swapper import InspectionIndeterminate, ProcessNotFound

# ---------------------------------------------------------------------------
# Fake psutil
# ---------------------------------------------------------------------------

Addr = namedtuple("Addr", "ip port")
Conn = namedtuple("Conn", "laddr status")
Uids = namedtuple("Uids", "real effective saved")


class _NoSuchProcess(Exception):
    pass


class _ZombieProcess(_NoSuchProcess):
    pass


class _AccessDenied(Exception):
    pass


class FakeProc:
    def __init__(self, pid, *, ppid=1, name="x", create_time=100.0, cmdline=("x",),
                 uid=1000, conns=(), status="running", deny=(), gone=False):
        self.pid = pid
        self._ppid = ppid
        self._name = name
        self._ct = create_time
        self._cmdline = cmdline
        self._uid = uid
        self._conns = conns
        self._status = status
        self.deny = set(deny)
        self.gone = gone
        self.sent = []

    class _One:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def oneshot(self):
        return self._One()

    def _check(self, what):
        if self.gone:
            raise _NoSuchProcess(self.pid)
        if what in self.deny:
            raise _AccessDenied(what)

    def ppid(self):
        self._check("ppid")
        return self._ppid

    def create_time(self):
        self._check("create_time")
        return self._ct

    def name(self):
        self._check("name")
        return self._name

    def cmdline(self):
        self._check("cmdline")
        return list(self._cmdline)

    def uids(self):
        self._check("uids")
        return Uids(self._uid, self._uid, self._uid)

    def username(self):
        self._check("uids")
        return f"user{self._uid}"

    def net_connections(self, kind="inet"):
        assert kind == "tcp"
        self._check("net_connections")
        return list(self._conns)

    def status(self):
        self._check("status")
        return self._status

    def terminate(self):
        self._check("signal")
        self.sent.append("TERM")

    def kill(self):
        self._check("signal")
        self.sent.append("KILL")


def make_fake_psutil(procs):
    table = {p.pid: p for p in procs}
    mod = types.ModuleType("psutil")
    mod.NoSuchProcess = _NoSuchProcess
    mod.ZombieProcess = _ZombieProcess
    mod.AccessDenied = _AccessDenied
    mod.CONN_LISTEN = "LISTEN"
    mod.STATUS_ZOMBIE = "zombie"

    def Process(pid=None):
        if pid not in table:
            raise _NoSuchProcess(pid)
        return table[pid]

    mod.Process = Process
    mod.process_iter = lambda: list(table.values())
    mod._table = table
    return mod


@pytest.fixture
def fake_ps(monkeypatch):
    def install(*procs):
        mod = make_fake_psutil(procs)
        monkeypatch.setitem(sys.modules, "psutil", mod)
        return spm.PsutilInspector(), mod

    return install


# ---------------------------------------------------------------------------
# Fake-psutil tests
# ---------------------------------------------------------------------------


def test_missing_psutil_is_indeterminate(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    with pytest.raises(InspectionIndeterminate, match="psutil"):
        spm.PsutilInspector()


def test_process_record_fields(fake_ps):
    insp, _ = fake_ps(FakeProc(10, ppid=2, name="ninfer-serve", create_time=5.0,
                               cmdline=("ninfer-serve", "--port", "5800"), uid=1000))
    r = insp.process(10)
    assert (r.pid, r.ppid, r.name, r.create_time) == (10, 2, "ninfer-serve", 5.0)
    assert r.cmdline == ("ninfer-serve", "--port", "5800")
    assert r.owner in ("1000", "user1000")


def test_missing_process_is_not_found(fake_ps):
    insp, _ = fake_ps()
    with pytest.raises(ProcessNotFound):
        insp.process(10)


def test_denied_essentials_are_indeterminate_not_not_found(fake_ps):
    insp, _ = fake_ps(FakeProc(10, deny={"create_time"}))
    with pytest.raises(InspectionIndeterminate):
        insp.process(10)


def test_denied_name_and_cmdline_read_as_none(fake_ps):
    insp, _ = fake_ps(FakeProc(10, deny={"name", "cmdline", "uids"}))
    r = insp.process(10)
    assert r.name is None and r.cmdline is None and r.owner is None


def test_zombie_details_are_indeterminate(fake_ps):
    insp, mod = fake_ps(FakeProc(10))

    def boom():
        raise _ZombieProcess(10)

    mod._table[10].ppid = boom
    with pytest.raises(InspectionIndeterminate, match="zombie"):
        insp.process(10)


def test_processes_skips_vanished_and_keeps_denied(fake_ps):
    insp, _ = fake_ps(
        FakeProc(1, name="init"),
        FakeProc(2, gone=True),
        FakeProc(3, deny={"ppid"}),
    )
    recs = {r.pid: r for r in insp.processes()}
    assert set(recs) == {1, 3}
    assert recs[3].ppid is None and recs[3].name is None


def test_listen_addrs_only_listen_sockets(fake_ps):
    conns = (
        Conn(Addr("127.0.0.1", 5800), "LISTEN"),
        Conn(Addr("127.0.0.1", 40000), "ESTABLISHED"),
        Conn(Addr("0.0.0.0", 8081), "LISTEN"),
    )
    insp, _ = fake_ps(FakeProc(10, conns=conns))
    assert insp.listen_addrs(10) == {("127.0.0.1", 5800), ("0.0.0.0", 8081)}


def test_listen_addrs_denied_is_indeterminate(fake_ps):
    insp, _ = fake_ps(FakeProc(10, deny={"net_connections"}))
    with pytest.raises(InspectionIndeterminate):
        insp.listen_addrs(10)


def test_signal_maps_term_and_kill(fake_ps):
    insp, mod = fake_ps(FakeProc(10, create_time=5.0))
    insp.signal(10, 5.0, spm.SIGTERM)
    insp.signal(10, 5.0, spm.SIGKILL)
    assert mod._table[10].sent == ["TERM", "KILL"]


def test_signal_refuses_reused_pid(fake_ps):
    insp, mod = fake_ps(FakeProc(10, create_time=6.0))
    with pytest.raises(ProcessNotFound):
        insp.signal(10, 5.0, spm.SIGTERM)
    assert mod._table[10].sent == []


def test_signal_denied_is_indeterminate(fake_ps):
    insp, _ = fake_ps(FakeProc(10, create_time=5.0, deny={"signal"}))
    with pytest.raises(InspectionIndeterminate):
        insp.signal(10, 5.0, spm.SIGTERM)


def test_signal_rejects_unknown_signal(fake_ps):
    insp, mod = fake_ps(FakeProc(10, create_time=5.0))
    with pytest.raises(ValueError):
        insp.signal(10, 5.0, "SIGHUP")
    assert mod._table[10].sent == []


def test_wait_gone_states(fake_ps):
    insp, _ = fake_ps(
        FakeProc(10, create_time=5.0, status="zombie"),
        FakeProc(11, create_time=7.0),
        FakeProc(12, create_time=5.0),
    )
    assert insp.wait_gone(10, 5.0, 0) is True  # zombie awaiting reap
    assert insp.wait_gone(11, 5.0, 0) is True  # PID reused
    assert insp.wait_gone(99, 5.0, 0) is True  # gone
    assert insp.wait_gone(12, 5.0, 0) is False  # still alive at timeout


@pytest.mark.parametrize(
    "raw,expected",
    [("llama-swap", "llama-swap"), ("llama-swap.exe", "llama-swap"),
     ("LLAMA-SERVER.EXE", "LLAMA-SERVER"), (None, None)],
)
def test_normalize_name(raw, expected):
    assert spm.normalize_name(raw) == expected


# ---------------------------------------------------------------------------
# Real psutil, against a child this test owns
# ---------------------------------------------------------------------------

# The child reports its own PID: on Windows a venv interpreter is a launcher
# that runs the real interpreter as ITS child, so Popen.pid is not the
# process holding the socket.
_CHILD = (
    "import os, socket, time\n"
    "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\n"
    "print(os.getpid(), s.getsockname()[1], flush=True)\n"
    "time.sleep(60)\n"
)


@pytest.fixture
def child():
    psutil = pytest.importorskip("psutil")
    proc = subprocess.Popen([sys.executable, "-c", _CHILD], stdout=subprocess.PIPE, text=True)
    real = None
    try:
        pid, port = (int(x) for x in proc.stdout.readline().split())
        real = psutil.Process(pid)
        yield proc, pid, port
    finally:
        if real is not None and real.is_running():
            real.kill()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        proc.stdout.close()


def test_real_inspector_reads_own_child(child):
    proc, pid, port = child
    insp = spm.PsutilInspector()
    rec = insp.process(pid)
    assert rec.ppid in (os.getpid(), proc.pid)
    assert rec.owner == insp.current_owner()
    assert ("127.0.0.1", port) in insp.listen_addrs(pid)
    assert pid in {r.pid for r in insp.processes()}


def test_real_signal_refuses_wrong_create_time_then_terminates(child):
    _, pid, _ = child
    insp = spm.PsutilInspector()
    ct = insp.process(pid).create_time
    with pytest.raises(ProcessNotFound):
        insp.signal(pid, ct + 1000.0, spm.SIGTERM)
    assert insp.wait_gone(pid, ct, 0.3) is False, "a mismatched create_time must not signal"
    insp.signal(pid, ct, spm.SIGTERM)
    assert insp.wait_gone(pid, ct, 10.0) is True


# ---------------------------------------------------------------------------
# port_is_free: the independent bind check (real loopback sockets only)
# ---------------------------------------------------------------------------


def test_port_is_free_sees_a_live_listener_and_a_released_port():
    import socket

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert spm.port_is_free(port) is False
    finally:
        srv.close()
    assert spm.port_is_free(port) is True


def test_port_is_free_sees_an_ipv6_loopback_listener():
    import socket

    if not socket.has_ipv6:
        pytest.skip("no IPv6 on this host")
    srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        srv.bind(("::1", 0))
    except OSError:
        srv.close()
        pytest.skip("IPv6 loopback unavailable")
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert spm.port_is_free(port) is False
    finally:
        srv.close()
