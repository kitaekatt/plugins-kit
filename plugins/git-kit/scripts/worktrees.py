"""Opt-in task worktrees, using bootstrap's effective layered manifest."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


POOL_FOLDER = "ready"  # <root>/<repo>/ready holds the shared pool (worktree_pool.py).


@dataclass(frozen=True)
class PoolSettings:
    size: int
    setup: list[list[str]]
    rebuild_when_changed: list[str]
    after_take_remove: list[str] = ()
    after_take: list[list[str]] = ()


@dataclass(frozen=True)
class WorktreeSettings:
    enabled: bool
    root: Path
    pool: PoolSettings | None = None


def parse_pool(raw: Any) -> PoolSettings | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("git_kit.worktrees.pool must be an object")
    if isinstance(raw.get("setup"), list) and raw["setup"] and isinstance(raw["setup"][0], dict):
        from worktree_config import Pool
        config = Pool.model_validate(raw)
        return PoolSettings(config.size, [step.argv for step in config.setup], list(config.rerun_files))
    unknown = set(raw) - {"size", "setup", "rebuild_when_changed", "after_take"}
    if unknown:
        raise ValueError(f"Unknown git_kit.worktrees.pool keys: {sorted(unknown)}")
    size = raw.get("size", 2)
    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        raise ValueError("git_kit.worktrees.pool.size must be an integer of 1 or more")
    setup = raw.get("setup", [["bootstrap", "run"]])
    if not setup or not _is_steps(setup):
        raise ValueError("git_kit.worktrees.pool.setup must be a nonempty list of command arrays")
    after = raw.get("after_take", {})
    if not isinstance(after, dict) or set(after) - {"remove", "steps"}:
        raise ValueError("git_kit.worktrees.pool.after_take must be an object with remove and steps")
    remove = after.get("remove", [])
    if not isinstance(remove, list) or not all(
            isinstance(p, str) and p and not Path(p).is_absolute() and ".." not in Path(p).parts for p in remove):
        raise ValueError("git_kit.worktrees.pool.after_take.remove must be a list of paths inside the worktree")
    after_steps = after.get("steps", [])
    if not _is_steps(after_steps):
        raise ValueError("git_kit.worktrees.pool.after_take.steps must be a list of command arrays")
    rebuild = raw.get("rebuild_when_changed", [])
    if not isinstance(rebuild, list) or not all(isinstance(p, str) and p for p in rebuild):
        raise ValueError("git_kit.worktrees.pool.rebuild_when_changed must be a list of paths")
    return PoolSettings(size, setup, rebuild, remove, after_steps)


def _is_steps(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(step, list) and step and all(isinstance(a, str) and a for a in step) for step in value)


def resolve_settings(project_dir: Path | None, *, include_pool: bool = True) -> WorktreeSettings:
    """Reuse bootstrap's profile-aware loader, including both local layers."""
    from bootstrap_lib.engine import _load_layered_manifests_ex

    if project_dir is not None:
        project_dir = Path(project_dir)
        if project_dir.is_dir():
            project_dir = current_project(project_dir)
    manifest, errors, profile = _load_layered_manifests_ex(project_dir)
    if errors:
        raise ValueError(f"Cannot resolve worktree settings: {errors}")
    if profile.status in {"invalid", "unknown"}:
        raise ValueError(f"Cannot resolve worktree settings: profile {profile.status}: {profile.errors or profile.warnings}")
    section = (manifest or {}).get("git_kit", {})
    if not isinstance(section, dict):
        raise ValueError("git_kit must be an object")
    config = section.get("worktrees", {})
    if not isinstance(config, dict):
        raise ValueError("git_kit.worktrees must be an object")
    unknown = set(config) - {"enabled", "root", "pool", "disk_floor_percent", "after_take", "before_release"}
    if unknown:
        raise ValueError(f"Unknown git_kit.worktrees keys: {sorted(unknown)}")
    enabled = config.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("git_kit.worktrees.enabled must be a boolean")
    raw_root = config.get("root", "~/.worktrees")
    if not isinstance(raw_root, str) or not raw_root.strip():
        raise ValueError("git_kit.worktrees.root must be a nonempty absolute or ~/ path")
    # Path.expanduser uses USERPROFILE on Windows; bootstrap prefers HOME.
    home = Path(os.environ.get("HOME") or Path.home())
    root = home / raw_root[2:] if raw_root.startswith("~/") else Path(raw_root)
    if not root.is_absolute():
        raise ValueError("git_kit.worktrees.root must be absolute or start with ~/")
    return WorktreeSettings(enabled, root.resolve(), parse_pool(config.get("pool")) if include_pool else None)


def _expand_entry(entry: str, home: Path) -> Path:
    """An additionalDirectories entry as an absolute path ("~/" means home)."""
    path = home / entry[2:] if entry.startswith("~/") else Path(entry)
    return path.resolve() if path.is_absolute() else path


def ensure_permissions(root: Path) -> bool:
    """Append one absolute directory without replacing permissions or entries."""
    home = Path(os.environ.get("HOME") or Path.home())
    target = (home / ".claude/settings.json").resolve()
    data = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    if not isinstance(data, dict):
        raise ValueError(f"{target} must contain a JSON object")
    permissions = data.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        raise ValueError(f"{target}: permissions must be an object")
    entries = permissions.setdefault("additionalDirectories", [])
    if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
        raise ValueError(f"{target}: permissions.additionalDirectories must be a string array")
    root_created = not root.exists()
    root.mkdir(parents=True, exist_ok=True)
    # Claude Code expands a "~/" entry, so one naming the same directory covers root.
    if any(_expand_entry(entry, home) == root for entry in entries):
        return root_created
    entries.append(str(root))
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".git-kit-settings-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(data, indent=2) + "\n")
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    """Every Git failure is fatal, including fetch and status failures."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        encoding="utf-8", timeout=120, check=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", **(env or {})},
    )
    return result.stdout.rstrip("\n")


def records(repo: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for block in git(repo, "worktree", "list", "--porcelain", "-z").split("\0\0"):
        if block:
            entries.append(dict(field.partition(" ")[::2] for field in block.split("\0") if field))
    return entries


def project_root(repo: Path) -> Path:
    return Path(records(repo)[0]["worktree"]).resolve()


def current_project(directory: Path) -> Path:
    """Non-Git directories still have user/project bootstrap layers."""
    try:
        return project_root(directory)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 128 and "not a git repository" in exc.stderr:
            return directory
        raise


def remote_refs(repo: Path) -> set[str]:
    return set(git(repo, "for-each-ref", "--format=%(refname)", "refs/remotes").splitlines())


def refresh(repo: Path) -> None:
    remotes = git(repo, "remote").splitlines()
    if not remotes:
        raise ValueError(f"{repo}: no remote; a fresh upstream is required")
    for remote in remotes:
        # Classification covers all remote branches, even when this checkout
        # normally fetches only a narrow subset. The explicit mapping also
        # makes --prune retire refs excluded by that configured subset.
        git(repo, "fetch", "--prune", "--", remote,
            f"+refs/heads/*:refs/remotes/{remote}/*")


def unpublished(repo: Path, head: str) -> bool:
    return bool(git(repo, "rev-list", head, "--not", "--remotes"))


def upstream_ref(repo: Path, branch: str) -> str | None:
    """Read tracking intent independently of a remote's narrow fetch mapping."""
    try:
        raw = git(repo, "config", "--get-regexp", rf"^branch\.{re.escape(branch)}\.(remote|merge)$")
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1:
            return None  # No tracking configuration is an ordinary branch state.
        raise
    entries = [line.split(" ", 1) for line in raw.splitlines()]
    remote = [value for key, value in entries if key.endswith(".remote")]
    merge = [value for key, value in entries if key.endswith(".merge")]
    if len(remote) != 1 or len(merge) != 1 or not merge[0].startswith("refs/heads/"):
        raise ValueError(f"{repo}: branch {branch} needs one remote upstream branch")
    if remote[0] not in git(repo, "remote").splitlines():
        raise ValueError(f"{repo}: upstream remote for {branch} is not configured: {remote[0]}")
    return f"refs/remotes/{remote[0]}/{merge[0].removeprefix('refs/heads/')}"


def main_upstream(main: Path) -> str:
    """Use the approved upstream, or the remote default when none is configured."""
    from worktree_config import upstream_settings

    return upstream_settings(main)[0]


def default_upstream(main: Path) -> str:
    """Resolve the remote default independently of lifecycle configuration."""
    remotes = git(main, "remote").splitlines()
    remote = "origin" if "origin" in remotes else (remotes[0] if len(remotes) == 1 else None)
    if remote is None:
        raise ValueError(f"{main}: default branch needs origin or exactly one remote")
    prefix = f"refs/remotes/{remote}/"
    try:
        ref = git(main, "symbolic-ref", "--quiet", prefix + "HEAD")
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 1:
            raise
        # Fetching a remote does not necessarily create its local HEAD symref.
        advertised = git(main, "ls-remote", "--symref", "--", remote, "HEAD")
        heads = [line.removeprefix("ref: ").split("\t")[0]
                 for line in advertised.splitlines() if line.startswith("ref: ") and line.endswith("\tHEAD")]
        if len(heads) != 1 or not heads[0].startswith("refs/heads/"):
            raise ValueError(f"{main}: cannot determine remote {remote}'s default branch")
        ref = prefix + heads[0].removeprefix("refs/heads/")
    if not ref.startswith(prefix) or ref == prefix + "HEAD":
        raise ValueError(f"{main}: invalid remote {remote} default branch: {ref}")
    git(main, "check-ref-format", ref)
    return ref


def contained_task(root: Path, path: Path) -> Path:
    """Only the root/repo/task slot is managed; no links outside that root."""
    resolved = path.resolve()
    relative = resolved.relative_to(root)
    if len(relative.parts) != 2 or path.is_symlink() or path.parent.is_symlink():
        raise ValueError(f"Expected a worktree at {root}/<repo>/<task>: {path}")
    return resolved


def validate_task(repo: Path, task: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", task) or task.endswith("."):
        raise ValueError("task must be one portable path component: letters, digits, _, . or -")
    if task.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}:
        raise ValueError("task uses a reserved Windows filename")
    if task == POOL_FOLDER:
        raise ValueError(f"task name {POOL_FOLDER} is reserved for the worktree pool")
    git(repo, "check-ref-format", "--branch", task)


def new(repo: Path, settings: WorktreeSettings, task: str, source: str | None, *, no_setup: bool = False) -> dict[str, Any]:
    """Deprecated raw creation entry point; lifecycle creation belongs to maintenance."""
    raise ValueError("Raw new is unavailable; run configure --propose, then take --agent --session")


def worktree_status(task: Path) -> tuple[bool, bool]:
    """Report tracked/untracked changes separately from ignored local data."""
    dirty = bool(git(task, "status", "--porcelain", "--ignored=no", "--untracked-files=all"))
    ignored = bool(git(task, "ls-files", "--others", "--ignored", "--exclude-standard", "-z"))
    return dirty, ignored


def list_worktrees(settings: WorktreeSettings) -> list[dict[str, Any]]:
    """Compatibility reader for the current repository's lifecycle records."""
    from worktree_lifecycle import Lifecycle
    return Lifecycle(Path.cwd()).status()["records"]


def remove(repo: Path, settings: WorktreeSettings, path: Path) -> dict[str, str]:
    """Deprecated removal entry point; a delivery releases an exact holder claim."""
    raise ValueError("Raw remove is unavailable; use release --claim after full delivery, or migrate legacy worktrees")


def main() -> int:
    from worktree_cli import main as lifecycle_main
    return lifecycle_main(sys.argv[1:])


if __name__ == "__main__":
    from bootstrap_guard import reexec_under_plugin_venv

    reexec_under_plugin_venv("git-kit")
    raise SystemExit(main())
