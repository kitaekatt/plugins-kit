"""The built katsoft-llm-kit wheel carries exactly llm_kit plus its release marker.

The wheel is built the way the release machinery builds it (see dist_support).
The library source is force-included from the plugin; the committed legacy
`llm_kit/_deployment.py` must not reach the wheel -- the build hook's package
marker replaces it (hatch skips a forced directory's file at a path an external
forced FILE targets).
"""
import io
import zipfile

import pytest

import dist_support as ds

VERSION = ds.package_version()
STEM = f"{ds.DIST_INFO_STEM}-{VERSION}.dist-info"
LEGACY_MARKER = (ds.SOURCE_DIR / "_deployment.py").read_bytes()


@pytest.fixture(scope="module")
def wheel():
    return ds.shared_wheel()


def expected_members() -> frozenset:
    """The source tree (no caches, legacy marker excluded), the marker, the dist-info."""
    members = {
        f"llm_kit/{path.relative_to(ds.SOURCE_DIR).as_posix()}"
        for path in ds.SOURCE_DIR.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }
    members.add("llm_kit/_deployment.py")
    members |= {f"{STEM}/{name}" for name in
                ("METADATA", "RECORD", "WHEEL", "entry_points.txt", "licenses/LICENSE")}
    return frozenset(members)


def wheel_problems(path) -> list[str]:
    rb = ds.release_build_module()
    registry = rb.parse_registry(ds.SYNTHETIC_REGISTRY)
    entry = registry.entry(ds.DIST)
    tag = rb.tag_name(ds.DIST, VERSION)
    url = rb.tag_url(registry.repository, tag)
    problems: list[str] = []
    try:
        rb.check_wheel(path, entry, VERSION, tag, ds.COMMIT, url)
    except rb.BuildError as exc:
        problems.append(f"release wheel-content test: {exc}")
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        expected = expected_members()
        if names != expected:
            problems.append(f"members differ: extra {sorted(names - expected)}, "
                            f"missing {sorted(expected - names)}")
        for member in sorted(names - {"llm_kit/_deployment.py"}):
            if member.startswith("llm_kit/"):
                source = ds.SOURCE_DIR / member[len("llm_kit/"):]
                if source.is_file() and zf.read(member) != source.read_bytes():
                    problems.append(f"{member} is not the plugin source")
        if "llm_kit/_deployment.py" in names and zf.read("llm_kit/_deployment.py") == LEGACY_MARKER:
            problems.append("the legacy deployment marker reached the wheel")
        problems += [f"{m} must not be in this wheel" for m in sorted(names)
                     if m.split("/")[0] in ("katsoft_contracts", "bootstrap_lib")]
    return problems


def test_wheel_contents(wheel):
    assert wheel_problems(wheel) == []


def test_wheel_marker_names_the_release_and_replaces_the_legacy_one(wheel):
    rb = ds.release_build_module()
    tag = rb.tag_name(ds.DIST, VERSION)
    with zipfile.ZipFile(wheel) as zf:
        marker = zf.read("llm_kit/_deployment.py").decode("utf-8")
    assert marker == rb.render_marker(ds.DIST, VERSION, tag, ds.COMMIT,
                                      rb.tag_url("https://github.com/kitaekatt/plugins-kit", tag))
    assert 'MODE = "package"' in marker and LEGACY_MARKER.decode() not in marker


def test_the_committed_marker_is_legacy():
    assert b'MODE = "legacy"' in LEGACY_MARKER


def test_wheel_metadata(wheel):
    with zipfile.ZipFile(wheel) as zf:
        meta = zf.read(f"{STEM}/METADATA").decode("utf-8")
        entry_points = zf.read(f"{STEM}/entry_points.txt").decode("utf-8")
    fields = [line.partition(": ") for line in meta.split("\n\n", 1)[0].splitlines()]
    requires = [value for key, _, value in fields if key == "Requires-Dist"]
    assert "katsoft-contracts<2,>=1.0" in requires                      # the unconditional dependency
    unconditional = [r for r in requires if "extra ==" not in r]
    assert unconditional == ["katsoft-contracts<2,>=1.0"]
    assert {v for k, _, v in fields if k == "Provides-Extra"} == {
        "config", "sdk", "local", "frontdoor", "jobs", "pipeline"}
    assert ("Requires-Python", ": ", ">=3.12") in fields
    assert "[console_scripts]" in entry_points and "llm-kit = llm_kit.cli:main" in entry_points


def test_wheel_build_is_reproducible(wheel, tmp_path):
    again = ds.build_wheel(tmp_path)
    assert again.read_bytes() == wheel.read_bytes()


def _rewrite(wheel, mutate):
    with zipfile.ZipFile(wheel) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    mutate(members)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as out:
        for name, data in members.items():
            out.writestr(name, data)
    return buffer.getvalue()


@pytest.mark.parametrize("label,mutate,expect", [
    ("extra member", lambda m: m.update({"llm_kit/extra.py": b"x = 1\n"}), "members differ"),
    ("missing module", lambda m: m.pop("llm_kit/registry/models.py"), "members differ"),
    ("edited module", lambda m: m.update({"llm_kit/registry/models.py": b"# changed\n"}),
     "is not the plugin source"),
    ("legacy marker", lambda m: m.update({"llm_kit/_deployment.py": LEGACY_MARKER}),
     "legacy deployment marker reached the wheel"),
    ("missing marker", lambda m: m.pop("llm_kit/_deployment.py"), "release wheel-content test"),
    ("bundled contracts", lambda m: m.update({"katsoft_contracts/__init__.py": b""}), "must not be in this wheel"),
    ("bundled bootstrap_lib", lambda m: m.update({"bootstrap_lib/__init__.py": b""}), "must not be in this wheel"),
])
def test_wheel_check_detects_a_broken_wheel(wheel, tmp_path, label, mutate, expect):
    broken = tmp_path / wheel.name
    broken.write_bytes(_rewrite(wheel, mutate))
    problems = wheel_problems(broken)
    assert any(expect in problem for problem in problems), (label, problems)
