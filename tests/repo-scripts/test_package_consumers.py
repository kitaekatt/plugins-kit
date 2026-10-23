"""Plugins consume the released libraries as packages, not as bootstrap links.

Three rules keep the packaging end state from regressing:

1. No published plugin links ``llm_kit`` through ``shared_lib_imports``. The
   library arrives as the ``katsoft-llm-kit`` distribution in the plugin's own
   venv. A dev-only plugin (``published: false``) is exempt: nobody installs it.
2. Every plugin with a Python file that imports ``llm_kit`` declares
   ``katsoft-llm-kit`` in its pyproject.toml. The plugin that owns the source
   (``plugins/llm-kit``) is the library, not a consumer of it.
3. Every manifest that links ``bootstrap_lib`` declares ``katsoft-contracts``,
   because bootstrap_lib delegates its contract modules to that distribution.

The detectors are functions of a plugins directory, so the fixture tests below
run the same code on a temporary tree. Each rule is shown to go red there when
a violation is added back, which a green run on the real tree cannot show.
"""
from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGINS = REPO_ROOT / "plugins"

LLM_KIT_OWNER = "llm-kit"
_SKIP_DIRS = {".venv", "__pycache__", "node_modules", "site-packages", "stubs"}


def _norm(dist: str) -> str:
    return re.sub(r"[-_.]+", "-", dist.strip().lower())


def _dist_of(requirement: str) -> str:
    return _norm(re.split(r"[<>=!~;\[ @]", requirement.strip(), maxsplit=1)[0])


def _plugin_dirs(plugins: Path) -> list[Path]:
    return sorted(p for p in plugins.iterdir() if p.is_dir())


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_published(plugin: Path) -> bool:
    manifest = plugin / ".claude-plugin" / "plugin.json"
    return not manifest.is_file() or _json(manifest).get("published", True) is not False


def shared_lib_imports(manifest: dict) -> list[str]:
    """Every name listed under a ``shared_lib_imports`` key, at any depth."""
    found: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "shared_lib_imports":
                    found.extend(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(manifest)
    return found


def linked_libs(plugin: Path) -> list[str]:
    manifest = plugin / "bootstrap.json"
    return shared_lib_imports(_json(manifest)) if manifest.is_file() else []


def declared_dists(plugin: Path) -> set[str]:
    """Distributions the plugin's pyproject.toml names in any dependency group."""
    pyproject = plugin / "pyproject.toml"
    if not pyproject.is_file():
        return set()
    project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
    requirements = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        requirements.extend(group)
    return {_dist_of(req) for req in requirements}


def _imports_module(tree: ast.AST, module: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] == module for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and (node.module or "").split(".")[0] == module:
                return True
    return False


def python_files_importing(plugin: Path, module: str) -> list[Path]:
    hits = []
    for path in sorted(plugin.rglob("*.py")):
        if _SKIP_DIRS & set(path.relative_to(plugin).parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _imports_module(tree, module):
            hits.append(path)
    return hits


def published_llm_kit_links(plugins: Path) -> list[str]:
    return [p.name for p in _plugin_dirs(plugins)
            if _is_published(p) and "llm_kit" in linked_libs(p)]


def llm_kit_importers_missing_the_dist(plugins: Path) -> list[str]:
    return [p.name for p in _plugin_dirs(plugins)
            if p.name != LLM_KIT_OWNER
            and python_files_importing(p, "llm_kit")
            and "katsoft-llm-kit" not in declared_dists(p)]


def bootstrap_lib_linkers(plugins: Path) -> list[str]:
    return [p.name for p in _plugin_dirs(plugins) if "bootstrap_lib" in linked_libs(p)]


def bootstrap_lib_linkers_missing_contracts(plugins: Path) -> list[str]:
    return [name for name in bootstrap_lib_linkers(plugins)
            if "katsoft-contracts" not in declared_dists(plugins / name)]


# --- the real tree ---------------------------------------------------------

def test_no_published_plugin_links_llm_kit():
    offenders = published_llm_kit_links(PLUGINS)
    assert not offenders, (
        "published plugins still link llm_kit through shared_lib_imports; declare "
        "katsoft-llm-kit in pyproject.toml and drop the link: " + ", ".join(offenders))


def test_every_llm_kit_importer_declares_katsoft_llm_kit():
    offenders = llm_kit_importers_missing_the_dist(PLUGINS)
    assert not offenders, (
        "plugins import llm_kit without declaring katsoft-llm-kit in pyproject.toml: "
        + ", ".join(offenders))


def test_every_bootstrap_lib_linker_declares_katsoft_contracts():
    offenders = bootstrap_lib_linkers_missing_contracts(PLUGINS)
    assert not offenders, (
        "manifests link bootstrap_lib but pyproject.toml lacks katsoft-contracts: "
        + ", ".join(offenders))


def test_the_detectors_see_the_real_tree():
    """A guard that finds nothing proves nothing: pin what the scans must find."""
    key = re.compile(r'"shared_lib_imports"\s*:')   # a key, not a word in a comment
    with_key = {m.parent.name for m in PLUGINS.glob("*/bootstrap.json")
                if key.search(m.read_text(encoding="utf-8"))}
    found = {p.name for p in _plugin_dirs(PLUGINS) if linked_libs(p)}
    assert with_key and found == with_key
    assert "unreal-kit" in bootstrap_lib_linkers(PLUGINS)
    importers = [p.name for p in _plugin_dirs(PLUGINS)
                 if p.name != LLM_KIT_OWNER and python_files_importing(p, "llm_kit")]
    assert "workflow-kit" in importers


# --- fixtures: each rule goes red when its violation is added back ----------

def _plugin(plugins: Path, name: str, *, links=(), requirements=(), imports=(),
            published=True) -> Path:
    root = plugins / name
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0", "published": published}),
        encoding="utf-8")
    (root / "bootstrap.json").write_text(
        json.dumps({"project_venv": {"shared_lib_imports": list(links)}}), encoding="utf-8")
    deps = ", ".join(json.dumps(r) for r in requirements)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "1.0.0"\ndependencies = [{deps}]\n',
        encoding="utf-8")
    (root / "lib").mkdir()
    (root / "lib" / "mod.py").write_text(
        "".join(f"{line}\n" for line in imports) or "x = 1\n", encoding="utf-8")
    return root


def _compliant_tree(tmp_path: Path) -> Path:
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    _plugin(plugins, "consumer", links=["bootstrap_lib"],
            requirements=["katsoft-llm-kit[sdk]>=1.1,<2", "katsoft-contracts>=1.0,<2"],
            imports=["from llm_kit.dispatch import declaration"])
    _plugin(plugins, LLM_KIT_OWNER, links=["bootstrap_lib"],
            requirements=["katsoft-contracts>=1.0,<2"],
            imports=["import llm_kit"])
    return plugins


def _problems(plugins: Path) -> dict[str, list[str]]:
    return {
        "link": published_llm_kit_links(plugins),
        "importer": llm_kit_importers_missing_the_dist(plugins),
        "contracts": bootstrap_lib_linkers_missing_contracts(plugins),
    }


def test_a_compliant_tree_has_no_findings(tmp_path):
    assert _problems(_compliant_tree(tmp_path)) == {"link": [], "importer": [], "contracts": []}


def test_re_adding_an_llm_kit_link_goes_red(tmp_path):
    plugins = _compliant_tree(tmp_path)
    manifest = plugins / "consumer" / "bootstrap.json"
    data = _json(manifest)
    data["project_venv"]["shared_lib_imports"].append("llm_kit")
    manifest.write_text(json.dumps(data), encoding="utf-8")
    assert _problems(plugins)["link"] == ["consumer"]


def test_an_llm_kit_link_on_an_unpublished_plugin_is_allowed(tmp_path):
    plugins = _compliant_tree(tmp_path)
    _plugin(plugins, "dev-only", links=["llm_kit"], published=False,
            requirements=["katsoft-llm-kit>=1.1,<2"])
    assert _problems(plugins)["link"] == []


def test_a_link_nested_anywhere_in_the_manifest_is_found(tmp_path):
    plugins = _compliant_tree(tmp_path)
    (plugins / "consumer" / "bootstrap.json").write_text(
        json.dumps({"venv": {"nested": [{"shared_lib_imports": ["llm_kit"]}]}}),
        encoding="utf-8")
    assert _problems(plugins)["link"] == ["consumer"]


def test_dropping_katsoft_llm_kit_from_an_importer_goes_red(tmp_path):
    plugins = _compliant_tree(tmp_path)
    (plugins / "consumer" / "pyproject.toml").write_text(
        '[project]\nname = "consumer"\nversion = "1.0.0"\n'
        'dependencies = ["katsoft-contracts>=1.0,<2", "openai>=1.0.0"]\n', encoding="utf-8")
    assert _problems(plugins)["importer"] == ["consumer"]


def test_every_import_spelling_counts_as_importing_llm_kit(tmp_path):
    for line in ("import llm_kit", "import llm_kit.cli as cli",
                 "from llm_kit import registry", "from llm_kit.completion import halt",
                 "def f():\n    from llm_kit.jobs import select"):
        plugins = tmp_path / str(abs(hash(line)))
        plugins.mkdir()
        _plugin(plugins, "consumer", requirements=["katsoft-contracts>=1.0,<2"],
                imports=[line])
        assert _problems(plugins)["importer"] == ["consumer"], line


def test_naming_llm_kit_without_importing_it_is_not_an_import(tmp_path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    _plugin(plugins, "prose", imports=['"""Talks about llm_kit."""', "import llm_kit_other",
                                       "from . import llm_kit"])
    assert _problems(plugins)["importer"] == []


def test_dropping_katsoft_contracts_from_a_linker_goes_red(tmp_path):
    plugins = _compliant_tree(tmp_path)
    (plugins / "consumer" / "pyproject.toml").write_text(
        '[project]\nname = "consumer"\nversion = "1.0.0"\n'
        'dependencies = ["katsoft-llm-kit>=1.1,<2"]\n', encoding="utf-8")
    assert _problems(plugins)["contracts"] == ["consumer"]


def test_a_requirement_in_an_extra_counts_as_declared(tmp_path):
    plugins = _compliant_tree(tmp_path)
    (plugins / "consumer" / "pyproject.toml").write_text(
        '[project]\nname = "consumer"\nversion = "1.0.0"\ndependencies = []\n'
        '[project.optional-dependencies]\nrun = ["katsoft-contracts>=1.0,<2",'
        ' "katsoft-llm-kit>=1.1,<2"]\n', encoding="utf-8")
    assert _problems(plugins) == {"link": [], "importer": [], "contracts": []}
