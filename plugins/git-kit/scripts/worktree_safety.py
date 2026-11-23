"""Conservative evidence for retirement. Unknown process evidence blocks it."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any

import worktrees as wt
from worktree_extensions import pending_blockers
from worktree_config import rebuildable_matches, relative_path
from worktree_paths import registration_matches


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


def process_info(pid: int) -> dict[str, Any]:
    """Read parent and executable evidence without trusting a caller's argv."""
    if os.name == "nt":
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
            f"Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' | Select-Object ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, check=True, timeout=15)
        row = json.loads(result.stdout)
        if not isinstance(row, dict):
            raise ValueError(f"Cannot inspect process {pid}")
        return {"ppid": int(row["ParentProcessId"]), "executable": row["Name"],
                "command": row.get("CommandLine") or ""}
    names = subprocess.run(["ps", "-p", str(pid), "-o", "ppid=,comm="],
                           capture_output=True, text=True, check=True, timeout=15)
    command = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                             capture_output=True, text=True, check=True, timeout=15)
    parent, executable = names.stdout.strip().split(None, 1)
    return {"ppid": int(parent), "executable": executable, "command": command.stdout.strip()}


def holder_identity(explicit: dict[str, Any] | None = None) -> dict[str, Any]:
    """Prefer the nearest Claude session ancestor; require an explicit fallback."""
    from worktree_migration import claude_command

    pid = os.getppid()
    seen: set[int] = set()
    while pid > 0 and pid not in seen:
        seen.add(pid)
        try:
            identity = process_identity(pid)
            info = process_info(pid)
            if claude_command(info["executable"], info["command"]):
                if process_state(identity) != "live":
                    raise ValueError("Claude holder changed during process inspection")
                return {**identity, "executable": info["executable"], "command": info["command"]}
            pid = info["ppid"]
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
            break  # An incomplete chain cannot establish an automatic holder.
    if explicit is None:
        raise ValueError("No live Claude session ancestor found; provide --holder-pid <pid> explicitly")
    if process_state(explicit) != "live":
        raise ValueError("Holder process identity must be live and certain")
    info = process_info(explicit["pid"])
    if process_state(explicit) != "live":
        raise ValueError("Holder changed during process inspection")
    return {**explicit, "executable": info["executable"], "command": info["command"]}


def caller_chain(holder: dict[str, Any]) -> list[dict[str, Any]]:
    """Identities from this process's parent up to and including the holder.

    Each one waits for this process to return, so a cwd or open file it keeps
    in the worktree is not use that this process can break. A chain that does
    not reach the recorded holder (same PID and start) gives no identities.
    """
    chain: list[dict[str, Any]] = []
    pid = os.getppid()
    seen: set[int] = set()
    while pid > 0 and pid not in seen:
        seen.add(pid)
        identity = process_identity(pid)
        chain.append(identity)
        if pid == holder.get("pid") and identity["start"] == holder.get("start"):
            return chain
        pid = process_info(pid)["ppid"]
    return []


def child_processes(parents: Collection[int]) -> list[dict[str, Any]]:
    """Start identities of the direct children of ``parents``."""
    if os.name == "nt":
        # Windows directory-use inspection is uncertain and blocks release anyway.
        return []
    listing = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, check=True, timeout=15)
    children = []
    for line in listing.stdout.splitlines():
        pid, ppid = (int(value) for value in line.split())
        if ppid in parents:
            try:
                children.append(process_identity(pid))
            except ValueError:
                continue  # Exited after the listing; an unknown process is never excluded.
    return children


def release_callers(holder: dict[str, Any]) -> list[dict[str, Any]]:
    """Processes whose use of the worktree does not block a release.

    The caller chain (``caller_chain``) plus the direct children of each chain
    member except the holder: the other processes of the same shell command,
    such as ``grep`` in ``release ... | grep``. Children of the holder (other
    shell commands of the session, background jobs) are not included.
    """
    chain = caller_chain(holder)
    if not chain:
        return []
    known = {caller["pid"] for caller in chain}
    siblings = child_processes({caller["pid"] for caller in chain[:-1]})
    return [*chain, *(child for child in siblings if child["pid"] not in known)]


def holder_state(identity: dict[str, Any]) -> str:
    """Only proven Claude-session death can end a claim automatically."""
    from worktree_migration import claude_command, claude_processes

    state = process_state(identity)
    if state != "dead":
        return state
    if not claude_command(identity.get("executable", ""), identity.get("command", "")):
        return "uncertain"  # Legacy shell holders provide no session-death evidence.
    session = identity.get("session")
    if not session:
        return "dead"
    try:
        processes = claude_processes()
        if any(session in row["command"] for row in processes):
            return "live"
        # An argv that omits the ID cannot prove a Claude process belongs to
        # another session. Keep ownership until all possible holders end.
        if processes:
            return "uncertain"
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return "uncertain"
    return "dead"


def process_cwd(pid: int) -> str | None:
    """Report a process cwd; directory-use inspection still owns safety evidence."""
    if sys.platform.startswith("linux"):
        try:
            return str((Path("/proc") / str(pid) / "cwd").resolve(strict=True))
        except (FileNotFoundError, PermissionError):
            return None
    if sys.platform == "darwin" and (executable := shutil.which("lsof")):
        result = subprocess.run([executable, "-nP", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                                capture_output=True, text=True, check=False, timeout=30)
        if not result.stderr.strip() and result.returncode == 0:
            return next((line[1:] for line in result.stdout.splitlines() if line.startswith("n")), None)
    return None


def path_processes(path: Path, *, pids: set[int] | None = None, exclude: Collection[int] = ()) -> list[str]:
    """Include cwd and open runtime files, including processes absent from records.

    ``exclude`` names processes whose use of the path is known not to block:
    release passes its callers (``release_callers``). The inspecting process
    itself never blocks, and the scanner runs from the filesystem root, so a
    caller whose cwd is inside the path cannot make its own scan report it.
    Other descendants of the caller still block, for example a server that a
    before_release script left running in the worktree.
    """
    exclude = {os.getpid(), *exclude}
    if sys.platform.startswith("linux"):
        blockers = []
        for process in Path("/proc").iterdir():
            if not process.name.isdigit():
                continue
            if (pids is not None and int(process.name) not in pids) or int(process.name) in exclude:
                continue
            try:
                if pids is None and process.stat().st_uid != os.getuid():
                    continue  # Other users' processes are outside our inspection scope.
                commandline = (process / "cmdline").read_bytes().decode("utf-8", errors="replace")
                if pids is None and str(path) in commandline:
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
        selection = ["-a", "-p", ",".join(str(pid) for pid in sorted(pids))] if pids is not None else []
        # lsof and the helper it forks inherit this cwd; keep it outside every worktree.
        result = subprocess.run([executable, "-nP", "-Fpn", *selection, "+D", str(path)], cwd=os.sep,
                                capture_output=True, text=True, check=False, timeout=30)
        if result.stderr.strip() or result.returncode not in (0, 1):
            return [f"uncertain process evidence: {result.stderr.strip() or result.returncode}"]
        # -F output: a "p<pid>" line, then that process's field lines.
        kept, owner = [], None
        for line in result.stdout.strip().splitlines():
            if line.startswith("p"):
                owner = int(line[1:])
            if owner not in exclude:
                kept.append(line)
        blockers = [f"live process/runtime: {chr(10).join(kept)}"] if kept else []
        if pids is not None:
            return blockers
        skipped = {str(pid) for pid in exclude}
        processes = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=True, timeout=15)
        for line in processes.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[0] not in skipped and str(path) in parts[1]:
                blockers.append(f"live process runtime {parts[0]}: {parts[1]}")
        return blockers
    return ["uncertain process/runtime evidence: directory-use inspection is unavailable on this platform"]


def outputs(path: Path, declared: list[str], rebuildable: Sequence[str] = ()) -> tuple[list[str], list[str]]:
    ignored = [p for p in wt.git(path, "ls-files", "--others", "--ignored", "--exclude-standard", "-z").split("\0") if p]
    tracked = rebuildable_matches(path, rebuildable, tracked=True)
    if tracked:
        raise ValueError(f"rebuildable glob matches tracked data: {sorted(tracked)}")
    matched = rebuildable_matches(path, rebuildable)
    reproducible, unique = [], []
    for name in ignored:
        (reproducible if name in matched or any(Path(name) == Path(p) or Path(name).is_relative_to(p) for p in declared) else unique).append(name)
    return reproducible, unique


def output_path(root: Path, name: Path, *, resolve_target: bool = True) -> Path:
    """Refuse linked parents; contain selected links by their parent for unlink."""
    target = root / name
    if any(parent.is_symlink() or parent.is_junction() for parent in target.parents if parent.is_relative_to(root)):
        raise ValueError(f"Output path traverses a symlink or junction: {target}")
    if resolve_target:
        linked = target.is_symlink()
        resolved = target.parent.resolve() if linked else target.resolve()
        if (not linked and resolved == root) or not resolved.is_relative_to(root):
            raise ValueError(f"Output path escapes the worktree: {target}")
    return target


def remove_directory(target: Path) -> None:
    """One native recursive delete per directory; propagate every failure."""
    if sys.platform == "win32":
        # Expand a quoted environment value, never interpolate a path as CMD code.
        argv = 'cmd.exe /d /v:off /c rmdir /s /q "%GIT_KIT_OUTPUT_PATH%"'
        env = {**os.environ, "GIT_KIT_OUTPUT_PATH": str(target)}
    else:
        argv, env = ["rm", "-rf", "--", str(target)], None
    subprocess.run(argv, env=env, capture_output=True, text=True, check=True)


def remove_outputs(path: Path, declared: list[str]) -> None:
    """Collapse fully approved ignored directories, then delete remaining files."""
    if not declared:
        return
    if any(parent.is_symlink() or parent.is_junction() for parent in (path, *path.parents)):
        raise ValueError(f"Worktree path traverses a symlink or junction: {path}")
    root = path.resolve(strict=True)
    approved = {Path(relative_path(name)) for name in declared}

    def covered(name: Path) -> bool:
        return any(parent in approved for parent in (name, *name.parents))

    candidates: set[Path] = set()
    for name in approved:
        # Nested links are unlinked by the native directory delete. Resolve only
        # the selected deletion roots after collapsing fully approved outputs.
        output_path(root, name, resolve_target=False)
        candidates.update(parent for parent in (name, *name.parents) if parent != Path("."))
    # Three batched Git queries replace two subprocesses per output file.
    protected = wt.git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    blocked: set[Path] = set()
    for name in filter(None, protected.split("\0")):
        relative = Path(name)
        if covered(relative):
            raise ValueError(f"Reproducible output contains tracked or unignored data: {root / relative}")
        blocked.update((relative, *relative.parents))
    ignored = wt.git(root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    for name in filter(None, ignored.split("\0")):
        relative = Path(name)
        if not covered(relative):
            blocked.update((relative, *relative.parents))
    result = subprocess.run(["git", "-C", str(root), "check-ignore", "--stdin", "-z"],
                            input="\0".join(p.as_posix() for p in candidates) + "\0",
                            capture_output=True, text=True, check=False)
    if result.returncode not in (0, 1):
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
    matched = {Path(name) for name in result.stdout.split("\0") if name}
    for name in approved:
        target = root / name
        if (target.is_symlink() or target.exists()) and name not in matched:
            raise ValueError(f"Reproducible output is not ignored: {target}")
    selected: set[Path] = set()
    for name in sorted(matched - blocked, key=lambda p: (len(p.parts), p.as_posix())):
        if not any(parent in selected for parent in name.parents):
            selected.add(name)
    # Validate again at deletion, after collapsing; directories precede single files.
    targets = [output_path(root, name) for name in selected]
    for target in sorted(targets, key=lambda p: (p.is_symlink() or not p.is_dir(), str(p))):
        output_path(root, target.relative_to(root))
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            remove_directory(target)


def declared_process_blockers(record: dict[str, Any]) -> list[str]:
    blockers = []
    for process in record.get("processes", []):
        state = process_state(process)
        if state != "dead":
            blockers.append(f"{state} declared process: {process}")
    return [*blockers, *pending_blockers(record)]


def inspect(record: dict[str, Any], upstream: str, declared: list[str], rebuildable: Sequence[str] = (),
            callers: Sequence[dict[str, Any]] = ()) -> dict[str, Any]:
    """``callers`` (from ``release_callers``) do not block; a caller that changed does."""
    path = Path(record["path"])
    blockers = []
    if path.is_symlink():
        blockers.append("worktree path is a symlink")
    registrations = wt.records(path)
    if registration_matches(registrations[0], path):
        blockers.append("main checkout cannot be retired")
    registration = next((r for r in registrations if registration_matches(r, path)), None)
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
    reproducible, unique = outputs(path, declared, rebuildable)
    if unique:
        blockers.append(f"unique ignored data must be preserved: {unique}")
    blockers.extend(declared_process_blockers(record))
    blockers.extend(path_processes(path, exclude={caller["pid"] for caller in callers}))
    # A PID can be reused only after its process exits; prove each one is unchanged.
    blockers.extend(f"caller process changed during inspection: {caller}"
                    for caller in callers if process_state(caller) != "live")
    return {"safe": not blockers, "at": time.time(), "upstream": upstream,
            "head": wt.git(path, "rev-parse", "HEAD"), "blockers": blockers,
            "reproducible": reproducible, "unique_ignored": unique,
            "excluded_callers": [caller["pid"] for caller in callers]}
