#!/usr/bin/env python3
"""Freeze, land, drop and prune review snapshots for git-code-review.

Usage:
    snapshot_review.py freeze (--paths <p>... | --patch <file> | --staged)
    snapshot_review.py land <snapshot> -F <msgfile>
    snapshot_review.py drop <snapshot> [--tree-only]
    snapshot_review.py prune [--older-than-days N]

A snapshot is a commit built from a PRIVATE index (an absolute GIT_INDEX_FILE),
parented on HEAD and pinned by `refs/git-kit/review/<sha12>`. Freeze touches
neither the shared index nor the worktree, so an agent may keep editing the
reviewed files while a background review reads the immutable commit.

freeze   Select this session's content (whole files, a filtered patch, or the
         shared index as it stands), write the snapshot commit and ref, print
         JSON {snapshot, tree, base, ref, paths, range}. Exit 2 with no
         selector; exit 1 when the selection equals HEAD (nothing to review).
         Runs `prune` afterwards; a prune failure is reported on stderr and
         does not fail the freeze.
land     Commit exactly the reviewed content: build a private landing index
         (the snapshot tree when HEAD still equals the snapshot base, else
         HEAD plus the snapshot's patch), run a normal `git commit` on it so
         hooks run, verify tree (or patch-id) equality, then reset the shared
         index entries of the snapshot paths so post-snapshot edits stay as
         unstaged changes. Prints JSON {landed, snapshot, tree_equal,
         patch_id_equal} and deletes the ref and bundle directory.
drop     Delete a snapshot's ref and bundle directory, or with --tree-only just
         `<bundle>/tree/` (which may hold copied credentials).
prune    Drop snapshots older than N days (default 7).

Only `refs/git-kit/review/<12 hex>` refs and `reviews/*/snap-<12 hex>` bundle
directories under the git-kit data directory are ever deleted. Bundle
directories are located by globbing, with no dependency on prepare_review.
Errors raise or exit non-zero with a message; nothing is retried or ignored.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from bootstrap_guard import data_dir

REF_PREFIX = "refs/git-kit/review/"
DEFAULT_PRUNE_DAYS = 7.0
_SHA12 = re.compile(r"^[0-9a-f]{12}$")
_HEX = re.compile(r"^[0-9a-f]{12,40}$")
_ZEROS = "0" * 40
_IDENTITY = {
    "GIT_AUTHOR_NAME": "git-kit review snapshot",
    "GIT_AUTHOR_EMAIL": "git-kit-review-snapshot@invalid",
    "GIT_COMMITTER_NAME": "git-kit review snapshot",
    "GIT_COMMITTER_EMAIL": "git-kit-review-snapshot@invalid",
}
# Keep pathspec arguments well under the Windows command-line limit.
_ARG_CHAR_BUDGET = 8000


class SnapshotError(Exception):
    """A refusal or failure that the caller must see; main() exits 1."""


# ---------------------------------------------------------------- git plumbing


def _git(
    args: list[str],
    *,
    cwd: Optional[str] = None,
    index: Optional[str] = None,
    input: Optional[bytes | str] = None,
    env: Optional[dict[str, str]] = None,
    literal: bool = False,
    check: bool = True,
    binary: bool = False,
) -> subprocess.CompletedProcess:
    """Run git. `index`, when given, must be absolute and becomes GIT_INDEX_FILE.

    An inherited GIT_INDEX_FILE is removed so a call without `index` always
    means the shared index.
    """
    full_env = dict(os.environ)
    full_env.pop("GIT_INDEX_FILE", None)
    if index is not None:
        if not os.path.isabs(index):
            raise SnapshotError(f"internal error: GIT_INDEX_FILE must be absolute: {index}")
        full_env["GIT_INDEX_FILE"] = index
    if literal:
        full_env["GIT_LITERAL_PATHSPECS"] = "1"
    if env:
        full_env.update(env)
    if isinstance(input, str):
        input = input.encode("utf-8")
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=full_env,
        input=input,
        capture_output=True,
    )
    if not binary:
        proc.stdout = proc.stdout.decode("utf-8", errors="replace")  # type: ignore[assignment]
        proc.stderr = proc.stderr.decode("utf-8", errors="replace")  # type: ignore[assignment]
    if check and proc.returncode != 0:
        err = proc.stderr if not binary else proc.stderr.decode("utf-8", errors="replace")
        raise SnapshotError(f"git {' '.join(args[:3])} failed ({proc.returncode}): {err.strip()}")
    return proc


def _repo_root() -> str:
    proc = _git(["rev-parse", "--show-toplevel"], check=False)
    if proc.returncode != 0:
        raise SnapshotError("not inside a git repository")
    return proc.stdout.strip()


def _rev(root: str, spec: str) -> str:
    proc = _git(["rev-parse", "--verify", "--quiet", spec], cwd=root, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise SnapshotError(f"cannot resolve {spec!r}")
    return proc.stdout.strip()


def _shared_index_path(root: str) -> str:
    out = _git(["rev-parse", "--path-format=absolute", "--git-path", "index"], cwd=root).stdout.strip()
    if not os.path.isabs(out):
        raise SnapshotError(f"cannot locate the shared index: {out!r}")
    return out


def _chunks(items: list[str]) -> list[list[str]]:
    out: list[list[str]] = []
    cur: list[str] = []
    size = 0
    for item in items:
        if cur and size + len(item) + 1 > _ARG_CHAR_BUDGET:
            out.append(cur)
            cur, size = [], 0
        cur.append(item)
        size += len(item) + 1
    if cur:
        out.append(cur)
    return out


def _nul(paths: list[str]) -> bytes:
    return b"".join(p.encode("utf-8") + b"\0" for p in paths)


def _changed_paths(root: str, base: str, snap: str) -> list[str]:
    out = _git(
        ["diff-tree", "-r", "--no-renames", "--name-only", "-z", base, snap],
        cwd=root,
        binary=True,
    ).stdout
    return sorted(p.decode("utf-8") for p in out.split(b"\0") if p)


def _write_tree(root: str, index: str) -> str:
    return _git(["write-tree"], cwd=root, index=index).stdout.strip()


def _patch_id(root: str, a: str, b: str) -> str:
    diff = _git(
        ["diff", "-U0", "--binary", "--full-index", "--no-renames", "--no-ext-diff", a, b],
        cwd=root,
        binary=True,
    ).stdout
    if not diff:
        return ""
    out = _git(["patch-id", "--stable"], cwd=root, input=diff).stdout.split()
    return out[0] if out else ""


def _rmtree(path: Path) -> None:
    """Remove a directory tree, clearing read-only bits; failures raise."""

    def _onexc(func, p, exc):  # noqa: ANN001
        if isinstance(exc, PermissionError):
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            func(p)
            return
        raise exc

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_onexc)
    else:  # pragma: no cover - the repo pins 3.12
        shutil.rmtree(path, onerror=lambda f, p, e: _onexc(f, p, e[1]))


# ------------------------------------------------------------- snapshot lookup


def _bundle_dirs(sha12: str) -> list[Path]:
    """Bundle directories `reviews/*/snap-<sha12>` under the git-kit data dir."""
    if not _SHA12.match(sha12):
        raise SnapshotError(f"internal error: not a 12-hex snapshot id: {sha12!r}")
    reviews = data_dir("git-kit") / "reviews"
    found = []
    for d in sorted(reviews.glob(f"*/snap-{sha12}")):
        if d.is_symlink():
            raise SnapshotError(f"refusing to follow a symlinked bundle directory: {d}")
        if d.is_dir():
            found.append(d)
    return found


def _parse_snapshot_id(arg: str) -> str:
    """The 12-hex id named by a sha (12-40 hex) or a full snapshot ref name."""
    text = arg[len(REF_PREFIX):] if arg.startswith(REF_PREFIX) else arg
    text = text.lower()
    if not _HEX.match(text):
        raise SnapshotError(f"not a snapshot sha: {arg!r} (want 12-40 hex digits)")
    return text[:12]


def _ref_target(root: str, sha12: str) -> Optional[str]:
    proc = _git(["rev-parse", "--verify", "--quiet", REF_PREFIX + sha12], cwd=root, check=False)
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def _require_snapshot(root: str, arg: str) -> tuple[str, str]:
    sha12 = _parse_snapshot_id(arg)
    full = _ref_target(root, sha12)
    if full is None:
        raise SnapshotError(f"no snapshot ref {REF_PREFIX}{sha12} in this repository")
    hex_arg = (arg[len(REF_PREFIX):] if arg.startswith(REF_PREFIX) else arg).lower()
    if len(hex_arg) > 12 and not full.startswith(hex_arg):
        raise SnapshotError(f"{arg} does not match the snapshot {full} named by {REF_PREFIX}{sha12}")
    return sha12, full


# ---------------------------------------------------------------------- freeze


def freeze(*, paths: Optional[list[str]], patch: Optional[str], staged: bool) -> dict:
    root = _repo_root()
    head = _git(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], cwd=root, check=False)
    if head.returncode != 0:
        raise SnapshotError("the repository has no commits; there is no base to snapshot against")
    base = head.stdout.strip()
    base_tree = _rev(root, f"{base}^{{tree}}")
    patch_abs = os.path.abspath(patch) if patch else None
    if patch_abs and not os.path.isfile(patch_abs):
        raise SnapshotError(f"patch file not found: {patch_abs}")

    tmp = tempfile.mkdtemp(prefix="git-kit-snap-")
    try:
        index = os.path.abspath(os.path.join(tmp, "index"))
        if staged:
            shutil.copyfile(_shared_index_path(root), index)
        else:
            _git(["read-tree", base], cwd=root, index=index)
        if paths:
            # Runs from the caller's cwd so relative paths mean what the caller meant.
            _git(
                ["add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul"],
                index=index,
                input=_nul(paths),
                literal=True,
            )
        elif patch_abs:
            _git(["apply", "--cached", "--binary", patch_abs], cwd=root, index=index)
        tree = _write_tree(root, index)
        if tree == base_tree:
            raise SnapshotError("the selection equals HEAD; there is nothing to review")
        commit = _git(
            ["-c", "commit.gpgsign=false", "commit-tree", tree, "-p", base, "-m", "git-kit review snapshot"],
            cwd=root,
            env=_IDENTITY,
        ).stdout.strip()
        ref = REF_PREFIX + commit[:12]
        _git(["update-ref", ref, commit], cwd=root)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    result = {
        "snapshot": commit,
        "tree": tree,
        "base": base,
        "ref": ref,
        "paths": _changed_paths(root, base, commit),
        "range": f"{base}..{commit}",
    }
    try:
        prune(DEFAULT_PRUNE_DAYS)
    except (SnapshotError, OSError) as exc:
        print(f"snapshot_review: warning: prune failed: {exc}", file=sys.stderr)
    return result


# ------------------------------------------------------------------ drop/prune


def drop(arg: str, *, tree_only: bool = False) -> dict:
    root = _repo_root()
    sha12 = _parse_snapshot_id(arg)
    return _drop_id(root, sha12, tree_only=tree_only)


def _drop_id(root: str, sha12: str, *, tree_only: bool) -> dict:
    removed: list[str] = []
    dirs = _bundle_dirs(sha12)
    target = _ref_target(root, sha12)
    if not tree_only and not dirs and target is None:
        raise SnapshotError(f"no snapshot {sha12}: no ref {REF_PREFIX}{sha12} and no bundle directory")
    for d in dirs:
        victim = d / "tree" if tree_only else d
        if victim.is_symlink():
            raise SnapshotError(f"refusing to follow a symlink: {victim}")
        if victim.exists():
            _rmtree(victim)
            removed.append(str(victim))
    ref_deleted = False
    if not tree_only and target is not None:
        # Directory first: a failed directory removal leaves the ref for a retry.
        _git(["update-ref", "-d", REF_PREFIX + sha12, target], cwd=root)
        ref_deleted = True
    return {"snapshot": target or sha12, "removed": removed, "ref_deleted": ref_deleted}


def prune(older_than_days: float) -> dict:
    if older_than_days < 0:
        raise SnapshotError("--older-than-days must not be negative")
    root = _repo_root()
    cutoff = time.time() - older_than_days * 86400.0
    out = _git(
        ["for-each-ref", "--format=%(refname) %(committerdate:unix)", REF_PREFIX],
        cwd=root,
    ).stdout
    dropped: list[str] = []
    for line in out.splitlines():
        name, _, stamp = line.rpartition(" ")
        if not name.startswith(REF_PREFIX) or not _SHA12.match(name[len(REF_PREFIX):]):
            continue  # not a snapshot ref of ours
        if int(stamp) < cutoff:
            sha12 = name[len(REF_PREFIX):]
            _drop_id(root, sha12, tree_only=False)
            dropped.append(sha12)
    return {"pruned": dropped}


# ------------------------------------------------------------------------ land


def _refuse_in_progress(root: str) -> None:
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        if _git(["rev-parse", "--verify", "--quiet", name], cwd=root, check=False).returncode == 0:
            raise SnapshotError(f"refusing to land: {name} exists (an operation is in progress)")


def _check_shared_index(root: str, head: str, snap: str, paths: list[str]) -> None:
    """Refuse when a snapshot path's shared-index entry differs from BOTH HEAD and the snapshot.

    An entry equal to HEAD is untouched; an entry equal to the snapshot is this
    session's own staging (a `--staged` freeze). Anything else is another
    session's staged work, which resetting would destroy.
    """
    snap_entries: dict[str, tuple[str, str]] = {}
    index_entries: dict[str, tuple[str, str]] = {}
    for chunk in _chunks(paths):
        ls = _git(["ls-tree", "-r", "-z", snap, "--", *chunk], cwd=root, literal=True, binary=True).stdout
        for rec in ls.split(b"\0"):
            if rec:
                meta, _, name = rec.partition(b"\t")
                mode, _type, sha = meta.decode().split(" ")
                snap_entries[name.decode("utf-8")] = (mode, sha)
        di = _git(
            ["diff-index", "--cached", "--no-renames", "--no-abbrev", "-z", "--raw", head, "--", *chunk],
            cwd=root,
            literal=True,
            binary=True,
        ).stdout
        parts = di.split(b"\0")
        i = 0
        while i + 1 < len(parts) and parts[i]:
            meta = parts[i].decode().lstrip(":").split(" ")
            name = parts[i + 1].decode("utf-8")
            _old_mode, new_mode, _old_sha, new_sha, status = meta[0], meta[1], meta[2], meta[3], meta[4]
            if status.startswith("U"):
                raise SnapshotError(f"refusing to land: {name} is unmerged in the shared index")
            index_entries[name] = (new_mode.zfill(6), new_sha)
            i += 2
    for name, entry in sorted(index_entries.items()):
        snap_entry = snap_entries.get(name, ("000000", _ZEROS))
        if (entry[0].zfill(6), entry[1]) != (snap_entry[0].zfill(6), snap_entry[1]):
            raise SnapshotError(
                f"refusing to land: the shared index holds a staged entry for {name} that "
                "matches neither HEAD nor the snapshot (another session's work); "
                "resetting it would destroy that record"
            )


def _run_commit(root: str, landing_index: str, msgfile: str) -> None:
    """A normal `git commit` on the landing index, so hooks run. Never `commit -- <paths>`."""
    _git(["commit", "-q", "-F", msgfile], cwd=root, index=landing_index)


def land(arg: str, msgfile: str) -> tuple[dict, Optional[Exception]]:
    root = _repo_root()
    msg = os.path.abspath(msgfile)
    if not os.path.isfile(msg):
        raise SnapshotError(f"message file not found: {msg}")
    sha12, snap = _require_snapshot(root, arg)
    parents = _git(["rev-list", "--parents", "-n", "1", snap], cwd=root).stdout.split()[1:]
    if len(parents) != 1:
        raise SnapshotError(f"{snap} is not a snapshot commit (expected exactly one parent)")
    base = parents[0]
    snap_tree = _rev(root, f"{snap}^{{tree}}")
    head0 = _rev(root, "HEAD^{commit}")
    _refuse_in_progress(root)
    paths = _changed_paths(root, base, snap)
    if not paths:
        raise SnapshotError(f"snapshot {snap} changes nothing relative to its base")
    _check_shared_index(root, head0, snap, paths)

    tmp = tempfile.mkdtemp(prefix="git-kit-land-")
    undone = False
    try:
        landing = os.path.abspath(os.path.join(tmp, "landing.index"))
        if head0 == base:
            _git(["read-tree", snap], cwd=root, index=landing)
        else:
            _git(["read-tree", head0], cwd=root, index=landing)
            diff = _git(
                ["diff", "--binary", "--full-index", "--no-renames", "--no-ext-diff", base, snap],
                cwd=root,
                binary=True,
            ).stdout
            applied = _git(
                ["apply", "--cached", "--binary"], cwd=root, index=landing, input=diff, check=False, binary=True
            )
            if applied.returncode != 0:
                raise SnapshotError(
                    f"HEAD moved from {base} to {head0} and the snapshot does not replay onto it; "
                    f"HEAD is untouched. git apply said:\n{applied.stderr.decode('utf-8', errors='replace').strip()}"
                )
        landing_tree = _write_tree(root, landing)
        if landing_tree == _rev(root, f"{head0}^{{tree}}"):
            raise SnapshotError("nothing to land: HEAD already contains the snapshot's change")
        now_head = _rev(root, "HEAD^{commit}")
        if now_head != head0:
            raise SnapshotError(f"HEAD moved from {head0} to {now_head} while preparing the landing; re-run land")

        try:
            _run_commit(root, landing, msg)
            landed = _rev(root, "HEAD^{commit}")
            parent = _rev(root, f"{landed}^")
            if parent != head0:
                # A foreign commit arrived between the pre-check and the start of git commit:
                # our tree was built on head0 and would revert it. Put HEAD back.
                _git(["update-ref", "HEAD", parent, landed], cwd=root)
                undone = True
                raise SnapshotError(
                    f"HEAD moved to {parent} (a foreign commit) before the landing commit started; "
                    f"undid the landing commit {landed} and left HEAD at {parent}. Re-run land."
                )
            landed_tree = _rev(root, f"{landed}^{{tree}}")
            tree_equal = landed_tree == snap_tree
            pid_snap = _patch_id(root, base, snap)
            pid_landed = _patch_id(root, parent, landed)
            patch_id_equal = bool(pid_snap) and pid_snap == pid_landed
            if head0 == base and not tree_equal:
                raise SnapshotError(
                    f"landed commit {landed} has tree {landed_tree}, not the reviewed tree {snap_tree} "
                    "(a hook changed the index?)"
                )
            if head0 != base and not patch_id_equal:
                raise SnapshotError(
                    f"landed commit {landed} does not carry the reviewed change: patch-id "
                    f"{pid_landed or 'empty'} != {pid_snap or 'empty'}"
                )
        finally:
            # Between the commit and this reset the shared index holds the pre-land
            # state, which reads as a revert of the landed change. Reset whenever HEAD
            # moved and was not put back, verification failure included.
            if not undone and _rev(root, "HEAD^{commit}") != head0:
                for chunk in _chunks(paths):
                    _git(["reset", "-q", "--", *chunk], cwd=root, literal=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    result = {
        "landed": landed,
        "snapshot": snap,
        "tree_equal": tree_equal,
        "patch_id_equal": patch_id_equal,
    }
    cleanup_error: Optional[Exception] = None
    try:
        _drop_id(root, sha12, tree_only=False)
    except (SnapshotError, OSError) as exc:
        cleanup_error = exc
    return result, cleanup_error


# ------------------------------------------------------------------------- CLI


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="snapshot_review.py",
        description="Freeze, land, drop and prune git-code-review snapshots.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_freeze = sub.add_parser("freeze", help="write a snapshot commit from a private index")
    sel = p_freeze.add_mutually_exclusive_group(required=True)
    sel.add_argument("--paths", nargs="+", metavar="PATH", help="whole files (adds, deletions, untracked included)")
    sel.add_argument("--patch", metavar="FILE", help="a filtered patch applied to a private copy of HEAD")
    sel.add_argument("--staged", action="store_true", help="the shared index as it stands")

    p_land = sub.add_parser("land", help="commit exactly the reviewed content")
    p_land.add_argument("snapshot", help="snapshot sha (12-40 hex) or its ref name")
    p_land.add_argument("-F", "--file", dest="msgfile", required=True, metavar="MSGFILE", help="commit message file")

    p_drop = sub.add_parser("drop", help="delete a snapshot's ref and bundle directory")
    p_drop.add_argument("snapshot", help="snapshot sha (12-40 hex) or its ref name")
    p_drop.add_argument("--tree-only", action="store_true", help="remove only <bundle>/tree/")

    p_prune = sub.add_parser("prune", help="drop snapshots older than N days")
    p_prune.add_argument("--older-than-days", type=float, default=DEFAULT_PRUNE_DAYS, metavar="N")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "freeze":
            result = freeze(paths=args.paths, patch=args.patch, staged=args.staged)
        elif args.command == "land":
            result, cleanup_error = land(args.snapshot, args.msgfile)
            print(json.dumps(result))
            if cleanup_error is not None:
                raise SnapshotError(
                    f"landed {result['landed']} but removing the snapshot failed: {cleanup_error}"
                )
            return 0
        elif args.command == "drop":
            result = drop(args.snapshot, tree_only=args.tree_only)
        else:
            result = prune(args.older_than_days)
    except SnapshotError as exc:
        print(f"snapshot_review: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
