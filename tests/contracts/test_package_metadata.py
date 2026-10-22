"""Static facts of the katsoft-contracts source package."""
import ast
import tomllib

import contract_support as support


def _project():
    return tomllib.loads((support.PACKAGE_DIR / "pyproject.toml").read_text(encoding="utf-8"))


def test_distribution_identity():
    data = _project()
    project = data["project"]
    assert project["name"] == "katsoft-contracts"
    assert project["version"] == "1.0.0"
    assert project["dependencies"] == []
    assert project["dynamic"] == ["urls"]
    assert data["build-system"]["build-backend"] == "hatchling.build"
    assert data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/katsoft_contracts"]


def test_both_hatch_hooks_point_at_the_shared_hook():
    data = _project()["tool"]["hatch"]
    assert data["metadata"]["hooks"]["custom"]["path"] == "../_build/hatch_build.py"
    assert data["build"]["targets"]["wheel"]["hooks"]["custom"]["path"] == "../_build/hatch_build.py"
    assert (support.PACKAGE_DIR / "pyproject.toml").parent.joinpath("../_build/hatch_build.py").resolve().is_file()


def test_modules_import_only_the_standard_library():
    for path in sorted(support.CANONICAL_DIR.glob("*.py")):
        assert support.third_party_imports(path.read_text(encoding="utf-8")) == [], path.name


def test_no_release_marker_is_committed():
    """The build hook writes _release.py into the wheel; a committed one would collide."""
    assert not (support.CANONICAL_DIR / "_release.py").exists()


def test_public_surface_is_the_literal_all_the_machinery_reads():
    tree = ast.parse((support.CANONICAL_DIR / "__init__.py").read_text(encoding="utf-8"))
    names = [ast.literal_eval(n.value) for n in tree.body
             if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "__all__" for t in n.targets)]
    assert names == [list(support.MODULES)]
