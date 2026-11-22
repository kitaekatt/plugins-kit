"""Conservative evidence for retirement. Unknown process evidence blocks it."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import worktrees as wt
from worktree_extensions import pending_blockers
from worktree_config import relative_path


def process_identity(pid: int) -> dict[str, Any]:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
        raise ValueError("process pid must be a positive integer")
    if os.name == "nt":
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                 f"$p=Get-Process -Id {pid} -ErrorAction SilentlyContinue; if ($null -eq $p) {{exit 3}}; $p.StartTime.ToUniversalTime().Ticks"],
                                capture_output=True, text=True, check=False, timeout=15)
    else:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                                capture_output=True, text=True, check=False, timeout=15)
    if os.name == "nt" and result.returncode == 3:
        raise ProcessLookupError(f"Process {pid} has exited")
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError(f"Cannot establish process start identity for pid {pid}: {result.stderr.strip()}")
    return {"pid": pid, "start": result.stdout.strip()}


def process_state(identity: dict[str, Any]) -> str:
    pid, start = identity.get("pid"), identity.get("start")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1 or not isinstance(start, str) or not start:
        return "uncertain"
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "dead"
        except PermissionError:
            return "uncertain"
    try:
        actual = process_identity(pid)
    except ProcessLookupError:
        return "dead"
    except (ValueError, OSError, subprocess.SubprocessError):
        return "uncertain"
    return "live" if actual["start"] == start else "dead"


def path_processes(path: Path) -> list[str]:
    """Include cwd and open runtime files, including processes absent from records."""
    if sys.platform.startswith("linux"):
        blockers = []
        for process in Path("/proc").iterdir():
            if not process.name.isdigit():
                continue
            try:
                if process.stat().st_uid != os.getuid():
                    continue  # Other users' processes are outside our inspection scope.
                commandline = (process / "cmdline").read_bytes().decode("utf-8", errors="replace")
                if str(path) in commandline:
                    blockers.append(f"live process runtime {process.name}: {commandline.replace(chr(0), ' ')}")
                targets = [process / "cwd", process / "exe", *(process / "fd").iterdir()]
                for target in targets:
                    try:
                        resolved = target.resolve(strict=True)
                    except FileNotFoundError:
                        continue  # Descriptor/process exited during the observation.
                    if resolved.is_relative_to(path):
                        blockers.append(f"live process {process.name}: {resolved}")
                        break
            except FileNotFoundError:
                continue
            except PermissionError as exc:
                blockers.append(f"uncertain process {process.name}: {exc}")
        return blockers
    if sys.platform == "darwin":
        executable = shutil.which("lsof")
        if not executable:
            return ["uncertain process evidence: lsof is unavailable"]
        result = subprocess.run([executable, "-nP", "-Fpn", "+D", str(path)],
                                capture_output=True, text=True, check=False, timeout=30)
        if result.stderr.strip() or result.returncode not in (0, 1):
            return [f"uncertain process evidence: {result.stderr.strip() or result.returncode}"]
        blockers = [f"live process/runtime: {result.stdout.strip()}"] if result.stdout.strip() else []
        processes = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=True, timeout=15)
        for line in processes.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[0] != str(os.getpid()) and str(path) in parts[1]:
                blockers.append(f"live process runtime {parts[0]}: {parts[1]}")
        return blockers
    return ["uncertain process/runtime evidence: directory-use inspection is unavailable on this platform"]


def outputs(path: Path, declared: list[str]) -> tuple[list[str], list[str]]:
    ignored = [p for p in wt.git(path, "ls-files", "--others", "--ignored", "--exclude-standard", "-z").split("\0") if p]
    reproducible, unique = [], []
    for name in ignored:
        (reproducible if any(Path(name) == Path(p) or Path(name).is_relative_to(p) for p in declared) else unique).append(name)
    return reproducible, unique


def remove_outputs(path: Path, declared: list[str]) -> None:
    """Delete only explicitly classified ignored output. Never use Git --force."""
    for name in declared:
        relative_path(name)
        target = path / name
        if not target.exists() and not target.is_symlink():
            continue
        if any(parent.is_symlink() for parent in target.relative_to(path).parents if str(parent) != "."
               for parent in [path / parent]):
            raise ValueError(f"Output path traverses a symlink: {target}")
        tracked = wt.git(path, "ls-files", "-z", "--", name)
        if tracked:
            raise ValueError(f"Reproducible output contains tracked data: {target}")
        result = subprocess.run(["git", "-C", str(path), "check-ignore", "-q", "--", name], capture_output=True, check=False)
        if result.returncode != 0:
            raise ValueError(f"Reproducible output is not ignored: {target}")
        if target.is_symlink() or target.is_file():
            target.unlink()
        else:
            shutil.rmtree(target)


def declared_process_blockers(record: dict[str, Any]) -> list[str]:
    blockers = []
    for process in record.get("processes", []):
        state = process_state(process)
        if state != "dead":
            blockers.append(f"{state} declared process: {process}")
    return [*blockers, *pending_blockers(record)]


def inspect(record: dict[str, Any], upstream: str, declared: list[str]) -> dict[str, Any]:
    path = Path(record["path"])
    blockers = []
    if path.is_symlink():
        blockers.append("worktree path is a symlink")
    if path.resolve() == wt.project_root(path):
        blockers.append("main checkout cannot be retired")
    registration = next((r for r in wt.records(path) if Path(r["worktree"]).resolve() == path.resolve()), None)
    if registration is None or "locked" in registration:
        blockers.append("missing or locked Git registration")
    if record.get("holder"):
        blockers.append("holder claim remains")
    dirty = wt.git(path, "status", "--porcelain=v1", "--untracked-files=all", "--ignored=no")
    if dirty:
        blockers.append("uncommitted files (including untracked)")
    cherry = wt.git(path, "cherry", upstream, "HEAD")
    if any(line.startswith("+") for line in cherry.splitlines()):
        blockers.append("commits outside fresh upstream")
    reproducible, unique = outputs(path, declared)
    if unique:
        blockers.append(f"unique ignored data must be preserved: {unique}")
    blockers.extend(declared_process_blockers(record))
    blockers.extend(path_processes(path))
    return {"safe": not blockers, "at": time.time(), "upstream": upstream,
            "head": wt.git(path, "rev-parse", "HEAD"), "blockers": blockers,
            "reproducible": reproducible, "unique_ignored": unique}
