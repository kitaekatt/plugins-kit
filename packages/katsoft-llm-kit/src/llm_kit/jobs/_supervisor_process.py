"""Private local process ownership for supervised jobs.

psutil is optional (the package's ``local`` extra). No process is signalled
without comparing its exact creation time. POSIX session enumeration catches
orphans; Windows job objects keep descendants inside a kill-on-close boundary.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any


class ProcessError(RuntimeError):
    """The OS cannot establish or enforce owned-process state."""


def psutil_module() -> Any:
    try:
        import psutil
    except ImportError as exc:
        raise ProcessError("Supervised jobs require katsoft-llm-kit[local] (psutil).") from exc
    return psutil


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    create_time: float

    def to_dict(self) -> dict[str, int | float]:
        return {"pid": self.pid, "create_time": self.create_time}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ProcessIdentity:
        if not isinstance(value, dict) or set(value) != {"pid", "create_time"}:
            raise ValueError("Invalid supervised process identity")
        pid, created = value["pid"], value["create_time"]
        if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
            raise ValueError("Invalid supervised process PID")
        if isinstance(created, bool) or not isinstance(created, (int, float)) or created <= 0:
            raise ValueError("Invalid supervised process creation time")
        return cls(pid, float(created))


def identity(pid: int) -> ProcessIdentity | None:
    psutil = psutil_module()
    try:
        return ProcessIdentity(pid, psutil.Process(pid).create_time())
    except psutil.NoSuchProcess:
        return None


def live(ident: ProcessIdentity) -> Any | None:
    psutil = psutil_module()
    try:
        proc = psutil.Process(ident.pid)
        if proc.create_time() != ident.create_time or proc.status() == psutil.STATUS_ZOMBIE:
            return None
        return proc
    except psutil.NoSuchProcess:
        return None


def descendants(ident: ProcessIdentity) -> list[ProcessIdentity]:
    psutil = psutil_module()
    parent = live(ident)
    if parent is None:
        return []
    found = []
    try:
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        return []
    for child in children:
        try:
            found.append(ProcessIdentity(child.pid, child.create_time()))
        except psutil.NoSuchProcess:
            continue
    return found


def session_members(leader: ProcessIdentity) -> list[ProcessIdentity]:
    if os.name != "posix":
        return []
    psutil = psutil_module()
    found = []
    for proc in psutil.process_iter():
        try:
            if os.getsid(proc.pid) != leader.pid:
                continue
            created = proc.create_time()
            if created >= leader.create_time:
                found.append(ProcessIdentity(proc.pid, created))
        except (psutil.NoSuchProcess, ProcessLookupError):
            continue
        except (psutil.AccessDenied, PermissionError):
            # Other users' sessions are outside this ownership boundary.
            # Access to an explicitly recorded process is never ignored.
            continue
    return found


def reachable(child: ProcessIdentity | None, recorded: list[ProcessIdentity]) -> list[ProcessIdentity]:
    roots = list(recorded) + ([child] if child is not None else [])
    members = {(item.pid, item.create_time): item for item in roots}
    for item in roots:
        for found in descendants(item):
            members[(found.pid, found.create_time)] = found
    if child is not None:
        for found in session_members(child):
            members[(found.pid, found.create_time)] = found
    return [item for item in members.values() if live(item) is not None]


def stop(child: ProcessIdentity | None, recorded: list[ProcessIdentity], grace_s: float) -> list[ProcessIdentity]:
    """Stop the known tree and its session; return any verified survivors."""
    psutil = psutil_module()
    seen = {(p.pid, p.create_time): p for p in reachable(child, recorded)}
    for signal_name in ("terminate", "kill"):
        targets = reachable(child, list(seen.values()))
        # Stop descendants before their ancestor. Depth is observable parentage,
        # never PID order (Windows launchers can have larger ancestor PIDs).
        def depth(item: ProcessIdentity) -> int:
            proc = live(item)
            if proc is None:
                return 0
            try:
                return len(proc.parents())
            except psutil.NoSuchProcess:
                return 0
        for item in sorted(targets, key=depth, reverse=True):
            seen[(item.pid, item.create_time)] = item
            proc = live(item)
            if proc is not None:
                try:
                    getattr(proc, signal_name)()
                except psutil.NoSuchProcess:
                    continue
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            remaining = reachable(child, list(seen.values()))
            if not remaining:
                return []
            seen.update({(p.pid, p.create_time): p for p in remaining})
            time.sleep(0.02)
    return reachable(child, list(seen.values()))


def assign_windows_job() -> Any:
    """Assign this supervisor to a kill-on-close job before it starts a child."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class Basic(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
        ]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Counters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise ProcessError(f"CreateJobObject failed: {ctypes.get_last_error()}")
    info = Extended()
    info.BasicLimitInformation.LimitFlags = 0x2000
    if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        error = ctypes.get_last_error()
        kernel.CloseHandle(handle)
        raise ProcessError(f"SetInformationJobObject failed: {error}")
    if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        error = ctypes.get_last_error()
        kernel.CloseHandle(handle)
        raise ProcessError(f"AssignProcessToJobObject failed: {error}")
    return handle
