"""Import-direction guard for the llm-kit component graph (check C2).

The signed architecture (docs/reference/llm-kit-architecture.md, "Exact
dependency graph") is the complete inter-component import set for the 16 final
components. This module parses every ``.py`` file under
``packages/katsoft-llm-kit/src/llm_kit`` and reports each import whose
source component -> target component pair is not in that set.

What counts as an import: eager (module level), local (inside a function or
method), ``TYPE_CHECKING``-only, and dynamic imports whose first argument is a
string literal (``importlib.import_module("llm_kit.x")``, ``__import__``).
Dynamic imports with a computed name cannot be resolved and are not counted.

Rules beyond the edge set:

* the root ``llm_kit/__init__.py`` and ``llm_kit/pipeline/__init__.py`` re-export
  nothing and import no llm_kit module;
* no other top-level module or directory is classified, so any unclassified
  module (or an import of one) is a violation;
* there is no ``_copy`` quarantine: a ``_copy`` directory under the package, a
  module that sits in it or an import of ``llm_kit._copy`` is a violation;
* ``llm_kit.bootstrap_guard`` is CLI-owned startup support: it imports no llm_kit
  module, and only ``cli`` and ``jobs`` import it;
* ``llm_kit._runtime`` is the deployment-mode support module: every component
  may import it, it imports no llm_kit module except ``llm_kit._deployment``,
  and only ``_runtime`` imports ``_deployment`` (the marker the wheel replaces);
* jobs and pipeline.execution never import each other (neither edge is in the
  signed set).

The scan functions take the package root as an argument so the same code is
exercised on the real tree and on the synthetic trees that prove every rule
can fail.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional

import pytest

REPO = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__import__("llm_kit").__file__).resolve().parent
ARCHITECTURE = REPO / "docs" / "reference" / "llm-kit-architecture.md"

ROOT = "<root>"
PIPELINE_ROOT = "<pipeline>"
COPY = "_copy"
GUARD = "bootstrap_guard"
RUNTIME = "_runtime"
DEPLOYMENT = "_deployment"

# The signed graph: A -> {B} is the complete inter-component import set.
ALLOWED: dict[str, frozenset[str]] = {
    "cli": frozenset({
        "schema", "completion", "registry", "adapters", "dispatch", "jobs",
        "pipeline.runtime", "pipeline.execution", "pipeline.analysis",
        "frontdoor", "scaffold",
    }),
    "pipeline.execution": frozenset({
        "execution", "storage", "pipeline.core", "pipeline.artifacts",
        "pipeline.runtime", "adapters",
    }),
    "jobs": frozenset({"completion", "registry", "dispatch", "execution", "storage"}),
    "pipeline.runtime": frozenset({
        "completion", "registry", "dispatch", "storage", "pipeline.core",
        "pipeline.artifacts",
    }),
    "dispatch": frozenset({"completion", "registry", "adapters"}),
    "frontdoor": frozenset({"completion", "registry", "adapters"}),
    "adapters": frozenset({"completion", "registry"}),
    "pipeline.analysis": frozenset({"pipeline.core", "pipeline.artifacts"}),
    "completion": frozenset({"schema"}),
    "execution": frozenset({"schema"}),
    "pipeline.artifacts": frozenset({"pipeline.core"}),
    "schema": frozenset(),
    "registry": frozenset(),
    "storage": frozenset(),
    "pipeline.core": frozenset(),
    "scaffold": frozenset(),
}
COMPONENTS = frozenset(ALLOWED)
TOP_PACKAGES = frozenset(c for c in COMPONENTS if "." not in c)
PIPELINE_PACKAGES = frozenset(c.split(".", 1)[1] for c in COMPONENTS if c.startswith("pipeline."))
GUARD_CONSUMERS = frozenset({"cli", "jobs"})


@dataclass(frozen=True)
class Violation:
    rule: str
    source: str
    target: str
    mode: str
    path: str
    line: int
    detail: str = ""

    def __str__(self) -> str:
        text = f"{self.path}:{self.line}: [{self.rule}] {self.source} -> {self.target} ({self.mode})"
        return f"{text}: {self.detail}" if self.detail else text


@dataclass(frozen=True)
class ImportRef:
    module: str                 # absolute dotted module named by the statement
    names: tuple[str, ...]      # names of a from-import ("" tuple for plain import)
    mode: str                   # eager | local | type_checking | dynamic
    line: int


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

def classify_module(dotted: str) -> Optional[str]:
    """Component of a dotted module name, or None when it is unclassified.

    Returns ROOT / PIPELINE_ROOT for the bare ``llm_kit`` and ``llm_kit.pipeline``.
    """
    parts = dotted.split(".")
    if parts[0] != "llm_kit":
        return None
    if len(parts) == 1:
        return ROOT
    head = parts[1]
    if head == GUARD:
        return GUARD
    if head in (RUNTIME, DEPLOYMENT):
        return head
    if head == "pipeline":
        if len(parts) == 2:
            return PIPELINE_ROOT
        return f"pipeline.{parts[2]}" if parts[2] in PIPELINE_PACKAGES else None
    return head if head in TOP_PACKAGES else None


def classify_file(root: Path, path: Path) -> tuple[Optional[str], str]:
    """(component, dotted module) of a source file under the package root.

    A component is a package: a flat module with a component's name (``schema.py``)
    and any other top-level module are unclassified.
    """
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    is_init = parts[-1] == "__init__"
    if is_init:
        parts = parts[:-1]
    dotted = ".".join(["llm_kit", *parts])
    if not parts:
        return ROOT, dotted
    if parts == ["pipeline"] and is_init:
        return PIPELINE_ROOT, dotted
    if parts[0] == GUARD and len(parts) == 1 and not is_init:
        return GUARD, dotted
    if parts[0] in (RUNTIME, DEPLOYMENT) and len(parts) == 1 and not is_init:
        return parts[0], dotted
    if len(parts) == 1 and not is_init:
        return None, dotted                       # flat top-level module
    if parts[0] == "pipeline":
        if len(parts) == 2 and not is_init:
            return None, dotted                   # flat module inside pipeline/
        return (f"pipeline.{parts[1]}" if parts[1] in PIPELINE_PACKAGES else None), dotted
    return (parts[0] if parts[0] in TOP_PACKAGES else None), dotted


# --------------------------------------------------------------------------
# import extraction
# --------------------------------------------------------------------------

def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def _dynamic_target(node: ast.Call) -> Optional[str]:
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name not in ("import_module", "__import__") or not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


class _Collector(ast.NodeVisitor):
    def __init__(self, package_parts: list[str]) -> None:
        self.package_parts = package_parts          # the module's containing package
        self.refs: list[ImportRef] = []
        self._function_depth = 0
        self._type_checking_depth = 0

    def _mode(self) -> str:
        if self._type_checking_depth:
            return "type_checking"
        return "local" if self._function_depth else "eager"

    def _function(self, node: ast.AST) -> None:
        self._function_depth += 1
        self.generic_visit(node)
        self._function_depth -= 1

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function
    visit_Lambda = _function

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking(node.test):
            self.visit(node.test)
            self._type_checking_depth += 1
            for child in node.body:
                self.visit(child)
            self._type_checking_depth -= 1
            for child in node.orelse:
                self.visit(child)
        else:
            self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.refs.append(ImportRef(alias.name, (), self._mode(), node.lineno))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            base = self.package_parts[: len(self.package_parts) - (node.level - 1)]
            module = ".".join([*base, *(node.module.split(".") if node.module else [])])
        else:
            module = node.module or ""
        self.refs.append(ImportRef(
            module, tuple(a.name for a in node.names), self._mode(), node.lineno))

    def visit_Call(self, node: ast.Call) -> None:
        target = _dynamic_target(node)
        if target is not None:
            self.refs.append(ImportRef(target, (), "dynamic", node.lineno))
        self.generic_visit(node)


def collect_imports(tree: ast.AST, dotted: str, is_init: bool) -> list[ImportRef]:
    parts = dotted.split(".")
    package_parts = parts if is_init else parts[:-1]
    collector = _Collector(package_parts)
    collector.visit(tree)
    return collector.refs


def _targets(ref: ImportRef) -> list[str]:
    """Dotted modules an import statement reaches (names expand under roots)."""
    if ref.names and classify_module(ref.module) in (ROOT, PIPELINE_ROOT):
        return [f"{ref.module}.{n}" if n != "*" else ref.module for n in ref.names]
    return [ref.module]


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------

def check_file(root: Path, path: Path, tree: ast.AST) -> list[Violation]:
    source, dotted = classify_file(root, path)
    rel = path.relative_to(root).as_posix()
    shown = f"llm_kit/{rel}"
    is_init = path.name == "__init__.py"
    found: list[Violation] = []

    def add(rule: str, target: str, ref: Optional[ImportRef], detail: str = "") -> None:
        found.append(Violation(rule, source or f"?{dotted}", target,
                               ref.mode if ref else "file", shown,
                               ref.line if ref else 1, detail))

    if source is None:
        add("unclassified-source", dotted, None,
            "not in a final component or the bootstrap_guard module")

    refs = collect_imports(tree, dotted, is_init)
    if COPY in rel.split("/"):
        add("quarantine-present", dotted, None, "the _copy quarantine is removed; no module may sit in it")

    if source in (ROOT, PIPELINE_ROOT):
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.ImportFrom) and node.module == "__future__" and not node.level:
                    continue
                add("root-reexport", dotted, ImportRef("", (), "eager", node.lineno),
                    "root initializers re-export nothing and import nothing")
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
                    add("root-reexport", dotted, ImportRef("", (), "eager", node.lineno),
                        "__all__ declares a re-export")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__getattr__":
                add("root-reexport", dotted, ImportRef("", (), "eager", node.lineno),
                    "module __getattr__ is a lazy re-export")

    for ref in refs:
        if ref.module.split(".")[0] != "llm_kit":
            continue                                  # stdlib or third party
        if ref.module == f"llm_kit.{COPY}" or ref.module.startswith(f"llm_kit.{COPY}."):
            add("imports-copy", ref.module, ref, "the _copy quarantine is removed; nothing may import it")
            continue
        if source == GUARD:
            add("guard-imports-llm-kit", ref.module, ref, "bootstrap_guard is stdlib-only")
            continue
        for module in _targets(ref):
            target = classify_module(module)
            if source in (RUNTIME, DEPLOYMENT):
                if not (source == RUNTIME and target == DEPLOYMENT):
                    add("runtime-imports-llm-kit", module, ref,
                        "_runtime imports only _deployment; _deployment imports nothing")
                continue
            if target == DEPLOYMENT:
                add("deployment-consumer", module, ref, "only llm_kit._runtime imports llm_kit._deployment")
                continue
            if target == RUNTIME and source not in (None, ROOT, PIPELINE_ROOT):
                continue                              # any component may use the mode seam
            if target is None:
                add("unclassified-target", module, ref, "no such component")
                continue
            if target in (ROOT, PIPELINE_ROOT):
                add("root-import", module, ref, "import a component, not the root")
                continue
            if source is None or source in (ROOT, PIPELINE_ROOT):
                continue                              # already reported above
            if target == source:
                continue                              # same-component imports are unrestricted
            if target == GUARD:
                if source not in GUARD_CONSUMERS:
                    add("guard-consumer", target, ref,
                        "llm_kit.bootstrap_guard is imported only by cli and jobs")
                continue
            if target not in ALLOWED[source]:
                add("edge", target, ref,
                    "jobs and pipeline.execution never import each other"
                    if {source, target} == {"jobs", "pipeline.execution"}
                    else "edge is not in the signed dependency graph")
    return found


def scan(root: Path) -> list[Violation]:
    """Every violation under the package root, ordered by path and line."""
    found: list[Violation] = []
    if (root / COPY).exists():
        found.append(Violation("quarantine-present", COPY, COPY, "file", f"llm_kit/{COPY}", 1,
                               "the _copy quarantine directory must not exist"))
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found.extend(check_file(root, path, tree))
    return sorted(found, key=lambda v: (v.path, v.line, v.rule, v.target))


def report(violations: Iterable[Violation]) -> str:
    return "\n".join(str(v) for v in violations)


# --------------------------------------------------------------------------
# C2: the real tree
# --------------------------------------------------------------------------

def test_package_root_exists():
    assert (PACKAGE_ROOT / "__init__.py").is_file(), PACKAGE_ROOT
    # the vacuity witness: the scan reads the whole final tree
    assert len(list(PACKAGE_ROOT.rglob("*.py"))) >= 150


def test_the_quarantined_copy_is_gone():
    assert not (PACKAGE_ROOT / COPY).exists()


def test_tree_obeys_the_signed_import_graph():
    violations = scan(PACKAGE_ROOT)
    assert not violations, "import-direction violations:\n" + report(violations)


def test_bootstrap_guard_consumers_are_only_cli_and_jobs():
    refs = [
        v for v in scan(PACKAGE_ROOT) if v.rule == "guard-consumer"
    ]
    assert not refs, report(refs)


# --------------------------------------------------------------------------
# the graph constant against its sources
# --------------------------------------------------------------------------

def _documented_graph() -> dict[str, frozenset[str]]:
    text = ARCHITECTURE.read_text(encoding="utf-8")
    section = text.split("## Exact dependency graph", 1)[1]
    block = section.split("~~~text", 1)[1].split("~~~", 1)[0]
    graph = {}
    for match in re.finditer(r"([a-z_.]+)\s*->\s*\{(.*?)\}", block, re.DOTALL):
        graph[match.group(1)] = frozenset(t.strip() for t in match.group(2).split(",") if t.strip())
    return graph


def test_encoded_graph_equals_the_architecture_document():
    assert _documented_graph() == ALLOWED


def test_there_are_sixteen_components():
    assert len(COMPONENTS) == 16
    for targets in ALLOWED.values():
        assert targets <= COMPONENTS


def test_graph_is_acyclic():
    state: dict[str, int] = {}

    def visit(node: str, trail: tuple[str, ...]) -> None:
        assert state.get(node) != 1, "cycle: " + " -> ".join((*trail, node))
        if state.get(node) == 2:
            return
        state[node] = 1
        for nxt in sorted(ALLOWED[node]):
            visit(nxt, (*trail, node))
        state[node] = 2

    for component in sorted(COMPONENTS):
        visit(component, ())


def test_jobs_and_pipeline_execution_do_not_cross_import():
    assert "pipeline.execution" not in ALLOWED["jobs"]
    assert "jobs" not in ALLOWED["pipeline.execution"]


def test_no_component_may_reach_the_cli_or_root():
    assert not any("cli" in targets for targets in ALLOWED.values())
    assert not any(ROOT in targets or PIPELINE_ROOT in targets for targets in ALLOWED.values())


def test_runtime_support_modules_are_classified_and_confined():
    """The real tree: _runtime/_deployment exist, import nothing they should not,
    and the mode seam is imported by components without breaking the graph."""
    assert (PACKAGE_ROOT / "_runtime.py").is_file() and (PACKAGE_ROOT / "_deployment.py").is_file()
    violations = [v for v in scan(PACKAGE_ROOT)
                  if v.rule in ("runtime-imports-llm-kit", "deployment-consumer", "unclassified-source")]
    assert not violations, report(violations)


# --------------------------------------------------------------------------
# every rule can fail: synthetic trees
# --------------------------------------------------------------------------

def _tree(tmp_path: Path, files: Mapping[str, str]) -> Path:
    root = tmp_path / "llm_kit"
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _rules(violations: list[Violation]) -> list[tuple[str, str, str]]:
    return [(v.rule, v.source + "->" + v.target, v.mode) for v in violations]


def test_a_clean_synthetic_tree_passes(tmp_path):
    root = _tree(tmp_path, {
        "schema/__init__.py": "",
        "completion/__init__.py": "from llm_kit.schema import x\n",
        "adapters/__init__.py": "from llm_kit.completion import y\nfrom llm_kit import registry\n",
        "registry/__init__.py": "",
        "adapters/inner.py": "from . import sibling\nfrom .sibling import z\n",
        "adapters/sibling.py": "import llm_kit.adapters.inner\n",
        "pipeline/__init__.py": "",
        "pipeline/core/__init__.py": "",
        "pipeline/runtime/__init__.py": "from llm_kit.pipeline.core import a\nfrom llm_kit.pipeline import artifacts\n",
        "pipeline/artifacts/__init__.py": "from llm_kit.pipeline import core\n",
        "cli/__init__.py": "from llm_kit.bootstrap_guard import reexec\nfrom llm_kit.jobs import j\n",
        "jobs/__init__.py": "import llm_kit.bootstrap_guard\n",
        "bootstrap_guard.py": "import os\n",
    })
    assert scan(root) == []


@pytest.mark.parametrize("body, mode", [
    ("from llm_kit.adapters import thing\n", "eager"),
    ("import llm_kit.adapters.thing\n", "eager"),
    ("from llm_kit import adapters\n", "eager"),
    ("from .. import adapters\n", "eager"),
    ("from ..adapters import thing\n", "eager"),
    ("def f():\n    from llm_kit.adapters import thing\n", "local"),
    ("class C:\n    def m(self):\n        import llm_kit.adapters\n", "local"),
    ("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from llm_kit.adapters import T\n",
     "type_checking"),
    ("import typing\nif typing.TYPE_CHECKING:\n    from llm_kit.adapters import T\n", "type_checking"),
    ("import importlib\nx = importlib.import_module('llm_kit.adapters.thing')\n", "dynamic"),
    ("x = __import__('llm_kit.adapters')\n", "dynamic"),
    ("def f():\n    import importlib\n    return importlib.import_module('llm_kit.adapters')\n", "dynamic"),
])
def test_completion_to_adapters_is_caught_in_every_import_form(tmp_path, body, mode):
    root = _tree(tmp_path, {
        "completion/__init__.py": "",
        "completion/mod.py": body,
        "adapters/__init__.py": "",
    })
    found = scan(root)
    assert [(v.rule, v.source, v.target, v.mode) for v in found] == [
        ("edge", "completion", "adapters", mode)], report(found)


def test_non_literal_dynamic_imports_are_not_resolvable_and_not_counted(tmp_path):
    root = _tree(tmp_path, {
        "completion/mod.py": "import importlib\nimportlib.import_module(NAME)\n",
    })
    assert scan(root) == []


def test_else_branch_of_type_checking_is_an_ordinary_import(tmp_path):
    root = _tree(tmp_path, {
        "completion/mod.py": (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    pass\n"
            "else:\n    from llm_kit.adapters import T\n"),
    })
    assert [v.mode for v in scan(root)] == ["eager"]


def test_unclassified_root_module_import_is_caught(tmp_path):
    root = _tree(tmp_path, {
        "completion/__init__.py": "from llm_kit.stray import x\nfrom llm_kit import other\n",
        "stray.py": "",
    })
    found = scan(root)
    assert {(v.rule, v.target) for v in found} >= {
        ("unclassified-target", "llm_kit.stray"),
        ("unclassified-target", "llm_kit.other"),
        ("unclassified-source", "llm_kit.stray"),
    }, report(found)


def test_unclassified_module_importing_a_component_is_caught(tmp_path):
    root = _tree(tmp_path, {"stray.py": "from llm_kit.schema import x\n", "schema/__init__.py": ""})
    assert [v.rule for v in scan(root)] == ["unclassified-source"]


def test_a_flat_module_named_like_a_component_is_unclassified(tmp_path):
    root = _tree(tmp_path, {"schema.py": "", "pipeline/core.py": ""})
    assert sorted(v.rule for v in scan(root)) == ["unclassified-source"] * 2


def test_unknown_pipeline_subpackage_is_unclassified(tmp_path):
    root = _tree(tmp_path, {"pipeline/__init__.py": "", "pipeline/extra/__init__.py": ""})
    assert [v.rule for v in scan(root)] == ["unclassified-source"]


@pytest.mark.parametrize("name, body", [
    ("__init__.py", "from llm_kit.schema import x\n"),
    ("__init__.py", "from .schema import x\n"),
    ("__init__.py", "import os\n"),
    ("__init__.py", "__all__ = []\n"),
    ("__init__.py", "def __getattr__(name):\n    return 1\n"),
    ("pipeline/__init__.py", "from llm_kit.pipeline.core import x\n"),
    ("pipeline/__init__.py", "__all__: list[str] = []\n"),
])
def test_root_initializers_that_re_export_are_caught(tmp_path, name, body):
    root = _tree(tmp_path, {"schema/__init__.py": "", "pipeline/__init__.py": "",
                            "pipeline/core/__init__.py": ""})
    (root / name).write_text(body, encoding="utf-8")
    assert "root-reexport" in {v.rule for v in scan(root)}


def test_root_initializer_may_carry_a_docstring_and_future_import(tmp_path):
    root = _tree(tmp_path, {})
    (root / "__init__.py").write_text('"""Doc."""\nfrom __future__ import annotations\n', encoding="utf-8")
    assert scan(root) == []


def test_importing_the_root_package_itself_is_caught(tmp_path):
    root = _tree(tmp_path, {"completion/mod.py": "import llm_kit\nfrom llm_kit import *\n"})
    assert [v.rule for v in scan(root)] == ["root-import", "root-import"]


def test_final_component_importing_copy_is_caught(tmp_path):
    root = _tree(tmp_path, {"schema/mod.py": "from llm_kit._copy.job_kit import x\nimport llm_kit._copy\n"})
    found = scan(root)
    assert [(v.rule, v.source) for v in found] == [("imports-copy", "schema")] * 2, report(found)


def test_a_copy_directory_and_the_modules_in_it_are_caught(tmp_path):
    root = _tree(tmp_path, {
        "_copy/__init__.py": "",
        "_copy/mod.py": "from llm_kit.schema.json_repair import x\n",
        "schema/__init__.py": "",
    })
    rules = sorted({v.rule for v in scan(root)})
    assert rules == ["quarantine-present", "unclassified-source"], rules
    assert any(v.path == "llm_kit/_copy" for v in scan(root))












def test_jobs_and_pipeline_execution_cross_imports_are_caught(tmp_path):
    root = _tree(tmp_path, {
        "jobs/mod.py": "from llm_kit.pipeline.execution import x\n",
        "pipeline/__init__.py": "",
        "pipeline/execution/mod.py": "from llm_kit.jobs import y\n",
    })
    found = scan(root)
    assert {(v.source, v.target, v.detail) for v in found} == {
        ("jobs", "pipeline.execution", "jobs and pipeline.execution never import each other"),
        ("pipeline.execution", "jobs", "jobs and pipeline.execution never import each other"),
    }


def test_same_component_imports_are_unrestricted(tmp_path):
    root = _tree(tmp_path, {
        "pipeline/__init__.py": "",
        "pipeline/core/a.py": "from llm_kit.pipeline.core import b\nfrom .b import c\n",
        "completion/a.py": "import llm_kit.completion.b\n",
    })
    assert scan(root) == []


@pytest.mark.parametrize("consumer", ["adapters", "dispatch", "pipeline/runtime", "schema"])
def test_bootstrap_guard_consumer_outside_cli_and_jobs_is_caught(tmp_path, consumer):
    files = {f"{consumer}/mod.py": "from llm_kit.bootstrap_guard import g\n",
             "bootstrap_guard.py": "", "pipeline/__init__.py": ""}
    root = _tree(tmp_path, files)
    assert [v.rule for v in scan(root)] == ["guard-consumer"]


def test_bootstrap_guard_importing_llm_kit_is_caught(tmp_path):
    root = _tree(tmp_path, {"bootstrap_guard.py": "from llm_kit.schema import x\n",
                            "schema/__init__.py": ""})
    assert [v.rule for v in scan(root)] == ["guard-imports-llm-kit"]





# --------------------------------------------------------------------------
# the deployment-mode support modules
# --------------------------------------------------------------------------

def test_any_component_may_import_the_runtime_seam(tmp_path):
    root = _tree(tmp_path, {
        "_runtime.py": "from llm_kit import _deployment\nimport os\n",
        "_deployment.py": 'MODE = "legacy"\n',
        "registry/__init__.py": "from llm_kit import _runtime\n",
        "jobs/__init__.py": "def f():\n    from llm_kit._runtime import mode\n",
        "execution/__init__.py": "import llm_kit._runtime\n",
    })
    assert scan(root) == []


@pytest.mark.parametrize("files, rule", [
    ({"_runtime.py": "from llm_kit.registry import x\n", "registry/__init__.py": ""},
     "runtime-imports-llm-kit"),
    ({"_runtime.py": "from llm_kit import bootstrap_guard\n", "bootstrap_guard.py": ""},
     "runtime-imports-llm-kit"),
    ({"_deployment.py": "from llm_kit import _runtime\n", "_runtime.py": ""},
     "runtime-imports-llm-kit"),
    ({"registry/__init__.py": "from llm_kit import _deployment\n", "_deployment.py": ""},
     "deployment-consumer"),
    ({"cli/__init__.py": "import llm_kit._deployment\n", "_deployment.py": ""},
     "deployment-consumer"),
])
def test_runtime_support_module_rules_can_fail(tmp_path, files, rule):
    found = scan(_tree(tmp_path, files))
    assert rule in {v.rule for v in found}, report(found)
