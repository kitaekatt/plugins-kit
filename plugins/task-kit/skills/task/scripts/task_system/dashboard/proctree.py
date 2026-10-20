"""Process-tree identity and shutdown, cross-platform, without ``ps``/``lsof``.

A process is identified by PID **and** creation time, because a PID can be
reused: before any signal the live process's creation time must match the
recorded one, so a stale identity never kills an unrelated process.

Stopping a tree is leaves-first (children before their parents), polite first
(terminate, then a grace wait) and then forceful (kill), and it reports the
survivors instead of pretending.

On Windows a runner also needs a kill-on-close Job Object assigned before it
dispatches anything: orphaned grandchildren have no parent to walk to, but
every descendant inherits the job, and closing the last handle (including when
the runner itself is killed) ends them all.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

CREATE_TIME_TOLERANCE_S = 1.0


class ProcTreeError(RuntimeError):
    """The platform refused a process-control primitive that cannot be skipped."""


@dataclass(frozen=True)
class ProcIdent:
    pid: int
    create_time: float

    def to_json(self) -> list:
        return [self.pid, self.create_time]

    @classmethod
    def from_json(cls, value) -> "ProcIdent":
        return cls(int(value[0]), float(value[1]))


def ident_of(pid: int) -> ProcIdent | None:
    try:
        return ProcIdent(pid, psutil.Process(pid).create_time())
    except psutil.NoSuchProcess:
        return None


def live(ident: ProcIdent) -> psutil.Process | None:
    """The process ``ident`` names, or None when it is gone or the PID was reused."""
    try:
        proc = psutil.Process(ident.pid)
        if abs(proc.create_time() - ident.create_time) > CREATE_TIME_TOLERANCE_S:
            return None
        if proc.status() == psutil.STATUS_ZOMBIE:
            return None
        return proc
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return None


def descendants(pid: int) -> list[ProcIdent]:
    """Every current descendant of ``pid`` (not ``pid`` itself)."""
    try:
        children = psutil.Process(pid).children(recursive=True)
    except psutil.NoSuchProcess:
        return []
    found = []
    for child in children:
        try:
            found.append(ProcIdent(child.pid, child.create_time()))
        except psutil.NoSuchProcess:
            continue
    return found


RUNNER_SCRIPT_NAME = "runner_cli.py"


def dashboard_runner_ancestor(root: Path) -> ProcIdent | None:
    """The live dashboard runner this process descends from, or None.

    ``root`` is a dashboard's runtime root. An ancestor counts only when ALL of these hold: its command
    line names ``runner_cli.py`` followed by a run directory under ``<root>/projects/``, and that
    directory's ``runner.json`` records this very process (PID and creation time). A look-alike command
    line, or a run directory of another dashboard (another ``--data-dir``), never matches.

    This is an executable-level guard, not confinement: ``env -u``, absolute paths and shell wrappers keep
    the ancestry, but a double fork that reparents before the check, or a direct call to the dashboard's
    HTTP API, does not.
    """
    projects = os.path.normcase(os.path.realpath(str(Path(root) / "projects")))
    try:
        ancestors = psutil.Process().parents()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    for proc in ancestors:
        try:
            cmdline = proc.cmdline()
            created = proc.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        run_dir = None
        for index, arg in enumerate(cmdline[:-1]):
            if os.path.basename(arg) == RUNNER_SCRIPT_NAME:
                run_dir = cmdline[index + 1]
                break
        if run_dir is None:
            continue
        resolved = os.path.normcase(os.path.realpath(run_dir))
        if not resolved.startswith(projects + os.sep):
            continue
        try:
            record = json.loads((Path(resolved) / "runner.json").read_text(encoding="utf-8"))
            recorded = ProcIdent(int(record["pid"]), float(record["create_time"]))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if recorded.pid == proc.pid and abs(recorded.create_time - created) <= CREATE_TIME_TOLERANCE_S:
            return recorded
    return None


def session_members(leader: ProcIdent) -> list[ProcIdent]:
    """Processes still in the session ``leader`` started, however far they were orphaned (POSIX).

    A command started with its own session keeps that session's id after its parent dies
    and its children are re-parented, so walking children can no longer find them but the
    session still can. The leader's PID cannot be reused while any member lives, and a
    process that began before the leader cannot be its member, which together keep an
    unrelated process that happens to reuse the number out. A member that called ``setsid``
    itself has left the session and is only found if it was recorded earlier.
    """
    if os.name != "posix":
        return []
    found = []
    for proc in psutil.process_iter():
        try:
            if proc.pid == leader.pid or os.getsid(proc.pid) != leader.pid:
                continue
            created = proc.create_time()
            if created + CREATE_TIME_TOLERANCE_S < leader.create_time:
                continue
            found.append(ProcIdent(proc.pid, created))
        except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError, PermissionError):
            continue
    return found


def reachable(recorded: list[ProcIdent], child: ProcIdent | None) -> list[ProcIdent]:
    """Everything of a dead runner's command that is still around.

    The recorded idents (``child.json`` and ``tree.json``), whatever descends from any of
    them that is still alive, and the command's leftover session. Each member is verified
    again by PID and creation time before it is signalled (see :func:`stop_idents`).
    """
    roots = ([child] if child is not None else []) + list(recorded)
    members: dict[int, ProcIdent] = {r.pid: r for r in roots}
    for root in roots:
        if live(root) is not None:
            for item in descendants(root.pid):
                members.setdefault(item.pid, item)
    if child is not None:
        for item in session_members(child):
            members.setdefault(item.pid, item)
    return [m for m in members.values() if live(m) is not None]


def _depths(idents: list[ProcIdent]) -> dict[int, int]:
    """Depth of each process below the shallowest member (deepest = leaves)."""
    parents: dict[int, int | None] = {}
    for item in idents:
        proc = live(item)
        parents[item.pid] = proc.ppid() if proc is not None else None
    depth: dict[int, int] = {}

    def of(pid: int, seen: frozenset = frozenset()) -> int:
        if pid in depth:
            return depth[pid]
        parent = parents.get(pid)
        value = 0 if parent is None or parent not in parents or parent in seen else 1 + of(parent, seen | {pid})
        depth[pid] = value
        return value

    for item in idents:
        of(item.pid)
    return depth


def stop_idents(idents: list[ProcIdent], *, grace_s: float = 5.0) -> list[ProcIdent]:
    """Terminate then kill the named processes, leaves first; the survivors."""
    unique = {i.pid: i for i in idents}
    ordered = list(unique.values())
    depth = _depths(ordered)
    ordered.sort(key=lambda i: depth.get(i.pid, 0), reverse=True)
    targets = []
    for item in ordered:
        proc = live(item)
        if proc is None:
            continue
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            continue
        targets.append(item)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline and any(live(t) for t in targets):
        time.sleep(0.05)
    for item in targets:
        proc = live(item)
        if proc is not None:
            try:
                proc.kill()
            except psutil.NoSuchProcess:
                pass
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline and any(live(t) for t in targets):
        time.sleep(0.05)
    return [t for t in targets if live(t) is not None]


def stop_tree(root: ProcIdent, extra: list[ProcIdent] = (), *, grace_s: float = 5.0) -> list[ProcIdent]:
    """Stop ``root`` and every descendant (now, plus ``extra`` seen earlier)."""
    members = [root, *descendants(root.pid), *extra]
    return stop_idents(members, grace_s=grace_s)


# -- Windows: kill-on-close Job Object ----------------------------------------


def assign_kill_on_close_job():
    """Put THIS process in a new kill-on-close Job Object; return its handle.

    The caller must keep the returned handle alive for the whole run. Returns
    None on a non-Windows platform. Raises :class:`ProcTreeError` when Windows
    refuses (a runner without the job could leave orphans, so it must not
    dispatch).
    """
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise ProcTreeError(f"CreateJobObject failed (error {ctypes.get_last_error()})")
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info))
    if not ok:
        raise ProcTreeError(f"SetInformationJobObject failed (error {ctypes.get_last_error()})")
    if not kernel32.AssignProcessToJobObject(handle, kernel32.GetCurrentProcess()):
        raise ProcTreeError(f"AssignProcessToJobObject failed (error {ctypes.get_last_error()})")
    return handle
