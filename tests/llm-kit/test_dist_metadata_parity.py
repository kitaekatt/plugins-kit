"""The plugin consumes the katsoft-llm-kit distribution; the distribution declares what it serves.

The plugin pyproject (plugins/llm-kit/pyproject.toml) is a non-package project:
its venv installs the released library through one requirement,
``katsoft-llm-kit[<extras>]>=A.B,<N``, and the console script and the extras'
third-party packages come from that distribution. Nothing the library needs is
restated in the plugin, so the checks here pin the requirement's form, that it
asks for every extra the distribution offers, that the version it floors is one
the distribution can satisfy, and that bootstrap resolves it against the
released-package ledger. The distribution pyproject
(packages/katsoft-llm-kit/pyproject.toml) must still carry its Python floor, the
console script and the contracts requirement.
"""
import json
import re
import tomllib

import pytest

import dist_support as ds

PLUGIN_ROOT = ds.REPO / "plugins" / "llm-kit"
# The plugin-venv import that proves each extra's third-party packages are present.
EXTRA_IMPORTS = {
    "config": {"yaml"},
    "sdk": {"openai"},
    "local": {"psutil"},
    "frontdoor": {"fastapi", "uvicorn", "httpx"},
    "jobs": {"yaml"},
    "pipeline": {"yaml"},
}


def load(path):
    return tomllib.loads(path.read_text(encoding="utf-8"))


def version_tuple(text):
    parts = [int(p) for p in text.split(".")]
    return tuple(parts + [0] * (3 - len(parts)))


@pytest.fixture(scope="module")
def plugin():
    return load(ds.PLUGIN_PYPROJECT)


@pytest.fixture(scope="module")
def dist():
    return load(ds.PACKAGE_DIR / "pyproject.toml")


@pytest.fixture(scope="module")
def manifest():
    return json.loads((PLUGIN_ROOT / "bootstrap.json").read_text(encoding="utf-8"))


def _requirement(plugin, name):
    (found,) = [r for r in plugin["project"]["dependencies"] if r.startswith(name)]
    return found


def test_the_plugin_is_a_non_package_project(plugin):
    assert plugin["tool"]["uv"]["package"] is False
    assert "build-system" not in plugin
    assert "scripts" not in plugin["project"], "the console script is the distribution's"
    assert set(plugin["project"].get("optional-dependencies", {})) == {"dev"}


def test_requires_python_agree(plugin, dist):
    assert dist["project"]["requires-python"] == plugin["project"]["requires-python"]


def test_console_script_is_the_distributions(dist):
    assert dist["project"]["scripts"] == {"llm-kit": "llm_kit.cli:main"}


def test_the_plugin_requires_exactly_the_two_distributions_in_the_form_bootstrap_enforces(plugin):
    from bootstrap_lib.venv_check import parse_restricted_requirement

    requirements = plugin["project"]["dependencies"]
    assert len(requirements) == 2
    parsed = {name.split("[")[0]: (low, hi) for name, low, hi in
              (parse_restricted_requirement(r) for r in requirements)}
    assert set(parsed) == {"katsoft-llm-kit", "katsoft-contracts"}
    assert parsed["katsoft-contracts"] == ((1, 0, 0), 2)
    assert parsed["katsoft-llm-kit"][1] == 2


def test_the_requirement_asks_for_every_extra_the_distribution_offers(plugin, dist):
    requirement = _requirement(plugin, "katsoft-llm-kit")
    asked = set(re.fullmatch(r"katsoft-llm-kit\[([^\]]*)\].*", requirement).group(1).split(","))
    assert asked == set(dist["project"]["optional-dependencies"]) == set(EXTRA_IMPORTS)


def test_the_floor_is_a_version_the_distribution_can_satisfy(plugin, dist):
    """The floor (1.3) is the release carrying the surface the plugin documents; the distribution is at or past it, below the cap."""
    from bootstrap_lib.venv_check import parse_restricted_requirement

    _name, floor, cap = parse_restricted_requirement(_requirement(plugin, "katsoft-llm-kit"))
    assert floor == (1, 3, 0)
    assert floor <= version_tuple(dist["project"]["version"]) and version_tuple(dist["project"]["version"])[0] < cap


def test_the_contracts_requirement_equals_the_distributions_own(plugin, dist):
    assert _requirement(plugin, "katsoft-contracts") in dist["project"]["dependencies"]
    assert dist["project"]["dependencies"] == ["katsoft-contracts>=1.0,<2"]


def test_the_venv_names_no_extras_and_checks_every_import_the_extras_bring(manifest):
    assert "extras" not in manifest["venv"], "extras flow from the katsoft-llm-kit requirement"
    wanted = set().union(*EXTRA_IMPORTS.values()) | {"llm_kit"}
    assert set(manifest["venv"]["check_imports"]) == wanted


def test_bootstrap_resolves_the_requirement_against_the_released_ledger(tmp_path):
    """With 1.3.1 released, the venv target is the highest 1.x; with only 1.1.x released, no target satisfies it."""
    from bootstrap_lib.venv_check import package_targets

    def ledger(*versions):
        path = tmp_path / f"released-{len(versions)}.json"
        path.write_text(json.dumps({
            "katsoft-contracts": {"1.0.0": {}},
            "katsoft-llm-kit": {v: {} for v in versions},
        }), encoding="utf-8")
        return str(path)

    targets, errors = package_targets(str(PLUGIN_ROOT), ledger("1.1.0", "1.2.0", "1.3.1"))
    assert errors == [] and targets == [("katsoft-contracts", "1.0.0"), ("katsoft-llm-kit", "1.3.1")]
    _targets, errors = package_targets(str(PLUGIN_ROOT), ledger("1.0.0", "1.1.0"))
    assert len(errors) == 1 and "no released version of katsoft-llm-kit" in errors[0]
    _targets, errors = package_targets(str(PLUGIN_ROOT), ledger("1.3.0", "2.0.0"))
    assert errors == [], "a major above the cap is not a candidate"


def test_distribution_identity_and_src_layout(dist):
    project = dist["project"]
    assert project["name"] == "katsoft-llm-kit"
    assert project["dynamic"] == ["urls"]
    assert dist["build-system"]["build-backend"] == "hatchling.build"
    wheel = dist["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["src/llm_kit"]
    assert wheel["exclude"] == ["src/llm_kit/_deployment.py"]
    assert "force-include" not in wheel
    sdist = dist["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert sdist["include"] == ["src/llm_kit", "README.md", "LICENSE", "pyproject.toml"]
    assert "hooks" not in sdist, "a release wheel is built only by release_build.py"
    assert dist["tool"]["hatch"]["metadata"]["hooks"]["custom"]["path"] == "../_build/hatch_build.py"
    assert wheel["hooks"]["custom"]["path"] == "../_build/hatch_build.py"
    assert (ds.PACKAGE_DIR / "../_build/hatch_build.py").resolve().is_file()
    assert (ds.PACKAGE_DIR / "src" / "llm_kit" / "__init__.py").is_file()
    assert not (ds.REPO / "plugins" / "llm-kit" / "lib").exists(), "the library source lives only in the package"


def test_the_distribution_version_is_independent_of_the_plugin_version(plugin, dist):
    """Library versions are independent of plugin versions: no relation is asserted,
    but both must be static strings the release machinery can read."""
    assert isinstance(dist["project"]["version"], str) and isinstance(plugin["project"]["version"], str)


def test_plugin_pyproject_and_manifest_versions_agree(plugin):
    manifest = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["version"] == plugin["project"]["version"]


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
