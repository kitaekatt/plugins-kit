"""Source guard: the spend ledger opens its transactions through the shared helper.

content-pipeline-kit's guard counted the ``BEGIN`` literals in the ledger module
(one plain ``BEGIN`` in the reader, ``BEGIN IMMEDIATE`` in the writer and in
``create_ledger``). In llm-kit the literals for the two scopes live in
``llm_kit.storage.transaction`` and the ledger opens them by name, so the same
property is asserted over the call sites:

* the write scope (``BEGIN IMMEDIATE``) is opened in ``_writer`` and the schema
  create (``create_ledger``), nowhere else;
* the one read scope (a plain ``BEGIN``) is opened in ``_reader`` alone, and
  ``_reader`` executes no write statement;
* ``_sweep_expired`` opens no transaction of its own (it runs in the caller's);
* the two literals are the ones the legacy ledger carried.

The legacy test is dispositioned as a declared difference (skipped) in the
content-pipeline-kit ledger and points here.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LEDGER = REPO / "packages" / "katsoft-llm-kit" / "src" / "llm_kit" / "pipeline" / "runtime" / "spend_ledger.py"
TRANSACTION = REPO / "packages" / "katsoft-llm-kit" / "src" / "llm_kit" / "storage" / "transaction.py"
_WRITE = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b", re.I)


def _owners(tree: ast.AST) -> dict[int, str]:
    """node id -> name of the innermost function containing it."""
    owners: dict[int, str] = {}

    def walk(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else owner
            owners[id(child)] = inner
            walk(child, inner)

    walk(tree, "<module>")
    return owners


def scan(source: str) -> dict[str, list[tuple[str, str]]]:
    """Sites by kind: literal BEGIN strings, write_transaction calls, read_transaction calls, write texts."""
    tree = ast.parse(source)
    owners = _owners(tree)
    docstrings = {
        id(n.value) for n in ast.walk(tree)
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
    }
    sites: dict[str, list[tuple[str, str]]] = {"begin": [], "write_scope": [], "read_scope": [], "write_text": []}
    for node in ast.walk(tree):
        owner = owners.get(id(node), "<module>")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            text = node.value.strip()
            if text.upper().startswith("BEGIN"):
                sites["begin"].append((owner, text))
            elif _WRITE.search(text):
                sites["write_text"].append((owner, text))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "write_transaction":
                sites["write_scope"].append((owner, "write_transaction"))
            elif node.func.attr == "read_transaction":
                sites["read_scope"].append((owner, "read_transaction"))
    return {key: sorted(value) for key, value in sites.items()}


def test_the_write_scope_is_opened_in_the_writer_and_the_schema_create_only():
    sites = scan(LEDGER.read_text(encoding="utf-8"))
    assert sites["write_scope"] == [("_writer", "write_transaction")], sites["write_scope"]
    assert sites["begin"] == [("create_ledger", "BEGIN IMMEDIATE")], sites["begin"]


def test_the_one_read_scope_is_the_reader_and_the_reader_writes_nothing():
    sites = scan(LEDGER.read_text(encoding="utf-8"))
    assert sites["read_scope"] == [("_reader", "read_transaction")], sites["read_scope"]
    reader_writes = [text for owner, text in sites["write_text"] if owner == "_reader"]
    assert reader_writes == [], f"_reader executes write statements: {reader_writes}"


def test_sweep_expired_opens_no_transaction_of_its_own():
    sites = scan(LEDGER.read_text(encoding="utf-8"))
    opened = [s for kind in ("begin", "write_scope", "read_scope") for s in sites[kind] if s[0] == "_sweep_expired"]
    assert opened == [], f"_sweep_expired opens its own transaction: {opened}"


def test_the_two_scopes_use_the_literals_the_legacy_ledger_carried():
    text = TRANSACTION.read_text(encoding="utf-8")
    assert 'BEGIN_WRITE = "BEGIN IMMEDIATE"' in text
    assert 'BEGIN_READ = "BEGIN"' in text
    assert re.search(r"def write_transaction.*?return transaction\(connect, BEGIN_WRITE", text, re.S)
    assert re.search(r"def read_transaction.*?connect, BEGIN_READ", text, re.S)


def test_the_scan_detects_a_plain_begin_in_a_writer():
    """The guard can fail: a writer that opens a plain BEGIN, and a reader that opens the write scope."""
    broken = (
        "class L:\n"
        "    def _writer(self):\n"
        "        self.conn.execute('BEGIN')\n"
        "    def _reader(self):\n"
        "        with _transaction.write_transaction(self._connect):\n"
        "            self.conn.execute('UPDATE halt SET halted = 1')\n"
    )
    sites = scan(broken)
    assert ("_writer", "BEGIN") in sites["begin"]
    assert sites["write_scope"] == [("_reader", "write_transaction")]
    assert [text for owner, text in sites["write_text"] if owner == "_reader"]
