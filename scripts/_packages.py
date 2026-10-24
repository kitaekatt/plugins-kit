"""Consumer target validation and private-index Git helpers for publish.py.

Library builds, tags, uploads and the authoritative release ledger belong to
https://github.com/kitaekatt/katsoft-libs. This module never releases packages.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEV_BRANCH = "dev"
TARGETS_PATH = "data/package-targets.json"
PUBLISH_OWNED = (".claude-plugin/marketplace.json",)
_REAL_ROOT = Path(__file__).resolve().parent.parent
# The consumer parser is this checkout's bootstrap source, never a provisioned copy.
sys.path.insert(0, str(_REAL_ROOT / "plugins" / "bootstrap"))

class PackageError(Exception):
    """Invalid package consumer configuration."""

class ObservationFailed(PackageError):
    """A Git observation failed."""

class _PushRejected(Exception):
    pass

@dataclass
class Repo:
    """Git access for one repository and its remote."""

    root: Path
    remote: str = "origin"

    def run(self, *args: str, env: dict | None = None,
            stdin: bytes | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self.root, env=env,
                              input=stdin, capture_output=True)

    def git(self, *args: str, env: dict | None = None,
            stdin: bytes | None = None) -> str:
        result = self.run(*args, env=env, stdin=stdin)
        if result.returncode != 0:
            raise PackageError(f"git {' '.join(args)} failed:\n"
                               + result.stderr.decode(errors="replace").strip())
        return result.stdout.decode("utf-8").strip()

    def show(self, ref: str, path: str) -> str | None:
        """`path` at `ref`, or None when the path is absent there.

        `ls-tree` answers presence and fails loudly on an unknown ref, so a
        bad ref never reads as an absent file.
        """
        if not self.git("ls-tree", ref, "--", path):
            return None
        result = self.run("show", f"{ref}:{path}")
        if result.returncode != 0:
            raise PackageError(f"git show {ref}:{path} failed:\n"
                               + result.stderr.decode(errors="replace").strip())
        return result.stdout.decode("utf-8")

    def blob(self, ref: str, path: str) -> str | None:
        line = self.git("ls-tree", ref, "--", path)
        return line.split()[2] if line else None

    def sha(self, ref: str) -> str:
        return self.git("rev-parse", "--verify", f"{ref}^{{commit}}")

    def fetch(self) -> None:
        self.git("fetch", self.remote, "--quiet")

    def remote_dev(self) -> str:
        return f"{self.remote}/{DEV_BRANCH}"

    def ls_remote(self, *refs: str) -> dict[str, str]:
        result = self.run("ls-remote", self.remote, *refs)
        if result.returncode != 0:
            raise ObservationFailed(
                f"git ls-remote {self.remote} failed: "
                + result.stderr.decode(errors="replace").strip())
        out = {}
        for line in result.stdout.decode().splitlines():
            sha, _, name = line.partition("\t")
            out[name.strip()] = sha.strip()
        return out

    def commit_in_private_index(self, parent: str, files: dict[str, str],
                                message: str) -> str:
        """A commit of `parent`'s tree with `files` replaced, built in a
        temporary index: read-tree, hash-object, update-index, write-tree,
        commit-tree. Returns `parent` itself when nothing changes. The real
        index, the working tree and every branch are untouched."""
        tmp = Path(tempfile.mkdtemp(prefix="publish-private-index-"))
        try:
            env = dict(os.environ)
            env["GIT_INDEX_FILE"] = str((tmp / "index").resolve())
            self.git("read-tree", parent, env=env)
            for path, text in sorted(files.items()):
                blob = self.git("hash-object", "-w", "--stdin",
                                stdin=text.encode("utf-8"))
                self.git("update-index", "--add", "--cacheinfo",
                         f"100644,{blob},{path}", env=env)
            tree = self.git("write-tree", env=env)
            if tree == self.git("rev-parse", f"{parent}^{{tree}}"):
                return parent
            msg = tmp / "message.txt"
            msg.write_text(message, encoding="utf-8")
            return self.git("commit-tree", tree, "-p", parent, "-F", str(msg))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def changed_paths(self, a: str, b: str) -> list[str]:
        return [p for p in self.git("diff", "--name-only", a, b, "--").splitlines() if p]


def advance_dev(repo: Repo, new: str) -> None:
    """Move dev to `new`: the ONE way publish.py advances dev.

    `git push <remote> <new>:refs/heads/dev` is fast-forward only, so a dev
    that moved underneath raises _PushRejected instead of being overwritten.
    The local branch follows only when `new` differs from it in publish-owned
    paths alone; then `git update-ref` (with the expected old value) moves it
    and `git restore` brings exactly those paths' index and working copies
    along. Otherwise another session's commits sit on local dev, the local ref
    is left alone, and the operator is told to pull.
    """
    push = repo.run("push", repo.remote, f"{new}:refs/heads/{DEV_BRANCH}")
    if push.returncode != 0:
        raise _PushRejected(push.stderr.decode(errors="replace").strip())
    local = repo.sha(f"refs/heads/{DEV_BRANCH}")
    if local == new:
        return
    ancestor = repo.run("merge-base", "--is-ancestor", local, new).returncode == 0
    changed = repo.changed_paths(local, new)
    if ancestor and set(changed) <= set(PUBLISH_OWNED):
        repo.git("update-ref", f"refs/heads/{DEV_BRANCH}", new, local)
        if changed:
            repo.git("restore", f"--source={new}", "--staged", "--worktree",
                     "--", *changed)
        return
    print(f"  local {DEV_BRANCH} has moved since this run started; it was left "
          f"alone. Run `git pull --no-rebase` to pick up {new[:9]}.")



def read_targets(repo: Repo, ref: str) -> dict[str, dict[str, dict]]:
    """Read the generated, provenance-bearing consumer artifact at one commit."""
    text = repo.show(ref, TARGETS_PATH)
    if text is None:
        return {}
    from bootstrap_lib.venv_check import validate_package_target_document
    try:
        return validate_package_target_document(json.loads(text))
    except (ValueError, TypeError) as exc:
        raise PackageError(f"{TARGETS_PATH}: {exc}") from exc


def _requirements(text: str | None) -> list[str]:
    if text is None:
        return []
    project = tomllib.loads(text).get("project", {})
    rows = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        rows.extend(group)
    return rows


@dataclass
class Plan:
    release_set: list[str] = field(default_factory=list)
    versions: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def plan(repo: Repo, ref: str, *, only: set[str] | None,
         shipping_plugins: set[str], master: str) -> Plan:
    """Require an already released target for every shipping first-party dependency."""
    from bootstrap_lib.venv_check import normalize_dist, parse_restricted_requirement, _release_tuple
    targets = read_targets(repo, ref)
    registered = {normalize_dist(name): versions for name, versions in targets.items()}
    for plugin in sorted(shipping_plugins):
        path = f"plugins/{plugin}/pyproject.toml"
        wanted: dict[str, list[tuple]] = {}
        for text in _requirements(repo.show(ref, path)):
            import re
            named = re.match(r"^\s*([A-Za-z0-9._-]+)", text)
            name = normalize_dist(named.group(1)) if named else ""
            if not name.startswith("katsoft-"):
                continue
            parsed = parse_restricted_requirement(text)
            if parsed is None:
                raise PackageError(f"{path}: {text!r} must use name[extras]>=A.B[.C],<N")
            if name not in registered:
                raise PackageError(f"{path}: {name} has no imported released target")
            wanted.setdefault(name, []).append(parsed)
        for name, requirements in wanted.items():
            admitted = [version for version in registered[name]
                        if all(lo <= _release_tuple(version) < (hi, 0, 0)
                               for _dist, lo, hi in requirements)]
            if not admitted:
                raise PackageError(f"{path}: no imported released target satisfies {name}; "
                                   "release the library and import its targets first")
    return Plan()


def unprojected_ledger(repo: Repo, ref: str, master: str) -> list[str]:
    """Changed consumer targets, including removals, justify a marketplace projection."""
    here, there = read_targets(repo, ref), read_targets(repo, master)
    if here == there:
        return []
    return [f"{name} {', '.join(sorted(here.get(name, {})))}" for name in sorted(set(here) | set(there))
            if here.get(name) != there.get(name)]
