"""Differential parity: llm_kit.pipeline.execution vs the legacy content_pipeline.execution.

The legacy side is ``content_pipeline.execution`` (plugins/content-pipeline-kit/lib)
over the rest of ``content_pipeline`` and ``llm_scripting_kit``, imported with a
sys.path insert scoped to a module fixture. The final side is
``llm_kit.pipeline.execution`` over ``llm_kit.pipeline.{core,artifacts,runtime}``,
``llm_kit.execution`` and ``llm_kit.storage``. Every scenario is a function of a
"side" and runs on both with the same inputs; the test asserts the two outcomes
are equal, results and exceptions (type chain, message, attributes) alike.

Nothing here starts a real ``claude`` or ``codex`` process or makes a network
call: the background-session driver runs against a scripted fake ``runner``
(the module's sole process boundary), the inline driver against ``generate``
callables, and HOME / USERPROFILE point at a temp directory for every test.

What is compared
----------------

* ledger bytes: the product DDL (``_MIGRATIONS``), the fresh-create and
  every-migration-prefix ``sqlite_master`` / ``schema_version`` dumps (against
  committed fixtures under ``fixtures/pipeline_schema/`` that must be byte-equal
  between the two sides), and the statement order every connection issues (the
  pragma order);
* the registered lease and fence behaviours: one dispatcher lease and fence per
  run (a different live holder is refused), expiry or release permits a higher
  fence, expiring unit claims with monotonic fences, stale unit and dispatcher
  fences refuse mutation, distinct runs proceed concurrently;
* interrupts, event projection bytes, status digests, waves and the parallel
  graph strategy rules, the controller, the protocol verbs, the inline driver
  and the background-session driver;
* the modules the move carries verbatim are compared as syntax trees (imports
  and docstrings aside), so a drivers or protocol edit cannot pass unseen.

Deliberate differences, each pinned by name in ``TestDeliberateDifferences``:

* the interrupt validator loader names ``llm_kit.schema.json_schema`` where the
  legacy module named the ``llm_scripting_kit`` validator;
* ``_atomic_json`` is ``llm_kit.storage.atomic_json`` (one module, two names in
  the quarantined copy).
"""

from __future__ import annotations

import ast
import dataclasses
import enum
import importlib
import json
import os
import re
import sqlite3
import sys
import threading
import types
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import pytest

from final_wording import floor_to_final

_REPO = Path(__file__).resolve().parents[2]
_CPK_LIB = _REPO / "plugins" / "content-pipeline-kit" / "lib"
_LSK_LIB = _REPO / "plugins" / "llm-scripting-kit" / "lib"
_LIB = _REPO / "plugins" / "llm-kit" / "lib"
_FINAL_DIR = _LIB / "llm_kit" / "pipeline" / "execution"
_LEGACY_DIR = _CPK_LIB / "content_pipeline" / "execution"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pipeline_schema"

_LEGACY_ROOTS = ("content_pipeline", "llm_scripting_kit", "bootstrap_guard")

#: The modules of the package, as (name, legacy dotted, final dotted).
_MODULES = (
    "adapter", "controller", "deadlines", "events", "failure_cache", "interrupts",
    "model", "parallel_graph", "protocol", "scheduler", "status", "store", "wave",
    "workerpack",
)


# --------------------------------------------------------------------------
# Loading both sides
# --------------------------------------------------------------------------


def _imp(name: str) -> Any:
    return importlib.import_module(name)


def _legacy_side() -> types.SimpleNamespace:
    ns = {m: _imp(f"content_pipeline.execution.{m}") for m in _MODULES}
    return types.SimpleNamespace(
        name="legacy",
        package=_imp("content_pipeline.execution"),
        inline=_imp("content_pipeline.execution.drivers.inline"),
        claude_bg=_imp("content_pipeline.execution.drivers.claude_bg"),
        drivers=_imp("content_pipeline.execution.drivers"),
        workunit=_imp("content_pipeline.pipeline.workunit"),
        gate=_imp("content_pipeline.pipeline.gate"),
        contract=_imp("content_pipeline.validate.contract"),
        platform=_imp("content_pipeline.llm.platform"),
        classify=_imp("content_pipeline.freshness.classify"),
        prefix="content_pipeline.execution",
        **ns,
    )


def _final_side() -> types.SimpleNamespace:
    ns = {m: _imp(f"llm_kit.pipeline.execution.{m}") for m in _MODULES}
    return types.SimpleNamespace(
        name="final",
        package=_imp("llm_kit.pipeline.execution"),
        inline=_imp("llm_kit.pipeline.execution.drivers.inline"),
        claude_bg=_imp("llm_kit.pipeline.execution.drivers.claude_bg"),
        drivers=_imp("llm_kit.pipeline.execution.drivers"),
        workunit=_imp("llm_kit.pipeline.core.workunit"),
        gate=_imp("llm_kit.pipeline.core.gate"),
        contract=_imp("llm_kit.pipeline.core.validate.contract"),
        platform=_imp("llm_kit.pipeline.runtime.platform"),
        classify=_imp("llm_kit.pipeline.artifacts.freshness.classify"),
        prefix="llm_kit.pipeline.execution",
        **ns,
    )


@pytest.fixture(scope="module")
def sides():
    """(legacy, final); legacy modules are removed from ``sys.modules`` afterwards."""
    before = set(sys.modules)
    saved = list(sys.path)
    for entry in (str(_LSK_LIB), str(_CPK_LIB)):
        sys.path.insert(0, entry)
    try:
        legacy = _legacy_side()
        final = _final_side()
        yield legacy, final
    finally:
        sys.path[:] = saved
        for name in list(sys.modules):
            if name not in before and name.split(".")[0] in _LEGACY_ROOTS:
                del sys.modules[name]


@pytest.fixture(autouse=True)
def _scratch_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """HOME and USERPROFILE point at a temp directory for every test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CLAUDE_BOOTSTRAP_DATA_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_TEST", raising=False)
    return home


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------


def plain(value: Any, root: str = "") -> Any:
    """Reduce a value to plain data, so two sides' classes compare by shape."""
    if isinstance(value, enum.Enum):
        return {"enum": type(value).__name__, "value": plain(value.value, root)}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        out = {"__type__": type(value).__name__}
        for field in dataclasses.fields(value):
            out[field.name] = plain(getattr(value, field.name), root)
        return out
    if isinstance(value, BaseException):
        return exc_shape(value, root)
    if isinstance(value, Mapping):
        return {str(k): plain(v, root) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (set, frozenset)):
        return sorted((plain(v, root) for v in value), key=repr)
    if isinstance(value, (list, tuple)):
        return [plain(v, root) for v in value]
    if isinstance(value, Path):
        return _scrub(str(value), root)
    if isinstance(value, str):
        return _scrub(value, root)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    return {"repr": _scrub(repr(value), root), "type": type(value).__name__}


def _scrub(text: str, root: str) -> str:
    if root:
        text = text.replace(root, "<ROOT>").replace(root.replace("\\", "/"), "<ROOT>")
    return text


def exc_shape(exc: BaseException, root: str = "") -> dict:
    """Type chain, message and plain attributes of an exception."""
    attrs = {}
    for key, val in sorted(vars(exc).items()):
        if key.startswith("__"):
            continue
        attrs[key] = plain(val, root)
    return {
        "mro": [c.__name__ for c in type(exc).__mro__],
        "message": _scrub(str(exc), root),
        "attrs": attrs,
    }


def outcome(fn: Callable[[], Any], root: str = "") -> dict:
    """A call's result, or the shape of what it raised."""
    try:
        return {"ok": plain(fn(), root)}
    except Exception as exc:  # noqa: BLE001 -- the outcome IS the raised exception
        return {"raised": exc_shape(exc, root)}


def first_difference(a: Any, b: Any, path: str = "$") -> Optional[str]:
    if type(a) is not type(b):
        return f"{path}: {a!r} != {b!r}"
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                return f"{path}.{key}: present on one side only ({a.get(key)!r} vs {b.get(key)!r})"
            diff = first_difference(a[key], b[key], f"{path}.{key}")
            if diff:
                return diff
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: length {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            diff = first_difference(x, y, f"{path}[{i}]")
            if diff:
                return diff
        return None
    return None if a == b else f"{path}: {a!r} != {b!r}"


_BINDING_FAILURE = re.compile(
    r"positional argument|unexpected keyword argument|missing \d+ required|required positional|"
    r"got multiple values|takes \d+ positional|takes no arguments"
)


def vacuous_sites(value: Any, path: str = "$") -> list[str]:
    """Every raised TypeError in an outcome whose message is an argument-binding failure.

    Both sides fail to bind a wrong call the same way, so such a step compares equal while testing
    nothing: the call never reached the code under test.
    """
    found: list[str] = []
    if isinstance(value, dict):
        raised = value.get("raised")
        if isinstance(raised, dict) and raised.get("mro", [""])[0] == "TypeError"                 and _BINDING_FAILURE.search(str(raised.get("message", ""))):
            found.append(f"{path}: {raised['message']}")
        for key, item in value.items():
            found += vacuous_sites(item, f"{path}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            found += vacuous_sites(item, f"{path}[{i}]")
    return found


def assert_equal_outcomes(legacy_out: Any, final_out: Any) -> None:
    diff = first_difference(legacy_out, final_out)
    assert diff is None, diff
    # Equal is not enough: a call that cannot bind its arguments is equal on both sides and proves nothing.
    sites = vacuous_sites(legacy_out) + vacuous_sites(final_out)
    assert not sites, "vacuous check (the call did not bind its arguments): " + "; ".join(sites)


def both(sides, tmp_path: Path, scenario: Callable[[Any, Path], Any]) -> tuple[Any, Any]:
    """Run ``scenario(side, base)`` on each side in its own directory."""
    results = []
    for side in sides:
        base = tmp_path / side.name
        base.mkdir()
        results.append(plain(scenario(side, base), str(base)))
    return results[0], results[1]


def check(sides, tmp_path: Path, scenario: Callable[[Any, Path], Any]) -> Any:
    legacy_out, final_out = both(sides, tmp_path, scenario)
    assert_equal_outcomes(legacy_out, final_out)
    return final_out


def _is_time_column(name: str) -> bool:
    return name == "at" or name.endswith("_at")


def dump_ledger(db: Path, *, mask_times: bool = False) -> dict:
    """Every table of a ledger, rows in rowid order."""
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        out = {}
        for table in tables:
            rows = []
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid"):
                item = {k: row[k] for k in row.keys()}
                if mask_times:
                    for key in item:
                        if _is_time_column(key) and item[key] is not None:
                            item[key] = "<time>"
                rows.append(item)
            out[table] = rows
        return out
    finally:
        conn.close()


def new_store(side: Any, base: Path, name: str = "run.db", **kwargs: Any) -> Any:
    return side.store.ExecutionStore(base / name, **kwargs)


def seeded(side: Any, base: Path, unit_ids=("u0", "u1", "u2"), run_id: str = "run-1", **kwargs: Any) -> Any:
    store = new_store(side, base, **kwargs)
    store.create_run(run_id, driver="inline", backend="mock", model="m", adapter_version="1", created_at=1.0)
    store.register_units(run_id, list(unit_ids), at=1.0)
    return store


# --------------------------------------------------------------------------
# Ledger bytes
# --------------------------------------------------------------------------


def _schema_dump(db: Path) -> dict:
    """The ``sqlite_master`` rows (rowid order), the version rows and the journal mode."""
    conn = sqlite3.connect(str(db))
    try:
        master = [
            list(row)
            for row in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY rowid")
        ]
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_version ORDER BY rowid")]
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    return {"sqlite_master": master, "schema_version": versions, "journal_mode": mode}


def _encode(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def _at_prefix(side: Any, db: Path, steps: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Create ``db`` as a ledger migrated through the first ``steps`` migrations only."""
    full = list(side.store._MIGRATIONS)
    monkeypatch.setattr(side.store, "_MIGRATIONS", full[:steps])
    try:
        side.store.ExecutionStore(db)
    finally:
        monkeypatch.setattr(side.store, "_MIGRATIONS", full)


def build_schema_fixtures(side: Any, work: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """The fixture files one side produces: fresh create, every prefix, every upgrade."""
    files: dict[str, bytes] = {}
    fresh = work / "fresh.sqlite3"
    side.store.ExecutionStore(fresh)
    files["fresh.json"] = _encode(_schema_dump(fresh))
    steps = len(side.store._MIGRATIONS)
    for prefix in range(1, steps + 1):
        db = work / f"prefix-{prefix:02d}.sqlite3"
        _at_prefix(side, db, prefix, monkeypatch)
        files[f"prefix-{prefix:02d}.json"] = _encode(_schema_dump(db))
        side.store.ExecutionStore(db)
        files[f"upgrade-from-{prefix:02d}.json"] = _encode(_schema_dump(db))
    return files


def _statement_trace(side: Any, base: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every SQL statement a fresh open, a reopen, a write run and a read issue."""
    base.mkdir()
    log: list[str] = []
    real_connect = sqlite3.connect

    def traced(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn = real_connect(*args, **kwargs)
        log.append("-- connect")
        conn.set_trace_callback(log.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced)
    try:
        store = new_store(side, base, "runs.sqlite3")
        log.append("== reopen")
        new_store(side, base, "runs.sqlite3")
        log.append("== write")
        store.create_run("r", driver="d", backend="b", model="m", adapter_version="1", created_at=1.0)
        store.register_units("r", ["u"], at=1.0)
        store.claim_unit("r", "u", "w", at=2.0)
        log.append("== read")
        store.get_run("r")
        store.snapshot("r")
    finally:
        monkeypatch.setattr(sqlite3, "connect", real_connect)
    return [line.replace(str(base), "<ROOT>") for line in log]


class TestLedgerSchema:
    def test_the_product_ddl_is_byte_identical(self, sides):
        legacy, final = sides
        assert final.store._MIGRATIONS == legacy.store._MIGRATIONS
        assert len(final.store._MIGRATIONS) == 10

    def test_version_one_is_inserted_by_the_runner_and_later_versions_updated(self, sides):
        """The migration loop (kept in the product) inserts version 1, updates the rest."""
        for side in sides:
            source = Path(side.store.__file__).read_text(encoding="utf-8")
            assert 'conn.execute(\n                            "INSERT INTO schema_version(version) VALUES (?)", (new_version,)' in source
            assert 'conn.execute("UPDATE schema_version SET version = ?", (new_version,))' in source
            assert not any("INSERT INTO schema_version" in s for step in side.store._MIGRATIONS for s in step)
            assert not any("UPDATE schema_version" in s for step in side.store._MIGRATIONS for s in step)

    def test_fixtures_are_byte_equal_between_the_two_sides(self):
        legacy = sorted(p.name for p in (_FIXTURES / "legacy").glob("*.json"))
        final = sorted(p.name for p in (_FIXTURES / "llm-kit").glob("*.json"))
        assert legacy == final
        assert len(legacy) == 1 + 10 + 10
        for name in legacy:
            assert (_FIXTURES / "legacy" / name).read_bytes() == (_FIXTURES / "llm-kit" / name).read_bytes(), name

    def test_each_side_regenerates_its_own_fixtures_byte_for_byte(self, sides, tmp_path, monkeypatch):
        for side, directory in zip(sides, ("legacy", "llm-kit")):
            work = tmp_path / directory
            work.mkdir()
            produced = build_schema_fixtures(side, work, monkeypatch)
            committed = {p.name: p.read_bytes() for p in (_FIXTURES / directory).glob("*.json")}
            assert produced == committed, directory

    def test_fresh_create_ends_at_schema_ten_with_one_version_row(self):
        fresh = json.loads((_FIXTURES / "llm-kit" / "fresh.json").read_text(encoding="ascii"))
        assert fresh["schema_version"] == [10]
        assert fresh["journal_mode"] == "wal"
        names = {row[1] for row in fresh["sqlite_master"] if row[0] == "table"}
        assert {"schema_version", "runs", "units", "attempts", "interrupts", "dispatches"} <= names

    def test_every_upgrade_converges_on_the_fresh_create_bytes(self):
        fresh = json.loads((_FIXTURES / "llm-kit" / "fresh.json").read_text(encoding="ascii"))
        for prefix in range(1, 11):
            upgraded = json.loads(
                (_FIXTURES / "llm-kit" / f"upgrade-from-{prefix:02d}.json").read_text(encoding="ascii")
            )
            assert upgraded["schema_version"] == [10]
            assert {row[1] for row in upgraded["sqlite_master"] if row[0] == "table"} == {
                row[1] for row in fresh["sqlite_master"] if row[0] == "table"
            }, prefix

    def test_statement_order_on_open_is_identical(self, sides, tmp_path, monkeypatch):
        """Pragma order: every statement a fresh open, a reopen and the verbs issue.

        The one statement that differs is the schema-version read's column alias
        (``TestDeliberateDifferences.test_the_version_read_alias_is_the_only_statement_difference``).
        """
        traces = [_statement_trace(side, tmp_path / side.name, monkeypatch) for side in sides]
        legacy_trace = [line.replace("AS v FROM schema_version", "AS version FROM schema_version") for line in traces[0]]
        assert legacy_trace == traces[1]
        text = " ; ".join(traces[1])
        # The documented order: busy_timeout, then WAL, then foreign_keys, then BEGIN IMMEDIATE.
        assert text.index("PRAGMA busy_timeout") < text.index("journal_mode") < text.index(
            "PRAGMA foreign_keys = ON"
        ) < text.index("BEGIN IMMEDIATE")

    def test_a_current_ledger_opens_without_taking_the_write_lock(self, sides, tmp_path, monkeypatch):
        counts = []
        for side in sides:
            base = tmp_path / side.name
            base.mkdir()
            new_store(side, base, "runs.sqlite3")
            log: list[str] = []
            real_connect = sqlite3.connect

            def traced(*args: Any, _log: list = log, **kwargs: Any) -> sqlite3.Connection:
                conn = real_connect(*args, **kwargs)
                conn.set_trace_callback(_log.append)
                return conn

            monkeypatch.setattr(sqlite3, "connect", traced)
            try:
                new_store(side, base, "runs.sqlite3")
            finally:
                monkeypatch.setattr(sqlite3, "connect", real_connect)
            counts.append(["BEGIN" in line for line in log])
        assert counts[0] == counts[1]
        assert not any(counts[1])

    def test_a_ledger_beyond_the_known_schema_opens_unchanged(self, sides, tmp_path):
        outcomes = []
        for side in sides:
            base = tmp_path / side.name
            base.mkdir()
            db = base / "runs.sqlite3"
            new_store(side, base, "runs.sqlite3")
            conn = sqlite3.connect(str(db))
            conn.execute("UPDATE schema_version SET version = 11")
            conn.commit()
            conn.close()
            before = _schema_dump(db)
            new_store(side, base, "runs.sqlite3")
            outcomes.append(_schema_dump(db) == before)
        assert outcomes == [True, True]

    def test_an_interrupted_migration_rolls_back_and_a_retry_starts_clean(self, sides, tmp_path, monkeypatch):
        outcomes = []
        for side in sides:
            base = tmp_path / side.name
            base.mkdir()
            full = list(side.store._MIGRATIONS)
            broken = full[:3] + [["THIS IS NOT SQL"]] + full[4:]
            monkeypatch.setattr(side.store, "_MIGRATIONS", broken)
            try:
                result = outcome(lambda: side.store.ExecutionStore(base / "runs.sqlite3"), str(base))
            finally:
                monkeypatch.setattr(side.store, "_MIGRATIONS", full)
            db = base / "runs.sqlite3"
            conn = sqlite3.connect(str(db))
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            conn.close()
            side.store.ExecutionStore(db)
            outcomes.append((result, tables, _schema_dump(db)["schema_version"]))
        assert_equal_outcomes(plain(outcomes[0]), plain(outcomes[1]))
        assert outcomes[1][0]["raised"]["mro"][0] == "OperationalError"
        assert outcomes[1][1] == []
        assert outcomes[1][2] == [10]

    def test_the_posture_is_foreign_keys_on_and_default_wal_failure(self, sides):
        final = sides[1]
        source = Path(final.store.__file__).read_text(encoding="utf-8")
        assert 'post_wal_pragmas=(("foreign_keys", "ON"),)' in source
        assert "WAL_RERAISE" not in source
        assert "CREATE TABLE" in source  # product DDL stays here, not in storage
        storage = _LIB / "llm_kit" / "storage"
        assert not any("CREATE TABLE" in p.read_text(encoding="utf-8") for p in storage.glob("*.py"))
        assert not any("_MIGRATIONS" in p.read_text(encoding="utf-8") for p in storage.glob("*.py"))

    def test_a_busy_first_open_fails_the_same_way(self, sides, tmp_path):
        """WAL cannot be set while another connection holds the file: same error, same wording."""
        outcomes = []
        for side in sides:
            base = tmp_path / side.name
            base.mkdir()
            db = base / "runs.sqlite3"
            holder = sqlite3.connect(str(db), isolation_level=None)
            holder.execute("PRAGMA journal_mode = DELETE")
            holder.execute("CREATE TABLE t (x)")
            holder.execute("BEGIN EXCLUSIVE")
            try:
                outcomes.append(
                    plain(outcome(lambda: side.store.ExecutionStore(db, busy_timeout_ms=50), str(base)))
                )
            finally:
                holder.execute("ROLLBACK")
                holder.close()
        assert_equal_outcomes(outcomes[0], outcomes[1])
        assert "raised" in outcomes[1]
        assert outcomes[1]["raised"]["mro"][0] == "OperationalError"

    def test_a_memory_database_and_a_network_path_warning_agree(self, sides, tmp_path, monkeypatch):
        import warnings

        outcomes = []
        for side in sides:
            base = tmp_path / side.name
            base.mkdir()
            refused = outcome(lambda: side.store.ExecutionStore(":memory:"))
            monkeypatch.setattr(side.store, "looks_like_network_path", lambda path: True)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                side.store.ExecutionStore(base / "n.db")
            warned = [(w.category.__name__, str(w.message).replace(str(base), "<ROOT>")) for w in caught]
            with warnings.catch_warnings(record=True) as quiet:
                warnings.simplefilter("always")
                side.store.ExecutionStore(base / "q.db", warn_on_network_path=False)
            outcomes.append(plain({"refused": refused, "warned": warned, "quiet": len(quiet)}))
        assert_equal_outcomes(outcomes[0], outcomes[1])
        assert outcomes[1]["warned"] and outcomes[1]["quiet"] == 0

    def test_the_network_heuristic_agrees(self, sides):
        samples = ["\\\\host\\share\\x.db", "//host/share/x.db", "/tmp/x.db", "relative/x.db"]
        results = [[side.store.looks_like_network_path(s) for s in samples] for side in sides]
        assert results[0] == results[1]
        assert results[1][:2] == [True, True]


# --------------------------------------------------------------------------
# Store: lifecycle, fences, leases
# --------------------------------------------------------------------------


def _record(log: list, label: str, fn: Callable[[], Any], root: str) -> Any:
    result = outcome(fn, root)
    log.append([label, result])
    return result


def no_harness_errors(out: Mapping[str, Any]) -> None:
    """A scenario step that raised a programming error is a broken scenario, not a parity result."""
    for label, result in out["log"]:
        if "raised" in result:
            assert result["raised"]["mro"][0] not in ("TypeError", "AttributeError", "NameError"), (
                label, result["raised"]["message"])


def _scenario_unit_lifecycle(side: Any, base: Path) -> dict:
    root = str(base)
    m = side.model
    store = new_store(side, base)
    log: list = []
    rec = lambda label, fn: _record(log, label, fn, root)  # noqa: E731
    rec("create", lambda: store.create_run("r1", driver="inline", backend="mock", model="m",
                                           adapter_version="1", created_at=1.0,
                                           environment={"A": "1", "B": "two"}))
    rec("create-dup", lambda: store.create_run("r1", driver="x", backend="y", model="z", adapter_version="2", created_at=2.0))
    rec("get-unknown", lambda: store.get_run("nope"))
    rec("register", lambda: store.register_units("r1", ["u0", "u1", "u2", "u3"], at=1.0))
    rec("register-dup", lambda: store.register_units("r1", ["u2", "u9", "u9"], at=1.0))
    rec("register-unknown-run", lambda: store.register_units("nope", ["u"], at=1.0))
    first = rec("claim-u0", lambda: store.claim_unit("r1", "u0", "wa", lease_seconds=5.0, at=10.0))
    rec("claim-u0-live", lambda: store.claim_unit("r1", "u0", "wb", lease_seconds=5.0, at=11.0))
    rec("renew-u0", lambda: store.renew_lease("r1", "u0", 1, lease_seconds=5.0, at=12.0))
    second = rec("claim-u0-expired", lambda: store.claim_unit("r1", "u0", "wb", lease_seconds=5.0, at=30.0))
    rec("renew-stale", lambda: store.renew_lease("r1", "u0", 1, lease_seconds=5.0, at=31.0))
    rec("accept-stale", lambda: store.accept_unit("r1", "u0", 1, text="late", at=32.0))
    rec("fail-stale", lambda: store.fail_unit("r1", "u0", 1, error="late", at=32.5))
    rec("accept-live", lambda: store.accept_unit("r1", "u0", 2, text="answer", usage=m.UsageRecord(
        input_tokens=3, output_tokens=4, cache_hit_tokens=1), at=33.0))
    rec("accept-again", lambda: store.accept_unit("r1", "u0", 2, text="answer", at=34.0))
    rec("renew-terminal", lambda: store.renew_lease("r1", "u0", 2, lease_seconds=5.0, at=35.0))
    rec("renew-stale-terminal", lambda: store.renew_lease("r1", "u0", 1, lease_seconds=5.0, at=35.0))
    rec("claim-terminal", lambda: store.claim_unit("r1", "u0", "wc", at=36.0))
    rec("claim-u1", lambda: store.claim_unit("r1", "u1", "wa", lease_seconds=50.0, at=40.0))
    rec("fail-retry", lambda: store.fail_unit("r1", "u1", 1, error="boom " * 200, terminal=False, at=41.0))
    rec("renew-pending", lambda: store.renew_lease("r1", "u1", 1, at=42.0))
    rec("claim-u1-again", lambda: store.claim_unit("r1", "u1", "wb", lease_seconds=50.0, at=43.0))
    rec("fail-terminal", lambda: store.fail_unit("r1", "u1", 2, error={"code": "x", "n": 1}, terminal=True, at=44.0))
    rec("claim-u1-failed", lambda: store.claim_unit("r1", "u1", "wb", at=45.0))
    rec("halt", lambda: store.set_halt("r1", "rate_limit", "slow down", at=50.0))
    rec("claim-halted", lambda: store.claim_unit("r1", "u2", "wa", at=51.0))
    claimed = store.get_unit("r1", "u3")
    rec("halt-clear", lambda: store.clear_halt("r1"))
    rec("claim-u3", lambda: store.claim_unit("r1", "u3", "wa", lease_seconds=50.0, at=60.0))
    rec("halt-again", lambda: store.set_halt("r1", "auth", "", at=61.0))
    rec("accept-halted-valid-fence", lambda: store.accept_unit("r1", "u3", 1, text="paid", at=62.0))
    rec("apply-started", lambda: store.record_apply_started("r1", "u3", at=63.0))
    rec("apply-rejected", lambda: store.record_apply_rejected("r1", "u3", reason="no " * 400, at=64.0))
    rec("apply-started-2", lambda: store.record_apply_started("r1", "u0", at=65.0))
    rec("apply-succeeded", lambda: store.record_apply_succeeded("r1", "u0", at=66.0))
    rec("apply-unknown-unit", lambda: store.record_apply_started("r1", "ghost", at=67.0))
    rec("get-unit", lambda: store.get_unit("r1", "u0"))
    rec("get-unit-unknown", lambda: store.get_unit("r1", "ghost"))
    rec("list-units", lambda: store.list_units("r1"))
    rec("list-attempts", lambda: store.list_attempts("r1"))
    rec("list-attempts-unit", lambda: store.list_attempts("r1", "u0"))
    rec("snapshot", lambda: store.snapshot("r1"))
    rec("snapshot-kinds", lambda: store.snapshot("r1", attempt_kinds=(m.AttemptKind.FAIL, m.AttemptKind.ACCEPT)))
    rec("get-run", lambda: store.get_run("r1"))
    return {"log": log, "first": first, "second": second, "claimed": claimed,
            "ledger": dump_ledger(base / "run.db")}


def _scenario_dispatcher_lease(side: Any, base: Path) -> dict:
    root = str(base)
    store = seeded(side, base, unit_ids=("u0",))
    log: list = []
    rec = lambda label, fn: _record(log, label, fn, root)  # noqa: E731
    rec("first", lambda: store.acquire_dispatcher_lease("run-1", "d1", lease_seconds=10.0, at=100.0))
    rec("other-refused", lambda: store.acquire_dispatcher_lease("run-1", "d2", lease_seconds=10.0, at=105.0))
    rec("same-holder-live", lambda: store.acquire_dispatcher_lease("run-1", "d1", lease_seconds=10.0, at=106.0))
    rec("renew-wrong-fence", lambda: store.renew_dispatcher_lease("run-1", "d1", 1, lease_seconds=10.0, at=107.0))
    rec("renew-wrong-holder", lambda: store.renew_dispatcher_lease("run-1", "d2", 2, lease_seconds=10.0, at=107.0))
    rec("renew", lambda: store.renew_dispatcher_lease("run-1", "d1", 2, lease_seconds=10.0, at=108.0))
    rec("other-after-expiry", lambda: store.acquire_dispatcher_lease("run-1", "d2", lease_seconds=10.0, at=200.0))
    rec("old-holder-renew-stale", lambda: store.renew_dispatcher_lease("run-1", "d1", 2, lease_seconds=10.0, at=201.0))
    rec("old-holder-release-stale", lambda: store.release_dispatcher_lease("run-1", "d1", 2, at=201.0))
    rec("old-holder-reacquire-refused", lambda: store.acquire_dispatcher_lease("run-1", "d1", lease_seconds=10.0, at=202.0))
    rec("release", lambda: store.release_dispatcher_lease("run-1", "d2", 3, at=203.0))
    rec("release-twice", lambda: store.release_dispatcher_lease("run-1", "d2", 3, at=204.0))
    rec("after-release", lambda: store.acquire_dispatcher_lease("run-1", "d1", lease_seconds=10.0, at=205.0))
    rec("same-after-own-expiry", lambda: store.acquire_dispatcher_lease("run-1", "d1", lease_seconds=10.0, at=900.0))
    rec("unknown-run", lambda: store.acquire_dispatcher_lease("nope", "d1", lease_seconds=10.0, at=1.0))
    rec("run", lambda: store.get_run("run-1"))
    # dispatches
    rec("dispatch-1", lambda: store.record_dispatch("run-1", "u0", "w1", at=300.0))
    rec("dispatch-dup", lambda: store.record_dispatch("run-1", "u0", "w2", at=301.0))
    rec("attach", lambda: store.attach_dispatch_session("run-1", "u0", "sess-1"))
    rec("attach-same", lambda: store.attach_dispatch_session("run-1", "u0", "sess-1"))
    rec("attach-other", lambda: store.attach_dispatch_session("run-1", "u0", "sess-2"))
    rec("attach-empty", lambda: store.attach_dispatch_session("run-1", "u0", ""))
    rec("open", lambda: store.open_dispatches("run-1"))
    rec("settle", lambda: store.settle_dispatch("run-1", "u0", outcome="done", at=302.0))
    rec("settle-none", lambda: store.settle_dispatch("run-1", "u0", outcome="done", at=303.0))
    rec("dispatch-2", lambda: store.record_dispatch("run-1", "u0", "w3", session_id="s3", cli_version="2.1", at=304.0))
    rec("open-2", lambda: store.open_dispatches("run-1"))
    rec("open-unknown-run", lambda: store.open_dispatches("nope"))
    return {"log": log, "ledger": dump_ledger(base / "run.db")}


def _scenario_fences_are_monotonic(side: Any, base: Path) -> dict:
    store = seeded(side, base, unit_ids=("u0",))
    fences = []
    for i in range(6):
        result = store.claim_unit("run-1", "u0", f"w{i}", lease_seconds=1.0, at=10.0 + 10 * i)
        fences.append(result.fencing_token)
    store.accept_unit("run-1", "u0", fences[-1], text="x", at=100.0)
    return {"fences": fences, "attempts": [a.kind for a in store.list_attempts("run-1", "u0")],
            "ledger": dump_ledger(base / "run.db")}


def _scenario_reopen_preserves_state(side: Any, base: Path) -> dict:
    store = seeded(side, base, unit_ids=("u0", "u1"))
    store.acquire_dispatcher_lease("run-1", "d1", lease_seconds=100.0, at=10.0)
    store.claim_unit("run-1", "u0", "w", lease_seconds=100.0, at=11.0)
    store.record_dispatch("run-1", "u0", "w", at=12.0)
    reopened = new_store(side, base)
    return {
        "run": reopened.get_run("run-1"),
        "units": reopened.list_units("run-1"),
        "open": reopened.open_dispatches("run-1"),
        "refused": outcome(lambda: reopened.acquire_dispatcher_lease("run-1", "d2", lease_seconds=1.0, at=20.0)),
    }


class TestStoreParity:
    def test_the_unit_lifecycle_agrees_step_by_step(self, sides, tmp_path):
        out = check(sides, tmp_path, _scenario_unit_lifecycle)
        no_harness_errors(out)
        names = {label: result for label, result in out["log"]}
        assert names["accept-live"] == {"ok": None}
        # The scenario exercises what it claims: each refusal is a refusal.
        for label, error in (
            ("claim-u0-live", "AlreadyClaimedError"),
            ("renew-stale", "StaleFenceError"),
            ("accept-stale", "StaleFenceError"),
            ("fail-stale", "StaleFenceError"),
            ("accept-again", "TerminalStateError"),
            ("renew-terminal", "TerminalStateError"),
            ("renew-stale-terminal", "StaleFenceError"),
            ("claim-terminal", "TerminalStateError"),
            ("claim-halted", "RunHaltedError"),
            ("create-dup", "IntegrityError"),
            ("register-dup", "DuplicateUnitError"),
        ):
            assert names[label]["raised"]["mro"][0] == error, label
        assert names["accept-halted-valid-fence"] == {"ok": None}
        assert out["first"]["ok"]["fencing_token"] == 1
        assert out["second"]["ok"]["fencing_token"] == 2

    def test_the_dispatcher_lease_agrees_and_each_rule_is_exercised(self, sides, tmp_path):
        out = check(sides, tmp_path, _scenario_dispatcher_lease)
        no_harness_errors(out)
        log = {label: result for label, result in out["log"]}
        assert log["first"] == {"ok": 1}
        assert log["other-refused"] == {"ok": None}  # a different live holder is refused
        assert log["same-holder-live"] == {"ok": 2}
        assert log["other-after-expiry"] == {"ok": 3}  # expiry permits a higher fence
        assert log["old-holder-renew-stale"]["raised"]["mro"][0] == "StaleDispatcherLeaseError"
        assert log["old-holder-release-stale"]["raised"]["mro"][0] == "StaleDispatcherLeaseError"
        assert log["old-holder-reacquire-refused"] == {"ok": None}
        assert log["after-release"] == {"ok": 4}  # release permits a higher fence
        assert log["same-after-own-expiry"] == {"ok": 5}
        assert log["dispatch-dup"]["raised"]["mro"][0] == "IntegrityError"
        assert log["attach-other"]["raised"]["mro"][0] == "ValueError"

    def test_unit_fences_are_monotonic_across_expiring_claims(self, sides, tmp_path):
        out = check(sides, tmp_path, _scenario_fences_are_monotonic)
        assert out["fences"] == [1, 2, 3, 4, 5, 6]

    def test_state_survives_a_reopen(self, sides, tmp_path):
        out = check(sides, tmp_path, _scenario_reopen_preserves_state)
        assert out["refused"] == {"ok": None}

    def test_a_dispatcher_fence_is_never_reset_by_release(self, sides, tmp_path):
        def scenario(side: Any, base: Path) -> list:
            store = seeded(side, base, unit_ids=("u0",))
            fences = []
            for i in range(4):
                fences.append(store.acquire_dispatcher_lease("run-1", "d", lease_seconds=5.0, at=10.0 * i))
                store.release_dispatcher_lease("run-1", "d", fences[-1], at=10.0 * i + 1)
            return fences

        assert check(sides, tmp_path, scenario) == [1, 2, 3, 4]

    def test_distinct_runs_proceed_concurrently_on_one_database(self, sides, tmp_path):
        def scenario(side: Any, base: Path) -> dict:
            store = new_store(side, base)
            runs = [f"run-{i}" for i in range(4)]
            for run_id in runs:
                store.create_run(run_id, driver="d", backend="b", model="m", adapter_version="1", created_at=1.0)
                store.register_units(run_id, [f"u{i}" for i in range(5)], at=1.0)
            errors: list = []
            barrier = threading.Barrier(len(runs))

            def work(run_id: str) -> None:
                mine = new_store(side, base)
                try:
                    barrier.wait(timeout=30)
                    fence = mine.acquire_dispatcher_lease(run_id, f"d-{run_id}", lease_seconds=60.0, at=10.0)
                    assert fence == 1
                    for i in range(5):
                        claim = mine.claim_unit(run_id, f"u{i}", f"w-{run_id}", lease_seconds=60.0, at=11.0 + i)
                        mine.accept_unit(run_id, f"u{i}", claim.fencing_token, text=f"t{i}", at=12.0 + i)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(repr(exc))

            threads = [threading.Thread(target=work, args=(r,)) for r in runs]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)
            ledger = dump_ledger(base / "run.db")
            per_run = {}
            for run_id in runs:
                units = [u for u in ledger["units"] if u["run_id"] == run_id]
                attempts = [a for a in ledger["attempts"] if a["run_id"] == run_id]
                per_run[run_id] = {
                    "states": sorted((u["unit_id"], u["state"], u["fencing_token"]) for u in units),
                    "kinds": sorted(a["kind"] for a in attempts),
                }
            runs_rows = {r["id"]: (r["dispatcher_id"], r["dispatcher_fence"]) for r in ledger["runs"]}
            return {"errors": errors, "per_run": per_run, "dispatchers": runs_rows}

        out = check(sides, tmp_path, scenario)
        assert out["errors"] == []
        assert all(
            state == "accepted" for run in out["per_run"].values() for _u, state, _f in run["states"]
        )

    def test_one_unit_claimed_by_many_threads_has_exactly_one_winner(self, sides, tmp_path):
        def scenario(side: Any, base: Path) -> dict:
            seeded(side, base, unit_ids=("u0",))
            results: list = []
            barrier = threading.Barrier(8)

            def race(i: int) -> None:
                store = new_store(side, base)
                barrier.wait(timeout=30)
                results.append(outcome(lambda: store.claim_unit("run-1", "u0", f"w{i}", lease_seconds=60.0, at=10.0)))

            threads = [threading.Thread(target=race, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)
            winners = [r for r in results if "ok" in r]
            losers = sorted({r["raised"]["mro"][0] for r in results if "raised" in r})
            ledger = dump_ledger(base / "run.db")
            return {"winners": len(winners), "fence": winners[0]["ok"]["fencing_token"], "losers": losers,
                    "claims": sum(1 for a in ledger["attempts"] if a["kind"] == "claim")}

        out = check(sides, tmp_path, scenario)
        assert out == {"winners": 1, "fence": 1, "losers": ["AlreadyClaimedError"], "claims": 1}

    def test_a_stale_fence_predicate_is_what_refuses_a_late_submission(self, sides, tmp_path):
        """The refusal comes from the fence comparison, not from the unit's state."""

        def scenario(side: Any, base: Path) -> list:
            store = seeded(side, base, unit_ids=("u0",))
            store.claim_unit("run-1", "u0", "a", lease_seconds=1.0, at=10.0)
            store.claim_unit("run-1", "u0", "b", lease_seconds=1.0, at=20.0)  # reclaim: fence 2
            return [
                outcome(lambda: store.accept_unit("run-1", "u0", 1, text="late", at=21.0)),
                outcome(lambda: store.fail_unit("run-1", "u0", 1, error="late", at=21.0)),
                outcome(lambda: store.renew_lease("run-1", "u0", 1, at=21.0)),
                [a.kind for a in store.list_attempts("run-1", "u0")],
                store.get_unit("run-1", "u0"),
            ]

        out = check(sides, tmp_path, scenario)
        assert [o["raised"]["mro"][0] for o in out[:3]] == ["StaleFenceError"] * 3
        assert out[4]["state"] == {"enum": "UnitState", "value": "claimed"}
        assert out[4]["claimed_by"] == "b"


# --------------------------------------------------------------------------
# Interrupts (the durable wait)
# --------------------------------------------------------------------------

_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def _scenario_interrupts(side: Any, base: Path) -> dict:
    root = str(base)
    m = side.model
    store = seeded(side, base, unit_ids=("u0", "u1", "u2", "u3"))
    log: list = []
    rec = lambda label, fn: _record(log, label, fn, root)  # noqa: E731

    def request(unit: str, token: int, **kw: Any) -> Any:
        return store.request_interrupt(
            "run-1", unit, token,
            m.InterruptRequest(kind="approve", request_schema=_SCHEMA, payload={"q": unit}, expires_in_s=kw.pop("expires", 100)),
            at=kw.pop("at", 20.0), **kw,
        )

    for i, unit in enumerate(("u0", "u1", "u2")):
        store.claim_unit("run-1", unit, "w", lease_seconds=500.0, at=10.0 + i)
    first = rec("request-u0", lambda: request("u0", 1))
    rec("request-twice", lambda: request("u0", 1))
    rec("request-stale", lambda: request("u1", 9))
    rec("request-bad-kind", lambda: store.request_interrupt(
        "run-1", "u1", 1, m.InterruptRequest(kind="", request_schema=_SCHEMA, payload={}), at=21.0))
    rec("request-bad-schema", lambda: store.request_interrupt(
        "run-1", "u1", 1, m.InterruptRequest(kind="k", request_schema={"type": 5}, payload={}), at=21.0))
    rec("request-u1-release", lambda: request("u1", 1, on_rejected=m.POLICY_RELEASE, on_expired=m.POLICY_RELEASE, expires=10))
    rec("request-u2", lambda: request("u2", 1, expires=None))
    rec("claim-waiting", lambda: store.claim_unit("run-1", "u0", "x", at=25.0))
    rec("renew-waiting", lambda: store.renew_lease("run-1", "u0", 1, at=25.0))
    rec("list", lambda: store.list_interrupts("run-1"))
    rec("get", lambda: store.get_interrupt("run-1", first["ok"]["id"]))
    rec("open", lambda: store.open_interrupt("run-1", "u0"))
    rec("resolve-bad-input", lambda: store.resolve_interrupt("run-1", first["ok"]["id"], decision="answer", input={"ok": "no"}, now=30.0))
    rec("resolve-no-input", lambda: store.resolve_interrupt("run-1", first["ok"]["id"], decision="answer", now=30.0))
    rec("resolve-answer", lambda: store.resolve_interrupt("run-1", first["ok"]["id"], decision="answer", input={"ok": True}, now=31.0))
    rec("resolve-replay", lambda: store.resolve_interrupt("run-1", first["ok"]["id"], decision="answer", input={"ok": True}, now=32.0))
    rec("resolve-conflict", lambda: store.resolve_interrupt("run-1", first["ok"]["id"], decision="reject", reason="no", now=33.0))
    rec("resolve-unknown", lambda: store.resolve_interrupt("run-1", "999", decision="answer", input={"ok": True}, now=33.0))
    rec("claim-after-answer", lambda: store.claim_unit("run-1", "u0", "x", lease_seconds=5.0, at=34.0))
    rec("expire-early", lambda: store.expire_interrupts("run-1", now=15.0))
    rec("expire", lambda: store.expire_interrupts("run-1", now=40.0))
    rec("expire-again", lambda: store.expire_interrupts("run-1", now=41.0))
    rec("resolve-expired", lambda: store.resolve_interrupt("run-1", "2", decision="answer", input={"ok": True}, now=42.0))
    rec("reject-stop", lambda: store.resolve_interrupt("run-1", "3", decision="reject", reason="x" * 3000, now=43.0))
    rec("fail-stop-state", lambda: store.get_unit("run-1", "u2"))
    rec("wait-under-dispatch", lambda: (store.claim_unit("run-1", "u3", "w", lease_seconds=500.0, at=50.0),
                                         store.record_dispatch("run-1", "u3", "w", at=50.0),
                                         request("u3", 1)))
    rec("snapshot", lambda: store.snapshot("run-1"))
    rec("events-error-free", lambda: [e["event"] for e in side.events.project_run(store, "run-1")])
    return {"log": log, "ledger": dump_ledger(base / "run.db")}


class TestInterruptsParity:
    def test_the_durable_wait_agrees_step_by_step(self, sides, tmp_path):
        out = check(sides, tmp_path, _scenario_interrupts)
        no_harness_errors(out)
        log = {label: result for label, result in out["log"]}
        assert log["claim-waiting"]["raised"]["mro"][:2] == ["UnitWaitingError", "AlreadyClaimedError"]
        assert log["resolve-conflict"]["raised"]["mro"][0] == "ResolutionConflictError"
        assert log["resolve-expired"]["raised"]["mro"][0] == "InterruptExpiredError"
        assert log["wait-under-dispatch"]["raised"]["mro"][0] == "WaitUnderDispatchError"
        assert log["resolve-bad-input"]["raised"]["mro"][0] == "ResolutionInputError"
        assert log["fail-stop-state"]["ok"]["state"]["value"] == "operator_rejected"
        assert "ok" in log["events-error-free"]


# --------------------------------------------------------------------------
# Events, status
# --------------------------------------------------------------------------


def _mixed_store(side: Any, base: Path) -> Any:
    """A run with accepted, failed, retried, skipped, expired, applied and rejected units."""
    m = side.model
    store = seeded(side, base, unit_ids=("a", "b", "c", "d", "e", "f"))
    store.claim_unit("run-1", "a", "w1", lease_seconds=10.0, at=10.0)
    store.accept_unit("run-1", "a", 1, text="ok", usage=m.UsageRecord(input_tokens=5, output_tokens=7, cache_hit_tokens=2), at=12.0)
    store.record_apply_started("run-1", "a", at=13.0)
    store.record_apply_succeeded("run-1", "a", at=14.0)
    store.claim_unit("run-1", "b", "w1", lease_seconds=10.0, at=20.0)
    store.fail_unit("run-1", "b", 1, error="rate limit hit, please slow down", terminal=False, at=21.0)
    store.claim_unit("run-1", "b", "w2", lease_seconds=10.0, at=22.0)
    store.fail_unit("run-1", "b", 2, error="bad output shape", terminal=True, usage=m.UsageRecord(input_tokens=1), at=23.0)
    store.claim_unit("run-1", "c", "prepare", lease_seconds=10.0, at=30.0)
    store.fail_unit("run-1", "c", 1, error=m.SKIP_ERROR_PREFIX + "up_to_date", terminal=True, at=31.0)
    store.claim_unit("run-1", "d", "w1", lease_seconds=5.0, at=40.0)
    store.claim_unit("run-1", "d", "w2", lease_seconds=5.0, at=50.0)  # expire + reclaim
    store.claim_unit("run-1", "e", "w1", lease_seconds=500.0, at=60.0)
    store.accept_unit("run-1", "e", 1, text="e-text", at=61.0)
    store.record_apply_started("run-1", "e", at=62.0)
    store.record_apply_rejected("run-1", "e", reason="declined", at=63.0)
    store.claim_unit("run-1", "f", "w1", lease_seconds=500.0, at=64.0)
    store.accept_unit("run-1", "f", 1, text="f-text", at=65.0)
    store.record_apply_started("run-1", "f", at=66.0)
    store.set_halt("run-1", "rate_limit", "You've hit your limit for today", at=70.0)
    return store


def _scenario_events(side: Any, base: Path) -> dict:
    store = _mixed_store(side, base)
    events = side.events.project_run(store, "run-1")
    return {
        "events": [dict(e) for e in events],
        "again": [dict(e) for e in side.events.project_run(store, "run-1")],
        "unknown": outcome(lambda: side.events.project_run(store, "ghost"), str(base)),
    }


def _scenario_status(side: Any, base: Path) -> dict:
    store = _mixed_store(side, base)
    root = str(base)
    return {
        "digest": side.status.compute_status(store, "run-1", now=100.0),
        "narrow": side.status.compute_status(store, "run-1", now=100.0, throughput_window_s=40.0, max_failure_groups=1),
        "dict": side.status.compute_status(store, "run-1", now=100.0).to_dict(),
        "unknown": outcome(lambda: side.status.compute_status(store, "ghost", now=1.0), root),
        "apply": side.status.apply_states(store.list_units("run-1"), store.list_attempts("run-1")),
        "constants": [side.status.APPLY_KINDS, side.status.MAX_APPLY_UNIT_IDS,
                      side.status.DEFAULT_THROUGHPUT_WINDOW_S, side.status.DEFAULT_MAX_FAILURE_GROUPS],
    }


class TestEventsAndStatus:
    def test_the_projected_event_bytes_agree(self, sides, tmp_path):
        out = check(sides, tmp_path, _scenario_events)
        kinds = {e["event"] for e in out["events"]}
        assert {"run-created", "call-started", "result", "terminal", "usage", "apply-started", "apply-succeeded",
                "apply-rejected"} <= {k.split(":")[-1] for k in kinds}
        assert out["events"] == out["again"]
        assert out["unknown"]["raised"]["mro"][0] == "UnknownRunError"
        assert {e["source"]["plugin"] for e in out["events"]} == {"content-pipeline-kit"}  # the source literal is frozen

    def test_the_status_digest_agrees(self, sides, tmp_path):
        out = check(sides, tmp_path, _scenario_status)
        assert out["digest"]["total_units"] == 6
        assert out["digest"]["halted_kind"] == "rate_limit"
        assert out["unknown"]["raised"]["mro"][0] == "KeyError"
        assert out["digest"]["apply_counts"]["apply_rejected"] == 1


# --------------------------------------------------------------------------
# Waves, parallel graph, scheduler, deadlines, failure cache
# --------------------------------------------------------------------------


def _scenario_flat_wave(side: Any, base: Path) -> dict:
    store = seeded(side, base, unit_ids=("u0", "u1", "u2", "u3", "u4"))
    flat = side.workunit.FlatChunkStrategy(select=lambda s: [])
    store.claim_unit("run-1", "u1", "w", lease_seconds=5.0, at=10.0)
    return {
        "all": [u.unit_id for u in side.wave.ready_wave(store, "run-1", flat)],
        "capped": [u.unit_id for u in side.wave.ready_wave(store, "run-1", flat, max_wave_size=2)],
        "is_graph": side.wave.is_graph_strategy(flat),
        "is_ordered": side.wave.is_dependency_ordered(flat),
        "block": side.wave.graph_block_reason(store, "run-1", flat),
    }


def _scenario_sequential_graph(side: Any, base: Path) -> dict:
    root = str(base)
    store = seeded(side, base, unit_ids=("g0", "g1", "g2"))
    walk = side.workunit.GraphWalkStrategy(order=lambda s: ["g0", "g1", "g2"])
    steps: list = []
    steps.append(["initial", [u.unit_id for u in side.wave.ready_wave(store, "run-1", walk)]])
    steps.append(["too-wide", outcome(lambda: side.wave.ready_wave(store, "run-1", walk, max_wave_size=2), root)])
    store.claim_unit("run-1", "g0", "w", lease_seconds=5.0, at=10.0)
    steps.append(["claimed-live", [u.unit_id for u in side.wave.ready_wave(store, "run-1", walk)]])
    steps.append(["claimed-reclaim", [u.unit_id for u in side.wave.ready_wave(store, "run-1", walk, reclaim_at=100.0)]])
    store.accept_unit("run-1", "g0", 1, text="t", at=11.0)
    steps.append(["unapplied", [u.unit_id for u in side.wave.ready_wave(store, "run-1", walk)]])
    steps.append(["why-unapplied", side.wave.graph_block_reason(store, "run-1", walk)])
    store.record_apply_started("run-1", "g0", at=12.0)
    steps.append(["why-interrupted", side.wave.graph_block_reason(store, "run-1", walk)])
    store.record_apply_succeeded("run-1", "g0", at=13.0)
    steps.append(["released", [u.unit_id for u in side.wave.ready_wave(store, "run-1", walk)]])
    store.claim_unit("run-1", "g1", "w", lease_seconds=5.0, at=14.0)
    store.fail_unit("run-1", "g1", 1, error="x", terminal=True, at=15.0)
    steps.append(["blocked-by-failed", [u.unit_id for u in side.wave.ready_wave(store, "run-1", walk)]])
    steps.append(["why-failed", side.wave.graph_block_reason(store, "run-1", walk)])
    steps.append(["is", [side.wave.is_graph_strategy(walk), side.wave.is_dependency_ordered(walk)]])
    return {"steps": steps}


def _scenario_parallel_graph(side: Any, base: Path) -> dict:
    root = str(base)
    parallel = side.parallel_graph
    ids = ("a", "b", "c", "d", "e")
    store = seeded(side, base, unit_ids=ids)
    strategy = parallel.ParallelGraphStrategy(
        dependencies={"a": [], "b": [], "c": ["a", "b"], "d": ["c"], "e": []}, capacity=2, deferred={"e"})
    steps: list = []

    def wave(**kw: Any) -> Any:
        return [u.unit_id for u in side.wave.ready_wave(store, "run-1", strategy, **kw)]

    steps.append(["initial", wave()])
    steps.append(["capped", wave(max_wave_size=1)])
    store.claim_unit("run-1", "a", "w", lease_seconds=5.0, at=10.0)
    steps.append(["a-running", wave()])
    store.accept_unit("run-1", "a", 1, text="t", at=11.0)
    steps.append(["a-accepted-unapplied", wave()])
    store.record_apply_started("run-1", "a", at=12.0)
    store.record_apply_succeeded("run-1", "a", at=13.0)
    store.claim_unit("run-1", "b", "w", lease_seconds=5.0, at=14.0)
    store.fail_unit("run-1", "b", 1, error="boom", terminal=True, at=15.0)
    steps.append(["b-failed", wave()])
    steps.append(["gated", [u.unit_id for u in side.wave.gated_units(store, "run-1", strategy)]])
    steps.append(["is", [side.wave.is_graph_strategy(strategy), side.wave.is_dependency_ordered(strategy)]])
    steps.append(["gated-needs-parallel", outcome(lambda: side.wave.gated_units(
        store, "run-1", side.workunit.FlatChunkStrategy(select=lambda s: [])), root)])
    store.create_run("run-2", driver="d", backend="b", model="m", adapter_version="1", created_at=1.0)
    store.register_units("run-2", ["a", "b", "zzz"], at=1.0)
    steps.append(["mismatch-wave", outcome(lambda: side.wave.ready_wave(store, "run-2", strategy), root)])
    steps.append(["mismatch-gated", outcome(lambda: side.wave.gated_units(store, "run-2", strategy), root)])
    steps.append(["bad-capacity", outcome(lambda: parallel.ParallelGraphStrategy(dependencies={"a": []}, capacity=0), root)])
    steps.append(["bad-cycle", outcome(lambda: parallel.ParallelGraphStrategy(dependencies={"a": ["b"], "b": ["a"]}), root)])
    steps.append(["bad-deferred", outcome(lambda: parallel.ParallelGraphStrategy(dependencies={"a": []}, deferred={"q"}), root)])
    steps.append(["units", [(u.id, dict(u.context)) for u in strategy.units(None)]])
    steps.append(["node-ids", list(strategy.node_ids)])
    steps.append(["is-parallel", [parallel.is_parallel_graph_strategy(strategy), parallel.is_parallel_graph_strategy(object())]])
    return {"steps": steps}


def _scenario_scheduler(side: Any, base: Path) -> dict:
    root = str(base)
    sched = side.scheduler
    nodes = [sched.SchedulerNode("a"), sched.SchedulerNode("b", ("a",)), sched.SchedulerNode("c", ("a",)),
             sched.SchedulerNode("d", ("b", "c")), sched.SchedulerNode("z", (), True)]
    s = sched.Scheduler(nodes, capacity=2)
    steps: list = []
    steps.append(["admit-1", [n.node_id for n in s.admit_ready(now=0.0)]])
    steps.append(["complete-a", outcome(lambda: s.complete("a"), root)])
    steps.append(["admit-2", [n.node_id for n in s.admit_ready(now=0.0)]])
    steps.append(["fail-b", outcome(lambda: s.fail("b"), root)])
    steps.append(["state", s.state])
    steps.append(["requeue-not-running", outcome(lambda: s.requeue("c"), root)])
    steps.append(["admit-3", [n.node_id for n in s.admit_ready(now=0.0)]])
    steps.append(["requeue-c", outcome(lambda: s.requeue("c", quiet_period=5.0, now=1.0), root)])
    steps.append(["requeue-negative", outcome(lambda: s.requeue("z", quiet_period=-1.0, now=1.0), root)])
    steps.append(["wakeup", s.next_wakeup()])
    steps.append(["admit-quiet", [n.node_id for n in s.admit_ready(now=3.0)]])
    steps.append(["admit-after-quiet", [n.node_id for n in s.admit_ready(now=7.0)]])
    steps.append(["complete-z", outcome(lambda: s.complete("z"), root)])
    steps.append(["complete-c", outcome(lambda: s.complete("c"), root)])
    steps.append(["complete-not-running", outcome(lambda: s.complete("d"), root)])
    steps.append(["fail-inactive", outcome(lambda: s.fail("a"), root)])
    steps.append(["final-state", s.state])
    steps.append(["graph-errors", [
        outcome(lambda: sched.validate_graph({"a": ["a"]}), root),
        outcome(lambda: sched.validate_graph({"a": ["b"]}), root),
        outcome(lambda: sched.validate_graph({"a": ["b"], "b": ["a"]}), root),
        outcome(lambda: sched.validate_graph({"a": [], "b": ["a"]}), root),
        outcome(lambda: sched.SchedulerNode("a", ("a",)), root),
        outcome(lambda: sched.SchedulerNode("a", ("b", "b")), root),
    ]])
    steps.append(["closure", sorted(sched.dependents_closure({"a": [], "b": ["a"], "c": ["b"], "d": []}, ["a"]))])
    done = []

    def execute(node: Any) -> str:
        done.append(node.node_id)
        if node.node_id == "b":
            raise sched.NodeFailed("b failed")
        return node.node_id.upper()

    run = sched.run_graph(sched.Scheduler(nodes, capacity=1), execute, clock=lambda: 0.0, sleep=lambda s: None)
    steps.append(["run-graph", {"state": run.state, "results": run.results,
                                "failures": {k: str(v) for k, v in run.failures.items()}, "order": done}])

    def explode(node: Any) -> None:
        raise RuntimeError("executor bug")

    steps.append(["run-graph-error", outcome(lambda: sched.run_graph(
        sched.Scheduler([sched.SchedulerNode("q")], capacity=1), explode, clock=lambda: 0.0, sleep=lambda s: None), root)])
    return {"steps": steps}


class _Clock:
    """A manual clock: a call advances it by exactly the seconds it claims to take."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _scenario_deadlines(side: Any, base: Path) -> dict:
    d = side.deadlines
    root = str(base)
    clock = _Clock()
    budget = d.UnitBudget(10.0, clock=clock)
    circuit = d.DeadlineCircuit(limit=2)
    steps: list = []

    def takes(seconds: float, result: Any = None, error: Optional[BaseException] = None) -> Callable[[float], Any]:
        def call(timeout: float) -> Any:
            clock.now += seconds
            if error is not None:
                raise error
            return (result, timeout)

        return call

    steps.append(["ok-call", d.call_within_budget(budget, 20.0, takes(3.0, "ran"), is_timeout=lambda e: False, circuit=circuit)])
    steps.append(["spent", [budget.spent, budget.remaining()]])
    steps.append(["budget-binding-timeout", outcome(lambda: d.call_within_budget(
        budget, 20.0, takes(8.0, error=TimeoutError("slow")),
        is_timeout=lambda e: isinstance(e, TimeoutError), circuit=circuit), root)])
    steps.append(["spent-after", [budget.spent, budget.remaining()]])
    steps.append(["exhausted", outcome(lambda: d.call_within_budget(
        budget, 20.0, takes(1.0), is_timeout=lambda e: False), root)])
    fresh = d.UnitBudget(100.0, clock=clock)
    steps.append(["own-timeout-unchanged", outcome(lambda: d.call_within_budget(
        fresh, 5.0, takes(5.0, error=TimeoutError("slow")), is_timeout=lambda e: isinstance(e, TimeoutError)), root)])
    steps.append(["not-a-timeout", outcome(lambda: d.call_within_budget(
        fresh, 5.0, takes(1.0, error=ValueError("other")), is_timeout=lambda e: isinstance(e, TimeoutError)), root)])
    before = fresh.spent
    steps.append(["uncharged", [outcome(lambda: d.call_within_budget(
        fresh, 5.0, takes(2.0, error=OSError("refused")), is_timeout=lambda e: False, uncharged=lambda e: True), root),
        fresh.spent - before]])
    steps.append(["circuit", [circuit.record_outcome(True), circuit.record_outcome(True), circuit.is_open(),
                              circuit.consecutive_deadlines, circuit.record_outcome(False)]])
    steps.append(["circuit-open-call", outcome(lambda: d.call_within_budget(
        fresh, 5.0, lambda t: t, is_timeout=lambda e: False, circuit=circuit), root)])
    steps.append(["bad-args", [outcome(lambda: d.UnitBudget(0), root), outcome(lambda: d.DeadlineCircuit(0), root),
                               outcome(lambda: fresh.charge(-1), root)]])
    steps.append(["refused", [d.refused_before_dispatch(d.CircuitOpen("x")), d.refused_before_dispatch(d.BudgetExhausted("y")),
                              d.refused_before_dispatch(d.DeadlineExpired("z")), d.refused_before_dispatch(ValueError())]])
    path = base / "notes.json"
    notes = d.DeadlineNotes(path)
    steps.append(["notes", [notes.lookup("k"), notes.record_expiry("k", "subject-1", "run-1"),
                            notes.record_expiry("k", "subject-1", "run-2"), notes.lookup("k")]])
    notes.clear("k")
    steps.append(["notes-cleared", notes.lookup("k")])
    notes.record_expiry("k2", "s", "r")
    steps.append(["notes-bytes", path.read_bytes().decode("ascii")])
    path.write_text("{not json", encoding="utf-8")
    steps.append(["notes-corrupt", outcome(lambda: d.DeadlineNotes(path).lookup("k2"), root)])
    path.write_text(json.dumps({"schema_version": 99, "notes": {}}), encoding="utf-8")
    steps.append(["notes-wrong-schema", outcome(lambda: d.DeadlineNotes(path).lookup("k2"), root)])
    return {"steps": steps}


def _scenario_failure_cache(side: Any, base: Path) -> dict:
    fc = side.failure_cache
    root = str(base)
    steps: list = []
    details = [{"code": "c  1", "file": "f.txt", "message": "bad\n  thing"}, {"code": "c0", "file": None, "message": "m"}]
    steps.append(["fingerprint", fc.error_fingerprint("parse", details)])
    steps.append(["fingerprint-order", fc.error_fingerprint("parse", list(reversed(details)))])
    path = base / "fail.json"
    cache = fc.FailureCache(path)
    rec = fc.FailureRecord("stage", "u1", "key-1", fc.error_fingerprint("parse", details), fc.FINGERPRINT_VERSION, "parse", "bad thing")
    steps.append(["lookup-empty", cache.lookup("stage", "u1", "key-1")])
    cache.record(rec)
    steps.append(["lookup", cache.lookup("stage", "u1", "key-1")])
    steps.append(["lookup-other-key", cache.lookup("stage", "u1", "key-2")])
    steps.append(["lookup-other-version", cache.lookup("stage", "u1", "key-1", fingerprint_version=2)])
    steps.append(["bytes", path.read_bytes().decode("ascii")])
    steps.append(["bad-record", outcome(lambda: cache.record(dataclasses.replace(rec, code="")), root)])
    cache.clear("stage", "u1")
    steps.append(["cleared", cache.lookup("stage", "u1", "key-1")])
    path.write_text("[]", encoding="utf-8")
    steps.append(["corrupt", outcome(lambda: fc.FailureCache(path).lookup("stage", "u1", "key-1"), root)])
    return {"steps": steps}


class TestWavesAndGraphs:
    def test_a_flat_wave_agrees(self, sides, tmp_path):
        out = check(sides, tmp_path, _scenario_flat_wave)
        assert out["all"] == ["u0", "u2", "u3", "u4"] and out["capped"] == ["u0", "u2"]
        assert (out["is_graph"], out["is_ordered"]) == (False, False)

    def test_a_sequential_graph_agrees_and_each_block_is_diagnosed(self, sides, tmp_path):
        out = check(sides, tmp_path, _scenario_sequential_graph)
        steps = {label: value for label, value in out["steps"]}
        assert steps["initial"] == ["g0"]
        assert steps["too-wide"]["raised"]["mro"][0] == "UnsafeGraphParallelismError"
        assert steps["unapplied"] == [] and "finalize_run" in steps["why-unapplied"]
        assert steps["released"] == ["g1"]
        assert steps["blocked-by-failed"] == [] and steps["why-failed"]
        assert steps["is"] == [True, True]

    def test_the_parallel_graph_rules_agree(self, sides, tmp_path):
        out = check(sides, tmp_path, _scenario_parallel_graph)
        steps = {label: value for label, value in out["steps"]}
        # is_dependency_ordered vs is_graph_strategy: a parallel graph is ordered, not a graph walk.
        assert steps["is"] == [False, True]
        assert steps["initial"] == ["a", "b"]
        assert steps["capped"] == ["a"]
        assert steps["a-running"] == ["b"]
        assert steps["gated"] == ["c", "d"]
        assert steps["gated-needs-parallel"]["raised"]["mro"][0] == "TypeError"
        assert steps["mismatch-wave"]["raised"]["mro"][0] == "GraphRegistrationMismatchError"
        assert steps["mismatch-gated"]["raised"]["mro"][0] == "GraphRegistrationMismatchError"
        assert steps["bad-cycle"]["raised"]["mro"][0] == "GraphError"

    def test_the_scheduler_agrees(self, sides, tmp_path):
        out = check(sides, tmp_path, _scenario_scheduler)
        steps = {label: value for label, value in out["steps"]}
        assert steps["run-graph"]["order"] == ["a", "b", "c", "z"]  # d is gated by the failed b
        assert steps["run-graph"]["state"]["gated"] == ["d"]
        assert steps["admit-quiet"] == [] and steps["admit-after-quiet"] == ["c"]
        assert steps["requeue-not-running"]["raised"]["mro"][0] == "ValueError"
        assert steps["run-graph-error"]["raised"]["mro"][0] == "RuntimeError"


class TestStdlibModules:
    def test_deadlines_agree(self, sides, tmp_path):
        out = check(sides, tmp_path, _scenario_deadlines)
        steps = {label: value for label, value in out["steps"]}
        assert steps["budget-binding-timeout"]["raised"]["mro"][0] == "DeadlineExpired"
        assert steps["exhausted"]["raised"]["mro"][:3] == ["BudgetExhausted", "DeadlineExpired", "NotDispatched"]
        assert steps["own-timeout-unchanged"]["raised"]["mro"][0] == "TimeoutError"
        assert steps["spent-after"] == [11.0, 0.0] and steps["uncharged"][1] == 0.0
        assert steps["circuit-open-call"]["raised"]["mro"][0] == "CircuitOpen"

    def test_the_failure_cache_agrees(self, sides, tmp_path):
        out = check(sides, tmp_path, _scenario_failure_cache)
        steps = {label: value for label, value in out["steps"]}
        assert steps["fingerprint"] == steps["fingerprint-order"]
        assert steps["lookup"] is not None and steps["lookup-other-key"] is None and steps["cleared"] is None

    @pytest.mark.parametrize("name", ["scheduler", "deadlines", "failure_cache"])
    def test_the_stdlib_only_modules_import_only_the_standard_library(self, name):
        """scheduler, deadlines and failure_cache are stdlib-only; their one internal edge is the atomic-JSON writer."""
        tree = ast.parse((_FINAL_DIR / f"{name}.py").read_text(encoding="utf-8"))
        foreign = []
        for item in ast.walk(tree):
            if isinstance(item, ast.Import):
                names = [alias.name for alias in item.names]
            elif isinstance(item, ast.ImportFrom):
                names = [item.module or ""]
                if item.module == "llm_kit.storage":
                    names = [f"llm_kit.storage.{a.name}" for a in item.names]
            else:
                continue
            for imported in names:
                if imported in ("llm_kit.storage.atomic_json", "__future__"):
                    continue
                if imported.split(".")[0] not in sys.stdlib_module_names:
                    foreign.append(imported)
        assert not foreign, foreign

    def test_the_atomic_json_writer_is_stdlib_only(self):
        tree = ast.parse((_LIB / "llm_kit" / "storage" / "atomic_json.py").read_text(encoding="utf-8"))
        names = {
            (alias.name if isinstance(n, ast.Import) else (n.module or ""))
            for n in ast.walk(tree)
            if isinstance(n, (ast.Import, ast.ImportFrom))
            for alias in (n.names if isinstance(n, ast.Import) else [n])
        }
        assert all(n.split(".")[0] in sys.stdlib_module_names or n == "__future__" for n in names), names


# --------------------------------------------------------------------------
# Controller, inline driver, protocol, worker pack
# --------------------------------------------------------------------------


def _flat(side: Any) -> Any:
    return side.workunit.FlatChunkStrategy(select=lambda s: [])


def _adapter(side: Any, applied: list, **kwargs: Any) -> Any:
    wu = side.workunit

    def apply(unit_id: str, payload: Any) -> None:
        applied.append((unit_id, payload))
        if unit_id == "u5":
            raise side.model.ApplyRejected("declined by the adapter")

    def validator(candidate: Any, context: Any) -> list:
        if candidate == "BAD":
            return [side.contract.Rejection(kind="not_ok", severity=side.contract.Severity.HARD, detail="nope")]
        return []

    spec = dict(
        unit_for=lambda uid: wu.WorkUnit(id=uid, payload={"n": uid}),
        user_for=lambda u: f"user:{u.id}",
        system_for=lambda u: "sys",
        parse_fn=lambda t: t.upper(),
        validators=(validator,),
        apply=apply,
        adapter_version="v1",
    )
    spec.update(kwargs)
    return side.adapter.RunAdapter(**spec)


def _scenario_controller_flow(side: Any, base: Path) -> dict:
    root = str(base)
    wu = side.workunit
    fresh = side.classify.FreshnessState
    unit_ids = [f"u{i}" for i in range(8)]
    store = new_store(side, base)
    store.create_run("run-1", driver="inline", backend="mock", model="m", adapter_version="v1", created_at=1.0)
    store.register_units("run-1", unit_ids, at=1.0)
    applied: list = []
    adapter = _adapter(side, applied)
    marked: list = []
    gates = (
        side.gate.Gate("not-u1", lambda w: "u1 is excluded" if w.id == "u1" else None),
        side.gate.Gate("sticky-u2", lambda w: "u2 unsupported" if w.id == "u2" else None, sticky=True),
    )

    def freshness_of(w: Any) -> Any:
        return fresh.FRESH if w.id == "u3" else fresh.MISSING

    work_units = [wu.WorkUnit(id=u, payload=u) for u in unit_ids]
    log: list = []
    rec = lambda label, fn: _record(log, label, fn, root)  # noqa: E731
    wave = rec("prepare", lambda: side.controller.prepare_run(
        store, "run-1", _flat(side), work_units, gates=gates, freshness_of=freshness_of,
        mark_unsupported=lambda uid, reason: marked.append((uid, reason)), max_wave_size=4, at=10.0))
    log.append(["marked", marked])

    def generate(w: Any) -> str:
        if w.id == "u6":
            raise side.platform.PipelineHaltError("rate_limit", "You've hit your limit")
        return f"text-{w.id}"

    ready = [u for u in store.list_units("run-1") if u.unit_id in {"u0", "u4", "u6", "u7"}]
    rec("run-wave", lambda: side.inline.run_wave(store, "run-1", ready, adapter, generate=generate, at=20.0))
    rec("run-wave-no-callable", lambda: side.inline.run_wave(store, "run-1", ready, adapter))
    rec("run-wave-both", lambda: side.inline.run_wave(store, "run-1", ready, adapter, generate=generate, backend=object()))
    rec("unfinished", lambda: [u.unit_id for u in side.controller.unfinished_units(store, "run-1")])
    rec("halted-run", lambda: store.get_run("run-1"))
    rec("resume", lambda: side.controller.resume_run(store, "run-1"))
    rec("pause", lambda: side.controller.pause_run(store, "run-1", detail="stop for lunch"))
    rec("run-wave-paused", lambda: side.inline.run_wave(store, "run-1", ready, adapter, generate=generate, at=21.0))
    rec("resume-2", lambda: side.controller.resume_run(store, "run-1"))
    ready2 = [u for u in store.list_units("run-1") if u.unit_id in {"u6", "u7"} and u.state.value == "pending"]
    rec("run-wave-2", lambda: side.inline.run_wave(store, "run-1", ready2, adapter, generate=lambda w: f"again-{w.id}", at=22.0))
    # u5 is accepted by hand so that its apply is declined
    store.claim_unit("run-1", "u5", "hand", lease_seconds=60.0, at=23.0)
    store.accept_unit("run-1", "u5", 1, text="five", at=23.5)
    rec("finalize", lambda: side.controller.finalize_run(store, "run-1", adapter, at=30.0))
    rec("finalize-again", lambda: side.controller.finalize_run(store, "run-1", adapter, at=40.0))
    log.append(["applied", applied])
    rec("statuses", lambda: side.status.compute_status(store, "run-1", now=100.0))
    store.register_units("run-1", ["u8"], at=45.0)
    store.claim_unit("run-1", "u8", "w", lease_seconds=60.0, at=46.0)
    rec("record-halt", lambda: side.controller.record_halt(
        store, "run-1", "u8", 1, side.platform.PipelineHaltError("auth", "bad key"), at=47.0))
    rec("record-halt-stale", lambda: side.controller.record_halt(
        store, "run-1", "u8", 1, side.platform.PipelineHaltError("auth", "bad key"), at=48.0))
    rec("after-halt", lambda: [store.get_run("run-1"), store.get_unit("run-1", "u8")])
    rec("unknown-run", lambda: side.controller.resume_run(store, "ghost"))
    rec("missing-text-finalize", lambda: _finalize_without_text(side, base))
    return {"log": log, "ledger": dump_ledger(base / "run.db")}


def _finalize_without_text(side: Any, base: Path) -> Any:
    store = new_store(side, base, "notext.db")
    store.create_run("r", driver="d", backend="b", model="m", adapter_version="v1", created_at=1.0)
    store.register_units("r", ["x"], at=1.0)
    store.claim_unit("r", "x", "w", at=2.0)
    store.accept_unit("r", "x", 1, at=3.0)
    return side.controller.finalize_run(store, "r", _adapter(side, []), at=4.0)


def _scenario_graph_controller(side: Any, base: Path) -> dict:
    root = str(base)
    wu = side.workunit
    store = new_store(side, base)
    store.create_run("run-1", driver="inline", backend="mock", model="m", adapter_version="v1", created_at=1.0)
    store.register_units("run-1", ["g0", "g1", "g2"], at=1.0)
    applied: list = []
    adapter = _adapter(side, applied)
    log: list = []
    rec = lambda label, fn: _record(log, label, fn, root)  # noqa: E731
    walk = wu.GraphWalkStrategy(order=lambda s: ["g0", "g1", "g2"])
    wrong = wu.GraphWalkStrategy(order=lambda s: ["g1", "g0", "g2"])
    units = [wu.WorkUnit(id=g, payload=g) for g in ("g0", "g1", "g2")]
    rec("order-mismatch", lambda: side.controller.prepare_run(store, "run-1", wrong, units, at=2.0))
    rec("prepare-1", lambda: [u.unit_id for u in side.controller.prepare_run(store, "run-1", walk, units, at=2.0)])
    rec("run-1", lambda: side.inline.run_wave(
        store, "run-1", store.list_units("run-1")[:1], adapter, generate=lambda w: "t0", at=3.0))
    rec("prepare-unapplied", lambda: side.controller.prepare_run(store, "run-1", walk, units, at=4.0))
    rec("finalize-1", lambda: side.controller.finalize_run(store, "run-1", adapter, at=5.0))
    rec("prepare-2", lambda: [u.unit_id for u in side.controller.prepare_run(store, "run-1", walk, units, at=6.0)])
    rec("why", lambda: side.wave.graph_block_reason(store, "run-1", walk))
    return {"log": log, "ledger": dump_ledger(base / "run.db")}


def _mask_live(value: Any) -> Any:
    """Blank the wall-clock fields of a protocol reply (the verbs read ``time.time()``)."""
    if isinstance(value, dict):
        return {
            k: ("<time>" if (k.endswith("_at") or k in ("elapsed_s", "oldest_in_flight_age_s")) and v is not None else _mask_live(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_mask_live(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"1[0-9]{9}[.][0-9]+", "<time>", value)
    return value


def _scenario_protocol(side: Any, base: Path) -> dict:
    version = side.protocol.PROTOCOL_VERSION
    store = new_store(side, base)
    store.create_run("run-1", driver="inline", backend="mock", model="m", adapter_version="v1", created_at=1.0)
    store.register_units("run-1", ["u0", "u1", "u2"], at=1.0)
    applied: list = []
    adapter = _adapter(side, applied)
    handlers = side.protocol.build_handlers(
        store, adapter, strategy=_flat(side), gates=(side.gate.Gate("g", lambda w: "no" if w.id == "u2" else None),),
        max_wave_size=5)

    def send(verb: Any, payload: Any = None, *, ver: Any = None) -> Any:
        envelope = {"protocol_version": version if ver is None else ver, "verb": verb, "payload": payload or {}}
        return _mask_live(side.protocol.dispatch(envelope, handlers))

    def fence(unit: str) -> int:
        return store.get_unit("run-1", unit).fencing_token

    log: list = []
    ids = {"run_id": "run-1"}
    log.append(["verbs", [list(side.protocol.VERBS), version, sorted(handlers)]])
    log.append(["malformed", [side.protocol.dispatch(e, handlers) for e in ("x", [], {"verb": "claim"}, {"protocol_version": version},
                                                                         {"protocol_version": version, "verb": ""},
                                                                         {"protocol_version": version, "verb": "claim", "payload": 5})]])
    log.append(["unknown-verb", send("teleport")])
    log.append(["wrong-version", send("status", ids, ver="1")])
    log.append(["prepare", send("prepare", {**ids, "unit_ids": ["u0", "u1", "u2"]})])
    log.append(["prepare-bad", send("prepare", {**ids, "unit_ids": "u0"})])
    log.append(["claim", send("claim", {**ids, "unit_id": "u0", "worker_id": "w"})])
    log.append(["claim-twice", send("claim", {**ids, "unit_id": "u0", "worker_id": "w2"})])
    log.append(["claim-no-worker", send("claim", {**ids, "unit_id": "u1"})])
    log.append(["read", send("read", {**ids, "unit_id": "u0"})])
    log.append(["renew", send("renew", {**ids, "unit_id": "u0", "fencing_token": fence("u0")})])
    log.append(["renew-stale", send("renew", {**ids, "unit_id": "u0", "fencing_token": 99})])
    log.append(["submit-bad", send("submit", {**ids, "unit_id": "u0", "fencing_token": fence("u0"), "text": "bad"})])
    log.append(["submit-stale", send("submit", {**ids, "unit_id": "u0", "fencing_token": 99, "text": "fine"})])
    log.append(["submit-no-text", send("submit", {**ids, "unit_id": "u0", "fencing_token": fence("u0")})])
    log.append(["submit", send("submit", {**ids, "unit_id": "u0", "fencing_token": fence("u0"), "text": "fine",
                                           "usage": {"input_tokens": 3, "output_tokens": 4}})])
    log.append(["claim-u1", send("claim", {**ids, "unit_id": "u1", "worker_id": "w"})])
    log.append(["fail", send("fail", {**ids, "unit_id": "u1", "fencing_token": fence("u1"), "error": "boom"})])
    log.append(["claim-u1-again", send("claim", {**ids, "unit_id": "u1", "worker_id": "w"})])
    log.append(["fail-terminal-code", send("fail", {**ids, "unit_id": "u1", "fencing_token": fence("u1"),
                                                     "error": "wrong cwd", "terminal": True, "code": "env_mismatch"})])
    log.append(["fail-bad-code", send("fail", {**ids, "unit_id": "u1", "fencing_token": fence("u1"), "code": "nope"})])
    log.append(["status", send("status", ids)])
    log.append(["status-unknown", send("status", {"run_id": "ghost"})])
    log.append(["pause", send("pause", {**ids, "detail": "later"})])
    log.append(["claim-paused", send("claim", {**ids, "unit_id": "u2", "worker_id": "w"})])
    log.append(["resume", send("resume", ids)])
    log.append(["finalize", send("finalize", ids)])
    log.append(["finalize-again", send("finalize", ids)])
    log.append(["applied", applied])
    mismatched = _adapter(side, [], adapter_version="v2")
    other = side.protocol.build_handlers(store, mismatched, strategy=_flat(side))
    log.append(["version-mismatch", [_mask_live(side.protocol.dispatch(
        {"protocol_version": version, "verb": verb, "payload": {**ids, "unit_id": "u2", "worker_id": "w", "unit_ids": ["u2"]}}, other))
        for verb in ("prepare", "claim", "resume", "finalize", "fail")]])
    return {"log": log, "ledger": dump_ledger(base / "run.db", mask_times=True)}


def _counting_uuid4() -> Callable[[], Any]:
    import itertools
    import uuid

    counter = itertools.count(1)
    return lambda: uuid.UUID(int=next(counter))


def _scenario_workerpack(side: Any, base: Path) -> dict:
    from unittest import mock

    with mock.patch.object(side.workerpack.uuid, "uuid4", _counting_uuid4()):
        return _workerpack_steps(side, base)


def _workerpack_steps(side: Any, base: Path) -> dict:
    wp = side.workerpack
    root = str(base)
    command = wp.WorkerCommand(argv=("python", "tool.py", "--db", "{run_id}.db"), answer_dir=str(base / "ans"),
                               envelope_dir=str(base / "env"))
    plain_command = wp.WorkerCommand(argv=("tool",), answer_dir=str(base / "ans"))
    steps: list = []
    steps.append(["paths", [wp.answer_path_for(command, "run/1", "unit:a b"), wp.envelope_path_for(command, "run-1", "u/1", "submit"),
                            wp.claim_envelope_path_for(command, "r", "u", "w x"), wp.envelope_path_for(plain_command, "r", "u", "read"),
                            plain_command.resolved_envelope_dir, command.resolved_envelope_dir]])
    steps.append(["fence", [wp.format_fenced_answer(7, "body\nmore"), wp.parse_fenced_answer(wp.format_fenced_answer(7, "body\nmore"), 7)]])
    steps.append(["fence-errors", [
        outcome(lambda: wp.parse_fenced_answer("no fence here", 7), root),
        outcome(lambda: wp.parse_fenced_answer(wp.format_fenced_answer(6, "x"), 7), root),
        outcome(lambda: wp.parse_fenced_answer("", 1), root),
    ]])
    steps.append(["invocations", wp.enumerate_worker_invocations(command, "run-1", "u0", "w1")])
    steps.append(["workflow-invocations", wp.enumerate_workflow_invocations(command, "run-1", "u0", "w1")])
    steps.append(["envelopes", wp.worker_envelopes_for(command, "run-1", "u0", "w1")])
    steps.append(["claim-text", wp.claim_envelope_text("run-1", "u0", "w1")])
    good = json.loads(wp.claim_envelope_text("run-1", "u0", "w1"))
    steps.append(["mismatch", [
        wp.envelope_file_mismatch(wp.claim_envelope_path_for(command, "run-1", "u0", "w1"), good),
        wp.envelope_file_mismatch(wp.claim_envelope_path_for(command, "run-1", "u9", "w1"), good),
        wp.envelope_file_mismatch(wp.envelope_path_for(command, "run-1", "u0", "read"), good),
        wp.envelope_file_mismatch(str(base / "free-form.json"), good),
        wp.envelope_file_mismatch(wp.envelope_path_for(command, "run-1", "u0", "read"), "not a mapping"),
    ]])
    store = seeded(side, base, unit_ids=("u0", "u1", "u2"))
    store.claim_unit("run-1", "u0", "a", lease_seconds=5.0, at=10.0)
    store.claim_unit("run-1", "u0", "b", lease_seconds=5.0, at=20.0)
    store.claim_unit("run-1", "u1", "a", lease_seconds=5.0, at=10.0)
    store.record_dispatch("run-1", "u1", "a", at=10.0)
    store.claim_unit("run-1", "u2", "a", lease_seconds=500.0, at=10.0)
    steps.append(["reclaimable", [u.unit_id for u in wp.reclaimable_units(store, "run-1", at=100.0)]])
    steps.append(["reclaim-count", [wp.reclaim_attempt_count(store, "run-1", u) for u in ("u0", "u1", "u2")]])
    adapter = _adapter(side, [], expected_unit_seconds=60.0)
    steps.append(["wave-args-unpriced", outcome(lambda: wp.build_wave_args(
        store, "run-1", _adapter(side, []), command, 2, at=100.0), root)])
    steps.append(["wave-args", outcome(lambda: wp.build_wave_args(store, "run-1", adapter, command, 2, at=100.0), root)])
    steps.append(["wave-args-flat-cap", outcome(lambda: wp.build_wave_args(
        store, "run-1", adapter, command, 3, strategy=_flat(side), max_wave_size=1, at=100.0), root)])
    steps.append(["wave-args-graph-wide", outcome(lambda: wp.build_wave_args(
        store, "run-1", adapter, command, 3, strategy=side.workunit.GraphWalkStrategy(order=lambda s: ["u0", "u1", "u2"]),
        max_wave_size=2, at=100.0), root)])
    return {"steps": steps, "files": sorted(p.name for p in base.rglob("*") if p.is_file() and p.suffix != ".db" and "-wal" not in p.name and "-shm" not in p.name)}


class TestControllerAndDrivers:
    def test_the_controller_and_inline_driver_agree_step_by_step(self, sides, tmp_path):
        out = check(sides, tmp_path, _scenario_controller_flow)
        no_harness_errors(out)
        log = {label: value for label, value in out["log"]}
        assert [u["unit_id"] for u in log["prepare"]["ok"]] == ["u0", "u4", "u5", "u6"]
        assert log["marked"] == [["u2", "u2 unsupported"]]
        assert log["run-wave"] == {"ok": ["u0", "u4"]}  # u6 halts the run
        assert log["run-wave-no-callable"]["raised"]["mro"][0] == "ValueError"
        assert log["run-wave-both"]["raised"]["mro"][0] == "ValueError"
        assert log["halted-run"]["ok"]["halted_kind"] == "rate_limit"
        assert log["missing-text-finalize"]["raised"]["mro"][0] == "MissingAcceptedTextError"
        assert ("u5", "FIVE") in [tuple(a) for a in log["applied"]]

    def test_a_graph_run_is_ordered_and_apply_aware(self, sides, tmp_path):
        out = check(sides, tmp_path, _scenario_graph_controller)
        no_harness_errors(out)
        log = {label: value for label, value in out["log"]}
        assert log["order-mismatch"]["raised"]["mro"][0] == "GraphOrderMismatchError"
        assert log["prepare-unapplied"]["raised"]["mro"][0] == "UnappliedPredecessorError"
        assert log["prepare-2"] == {"ok": ["g1"]}

    def test_the_protocol_verbs_agree(self, sides, tmp_path):
        out = check(sides, tmp_path, _scenario_protocol)
        log = {label: value for label, value in out["log"]}
        assert out["log"][0][1][1] == "2"
        assert log["prepare"]["ok"] is True
        assert log["claim-twice"]["error"]["type"] == "AlreadyClaimedError"
        assert log["renew-stale"]["error"]["type"] == "StaleFenceError"
        assert log["submit-bad"]["result"]["accepted"] is False
        assert log["submit-stale"]["error"]["type"] == "StaleFenceError"
        assert log["submit"]["result"]["accepted"] is True
        assert log["fail-bad-code"]["error"]["type"] == "MalformedEnvelopeError"
        assert log["claim-paused"]["error"]["type"] == "RunHaltedError"
        assert log["finalize-again"]["result"]["applied"] == []
        assert all(r["error"]["type"] == "AdapterVersionMismatchError" for r in log["version-mismatch"][:4] + [log["version-mismatch"][4]])

    def test_the_worker_pack_agrees(self, sides, tmp_path):
        out = check(sides, tmp_path, _scenario_workerpack)
        steps = {label: value for label, value in out["steps"]}
        assert steps["reclaimable"] == ["u0"]
        assert steps["reclaim-count"] == [1, 0, 0]
        assert steps["wave-args-unpriced"]["raised"]["mro"][0] == "ValueError"
        assert "ok" in steps["wave-args"] and "ok" in steps["wave-args-flat-cap"]
        assert steps["fence-errors"][0]["raised"]["mro"][0] == "MissingAnswerFenceError"
        assert steps["fence-errors"][1]["raised"]["mro"][0] == "AnswerFenceMismatchError"


# --------------------------------------------------------------------------
# Background-session driver (claude_bg), against a scripted fake process
# --------------------------------------------------------------------------


class FakeRunner:
    """Script responses by longest matching argv prefix and log every call.

    The module's sole process boundary: nothing here starts a real ``claude``.
    """

    def __init__(self, scripts: Any = None, *, hook: Optional[Callable[[list], None]] = None) -> None:
        self.scripts = dict(scripts or {})
        self.hook = hook
        self.calls: list = []

    def script(self, argv_prefix: Any, response: Any) -> None:
        self.scripts[tuple(argv_prefix)] = response

    def __call__(self, argv: Any, **kwargs: Any) -> Any:
        values = list(argv)
        self.calls.append([values, {k: kwargs[k] for k in sorted(kwargs)}])
        if self.hook is not None:
            self.hook(values)
        matches = [(p, r) for p, r in self.scripts.items() if tuple(values[: len(p)]) == p]
        if not matches:
            raise AssertionError(f"FakeRunner: no script for argv {values!r}")
        response = max(matches, key=lambda item: len(item[0]))[1]
        if isinstance(response, list):
            return response.pop(0) if len(response) > 1 else response[0]
        return response


def healthy_runner(*, agents_json_body: str = "[]", version: Any = ("2.1.233", "", 0),
                   hook: Optional[Callable[[list], None]] = None) -> FakeRunner:
    agents_help = (
        "Usage: claude agents [options] [command]\n\nManage background sessions "
        "(subcommands: stop, logs, rm, respawn)"
    )
    runner = FakeRunner(hook=hook)
    runner.script(("claude", "--version"), version)
    runner.script(("claude", "agents", "--json"), (agents_json_body, "", 0))
    runner.script(("claude", "agents", "--help"), (agents_help, "", 0))
    runner.script(("claude", "agents", "stop", "--help"), (agents_help, "", 0))
    for verb in ("stop", "logs", "rm", "respawn"):
        runner.script(("claude", verb, "--help"), (f"Usage: claude {verb}", "", 0))
    runner.script(("claude", "--bg", "-p"), ("", "error: option '-p' cannot be used with '--bg'", 1))
    runner.script(("claude", "stop"), ("stopped", "", 0))
    runner.script(("claude", "rm"), ("removed", "", 0))
    return runner


def bg_record(*, id: str = "a1b2c3d4", session_id: str = "sess-1", state: str = "working", **extra: Any) -> dict:
    record = {"kind": "background", "id": id, "sessionId": session_id, "state": state}
    record.update(extra)
    return record


def launch_response(short_id: str = "f77605d2") -> tuple:
    """The shape of a real ``claude --bg`` launch (stdout, stderr, rc); the separator is U+00B7."""
    stdout = (
        "backgrounded " + chr(0xB7) + f" {short_id}\n"
        f"  claude agents             list sessions\n"
        f"  claude attach {short_id}    open in this terminal\n"
    )
    return stdout, "Starting background service" + chr(0xFFFD) + "\n", 0


def _cli(side: Any, runner: Any) -> Any:
    return side.claude_bg.ClaudeCli(executable="claude", runner=runner)


def _worker_command(side: Any, base: Path) -> Any:
    (base / "answers").mkdir(exist_ok=True)
    (base / "envelopes").mkdir(exist_ok=True)
    return side.workerpack.WorkerCommand(
        argv=("python", "mytool.py", "run"), answer_dir=str(base / "answers"), envelope_dir=str(base / "envelopes"))


def _calls(runner: FakeRunner) -> list:
    """The runner's call log, minus the environment mapping (compared once, separately)."""
    return [[argv, {k: v for k, v in kwargs.items() if k != "env"}] for argv, kwargs in runner.calls]


def _drive_wave(side: Any, base: Path, variant: str) -> dict:
    from unittest import mock

    cb = side.claude_bg
    store = seeded(side, base, unit_ids=("u0", "u1") if variant == "misconfiguration" else ("u0",))
    wc = _worker_command(side, base)
    clock = {"t": 1000.0}
    state = {"launched": False, "agents": 0}
    sequences = {
        "completes-unaccepted": ["done"], "completes-accepted": ["done"], "halt-classified": ["working", "failed"],
    }
    sequence = sequences.get(variant, ["working"])

    def hook(argv: list) -> None:
        if variant == "misconfiguration":
            return
        if argv[1] == "--bg" and argv[2:3] != ["-p"]:
            state["launched"] = True
        elif argv[1:3] == ["agents", "--json"] and state["launched"]:
            seen = state["agents"]
            state["agents"] += 1
            current = sequence[min(seen, len(sequence) - 1)]
            runner.script(("claude", "agents", "--json"),
                          (json.dumps([bg_record(id="abc12345", session_id="sess-1", state=current)]), "", 0))
            if variant == "completes-accepted":
                unit = store.get_unit("run-1", "u0")
                if unit.state.value == "claimed":
                    store.accept_unit("run-1", "u0", unit.fencing_token, text="worker text", at=clock["t"])

    runner = healthy_runner(hook=hook)
    if variant == "misconfiguration":
        runner.script(("claude", "--bg"), launch_response("baaaaaad"))
        runner.script(("claude", "agents", "--json"),
                      (json.dumps([bg_record(id="baaaaaad", session_id="sess-bad", state="failed")]), "", 0))
    else:
        runner.script(("claude", "--bg"), launch_response("abc12345"))
    if variant == "halt-classified":
        projects = Path.home() / ".claude" / "projects" / "proj"
        projects.mkdir(parents=True, exist_ok=True)
        (projects / "sess-1.jsonl").write_text(
            '{"type": "error", "text": "You have hit your limit, try again later"}\n', encoding="utf-8")
    adapter = side.adapter.RunAdapter(unit_seconds_for=lambda u: 5.0)
    if variant == "second-dispatcher":
        store.acquire_dispatcher_lease("run-1", "someone-else", lease_seconds=120.0, at=1000.0)
    if variant == "lease-lost":
        stolen = {"done": False}

        def steal(unit: Any) -> float:
            if not stolen["done"]:
                stolen["done"] = True
                assert store.acquire_dispatcher_lease("run-1", "rival", lease_seconds=999.0, at=5000.0) is not None
            return 5.0

        adapter = side.adapter.RunAdapter(unit_seconds_for=steal)
    if variant == "reclaim-exhausted":
        store.claim_unit("run-1", "u0", "w1", lease_seconds=1.0, at=1000.0)
        store.claim_unit("run-1", "u0", "w2", lease_seconds=1.0, at=1002.0)
        store.claim_unit("run-1", "u0", "w3", lease_seconds=1.0, at=1004.0)
    wave = [] if variant == "reclaim-exhausted" else store.list_units("run-1")
    with mock.patch.object(cb, "_mint_worker_id", lambda: "our-dispatcher"):
        report = cb.dispatch_wave(
            store, "run-1", wave, adapter, cli=_cli(side, runner), worker_command=wc,
            max_agents=2 if variant == "misconfiguration" else 1, max_reclaims_per_unit=2,
            at=1010.0 if variant == "reclaim-exhausted" else 1000.0,
            sleep_fn=lambda s: None, clock_fn=lambda: clock["t"])
    return _mask_live(plain({
        "report": report,
        "calls": _calls(runner),
        "ledger": dump_ledger(base / "run.db", mask_times=True),
        "run": store.get_run("run-1"),
        "open": store.open_dispatches("run-1"),
    }, str(base)))


_WAVE_VARIANTS = (
    "completes-unaccepted", "completes-accepted", "misconfiguration", "second-dispatcher",
    "lease-lost", "reclaim-exhausted", "halt-classified",
)


def _scenario_dispatch_unit(side: Any, base: Path) -> dict:
    root = str(base)
    cb = side.claude_bg
    store = seeded(side, base, unit_ids=("u0", "u1", "u2"))
    wc = _worker_command(side, base)
    log: list = []
    rec = lambda label, fn: _record(log, label, fn, root)  # noqa: E731

    runner = healthy_runner()
    runner.script(("claude", "--bg"), launch_response("abc12345"))
    runner.script(("claude", "agents", "--json"),
                  [("[]", "", 0), (json.dumps([bg_record(id="abc12345", session_id="sess-1", state="working")]), "", 0)])
    unit = store.get_unit("run-1", "u0")
    opened = rec("dispatch-ok", lambda: cb.dispatch_unit(
        store, "run-1", unit, _cli(side, runner), wc, worker_id="worker-a", clock_fn=lambda: 1000.0,
        sleep_fn=lambda s: None, at=1000.0))
    log.append(["calls-ok", _calls(runner)])
    rec("attached", lambda: store.open_dispatches("run-1"))
    rec("prompt", lambda: cb.build_launch_prompt(wc, "run-1", "u0", "worker-a", 1))

    rec("dispatch-again-open-dispatch", lambda: cb.dispatch_unit(
        store, "run-1", unit, _cli(side, runner), wc, worker_id="worker-b", clock_fn=lambda: 1001.0,
        sleep_fn=lambda s: None, at=1001.0))

    # A banner with no parsable id: adopt the single new record.
    runner2 = healthy_runner()
    runner2.script(("claude", "--bg"), ("launched something\n", "", 0))
    runner2.script(("claude", "agents", "--json"),
                   [("[]", "", 0), (json.dumps([bg_record(id="eeee0001", session_id="sess-2", state="working")]), "", 0)])
    rec("dispatch-banner-drift", lambda: cb.dispatch_unit(
        store, "run-1", store.get_unit("run-1", "u1"), _cli(side, runner2), wc, worker_id="worker-c",
        clock_fn=lambda: 1002.0, sleep_fn=lambda s: None, at=1002.0))
    log.append(["calls-drift", _calls(runner2)])

    # A launch that never confirms: the corpse is removed and the claim released.
    runner3 = healthy_runner()
    runner3.script(("claude", "--bg"), launch_response("dead0001"))
    runner3.script(("claude", "agents", "--json"),
                   (json.dumps([bg_record(id="dead0001", session_id="sess-3", state="failed")]), "", 0))
    rec("dispatch-failed-launch", lambda: cb.dispatch_unit(
        store, "run-1", store.get_unit("run-1", "u2"), _cli(side, runner3), wc, worker_id="worker-d",
        clock_fn=lambda: 1003.0, sleep_fn=lambda s: None, at=1003.0))
    log.append(["calls-failed", _calls(runner3)])
    rec("after-failed-launch", lambda: [store.get_unit("run-1", "u2"), store.open_dispatches("run-1")])

    # A launch that raises: the claim is released and the dispatch settled.
    def boom(argv: Any, **kwargs: Any) -> Any:
        if list(argv)[1] == "--bg":
            raise OSError("spawn failed")
        return ("[]", "", 0)

    store.register_units("run-1", ["u3"], at=1004.0)
    rec("dispatch-spawn-error", lambda: cb.dispatch_unit(
        store, "run-1", store.get_unit("run-1", "u3"), _cli(side, boom), wc, worker_id="worker-e",
        clock_fn=lambda: 1004.0, sleep_fn=lambda s: None, at=1004.0))
    rec("after-spawn-error", lambda: [store.get_unit("run-1", "u3"), store.open_dispatches("run-1")])
    return {"log": log, "ledger": dump_ledger(base / "run.db", mask_times=True), "files": sorted(
        p.relative_to(base).as_posix() for p in base.rglob("*.json") if "answers" in p.parts or "envelopes" in p.parts)}


def _scenario_supervise_tick(side: Any, base: Path) -> dict:
    cb = side.claude_bg
    store = seeded(side, base, unit_ids=("u0", "u1", "u2", "u3"))
    adapter = side.adapter.RunAdapter(unit_seconds_for=lambda u: 30.0)
    open_dispatches = {}
    for unit_id, short in (("u0", "a0000000"), ("u1", "a1111111"), ("u2", "a2222222"), ("u3", "a3333333")):
        claim = store.claim_unit("run-1", unit_id, f"w-{unit_id}", lease_seconds=100.0, at=1000.0)
        store.record_dispatch("run-1", unit_id, f"w-{unit_id}", session_id=f"sess-{unit_id}", at=1000.0)
        open_dispatches[unit_id] = cb.OpenDispatch(
            unit_id=unit_id, worker_id=f"w-{unit_id}", session_id=f"sess-{unit_id}", id=short,
            fencing_token=claim.fencing_token, claimed_by=f"w-{unit_id}")
    store.accept_unit("run-1", "u1", 1, text="done", at=1001.0)
    records = [
        bg_record(id="a0000000", session_id="sess-u0", state="working"),
        bg_record(id="a1111111", session_id="sess-u1", state="working"),
        bg_record(id="a2222222", session_id="sess-u2", state="done"),
        # u3's session vanished from the listing
    ]
    runner = healthy_runner(agents_json_body=json.dumps(records))
    ticks = []
    for at in (1010.0, 1500.0):
        ticks.append(cb.supervise_tick(store, "run-1", _cli(side, runner), adapter, open_dispatches, at=at))
    return {"ticks": ticks, "calls": _calls(runner), "ledger": dump_ledger(base / "run.db", mask_times=True),
            "open": sorted(open_dispatches)}


def _scenario_claude_cli(side: Any, base: Path) -> dict:
    root = str(base)
    cb = side.claude_bg
    log: list = []
    rec = lambda label, fn: _record(log, label, fn, root)  # noqa: E731
    runner = healthy_runner()
    cli = _cli(side, runner)
    rec("launch", lambda: cli.launch_bg("do the thing", extra_args=("--agent", "pipeline-worker"), env={"A": "1"}, cwd=root, timeout=5.0))
    rec("launch-dash-prompt", lambda: cli.launch_bg("-rf", extra_args=("--allowedTools", "Read")))
    rec("agents", lambda: cli.agents_json())
    rec("agents-mine", lambda: cli.agents_json(all_sessions=False, timeout=3.0))
    rec("stop", lambda: cli.stop("s1"))
    rec("rm", lambda: cli.rm("s1"))
    rec("respawn", lambda: cli.respawn("s1"))
    rec("version", lambda: cli.version())
    log.append(["calls", _calls(runner)])
    rec("no-executable", lambda: _no_path_executable(cb, runner))
    rec("has-logs-method", lambda: hasattr(cli, "logs"))
    rec("preflight", lambda: cb.preflight(cli, env={}))
    rec("preflight-billing", lambda: cb.preflight(cli, env={"ANTHROPIC_API_KEY": "sk-x"}))
    rec("preflight-empty-key", lambda: cb.preflight(cli, env={"ANTHROPIC_API_KEY": ""}))
    bad = healthy_runner(agents_json_body="{not json")
    rec("preflight-bad-agents", lambda: cb.preflight(_cli(side, bad), env={}))
    drift = healthy_runner(version=("Claude Code vNext-experimental", "", 0))
    rec("preflight-version-drift", lambda: cb.preflight(_cli(side, drift), env={}))
    spawned = healthy_runner()
    spawned.script(("claude", "--bg", "-p"), ("backgrounded", "", 0))
    rec("preflight-bg-dash-p-accepted", lambda: cb.preflight(_cli(side, spawned), env={}))
    return {"log": log}


def _no_path_executable(cb: Any, runner: Any) -> Any:
    from unittest import mock

    with mock.patch.object(cb.shutil, "which", lambda name: None):
        return cb.ClaudeCli(executable=None, runner=runner).resolve_executable()


def _scenario_claude_parsing(side: Any, base: Path) -> dict:
    root = str(base)
    cb = side.claude_bg
    banners = [
        "backgrounded " + chr(0xB7) + " ff97012c\n", "backgrounded " + chr(0xC2) + chr(0xB7) + " ff97012c\n",
        "backgrounded - ABCDEF12\n", "nothing here", "", "backgrounded \u00b7 12\n",
    ]
    texts = [
        "[]", json.dumps([bg_record()]), json.dumps([bg_record(extra="x"), bg_record(id="b", state="failed")]),
        "{}", "[1, 2]", "not json", json.dumps([{"kind": "background"}]), json.dumps([bg_record(state=5)]),
        json.dumps([bg_record(id="")]),
    ]
    return {
        "banners": [cb._parse_launch_session_id(b) for b in banners],
        "agents": [outcome(lambda t=t: cb.parse_agents_json(t), root) for t in texts],
        "stderr": [cb._stderr_excerpt(x) for x in (None, "", "short", "x" * 1000)],
        "billing": [cb._billing_diverting_hits(e) for e in ({}, {"ANTHROPIC_API_KEY": "k"}, {"ANTHROPIC_API_KEY": ""},
                                                          {"CLAUDE_CODE_USE_BEDROCK": "1", "OTHER": "2"})],
        "constants": [cb.DEFAULT_LAUNCH_CONFIRM_SECONDS, cb.DEFAULT_LAUNCH_POLL_INTERVAL_S, cb.DEFAULT_MAX_AGENTS,
                      cb.DEFAULT_BATCH_SIZE, cb.DEFAULT_DISPATCH_POLL_INTERVAL_S, cb.DEFAULT_DISPATCHER_LEASE_SECONDS,
                      cb.DEFAULT_SYSTEMIC_FAILURE_HALT_THRESHOLD, cb.DEFAULT_WAVE_STALL_SECONDS,
                      cb.DEFAULT_TERMINAL_EXIT_GRACE_SECONDS, cb.DEFAULT_TRANSCRIPT_TAIL_LINES],
    }


def _scenario_worker_environment(side: Any, base: Path) -> dict:
    from unittest import mock

    root = str(base)
    cb = side.claude_bg
    ad = side.adapter
    store = seeded(side, base, unit_ids=("u0",))
    log: list = []
    rec = lambda label, fn: _record(log, label, fn, root)  # noqa: E731
    cwd = os.getcwd()
    env_decl = ad.WorkerEnvironment(
        required_vars=("PIPE_MODE",), forbidden_vars=("DROP_ME",), cwd_vars=("PIPE_CWD",), require_cwd=True)
    live = {"PIPE_MODE": "fast", "PIPE_CWD": cwd}
    base_env = {"PATH": "/bin", "DROP_ME": "1", "ANTHROPIC_API_KEY": "sk", "KEEP": "yes"}
    with mock.patch.dict(os.environ, live), mock.patch.dict(os.environ, {"DROP_ME": ""}):
        snapshot = env_decl.snapshot()
        log.append(["snapshot", snapshot])
        store.create_run("run-env", driver="d", backend="b", model="m", adapter_version="v1", created_at=2.0,
                         environment=snapshot)
        run = store.get_run("run-env")
        adapter = ad.RunAdapter(environment=env_decl)
        rec("compose", lambda: cb.compose_worker_environment(run, adapter, base=base_env))
        rec("compose-default", lambda: cb.compose_worker_environment(
            store.get_run("run-1"), ad.RunAdapter(), base=base_env))
        # worker-side check: the live environment against the recorded snapshot
        rec("check-ok", lambda: env_decl.check(snapshot, run_id="run-env"))
        rec("check-default-is-a-no-op", lambda: ad.WorkerEnvironment().check({"X": "1"}))
        rec("check-required-changed", lambda: env_decl.check({**snapshot, "PIPE_MODE": "slow"}, run_id="run-env"))
        rec("check-recorded-cwd-differs", lambda: env_decl.check({**snapshot, "__cwd__": cwd + "-elsewhere"}, run_id="run-env"))
        with mock.patch.dict(os.environ, {"PIPE_CWD": cwd + "-elsewhere"}):
            rec("check-cwd-var-elsewhere", lambda: env_decl.check(snapshot, run_id="run-env"))
        with mock.patch.dict(os.environ, {"DROP_ME": "1"}):
            rec("check-forbidden-set", lambda: env_decl.check(snapshot, run_id="run-env"))
        with mock.patch.dict(os.environ, {"PIPE_CWD": cwd + os.sep}):
            rec("check-cwd-var-trailing-separator", lambda: env_decl.check(snapshot, run_id="run-env"))
        rec("materialize", lambda: env_decl.materialize(snapshot))
        # protocol-mount enforcement: the run's recorded snapshot against the adapter's declaration
        rec("compat-ok", lambda: ad.require_compatible_environment(run, adapter))
        with mock.patch.dict(os.environ, {"PIPE_MODE": "slow"}):
            rec("compat-refused", lambda: ad.require_compatible_environment(run, adapter))
        rec("compat-version", lambda: ad.require_compatible_adapter(run, ad.RunAdapter(adapter_version="v9")))
        rec("compat-version-ok", lambda: ad.require_compatible_adapter(run, ad.RunAdapter(adapter_version="v1")))
        # create-run anchor: a declared cwd var must resolve to the orchestrator's cwd
        rec("creatable-ok", lambda: ad.require_creatable_environment("run-env", env_decl, {"PIPE_CWD": cwd}))
        rec("creatable-trailing-separator", lambda: ad.require_creatable_environment(
            "run-env", env_decl, {"PIPE_CWD": cwd + os.sep}))
        rec("creatable-unset-is-skipped", lambda: ad.require_creatable_environment("run-env", env_decl, {}))
        rec("creatable-elsewhere", lambda: ad.require_creatable_environment(
            "run-env", env_decl, {"PIPE_CWD": cwd + "-elsewhere"}))
        rec("creatable-posix-style", lambda: ad.require_creatable_environment(
            "run-env", env_decl, {"PIPE_CWD": "/d/dev/example-project/main"}))
        rec("overlap-refused", lambda: ad.WorkerEnvironment(required_vars=("A",), forbidden_vars=("A",)))
    return {"log": log}


class TestBackgroundDriver:
    @pytest.mark.parametrize("variant", _WAVE_VARIANTS)
    def test_dispatch_wave_agrees(self, sides, tmp_path, variant):
        out = check(sides, tmp_path, lambda side, base: _drive_wave(side, base, variant))
        report = out["report"]
        expected = {
            "completes-unaccepted": lambda r: r["dispatched"] == ["u0"] and r["settled"] == {"u0": "done_unaccepted"},
            "completes-accepted": lambda r: r["dispatched"] == ["u0"] and r["accepted"] == ["u0"],
            "misconfiguration": lambda r: r["aborted_reason"] == "launch_misconfiguration" and r["dispatched"] == [],
            "second-dispatcher": lambda r: r["aborted_reason"] == "dispatcher_lease_held_by_another_dispatcher"
            and r["dispatcher_acquired"] is False,
            "lease-lost": lambda r: r["aborted_reason"] == "dispatcher_lease_lost",
            "reclaim-exhausted": lambda r: r["failed_exhausted"] == ["u0"],
            "halt-classified": lambda r: r["dispatched"] == ["u0"],
        }[variant]
        assert expected(report), (variant, report)

    def test_a_halt_found_in_a_settled_session_halts_the_run(self, sides, tmp_path):
        out = check(sides, tmp_path, lambda side, base: _drive_wave(side, base, "halt-classified"))
        assert out["run"]["halted_kind"] == "rate_limit"

    def test_dispatch_unit_agrees_across_its_failure_paths(self, sides, tmp_path):
        out = check(sides, tmp_path, _scenario_dispatch_unit)
        no_harness_errors(out)
        log = {label: value for label, value in out["log"]}
        assert log["dispatch-ok"]["ok"]["worker_id"] == "worker-a"
        assert log["dispatch-again-open-dispatch"]["raised"]["mro"][0] == "IntegrityError"
        assert log["dispatch-banner-drift"]["ok"]["id"] == "eeee0001"
        assert log["dispatch-failed-launch"]["raised"]["mro"][0] == "LaunchMisconfigurationError"
        assert log["dispatch-spawn-error"]["raised"]["mro"][0] == "OSError"
        assert [c[0][1] for c in log["calls-failed"] if c[0][1] in ("stop", "rm")] == ["stop", "rm"]

    def test_supervision_agrees(self, sides, tmp_path):
        out = check(sides, tmp_path, _scenario_supervise_tick)
        assert out["ticks"][0]["renewed"] == ["u0"]

    def test_the_claude_cli_and_preflight_agree(self, sides, tmp_path):
        out = check(sides, tmp_path, _scenario_claude_cli)
        log = {label: value for label, value in out["log"]}
        assert log["has-logs-method"] == {"ok": False}  # no `claude logs` channel by construction
        assert log["preflight-billing"]["raised"]["mro"][0] == "PreflightError"
        assert log["preflight"]["ok"]["__type__"] == "PreflightReport"
        assert log["preflight-bg-dash-p-accepted"]["raised"]["mro"][0] == "PreflightError"

    def test_banner_and_agent_list_parsing_agree(self, sides, tmp_path):
        out = check(sides, tmp_path, _scenario_claude_parsing)
        assert out["banners"][0] == "ff97012c" and out["banners"][3] is None

    def test_the_worker_environment_rules_agree(self, sides, tmp_path):
        out = check(sides, tmp_path, _scenario_worker_environment)
        no_harness_errors(out)
        log = {label: value for label, value in out["log"]}
        for passing in ("check-ok", "check-default-is-a-no-op", "check-cwd-var-trailing-separator", "compat-ok",
                        "compat-version-ok", "creatable-ok", "creatable-trailing-separator", "creatable-unset-is-skipped"):
            assert log[passing] == {"ok": None}, (passing, log[passing])
        for refusing in ("check-required-changed", "check-recorded-cwd-differs", "check-cwd-var-elsewhere",
                         "check-forbidden-set", "compat-refused", "creatable-elsewhere", "creatable-posix-style"):
            assert log[refusing]["raised"]["mro"][0] == "WorkerEnvironmentMismatchError", (refusing, log[refusing])
        assert log["compat-version"]["raised"]["mro"][0] == "AdapterVersionMismatchError"
        assert log["check-forbidden-set"]["raised"]["attrs"]["forbidden"] is True

    def test_the_default_runner_passes_utf8_and_never_reaches_a_real_process(self, sides, monkeypatch):
        seen = []
        for side in sides:
            calls: list = []

            class _Proc:
                stdout, stderr, returncode = "out", "err", 3

            def fake_run(argv: Any, **kwargs: Any) -> Any:
                calls.append((list(argv), {k: v for k, v in kwargs.items() if k != "env"}, kwargs.get("env")))
                return _Proc()

            monkeypatch.setattr(side.claude_bg.subprocess, "run", fake_run)
            result = side.claude_bg._default_runner(["claude", "--version"], stdin="in", env={"A": "1"}, cwd="/x", timeout=2.0)
            seen.append((result, calls))
        assert seen[0] == seen[1]
        assert seen[1][1][0][1]["encoding"] == "utf-8" and seen[1][1][0][1]["errors"] == "replace"

    def test_a_supervised_launch_under_claudx_is_refused_before_any_process(self, sides, monkeypatch):
        """CLAUDE_PLUGIN_TEST sessions cannot contain a supervised claude: refuse, never spawn."""
        monkeypatch.setenv("CLAUDE_PLUGIN_TEST", "1")
        outcomes = []
        for side in sides:
            runner = FakeRunner()
            cli = _cli(side, runner)
            outcomes.append((outcome(lambda: cli.launch_bg("p", env={"CLAUDE_PLUGIN_TEST": "1"})), runner.calls))
        assert_equal_outcomes(plain(outcomes[0]), plain(outcomes[1]))
        assert outcomes[1][1] == []  # no process boundary was crossed


# --------------------------------------------------------------------------
# The modules the move carries verbatim, and the public surface
# --------------------------------------------------------------------------

#: Legacy dotted module -> final dotted module, for imports.
_IMPORT_MAP = (
    ("content_pipeline.execution._atomic_json", "llm_kit.storage.atomic_json"),
    ("content_pipeline.execution", "llm_kit.pipeline.execution"),
    ("content_pipeline.llm.platform", "llm_kit.pipeline.runtime.platform"),
    ("content_pipeline.pipeline.workunit", "llm_kit.pipeline.core.workunit"),
    ("content_pipeline.pipeline.gate", "llm_kit.pipeline.core.gate"),
    ("content_pipeline.pipeline.single_pass", "llm_kit.pipeline.core.single_pass"),
    ("content_pipeline.freshness.classify", "llm_kit.pipeline.artifacts.freshness.classify"),
    ("content_pipeline.validate", "llm_kit.pipeline.core.validate"),
)

#: Modules whose code is the legacy code, line for line (imports and docstrings aside).
_VERBATIM = (
    "adapter.py", "controller.py", "deadlines.py", "failure_cache.py", "parallel_graph.py", "protocol.py",
    "scheduler.py", "status.py", "wave.py", "workerpack.py", "drivers/__init__.py", "drivers/inline.py",
    "drivers/claude_bg.py",
)


def _strip(path: Path) -> tuple[str, list[str]]:
    """The tree of a module without imports and docstrings, plus its normalized imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: list[str] = []

    class Strip(ast.NodeTransformer):
        def _import(self, node: ast.AST) -> None:
            imports.append(ast.dump(node))
            return None

        def visit_Expr(self, node: ast.Expr) -> Any:
            # `require_fleet(...)` is the one deliberate package-mode refusal
            # (llm_kit._runtime); it changes nothing in legacy mode.
            call = node.value
            if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "require_fleet":
                return None
            return self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            return self._import(node)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            return self._import(node)

        def _doc(self, node: Any) -> Any:
            self.generic_visit(node)
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
            return node

        visit_Module = visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _doc

    return ast.dump(Strip().visit(tree)), imports


def _legacy_imports_by_name(path: Path) -> set[tuple[str, str, str]]:
    """(module, imported name, alias) for every import, with the legacy -> final module map applied."""
    found: set[tuple[str, str, str]] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                name, as_name = alias.name, alias.asname or alias.name
                if module == "content_pipeline.execution" and name == "_atomic_json":
                    module, name = "llm_kit.storage", "atomic_json"
                for old, new in _IMPORT_MAP:
                    if module == old or module.startswith(old + "."):
                        module = new + module[len(old):]
                        break
                found.add((module, name, as_name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add((alias.name, "", alias.asname or alias.name))
    return found


class TestVerbatimModules:
    @pytest.mark.parametrize("relative", _VERBATIM)
    def test_the_code_is_the_legacy_code(self, relative):
        legacy_tree, _ = _strip(_LEGACY_DIR / relative)
        final_tree, _ = _strip(_FINAL_DIR / relative)
        assert final_tree == legacy_tree

    @pytest.mark.parametrize("relative", _VERBATIM + ("store.py", "model.py", "events.py", "interrupts.py"))
    def test_the_imports_are_the_legacy_imports_mapped_to_final_names(self, relative):
        """Only the shared-kernel modules (storage, execution) add imports; none is lost."""
        legacy = _legacy_imports_by_name(_LEGACY_DIR / relative)
        final = _legacy_imports_by_name(_FINAL_DIR / relative)
        added = {item for item in final - legacy
                 if not item[0].startswith(("llm_kit.storage", "llm_kit.execution", "llm_kit._runtime"))}
        assert not added, added
        lost = {item for item in legacy - final if item[0].startswith(("content_pipeline", "llm_scripting_kit"))}
        assert not lost, lost
        if relative not in ("store.py", "model.py", "events.py", "interrupts.py"):
            assert {item for item in final if not item[0].startswith("llm_kit._runtime")} == legacy

    @pytest.mark.parametrize("module", _MODULES)
    def test_the_public_surface_is_the_legacy_surface(self, sides, module):
        legacy, final = (getattr(side, module) for side in sides)

        def public(mod: Any) -> set:
            return {k for k, v in vars(mod).items()
                    if not k.startswith("_") and not isinstance(v, types.ModuleType)}

        # model no longer imports ``dataclasses.field`` (the one class that used it moved to the shared kernel).
        dropped_imports = {"model": {"field"}}.get(module, set())
        assert public(legacy) - dropped_imports <= public(final)
        # The only additions are shared-kernel names the slice imports (probe specs and shapes).
        extras = public(final) - public(legacy)
        assert all(getattr(getattr(final, k), "__module__", "").startswith(("llm_kit.execution", "llm_kit.storage"))
                   for k in extras), extras
        # Private helpers: the slices moved to the shared kernels and the network probe took the storage form.
        private = {"events": ({"_too_old"}, {"_event_spec", "_render_support_failure"}),
                   "interrupts": ({"_binds", "_contract_too_old"},
                                  {"_adapter", "_contract_spec", "_render_contract_failure",
                                   "_render_validator_failure", "_validator_spec"}),
                   "store": ({"_DRIVE_REMOTE"}, set())}
        def hidden(mod: Any) -> set:
            return {k for k, v in vars(mod).items() if k.startswith("_") and not k.startswith("__")
                    and not isinstance(v, types.ModuleType) and getattr(v, "__module__", mod.__name__) == mod.__name__}
        only_legacy, only_final = private.get(module, (set(), set()))
        assert hidden(legacy) - hidden(final) == only_legacy
        assert hidden(final) - hidden(legacy) == only_final

    def test_the_exports_are_the_legacy_exports(self, sides):
        for module in _MODULES:
            legacy, final = (getattr(side, module) for side in sides)
            assert getattr(final, "__all__", None) == getattr(legacy, "__all__", None), module

    def test_the_sliced_modules_keep_the_legacy_classes_and_functions(self, sides):
        """events, interrupts, model and store delegate to the shared kernels but keep every legacy callable."""
        for module in ("events", "interrupts", "model", "store"):
            legacy, final = (getattr(side, module) for side in sides)
            for name, value in vars(legacy).items():
                if name.startswith("_") or isinstance(value, types.ModuleType):
                    continue
                if isinstance(value, type) or callable(value):
                    if getattr(value, "__module__", None) == legacy.__name__:
                        assert hasattr(final, name), (module, name)

    def test_every_method_of_every_class_is_kept(self, sides):
        legacy, final = sides
        for module in _MODULES + ("inline", "claude_bg"):
            lmod, fmod = getattr(legacy, module), getattr(final, module)
            for name, value in vars(lmod).items():
                if isinstance(value, type) and value.__module__ == lmod.__name__:
                    fclass = getattr(fmod, name)
                    lmethods = {k for k in vars(value) if not k.startswith("__")}
                    fmethods = {k for k in vars(fclass) if not k.startswith("__")}
                    # InterruptResolution is the shared kernel's record; it adds to_mapping for jobs.
                    extra = {"to_mapping"} if (module, name) == ("model", "InterruptResolution") else set()
                    assert lmethods | extra == fmethods, (module, name, lmethods ^ fmethods)


class TestImportBoundary:
    _ALLOWED = ("llm_kit.execution", "llm_kit.storage", "llm_kit.pipeline.core", "llm_kit.pipeline.artifacts",
                "llm_kit.pipeline.runtime", "llm_kit.pipeline.execution",
                "llm_kit._runtime")

    def _imports(self) -> list[tuple[str, int, str]]:
        found = []
        for path in sorted(_FINAL_DIR.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.module:
                    found.append((path.relative_to(_FINAL_DIR).as_posix(), node.lineno, node.module))
                elif isinstance(node, ast.Import):
                    found.extend((path.relative_to(_FINAL_DIR).as_posix(), node.lineno, a.name) for a in node.names)
        return found

    def test_only_the_edge_table_components_are_imported(self):
        edges = [(f, line, mod) for f, line, mod in self._imports() if mod.split(".")[0] == "llm_kit"]
        assert edges, "the package imports nothing from llm_kit; the scan is broken"
        stray = [(f, line, mod) for f, line, mod in edges if not any(
            mod == ok or mod.startswith(ok + ".") for ok in self._ALLOWED)]
        assert not stray, stray

    def test_the_package_reaches_neither_jobs_nor_the_quarantined_copy_nor_an_old_package(self):
        forbidden = ("llm_kit.jobs", "llm_kit._copy", "llm_kit.dispatch", "llm_kit.adapters", "llm_kit.completion",
                     "llm_kit.registry", "llm_kit.frontdoor", "llm_kit.cli", "content_pipeline", "job_kit", "llm_scripting_kit")
        stray = [(f, line, mod) for f, line, mod in self._imports()
                 if any(mod == bad or mod.startswith(bad + ".") for bad in forbidden)]
        assert not stray, stray

    def test_jobs_does_not_import_the_package(self):
        jobs = _LIB / "llm_kit" / "jobs"
        stray = []
        for path in sorted(jobs.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                modules = [node.module or ""] if isinstance(node, ast.ImportFrom) else (
                    [a.name for a in node.names] if isinstance(node, ast.Import) else [])
                stray += [(path.name, node.lineno, m) for m in modules if m.startswith("llm_kit.pipeline.execution")]
        assert not stray, stray

    def test_the_package_re_exports_nothing(self):
        for path in (_FINAL_DIR / "__init__.py", _FINAL_DIR / "drivers" / "__init__.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            assert not [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom, ast.Assign))], path

    def test_every_file_is_ascii(self):
        for path in sorted(_FINAL_DIR.rglob("*.py")):
            path.read_bytes().decode("ascii")


class TestDeliberateDifferences:
    def test_the_version_read_alias_is_the_only_statement_difference(self, sides, tmp_path, monkeypatch):
        """The shared storage read names its column ``version``; the legacy store named it ``v``.

        A read-only SELECT, so no persisted byte and no pragma moves; the result is identical.
        """
        legacy_trace = _statement_trace(sides[0], tmp_path / "legacy", monkeypatch)
        final_trace = _statement_trace(sides[1], tmp_path / "final", monkeypatch)
        changed = [(a, b) for a, b in zip(legacy_trace, final_trace) if a != b]
        assert len(legacy_trace) == len(final_trace)
        assert changed and {a for a, _ in changed} == {"SELECT MAX(version) AS v FROM schema_version"}
        assert {b for _, b in changed} == {"SELECT MAX(version) AS version FROM schema_version"}

    def test_the_interrupt_validator_is_the_llm_kit_validator(self, sides):
        legacy, final = sides
        assert legacy.interrupts._VALIDATOR_MODULE == "llm_scripting_kit.completion.json_schema"
        assert final.interrupts._VALIDATOR_MODULE == "llm_kit.schema.json_schema"
        assert final.interrupts.VALIDATOR_SUBSET == legacy.interrupts.VALIDATOR_SUBSET
        assert final.interrupts.CONTRACT == legacy.interrupts.CONTRACT

    def test_the_atomic_json_writer_is_one_module_in_the_storage_package(self, sides):
        final = sides[1]
        assert final.deadlines._atomic_json.__name__ == "llm_kit.storage.atomic_json"
        assert final.failure_cache._atomic_json is final.deadlines._atomic_json
        legacy = sides[0]
        assert legacy.deadlines._atomic_json.__name__ == "content_pipeline.execution._atomic_json"

    def test_the_interrupt_resolution_record_is_the_shared_kernel_record(self, sides):
        """The record moved to llm_kit.execution.values and carries ``to_mapping`` for jobs; the fields are the legacy fields."""
        legacy, final = sides
        assert final.model.InterruptResolution.__module__ == "llm_kit.execution.values"
        assert legacy.model.InterruptResolution.__module__ == "content_pipeline.execution.model"
        assert [f.name for f in dataclasses.fields(final.model.InterruptResolution)] == [
            f.name for f in dataclasses.fields(legacy.model.InterruptResolution)]
        assert hasattr(final.model.InterruptResolution, "to_mapping")
        assert not hasattr(legacy.model.InterruptResolution, "to_mapping")

    def test_the_frozen_literals_are_unchanged(self, sides):
        for side in sides:
            assert side.events.PLUGIN == "content-pipeline-kit"
            assert side.interrupts.OWNER == "content-pipeline-kit"
            assert side.workerpack.ANSWER_FENCE_PREFIX == "content-pipeline-fence:"
            assert side.protocol.PROTOCOL_VERSION == "2"
            assert side.store.DEFAULT_LEASE_SECONDS == 300.0 and side.store.LEASE_HEADROOM_FACTOR == 2.0
            assert side.store.DEFAULT_BUSY_TIMEOUT_MS == 5000

    def test_the_events_and_interrupts_refusals_name_the_same_floors(self, sides):
        legacy, final = sides
        assert final.interrupts.INTERRUPT_CONTRACT_BOOTSTRAP == legacy.interrupts.INTERRUPT_CONTRACT_BOOTSTRAP
        # the validator floor is the first llm-kit release that ships the module
        # (final_wording.FLOORS); the contract floor is bootstrap's, unchanged
        assert final.interrupts.JSON_SCHEMA_LSK_VERSION == floor_to_final(legacy.interrupts.JSON_SCHEMA_LSK_VERSION)


def _regenerate_fixtures() -> None:
    """Rewrite the committed fixtures from the live implementations."""
    import tempfile

    sys.path.insert(0, str(_LIB))
    for entry in (str(_LSK_LIB), str(_CPK_LIB)):
        sys.path.insert(0, entry)
    legacy, final = _legacy_side(), _final_side()
    mp = pytest.MonkeyPatch()
    try:
        for side, directory in ((legacy, "legacy"), (final, "llm-kit")):
            with tempfile.TemporaryDirectory() as tmp:
                produced = build_schema_fixtures(side, Path(tmp), mp)
            target = _FIXTURES / directory
            target.mkdir(parents=True, exist_ok=True)
            for old in target.glob("*.json"):
                old.unlink()
            for name, data in produced.items():
                (target / name).write_bytes(data)
            print(f"wrote {len(produced)} fixtures to {target}")
    finally:
        mp.undo()


if __name__ == "__main__":
    if sys.argv[1:] == ["--write-fixtures"]:
        os.environ.setdefault("_BOOTSTRAP_GUARD_SKIP_REEXEC", "1")
        _regenerate_fixtures()
    else:
        raise SystemExit("usage: test_pipeline_execution_parity.py --write-fixtures")
