#!/usr/bin/env python3
"""Publish the marketplace: preflight, the four-step release flow, and verify.

This script is the SOURCE OF TRUTH for how a publish happens. CLAUDE.md states
the intent and points here; the steps live in code so the two cannot drift.

"Publish" means all four of these, and a publish that stops early is not a
publish -- it is a state where users see something other than what you meant:

  1. Version bump (yours) + regenerate the derived marketplace.json.
  2. Regenerate index.html from the dev tree, INSIDE the release commit.
  3. Push dev.
  4. Fast-forward master and push it. master is the cache source, so nothing
     reaches a user until this lands. A bump without a merge ships nothing; a
     merge without a bump doesn't change the cache key, so consumers never
     refetch.

Contract: you commit your code and the version bump on `dev`; this script owns
everything derived from them and every git step after them.

Why a script rather than a checklist -- three footguns it removes:

  - dev-tree.py flips every installPath at your working copy so the page renders
    what you are ABOUT to publish. If the regen throws in between, the tree stays
    flipped and your next Claude session silently loads plugins from the working
    copy instead of the cache. Here the restore is a `finally`, not a discipline,
    and the post-verify checks it landed even if the finally misfired.
  - The merge is only USUALLY a fast-forward. When dev carries commits for a
    dev-only (published: false) plugin, a fast-forward would publish them, so
    the release is a PROJECTION instead: in a temporary worktree, master's next
    commit takes dev's tree with the dev-only plugins' own files held at the
    content master already has. dev is untouched and keeps the excluded work.
    `published: false` already recorded the decision that the plugin does not
    ship, so honouring it is not the script guessing -- what it must never do
    is decide that a plugin's status has changed.

    A projection rather than a commit replay, because replaying is not
    idempotent and the damage compounds. A replay gives every shipped commit a
    NEW sha, so the originals sit in `master..dev` forever; their patch-ids do
    not match the replays either, because the replay was built without the
    excluded commits beneath it and its context lines differ. So every LATER
    publish tried to ship work master already had and died on a duplication
    conflict, while preflight read master's replay commits as a reconcile and
    refused before even getting there. The projection has neither failure: it
    computes the tree master should have -- which is exactly what verify()
    asserts -- and stamps `Published-From: <dev sha>` on the commit so the next
    run knows where the range starts instead of inferring it from ancestry.
  - index.html must ride INSIDE the release commit, or master briefly holds a
    page that disagrees with its own marketplace.json.

What preflight refuses on (all of it unbypassable -- no environment variable
turns any of it off, which is the whole point of a gate that sits after the
escapable pre-commit hooks):

  - not on dev; a dirty tree; a merge that would not fast-forward; a range with
    nothing to publish.
  - a single commit touching BOTH a dev-only plugin and files that would
    otherwise ship -- but ONLY for a plugin named in --exclude-dev-only. By
    default every dev-only plugin's commits ship, so there is nothing to
    refuse. See excluded_dev_only_commits() for why that is the default and
    what it does NOT change (a dev-only plugin still never reaches the
    marketplace listing, so it stays uninstallable).
  - a plugin that does not declare the bootstrap dependency.
  - no published plugin bumped at all, AND any published plugin whose files
    changed in the range without a bump (the cache keys on version, so those
    files would ship under a version consumers already hold and never refetch).
  - a pyproject.toml version disagreeing with its plugin.json.

The last two exist as pre-commit hooks too, but those are skippable with
--no-verify (and PLUGINS_KIT_SKIP_BUMP_CHECK=1, whose documented purpose is
legitimate dev-branch commits between publish checkpoints). Skipping them on dev
is sanctioned; shipping the result is not, so publish re-runs them from the same
source of truth rather than restating the rules.

A PARTIAL release (`--only <plugin>`, repeatable) is the same projection with
a larger hold-back set: every published plugin NOT named is held at master's
content exactly as a dev-only plugin is, so master receives one plugin's release
while the rest of dev stays unpublished. The derived artifacts are regenerated
INSIDE the projection worktree, from the tree master is about to hold, so
master's marketplace.json lists the held-back plugins at the versions master
actually carries. Nothing on dev is regenerated or committed: the dirty gate
admits uncommitted work inside held-back plugins, and a dev-side regen would
read those working-tree manifests while commit_derived would sweep another
session's staged work along. Two things a partial release deliberately does
NOT do:

  - It does not advance `Published-From:`. The held-back plugins' commits stay
    in the range so the next full publish still sees them; the projection
    commit records `Published-Only:` and `Built-From:` instead, which
    range_base() ignores.
  - It does not check cross-plugin coupling. A plugin that consumes a shared
    library another plugin owns can be shipped ahead of that library's change;
    the whole-tree publish is the one that cannot do that. Use --only for a
    plugin whose change is self-contained.

Usage:
  python scripts/publish.py                   # preflight, publish, verify
  python scripts/publish.py --check           # preflight + verify only; no writes, no pushes
  python scripts/publish.py --only awesome-kit  # partial release of one plugin
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = REPO_ROOT / "plugins"
SCRIPTS_DIR = Path(__file__).resolve().parent

# The repo-wide invariant checks below judge the REAL plugin tree, resolved
# next to THIS file, rather than PLUGINS_DIR -- same reasoning as
# _require_bootstrap_dependency: the invariant is a property of the actual
# plugin tree, and PLUGINS_DIR is patched to a synthetic repo by
# tests/repo-scripts/test_publish.py. Tests point these two at fixture data.
REAL_PLUGINS_DIR = REPO_ROOT / "plugins"
MARKETPLACE_JSON = REPO_ROOT / ".claude-plugin" / "marketplace.json"
POSTER_YAML = REPO_ROOT / ".claude-plugin" / "poster.yaml"
INDEX_PAGE_YAML = REPO_ROOT / ".claude-plugin" / "index-page.yaml"
INDEX_HTML = REPO_ROOT / "index.html"

DEV_BRANCH = "dev"
MASTER_BRANCH = "master"
REMOTE = "origin"
MARKETPLACE_NAME = "plugins-kit"
PAGE_TITLE = "plugins-kit marketplace"

GENERATE_PY = (PLUGINS_DIR / "awesome-kit" / "skills" / "plugin-ecosystem"
               / "scripts" / "generate.py")
DEV_TREE_PY = REPO_ROOT / "scripts" / "dev-tree.py"
REGEN_MARKETPLACE_PY = REPO_ROOT / "scripts" / "regen_marketplace.py"


class PublishError(Exception):
    """A refusal. The message says what is wrong and what to do about it."""


# --- shell -----------------------------------------------------------------

def git(*args: str, check: bool = True) -> str:
    """Run a git command in the repo and return stripped stdout."""
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT,
        capture_output=True, text=True,
    )
    if check and result.returncode != 0:
        raise PublishError(
            f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def run(cmd: list[str], what: str) -> None:
    """Run a subprocess, surfacing its output on failure."""
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        raise PublishError(
            f"{what} failed:\n{result.stdout.strip()}\n{result.stderr.strip()}")


# --- plugin manifests ------------------------------------------------------

def _manifest_path(plugin: str) -> Path:
    return PLUGINS_DIR / plugin / ".claude-plugin" / "plugin.json"


def local_plugins() -> dict[str, dict]:
    """Every plugin's manifest as it stands in the working tree."""
    out = {}
    for plugin_dir in sorted(PLUGINS_DIR.iterdir()):
        manifest = _manifest_path(plugin_dir.name)
        if manifest.is_file():
            out[plugin_dir.name] = json.loads(manifest.read_text(encoding="utf-8"))
    return out


def is_published(manifest: dict) -> bool:
    # Missing means published; only an explicit false opts out.
    return manifest.get("published", True) is not False


def published_plugins() -> set[str]:
    return {name for name, m in local_plugins().items() if is_published(m)}


def held_back_for(only: set[str] | None) -> set[str]:
    """Published plugins a partial (--only) release holds at master's content."""
    if not only:
        return set()
    return published_plugins() - only


def is_poster_hidden(plugin: str) -> bool:
    """True when a plugin opts out of the generated poster / index.html.

    `hidden: true` in plugins/<name>/.claude-plugin/poster.yaml means published
    (installable, listed in marketplace.json) but deliberately absent from the
    user-facing page -- see the plugin-ecosystem generator, which owns the
    feature. verify() must honour it or it reports a missing entry that the
    generator was correct to omit.

    Deliberately a substring check rather than a YAML parse: publish.py has no
    YAML dependency, and this file's whole grammar is a handful of scalar keys.
    """
    poster = PLUGINS_DIR / plugin / ".claude-plugin" / "poster.yaml"
    if not poster.is_file():
        return False
    for line in poster.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.replace(" ", "").lower() == "hidden:true":
            return True
    return False


def version_at(ref: str, plugin: str) -> str | None:
    """A plugin's version at a git ref, or None if it doesn't exist there."""
    path = f"plugins/{plugin}/.claude-plugin/plugin.json"
    raw = git("show", f"{ref}:{path}", check=False)
    if not raw:
        return None
    try:
        return json.loads(raw).get("version")
    except json.JSONDecodeError:
        return None


# --- where the range starts, and what master must not lose -----------------

PUBLISHED_FROM = "Published-From:"
PUBLISHED_ONLY = "Published-Only:"
BUILT_FROM = "Built-From:"

# How far down master to look for the last recorded publish point. Generous
# enough to see past a run of non-release commits (infra syncs, reconciles),
# small enough that a master which never carried one falls back promptly.
_RANGE_BASE_SEARCH_DEPTH = 50

# Derived artifacts, regenerated from the plugin manifests on every publish.
# master's copy is an OUTPUT of the last release rather than content anyone
# authored there, so dev wins unconditionally -- the same rule the reconcile
# procedure states in docs/reference/publish-reconcile.md.
GENERATED_PATHS = frozenset({".claude-plugin/marketplace.json", "index.html"})


def _rc(*args: str) -> int:
    """Exit code of a git command, for the questions git answers that way."""
    return subprocess.run(["git", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True).returncode


def blob_at(ref: str, path: str) -> str | None:
    """Object id of `path` at `ref`, or None when it does not exist there."""
    return git("rev-parse", "--verify", "--quiet", f"{ref}:{path}",
               check=False) or None


def _blobs_along(path: str, commits: list[str]) -> list[str]:
    """The blobs `path` held at `commits`, in the order given.

    One `cat-file --batch-check` resolves the whole list, so the cost is two
    processes per path rather than one per commit. Commits where the path is
    absent print "<input> missing" and are dropped -- an absent path is not a
    state anything can hold.
    """
    if not commits:
        return []
    probe = subprocess.run(
        ["git", "cat-file", "--batch-check=%(objectname)"],
        cwd=REPO_ROOT, capture_output=True, text=True,
        input="".join(f"{sha}:{path}\n" for sha in commits))
    # A present entry is the object id alone; a missing one is
    # "<input> missing", two fields, which cannot collide with a sha.
    return [fields[0] for fields in
            (line.split() for line in probe.stdout.splitlines())
            if len(fields) == 1]


def _dev_introduction_order(path: str) -> dict[str, int]:
    """When dev first INTRODUCED each state of `path`; 0 is the oldest.

    An ordering, not mere membership, is what `_master_only_paths` needs: dev
    holding a blob SOMEWHERE says nothing about which of two blobs came later,
    and a master that sits on content dev wrote EARLIER may be sitting there
    deliberately.

    The order is by INTRODUCTION -- dev's oldest commit holding the blob -- and
    that choice is the load-bearing one. Dev can return to content it published
    before (a revert on dev), and it then holds that old content at its TIP;
    ordering by most recent appearance would call that content dev's newest
    state and every state written between the two the older one, inverting the
    real development sequence. Introducing content happens once: a later
    re-appearance is dev going back to old content, not writing new content. So
    "master's blob was introduced before this other one" stays well defined
    however often either branch moves back and forth.

    The walk is the SIMPLIFIED one -- no `--full-history` -- and that is a
    judgement, not an omission. Simplification keeps the commits where the path
    changed along the history dev's tree actually descends from, which is the
    set of states dev held. `--full-history` would additionally reach content
    on a side branch a merge RESOLVED AWAY, and dev rejecting a state is
    precisely the case where master still carrying it is a real loss the
    operator should see.
    """
    commits = git("rev-list", DEV_BRANCH, "--", path, check=False).split()
    order: dict[str, int] = {}
    # rev-list is newest-first, so walk it backwards to number introductions
    # from dev's oldest commit forward.
    for rank, blob in enumerate(reversed(_blobs_along(path, commits))):
        order.setdefault(blob, rank)
    return order


def _master_holds_discardable_state(path: str, master_base: str, master: str,
                                    master_blob: str) -> bool:
    """True when publishing over master's state at `path` would discard it.

    A publish overwrites master's CURRENT state with dev's, so that is the only
    state at risk. Two things make overwriting it safe, and both are required.

    Dev must have held that state at all: a blob nowhere in dev's history is
    content dev never picked up -- a hotfix written straight on master -- and
    losing it is the loss this guard exists to refuse.

    And master must not have chosen it OVER content dev wrote LATER. That is
    the whole question, because the two cases that reach here look identical by
    content: in both, master sits on a state dev's tip has moved past. What
    separates them is WHICH BRANCH MOVED, and the observable difference is
    whether master itself gave up newer content:

      * MASTER moved -- master held content dev introduced later and left it.
        Reverting on master is the documented way to retract a bad publish, so
        master is sitting on the earlier content deliberately, and a publish
        restores exactly what the revert withdrew. REFUSE.
      * DEV moved -- dev published one state, then another, then went back to
        the first. Master was handed each in turn and never gave one up; the
        state it holds is the newest content it was ever given. Dev superseded
        that content itself, so a publish discards nothing master decided.
        CLEAR.

    So the test is: has master held, since the publish point, content dev
    introduced later than what master holds now? States master held that dev
    never had are ignored -- master abandoned those itself, and a publish
    cannot discard what master no longer holds.

    `master_base` must be a MASTER-side commit. A range measured from the dev
    sha `range_base()` returns spans master's whole post-divergence history,
    including projections older than the last publish point, which is a wider
    claim than "what master has done since it was last given dev's tree".

    The state master held AT `master_base` counts and has to be added by hand,
    because `<master_base>..<master>` EXCLUDES that commit. It is the state the
    release itself handed master, so a retraction of a just-published state is
    a master-side move whose entire evidence sits on the boundary commit: the
    revert is the only commit in the range, master's blob after it is the
    earlier content, and without the boundary the guard would see master
    holding old content having apparently given up nothing.

    Known limit, stated because the guard has no way to see past it: if dev
    reverts and an infra sync then carries that revert to master, master's own
    move is backwards too and this refuses. That direction is the safe one --
    the operator is shown a path both branches moved backwards on -- and no
    signal in either history distinguishes it from a master-side retraction of
    the same content.
    """
    order = _dev_introduction_order(path)
    current = order.get(master_blob)
    if current is None:
        return True
    commits = git("rev-list", f"{master_base}..{master}", "--", path,
                  check=False).split()
    held = _blobs_along(path, commits)
    boundary = blob_at(master_base, path)
    if boundary is not None:
        held.append(boundary)
    newest = max((order[blob] for blob in held if blob in order),
                 default=current)
    return newest > current


def range_base() -> str:
    """The commit `..dev` should be measured from.

    Normally `origin/master` itself. After a PROJECTION release master's tip is
    a commit dev has never seen, and plain ancestry then counts every
    already-published commit as unshipped FOREVER: the range grows without
    bound, dev-only commits excluded years ago are re-reported on every run,
    and the bump gates judge work that is already on master. The projection
    therefore records the dev commit it was built from, and that -- not
    ancestry -- is the honest boundary.

    The trailer is searched for down master's history, not read off its TIP.
    Master legitimately carries commits that are not projections -- an
    infra-drift sync, a reconcile (both are documented operations in
    docs/reference/publish-reconcile.md) -- and none of them records a boundary
    because none of them is a release. Reading only the tip therefore loses the
    boundary the moment anyone lands one, and the loss is silent: the fallback
    below is the ANCIENT merge base, against which every file the last release
    shipped looks like a master-side change, so `_master_only_paths` reports a
    reconcile that does not exist and refuses a routine publish. The walk is
    bounded because a master that never carried a projection has no boundary to
    find and should reach the fallback quickly rather than scan its whole
    history.

    Falls back to `origin/master` when no trailer is found within that window,
    or when the ones found name objects this clone lacks or commits that are
    not ancestors of dev (a rewritten dev). The fallback over-reports rather
    than under-reports, which is the safe direction: a wider range costs noise,
    a narrower one silently drops a commit from the release.
    """
    point = _recorded_publish_point()
    return point[1] if point else f"{REMOTE}/{MASTER_BRANCH}"


def _recorded_publish_point() -> tuple[str, str] | None:
    """The last release master carries: (its own commit, the dev sha it shipped).

    Both halves are needed and they are not interchangeable. The dev sha is the
    boundary for `..dev` questions -- what has been written since the release.
    The master commit is the boundary for master-side questions -- what master
    has done since the release -- and only a MASTER commit can bound those: the
    dev sha is not an ancestor of master, so `<dev sha>..<master>` reaches every
    master commit back to the divergence, older projections included.

    None when no usable trailer is found within the search window, or when the
    ones found name objects this clone lacks or commits that are not ancestors
    of dev (a rewritten dev). Callers then fall back to the merge base, which
    over-reports rather than under-reports.
    """
    master = f"{REMOTE}/{MASTER_BRANCH}"
    log = git("log", f"-{_RANGE_BASE_SEARCH_DEPTH}", "--format=%H%x1e%B%x1f",
              master, check=False)
    for entry in log.split("\x1f"):
        if not entry.strip():
            continue
        commit, _sep, message = entry.partition("\x1e")
        for line in reversed(message.splitlines()):
            line = line.strip()
            if not line.startswith(PUBLISHED_FROM):
                continue
            sha = line[len(PUBLISHED_FROM):].strip()
            if sha and _rc("merge-base", "--is-ancestor", sha, DEV_BRANCH) == 0:
                return commit.strip(), sha
            break
    return None


def _master_only_paths() -> list[str]:
    """Paths where master holds content dev does not.

    The old guard for this was ancestry -- "master has commits dev lacks" --
    which a projection (or the replay before it) makes permanently true even
    though nothing has diverged. What actually matters is CONTENT: a publish
    takes dev's version of every shippable file, so the only thing that can be
    LOST is a file master changed since the branches last agreed and dev never
    picked up. Generated artifacts are exempt (dev wins by definition) and so
    are the dev-only plugins' own files, which are supposed to differ.

    "Last agreed" is range_base(), NOT the merge base, and the difference is
    load-bearing. A projection's content comes from dev, but its commit is not
    in dev's history, so `git merge-base` still points at the ancient common
    ancestor -- against which every file the last release shipped looks like a
    master-side change. Measuring from the recorded publish point instead
    compares master to the dev tree it was actually built from. The merge base
    is only the fallback, for a master that never carried a projection.

    The base comparison is a PREFILTER, not the verdict. Master legitimately
    receives dev content after the base -- an infra sync, a hand reconcile --
    and master's blob then differs from the base while being a state dev
    already holds, so comparing against the base alone reports a path dev is
    strictly ahead on. What settles it is
    `_master_holds_discardable_state`: master's state must be one dev held, and
    master must not have given up content dev introduced later.

    The prefilter still earns its place, but it answers only the paths master
    has left ALONE since the base: a blob equal to the base's is dev's own by
    construction (the base is an ancestor of dev), so an untouched path needs
    no history walk. A path master touched and returned to the base's blob is a
    revert like any other and goes to the full check.

    The two boundaries are DIFFERENT COMMITS and must not be conflated. `base`
    is a dev sha, which is what the base-blob comparison needs. Every question
    about what MASTER has done is bounded by `master_base`, the projection
    commit that shipped that dev sha: `<dev sha>..<master>` is not "master since
    the release" at all, because the dev sha is not an ancestor of master, so
    the range walks back to the divergence and sweeps in earlier projections --
    which is exactly how a path master has not touched since the release reads
    as one master moved.
    """
    dev_only = {name for name, m in local_plugins().items() if not is_published(m)}
    master = f"{REMOTE}/{MASTER_BRANCH}"
    point = _recorded_publish_point()
    if point:
        master_base, base = point
    else:
        master_base = base = git("merge-base", master, DEV_BRANCH, check=False)
    if not base:
        return []
    stray = []
    # Which paths master's own commits touched since the publish point. One
    # walk answers it for every path, which is what lets the prefilter below
    # distinguish "master never moved this" from "master moved it back".
    master_touched = set(git("log", "--name-only", "--pretty=format:",
                             f"{master_base}..{master}", "--",
                             check=False).split("\n"))
    # The trailing "--" is required, not tidiness: this repo has a `dev/`
    # directory, so `git diff <ref> dev` is ambiguous between a revision and a
    # path and git refuses outright.
    for path in git("diff", "--name-only", master, DEV_BRANCH, "--").splitlines():
        path = path.strip()
        if not path or path in GENERATED_PATHS or _dev_only_owned(path, dev_only):
            continue
        master_blob = blob_at(master, path)
        if master_blob == blob_at(base, path) and path not in master_touched:
            continue
        # A path master no longer has holds no content to discard by this
        # test, but master DELETING it since the base is a master-side change
        # a publish would undo, and there is no blob to look for. Report it and
        # let the operator judge, which is what the guard did before.
        if master_blob is None or _master_holds_discardable_state(
                path, master_base, master, master_blob):
            stray.append(path)
    return stray


def _require_no_master_only_content() -> None:
    stray = _master_only_paths()
    if not stray:
        return
    raise PublishError(
        f"{REMOTE}/{MASTER_BRANCH} holds content {DEV_BRANCH} does not, so a "
        f"publish would discard it:\n  " + "\n  ".join(stray[:12])
        + (f"\n  (+{len(stray) - 12} more)" if len(stray) > 12 else "")
        + f"\n\nThat is a reconcile, not a routine publish. Back-port those "
          f"changes to {DEV_BRANCH} first (see the conflict-resolution policy "
          f"in docs/reference/publish-reconcile.md), then publish.")


# --- preflight -------------------------------------------------------------

def preflight(allow_dev_only: set[str] | None = None,
              only: set[str] | None = None,
              ) -> tuple[list[str], dict[str, set[str]]]:
    """Refuse anything unsafe. Returns (publish summary, excluded commits).

    Every check here refuses rather than fixes: a publish is visible to other
    machines, so guessing is worse than stopping. The ONE thing it no longer
    refuses is a range carrying dev-only commits -- those are excluded from the
    release instead, per excluded_dev_only_commits(). `allow_dev_only` names
    dev-only plugins whose commits the operator has decided to ship anyway
    (see --allow-dev-only). `only` names the published plugins a partial
    release ships; every other published plugin is then held back, and the
    gates that judge shippable content judge only what ships.
    """
    held_back = held_back_for(only)
    if git("rev-parse", "--abbrev-ref", "HEAD") != DEV_BRANCH:
        raise PublishError(
            f"not on {DEV_BRANCH} (publish releases {DEV_BRANCH} -> "
            f"{MASTER_BRANCH}); checkout {DEV_BRANCH} first")

    dirty = _shippable_dirty_paths(held_back)
    if dirty:
        raise PublishError(
            "working tree is dirty -- commit your work before publishing "
            "(the dev tree is shared by multiple agents/sessions, so uncommitted "
            "changes may be another session's in-flight work; commit them in "
            "scoped commits rather than stashing or discarding). This script "
            "owns the derived artifacts and the git flow, not your changes.\n"
            "Dirty files:\n" + "\n".join(dirty))

    git("fetch", REMOTE, "--quiet")

    # NOT "is master an ancestor of dev". A filtered release leaves master
    # carrying commits dev will never see, by design and permanently, so
    # ancestry reports a reconcile on every publish after the first one.
    _require_no_master_only_content()

    if git("rev-list", "--count", f"{range_base()}..{DEV_BRANCH}") == "0":
        raise PublishError(
            f"{DEV_BRANCH} has nothing {REMOTE}/{MASTER_BRANCH} lacks -- "
            f"nothing to publish.")

    excluded = excluded_dev_only_commits(allow_dev_only or set())
    _refuse_mixed_dev_only_commit(excluded)
    _require_bootstrap_dependency()
    _require_pyproject_sync()
    bumps = _require_version_bump(only)
    _require_bump_for_changed_plugins(held_back)
    return bumps, excluded


def _load_rule_module(script_name: str):
    """Load a sibling checker script as a module, so its rule is used verbatim.

    The publish gate must not restate a rule that a pre-commit hook also
    enforces -- two copies can disagree, and the copy that loses is always the
    unbypassable one nobody re-reads.
    """
    import importlib.util

    script = SCRIPTS_DIR / script_name
    spec = importlib.util.spec_from_file_location(script.stem, script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _require_bootstrap_dependency() -> None:
    """Refuse when any plugin fails to declare the bootstrap dependency.

    Also enforced at pre-commit, but re-checked here because publishing is the
    moment the invariant reaches consumers: a plugin installable without
    bootstrap is one whose bootstrap.json never runs (no venv, no tools) and
    which cannot read the fleet-wide user posture bootstrap owns
    (docs/reference/first-run-experience.md). The pre-commit hook is
    bypassable (--no-verify, PLUGINS_KIT_SKIP_BUMP_CHECK=1), so a commit can
    reach dev without it; this gate is not bypassable.

    The rule lives in scripts/check_bootstrap_dependency.py and is loaded from
    there rather than restated, so the publish gate and the commit gate can
    never disagree.
    """
    # Resolved next to THIS file, and scanning the checker's own default
    # plugins dir, rather than via REPO_ROOT: the invariant is a property of
    # the real plugin tree, not of the commit range, and REPO_ROOT is patched
    # to a synthetic repo by tests/repo-scripts/test_publish.py.
    module = _load_rule_module("check_bootstrap_dependency.py")

    outliers = module.find_outliers()
    if outliers:
        raise PublishError(
            "refusing: plugin(s) do not declare the bootstrap dependency:\n  "
            + "\n  ".join(outliers)
            + '\n\nAdd "dependencies": ["bootstrap"] (bare string -- no '
            "marketplace field, no version) to each plugin's "
            ".claude-plugin/plugin.json.\nA dependencies edit is a manifest "
            "change: bump that plugin's version too, or consumers keep the "
            "old manifest.")


def _require_pyproject_sync() -> None:
    """Refuse when a plugin's pyproject.toml states a version its plugin.json
    disagrees with.

    Enforced at pre-commit by scripts/check_pyproject_sync.py, but that hook is
    skippable two ways -- --no-verify and PLUGINS_KIT_SKIP_BUMP_CHECK=1, whose
    DOCUMENTED purpose is legitimate dev-branch commits between publish
    checkpoints. So nothing enforced this at the moment it reaches consumers,
    and bootstrap's stated version duly drifted across five releases. This gate
    honours no escape hatch: the env var is a commit-time allowance, not a
    publish-time one, and is deliberately not consulted here (find_drift is
    called directly, bypassing the checker's main()).
    """
    module = _load_rule_module("check_pyproject_sync.py")
    drift = module.find_drift(REAL_PLUGINS_DIR)
    if drift:
        raise PublishError(
            "refusing: pyproject.toml versions disagree with the "
            "authoritative plugin.json:\n  "
            + "\n  ".join(drift)
            + "\n\nplugin.json is the source of truth -- set each "
              "pyproject.toml version equal to it and commit that. "
              "(PLUGINS_KIT_SKIP_BUMP_CHECK is a commit-time allowance; it "
              "does not apply to a publish.)")


def _range_commits() -> list[str]:
    """The commits a publish would land on master, newest first."""
    return git("rev-list", f"{range_base()}..{DEV_BRANCH}").split()


def _commit_files(sha: str) -> list[str]:
    """Repo-relative paths a commit touched."""
    return [f for f in git("show", "--name-only", "--format=", sha).split("\n") if f]


def excluded_dev_only_commits(allow: set[str]) -> dict[str, set[str]]:
    """Which commits in the range touch a dev-only plugin, so must NOT ship.

    `published: false` is a standing decision that a plugin does not go to
    consumers, so a shared dev branch carrying its commits is the NORMAL state,
    not an operator error. This returns those commits for EXCLUSION; it does not
    refuse. push_and_merge() then publishes a FILTERED release built from the
    remaining commits, and master never receives the excluded ones.

    That is a change of default, and the reason is worth keeping. This used to
    refuse the whole publish and tell the operator to cherry-pick by hand, which
    let one team's in-flight work block every other team's finished work, and
    pushed the exact filtering the field already implies onto a human doing it
    under time pressure -- by hand, against master, with no verification.
    Deciding what ships is still not this script's job: the `published` field is
    where that decision was already recorded, and honouring it is not a guess.

    SHIPPING IS NOW THE DEFAULT, and `allow` normally arrives holding every
    dev-only plugin (main() builds it that way). The filtering below is opt-in
    per plugin via --exclude-dev-only.

    The reason: excluding a plugin's commits does not keep it off master for
    long -- its files are already there from before it was marked dev-only, or
    they arrive with the first mixed commit -- so the exclusion bought a master
    tree that only PARTIALLY matched dev, which is the state the projection
    machinery exists to avoid. What actually keeps a dev-only plugin from
    consumers is `published: false` filtering it out of `marketplace.json`, and
    that is untouched here: a shipped-but-unpublished plugin has source on
    master and no way to install it. So the field still records the decision;
    it just no longer implies a divergent tree.

    --exclude-dev-only restores the old behaviour for a plugin whose source
    genuinely must not appear on a public master. Both the allowances and the
    exclusions are printed so they stay visible in the publish log.
    """
    dev_only = {name for name, m in local_plugins().items() if not is_published(m)}
    unknown = allow - dev_only
    if unknown:
        raise PublishError(
            "--allow-dev-only names plugins that are not dev-only here: "
            + ", ".join(sorted(unknown)))
    for plugin in sorted(allow):
        print(f"  shipping dev-only plugin's commits: {plugin} "
              f"(default; --exclude-dev-only holds one back)")
    dev_only -= allow
    if not dev_only:
        return {}

    offenders: dict[str, set[str]] = {}
    for sha in _range_commits():
        for f in _commit_files(sha):
            for plugin in dev_only:
                if f.startswith(f"plugins/{plugin}/"):
                    offenders.setdefault(sha, set()).add(plugin)
    return offenders


def _shippable_dirty_paths(held_back: set[str] | None = None) -> list[str]:
    """Uncommitted paths that could affect a consumer, in `git status` form.

    A dev-only (`published: false`) plugin's own files are EXCLUDED, and not
    merely tolerated -- they are not reported either. The dirty gate exists so a
    publish cannot silently omit work a consumer would otherwise receive, and
    for these paths that cannot happen twice over: the plugin is absent from
    `marketplace.json`, so nobody can install it, and the change is uncommitted,
    so it would not ship even from a published plugin. Reporting them makes the
    shared tree unpublishable for a reason the operator cannot act on -- another
    session's in-flight work on a plugin that reaches nobody -- and the only way
    to clear it is to commit someone else's half-finished work, which is the one
    thing this repo's git discipline forbids.

    No flag forces a dev-only plugin INTO a release, so there is no case to
    carve out here: `published: false` is a standing decision and
    `--exclude-dev-only` only holds such a plugin's SOURCE back further. Should
    a force-publish flag ever exist, this is the function that has to learn
    about it -- a plugin being forced into a release makes its uncommitted work
    shippable again, and therefore the operator's business.

    Deleted paths are read from the status line rather than a diff so a rename
    or a staged deletion is judged by the same rule as an edit.

    `-uall` is load-bearing, not tidiness: plain porcelain collapses a wholly
    untracked DIRECTORY into a single entry for the directory itself, so a new
    folder inside a dev-only plugin arrives as its parent path and matches no
    plugin. Listing untracked files individually makes every path judgable on
    its own.

    `held_back` extends the exemption to the published plugins a partial
    release (--only) holds at master's content: their uncommitted work cannot
    ship either, for the same reason, and refusing on it would make --only
    useless in exactly the situation it exists for -- another session mid-way
    through a plugin this release does not touch.
    """
    dev_only = {name for name, m in local_plugins().items() if not is_published(m)}
    dev_only |= held_back or set()
    kept = []
    for line in git("status", "--porcelain", "-uall").splitlines():
        if not line.strip():
            continue
        # Porcelain v1: two status columns, a space, then the path. A rename
        # carries "old -> new"; judge the DESTINATION, which is where the
        # content lands.
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip().strip('"')
        if _dev_only_owned(path, dev_only):
            continue
        kept.append(line)
    return kept


def _dev_only_owned(path: str, dev_only: set[str]) -> bool:
    """True when this path belongs to a dev-only plugin's own tree."""
    for plugin in dev_only:
        if path.startswith(f"plugins/{plugin}/") or path.startswith(f"tests/{plugin}/"):
            return True
    return False


def _refuse_mixed_dev_only_commit(excluded: dict[str, set[str]]) -> None:
    """Refuse a commit touching BOTH a dev-only plugin and shippable files.

    Such a commit cannot be excluded or included without being wrong either way:
    dropping it withholds work that was bumped for release, taking it puts
    dev-only files on master. Splitting someone else's commit is a judgement
    call, so this stops and names it.

    This now fires only for a plugin the operator named in --exclude-dev-only,
    because shipping is otherwise the default and nothing is excluded. Someone
    who asked for an exclusion is asking for a master tree that diverges, and
    a mixed commit is exactly where that request cannot be honoured silently.
    """
    dev_only = {name for name, m in local_plugins().items() if not is_published(m)}
    mixed = []
    for sha in sorted(excluded):
        for f in _commit_files(sha):
            if not _dev_only_owned(f, dev_only):
                mixed.append((sha, f, sorted(excluded[sha])))
                break
    if not mixed:
        return
    lines = []
    for sha, path, plugins in mixed:
        subject = git("log", "-1", "--format=%s", sha)
        lines.append(f"  {sha[:9]} {subject}  "
                     f"[dev-only: {', '.join(plugins)}; also touches {path}]")
    raise PublishError(
        "refusing: commit(s) touch BOTH a dev-only plugin and files that would "
        "otherwise ship:\n" + "\n".join(lines)
        + "\n\nExcluding them would withhold shippable work; including them "
          "would put dev-only files on master. Split the commit, or drop the "
          "plugin from --exclude-dev-only (shipping is the default).")


def _require_version_bump(only: set[str] | None = None) -> list[str]:
    """A merge without a version bump is not a publish -- the cache keys on
    version, so consumers never refetch. Refuse rather than ship a no-op.

    Under --only, judge only the plugins that ship: a bump elsewhere on dev is
    not this release's bump."""
    bumps = []
    for name, manifest in local_plugins().items():
        if not is_published(manifest):
            continue
        if only and name not in only:
            continue
        new = manifest.get("version")
        old = version_at(f"{REMOTE}/{MASTER_BRANCH}", name)
        if old != new:
            bumps.append(f"{name}: {old or '(new)'} -> {new}")
    if not bumps:
        raise PublishError(
            ("none of the --only plugins" if only else "no published plugin")
            + "'s version differs from "
            f"{REMOTE}/{MASTER_BRANCH}. The plugin cache keys on version, so a "
            "merge without a bump changes nothing for users. Bump the version "
            "you mean to release.")
    return bumps


def _changed_plugins() -> set[str]:
    """Published plugins whose files DIFFER between master and the dev tip.

    The commit walk finds candidates; the net diff decides. Both halves are
    needed and the second is the one that is easy to omit: after a filtered
    release, master carries cherry-picked equivalents of dev commits, so those
    commits are still in the range and still name the plugin's files while
    master already holds their content byte-for-byte. Reporting those as
    unbumped demands a version bump that would ship nothing -- burning a
    version number and pushing a no-op refetch to every consumer, which is a
    milder form of the same waste gotcha 3 describes.

    So a plugin is "changed" only when master would actually receive different
    bytes. `git diff --quiet` exits 1 when they differ, 0 when they do not.
    """
    known = set(local_plugins())
    candidates = set()
    for sha in _range_commits():
        for path in _commit_files(sha):
            parts = path.split("/")
            if len(parts) > 2 and parts[0] == "plugins" and parts[1] in known:
                candidates.add(parts[1])
    changed = set()
    for name in candidates:
        differs = subprocess.run(
            ["git", "diff", "--quiet",
             f"{REMOTE}/{MASTER_BRANCH}..{DEV_BRANCH}", "--", f"plugins/{name}"],
            cwd=REPO_ROOT, capture_output=True, text=True).returncode != 0
        if differs:
            changed.add(name)
    return changed


def _require_bump_for_changed_plugins(held_back: set[str] | None = None) -> None:
    """Every published plugin whose FILES changed in the range must be bumped.

    _require_version_bump only asserts that SOMETHING was bumped, which is a
    weaker rule than the pre-commit bump gate it backstops, and the gap is
    reachable from two individually-sanctioned actions: session A commits
    plugin X with PLUGINS_KIT_SKIP_BUMP_CHECK=1 (sanctioned on dev), session B
    later publishes an unrelated plugin Y's bump (sanctioned). X's changed
    files then ship to consumers under a version string they already hold, and
    because the cache keys on version they never refetch -- silent divergence,
    CLAUDE.md's gotcha 3.

    Dev-only (published: false) plugins are exempt: they have no consumers and
    no cache entry to invalidate. --allow-dev-only therefore does not interact
    with this check -- it ships files, not a marketplace listing, so a bump
    would mean nothing.

    A plugin a partial release holds back (`held_back`) is exempt for THIS
    release only: its files stay at master's content, so nothing of it ships.
    The range base does not move on a partial release, so its commits are
    still in the range and this same check judges them at the next full
    publish.
    """
    offenders = []
    for name in sorted(_changed_plugins()):
        manifest = local_plugins()[name]
        if not is_published(manifest):
            continue
        if held_back and name in held_back:
            continue
        new = manifest.get("version")
        old = version_at(f"{REMOTE}/{MASTER_BRANCH}", name)
        if old is not None and old == new:
            offenders.append(f"{name}: files changed, still {new}")
    if offenders:
        raise PublishError(
            "refusing: published plugin(s) have files changed in "
            f"{REMOTE}/{MASTER_BRANCH}..{DEV_BRANCH} but no version bump:\n  "
            + "\n  ".join(offenders)
            + "\n\nThe plugin cache keys on version, so those changes would "
              "ship under a version consumers already have and would never be "
              "refetched (CLAUDE.md gotcha 3). Bump each plugin's version in "
              "its .claude-plugin/plugin.json (and its pyproject.toml, if it "
              "states one) and commit that.")


# --- derived artifacts -----------------------------------------------------

def regenerate(root: Path | None = None) -> bool:
    """Regenerate marketplace.json and index.html. True if anything changed.

    index.html renders the versions and skill roster read from the installPaths,
    so dev-tree must point them at THIS working copy for the page to show what
    is about to be published. Restoring is a finally: leaving the tree flipped
    silently loads plugins from the working copy in the next session.

    generate.py's default job is to describe the MACHINE it runs on -- every
    input it reads is local state. Publishing needs the opposite: a page that
    describes this repo at this release and comes out identical on any
    maintainer's machine. Every flag below redirects one of those inputs at the
    working copy, and each is load-bearing rather than decorative:

    --marketplace scopes the page to this marketplace. Without it the page
    includes every marketplace with a poster.yaml installed on the generating
    machine -- on a machine carrying a private marketplace, that publishes it to
    a public repo. It is the one flag whose omission leaks rather than merely
    misreports, so verify() re-checks the result.

    --public drops the on/off/installed badges, which report THIS machine's
    enabledPlugins -- meaningless-to-wrong on a page checked in for other people.

    --marketplace-json points the phantom-install filter at the listing
    regenerated above. The CACHED listing lags the source by one publish, so a
    plugin introduced in this release is absent from it and gets filtered off its
    own release's page (how hue-kit 0.7.0 shipped a page missing
    bootstrap-stuck-fix 0.1.0).

    --poster reads the marketplace's subtitle and url from the repo for the same
    reason, and additionally removes the requirement that the publishing machine
    have plugins-kit installed as a marketplace at all.

    --config supplies the page copy from the repo instead of the maintainer's
    ~/.claude/.local-data/awesome-kit/plugin-ecosystem-poster.yaml.

    `root` selects WHICH checkout is described. Default: this working copy.
    A partial release passes the projection worktree instead, so the page and
    listing describe the tree master is about to hold -- with the held-back
    plugins at master's versions -- rather than dev. Every script is then the
    worktree's own copy, because each resolves its repo root from its own file
    location (dev-tree.py flips installPaths at ITS repo, which is what makes
    the generator read the worktree's manifests).
    """
    if root is None:
        regen_py, dev_tree_py, generate_py = REGEN_MARKETPLACE_PY, DEV_TREE_PY, GENERATE_PY
        marketplace_json, poster_yaml = MARKETPLACE_JSON, POSTER_YAML
        index_page_yaml, index_html = INDEX_PAGE_YAML, INDEX_HTML
    else:
        regen_py = root / REGEN_MARKETPLACE_PY.relative_to(REPO_ROOT)
        dev_tree_py = root / DEV_TREE_PY.relative_to(REPO_ROOT)
        generate_py = root / GENERATE_PY.relative_to(REPO_ROOT)
        marketplace_json = root / MARKETPLACE_JSON.relative_to(REPO_ROOT)
        poster_yaml = root / POSTER_YAML.relative_to(REPO_ROOT)
        index_page_yaml = root / INDEX_PAGE_YAML.relative_to(REPO_ROOT)
        index_html = root / INDEX_HTML.relative_to(REPO_ROOT)

    run([sys.executable, str(regen_py)], "marketplace.json regen")

    run([sys.executable, str(dev_tree_py), "dev"], "dev-tree dev")
    try:
        run([sys.executable, str(generate_py),
             "--marketplace", MARKETPLACE_NAME,
             "--marketplace-json", f"{MARKETPLACE_NAME}={marketplace_json}",
             "--poster", f"{MARKETPLACE_NAME}={poster_yaml}",
             "--config", str(index_page_yaml),
             "--title", PAGE_TITLE,
             "--output", str(index_html),
             "--public",
             "--no-open"], "index.html regen")
    finally:
        run([sys.executable, str(dev_tree_py), "normal"], "dev-tree normal")

    if root is not None:
        return _rc_in(root, "diff", "--quiet", "--", *sorted(GENERATED_PATHS)) != 0
    return bool(git("status", "--porcelain"))


def commit_derived(bumps: list[str]) -> None:
    """Land the derived artifacts in the release commit.

    Amend when HEAD is unpushed, so index.html rides INSIDE the release commit
    and master never holds a page that disagrees with its own marketplace.json.
    When HEAD is already pushed, amending would rewrite published history --
    make a follow-up commit instead; dev's tip is still correct before the
    merge, which is what master inherits.
    """
    git("add", str(MARKETPLACE_JSON), str(INDEX_HTML))

    head = git("rev-parse", "HEAD")
    pushed = git("branch", "-r", "--contains", head, check=False)

    if pushed:
        git("commit", "-m",
            "publish: regenerate derived artifacts\n\n" + "\n".join(bumps))
        print("  committed derived artifacts (HEAD was already pushed, "
              "so not amended)")
    else:
        git("commit", "--amend", "--no-edit")
        print("  amended derived artifacts into the release commit")


# --- publish + verify ------------------------------------------------------

def _in_worktree(workdir, *args: str) -> str:
    """Run git inside the projection worktree, surfacing failures verbatim."""
    result = subprocess.run(["git", "-C", str(workdir), *args],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise PublishError(
            f"projecting the release onto {MASTER_BRANCH} failed at "
            f"`git {' '.join(args)}`:\n"
            + ((result.stderr or result.stdout).strip() or "(no output)"))
    return result.stdout.strip()


def _master_is_ancestor_of_dev() -> bool:
    return _rc("merge-base", "--is-ancestor",
               f"{REMOTE}/{MASTER_BRANCH}", DEV_BRANCH) == 0


def _fast_forward_is_safe() -> bool:
    """True when a fast-forward cannot carry dev-only work onto master.

    A fast-forward moves dev's TREE wholesale, so it bypasses the hold-back
    _publish_projection applies. The exclusion set does not guard that: it is
    populated per COMMIT from the publish range, so a range touching no
    dev-only plugin leaves it empty while the dev-only files sit in dev's tree
    ready to ship. What a fast-forward moves is the tree, so the question has
    to be asked of the manifests, not of the range.

    Deliberately asks whether a dev-only plugin EXISTS rather than whether
    _held_back_paths finds files for it. The two agree in every real case -- a
    declared plugin always has at least its own manifest on disk -- but
    _held_back_paths lists trees with check=False, so a failing ls-tree
    returns empty and would be indistinguishable from "nothing to hold back".
    This guard protects a push to a public master, so it must not have a
    branch on which a git failure reads as safe.

    Unreachable while master is not an ancestor of dev, which a projection
    release guarantees. It becomes reachable the moment anyone merges master
    back into dev -- a move publish-reconcile.md explicitly contemplates --
    and it is exactly then that the empty exclusion set stops meaning what it
    appears to mean.
    """
    return not any(not is_published(m) for m in local_plugins().values())


def _held_back_paths(dev_only: set[str]) -> tuple[list[str], list[str]]:
    """The dev-only files to hold at master's content: (on master, dev-only new).

    The UNION of both trees, because the two halves need opposite treatment. A
    file master already carries is restored to master's version -- the plugin
    stays exactly where it was published. A file that exists only on dev has
    never shipped and must be removed from the projected tree entirely; taking
    dev's tree wholesale and forgetting this half is how unshipped work leaks.
    """
    if not dev_only:
        return [], []
    master = f"{REMOTE}/{MASTER_BRANCH}"
    prefixes = [f"{top}/{name}/" for name in sorted(dev_only)
                for top in ("plugins", "tests")]
    on_master, dev_new, seen = [], [], set()
    for ref in (master, DEV_BRANCH):
        listing = git("ls-tree", "-r", "--name-only", "-z", ref, "--", *prefixes,
                      check=False)
        for path in listing.split("\0"):
            path = path.strip()
            if not path or path in seen:
                continue
            seen.add(path)
            (on_master if blob_at(master, path) else dev_new).append(path)
    return sorted(on_master), sorted(dev_new)


def _projection_message(shipping: list[str], excluded: dict[str, set[str]],
                        dev_sha: str, only: set[str] | None = None) -> str:
    """The projection's commit message: what shipped, what did not, and from where.

    The `Published-From:` trailer is load-bearing, not a courtesy -- range_base()
    reads it back on the next publish. Without it the next run has only ancestry
    to go on, which is the thing that broke.

    A partial release (`only`) must NOT carry it: the trailer says "everything
    up to this dev sha has shipped", and a partial release has shipped one
    plugin's slice of it. Stamping it would drop the held-back plugins' commits
    out of the next range, and with them the per-plugin bump gate that judges
    their files. It records `Published-Only:` and `Built-From:` instead.
    """
    if only:
        names = ", ".join(sorted(only))
        lines = [f"publish --only {names}: {len(shipping)} commit(s) from "
                 f"{DEV_BRANCH}", ""]
    else:
        lines = [f"publish: {len(shipping)} commit(s) from {DEV_BRANCH}", ""]
    for sha in reversed(shipping):
        lines.append(f"  {sha[:9]} {git('log', '-1', '--format=%s', sha)}")
    if excluded:
        held = sorted({p for plugins in excluded.values() for p in plugins})
        lines += ["", f"Held back on {DEV_BRANCH} ({len(excluded)} commit(s), "
                      f"dev-only): {', '.join(held)}"]
    if only:
        held = sorted(held_back_for(only))
        if held:
            lines += ["", f"Held back on {DEV_BRANCH} (published, not "
                          f"selected): {', '.join(held)}"]
        lines += ["", f"{PUBLISHED_ONLY} {', '.join(sorted(only))}",
                  f"{BUILT_FROM} {dev_sha}"]
    else:
        lines += ["", f"{PUBLISHED_FROM} {dev_sha}"]
    return "\n".join(lines)


def _commit_touches(sha: str, plugins: set[str]) -> bool:
    prefixes = tuple(f"{top}/{name}/" for name in plugins for top in ("plugins", "tests"))
    return any(f.startswith(prefixes) for f in _commit_files(sha))


def _regenerate_derived_in(workdir: Path) -> None:
    """Rebuild the derived artifacts from the projected tree and stage them.

    A module-level seam so the tests, whose fixture repo has no generator, can
    stand in a stub for it.
    """
    if regenerate(root=workdir):
        _in_worktree(workdir, "add", "--", *sorted(GENERATED_PATHS))


def _publish_projection(excluded: dict[str, set[str]],
                        only: set[str] | None = None) -> None:
    """Land one commit on master whose tree is dev's, minus the dev-only plugins.

    This is the whole filtered release. It cannot conflict, because nothing is
    being merged: the tree is computed, not negotiated. It is idempotent, so
    running it twice is a no-op rather than a duplicate-work conflict. And it
    produces by construction exactly the invariant verify() checks -- master
    matches dev everywhere except the excluded plugins' own files.

    With `only`, the hold-back set grows by every published plugin not named,
    and the derived artifacts are regenerated from the projected tree rather
    than taken from dev, so master's listing describes master.
    """
    dev_only = {name for name, m in local_plugins().items() if not is_published(m)}
    held = dev_only | held_back_for(only)
    shipping = [sha for sha in _range_commits() if sha not in excluded]
    if only:
        shipping = [sha for sha in shipping if _commit_touches(sha, only)]
    if not shipping:
        raise PublishError(
            "every commit in the range touches a dev-only plugin -- there is "
            "nothing to publish. The bumps that passed preflight are on "
            "commits that cannot ship.")

    dev_sha = git("rev-parse", DEV_BRANCH)
    on_master, dev_new = _held_back_paths(held)

    workdir = Path(tempfile.mkdtemp(prefix="publish-master-"))
    try:
        git("worktree", "add", "--detach", str(workdir), f"{REMOTE}/{MASTER_BRANCH}")
        try:
            # Index and worktree := dev's tree, then put the dev-only plugins
            # back the way master had them.
            _in_worktree(workdir, "read-tree", "--reset", "-u", DEV_BRANCH)
            if on_master:
                _in_worktree(workdir, "checkout", f"{REMOTE}/{MASTER_BRANCH}",
                             "--", *on_master)
            if dev_new:
                _in_worktree(workdir, "rm", "-q", "-f", "--ignore-unmatch",
                             "--", *dev_new)
            if only:
                _regenerate_derived_in(workdir)

            if _rc_in(workdir, "diff", "--cached", "--quiet", "HEAD") == 0:
                print(f"  {MASTER_BRANCH} already carries this content -- "
                      f"nothing to push")
                return

            # --no-verify: the pre-commit gates already ran against dev, and
            # this tree is a computed artifact rather than an authored change.
            _in_worktree(workdir, "commit", "--no-verify", "-q", "-m",
                         _projection_message(shipping, excluded, dev_sha, only))
            _in_worktree(workdir, "push", REMOTE,
                         f"HEAD:refs/heads/{MASTER_BRANCH}")
            print(f"  projected {len(shipping)} commit(s) onto {MASTER_BRANCH}"
                  + (f"; held back {len(excluded)} dev-only commit(s)"
                     if excluded else "")
                  + (f"; held back {', '.join(sorted(held_back_for(only)))}"
                     if only and held_back_for(only) else ""))
        finally:
            git("worktree", "remove", "--force", str(workdir), check=False)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _rc_in(workdir, *args: str) -> int:
    return subprocess.run(["git", "-C", str(workdir), *args],
                          capture_output=True, text=True).returncode


def push_and_merge(excluded: dict[str, set[str]] | None = None,
                   only: set[str] | None = None) -> None:
    """Push dev, then land the release on master.

    Fast-forward only when nothing is excluded, master is still an ancestor
    of dev, AND no dev-only file exists on either tree (_fast_forward_is_safe).
    All three are needed: a fast-forward moves dev's tree wholesale, so an
    empty exclusion set alone would ship every dev-only plugin the range
    happened not to touch. Otherwise project (see _publish_projection): master
    gets dev's tree with the dev-only plugins held back, and dev is untouched.

    The worktree in the projection path is not a stylistic choice. The
    fast-forward path checks master out in THIS tree, which is shared with
    other agent sessions -- their commits would land on whatever branch the
    tree is on. That risk is tolerable for the seconds a fast-forward takes;
    anything that can stop partway is a different matter, so the projection
    never moves this tree.
    """
    git("push", REMOTE, DEV_BRANCH)
    print(f"  pushed {DEV_BRANCH}")

    if (not excluded and not only and _master_is_ancestor_of_dev()
            and _fast_forward_is_safe()):
        git("checkout", MASTER_BRANCH)
        try:
            git("merge", "--ff-only", DEV_BRANCH)
            git("push", REMOTE, MASTER_BRANCH)
            print(f"  fast-forwarded and pushed {MASTER_BRANCH}")
        finally:
            git("checkout", DEV_BRANCH)
        return

    _publish_projection(excluded or {}, only)


def check_index_scope(index_text: str) -> list[str]:
    """Refuse an index.html that describes anything but this marketplace.

    regenerate() scopes the page with --marketplace, and dropping that flag does
    not fail or look wrong -- it silently adds every OTHER marketplace installed
    on the generating machine, private ones included, and commits them to a
    public repo. That failure is invisible in a diff-free glance at a 100KB
    generated file, so it is checked against the artifact rather than trusted to
    the invocation. The same parse catches a --public regression, which would
    embed this machine's enabledPlugins.
    """
    match = re.search(r"^const data = (\{.*\});$", index_text, re.MULTILINE)
    if not match:
        return ["index.html does not embed a parseable data block -- "
                "the generator's output shape changed; update check_index_scope"]
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        return [f"index.html data block is not valid JSON: {exc}"]

    problems = []
    foreign = sorted({p.get("marketplace") for p in data.get("plugins", [])
                      if p.get("marketplace") != MARKETPLACE_NAME})
    foreign += [m for m in data.get("marketplace_order", [])
                if m != MARKETPLACE_NAME and m not in foreign]
    if foreign:
        problems.append(
            f"index.html describes marketplaces other than {MARKETPLACE_NAME}: "
            f"{', '.join(str(m) for m in foreign)} -- this page ships to a public "
            f"repo. regenerate() must pass --marketplace {MARKETPLACE_NAME}.")
    if any("state" in p for p in data.get("plugins", [])):
        problems.append(
            "index.html embeds per-plugin state, which describes the generating "
            "machine's enabledPlugins. regenerate() must pass --public.")
    return problems


def verify(only: set[str] | None = None) -> list[str]:
    """Post-publish verification. Returns a list of problems (empty = good).

    Identical tips are the strongest possible result, but they are only
    reachable on the fast-forward path. A projection gives master a commit dev
    has never seen, so sha identity is the wrong question there -- the contract
    is a CONTENT one: master must match dev everywhere except the dev-only
    plugins' own files. That check is correct in both cases, so it runs
    whenever the tips differ, whether or not this release excluded anything.

    After a partial release (`only`) the held-back plugins are ALLOWED to
    differ, and so are the derived artifacts, which master regenerated from
    its own tree. What must then hold instead is that master's listing agrees
    with master's manifests -- the only-plugins at their new versions, the
    held-back ones at the versions master still carries.
    """
    problems = []
    held_back = held_back_for(only)

    git("fetch", REMOTE, "--quiet")
    dev_sha = git("rev-parse", f"{REMOTE}/{DEV_BRANCH}")
    master_sha = git("rev-parse", f"{REMOTE}/{MASTER_BRANCH}")
    if dev_sha != master_sha:
        dev_only = {n for n, m in local_plugins().items() if not is_published(m)}
        diff = git("diff", "--name-only", f"{REMOTE}/{MASTER_BRANCH}",
                   f"{REMOTE}/{DEV_BRANCH}", "--")
        leaked = [f for f in diff.splitlines()
                  if f.strip()
                  and not _dev_only_owned(f.strip(), dev_only | held_back)
                  and not (held_back and f.strip() in GENERATED_PATHS)]
        if leaked:
            problems.append(
                f"{REMOTE}/{MASTER_BRANCH} differs from {REMOTE}/{DEV_BRANCH} "
                f"outside the dev-only plugins, so shippable work did not "
                f"land: {', '.join(leaked[:8])}"
                + (f" (+{len(leaked) - 8} more)" if len(leaked) > 8 else ""))
        for name in sorted(dev_only):
            on_master = git("ls-tree", "-r", "--name-only",
                            f"{REMOTE}/{MASTER_BRANCH}", f"plugins/{name}/",
                            check=False)
            changed = [f for f in diff.splitlines()
                       if f.startswith(f"plugins/{name}/")]
            if on_master and changed:
                print(f"  note: {name} stays at its existing master version "
                      f"({len(changed)} file(s) held back on {DEV_BRANCH})")

    # A bare publish regenerated dev's artifacts and projected them, so dev's
    # files are the ones to judge against dev's manifests. A partial release
    # left dev alone and regenerated INSIDE the projection, so the artifacts
    # to judge are master's, against the manifests master carries -- the
    # only-plugins at their new versions, the held-back ones where they were.
    if only:
        master = f"{REMOTE}/{MASTER_BRANCH}"
        where = f"{master} "
        marketplace_text = git(
            "show", f"{master}:{MARKETPLACE_JSON.relative_to(REPO_ROOT).as_posix()}")
        index_text = git("show", f"{master}:{INDEX_HTML.relative_to(REPO_ROOT).as_posix()}")
        expected = {name: version_at(master, name) for name in local_plugins()}
    else:
        where = ""
        marketplace_text = MARKETPLACE_JSON.read_text(encoding="utf-8")
        index_text = INDEX_HTML.read_text(encoding="utf-8")
        expected = {name: m.get("version") for name, m in local_plugins().items()}
    marketplace = json.loads(marketplace_text)
    listed = {p["name"]: p.get("version") for p in marketplace.get("plugins", [])}
    problems.extend(check_index_scope(index_text))

    for name, manifest in local_plugins().items():
        if not is_published(manifest):
            if name in listed:
                problems.append(f"dev-only plugin {name} is listed in marketplace.json")
            continue
        version = expected[name]
        if listed.get(name) != version:
            problems.append(
                f"{where}marketplace.json has {name}={listed.get(name)}, "
                f"plugin.json has {version}"
                + (" (held back)" if name in held_back else ""))
        # A poster-hidden plugin is published but intentionally off the page;
        # asserting its presence would fail every publish while it ships.
        if is_poster_hidden(name):
            if f'"name": "{name}"' in index_text:
                problems.append(
                    f"index.html shows {name}, which opts out via poster.yaml hidden: true")
        elif f'"name": "{name}", "version": "{version}"' not in index_text:
            problems.append(f"{where}index.html does not show {name} {version}")

    # The dev-tree restore is the failure that bites silently later: a flipped
    # tree makes the next session load plugins from this working copy.
    status = subprocess.run(
        [sys.executable, str(DEV_TREE_PY), "status"],
        cwd=REPO_ROOT, capture_output=True, text=True).stdout
    if "installPaths @ dev : 0" not in status:
        problems.append(
            "dev-tree is NOT restored to normal -- installPaths still point at "
            f"this working copy. Run: python {DEV_TREE_PY.name} normal\n{status}")

    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Publish the plugins-kit marketplace (dev -> master).")
    parser.add_argument(
        "--check", action="store_true",
        help="preflight and verify only; make no writes and no pushes")
    parser.add_argument(
        "--exclude-dev-only", action="append", default=[], metavar="PLUGIN",
        help="hold this dev-only (published: false) plugin's commits back from "
             "master, giving a master tree that diverges from dev "
             "(repeatable). Shipping every dev-only plugin is the DEFAULT; "
             "`published: false` is what keeps a plugin out of the marketplace "
             "listing, and that is unaffected either way")
    parser.add_argument(
        "--allow-dev-only", action="append", default=[], metavar="PLUGIN",
        help=argparse.SUPPRESS)
    parser.add_argument(
        "--only", action="append", default=[], metavar="PLUGIN",
        help="partial release: ship this published plugin's files and hold "
             "every other published plugin at master's content (repeatable). "
             "Does not advance the publish range, so the held-back work still "
             "ships whole at the next bare publish. Does not check cross-plugin "
             "coupling -- use it for a self-contained change")
    parser.add_argument(
        "--print-range-base", action="store_true",
        help="print the commit `..dev` is measured from and exit, making no "
             "writes and no network calls. scripts/check-staged-version-bump.sh "
             "asks this so both gates measure a bump from the same publish "
             "point rather than each deriving one")
    args = parser.parse_args(argv)

    # Answered before anything else, because the caller is a pre-commit hook on
    # a possibly unprovisioned clone: no preflight, no manifest reads, no writes.
    if args.print_range_base:
        try:
            print(range_base())
        except PublishError as exc:
            print(f"range base unavailable: {exc}", file=sys.stderr)
            return 1
        return 0

    try:
        print("preflight:")
        # Shipping every dev-only plugin's commits is the default; the
        # operator opts one OUT, rather than opting each one in.
        allow = {name for name, m in local_plugins().items() if not is_published(m)}
        held_back = set(args.exclude_dev_only)
        unknown = held_back - allow
        if unknown:
            raise PublishError(
                "--exclude-dev-only names plugins that are not dev-only here: "
                + ", ".join(sorted(unknown)))
        for plugin in sorted(held_back):
            print(f"  holding dev-only plugin back from master: {plugin} "
                  f"(operator decision via --exclude-dev-only)")
        only = set(args.only) or None
        if only:
            unknown = only - published_plugins()
            if unknown:
                raise PublishError(
                    "--only names plugins that are not published plugins "
                    "here: " + ", ".join(sorted(unknown)))
            print(f"  partial release: {', '.join(sorted(only))} only; "
                  f"holding back: "
                  f"{', '.join(sorted(held_back_for(only))) or '(none)'}")
        bumps, excluded = preflight(allow - held_back, only)
        for bump in bumps:
            print(f"  publishing {bump}")
        if excluded:
            print(f"  excluding {len(excluded)} commit(s) for dev-only "
                  f"(published: false) plugin(s) -- they stay on {DEV_BRANCH}:")
            for sha in sorted(excluded):
                subject = git("log", "-1", "--format=%s", sha)
                plugins = ", ".join(sorted(excluded[sha]))
                print(f"    {sha[:9]} {subject}  [{plugins}]")

        if args.check:
            print("\n--check: preflight passed; no changes made.")
            return 0

        print("\nregenerating derived artifacts:")
        if only:
            # Nothing on dev is touched. The dirty gate has let held-back
            # plugins' uncommitted work through, so a working-tree regen here
            # would bake their unpublished manifests into dev's listing, and
            # commit_derived's commit would sweep another session's staged
            # work along. master gets its own artifacts, regenerated from
            # the projected tree; dev's index.html catches up at the next
            # bare publish.
            print(f"  --only: {DEV_BRANCH}'s derived artifacts left as "
                  f"committed; {MASTER_BRANCH}'s are regenerated from the "
                  f"projection")
        elif regenerate():
            commit_derived(bumps)
        else:
            print("  already current (nothing to commit)")

        print("\npublishing:")
        push_and_merge(excluded, only)

    except PublishError as exc:
        # Flush first: stdout is block-buffered when piped and stderr is not, so
        # without this the refusal prints ABOVE the steps it refused at.
        sys.stdout.flush()
        print(f"\npublish refused: {exc}", file=sys.stderr)
        return 1

    print("\nverifying:")
    problems = verify(only)
    if problems:
        sys.stdout.flush()
        for problem in problems:
            print(f"  FAILED: {problem}", file=sys.stderr)
        return 1

    print("  origin/dev == origin/master"
          if git("rev-parse", f"{REMOTE}/{DEV_BRANCH}")
          == git("rev-parse", f"{REMOTE}/{MASTER_BRANCH}")
          else ("  origin/master carries the --only plugin(s); the rest of "
                "dev is held back" if only else
                "  origin/master carries every shippable commit; "
                "dev-only work held back"))
    print("  marketplace.json, index.html, and plugin.json agree")
    print("  dev-tree restored to normal")
    print("\npublished. Users with autoUpdate get it next session start.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
