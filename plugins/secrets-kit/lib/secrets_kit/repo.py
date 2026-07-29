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
    if code != 0:
        raise SecretsError(
            f"git push failed: {output}",
            "The change is committed locally but not published, so other "
            "machines will not see it. Resolve and push from the clone at "
            f"{clone_dir}.",
        )
