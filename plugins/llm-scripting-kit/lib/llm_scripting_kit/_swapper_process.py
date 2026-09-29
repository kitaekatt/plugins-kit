"""Local process inspection and signalling for the swapper lifecycle.

This is the process layer under :mod:`llm_scripting_kit.swapper`. It answers
narrow questions about the local process table -- who is this PID, who are
its peers, which TCP ports does it listen on -- and delivers a signal to one
PID whose identity it re-checks first. It makes no safety decision: which
PID may be signalled is decided in ``swapper.py``, against the
:class:`Inspector` protocol, so tests drive that logic with a fake.

``psutil`` is imported lazily inside :class:`PsutilInspector`, so importing
this module (or ``swapper``) costs nothing for an HTTP-only consumer.

Listener discovery uses per-process ``Process.net_connections()`` on the
candidate processes only, never the system-wide ``psutil.net_connections()``:
on macOS the system-wide call needs root, while the per-process call works
for the caller's own processes. The same path runs on Linux and Windows.

Error vocabulary, shared by every inspector:

- :class:`ProcessNotFound` -- the PID does not exist, or it exists with a
  different ``create_time`` (the PID was reused).
- :class:`InspectionIndeterminate` -- the question could not be answered
  (access denied, or ``psutil`` unavailable). It is never reported as
  "not found".
"""

from __future__ import annotations

import errno
import os
import socket
import sys
import time
from dataclasses import dataclass
from typing import FrozenSet, Optional, Protocol, Tuple

from .swapper import InspectionIndeterminate, ProcessNotFound

#: The two signals the lifecycle sends. Names are portable strings; the
#: psutil inspector maps them to ``Process.terminate()`` / ``Process.kill()``,
#: which are SIGTERM / SIGKILL on POSIX and TerminateProcess on Windows.
SIGTERM = "SIGTERM"
SIGKILL = "SIGKILL"
SIGNALS = (SIGTERM, SIGKILL)

#: Poll interval while waiting for a signalled process to exit.
_WAIT_POLL_S = 0.1


@dataclass(frozen=True)
class ProcessRecord:
    """One process as the inspector saw it.

    ``owner`` is the real uid as a string on POSIX and the account name on
    Windows. ``name`` and ``cmdline`` are None when the OS denied reading
    them; callers treat None as indeterminate, never as a non-match.
    """

    pid: int
    ppid: Optional[int]
    name: Optional[str]
    owner: Optional[str]
    create_time: float
    cmdline: Optional[Tuple[str, ...]] = None


#: A listening TCP socket address: (ip, port).
ListenAddr = Tuple[str, int]


class Inspector(Protocol):
    """What the swapper lifecycle needs from the local process table."""

    def current_owner(self) -> str:
        """The owner key (see :class:`ProcessRecord`) of the calling user."""

    def process(self, pid: int) -> ProcessRecord:
        """One process. Raises ProcessNotFound or InspectionIndeterminate."""

    def processes(self) -> Tuple[ProcessRecord, ...]:
        """Every visible process. Processes that exit mid-scan are omitted."""

    def listen_addrs(self, pid: int) -> FrozenSet[ListenAddr]:
        """TCP LISTEN addresses of ``pid``. Raises like :meth:`process`."""

    def signal(self, pid: int, create_time: float, sig: str) -> None:
        """Send ``sig`` to ``pid`` only if its create_time still matches.

        Raises ProcessNotFound when the PID is gone or was reused, and
        InspectionIndeterminate when the OS denies the signal.
        """

    def wait_gone(self, pid: int, create_time: float, timeout_s: float) -> bool:
        """Wait up to ``timeout_s`` for the process to exit.

        True when the PID is gone, reused, or a zombie awaiting its parent.
        """


def normalize_name(name: Optional[str]) -> Optional[str]:
    """Drop a Windows ``.exe`` suffix so exact-name checks are portable."""
    if name is None:
        return None
    if name.lower().endswith(".exe"):
        return name[:-4]
    return name


class PsutilInspector:
    """The real :class:`Inspector`, backed by ``psutil``."""

    def __init__(self) -> None:
        try:
            import psutil  # noqa: PLC0415 -- lazy by design, see module doc
        except ImportError as exc:
            raise InspectionIndeterminate(
                "psutil is not importable, so local processes cannot be "
                f"inspected ({exc}); it is declared in llm-scripting-kit's "
                "pyproject.toml and provisioned by bootstrap"
            ) from exc
        self._ps = psutil

    # -- owner --------------------------------------------------------------

    def current_owner(self) -> str:
        if hasattr(os, "getuid"):
            return str(os.getuid())
        return self._guard(os.getpid(), lambda: self._ps.Process().username())

    def _owner_of(self, proc) -> Optional[str]:
        try:
            if sys.platform == "win32":
                return proc.username()
            return str(proc.uids().real)
        except self._ps.AccessDenied:
            return None

    # -- reads --------------------------------------------------------------

    def _guard(self, pid: int, fn):
        ps = self._ps
        try:
            return fn()
        except ps.NoSuchProcess as exc:  # includes ZombieProcess
            if isinstance(exc, ps.ZombieProcess):
                raise InspectionIndeterminate(
                    f"process {pid} is a zombie; its details are unreadable"
                ) from exc
            raise ProcessNotFound(f"process {pid} does not exist") from exc
        except ps.AccessDenied as exc:
            raise InspectionIndeterminate(
                f"access denied while inspecting process {pid}"
            ) from exc

    def _record(self, proc) -> ProcessRecord:
        ps = self._ps
        with proc.oneshot():
            ppid = proc.ppid()
            create_time = proc.create_time()
            try:
                name: Optional[str] = proc.name()
            except ps.AccessDenied:
                name = None
            try:
                cmdline: Optional[Tuple[str, ...]] = tuple(proc.cmdline())
            except ps.AccessDenied:
                cmdline = None
            owner = self._owner_of(proc)
        return ProcessRecord(
            pid=proc.pid,
            ppid=ppid,
            name=name,
            owner=owner,
            create_time=create_time,
            cmdline=cmdline,
        )

    def process(self, pid: int) -> ProcessRecord:
        return self._guard(pid, lambda: self._record(self._ps.Process(pid)))

    def processes(self) -> Tuple[ProcessRecord, ...]:
        ps = self._ps
        out = []
        for proc in ps.process_iter():
            try:
                out.append(self._record(proc))
            except ps.NoSuchProcess:
                continue  # exited mid-scan
            except ps.AccessDenied:
                # ppid/create_time unreadable: keep a record whose unknown
                # fields read as indeterminate rather than dropping it.
                out.append(
                    ProcessRecord(
                        pid=proc.pid, ppid=None, name=None, owner=None,
                        create_time=0.0, cmdline=None,
                    )
                )
        return tuple(out)

    def listen_addrs(self, pid: int) -> FrozenSet[ListenAddr]:
        ps = self._ps

        def read() -> FrozenSet[ListenAddr]:
            conns = ps.Process(pid).net_connections(kind="tcp")
            return frozenset(
                (c.laddr.ip, c.laddr.port)
                for c in conns
                if c.status == ps.CONN_LISTEN and c.laddr
            )

        return self._guard(pid, read)

    # -- signalling ---------------------------------------------------------

    def _same_process(self, pid: int, create_time: float):
        """The live psutil.Process for ``pid`` iff create_time still matches."""
        proc = self._guard(pid, lambda: self._ps.Process(pid))
        actual = self._guard(pid, proc.create_time)
        if actual != create_time:
            raise ProcessNotFound(
                f"process {pid} was replaced (create_time {actual} != {create_time})"
            )
        return proc

    def signal(self, pid: int, create_time: float, sig: str) -> None:
        if sig not in SIGNALS:
            raise ValueError(f"unsupported signal {sig!r}; expected one of {SIGNALS}")
        proc = self._same_process(pid, create_time)
        # psutil.Process.terminate()/kill() also refuse a reused PID.
        send = proc.terminate if sig == SIGTERM else proc.kill
        self._guard(pid, send)

    def wait_gone(self, pid: int, create_time: float, timeout_s: float) -> bool:
        ps = self._ps
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            try:
                proc = ps.Process(pid)
                if proc.create_time() != create_time:
                    return True
                if proc.status() == ps.STATUS_ZOMBIE:
                    return True
            except ps.NoSuchProcess:
                return True
            except ps.AccessDenied:
                pass  # still present, unreadable: keep waiting
            if time.monotonic() >= deadline:
                return False
            time.sleep(_WAIT_POLL_S)


def port_is_free(port: int) -> bool:
    """Bind and connect probes for TCP ``port``: necessary, not sufficient.

    Does not consult the process table. The port is occupied when a loopback
    connect (IPv4, or IPv6 where available) succeeds, when binding the IPv4
    loopback or wildcard address fails, or when binding the IPv6 wildcard fails
    with an address-in-use or permission error. ``SO_REUSEADDR`` is set off
    Windows, so sockets in TIME_WAIT do not read as a live listener.

    A True result does NOT prove the port is unused: a listener bound only to
    one specific non-loopback address, or only to IPv6, can leave every probe
    here succeeding on some platforms (macOS, Windows). Callers that must know
    the port is clear also scan listener sockets, as
    :func:`llm_scripting_kit.swapper.terminate_listener` does.
    """
    hosts = ["127.0.0.1"] + (["::1"] if socket.has_ipv6 else [])
    for host in hosts:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return False
        except OSError:
            pass
    for host in ("127.0.0.1", "0.0.0.0"):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if sys.platform != "win32":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
        except OSError:
            return False
        finally:
            probe.close()
    if socket.has_ipv6:
        try:
            probe6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        except OSError:
            return True
        try:
            if sys.platform != "win32":
                probe6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            probe6.bind(("::", port))
        except OSError as exc:
            # Only "in use" and "denied" mean occupied; an absent or disabled
            # IPv6 stack says nothing about the port.
            if exc.errno in (errno.EADDRINUSE, errno.EACCES):
                return False
        finally:
            probe6.close()
    return True


__all__ = [
    "SIGTERM",
    "SIGKILL",
    "SIGNALS",
    "ProcessRecord",
    "ListenAddr",
    "Inspector",
    "PsutilInspector",
    "normalize_name",
    "port_is_free",
]
