"""Copy the target into a scratch directory and prune it to the analysis inputs.

The metric tools write into the directory they scan, so they never scan the
target itself: they scan a snapshot, which the caller removes afterwards.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

from .errors import Refusal
from .languages import unconfigured_extensions


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise Refusal(f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}")
    return proc.stdout


def repo_root(target: Path) -> Path:
    if not target.is_dir():
        raise Refusal(f"--target {target} is not a directory")
    return Path(git(target, "rev-parse", "--show-toplevel").strip()).resolve()


def head_sha(repo: Path) -> str:
    return git(repo, "rev-parse", "--verify", "HEAD").strip()


def dirty_paths(repo: Path) -> int:
    """The number of entries ``git status --porcelain`` lists."""
    return len([line for line in git(repo, "status", "--porcelain").splitlines() if line])


def snapshot_head(repo: Path, dest: Path) -> None:
    """Extract ``git archive HEAD`` into ``dest``; members may not leave ``dest``."""
    proc = subprocess.Popen(
        ["git", "-C", str(repo), "archive", "--format=tar", "HEAD"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as archive:
            archive.extractall(dest, filter="data")
    finally:
        proc.stdout.close()
        stderr = proc.stderr.read().decode("utf-8", errors="replace")
        proc.stderr.close()
        code = proc.wait()
    if code != 0:
        raise Refusal(f"git archive HEAD failed in {repo}: {stderr.strip()}")


def snapshot_working_tree(repo: Path, dest: Path) -> None:
    """Copy every tracked or untracked-but-not-ignored path that exists on disk."""
    listing = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    for rel in sorted({p for p in listing.split("\0") if p}):
        src = repo / rel
        if not (src.is_symlink() or src.is_file()):
            continue  # deleted from the working tree, or a submodule directory
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out, follow_symlinks=False)


def take(repo: Path, mode: str, dest: Path) -> None:
    if mode == "head":
        snapshot_head(repo, dest)
    elif mode == "working-tree":
        snapshot_working_tree(repo, dest)
    else:
        raise Refusal(f"unknown snapshot mode {mode!r}")


def require_roots(root: Path, roots: list[str], key: str, config_path: Path, mode: str) -> None:
    for rel in roots:
        if not (root / rel).exists():
            raise Refusal(f"{config_path}: {key} entry {rel!r} does not exist in the {mode} snapshot")


def prune(root: Path, exclude: list[str], languages: list[str]) -> dict[str, int]:
    """Delete excluded files and sources of unconfigured languages; keep everything else."""
    drop_ext = unconfigured_extensions(languages)
    counts = {"excluded": 0, "other_language": 0}
    files = [Path(top) / name
             for top, dirs, names in os.walk(root, followlinks=False)
             for name in names]
    for path in sorted(files):
        rel = path.relative_to(root).as_posix()
        if any(fnmatch.fnmatchcase(rel, glob) for glob in exclude):
            counts["excluded"] += 1
        elif path.suffix in drop_ext:
            counts["other_language"] += 1
        else:
            continue
        os.unlink(path)
    return counts
