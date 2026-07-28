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

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGINS = _REPO_ROOT / "plugins"

_SKIP_DIRS = {".venv", "site-packages", "__pycache__", "node_modules", "stubs"}

# import name -> distribution name, where they differ.
_IMPORT_TO_DIST = {
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


def _required_third_party(plugin_root):
    """Walk the plugin's first-party import closure; return required dist names."""
    required = set()
    plugin_root_resolved = plugin_root.resolve()
    worklist = list(_py_files(plugin_root))
    visited = set(p.resolve() for p in worklist)

    def _enqueue(files):
        for f in files:
            r = f.resolve()
            if r not in visited:
                visited.add(r)
                worklist.append(f)

    while worklist:
        f = worklist.pop()
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
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
                    dotted_targets.append(mod)
                    dotted_targets += [mod + "." + nm for nm in names]
                else:
                    dotted_targets += list(names)
                for dotted in dotted_targets:
                    target = pkg / Path(*dotted.split("."))
                    cand = []
                    if target.with_suffix(".py").exists():
                        cand.append(target.with_suffix(".py"))
                    if (target / "__init__.py").exists():
                        cand.append(target / "__init__.py")
                    _enqueue(cand)
                continue

            if mod is None:
                continue
            top = mod.split(".")[0]
            if top in _STDLIB or top in _RUNTIME_PROVIDED:
                continue
            fp = _resolve_first_party(mod, sibling_dir)
            if fp:
                _enqueue(fp)
                if kind == "from":
                    for nm in names:
                        _enqueue(_resolve_first_party(mod + "." + nm, sibling_dir))
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
