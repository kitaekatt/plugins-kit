"""Network-filesystem heuristic shared by the SQLite stores. Standard library only."""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from typing import Union

_DRIVE_REMOTE = 4  # Windows GetDriveTypeW result for a mapped/UNC network drive


def looks_like_network_path(path: Union[str, Path]) -> bool:
    """Best-effort check whether ``path`` resolves to a network filesystem.

    False positives are tolerated (the caller only warns, never refuses) and
    false negatives are expected on filesystems this function does not know
    how to probe -- it is a heuristic, not a guarantee. Two checks:

    - **UNC form** -- a path starting with ``\\\\`` (or POSIX-style ``//``,
      the form ``pathlib`` normalizes a UNC path to on some platforms) is
      always treated as a network path, no OS call needed.
    - **Windows mapped drive** -- ``GetDriveTypeW`` via ``ctypes`` (stdlib,
      no ``pywin32`` dependency) reports ``DRIVE_REMOTE`` for a mapped
      network drive letter.

    POSIX network mounts (NFS, CIFS/SMB mounted under a local-looking path)
    are not detected -- there is no stdlib-portable way to ask "is this mount
    remote" without parsing ``/proc/mounts``, which is Linux-only and easy to
    get wrong. Under-detection there is accepted; the warning is a courtesy,
    not a safety net.
    """
    text = str(path)
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    if sys.platform == "win32":
        drive = Path(path).resolve().drive
        if drive:
            root = drive + "\\"
            try:
                drive_type = ctypes.windll.kernel32.GetDriveTypeW(root)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 -- best-effort probe, never fatal
                return False
            return drive_type == _DRIVE_REMOTE
    return False
