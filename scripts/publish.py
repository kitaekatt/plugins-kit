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
    dev-only (published: false) plugin, merging publishes them. This refuses and
    names them; picking what ships is a judgement call, not a script's job.
  - index.html must ride INSIDE the release commit, or master briefly holds a
    page that disagrees with its own marketplace.json.

Usage:
  python scripts/publish.py            # preflight, publish, verify
  python scripts/publish.py --check    # preflight + verify only; no writes, no pushes
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = REPO_ROOT / "plugins"
MARKETPLACE_JSON = REPO_ROOT / ".claude-plugin" / "marketplace.json"
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

def preflight() -> list[str]:
    """Refuse anything unsafe. Returns the human summary of what will publish.

    Every check here refuses rather than fixes: a publish is visible to other
    machines, so guessing is worse than stopping.
    """
    if git("rev-parse", "--abbrev-ref", "HEAD") != DEV_BRANCH:
        raise PublishError(
            f"not on {DEV_BRANCH} (publish releases {DEV_BRANCH} -> "
            f"{MASTER_BRANCH}); checkout {DEV_BRANCH} first")

    if git("status", "--porcelain"):
        raise PublishError(
            "working tree is dirty. Commit your code and version bump first -- "
            "this script owns the derived artifacts and the git flow, not your "
            "changes.")

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

    _refuse_dev_only_commits()
    bumps = _require_version_bump()
    return bumps


def _refuse_dev_only_commits() -> None:
    """Refuse when the range touches a dev-only plugin.

    Merging would publish a plugin marked `published: false`. The marketplace
    regenerator filters it out of the LISTING, but its files would still land on
    master. Which commits ship is a judgement call -- cherry-pick by hand.
    """
    dev_only = {name for name, m in local_plugins().items() if not is_published(m)}
    if not dev_only:
        return

    offenders: dict[str, set[str]] = {}
    commits = git("rev-list", f"{REMOTE}/{MASTER_BRANCH}..{DEV_BRANCH}").split()
    for sha in commits:
        files = git("show", "--name-only", "--format=", sha).split("\n")
        for f in files:
            for plugin in dev_only:
                if f.startswith(f"plugins/{plugin}/"):
                    offenders.setdefault(sha, set()).add(plugin)

    if offenders:
        lines = []
        for sha, plugins in offenders.items():
            subject = git("log", "-1", "--format=%s", sha)
            lines.append(f"  {sha[:9]} {subject}  [{', '.join(sorted(plugins))}]")
        raise PublishError(
            "refusing: commits for dev-only (published: false) plugins are in "
            f"{REMOTE}/{MASTER_BRANCH}..{DEV_BRANCH}:\n"
            + "\n".join(lines)
            + "\n\nMerging would put their files on master. Branch from master "
              "and cherry-pick only the publish-ready commits. Deciding what "
              "ships is yours, not this script's.")


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


# --- derived artifacts -----------------------------------------------------

def regenerate() -> bool:
    """Regenerate marketplace.json and index.html. True if anything changed.

    index.html renders the versions and skill roster read from the installPaths,
    so dev-tree must point them at THIS working copy for the page to show what
    is about to be published. Restoring is a finally: leaving the tree flipped
    silently loads plugins from the working copy in the next session.

    --public is mandatory here: the default poster badges each plugin on/off/
    installed from THIS machine's enabledPlugins, which is meaningless-to-wrong
    on a page checked in for other people to read.

    --marketplace-json is mandatory for the same class of reason: generate.py
    filters phantom installs against the CACHED marketplace.json, which lags the
    source by one publish. A plugin introduced in this release is not in it yet,
    so it would be dropped from its own release's page (how hue-kit 0.7.0 shipped
    a page missing bootstrap-stuck-fix 0.1.0). Point it at the copy just
    regenerated above.
    """
    run([sys.executable, str(REGEN_MARKETPLACE_PY)], "marketplace.json regen")

    run([sys.executable, str(DEV_TREE_PY), "dev"], "dev-tree dev")
    try:
        run([sys.executable, str(GENERATE_PY),
             "--marketplace", MARKETPLACE_NAME,
             "--marketplace-json", f"{MARKETPLACE_NAME}={MARKETPLACE_JSON}",
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

def push_and_merge() -> None:
    git("push", REMOTE, DEV_BRANCH)
    print(f"  pushed {DEV_BRANCH}")
    git("checkout", MASTER_BRANCH)
    try:
        git("merge", "--ff-only", DEV_BRANCH)
        git("push", REMOTE, MASTER_BRANCH)
        print(f"  fast-forwarded and pushed {MASTER_BRANCH}")
    finally:
        git("checkout", DEV_BRANCH)


def verify() -> list[str]:
    """Post-publish verification. Returns a list of problems (empty = good)."""
    problems = []

    dev_sha = git("rev-parse", f"{REMOTE}/{DEV_BRANCH}")
    master_sha = git("rev-parse", f"{REMOTE}/{MASTER_BRANCH}")
    if dev_sha != master_sha:
        problems.append(
            f"{REMOTE}/{DEV_BRANCH} ({dev_sha[:9]}) != {REMOTE}/{MASTER_BRANCH} "
            f"({master_sha[:9]}) -- the publish did not land on the cache source")

    marketplace = json.loads(MARKETPLACE_JSON.read_text(encoding="utf-8"))
    listed = {p["name"]: p.get("version") for p in marketplace.get("plugins", [])}
    index_text = INDEX_HTML.read_text(encoding="utf-8")

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
        if f'"name": "{name}", "version": "{version}"' not in index_text:
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
    args = parser.parse_args(argv)

    try:
        print("preflight:")
        bumps = preflight()
        for bump in bumps:
            print(f"  publishing {bump}")

        if args.check:
            print("\n--check: preflight passed; no changes made.")
            return 0

        print("\nregenerating derived artifacts:")
        if regenerate():
            commit_derived(bumps)
        else:
            print("  already current (nothing to commit)")

        print("\npublishing:")
        push_and_merge()

    except PublishError as exc:
        # Flush first: stdout is block-buffered when piped and stderr is not, so
        # without this the refusal prints ABOVE the steps it refused at.
        sys.stdout.flush()
        print(f"\npublish refused: {exc}", file=sys.stderr)
        return 1

    print("\nverifying:")
    problems = verify()
    if problems:
        sys.stdout.flush()
        for problem in problems:
            print(f"  FAILED: {problem}", file=sys.stderr)
        return 1

    print("  origin/dev == origin/master")
    print("  marketplace.json, index.html, and plugin.json agree")
    print("  dev-tree restored to normal")
    print("\npublished. Users with autoUpdate get it next session start.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
