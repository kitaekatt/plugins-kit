#!/usr/bin/env python3
"""Shared Git-index helpers for this repo's pre-commit checks.

Why this module exists
----------------------
A pre-commit check must answer "is the COMMIT I am about to make
self-consistent", not "is the working tree self-consistent". Those differ
constantly here: the tree is shared with concurrent agent sessions, so a
worktree-wide check fails on edits the commit does not contain, and it passes
an inconsistent pair that IS staged, because history is built from the index.

Answering the right question takes two halves, and every check needs both:

  1. INDEX-AWARE -- read staged blobs (``git show :<path>``), not the worktree.
  2. SCOPED -- pass early when the commit stages nothing the check derives
     from. A commit staging none of a check's inputs cannot violate that
     check's invariant; pre-existing drift belongs to whichever commit
     introduced it.

Both halves were being re-implemented per script (regen_marketplace.py,
check_pyproject_sync.py, generate_orchestration.py, precommit_guard.py), with
comments saying "mirroring X" and real behavioural divergence between the
copies. This module is the one implementation.

Unified semantics, and why each was chosen over the copy it replaces
--------------------------------------------------------------------
* **Every git call is bounded by a timeout** (``GIT_TIMEOUT``). Two copies had
  no timeout at all, which lets a wedged git hang a commit forever with no
  output. A bounded call that reports "git could not answer" is strictly
  better than an unbounded one.
* **Staged paths are read NUL-delimited** (``-z``). Without it git quotes and
  escapes paths containing spaces, quotes or non-ASCII bytes, so the plain
  ``--name-only`` copies silently mis-report those paths. ``-z`` removes the
  quoting entirely rather than trying to unquote it.
* **No ``--diff-filter``.** One copy filtered to ``AM`` (added/modified). That
  hides staged DELETIONS, which is exactly the state an invariant check most
  needs to see -- a staged ``git rm``/``git rm --cached`` of a manifest whose
  worktree file still exists. Callers that genuinely want a filter pass one
  explicitly.
* **Backslashes are normalised to forward slashes.** Git emits POSIX
  separators already, so this is a no-op on real output; it is kept (from the
  one copy that had it) because callers compare these strings against
  ``as_posix()`` paths and a stray platform separator would silently fail to
  match rather than raise.
* **"Git could not answer" is always ``None``, never ``[]``.** The two are not
  interchangeable for a SCOPED check: ``[]`` means "this commit stages none of
  my inputs" and legitimately skips the check, while ``None`` means "I could
  not find out" and must fall back to the worktree. One copy returned ``[]``
  for a non-repo directory, which under scoping would turn an unanswerable
  question into a silent pass. A check that passes because its input was
  unavailable is not a check.
* **Text and bytes are both first class.** ``index_blob`` returns bytes
  (needed by byte-oriented consumers), ``index_text`` decodes UTF-8 with
  ``errors="replace"`` -- a manifest with a bad byte should be reported by the
  parser as malformed content, not crash the hook with a UnicodeDecodeError.

Stdlib-only on purpose: these run inside a pre-commit hook, on machines that
may not have a provisioned venv.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

GIT_TIMEOUT = 30

# Scope verdicts returned by classify_scope().
SCOPE_SKIP = "skip"          # git answered; the commit stages none of the inputs
SCOPE_INDEX = "index"        # git answered; judge the staged blobs
SCOPE_WORKTREE = "worktree"  # git could not answer; judge the working tree


def is_git_repo(repo_root: Path) -> bool:
    """True when repo_root looks like a Git working tree root.

    ``.git`` may be a directory (normal clone) or a file (worktree/submodule),
    so existence -- not is_dir() -- is the test.
    """
    return (Path(repo_root) / ".git").exists()


def git_output(repo_root: Path, args: Sequence[str]) -> bytes | None:
    """Raw stdout of ``git <args>`` run in repo_root, or None if git cannot answer.

    None covers every "no answer" case uniformly: git missing, not a repo,
    non-zero exit, or a call that outran GIT_TIMEOUT.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _decode_paths(raw: bytes) -> list[str]:
    return [
        item.decode("utf-8", "replace").replace("\\", "/")
        for item in raw.split(b"\0")
        if item
    ]


def staged_paths(
    repo_root: Path, *, diff_filter: str | None = None
) -> list[str] | None:
    """Repo-relative staged paths, or None when Git cannot answer.

    Deletions are INCLUDED unless the caller passes an explicit ``diff_filter``
    (see the module header). An empty list means "this commit stages nothing",
    which is a real answer and must not be confused with None.
    """
    if not is_git_repo(repo_root):
        return None
    args = ["diff", "--cached", "--name-only", "-z"]
    if diff_filter:
        args.append(f"--diff-filter={diff_filter}")
    raw = git_output(repo_root, args)
    if raw is None:
        return None
    return _decode_paths(raw)


def index_files(repo_root: Path, *pathspecs: str) -> list[str] | None:
    """Paths recorded in the INDEX matching pathspecs, or None if git cannot answer.

    This is how a check enumerates "what files does the commit contain", as
    opposed to ``Path.iterdir()``, which enumerates the shared worktree and so
    still sees a file whose deletion is already staged.
    """
    if not is_git_repo(repo_root):
        return None
    raw = git_output(repo_root, ["ls-files", "--cached", "-z", "--", *pathspecs])
    if raw is None:
        return None
    return _decode_paths(raw)


def index_blob(repo_root: Path, rel_path: str) -> bytes | None:
    """A staged blob as bytes, or None when the index has no such path."""
    return git_output(repo_root, ["show", f":{rel_path}"])


def index_text(repo_root: Path, rel_path: str) -> str | None:
    """A staged blob decoded as UTF-8, or None when the index has no such path."""
    raw = index_blob(repo_root, rel_path)
    if raw is None:
        return None
    return raw.decode("utf-8", "replace")


# Backwards-compatible alias for the name used by some callers.
read_from_index = index_text


def classify_scope(
    repo_root: Path,
    is_input: Callable[[str], bool],
    *,
    staged: Iterable[str] | None = None,
) -> tuple[str, list[str]]:
    """Decide whether a staged-mode check should run, and against what.

    Returns ``(verdict, staged_paths)`` where verdict is one of SCOPE_SKIP
    (nothing this check derives from is staged -- pass), SCOPE_INDEX (judge the
    staged blobs) or SCOPE_WORKTREE (git could not answer -- judge the worktree
    rather than silently passing).

    ``staged`` is a test-injection seam; when given, git is not consulted.
    """
    paths = list(staged) if staged is not None else staged_paths(repo_root)
    if paths is None:
        return SCOPE_WORKTREE, []
    if not any(is_input(p) for p in paths):
        return SCOPE_SKIP, paths
    return SCOPE_INDEX, paths
