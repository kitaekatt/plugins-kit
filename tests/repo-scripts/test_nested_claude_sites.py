"""Inventory guard: every code site in ``plugins/`` that can start a ``claude``
session is either wired to ``bootstrap_lib.claudx`` or an anchored exemption.

A claudx session (``scripts/claude_plugin_test.py``) loads the working copy
through ``--plugin-dir`` and redirects bootstrap's data root. A nested ``claude``
child started by plugin code inherits the variables but not the flags, so each
site that starts one must call into ``bootstrap_lib.claudx`` (direct children get
the flags, supervised sessions are refused). This test lists the sites by AST
and fails when a new one appears that the table below does not classify.

WHAT COUNTS AS A SITE (Python files under ``plugins/``, tests and ``.venv``
excluded; tracked or untracked-and-unignored, so a new file is seen before it is
committed):

- WHICH: a call to ``which`` or ``_resolve_cli`` whose first argument is the
  string ``"claude"``, or any call to ``resolve_claude_executable`` /
  ``resolve_claude_cli`` (the executable is being resolved; the last two are
  ``llm_kit.adapters.claude_command``, which resolves ``LLM_KIT_CLAUDE_EXECUTABLE``
  and so passes no literal ``"claude"`` at its call sites; counted only in a
  file that imports from ``claude_command``, because bootstrap's
  ``marketplace_lifecycle.resolve_claude_cli`` shares the name and is a probe
  for `claude plugin ...` management commands).
- LIST: a list literal that does not begin with a string constant and holds one
  of ``"-p"``, ``"--print"``, ``"--bg"``, ``"respawn"`` (an argv is being built).
- RESPAWN: a call passing the string ``"respawn"`` positionally.
- CONTRACT: an attribute read ``contract.command`` (job-kit's contract command).

A WIRED entry names a call that must appear inside the site's function, and the
file must import ``bootstrap_lib.claudx``. An EXEMPT entry names an anchor string
that must still occur in the file, and a reason. Not covered: non-Python
launchers (shell, JS, ``.cmd``); a grep of ``plugins/`` for ``claude -p`` and
``claude --bg`` in those finds comments only.

Revert-to-red: each wired entry is mutated below (its call renamed away, then the
claudx import renamed away) and the verdict must flag it; a synthetic new site
must be flagged.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_LIST_TOKENS = frozenset({"-p", "--print", "--bg", "respawn"})
_CLAUDE_RESOLVERS = frozenset({"resolve_claude_executable", "resolve_claude_cli"})
_CLAUDX_MODULE = "bootstrap_lib.claudx"

_MARKETPLACE = "plugins/bootstrap/bootstrap_lib/marketplace_lifecycle.py"
_STUCK_FIX = "plugins/bootstrap-stuck-fix/scripts/repair_update_scope.py"
_AGEFILE = "plugins/secrets-kit/lib/secrets_kit/agefile.py"

# (relpath, function qualname, rule) -> ("wired", call) | ("exempt", anchor, reason)
SITES = {
    (_MARKETPLACE, "_find_claude_cli", "WHICH"): (
        "exempt", "def _find_claude_cli",
        "locates the CLI for `claude plugin ...` management commands, which start no session",
    ),
    (_STUCK_FIX, "run_update", "WHICH"): (
        "exempt", 'shutil.which("claude")',
        "runs `claude plugin ...` management commands, which start no session",
    ),
    (_AGEFILE, "wrap_identity", "LIST"): (
        "exempt", "def wrap_identity",
        "the `age -p` passphrase flag; the executable is age, not claude",
    ),
}


# Library source is audited in katsoft-libs; this inventory owns plugins only.

SITE_TABLE = SITES


def scan(source: str, relpath: str):
    """Return ``[(qualname, rule, lineno)]`` and ``{qualname: function node}``."""
    tree = ast.parse(source, filename=relpath)
    uses_claude_command = any(
        isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[-1] == "claude_command"
        for n in ast.walk(tree)
    )
    found = []
    functions = {}
    stack = []

    class Visitor(ast.NodeVisitor):
        def _qualname(self):
            return ".".join(stack) or "<module>"

        def _scoped(self, node):
            stack.append(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions[".".join(stack)] = node
            self.generic_visit(node)
            stack.pop()

        visit_FunctionDef = _scoped
        visit_AsyncFunctionDef = _scoped
        visit_ClassDef = _scoped

        def visit_Call(self, node):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            args = node.args
            if (uses_claude_command and name in _CLAUDE_RESOLVERS) or (
                name in ("which", "_resolve_cli")
                and args
                and isinstance(args[0], ast.Constant)
                and args[0].value == "claude"
            ):
                found.append((self._qualname(), "WHICH", node.lineno))
            if any(isinstance(a, ast.Constant) and a.value == "respawn" for a in args):
                found.append((self._qualname(), "RESPAWN", node.lineno))
            self.generic_visit(node)

        def visit_List(self, node):
            if node.elts and not isinstance(node.elts[0], ast.Constant):
                values = {e.value for e in node.elts if isinstance(e, ast.Constant)}
                if values & _LIST_TOKENS:
                    found.append((self._qualname(), "LIST", node.lineno))
            self.generic_visit(node)

        def visit_Attribute(self, node):
            if (
                node.attr == "command"
                and isinstance(node.value, ast.Name)
                and node.value.id == "contract"
            ):
                found.append((self._qualname(), "CONTRACT", node.lineno))
            self.generic_visit(node)

    Visitor().visit(tree)
    return found, functions, tree


def _imports_claudx(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == _CLAUDX_MODULE:
            return True
        if isinstance(node, ast.Import) and any(a.name == _CLAUDX_MODULE for a in node.names):
            return True
    return False


def _calls(function: ast.AST, name: str) -> bool:
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if called == name:
                return True
    return False


def verdict(relpath: str, source: str, table=None):
    """Problems for one file against the table; empty when it is classified."""
    table = SITE_TABLE if table is None else table
    found, functions, tree = scan(source, relpath)
    problems = []
    seen = set()
    for qualname, rule, lineno in found:
        key = (relpath, qualname, rule)
        seen.add(key)
        entry = table.get(key)
        where = f"{relpath}:{lineno} {qualname} [{rule}]"
        if entry is None:
            problems.append(f"{where}: unclassified claude site (wire it to bootstrap_lib.claudx or add an exemption)")
        elif entry[0] == "wired":
            if not _imports_claudx(tree):
                problems.append(f"{where}: wired but the file does not import {_CLAUDX_MODULE}")
            function = functions.get(qualname)
            if function is None or not _calls(function, entry[1]):
                problems.append(f"{where}: wired but {qualname} does not call {entry[1]}")
        elif entry[0] == "exempt":
            if entry[1] not in source:
                problems.append(f"{where}: exemption anchor {entry[1]!r} is no longer in the file")
        else:
            problems.append(f"{where}: unknown disposition {entry[0]!r}")
    for key, entry in table.items():
        if key[0] == relpath and key not in seen:
            problems.append(f"{key}: table entry matches no site in {relpath} (stale)")
    return problems


def _tree_files():
    out = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "ls-files", "--cached", "--others",
         "--exclude-standard", "--", "plugins", "packages/katsoft-llm-kit/src"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    return sorted(
        p for p in out
        if p.endswith(".py") and "/.venv/" not in p and "/tests/" not in p
        and (_REPO_ROOT / p).is_file()
    )


def test_every_claude_site_in_plugins_is_classified():
    files = _tree_files()
    assert files, "git ls-files found no plugin Python files"
    problems = []
    for relpath in files:
        source = (_REPO_ROOT / relpath).read_text(encoding="utf-8")
        found, _functions, _tree = scan(source, relpath)
        if found or any(key[0] == relpath for key in SITE_TABLE):
            problems.extend(verdict(relpath, source))
    assert not problems, "\n".join(problems)


def test_the_scan_finds_every_rule_in_the_real_tree():
    seen_rules = set()
    for relpath in _tree_files():
        source = (_REPO_ROOT / relpath).read_text(encoding="utf-8")
        seen_rules.update(rule for _q, rule, _n in scan(source, relpath)[0])
    # RESPAWN and CONTRACT sites lived in the removed content-pipeline-kit and job-kit
    # libraries; their detectors are shown to fire on planted source below.
    assert seen_rules == {"WHICH", "LIST"}


def test_the_respawn_and_contract_detectors_fire_on_planted_source():
    planted = (
        "def go(runner, contract):\n"
        "    runner('claude', 'respawn')\n"
        "    return contract.command\n"
    )
    rules = {rule for _q, rule, _n in scan(planted, "planted.py")[0]}
    assert {"RESPAWN", "CONTRACT"} <= rules, rules


def test_every_table_file_is_scanned():
    files = set(_tree_files())
    missing = sorted({key[0] for key in SITE_TABLE} - files)
    assert not missing, f"table names files the scan does not see: {missing}"


_WIRED = sorted(key for key, entry in SITE_TABLE.items() if entry[0] == "wired")


def test_there_are_wired_sites_to_mutate():
    # Every wired site lived in a removed library; the plugins now hold exemptions only.
    assert _WIRED == []


@pytest.mark.parametrize("key", _WIRED, ids=lambda k: f"{Path(k[0]).name}:{k[1]}")
def test_the_unmodified_wired_file_is_clean(key):
    relpath = key[0]
    source = (_REPO_ROOT / relpath).read_text(encoding="utf-8")
    assert verdict(relpath, source) == []


@pytest.mark.parametrize("key", _WIRED, ids=lambda k: f"{Path(k[0]).name}:{k[1]}")
def test_removing_the_wired_call_is_flagged(key):
    """MUTATION: rename the wired call away; the site is flagged."""
    relpath, qualname, _rule = key
    call = SITE_TABLE[key][1]
    source = (_REPO_ROOT / relpath).read_text(encoding="utf-8")
    assert f"{call}(" in source
    mutated = source.replace(f"{call}(", "_removed_call(")
    problems = verdict(relpath, mutated)
    assert any(qualname in p and call in p for p in problems), problems


@pytest.mark.parametrize("key", _WIRED, ids=lambda k: f"{Path(k[0]).name}:{k[1]}")
def test_removing_the_claudx_import_is_flagged(key):
    """MUTATION: point the import at another module; the site is flagged."""
    relpath, qualname, _rule = key
    source = (_REPO_ROOT / relpath).read_text(encoding="utf-8")
    assert _CLAUDX_MODULE in source
    mutated = source.replace(_CLAUDX_MODULE, "bootstrap_lib.other")
    problems = verdict(relpath, mutated)
    assert any(qualname in p and "does not import" in p for p in problems), problems


_EXEMPT = sorted(key for key, entry in SITE_TABLE.items() if entry[0] == "exempt")


@pytest.mark.parametrize("key", _EXEMPT, ids=lambda k: f"{Path(k[0]).name}:{k[1]}")
def test_a_moved_exemption_anchor_is_flagged(key):
    relpath = key[0]
    anchor = SITE_TABLE[key][1]
    source = (_REPO_ROOT / relpath).read_text(encoding="utf-8")
    assert anchor in source
    # A def anchor is renamed; an expression anchor is replaced by None. Both
    # stay valid Python, so the verdict (not the parser) must notice.
    moved = anchor + "_moved" if anchor.startswith("def ") else "None"
    problems = verdict(relpath, source.replace(anchor, moved))
    assert problems, "a moved exemption anchor went unnoticed"


def test_a_new_argv_builder_is_flagged():
    source = "def launch(exe, prompt):\n    return [exe, '-p', prompt]\n"
    problems = verdict("plugins/new-kit/lib/new_kit/spawn.py", source)
    assert problems and "unclassified" in problems[0]


def test_a_new_executable_resolver_is_flagged():
    source = "import shutil\n\ndef find():\n    return shutil.which('claude')\n"
    problems = verdict("plugins/new-kit/lib/new_kit/find.py", source)
    assert problems and "unclassified" in problems[0]


def test_a_new_respawn_call_is_flagged():
    source = "def again(cli, sid):\n    return cli._lifecycle('respawn', sid)\n"
    problems = verdict("plugins/new-kit/lib/new_kit/again.py", source)
    assert problems and "unclassified" in problems[0]


def test_a_non_claude_list_is_not_a_site():
    source = "def run():\n    return ['git', '-p', 'x']\n"
    assert verdict("plugins/new-kit/lib/new_kit/git.py", source) == []


def test_a_new_claude_command_resolution_is_flagged():
    source = (
        "from llm_kit.adapters.claude_command import resolve_claude_executable\n\n"
        "def find():\n    return resolve_claude_executable()\n"
    )
    problems = verdict("packages/new/src/new/find.py", source)
    assert problems and "unclassified" in problems[0]


def test_a_same_named_resolver_outside_claude_command_is_not_a_site():
    source = (
        "from .marketplace_lifecycle import resolve_claude_cli\n\n"
        "def probe():\n    return resolve_claude_cli()\n"
    )
    assert verdict("plugins/new-kit/lib/new_kit/probe.py", source) == []
