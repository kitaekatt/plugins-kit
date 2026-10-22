"""Which machine this is: a host id for file names, a fingerprint for ownership, and the OS flavor.

- **Host id**: the short host name, lowercased (the file system may be case-insensitive), with ``-wsl``
  appended under WSL unless the name already ends in ``wsl`` (a WSL instance defaults to its Windows
  host's name but has a separate process table). ``override`` (the ``sessions.host`` setting) replaces
  it to resolve a real name clash between two machines.
- **Fingerprint**: the first 16 hex digits of SHA-256 over ``task-kit-machine-v1:`` plus the operating
  system's machine id (macOS ``IOPlatformUUID``, Linux and WSL ``/etc/machine-id``, Windows
  ``MachineGuid``). The id is hashed so no hardware identifier is ever written to a file. A machine id
  that cannot be read is an error: a machine that cannot prove which machine it is must not claim a file.
- **OS instance**: ``macos``, ``linux``, ``linux-wsl`` or ``windows``; the process table the machine's
  records are verified against.
"""

from __future__ import annotations

import hashlib
import platform as platform_module
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import resolve

FINGERPRINT_SALT = "task-kit-machine-v1:"
FINGERPRINT_LENGTH = 16
LINUX_ID_PATHS = (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id"))
HOST_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
IOREG_TIMEOUT_S = 10
OS_MACOS, OS_LINUX, OS_LINUX_WSL, OS_WINDOWS = "macos", "linux", "linux-wsl", "windows"


class MachineError(RuntimeError):
    """The machine cannot be identified, so nothing may be recorded under its name."""


@dataclass(frozen=True)
class Machine:
    host: str
    fingerprint: str
    os_instance: str


def os_instance(*, platform: str | None = None, release: str | None = None) -> str:
    """``macos``, ``linux``, ``linux-wsl`` or ``windows``; WSL is a Linux kernel release naming Microsoft."""
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        return OS_MACOS
    if platform in ("win32", "cygwin", "msys"):
        return OS_WINDOWS
    if platform.startswith("linux"):
        release = platform_module.release() if release is None else release
        return OS_LINUX_WSL if "microsoft" in release.lower() else OS_LINUX
    raise MachineError(f"task-kit sessions do not support the platform {platform!r}")


def host_id(*, override: str | None = None, hostname: str | None = None, instance: str | None = None) -> str:
    """The host id: the file name this machine writes and the name a reader sees."""
    if override:
        name, source = override.strip().lower(), "sessions.host"
    else:
        name = (resolve.short_hostname() if hostname is None else hostname.split(".")[0]).strip().lower()
        if (os_instance() if instance is None else instance) == OS_LINUX_WSL and not name.endswith("wsl"):
            name += "-wsl"
        source = "the host name"
    if not HOST_ID_PATTERN.match(name):
        raise MachineError(
            f"{source} {name!r} cannot be a session file name (lowercase letters, digits, '.', '_' and '-' only, "
            "starting with a letter or digit); set sessions.host to a valid name")
    return name


def _ioreg_uuid() -> str:
    try:
        completed = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, text=True,
                                   timeout=IOREG_TIMEOUT_S, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MachineError(f"cannot read the macOS machine id (ioreg): {exc}") from exc
    match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', completed.stdout)
    if not match:
        raise MachineError("ioreg did not report an IOPlatformUUID")
    return match.group(1)


def _linux_machine_id(paths) -> str:
    errors = []
    for path in paths:
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            continue
        if value:
            return value
        errors.append(f"{path}: empty")
    raise MachineError("cannot read the machine id (" + "; ".join(errors) + ")")


def _windows_machine_guid() -> str:
    import winreg  # Windows only

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                            access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            value, _kind = winreg.QueryValueEx(key, "MachineGuid")
    except OSError as exc:
        raise MachineError(f"cannot read the Windows MachineGuid: {exc}") from exc
    if not value:
        raise MachineError("the Windows MachineGuid is empty")
    return str(value)


def machine_id(*, platform: str | None = None, linux_paths=LINUX_ID_PATHS,
               reader: Callable[[], str] | None = None) -> str:
    """The operating system's machine id. ``reader`` replaces the platform read (tests)."""
    if reader is not None:
        value = reader()
        if not value or not str(value).strip():
            raise MachineError("the machine id is empty")
        return str(value).strip()
    kind = os_instance(platform=platform)
    if kind == OS_MACOS:
        return _ioreg_uuid()
    if kind == OS_WINDOWS:
        return _windows_machine_guid()
    return _linux_machine_id(linux_paths)


def fingerprint(machine_id_value: str) -> str:
    digest = hashlib.sha256((FINGERPRINT_SALT + machine_id_value).encode("utf-8")).hexdigest()
    return digest[:FINGERPRINT_LENGTH]


def current(*, override: str | None = None, id_reader: Callable[[], str] | None = None) -> Machine:
    """This machine's host id, fingerprint and OS flavor; raises :class:`MachineError` when any is unknown."""
    instance = os_instance()
    return Machine(host_id(override=override, instance=instance), fingerprint(machine_id(reader=id_reader)), instance)
