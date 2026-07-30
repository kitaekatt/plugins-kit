"""Clone and refresh the private secrets repo.

Two rules shape this module, both learned from llm-scripting-kit's account
check: **never block a session on connectivity**, and **rate-limit the network
call** so the steady-state pass costs nothing. An offline laptop is a normal
state, not a fault; if a clone already exists, a failed fetch is a log line and
the pass proceeds on what is on disk.
"""

import os
import subprocess
import time
from pathlib import Path
from typing import List, Optional, Tuple

from . import SecretsError

CLONE_TIMEOUT = 60
FETCH_TIMEOUT = 15

# How often to talk to the remote at all. A rotated secret converges on the
# next pass after this window -- fine, because rotation is rare and the local
# copy stays valid until then. Set low enough that "I rotated it this morning"
# lands the same day.
REFRESH_COOLDOWN_SECONDS = 6 * 60 * 60


def _git(args: List[str], *, cwd: Optional[Path], timeout: int) -> Tuple[int, str]:
    env = dict(os.environ)
    # Never let git stop to ask for credentials inside a session-start pass:
    # it would hang the hook rather than fail it.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return (124, f"git {' '.join(args)} timed out after {timeout}s")
    except OSError as e:
        return (127, f"could not run git: {e}")
    return (proc.returncode, proc.stdout.decode("utf-8", "replace").strip())


def is_clone(path: Path) -> bool:
    return (path / ".git").is_dir()


def clone(repo_url: str, dest: Path) -> None:
    """Clone the secrets repo. Raises on failure -- with no clone there is nothing to do."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    code, output = _git(
        ["clone", "--quiet", repo_url, str(dest)], cwd=None, timeout=CLONE_TIMEOUT
    )
    if code != 0:
        raise SecretsError(
            f"could not clone {repo_url}: {output}",
            "Check that this machine's SSH key can read the repo "
            "(`ssh -T git@github.com`) and that the URL in secrets.json is "
            "correct. The same credential that clones your other private "
            "repos should work here -- no deploy key is needed.",
        )


def refresh(clone_dir: Path, stamp: Path, *, force: bool = False) -> Optional[str]:
    """Fast-forward the clone, at most once per cooldown.

    Returns a short note when something notable happened (a failure worth
    logging), or None when the refresh was skipped or succeeded quietly.
    Never raises: a stale clone is strictly better than a blocked session.
    """
    if not force and not _cooldown_elapsed(stamp):
        return None

    code, output = _git(["fetch", "--quiet", "--prune"], cwd=clone_dir, timeout=FETCH_TIMEOUT)
    if code != 0:
        return f"fetch failed ({output}); continuing on the existing clone"

    # --ff-only so a rewritten remote history surfaces as a failure to look at
    # rather than a silent merge commit in a repo nobody reviews.
    code, output = _git(
        ["merge", "--ff-only", "--quiet", "@{u}"], cwd=clone_dir, timeout=FETCH_TIMEOUT
    )
    _touch(stamp)
    if code != 0:
        return (
            f"could not fast-forward the secrets clone ({output}); "
            f"continuing on the existing checkout"
        )
    return None


def _cooldown_elapsed(stamp: Path) -> bool:
    try:
        age = time.time() - stamp.stat().st_mtime
    except OSError:
        return True
    return age >= REFRESH_COOLDOWN_SECONDS


def _touch(stamp: Path) -> None:
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass


def head_sha(clone_dir: Path) -> Optional[str]:
    code, output = _git(["rev-parse", "HEAD"], cwd=clone_dir, timeout=FETCH_TIMEOUT)
    return output if code == 0 else None


def rollback_to(
    clone_dir: Path, sha: Optional[str], *, created: Optional[List[str]] = None
) -> None:
    """Discard everything the current verb did to the clone.

    Only ever called on the failure path of an authoring verb, and only with a
    sha this process itself read moments earlier. The clone is a cache of the
    remote, so throwing away work that never reached the remote loses nothing
    -- whereas KEEPING it is what leaves the next run reasoning about a repo
    state no other machine will ever see.

    ``created`` names files this verb brought into existence, removed
    explicitly because a reset only unwinds what got as far as a COMMIT: a
    failure between writing them and committing them would otherwise leave the
    same misleading state behind, untracked. Nothing outside that list is
    touched -- a blanket clean in a secrets clone is not a thing to reach for.
    """
    if sha:
        _git(["reset", "--hard", "--quiet", sha], cwd=clone_dir, timeout=FETCH_TIMEOUT)
    for rel in created or []:
        try:
            (clone_dir / rel).unlink()
        except OSError:
            pass


def _ahead_behind(clone_dir: Path) -> Optional[Tuple[int, int]]:
    """(commits we have that the remote does not, and vice versa), or None."""
    code, _ = _git(["rev-parse", "--verify", "--quiet", "@{u}"], cwd=clone_dir, timeout=FETCH_TIMEOUT)
    if code != 0:
        return None
    code, output = _git(
        ["rev-list", "--left-right", "--count", "HEAD...@{u}"],
        cwd=clone_dir,
        timeout=FETCH_TIMEOUT,
    )
    if code != 0:
        return None
    try:
        ahead, behind = (int(n) for n in output.split())
    except ValueError:
        return None
    return (ahead, behind)


def remote_has(clone_dir: Path, rel_path: str) -> bool:
    """Does the tracking branch contain this path? Truth for 'is it seeded'.

    The working tree answers that question about whenever this clone last
    fetched, which on a machine that reads secrets far more often than it
    writes them can be hours or weeks ago. Every decision that would CREATE
    something irreplaceable asks the remote instead.
    """
    code, _ = _git(
        ["cat-file", "-e", f"@{{u}}:{rel_path}"], cwd=clone_dir, timeout=FETCH_TIMEOUT
    )
    return code == 0


def sync(clone_dir: Path) -> None:
    """Bring the clone level with the remote. Raises rather than proceeding stale.

    The counterpart to :func:`refresh`, and deliberately its opposite in both
    respects: no cooldown, and a failure is fatal instead of a note. Reading a
    stale clone is fine when the pass is only materializing what it already
    has; it is never fine when the next step WRITES, because every authoring
    decision -- is this repo seeded, does this entry exist, what is the current
    recipient -- is read off the working tree. Seeding a repo that a stale
    clone reported as empty generates a second fleet identity and orphans
    every blob encrypted to the first.
    """
    code, output = _git(["fetch", "--quiet", "--prune"], cwd=clone_dir, timeout=FETCH_TIMEOUT)
    if code != 0:
        raise SecretsError(
            f"could not fetch the secrets repo: {output}",
            "Authoring needs an up-to-date view of the remote, so this stops "
            "here rather than deciding anything from a stale checkout. Fix "
            "connectivity (`ssh -T git@github.com`) and re-run.",
        )

    counts = _ahead_behind(clone_dir)
    if counts is None:
        return
    ahead, behind = counts
    if behind and not ahead:
        code, output = _git(
            ["merge", "--ff-only", "--quiet", "@{u}"], cwd=clone_dir, timeout=FETCH_TIMEOUT
        )
        if code != 0:
            raise SecretsError(f"could not fast-forward the secrets clone: {output}")
        return
    if ahead and behind:
        raise SecretsError(
            f"the secrets clone has diverged from the remote "
            f"({ahead} local commit(s), {behind} remote commit(s))",
            "An unpushed commit in this clone is always a FAILED earlier "
            "authoring attempt -- every verb here pushes as it writes -- so "
            "the local side is safe to throw away once you have looked at it:\n"
            f"    git -C {clone_dir} log --oneline @{{u}}..HEAD\n"
            f"    git -C {clone_dir} reset --hard @{{u}}\n"
            "Then re-run the verb.",
        )


def commit_and_push(clone_dir: Path, message: str, paths: List[str]) -> None:
    """Record an authoring act (seed / add / rotate) and publish it.

    Authoring is the ONLY direction that writes; every consuming machine
    pulls. Push failures raise, because an unpushed secret is invisible to the
    fleet and silently pretending otherwise is how drift starts.
    """
    code, output = _git(["add", "--"] + paths, cwd=clone_dir, timeout=FETCH_TIMEOUT)
    if code != 0:
        raise SecretsError(f"git add failed: {output}")

    code, output = _git(["commit", "-m", message], cwd=clone_dir, timeout=FETCH_TIMEOUT)
    if code != 0 and "nothing to commit" not in output:
        raise SecretsError(f"git commit failed: {output}")

    code, output = _git(["push", "--quiet"], cwd=clone_dir, timeout=CLONE_TIMEOUT)
    if code == 0:
        return

    # Someone else pushed between our sync and our push. Rebasing our single
    # commit onto theirs is the correct resolution when the two touched
    # different files (two machines adding different secrets -- the normal
    # case); when they touched the same ones, the rebase conflicts and we stop,
    # because a merged manifest is not something to guess at.
    _git(["fetch", "--quiet", "--prune"], cwd=clone_dir, timeout=FETCH_TIMEOUT)
    code, rebase_output = _git(
        ["rebase", "--quiet", "@{u}"], cwd=clone_dir, timeout=FETCH_TIMEOUT
    )
    if code == 0:
        code, output = _git(["push", "--quiet"], cwd=clone_dir, timeout=CLONE_TIMEOUT)
        if code == 0:
            return
    else:
        _git(["rebase", "--abort"], cwd=clone_dir, timeout=FETCH_TIMEOUT)
        output = f"{output}\nrebase onto the remote also failed: {rebase_output}"

    raise SecretsError(
        f"git push failed: {output}",
        "The remote moved and this change could not be replayed on top of it "
        "automatically. Nothing was published, so no other machine is "
        f"affected. Inspect the clone at {clone_dir} -- `git log --oneline "
        "@{u}..HEAD` shows what is unpushed -- and either resolve it there or "
        "`git reset --hard @{u}` and re-run the verb.",
    )
