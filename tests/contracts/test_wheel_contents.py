"""The built wheel carries exactly the contracts distribution and its release marker.

The wheel is built the way the release machinery builds it
(packages/_build/release_build.py: shared hatchling hook, pinned build
constraints, SOURCE_DATE_EPOCH, release identity) from a throwaway copy of the
working tree with a synthetic registry entry. The real registry stays empty
while the package is dormant.
"""
import io
import zipfile

import pytest

import contract_support as support


@pytest.fixture(scope="module")
def wheel(tmp_path_factory):
    return support.build_wheel(tmp_path_factory.mktemp("wheel"))


def test_wheel_contents(wheel):
    assert support.wheel_problems(wheel) == []


def test_wheel_marker_names_the_release(wheel):
    rb = support.release_build_module()
    tag = rb.tag_name(support.DIST, support.VERSION)
    with zipfile.ZipFile(wheel) as zf:
        marker = zf.read("katsoft_contracts/_release.py").decode("utf-8")
    assert marker == rb.render_marker(
        support.DIST, support.VERSION, tag, support.COMMIT,
        rb.tag_url("https://github.com/kitaekatt/plugins-kit", tag))
    assert 'MODE = "package"' in marker


def test_wheel_build_is_reproducible(wheel, tmp_path):
    again = support.build_wheel(tmp_path)
    assert support.sha256(again) == support.sha256(wheel)


def _rewrite(wheel, mutate):
    """A copy of the wheel's bytes with the member dict edited by `mutate`."""
    with zipfile.ZipFile(wheel) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    mutate(members)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as out:
        for name, data in members.items():
            out.writestr(name, data)
    return buffer.getvalue()


@pytest.mark.parametrize("label,mutate,expect", [
    ("extra member", lambda m: m.update({"katsoft_contracts/extra.py": b"x = 1\n"}), "members differ"),
    ("missing module", lambda m: m.pop("katsoft_contracts/model_declaration.py"), "members differ"),
    ("edited module", lambda m: m.update({"katsoft_contracts/execution_event.py": b"# changed\n"}),
     "is not the canonical source"),
    ("missing marker", lambda m: m.pop("katsoft_contracts/_release.py"), "release wheel-content test"),
    ("third-party import",
     lambda m: m.update({"katsoft_contracts/__init__.py": b"import yaml\n"}), "imports non-standard-library"),
])
def test_wheel_check_detects_a_broken_wheel(wheel, tmp_path, label, mutate, expect):
    broken = tmp_path / wheel.name
    broken.write_bytes(_rewrite(wheel, mutate))
    problems = support.wheel_problems(broken)
    assert any(expect in problem for problem in problems), (label, problems)
