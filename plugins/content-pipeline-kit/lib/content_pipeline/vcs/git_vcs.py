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
- **add / move_into == ``git add`` of the exact paths, as literal pathspecs.**
  ``move_into`` stages each path (``git add -- :(literal)<path>``) and records
  it on the changeset, mirroring Perforce's per-item ``p4 reopen`` into the
  CL. The ``:(literal)`` magic prefix disables git's post-``--`` glob
  expansion of ``*``/``?``/``[`` in the pathspec, so a path containing one of
  those characters (a real filename) stages exactly itself, never a sibling.
  Exactly the paths passed are staged -- never a wildcard -- so unrelated
  working-tree changes are never swept in (the source system's CL-sweep bug
  this seam was built to avoid).
- **finalize_description == commit the staged subset with the rebuilt message.**
  ``git commit -m <description> -- <paths>`` commits ONLY the paths moved into
  the changeset (pathspec-scoped), so a commit contains exactly the subset that
  was successfully moved in -- the "description rebuilt from the successfully-
  moved subset" choreography. When none of those paths carries a staged diff
  against ``HEAD`` (a re-delivery of unchanged content -- ``apply_inplace`` is
  idempotent by contract), git's own "nothing to commit" exit is treated the
  same as an empty changeset: no commit, ``None`` returned, no error. Any
  other commit failure still raises :class:`GitVcsError`. Returns the new
  commit sha on success.
- **revert == restore the exact path, or remove it when git has no ``HEAD``
  version to restore.** A path tracked in ``HEAD`` is restored with
  ``git checkout HEAD -- <path>`` (the analogue of ``p4 revert <file>``). A
  path that exists only in the index -- ``add``-ed but never committed -- has
  no ``HEAD`` version to check out, so it is unstaged and its working file is
  removed instead, matching what ``p4 revert`` does to a newly opened add.
  Never a wildcard -- every pathspec is a literal.
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

    def _literal(self, rel: str) -> str:
        """Return ``rel`` wrapped as a literal git pathspec.

        After ``--`` git still expands ``*``, ``?`` and ``[`` as glob
        metacharacters against the pathspec, so a relative path containing
        one of them (a real filename such as ``notes[draft].md``) can match
        sibling files instead of naming exactly itself. The ``:(literal)``
        magic prefix disables that expansion, matching the module docstring's
        "exactly the paths passed are staged -- never a wildcard" guarantee.
        """
        return f":(literal){rel}"

    # -- VcsBackend protocol --------------------------------------------------

    def open_for_edit(self, path) -> None:
        """No-op: git needs no per-file checkout-for-edit (see module docstring)."""

    def add(self, path) -> None:
        """Stage ``path`` (``git add -- <path>``), as a literal pathspec."""
        self._git("add", "--", self._literal(self._rel(path)))

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
            self._git("add", "--", self._literal(rel))
            changeset._add_path(rel)

    def finalize_description(
        self, changeset: GitChangeset, description: str
    ) -> Optional[str]:
        """Commit the staged subset with ``description`` (rebuilt message).

        Commits ONLY the paths moved into ``changeset`` (a pathspec-scoped
        ``git commit -- <paths>``), so the commit contains exactly the
        successfully-moved subset. An empty changeset commits nothing and
        returns ``None`` (no empty commit). When the staged paths carry no
        diff against ``HEAD`` (e.g. a re-delivery of unchanged content --
        ``apply_inplace`` is idempotent by contract), git exits with "nothing
        to commit" for that pathspec; this is the documented empty-changeset
        result, not a failure, so it also returns ``None`` rather than
        raising. Any other commit failure still raises
        :class:`GitVcsError`. Returns the new commit sha on success.
        """
        changeset.description = description
        if not changeset.paths:
            return None
        literal_paths = [self._literal(p) for p in changeset.paths]
        rc, out, err = self._git(
            "commit", "-m", description, "--", *literal_paths, check=False
        )
        if rc != 0:
            if self._is_nothing_to_commit(literal_paths):
                return None
            raise GitVcsError(
                f"git commit failed (exit {rc}): {err.strip() or out.strip()}"
            )
        _rc, out, _err = self._git("rev-parse", "HEAD")
        changeset.committed = out.strip()
        return changeset.committed

    def _is_nothing_to_commit(self, literal_paths: List[str]) -> bool:
        """True when none of ``literal_paths`` has a staged diff against ``HEAD``.

        Distinguishes the benign "nothing to commit" case (a re-delivery of
        content ``git diff --cached`` sees no change in) from a real commit
        failure, so only the former is swallowed. ``literal_paths`` are
        already ``:(literal)``-wrapped pathspecs.
        """
        rc, _out, _err = self._git(
            "diff", "--cached", "--quiet", "--", *literal_paths, check=False
        )
        return rc == 0

    def revert(self, path) -> None:
        """Discard working-tree + index changes for exactly ``path``.

        A path tracked in ``HEAD`` is restored from it (``git checkout
        HEAD -- <path>``), the per-file ``p4 revert`` analogue. A path that
        exists only in the index -- a delivery that ``add``-ed a brand-new
        file -- has no ``HEAD`` version to check out (git fails that
        pathspec with "did not match"), so it is unstaged and the working
        file is removed instead, matching ``p4 revert`` on a newly opened
        add and ``NullVcs``'s no-op-on-nothing-to-revert stance. Never
        wildcards -- the pathspec is a literal.
        """
        rel = self._rel(path)
        literal = self._literal(rel)
        if self._tracked_at_head(rel):
            self._git("checkout", "HEAD", "--", literal)
            return
        self._git("reset", "-q", "HEAD", "--", literal, check=False)
        target = Path(self.repo_root) / rel if not Path(rel).is_absolute() else Path(rel)
        if target.exists():
            target.unlink()

    def _tracked_at_head(self, rel: str) -> bool:
        """True when ``rel`` (a repo-relative path) exists in the ``HEAD`` tree.

        ``git cat-file -e <rev>:<path>`` addresses a blob by object path, not
        a pathspec, so no ``:(literal)`` wrapping applies here.
        """
        rc, _out, _err = self._git("cat-file", "-e", f"HEAD:{rel}", check=False)
        return rc == 0

    def delete_if_empty(self, changeset: GitChangeset) -> None:
        """No-op: an empty changeset was never committed, so nothing to delete."""


__all__ = ["GitVcs", "GitChangeset", "GitVcsError", "GitRunner"]
