#!/usr/bin/env python3
"""Deterministic release builds for the distributions in packages/registry.toml.

One implementation serves both builders, so they cannot disagree:

  * the release workflow (.github/workflows/release-packages.yml) builds the
    wheel it uploads, from a checkout of the release tag (`ci` subcommand);
  * scripts/publish.py builds a REFERENCE wheel from the same tag and compares
    it, member by member, with the file PyPI serves (docs/reference/
    package-release.md, "Verify").

This file lives under packages/_build/, which is itself a build input of every
registered distribution, so a change here changes every package's build inputs
and same_src() refuses to reuse a tag built by an older builder.

Build (B3): `git archive <commit> -- <inputs>` extracted to a temporary
directory keeping repo-relative paths; `uv build --wheel` with every build
dependency pinned by constraints.txt; SOURCE_DATE_EPOCH = the commit's
committer time; the release identity (RELEASE_TAG, RELEASE_COMMIT,
RELEASE_TAG_URL) handed to the hatchling hook in hatch_build.py, which writes
it into the wheel's release marker and its Source URL.

Standard library only: it runs under any Python 3.12 before a venv exists.
`tomllib` (3.11+) is imported where TOML is parsed, because scripts/publish.py
loads this module and its `--print-range-base` must still answer the
pre-commit hook under an older system Python.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

SCHEMA = 1
REGISTRY_PATH = "packages/registry.toml"
RELEASED_PATH = "packages/released.json"
BUILD_DIR = "packages/_build"
CONSTRAINTS_PATH = "packages/_build/constraints.txt"
IDENTITY_VARS = ("RELEASE_TAG", "RELEASE_COMMIT", "RELEASE_TAG_URL")
BUILD_PYTHON = "3.12"

_SHA = re.compile(r"^[0-9a-f]{40}$")
_TAG_SPLIT = re.compile(r"^(?P<dist>.+)-v(?P<version>[0-9][^/\s]*)$")


class BuildError(Exception):
    """A build, identity or wheel-content failure. The message names it."""


# --- registry --------------------------------------------------------------

@dataclass(frozen=True)
class Entry:
    """One registered distribution."""

    name: str
    import_name: str
    marker: str
    sources: tuple[str, ...]
    surface_kind: str | None
    surface_path: str | None

    @property
    def project_dir(self) -> str:
        return f"packages/{self.name}"

    @property
    def pyproject(self) -> str:
        return f"{self.project_dir}/pyproject.toml"

    def inputs(self) -> tuple[str, ...]:
        """S(X): every path whose content can change the built wheel."""
        return (*self.sources, self.project_dir, BUILD_DIR, REGISTRY_PATH)

    @property
    def marker_module(self) -> str:
        return self.marker[: -len(".py")].replace("/", ".")


@dataclass(frozen=True)
class Registry:
    repository: str
    entries: tuple[Entry, ...]

    def entry(self, name: str) -> Entry:
        wanted = normalize_dist(name)
        for entry in self.entries:
            if normalize_dist(entry.name) == wanted:
                return entry
        raise BuildError(f"{name} is not registered in {REGISTRY_PATH}")


_TOP_KEYS = {"schema", "repository", "package"}
_ENTRY_KEYS = {"name", "import", "marker", "sources", "surface"}


def parse_registry(text: str) -> Registry:
    """Parse and validate registry.toml. Every deviation is a BuildError."""
    import tomllib
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise BuildError(f"{REGISTRY_PATH} is not valid TOML: {exc}") from exc
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise BuildError(f"{REGISTRY_PATH}: unknown keys {sorted(unknown)}")
    if data.get("schema") != SCHEMA:
        raise BuildError(f"{REGISTRY_PATH}: schema must be {SCHEMA}")
    repository = data.get("repository")
    if not isinstance(repository, str) or not repository.startswith("https://"):
        raise BuildError(f"{REGISTRY_PATH}: repository must be an https:// URL")
    entries = []
    seen = set()
    for raw in data.get("package", []):
        unknown = set(raw) - _ENTRY_KEYS
        if unknown:
            raise BuildError(f"{REGISTRY_PATH}: unknown package keys {sorted(unknown)}")
        for key in ("name", "import", "marker"):
            if not isinstance(raw.get(key), str) or not raw[key]:
                raise BuildError(f"{REGISTRY_PATH}: package entry needs a string {key!r}")
        name = raw["name"]
        if normalize_dist(name) in seen:
            raise BuildError(f"{REGISTRY_PATH}: {name} is registered twice")
        seen.add(normalize_dist(name))
        if not raw["marker"].endswith(".py"):
            raise BuildError(f"{REGISTRY_PATH}: {name}: marker must name a .py file")
        sources = raw.get("sources", [])
        if not isinstance(sources, list) or not all(isinstance(s, str) and s for s in sources):
            raise BuildError(f"{REGISTRY_PATH}: {name}: sources must be a list of paths")
        surface = raw.get("surface")
        kind = path = None
        if surface is not None:
            if not isinstance(surface, dict) or len(surface) != 1 or \
                    next(iter(surface)) not in ("json", "all"):
                raise BuildError(f"{REGISTRY_PATH}: {name}: surface must be "
                                 "{ json = <path> } or { all = <module file> }")
            kind, path = next(iter(surface.items()))
        entries.append(Entry(name, raw["import"], raw["marker"], tuple(sources), kind, path))
    return Registry(repository.rstrip("/"), tuple(entries))


# --- names -----------------------------------------------------------------

def normalize_dist(name: str) -> str:
    """PEP 503 normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def tag_name(dist: str, version: str) -> str:
    return f"{dist}-v{version}"


def split_tag(tag: str) -> tuple[str, str]:
    match = _TAG_SPLIT.match(tag)
    if not match:
        raise BuildError(f"{tag!r} is not a release tag (<distribution>-v<version>)")
    return match["dist"], match["version"]


def tag_url(repository: str, tag: str) -> str:
    return f"{repository}/tree/{tag}"


def wheel_filename(dist: str, version: str) -> str:
    return f"{re.sub(r'[-_.]+', '_', dist).lower()}-{version}-py3-none-any.whl"


def project_version(pyproject_text: str, dist: str) -> str:
    """The static [project] version of packages/<dist>/pyproject.toml."""
    import tomllib
    try:
        project = tomllib.loads(pyproject_text).get("project", {})
    except tomllib.TOMLDecodeError as exc:
        raise BuildError(f"packages/{dist}/pyproject.toml is not valid TOML: {exc}") from exc
    if normalize_dist(str(project.get("name", ""))) != normalize_dist(dist):
        raise BuildError(f"packages/{dist}/pyproject.toml names project "
                         f"{project.get('name')!r}, not {dist!r}")
    version = project.get("version")
    if not isinstance(version, str) or not version:
        raise BuildError(f"packages/{dist}/pyproject.toml must state a static version")
    return version


# --- identity --------------------------------------------------------------

def identity_from_env(environ) -> dict[str, str]:
    """The three release-identity inputs; refuses when any is missing."""
    missing = [name for name in IDENTITY_VARS if not environ.get(name)]
    if missing:
        raise BuildError("release identity missing: " + ", ".join(missing)
                         + " (a release wheel is built only by release_build.py)")
    identity = {name: environ[name] for name in IDENTITY_VARS}
    if not _SHA.match(identity["RELEASE_COMMIT"]):
        raise BuildError("RELEASE_COMMIT must be a full 40-hex commit id")
    return identity


def check_identity(identity: dict[str, str], dist: str, version: str,
                   repository: str) -> None:
    tag_dist, tag_version = split_tag(identity["RELEASE_TAG"])
    if normalize_dist(tag_dist) != normalize_dist(dist) or tag_version != version:
        raise BuildError(f"RELEASE_TAG {identity['RELEASE_TAG']} does not name "
                         f"{dist} {version}")
    expected = tag_url(repository, identity["RELEASE_TAG"])
    if identity["RELEASE_TAG_URL"] != expected:
        raise BuildError(f"RELEASE_TAG_URL {identity['RELEASE_TAG_URL']} != {expected}")


def render_marker(dist: str, version: str, tag: str, commit: str, url: str) -> str:
    """The release marker module the hook writes into the wheel."""
    return (
        '"""Release identity written by packages/_build/hatch_build.py. Do not edit."""\n'
        'MODE = "package"\n'
        f"DIST = {dist!r}\n"
        f"VERSION = {version!r}\n"
        f"TAG = {tag!r}\n"
        f"COMMIT = {commit!r}\n"
        f"TAG_URL = {url!r}\n"
    )


# --- wheel content ---------------------------------------------------------

def _metadata(zf: zipfile.ZipFile, dist: str, version: str) -> list[str]:
    stem = f"{re.sub(r'[-_.]+', '_', dist).lower()}-{version}.dist-info"
    try:
        return zf.read(f"{stem}/METADATA").decode("utf-8").splitlines()
    except KeyError as exc:
        raise BuildError(f"wheel has no {stem}/METADATA") from exc


def check_wheel(wheel: Path, entry: Entry, version: str, tag: str, commit: str,
                url: str) -> None:
    """The wheel-content test: name, version, Source URL and release marker."""
    if wheel.name != wheel_filename(entry.name, version):
        raise BuildError(f"wheel {wheel.name} != {wheel_filename(entry.name, version)}")
    with zipfile.ZipFile(wheel) as zf:
        metadata = _metadata(zf, entry.name, version)
        fields = {}
        for line in metadata:
            if not line:
                break
            key, _, value = line.partition(": ")
            fields.setdefault(key, []).append(value)
        if [normalize_dist(n) for n in fields.get("Name", [])] != [normalize_dist(entry.name)]:
            raise BuildError(f"wheel METADATA Name is {fields.get('Name')}")
        if fields.get("Version") != [version]:
            raise BuildError(f"wheel METADATA Version is {fields.get('Version')}")
        if f"Source, {url}" not in fields.get("Project-URL", []):
            raise BuildError(f"wheel METADATA lacks Project-URL Source, {url}")
        try:
            marker = zf.read(entry.marker).decode("utf-8")
        except KeyError as exc:
            raise BuildError(f"wheel has no release marker {entry.marker}") from exc
        expected = render_marker(entry.name, version, tag, commit, url)
        if marker != expected:
            raise BuildError(f"wheel release marker {entry.marker} does not carry "
                             f"{tag} at {commit}")


def member_digests(wheel_bytes: bytes) -> dict[str, str]:
    """sha256 of every member's decompressed bytes, by archive path."""
    with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as zf:
        names = zf.namelist()
        if len(names) != len(set(names)):
            raise BuildError("wheel has duplicate archive members")
        return {name: hashlib.sha256(zf.read(name)).hexdigest() for name in names}


def parse_record(text: str) -> dict[str, tuple[str, str]]:
    rows = {}
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        if len(row) != 3:
            raise BuildError(f"malformed RECORD row {row!r}")
        rows[row[0]] = (row[1], row[2])
    return rows


def compare_wheels(published: bytes, reference: bytes) -> list[str]:
    """Differences between two wheels; empty when they match.

    Member sets must be equal, every member except *.dist-info/RECORD must have
    equal decompressed content, and RECORD must be equal once parsed (row order
    and line endings are not content).
    """
    left, right = member_digests(published), member_digests(reference)
    problems = []
    if set(left) != set(right):
        problems.append("member sets differ: only published "
                        f"{sorted(set(left) - set(right))}, only reference "
                        f"{sorted(set(right) - set(left))}")
    for name in sorted(set(left) & set(right)):
        if name.endswith(".dist-info/RECORD"):
            with zipfile.ZipFile(io.BytesIO(published)) as a, \
                    zipfile.ZipFile(io.BytesIO(reference)) as b:
                if parse_record(a.read(name).decode("utf-8")) != \
                        parse_record(b.read(name).decode("utf-8")):
                    problems.append(f"{name} differs")
        elif left[name] != right[name]:
            problems.append(f"{name} differs")
    return problems


# --- build -----------------------------------------------------------------

def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True)
    if result.returncode != 0:
        raise BuildError(f"git {' '.join(args)} failed:\n"
                         + result.stderr.decode(errors="replace").strip())
    return result.stdout


def build(repo: Path, commit: str, dist: str, out_dir: Path) -> Path:
    """Build `dist` from `commit` (B3) into out_dir and run the content test."""
    commit = _git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
    registry = parse_registry(_git(repo, "show", f"{commit}:{REGISTRY_PATH}").decode())
    entry = registry.entry(dist)
    version = project_version(
        _git(repo, "show", f"{commit}:{entry.pyproject}").decode(), entry.name)
    tag = tag_name(entry.name, version)
    url = tag_url(registry.repository, tag)
    epoch = _git(repo, "log", "-1", "--format=%ct", commit).decode().strip()
    archive = _git(repo, "-c", "core.autocrlf=false", "-c", "core.eol=lf",
                   "archive", "--format=tar", commit, "--", *entry.inputs())
    out_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="release-build-"))
    try:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(scratch, filter="data")
        env = dict(os.environ)
        env.update({"SOURCE_DATE_EPOCH": epoch, "RELEASE_TAG": tag,
                    "RELEASE_COMMIT": commit, "RELEASE_TAG_URL": url})
        staging = scratch / "_out"
        result = subprocess.run(
            ["uv", "build", "--wheel", "--no-config", "--python", BUILD_PYTHON,
             "--build-constraint", str(scratch / CONSTRAINTS_PATH),
             "--out-dir", str(staging), str(scratch / entry.project_dir)],
            capture_output=True, text=True, env=env)
        if result.returncode != 0:
            raise BuildError(f"uv build failed for {entry.name} at {commit}:\n"
                             f"{result.stdout.strip()}\n{result.stderr.strip()}")
        built = sorted(staging.glob("*.whl"))
        if [w.name for w in built] != [wheel_filename(entry.name, version)]:
            raise BuildError(f"uv build produced {[w.name for w in built]}")
        target = out_dir / built[0].name
        shutil.copyfile(built[0], target)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    check_wheel(target, entry, version, tag, commit, url)
    return target


def wheel_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def record_hash(data: bytes) -> str:
    """The RECORD form of a digest, for tests that assemble wheels."""
    return "sha256=" + base64.urlsafe_b64encode(
        hashlib.sha256(data).digest()).rstrip(b"=").decode()


def ci(repo: Path, tag: str, out_dir: Path) -> Path:
    """The workflow's build: assert the checkout IS the tag, then build it."""
    dist, version = split_tag(tag)
    head = _git(repo, "rev-parse", "HEAD").decode().strip()
    peeled = _git(repo, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}").decode().strip()
    if head != peeled:
        raise BuildError(f"HEAD {head} is not the peeled tag {tag} ({peeled})")
    registry = parse_registry(_git(repo, "show", f"{head}:{REGISTRY_PATH}").decode())
    entry = registry.entry(dist)
    stated = project_version(_git(repo, "show", f"{head}:{entry.pyproject}").decode(),
                             entry.name)
    if stated != version:
        raise BuildError(f"{entry.pyproject} states {stated}, tag {tag} says {version}")
    return build(repo, head, entry.name, out_dir)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="build one distribution from a commit")
    b.add_argument("--repo", type=Path, required=True)
    b.add_argument("--commit", required=True)
    b.add_argument("--dist", required=True)
    b.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("ci", help="release-workflow build of a tag checkout")
    c.add_argument("--repo", type=Path, required=True)
    c.add_argument("--tag", required=True)
    c.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            wheel = build(args.repo.resolve(), args.commit, args.dist, args.out.resolve())
        else:
            wheel = ci(args.repo.resolve(), args.tag, args.out.resolve())
    except BuildError as exc:
        print(f"release build refused: {exc}", file=sys.stderr)
        return 1
    print(wheel)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
