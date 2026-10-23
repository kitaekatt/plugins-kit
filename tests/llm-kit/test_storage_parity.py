"""Differential parity: llm_kit.storage vs the legacy SQLite plumbing.

Legacy sources driven here:

- content_pipeline.execution.store.ExecutionStore (_connect, _ensure_wal,
  _writer, read_transaction, _read_schema_version, looks_like_network_path)
- job_kit.store.JobStore and LedgerReader (same plumbing, read-only variant)
- content_pipeline.llm.spend_ledger.SpendLedger (_connect, _ensure_wal,
  _writer, _reader, looks_like_network_path)
- content_pipeline.execution._atomic_json

Each scenario runs the legacy object and the llm_kit.storage helper, configured
for that product, against fresh databases with ``sqlite3.connect`` replaced by a
recording connection. The recorded sequence of connect arguments, attribute
assignments, executed statements (pragma order), commits, rollbacks and closes
must be equal, as must outcomes including exception types and messages.
"""

from __future__ import annotations

import ctypes
import importlib
import sqlite3
import sys
import types
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

import pytest

from llm_kit.storage import atomic_json, network, schema_version
from llm_kit.storage import connection as storage_connection
from llm_kit.storage import transaction as storage_transaction
from llm_kit.storage.connection import (
    WAL_REPORT_TIMEOUT,
    WAL_RERAISE,
    ConnectionPolicy,
    connect,
    connect_probe,
    connect_readonly,
)

_REPO = Path(__file__).resolve().parents[2]
_LEGACY_PATHS = [
    _REPO / "plugins" / "content-pipeline-kit" / "lib",
    _REPO / "plugins" / "job-kit" / "lib",
    _REPO / "plugins" / "llm-scripting-kit" / "lib",
]


@pytest.fixture(scope="module")
def legacy():
    """Import the legacy modules with a sys.path insert scoped to this fixture."""
    added = [str(p) for p in _LEGACY_PATHS]
    for entry in added:
        sys.path.insert(0, entry)
    try:
        ns = types.SimpleNamespace(
            cpk_store=importlib.import_module("content_pipeline.execution.store"),
            ledger=importlib.import_module("content_pipeline.llm.spend_ledger"),
            atomic=importlib.import_module("content_pipeline.execution._atomic_json"),
            job_store=importlib.import_module("job_kit.store"),
        )
    finally:
        for entry in added:
            sys.path.remove(entry)
    return ns


# ---------------------------------------------------------------------------
# Recording connection
# ---------------------------------------------------------------------------

LOG: list = []
WAL_INJECT = {"fn": None}


class Recorder(sqlite3.Connection):
    """A real connection that logs every statement, assignment and lifecycle call."""

    def execute(self, sql, *args, **kwargs):
        LOG.append(("execute", sql))
        fn = WAL_INJECT["fn"]
        if fn is not None and sql == "PRAGMA journal_mode = WAL":
            fn()
        return super().execute(sql, *args, **kwargs)

    def __setattr__(self, name, value):
        LOG.append(("set", name, repr(value)))
        super().__setattr__(name, value)

    def commit(self):
        LOG.append(("commit",))
        return super().commit()

    def rollback(self):
        LOG.append(("rollback",))
        return super().rollback()

    def close(self):
        LOG.append(("close",))
        return super().close()


@contextmanager
def recording(monkeypatch):
    real = sqlite3.connect
    LOG.clear()

    def spy(database, *args, **kwargs):
        LOG.append(("connect", str(database), kwargs.get("timeout"), bool(kwargs.get("uri"))))
        return real(database, *args, factory=Recorder, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    try:
        yield LOG
    finally:
        monkeypatch.setattr(sqlite3, "connect", real)


def _norm(value, directory: Path):
    """Replace the per-side directory so two sides compare equal."""
    spellings = {
        str(directory),
        directory.as_posix(),
        str(directory.resolve()),
        directory.resolve().as_posix(),
        urllib.parse.quote(directory.resolve().as_posix()),
        urllib.parse.quote(directory.as_posix()),
    }
    if isinstance(value, str):
        for spelling in sorted(spellings, key=len, reverse=True):
            value = value.replace(spelling, "<D>")
        return value
    if isinstance(value, tuple):
        return tuple(_norm(item, directory) for item in value)
    return value


def _log(directory: Path):
    return [_norm(entry, directory) for entry in LOG]


def _outcome(fn, directory: Path):
    try:
        return ("ok", fn())
    except BaseException as exc:  # noqa: BLE001 - parity compares any exception
        cause = type(exc.__cause__).__name__ if exc.__cause__ is not None else None
        return ("raise", type(exc).__name__, _norm(str(exc), directory), cause)


def _observe(conn):
    LOG.append(("state", conn.isolation_level, conn.row_factory is sqlite3.Row))


# ---------------------------------------------------------------------------
# Product factories: a legacy object and the storage policy for that product
# ---------------------------------------------------------------------------

SYNC = "NORMAL"


def _make_cpk(legacy, directory: Path, ms, fresh: bool):
    directory.mkdir(parents=True, exist_ok=True)
    seed = directory / ("seed.db" if fresh else "x.db")
    store = legacy.cpk_store.ExecutionStore(seed, busy_timeout_ms=ms, warn_on_network_path=False)
    store.db_path = directory / "x.db"
    return store, store.db_path


def _make_job(legacy, directory: Path, ms, fresh: bool):
    directory.mkdir(parents=True, exist_ok=True)
    seed = directory / ("seed.db" if fresh else "x.db")
    store = legacy.job_store.JobStore(seed, busy_timeout_ms=ms)
    store.db_path = directory / "x.db"
    return store, store.db_path


def _make_spend(legacy, directory: Path, ms, fresh: bool):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "x.db"
    if fresh:
        ledger = legacy.ledger.SpendLedger(
            path, run_id="r", cap_nano=10**9, busy_timeout_ms=ms,
            synchronous=SYNC, warn_on_network_path=False,
        )
    else:
        ledger = legacy.ledger.create_ledger(
            path, cap_usd=1, run_id="r", busy_timeout_ms=ms,
            synchronous=SYNC, warn_on_network_path=False,
        )
    return ledger, path


def _policy(product: str, ms) -> ConnectionPolicy:
    if product == "cpk":
        return ConnectionPolicy(
            busy_timeout_ms=ms, post_wal_pragmas=(("foreign_keys", "ON"),)
        )
    if product == "job":
        return ConnectionPolicy(
            busy_timeout_ms=ms,
            post_wal_pragmas=(("foreign_keys", "ON"),),
            wal_failure=WAL_RERAISE,
        )
    return ConnectionPolicy(
        busy_timeout_ms=ms,
        post_wal_pragmas=(("synchronous", SYNC),),
        explicit_transactions=True,
    )


FACTORIES = {"cpk": _make_cpk, "job": _make_job, "spend": _make_spend}
PRODUCTS = ["cpk", "job", "spend"]


def _both(legacy, monkeypatch, tmp_path, product, ms, fresh, legacy_act, final_act):
    """Run one scenario on each side; return ((log, outcome), (log, outcome))."""
    results = []
    for side in ("legacy", "final"):
        directory = tmp_path / side
        obj, path = FACTORIES[product](legacy, directory, ms, fresh)
        policy = _policy(product, ms)
        with recording(monkeypatch):
            if side == "legacy":
                outcome = _outcome(lambda: legacy_act(obj), directory)
            else:
                outcome = _outcome(lambda: final_act(obj, path, policy), directory)
            results.append((_log(directory), outcome))
    return results[0], results[1]


# ---------------------------------------------------------------------------
# Connection posture and pragma order
# ---------------------------------------------------------------------------


def _legacy_open(obj):
    with obj._connect() as conn:
        _observe(conn)
    with obj._connect() as conn:
        _observe(conn)


def _final_open(obj, path, policy):
    with connect(path, policy) as conn:
        _observe(conn)
    with connect(path, policy) as conn:
        _observe(conn)


@pytest.mark.parametrize("product", PRODUCTS)
@pytest.mark.parametrize("ms", [5000, 1234, 2500])
def test_connection_pragma_sequence_matches_legacy(legacy, monkeypatch, tmp_path, product, ms):
    left, right = _both(legacy, monkeypatch, tmp_path, product, ms, True, _legacy_open, _final_open)
    assert left == right
    log = left[0]
    # The fresh first open really set WAL, and the second open skipped the write.
    assert log.count(("execute", "PRAGMA journal_mode = WAL")) == 1
    assert left[1][0] == "ok"


def test_pragma_order_is_the_product_order(legacy, monkeypatch, tmp_path):
    """Pin the three orders literally so a refactor cannot move both sides together."""
    expected = {
        "cpk": ["PRAGMA busy_timeout = 1234", "PRAGMA journal_mode", "PRAGMA journal_mode = WAL",
                "PRAGMA foreign_keys = ON"],
        "job": ["PRAGMA busy_timeout = 1234", "PRAGMA journal_mode", "PRAGMA journal_mode = WAL",
                "PRAGMA foreign_keys = ON"],
        "spend": ["PRAGMA busy_timeout = 1234", "PRAGMA journal_mode", "PRAGMA journal_mode = WAL",
                  "PRAGMA synchronous = NORMAL"],
    }
    for product in PRODUCTS:
        obj, path = FACTORIES[product](legacy, tmp_path / product, 1234, True)
        with recording(monkeypatch):
            with connect(path, _policy(product, 1234)) as conn:
                _observe(conn)
        statements = [e[1] for e in LOG if e[0] == "execute"]
        assert statements == expected[product], product
        state = [e for e in LOG if e[0] == "state"][0]
        assert state == ("state", None if product == "spend" else "", True), product


def test_float_busy_timeout_matches_legacy_pipeline_store(legacy, monkeypatch, tmp_path):
    left, right = _both(
        legacy, monkeypatch, tmp_path, "cpk", 1234.7, True, _legacy_open, _final_open
    )
    assert left == right
    assert ("execute", "PRAGMA busy_timeout = 1234") in left[0]


# ---------------------------------------------------------------------------
# WAL retry and failure
# ---------------------------------------------------------------------------


def _locked_n(count):
    state = {"left": count}

    def inject():
        if state["left"] > 0:
            state["left"] -= 1
            raise sqlite3.OperationalError("database is locked")

    return inject


def _locked_always():
    raise sqlite3.OperationalError("database is locked")


def _other_error():
    raise sqlite3.OperationalError("disk I/O error")


def _collapse(log):
    out = []
    for entry in log:
        if not out or out[-1] != entry:
            out.append(entry)
    return out


@pytest.mark.parametrize("product", PRODUCTS)
@pytest.mark.parametrize(
    "name,make_inject,collapse",
    [
        ("retry_then_ok", lambda: _locked_n(2), False),
        ("other_error", lambda: _other_error, False),
        ("deadline", lambda: _locked_always, True),
    ],
)
def test_wal_failure_matches_legacy(legacy, monkeypatch, tmp_path, product, name, make_inject, collapse):
    results = []
    for side in ("legacy", "final"):
        directory = tmp_path / side
        obj, path = FACTORIES[product](legacy, directory, 40, True)
        policy = _policy(product, 40)
        WAL_INJECT["fn"] = make_inject()
        try:
            with recording(monkeypatch):
                if side == "legacy":
                    outcome = _outcome(lambda: _legacy_open(obj), directory)
                else:
                    outcome = _outcome(lambda: _final_open(obj, path, policy), directory)
                log = _log(directory)
        finally:
            WAL_INJECT["fn"] = None
        results.append((_collapse(log) if collapse else log, outcome))
    assert results[0] == results[1]
    outcome = results[0][1]
    if name == "retry_then_ok":
        assert outcome[0] == "ok"
    elif name == "other_error":
        assert outcome[:3] == ("raise", "OperationalError", "disk I/O error")
    else:
        assert outcome[0] == "raise" and outcome[1] == "OperationalError"
        wrapped = product != "job"
        assert ("could not set WAL journal mode within 40 ms" in outcome[2]) == wrapped
        assert (outcome[3] == "OperationalError") == wrapped


def test_wal_failure_modes_are_distinct_and_validated():
    with pytest.raises(ValueError, match="wal_failure"):
        ConnectionPolicy(wal_failure="swallow")
    with pytest.raises(ValueError, match="post_wal_pragmas"):
        ConnectionPolicy(post_wal_pragmas=(("foreign keys", "ON"),))
    assert WAL_REPORT_TIMEOUT != WAL_RERAISE
    assert storage_connection.DEFAULT_BUSY_TIMEOUT_MS == 5000


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


def _legacy_write(boom):
    def act(obj):
        with obj._writer() as conn:
            _observe(conn)
            conn.execute("SELECT 1").fetchone()
            if boom:
                raise ValueError("boom")
    return act


def _final_write(boom, validate_for=None):
    def act(obj, path, policy):
        validate = validate_for(obj) if validate_for else None
        with storage_transaction.write_transaction(
            lambda: connect(path, policy), validate=validate
        ) as conn:
            _observe(conn)
            conn.execute("SELECT 1").fetchone()
            if boom:
                raise ValueError("boom")
    return act


def _spend_validate(ledger):
    def validate(conn):
        ledger._validate_identity(conn)
        ledger._validate_structure(conn)
    return validate


@pytest.mark.parametrize("boom", [False, True])
@pytest.mark.parametrize("product", ["cpk", "job"])
def test_write_transaction_matches_legacy(legacy, monkeypatch, tmp_path, product, boom):
    left, right = _both(
        legacy, monkeypatch, tmp_path, product, 5000, False, _legacy_write(boom), _final_write(boom)
    )
    assert left == right
    assert ("execute", "BEGIN IMMEDIATE") in left[0]
    assert (("rollback",) in left[0]) == boom
    assert (("commit",) in left[0]) == (not boom)


@pytest.mark.parametrize("boom", [False, True])
def test_spend_write_transaction_with_validation_matches_legacy(legacy, monkeypatch, tmp_path, boom):
    left, right = _both(
        legacy, monkeypatch, tmp_path, "spend", 5000, False,
        _legacy_write(boom), _final_write(boom, _spend_validate),
    )
    assert left == right
    selects = [e for e in left[0] if e[0] == "execute" and "policy" in e[1]]
    assert selects, "the validation hook ran inside the transaction"


def test_spend_validation_refusal_rolls_back_like_legacy(legacy, monkeypatch, tmp_path):
    results = []
    for side in ("legacy", "final"):
        directory = tmp_path / side
        ledger, path = _make_spend(legacy, directory, 5000, False)
        ledger._cap_nano += 1  # the handle no longer matches the pinned policy row
        policy = _policy("spend", 5000)
        with recording(monkeypatch):
            if side == "legacy":
                outcome = _outcome(lambda: _legacy_write(False)(ledger), directory)
            else:
                outcome = _outcome(lambda: _final_write(False, _spend_validate)(ledger, path, policy), directory)
            results.append((_log(directory), outcome))
    assert results[0] == results[1]
    assert results[0][1][:2] == ("raise", "LedgerIdentityChanged")
    assert ("rollback",) in results[0][0]
    assert ("commit",) not in results[0][0]


def _legacy_read(attr):
    def act(obj):
        with getattr(obj, attr)() as conn:
            _observe(conn)
            conn.execute("SELECT 1").fetchone()
    return act


def _final_read(validate_for=None):
    def act(obj, path, policy):
        validate = validate_for(obj) if validate_for else None
        with storage_transaction.read_transaction(
            lambda: connect(path, policy), validate=validate
        ) as conn:
            _observe(conn)
            conn.execute("SELECT 1").fetchone()
    return act


@pytest.mark.parametrize("product", ["cpk", "job"])
def test_read_transaction_matches_legacy(legacy, monkeypatch, tmp_path, product):
    left, right = _both(
        legacy, monkeypatch, tmp_path, product, 5000, False,
        _legacy_read("read_transaction"), _final_read(),
    )
    assert left == right
    assert ("execute", "BEGIN") in left[0] and ("commit",) in left[0]


def test_spend_reader_matches_legacy(legacy, monkeypatch, tmp_path):
    left, right = _both(
        legacy, monkeypatch, tmp_path, "spend", 5000, False,
        _legacy_read("_reader"), _final_read(_spend_validate),
    )
    assert left == right
    assert ("execute", "BEGIN") in left[0]


def test_readonly_connection_and_snapshot_match_legacy_ledger_reader(legacy, monkeypatch, tmp_path):
    results = []
    for side in ("legacy", "final"):
        directory = tmp_path / side
        _store, path = _make_job(legacy, directory, 5000, False)
        reader = legacy.job_store.LedgerReader(path, busy_timeout_ms=1500)

        def legacy_act():
            with reader.read_transaction() as conn:
                _observe(conn)
                conn.execute("SELECT 1").fetchone()

        def final_act():
            with storage_transaction.read_transaction(
                lambda: connect_readonly(path, 1500), rollback_on_exit=True
            ) as conn:
                _observe(conn)
                conn.execute("SELECT 1").fetchone()

        with recording(monkeypatch):
            outcome = _outcome(legacy_act if side == "legacy" else final_act, directory)
            results.append((_log(directory), outcome))
    assert results[0] == results[1]
    log = results[0][0]
    assert log[0][3] is True  # opened as a URI
    assert ("execute", "PRAGMA query_only = ON") in log
    assert not any("journal_mode" in e[1] for e in log if e[0] == "execute")
    assert ("commit",) not in log and ("rollback",) in log


def test_readonly_connection_cannot_write(legacy, tmp_path):
    _store, path = _make_job(legacy, tmp_path, 5000, False)
    with connect_readonly(path, 1000) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE nope (x)")


def test_readonly_uri_matches_legacy(legacy, tmp_path):
    _store, path = _make_job(legacy, tmp_path, 5000, False)
    reader = legacy.job_store.LedgerReader(path)
    assert storage_connection.readonly_uri(path) == reader._uri()


def test_missing_validate_hook_and_body_errors_roll_back(tmp_path):
    path = tmp_path / "t.db"
    policy = ConnectionPolicy()
    with connect(path, policy) as conn:
        conn.execute("CREATE TABLE t (x)")
        conn.commit()
    with pytest.raises(ValueError, match="stop"):
        with storage_transaction.write_transaction(lambda: connect(path, policy)) as conn:
            conn.execute("INSERT INTO t VALUES (1)")
            raise ValueError("stop")
    with connect(path, policy) as conn:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Probe connections
# ---------------------------------------------------------------------------


def test_job_probe_prefix_matches_legacy_migrate(legacy, monkeypatch, tmp_path):
    """The job ledger opens a pragma-free probe before its migration loop."""
    results = []
    for side in ("legacy", "final"):
        directory = tmp_path / side
        store, path = _make_job(legacy, directory, 3000, False)
        with recording(monkeypatch):
            if side == "legacy":
                store._migrate()
                end = next(i for i, e in enumerate(LOG) if e[0] == "close")
                log = _log(directory)[: end + 1]
                version = None
            else:
                with connect_probe(path, 3000, set_busy_timeout=False) as probe:
                    version = schema_version.read_schema_version(probe, as_int=True)
                log = _log(directory)
        results.append(log)
    assert results[0] == results[1]
    assert results[0][0][0] == "connect" and results[0][-1] == ("close",)
    assert not any(e[0] == "execute" and e[1].startswith("PRAGMA") for e in results[0])


def test_spend_probe_prefix_matches_legacy_open(legacy, monkeypatch, tmp_path):
    """open_ledger probes with row_factory and busy_timeout, then reads."""
    results = []
    for side in ("legacy", "final"):
        directory = tmp_path / side
        _ledger, path = _make_spend(legacy, directory, 4000, False)
        with recording(monkeypatch):
            if side == "legacy":
                legacy.ledger.open_ledger(path, busy_timeout_ms=4000, warn_on_network_path=False)
                end = next(
                    i for i, e in enumerate(LOG) if e[0] == "execute" and e[1].startswith("SELECT")
                )
                log = _log(directory)[:end]
            else:
                with connect_probe(path, 4000, set_busy_timeout=True):
                    pass
                log = _log(directory)[:-1]  # drop the close; legacy reads on
        results.append(log)
    assert results[0] == results[1]
    assert [e[0] for e in results[0]] == ["connect", "set", "execute"]


# ---------------------------------------------------------------------------
# Schema-version read
# ---------------------------------------------------------------------------


def _version_db(tmp_path, name, statements):
    path = tmp_path / name
    conn = sqlite3.connect(str(path))
    try:
        for statement in statements:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()
    return path


VERSION_CASES = {
    "no_table": [],
    "empty_table": ["CREATE TABLE schema_version (version INTEGER NOT NULL)"],
    "one_row": [
        "CREATE TABLE schema_version (version INTEGER NOT NULL)",
        "INSERT INTO schema_version VALUES (10)",
    ],
    "many_rows": [
        "CREATE TABLE schema_version (version INTEGER NOT NULL)",
        "INSERT INTO schema_version VALUES (3)",
        "INSERT INTO schema_version VALUES (12)",
        "INSERT INTO schema_version VALUES (7)",
    ],
    "text_version": [
        "CREATE TABLE schema_version (version)",
        "INSERT INTO schema_version VALUES ('7')",
    ],
    "other_table": ["CREATE TABLE schema_versions (version INTEGER)"],
}


@pytest.mark.parametrize("case", sorted(VERSION_CASES))
def test_read_schema_version_matches_legacy(legacy, tmp_path, case):
    path = _version_db(tmp_path, case + ".db", VERSION_CASES[case])
    for legacy_fn, as_int in (
        (legacy.cpk_store.ExecutionStore._read_schema_version, False),
        (legacy.job_store.JobStore._read_schema_version, True),
    ):
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            expected = _outcome(lambda: legacy_fn(conn), tmp_path)
            actual = _outcome(
                lambda: schema_version.read_schema_version(conn, as_int=as_int), tmp_path
            )
        finally:
            conn.close()
        assert actual == expected, (case, as_int)


# ---------------------------------------------------------------------------
# looks_like_network_path (carried once; two legacy copies)
# ---------------------------------------------------------------------------

NETWORK_CASES = [
    "//server/share/ledger.sqlite",
    "\\\\server\\share\\ledger.sqlite",
    "/tmp/ledger.sqlite",
    "relative/ledger.sqlite",
    "C:\\data\\ledger.sqlite",
    "Z:\\data\\ledger.sqlite",
    "",
]


def _fake_windll(drive_type):
    def get_drive_type(root):
        if isinstance(drive_type, BaseException):
            raise drive_type
        return drive_type

    return types.SimpleNamespace(kernel32=types.SimpleNamespace(GetDriveTypeW=get_drive_type))


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
@pytest.mark.parametrize("drive_type", [4, 3, OSError("boom")])
def test_looks_like_network_path_matches_both_legacy_copies(
    legacy, monkeypatch, tmp_path, platform, drive_type
):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(ctypes, "windll", _fake_windll(drive_type), raising=False)
    cases = NETWORK_CASES + [tmp_path / "ledger.sqlite", Path(".")]
    for case in cases:
        arg = Path(case) if isinstance(case, Path) else case
        want_store = _outcome(lambda: legacy.cpk_store.looks_like_network_path(arg), tmp_path)
        want_ledger = _outcome(lambda: legacy.ledger.looks_like_network_path(arg), tmp_path)
        got = _outcome(lambda: network.looks_like_network_path(arg), tmp_path)
        assert got == want_store == want_ledger, (platform, drive_type, case)


def test_looks_like_network_path_real_answers(tmp_path):
    assert network.looks_like_network_path(r"\\fileserver\share\run.db") is True
    assert network.looks_like_network_path("//fileserver/share/run.db") is True
    assert network.looks_like_network_path(tmp_path / "run.db") is False


# ---------------------------------------------------------------------------
# Atomic JSON
# ---------------------------------------------------------------------------


def _validate_has_a(payload):
    if "a" not in payload:
        raise KeyError("a")


def _snapshot(directory: Path):
    return sorted(
        (p.relative_to(directory).as_posix(), p.read_bytes()) for p in directory.rglob("*") if p.is_file()
    )


READ_FILES = {
    "missing": None,
    "valid": b'{"a":1}\n',
    "invalid_json": b"{not json",
    "non_ascii": "{\"a\":\"\u00e9\"}".encode("utf-8"),
    "validator_rejects": b'{"b":1}\n',
    "not_a_dict": b"[1,2]\n",
}


@pytest.mark.parametrize("case", sorted(READ_FILES))
def test_atomic_json_read_matches_legacy(legacy, tmp_path, case):
    path = tmp_path / "state.json"
    if READ_FILES[case] is not None:
        path.write_bytes(READ_FILES[case])
    empty = {"a": 0, "nested": {"k": []}}
    want = legacy.atomic.read(path, empty, _validate_has_a, "state")
    got = atomic_json.read(path, empty, _validate_has_a, "state")
    assert got == want
    if case == "missing":
        assert got[0] == empty and got[0] is not empty and got[1] is None
    assert empty == {"a": 0, "nested": {"k": []}}


PAYLOADS = [
    {},
    {"b": 2, "a": [1, 2, {"z": None, "y": True}]},
    {"text": "caf\u00e9 \u2603", "n": 1.5},
]


@pytest.mark.parametrize("index", range(len(PAYLOADS)))
def test_atomic_json_write_bytes_match_legacy(legacy, tmp_path, index):
    left, right = tmp_path / "legacy", tmp_path / "final"
    legacy.atomic.write(left / "deep" / "state.json", PAYLOADS[index])
    atomic_json.write(right / "deep" / "state.json", PAYLOADS[index])
    assert _snapshot(left) == _snapshot(right)
    snap = _snapshot(left)
    assert len(snap) == 1 and snap[0][1].endswith(b"\n")


def test_atomic_json_failed_write_matches_legacy_and_leaves_no_temp(legacy, tmp_path):
    outcomes = []
    for name, module in (("legacy", legacy.atomic), ("final", atomic_json)):
        directory = tmp_path / name
        path = directory / "state.json"
        module.write(path, {"keep": 1})
        before = _snapshot(directory)
        outcome = _outcome(lambda: module.write(path, {"bad": object()}), directory)
        outcomes.append((outcome[:2], _snapshot(directory) == before, len(_snapshot(directory))))
    assert outcomes[0] == outcomes[1]
    assert outcomes[0][0][:2] == ("raise", "TypeError")
    assert outcomes[0][1] is True and outcomes[0][2] == 1


# ---------------------------------------------------------------------------
# Scope: no product DDL, no migration loop
# ---------------------------------------------------------------------------


def test_storage_carries_no_product_ddl_or_migration_loop():
    root = _REPO / "packages" / "katsoft-llm-kit" / "src" / "llm_kit" / "storage"
    forbidden = ("CREATE TABLE", "CREATE TRIGGER", "CREATE INDEX", "INSERT INTO schema_version",
                 "UPDATE schema_version", "_MIGRATIONS", "executescript")
    for source in root.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, (source.name, token)
