"""Package release states (scripts/_packages.py), one row of the table at a time.

The table lives in docs/reference/package-release.md ("Release states"). Every
test drives REAL git -- a work tree on `dev` and a bare origin in a temporary
directory -- because tags, fast-forward pushes and ledger commits are the
state. Everything that would leave the machine (PyPI, the GitHub CLI, wheel
builds, the smoke install) goes through `_packages.Services`, which these
tests replace with `World`, an in-memory PyPI plus a scripted release
workflow. No test makes a network call.

This module also carries the shared fixture for test_publish_packages.py,
which loads it by path.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))
import _packages  # noqa: E402

rb = _packages.rb

DIST = "alpha-lib"
REPOSITORY = "https://github.com/example/repo"


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout.strip()


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def registry_text(*entries: str) -> str:
    return f'schema = 1\nrepository = "{REPOSITORY}"\n' + "".join(entries)


def entry_text(name: str, src: str, surface: str | None = None) -> str:
    text = (f'\n[[package]]\nname = "{name}"\nimport = "{src}"\n'
            f'marker = "{src}/_release.py"\nsources = ["libs/{src}"]\n')
    if surface:
        text += f'surface = {{ all = "libs/{src}/__init__.py" }}\n'
    return text


def pyproject(name: str, version: str, deps: tuple[str, ...] = ()) -> str:
    listed = ", ".join(f'"{d}"' for d in deps)
    return (f'[project]\nname = "{name}"\nversion = "{version}"\n'
            f'dependencies = [{listed}]\n')


def add_package(root: Path, name: str, src: str, version: str = "1.0.0",
                deps: tuple[str, ...] = ()) -> None:
    write(root, f"libs/{src}/__init__.py", '__all__ = ["one", "two"]\none = two = 1\n')
    write(root, f"packages/{name}/pyproject.toml", pyproject(name, version, deps))


def make_repo(tmp_path: Path, packages: tuple[tuple[str, str], ...] = ((DIST, "alpha"),),
              *, workflow_on_master: bool = True) -> Path:
    """A miniature plugins-kit with registered packages, on dev, with origin.

    `pub-kit` is a published plugin. master starts at the same commit as dev,
    so the release workflow is already on master unless told otherwise.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "master")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    write(root, "plugins/pub-kit/.claude-plugin/plugin.json",
          json.dumps({"name": "pub-kit", "version": "1.0.0"}, indent=2) + "\n")
    write(root, ".claude-plugin/marketplace.json",
          json.dumps({"plugins": [{"name": "pub-kit", "version": "1.0.0"}]}, indent=2) + "\n")
    write(root, "dev/notes.md", "shared dev scratch\n")
    write(root, "packages/_build/constraints.txt", "hatchling==1.32.0\n")
    write(root, "packages/_build/hatch_build.py", "# hook\n")
    write(root, "packages/released.json", "{}\n")
    write(root, "packages/registry.toml",
          registry_text(*(entry_text(n, s, surface="yes") for n, s in packages)))
    for name, src in packages:
        add_package(root, name, src)
    if workflow_on_master:
        write(root, _packages.WORKFLOW_PATH, "name: release-packages\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "initial")
    git(root, "remote", "add", "origin", str(origin))
    git(root, "push", "-q", "origin", "master")
    git(root, "checkout", "-qb", "dev")
    if not workflow_on_master:
        write(root, _packages.WORKFLOW_PATH, "name: release-packages\n")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "workflow")
    git(root, "push", "-q", "origin", "dev")
    git(root, "fetch", "-q", "origin")
    return root


def commit_all(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


def other_clone(root: Path, tmp_path: Path, name: str = "other") -> Path:
    """Another machine's clone of origin, on dev."""
    clone = tmp_path / name
    subprocess.run(["git", "clone", "-q", "-b", "dev", str(tmp_path / "origin.git"),
                    str(clone)], check=True)
    git(clone, "config", "user.email", "other@example.com")
    git(clone, "config", "user.name", "Other")
    return clone


# --- a wheel the way the real builder lays it out ---------------------------------

def fake_wheel_bytes(root: Path, commit: str, dist: str, *, tamper: bool = False) -> bytes:
    """A wheel for `dist` at `commit`: sources, METADATA, release marker, RECORD.

    Content is a pure function of the commit (sorted members, fixed
    timestamps), which is the property B3 establishes for real builds.
    """
    registry = rb.parse_registry(git(root, "show", f"{commit}:{rb.REGISTRY_PATH}"))
    entry = registry.entry(dist)
    version = rb.project_version(git(root, "show", f"{commit}:{entry.pyproject}"), dist)
    tag = rb.tag_name(dist, version)
    url = rb.tag_url(registry.repository, tag)
    stem = f"{dist.replace('-', '_')}-{version}.dist-info"
    members: dict[str, bytes] = {}
    for src in entry.sources:
        for path in git(root, "ls-tree", "-r", "--name-only", commit, "--", src).splitlines():
            rel = path[len(src.rsplit("/", 1)[0]) + 1:]
            members[rel] = subprocess.run(["git", "show", f"{commit}:{path}"], cwd=root,
                                          capture_output=True, check=True).stdout
    if tamper:
        first = sorted(members)[0]
        members[first] += b"# tampered\n"
    members[entry.marker] = rb.render_marker(dist, version, tag, commit, url).encode()
    members[f"{stem}/METADATA"] = (
        f"Metadata-Version: 2.4\nName: {dist}\nVersion: {version}\n"
        f"Project-URL: Source, {url}\n\n").encode()
    members[f"{stem}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: fake\nTag: py3-none-any\n"
    record = "".join(f"{n},{rb.record_hash(d)},{len(d)}\n" for n, d in sorted(members.items()))
    members[f"{stem}/RECORD"] = (record + f"{stem}/RECORD,,\n").encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            zf.writestr(info, members[name])
    return buf.getvalue()


# --- the outside world -------------------------------------------------------------

class World:
    """In-memory PyPI, a scripted release workflow, a fake clock.

    `ci` decides what the workflow does once a tag appears on origin:
    "succeed" uploads the tag's wheel after one poll, "fail" ends the run
    failed, "never" never starts one, "running" stays in progress.
    """

    def __init__(self, root: Path, ci: str = "succeed"):
        self.root = root
        self.origin = root.parent / "origin.git"
        self.ci = ci
        self.pypi: dict[tuple[str, str], dict] = {}
        self.runs: list[dict] = []
        self.calls: list[tuple] = []
        self.clock = 0.0
        self.smoked: list[bytes] = []
        self.pypi_error = False
        self.build_error = False
        self.tamper_upload = False
        self.smoke_result: dict | None = None
        self.run_id = 100

    # Services surface
    def services(self) -> _packages.Services:
        return _packages.Services(pypi_json=self.pypi_json, download=self.download,
                                  gh=self.gh, build=self.build, smoke=self.smoke,
                                  sleep=self.sleep, now=lambda: self.clock)

    def upload(self, dist: str, version: str, data: bytes, *, yanked: bool = False,
               extra: bool = False) -> None:
        filename = rb.wheel_filename(dist, version)
        files = [{"filename": filename, "packagetype": "bdist_wheel",
                  "digests": {"sha256": hashlib.sha256(data).hexdigest()},
                  "url": f"https://files.example/{filename}", "yanked": yanked}]
        if extra:
            files.append({"filename": f"{dist}-{version}.tar.gz", "packagetype": "sdist",
                          "digests": {"sha256": "0" * 64}, "url": "https://x", "yanked": False})
        self.pypi[(dist, version)] = {"files": files, "data": data}

    def pypi_json(self, url: str):
        self.calls.append(("pypi", url))
        if self.pypi_error:
            raise _packages.ObservationFailed(f"GET {url}: timed out")
        _, _, rest = url.partition("/pypi/")
        dist, version, _ = rest.split("/")
        release = self.pypi.get((dist, version))
        if release is None:
            return 404, None
        return 200, {"info": {"yanked": False}, "urls": release["files"]}

    def download(self, url: str) -> bytes:
        self.calls.append(("download", url))
        for release in self.pypi.values():
            if release["files"][0]["url"] == url:
                return release["data"]
        raise AssertionError(f"no such file {url}")

    def gh(self, args: list[str]) -> str:
        self.calls.append(("gh", tuple(args)))
        assert args[:2] == ["run", "list"], f"unexpected gh call {args}"
        return json.dumps(self.runs)

    def build(self, repo, commit: str, dist: str, out_dir: Path) -> Path:
        self.calls.append(("build", commit, dist))
        if self.build_error:
            raise _packages.ObservationFailed("uv build failed: resolver error")
        version = rb.project_version(
            git(self.root, "show", f"{commit}:packages/{dist}/pyproject.toml"), dist)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / rb.wheel_filename(dist, version)
        path.write_bytes(fake_wheel_bytes(self.root, commit, dist))
        return path

    def smoke(self, wheel: Path, entry, pins: list[str]) -> dict:
        data = wheel.read_bytes()
        self.calls.append(("smoke", wheel.name, tuple(pins)))
        self.smoked.append(data)
        if self.smoke_result is not None:
            return self.smoke_result
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            namespace: dict = {}
            exec(zf.read(entry.marker).decode(), namespace)
        return {"version": namespace["VERSION"], "mode": namespace["MODE"],
                "commit": namespace["COMMIT"], "error": None}

    def sleep(self, seconds: float) -> None:
        self.clock += seconds
        self._ci_step()

    # the scripted workflow
    def _origin_tags(self) -> dict[str, str]:
        out = git(self.origin, "for-each-ref", "--format=%(refname:short) %(*objectname)",
                  "refs/tags")
        return dict(line.split() for line in out.splitlines() if line.strip())

    def _ci_step(self) -> None:
        if self.ci == "never":
            return
        for tag, commit in self._origin_tags().items():
            title = f"release {tag}"
            mine = [r for r in self.runs if r["displayTitle"] == title]
            if not mine:
                self.run_id += 1
                self.runs.append({"databaseId": self.run_id, "status": "in_progress",
                                  "conclusion": "", "displayTitle": title,
                                  "createdAt": f"2026-01-01T00:{self.run_id - 100:02d}:00Z",
                                  "url": f"https://github.com/example/repo/actions/runs/{self.run_id}"})
                continue
            run = max(mine, key=lambda r: r["createdAt"])
            if run["status"] == "completed" or self.ci == "running":
                continue
            run["status"] = "completed"
            run["conclusion"] = "success" if self.ci == "succeed" else "failure"
            if self.ci == "succeed":
                dist, version = rb.split_tag(tag)
                self.upload(dist, version, fake_wheel_bytes(
                    self.root, commit, dist, tamper=self.tamper_upload))


# --- helpers over _packages ------------------------------------------------------

def release_for(root: Path, world: World, repo: _packages.Repo | None = None):
    repo = repo or _packages.Repo(root)
    p = git(root, "rev-parse", "dev")
    plan = _packages.plan(repo, p, only=None, shipping_plugins=set(),
                          master="origin/master")
    return _packages.Release(repo, plan, p, world.services())


def remote_tag(root: Path, tag: str) -> str | None:
    out = git(root, "ls-remote", "origin", f"refs/tags/{tag}^{{}}")
    return out.split()[0] if out else None


def remote_dev(root: Path) -> str:
    return git(root, "ls-remote", "origin", "refs/heads/dev").split()[0]


def push_tag(root: Path, tag: str, commit: str) -> str:
    git(root, "tag", "-a", tag, commit, "-m", f"release {tag}")
    git(root, "push", "-q", "origin", f"refs/tags/{tag}")
    return git(root, "rev-parse", f"refs/tags/{tag}")


def record_on_origin(root: Path, tmp_path: Path, dist: str, version: str,
                     entry: dict) -> str:
    """Another machine records (dist, version) on origin/dev."""
    clone = other_clone(root, tmp_path, "recorder")
    path = clone / rb.RELEASED_PATH
    data = json.loads(path.read_text())
    data.setdefault(dist, {})[version] = entry
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    sha = commit_all(clone, "record")
    git(clone, "push", "-q", "origin", "dev")
    git(root, "fetch", "-q", "origin")
    return sha


TAG = rb.tag_name(DIST, "1.0.0")


@pytest.fixture
def root(tmp_path):
    return make_repo(tmp_path)


# --- B2, one test per row -------------------------------------------------------------

class TestRows:
    def _recorded_world(self, root, tmp_path):
        """Tag, upload and ledger entry all agree: the RECORDED state."""
        world = World(root)
        head = git(root, "rev-parse", "dev")
        push_tag(root, TAG, head)
        data = fake_wheel_bytes(root, head, DIST)
        world.upload(DIST, "1.0.0", data)
        entry = {"version": "1.0.0", "tag": TAG, "tag_url": rb.tag_url(REPOSITORY, TAG),
                 "commit": head, "filename": rb.wheel_filename(DIST, "1.0.0"),
                 "wheel_sha256": hashlib.sha256(data).hexdigest(),
                 "verified_at": "2026-01-01T00:00:00Z"}
        return world, head, entry

    def test_row1_recorded_does_nothing(self, root, tmp_path):
        world, head, entry = self._recorded_world(root, tmp_path)
        release = release_for(root, world)
        record_on_origin(root, tmp_path, DIST, "1.0.0", entry)

        release._settle(DIST)

        assert release.recorded[DIST]["commit"] == head
        assert not [c for c in world.calls if c[0] in ("build", "gh", "download", "smoke")]
        assert release.advanced is None

    def test_row2_recorded_but_disagreeing_refuses_and_names_yanked(self, root, tmp_path):
        world, head, entry = self._recorded_world(root, tmp_path)
        release = release_for(root, world)
        record_on_origin(root, tmp_path, DIST, "1.0.0", entry)
        world.upload(DIST, "1.0.0", world.pypi[(DIST, "1.0.0")]["data"], yanked=True)

        with pytest.raises(_packages.Refusal, match="recorded in .*YANKED"):
            release._settle(DIST)

    def test_row2_recorded_with_another_wheel_refuses(self, root, tmp_path):
        world, head, entry = self._recorded_world(root, tmp_path)
        release = release_for(root, world)
        entry["wheel_sha256"] = "f" * 64
        record_on_origin(root, tmp_path, DIST, "1.0.0", entry)

        with pytest.raises(_packages.Refusal, match="recorded in"):
            release._settle(DIST)

    def test_row3_unreleased_builds_advances_dev_then_tags(self, root):
        world = World(root, ci="never")
        release = release_for(root, world)
        head = git(root, "rev-parse", "dev")

        with pytest.raises(_packages.Waiting, match="no release run"):
            release.run()

        assert ("build", head, DIST) in world.calls
        assert remote_tag(root, TAG) == head
        assert remote_dev(root) == head

    def test_row4_on_pypi_without_a_tag_refuses(self, root):
        world = World(root)
        world.upload(DIST, "1.0.0", b"uploaded by hand")
        release = release_for(root, world)

        with pytest.raises(_packages.Refusal, match="outside this machinery"):
            release.run()
        assert remote_tag(root, TAG) is None

    def test_row5_tag_with_other_build_inputs_refuses(self, root):
        head = git(root, "rev-parse", "dev")
        push_tag(root, TAG, head)
        write(root, "libs/alpha/extra.py", "x = 1\n")
        commit_all(root, "alpha: change sources without a bump")
        world = World(root)
        release = release_for(root, world)

        with pytest.raises(_packages.Refusal, match="different build inputs; bump"):
            release.run()
        assert remote_tag(root, TAG) == head  # never moved

    def test_row6_tagged_waits_for_the_run_then_pypi(self, root):
        head = git(root, "rev-parse", "dev")
        push_tag(root, TAG, head)
        world = World(root)
        release = release_for(root, world)

        final = release.run()

        assert world.clock > 0  # it polled
        assert json.loads(git(root, "show", f"{final}:{rb.RELEASED_PATH}"))[DIST]["1.0.0"][
            "commit"] == head

    def test_row7_anomaly_refuses(self, root):
        head = git(root, "rev-parse", "dev")
        push_tag(root, TAG, head)
        world = World(root)
        world.upload(DIST, "1.0.0", fake_wheel_bytes(root, head, DIST), extra=True)

        with pytest.raises(_packages.Refusal, match="ANOMALY"):
            release_for(root, world).run()

    def test_row8_uploaded_is_verified_and_recorded(self, root):
        head = git(root, "rev-parse", "dev")
        push_tag(root, TAG, head)
        world = World(root)
        data = fake_wheel_bytes(root, head, DIST)
        world.upload(DIST, "1.0.0", data)

        final = release_for(root, world).run()

        entry = json.loads(git(root, "show", f"{final}:{rb.RELEASED_PATH}"))[DIST]["1.0.0"]
        assert entry["wheel_sha256"] == hashlib.sha256(data).hexdigest()
        assert entry["commit"] == head
        assert entry["tag"] == TAG
        assert set(entry) == set(_packages.LEDGER_FIELDS)
        assert remote_dev(root) == final


# --- observations ------------------------------------------------------------------

class TestObservations:
    def test_peeled_tag_used(self, root):
        head = git(root, "rev-parse", "dev")
        tag_object = push_tag(root, TAG, head)
        assert tag_object != head  # annotated: the ref names a tag object

        assert _packages.observe_tag(_packages.Repo(root), TAG) == head

    def test_obs_error_no_state_change(self, root):
        world = World(root)
        world.pypi_error = True
        before = remote_dev(root)

        with pytest.raises(_packages.ObservationFailed, match="re-run"):
            release_for(root, world).run()

        assert remote_dev(root) == before
        assert remote_tag(root, TAG) is None
        assert not [c for c in world.calls if c[0] == "build"]

    def test_yanked_and_anomaly_refuse(self, root):
        head = git(root, "rev-parse", "dev")
        push_tag(root, TAG, head)
        world = World(root)
        world.upload(DIST, "1.0.0", fake_wheel_bytes(root, head, DIST), yanked=True)
        with pytest.raises(_packages.Refusal, match="YANKED"):
            release_for(root, world).run()

        world.upload(DIST, "1.0.0", b"x", extra=True)
        with pytest.raises(_packages.Refusal, match="ANOMALY"):
            release_for(root, world).run()
        assert "1.0.0" not in json.loads(
            git(root, "show", f"origin/dev:{rb.RELEASED_PATH}")).get(DIST, {})

    def test_newest_run_wins(self, root):
        head = git(root, "rev-parse", "dev")
        push_tag(root, TAG, head)
        world = World(root)
        world.runs.append({"databaseId": 1, "status": "completed", "conclusion": "failure",
                           "displayTitle": f"release {TAG}",
                           "createdAt": "2025-12-31T00:00:00Z", "url": "https://old"})
        world.runs.append({"databaseId": 2, "status": "in_progress", "conclusion": "",
                           "displayTitle": f"release {TAG}",
                           "createdAt": "2026-01-01T00:00:00Z", "url": "https://new"})

        final = release_for(root, world).run()

        assert DIST in json.loads(git(root, "show", f"{final}:{rb.RELEASED_PATH}"))

    def test_no_automatic_redispatch(self, root):
        head = git(root, "rev-parse", "dev")
        push_tag(root, TAG, head)
        world = World(root, ci="fail")

        with pytest.raises(_packages.Waiting) as exc:
            release_for(root, world).run()

        message = str(exc.value)
        assert "ended failure" in message
        assert f"gh workflow run release-packages.yml --ref master -f tag={TAG}" in message
        assert "gh run rerun" in message
        assert {c[1][:2] for c in world.calls if c[0] == "gh"} == {("run", "list")}

    def test_running_too_long_stops_without_state_change(self, root):
        head = git(root, "rev-parse", "dev")
        push_tag(root, TAG, head)
        world = World(root, ci="running")
        before = remote_dev(root)

        with pytest.raises(_packages.Waiting, match="still in_progress"):
            release_for(root, world).run()
        assert remote_dev(root) == before


# --- verify (B4) ------------------------------------------------------------------

class TestVerify:
    def _uploaded(self, root, *, tamper=False):
        head = git(root, "rev-parse", "dev")
        push_tag(root, TAG, head)
        world = World(root)
        world.upload(DIST, "1.0.0", fake_wheel_bytes(root, head, DIST, tamper=tamper))
        return world

    def test_member_mismatch_permanent_but_verifier_error_not(self, root):
        world = self._uploaded(root, tamper=True)
        with pytest.raises(_packages.Refusal,
                           match="PyPI artifact differs from the tag source; bump"):
            release_for(root, world).run()

        world = self._uploaded_again(root)
        world.build_error = True
        with pytest.raises(_packages.ObservationFailed, match="re-run"):
            release_for(root, world).run()
        assert DIST not in json.loads(git(root, "show", f"origin/dev:{rb.RELEASED_PATH}"))

    def _uploaded_again(self, root):
        head = git(root, "rev-parse", "dev")
        world = World(root)
        world.upload(DIST, "1.0.0", fake_wheel_bytes(root, head, DIST))
        return world

    def test_downloaded_file_is_installed(self, root):
        """The upload is the reference's members re-zipped (deflated, not
        stored): equal content, different bytes, so installing the reference
        instead of the downloaded file would show."""
        world = self._uploaded(root)
        with zipfile.ZipFile(io.BytesIO(world.pypi[(DIST, "1.0.0")]["data"])) as zf:
            members = [(i, zf.read(i.filename)) for i in zf.infolist()]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for info, body in members:
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, body)
        world.upload(DIST, "1.0.0", buf.getvalue())
        published = world.pypi[(DIST, "1.0.0")]["data"]
        assert published != fake_wheel_bytes(root, git(root, "rev-parse", "dev"), DIST)

        release_for(root, world).run()

        assert world.smoked == [published]

    def test_smoke_failure_on_matching_bytes_is_not_recorded(self, root):
        world = self._uploaded(root)
        world.smoke_result = {"error": "ImportError('boom')"}

        with pytest.raises(_packages.Refusal, match="NOT recorded"):
            release_for(root, world).run()
        assert DIST not in json.loads(git(root, "show", f"origin/dev:{rb.RELEASED_PATH}"))

    def test_record_rows_reorder_is_not_a_mismatch(self, root):
        head = git(root, "rev-parse", "dev")
        data = fake_wheel_bytes(root, head, DIST)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            members = {n: zf.read(n) for n in zf.namelist()}
        record = next(n for n in members if n.endswith("RECORD"))
        members[record] = b"".join(reversed(members[record].splitlines(keepends=True)))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, body in members.items():
                zf.writestr(name, body)

        assert rb.compare_wheels(buf.getvalue(), data) == []
        members[record] += b"extra,sha256=x,1\n"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, body in members.items():
                zf.writestr(name, body)
        assert rb.compare_wheels(buf.getvalue(), data) == [f"{record} differs"]


# --- ledger (B5) and dev advance ---------------------------------------------------

class _CompetingRepo(_packages.Repo):
    """A Repo whose every push to dev is preceded by another machine's push."""

    def __init__(self, root: Path, clone: Path):
        super().__init__(root)
        self.clone = clone
        self.dev_pushes = 0

    def run(self, *args, **kwargs):
        if args[:1] == ("push",) and args[-1].endswith(":refs/heads/dev"):
            self.dev_pushes += 1
            write(self.clone, f"other-{self.dev_pushes}.txt", "elsewhere\n")
            commit_all(self.clone, f"other session {self.dev_pushes}")
            git(self.clone, "push", "-q", "origin", "dev")
        return super().run(*args, **kwargs)


class _OrderRepo(_packages.Repo):
    def __init__(self, root: Path):
        super().__init__(root)
        self.pushes: list[str] = []

    def run(self, *args, **kwargs):
        if args[:1] == ("push",):
            self.pushes.append(args[-1])
        return super().run(*args, **kwargs)


class TestLedgerAndDev:
    ENTRY = {"version": "1.0.0", "tag": TAG, "tag_url": rb.tag_url(REPOSITORY, TAG),
             "commit": "a" * 40, "filename": rb.wheel_filename(DIST, "1.0.0"),
             "wheel_sha256": "b" * 64, "verified_at": "2026-01-01T00:00:00Z"}

    def test_ledger_refuses_moved_parent(self, root, tmp_path):
        repo = _CompetingRepo(root, other_clone(root, tmp_path))

        with pytest.raises(_packages.Refusal, match="moved on every one of 3"):
            _packages.ledger_commit(repo, DIST, dict(self.ENTRY))

        assert repo.dev_pushes == _packages.LEDGER_TRIES
        assert DIST not in json.loads(git(root, "show", f"origin/dev:{rb.RELEASED_PATH}"))

    def test_ledger_rebuilds_on_the_new_tip(self, root, tmp_path):
        clone = other_clone(root, tmp_path)
        write(clone, "other.txt", "elsewhere\n")
        moved = commit_all(clone, "other session")
        git(clone, "push", "-q", "origin", "dev")

        tip = _packages.ledger_commit(_packages.Repo(root), DIST, dict(self.ENTRY))

        assert git(root, "rev-parse", f"{tip}^") == moved
        # Local dev holds none of the other session's commits, so it is left alone.
        assert git(root, "rev-parse", "dev") != tip

    def test_identical_entry_is_a_no_op(self, root):
        first = _packages.ledger_commit(_packages.Repo(root), DIST, dict(self.ENTRY))
        again = dict(self.ENTRY, verified_at="2026-02-02T00:00:00Z")
        assert _packages.ledger_commit(_packages.Repo(root), DIST, again) == first

    def test_tag_waits_for_B_on_remote(self, root, tmp_path):
        repo = _OrderRepo(root)
        world = World(root, ci="never")
        with pytest.raises(_packages.Waiting):
            release_for(root, world, repo).run()
        assert repo.pushes.index(f"refs/tags/{TAG}") > repo.pushes.index(
            f"{git(root, 'rev-parse', 'dev')}:refs/heads/dev")

        # dev moved on origin: B cannot reach it, so nothing is tagged.
        root2 = make_repo(tmp_path / "second")
        clone = other_clone(root2, tmp_path / "second")
        write(clone, "other.txt", "elsewhere\n")
        commit_all(clone, "other session")
        git(clone, "push", "-q", "origin", "dev")
        world = World(root2, ci="never")
        with pytest.raises(_packages.Refusal, match="git pull --no-rebase"):
            release_for(root2, world).run()
        assert remote_tag(root2, TAG) is None

    def test_dependent_tag_waits_for_dep_recorded(self, tmp_path):
        root = make_repo(tmp_path, ((DIST, "alpha"), ("beta-lib", "beta")))
        write(root, "packages/beta-lib/pyproject.toml",
              pyproject("beta-lib", "1.0.0", (f"{DIST}>=1.0",)))
        commit_all(root, "beta depends on alpha")
        world = World(root, ci="never")
        release = release_for(root, world)
        assert release.plan.release_set == [DIST, "beta-lib"]

        with pytest.raises(_packages.Waiting, match="beta-lib waits for its dependency"):
            release._settle("beta-lib")
        assert remote_tag(root, rb.tag_name("beta-lib", "1.0.0")) is None
        assert not [c for c in world.calls if c[0] == "build"]


# --- same_src ------------------------------------------------------------------

class TestSameSrc:
    @pytest.mark.parametrize("path", ["packages/_build/constraints.txt",
                                      "packages/_build/hatch_build.py",
                                      "packages/registry.toml",
                                      f"packages/{DIST}/pyproject.toml",
                                      "libs/alpha/__init__.py"])
    def test_same_src_covers_hook_and_constraints(self, root, path):
        before = git(root, "rev-parse", "dev")
        (root / path).write_text((root / path).read_text() + "\n# changed\n")
        after = commit_all(root, f"change {path}")
        assert not _packages.same_src(_packages.Repo(root), before, after, DIST)

    def test_unrelated_change_keeps_same_src(self, root):
        before = git(root, "rev-parse", "dev")
        write(root, "plugins/pub-kit/README.md", "docs\n")
        after = commit_all(root, "docs")
        assert _packages.same_src(_packages.Repo(root), before, after, DIST)


# --- release_build: identity and content ------------------------------------------------

class TestReleaseBuild:
    def test_hook_inputs_refuse_when_missing(self):
        full = {"RELEASE_TAG": TAG, "RELEASE_COMMIT": "a" * 40,
                "RELEASE_TAG_URL": rb.tag_url(REPOSITORY, TAG)}
        assert rb.identity_from_env(full) == full
        for name in rb.IDENTITY_VARS:
            partial = {k: v for k, v in full.items() if k != name}
            with pytest.raises(rb.BuildError, match=name):
                rb.identity_from_env(partial)

    def test_identity_must_name_this_distribution(self):
        identity = {"RELEASE_TAG": TAG, "RELEASE_COMMIT": "a" * 40,
                    "RELEASE_TAG_URL": rb.tag_url(REPOSITORY, TAG)}
        rb.check_identity(identity, DIST, "1.0.0", REPOSITORY)
        with pytest.raises(rb.BuildError):
            rb.check_identity(identity, DIST, "1.0.1", REPOSITORY)

    def test_wheel_content_test(self, root, tmp_path):
        head = git(root, "rev-parse", "dev")
        wheel = tmp_path / rb.wheel_filename(DIST, "1.0.0")
        wheel.write_bytes(fake_wheel_bytes(root, head, DIST))
        entry = rb.parse_registry((root / rb.REGISTRY_PATH).read_text()).entry(DIST)
        url = rb.tag_url(REPOSITORY, TAG)
        rb.check_wheel(wheel, entry, "1.0.0", TAG, head, url)
        with pytest.raises(rb.BuildError, match="release marker"):
            rb.check_wheel(wheel, entry, "1.0.0", TAG, "c" * 40, url)

    def test_split_tag(self):
        assert rb.split_tag("katsoft-llm-kit-v1.2.0") == ("katsoft-llm-kit", "1.2.0")
        with pytest.raises(rb.BuildError):
            rb.split_tag("katsoft-llm-kit")

    def test_registry_rejects_unknown_keys(self):
        with pytest.raises(rb.BuildError, match="unknown"):
            rb.parse_registry(registry_text() + "extra = 1\n")

    def test_the_committed_registry_and_ledger_are_valid(self):
        registry = rb.parse_registry((_ROOT / rb.REGISTRY_PATH).read_text())
        ledger = json.loads((_ROOT / rb.RELEASED_PATH).read_text())
        registered = {rb.normalize_dist(e.name) for e in registry.entries}
        assert {rb.normalize_dist(d) for d in ledger} <= registered


def load_as(name: str):
    """test_publish_packages.py loads this module's helpers through here."""
    spec = importlib.util.spec_from_file_location(name, Path(__file__))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
