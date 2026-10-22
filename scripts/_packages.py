#!/usr/bin/env python3
"""Package release machinery for scripts/publish.py.

publish.py moves plugin releases from dev to master. When packages/registry.toml
registers a distribution whose pyproject version has no entry in
packages/released.json, the same run also releases that version to PyPI, BEFORE
master can expose a plugin that needs it. The procedure, the states and the
user's commands: docs/reference/package-release.md. This module is the source
of truth for the mechanics; the document states the contract.

Three groups, in the order a run uses them:

  * preflight policy (plan): the release set R, the --only refusal, registry
    and dependency checks, the partial semver check, the source-drift report,
    and the workflow-on-master check. Reads git objects only -- no network.
  * observations (re-read every run): the ledger entry J at origin/dev, the
    peeled tag T on origin, the PyPI JSON for (d, v), and the release workflow's
    newest run. An observation that cannot be made raises ObservationFailed:
    the run stops with no state change and is safe to re-run.
  * transitions (release): per (d, v), in dependency order, the table in
    docs/reference/package-release.md ("Release states"). State changes are
    the dev advance, the tag push, and the ledger commit -- nothing else.

Every network-facing operation goes through `Services`, so a test substitutes
local fakes and a run with an empty release set never reaches one.

Import is standard-library only: publish.py imports this module, and the
pre-commit hook runs `publish.py --print-range-base` on a possibly
unprovisioned clone. `packaging` (root dev extra) is imported where a
registered package needs it, so an empty registry needs no third-party module.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

_REAL_ROOT = Path(__file__).resolve().parent.parent


def _load_release_build():
    """packages/_build/release_build.py, loaded from this checkout.

    Parsing (registry, names, wheel comparison) uses this copy. A BUILD never
    does: it runs the builder committed at the commit being built, so a tag is
    always rebuilt by the builder it was released with (see `real_build`).
    """
    path = _REAL_ROOT / "packages" / "_build" / "release_build.py"
    spec = importlib.util.spec_from_file_location("_katsoft_release_build", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rb = _load_release_build()

DEV_BRANCH = "dev"
REGISTRY_PATH = rb.REGISTRY_PATH
RELEASED_PATH = rb.RELEASED_PATH
WORKFLOW_PATH = ".github/workflows/release-packages.yml"
WORKFLOW_FILE = "release-packages.yml"
MARKETPLACE_PATH = ".claude-plugin/marketplace.json"
# Paths only publish.py writes on dev. P and F may differ in these alone.
PUBLISH_OWNED = (MARKETPLACE_PATH, RELEASED_PATH)
LEDGER_FIELDS = ("version", "tag", "tag_url", "commit", "filename",
                 "wheel_sha256", "verified_at")

POLL_SECONDS = 10
RUN_APPEAR_SECONDS = 120
RUN_WAIT_SECONDS = 20 * 60
PYPI_WAIT_SECONDS = 15 * 60
LEDGER_TRIES = 3

ABSENT = "ABSENT"
FILES_PRESENT = "FILES_PRESENT"
YANKED = "YANKED"
ANOMALY = "ANOMALY"


class PackageError(Exception):
    """Base for every package-release stop. The message says what to do."""


class Refusal(PackageError):
    """A state only the user can resolve (bump the version, yank, decide)."""


class ObservationFailed(PackageError):
    """An observation, build or tool failed. Nothing changed; re-run."""

    def __init__(self, message: str):
        super().__init__(f"observation failed; re-run. {message}")


class Waiting(PackageError):
    """The release workflow has not finished the upload; the message says how
    to resume. publish.py never re-dispatches a run on its own."""


class _PushRejected(Exception):
    """A push git refused (non-fast-forward or an existing tag)."""


# --- git ---------------------------------------------------------------------

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


# --- reads at a ref ------------------------------------------------------------

def read_registry(repo: Repo, ref: str):
    """The registry at `ref`. A ref without packages/registry.toml registers
    nothing -- that is every commit before the registry existed."""
    text = repo.show(ref, REGISTRY_PATH)
    if text is None:
        return rb.Registry("", ())
    try:
        return rb.parse_registry(text)
    except rb.BuildError as exc:
        raise Refusal(str(exc)) from exc


def read_released(repo: Repo, ref: str) -> dict[str, dict[str, dict]]:
    """packages/released.json at `ref`; absent (before its first projection,
    or before it existed) is the empty ledger."""
    text = repo.show(ref, RELEASED_PATH)
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise Refusal(f"{RELEASED_PATH} at {ref} is not JSON: {exc}") from exc
    if not isinstance(data, dict) or not all(
            isinstance(v, dict) and all(isinstance(e, dict) for e in v.values())
            for v in data.values()):
        raise Refusal(f"{RELEASED_PATH} at {ref} must map distribution -> "
                      "version -> entry")
    return data


def _requirements(text: str | None, where: str) -> list["Requirement"]:
    from packaging.requirements import InvalidRequirement, Requirement
    if text is None:
        return []
    import tomllib
    project = tomllib.loads(text).get("project", {})
    raw = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        raw.extend(group)
    out = []
    for item in raw:
        try:
            out.append(Requirement(item))
        except InvalidRequirement as exc:
            raise Refusal(f"{where}: invalid requirement {item!r}: {exc}") from exc
    return out


# --- plan (preflight policy) -----------------------------------------------------

@dataclass
class Plan:
    """What preflight decided about packages at P."""

    registry: object
    versions: dict[str, str]
    released: dict[str, dict[str, dict]]
    deps: dict[str, list["Requirement"]]
    release_set: list[str]
    notes: list[str] = field(default_factory=list)

    def entry(self, name: str):
        return self.registry.entry(name)


def plan(repo: Repo, ref: str, *, only: set[str] | None,
         shipping_plugins: set[str], master: str) -> Plan:
    """Every package check publish preflight makes. Reads git objects only."""
    registry = read_registry(repo, ref)
    released = read_released(repo, ref)
    names = {rb.normalize_dist(e.name): e.name for e in registry.entries}
    versions, deps = {}, {}
    if not registry.entries:
        return Plan(registry, versions, released, deps, [], [])
    from packaging.version import InvalidVersion, Version

    for entry in registry.entries:
        text = repo.show(ref, entry.pyproject)
        if text is None:
            raise Refusal(f"{entry.name} is registered but {entry.pyproject} "
                          f"does not exist at {ref[:9]}")
        try:
            versions[entry.name] = rb.project_version(text, entry.name)
            Version(versions[entry.name])
        except (rb.BuildError, InvalidVersion) as exc:
            raise Refusal(f"{entry.pyproject}: {exc}") from exc
        deps[entry.name] = [r for r in _requirements(text, entry.pyproject)
                            if rb.normalize_dist(r.name) in names]
    release_set = [e.name for e in registry.entries
                   if versions[e.name] not in released.get(e.name, {})]

    if release_set and only:
        raise Refusal(
            "--only is refused while a package release is pending ("
            + ", ".join(f"{d} {versions[d]}" for d in release_set)
            + "): a partial release could expose a plugin before the package "
              "it needs. Publish without --only.")

    order = _topological(deps, names)
    _require_satisfiable(repo, ref, deps, names, versions, released,
                         release_set, shipping_plugins)
    for dist in release_set:
        _require_major_for_removed_names(repo, ref, registry.entry(dist),
                                         versions[dist], released.get(dist, {}))
    notes = []
    for entry in registry.entries:
        if entry.name in release_set:
            continue
        recorded = released[entry.name][versions[entry.name]]
        if not same_src(repo, recorded["commit"], ref, entry.name):
            notes.append(
                f"{entry.name} {versions[entry.name]}: build inputs changed since "
                f"its release at {recorded['commit'][:9]}; not released (bump "
                f"the version in {entry.pyproject} to release them)")
    if release_set:
        on_master = repo.blob(master, WORKFLOW_PATH)
        if on_master is None or on_master != repo.blob(ref, WORKFLOW_PATH):
            raise Refusal(
                f"{WORKFLOW_PATH} on {master} is "
                + ("absent" if on_master is None else "not the version on dev")
                + ": the release workflow runs from the default branch, so a "
                  "package cannot release until a publish that releases no "
                  "package has carried the workflow to master.")
    return Plan(registry, versions, released, deps,
                [d for d in order if d in release_set], notes)


def _topological(deps: dict[str, list["Requirement"]],
                 names: dict[str, str]) -> list[str]:
    """Registered packages, dependencies first. A cycle refuses."""
    order: list[str] = []
    state: dict[str, str] = {}

    def visit(dist: str, path: list[str]) -> None:
        if state.get(dist) == "done":
            return
        if state.get(dist) == "active":
            cycle = path[path.index(dist):] + [dist]
            raise Refusal("first-party dependency cycle: " + " -> ".join(cycle))
        state[dist] = "active"
        for req in deps[dist]:
            visit(names[rb.normalize_dist(req.name)], path + [dist])
        state[dist] = "done"
        order.append(dist)

    for dist in sorted(deps):
        visit(dist, [])
    return order


def _require_satisfiable(repo: Repo, ref: str, deps, names, versions, released,
                         release_set, shipping_plugins: set[str]) -> None:
    """Every first-party requirement must admit a released or pending version."""
    owners: list[tuple[str, Requirement]] = [
        (f"packages/{d}", r) for d in release_set for r in deps[d]]
    for plugin in sorted(shipping_plugins):
        where = f"plugins/{plugin}/pyproject.toml"
        for req in _requirements(repo.show(ref, where), where):
            if rb.normalize_dist(req.name) in names:
                owners.append((where, req))
    problems = []
    for where, req in owners:
        dist = names[rb.normalize_dist(req.name)]
        candidates = list(released.get(dist, {}))
        if dist in release_set:
            candidates.append(versions[dist])
        if not any(req.specifier.contains(c) for c in candidates):
            problems.append(f"{where}: {req} (released or pending: "
                            f"{', '.join(candidates) or 'none'})")
    if problems:
        raise Refusal("unsatisfiable first-party requirement(s):\n  "
                      + "\n  ".join(problems))


def surface_names(repo: Repo, ref: str, entry) -> set[str] | None:
    """The public names `entry` lists at `ref`; None when it lists none there."""
    if entry.surface_kind is None:
        return None
    text = repo.show(ref, entry.surface_path)
    if text is None:
        return None
    if entry.surface_kind == "json":
        data = json.loads(text)
        return {e["name"] for e in data["entries"] if e.get("status") != "removed"}
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            return set(ast.literal_eval(node.value))
    raise Refusal(f"{entry.surface_path} at {ref[:9]} has no literal __all__")


def _require_major_for_removed_names(repo: Repo, ref: str, entry, version: str,
                                     released: dict[str, dict]) -> None:
    """Partial semver check: a listed public name removed needs a major bump.

    Partial on purpose and by construction: it sees only the names the
    surface lists, never a behavioural break.
    """
    if not released:
        return
    from packaging.version import Version
    base = max(released, key=Version)
    before = surface_names(repo, released[base]["commit"], entry)
    if before is None:
        return
    removed = before - (surface_names(repo, ref, entry) or set())
    if removed and Version(version).major <= Version(base).major:
        raise Refusal(
            f"{entry.name} {version} removes public name(s) listed in "
            f"{entry.surface_path} since {base} without a major bump:\n  "
            + "\n  ".join(sorted(removed)))


def same_src(repo: Repo, x: str, b: str, dist: str) -> bool:
    """same_src(X, B): equal build inputs S, and no diff under them."""
    try:
        sx = read_registry(repo, x).entry(dist).inputs()
    except (rb.BuildError, Refusal):
        return False
    sb = read_registry(repo, b).entry(dist).inputs()
    if sx != sb:
        return False
    result = repo.run("diff", "--quiet", x, b, "--", *sb)
    if result.returncode not in (0, 1):
        raise PackageError("git diff failed:\n"
                           + result.stderr.decode(errors="replace").strip())
    return result.returncode == 0


def unprojected_ledger(repo: Repo, ref: str, master: str) -> list[str]:
    """Ledger entries at `ref` that master's released.json lacks."""
    here, there = read_released(repo, ref), read_released(repo, master)
    return sorted(f"{d} {v}" for d, versions in here.items() for v in versions
                  if v not in there.get(d, {}))


# --- services (everything that leaves this machine) ------------------------------

def _real_pypi_json(url: str) -> tuple[int, object]:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return 404, None
        raise ObservationFailed(f"GET {url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ObservationFailed(f"GET {url}: {exc}") from exc
    try:
        return status, json.loads(body)
    except json.JSONDecodeError as exc:
        raise ObservationFailed(f"GET {url}: malformed JSON: {exc}") from exc


def _real_download(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ObservationFailed(f"download {url}: {exc}") from exc


def _real_gh(args: list[str], root: Path) -> str:
    result = subprocess.run(["gh", *args], cwd=root, capture_output=True, text=True)
    if result.returncode != 0:
        raise ObservationFailed(f"gh {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def real_build(repo: Repo, commit: str, dist: str, out_dir: Path) -> Path:
    """B3 with the builder committed at `commit` (packages/_build is a build
    input, so same_src already guarantees it is the builder of the tag)."""
    builder = repo.show(commit, "packages/_build/release_build.py")
    if builder is None:
        raise ObservationFailed(f"{commit[:9]} has no packages/_build/release_build.py")
    script = out_dir / "release_build.py"
    out_dir.mkdir(parents=True, exist_ok=True)
    script.write_text(builder, encoding="utf-8")
    wheels = out_dir / "wheels"
    result = subprocess.run(
        [sys.executable, "-I", str(script), "build", "--repo", str(repo.root),
         "--commit", commit, "--dist", dist, "--out", str(wheels)],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise ObservationFailed(f"build of {dist} at {commit[:9]} failed:\n"
                                + result.stderr.strip())
    return Path(result.stdout.strip().splitlines()[-1])


_SMOKE = """
import importlib, importlib.metadata, json, sys
dist, name, marker = sys.argv[1:4]
try:
    importlib.import_module(name)
    module = importlib.import_module(marker)
    out = {"version": importlib.metadata.version(dist), "mode": module.MODE,
           "commit": module.COMMIT, "error": None}
except Exception as exc:
    out = {"error": repr(exc)}
print(json.dumps(out))
"""


def real_smoke(wheel: Path, entry, pins: list[str]) -> dict:
    """Install the DOWNLOADED wheel into a fresh venv and read its identity."""
    tmp = Path(tempfile.mkdtemp(prefix="publish-smoke-"))
    try:
        venv = tmp / "venv"
        steps = [["uv", "venv", "--no-config", "--python", rb.BUILD_PYTHON, str(venv)]]
        venv_python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        steps.append(["uv", "pip", "install", "--no-config", "--python", str(venv_python),
                      str(wheel), *pins])
        for cmd in steps:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise ObservationFailed(f"{' '.join(cmd[:3])} failed:\n"
                                        + result.stderr.strip())
        result = subprocess.run(
            [str(venv_python), "-I", "-c", _SMOKE, entry.name, entry.import_name,
             entry.marker_module], capture_output=True, text=True, cwd=tmp)
        if result.returncode != 0:
            raise ObservationFailed("smoke interpreter failed:\n" + result.stderr.strip())
        return json.loads(result.stdout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@dataclass
class Services:
    """Every operation that leaves the repository. Tests pass fakes."""

    pypi_json: Callable[[str], tuple[int, object]]
    download: Callable[[str], bytes]
    gh: Callable[[list[str]], str]
    build: Callable[[Repo, str, str, Path], Path]
    smoke: Callable[[Path, object, list[str]], dict]
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.monotonic

    @classmethod
    def real(cls, root: Path) -> "Services":
        return cls(pypi_json=_real_pypi_json, download=_real_download,
                   gh=lambda args: _real_gh(args, root), build=real_build,
                   smoke=real_smoke)


# --- observations ------------------------------------------------------------------

def observe_pypi(services: Services, dist: str, version: str) -> tuple[str, dict]:
    url = f"https://pypi.org/pypi/{dist}/{version}/json"
    status, data = services.pypi_json(url)
    if status == 404:
        return ABSENT, {}
    if status != 200 or not isinstance(data, dict) \
            or not isinstance(data.get("urls"), list) \
            or not isinstance(data.get("info"), dict):
        raise ObservationFailed(f"GET {url}: unexpected response (HTTP {status})")
    files = data["urls"]
    if data["info"].get("yanked") or any(f.get("yanked") for f in files):
        return YANKED, {}
    if len(files) == 1:
        only = files[0]
        digest = (only.get("digests") or {}).get("sha256")
        if only.get("packagetype") == "bdist_wheel" and digest and only.get("url") \
                and only.get("filename") == rb.wheel_filename(dist, version):
            return FILES_PRESENT, {"url": only["url"], "sha256": digest,
                                   "filename": only["filename"]}
    return ANOMALY, {"files": [f.get("filename") for f in files]}


def observe_tag(repo: Repo, tag: str) -> str | None:
    """The PEELED commit of refs/tags/<tag> on the remote, never a tag object."""
    refs = repo.ls_remote(f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
    return refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")


def newest_run(services: Services, tag: str) -> dict | None:
    raw = services.gh(["run", "list", "--workflow", WORKFLOW_FILE, "--json",
                       "databaseId,status,conclusion,createdAt,displayTitle,url",
                       "--limit", "100"])
    try:
        runs = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ObservationFailed(f"gh run list: malformed JSON: {exc}") from exc
    mine = [r for r in runs if r.get("displayTitle") == f"release {tag}"]
    return max(mine, key=lambda r: r["createdAt"]) if mine else None


# --- transitions ------------------------------------------------------------------

def _resume(tag: str, run: dict | None) -> str:
    lines = [f"  gh workflow run {WORKFLOW_FILE} --ref master -f tag={tag}"]
    if run is not None:
        lines.insert(0, f"  gh run rerun {run['databaseId']}")
    return ("Resume it yourself (publish.py never re-dispatches), then run "
            "publish.py again:\n" + "\n".join(lines))


@dataclass
class Release:
    """One run's package releases, in dependency order."""

    repo: Repo
    plan: Plan
    base: str            # B
    services: Services
    advanced: str | None = None       # what this run last pushed to dev
    recorded: dict[str, dict] = field(default_factory=dict)

    # ---- dev ----
    def dev_at_base(self) -> str:
        """Advance dev to B once; later ledger commits descend from it."""
        if self.advanced is None:
            try:
                advance_dev(self.repo, self.base)
            except _PushRejected as exc:
                raise Refusal(f"{DEV_BRANCH} moved on {self.repo.remote}; run "
                              f"`git pull --no-rebase` and publish again.\n{exc}") from exc
            self.advanced = self.base
        return self.advanced

    def run(self) -> str:
        """Release every package in R; returns F (last ledger commit, or B)."""
        for dist in self.plan.release_set:
            self._settle(dist)
        return self.dev_at_base()

    # ---- the table ----
    def _settle(self, dist: str) -> None:
        version = self.plan.versions[dist]
        tag = rb.tag_name(dist, version)
        for _ in range(6):
            self.repo.fetch()
            j = read_released(self.repo, self.repo.remote_dev()).get(dist, {}).get(version)
            t = observe_tag(self.repo, tag)
            state, info = observe_pypi(self.services, dist, version)
            if j is not None:
                if t == j.get("commit") and state == FILES_PRESENT \
                        and info["sha256"] == j.get("wheel_sha256"):
                    self.recorded[dist] = j                             # row 1
                    return
                raise Refusal(                                          # row 2
                    f"{dist} {version} is recorded in {RELEASED_PATH} but the "
                    f"remote now shows tag={t or 'absent'}, PyPI={state}"
                    + (" (the release is YANKED on PyPI)" if state == YANKED else "")
                    + ". Decide how to repair it; publish.py will not.")
            if t is None:
                if state != ABSENT:
                    raise Refusal(                                      # row 4
                        f"{dist} {version} is on PyPI ({state}) with no tag {tag}: "
                        "it was uploaded outside this machinery. Bump the version.")
                self._unreleased(dist, version, tag)                    # row 3
                continue
            if not same_src(self.repo, t, self.base, dist):
                raise Refusal(                                          # row 5
                    f"{version} is tagged at {t} with different build inputs; "
                    f"bump the version of {dist}. Tags are never moved or deleted.")
            if state == ABSENT:
                self._wait(dist, version, tag)                          # row 6
                continue
            if state in (ANOMALY, YANKED):
                raise Refusal(                                          # row 7
                    f"{dist} {version} on PyPI is {state} "
                    f"({info.get('files', '')}); decide how to repair it.")
            self._uploaded(dist, version, tag, t, info)                  # row 8
            return
        raise PackageError(f"{dist} {version}: release state did not settle; re-run")

    def _first_party_pins(self, dist: str) -> list[str]:
        """Each first-party dependency at a RECORDED version (row 3a, B4)."""
        from packaging.version import Version
        released = read_released(self.repo, self.repo.remote_dev())
        names = {rb.normalize_dist(e.name): e.name for e in self.plan.registry.entries}
        pins = []
        for req in self.plan.deps[dist]:
            dep = names[rb.normalize_dist(req.name)]
            ok = [v for v in released.get(dep, {}) if req.specifier.contains(v)]
            if not ok:
                raise Waiting(f"{dist} waits for its dependency {dep} to be "
                              f"recorded at a version matching {req.specifier}; "
                              "nothing was tagged.")
            pins.append(f"{dep}=={max(ok, key=Version)}")
        return pins

    def _unreleased(self, dist: str, version: str, tag: str) -> None:
        self._first_party_pins(dist)                                    # (a)
        entry = self.plan.entry(dist)
        url = rb.tag_url(self.plan.registry.repository, tag)
        with tempfile.TemporaryDirectory(prefix="publish-snapshot-") as tmp:
            wheel = self.services.build(self.repo, self.base, dist, Path(tmp))  # (b)
            try:
                rb.check_wheel(wheel, entry, version, tag, self.base, url)  # (c)
            except rb.BuildError as exc:
                raise Refusal(f"{dist} {version}: snapshot wheel from "
                              f"{self.base[:9]} fails the content test: {exc}") from exc
        advanced = self.dev_at_base()                                   # (d)
        remote = self.repo.ls_remote(f"refs/heads/{DEV_BRANCH}").get(
            f"refs/heads/{DEV_BRANCH}")
        if remote != advanced:
            raise Refusal(f"{self.repo.remote} {DEV_BRANCH} is {remote}, not "
                          f"{advanced[:9]}; {tag} was not created. Pull and re-run.")
        local = self.repo.run("rev-parse", "--verify", "--quiet",
                              f"refs/tags/{tag}^{{commit}}").stdout.decode().strip()
        if local and local != self.base:
            raise Refusal(f"a local tag {tag} points at {local[:9]}, not "
                          f"{self.base[:9]}; delete it (`git tag -d {tag}`).")
        if not local:
            self.repo.git("tag", "-a", tag, self.base, "-m", f"release {tag}")
        pushed = self.repo.run("push", self.repo.remote, f"refs/tags/{tag}")  # (e)
        if pushed.returncode == 0:
            print(f"  tagged {tag} at {self.base[:9]}; the release workflow builds "
                  "and uploads it")
        else:
            print(f"  {tag} push rejected (a concurrent claim); re-reading state")

    def _wait(self, dist: str, version: str, tag: str) -> None:
        """TAGGED: poll the newest run, then PyPI. Never re-dispatch."""
        start = self.services.now()
        while True:
            run = newest_run(self.services, tag)
            elapsed = self.services.now() - start
            if run is None:
                if elapsed >= RUN_APPEAR_SECONDS:
                    raise Waiting(f"no release run for {tag} after "
                                  f"{RUN_APPEAR_SECONDS} s. " + _resume(tag, None))
            elif run.get("status") != "completed":
                if elapsed >= RUN_WAIT_SECONDS:
                    raise Waiting(f"release run {run.get('url')} for {tag} is still "
                                  f"{run.get('status')}; stopped with no state "
                                  "change. Run publish.py again to keep waiting.")
            elif run.get("conclusion") == "success":
                break
            else:
                raise Waiting(f"release run {run.get('url')} for {tag} ended "
                              f"{run.get('conclusion')}. " + _resume(tag, run))
            self.services.sleep(POLL_SECONDS)
        start = self.services.now()
        while observe_pypi(self.services, dist, version)[0] == ABSENT:
            if self.services.now() - start >= PYPI_WAIT_SECONDS:
                raise Waiting(f"the release run for {tag} succeeded but PyPI does "
                              f"not list {dist} {version} after "
                              f"{PYPI_WAIT_SECONDS // 60} min; run publish.py again.")
            self.services.sleep(POLL_SECONDS)

    def _uploaded(self, dist: str, version: str, tag: str, t: str, info: dict) -> None:
        sha = self._verify(dist, version, t, info)
        entry = {
            "version": version, "tag": tag,
            "tag_url": rb.tag_url(self.plan.registry.repository, tag),
            "commit": t, "filename": info["filename"], "wheel_sha256": sha,
            "verified_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        self.dev_at_base()
        self.advanced = ledger_commit(self.repo, dist, entry)
        self.recorded[dist] = entry
        print(f"  recorded {dist} {version} ({info['filename']}) in {RELEASED_PATH}")

    def _verify(self, dist: str, version: str, t: str, info: dict) -> str:
        """B4. Returns the downloaded wheel's sha256."""
        data = self.services.download(info["url"])
        sha = hashlib.sha256(data).hexdigest()
        if sha != info["sha256"]:
            raise ObservationFailed(f"downloaded {info['filename']} does not match "
                                    "PyPI's own sha256")
        entry = self.plan.entry(dist)
        with tempfile.TemporaryDirectory(prefix="publish-verify-") as tmp:
            reference = self.services.build(self.repo, t, dist, Path(tmp) / "ref")
            try:
                problems = rb.compare_wheels(data, reference.read_bytes())
            except rb.BuildError as exc:
                raise ObservationFailed(f"comparing wheels: {exc}") from exc
            if problems:
                raise Refusal(
                    f"PyPI artifact differs from the tag source; bump {dist} "
                    f"past {version}; yanking {version} is your call.\n  "
                    + "\n  ".join(problems[:12]))
            downloaded = Path(tmp) / info["filename"]
            downloaded.write_bytes(data)
            observed = self.services.smoke(downloaded, entry,
                                           self._first_party_pins(dist))
        expected = {"version": version, "mode": "package", "commit": t, "error": None}
        if any(observed.get(k) != v for k, v in expected.items()):
            raise Refusal(
                f"{dist} {version} matches its tag byte for byte but its smoke "
                f"test failed ({observed}); it is NOT recorded. Fix it and bump "
                "the version.")
        return sha


def ledger_commit(repo: Repo, dist: str, entry: dict) -> str:
    """B5: record `entry` on the current remote dev tip E; returns the new tip.

    Built in a private index on E and pushed fast-forward only; a dev that
    moved in between rejects the push and the commit is rebuilt on the new
    tip, at most LEDGER_TRIES times. An identical entry already present is a
    no-op (a concurrent publisher recorded the same verified release).
    """
    for _ in range(LEDGER_TRIES):
        repo.fetch()
        tip = repo.sha(repo.remote_dev())
        released = read_released(repo, tip)
        existing = released.get(dist, {}).get(entry["version"])
        if existing is not None:
            same = all(existing.get(k) == entry[k] for k in LEDGER_FIELDS
                       if k != "verified_at")
            if not same:
                raise Refusal(f"{RELEASED_PATH} on {DEV_BRANCH} already records "
                              f"{dist} {entry['version']} differently: {existing}")
            return tip
        released.setdefault(dist, {})[entry["version"]] = {k: entry[k] for k in LEDGER_FIELDS}
        text = json.dumps(released, indent=2, sort_keys=True) + "\n"
        commit = repo.commit_in_private_index(
            tip, {RELEASED_PATH: text},
            f"publish: record {entry['tag']} in {RELEASED_PATH}\n\n"
            f"Wheel {entry['filename']} sha256 {entry['wheel_sha256']}\n"
            f"verified against {entry['commit']}\n")
        try:
            advance_dev(repo, commit)
        except _PushRejected:
            continue
        return commit
    raise Refusal(f"{DEV_BRANCH} moved on every one of {LEDGER_TRIES} attempts to "
                  f"record {dist} {entry['version']}; the release is uploaded and "
                  "verified -- run publish.py again to record it.")
