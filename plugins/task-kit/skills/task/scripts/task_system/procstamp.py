"""An exact process stamp: which process a recorded PID names, with no tolerance window.

A PID is reused, so a recorded process is identified by the PID plus a stamp the operating system
fixes when the process starts. The dashboard's own ``proctree.ProcIdent`` compares creation times
within 1.0 s, which is too loose to prove that a recorded session process is the same process; this
module compares exactly:

- macOS and Windows: the kernel records the start time and psutil returns it unchanged, so the stamp
  is that creation time. Two reads of one process are identical, and the 1 ms window only absorbs the
  float round-trip through a text file.
- Linux: psutil derives the creation time from the boot time, which moves when the clock is stepped.
  The stamp is the start tick count (``/proc/<pid>/stat`` field 22) plus the kernel boot id
  (``/proc/sys/kernel/random/boot_id``); both must be equal.

A stamp that cannot be read is an error, never "not running": an unreadable process table must not
read as every record's process having died. A process that is gone, or is a zombie, is not running.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import psutil

CREATED_ROUND_TRIP_S = 0.001
PROC_ROOT = Path("/proc")
BOOT_ID_RELATIVE = Path("sys/kernel/random/boot_id")


class StampError(RuntimeError):
    """The platform would not give a process stamp that is needed."""


@dataclass(frozen=True)
class Stamp:
    pid: int
    created: float | None = None  # macOS and Windows
    boot_id: str | None = None  # Linux
    start_ticks: int | None = None  # Linux

    def to_dict(self) -> dict:
        """The stamp as record fields (keys in a fixed order, absent platform fields omitted)."""
        data: dict = {"pid": self.pid}
        if self.created is not None:
            data["created"] = self.created
        if self.boot_id is not None:
            data["boot_id"] = self.boot_id
            data["start_ticks"] = self.start_ticks
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Stamp":
        if not isinstance(data, dict) or isinstance(data.get("pid"), bool) or not isinstance(data.get("pid"), int):
            raise StampError("a process stamp needs an integer pid")
        extra = sorted(set(data) - {"pid", "created", "boot_id", "start_ticks"})
        if extra:
            raise StampError(f"a process stamp has unknown fields {extra}")
        created = data.get("created")
        boot_id = data.get("boot_id")
        ticks = data.get("start_ticks")
        if created is not None and (isinstance(created, bool) or not isinstance(created, (int, float))):
            raise StampError("a process stamp's created must be a number")
        if (boot_id is None) != (ticks is None):
            raise StampError("a Linux process stamp needs boot_id and start_ticks together")
        if boot_id is not None and (not isinstance(boot_id, str) or isinstance(ticks, bool) or not isinstance(ticks, int)):
            raise StampError("a Linux process stamp needs a string boot_id and an integer start_ticks")
        if created is None and boot_id is None:
            raise StampError("a process stamp needs created, or boot_id with start_ticks")
        return cls(int(data["pid"]), None if created is None else float(created), boot_id, ticks)


def _is_linux(platform: str) -> bool:
    return platform.startswith("linux")


def _read_linux_stat(pid: int, proc_root: Path) -> tuple[str, int]:
    """(state letter, start ticks) from ``<proc_root>/<pid>/stat``; FileNotFoundError when the process is gone.

    The command name (field 2) is parenthesised and may itself contain spaces and parentheses, so the
    fields after it are found from the LAST closing parenthesis.
    """
    text = (proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    close = text.rfind(")")
    if close < 0:
        raise StampError(f"{proc_root / str(pid) / 'stat'} is not a procfs stat record")
    fields = text[close + 1:].split()
    # fields[0] is field 3 (state); field 22 (starttime) is fields[19]
    if len(fields) < 20:
        raise StampError(f"{proc_root / str(pid) / 'stat'} has too few fields")
    try:
        return fields[0], int(fields[19])
    except ValueError as exc:
        raise StampError(f"{proc_root / str(pid) / 'stat'} has a non-numeric start time") from exc


def _read_boot_id(proc_root: Path) -> str:
    path = proc_root / BOOT_ID_RELATIVE
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise StampError(f"cannot read the kernel boot id {path}: {exc}") from exc
    if not value:
        raise StampError(f"the kernel boot id {path} is empty")
    return value


def stamp_of(pid: int, *, platform: str | None = None, proc_root: Path | None = None) -> Stamp | None:
    """The stamp of the running process ``pid``, or None when no such process exists.

    ``platform`` and ``proc_root`` exist so a test can compose the Linux path on any host.
    """
    platform = sys.platform if platform is None else platform
    if _is_linux(platform):
        root = PROC_ROOT if proc_root is None else Path(proc_root)
        try:
            _state, ticks = _read_linux_stat(pid, root)
        except (FileNotFoundError, ProcessLookupError):
            return None
        except PermissionError as exc:
            raise StampError(f"cannot read the stat record of process {pid}: {exc}") from exc
        return Stamp(pid, None, _read_boot_id(root), ticks)
    try:
        return Stamp(pid, psutil.Process(pid).create_time())
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied as exc:
        raise StampError(f"cannot read the creation time of process {pid}: {exc}") from exc


def same_process(recorded: Stamp, current: Stamp, *, platform: str | None = None) -> bool:
    """Whether two stamps name the same process: exact on Linux, float round-trip only elsewhere."""
    if recorded.pid != current.pid:
        return False
    platform = sys.platform if platform is None else platform
    if _is_linux(platform):
        return (recorded.boot_id is not None and recorded.boot_id == current.boot_id
                and recorded.start_ticks is not None and recorded.start_ticks == current.start_ticks)
    if recorded.created is None or current.created is None:
        return False
    return abs(recorded.created - current.created) <= CREATED_ROUND_TRIP_S


def is_running(recorded: Stamp, *, platform: str | None = None, proc_root: Path | None = None) -> bool:
    """Whether the process ``recorded`` names is running now (a zombie is not)."""
    platform = sys.platform if platform is None else platform
    current = stamp_of(recorded.pid, platform=platform, proc_root=proc_root)
    if current is None or not same_process(recorded, current, platform=platform):
        return False
    if _is_linux(platform):
        root = PROC_ROOT if proc_root is None else Path(proc_root)
        try:
            state, _ticks = _read_linux_stat(recorded.pid, root)
        except FileNotFoundError:
            return False
        return state not in ("Z", "X")
    try:
        return psutil.Process(recorded.pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
