"""Opt-in task worktrees, using bootstrap's effective layered manifest."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WorktreeSettings:
    enabled: bool
    root: Path


def resolve_settings(project_dir: Path | None) -> WorktreeSettings:
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
    unknown = set(config) - {"enabled", "root"}
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
    return WorktreeSettings(enabled, root.resolve())


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
    if str(root) in entries:
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


def git(repo: Path, *args: str) -> str:
    """Every Git failure is fatal, including fetch and status failures."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        encoding="utf-8", timeout=120, check=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"},
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
    branch = git(main, "symbolic-ref", "--short", "HEAD")
    ref = upstream_ref(main, branch)
    if ref is None:
        raise ValueError(f"{main}: main checkout branch {branch} has no remote upstream")
    return ref


def contained_task(root: Path, path: Path) -> Path:
    """Only the root/repo/task slot is managed; no links outside that root."""
    resolved = path.resolve()
    relative = resolved.relative_to(root)
    if len(relative.parts) != 2 or path.is_symlink() or path.parent.is_symlink():
        raise ValueError(f"Expected a worktree at {root}/<repo>/<task>: {path}")
    return resolved


def new(repo: Path, settings: WorktreeSettings, task: str, source: str | None) -> dict[str, str]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", task) or task.endswith("."):
        raise ValueError("task must be one portable path component: letters, digits, _, . or -")
    if task.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}:
        raise ValueError("task uses a reserved Windows filename")
    git(repo, "check-ref-format", "--branch", task)
    main = project_root(repo)
    destination = contained_task(settings.root, settings.root / main.name / task)
    refresh(repo)
    refs = remote_refs(repo)
    if source is None:
        source = main_upstream(main)
    elif not source.startswith("refs/remotes/"):
        source = "refs/remotes/" + source
    if source not in refs or source.endswith("/HEAD"):
        raise ValueError(f"--from must name a fetched remote branch, such as origin/main: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "--no-track", "-b", task, str(destination), source)
    return {"path": str(destination), "branch": task, "from": source}


def list_worktrees(settings: WorktreeSettings) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not settings.root.exists():
        return result
    refreshed: set[Path] = set()
    for repo_dir in sorted(settings.root.iterdir()):
        if not repo_dir.is_dir() or repo_dir.is_symlink():
            raise ValueError(f"Unexpected entry under worktree root: {repo_dir}")
        for path in sorted(repo_dir.iterdir()):
            task = contained_task(settings.root, path)
            if not (task / ".git").is_file():
                raise ValueError(f"Unregistered leftover under worktree root: {task}")
            main = project_root(task)
            if main not in refreshed:
                refresh(task)
                refreshed.add(main)
            entry = next((r for r in records(task) if Path(r["worktree"]).resolve() == task), None)
            if entry is None:
                raise ValueError(f"Unregistered worktree: {task}")
            head = entry["HEAD"]
            branch = entry.get("branch", "")
            upstream = upstream_ref(task, branch.removeprefix("refs/heads/")) if branch else None
            refs = remote_refs(task)
            base = main_upstream(main)
            containing = set(git(task, "for-each-ref", f"--contains={head}", "--format=%(refname)", "refs/remotes").splitlines())
            merged = base in containing
            result.append({
                "path": str(task), "branch": branch.removeprefix("refs/heads/"),
                "no_unpublished_commits": not unpublished(task, head),
                "merged": merged, "gone": bool(upstream and upstream not in refs),
                "dirty": bool(git(task, "status", "--porcelain", "--ignored=matching", "--untracked-files=all")),
            })
    return result


def remove(repo: Path, settings: WorktreeSettings, path: Path) -> dict[str, str]:
    task = contained_task(settings.root, path)
    entry = next((r for r in records(repo) if Path(r["worktree"]).resolve() == task), None)
    if entry is None or task == project_root(repo):
        raise ValueError(f"Not a linked worktree of the current repository: {task}")
    if Path.cwd().resolve().is_relative_to(task):
        raise ValueError("Run remove from a different worktree of this repository")
    refresh(repo)
    if unpublished(repo, entry["HEAD"]):
        raise ValueError(f"Refusing to remove worktree with unpublished commits: {task}")
    if git(task, "status", "--porcelain", "--ignored=matching", "--untracked-files=all"):
        raise ValueError(f"Refusing to remove dirty worktree (including ignored files): {task}")
    git(repo, "worktree", "remove", str(task))
    git(repo, "worktree", "prune")
    return {"removed": str(task)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("new")
    create.add_argument("task")
    create.add_argument("--from", dest="source")
    commands.add_parser("list")
    commands.add_parser("config", help="Read effective settings without changing files")
    delete = commands.add_parser("remove")
    delete.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        repo = Path.cwd()
        if args.command == "remove":
            repo = project_root(args.path)
            project = repo
        elif args.command in {"list", "config"}:
            project = current_project(repo)
        else:
            project = project_root(repo)
        settings = resolve_settings(project)
        if args.command == "config":
            output = {"enabled": settings.enabled, "root": str(settings.root)}
        elif not settings.enabled:
            raise ValueError("git_kit.worktrees.enabled is false; opt in through bootstrap.json")
        elif args.command == "new":
            output = new(repo, settings, args.task, args.source)
        elif args.command == "list":
            output = list_worktrees(settings)
        else:
            output = remove(repo, settings, args.path)
        print(json.dumps(output, indent=2))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f"git-kit worktrees: {detail}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    from bootstrap_guard import reexec_under_plugin_venv

    reexec_under_plugin_venv("git-kit")
    raise SystemExit(main())
