"""The katsoft-llm-kit distribution declares what the plugin's own project declares.

The plugin pyproject (plugins/llm-kit/pyproject.toml) is what the plugin venv
installs; the distribution pyproject (packages/katsoft-llm-kit/pyproject.toml)
is what a consumer installs. They must agree on the Python floor, the console
script and every optional extra, so a capability has the same requirements in
both. The distribution adds exactly one dependency, katsoft-contracts, and the
plugin has none because bootstrap links bootstrap_lib instead.
"""
import tomllib

import pytest

import dist_support as ds


def load(path):
    return tomllib.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def plugin():
    return load(ds.PLUGIN_PYPROJECT)["project"]


@pytest.fixture(scope="module")
def dist():
    return load(ds.PACKAGE_DIR / "pyproject.toml")


def test_requires_python_agree(plugin, dist):
    assert dist["project"]["requires-python"] == plugin["requires-python"]


def test_console_script_agrees(plugin, dist):
    assert dist["project"]["scripts"] == plugin["scripts"] == {"llm-kit": "llm_kit.cli:main"}


def test_every_extra_agrees_except_the_plugins_dev_extra(plugin, dist):
    plugin_extras = {k: v for k, v in plugin["optional-dependencies"].items() if k != "dev"}
    assert dist["project"]["optional-dependencies"] == plugin_extras
    assert "dev" not in dist["project"]["optional-dependencies"]


def test_base_dependencies_differ_by_exactly_the_contracts_package(plugin, dist):
    assert plugin["dependencies"] == []
    assert dist["project"]["dependencies"] == ["katsoft-contracts>=1.0,<2"]


def test_distribution_identity_and_wheel_only_layout(dist):
    project = dist["project"]
    assert project["name"] == "katsoft-llm-kit"
    assert project["dynamic"] == ["urls"]
    assert dist["build-system"]["build-backend"] == "hatchling.build"
    wheel = dist["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["force-include"] == {"../../plugins/llm-kit/lib/llm_kit": "llm_kit"}
    assert "sdist" not in dist["tool"]["hatch"]["build"]["targets"]
    assert dist["tool"]["hatch"]["metadata"]["hooks"]["custom"]["path"] == "../_build/hatch_build.py"
    assert wheel["hooks"]["custom"]["path"] == "../_build/hatch_build.py"
    assert (ds.PACKAGE_DIR / "../_build/hatch_build.py").resolve().is_file()
    assert (ds.PACKAGE_DIR / "../../plugins/llm-kit/lib/llm_kit").resolve().is_dir()


def test_the_distribution_version_is_independent_of_the_plugin_version(plugin, dist):
    """Library versions are independent of plugin versions: no relation is asserted,
    but both must be static strings the release machinery can read."""
    assert isinstance(dist["project"]["version"], str) and isinstance(plugin["version"], str)


def test_plugin_pyproject_and_manifest_versions_agree(plugin):
    import json
    manifest = json.loads((ds.REPO / "plugins" / "llm-kit" / ".claude-plugin" / "plugin.json")
                          .read_text(encoding="utf-8"))
    assert manifest["version"] == plugin["version"]


def test_the_wheel_requirements_equal_the_declared_ones(dist):
    """What pip would resolve from the built wheel equals the pyproject, extra by extra."""
    import zipfile
    from packaging.requirements import Requirement
    with zipfile.ZipFile(ds.shared_wheel()) as zf:
        name = next(n for n in zf.namelist() if n.endswith("/METADATA"))
        meta = zf.read(name).decode("utf-8")
    built: dict = {}
    for line in meta.split("\n\n", 1)[0].splitlines():
        if line.startswith("Requires-Dist:"):
            requirement = Requirement(line.partition(": ")[2])
            extra = None
            if requirement.marker is not None:
                text = str(requirement.marker)
                extra = text.split("==")[1].strip(" '\"") if "extra ==" in text else None
            built.setdefault(extra, set()).add(f"{requirement.name}{requirement.specifier}")
    declared = {None: {"katsoft-contracts<2,>=1.0"}}
    for extra, requirements in dist["project"]["optional-dependencies"].items():
        declared[extra] = {f"{r.name}{r.specifier}" for r in map(Requirement, requirements)}
    assert built == declared
