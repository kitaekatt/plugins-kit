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
    the release is FILTERED instead: the shippable commits are replayed onto
    master in a temporary worktree and master is pushed from there. dev is
    untouched and keeps the excluded work. `published: false` already recorded
    the decision that the plugin does not ship, so honouring it is not the
    script guessing -- what it must never do is decide that a plugin's status
    has changed.
  - index.html must ride INSIDE the release commit, or master briefly holds a
    page that disagrees with its own marketplace.json.

What preflight refuses on (all of it unbypassable -- no environment variable
turns any of it off, which is the whole point of a gate that sits after the
escapable pre-commit hooks):

  - not on dev; a dirty tree; a merge that would not fast-forward; a range with
    nothing to publish.
  - a single commit touching BOTH a dev-only plugin and files that would
    otherwise ship. Excluding it withholds released work, including it puts
    dev-only files on master, and splitting someone else's commit is a
    judgement call. (Commits touching ONLY a dev-only plugin are excluded
    silently-but-reported, not refused.)
  - a plugin that does not declare the bootstrap dependency.
  - no published plugin bumped at all, AND any published plugin whose files
    changed in the range without a bump (the cache keys on version, so those
    files would ship under a version consumers already hold and never refetch).
  - a pyproject.toml version disagreeing with its plugin.json.
  - awesome-kit's generated orchestration policy drifting from its principles.

The last three exist as pre-commit hooks too, but those are skippable with
--no-verify (and PLUGINS_KIT_SKIP_BUMP_CHECK=1, whose documented purpose is
legitimate dev-branch commits between publish checkpoints). Skipping them on dev
is sanctioned; shipping the result is not, so publish re-runs them from the same
source of truth rather than restating the rules.

Usage:
  python scripts/publish.py            # preflight, publish, verify
  python scripts/publish.py --check    # preflight + verify only; no writes, no pushes
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
GENERATE_ORCHESTRATION_PY = SCRIPTS_DIR / "generate_orchestration.py"
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


# --- preflight -------------------------------------------------------------

def preflight(allow_dev_only: set[str] | None = None
              ) -> tuple[list[str], dict[str, set[str]]]:
    """Refuse anything unsafe. Returns (publish summary, excluded commits).

    Every check here refuses rather than fixes: a publish is visible to other
    machines, so guessing is worse than stopping. The ONE thing it no longer
    refuses is a range carrying dev-only commits -- those are excluded from the
    release instead, per excluded_dev_only_commits(). `allow_dev_only` names
    dev-only plugins whose commits the operator has decided to ship anyway
    (see --allow-dev-only).
    """
    if git("rev-parse", "--abbrev-ref", "HEAD") != DEV_BRANCH:
        raise PublishError(
            f"not on {DEV_BRANCH} (publish releases {DEV_BRANCH} -> "
            f"{MASTER_BRANCH}); checkout {DEV_BRANCH} first")

    dirty = git("status", "--porcelain")
    if dirty:
        raise PublishError(
            "working tree is dirty -- commit your work before publishing "
            "(the dev tree is shared by multiple agents/sessions, so uncommitted "
            "changes may be another session's in-flight work; commit them in "
            "scoped commits rather than stashing or discarding). This script "
            "owns the derived artifacts and the git flow, not your changes.\n"
            "Dirty files:\n" + dirty)

    git("fetch", REMOTE, "--quiet")

    behind = git("rev-list", "--count", f"{DEV_BRANCH}..{REMOTE}/{MASTER_BRANCH}")
    if behind != "0":
        raise PublishError(
            f"{REMOTE}/{MASTER_BRANCH} has {behind} commit(s) {DEV_BRANCH} lacks, "
            f"so the merge would not fast-forward. This is a reconcile, not a "
            f"routine publish -- resolve toward {DEV_BRANCH} by hand (see "
            f"CLAUDE.md on reconciles).")

    ahead = git("rev-list", "--count", f"{REMOTE}/{MASTER_BRANCH}..{DEV_BRANCH}")
    if ahead == "0":
        raise PublishError(
            f"{DEV_BRANCH} has nothing {REMOTE}/{MASTER_BRANCH} lacks -- "
            f"nothing to publish.")

    excluded = excluded_dev_only_commits(allow_dev_only or set())
    _refuse_mixed_dev_only_commit(excluded)
    _require_bootstrap_dependency()
    _require_pyproject_sync()
    _require_generated_orchestration()
    bumps = _require_version_bump()
    _require_bump_for_changed_plugins()
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


def _require_generated_orchestration() -> None:
    """Refuse when awesome-kit's generated orchestration policy has drifted.

    plugins/awesome-kit/skills/orchestrate/defaults/orchestration.yaml is
    GENERATED from docs/reference/orchestrate/tier-principles.md plus the
    skill's lexicon.md; a hand-edit or an unregenerated principles change makes
    the shipped policy disagree with its own source. Also a pre-commit check,
    also skippable with --no-verify, so it is re-run here where it reaches
    consumers.
    """
    result = subprocess.run(
        [sys.executable, str(GENERATE_ORCHESTRATION_PY), "--check"],
        cwd=REPO_ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stdout.strip() + "\n" + result.stderr.strip()).strip()
        raise PublishError(
            "refusing: the generated orchestration policy is not current "
            "(scripts/generate_orchestration.py --check failed).\n"
            "Regenerate it and commit the result:\n"
            "  uv run python scripts/generate_orchestration.py --write\n"
            "Never hand-edit orchestration.yaml -- the principles source is "
            "authoritative.\n" + detail)


def already_applied(sha_prefixes: list[str]) -> set[str]:
    """Which of these commits master already carries AS CONTENT.

    A filtered release replays commits onto master, which gives them new SHAs.
    The originals therefore stay in `master..dev` forever, and every LATER
    filtered publish would try to replay them again -- landing on an empty
    cherry-pick and aborting the whole publish. `git cherry` answers the right
    question (is an equivalent patch already upstream?) instead of the SHA
    identity question, so shipped work drops out of the range on its own.

    This does not arise on the fast-forward path, where dev and master share
    history; it is the cost of filtering, and it is paid here rather than by a
    human diagnosing a confusing abort on some future publish.
    """
    out = git("cherry", f"{REMOTE}/{MASTER_BRANCH}", DEV_BRANCH, check=False)
    applied = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "-":
            applied.add(parts[1])
    return applied


def _range_commits() -> list[str]:
    """The commits a publish would land on master, newest first."""
    return git("rev-list", f"{REMOTE}/{MASTER_BRANCH}..{DEV_BRANCH}").split()


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

    --allow-dev-only overrides per plugin, for finished work master's tree needs
    (e.g. a cross-plugin refactor). The allowance is printed so it stays visible
    in the publish log.
    """
    dev_only = {name for name, m in local_plugins().items() if not is_published(m)}
    unknown = allow - dev_only
    if unknown:
        raise PublishError(
            "--allow-dev-only names plugins that are not dev-only here: "
            + ", ".join(sorted(unknown)))
    for plugin in sorted(allow):
        print(f"  allowing dev-only plugin commits to ship: {plugin} "
              f"(operator decision via --allow-dev-only)")
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
    call, so this stops and names it. Refusing is still right HERE -- what
    changed above is only the case where the split is unambiguous.
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
          "would put dev-only files on master. Split the commit, or name the "
          "plugin in --allow-dev-only.")


def _require_version_bump() -> list[str]:
    """A merge without a version bump is not a publish -- the cache keys on
    version, so consumers never refetch. Refuse rather than ship a no-op."""
    bumps = []
    for name, manifest in local_plugins().items():
        if not is_published(manifest):
            continue
        new = manifest.get("version")
        old = version_at(f"{REMOTE}/{MASTER_BRANCH}", name)
        if old != new:
            bumps.append(f"{name}: {old or '(new)'} -> {new}")
    if not bumps:
        raise PublishError(
            "no published plugin's version differs from "
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


def _require_bump_for_changed_plugins() -> None:
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
    """
    offenders = []
    for name in sorted(_changed_plugins()):
        manifest = local_plugins()[name]
        if not is_published(manifest):
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

def regenerate() -> bool:
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
    """
    run([sys.executable, str(REGEN_MARKETPLACE_PY)], "marketplace.json regen")

    run([sys.executable, str(DEV_TREE_PY), "dev"], "dev-tree dev")
    try:
        run([sys.executable, str(GENERATE_PY),
             "--marketplace", MARKETPLACE_NAME,
             "--marketplace-json", f"{MARKETPLACE_NAME}={MARKETPLACE_JSON}",
             "--poster", f"{MARKETPLACE_NAME}={POSTER_YAML}",
             "--config", str(INDEX_PAGE_YAML),
             "--title", PAGE_TITLE,
             "--output", str(INDEX_HTML),
             "--public",
             "--no-open"], "index.html regen")
    finally:
        run([sys.executable, str(DEV_TREE_PY), "normal"], "dev-tree normal")

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

def _is_merge_commit(sha: str) -> bool:
    """True when the commit has more than one parent."""
    return len(git("rev-list", "--parents", "-n", "1", sha).split()) > 2


def push_and_merge(excluded: dict[str, set[str]] | None = None) -> None:
    """Push dev, then land the release on master.

    With nothing excluded this is the fast-forward it has always been. With
    dev-only commits in the range it is a FILTERED release: the shippable
    commits are replayed onto master in a temporary worktree and pushed from
    there, so master gets exactly the publishable work and dev is untouched.

    The worktree is not a stylistic choice. The fast-forward path checks master
    out in THIS tree, which is shared with other agent sessions -- their commits
    would land on whatever branch the tree is on. That risk is tolerable for the
    seconds a fast-forward takes; a cherry-pick sequence that can stop on a
    conflict is a different matter, so the filtered path never moves this tree.
    """
    git("push", REMOTE, DEV_BRANCH)
    print(f"  pushed {DEV_BRANCH}")

    if not excluded:
        git("checkout", MASTER_BRANCH)
        try:
            git("merge", "--ff-only", DEV_BRANCH)
            git("push", REMOTE, MASTER_BRANCH)
            print(f"  fast-forwarded and pushed {MASTER_BRANCH}")
        finally:
            git("checkout", DEV_BRANCH)
        return

    applied = already_applied([])
    shipping = [sha for sha in _range_commits()
                if sha not in excluded and sha not in applied]
    if applied:
        print(f"  skipping {len(applied)} commit(s) master already carries "
              f"(replayed by an earlier filtered release)")
    if not shipping:
        raise PublishError(
            "every commit in the range touches a dev-only plugin -- there is "
            "nothing to publish. The bumps that passed preflight are on "
            "commits that cannot ship.")

    # _range_commits() is newest-first; cherry-pick oldest-first.
    shipping = list(reversed(shipping))
    workdir = Path(tempfile.mkdtemp(prefix="publish-master-"))
    try:
        git("worktree", "add", "--detach", str(workdir), f"{REMOTE}/{MASTER_BRANCH}")
        try:
            skipped_empty = 0
            for sha in shipping:
                if _is_merge_commit(sha):
                    # A merge carries no content of its own; its parents' work
                    # is already in `shipping` (or already on master). Replaying
                    # one needs a -m parent choice that means nothing on a
                    # linearised master, so there is nothing here to pick.
                    skipped_empty += 1
                    continue
                done = subprocess.run(
                    ["git", "-C", str(workdir), "cherry-pick", sha],
                    capture_output=True, text=True)
                if done.returncode == 0:
                    continue
                blob = (done.stderr or "") + (done.stdout or "")
                if "now empty" in blob or "nothing to commit" in blob:
                    # Master already holds this content -- a cherry-picked
                    # equivalent from an earlier filtered release, or a commit
                    # whose changes another release carried. `already_applied`
                    # catches most of these up front by patch-id; a rebase or a
                    # conflict resolution can change the patch-id while leaving
                    # the RESULT identical, and only the replay sees that. An
                    # empty pick is the success case, not a conflict: there is
                    # nothing left to ship because it already shipped.
                    subprocess.run(["git", "-C", str(workdir), "cherry-pick",
                                    "--skip"], check=True, capture_output=True,
                                   text=True)
                    skipped_empty += 1
                    continue
                raise subprocess.CalledProcessError(
                    done.returncode, done.args, done.stdout, done.stderr)
            if skipped_empty:
                print(f"  skipped {skipped_empty} commit(s) that added nothing "
                      f"to {MASTER_BRANCH} (already carried, or a merge)")
            head = subprocess.run(["git", "-C", str(workdir), "rev-parse", "HEAD"],
                                  check=True, capture_output=True, text=True).stdout.strip()
            git("push", REMOTE, f"{head}:refs/heads/{MASTER_BRANCH}")
            print(f"  published {len(shipping)} commit(s) to {MASTER_BRANCH}; "
                  f"excluded {len(excluded)} dev-only commit(s)")
        finally:
            git("worktree", "remove", "--force", str(workdir), check=False)
    except subprocess.CalledProcessError as exc:
        raise PublishError(
            "the filtered release could not be replayed onto "
            f"{MASTER_BRANCH} -- a shippable commit conflicts with master "
            "without the excluded dev-only commits beneath it:\n"
            f"{(exc.stderr or exc.stdout or '').strip()}\n\n"
            "Nothing was pushed to master. Split the dependency, or ship the "
            "dev-only plugin with --allow-dev-only.") from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


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


def verify(excluded: dict[str, set[str]] | None = None) -> list[str]:
    """Post-publish verification. Returns a list of problems (empty = good).

    `excluded` is the dev-only commit set the release filtered out. When it is
    non-empty the branches are SUPPOSED to differ, so identity is the wrong
    check and the tree contents are checked instead -- master must match dev
    everywhere except those plugins' own files.
    """
    problems = []

    git("fetch", REMOTE, "--quiet")
    dev_sha = git("rev-parse", f"{REMOTE}/{DEV_BRANCH}")
    master_sha = git("rev-parse", f"{REMOTE}/{MASTER_BRANCH}")
    if not excluded:
        if dev_sha != master_sha:
            problems.append(
                f"{REMOTE}/{DEV_BRANCH} ({dev_sha[:9]}) != {REMOTE}/{MASTER_BRANCH} "
                f"({master_sha[:9]}) -- the publish did not land on the cache source")
    else:
        dev_only = {n for n, m in local_plugins().items() if not is_published(m)}
        diff = git("diff", "--name-only", f"{REMOTE}/{MASTER_BRANCH}",
                   f"{REMOTE}/{DEV_BRANCH}")
        leaked = [f for f in diff.splitlines()
                  if f.strip() and not _dev_only_owned(f.strip(), dev_only)]
        if leaked:
            problems.append(
                f"filtered release: {REMOTE}/{MASTER_BRANCH} differs from "
                f"{REMOTE}/{DEV_BRANCH} outside the excluded dev-only plugins, "
                f"so shippable work did not land: {', '.join(leaked[:8])}"
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

    marketplace = json.loads(MARKETPLACE_JSON.read_text(encoding="utf-8"))
    listed = {p["name"]: p.get("version") for p in marketplace.get("plugins", [])}
    index_text = INDEX_HTML.read_text(encoding="utf-8")
    problems.extend(check_index_scope(index_text))

    for name, manifest in local_plugins().items():
        if not is_published(manifest):
            if name in listed:
                problems.append(f"dev-only plugin {name} is listed in marketplace.json")
            continue
        version = manifest.get("version")
        if listed.get(name) != version:
            problems.append(
                f"marketplace.json has {name}={listed.get(name)}, "
                f"plugin.json has {version}")
        # A poster-hidden plugin is published but intentionally off the page;
        # asserting its presence would fail every publish while it ships.
        if is_poster_hidden(name):
            if f'"name": "{name}"' in index_text:
                problems.append(
                    f"index.html shows {name}, which opts out via poster.yaml hidden: true")
        elif f'"name": "{name}", "version": "{version}"' not in index_text:
            problems.append(f"index.html does not show {name} {version}")

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
        "--allow-dev-only", action="append", default=[], metavar="PLUGIN",
        help="ship commits touching this dev-only (published: false) plugin "
             "anyway -- an explicit operator decision for finished work "
             "master's tree needs (repeatable; does NOT add the plugin to "
             "the marketplace listing)")
    args = parser.parse_args(argv)

    try:
        print("preflight:")
        bumps, excluded = preflight(set(args.allow_dev_only))
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
        if regenerate():
            commit_derived(bumps)
        else:
            print("  already current (nothing to commit)")

        print("\npublishing:")
        push_and_merge(excluded)

    except PublishError as exc:
        # Flush first: stdout is block-buffered when piped and stderr is not, so
        # without this the refusal prints ABOVE the steps it refused at.
        sys.stdout.flush()
        print(f"\npublish refused: {exc}", file=sys.stderr)
        return 1

    print("\nverifying:")
    problems = verify(excluded)
    if problems:
        sys.stdout.flush()
        for problem in problems:
            print(f"  FAILED: {problem}", file=sys.stderr)
        return 1

    print("  origin/dev == origin/master" if not excluded
          else "  origin/master carries every shippable commit; dev-only work held back")
    print("  marketplace.json, index.html, and plugin.json agree")
    print("  dev-tree restored to normal")
    print("\npublished. Users with autoUpdate get it next session start.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
