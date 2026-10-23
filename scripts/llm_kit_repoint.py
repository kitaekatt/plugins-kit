#!/usr/bin/env python3
"""Generate the re-pointed legacy test mirror for llm-kit's differential.

Maintainer tool for the llm-kit consolidation. It reads the three legacy plugin
trees and their test suites and writes the ignored in-repo mirror under ``tmp/``:
each legacy test file whose ledger row says ``rewrite``, re-pointed from the
legacy package names to llm-kit's final names, plus the generated alias
modules that stand in for the legacy package namespaces (``tmp/legacy_names``).
The legacy trees are never written, and nothing here writes under ``plugins/``.

Commands (run from the repository root, ``uv run --extra dev python <this file>
<command>``; ``sync`` collects the legacy suites, so pytest must be importable):

``sync``      Refresh the collection counts in the disposition ledgers and the
              mirror. It never touches the legacy-module hash table.
``mirror``    Regenerate only the mirror under ``tmp/``.
``clean``     Remove the mirror under tmp/ (the files this tool wrote, nothing else).
``check``     Exit 1 when the map, the ledgers or the final tree disagree with
              the legacy trees (what ``tests/llm-kit/test_copy_inventory.py`` asserts).
``report``    Print every legacy name a ``rewrite`` test file uses that has no
              final home yet, per file (the input to a ledger decision).

The map (``tests/llm-kit/repoint-map.json``, schema 2) names, for every legacy
module, where its code lives in the final package:

``module``          the whole module lives at ``final``.
``facade-package``  a legacy package ``__init__``; its names are the final
                    package's (``dropped_names`` are names the final package
                    deliberately no longer binds).
``facade-names``    a legacy ``__init__`` whose names come from several final
                    modules; ``names`` maps each to ``[final module, attribute]``.
``forwarded``       a legacy CLI module whose code now lives in the CLI groups
                    (``homes``). Its alias module forwards attribute reads and
                    writes to the group modules that bind the name and translates
                    ``main(argv)`` from the legacy verb to the group form, so a
                    test patches and drives the final code; what the translation
                    cannot reproduce is declared in the ledger.
``data``            a data file the final package does not carry.

The rewrite is mechanical and parsed, never a blind text replace: absolute
imports are rewritten from the syntax tree, module-name strings and code strings
only in the shapes listed in ``rewrite_test_source``. A legacy name that has no
final home is collected and reported (``report``); generation fails on it, so a
test never silently runs against a stale name. Three map sections state the
deliberate exceptions, each with a reason: ``name_overrides`` (a legacy dotted
name written as a given final one), ``accepted_unmapped`` (a name the final
package deliberately lacks; the statement is left and a ``test_text_edits`` row
removes it) and ``path_overrides`` (a path string with no final file).
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

REPO = Path(__file__).resolve().parents[1]
MAP_PATH = REPO / "tests" / "llm-kit" / "repoint-map.json"
DISPOSITIONS_DIR = REPO / "tests" / "llm-kit" / "repoint-dispositions"
PORTING_LEDGER = REPO / "tests" / "llm-kit" / "porting-ledger.json"
LLM_KIT_LIB = REPO / "packages" / "katsoft-llm-kit" / "src"
MIRROR_ROOT = REPO / "tmp"
FACADE_PKG = "legacy_names"
GUARD_MODULE = "llm_kit.bootstrap_guard"
SUITES = ("llm-scripting-kit", "content-pipeline-kit", "job-kit")
DISPOSITIONS = ("rewrite", "legacy-only")
KINDS = ("module", "facade-package", "facade-names", "forwarded", "data")

_ROOT_ALT = "llm_scripting_kit|content_pipeline|job_kit"


def _read(path: Path) -> bytes:
    """File bytes with CRLF folded to LF.

    The repository pins LF in the index (.gitattributes), but a working tree
    checked out under another setting can hold CRLF. Every hash and rewrite is
    taken over the LF form so the mirror and its hashes do not depend on the
    checkout that produced them.
    """
    return path.read_bytes().replace(b"\r\n", b"\n")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_map() -> dict:
    return json.loads(MAP_PATH.read_text(encoding="utf-8"))


def dump_json(path: Path, data: Any) -> None:
    text = json.dumps(data, indent=2, sort_keys=False, ensure_ascii=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("ascii"))


def git_files(*paths: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", *paths],
        cwd=REPO, capture_output=True, check=True,
    ).stdout.decode("utf-8")
    return sorted(p for p in out.split("\0") if p)


# --------------------------------------------------------------------------
# source positions
# --------------------------------------------------------------------------

def _line_starts(data: bytes) -> list[int]:
    starts = [0]
    for i, b in enumerate(data):
        if b == 0x0A:
            starts.append(i + 1)
    return starts


def _span(starts: list[int], node: ast.AST) -> tuple[int, int]:
    return (
        starts[node.lineno - 1] + node.col_offset,
        starts[node.end_lineno - 1] + node.end_col_offset,
    )


def apply_edits(data: bytes, edits: list[tuple[int, int, bytes]]) -> bytes:
    out = data
    last = len(data) + 1
    for start, end, repl in sorted(edits, key=lambda e: e[0], reverse=True):
        if end > last:
            raise ValueError("overlapping edits")
        out = out[:start] + repl + out[end:]
        last = start
    return out


# --------------------------------------------------------------------------
# string constants
# --------------------------------------------------------------------------

def _prose_ids(tree: ast.AST) -> set[int]:
    """Constants that are bare expression statements: docstrings and attribute docs."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                ids.add(id(node.value))
    return ids


def string_constants(tree: ast.AST) -> list[ast.Constant]:
    prose = _prose_ids(tree)
    found = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in prose
    ]
    found.sort(key=lambda n: (n.lineno, n.col_offset))
    return found


def site_key(relpath: str, value: str, ordinal: int) -> str:
    return f"{relpath}::{hashlib.sha1(value.encode('utf-8')).hexdigest()[:10]}::{ordinal}"


# --------------------------------------------------------------------------
# the legacy trees and the map
# --------------------------------------------------------------------------

def legacy_dotted(path: str) -> str:
    """Dotted legacy module name of a tracked package file (a package for ``__init__``)."""
    rel = path.split("/lib/", 1)[1]
    if rel.endswith("/__init__.py"):
        rel = rel[: -len("/__init__.py")]
    elif rel.endswith(".py"):
        rel = rel[:-3]
    return rel.replace("/", ".")


def legacy_package_files(mapping: Mapping) -> list[dict]:
    """Every tracked file of the three legacy packages with its classification."""
    rows = []
    for name, spec in mapping["packages"].items():
        for path in git_files(spec["source_dir"]):
            base = os.path.basename(path)
            if path in mapping["dropped"]:
                state = "dropped"
            elif path in mapping["excluded"]:
                state = "excluded"
            elif base.endswith(".py") or base in mapping["data_files"]:
                state = "module"
            else:
                raise SystemExit(
                    f"unclassified legacy package file: {path} (add it to "
                    f"'excluded' or 'dropped' in {MAP_PATH.name})"
                )
            rows.append({"legacy": path, "package": name, "state": state})
    return rows


class Unmapped(Exception):
    """A legacy name with no final home."""


class Names:
    """Resolve legacy dotted names to final ones, from the map's ``modules`` table."""

    def __init__(self, mapping: Mapping) -> None:
        self.mapping = mapping
        self.rows: dict[str, dict] = {}
        for path, row in mapping["modules"].items():
            if path.endswith(".py"):
                self.rows[legacy_dotted(path)] = row
        self.roots = tuple(mapping["packages"])
        self.guard_legacy = mapping["guard"]["legacy"]
        self.guard_module = mapping["guard"]["module"]

    # -- lookup ---------------------------------------------------------

    def prefix(self, dotted: str) -> tuple[Optional[str], list[str]]:
        """(longest dotted prefix that has a row, the remaining components)."""
        parts = dotted.split(".")
        for i in range(len(parts), 0, -1):
            head = ".".join(parts[:i])
            if head in self.rows:
                return head, parts[i:]
        return None, parts

    def children(self, facade: str) -> dict[str, str]:
        """child name -> import target (final module, or alias for a facade child)."""
        out = {}
        stem = facade + "."
        for name, row in self.rows.items():
            if name.startswith(stem) and "." not in name[len(stem):]:
                if row["kind"] in ("module", "facade-package", "facade-names", "forwarded"):
                    out[name[len(stem):]] = self.module_target(name)
        for dropped in self.mapping["dropped"]:
            if dropped.endswith(f"/{self.guard_legacy}.py") and legacy_dotted(dropped) == f"{facade}.{self.guard_legacy}":
                out[self.guard_legacy] = self.guard_module
        return dict(sorted(out.items()))

    def alias(self, dotted: str) -> str:
        return f"{FACADE_PKG}.{dotted}"

    def override(self, dotted: str) -> Optional[str]:
        entry = self.mapping.get("name_overrides", {}).get(dotted)
        return None if entry is None else entry["to"]

    def module_target(self, dotted: str) -> str:
        """The module to import for a legacy module or package named by itself."""
        head, rest = self.prefix(dotted)
        if head is None or rest:
            raise Unmapped(f"{dotted}: no legacy module")
        row = self.rows[head]
        kind = row["kind"]
        if kind == "module":
            return row["final"]
        if kind == "facade-package":
            return row["final"]
        if kind in ("facade-names", "forwarded"):
            return self.alias(head)
        raise Unmapped(f"{dotted}: {kind} module has no final import target")

    def dotted(self, dotted: str) -> str:
        """A legacy dotted name (module, or module plus attributes) as a final one."""
        if dotted == self.guard_legacy:
            return self.guard_module
        forced = self.override(dotted)
        if forced is not None:
            return forced
        head, rest = self.prefix(dotted)
        if head is None:
            raise Unmapped(f"{dotted}: no legacy module")
        row = self.rows[head]
        kind = row["kind"]
        if kind == "forwarded":
            return self.alias(head) + "".join("." + r for r in rest)
        if kind == "module":
            return row["final"] + "".join("." + r for r in rest)
        if kind == "facade-package":
            if row.get("dropped_names") and rest and rest[0] in row["dropped_names"]:
                raise Unmapped(f"{dotted}: {rest[0]} is a dropped name of {head}")
            return row["final"] + "".join("." + r for r in rest)
        if kind == "facade-names":
            if not rest:
                return self.alias(head)
            entry = row["names"].get(rest[0])
            if entry is None:
                if rest[0] in row.get("constants", {}):
                    return self.alias(head) + "".join("." + r for r in rest)
                raise Unmapped(f"{dotted}: {head} exports no name {rest[0]!r}")
            return entry[0] + "." + entry[1] + "".join("." + r for r in rest[1:])
        raise Unmapped(f"{dotted}: {head} is a {kind} module with no final home")

    def from_import(self, module: str, name: str) -> tuple[str, str, str]:
        """(kind, module, attribute) for ``from <module> import <name>``.

        kind is ``attr`` (``from module import attribute``) or ``module`` (the
        name is itself a module: ``import module as name``).
        """
        head, rest = self.prefix(module)
        if head is None or rest:
            raise Unmapped(f"{module}: no legacy module")
        row = self.rows[head]
        kind = row["kind"]
        child = module + "." + name
        # A name the legacy package binds to a callable or value wins over a
        # submodule of the same name (``from .adapter_capabilities import
        # adapter_capabilities`` rebinds the attribute the import created).
        bound_by_name = kind == "facade-names" and name in row["names"]
        if not bound_by_name and child in self.rows and self.rows[child]["kind"] in (
                "module", "facade-package", "facade-names", "forwarded"):
            return "module", self.module_target(child), ""
        if kind == "forwarded":
            return "attr", self.alias(head), name
        if kind in ("module", "facade-package"):
            if row.get("dropped_names") and name in row["dropped_names"]:
                raise Unmapped(f"{module}.{name}: dropped name of {head}")
            return "attr", row["final"], name
        if kind == "facade-names":
            entry = row["names"].get(name)
            if entry is None:
                if name in row.get("constants", {}):
                    return "attr", self.alias(head), name
                raise Unmapped(f"{module}.{name}: {head} exports no name {name!r}")
            return "attr", entry[0], entry[1]
        raise Unmapped(f"{module}.{name}: {head} is a {kind} module with no final home")


# --------------------------------------------------------------------------
# import rewriting
# --------------------------------------------------------------------------

def _alias_text(alias: ast.alias) -> str:
    return alias.name + (f" as {alias.asname}" if alias.asname else "")


def _is_legacy(name: str, names: Names) -> bool:
    return name.split(".")[0] in names.roots or name == names.guard_legacy


def rewrite_import_node(node: ast.AST, seg: bytes, names: Names, unmapped: list[str],
                        where: str) -> Optional[bytes]:
    """Replacement bytes for one import statement, or None to keep it."""
    if isinstance(node, ast.Import):
        if not any(_is_legacy(a.name, names) for a in node.names):
            return None
        pieces: list[str] = []
        for alias in node.names:
            if not _is_legacy(alias.name, names):
                pieces.append(f"import {_alias_text(alias)}")
                continue
            try:
                target = names.module_target(alias.name) if alias.name != names.guard_legacy \
                    else names.guard_module
            except Unmapped as exc:
                unmapped.append(f"{where}: {exc}")
                pieces.append(f"import {_alias_text(alias)}")
                continue
            if alias.asname:
                pieces.append(f"import {target} as {alias.asname}")
            elif alias.name == names.guard_legacy:
                pieces.append("from llm_kit import bootstrap_guard")
            else:
                top = alias.name.split(".")[0]
                if "." in alias.name:
                    pieces.append(f"import {target}")
                pieces.append(f"import {names.alias(top)} as {top}")
        return "; ".join(pieces).encode("utf-8")
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if node.level or not module or not _is_legacy(module, names):
            return None
        if module == names.guard_legacy:
            m = re.match(rb"(from\s+)bootstrap_guard(\s+import\b)", seg)
            if m is None:
                raise ValueError("unexpected guard import shape")
            return m.group(1) + names.guard_module.encode() + seg[m.end(1) + len("bootstrap_guard"):]
        pieces = []
        for alias in node.names:
            local = alias.asname or alias.name
            if alias.name == "*":
                unmapped.append(f"{where}: star import from {module}")
                return None
            if alias.name == names.guard_legacy and module in names.roots:
                pieces.append("from llm_kit import bootstrap_guard" + (f" as {alias.asname}" if alias.asname else ""))
                continue
            try:
                kind, target, attr = names.from_import(module, alias.name)
            except Unmapped as exc:
                unmapped.append(f"{where}: {exc}")
                return None
            if kind == "module":
                pieces.append(f"import {target} as {local}")
            elif attr == local:
                pieces.append(f"from {target} import {attr}")
            else:
                pieces.append(f"from {target} import {attr} as {local}")
        return "; ".join(pieces).encode("utf-8")
    return None


# --------------------------------------------------------------------------
# strings and paths
# --------------------------------------------------------------------------

def _code_like(value: str) -> bool:
    return bool(re.search(r"(^|\n)\s*(import|from)\s+\w", value)) or "sys.modules" in value \
        or "run_module(" in value or "import_module(" in value


def _is_module_name(value: str, names: Iterable[str]) -> bool:
    return re.fullmatch(rf"({'|'.join(names)})(\.\w+)*", value) is not None


_DOTTED_IN_QUOTES = re.compile(
    rf"(?P<q>['\"])(?P<name>(?:{_ROOT_ALT})(?:\.\w+)*)(?P=q)"
)
_QUOTED_PREFIX = re.compile(rf"(?P<q>['\"])(?P<name>(?:{_ROOT_ALT})(?:\.\w+)*\.)(?P=q)")
_FROM_STMT = re.compile(
    rf"(?P<lead>(?:^|;)[ \t]*)from[ \t]+(?P<mod>(?:{_ROOT_ALT}|bootstrap_guard)(?:\.\w+)*)[ \t]+import[ \t]+"
    rf"(?P<names>\([^)]*\)|[\w, \t]+?(?=;|\n|$))",
    re.M,
)
_IMPORT_INLINE = re.compile(
    rf"(?P<lead>(?:^|;)[ \t]*)import[ \t]+(?P<mod>(?:{_ROOT_ALT})(?:\.\w+)*)(?:[ \t]+as[ \t]+(?P<as>\w+))?(?=[ \t]*(?:;|\n|$))",
    re.M,
)


def rewrite_code_string(seg: str, names: Names, unmapped: list[str], where: str) -> str:
    """Rewrite module names inside a code string's source text.

    Only import statements and quoted module names change. A bare dotted use
    (``llm_scripting_kit.completion.X``) is left: the import statement binds the
    top name to the generated alias tree, which resolves it at run time.
    """
    def from_stmt(m: "re.Match[str]") -> str:
        module = m.group("mod")
        raw = m.group("names").strip()
        if raw.startswith("("):
            raw = raw[1:-1]
        if module == names.guard_legacy:
            return f"{m.group('lead')}from {names.guard_module} import {raw.strip()}"
        pieces = []
        for part in [p.strip() for p in raw.replace("\n", " ").split(",") if p.strip()]:
            name, _, local = part.partition(" as ")
            name, local = name.strip(), (local.strip() or name.strip())
            try:
                kind, target, attr = names.from_import(module, name)
            except Unmapped as exc:
                unmapped.append(f"{where}: {exc}")
                return m.group(0)
            if kind == "module":
                pieces.append(f"import {target} as {local}")
            elif attr == local:
                pieces.append(f"from {target} import {attr}")
            else:
                pieces.append(f"from {target} import {attr} as {local}")
        return m.group("lead") + "; ".join(pieces)

    def import_stmt(m: "re.Match[str]") -> str:
        dotted = m.group("mod")
        try:
            target = names.module_target(dotted)
        except Unmapped as exc:
            unmapped.append(f"{where}: {exc}")
            return m.group(0)
        if m.group("as"):
            return f"{m.group('lead')}import {target} as {m.group('as')}"
        top = dotted.split(".")[0]
        lead = m.group("lead")
        if "." in dotted:
            return f"{lead}import {target}; import {names.alias(top)} as {top}"
        return f"{lead}import {names.alias(top)} as {top}"

    def quoted(m: "re.Match[str]") -> str:
        try:
            return m.group("q") + names.dotted(m.group("name")) + m.group("q")
        except Unmapped as exc:
            unmapped.append(f"{where}: {exc}")
            return m.group(0)

    seg = _FROM_STMT.sub(from_stmt, seg)
    seg = _IMPORT_INLINE.sub(import_stmt, seg)
    seg = _DOTTED_IN_QUOTES.sub(quoted, seg)
    seg = re.sub(r"(?m)^([ \t]*)import[ \t]+bootstrap_guard[ \t]*$",
                 lambda mm: f"{mm.group(1)}from llm_kit import bootstrap_guard", seg)
    return seg


_PATH_CALLS = {"join", "joinpath", "Path", "PurePath", "PurePosixPath", "PureWindowsPath"}


def _is_path_segment(node: ast.AST, parents: Mapping[ast.AST, ast.AST]) -> bool:
    """True for a string literal used as one component of a filesystem path."""
    parent = parents.get(node)
    if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Div):
        return True
    if isinstance(parent, ast.Call):
        func = parent.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        return name in _PATH_CALLS and node in parent.args
    return False


def _qualnames(tree: ast.AST) -> dict[str, ast.AST]:
    """qualified name ("Class.test" or "test") -> function node, for tests."""
    out: dict[str, ast.AST] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[f"{prefix}{child.name}"] = child
            else:
                walk(child, prefix)

    walk(tree, "")
    return out


_PLUGIN_DIR_RE_TMPL = (
    r"(?P<q1>[\"'])plugins(?P=q1)(?P<sep>\s*(?:/|,)\s*)"
    r"(?P<q2>[\"'])(?P<name>{names})(?P=q2)"
)
# A legacy plugin's lib directory, spelled as path segments from `plugins`; the
# final library lives in the katsoft-llm-kit package, not under the plugin.
_PLUGIN_LIB_RE_TMPL = (
    r"(?P<q1>[\"'])plugins(?P=q1)(?P<sep>\s*(?:/|,)\s*)"
    r"(?P<q2>[\"'])(?:{names})(?P=q2)(?P<sep2>\s*(?:/|,)\s*)"
    r"(?P<q3>[\"'])lib(?P=q3)"
    rf"(?:(?P<sep3>\s*(?:/|,)\s*)(?P<q4>[\"'])(?:{_ROOT_ALT})(?P=q4))?"
)
_LIB_PKG_RE = re.compile(
    r"(?P<q1>[\"'])lib(?P=q1)(?P<sep>\s*(?:/|,)\s*)(?P<q2>[\"'])"
    rf"(?P<pkg>{_ROOT_ALT})(?P=q2)"
)
_SLASH_PATH_RE = re.compile(
    rf"plugins/(?P<plugin>llm-scripting-kit|content-pipeline-kit|job-kit)/lib"
    rf"(?:/(?P<pkg>{_ROOT_ALT})\b(?P<rest>(?:/[\w.\-]+)*))?"
)


def final_path(names: Names, pkg: str, rest: str) -> Optional[str]:
    """Final repo-relative path for a legacy package-relative file path, or None."""
    if not rest.endswith(".py"):
        return None
    legacy = pkg + rest[: -len(".py")].replace("/", ".")
    if legacy.endswith(".__init__"):
        legacy = legacy[: -len(".__init__")]
    row = names.rows.get(legacy)
    if row is None or row["kind"] not in ("module", "facade-package"):
        return None
    base = "packages/katsoft-llm-kit/src/" + row["final"].replace(".", "/")
    return base + ("/__init__.py" if row["kind"] == "facade-package" else ".py")


def rewrite_test_source(data: bytes, relpath: str, mapping: Mapping,
                        skips: Optional[Mapping[str, str]] = None,
                        unmapped: Optional[list[str]] = None) -> tuple[bytes, list[str]]:
    """Re-point one legacy test file at the final package. Returns (bytes, rewrite classes).

    One pass over the original syntax tree collects every edit (imports, module
    and code strings); a second regex pass rewrites the path shapes. A string
    that is a path component, or prose, is never treated as a module name. Every
    legacy name with no final home is appended to ``unmapped``.
    """
    names = Names(mapping)
    bad: list[str] = unmapped if unmapped is not None else []
    classes: set[str] = set()
    tree = ast.parse(data.decode("utf-8"))
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    starts = _line_starts(data)
    edits: list[tuple[int, int, bytes]] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            s, e = _span(starts, node)
            seg = data[s:e]
            repl = rewrite_import_node(node, seg, names, bad, f"{relpath}:{node.lineno}")
            if repl is not None and repl != seg:
                edits.append((s, e, repl))
                classes.add("import")

    overrides = mapping.get("test_site_overrides", {})
    seen: dict[str, int] = {}
    pattern = re.compile(rf"\b({_ROOT_ALT}|bootstrap_guard)\b")
    for node in string_constants(tree):
        value = node.value
        if not pattern.search(value):
            continue
        n = seen.get(value, 0)
        seen[value] = n + 1
        override = overrides.get(site_key(relpath, value, n))
        if override is not None and override["action"] == "keep":
            classes.add("string-kept")
            continue
        forced = override is not None and override["action"] == "rewrite"
        if not forced and _is_path_segment(node, parents):
            continue
        module_like = _is_module_name(value, names.roots)
        code_like = _code_like(value)
        if not (forced or module_like or code_like):
            classes.add("prose-kept")
            continue
        s, e = _span(starts, node)
        seg = data[s:e].decode("utf-8")
        where = f"{relpath}:{node.lineno}"
        if module_like:
            try:
                resolved = names.dotted(value)
            except Unmapped as exc:
                bad.append(f"{where}: {exc}")
                continue
            quote = seg[0]
            new_seg = f"{quote}{resolved}{quote}"
            classes.add("module-string")
        else:
            new_value = rewrite_code_string(value, names, bad, where)
            # the rewrite works on the evaluated text; the literal is re-emitted
            # from it, so statements split by an escaped newline are seen as lines
            new_seg = seg if new_value == value else repr(new_value)
            classes.add("code-string" if code_like else "named-message")
        if new_seg != seg:
            edits.append((s, e, new_seg.encode("utf-8")))
    if skips:
        defs = _qualnames(tree)
        for qual, reason in skips.items():
            node = defs.get(qual)
            if node is None:
                raise SystemExit(f"{relpath}: declared skip names no test: {qual}")
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])
            at = starts[first - 1]
            indent = " " * (node.col_offset)
            line = f'{indent}@__import__("pytest").mark.skip(reason={json.dumps(reason)})\n'
            edits.append((at, at, line.encode("utf-8")))
        classes.add("declared-skip")
    text = apply_edits(data, edits).decode("utf-8")

    plugin_names = "|".join(re.escape(spec["plugin"]) for spec in mapping["packages"].values())

    def plugin_lib(m: "re.Match[str]") -> str:
        classes.add("path-package-src")
        q1, q2, q3, sep = m.group("q1"), m.group("q2"), m.group("q3"), m.group("sep")
        out = (f"{q1}packages{q1}{sep}{q2}katsoft-llm-kit{q2}{m.group('sep2')}{q3}src{q3}")
        if m.group("q4"):
            q4 = m.group("q4")
            out += f"{m.group('sep3')}{q4}llm_kit{q4}"
        return out

    text = re.compile(_PLUGIN_LIB_RE_TMPL.format(names=plugin_names)).sub(plugin_lib, text)
    plugin_re = re.compile(_PLUGIN_DIR_RE_TMPL.format(names=plugin_names))

    def plugin_dir(m: "re.Match[str]") -> str:
        classes.add("path-plugin-dir")
        return f"{m.group('q1')}plugins{m.group('q1')}{m.group('sep')}{m.group('q2')}llm-kit{m.group('q2')}"

    text = plugin_re.sub(plugin_dir, text)

    def lib_pkg(m: "re.Match[str]") -> str:
        classes.add("path-package-dir")
        sep, q1, q2 = m.group("sep"), m.group("q1"), m.group("q2")
        # A bare `<plugin dir> / "lib" / <package>` (the plugin directory named
        # earlier, in another statement) climbs out of plugins/llm-kit to the
        # package source.
        return (f"{q1}..{q1}{sep}{q1}..{q1}{sep}{q1}packages{q1}{sep}{q1}katsoft-llm-kit{q1}{sep}"
                f"{q1}src{q1}{sep}{q2}llm_kit{q2}")

    text = _LIB_PKG_RE.sub(lib_pkg, text)

    def slash_path(m: "re.Match[str]") -> str:
        classes.add("path-string")
        if not m.group("pkg"):
            return "packages/katsoft-llm-kit/src"
        override = mapping.get("path_overrides", {}).get(m.group(0))
        if override is not None:
            return override["to"]
        rest = m.group("rest") or ""
        if not rest:
            return "packages/katsoft-llm-kit/src/llm_kit"
        target = final_path(names, m.group("pkg"), rest)
        if target is None:
            bad.append(f"{relpath}: path {m.group(0)} has no final file")
            return m.group(0)
        return target

    text = _SLASH_PATH_RE.sub(slash_path, text)
    text, edited = apply_text_edits(text, relpath, mapping)
    if edited:
        classes.add("text-edit")
    text, worded = apply_wording_edits(text, relpath, mapping)
    if worded:
        classes.add("wording-edit")
    return text.encode("utf-8"), sorted(classes)


# --------------------------------------------------------------------------
# edits a final module makes to a mirrored test
# --------------------------------------------------------------------------

def apply_text_edits(text: str, relpath: str, mapping: Mapping) -> tuple[str, bool]:
    """Apply the map's ``test_text_edits`` rows for one test file.

    Each row names an exact text, its replacement and a reason, and must match
    exactly ``count`` times (default once): a row that no longer matches fails
    the run instead of silently leaving the test pointed at a moved module.
    """
    changed = False
    for row in mapping.get("test_text_edits", []):
        if row["file"] != relpath:
            continue
        found = text.count(row["old"])
        want = row.get("count", 1)
        if found != want:
            raise SystemExit(
                f"{relpath}: test_text_edits row matches {found} time(s), expected {want}: "
                f"{row['old'][:70]!r}")
        text = text.replace(row["old"], row["new"])
        changed = True
    return text, changed


def apply_wording_edits(text: str, relpath: str, mapping: Mapping) -> tuple[str, bool]:
    """Apply the map's ``wording_edits`` to one test file.

    A wording edit states a message the final package words for llm-kit; it is
    applied wherever its text occurs, in every mirrored file. A rule that
    occurs in no file at all is stale and fails ``check``.
    """
    changed = False
    for row in mapping.get("wording_edits", []):
        if row["old"] in text:
            text = text.replace(row["old"], row["new"])
            changed = True
    return text, changed


# --------------------------------------------------------------------------
# the import-root guard (sitecustomize)
# --------------------------------------------------------------------------

def render_sitecustomize(mapping: Mapping) -> str:
    legacy_libs = sorted({
        spec["lib_dir"] for spec in mapping["packages"].values()
    })
    legacy_plugins = sorted({spec["plugin_dir"] for spec in mapping["packages"].values()})
    tops = sorted(list(mapping["packages"]) + ["job_kit_entrypoint"])
    return _SITECUSTOMIZE.format(
        repo=str(REPO).replace("\\", "/"),
        mirror=str(MIRROR_ROOT).replace("\\", "/"),
        libs=json.dumps(legacy_libs),
        plugins=json.dumps(legacy_plugins),
        tops=json.dumps(tops),
    )


_SITECUSTOMIZE = '''\
"""Import-root enforcement for the re-pointed legacy test mirror (generated).

Loaded by the parent pytest process and by every child interpreter that
inherits the mirror directory on PYTHONPATH. It rejects the three legacy
top-level packages and any legacy plugin directory on sys.path, and it keeps
the mirror directory on PYTHONPATH of every child process, including children
launched with an explicit environment.
"""
import importlib.abc
import os
import subprocess
import sys

_REPO = {repo!r}
_MIRROR = {mirror!r}
_LEGACY_LIBS = {libs}
_LEGACY_PLUGINS = {plugins}
_LEGACY_TOPS = frozenset({tops})


def _norm(path):
    try:
        return os.path.normcase(os.path.abspath(path)).replace("\\\\", "/")
    except (TypeError, ValueError):
        return ""


_LEGACY_PREFIXES = tuple(_norm(os.path.join(_REPO, p)) for p in _LEGACY_PLUGINS)
_MIRROR_NORM = _norm(_MIRROR)


def _is_legacy_path(path):
    n = _norm(path)
    return any(n == p or n.startswith(p + "/") for p in _LEGACY_PREFIXES)


def _scan_sys_path():
    for entry in sys.path:
        if entry and _is_legacy_path(entry):
            raise ImportError("legacy plugin directory on sys.path in a mirrored run: " + entry)


class _LegacyBlocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.partition(".")[0] in _LEGACY_TOPS:
            raise ImportError("legacy package imported in a mirrored run: " + fullname,
                              name=fullname)
        _scan_sys_path()
        return None


if not any(isinstance(f, _LegacyBlocker) for f in sys.meta_path):
    sys.meta_path.insert(0, _LegacyBlocker())


def _guard_value(value, where):
    text = os.fspath(value) if isinstance(value, os.PathLike) else value
    if not isinstance(text, str):
        return
    for part in text.split(os.pathsep):
        if part and _is_legacy_path(part):
            raise RuntimeError("legacy plugin path handed to a child process (" + where + "): " + part)


_orig_init = subprocess.Popen.__init__


def _patched_init(self, args, *a, **kw):
    argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
    for item in argv:
        if isinstance(item, (str, os.PathLike)):
            _guard_value(item, "argv")
    env = kw.get("env")
    if env is not None:
        env = dict(env)
        current = env.get("PYTHONPATH", "")
        _guard_value(current, "PYTHONPATH")
        parts = [p for p in current.split(os.pathsep) if p]
        if not any(_norm(p) == _MIRROR_NORM for p in parts):
            parts.append(_MIRROR)
        env["PYTHONPATH"] = os.pathsep.join(parts)
        kw["env"] = env
    _orig_init(self, args, *a, **kw)


if not getattr(subprocess.Popen.__init__, "_mirror_patched", False):
    _patched_init._mirror_patched = True
    subprocess.Popen.__init__ = _patched_init
'''


# --------------------------------------------------------------------------
# the alias tree (legacy package namespaces for the mirrored tests)
# --------------------------------------------------------------------------

_FACADE_HELPER = '''"""Alias-module support for the legacy package namespaces (generated).

Each module under ``legacy_names`` stands for one legacy package: it carries no
code of its own and resolves every attribute to the llm-kit object that now
holds it, by a lazy module ``__getattr__``. Test-only: nothing under ``plugins/``
imports it.
"""
import importlib
import sys


def install(name, final=None, names=None, children=None, constants=None):
    module = sys.modules[name]
    names = names or {}
    children = children or {}
    constants = constants or {}

    def __getattr__(attr):
        if attr in constants:
            return constants[attr]
        if attr in names:
            owner, original = names[attr]
            return getattr(importlib.import_module(owner), original)
        if attr in children:
            return importlib.import_module(children[attr])
        if final is not None:
            return getattr(importlib.import_module(final), attr)
        raise AttributeError(f"module {name!r} has no attribute {attr!r}")

    module.__getattr__ = __getattr__


def forwarded_module(name, homes, group=None, verbs=None, passthrough=()):
    """Make ``name`` a view of the CLI group modules ``homes``.

    Reads resolve in the first home that binds the attribute; a write or delete
    goes to every home that binds it, so a test that patches ``cli.create_backend``
    patches the code that runs. ``main`` translates a legacy argv (a bare verb, or
    job-kit's verbs under ``group``) to the llm-kit form and runs llm-kit's entry.
    """
    import types

    module = sys.modules[name]
    verbs = verbs or {}

    def owners(attr):
        found = []
        for home in homes:
            mod = importlib.import_module(home)
            if hasattr(mod, attr):
                found.append(mod)
        return found

    def translate(argv):
        argv = list(argv)
        if group is not None:
            return [group, *argv]
        if argv and argv[0] in verbs:
            owner, verb = verbs[argv[0]]
            return [owner, verb, *argv[1:]]
        if argv and argv[0] in passthrough:
            return argv
        return argv

    def main(argv=None):
        entry = importlib.import_module("llm_kit.cli")
        if argv is None:
            saved = sys.argv
            sys.argv = [saved[0], *translate(saved[1:])]
            try:
                return entry.main(None)
            finally:
                sys.argv = saved
        return entry.main(translate(argv))

    class Forwarded(types.ModuleType):
        def __getattr__(self, attr):
            if attr.startswith("__") and attr.endswith("__"):
                raise AttributeError(attr)
            found = owners(attr)
            if not found:
                raise AttributeError(f"module {name!r} has no attribute {attr!r}")
            return getattr(found[0], attr)

        def __setattr__(self, attr, value):
            found = owners(attr) if not (attr.startswith("__") and attr.endswith("__")) else []
            if not found:
                super().__setattr__(attr, value)
            for mod in found:
                setattr(mod, attr, value)

        def __delattr__(self, attr):
            found = owners(attr)
            if not found:
                super().__delattr__(attr)
            for mod in found:
                delattr(mod, attr)

    module.__class__ = Forwarded
    object.__setattr__(module, "main", main)
'''


def build_facades(mapping: Mapping) -> dict[str, tuple[bytes, list[str]]]:
    """Mirror-relative path -> (bytes, []) for the alias tree."""
    names = Names(mapping)
    files: dict[str, tuple[bytes, list[str]]] = {
        f"{FACADE_PKG}/__init__.py": (b"", []),
        f"{FACADE_PKG}/_facade.py": (_FACADE_HELPER.encode("ascii"), []),
    }
    for dotted, row in names.rows.items():
        if row["kind"] == "forwarded":
            args = [json.dumps(row["homes"])]
            if row.get("group"):
                args.append(f"group={row['group']!r}")
            if row.get("verbs"):
                args.append(f"verbs={json.dumps(row['verbs'], sort_keys=True)}")
            if row.get("passthrough"):
                args.append(f"passthrough={json.dumps(row['passthrough'])}")
            files[f"{FACADE_PKG}/{dotted.replace('.', '/')}.py"] = ((
                f'"""Forwarding view of the legacy CLI module ``{dotted}`` (generated by scripts/llm_kit_repoint.py)."""\n'
                f"from {FACADE_PKG}._facade import forwarded_module\n\n"
                f"forwarded_module(__name__, {', '.join(args)})\n"
            ).encode("ascii"), [])
            continue
        if row["kind"] not in ("facade-package", "facade-names"):
            continue
        body = [
            f'"""Alias for the legacy package ``{dotted}`` (generated by scripts/llm_kit_repoint.py)."""',
            f"from {FACADE_PKG}._facade import install",
            "",
            "install(",
            "    __name__,",
        ]
        if row["kind"] == "facade-package":
            body.append(f"    final={row['final']!r},")
        else:
            body.append(f"    names={json.dumps(row['names'], sort_keys=True)},")
            if row.get("constants"):
                body.append(f"    constants={json.dumps(row['constants'], sort_keys=True)},")
        body.append(f"    children={json.dumps(names.children(dotted), sort_keys=True)},")
        body.append(")")
        files[f"{FACADE_PKG}/{dotted.replace('.', '/')}/__init__.py"] = (
            ("\n".join(body) + "\n").encode("ascii"), [])
    return files


# --------------------------------------------------------------------------
# the mirror
# --------------------------------------------------------------------------

def load_dispositions() -> dict[str, dict]:
    out = {}
    for suite in SUITES:
        path = DISPOSITIONS_DIR / f"{suite}.json"
        out[suite] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {
            "suite": suite, "rows": []}
    return out


def build_mirror(mapping: Mapping, dispositions: Mapping[str, Mapping],
                 unmapped: Optional[list[str]] = None) -> dict[str, tuple[bytes, list[str]]]:
    """mirror path (relative to MIRROR_ROOT) -> (bytes, classes)."""
    files: dict[str, tuple[bytes, list[str]]] = {}
    bad: list[str] = unmapped if unmapped is not None else []
    for suite in SUITES:
        led = dispositions[suite]
        for row in led["rows"]:
            if row["disposition"] != "rewrite":
                continue
            src = REPO / row["path"]
            rel = f"{suite}/{src.name}"
            skips = {}
            for node_id, entry in row.get("declared_differences", {}).items():
                if entry["mirror_status"] == "skipped" and entry.get("injected", True):
                    nested = node_id.split("::", 1)[1].split("[")[0].replace("::", ".")
                    skips[nested] = entry["reason"]
            files[rel] = rewrite_test_source(_read(src), row["path"], mapping, skips, bad)
        conftest = REPO / "tests" / suite / "conftest.py"
        files[f"{suite}/conftest.py"] = rewrite_test_source(
            _read(conftest), f"tests/{suite}/conftest.py", mapping, None, bad)
    files["conftest.py"] = (_read(REPO / "tests" / "conftest.py"), [])
    for p in sorted((REPO / "tests" / "test_support").rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            files["test_support/" + p.relative_to(REPO / "tests" / "test_support").as_posix()] = (
                _read(p), [])
    files["sitecustomize.py"] = (render_sitecustomize(mapping).encode("utf-8"), [])
    files.update(build_facades(mapping))
    for rule in mapping.get("wording_edits", []):
        if not any(rule["new"] in data.decode("utf-8", "replace") for rel, (data, _c) in files.items()
                   if rel.endswith(".py") and "/" in rel):
            bad.append(f"wording_edits rule applies nowhere: {rule['old'][:60]!r}")
    kept = filter_accepted(mapping, bad)
    if unmapped is None:
        if kept:
            raise SystemExit(_unmapped_report(kept))
    else:
        unmapped[:] = kept
    return files


def filter_accepted(mapping: Mapping, bad: list[str]) -> list[str]:
    """``bad`` without the names the map's ``accepted_unmapped`` rows state deliberately lack a home."""
    kept = list(bad)
    for row in mapping.get("accepted_unmapped", []):
        hit = [b for b in kept if b.startswith(row["file"] + ":") and f" {row['name']}:" in b]
        if not hit:
            raise SystemExit(f"accepted_unmapped row matches nothing: {row['file']} {row['name']}")
        kept = [b for b in kept if b not in hit]
    return kept


def _unmapped_report(bad: list[str]) -> str:
    lines = sorted(set(bad))
    return (f"{len(lines)} legacy name(s) with no final home in rewrite files "
            "(dispose of the file as legacy-only, add the name to the map, or fix the test):\n  "
            + "\n  ".join(lines))


def write_mirror(files: Mapping[str, tuple[bytes, list[str]]]) -> None:
    """Write the mirror idempotently: unchanged files are left alone and stale
    ones removed, so concurrent callers (parallel test workers) never see a
    half-deleted tree."""
    for rel, (data, _classes) in files.items():
        target = MIRROR_ROOT / rel
        if target.is_file() and target.read_bytes() == data:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    wanted = {MIRROR_ROOT / rel for rel in files}
    for managed in [MIRROR_ROOT / s for s in SUITES] + [MIRROR_ROOT / "test_support", MIRROR_ROOT / FACADE_PKG]:
        if not managed.is_dir():
            continue
        for path in managed.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path not in wanted:
                path.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# sync / check
# --------------------------------------------------------------------------

def collect_counts(suite: str) -> dict[str, int]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", f"tests/{suite}", "--collect-only", "-q",
         "-pno:cacheprovider"],
        cwd=REPO, capture_output=True, text=True,
    )
    counts: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        if "::" in line and line.startswith("tests/"):
            path = line.split("::", 1)[0]
            counts[path] = counts.get(path, 0) + 1
    if proc.returncode not in (0,):
        raise SystemExit(f"collection of tests/{suite} failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    return counts


def cmd_sync(args: argparse.Namespace) -> int:
    mapping = load_map()
    dispositions = load_dispositions()
    if not args.no_counts:
        for suite in SUITES:
            counts = collect_counts(suite)
            led = dispositions[suite]
            known = {r["path"] for r in led["rows"]}
            for path in sorted(counts):
                if path not in known:
                    led["rows"].append({
                        "path": path, "disposition": "rewrite", "reason": "",
                        "rewrite_classes": [], "collection_count": 0, "baseline_failures": [],
                    })
            for row in led["rows"]:
                row["collection_count"] = counts.get(row["path"], 0)
            led["rows"].sort(key=lambda r: r["path"])
            led["collection_total"] = sum(r["collection_count"] for r in led["rows"])
    mirror = build_mirror(mapping, dispositions)
    for suite in SUITES:
        for row in dispositions[suite]["rows"]:
            if row["disposition"] == "rewrite":
                src = REPO / row["path"]
                row["rewrite_classes"] = mirror[f"{suite}/{src.name}"][1]
            else:
                row["rewrite_classes"] = []
        dump_json(DISPOSITIONS_DIR / f"{suite}.json", dispositions[suite])
    write_mirror(mirror)
    print(f"mirror: {len(mirror)} files")
    return 0


def cmd_clean(_args: argparse.Namespace) -> int:
    """Remove exactly the files the mirror owns under tmp/."""
    for suite in SUITES:
        shutil.rmtree(MIRROR_ROOT / suite, ignore_errors=True)
    shutil.rmtree(MIRROR_ROOT / "test_support", ignore_errors=True)
    shutil.rmtree(MIRROR_ROOT / FACADE_PKG, ignore_errors=True)
    for name in ("conftest.py", "sitecustomize.py"):
        (MIRROR_ROOT / name).unlink(missing_ok=True)
    return 0


def cmd_mirror(_args: argparse.Namespace) -> int:
    mapping = load_map()
    write_mirror(build_mirror(mapping, load_dispositions()))
    return 0


def cmd_report(_args: argparse.Namespace) -> int:
    bad: list[str] = []
    build_mirror(load_map(), load_dispositions(), bad)
    print(_unmapped_report(bad) if bad else "every rewrite file maps")
    return 0


def final_file(final: str) -> Optional[Path]:
    """The source file of a final dotted module, or None."""
    base = LLM_KIT_LIB / final.replace(".", "/")
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def _bound_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        bound.add(n.id)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                bound.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
    return bound


def check_problems() -> list[str]:
    """Disagreements between the legacy trees, the map and the final tree."""
    mapping = load_map()
    problems: list[str] = []
    if mapping.get("schema") != 2:
        problems.append("repoint-map.json is not schema 2")
    if (LLM_KIT_LIB / "llm_kit" / "_copy").exists():
        problems.append("the quarantined copy llm_kit/_copy still exists")
    table = mapping["modules"]
    legacy = {r["legacy"]: r for r in legacy_package_files(mapping) if r["state"] == "module"}
    for path in sorted(set(legacy) - set(table)):
        problems.append(f"module missing from the map: {path}")
    for path in sorted(set(table) - set(legacy)):
        problems.append(f"map lists a module the legacy tree no longer has: {path}")
    ledger = json.loads(PORTING_LEDGER.read_text(encoding="utf-8")) if PORTING_LEDGER.is_file() \
        else {"entries": []}
    for path, row in sorted(table.items()):
        if path not in legacy:
            continue
        if row["kind"] not in KINDS:
            problems.append(f"{path}: unknown kind {row['kind']!r}")
            continue
        current = _sha(_read(REPO / path))
        if current != row["legacy_sha256"] and not any(
                e["legacy_path"] == path and e["legacy_sha256"] == current for e in ledger["entries"]):
            problems.append(f"legacy module edited since the map recorded it, and the edit is not in "
                            f"{PORTING_LEDGER.name}: {path}")
        finals: list[tuple[str, Optional[str]]] = []
        if row["kind"] in ("module", "facade-package"):
            finals.append((row["final"], None))
        elif row["kind"] == "facade-names":
            finals.extend((m, a) for m, a in row["names"].values())
        elif row["kind"] == "forwarded":
            finals.extend((m, None) for m in row["homes"])
        elif row["kind"] == "data" and row.get("final") is not None:
            problems.append(f"{path}: a data file names a final module")
        for module, attr in finals:
            found = final_file(module)
            if found is None:
                problems.append(f"{path}: final module {module} does not exist")
            elif attr is not None and attr not in _bound_names(found):
                problems.append(f"{path}: final module {module} binds no {attr!r}")
    return problems


def cmd_check(_args: argparse.Namespace) -> int:
    problems = check_problems()
    for p in problems:
        print(p, file=sys.stderr)
    return 1 if problems else 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("sync", help="refresh collection counts and mirror")
    p.add_argument("--no-counts", action="store_true", help="skip pytest collection counts")
    p.set_defaults(func=cmd_sync)
    sub.add_parser("mirror", help="regenerate the mirror only").set_defaults(func=cmd_mirror)
    sub.add_parser("clean", help="remove the mirror under tmp/").set_defaults(func=cmd_clean)
    sub.add_parser("check", help="verify the map against the legacy trees and the final tree").set_defaults(func=cmd_check)
    sub.add_parser("report", help="print legacy names with no final home").set_defaults(func=cmd_report)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
