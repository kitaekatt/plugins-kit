"""Safe fast-forward of the project checkout (layered manifest `project_git_pull`).

Bootstrap updates the project to its upstream only when that cannot conflict:
a fast-forward, with no operation in progress and no local change on a path the
update touches. Anything else leaves the checkout exactly as it was and reports
WHY, as one of a fixed set of outcome codes decided from git exit codes and
plumbing output -- never by reading git's human-facing messages -- so nobody
has to infer the reason from a log.

Local changes that touch none of the incoming paths do not block: git leaves
them in place across a fast-forward, so no conflict is possible.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

from .subprocess_run import run_captured

# Outcome codes. The code is the classification; the summary adds the counts,
# paths or reason that make it actionable.
UPDATED = "updated"
CURRENT = "current"
GIT_UNAVAILABLE = "git-unavailable"
NOT_A_REPOSITORY = "not-a-repository"
DETACHED_HEAD = "detached-head"
NO_UPSTREAM = "no-upstream"
OPERATION_IN_PROGRESS = "operation-in-progress"
FETCH_FAILED = "fetch-failed"
DIVERGED = "diverged"
LOCAL_CHANGES_OVERLAP = "local-changes-overlap"
GATE_DECLINED = "gate-declined"
GATE_FAILED = "gate-failed"
UNSUPPORTED_FILTER = "unsupported-filter"
LFS_UNAVAILABLE = "lfs-unavailable"
LFS_FETCH_FAILED = "lfs-fetch-failed"
FF_REFUSED = "ff-refused"

#: Nothing to attempt, and each is an ordinary development state (a new local
#: branch, a bisect, a scratch directory, a fresh machine whose tools phase has
#: not installed git yet): logged, never shown, or every session in that state
#: would repeat the same line.
NOT_APPLICABLE = frozenset({CURRENT, GIT_UNAVAILABLE, NOT_A_REPOSITORY, DETACHED_HEAD, NO_UPSTREAM})

#: The gate's "do not update now" exit status (sysexits EX_TEMPFAIL). Any other
#: non-zero exit is a gate that failed to answer, which also blocks.
GATE_DECLINE_EXIT = 75

GIT_TIMEOUT = 30
FETCH_TIMEOUT = 60
LFS_FETCH_TIMEOUT = 600
#: The merge runs the project's post-merge hooks, exactly as a manual pull
#: would, and a hook may build native code.
MERGE_TIMEOUT = 1200
DEFAULT_GATE_TIMEOUT = 120
MAX_GATE_TIMEOUT = 3600

#: Git state files whose presence means an operation is mid-flight; order is
#: the reporting order.
_OPERATION_MARKERS = (
    ("rebase-merge", "rebase in progress"),
    ("rebase-apply", "rebase in progress"),
    ("MERGE_HEAD", "merge in progress"),
    ("CHERRY_PICK_HEAD", "cherry-pick in progress"),
    ("REVERT_HEAD", "revert in progress"),
    ("sequencer", "cherry-pick or revert sequence in progress"),
    ("BISECT_LOG", "bisect in progress"),
)

_OVERLAP_SHOWN = 3
_GATE_REASON_MAX = 200

_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}


@dataclass(frozen=True)
class PullConfig:
    enabled: bool
    gate: Optional[str] = None
    gate_timeout: int = DEFAULT_GATE_TIMEOUT


@dataclass(frozen=True)
class PullResult:
    outcome: str
    summary: str
    #: Log-only diagnostics: every overlapping path, git's own output.
    detail: str = ""
    #: HEAD moved, so anything read from the old tree (the manifest) is stale.
    moved: bool = False

    @property
    def notice(self) -> Optional[str]:
        """The one line the user sees, or None for a not-applicable outcome."""
        if self.outcome in NOT_APPLICABLE:
            return None
        if self.outcome == UPDATED:
            return f"project updated: {self.summary}"
        return f"project not updated [{self.outcome}]: {self.summary}"

    @property
    def log_line(self) -> str:
        return f"project_git_pull: {self.outcome} - {self.summary}"


def parse_config(value) -> tuple[Optional[PullConfig], Optional[str]]:
    """(config, None) for a valid declaration, (None, reason) otherwise.

    `true` enables a plain safe pull; an object may add a gate. `false`, or an
    object with `"enabled": false` in a higher-priority layer, disables it.
    """
    if value is True:
        return PullConfig(enabled=True), None
    if value is False:
        return PullConfig(enabled=False), None
    if not isinstance(value, dict):
        return None, "must be true, false, or an object"
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        return None, "'enabled' must be true or false"
    gate = value.get("gate")
    if gate is not None and (not isinstance(gate, str) or not gate.strip()):
        return None, "'gate' must be a non-empty command string"
    timeout = value.get("gate_timeout", DEFAULT_GATE_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_GATE_TIMEOUT:
        return None, f"'gate_timeout' must be whole seconds from 1 to {MAX_GATE_TIMEOUT}"
    return PullConfig(enabled=enabled, gate=gate, gate_timeout=timeout), None


def _git(repo: str, *args: str, timeout: int = GIT_TIMEOUT) -> tuple[Optional[int], str, str]:
    """(returncode, stdout, stderr); returncode None when git timed out or could not start."""
    try:
        return run_captured(["git", "-C", repo, *args], timeout=timeout,
                            env={**os.environ, **_GIT_ENV})
    except subprocess.TimeoutExpired:
        return None, "", f"timed out after {timeout}s"
    except OSError as exc:
        return None, "", str(exc)


_URL_CREDENTIALS = re.compile(r"\b([a-z][a-z0-9+.-]*://)[^/\s@]+@", re.IGNORECASE)


def _redact(text: str) -> str:
    """Drop credentials embedded in a remote URL: git echoes the URL in its
    errors, and both the notice and the log would otherwise carry a token."""
    return _URL_CREDENTIALS.sub(r"\1***@", text or "")


def _short_cause(output: str) -> str:
    from .marketplace_lifecycle import summarize_cli_error
    return summarize_cli_error(_redact(output))


def _operation_in_progress(repo: str) -> Optional[str]:
    names = [name for name, _ in _OPERATION_MARKERS]
    args = [arg for name in names for arg in ("--git-path", name)]
    rc, out, _ = _git(repo, "rev-parse", *args)
    if rc == 0:
        paths = out.splitlines()
        for (name, kind), path in zip(_OPERATION_MARKERS, paths):
            full = path if os.path.isabs(path) else os.path.join(repo, path)
            if os.path.exists(full):
                return kind
    rc, out, _ = _git(repo, "ls-files", "-u", "-z")
    if rc == 0 and out.strip("\0"):
        return "unresolved merge conflicts"
    return None


def _nul_fields(out: str) -> list[str]:
    return [f for f in out.split("\0") if f]


def _incoming(repo: str, head: str, target: str) -> Optional[list[tuple[str, str]]]:
    """[(status, path)] the update changes, renames split into delete + add."""
    rc, out, _ = _git(repo, "diff", "--name-status", "--no-renames", "-z", head, target)
    if rc != 0:
        return None
    fields = _nul_fields(out)
    return [(fields[i][:1], fields[i + 1]) for i in range(0, len(fields) - 1, 2)]


def _local_paths(repo: str) -> Optional[set[str]]:
    """Every path with a staged, unstaged or untracked change (both sides of a rename)."""
    rc, out, _ = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if rc != 0:
        return None
    paths: set[str] = set()
    fields = out.split("\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.add(entry[3:])
        if entry[0] in "RC" and i < len(fields):
            paths.add(fields[i])
            i += 1
    return paths


def _overlap(repo: str, incoming: list[tuple[str, str]], local: set[str], fold: bool) -> list[str]:
    """Incoming paths a fast-forward would have to overwrite.

    A local change on an incoming path overlaps. So does anything already on
    disk where the update ADDS a file -- including an ignored file, which git
    treats as expendable and would overwrite silently -- and a non-directory
    where an added file needs a parent directory.
    """
    key = (lambda p: p.casefold()) if fold else (lambda p: p)
    local_keys = {key(p) for p in local}
    hits: list[str] = []
    for status, path in incoming:
        if key(path) in local_keys:
            hits.append(path)
            continue
        if status != "A":
            continue
        full = os.path.join(repo, *path.split("/"))
        if os.path.lexists(full):
            hits.append(path)
            continue
        parent = os.path.dirname(path)
        while parent:
            full_parent = os.path.join(repo, *parent.split("/"))
            if os.path.lexists(full_parent) and not os.path.isdir(full_parent):
                hits.append(path)
                break
            parent = os.path.dirname(parent)
    return hits


def _sanitize_reason(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return "the gate gave no reason"
    printable = "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in lines[-1])
    if len(printable) > _GATE_REASON_MAX:
        printable = printable[:_GATE_REASON_MAX - 3].rstrip() + "..."
    return printable


def _run_gate(repo: str, config: PullConfig, env: dict[str, str]) -> Optional[PullResult]:
    """None when the gate allows the update; the blocking result otherwise."""
    from .tool_check import resolve_bash
    if sys.platform == "win32" or "MSYSTEM" in os.environ:
        bash = resolve_bash()
        if not bash:
            return PullResult(GATE_FAILED, "bash was not found to run the gate")
        argv: object = [bash, "-c", config.gate]
    else:
        argv = ["/bin/sh", "-c", config.gate]
    try:
        proc = subprocess.run(argv, cwd=repo, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=config.gate_timeout,
                              stdin=subprocess.DEVNULL, env={**os.environ, **_GIT_ENV, **env})
    except subprocess.TimeoutExpired:
        return PullResult(GATE_FAILED, f"the gate timed out after {config.gate_timeout}s")
    except OSError as exc:
        return PullResult(GATE_FAILED, "the gate could not be started", detail=str(exc))
    if proc.returncode == 0:
        return None
    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    if proc.returncode == GATE_DECLINE_EXIT:
        return PullResult(GATE_DECLINED, _sanitize_reason(_redact(proc.stdout)), detail=_redact(output).strip())
    return PullResult(GATE_FAILED, f"the gate exited {proc.returncode}", detail=_redact(output).strip())


def _filters(repo: str, target: str, paths: list[str]) -> Optional[dict[str, str]]:
    """{path: filter driver} for the paths that have one, read from the TARGET's
    attributes (a .gitattributes the update itself changes counts). None when
    the attributes could not be read."""
    if not paths:
        return {}
    # Paths on stdin, not argv: a large update would overflow the command line.
    try:
        proc = subprocess.run(
            ["git", "-C", repo, "check-attr", "-z", "--stdin", f"--source={target}", "filter"],
            input="\0".join(paths) + "\0", capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=GIT_TIMEOUT, env={**os.environ, **_GIT_ENV})
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout
    fields = out.split("\0")
    found: dict[str, str] = {}
    for i in range(0, len(fields) - 2, 3):
        path, value = fields[i], fields[i + 2]
        if value not in ("unspecified", "unset", ""):
            found[path] = value
    return found


def pull_project(project_dir: str, config: PullConfig) -> PullResult:
    """Fast-forward the checkout containing `project_dir` when that is safe."""
    if shutil.which("git") is None:
        return PullResult(GIT_UNAVAILABLE, "git is not installed")
    rc, out, _ = _git(project_dir, "rev-parse", "--show-toplevel")
    if rc != 0 or not out.strip():
        return PullResult(NOT_A_REPOSITORY, "the project directory is not in a git checkout")
    repo = out.strip()

    # Before the branch checks: a rebase detaches HEAD, and "rebase in
    # progress" is the answer the user can act on.
    kind = _operation_in_progress(repo)
    if kind:
        return PullResult(OPERATION_IN_PROGRESS, kind)

    rc, out, _ = _git(repo, "symbolic-ref", "-q", "--short", "HEAD")
    if rc != 0:
        return PullResult(DETACHED_HEAD, "HEAD is not on a branch")
    branch = out.strip()

    rc, out, _ = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if rc != 0:
        return PullResult(NO_UPSTREAM, f"{branch} has no upstream branch")
    upstream = out.strip()
    rc, out, _ = _git(repo, "config", "--get", f"branch.{branch}.remote")
    remote = out.strip() if rc == 0 else ""

    if remote and remote != ".":
        rc, _, err = _git(repo, "fetch", "--quiet", remote, timeout=FETCH_TIMEOUT)
        if rc is None:
            return PullResult(FETCH_FAILED, f"fetching {remote} {err}")
        if rc != 0:
            return PullResult(FETCH_FAILED, f"fetching {remote} failed ({_short_cause(err)})",
                              detail=_redact(err).strip())

    rc_head, head, _ = _git(repo, "rev-parse", "--verify", "HEAD^{commit}")
    rc_target, target, _ = _git(repo, "rev-parse", "--verify", "@{upstream}^{commit}")
    rc, counts, _ = _git(repo, "rev-list", "--left-right", "--count", "HEAD...@{upstream}")
    if rc_head != 0 or rc_target != 0 or rc != 0:
        return PullResult(FF_REFUSED, f"could not compare {branch} with {upstream}")
    head, target = head.strip(), target.strip()
    ahead, behind = (int(n) for n in counts.split())
    if behind == 0:
        extra = f"; {ahead} local commit(s) not on {upstream}" if ahead else ""
        return PullResult(CURRENT, f"{branch} is up to date with {upstream}{extra}")
    if ahead:
        return PullResult(DIVERGED, f"{branch} has {ahead} local commit(s) not on {upstream}, "
                                    f"which has {behind} new commit(s); a fast-forward is impossible")

    incoming = _incoming(repo, head, target)
    local = _local_paths(repo)
    if incoming is None or local is None:
        return PullResult(FF_REFUSED, "could not list the incoming or local changes")
    rc, out, _ = _git(repo, "config", "--bool", "core.ignorecase")
    hits = _overlap(repo, incoming, local, fold=(rc == 0 and out.strip() == "true"))
    if hits:
        shown = ", ".join(hits[:_OVERLAP_SHOWN])
        more = f" and {len(hits) - _OVERLAP_SHOWN} more" if len(hits) > _OVERLAP_SHOWN else ""
        return PullResult(LOCAL_CHANGES_OVERLAP,
                          f"{len(hits)} local file(s) would be overwritten by {behind} incoming "
                          f"commit(s): {shown}{more}",
                          detail="\n".join(hits))

    if config.gate:
        blocked = _run_gate(repo, config, {
            "BOOTSTRAP_PULL_FROM": head,
            "BOOTSTRAP_PULL_TO": target,
            "BOOTSTRAP_PULL_BRANCH": branch,
            "BOOTSTRAP_PULL_UPSTREAM": upstream,
        })
        if blocked:
            return blocked

    # A smudge filter that fails mid-checkout makes git abort with HEAD
    # unmoved but tracked files already deleted from the working tree (probed:
    # `git merge --ff-only` under a failing required filter leaves " D <file>").
    # So every filtered incoming file must be known to check out before the
    # merge starts: Git LFS content is downloaded first, so its smudge needs no
    # network; any other filter cannot be verified and blocks.
    written = [path for status, path in incoming if status != "D"]
    filters = _filters(repo, target, written)
    if filters is None:
        return PullResult(FF_REFUSED, "could not read the update's git attributes")
    others = sorted(p for p, driver in filters.items() if driver != "lfs")
    if others:
        drivers = ", ".join(sorted({filters[p] for p in others}))
        return PullResult(UNSUPPORTED_FILTER,
                          f"{len(others)} incoming file(s) use a git filter bootstrap cannot "
                          f"pre-verify ({drivers}): {', '.join(others[:_OVERLAP_SHOWN])}",
                          detail="\n".join(others))
    if len(filters) > len(others):
        rc, _, err = _git(repo, "lfs", "version")
        if rc != 0:
            return PullResult(LFS_UNAVAILABLE, "the update needs Git LFS, which is not installed")
        if remote and remote != ".":
            rc, _, err = _git(repo, "lfs", "fetch", remote, target, timeout=LFS_FETCH_TIMEOUT)
            if rc is None:
                return PullResult(LFS_FETCH_FAILED, f"downloading Git LFS content {err}")
            if rc != 0:
                return PullResult(LFS_FETCH_FAILED,
                                  f"downloading Git LFS content failed ({_short_cause(err)})",
                                  detail=_redact(err).strip())

    rc, out, err = _git(repo, "merge", "--ff-only", "--quiet", target, timeout=MERGE_TIMEOUT)
    # HEAD, not the exit status, decides: a hook killed by the timeout after
    # HEAD moved still leaves the checkout updated.
    _, now, _ = _git(repo, "rev-parse", "--verify", "HEAD^{commit}")
    if now.strip() == target:
        note = "; a git hook did not finish" if rc != 0 else ""
        return PullResult(UPDATED, f"{branch} fast-forwarded {behind} commit(s) to {target[:7]}{note}",
                          detail=_redact(out + err).strip(), moved=True)
    why = err.strip() if rc is None else _short_cause(out + err)
    summary = f"git refused the fast-forward ({why})"
    # A refusal is not proof the tree is untouched (see the filter note above):
    # name anything the attempt changed that was clean before it.
    after = _local_paths(repo)
    touched = sorted(after - local) if after is not None else []
    if touched:
        summary += (f"; the attempt left {len(touched)} file(s) changed: "
                    f"{', '.join(touched[:_OVERLAP_SHOWN])}")
    return PullResult(FF_REFUSED, summary, detail=_redact(out + err).strip())
