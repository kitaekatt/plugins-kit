"""Static dependency-completeness check across all plugins.

Goal: fail when a plugin USES a third-party package -- directly in its own .py
files, or transitively through a first-party library it imports (e.g. workflow-kit
-> llm_scripting_kit -> openai) -- but does NOT DECLARE it in the plugin's
pyproject.toml. That is the failure mode that only surfaces on a fresh machine,
after the dep was hand-installed on the dev box.

How it works (pure static analysis -- no network, no venv build):
  1. Build the repo's first-party name set: every top-level package root, plus
     every shipped .py module basename (single-file modules imported cross-dir via
     a sys.path entry are still first-party).
  2. For each plugin, start from its shipped .py files and follow imports through
     first-party PACKAGES (the plugin's own packages and any first-party lib it
     imports, incl. shared libs in OTHER plugins), collecting third-party leaves.
     Foreign functions follow selected imports and local references; unrelated
     lazy function bodies do not become consumer dependencies.
     A plugin's own single-file modules don't need following -- they are already
     in its own scanned file set, so their direct third-party imports are caught.
  3. Classify each imported top-level name: stdlib / runtime-provided -> ignore;
     first-party -> follow (packages) or ignore (single-file); else third-party.
  4. Resolve declared distributions from pyproject.toml (deps + optional groups)
     via an import-name -> dist-name alias table.
  5. Assert every required third-party import is declared.

Deliberate exclusions:
  - Imports inside a ``try/except ImportError`` (the optional-dependency
    convention, e.g. ``try: import yaml`` with a HAVE_YAML fallback) are NOT
    required -- the code already handles their absence.
  - Modules provided by the Unreal Editor's embedded Python at runtime (``unreal``,
    ``unreal_pip``, ``pkg_resources``) are not pip-installable and are exempt.
"""

import ast
import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGINS = _REPO_ROOT / "plugins"

_SKIP_DIRS = {".venv", "site-packages", "__pycache__", "node_modules", "stubs"}

# import name -> distribution name, where they differ.
_IMPORT_TO_DIST = {
    "markdown_it": "markdown-it-py",
    "yaml": "pyyaml",
    "websocket": "websocket-client",
}

# Provided by the Unreal Editor's embedded Python at runtime (script-bootstrap
# layer); not pip-installable, so not declarable in pyproject.
_RUNTIME_PROVIDED = {"unreal", "unreal_pip", "pkg_resources"}

_STDLIB = set(sys.stdlib_module_names) | {"__future__"}


def _norm(dist):
    """PEP 503-ish normalization of a distribution name for comparison."""
    return re.sub(r"[-_.]+", "-", dist.strip().lower())


def _skipped(path):
    return any(part in _SKIP_DIRS for part in path.parts)


def _py_files(root):
    for p in root.rglob("*.py"):
        if not _skipped(p.relative_to(_REPO_ROOT)):
            yield p


# --- first-party name discovery ------------------------------------------

def _build_first_party_pkgs():
    """top-level package name -> its package directory (Path), for following."""
    out = {}
    for init in _PLUGINS.rglob("__init__.py"):
        if _skipped(init.relative_to(_REPO_ROOT)):
            continue
        pkg_dir = init.parent
        if (pkg_dir.parent / "__init__.py").exists():
            continue  # only top-level package roots
        out.setdefault(pkg_dir.name, pkg_dir)
    return out


def _build_first_party_names(pkgs):
    """Every first-party top-level importable name (packages + module basenames)."""
    names = set(pkgs)
    for p in _PLUGINS.rglob("*.py"):
        if _skipped(p.relative_to(_REPO_ROOT)):
            continue
        if p.stem != "__init__":
            names.add(p.stem)
    return names


_FIRST_PARTY = _build_first_party_pkgs()
_FIRST_PARTY_NAMES = _build_first_party_names(_FIRST_PARTY)


def _resolve_first_party(dotted, sibling_dir):
    """Resolve a dotted name to first-party PACKAGE source files, or [].

    Returns every source file the import would execute (intermediate package
    __init__.py files plus the leaf). Only resolves packages and same-dir
    siblings -- single-file first-party modules elsewhere are classified
    first-party but not followed (handled by their own plugin's scan).
    """
    parts = dotted.split(".")
    top = parts[0]
    if top in _FIRST_PARTY:
        base = _FIRST_PARTY[top].parent
    elif (sibling_dir / (top + ".py")).exists() or (sibling_dir / top / "__init__.py").exists():
        base = sibling_dir
    else:
        return []

    files = []
    cur = base
    for i, seg in enumerate(parts):
        cur = cur / seg
        ini = cur / "__init__.py"
        if ini.exists():
            files.append(ini)
        leaf = cur.with_suffix(".py")
        if i == len(parts) - 1 and leaf.exists():
            files.append(leaf)
    return files


# --- import extraction ----------------------------------------------------

def _handler_catches_import(handler):
    exc = handler.type
    if exc is None:
        return True  # bare except
    targets = exc.elts if isinstance(exc, ast.Tuple) else [exc]
    for t in targets:
        name = getattr(t, "id", None) or getattr(t, "attr", None)
        if name in ("ImportError", "ModuleNotFoundError"):
            return True
    return False


def _guarded_import_nodes(tree):
    """ids of Import/ImportFrom nodes inside a try/except-ImportError (optional)."""
    guarded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(_handler_catches_import(h) for h in node.handlers):
            for region in (node.body, *(h.body for h in node.handlers)):
                for sub in region:
                    for n in ast.walk(sub):
                        if isinstance(n, (ast.Import, ast.ImportFrom)):
                            guarded.add(id(n))
    return guarded


def _imports_in(tree):
    """Yield (kind, module_or_name, level, from_names, guarded) for every import.

    ``guarded`` marks imports inside a try/except-ImportError. Guards never stop
    FOLLOWING a first-party import (the lib's transitive deps are still needed when
    it is present) -- they only exempt a THIRD-PARTY leaf, and only in the plugin's
    OWN files (its own optional-dependency handling). A guard inside a first-party
    lib owned by ANOTHER plugin reflects that lib's optional stance, not the
    consumer's: a consumer importing the guarded path genuinely needs the dep
    (llm_scripting_kit guards ``openai``, but workflow-kit calling make_openai_client
    requires it).
    """
    guarded_ids = _guarded_import_nodes(tree)
    for node in ast.walk(tree):
        g = id(node) in guarded_ids
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield ("import", alias.name, 0, [], g)
        elif isinstance(node, ast.ImportFrom):
            yield ("from", node.module, node.level or 0, [a.name for a in node.names], g)


def _reachable_tree(tree: ast.Module, names: set[str] | None) -> ast.Module:
    """Follow selected foreign symbols and their local references.

    Module-level statements always execute. Function/class bodies are needed
    only when imported or referenced by reachable code. A module import (None)
    remains conservative because callers can use any of its attributes.
    """
    if names is None or "*" in names:
        return tree
    definitions = {
        node.name: node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    body = [node for node in tree.body if node not in definitions.values()]
    pending = set(names)
    # Defaults, annotations, bases and decorators execute during definition.
    for node in definitions.values():
        if isinstance(node, ast.ClassDef):
            body.extend(node.bases)
            body.extend(node.keywords)
            body.extend(_reachable_tree(ast.Module(body=node.body, type_ignores=[]), set()).body)
        else:
            body.append(node.args)
            if node.returns is not None:
                body.append(node.returns)
        body.extend(node.decorator_list)
    visited = set()
    while True:
        pending.update(
            node.id for root in body for node in ast.walk(root)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        )
        selected = (pending & definitions.keys()) - visited
        if not selected:
            break
        body.extend(definitions[name] for name in selected)
        visited.update(selected)
    return ast.Module(body=body, type_ignores=[])


def _required_third_party(plugin_root):
    """Walk the plugin's first-party import closure; return required dist names."""
    required = set()
    plugin_root_resolved = plugin_root.resolve()
    own_files = list(_py_files(plugin_root))
    worklist = list(own_files)
    selected_names: dict[Path, set[str] | None] = {
        p.resolve(): None for p in own_files
    }

    def _enqueue(files: list[Path], names: set[str] | None = None) -> None:
        for index, f in enumerate(files):
            r = f.resolve()
            selected = names if index == len(files) - 1 else set()
            if r not in selected_names:
                selected_names[r] = selected
                worklist.append(f)
            elif selected_names[r] is not None:
                previous = selected_names[r]
                if selected is None or not selected.issubset(previous):
                    selected_names[r] = None if selected is None else previous | selected
                    worklist.append(f)

    while worklist:
        f = worklist.pop()
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        tree = _reachable_tree(tree, selected_names[f.resolve()])
        sibling_dir = f.parent
        is_own = f.resolve().is_relative_to(plugin_root_resolved)
        for kind, mod, level, names, guarded in _imports_in(tree):
            if level and level > 0:
                # Relative import: within this first-party package; follow, never third-party.
                pkg = f.parent
                for _ in range(level - 1):
                    pkg = pkg.parent
                # Follow the target module itself (from .client import X -> client.py)
                # AND each name as a possible submodule (from .sub import deeper).
                dotted_targets = []
                if mod:
                    dotted_targets.append((mod, set(names)))
                    dotted_targets += [(mod + "." + nm, None) for nm in names]
                else:
                    dotted_targets += [(nm, None) for nm in names]
                for dotted, selected in dotted_targets:
                    target = pkg / Path(*dotted.split("."))
                    cand = []
                    if target.with_suffix(".py").exists():
                        cand.append(target.with_suffix(".py"))
                    if (target / "__init__.py").exists():
                        cand.append(target / "__init__.py")
                    _enqueue(cand, selected)
                continue

            if mod is None:
                continue
            top = mod.split(".")[0]
            if top in _STDLIB or top in _RUNTIME_PROVIDED:
                continue
            fp = _resolve_first_party(mod, sibling_dir)
            if fp:
                _enqueue(fp, set(names) if kind == "from" else None)
                if kind == "from":
                    for nm in names:
                        submodule = _resolve_first_party(mod + "." + nm, sibling_dir)
                        if submodule and submodule[-1] not in fp:
                            _enqueue(submodule)
                continue
            if top in _FIRST_PARTY_NAMES:
                continue  # first-party single-file module; its deps caught in its own plugin
            # Third-party leaf. Guard exempts only the plugin's OWN optional handling.
            if guarded and is_own:
                continue
            required.add(_norm(_IMPORT_TO_DIST.get(top, top)))

    return required


# --- declared distributions ----------------------------------------------

def _load_toml(path):
    try:
        import tomllib
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except ModuleNotFoundError:
        try:
            import tomli
            return tomli.loads(path.read_text(encoding="utf-8"))
        except ModuleNotFoundError:
            return None


def _dist_of(req):
    """Distribution name from a requirement string (strip version/extras/markers/url)."""
    return _norm(re.split(r"[<>=!~;\[ @]", req.strip(), maxsplit=1)[0])


def _declared_dists(plugin_root):
    pyproject = plugin_root / "pyproject.toml"
    if not pyproject.is_file():
        return None  # no declaration surface at all
    data = _load_toml(pyproject)
    declared = set()
    if data is not None:
        project = data.get("project", {})
        for req in project.get("dependencies", []):
            declared.add(_dist_of(req))
        for group in project.get("optional-dependencies", {}).values():
            for req in group:
                declared.add(_dist_of(req))
    else:
        # Fallback when no TOML parser is available: regex-extract requirement strings.
        text = pyproject.read_text(encoding="utf-8")
        for m in re.finditer(r"(?:dependencies\s*=\s*\[|optional-dependencies)[^\]]*", text):
            for q in re.findall(r"[\"']([^\"']+)[\"']", m.group(0)):
                if re.match(r"^[A-Za-z0-9_.\-]+", q):
                    declared.add(_dist_of(q))
    return declared


def _plugin_dirs():
    for p in sorted(_PLUGINS.iterdir()):
        if p.is_dir() and any(_py_files(p)):
            yield p


def test_every_plugin_declares_its_third_party_deps():
    problems = {}
    for plugin in _plugin_dirs():
        required = _required_third_party(plugin)
        if not required:
            continue
        declared = _declared_dists(plugin)
        if declared is None:
            problems[plugin.name] = sorted(required) + ["(no pyproject.toml)"]
            continue
        missing = sorted(required - declared)
        if missing:
            problems[plugin.name] = missing

    assert not problems, (
        "Plugins import third-party packages they do not declare in pyproject.toml "
        "(import-name -> dist via alias table; add to [project] dependencies or an "
        "optional group):\n"
        + "\n".join(f"  {name}: {deps}" for name, deps in sorted(problems.items()))
    )


def test_first_party_map_is_sane():
    # Sanity guard so a refactor that breaks package discovery is caught.
    assert "bootstrap_lib" in _FIRST_PARTY
    assert "llm_scripting_kit" in _FIRST_PARTY


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from shared.mechanical import check_phrase\n", set()),
        ("from shared.mechanical import resolve_checks\n", {"regex"}),
        ("from shared.mechanical import assemble_bundle\n", {"regex"}),
        ("import shared.mechanical\n", {"regex"}),
        (
            "from shared.mechanical import check_phrase\n"
            "from shared.mechanical import assemble_bundle\n",
            {"regex"},
        ),
    ],
)
def test_foreign_imports_follow_selected_symbols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    source: str, expected: set[str],
) -> None:
    shared = tmp_path / "provider" / "shared"
    shared.mkdir(parents=True)
    (shared / "__init__.py").write_text("", encoding="utf-8")
    (shared / "mechanical.py").write_text(
        "def check_phrase():\n    return 'checked'\n"
        "def resolve_checks():\n"
        "    from .config import build_check\n    return build_check()\n"
        "def assemble_bundle():\n    return resolve_checks()\n",
        encoding="utf-8",
    )
    (shared / "config.py").write_text(
        "def build_check():\n    import regex\n    return regex.compile('x')\n",
        encoding="utf-8",
    )
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    (consumer / "main.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "_FIRST_PARTY", {"shared": shared})
    monkeypatch.setattr(sys.modules[__name__], "_FIRST_PARTY_NAMES", {"shared"})

    assert _required_third_party(consumer) == expected


def test_review_consumers_require_regex_but_prompt_consumer_does_not() -> None:
    for plugin in ("git-kit", "p4-kit"):
        required = _required_third_party(_PLUGINS / plugin)
        assert {"regex", "markdown-it-py"} <= required
    assert "regex" not in _required_third_party(_PLUGINS / "llm-scripting-kit")


def test_foreign_definition_time_code_remains_reachable() -> None:
    tree = ast.parse(
        "def load_default():\n    import yaml\n    return None\n"
        "def unused(value=load_default()):\n    import regex\n"
        "class Unused:\n    import markdown_it\n"
        "    def method(self):\n        import openai\n"
    )
    imports = {item[1] for item in _imports_in(_reachable_tree(tree, set()))}
    assert imports == {"yaml", "markdown_it"}
