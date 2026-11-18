"""presence-kit process sampling, shared by watch.py and wait.py.

Which processes belong to a watched job and used CPU since the previous sample:
`ps` plus `/proc/<pid>/cwd` (Linux) or `lsof` (macOS), or the Win32 API on native
Windows. A sampling error raises ProcessSampleError (never read as "no activity").
Stdlib only; the module reads and writes no file.
"""
import ctypes
import os
import re
import struct
import subprocess
from collections import namedtuple

# A process counts as working when it used at least this fraction of one core since the
# previous poll: an idle process still ticks a little, a build or a hook does not.
MIN_CPU_FRACTION = 0.01

Proc = namedtuple("Proc", "ppid cpu command")


class ProcessSampleError(RuntimeError):
    """The process table or a working directory could not be read."""


# --- process activity ----------------------------------------------------------------

_CPU_TIME = re.compile(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)")


def _parse_cpu(text):
    """Cumulative CPU seconds from ps: `[[DD-]HH:]MM:SS[.ss]` (macOS and Linux spellings)."""
    match = _CPU_TIME.fullmatch(text)
    if match is None:
        raise ProcessSampleError(f"unrecognized CPU time {text!r} in ps output")
    days, hours, minutes, seconds = match.groups()
    return int(days or 0) * 86400 + int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)


def _run_tool(argv, ok_codes=(0,)):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise ProcessSampleError(f"cannot run {argv[0]}: {exc}")
    if done.returncode not in ok_codes:
        raise ProcessSampleError(f"{argv[0]} exited {done.returncode}: {done.stderr.strip()}")
    return done.stdout


def _sample_procs():
    """pid -> Proc(ppid, cumulative CPU seconds, command) for every process."""
    if os.name == "nt":
        return _sample_procs_windows()
    table = {}
    for raw in _run_tool(["ps", "-A", "-o", "pid=,ppid=,time=,command="]).splitlines():
        fields = raw.split(None, 3)
        if len(fields) < 3:
            continue
        try:
            pid, ppid = int(fields[0]), int(fields[1])
        except ValueError:
            raise ProcessSampleError(f"unrecognized ps line {raw!r}")
        table[pid] = Proc(ppid, _parse_cpu(fields[2]), fields[3] if len(fields) > 3 else "")
    return table


def _sample_cwds(pids):
    """pid -> working directory, for the pids whose directory can be read (others are absent)."""
    if not pids:
        return {}
    if os.name == "nt":
        return _sample_cwds_windows(pids)
    if os.path.isdir("/proc/self"):
        found = {}
        for pid in pids:
            try:
                found[pid] = os.readlink(f"/proc/{pid}/cwd")
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue  # exited, or another user's process
        return found
    # lsof exits 1 when a pid is gone or unreadable; anything else is a real failure.
    text = _run_tool(["lsof", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(str(p) for p in sorted(pids))],
                     ok_codes=(0, 1))
    found, pid = {}, None
    for raw in text.splitlines():
        if raw.startswith("p"):
            pid = int(raw[1:])
        elif raw.startswith("n") and pid is not None:
            found[pid] = raw[1:]
    return found


# --- Windows process sampling ----------------------------------------------------------
# Git Bash's ps (MSYS) has no -A and sees no native process's CPU time or working
# directory, and the interpreter bootstrap provisions on Windows is native. So the
# Windows samplers call the Win32 API directly: a Toolhelp snapshot for pid, parent and
# image name, GetProcessTimes for CPU, and the target's PEB for its working directory.

_TH32CS_SNAPPROCESS = 0x2
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ = 0x0010
_ERROR_NO_MORE_FILES = 18
_PEB_PROCESS_PARAMETERS = 0x20  # offset in the 64-bit PEB
_PARAMETERS_CURRENT_DIRECTORY = 0x38  # offset in RTL_USER_PROCESS_PARAMETERS (a UNICODE_STRING)


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32),
                ("th32ProcessID", ctypes.c_uint32), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", ctypes.c_uint32), ("cntThreads", ctypes.c_uint32),
                ("th32ParentProcessID", ctypes.c_uint32), ("pcPriClassBase", ctypes.c_int32),
                ("dwFlags", ctypes.c_uint32), ("szExeFile", ctypes.c_wchar * 260)]


class _BasicInformation(ctypes.Structure):
    _fields_ = [("ExitStatus", ctypes.c_int32), ("PebBaseAddress", ctypes.c_void_p),
                ("AffinityMask", ctypes.c_size_t), ("BasePriority", ctypes.c_int32),
                ("UniqueProcessId", ctypes.c_size_t), ("InheritedFromUniqueProcessId", ctypes.c_size_t)]


def _win32():
    """(kernel32, ntdll, wintypes) with argument types set for 64-bit handles."""
    if struct.calcsize("P") != 8:
        raise ProcessSampleError("process sampling on Windows needs a 64-bit Python")
    import ctypes.wintypes as wt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    kernel.CreateToolhelp32Snapshot.restype = wt.HANDLE
    kernel.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    kernel.Process32FirstW.argtypes = [wt.HANDLE, ctypes.c_void_p]
    kernel.Process32NextW.argtypes = [wt.HANDLE, ctypes.c_void_p]
    kernel.OpenProcess.restype = wt.HANDLE
    kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    kernel.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
    kernel.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                         ctypes.POINTER(ctypes.c_size_t)]
    ntdll.NtQueryInformationProcess.argtypes = [wt.HANDLE, wt.ULONG, ctypes.c_void_p, wt.ULONG,
                                                ctypes.POINTER(wt.ULONG)]
    return kernel, ntdll, wt


def _filetime_seconds(ft):
    return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7


def _sample_procs_windows():
    """pid -> Proc for every process. A process whose times cannot be opened (another
    user's, protected) keeps its place in the tree with CPU 0.0, so it never counts as busy."""
    kernel, _, wt = _win32()
    snapshot = kernel.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, wt.HANDLE(-1).value):
        raise ProcessSampleError(f"CreateToolhelp32Snapshot failed: error {ctypes.get_last_error()}")
    table = {}
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        ctypes.set_last_error(0)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            cpu = 0.0
            handle = kernel.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, entry.th32ProcessID)
            if handle:
                try:
                    created, exited, kernel_time, user_time = (wt.FILETIME() for _ in range(4))
                    if kernel.GetProcessTimes(handle, created, exited, kernel_time, user_time):
                        cpu = _filetime_seconds(kernel_time) + _filetime_seconds(user_time)
                finally:
                    kernel.CloseHandle(handle)
            table[entry.th32ProcessID] = Proc(entry.th32ParentProcessID, cpu, entry.szExeFile)
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() not in (0, _ERROR_NO_MORE_FILES):
            raise ProcessSampleError(f"Toolhelp process walk failed: error {ctypes.get_last_error()}")
    finally:
        kernel.CloseHandle(snapshot)
    return table


def _read_memory(kernel, handle, address, size):
    buffer = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    ok = kernel.ReadProcessMemory(handle, ctypes.c_void_p(address), buffer, size, ctypes.byref(got))
    return buffer.raw if ok and got.value == size else None


def _cwd_of_windows(kernel, ntdll, pid):
    """The working directory of one process, or None when it cannot be read."""
    handle = kernel.OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, pid)
    if not handle:
        return None  # exited, or another user's process
    try:
        info = _BasicInformation()
        if ntdll.NtQueryInformationProcess(handle, 0, ctypes.byref(info), ctypes.sizeof(info), None) != 0 \
                or not info.PebBaseAddress:
            return None
        raw = _read_memory(kernel, handle, info.PebBaseAddress + _PEB_PROCESS_PARAMETERS, 8)
        if raw is None:
            return None
        parameters = struct.unpack("<Q", raw)[0]
        raw = _read_memory(kernel, handle, parameters + _PARAMETERS_CURRENT_DIRECTORY, 16)
        if raw is None:
            return None
        length, _, buffer = struct.unpack("<HHxxxxQ", raw)  # UNICODE_STRING: Length, MaximumLength, Buffer
        raw = _read_memory(kernel, handle, buffer, length)
        return None if raw is None else raw.decode("utf-16-le")
    finally:
        kernel.CloseHandle(handle)


def _sample_cwds_windows(pids):
    """pid -> working directory for the pids whose directory can be read (others are absent)."""
    kernel, ntdll, _ = _win32()
    found = {}
    for pid in pids:
        cwd = _cwd_of_windows(kernel, ntdll, pid)
        if cwd:
            found[pid] = cwd
    return found


def _ancestors(table, pid):
    """pid and every ancestor reachable in the table."""
    seen = set()
    while pid in table and pid not in seen:
        seen.add(pid)
        pid = table[pid].ppid
    seen.add(pid)
    return seen


def _descendants(table, roots):
    """The given pids and every process below them."""
    below = set(roots)
    grew = True
    while grew:
        grew = False
        for pid, proc in table.items():
            if pid not in below and proc.ppid in below:
                below.add(pid)
                grew = True
    return below


def _within(path, roots):
    path = os.path.normcase(path)
    return any(path == root or path.startswith(root.rstrip(os.sep) + os.sep)
               for root in (os.path.normcase(r) for r in roots))


def _working(prev, table, elapsed, own, roots, pids, cwds):
    """Processes that used CPU since `prev` (pid -> CPU seconds) and belong to the watched job.

    A process absent from `prev` (first sighting, or a reused pid whose counter went
    backwards) is a baseline. Returns [(pid, command)].
    """
    threshold = MIN_CPU_FRACTION * max(elapsed, 0.0)
    busy = [pid for pid, proc in table.items()
            if pid not in own and pid in prev and proc.cpu - prev[pid] > threshold]
    below = _descendants(table, pids) if pids else set()
    unknown = [pid for pid in busy if pid not in below]
    where = cwds(unknown) if roots and unknown else {}
    return [(pid, table[pid].command) for pid in sorted(busy)
            if pid in below or (pid in where and _within(os.path.realpath(where[pid]), roots))]
