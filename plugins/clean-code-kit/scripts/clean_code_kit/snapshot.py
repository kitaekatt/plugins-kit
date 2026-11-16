"""Copy the files the audit reads into a scratch directory, and nothing else.

The metric tools write into the directory they scan, so they never scan the
target itself: they scan a snapshot, which the caller removes afterwards. The
path list is chosen from git's file list before anything is written, so a file
the audit never reads (outside every root, excluded, or of an unconfigured
language) is never copied.
"""

from __future__ import annotations

import fnmatch
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

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


def dirty_paths(repo: Path, ignore: str | None = None) -> list[str]:
    """The paths ``git status --porcelain`` lists, one per file, except ``ignore`` (repo-relative posix)."""
    fields = git(repo, "status", "--porcelain", "-z", "--untracked-files=all").split("\0")
    names: list[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4 or entry[2] != " ":
            raise Refusal(f"unexpected git status entry {entry!r} in {repo}")
        if entry[0] in "RC" or entry[1] in "RC":
            index += 1  # a rename or copy carries its source path as the next field
        if entry[3:] != ignore:
            names.append(entry[3:])
    return names


def tracked_files(repo: Path, mode: str) -> list[str]:
    """Every file the ``mode`` snapshot holds, from git's file list (not limited to source roots)."""
    if mode == "head":
        listing = git(repo, "ls-tree", "-r", "-z", "--name-only", "HEAD")
    elif mode == "working-tree":
        listing = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    else:
        raise Refusal(f"unknown snapshot mode {mode!r}")
    paths = sorted({p for p in listing.split("\0") if p})
    if mode == "working-tree":
        paths = [p for p in paths if (repo / p).is_symlink() or (repo / p).is_file()]
    return paths


def _under(rel: str, roots: list[str]) -> bool:
    return any(rel == r or rel.startswith(r.rstrip("/") + "/") for r in roots)


def select_paths(tracked: list[str], source_roots: list[str], test_roots: list[str],
                 exclude: list[str], languages: list[str]) -> list[str]:
    """The ``tracked`` paths the audit reads: under a root, not excluded, not of an unconfigured language."""
    drop_ext = unconfigured_extensions(languages)
    roots = [*source_roots, *test_roots]
    return [rel for rel in tracked
            if _under(rel, roots)
            and not any(fnmatch.fnmatchcase(rel, glob) for glob in exclude)
            and PurePosixPath(rel).suffix not in drop_ext]


def snapshot_head(repo: Path, dest: Path, paths: list[str], roots: list[str]) -> None:
    """Extract only ``paths`` from ``git archive HEAD -- <roots>`` into ``dest``; members may not leave ``dest``.

    Limiting the archive to ``roots`` keeps git from producing (and smudging) blobs outside them.
    """
    wanted = set(paths)
    seen: set[str] = set()
    proc = subprocess.Popen(
        ["git", "-C", str(repo), "archive", "--format=tar", "HEAD", "--", *(f":(literal){r}" for r in roots)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as archive:
            for member in archive:
                if member.name in wanted:
                    seen.add(member.name)
                    archive.extract(member, dest, filter="data")
    finally:
        proc.stdout.close()
        stderr = proc.stderr.read().decode("utf-8", errors="replace")
        proc.stderr.close()
        code = proc.wait()
    if code != 0:
        raise Refusal(f"git archive HEAD failed in {repo}: {stderr.strip()}")
    if seen != wanted:
        raise Refusal(f"git archive HEAD in {repo} lacks {len(wanted - seen)} listed paths, "
                      f"for example {sorted(wanted - seen)[0]!r}")


def snapshot_working_tree(repo: Path, dest: Path, paths: list[str]) -> None:
    """Copy ``paths`` (files or symlinks on disk) from the working tree into ``dest``."""
    for rel in paths:
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo / rel, out, follow_symlinks=False)


def take(repo: Path, mode: str, dest: Path, paths: list[str], roots: list[str]) -> None:
    """Write ``paths`` (from ``tracked_files(repo, mode)``, all under ``roots``) into ``dest`` and nothing else."""
    if mode == "head":
        snapshot_head(repo, dest, paths, roots)
    elif mode == "working-tree":
        snapshot_working_tree(repo, dest, paths)
    else:
        raise Refusal(f"unknown snapshot mode {mode!r}")


def require_roots(tracked: list[str], roots: list[str], key: str, config_path: Path, mode: str) -> None:
    """Refuse a root that no path of the ``mode`` snapshot is, or is under."""
    for rel in roots:
        if not any(_under(path, [rel]) for path in tracked):
            raise Refusal(f"{config_path}: {key} entry {rel!r} does not exist in the {mode} snapshot")

