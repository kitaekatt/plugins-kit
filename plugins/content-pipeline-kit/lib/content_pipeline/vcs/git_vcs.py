"""The git VcsBackend implementation -- the shipped, implied default.

Implements the ``VcsBackend`` protocol against a local git working tree.
Git is the implied default VCS for content-pipeline-kit (see the plugin
proposal's VCS-seam decision); a Perforce implementation of the same protocol
ships in p4-kit rather than here, so this plugin never depends on p4 tooling.

The changeset mapping (git has no pending-changelist concept)
-------------------------------------------------------------

The ``VcsBackend`` protocol is shaped around Perforce's pending changelist: a
server-side container you create up front, move files into one at a time, give
a final description, and delete if it ended up empty. Git has no such object,
so the mapping is:

- **changeset == a staged set finalized as a commit.** ``make_changeset``
  creates NO git object -- it returns an in-memory :class:`GitChangeset` that
  accumulates the paths staged into it. Nothing is committed until
  ``finalize_description``.
- **open_for_edit == no-op.** Git tracks the whole working tree; there is no
  per-file "open for edit" checkout. The method exists for protocol parity and
  does nothing (unlike Perforce, where it is a real ``p4 edit``).
- **add / move_into == ``git add`` of the exact paths.** ``move_into`` stages
  each path (``git add -- <path>``) and records it on the changeset, mirroring
  Perforce's per-item ``p4 reopen`` into the CL. Exactly the paths passed are
  staged -- never a wildcard -- so unrelated working-tree changes are never
  swept in (the source system's CL-sweep bug this seam was built to avoid).
- **finalize_description == commit the staged subset with the rebuilt message.**
  ``git commit -m <description> -- <paths>`` commits ONLY the paths moved into
  the changeset (pathspec-scoped), so a commit contains exactly the subset that
  was successfully moved in -- the "description rebuilt from the successfully-
  moved subset" choreography. Returns the new commit sha.
- **revert == restore the exact paths.** ``git checkout HEAD -- <path>``
  discards working-tree + index changes for that one path, the analogue of
  ``p4 revert <file>``. Never a wildcard.
- **delete_if_empty == no-op when nothing was staged.** An empty changeset was
  never committed (``finalize_description`` refuses an empty pathspec), so there
  is nothing to delete; the method is a safety parity no-op.

Everything routes through an injected ``runner`` seam so tests can drive a real
git repo in a tmp dir (git is available) without this module hardcoding
``subprocess``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

# A git runner: (args, cwd) -> (returncode, stdout, stderr). ``args`` is the
# git argument vector WITHOUT the leading "git" (e.g. ["add", "--", "x"]).
GitRunner = Callable[[List[str], Path], Tuple[int, str, str]]


class GitVcsError(RuntimeError):
    """Raised when a git command fails non-recoverably."""


def _default_runner(args: List[str], cwd: Path) -> Tuple[int, str, str]:
    """Run ``git <args>`` in ``cwd`` and return ``(rc, stdout, stderr)``.

    Injected as the ``runner`` seam so a test can substitute a scripted stub;
    the real path spawns ``git`` with UTF-8 pipes. Imported locally so the
    module loads without ``subprocess`` being touched when a runner is
    injected.
    """
    import subprocess  # noqa: PLC0415

    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


@dataclass
class GitChangeset:
    """The git analogue of a pending changelist: a staged set + description.

    ``paths`` accumulates the working-tree paths moved into this changeset (in
    move order, de-duplicated). No git object exists until
    :meth:`GitVcs.finalize_description` commits the staged subset. ``committed``
    carries the commit sha once finalized (``None`` before, or when the
    changeset was empty and nothing was committed).
    """

    description: str = ""
    paths: List[str] = field(default_factory=list)
    committed: Optional[str] = None

    def _add_path(self, path: str) -> None:
        if path not in self.paths:
            self.paths.append(path)


@dataclass
class GitVcs:
    """``VcsBackend`` over a local git working tree.

    - ``repo_root`` -- the working tree root git commands run in.
    - ``runner`` -- the ``(args, cwd) -> (rc, out, err)`` seam (defaults to a
      real ``git`` subprocess).
    """

    repo_root: Path
    runner: GitRunner = _default_runner

    def _git(self, *args: str, check: bool = True) -> Tuple[int, str, str]:
        rc, out, err = self.runner(list(args), Path(self.repo_root))
        if check and rc != 0:
            raise GitVcsError(
                f"git {' '.join(args)} failed (exit {rc}): {err.strip() or out.strip()}"
            )
        return rc, out, err

    def _rel(self, path) -> str:
        """Return ``path`` relative to the repo root when it is absolute.

        A path already inside the working tree is passed through as a
        repo-relative pathspec so git treats it identically whether the caller
        supplied an absolute or a relative path.
        """
        p = Path(path)
        if p.is_absolute():
            for base in (Path(self.repo_root).resolve(), Path(self.repo_root)):
                try:
                    return str(p.relative_to(base))
                except ValueError:
                    continue
            return str(path)
        return str(path)

    # -- VcsBackend protocol --------------------------------------------------

    def open_for_edit(self, path) -> None:
        """No-op: git needs no per-file checkout-for-edit (see module docstring)."""

    def add(self, path) -> None:
        """Stage ``path`` (``git add -- <path>``)."""
        self._git("add", "--", self._rel(path))

    def make_changeset(self, description: str) -> GitChangeset:
        """Return a fresh in-memory changeset (no git object created yet)."""
        return GitChangeset(description=description)

    def move_into(self, changeset: GitChangeset, paths: list) -> None:
        """Stage each of ``paths`` and record them on ``changeset``.

        Exactly the given paths are staged -- never a wildcard -- so no
        unrelated working-tree change is swept into the eventual commit.
        """
        for path in paths:
            rel = self._rel(path)
            self._git("add", "--", rel)
            changeset._add_path(rel)

    def finalize_description(
        self, changeset: GitChangeset, description: str
    ) -> Optional[str]:
        """Commit the staged subset with ``description`` (rebuilt message).

        Commits ONLY the paths moved into ``changeset`` (a pathspec-scoped
        ``git commit -- <paths>``), so the commit contains exactly the
        successfully-moved subset. An empty changeset commits nothing and
        returns ``None`` (no empty commit). Returns the new commit sha on
        success.
        """
        changeset.description = description
        if not changeset.paths:
            return None
        self._git("commit", "-m", description, "--", *changeset.paths)
        _rc, out, _err = self._git("rev-parse", "HEAD")
        changeset.committed = out.strip()
        return changeset.committed

    def revert(self, path) -> None:
        """Discard working-tree + index changes for exactly ``path``.

        Uses ``git checkout HEAD -- <path>`` (universally available); this is
        the per-file ``p4 revert`` analogue. Never wildcards.
        """
        self._git("checkout", "HEAD", "--", self._rel(path))

    def delete_if_empty(self, changeset: GitChangeset) -> None:
        """No-op: an empty changeset was never committed, so nothing to delete."""


__all__ = ["GitVcs", "GitChangeset", "GitVcsError", "GitRunner"]
