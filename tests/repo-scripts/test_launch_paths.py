"""Instrument: every launch path in ``plugins/`` is redirect-aware or reaches a guarded entry.

A claudx session (``scripts/claude_plugin_test.py``) loads the working copy through
``--plugin-dir`` and redirects bootstrap's data root with ``CLAUDE_BOOTSTRAP_DATA_ROOT``. A
launch path that names the PRODUCTION data root (``~/.claude/plugins/data``) runs the
published release's interpreter, or reads the published release's bootstrap records, inside a
session meant to test the working copy. This file scans every shipped text file under
``plugins/`` (tracked, or untracked and not ignored, so a file is seen before it is
committed; tests and ``.venv`` excluded) for each way a launch path is spelled, and fails on
one that is neither redirect-aware nor guarded.

FORMS. Each is found in the real tree (the SPLIT production literal is the one exception:
the tree holds none, and fixtures cover it), and each has a mutation test below.

* P, a production literal that reaches ``.venv`` or a bootstrap record (``bootstrap.log``,
  ``deferred_requirements.json``). Three spellings: SINGLE (one physical line), SPLIT (a
  ``\\`` or ``^`` continuation, or a Python bracket or operator continuation, joins the
  lines) and TWO-STEP (a name assigned a production base in one statement and composed with a
  ``.venv`` piece in a later one; dataflow through shell and cmd assignments). A P site is an
  offender unless a launcher keeps it behind a redirect branch (form B) or an ``EXEMPT``
  entry names its line. A guarded target does not excuse a P literal: the launch path must
  work when the production interpreter is absent.
* R, the redirect-aware spelling: ``${CLAUDE_BOOTSTRAP_DATA_ROOT:-<base>}`` in text and
  code, or a launcher variable that holds ``CLAUDE_BOOTSTRAP_DATA_ROOT``. The mutation
  rewrites an expression back to its bare base, or deletes a launcher's redirect branch.
* S, a launch-to-target: an interpreter token followed by a script or a ``-m`` module, or a
  venv console script. Interpreters: ``${BOOTSTRAP_PYTHON...}``, the redirect-aware and
  production venv interpreters, and the placeholders ``<plugin-venv-python>``,
  ``<workflow-kit-venv-python>``, ``<venvPython>``, ``${venvPython}``, ``${venvPy}``,
  ``${refs.venvPython}``, ``${inputs.workflowKitVenvPython}``,
  ``${args.workflowKitVenvPython}`` and ``{{ inputs.workflowKitVenvPython }}``. Targets
  resolve through ``${CLAUDE_SKILL_DIR}``, ``${CLAUDE_PLUGIN_ROOT}``, ``${<PLUGIN>_ROOT...}``,
  ``<pluginRoot>``, ``<root>``, ``${inputs.pluginRoot}``, ``${args.pluginRoot}``,
  ``{{ inputs.pluginRoot }}``, ``$SCRIPT_DIR``, a path relative to a ``cd``'d root, or a
  unique name across plugins. Python string constants are scanned too, so the launch
  commands a compiler emits count. An unresolvable target is an offender.
  The target must be GUARDED (it calls ``reexec_under_plugin_venv`` before it imports
  anything outside the standard library and its plugin) or CLEAN (it imports nothing of the
  kind). What counts as an import: every import in the launched script itself, lazy ones
  included, plus what the plugin modules it loads import at import time. An import inside a
  ``try`` whose handler only records the absence (``yaml = None``) is optional and skipped;
  a handler that raises, exits, prints, returns or runs a fallback makes the import required.
* D, a placeholder definition: a paragraph that names a venv-interpreter placeholder AND a
  venv path holds no P literal.
* C, a console script: each ``[project.scripts]`` function calls the guard on its
  ``argv is None`` path (the wrapper's path). A console script a plugin's venv gets from
  a first-party package the plugin requires (``katsoft-*`` under ``packages/``) is that
  package's own entry point: the venv holds the released wheel, so no stale plugin copy
  can run and the launch needs no guard.
* B, a launcher: a shell or cmd file that composes a production venv path needs a
  ``CLAUDE_BOOTSTRAP_DATA_ROOT`` branch that overrides the interpreter and REFUSES (exit 2) a
  missing redirected interpreter. A ``plugins/*/bin/*`` shim also refuses to run as the
  cached copy under ``CLAUDE_PLUGIN_TEST``, and every ``.py`` a launcher names must be
  guarded or clean.

EXEMPT lists, with a reason each, what stays on production paths on purpose (user state, the
human CLI contract, maintainer prose, bootstrap's own records, diagnostics a person runs
against the real machine) and the launches the scan cannot resolve to a plugin script. An
entry is scoped to a line (path glob plus an anchor in the line) and must still suppress a
live site; one that suppresses nothing fails. An entry with status ``open`` records a REAL
unguarded launch path that the scan found and this file does not fix; it must still be
flagged once its exemption is removed, so a fixed defect fails and asks for the entry's
removal. Open entries are also raised as warnings in the test report.

NOT COVERED. Comments in Python files (only string constants are read as text); UE-side
scripts that run inside the editor; a launch whose interpreter is a bare shell variable that
the same file does not assign; the interpreter chains inside bootstrap's own hook and engine
(pinned by ``test_forced_interpreter_order.py``, and bootstrap cannot re-exec itself).

REVERT-TO-RED. The tests mutate a copy of the tree in memory and require the verdict to flag
the mutated site: restore a production literal, strip a guard call (or give a clean target a
foreign import), delete a launcher's redirect branch or its refusal, delete a shim's
cached-copy refusal, inject a production path into a placeholder definition, add a second
literal beside an exemption.
"""

from __future__ import annotations

import ast
import fnmatch
import functools
import re
import subprocess
import sys
import tomllib
import warnings
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Iterator, NamedTuple, Optional

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_STDLIB = frozenset(sys.stdlib_module_names)

_TEXT_SUFFIXES = frozenset(
    {".md", ".yaml", ".yml", ".json", ".js", ".py", ".sh", ".cmd", ".bat", ".ps1", ".toml", ".html", ".txt"}
)
_SKIP_PARTS = frozenset({".venv", "node_modules", "__pycache__", ".git"})
_MAX_BYTES = 2_000_000
_MAX_JOIN = 8

GUARD_NAME = "reexec_under_plugin_venv"
REDIRECT_VAR = "CLAUDE_BOOTSTRAP_DATA_ROOT"


# --------------------------------------------------------------------------
# The tree under scan: real files, or a copy with in-memory edits
# --------------------------------------------------------------------------


class Tree:
    """Path -> text for the shipped text files, with copy-on-edit mutation."""

    def __init__(self, files: dict[str, str]):
        self.files = files

    @classmethod
    def load(cls) -> "Tree":
        listed = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "ls-files", "--cached", "--others",
             "--exclude-standard", "--", "plugins"],
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        files: dict[str, str] = {}
        for rel in listed:
            parts = rel.split("/")
            if _SKIP_PARTS & set(parts) or "tests" in parts[:-1] or parts[-1] == "uv.lock":
                continue
            full = _REPO_ROOT / rel
            if not full.is_file():
                continue
            if full.suffix and full.suffix.lower() not in _TEXT_SUFFIXES:
                continue
            data = full.read_bytes()
            if len(data) > _MAX_BYTES or b"\x00" in data:
                continue
            files[rel] = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
        return cls(files)

    def text(self, path: str) -> str:
        return self.files[path]

    def has(self, path: str) -> bool:
        return path in self.files

    def with_text(self, path: str, text: str) -> "Tree":
        assert path in self.files, f"cannot edit {path}: not in the tree"
        files = dict(self.files)
        files[path] = text
        return Tree(files)

    def replaced(self, path: str, old: str, new: str, *, count: int = 1) -> "Tree":
        """A copy with ``old`` replaced; the edit must apply (a no-op mutation is a bug)."""
        text = self.text(path)
        assert old in text, f"mutation anchor {old!r} is not in {path}"
        return self.with_text(path, text.replace(old, new, count))

    def paths(self) -> list[str]:
        return sorted(self.files)


@functools.lru_cache(maxsize=1)
def real_tree() -> Tree:
    tree = Tree.load()
    assert tree.files, "git ls-files found no shipped text files under plugins/"
    return tree


def plugin_of(path: str) -> Optional[str]:
    parts = path.split("/")
    return parts[1] if len(parts) > 2 and parts[0] == "plugins" else None


def plugin_names(tree: Tree) -> set[str]:
    return {name for name in (plugin_of(p) for p in tree.files) if name}


def root_variable_plugins(tree: Tree) -> dict[str, str]:
    """``GIT_KIT_ROOT`` -> ``git-kit``: the name bootstrap exports for each plugin."""
    return {name.upper().replace("-", "_") + "_ROOT": name for name in plugin_names(tree)}


def skill_dir_of(tree: Tree, path: str) -> Optional[str]:
    """Nearest ancestor directory of ``path`` that holds a SKILL.md."""
    parts = path.split("/")[:-1]
    while parts:
        candidate = "/".join(parts)
        if f"{candidate}/SKILL.md" in tree.files:
            return candidate
        parts.pop()
    return None


def dialect_of(path: str) -> Optional[str]:
    p = PurePosixPath(path)
    suffix = p.suffix.lower()
    if suffix in {".cmd", ".bat"}:
        return "cmd"
    if suffix == ".sh" or (not suffix and "bin" in p.parts[:-1]):
        return "sh"
    return None


# --------------------------------------------------------------------------
# Logical lines: physical lines joined across the spellings a command is split
# --------------------------------------------------------------------------


class LLine(NamedTuple):
    no: int
    nphys: int
    text: str


_SH_CONT = re.compile(r"\s\\{1,2}\s*$")


def _continues(suffix: str, cur: str, nxt: str) -> bool:
    if suffix in {".cmd", ".bat"}:
        return cur.rstrip().endswith("^")
    if suffix == ".py":
        end = cur.rstrip()
        if end.lstrip().startswith("#"):
            return False
        return end.endswith(("(", ",", "/", "+", "\\")) or nxt.lstrip().startswith(("/ ", "+ "))
    return bool(_SH_CONT.search(cur)) or cur.rstrip().endswith(" \\")


def _join(suffix: str, cur: str, nxt: str) -> str:
    if suffix in {".cmd", ".bat"}:
        return cur.rstrip()[:-1].rstrip() + " " + nxt.strip()
    if suffix == ".py":
        return cur.rstrip().rstrip("\\") + " " + nxt.strip()
    return re.sub(r"\s*\\{1,2}\s*$", " ", cur) + nxt.strip()


def logical_lines(path: str, text: str, *, first_no: int = 1) -> list[LLine]:
    suffix = PurePosixPath(path).suffix.lower()
    lines = text.split("\n")
    out: list[LLine] = []
    i = 0
    while i < len(lines):
        cur = lines[i]
        n = 1
        while i + n < len(lines) and n < _MAX_JOIN and _continues(suffix, cur, lines[i + n]):
            cur = _join(suffix, cur, lines[i + n])
            n += 1
        out.append(LLine(first_no + i, n, cur))
        i += n
    return out


def scan_view(path: str, text: str) -> str:
    """The text a scan reads: a JSON file's escaped quotes are real quotes."""
    return text.replace('\\"', '"') if path.endswith(".json") else text


# --------------------------------------------------------------------------
# P / R: production and redirect-aware data bases that reach a venv or a record
# --------------------------------------------------------------------------

_R_EXPR = re.compile(r"\$\{CLAUDE_BOOTSTRAP_DATA_ROOT:-[^}]*\}")
_R_MARK = "\x00"
_R_ANY = re.compile(r"CLAUDE_BOOTSTRAP_DATA_ROOT")
_PROD = re.compile(
    r"""(?:~|\$\{?HOME\}?|%USERPROFILE%|\$USERPROFILE)[/\\]+\.claude[/\\]+plugins[/\\]+data(?![\w-])
    |Path\.home\(\)\s*/\s*["']\.claude["']\s*/\s*["']plugins["']\s*/\s*["']data["']
    |expanduser\(\s*["']~/\.claude/plugins/data(?![\w-])""",
    re.X,
)
_REACH = re.compile(r"\.venv(?![\w-])|bootstrap\.log|deferred_requirements\.json")
_REACH_VENV = re.compile(r"\.venv(?![\w-])")


@dataclass(frozen=True)
class PRSite:
    kind: str       # "P" or "R"
    shape: str      # "single" | "split" | "two-step"
    variant: str    # R: "expr" | "var"; P: "literal" | "var"
    path: str
    line: int
    nphys: int
    text: str


def _pr_line_sites(path: str, ll: LLine) -> list[PRSite]:
    stripped = _R_EXPR.sub(_R_MARK, ll.text)
    shape = "split" if ll.nphys > 1 else "single"
    text = ll.text.strip()
    out: list[PRSite] = []
    for m in _PROD.finditer(stripped):
        if _REACH.search(stripped, m.end()):
            out.append(PRSite("P", shape, "literal", path, ll.no, ll.nphys, text))
            break
    marks = [m.end() for m in re.finditer(_R_MARK, stripped)]
    if marks and any(_REACH.search(stripped, pos) for pos in marks):
        out.append(PRSite("R", shape, "expr", path, ll.no, ll.nphys, text))
    return out


_ASSIGN = {
    # `NAME=value`, optionally after a `case` label (`MINGW*) NAME=value`) or `export`/`local`/`readonly`
    "sh": re.compile(
        r"^\s*(?:[^\s=()]+\)\s*)?(?:(?:export|local|readonly|declare(?:\s+-\w+)?)\s+)*"
        r"(?P<name>[A-Za-z_]\w*)=(?P<value>.*)$"
    ),
    # `set "NAME=value"`, also after an `if ...` prefix on the same line
    "cmd": re.compile(r'(?:^|\s)set\s+"?(?P<name>[A-Za-z_]\w*)=(?P<value>.*?)"?\s*$', re.I),
}
_REFS = {
    "sh": re.compile(r"\$\{([A-Za-z_]\w*)|\$([A-Za-z_]\w*)"),
    "cmd": re.compile(r"%([A-Za-z_]\w*)(?::[^%]*)?%|!([A-Za-z_]\w*)(?::[^!]*)?!"),
}


def _refs(dialect: str, value: str) -> set[str]:
    found = set()
    for m in _REFS[dialect].finditer(value):
        name = m.group(1) or m.group(2)
        found.add(name.upper() if dialect == "cmd" else name)
    return found


def _norm_name(dialect: str, name: str) -> str:
    return name.upper() if dialect == "cmd" else name


def _two_step_sites(path: str, lls: list[LLine], dialect: str) -> list[PRSite]:
    """Dataflow over assignments: a production base composed with a venv piece in a later statement.

    A name is P-only when some assignment gives it a production base (directly or
    through another P-only name) and none gives it the redirect variable. An
    assignment that mentions a P-only name and a venv piece (a literal ``.venv`` or
    a name that holds one) is a P site; one that mentions a redirect-holding name and
    a venv piece is an R site; a later use of the name is not a site. Control flow is
    ignored on purpose: a launcher that
    assigns the production interpreter first and overrides it in a redirect
    branch has both, and the launcher check (form B) judges the pair.
    """
    assigns = []
    for ll in lls:
        m = _ASSIGN[dialect].search(ll.text)
        if m:
            assigns.append((ll, _norm_name(dialect, m.group("name")), m.group("value")))

    def has_prod(value: str) -> bool:
        return bool(_PROD.search(_R_EXPR.sub(_R_MARK, value)))

    def redirects(value: str) -> bool:
        return bool(_R_ANY.search(value))

    p_names: set[str] = set()
    r_names: set[str] = {REDIRECT_VAR}
    v_names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for _ll, name, value in assigns:
            refs = _refs(dialect, value)
            if (has_prod(value) or (refs & p_names)) and not (refs & r_names) and name not in p_names:
                p_names.add(name)
                changed = True
            if (redirects(value) or (refs & r_names)) and name not in r_names:
                r_names.add(name)
                changed = True
            if (_REACH_VENV.search(value) or (refs & v_names)) and name not in v_names:
                v_names.add(name)
                changed = True
    p_only = p_names - r_names

    out: list[PRSite] = []
    for ll, _name, value in assigns:
        refs = _refs(dialect, value)
        if not (_REACH_VENV.search(value) or (refs & v_names)):
            continue
        text = ll.text.strip()
        if (refs & p_only) and not has_prod(value) and not (refs & r_names):
            out.append(PRSite("P", "two-step", "var", path, ll.no, ll.nphys, text))
        elif refs & r_names:
            out.append(PRSite("R", "two-step", "var", path, ll.no, ll.nphys, text))
    return out


def pr_sites(tree: Tree, path: str) -> list[PRSite]:
    text = scan_view(path, tree.text(path))
    lls = logical_lines(path, text)
    out: list[PRSite] = []
    for ll in lls:
        if "plugins" in ll.text or "CLAUDE_BOOTSTRAP_DATA_ROOT" in ll.text:
            out.extend(_pr_line_sites(path, ll))
    dialect = dialect_of(path)
    if dialect:
        seen = {(s.kind, s.line) for s in out}
        for site in _two_step_sites(path, lls, dialect):
            if (site.kind, site.line) not in seen:
                out.append(site)
    return out


# --------------------------------------------------------------------------
# D: placeholder definitions
# --------------------------------------------------------------------------

_D_NAMES = re.compile(
    r"<workflow-kit-venv-python>|<plugin-venv-python>|<venvPython>|workflowKitVenvPython|\bvenvPy\b|\bvenvPython\b"
)
_COMMENT_PREFIX = re.compile(r"^\s*(?://+|#+|\*+|>)?\s*")


@dataclass(frozen=True)
class DBlock:
    path: str
    first: int
    last: int
    has_p: bool


def definition_blocks(tree: Tree, path: str) -> list[DBlock]:
    """Paragraphs that name a venv-interpreter placeholder and hold a venv path."""
    lines = scan_view(path, tree.text(path)).split("\n")
    blocks: list[DBlock] = []
    start = None
    for idx in range(len(lines) + 1):
        core = _COMMENT_PREFIX.sub("", lines[idx]) if idx < len(lines) else ""
        if core.strip() and idx < len(lines):
            if start is None:
                start = idx
            continue
        if start is not None:
            body = "\n".join(lines[start:idx])
            if _D_NAMES.search(body) and _REACH_VENV.search(body):
                stripped = _R_EXPR.sub(_R_MARK, body)
                has_p = any(_REACH.search(stripped, m.end()) for m in _PROD.finditer(stripped))
                blocks.append(DBlock(path, start + 1, idx, has_p))
            start = None
    return blocks


# --------------------------------------------------------------------------
# Module analysis: the guard, and what a script imports
# --------------------------------------------------------------------------


class Imp(NamedTuple):
    line: int
    module: str      # dotted, "" for ``from . import x``
    level: int
    names: tuple[str, ...]
    import_time: bool
    optional: bool   # inside a try whose handler tolerates a failed import


class FuncGuard(NamedTuple):
    qualname: str
    line: int
    under_argv_none: bool


class ModInfo(NamedTuple):
    imports: tuple[Imp, ...]
    guards: tuple[tuple[int, bool], ...]          # (line, executes at import time)
    func_guards: tuple[FuncGuard, ...]
    functions: frozenset[str]


_IMPORT_TOLERANT = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}


def _recovers_silently(handler: ast.ExceptHandler) -> bool:
    """True when the handler only records the absence (``yaml = None``, ``HAVE_YAML = False``, ``pass``)."""
    return all(
        isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.Pass))
        or (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
        for stmt in handler.body
    )


def _tolerates_import_failure(handlers: list[ast.ExceptHandler]) -> bool:
    """True when a failed import in the ``try`` body is absorbed and the script carries on unchanged.

    Only a handler that records the absence and does nothing else makes the import optional. A
    handler that raises, exits, returns, prints, calls ``require_bootstrap`` or runs a fallback
    makes the script's behaviour depend on the import, so the import is REQUIRED.
    """
    for handler in handlers:
        if handler.type is None:
            caught = True
        else:
            names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
            caught = any(isinstance(n, ast.Name) and n.id in _IMPORT_TOLERANT for n in names)
        if caught and _recovers_silently(handler):
            return True
    return False


class _ModVisitor(ast.NodeVisitor):
    """Imports (with where they run) and guard calls (with the function they sit in)."""

    def __init__(self) -> None:
        self.imports: list[Imp] = []
        self.guards: list[tuple[int, bool]] = []
        self.func_guards: list[FuncGuard] = []
        self.functions: set[str] = set()
        self._names: list[str] = []     # enclosing function and class names
        self._defs = 0                  # enclosing function definitions
        self._optional = 0
        self._argv_none = 0

    def _def(self, node) -> None:
        self._names.append(node.name)
        self.functions.add(".".join(self._names))
        self._defs += 1
        self.generic_visit(node)
        self._defs -= 1
        self._names.pop()

    visit_FunctionDef = _def
    visit_AsyncFunctionDef = _def

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._names.append(node.name)
        self.generic_visit(node)
        self._names.pop()

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._defs += 1
        self.generic_visit(node)
        self._defs -= 1

    def visit_Try(self, node: ast.Try) -> None:
        tolerant = _tolerates_import_failure(node.handlers)
        self._optional += 1 if tolerant else 0
        for stmt in node.body:
            self.visit(stmt)
        self._optional -= 1 if tolerant else 0
        for part in (node.handlers, node.orelse, node.finalbody):
            for stmt in part:
                self.visit(stmt)

    def visit_If(self, node: ast.If) -> None:
        test = ast.unparse(node.test)
        if "TYPE_CHECKING" in test:
            for stmt in node.orelse:
                self.visit(stmt)
            return
        argv_none = bool(re.search(r"\bargv\s+is\s+None\b", test)) or bool(re.search(r"\bnot\s+argv\b", test))
        self.visit(node.test)
        self._argv_none += 1 if argv_none else 0
        for stmt in node.body:
            self.visit(stmt)
        self._argv_none -= 1 if argv_none else 0
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(Imp(node.lineno, alias.name, 0, (), self._defs == 0, self._optional > 0))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.imports.append(
            Imp(node.lineno, node.module or "", node.level, tuple(a.name for a in node.names),
                self._defs == 0, self._optional > 0)
        )

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name == GUARD_NAME:
            self.guards.append((node.lineno, self._defs == 0))
            if self._names:
                self.func_guards.append(FuncGuard(".".join(self._names), node.lineno, self._argv_none > 0))
        self.generic_visit(node)


@functools.lru_cache(maxsize=None)
def mod_info(path: str, text: str) -> ModInfo:
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:  # fail loudly: a launched script must parse
        raise AssertionError(f"{path} does not parse as Python 3: {exc}") from exc
    visitor = _ModVisitor()
    visitor.visit(tree)
    return ModInfo(tuple(visitor.imports), tuple(visitor.guards), tuple(visitor.func_guards),
                   frozenset(visitor.functions))


def _plugin_root(path: str) -> Optional[str]:
    name = plugin_of(path)
    return f"plugins/{name}" if name else None


def _local_roots(path: str) -> list[str]:
    """Directories a script's imports resolve against: its ancestors up to the plugin root, plus lib/ and scripts/.

    Scripts put their own directory, the plugin root, ``lib/`` or the vendored guard's
    ``scripts/`` directory on ``sys.path`` before importing; the ancestors, ``lib/`` and
    ``scripts/`` of the plugin are the places those inserts point at.
    """
    root = _plugin_root(path)
    if not root:
        return []
    roots = []
    cur = str(PurePosixPath(path).parent)
    while True:
        roots.append(cur)
        if cur == root or cur in {"", "."}:
            break
        cur = str(PurePosixPath(cur).parent)
    for extra in (f"{root}/lib", f"{root}/scripts"):
        if extra not in roots:
            roots.append(extra)
    return roots


def _module_candidates(base: str, dotted: str) -> list[str]:
    rel = dotted.replace(".", "/")
    stem = f"{base}/{rel}" if rel else base
    return [f"{stem}.py", f"{stem}/__init__.py"]


def resolve_local(tree: Tree, path: str, imp: Imp) -> list[str]:
    """Tracked files an import resolves to inside the plugin; empty when it leaves the plugin."""
    found: list[str] = []
    if imp.level:
        base = str(PurePosixPath(path).parent)
        for _ in range(imp.level - 1):
            base = str(PurePosixPath(base).parent)
        for cand in _module_candidates(base, imp.module):
            if tree.has(cand):
                found.append(cand)
        if imp.names:
            for name in imp.names:
                for cand in _module_candidates(base, f"{imp.module}.{name}" if imp.module else name):
                    if tree.has(cand):
                        found.append(cand)
        return found
    for root in _local_roots(path):
        hit = [c for c in _module_candidates(root, imp.module) if tree.has(c)]
        if not hit:
            top = imp.module.split(".")[0]
            hit = [c for c in _module_candidates(root, top) if tree.has(c)]
        if hit:
            found.extend(hit)
            for name in imp.names:
                found.extend(c for c in _module_candidates(root, f"{imp.module}.{name}") if tree.has(c))
            break
    return found


def foreign_imports(
    tree: Tree, path: str, *, scope: str = "entry", before_line: Optional[int] = None,
    include_optional: bool = False, _seen: Optional[set[str]] = None,
) -> dict[str, str]:
    """Top-level modules outside the stdlib and the plugin that ``path`` imports.

    ``scope`` says which imports count:

    * ``"import"``: only imports that run when a module is imported (not inside a function body);
    * ``"entry"``: every import in ``path`` itself, lazy ones included, and for the plugin modules
      those imports pull in, only what they import at import time. A script's own body states
      the libraries it uses; a module it imports is judged by what loading it does;
    * ``"all"``: every import in every plugin module reached.

    Optional imports (inside a ``try`` whose handler recovers instead of stopping) are skipped
    unless ``include_optional``: a script that degrades without a module does not need it, but
    still imports the wrong copy when the wrong interpreter has one.
    Returns ``{module: "file:line"}`` for the first sighting of each.
    """
    assert scope in {"import", "entry", "all"}, scope
    seen = _seen if _seen is not None else set()
    if path in seen:
        return {}
    seen.add(path)
    info = mod_info(path, tree.text(path))
    child_scope = "import" if scope == "entry" else scope
    out: dict[str, str] = {}
    for imp in info.imports:
        if imp.optional and not include_optional:
            continue
        if scope == "import" and not imp.import_time:
            continue
        if before_line is not None and imp.line >= before_line:
            continue
        top = imp.module.split(".")[0]
        if imp.level == 0 and top in _STDLIB:
            continue
        local = resolve_local(tree, path, imp)
        if local:
            for target in local:
                for mod, where in foreign_imports(
                    tree, target, scope=child_scope, include_optional=include_optional, _seen=seen
                ).items():
                    out.setdefault(mod, where)
            continue
        if imp.level:
            continue
        out.setdefault(top, f"{path}:{imp.line}")
    return out


class GuardVerdict(NamedTuple):
    state: str      # "guarded" | "clean" | "late" | "unguarded"
    detail: str


def guard_verdict(tree: Tree, path: str) -> GuardVerdict:
    info = mod_info(path, tree.text(path))
    if not info.guards:
        foreign = foreign_imports(tree, path)
        if not foreign:
            return GuardVerdict("clean", "imports nothing outside the standard library and the plugin")
        shown = ", ".join(f"{m} ({w})" for m, w in sorted(foreign.items())[:4])
        return GuardVerdict("unguarded", f"no {GUARD_NAME} call and it imports {shown}")
    module_level = [line for line, at_import in info.guards if at_import]
    first = min(module_level) if module_level else None
    late = foreign_imports(tree, path, scope="import", before_line=first)
    if late:
        shown = ", ".join(f"{m} ({w})" for m, w in sorted(late.items())[:4])
        return GuardVerdict("late", f"the guard runs after it imports {shown}")
    return GuardVerdict("guarded", f"calls {GUARD_NAME} before any foreign import")


OK_STATES = frozenset({"guarded", "clean"})


# --------------------------------------------------------------------------
# C: console scripts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsoleEntry:
    plugin: str
    pyproject: str
    name: str
    target: str
    module_path: Optional[str]
    func: str


def console_entries(tree: Tree) -> list[ConsoleEntry]:
    out = []
    for path in tree.paths():
        parts = path.split("/")
        if len(parts) != 3 or parts[0] != "plugins" or parts[2] != "pyproject.toml":
            continue
        data = tomllib.loads(tree.text(path))
        for name, target in (data.get("project", {}).get("scripts") or {}).items():
            module, _, func = target.partition(":")
            root = f"plugins/{parts[1]}"
            mod_path = None
            for base in (root, f"{root}/lib"):
                for cand in _module_candidates(base, module):
                    if tree.has(cand):
                        mod_path = cand
                        break
                if mod_path:
                    break
            out.append(ConsoleEntry(parts[1], path, name, target, mod_path, func))
    return out


def package_console_scripts(tree: Tree, plugin: str) -> dict[str, str]:
    """Console scripts the plugin's venv installs from first-party packages it requires.

    Maps script name -> ``<distribution>: <target>``. Reads the plugin's pyproject from
    ``tree`` and each required ``katsoft-*`` distribution's pyproject from ``packages/``.
    """
    pyproject = f"plugins/{plugin}/pyproject.toml"
    if not tree.has(pyproject):
        return {}
    out: dict[str, str] = {}
    for requirement in tomllib.loads(tree.text(pyproject)).get("project", {}).get("dependencies", []):
        name = re.split(r"[\[<>=!~;\s]", requirement.strip(), maxsplit=1)[0].lower().replace("_", "-")
        if not name.startswith("katsoft-"):
            continue
        source = _REPO_ROOT / "packages" / name / "pyproject.toml"
        if not source.is_file():
            continue
        for script, target in (tomllib.loads(source.read_text(encoding="utf-8"))
                               .get("project", {}).get("scripts") or {}).items():
            out[script] = f"{name}: {target}"
    return out


def console_problem(tree: Tree, entry: ConsoleEntry) -> Optional[str]:
    if entry.module_path is None:
        return f"console script {entry.name!r} -> {entry.target}: the module is not a tracked file of the plugin"
    info = mod_info(entry.module_path, tree.text(entry.module_path))
    if entry.func not in info.functions:
        return f"console script {entry.name!r} -> {entry.target}: no function {entry.func} in {entry.module_path}"
    guards = [g for g in info.func_guards if g.qualname == entry.func]
    if not guards:
        return (f"console script {entry.name!r} -> {entry.target}: {entry.func} never calls {GUARD_NAME}, so the "
                f"venv wrapper runs whatever code its venv holds")
    if not any(g.under_argv_none for g in guards):
        return (f"console script {entry.name!r} -> {entry.target}: {entry.func} calls {GUARD_NAME} but not on its "
                f"`argv is None` path (the console wrapper's path)")
    return None


# --------------------------------------------------------------------------
# S: launch-to-target
# --------------------------------------------------------------------------

_BOOT_RE = (
    r'"?\$\{BOOTSTRAP_PROJECT_PYTHON:-\$\{BOOTSTRAP_PYTHON:\?[^}]*\}\}"?'
    r'|"?\$\{BOOTSTRAP_PYTHON:\?[^}]*\}"?'
    r'|"?\$\{?BOOTSTRAP_PYTHON\}?"?'
)
_PLACEHOLDER_RE = (
    r'"?<plugin-venv-python>"?|"?<workflow-kit-venv-python>"?|"?<venvPython>"?'
    r'|"?\$\{(?:inputs|args)\.workflowKitVenvPython\}"?|"?\{\{ inputs\.workflowKitVenvPython \}\}"?'
    r'|"?\$\{venvPy\}"?|"?\$\{venvPython\}"?|"?\$\{refs\.venvPython\}"?'
)
_VENV_RE = (
    r'(?P<base>\$\{CLAUDE_BOOTSTRAP_DATA_ROOT:-[^}]*\}|~|\$\{?HOME\}?|%USERPROFILE%)"?'
    r'(?:[/\\]+[^\s"\'`]*?)?[/\\]+(?P<plugin>[\w-]+)[/\\]+\.venv[/\\]+(?:bin|Scripts)[/\\]+(?P<exe>[\w.-]+)"?'
)
_INTERP = re.compile(
    r"(?<![\w$.])(?:(?P<boot>" + _BOOT_RE + r")|(?P<place>" + _PLACEHOLDER_RE + r")|" + _VENV_RE + r")(?=\s)"
)
_TOKEN = re.compile(r"""\s*((?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\s"'])+)""")
_QUICK = re.compile(r"BOOTSTRAP_PYTHON|\.venv|venvpy|venv-python", re.I)
_PYTHON_EXES = {"python", "python.exe", "python3", "python3.exe"}


@dataclass(frozen=True)
class Launch:
    path: str
    line: int
    nphys: int
    text: str
    interp: str        # boot | placeholder | venv-r | venv-p | console-r | console-p
    tok: str           # the target token, the module, or the console name
    res_kind: str      # file | module | console | template | unresolved | inline
    res_path: Optional[str]
    plugin: Optional[str]    # for console launches, the plugin whose venv is named


def _strip_quotes(tok: str) -> str:
    return tok.replace('"', "").replace("'", "")


_PY_OPERAND = re.compile(r"^(?P<script>.*?\.py)(?![\w])")


def _target_of(rest: str) -> Optional[tuple[str, str]]:
    """("file"|"module"|"inline", token) for the first operand after the interpreter, or None.

    A token that is not a ``.py`` path (``set-key``, ``describe``, ``--version``) is not a
    script launch. Markdown emphasis and punctuation after the ``.py`` are ignored.
    """
    pos = 0
    while True:
        m = _TOKEN.match(rest, pos)
        if not m:
            return None
        pos = m.end()
        tok = _strip_quotes(m.group(1))
        if tok == "-m":
            m2 = _TOKEN.match(rest, pos)
            if not m2:
                return None
            module = re.match(r"[\w.]+", _strip_quotes(m2.group(1)))
            return ("module", module.group(0)) if module else None
        if tok == "-c":
            return ("inline", "-c")
        if tok.startswith("-"):
            continue
        py = _PY_OPERAND.match(tok)
        return ("file", py.group("script")) if py else None


_ROOT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\$\{CLAUDE_SKILL_DIR\}[/\\](?P<rel>.+)$"), "skill"),
    (re.compile(r"^\$\{CLAUDE_PLUGIN_ROOT\}[/\\](?P<rel>.+)$"), "plugin"),
    (re.compile(r"^\$\{(?P<var>[A-Z0-9_]+_ROOT)(?::\?.*?)?\}[/\\](?P<rel>.+)$"), "env"),
    (re.compile(r"^(?:<pluginRoot>|<root>|\$\{(?:inputs|args)\.pluginRoot\}|\{\{ inputs\.pluginRoot \}\})[/\\](?P<rel>.+)$"),
     "named"),
    (re.compile(r"^(?:\$\{?SCRIPT_DIR\}?|%SCRIPT_DIR%)[/\\](?P<rel>.+)$"), "sibling"),
]


def _unique_suffix(tree: Tree, rel: str) -> Optional[str]:
    hits = [p for p in tree.files if p.startswith("plugins/") and p.endswith("/" + rel)]
    return hits[0] if len(hits) == 1 else None


def _cd_plugin(tree: Tree, line_text: str) -> Optional[str]:
    """The plugin a command ``cd``s into first (``cd "${X_ROOT...}"`` or ``cd plugins/<name>``), if any."""
    env = root_variable_plugins(tree)
    m = re.search(r'cd\s+"?\$\{(?P<var>[A-Z0-9_]+_ROOT)', line_text)
    if m and m.group("var") in env:
        return env[m.group("var")]
    m = re.search(r'cd\s+"?plugins/(?P<name>[\w-]+)', line_text)
    return m.group("name") if m else None


def _module_files(tree: Tree, plugins: Iterable[str], dotted: str) -> list[str]:
    """Tracked files for ``dotted`` under each plugin's root and lib/, in the order given."""
    found: list[str] = []
    for name in plugins:
        for base in (f"plugins/{name}", f"plugins/{name}/lib"):
            found.extend(c for c in _module_candidates(base, dotted) if tree.has(c))
    return found


def resolve_target(tree: Tree, file_path: str, kind: str, tok: str, line_text: str) -> tuple[str, Optional[str]]:
    """(res_kind, tracked path) for a launch operand."""
    if kind == "inline":
        return "inline", None
    if kind == "module":
        near = [p for p in (_cd_plugin(tree, line_text), plugin_of(file_path)) if p]
        hits = _module_files(tree, near, tok)
        if hits:
            return "module", hits[0]
        hits = sorted(set(_module_files(tree, sorted(plugin_names(tree)), tok)))
        return ("module", hits[0]) if len(hits) == 1 else ("unresolved", None)
    if "<" in tok and not tok.startswith(("<pluginRoot>", "<root>")):
        return "template", None
    rel = None
    hint = None
    var_map = root_variable_plugins(tree)
    for pattern, how in _ROOT_PATTERNS:
        m = pattern.match(tok)
        if not m:
            continue
        rel = m.group("rel").replace("\\", "/")
        if how == "env":
            hint = var_map.get(m.group("var"))
        elif how == "sibling":
            sibling = f"{PurePosixPath(file_path).parent}/{rel}"
            return ("file", sibling) if tree.has(sibling) else ("unresolved", None)
        break
    else:
        rel = tok.replace("\\", "/")
        if tree.has(rel):
            return "file", rel
    cands = []
    if hint:
        cands.append(f"plugins/{hint}/{rel}")
    cd = _cd_plugin(tree, line_text)
    if cd:
        cands.append(f"plugins/{cd}/{rel}")
    own = plugin_of(file_path)
    if own:
        cands.append(f"plugins/{own}/{rel}")
    skill = skill_dir_of(tree, file_path)
    if skill:
        cands.append(f"{skill}/{rel}")
    for cand in cands:
        if tree.has(cand):
            return "file", cand
    unique = _unique_suffix(tree, rel)
    return ("file", unique) if unique else ("unresolved", None)


def _fragments(path: str, text: str) -> Iterator[tuple[int, str]]:
    """(first line number, text) of each region an S scan reads."""
    if path.endswith(".py"):
        try:
            module = ast.parse(text, filename=path)
        except SyntaxError as exc:
            raise AssertionError(f"{path} does not parse as Python 3: {exc}") from exc
        for node in ast.walk(module):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip():
                yield node.lineno, node.value
    else:
        yield 1, scan_view(path, text)


def launches(tree: Tree, path: str) -> list[Launch]:
    out: list[Launch] = []
    seen: set[tuple[int, str]] = set()
    for first, fragment in _fragments(path, tree.text(path)):
        for ll in logical_lines(path if not path.endswith(".py") else "fragment.txt", fragment, first_no=first):
            if not _QUICK.search(ll.text):
                continue
            for m in _INTERP.finditer(ll.text):
                rest = ll.text[m.end():]
                exe = m.group("exe")
                if m.group("boot"):
                    interp = "boot"
                elif m.group("place"):
                    interp = "placeholder"
                else:
                    redirected = m.group("base").startswith("${CLAUDE_BOOTSTRAP_DATA_ROOT")
                    suffix = "r" if redirected else "p"
                    interp = ("venv-" if exe.lower() in _PYTHON_EXES else "console-") + suffix
                key = (ll.no, m.group(0))
                if key in seen:
                    continue
                if interp.startswith("console-"):
                    seen.add(key)
                    name = re.sub(r"\.exe$", "", exe, flags=re.I)
                    out.append(Launch(path, ll.no, ll.nphys, ll.text.strip(), interp, name, "console", None,
                                      m.group("plugin")))
                    continue
                target = _target_of(rest)
                if target is None:
                    continue
                seen.add(key)
                kind, tok = target
                res_kind, res_path = resolve_target(tree, path, kind, tok, ll.text)
                out.append(Launch(path, ll.no, ll.nphys, ll.text.strip(), interp, tok, res_kind, res_path,
                                  m.group("plugin") if m.group("base") else None))
    return out


# --------------------------------------------------------------------------
# B: launchers and shims
# --------------------------------------------------------------------------


def _label_body(lines: list[str], label: str) -> str:
    start = None
    for i, line in enumerate(lines):
        if line.strip().lower() == f":{label}".lower():
            start = i + 1
            break
    if start is None:
        return ""
    body = []
    for line in lines[start:]:
        if re.match(r"^:\w", line.strip()):
            break
        body.append(line)
    return "\n".join(body)


def _cmd_block_after(lines: list[str], idx: int) -> str:
    """Lines of the parenthesised block that opens at ``lines[idx]``."""
    indent = len(lines[idx]) - len(lines[idx].lstrip())
    body = [lines[idx]]
    for line in lines[idx + 1:]:
        body.append(line)
        if line.strip() == ")" and len(line) - len(line.lstrip()) == indent:
            break
    return "\n".join(body)


# The shell branches the launcher contract names, in the spelling the shims use. A launcher
# that tests the variable some other way is reported, not guessed at.
SH_REDIRECT_BLOCK = re.compile(
    r'if \[\[? -n "\$\{CLAUDE_BOOTSTRAP_DATA_ROOT:-\}" \]\]?; then\n(?P<body>.*?)\n\s*fi\b\n?', re.S
)
SH_CACHED_BLOCK = re.compile(
    r'if \[\[? -n "\$\{CLAUDE_PLUGIN_TEST:-\}" \]\]?; then\n(?P<body>.*?)\nfi\b\n?', re.S
)


def redirect_problems(path: str, text: str) -> list[str]:
    """What is missing from a launcher's CLAUDE_BOOTSTRAP_DATA_ROOT branch."""
    dialect = dialect_of(path)
    problems: list[str] = []
    if dialect == "sh":
        m = SH_REDIRECT_BLOCK.search(text)
        if not m:
            return ["no CLAUDE_BOOTSTRAP_DATA_ROOT branch overrides the production interpreter"]
        body = m.group("body")
        if "CLAUDE_BOOTSTRAP_DATA_ROOT" not in body or "=" not in body:
            problems.append("the CLAUDE_BOOTSTRAP_DATA_ROOT branch assigns no interpreter")
        if not re.search(r"\bexit 2\b", body):
            problems.append("the CLAUDE_BOOTSTRAP_DATA_ROOT branch does not refuse (exit 2) a missing redirected interpreter")
        return problems
    if dialect == "cmd":
        lines = text.split("\n")
        idxs = [i for i, l in enumerate(lines) if re.search(r"defined\s+CLAUDE_BOOTSTRAP_DATA_ROOT", l, re.I)]
        if not idxs:
            return ["no `defined CLAUDE_BOOTSTRAP_DATA_ROOT` branch overrides the production interpreter"]
        refused = False
        assigned = False
        for i in idxs:
            line = lines[i]
            if re.search(r'set\s+"?\w+=', line, re.I):
                assigned = True
            goto = re.search(r"goto\s+:(\w+)", line, re.I)
            if goto and re.search(r"exit\s+/b\s+2", _label_body(lines, goto.group(1)), re.I):
                refused = True
            if line.rstrip().endswith("("):
                block = _cmd_block_after(lines, i)
                if re.search(r"exit\s+/b\s+2", block, re.I):
                    refused = True
                if re.search(r'set\s+"?\w+=', block, re.I):
                    assigned = True
        if not assigned:
            problems.append("the `defined CLAUDE_BOOTSTRAP_DATA_ROOT` branch assigns no interpreter")
        if not refused:
            problems.append("the `defined CLAUDE_BOOTSTRAP_DATA_ROOT` branch does not refuse (exit /b 2) a missing redirected interpreter")
        return problems
    return ["not a shell or cmd launcher"]


def cached_refusal_problems(path: str, text: str) -> list[str]:
    """A shim must refuse to run as the plugin-cache copy when CLAUDE_PLUGIN_TEST is set."""
    dialect = dialect_of(path)
    if dialect == "sh":
        m = SH_CACHED_BLOCK.search(text)
        if not m:
            return ["no CLAUDE_PLUGIN_TEST branch refuses the cached copy"]
        body = m.group("body")
        out = []
        if "/plugins/cache/" not in body:
            out.append("the CLAUDE_PLUGIN_TEST branch does not test for a plugins/cache path")
        if not re.search(r"\bexit 2\b", body):
            out.append("the CLAUDE_PLUGIN_TEST branch does not refuse (exit 2)")
        return out
    if dialect == "cmd":
        lines = text.split("\n")
        hit = [l for l in lines if re.search(r"defined\s+CLAUDE_PLUGIN_TEST\b(?!_)", l, re.I) and "goto" in l.lower()]
        if not hit:
            return ["no `defined CLAUDE_PLUGIN_TEST ... goto` line refuses the cached copy"]
        label = re.search(r"goto\s+:(\w+)", hit[0], re.I).group(1)
        body = _label_body(lines, label)
        out = []
        if not re.search(r"exit\s+/b\s+2", body, re.I):
            out.append(f"the :{label} section does not refuse (exit /b 2)")
        if "plugins\\cache" not in text:
            out.append("the shim does not test for a plugins\\cache path")
        return out
    return ["not a shell or cmd launcher"]


_TARGET_REF = re.compile(
    r"""(?:\$\{?(?:PLUGIN_ROOT|SCRIPT_DIR|script_dir)\}?|%(?:PLUGIN_ROOT|SCRIPT_DIR)%)[/\\]+(?P<rel>[\w./\\-]+\.py)""",
    re.I,
)


def _normalize(path: str) -> str:
    parts: list[str] = []
    for p in path.split("/"):
        if p == "..":
            if parts:
                parts.pop()
        elif p and p != ".":
            parts.append(p)
    return "/".join(parts)


def launcher_targets(tree: Tree, path: str) -> list[str]:
    """Paths of the ``.py`` files a shell or cmd launcher names relative to its plugin root or directory."""
    found: list[str] = []
    root = _plugin_root(path)
    here = str(PurePosixPath(path).parent)
    for line in tree.text(path).split("\n"):
        if line.lstrip().startswith(("#", "rem ", "::")):
            continue
        for m in _TARGET_REF.finditer(line):
            base = here if "script_dir" in m.group(0).lower() else root
            if base is None:
                continue
            rel = m.group("rel").replace("\\", "/")
            cand = _normalize(f"{base}/{rel}")
            if cand not in found:
                found.append(cand)
    return found


def is_shim(path: str) -> bool:
    parts = path.split("/")
    return len(parts) == 4 and parts[0] == "plugins" and parts[2] == "bin"


# --------------------------------------------------------------------------
# Exemptions: what stays on a production path or is an open finding
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Exempt:
    kind: str                       # P | S | C
    path: str                       # exact path or fnmatch glob
    anchor: str                     # text the site must still contain
    reason: str
    status: str = "exempt"          # "exempt" or "open" (a real finding, reported not fixed)
    check: Optional[Callable[[Tree], Optional[str]]] = None   # extra machine check of the stated reason


def _target_guarded(tree: Tree, script: str) -> Optional[str]:
    verdict = guard_verdict(tree, script)
    if verdict.state not in OK_STATES:
        return f"the exemption rests on {script} being guarded or clean, but it is {verdict.state}: {verdict.detail}"
    return None


_MAINT = (
    "maintainer prose in a plugin CLAUDE.md; it describes the production data layout or a command a human runs "
    "on the real machine, and launches nothing from a session"
)
_BOOTSTRAP_DOCS = (
    "bootstrap's own documentation of its production records and layout; a read-only diagnostic pointer or a "
    "layout description, not a launch (bootstrap is the one plugin that manages the production tree)"
)
_USER_STATE = (
    "user state, intentionally on the production path: the statusline is installed once per user into "
    "settings.json and its synced script lives under the canonical data root"
)
_HUMAN_CONTRACT = (
    "the human CLI contract: llm_scripting_kit.constants.CLI_COMMAND is the stable path a person types, pinned "
    "by tests/llm-scripting-kit/test_cli_command_contract.py; agent-facing text uses cli_command(), which "
    "returns the redirect-aware form under claudx"
)
_GUARD_DOC = (
    "the docstring of the vendored bootstrap_guard.py, which names the production log location; its code "
    "resolves the data root through CLAUDE_BOOTSTRAP_DATA_ROOT (data_dir), so the text is a description"
)
_RECORD_PROSE = (
    "a read-only pointer to a bootstrap record in skill text or a file table; it launches nothing and names "
    "no interpreter (the interpreter and the log precondition lines in the same skills are redirect-aware)"
)
_ERROR_TEXT = "text inside an error message naming where a venv normally lives; nothing is launched from it"


def _p(path: str, reason: str, *anchors: str) -> list[Exempt]:
    return [Exempt("P", path, anchor, reason) for anchor in anchors]


EXEMPT: list[Exempt] = [
    # ---- P: prose, contracts and diagnostics that stay on the production path ----
    *_p("plugins/CLAUDE.md", _MAINT,
        "<marketplace>/<plugin>/.venv/Scripts/python.exe",
        "<marketplace>/<plugin>/.venv/bin/python",
        "Runtime guard (provision-time)"),
    *_p("plugins/skills-kit/CLAUDE.md", _MAINT,
        "the plugin venv lives at",
        "The plugin venv path is",
        "provisions (~/.claude/plugins/data/plugins-kit/skills-kit/.venv)"),
    *_p("plugins/*bootstrap_guard.py", _GUARD_DOC, "`~/.claude/plugins/data/<marketplace>/<plugin>/bootstrap.log`"),
    *_p("plugins/bootstrap-stuck-fix/README.md", _BOOTSTRAP_DOCS, "tail ~/.claude/plugins/data/plugins-kit/bootstrap/bootstrap.log"),
    *_p("plugins/bootstrap/README.md", _BOOTSTRAP_DOCS, "~/.claude/plugins/data/plugins-kit/<plugin>/bootstrap.log"),
    *_p("plugins/bootstrap/skills/bootstrap/SKILL.md", _BOOTSTRAP_DOCS,
        "Verify by checking each plugin's log at ~/.claude/plugins/data/<marketplace>/<plugin>/bootstrap.log"),
    *_p("plugins/bootstrap/skills/bootstrap/references/manifest-reference.md", _BOOTSTRAP_DOCS,
        "Windows:     ~/.claude/plugins/data/<marketplace>/<plugin>/.venv/Scripts/python.exe",
        "macOS/Linux: ~/.claude/plugins/data/<marketplace>/<plugin>/.venv/bin/python",
        "The plugin venv interpreter is at the deterministic"),
    *_p("plugins/bootstrap/skills/bootstrap/references/remediation-reference.md", _BOOTSTRAP_DOCS,
        "Check `~/.claude/plugins/data/bootstrap/bootstrap.log`"),
    *_p("plugins/claude-ui-kit/skills/statusline/SKILL.md", _USER_STATE,
        "(equivalently the plugin's `~/.claude/plugins/data/plugins-kit/claude-ui-kit/bootstrap.log`)",
        "(or the plugin's bootstrap.log)"),
    *_p("plugins/llm-scripting-kit/README.md", _HUMAN_CONTRACT,
        "~/.claude/plugins/data/plugins-kit/llm-scripting-kit/.venv/bin/llm-scripting-kit",
        "(Windows: ~/.claude/plugins/data/plugins-kit/llm-scripting-kit/.venv/Scripts/llm-scripting-kit.exe)"),
    *_p("plugins/llm-scripting-kit/lib/llm_scripting_kit/constants.py", _HUMAN_CONTRACT, "CLI_COMMAND ="),
    *_p("plugins/llm-scripting-kit/skills/openrouter-account/SKILL.md", _RECORD_PROSE,
        "~/.claude/plugins/data/plugins-kit/llm-scripting-kit/deferred_requirements.json"),
    *_p("plugins/unreal-kit/skills/ue-python-api/SKILL.md", _RECORD_PROSE,
        "~/.claude/plugins/data/plugins-kit/unreal-kit/deferred_requirements.json"),
    *_p("plugins/unreal-kit/skills/ue-python-api/references/script-bootstrap.md",
        "describes how ue-runner.cmd reaches the plugin venv; the cmd itself is checked as a launcher",
        "checks the bootstrap-provisioned plugin venv"),
    *_p("plugins/unreal-kit/skills/unreal-domain/references/shared-runtime.md",
        "describes where bootstrap provisions the unreal-kit venv; launches nothing",
        "bootstrap provisions `~/.claude/plugins/data/plugins-kit/unreal-kit/.venv`"),
    *_p("plugins/skills-kit/skills_kit_lib/standards_resolve.py", _ERROR_TEXT,
        '"(~/.claude/plugins/data/<marketplace>/skills-kit/.venv/, "'),
    Exempt("P", "plugins/pdf-kit/skills/html-pdf/scripts/html_to_pdf.py", "deferred_requirements.json",
           "a diagnostic hint only: when Chromium is missing the script reads bootstrap's recorded install "
           "command for the message it prints, and never executes it. Under claudx it reads the production "
           "record, which can differ from the redirected tree's; a redirect-aware read would be more accurate, "
           "but no interpreter or code is selected by it"),
    Exempt("P", "plugins/bootstrap/scripts/diagnose-python-venv.sh", "VENV_DIR",
           "a diagnostic a human runs against the REAL machine to inspect the production venvs (bootstrap "
           "and unreal-kit); it is meant to read the production tree and launches no plugin script"),
    Exempt("P", "plugins/llm-scripting-kit/scripts/frontdoor.sh", "DEFAULT_PY",
           "a long-running LAN service a person starts outside any Claude session; the code it serves comes "
           "from this checkout's lib/ through PYTHONPATH, and only third-party packages come from the "
           "production venv"),
    Exempt("P", "plugins/llm-scripting-kit/scripts/model-server.sh", "default_py",
           "the swapper launch guard runs the production venv's python on llm_scripting_kit_cli.py, which "
           "calls reexec_under_plugin_venv, so under claudx it moves into the redirected venv or exits 2",
           check=lambda tree: _target_guarded(tree, "plugins/llm-scripting-kit/scripts/llm_scripting_kit_cli.py")),
    # ---- S: launches the scan cannot resolve to a plugin script, by design ----
    Exempt("S", "plugins/CLAUDE.md", "<script>.py",
           "a template in maintainer prose: `<script>` stands for any guarded script of a plugin"),
    Exempt("S", "plugins/workflow-kit/skills/workflow-kit/references/*", "wc.py",
           "an illustrative user script (wc.py) in an example of the script node strategy; it is the "
           "workflow author's payload, not a plugin script"),
    Exempt("S", "plugins/workflow-kit/skills/workflow-kit/references/*", "mypkg.parse",
           "an illustrative user module (mypkg.parse) in an example of the script node strategy; it is the "
           "workflow author's payload, not a plugin module"),
    # ---- C: console scripts ----
    Exempt("C", "plugins/bootstrap/pyproject.toml", "bootstrap-engine",
           "bootstrap's own engine entry point: it cannot re-exec under a plugin venv because it provisions "
           "them (see 'Bootstrap cannot patch itself' in the root CLAUDE.md); its interpreter chain is pinned "
           "by test_forced_interpreter_order.py"),
]

# llm-kit is a dev-only plugin whose source master does not carry, so its entries
# are present only where its files are; an entry for an absent file would be stale.
EXEMPT.extend(
    entry for entry in [
        *_p("plugins/llm-kit/lib/llm_kit/registry/constants.py",
            "CLI_COMMAND is the human contract: the literal ~/-prefixed display string an agent-facing "
            "message tells a reader to run for the llm-kit console script (the plugin venv's "
            "bin/llm-kit, Scripts/llm-kit.exe on Windows). It is text, not a launch path; the "
            "redirect-aware form is REDIRECTED_CLI_COMMAND, selected by cli_command() under a redirected "
            "data root",
            "CLI_COMMAND ="),
        *_p("plugins/llm-kit/skills/openrouter-account/SKILL.md", _RECORD_PROSE,
            "~/.claude/plugins/data/plugins-kit/llm-kit/deferred_requirements.json"),
        *_p("plugins/llm-kit/references/llm-access.md",
            "the human CLI contract: llm_kit.registry.constants.CLI_COMMAND is the stable path a person "
            "types; agent-facing text uses cli_command(), which returns the redirect-aware form under claudx",
            "~/.claude/plugins/data/plugins-kit/llm-kit/.venv/bin/llm-kit",
            "(Windows: ~/.claude/plugins/data/plugins-kit/llm-kit/.venv/Scripts/llm-kit.exe)"),
    ]
    if (_REPO_ROOT / entry.path).is_file()
)


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Problem:
    kind: str
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} [{self.kind}] {self.message}"


@dataclass
class FileScan:
    path: str
    pr: list[PRSite]
    launches: list[Launch]
    dblocks: list[DBlock]
    consoles: list[ConsoleEntry]
    problems: list[Problem]
    used: set[int]
    shim: bool = False
    launcher_targets: tuple[str, ...] = ()


def _find_exempt(exempts: list[Exempt], kind: str, path: str, text: str) -> Optional[int]:
    for i, e in enumerate(exempts):
        if e.kind == kind and fnmatch.fnmatchcase(path, e.path) and e.anchor in text:
            return i
    return None


def _reach_of(text: str) -> str:
    m = _REACH.search(text)
    return m.group(0) if m else ".venv"


def scan_file(tree: Tree, path: str, exempts: Optional[list[Exempt]] = None) -> FileScan:
    exempts = EXEMPT if exempts is None else exempts
    text = tree.text(path)
    fs = FileScan(path, pr_sites(tree, path), launches(tree, path), definition_blocks(tree, path), [], [], set())
    dialect = dialect_of(path)

    def use_exemption(idx: int, line: int) -> None:
        """Record that an exemption suppressed a site, and run the machine check of its stated reason."""
        fs.used.add(idx)
        entry = exempts[idx]
        if entry.check is not None:
            failed = entry.check(tree)
            if failed:
                fs.problems.append(Problem(entry.kind, path, line, f"the exemption's stated reason fails: {failed}"))

    # P: a production literal that reaches a venv or a bootstrap record
    d_p_ranges = [(b.first, b.last) for b in fs.dblocks if b.has_p]
    launcher_msgs: Optional[list[str]] = None
    for site in fs.pr:
        if site.kind != "P":
            continue
        if any(lo <= site.line <= hi for lo, hi in d_p_ranges):
            fs.problems.append(Problem(
                "D", path, site.line,
                "a venv-interpreter placeholder is defined with a production path; "
                "define it with the redirect-aware ${CLAUDE_BOOTSTRAP_DATA_ROOT:-...} form"))
            continue
        idx = _find_exempt(exempts, "P", path, site.text)
        if idx is not None:
            use_exemption(idx, site.line)
            continue
        if dialect is not None:
            if launcher_msgs is None:
                launcher_msgs = redirect_problems(path, text)
            if launcher_msgs:
                fs.problems.append(Problem(
                    "B", path, site.line,
                    "this launcher composes a production venv path and " + "; ".join(launcher_msgs)))
            continue
        fs.problems.append(Problem(
            "P", path, site.line,
            f"a production data path reaches {_reach_of(site.text)} ({site.shape}); use the redirect-aware "
            f"\"${{CLAUDE_BOOTSTRAP_DATA_ROOT:-$HOME/.claude/plugins/data}}\" form or add an EXEMPT entry with a reason"))

    # S: launch-to-target
    consoles = {(c.plugin, c.name): c for c in console_entries(tree)}
    for launch in fs.launches:
        msg = None
        if launch.res_kind == "inline":
            continue
        if launch.res_kind == "console":
            entry = consoles.get((launch.plugin or "", launch.tok))
            if entry is None:
                if launch.tok in package_console_scripts(tree, launch.plugin or ""):
                    continue
                msg = (f"console script {launch.tok!r} is not declared in plugins/{launch.plugin}/pyproject.toml "
                       f"[project.scripts] and is not installed by a first-party package it requires")
            else:
                msg = console_problem(tree, entry)
        elif launch.res_kind in {"template", "unresolved"}:
            msg = f"the launched target {launch.tok!r} does not resolve to a tracked script ({launch.res_kind})"
        else:
            assert launch.res_path is not None
            verdict = guard_verdict(tree, launch.res_path)
            if verdict.state not in OK_STATES:
                msg = f"{launch.res_path} is {verdict.state}: {verdict.detail}"
        if msg is None:
            continue
        idx = _find_exempt(exempts, "S", path, launch.text)
        if idx is not None:
            use_exemption(idx, launch.line)
            continue
        fs.problems.append(Problem("S", path, launch.line, f"launch of {launch.tok!r} via {launch.interp}: {msg}"))

    # C: console scripts declared by this pyproject
    for entry in console_entries(tree):
        if entry.pyproject != path:
            continue
        fs.consoles.append(entry)
        msg = console_problem(tree, entry)
        if msg is None:
            continue
        idx = _find_exempt(exempts, "C", path, entry.name)
        if idx is not None:
            use_exemption(idx, 0)
            continue
        fs.problems.append(Problem("C", path, 0, msg))

    # B: shell and cmd launchers
    if dialect is not None:
        targets = launcher_targets(tree, path)
        fs.launcher_targets = tuple(targets)
        for target in targets:
            if not tree.has(target):
                fs.problems.append(Problem("B", path, 0, f"the launcher names {target}, which is not a tracked file"))
                continue
            verdict = guard_verdict(tree, target)
            if verdict.state not in OK_STATES:
                fs.problems.append(Problem(
                    "B", path, 0, f"the launcher's target {target} is {verdict.state}: {verdict.detail}"))
        if is_shim(path) and targets:
            fs.shim = True
            for msg in cached_refusal_problems(path, text):
                fs.problems.append(Problem("B", path, 0, msg))
    return fs


@dataclass
class TreeScan:
    files: dict[str, FileScan]
    problems: list[Problem]
    used: set[int]


def scan_tree(tree: Tree, exempts: Optional[list[Exempt]] = None) -> TreeScan:
    exempts = EXEMPT if exempts is None else exempts
    files: dict[str, FileScan] = {}
    problems: list[Problem] = []
    used: set[int] = set()
    for path in tree.paths():
        fs = scan_file(tree, path, exempts)
        files[path] = fs
        problems.extend(fs.problems)
        used |= fs.used
    return TreeScan(files, problems, used)


@functools.lru_cache(maxsize=1)
def real_scan() -> TreeScan:
    return scan_tree(real_tree())


# ==========================================================================
# Tests
# ==========================================================================

_COMPILE = "plugins/workflow-kit/scripts/compile_workflow.py"


def _fmt(problems: list[Problem]) -> str:
    return "\n".join(f"  {p}" for p in problems)


def _kinds(problems: list[Problem], *kinds: str) -> list[Problem]:
    return [p for p in problems if p.kind in kinds]


# ---- mutation helpers (all in memory; the working tree is never written) ----


def strip_guard(tree: Tree, path: str) -> Tree:
    """Rename every call of the guard away; the file still parses."""
    text = tree.text(path)
    assert re.search(rf"\b{GUARD_NAME}\(", text), f"{path} has no {GUARD_NAME} call to remove"
    return tree.with_text(path, re.sub(rf"\b{GUARD_NAME}\(", "_removed_guard(", text))


def add_foreign_import(tree: Tree, path: str) -> Tree:
    """A clean script gains an import from outside the standard library and the plugin."""
    return tree.with_text(path, tree.text(path) + "\nimport yaml  # mutation: a foreign import\n")


def break_target(tree: Tree, path: str) -> Tree:
    """Make a launched target fail the verdict.

    A guarded target loses its guard. When the script's own import-time closure holds no foreign
    import (a dispatcher that imports by name inside functions), losing the guard changes nothing
    observable, so it also gains one: the result is a script that needs a guard and has none.
    """
    state = guard_verdict(tree, path).state
    assert state in OK_STATES, f"{path} is {state}; only a passing target can be broken"
    broken = strip_guard(tree, path) if state == "guarded" else tree
    if guard_verdict(broken, path).state in OK_STATES:
        broken = add_foreign_import(broken, path)
    assert guard_verdict(broken, path).state not in OK_STATES
    return broken


def _cmd_span(lines: list[str], idx: int) -> tuple[int, int]:
    """Index range [start, end) of the statement that begins at ``lines[idx]``: one line or its paren block."""
    if not lines[idx].rstrip().endswith("("):
        return idx, idx + 1
    indent = len(lines[idx]) - len(lines[idx].lstrip())
    for j in range(idx + 1, len(lines)):
        if lines[j].strip() == ")" and len(lines[j]) - len(lines[j].lstrip()) == indent:
            return idx, j + 1
    raise AssertionError("unterminated cmd block")


def _cmd_label_span(lines: list[str], label: str) -> tuple[int, int]:
    start = next(i for i, l in enumerate(lines) if l.strip().lower() == f":{label}".lower())
    end = next((j for j in range(start + 1, len(lines)) if re.match(r"^:\w", lines[j].strip())), len(lines))
    return start + 1, end


def drop_redirect_branch(path: str, text: str) -> str:
    """Delete the CLAUDE_BOOTSTRAP_DATA_ROOT branch from a launcher."""
    if dialect_of(path) == "sh":
        out, n = SH_REDIRECT_BLOCK.subn("", text, count=1)
        assert n == 1, f"{path} has no redirect branch to drop"
        return out
    lines = text.split("\n")
    keep, i, dropped = [], 0, 0
    while i < len(lines):
        if re.search(r"defined\s+CLAUDE_BOOTSTRAP_DATA_ROOT", lines[i], re.I):
            lo, hi = _cmd_span(lines, i)
            i = hi
            dropped += 1
            continue
        keep.append(lines[i])
        i += 1
    assert dropped, f"{path} has no redirect branch to drop"
    return "\n".join(keep)


def drop_redirect_refusal(path: str, text: str) -> str:
    """Keep the redirect branch but let a missing redirected interpreter through (exit 0, not 2)."""
    if dialect_of(path) == "sh":
        def swap(m: re.Match[str]) -> str:
            assert "exit 2" in m.group(0)
            return m.group(0).replace("exit 2", "true")
        out, n = SH_REDIRECT_BLOCK.subn(swap, text, count=1)
        assert n == 1, f"{path} has no redirect branch"
        return out
    lines = text.split("\n")
    changed = 0
    for i, line in enumerate(list(lines)):
        if not re.search(r"defined\s+CLAUDE_BOOTSTRAP_DATA_ROOT", line, re.I):
            continue
        spans = []
        goto = re.search(r"goto\s+:(\w+)", line, re.I)
        if goto:
            spans.append(_cmd_label_span(lines, goto.group(1)))
        if line.rstrip().endswith("("):
            spans.append(_cmd_span(lines, i))
        for lo, hi in spans:
            for j in range(lo, hi):
                if re.search(r"exit\s+/b\s+2", lines[j], re.I):
                    lines[j] = re.sub(r"exit\s+/b\s+2", "exit /b 0", lines[j], flags=re.I)
                    changed += 1
    assert changed, f"{path} has no refusal to drop"
    return "\n".join(lines)


def drop_cached_refusal(path: str, text: str) -> str:
    """Delete the shim's CLAUDE_PLUGIN_TEST branch (the cached-copy refusal)."""
    if dialect_of(path) == "sh":
        out, n = SH_CACHED_BLOCK.subn("", text, count=1)
        assert n == 1, f"{path} has no cached-copy branch"
        return out
    lines = text.split("\n")
    keep = [l for l in lines if not (re.search(r"defined\s+CLAUDE_PLUGIN_TEST\b(?!_)", l, re.I) and "goto" in l.lower())]
    assert len(keep) < len(lines), f"{path} has no cached-copy branch"
    return "\n".join(keep)


def drop_cached_exit(path: str, text: str) -> str:
    """Keep the cached-copy test but let the cached shim run (exit 0, not 2)."""
    if dialect_of(path) == "sh":
        def swap(m: re.Match[str]) -> str:
            assert "exit 2" in m.group(0)
            return m.group(0).replace("exit 2", "true")
        out, n = SH_CACHED_BLOCK.subn(swap, text, count=1)
        assert n == 1
        return out
    lines = text.split("\n")
    hit = next(l for l in lines if re.search(r"defined\s+CLAUDE_PLUGIN_TEST\b(?!_)", l, re.I) and "goto" in l.lower())
    lo, hi = _cmd_label_span(lines, re.search(r"goto\s+:(\w+)", hit, re.I).group(1))
    changed = 0
    for j in range(lo, hi):
        if re.search(r"exit\s+/b\s+2", lines[j], re.I):
            lines[j] = re.sub(r"exit\s+/b\s+2", "exit /b 0", lines[j], flags=re.I)
            changed += 1
    assert changed
    return "\n".join(lines)


def restore_production_literal(tree: Tree, path: str, line: int) -> Tree:
    """Turn the redirect-aware expression on a line back into the bare production base."""
    lines = tree.text(path).split("\n")
    old = lines[line - 1]
    assert _R_EXPR.search(old), f"{path}:{line} holds no redirect-aware expression"
    prefix = "${CLAUDE_BOOTSTRAP_DATA_ROOT:-"
    lines[line - 1] = _R_EXPR.sub(lambda m: m.group(0)[len(prefix):-1], old)
    return tree.with_text(path, "\n".join(lines))


# ---- the real tree: parameter sets, computed once --------------------------------

_REAL = real_scan()
_ALL_LAUNCHES = [l for fs in _REAL.files.values() for l in fs.launches]
_ALL_PR = [s for fs in _REAL.files.values() for s in fs.pr]


def _launch_id(l: Launch) -> str:
    return f"{l.path.removeprefix('plugins/')}:{l.line}"


def _guard_sensitive(l: Launch) -> bool:
    """A launch whose target passes the verdict, so breaking the target must turn it red."""
    tree = real_tree()
    if l.res_kind in {"file", "module"}:
        return guard_verdict(tree, l.res_path).state in OK_STATES
    if l.res_kind == "console":
        entry = {(c.plugin, c.name): c for c in console_entries(tree)}.get((l.plugin or "", l.tok))
        return entry is not None and console_problem(tree, entry) is None
    return False


_GUARD_LAUNCHES = [pytest.param(l, id=_launch_id(l)) for l in _ALL_LAUNCHES if _guard_sensitive(l)]
_R_EXPR_SITES = [
    pytest.param(s, id=f"{s.path.removeprefix('plugins/')}:{s.line}")
    for s in _ALL_PR if s.kind == "R" and s.variant == "expr" and s.path.endswith((".md", ".js", ".yaml", ".json", ".py"))
]
_REDIRECT_LAUNCHERS = sorted({s.path for s in _ALL_PR if s.kind == "R" and s.shape == "two-step"})
_SHIMS = sorted(p for p, fs in _REAL.files.items() if fs.shim)
_D_BLOCKS = [pytest.param(b, id=f"{b.path.removeprefix('plugins/')}:{b.first}")
             for fs in _REAL.files.values() for b in fs.dblocks]
_CONSOLES = [c for fs in _REAL.files.values() for c in fs.consoles]
_OK_CONSOLES = [pytest.param(c, id=c.name) for c in _CONSOLES if console_problem(real_tree(), c) is None]


# ---- the verdict on the real tree -------------------------------------------------


def test_every_launch_path_is_redirect_aware_or_guarded():
    assert not _REAL.problems, (
        "launch paths that name the production data root or reach an unguarded script. Spell a path with "
        "\"${CLAUDE_BOOTSTRAP_DATA_ROOT:-$HOME/.claude/plugins/data}\", give the target the guard, or add an "
        "EXEMPT entry with a reason:\n" + _fmt(_REAL.problems)
    )


def test_every_exemption_suppresses_a_live_site():
    stale = [f"{e.kind} {e.path} anchor={e.anchor!r}" for i, e in enumerate(EXEMPT) if i not in _REAL.used]
    assert not stale, "EXEMPT entries that match no site (remove or re-anchor them):\n  " + "\n  ".join(stale)


def test_every_exemption_states_a_reason_an_anchor_and_a_known_status():
    for e in EXEMPT:
        assert e.kind in {"P", "S", "C"}, e
        assert e.status in {"exempt", "open"}, e
        assert e.anchor, f"an empty anchor exempts a whole file: {e}"
        assert len(e.reason.split()) >= 8, f"the reason is too thin to judge: {e}"
        assert e.reason.isascii() and e.anchor.isascii(), e


@pytest.mark.parametrize("entry", [e for e in EXEMPT if e.status == "open"], ids=lambda e: f"{e.kind}:{e.anchor}")
def test_an_open_finding_is_still_flagged_without_its_exemption(entry):
    """An OPEN entry records a real defect; when the defect is fixed the entry must go."""
    tree = real_tree()
    others = [e for e in EXEMPT if e is not entry]
    paths = [p for p in tree.paths() if fnmatch.fnmatchcase(p, entry.path)]
    assert paths, f"{entry.path} matches no file"
    found = [pr for p in paths for pr in scan_file(tree, p, others).problems if pr.kind == entry.kind]
    assert found, (f"the finding behind OPEN entry {entry.kind} {entry.path} {entry.anchor!r} is gone; "
                   f"delete the entry")


def test_the_open_findings_are_reported_for_the_owner():
    """Not a failure: each open finding is raised as a warning so the test report keeps it visible."""
    for e in (e for e in EXEMPT if e.status == "open"):
        warnings.warn(f"OPEN FINDING [{e.kind}] {e.path} :: {e.anchor}: {e.reason}", stacklevel=1)


_P_EXEMPT_FILES = sorted({p for e in EXEMPT if e.kind == "P"
                         for p in real_tree().paths() if fnmatch.fnmatchcase(p, e.path)})


@pytest.mark.parametrize("path", _P_EXEMPT_FILES)
def test_an_exemption_covers_its_own_line_and_not_a_second_literal_in_the_file(path):
    """MUTATION: add one more production venv path to a file that holds an exemption."""
    tree = real_tree()
    probe = "# ~/.claude/plugins/data/plugins-kit/mutant-kit/.venv/bin/python"
    mutated = tree.with_text(path, tree.text(path).rstrip("\n") + "\n" + probe + "\n")
    line = len(mutated.text(path).rstrip("\n").split("\n"))
    flagged = [p for p in scan_file(mutated, path).problems if p.line == line and p.kind in {"P", "B"}]
    assert flagged, f"{path}: an exemption swallowed a second production literal"


def test_an_exemption_that_rests_on_a_guarded_target_fails_when_the_target_loses_its_guard():
    tree = real_tree()
    target = "plugins/llm-scripting-kit/scripts/llm_scripting_kit_cli.py"
    shell = "plugins/llm-scripting-kit/scripts/model-server.sh"
    assert not scan_file(tree, shell).problems
    problems = scan_file(break_target(tree, target), shell).problems
    assert any("stated reason fails" in p.message for p in problems), _fmt(problems)


# ---- every form finds a real instance --------------------------------------------


def test_every_form_has_a_real_instance():
    p_shapes = {s.shape for s in _ALL_PR if s.kind == "P"}
    r_shapes = {(s.shape, s.variant) for s in _ALL_PR if s.kind == "R"}
    assert {"single", "two-step"} <= p_shapes, f"production literals found only as {p_shapes}"
    # No production literal is split across lines in the tree (that is what the instrument keeps true);
    # the split spelling is real in the redirect-aware launches, and fixtures below cover the P spelling.
    assert ("split", "expr") in r_shapes, "no redirect-aware launch spans two lines"
    assert ("single", "expr") in r_shapes
    assert ("two-step", "var") in r_shapes, "no launcher holds the redirect variable"
    interps = {(l.interp, l.res_kind) for l in _ALL_LAUNCHES}
    for want in [("boot", "file"), ("boot", "module"), ("placeholder", "file"), ("placeholder", "module"),
                 ("venv-r", "file"), ("console-r", "console")]:
        assert want in interps, f"no launch of kind {want}; found {sorted(interps)}"
    texts = [l.text for l in _ALL_LAUNCHES]
    for spelling in ["<plugin-venv-python>", "<workflow-kit-venv-python>", "<venvPython>", "${args.workflowKitVenvPython}",
                     "{{ inputs.workflowKitVenvPython }}", "${inputs.workflowKitVenvPython}", "${venvPy}",
                     "<pluginRoot>", "${inputs.pluginRoot}", "{{ inputs.pluginRoot }}", "${args.pluginRoot}"]:
        assert any(spelling in t for t in texts), f"no launch spelled with {spelling}"
    assert _D_BLOCKS, "no placeholder definition block"
    assert _OK_CONSOLES, "no guarded console script"
    assert any(p.endswith(".cmd") for p in _SHIMS) and any(not p.endswith(".cmd") for p in _SHIMS)
    assert _REDIRECT_LAUNCHERS


def test_the_scan_sees_files_in_every_shipped_plugin_dir():
    scanned = {plugin_of(p) for p in _REAL.files}
    assert {"bootstrap", "git-kit", "job-kit", "llm-scripting-kit", "pdf-kit", "unreal-kit", "workflow-kit"} <= scanned


# ---- mutation: restore a production literal -------------------------------------


@pytest.mark.parametrize("site", _R_EXPR_SITES)
def test_restoring_a_production_literal_is_flagged(site):
    """MUTATION: rewrite one redirect-aware expression back to the bare production base."""
    tree = real_tree()
    clean = [p for p in scan_file(tree, site.path).problems if p.line == site.line]
    assert not clean, _fmt(clean)
    mutated = restore_production_literal(tree, site.path, site.line)
    flagged = [p for p in scan_file(mutated, site.path).problems if p.kind in {"P", "D", "B"} and p.line == site.line]
    assert flagged, f"{site.path}:{site.line}: a restored production literal went unnoticed"


# ---- mutation: remove a guard / break a launched target -------------------------


@pytest.mark.parametrize("launch", _GUARD_LAUNCHES)
def test_breaking_the_launched_target_is_flagged(launch):
    """MUTATION: strip the guard from a guarded target (or give a clean one a foreign import)."""
    tree = real_tree()
    assert not [p for p in scan_file(tree, launch.path).problems if p.line == launch.line]
    if launch.res_kind == "console":
        entry = {(c.plugin, c.name): c for c in console_entries(tree)}[(launch.plugin, launch.tok)]
        mutated = strip_guard(tree, entry.module_path)
    else:
        mutated = break_target(tree, launch.res_path)
    flagged = [p for p in scan_file(mutated, launch.path).problems if p.kind == "S" and p.line == launch.line]
    assert flagged, f"{launch.path}:{launch.line}: breaking {launch.res_path or launch.tok} went unnoticed"


@pytest.mark.parametrize("entry", _OK_CONSOLES, ids=lambda c: c.name)
def test_removing_the_guard_from_a_console_entry_is_flagged(entry):
    tree = real_tree()
    assert console_problem(tree, entry) is None
    mutated = strip_guard(tree, entry.module_path)
    problem = console_problem(mutated, entry)
    assert problem and "never calls" in problem
    assert [p for p in scan_file(mutated, entry.pyproject).problems if p.kind == "C"]


# ---- mutation: launchers and shims ------------------------------------------------


@pytest.mark.parametrize("path", _REDIRECT_LAUNCHERS)
def test_a_launcher_without_its_redirect_branch_is_flagged(path):
    """MUTATION: delete the CLAUDE_BOOTSTRAP_DATA_ROOT branch; the production venv path is left bare."""
    tree = real_tree()
    assert not scan_file(tree, path).problems
    mutated = tree.with_text(path, drop_redirect_branch(path, tree.text(path)))
    flagged = _kinds(scan_file(mutated, path).problems, "B")
    assert flagged and any("CLAUDE_BOOTSTRAP_DATA_ROOT" in p.message for p in flagged), _fmt(flagged)


@pytest.mark.parametrize("path", _REDIRECT_LAUNCHERS)
def test_a_launcher_that_falls_back_instead_of_refusing_is_flagged(path):
    """MUTATION: keep the branch, but a missing redirected interpreter does not exit 2."""
    tree = real_tree()
    mutated = tree.with_text(path, drop_redirect_refusal(path, tree.text(path)))
    flagged = _kinds(scan_file(mutated, path).problems, "B")
    assert flagged and any("refuse" in p.message for p in flagged), _fmt(flagged)


@pytest.mark.parametrize("path", _SHIMS)
def test_a_shim_without_the_cached_copy_refusal_is_flagged(path):
    """MUTATION: delete the CLAUDE_PLUGIN_TEST branch that refuses to run the cached copy."""
    tree = real_tree()
    mutated = tree.with_text(path, drop_cached_refusal(path, tree.text(path)))
    flagged = _kinds(scan_file(mutated, path).problems, "B")
    assert flagged and any("CLAUDE_PLUGIN_TEST" in p.message for p in flagged), _fmt(flagged)


@pytest.mark.parametrize("path", _SHIMS)
def test_a_shim_that_tests_but_does_not_refuse_the_cached_copy_is_flagged(path):
    tree = real_tree()
    mutated = tree.with_text(path, drop_cached_exit(path, tree.text(path)))
    flagged = _kinds(scan_file(mutated, path).problems, "B")
    assert flagged and any("refuse" in p.message for p in flagged), _fmt(flagged)


@pytest.mark.parametrize("path", _SHIMS)
def test_a_shim_whose_target_loses_its_guard_is_flagged(path):
    tree = real_tree()
    target = scan_file(tree, path).launcher_targets[0]
    mutated = break_target(tree, target)
    flagged = [p for p in scan_file(mutated, path).problems if p.kind == "B" and target in p.message]
    assert flagged, f"{path}: breaking its target {target} went unnoticed"


# ---- mutation: placeholder definitions --------------------------------------------


@pytest.mark.parametrize("block", _D_BLOCKS)
def test_a_production_path_in_a_placeholder_definition_is_flagged(block):
    """MUTATION: inject a production venv path into the paragraph that defines the placeholder."""
    tree = real_tree()
    assert not [p for p in scan_file(tree, block.path).problems if p.kind == "D"]
    lines = tree.text(block.path).split("\n")
    lines[block.first - 1] += " ~/.claude/plugins/data/plugins-kit/mutant-kit/.venv/bin/python"
    mutated = tree.with_text(block.path, "\n".join(lines))
    flagged = [p for p in scan_file(mutated, block.path).problems if p.kind == "D"]
    assert flagged, f"{block.path}:{block.first}: a production path in a definition went unnoticed"


# ---- compile_workflow.py: an explicit verdict ---------------------------------------


def test_compile_workflow_is_guarded_before_its_foreign_imports():
    """VERDICT: guarded.

    compile_workflow.py is launched under ``<plugin-venv-python>``. Its closure imports ``yaml`` at import
    time and, lazily, ``bootstrap_lib`` and ``llm_kit`` (model declarations and output contracts),
    so it carries the same vendored guard as its siblings, ahead of the ``workflow_kit_lib`` import.
    """
    tree = real_tree()
    verdict = guard_verdict(tree, _COMPILE)
    assert verdict.state == "guarded", verdict
    reached = foreign_imports(tree, _COMPILE, scope="all", include_optional=True)
    assert {"yaml", "bootstrap_lib", "llm_kit"} <= set(reached), sorted(reached)
    launchers = [l for l in _ALL_LAUNCHES if l.res_path == _COMPILE]
    assert launchers, "no launch of compile_workflow.py is left, so the verdict is moot"
    assert not [p for p in _REAL.problems if _COMPILE in p.message]
    assert not any(e.anchor == "compile_workflow.py" for e in EXEMPT), "compile_workflow.py needs no exemption"


def test_removing_the_guard_from_compile_workflow_flags_every_launch():
    """MUTATION: drop the guard call and every launch of the script is flagged again."""
    tree = real_tree()
    text = tree.text(_COMPILE)
    call = 'reexec_under_plugin_venv("workflow-kit")\n'
    assert text.count(call) == 1
    patched = tree.with_text(_COMPILE, text.replace(call, ""))
    assert guard_verdict(patched, _COMPILE).state != "guarded"
    launchers = [l for l in _ALL_LAUNCHES if l.res_path == _COMPILE]
    flagged = [p for p in scan_tree(patched, EXEMPT).problems if p.kind == "S" and _COMPILE in p.message]
    assert len(flagged) == len(launchers), _fmt(flagged)


def test_a_guard_placed_after_the_foreign_import_is_flagged_as_late():
    """A guard that runs after the workflow_kit_lib import (and its yaml) protects nothing."""
    tree = real_tree()
    text = tree.text(_COMPILE)
    call = "reexec_under_plugin_venv(\"workflow-kit\")\n"
    assert text.count(call) == 1
    # Move the guard call from ahead of the workflow_kit_lib import to the end of the file.
    late = text.replace(call, "") + "\n" + call
    verdict = guard_verdict(tree.with_text(_COMPILE, late), _COMPILE)
    assert verdict.state == "late" and "yaml" in verdict.detail


# ---- the job-kit console script ----------------------------------------------------------


def test_job_kit_cli_main_is_guarded_on_its_console_path():
    tree = real_tree()
    entry = next(c for c in console_entries(tree) if c.name == "job-kit")
    assert console_problem(tree, entry) is None
    assert not any(e.kind == "C" and e.anchor == "job-kit" for e in EXEMPT), "job-kit needs no exemption"


def test_unguarding_job_kit_cli_main_reopens_its_console_finding():
    """MUTATION: remove the guard, or move it off the ``argv is None`` path."""
    tree = real_tree()
    entry = next(c for c in console_entries(tree) if c.name == "job-kit")
    text = tree.text(entry.module_path)
    call = '        reexec_under_plugin_venv("job-kit")\n'
    assert text.count(call) == 1
    removed = tree.with_text(entry.module_path, text.replace(call, "        pass\n"))
    assert "never calls" in (console_problem(removed, entry) or "")
    head = "    if argv is None:\n"
    assert text.count(head) == 1
    off_path = tree.with_text(entry.module_path, text.replace(head, "    if True:\n"))
    assert "argv is None" in (console_problem(off_path, entry) or "")


# ---- the llm-kit console script ----------------------------------------------------------


def test_llm_kit_console_script_comes_from_the_package_the_plugin_requires():
    """The plugin venv's ``llm-kit`` is katsoft-llm-kit's entry point, not a plugin script."""
    tree = real_tree()
    assert not [c for c in console_entries(tree) if c.plugin == "llm-kit"], (
        "plugins/llm-kit/pyproject.toml declares its own console script; the plugin installs "
        "the released package instead")
    assert package_console_scripts(tree, "llm-kit").get("llm-kit") == "katsoft-llm-kit: llm_kit.cli:main"
    assert not any(e.kind == "C" and e.anchor == "llm-kit" for e in EXEMPT), "llm-kit needs no exemption"
    llm_kit_launches = [
        launch for fs in real_scan().files.values() for launch in fs.launches
        if launch.res_kind == "console" and launch.tok == "llm-kit"
    ]
    assert llm_kit_launches, "no llm-kit console launch found; the resolution below would be vacuous"


def test_dropping_the_package_requirement_reopens_the_llm_kit_launch_findings():
    """MUTATION: without the katsoft-llm-kit requirement, nothing installs the launched script."""
    tree = real_tree()
    path = "plugins/llm-kit/pyproject.toml"
    text = tree.text(path)
    start = text.index("dependencies = [")
    end = text.index("\n]", start) + 2
    mutated = tree.with_text(path, text[:start] + "dependencies = []" + text[end:])
    assert package_console_scripts(mutated, "llm-kit") == {}
    problems = scan_tree(mutated).problems
    assert any(p.kind == "S" and "'llm-kit'" in p.message for p in problems), problems


# ---- fixtures: forms with no instance in the tree, and the verdict's edges -------------

_FIX = "plugins/fix-kit"


def _fixture(files: dict[str, str]) -> Tree:
    return Tree(dict(files))


def test_a_split_production_literal_is_found_and_flagged():
    doc = (f"{_FIX}/skills/fix/SKILL.md",
           'run "$HOME/.claude/plugins/data"/plugins-kit/fix-kit/ \\\n  .venv/bin/python\n')
    tree = _fixture(dict([doc]))
    sites = [s for s in pr_sites(tree, doc[0]) if s.kind == "P"]
    assert sites and sites[0].shape == "split" and sites[0].nphys == 2
    assert _kinds(scan_file(tree, doc[0], []).problems, "P")


def test_a_split_production_literal_in_cmd_and_in_python_is_found():
    cmd = (f"{_FIX}/bin/fix.cmd",
           '@echo off\nset "VENV_PY=%USERPROFILE%\\.claude\\plugins\\data\\plugins-kit\\fix-kit\\ ^\n'
           '.venv\\Scripts\\python.exe"\n')
    py = (f"{_FIX}/lib/fix_kit/state.py",
          'from pathlib import Path\n\n'
          'RECORD = (Path.home() / ".claude" / "plugins" / "data" / "plugins-kit"\n'
          '          / "fix-kit" / "deferred_requirements.json")\n')
    tree = _fixture(dict([cmd, py]))
    cmd_sites = [s for s in pr_sites(tree, cmd[0]) if s.kind == "P"]
    py_sites = [s for s in pr_sites(tree, py[0]) if s.kind == "P"]
    assert cmd_sites and cmd_sites[0].shape == "split"
    assert py_sites and py_sites[0].shape == "split"
    assert _kinds(scan_file(tree, py[0], []).problems, "P")
    assert _kinds(scan_file(tree, cmd[0], []).problems, "B")


_SH_TWO_STEP = (
    "#!/usr/bin/env bash\n"
    'DATA="$HOME/.claude/plugins/data/plugins-kit/fix-kit"\n'
    'PY="$DATA/.venv/bin/python"\n'
    'exec "$PY" "$@"\n'
)
_CMD_TWO_STEP = (
    "@echo off\n"
    'set "DATA=%USERPROFILE%\\.claude\\plugins\\data\\plugins-kit\\fix-kit"\n'
    'set "PY=%DATA%\\.venv\\Scripts\\python.exe"\n'
    '"%PY%" %*\n'
)


def test_a_two_step_production_literal_is_found_and_flagged_in_sh_and_cmd():
    sh, cmd = f"{_FIX}/scripts/run.sh", f"{_FIX}/scripts/run.cmd"
    tree = _fixture({sh: _SH_TWO_STEP, cmd: _CMD_TWO_STEP})
    for path, line in ((sh, 3), (cmd, 3)):
        sites = [s for s in pr_sites(tree, path) if s.kind == "P" and s.shape == "two-step"]
        assert [s.line for s in sites] == [line], (path, sites)
        assert _kinds(scan_file(tree, path, []).problems, "B"), path


def test_a_launcher_with_a_redirect_branch_and_a_refusal_passes_and_loses_either_to_a_flag():
    good_sh = _SH_TWO_STEP.replace(
        'exec "$PY"',
        'if [ -n "${CLAUDE_BOOTSTRAP_DATA_ROOT:-}" ]; then\n'
        '    PY="${CLAUDE_BOOTSTRAP_DATA_ROOT}/plugins-kit/fix-kit/.venv/bin/python"\n'
        '    if [ ! -x "$PY" ]; then\n        echo missing >&2\n        exit 2\n    fi\nfi\nexec "$PY"')
    good_cmd = _CMD_TWO_STEP.replace(
        '"%PY%" %*',
        'if defined CLAUDE_BOOTSTRAP_DATA_ROOT set "PY=%CLAUDE_BOOTSTRAP_DATA_ROOT%\\plugins-kit\\fix-kit\\.venv\\Scripts\\python.exe"\n'
        'if defined CLAUDE_BOOTSTRAP_DATA_ROOT if not exist "%PY%" goto :missing\n"%PY%" %*\nexit /b 0\n'
        ':missing\necho missing 1>&2\nexit /b 2\n')
    sh, cmd = f"{_FIX}/scripts/run.sh", f"{_FIX}/scripts/run.cmd"
    tree = _fixture({sh: good_sh, cmd: good_cmd})
    assert not scan_file(tree, sh, []).problems and not scan_file(tree, cmd, []).problems
    for path in (sh, cmd):
        bare = tree.with_text(path, drop_redirect_branch(path, tree.text(path)))
        assert _kinds(scan_file(bare, path, []).problems, "B"), path
        soft = tree.with_text(path, drop_redirect_refusal(path, tree.text(path)))
        assert any("refuse" in p.message for p in scan_file(soft, path, []).problems), path


_GUARDED_SCRIPT = (
    "import sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parent))\n"
    "from bootstrap_guard import reexec_under_plugin_venv\n\n"
    'reexec_under_plugin_venv("fix-kit")\n'
    "import yaml\n"
)
_GUARD_STUB = "def reexec_under_plugin_venv(name):\n    pass\n"
_LAUNCH_DOC = (
    'Run "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLAUDE_PLUGIN_ROOT}/scripts/tool.py" now.\n'
)


def _launch_tree(script: str) -> Tree:
    return _fixture({
        f"{_FIX}/skills/fix/SKILL.md": _LAUNCH_DOC,
        f"{_FIX}/scripts/tool.py": script,
        f"{_FIX}/scripts/bootstrap_guard.py": _GUARD_STUB,
    })


def test_the_target_verdict_separates_guarded_clean_late_and_unguarded():
    doc = f"{_FIX}/skills/fix/SKILL.md"
    cases = {
        "guarded": _GUARDED_SCRIPT,
        "clean": "import json\nimport sys\n",
        "unguarded": "import yaml\n",
        "late": "import yaml\n" + _GUARDED_SCRIPT.replace("import yaml\n", ""),
    }
    for state, script in cases.items():
        tree = _launch_tree(script)
        assert guard_verdict(tree, f"{_FIX}/scripts/tool.py").state == state, state
        problems = [p for p in scan_file(tree, doc, []).problems if p.kind == "S"]
        assert bool(problems) == (state in {"unguarded", "late"}), (state, problems)


def test_an_import_that_degrades_is_optional_and_one_that_stops_the_script_is_required():
    tool = f"{_FIX}/scripts/tool.py"
    degrades = _launch_tree("try:\n    import yaml\nexcept ImportError:\n    yaml = None\n")
    assert guard_verdict(degrades, tool).state == "clean"
    assert "yaml" in foreign_imports(degrades, tool, include_optional=True)
    stops = _launch_tree("try:\n    import yaml\nexcept ImportError:\n    raise SystemExit('install yaml')\n")
    assert guard_verdict(stops, tool).state == "unguarded"
    requires = _launch_tree(
        "try:\n    import yaml\nexcept ImportError:\n    from bootstrap_guard import require_bootstrap\n"
        "    require_bootstrap('fix-kit')\n")
    assert guard_verdict(requires, tool).state == "unguarded"


def test_a_lazy_import_counts_in_the_launched_script_itself_and_not_in_the_modules_it_loads():
    tool = f"{_FIX}/scripts/tool.py"
    own = _fixture({tool: "def run():\n    import yaml\n    return yaml\n"})
    assert guard_verdict(own, tool).state == "unguarded"
    deeper = _fixture({tool: "import helper\n", f"{_FIX}/scripts/helper.py": "def run():\n    import yaml\n"})
    assert guard_verdict(deeper, tool).state == "clean"
    assert "yaml" in foreign_imports(deeper, tool, scope="all")


def test_a_foreign_import_reached_through_a_plugin_module_makes_a_script_foreign():
    tree = _fixture({
        f"{_FIX}/skills/fix/SKILL.md": _LAUNCH_DOC,
        f"{_FIX}/scripts/tool.py": "import helper\n",
        f"{_FIX}/scripts/helper.py": "import yaml\n",
    })
    verdict = guard_verdict(tree, f"{_FIX}/scripts/tool.py")
    assert verdict.state == "unguarded" and "yaml" in verdict.detail


def test_an_unresolvable_launch_target_is_flagged():
    tree = _fixture({f"{_FIX}/skills/fix/SKILL.md": _LAUNCH_DOC})
    problems = scan_file(tree, f"{_FIX}/skills/fix/SKILL.md", []).problems
    assert [p for p in problems if p.kind == "S" and "does not resolve" in p.message]


def test_a_module_launch_resolves_through_the_plugin_and_is_judged():
    doc = ('Run `(cd "${FIX_KIT_ROOT:?requires a bootstrap engine pass}" && <venvPython> -m fix_kit.audit)`.\n')
    tree = _fixture({
        f"{_FIX}/skills/fix/SKILL.md": doc,
        f"{_FIX}/fix_kit/__init__.py": "",
        f"{_FIX}/fix_kit/audit.py": "import yaml\n",
    })
    launches_found = launches(tree, f"{_FIX}/skills/fix/SKILL.md")
    assert [(l.interp, l.res_kind, l.res_path) for l in launches_found] == [
        ("placeholder", "module", f"{_FIX}/fix_kit/audit.py")]
    assert [p for p in scan_file(tree, f"{_FIX}/skills/fix/SKILL.md", []).problems if p.kind == "S"]


def test_a_generated_launch_in_a_python_string_is_scanned():
    py = (f"{_FIX}/lib/fix_kit/compile.py",
          'RUNNER = (\n    \'`"${inputs.workflowKitVenvPython}" \'\n    \'"${inputs.pluginRoot}/scripts/tool.py"`\'\n)\n')
    tree = _fixture(dict([py, (f"{_FIX}/scripts/tool.py", "import yaml\n")]))
    found = launches(tree, py[0])
    assert [(l.interp, l.res_path) for l in found] == [("placeholder", f"{_FIX}/scripts/tool.py")]
    assert [p for p in scan_file(tree, py[0], []).problems if p.kind == "S"]


@pytest.mark.parametrize("body, want", [
    ("", "never calls"),
    ('    reexec_under_plugin_venv("fix-kit")\n', "argv is None"),
    ('    if argv is None:\n        reexec_under_plugin_venv("fix-kit")\n', None),
], ids=["no guard", "guard off the argv path", "guard on the argv path"])
def test_a_console_entry_must_be_guarded_on_its_argv_is_none_path(body, want):
    tree = _fixture({
        f"{_FIX}/pyproject.toml": '[project]\nname = "fix-kit"\n[project.scripts]\nfix = "fix_kit.cli:main"\n',
        f"{_FIX}/lib/fix_kit/cli.py": f"def main(argv=None):\n{body}    return 0\n",
    })
    problem = console_problem(tree, console_entries(tree)[0])
    assert (problem is None) if want is None else (want in (problem or "")), problem


def test_a_production_literal_in_a_file_the_exemptions_do_not_name_is_flagged():
    path = f"{_FIX}/README.md"
    tree = _fixture({path: "Run ~/.claude/plugins/data/plugins-kit/fix-kit/.venv/bin/python x.py\n"})
    assert _kinds(scan_file(tree, path).problems, "P")


def test_data_dev_is_not_the_production_root():
    tree = _fixture({f"{_FIX}/README.md": "~/.claude/plugins/data-dev/plugins-kit/fix-kit/.venv/bin/python\n"})
    assert not [s for s in pr_sites(tree, f"{_FIX}/README.md") if s.kind == "P"]
