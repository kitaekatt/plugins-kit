"""Defensive PATH repair for Windows subprocess reliability.

On Windows, when bash launches python.bat, the venv's activate.bat runs
`set "PATH=<venv>\\Scripts;%PATH%"`. If the inherited PATH is large
enough (commonly >8 KB after accumulated duplicates from many shell
sessions), cmd.exe silently overflows its variable size limit and
drops the rest of PATH. The Python child then inherits a stripped
PATH and `subprocess.run(["p4", ...])` etc. fails with
FileNotFoundError even when the tool is installed.

This module is the canonical source for PATH repair across plugins-kit.
It dedups the inherited PATH case-insensitively and merges in HKLM +
HKCU PATH entries read directly via winreg (no string-size limits,
unlike `reg query` or cmd.exe variables). Idempotent. No-op on
non-Windows.

Vendored copies live in consumer plugins (plugins/p4-kit/lib/,
plugins/unreal-kit/lib/) so each plugin can call it without depending
on bootstrap being importable. Keep the copies in sync with this file.
"""

import os
import sys
from dataclasses import dataclass


@dataclass
class PathRepairResult:
    before_entries: int
    after_entries: int
    deduped: int
    restored: int
    changed: bool


def repair_path() -> PathRepairResult:
    """Dedup the inherited PATH and merge in HKLM + HKCU registry entries.

    Mutates os.environ["PATH"] in place. Returns a summary so callers can
    decide whether to surface the change in their own diagnostics.
    """
    raw = os.environ.get("PATH", "")
    raw_entries = [p for p in raw.split(os.pathsep) if p]
    before_count = len(raw_entries)

    def _key(entry: str) -> str:
        # normpath drops a trailing separator: the registry often spells a
        # directory "C:\Program Files\dotnet\" where the inherited PATH has
        # it without the backslash, and comparing raw strings re-added
        # every such directory as a duplicate.
        return os.path.normpath(entry).lower()

    deduped: list[str] = []
    seen: set[str] = set()
    for entry in raw_entries:
        key = _key(entry)
        if key not in seen:
            seen.add(key)
            deduped.append(entry)
    deduped_count = before_count - len(deduped)

    restored = 0
    if sys.platform == "win32":
        try:
            import winreg
        except ImportError:
            winreg = None
        if winreg is not None:
            for hive, subkey in (
                (winreg.HKEY_LOCAL_MACHINE,
                 r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                (winreg.HKEY_CURRENT_USER, r"Environment"),
            ):
                try:
                    with winreg.OpenKey(hive, subkey) as k:
                        value, _ = winreg.QueryValueEx(k, "Path")
                except OSError:
                    continue
                for entry in value.split(os.pathsep):
                    if not entry:
                        continue
                    key = _key(entry)
                    if key not in seen:
                        seen.add(key)
                        deduped.append(entry)
                        restored += 1

    new_path = os.pathsep.join(deduped)
    changed = new_path != raw
    if changed:
        os.environ["PATH"] = new_path

    return PathRepairResult(
        before_entries=before_count,
        after_entries=len(deduped),
        deduped=deduped_count,
        restored=restored,
        changed=changed,
    )
