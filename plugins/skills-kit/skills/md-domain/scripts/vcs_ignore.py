#!/usr/bin/env python3
"""vcs_ignore.py -- does the PROJECT's version control exclude this path?

One question, three answers, and the third is the one that is easy to get
wrong:

  * git project    -> exclude what git's ignore rules cover
  * p4 project     -> exclude what p4's ignore rules cover
  * neither        -> exclude NOTHING

"Neither" must never be read as "exclude everything" or as "exclude the
built-in guesses". A directory tree outside version control has no ignore
information at all, and inventing some would silently drop a real subject.
Every failure path here therefore degrades to "no ignore information": git
absent, not a work tree, p4 unreachable, a malformed answer -- all of them
return an empty ignored-set rather than raising or guessing.

Two properties are load-bearing:

  * ``git check-ignore --no-index`` is REQUIRED. Plain ``check-ignore``
    consults the index first and reports a TRACKED path as NOT ignored,
    because exclude rules do not apply to tracked files. That is the opposite
    of the question asked here, which is whether the ignore RULES cover the
    path -- and the rule this module implements is that an ignored directory
    is out regardless of what happens to be tracked inside it. So there is
    deliberately NO second "and git tracks nothing in it" half; a caller that
    needs that conjunction (awesome-kit's task validator does, for a
    different question) must compose it itself.

  * The entry point is BATCHED. A tree walk asks about hundreds of paths, and
    one subprocess per path is the difference between a discovery step and a
    noticeable stall. ``ignored_paths`` takes a list and spends one
    subprocess; ``is_ignored`` is the one-path convenience built on it.

Stdlib-only, side-effect free, read-only: it runs query commands and writes
nothing.
"""

import os
import subprocess
from pathlib import Path

GIT = "git"
P4 = "p4"

_TIMEOUT = 30

# Detection is cached per directory: a tree walk asks the same question for
# every sibling, and the answer cannot change during one run. `clear_cache()`
# exists for tests, which create and destroy repositories in one process.
_VCS_CACHE: dict[str, str | None] = {}
_TOPLEVEL_CACHE: dict[str, Path | None] = {}


def clear_cache() -> None:
    """Forget cached VCS detections. For tests."""
    _VCS_CACHE.clear()
    _TOPLEVEL_CACHE.clear()


def detect_vcs(directory: Path) -> str | None:
    """Return ``"git"``, ``"p4"``, or None for the project containing `directory`.

    git wins when both look plausible: a git work tree is positively
    identified by git itself, while the p4 signal below is a filesystem
    heuristic.
    """
    key = str(Path(directory))
    if key in _VCS_CACHE:
        return _VCS_CACHE[key]
    vcs = _detect_uncached(Path(directory))
    _VCS_CACHE[key] = vcs
    return vcs


def _detect_uncached(directory: Path) -> str | None:
    if _git_toplevel(directory) is not None:
        return GIT
    if _p4_config_above(directory) is not None:
        return P4
    return None


def _git_toplevel(start: Path) -> Path | None:
    """Cached: detection and the root-path guard in `_git_ignored` both want it,
    and a tree walk asks per directory."""
    key = str(Path(start))
    if key in _TOPLEVEL_CACHE:
        return _TOPLEVEL_CACHE[key]
    top = _git_toplevel_uncached(Path(start))
    _TOPLEVEL_CACHE[key] = top
    return top


def _git_toplevel_uncached(start: Path) -> Path | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False, timeout=_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return Path(out) if out else None


def _p4_config_above(start: Path) -> Path | None:
    """The nearest Perforce config file at or above `start`, or None.

    UNVERIFIED HEURISTIC, deliberately chosen for its failure mode rather than
    its precision. The obvious alternative -- shelling out to ``p4 info`` --
    contacts a server, so on a machine with P4PORT set but no reachable server
    every non-git directory would pay a timeout, and a wrong answer would be
    indistinguishable from a slow one. A config-file marker is a pure filesystem
    read: it can only ever be wrong in the direction of reporting "no p4
    project", which degrades to excluding nothing.

    ``P4CONFIG`` names the file when it is set, which is Perforce's own
    mechanism; the fallbacks are the conventional names this fleet uses.
    """
    names = [n for n in [os.environ.get("P4CONFIG")] if n]
    if not names:
        names = [".p4config", ".p4config.txt", "p4config.txt"]
    current = Path(start)
    while True:
        for name in names:
            candidate = current / name
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                pass
        if current == current.parent:
            return None
        current = current.parent


def ignored_paths(
    paths,
    root: Path | None = None,
    vcs: str | None = None,
) -> set[Path]:
    """The subset of `paths` the project's ignore rules cover.

    Batched: one subprocess for the whole list. `root` is the directory the
    query runs from (defaults to the first path's directory); `vcs` short-cuts
    detection when the caller already knows the answer. An empty return means
    "nothing here is ignored" OR "no ignore information available" -- the two
    are deliberately indistinguishable, because both must exclude nothing.
    """
    items = [Path(p) for p in paths]
    if not items:
        return set()
    if root is None:
        first = items[0]
        root = first if first.is_dir() else first.parent
    if vcs is None:
        vcs = detect_vcs(root)
    if vcs == GIT:
        return _git_ignored(items, Path(root))
    if vcs == P4:
        return _p4_ignored(items, Path(root))
    return set()


def is_ignored(path: Path, root: Path | None = None, vcs: str | None = None) -> bool:
    """True when the project's ignore rules cover `path`.

    One-path convenience. Prefer `ignored_paths` in a loop -- this spends a
    subprocess per call.
    """
    return Path(path) in ignored_paths([path], root=root, vcs=vcs)


def _norm(path: Path) -> str:
    """Comparison key: git prints POSIX separators and Windows folds case."""
    return os.path.normcase(path.as_posix())


def _git_ignored(items: list[Path], root: Path) -> set[Path]:
    """`git check-ignore --no-index --stdin -z` over the whole list.

    Paths are sent as POSIX strings and git echoes back exactly what it was
    given, so the reply is matched against the sent form rather than
    re-derived. Windows separators are normalized on the way in for that
    reason: `D:/x` round-trips, `D:\\x` need not.
    """
    # NEVER ask about the worktree ROOT itself. A repository cannot be excluded
    # from itself, and git's answer there is not merely uninteresting, it is
    # WRONG: with --no-index the root's repo-relative path is empty and matches a
    # blank line in .gitignore, so a perfectly ordinary repository reports its
    # own root as ignored. Observed on git 2.55.0.windows.3 -- `git check-ignore
    # --no-index -v -- .` at one worktree root printed `.gitignore:55:` with an
    # empty pattern, line 55 being blank. Truncating the file moved the match to
    # whichever blank line was last, which is what identifies it as a degenerate
    # empty-path match rather than a real rule.
    top = _git_toplevel(root)
    top_key = None if top is None else _norm(top)

    sent: dict[str, Path] = {}
    for item in items:
        if top_key is not None and _norm(item) == top_key:
            continue
        sent[item.as_posix()] = item
    if not sent:
        return set()
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--no-index", "--stdin", "-z"],
            input="\0".join(sent),
            capture_output=True, text=True, check=False, timeout=_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    # 0: some ignored (echoed on stdout). 1: none ignored. Anything else
    # (128 = not a repository, bad usage) is NO INFORMATION, not "none".
    if proc.returncode not in (0, 1):
        return set()
    return {sent[line] for line in proc.stdout.split("\0") if line in sent}


def _p4_ignored(items: list[Path], root: Path) -> set[Path]:
    """`p4 ignores -i <path>...` over the whole list.

    UNVERIFIED. `p4 ignores -i` is the documented way to ask whether a path
    would be ignored, but neither its exact output wording nor its behaviour
    on a batch of paths has been confirmed against a live server here. It is
    therefore written to fail CLOSED-ON-EXCLUSION and open-on-subject: any
    non-zero exit, missing binary, timeout, or line it cannot parse yields no
    exclusion at all. A wrong guess must never remove a real coverage subject.

    Expected shape, one line per path: ``<path> ignored`` /
    ``<path> not ignored``. Only the affirmative form counts, and "not
    ignored" is matched first so the substring cannot be misread.
    """
    try:
        proc = subprocess.run(
            ["p4", "-d", str(root), "ignores", "-i", *[str(p) for p in items]],
            capture_output=True, text=True, check=False, timeout=_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if proc.returncode != 0:
        return set()
    by_str = {str(p): p for p in items}
    by_posix = {p.as_posix(): p for p in items}
    ignored: set[Path] = set()
    for line in proc.stdout.splitlines():
        text = line.strip()
        if not text or "not ignored" in text:
            continue
        if not text.endswith("ignored"):
            continue
        name = text[: -len("ignored")].strip()
        match = by_str.get(name) or by_posix.get(Path(name).as_posix())
        if match is not None:
            ignored.add(match)
    return ignored
