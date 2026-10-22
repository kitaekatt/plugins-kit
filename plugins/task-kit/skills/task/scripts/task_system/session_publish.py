"""Publishing this machine's working-session files through git.

One file per machine per physical task root: ``<task root>/.sessions/<host id>.yaml``. This module commits that
file, then fetches, integrates and pushes it, and nothing else. It never rewrites history (no force, rebase,
reset, stash or branch switch) and never pushes a path that is not this machine's session file.

The order is load-bearing (plan "Git publication", R2-1): **commit before any network access**. From the moment
the commit succeeds the record is committed whatever the network does, so an offline machine has a committed,
unpublished record that every later :func:`publish` drains, never an uncommitted edit that a failed fetch could
strand. Each network attempt then:

1. fetches the upstream (a failure stops);
2. refuses when any local-only (unpushed, non-merge) commit touches a path that is not this machine's session file;
3. fast-forwards, or merges, the upstream into HEAD (an incoming change to any file is integrated like a pull;
   a merge git refuses leaves no merge state, a merge that stops on conflicts is aborted);
4. pushes ``HEAD`` to the upstream branch only when the net diff from the upstream names only this machine's
   session files. A non-fast-forward rejection is the expected race: the next attempt refetches and integrates.
   Attempts are bounded (:data:`MAX_ATTEMPTS` within :data:`WINDOW_S` seconds).

Library API (every function is offline except :func:`publish`): :func:`session_file`, :func:`preflight`,
:func:`pending`, :func:`publish`. Every :func:`publish` outcome, failures included, is the returned
:class:`PublishResult`; the caller reports it (the CLI to its output, the SessionStart hook to its failure log).
The data directory keeps only the per-repository lock file (information-free), never a record of outcomes.
"""

from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from . import procspawn
from .datadir import resolve_data_dir
from .oslock import FileLock, LockTimeout
from .resolve import LOCATION_DEV_TASKS, LOCATION_TMP

SESSIONS_DIRNAME = ".sessions"
SESSION_SUFFIX = ".yaml"
SESSIONS_DATA_SUBDIR = "sessions"

MAX_ATTEMPTS = 3
WINDOW_S = 60.0
NETWORK_TIMEOUT_S = 20.0
LOCAL_TIMEOUT_S = 30.0
LOCK_WAIT_S = 20.0

# Outcomes (PublishResult.status).
PUBLISHED = "published"
NOTHING_PENDING = "nothing-pending"
DISABLED = "disabled"
REFUSED = "refused"
BUSY = "busy"
NOT_COMMITTED = "not-committed"
FETCH_FAILED = "fetch-failed"
FOREIGN_LOCAL = "foreign-local-commits"
INTEGRATE_FAILED = "integrate-failed"
FOREIGN_DIFF = "foreign-diff"
PUSH_FAILED = "push-failed"
EXHAUSTED = "retries-exhausted"

KIND_LOCAL = "local"
KIND_DURABLE = "durable"
KIND_DISABLED = "disabled"

CANNOT_SEE = (
    "other machines cannot see it until this machine's next publish (`task sessions publish`)"
)
_TIMED_OUT = -9999
# The push race, in its two spellings: the client sees the remote moved ("[rejected] ... fetch first"), or two
# pushes reach the remote together and the loser fails the ref update ("incorrect old value provided").
# A hook's "[remote rejected] ... declined" is neither.
_RACE = re.compile(r"^\s*!\s*\[rejected\]|incorrect old value provided|cannot lock ref", re.MULTILINE)
_REPO_ENV_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR")


@dataclass(frozen=True)
class Preflight:
    """Whether a task root may carry working-session records, and why not."""

    ok: bool
    kind: str  # KIND_DURABLE, KIND_LOCAL (tmp: machine-local, no git) or KIND_DISABLED
    repo_root: Path | None
    reason: str = ""


@dataclass(frozen=True)
class PublishResult:
    """One publish outcome. ``message`` is the fragment a caller appends after ``registered ...;``."""

    status: str
    message: str
    committed: bool  # every session file of this machine in the repository is committed
    published: bool  # the upstream has this machine's session files
    attempts: int
    files: tuple[str, ...]  # session files this call committed
    repo_root: Path | None


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def timed_out(self) -> bool:
        return self.returncode == _TIMED_OUT


# --------------------------------------------------------------------------- naming


def host_of(machine) -> str:
    """The host id of a :class:`machine.Machine` or of a plain host-id string."""
    host = machine if isinstance(machine, str) else machine.host
    if not host or "/" in host or "\\" in host or host in (".", ".."):
        raise ValueError(f"not a host id: {host!r}")
    return host


def session_file(task_root: Path, machine) -> Path:
    """``<task root>/.sessions/<host id>.yaml``: this machine's file in a physical task root."""
    return Path(task_root) / SESSIONS_DIRNAME / (host_of(machine) + SESSION_SUFFIX)


def is_session_file(rel_path: str, machine) -> bool:
    """True for a repository-relative path that is this machine's session file, at any depth."""
    parts = rel_path.replace("\\", "/").split("/")
    return len(parts) >= 2 and parts[-2] == SESSIONS_DIRNAME and parts[-1] == host_of(machine) + SESSION_SUFFIX


def _pathspec(machine) -> str:
    return f":(glob)**/{SESSIONS_DIRNAME}/{host_of(machine)}{SESSION_SUFFIX}"


# --------------------------------------------------------------------------- running git


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _REPO_ENV_VARS}
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", LC_ALL="C", LANGUAGE="C")
    return env


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        proc.kill()


def _run(cmd: Sequence[str], *, cwd: Path, timeout: float) -> GitResult:
    """Run one command with stdin closed and a bound; a timeout kills the whole process group."""
    proc = subprocess.Popen(
        list(cmd), cwd=str(cwd), env=_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        **procspawn.hidden(windows=procspawn.WINDOWS),
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        out, err = proc.communicate()
        return GitResult(_TIMED_OUT, out or "", f"timed out after {timeout:g} s")
    return GitResult(proc.returncode, out, err)


def _git(args: Sequence[str], cwd: Path, *, timeout: float = LOCAL_TIMEOUT_S, optional_locks: bool = True) -> GitResult:
    """Every git invocation goes through here (tests wrap it to inject a race)."""
    prefix = ["git"] if optional_locks else ["git", "--no-optional-locks"]
    return _run([*prefix, *args], cwd=cwd, timeout=timeout)


def _last_line(result: GitResult) -> str:
    lines = [ln.strip() for ln in (result.stderr + "\n" + result.stdout).splitlines() if ln.strip()]
    lines = [ln for ln in lines if not ln.startswith("hint:")] or lines
    return lines[-1] if lines else f"git exited {result.returncode}"


def _toplevel(path: Path) -> Path | None:
    path = Path(path)
    if not path.is_dir():
        return None
    result = _git(["rev-parse", "--show-toplevel"], path.resolve(), optional_locks=False)
    top = result.stdout.strip()
    return Path(top).resolve() if result.ok and top else None


@dataclass(frozen=True)
class _Upstream:
    remote: str
    branch_ref: str  # refs/heads/<branch> on the remote
    tracking: str  # refs/remotes/<remote>/<branch>, the local copy of it


def _upstream(top: Path) -> tuple[_Upstream | None, str]:
    """The current branch's upstream, or ``(None, reason)``."""
    branch = _git(["symbolic-ref", "-q", "--short", "HEAD"], top, optional_locks=False)
    if not branch.ok or not branch.stdout.strip():
        return None, "HEAD is detached, so it has no upstream branch"
    name = branch.stdout.strip()
    remote = _git(["config", "--get", f"branch.{name}.remote"], top, optional_locks=False).stdout.strip()
    merge = _git(["config", "--get", f"branch.{name}.merge"], top, optional_locks=False).stdout.strip()
    tracking = _git(["rev-parse", "-q", "--verify", "--symbolic-full-name", "@{upstream}"], top,
                    optional_locks=False).stdout.strip()
    if not remote or not merge or not tracking:
        return None, f"branch {name} has no upstream"
    if remote == ".":
        return None, f"branch {name} tracks a local branch, not a remote"
    return _Upstream(remote, merge, tracking), ""


# --------------------------------------------------------------------------- reading state (offline, read-only)


def _status_paths(top: Path, machine) -> list[tuple[str, str]]:
    """``(status code, path)`` for this machine's session files that differ from HEAD or are untracked."""
    result = _git(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", _pathspec(machine)], top,
                  optional_locks=False)
    if not result.ok:
        raise RuntimeError(f"git status failed in {top}: {_last_line(result)}")
    found: list[tuple[str, str]] = []
    tokens = result.stdout.split("\0")
    index = 0
    while index < len(tokens):
        entry = tokens[index]
        index += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if "R" in code or "C" in code:
            index += 1  # the original path follows
        if is_session_file(path, machine):
            found.append((code, path))
    return found


def pending(repo_root: Path, machine) -> tuple[str, ...]:
    """This machine's session files in the repository that are uncommitted or differ from the upstream.

    Offline and read-only (``--no-optional-locks``): "differ from the upstream" is judged against the last
    fetch. Repository-relative POSIX paths, sorted. A directory that is not a git work tree has none.
    """
    top = _toplevel(Path(repo_root))
    if top is None:
        return ()
    names = {path for _code, path in _status_paths(top, machine)}
    upstream, _reason = _upstream(top)
    if upstream is not None:
        diff = _git(["diff", "--name-only", "-z", upstream.tracking, "HEAD", "--", _pathspec(machine)], top,
                    optional_locks=False)
        if diff.ok:
            names.update(p for p in diff.stdout.split("\0") if p and is_session_file(p, machine))
    return tuple(sorted(names))


def preflight(task_root: Path, *, machine=None, location: str | None = None, record: bool = True) -> Preflight:
    """Whether ``task_root`` can carry session records (decision 10), before anything is written.

    A ``tmp`` root is machine-local and never touches git. A durable root (``dev/tasks``) must be a git work
    tree, must not git-ignore its session file, and its branch must track an upstream; otherwise registration
    is refused with ``reason``. ``record=False`` is ``sessions.record: false``. ``location`` overrides the
    inference from the path (``.../dev/tasks`` is durable, ``.../tmp`` is local).
    """
    task_root = Path(task_root)
    if not record:
        return Preflight(False, KIND_DISABLED, None, "sessions.record is false")
    if location is None:
        tail = tuple(task_root.parts[-2:])
        location = LOCATION_DEV_TASKS if tail == ("dev", "tasks") else (
            LOCATION_TMP if task_root.name == LOCATION_TMP else LOCATION_DEV_TASKS)
    if location == LOCATION_TMP:
        return Preflight(True, KIND_LOCAL, None, "tmp is machine-local")
    if not task_root.is_dir():
        return Preflight(False, KIND_DURABLE, None, f"the task root {task_root} does not exist")
    top = _toplevel(task_root)
    if top is None:
        return Preflight(False, KIND_DURABLE, None, f"{task_root} is not inside a git work tree")
    probe = Path(task_root).resolve() / SESSIONS_DIRNAME / ((host_of(machine) if machine else "host") + SESSION_SUFFIX)
    ignored = _git(["check-ignore", "--no-index", "-q", "--", str(probe)], top, optional_locks=False)
    if ignored.ok:
        return Preflight(False, KIND_DURABLE, top, f"git ignores {SESSIONS_DIRNAME}/ in {task_root}")
    if ignored.returncode != 1:
        return Preflight(False, KIND_DURABLE, top, f"git check-ignore failed: {_last_line(ignored)}")
    upstream, reason = _upstream(top)
    if upstream is None:
        return Preflight(False, KIND_DURABLE, top, reason)
    return Preflight(True, KIND_DURABLE, top)


# --------------------------------------------------------------------------- publishing


def lock_path(top: Path, data_dir: Path) -> Path:
    """The per-repository lock, in the user-scoped data directory."""
    key = hashlib.sha256(os.path.normcase(str(Path(top).resolve())).encode("utf-8")).hexdigest()[:16]
    return Path(data_dir) / SESSIONS_DATA_SUBDIR / "locks" / f"repo-{key}.lock"


def _result(status: str, message: str, *, committed: bool, published: bool, attempts: int,
            files: Sequence[str], top: Path | None) -> PublishResult:
    return PublishResult(status, message, committed, published, attempts, tuple(files), top)


def _commit_session_files(top: Path, machine) -> tuple[list[str], str | None]:
    """Commit this machine's changed session files; ``(committed paths, error line or None)``."""
    changed = _status_paths(top, machine)
    if not changed:
        return [], None
    paths = sorted({path for _code, path in changed})
    untracked = sorted({path for code, path in changed if code == "??"})
    if untracked:
        added = _git(["add", "--", *untracked], top)
        if not added.ok:
            return [], _last_line(added)
    # A pathspec commit: other staged content in the index is left alone.
    committed = _git(["commit", "-q", "-m", f"task-kit sessions: {host_of(machine)}", "--", *paths], top)
    if not committed.ok:
        return [], _last_line(committed)
    return paths, None


def _foreign_local_paths(top: Path, upstream: _Upstream, machine) -> list[str]:
    """Paths touched by local-only non-merge commits that are not this machine's session file."""
    listed = _git(["rev-list", "--no-merges", f"{upstream.tracking}..HEAD"], top, optional_locks=False)
    if not listed.ok:
        raise RuntimeError(f"git rev-list failed: {_last_line(listed)}")
    foreign: list[str] = []
    for sha in listed.stdout.split():
        touched = _git(["diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "--root", sha], top,
                       optional_locks=False)
        if not touched.ok:
            raise RuntimeError(f"git diff-tree failed: {_last_line(touched)}")
        foreign.extend(p for p in touched.stdout.split("\0") if p and not is_session_file(p, machine))
    return sorted(set(foreign))


def _integrate(top: Path, upstream: _Upstream) -> str | None:
    """Bring the upstream into HEAD; an error line, or None."""
    if _git(["merge-base", "--is-ancestor", upstream.tracking, "HEAD"], top, optional_locks=False).ok:
        return None
    if _git(["merge-base", "--is-ancestor", "HEAD", upstream.tracking], top, optional_locks=False).ok:
        merged = _git(["merge", "--ff-only", "-q", upstream.tracking], top)
    else:
        merged = _git(["merge", "--no-edit", "-q", upstream.tracking], top)
    if merged.ok:
        return None
    if _git(["rev-parse", "-q", "--verify", "MERGE_HEAD"], top, optional_locks=False).ok:
        _git(["merge", "--abort"], top)
    return _last_line(merged)


def _publish_locked(top: Path, machine, window_s: float, network_timeout_s: float) -> PublishResult:
    files, error = _commit_session_files(top, machine)
    if error is not None:
        return _result(NOT_COMMITTED, f"not committed: {error}", committed=False, published=False, attempts=0,
                       files=(), top=top)
    if not files and not pending(top, machine):
        return _result(NOTHING_PENDING, "nothing to publish", committed=True, published=True, attempts=0,
                       files=(), top=top)
    upstream, reason = _upstream(top)
    if upstream is None:
        return _result(REFUSED, f"committed; not published: {reason}; {CANNOT_SEE}", committed=True,
                       published=False, attempts=0, files=files, top=top)

    deadline = time.monotonic() + window_s
    last = ""
    attempts = 0
    while attempts < MAX_ATTEMPTS:
        remaining = deadline - time.monotonic()
        if attempts and remaining <= 0:
            break
        attempts += 1
        bound = max(1.0, min(network_timeout_s, remaining))

        fetched = _git(["fetch", "-q", upstream.remote], top, timeout=bound)
        if not fetched.ok:
            return _result(FETCH_FAILED, f"committed; not published: fetch failed: {_last_line(fetched)}; {CANNOT_SEE}",
                           committed=True, published=False, attempts=attempts, files=files, top=top)

        foreign = _foreign_local_paths(top, upstream, machine)
        if foreign:
            return _result(
                FOREIGN_LOCAL,
                "committed; not published: unpushed commits outside this machine's session records "
                f"({', '.join(foreign[:3])}{', ...' if len(foreign) > 3 else ''}); {CANNOT_SEE}",
                committed=True, published=False, attempts=attempts, files=files, top=top)

        refused = _integrate(top, upstream)
        if refused is not None:
            return _result(INTEGRATE_FAILED, f"committed; not published: {refused}; {CANNOT_SEE}", committed=True,
                           published=False, attempts=attempts, files=files, top=top)

        diff = _git(["diff", "--name-only", "-z", upstream.tracking, "HEAD"], top, optional_locks=False)
        if not diff.ok:
            raise RuntimeError(f"git diff failed: {_last_line(diff)}")
        names = [p for p in diff.stdout.split("\0") if p]
        stray = [p for p in names if not is_session_file(p, machine)]
        if stray:
            return _result(
                FOREIGN_DIFF,
                "committed; not published: the push would carry files outside this machine's session records "
                f"({', '.join(stray[:3])}{', ...' if len(stray) > 3 else ''}); {CANNOT_SEE}",
                committed=True, published=False, attempts=attempts, files=files, top=top)
        if not names:
            return _result(PUBLISHED, "published", committed=True, published=True, attempts=attempts, files=files,
                           top=top)

        pushed = _git(["push", "-q", upstream.remote, f"HEAD:{upstream.branch_ref}"], top, timeout=bound)
        if pushed.ok:
            return _result(PUBLISHED, "published", committed=True, published=True, attempts=attempts, files=files,
                           top=top)
        last = _last_line(pushed)
        if not _RACE.search(pushed.stderr):
            return _result(PUSH_FAILED, f"committed; not published: push failed: {last}; {CANNOT_SEE}",
                           committed=True, published=False, attempts=attempts, files=files, top=top)
        # A non-fast-forward rejection is the expected race: refetch, integrate, push again.

    return _result(EXHAUSTED, f"committed; not published after {attempts} attempts: {last}; {CANNOT_SEE}",
                   committed=True, published=False, attempts=attempts, files=files, top=top)


def publish(repo_root: Path, machine, *, data_dir: Path | None = None, record: bool = True,
            window_s: float = WINDOW_S, network_timeout_s: float = NETWORK_TIMEOUT_S,
            lock_wait_s: float = LOCK_WAIT_S) -> PublishResult:
    """Commit this machine's session files in ``repo_root``'s repository, then publish them.

    Holds the per-repository lock for the whole call. Never raises for a git or network condition: those are
    :class:`PublishResult` statuses; nothing is written to the data directory but the lock file. ``data_dir``
    defaults to the plugin's bootstrap data directory.
    """
    data_dir = Path(data_dir) if data_dir is not None else resolve_data_dir()
    top = _toplevel(Path(repo_root))
    if not record:
        result = _result(DISABLED, "sessions.record is false", committed=False, published=False, attempts=0,
                         files=(), top=top)
    elif top is None:
        result = _result(REFUSED, f"not published: {repo_root} is not inside a git work tree", committed=False,
                         published=False, attempts=0, files=(), top=None)
    else:
        try:
            with FileLock(lock_path(top, data_dir), lock_wait_s):
                result = _publish_locked(top, machine, window_s, network_timeout_s)
        except LockTimeout:
            result = _result(
                BUSY, f"not committed: another publish holds the repository lock for {top}; the record "
                f"publishes at the next publish (`task sessions publish`)", committed=False, published=False,
                attempts=0, files=(), top=top)
    return result
