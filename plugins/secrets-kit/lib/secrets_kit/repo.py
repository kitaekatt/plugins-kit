"""Clone and refresh the private secrets repo.

Two rules shape this module, both learned from llm-scripting-kit's account
check: **never block a session on connectivity**, and **rate-limit the network
call** so the steady-state pass costs nothing. An offline laptop is a normal
state, not a fault; if a clone already exists, a failed fetch is a log line and
the pass proceeds on what is on disk.
"""

import os
import re
import subprocess
import time
from pathlib import Path
from typing import List, Optional, Tuple

from . import SecretsError

CLONE_TIMEOUT = 60
FETCH_TIMEOUT = 15

# Local-only queries (rev-parse / check-ignore). No network, so a value this
# small only ever trips on a wedged filesystem.
QUERY_TIMEOUT = 10

# Local WRITES on the authoring path (add / commit / rebase). Deliberately far
# larger than FETCH_TIMEOUT, and the reason is the opposite of the network one:
# these do not wait on a remote, they run our own pre-commit hook, which shells
# out to `git show` twice plus a `grep` PER STAGED PATH. That is O(paths)
# process spawns, and process spawn is precisely what degrades on a loaded
# machine -- so the budget has to cover a slow box doing real work, not a
# typical one doing a little.
#
# Sizing this off FETCH_TIMEOUT was a latent bug, not a shortcut: a 15s network
# budget is generous for a fetch and tight for a hook, so the two only looked
# interchangeable while machines were idle. Observed failing: `git commit ...
# timed out after 15s` under parallel test load (2026-08-09).
#
# Safe to be generous HERE specifically because commit_and_push runs on the
# explicit authoring verbs (seed / add / rotate), never in the SessionStart
# pass. refresh() keeps the short budget on purpose -- that is the path the
# module's "never block a session on connectivity" rule is about.
LOCAL_WRITE_TIMEOUT = 120

# How often to talk to the remote at all. A rotated secret converges on the
# next pass after this window -- fine, because rotation is rare and the local
# copy stays valid until then. Set low enough that "I rotated it this morning"
# lands the same day.
REFRESH_COOLDOWN_SECONDS = 6 * 60 * 60


# Two distinct families of inherited environment are removed before every git
# invocation in this module. They are kept separate because the reasoning that
# justifies scrubbing each differs, and so does the reasoning about what is
# deliberately LEFT alone -- collapsing them into one list is how the retained
# exclusions below stop looking like decisions and start looking like gaps.
#
# Both are removed for EVERY invocation rather than per call site: a default
# cannot be forgotten by whoever adds the next caller, and forgetting it is
# exactly how this got missed the first time.

# Family 1 -- variables that RELOCATE the repository git operates on. Every
# call in this module names the repo it means by passing an explicit `cwd`, so
# any of these arriving from an outer process can only ever redirect us away
# from what we asked for; there is no case where inheriting one is wanted.
#
# It matters most on the verbs that WRITE. A stale GIT_DIR does not merely make
# `rev-parse` misreport -- it can make `add`/`commit`/`push` record blobs into
# a repository nobody intended, which is the failure this whole plugin exists
# to prevent.
#
# Scrubbed, and why each earns its place:
#   GIT_DIR            - names the repo outright; the primary redirect.
#                        REPRODUCED: diverts commit_and_push and sync.
#   GIT_WORK_TREE      - repoints the working tree under any repo.
#                        REPRODUCED: diverts commit_and_push.
#   GIT_INDEX_FILE     - git SETS this for hooks and rebases, so a nested run
#                        really can see one. REPRODUCED, and it is DESTRUCTIVE
#                        rather than merely misdirecting: pointed at a path
#                        that does not exist, `git add -- <path>` succeeds into
#                        a fresh EMPTY index and the commit then records a tree
#                        containing ONLY that path. Every other tracked file --
#                        identity.age, the other blobs -- is committed as
#                        deleted and pushed that way. Do not drop this one.
#   GIT_COMMON_DIR     - resolves refs/config for linked worktrees; a leftover
#                        one still misdirects after GIT_DIR is gone.
#   GIT_OBJECT_DIRECTORY - where new objects are WRITTEN: a stale value puts
#                        our ciphertext in someone else's object store.
#   GIT_ALTERNATE_OBJECT_DIRECTORIES - extra object lookup paths; would let
#                        `remote_has` see an object this repo does not have,
#                        and that call decides "is this repo already seeded".
#   GIT_CEILING_DIRECTORIES - stops upward discovery, so a real repo can read
#                        as no repo at all -- the permissive verdict.
#   GIT_NAMESPACE      - rewrites ref names, so a push would publish where no
#                        other machine looks.
#
# The last four are scrubbed on family membership and cost-nil grounds; only
# the first three have a reproduced failure behind them (see the tests).
_RELOCATING_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_NAMESPACE",
)

# Family 2 -- variables that INJECT config into the invocation. These do not
# move the repo; they rewrite what it is configured to do, which reaches the
# same outcome by a different door. REPRODUCED: with the relocating family
# fully scrubbed,
#     GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.url \
#     GIT_CONFIG_VALUE_0=<attacker>
# makes `commit_and_push` publish the encrypted blobs to an attacker-controlled
# remote and exit 0. GIT_CONFIG_PARAMETERS is the same hazard from the other
# direction: git sets it ITSELF for subprocesses, so inheriting it is precisely
# the nested-run case already accepted for GIT_INDEX_FILE.
#
# The indexed GIT_CONFIG_KEY_<n> / GIT_CONFIG_VALUE_<n> pairs cannot live in a
# fixed tuple, so they are matched by pattern -- and ALL of them are removed,
# not just those below the inherited GIT_CONFIG_COUNT. A pair left behind above
# the count is inert only until something re-sets the count, and leaving armed
# ammunition next to a removed trigger is not a defence.
_INJECTING_ENV = ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS")
_INDEXED_CONFIG_RE = re.compile(r"\AGIT_CONFIG_(?:KEY|VALUE)_\d+\Z")

# NOT scrubbed, deliberately, and this stays a decision rather than an
# oversight: GIT_TERMINAL_PROMPT and GIT_SSH_COMMAND, which `_git` sets below
# (the scrub runs first so the ordering is explicit); and GIT_CONFIG_GLOBAL /
# GIT_CONFIG_SYSTEM, which are how test harnesses and CI legitimately isolate
# config -- scrubbing those would break correct setups to defend against a
# threat the indexed-override mechanism above already covers. Do not add a
# variable to either family just because it starts with GIT_.


def _git(args: List[str], *, cwd: Optional[Path], timeout: int) -> Tuple[int, str]:
    env = dict(os.environ)
    for name in _RELOCATING_ENV + _INJECTING_ENV:
        env.pop(name, None)
    for name in [n for n in env if _INDEXED_CONFIG_RE.match(n)]:
        env.pop(name, None)
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


# --------------------------------------------------------------------------
# Is a materialization destination exposed to someone else's git repo?
# --------------------------------------------------------------------------
#
# The pre-commit guard in this module's sibling ``guard`` protects the SECRETS
# repo. It cannot protect a CONSUMER repo that a `--dest` happens to point
# into: nothing there knows a credential is being written every session, and a
# routine `git add -A` stages it. A credential pushed once survives in the
# object store, in every clone, and in any fork or backup taken meanwhile --
# rewriting history does not fix it. So the destination itself has to be
# classified before anything writes to it.
#
# This lives here rather than in a new module because it is, entirely, a pair
# of git queries -- and this is the one place the plugin shells out to git.

#: The dest is not inside any git working tree (or git cannot see one). Safe.
DEST_NOT_IN_REPO = "not-in-repo"
#: The dest is inside a working tree but gitignored. Safe.
DEST_IGNORED = "ignored"
#: The dest is inside a working tree and NOT ignored. The dangerous case.
DEST_EXPOSED = "exposed"
#: git could not be asked (absent, timed out, refused). Not an answer.
DEST_UNDETERMINED = "undetermined"

#: Why a verdict is undetermined. git being missing is SYSTEMIC and expected --
#: a machine that only consumes secrets need never have installed it, and it
#: will be true of every entry on every pass. git being present and answering
#: something we cannot read is an ANOMALY: it should read as one, because it is
#: the case where the guard is silently not guarding.
DEST_UNDETERMINED_UNAVAILABLE = "git-unavailable"
DEST_UNDETERMINED_ANOMALY = "git-anomaly"


class DestExposure:
    """What git says about one materialization destination.

    Four states, and conflating any two of them is a bug. In particular
    ``DEST_UNDETERMINED`` is NOT ``DEST_NOT_IN_REPO``: "there is no repo here"
    and "we could not find out" have opposite risk profiles, and collapsing
    them into a boolean that reads as *safe* is precisely how a guard fails
    open. Callers decide what an undetermined answer means for them.
    """

    def __init__(
        self,
        status: str,
        *,
        dest: Path,
        toplevel: Optional[Path] = None,
        gitignore_line: Optional[str] = None,
        detail: str = "",
        cause: Optional[str] = None,
    ) -> None:
        self.status = status
        self.dest = dest
        self.toplevel = toplevel
        self.gitignore_line = gitignore_line
        self.detail = detail
        #: Only set when undetermined: DEST_UNDETERMINED_UNAVAILABLE (systemic,
        #: expected) or DEST_UNDETERMINED_ANOMALY (git answered, unreadably).
        self.cause = cause

    @property
    def anomalous(self) -> bool:
        """Undetermined for a reason that should not happen."""
        return self.cause == DEST_UNDETERMINED_ANOMALY

    @property
    def repo_relative(self) -> Optional[str]:
        """The dest as git names it: root-relative, no anchor (``config/x.txt``).

        The spelling `git rm --cached` and friends want, as opposed to the
        anchored form `.gitignore` wants. Derived from the one computation so
        the two can never disagree about which file they mean.
        """
        if not self.gitignore_line:
            return None
        return self.gitignore_line.lstrip("/")

    @property
    def exposed(self) -> bool:
        return self.status == DEST_EXPOSED

    @property
    def undetermined(self) -> bool:
        return self.status == DEST_UNDETERMINED

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<DestExposure {self.status} {self.dest}>"


def dest_exposure(dest: Path) -> DestExposure:
    """Classify ``dest`` against the git working tree it may sit inside.

    Never raises, matching :func:`refresh`: a machine without git is a normal
    machine, not a fault. It reports ``DEST_UNDETERMINED`` and lets the caller
    choose.
    """
    dest = _normalize(dest)

    # git has to run somewhere that exists, and the dest file itself normally
    # does not yet. Walk up to the nearest existing ancestor DIRECTORY rather
    # than giving up: `dest.parent` is routinely a directory the consuming repo
    # has not been cloned into yet, and the repo root -- if there is one -- is
    # always an existing ancestor of anything inside it, so walking up can
    # never escape the tree we are asking about.
    base = _nearest_existing_dir(dest.parent)
    if base is None:
        # No ancestor exists at all (a bad drive letter, a vanished mount).
        # Nothing can be inside a working tree we cannot even stat, and there
        # is nothing for git to answer about.
        return DestExposure(DEST_NOT_IN_REPO, dest=dest, detail="no existing ancestor")

    code, output = _query(["rev-parse", "--is-inside-work-tree"], base)
    if code != 0:
        if _is_not_a_repo(code, output):
            return DestExposure(DEST_NOT_IN_REPO, dest=dest)
        return _undetermined(dest, code, output)

    inside = _boolean_answer(output)
    if inside is None:
        # Exit 0 but no answer we recognize. This MUST NOT fall through to
        # "not in a repo": that is the permissive state, and reaching it on
        # output we failed to parse is the guard failing open.
        return DestExposure(
            DEST_UNDETERMINED,
            dest=dest,
            detail=f"`rev-parse --is-inside-work-tree` returned 0 with unreadable output: {output!r}",
            cause=DEST_UNDETERMINED_ANOMALY,
        )
    if not inside:
        # "false" from inside a bare repo's GIT_DIR: no working tree, nothing
        # anyone can accidentally `git add`.
        return DestExposure(DEST_NOT_IN_REPO, dest=dest)

    # Exit code only -- `-q` prints nothing, so there is no output to misread.
    # 0 = ignored, 1 = not ignored, anything else is a fault, not an answer.
    code, output = _query(["check-ignore", "-q", "--", _git_path(dest)], base)
    if code == 0:
        return DestExposure(DEST_IGNORED, dest=dest, toplevel=_toplevel(base, dest))
    if code != 1:
        return _undetermined(dest, code, output)

    toplevel = _toplevel(base, dest)
    return DestExposure(
        DEST_EXPOSED,
        dest=dest,
        toplevel=toplevel,
        gitignore_line=gitignore_line_for(dest, toplevel) if toplevel else None,
    )


def gitignore_line_for(dest: Path, toplevel: Path) -> Optional[str]:
    """The exact ``.gitignore`` line that would ignore ``dest``, or None.

    Repo-root-relative and leading-slash-anchored (``/config/ha-token.txt``),
    because that is the only form that means "this one file" rather than "any
    path component with this name anywhere in the tree". This is user-facing
    remediation text, so it is written with forward slashes on every platform
    -- git's ignore syntax has no other separator.
    """
    dest = _normalize(dest)
    toplevel = _normalize(toplevel)
    try:
        rel = os.path.relpath(str(dest), str(toplevel))
    except ValueError:
        # Different drives on Windows: not a relative path at all.
        return None
    rel = rel.replace(os.sep, "/")
    if rel.startswith("../"):
        return None
    return "/" + rel


def _normalize(path: Path) -> Path:
    """Absolute, ``~``-expanded, and symlink-resolved.

    Resolution matters on macOS, where ``/tmp`` is a symlink to ``/private/tmp``
    and an unresolved dest would refuse to sit under the resolved toplevel git
    reports.
    """
    expanded = Path(os.path.expanduser(str(path)))
    try:
        return expanded.resolve()
    except OSError:  # pragma: no cover - resolve is non-strict on 3.6+
        return Path(os.path.abspath(str(expanded)))


def _nearest_existing_dir(start: Path) -> Optional[Path]:
    for candidate in [start] + list(start.parents):
        if candidate.is_dir():
            return candidate
    return None


def _git_path(path: Path) -> str:
    """Hand git a path spelled the way this platform spells one.

    git on Windows accepts both separators here; ``os.path.normpath`` keeps the
    native form so nothing downstream has to guess which one it got.
    """
    return os.path.normpath(str(path))


def _query(args: List[str], cwd: Path) -> Tuple[int, str]:
    """Run one exposure query. Local-only, so it gets the short timeout.

    Note what it does NOT have to do. git resolves a repository from the
    environment before it looks at the cwd, so an inherited GIT_DIR would make
    these queries answer about a DIFFERENT repository -- or fail with "fatal:
    not a git repository", which classifies the dest as safe. That is handled
    for every invocation in this module by the scrubbing in :func:`_git`, so
    the queries need no special casing of their own.
    """
    return _git(args, cwd=cwd, timeout=QUERY_TIMEOUT)


def _undetermined(dest: Path, code: int, output: str) -> DestExposure:
    """Classify a failed query by whether it is expected or alarming."""
    unavailable = code in (124, 127)
    return DestExposure(
        DEST_UNDETERMINED,
        dest=dest,
        detail=output,
        cause=(
            DEST_UNDETERMINED_UNAVAILABLE if unavailable else DEST_UNDETERMINED_ANOMALY
        ),
    )


def _boolean_answer(output: str) -> Optional[bool]:
    """git's own true/false, isolated from anything else on the stream.

    ``_git`` folds stderr into stdout so a failing caller can report the whole
    story in one string. That is right for the network verbs and hostile here:
    a config warning, a ``safe.directory`` notice, a broken-ref advisory or an
    autocrlf grumble arrives CONCATENATED with the answer, and comparing the
    whole blob to ``"true"`` then reads a real repository as no repository at
    all -- the permissive state.

    So compare per LINE and return None when neither token appears, leaving the
    caller to classify that as undetermined. Parsing here rather than dropping
    ``stderr=STDOUT`` in :func:`_git`: that wrapper is shared with clone,
    fetch, merge, push and commit, several of which put ``output`` straight
    into a user-facing :class:`SecretsError`, and quietly draining their stderr
    would degrade every one of those diagnostics to fix a bug in this one
    caller.
    """
    for line in output.splitlines():
        token = line.strip()
        if token == "true":
            return True
        if token == "false":
            return False
    return None


def _is_not_a_repo(code: int, output: str) -> bool:
    """A clean "there is no repo here", as opposed to git failing to run.

    ``_git`` reserves 127 for "could not run git" and 124 for a timeout, so
    those are never this. Everything else is judged on git's own words: it
    exits 128 both for "not a git repository" and for genuine faults, and only
    the first is an answer.
    """
    if code in (124, 127):
        return False
    return "not a git repository" in output.lower()


def _toplevel(cwd: Path, dest: Path) -> Optional[Path]:
    """The repo root, or None when we cannot be sure which directory it is.

    Same merged-stderr hazard as :func:`_boolean_answer`, with a different
    consequence: a warning line glued to the path would not change the
    exposed/ignored VERDICT, but it would produce a bogus toplevel and hence a
    bogus ``.gitignore`` line -- remediation text that looks authoritative and
    silently names the wrong file. `--show-toplevel` prints exactly one line on
    stdout, so take the last non-empty line and then VERIFY it: a real repo
    root is always an ancestor of a dest inside it. Anything else yields None,
    and the caller degrades to a message with no fix line rather than a wrong
    one.
    """
    code, output = _query(["rev-parse", "--show-toplevel"], cwd)
    if code != 0 or not output:
        return None
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return None
    # git answers with forward slashes even on Windows ("C:/dev/repo"); Path
    # normalizes that to the native form.
    candidate = _normalize(Path(lines[-1]))
    if candidate == dest or candidate not in dest.parents:
        return None
    return candidate


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
    code, output = _git(["add", "--"] + paths, cwd=clone_dir, timeout=LOCAL_WRITE_TIMEOUT)
    if code != 0:
        raise SecretsError(f"git add failed: {output}")

    code, output = _git(["commit", "-m", message], cwd=clone_dir, timeout=LOCAL_WRITE_TIMEOUT)
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
        ["rebase", "--quiet", "@{u}"], cwd=clone_dir, timeout=LOCAL_WRITE_TIMEOUT
    )
    if code == 0:
        code, output = _git(["push", "--quiet"], cwd=clone_dir, timeout=CLONE_TIMEOUT)
        if code == 0:
            return
    else:
        _git(["rebase", "--abort"], cwd=clone_dir, timeout=LOCAL_WRITE_TIMEOUT)
        output = f"{output}\nrebase onto the remote also failed: {rebase_output}"

    raise SecretsError(
        f"git push failed: {output}",
        "The remote moved and this change could not be replayed on top of it "
        "automatically. Nothing was published, so no other machine is "
        f"affected. Inspect the clone at {clone_dir} -- `git log --oneline "
        "@{u}..HEAD` shows what is unpushed -- and either resolve it there or "
        "`git reset --hard @{u}` and re-run the verb.",
    )
