"""Report which content_pipeline names a consumer uses are deprecated or removed.

Usage (stdlib only; run with the bootstrap interpreter):

    "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" \
        "${CONTENT_PIPELINE_KIT_ROOT:?requires a bootstrap engine pass; run bootstrap run}/scripts/check_consumer_contract.py" <path> [<path> ...]

Each <path> is a Python file or a directory scanned recursively. The listing
read is contract/public-surface.json under the plugin root; pass --contract to
read another copy.

Exit status: 1 when any removed name is used, 0 otherwise. --strict also
exits 1 for deprecated names. Exit 2 means the contract could not be read.

Detection is static. It sees `from content_pipeline.x import name`,
`import content_pipeline.x as m` followed by `m.name`, and, for a removed
Class.attr entry, a file that imports Class and also uses `attr` as an
attribute or keyword argument. Dynamic access (getattr, string imports) is
not seen.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

PACKAGE = "content_pipeline"
DEFAULT_CONTRACT = Path(__file__).resolve().parent.parent / "contract" / "public-surface.json"


def load_contract(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {e["name"]: e for e in data["entries"]}


def iter_py_files(paths):
    for p in paths:
        p = Path(p)
        if p.is_dir():
            yield from sorted(f for f in p.rglob("*.py") if ".venv" not in f.parts)
        elif p.suffix == ".py" and p.is_file():
            yield p


def _is_pkg(module: str) -> bool:
    return module == PACKAGE or module.startswith(PACKAGE + ".")


def _module_aliases(tree: ast.AST) -> dict[str, str]:
    """Local name -> dotted content_pipeline name it is bound to."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if _is_pkg(a.name):
                    aliases[a.asname or a.name.split(".")[0]] = a.name if a.asname else PACKAGE
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if _is_pkg(node.module):
                for a in node.names:
                    aliases[a.asname or a.name] = node.module + "." + a.name
    return aliases


def used_names(tree: ast.AST) -> list[tuple[str, int]]:
    """Dotted content_pipeline names the tree refers to, with line numbers."""
    aliases = _module_aliases(tree)
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if _is_pkg(node.module):
                for a in node.names:
                    found.append((node.module + "." + a.name, node.lineno))
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith(PACKAGE + "."):
                    found.append((a.name, node.lineno))
        elif isinstance(node, ast.Attribute):
            chain = []
            cur: ast.AST = node
            while isinstance(cur, ast.Attribute):
                chain.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name) and cur.id in aliases:
                found.append((".".join([aliases[cur.id]] + chain[::-1]), node.lineno))
    return found


def _attr_uses(tree: ast.AST) -> dict[str, int]:
    """Attribute and keyword-argument names used anywhere, with first line."""
    uses: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            uses.setdefault(node.attr, node.lineno)
        elif isinstance(node, ast.keyword) and node.arg:
            uses.setdefault(node.arg, getattr(node.value, "lineno", 0))
    return uses


def scan_file(path: Path, contract: dict[str, dict]) -> list[dict]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []
    hits: list[dict] = []
    seen: set[tuple[str, int]] = set()
    names = used_names(tree)
    flagged = {n: e for n, e in contract.items() if e["status"] != "public"}
    for name, line in names:
        for listed, entry in flagged.items():
            if name == listed or name.startswith(listed + "."):
                if (listed, line) not in seen:
                    seen.add((listed, line))
                    hits.append({"file": str(path), "line": line, "name": listed, "entry": entry})
    attrs = _attr_uses(tree)
    imported = {n for n, _ in names}
    for listed, entry in flagged.items():
        owner, _, attr = listed.rpartition(".")
        if owner in imported and attr in attrs:
            if (listed, attrs[attr]) not in seen:
                seen.add((listed, attrs[attr]))
                hits.append({"file": str(path), "line": attrs[attr], "name": listed,
                             "entry": entry, "heuristic": True})
    return hits


def describe(hit: dict) -> str:
    e = hit["entry"]
    if e["status"] == "removed":
        text = f"REMOVED since {e.get('since', '?')}: {e.get('replacement', '')}"
    else:
        text = (f"DEPRECATED since {e.get('since', '?')}, removed after "
                f"{e.get('remove_after', '?')}: {e.get('replacement', '')}")
    note = " (attribute/keyword match)" if hit.get("heuristic") else ""
    return f"{hit['file']}:{hit['line']}: {hit['name']}{note} -- {text}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="+", help="Python files or directories to scan")
    ap.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    ap.add_argument("--strict", action="store_true", help="exit 1 for deprecated names too")
    args = ap.parse_args(argv)
    try:
        contract = load_contract(args.contract)
    except (OSError, ValueError, KeyError) as exc:
        print(f"cannot read contract {args.contract}: {exc}", file=sys.stderr)
        return 2
    hits = [h for f in iter_py_files(args.paths) for h in scan_file(f, contract)]
    for h in hits:
        print(describe(h))
    removed = sum(1 for h in hits if h["entry"]["status"] == "removed")
    deprecated = len(hits) - removed
    print(f"{removed} removed, {deprecated} deprecated name use(s)")
    return 1 if removed or (args.strict and deprecated) else 0


if __name__ == "__main__":
    sys.exit(main())
