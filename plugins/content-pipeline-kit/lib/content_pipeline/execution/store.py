"""SQLite-backed durable run store: runs, units, attempts, claims, leases.

The store is the ONLY place run truth lives. Everything above it -- a
prepare/finalize controller, a worker protocol, a driver -- is a later phase;
this module owns just the primitives A-min.1 settles: run
identity, unit registration, atomic claims with monotonically increasing
fencing tokens, lease expiry, and an append-only attempt/event log with
nullable usage.

Operational posture (not redesigned here):

- **WAL journal mode** -- set on every connection (idempotent; the mode
  persists in the database file once set, but a connection freshly opened
  against a network-copied file cannot assume that, so it is set every time).
- **``busy_timeout`` on every connection** -- default 5000 ms. This is a
  per-CONNECTION PRAGMA (SQLite does not persist it in the file), so it is
  set on every :meth:`ExecutionStore._connect` call, not once at open.
- **``PRAGMA foreign_keys = ON`` on every connection** -- the declared FKs
  (``units.run_id``, ``attempts(run_id, unit_id)``) are enforced by SQLite
  itself, not only by the Python-side existence checks (``_require_run`` /
  ``_require_unit``) that already gate every write path.
- **Connections opened per verb and closed.** The contention profile is many
  short-lived CLI processes (a worker claims, submits, exits), not one
  long-lived pool -- so every public method opens its own connection via
  :meth:`ExecutionStore._connect` and closes it before returning.
  :meth:`ExecutionStore.snapshot` is the one exception: it needs several
  queries not to observe an interleaved write between them, so it opens ONE
  connection and runs them inside one read transaction (:meth:`read_transaction`).
- **Single-writer discipline is a caller convention, not enforced here.** The
  dispatcher is the only long-lived writer by design; worker processes write
  only through short claim/submit transactions. SQLite's own locking (``BEGIN
  IMMEDIATE`` below) is what actually serializes concurrent writers.
- **A loud warning, never a refusal, when the path looks like a network
  filesystem** -- see :func:`looks_like_network_path`. WAL on a network share
  is a known corruption vector, but path detection has false positives and
  the consumer chooses the path, so this warns and proceeds.
- **``:memory:`` is refused.** A per-verb-connection store cannot share a
  private in-memory database across connections (each ``sqlite3.connect(":memory:")``
  is a distinct, empty database) -- see :meth:`ExecutionStore.__init__`.

Fencing tokens are monotonically increasing PER UNIT (a ``next_fencing_token``
counter column bumped on every successful claim, expiry-reclaim included).
Any renew/accept/fail against a token that does not match the unit's CURRENT
fencing token is a :class:`~content_pipeline.execution.model.StaleFenceError`
-- checked FIRST, before any state check, so a fenced-out caller always gets
this one typed error regardless of what the unit's current state is
(terminal, pending, or claimed by someone else). A stale ``accept``/``fail``
additionally records a payload-free
:class:`~content_pipeline.execution.model.AttemptKind.SUPERSEDED` attempt row
(worker, presented token, timestamp) before raising, so a fenced-out
submission is a visible, durable fact -- not a silently discarded one
(a fenced-out late submission is superseded, never applied).

Interrupts are opt-in. ``request_interrupt`` moves a CLAIMED unit to WAITING
on a typed request, and ``resolve_interrupt`` and ``expire_interrupts`` close
the request; see "Waiting on an interrupt" in ``execution.model`` and the
section comment above those verbs. Their two tables exist in every store
(schema step 10) and stay empty for a run that never calls them.
"""

from __future__ import annotations

import ctypes
import json
import sqlite3
import sys
import time
import warnings
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

from content_pipeline.execution import interrupts as _interrupts
from content_pipeline.execution.model import (
    AlreadyClaimedError,
    AttemptKind,
    AttemptRecord,
    ClaimResult,
    DispatchRecord,
    DuplicateUnitError,
    ExecutionError,
    INTERRUPT_POLICIES,
    InterruptExpiredError,
    InterruptRecord,
    InterruptRequest,
    InterruptRequestError,
    InterruptResolution,
    NoOpenDispatchError,
    NotAcceptedError,
    NotClaimedError,
    POLICY_STOP,
    ResolutionConflictError,
    RunHaltedError,
    RunRecord,
    StaleDispatcherLeaseError,
    StaleFenceError,
    TERMINAL_STATES,
    TerminalStateError,
    UnitRecord,
    UnitState,
    UnitWaitingError,
    UnknownInterruptError,
    UnknownRunError,
    UnknownUnitError,
    UsageRecord,
    WaitUnderDispatchError,
)

DEFAULT_BUSY_TIMEOUT_MS = 5000
DEFAULT_LEASE_SECONDS = 300.0
_ERROR_TRUNCATE = 500  # a defensive cap; error text is operational, not content

# Item 2 (A-min.4): the adapter declares COST (expected_unit_seconds), this
# module owns the LEASE FORMULA -- see execution.adapter.RunAdapter's own
# comment for why the split is per-lane rather than one adapter-declared
# lease. The factor rests on ONE measurement (213s, the CHEAP case: no
# retry, no contention) and an asymmetric error (an undersized lease
# destroys a healthy unit after two reclaim-then-fail cycles, the "workflow
# lane has no renewer" gap; an oversized one merely holds a slot longer).
# 2.0 x 213s = 426s sits comfortably under the consumer's own 900s ceiling
# for the same operation. This is a single named module constant precisely
# so a second measurement moves one number, not a design.
LEASE_HEADROOM_FACTOR = 2.0


def lease_for(expected_unit_seconds: Optional[float], *, default: float = DEFAULT_LEASE_SECONDS) -> float:
    """The lease-duration formula for a unit whose adapter declared
    ``expected_unit_seconds`` (see
    :meth:`~content_pipeline.execution.adapter.RunAdapter.resolve_expected_unit_seconds`).

    ``max(default, ...)`` is load-bearing: a declaration may only RAISE the
    lease above ``default``, never shorten it below -- an adapter that
    under-declares its own cost (or declares nothing, ``expected_unit_seconds
    is None``) never makes today's behavior worse, it only ever adds
    headroom. A non-positive or ``None`` declaration is treated as
    "undeclared" and returns ``default`` unchanged, with no warning --
    not knowing your unit cost is not an error (item 2's decision)."""
    if expected_unit_seconds is None or expected_unit_seconds <= 0:
        return default
    return max(default, expected_unit_seconds * LEASE_HEADROOM_FACTOR)

# ---------------------------------------------------------------------------
# Network-path detection -- a loud warning, never a refusal
# ---------------------------------------------------------------------------

_DRIVE_REMOTE = 4  # Windows GetDriveTypeW result for a mapped/UNC network drive


def looks_like_network_path(path: Union[str, Path]) -> bool:
    """Best-effort check whether ``path`` resolves to a network filesystem.

    False positives are tolerated (the caller only warns, never refuses) and
    false negatives are expected on filesystems this function does not know
    how to probe -- it is a heuristic, not a guarantee. Two checks:

    - **UNC form** -- a path starting with ``\\\\`` (or POSIX-style ``//``,
      the form ``pathlib`` normalizes a UNC path to on some platforms) is
      always treated as a network path, no OS call needed.
    - **Windows mapped drive** -- ``GetDriveTypeW`` via ``ctypes`` (stdlib,
      no ``pywin32`` dependency) reports ``DRIVE_REMOTE`` for a mapped
      network drive letter.

    POSIX network mounts (NFS, CIFS/SMB mounted under a local-looking path)
    are not detected -- there is no stdlib-portable way to ask "is this mount
    remote" without parsing ``/proc/mounts``, which is Linux-only and easy to
    get wrong. Under-detection there is accepted; the warning is a courtesy,
    not a safety net.
    """
    text = str(path)
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    if sys.platform == "win32":
        drive = Path(path).resolve().drive
        if drive:
            root = drive + "\\"
            try:
                drive_type = ctypes.windll.kernel32.GetDriveTypeW(root)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 -- best-effort probe, never fatal
                return False
            return drive_type == _DRIVE_REMOTE
    return False


# ---------------------------------------------------------------------------
# Schema / migrations
# ---------------------------------------------------------------------------

# Each entry is one migration STEP: a list of individual statements applied
# with plain ``execute()`` (never ``executescript`` -- see the module
# docstring and ``_migrate`` below for why). ALL remaining steps for a fresh
# or behind-the-times database are applied inside ONE ``BEGIN IMMEDIATE``
# transaction, so a failure partway through rolls back everything, including
# ``schema_version`` itself -- there is no state in which a retry sees
# "table schema_version already exists" from a half-applied migration.
#
# Never edit an existing entry once it has shipped (that is what makes
# "reopen preserves run truth" possible across versions) -- append a new step
# instead.
_MIGRATIONS: List[List[str]] = [
    [
        """
        CREATE TABLE schema_version (
            version INTEGER NOT NULL
        );
        """,
    ],
    [
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            driver TEXT NOT NULL,
            backend TEXT NOT NULL,
            model TEXT NOT NULL,
            adapter_version TEXT NOT NULL,
            created_at REAL NOT NULL,
            halted_kind TEXT,
            halted_detail TEXT,
            halted_at REAL
        );
        """,
    ],
    [
        """
        CREATE TABLE units (
            run_id TEXT NOT NULL REFERENCES runs(id),
            unit_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            state TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            claimed_by TEXT,
            claimed_at REAL,
            fencing_token INTEGER NOT NULL DEFAULT 0,
            lease_expires_at REAL,
            accepted_at REAL,
            failed_at REAL,
            PRIMARY KEY (run_id, unit_id)
        );
        """,
    ],
    [
        "CREATE INDEX idx_units_run_state ON units(run_id, state);",
    ],
    [
        """
        CREATE TABLE attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            unit_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            at REAL NOT NULL,
            worker_id TEXT,
            fencing_token INTEGER,
            error TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_hit_tokens INTEGER,
            FOREIGN KEY (run_id, unit_id) REFERENCES units(run_id, unit_id)
        );
        """,
    ],
    [
        "CREATE INDEX idx_attempts_run_unit ON attempts(run_id, unit_id);",
    ],
    [
        "ALTER TABLE units ADD COLUMN accepted_text TEXT;",
    ],
    [
        # Item 5 (A-min.4): the environment snapshot taken at create-run
        # time (execution.adapter.WorkerEnvironment.snapshot()), stored as a
        # small JSON object. NULL means no snapshot was recorded (an
        # adapter-less create, or a mount whose adapter declared nothing).
        "ALTER TABLE runs ADD COLUMN environment TEXT;",
    ],
    [
        # B1: the run-level dispatcher (launcher-election) lease -- a
        # SEPARATE lease from a per-unit claim lease, held by at most one
        # background-lane dispatcher process at a time. `dispatcher_fence`
        # is a monotonically increasing counter, same shape as a unit's own
        # `fencing_token`, bumped on every successful acquire (see
        # `acquire_dispatcher_lease`).
        "ALTER TABLE runs ADD COLUMN dispatcher_id TEXT;",
        "ALTER TABLE runs ADD COLUMN dispatcher_lease_expires_at REAL;",
        "ALTER TABLE runs ADD COLUMN dispatcher_fence INTEGER DEFAULT 0;",
        # B1: one row per background-session LAUNCH of a unit, layered on
        # top of (never replacing) the unit's own store-level claim. The
        # partial unique index enforces "at most one OPEN dispatch per unit"
        # at the database level -- a second `record_dispatch` for a unit
        # that already has an open (settled_at IS NULL) dispatch fails with
        # sqlite3.IntegrityError rather than silently launching a duplicate
        # worker for it.
        """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            unit_id TEXT NOT NULL,
            worker_id TEXT NOT NULL,
            session_id TEXT,
            launched_at REAL NOT NULL,
            settled_at REAL,
            outcome TEXT,
            cli_version TEXT,
            FOREIGN KEY (run_id, unit_id) REFERENCES units(run_id, unit_id)
        );
        """,
        "CREATE INDEX idx_dispatches_run_unit ON dispatches(run_id, unit_id);",
        "CREATE UNIQUE INDEX idx_dispatches_open_unique ON dispatches(run_id, unit_id) "
        "WHERE settled_at IS NULL;",
    ],
    [
        # Interrupts: a claim holder's typed request to wait for a person's
        # answer, and the one resolution of each request. Two tables, one
        # index and five triggers; no existing table is altered. Rows of
        # both tables are immutable, the two policy columns included.
        # `fencing_token` is the token of the claim that made the request,
        # and UNIQUE(run_id, unit_id, fencing_token) allows one request per
        # claim. The insert trigger allows one UNRESOLVED request per unit.
        """
        CREATE TABLE interrupts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            unit_id TEXT NOT NULL,
            fencing_token INTEGER NOT NULL,
            envelope TEXT NOT NULL,
            kind TEXT NOT NULL,
            request_schema_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL,
            on_rejected TEXT NOT NULL CHECK (on_rejected IN ('stop', 'release')),
            on_expired TEXT NOT NULL CHECK (on_expired IN ('stop', 'release')),
            FOREIGN KEY (run_id, unit_id) REFERENCES units(run_id, unit_id),
            UNIQUE (run_id, unit_id, fencing_token)
        );
        """,
        "CREATE INDEX idx_interrupts_run_unit ON interrupts(run_id, unit_id, id);",
        """
        CREATE TABLE interrupt_resolutions (
            interrupt_id INTEGER PRIMARY KEY REFERENCES interrupts(id),
            outcome TEXT NOT NULL CHECK (outcome IN ('answered', 'rejected', 'expired')),
            input_json TEXT,
            reason TEXT,
            resolved_at REAL NOT NULL
        );
        """,
        """
        CREATE TRIGGER interrupts_one_open_per_unit BEFORE INSERT ON interrupts
            WHEN EXISTS (
                SELECT 1 FROM interrupts AS open
                WHERE open.run_id = NEW.run_id AND open.unit_id = NEW.unit_id
                  AND NOT EXISTS (
                      SELECT 1 FROM interrupt_resolutions AS r
                      WHERE r.interrupt_id = open.id))
            BEGIN SELECT RAISE(ABORT, 'unit already has an unresolved interrupt'); END;
        """,
        """
        CREATE TRIGGER interrupts_immutable_update BEFORE UPDATE ON interrupts
            BEGIN SELECT RAISE(ABORT, 'interrupt records are immutable'); END;
        """,
        """
        CREATE TRIGGER interrupts_immutable_delete BEFORE DELETE ON interrupts
            BEGIN SELECT RAISE(ABORT, 'interrupt records are immutable'); END;
        """,
        """
        CREATE TRIGGER resolutions_immutable_update BEFORE UPDATE ON interrupt_resolutions
            BEGIN SELECT RAISE(ABORT, 'interrupt resolutions are immutable'); END;
        """,
        """
        CREATE TRIGGER resolutions_immutable_delete BEFORE DELETE ON interrupt_resolutions
            BEGIN SELECT RAISE(ABORT, 'interrupt resolutions are immutable'); END;
        """,
    ],
]


def _row_to_run(row: sqlite3.Row) -> RunRecord:
    # `environment` (item 5) may be absent from `row` when this method runs
    # against a database whose migrations have not yet applied that step
    # (as the migration-truncation test exercises) --
    # tolerate a missing column the same way a genuinely older reader would
    # have to, rather than raising a bare sqlite3.Row IndexError.
    raw_environment = row["environment"] if "environment" in row.keys() else None
    # B1's dispatcher-lease columns: same tolerance as `environment` above,
    # for a database whose migrations have not yet applied that step.
    row_keys = row.keys()
    return RunRecord(
        id=row["id"],
        driver=row["driver"],
        backend=row["backend"],
        model=row["model"],
        adapter_version=row["adapter_version"],
        created_at=row["created_at"],
        halted_kind=row["halted_kind"],
        halted_detail=row["halted_detail"],
        halted_at=row["halted_at"],
        environment=json.loads(raw_environment) if raw_environment is not None else None,
        dispatcher_id=row["dispatcher_id"] if "dispatcher_id" in row_keys else None,
        dispatcher_lease_expires_at=(
            row["dispatcher_lease_expires_at"] if "dispatcher_lease_expires_at" in row_keys else None
        ),
        dispatcher_fence=(row["dispatcher_fence"] or 0) if "dispatcher_fence" in row_keys else 0,
    )


def _row_to_unit(row: sqlite3.Row) -> UnitRecord:
    return UnitRecord(
        run_id=row["run_id"],
        unit_id=row["unit_id"],
        ordinal=row["ordinal"],
        state=UnitState(row["state"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        claimed_by=row["claimed_by"],
        claimed_at=row["claimed_at"],
        fencing_token=row["fencing_token"],
        lease_expires_at=row["lease_expires_at"],
        accepted_at=row["accepted_at"],
        failed_at=row["failed_at"],
        accepted_text=row["accepted_text"],
    )


def _row_to_attempt(row: sqlite3.Row) -> AttemptRecord:
    usage = None
    if (
        row["input_tokens"] is not None
        or row["output_tokens"] is not None
        or row["cache_hit_tokens"] is not None
    ):
        usage = UsageRecord(
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            cache_hit_tokens=row["cache_hit_tokens"],
        )
    return AttemptRecord(
        id=row["id"],
        run_id=row["run_id"],
        unit_id=row["unit_id"],
        kind=AttemptKind(row["kind"]),
        at=row["at"],
        worker_id=row["worker_id"],
        fencing_token=row["fencing_token"],
        error=row["error"],
        usage=usage,
    )


def _row_to_dispatch(row: sqlite3.Row) -> DispatchRecord:
    return DispatchRecord(
        id=row["id"],
        run_id=row["run_id"],
        unit_id=row["unit_id"],
        worker_id=row["worker_id"],
        session_id=row["session_id"],
        launched_at=row["launched_at"],
        settled_at=row["settled_at"],
        outcome=row["outcome"],
        cli_version=row["cli_version"],
    )


_INTERRUPT_SELECT = (
    "SELECT i.*, r.outcome AS r_outcome, r.input_json AS r_input_json, "
    "r.reason AS r_reason, r.resolved_at AS r_resolved_at "
    "FROM interrupts AS i LEFT JOIN interrupt_resolutions AS r ON r.interrupt_id = i.id "
)


def _row_to_interrupt(row: sqlite3.Row) -> InterruptRecord:
    """Decode one :data:`_INTERRUPT_SELECT` row: an interrupt, and its
    resolution when the joined columns hold one."""
    resolution = None
    if row["r_outcome"] is not None:
        resolution = InterruptResolution(
            interrupt_id=str(row["id"]),
            outcome=row["r_outcome"],
            resolved_at=row["r_resolved_at"],
            input=json.loads(row["r_input_json"]) if row["r_input_json"] is not None else None,
            reason=row["r_reason"],
        )
    return InterruptRecord(
        id=str(row["id"]),
        run_id=row["run_id"],
        unit_id=row["unit_id"],
        fencing_token=row["fencing_token"],
        envelope=row["envelope"],
        kind=row["kind"],
        request_schema=json.loads(row["request_schema_json"]),
        payload=json.loads(row["payload_json"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        on_rejected=row["on_rejected"],
        on_expired=row["on_expired"],
        resolution=resolution,
    )


def _fetch_interrupt_rows(
    conn: sqlite3.Connection, run_id: str, unit_id: Optional[str] = None
) -> List[sqlite3.Row]:
    if unit_id is None:
        return conn.execute(
            _INTERRUPT_SELECT + "WHERE i.run_id = ? ORDER BY i.id", (run_id,)
        ).fetchall()
    return conn.execute(
        _INTERRUPT_SELECT + "WHERE i.run_id = ? AND i.unit_id = ? ORDER BY i.id",
        (run_id, unit_id),
    ).fetchall()


def _interrupt_row_id(interrupt_id: object) -> Optional[int]:
    """The row id an interrupt id names, or ``None`` when it names none."""
    text = str(interrupt_id).strip()
    if not text.isdecimal() or not text.isascii():
        return None
    return int(text)


def _fetch_run_row(conn: sqlite3.Connection, run_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def _fetch_unit_rows(conn: sqlite3.Connection, run_id: str) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM units WHERE run_id = ? ORDER BY ordinal", (run_id,)
    ).fetchall()


def _fetch_attempt_rows(
    conn: sqlite3.Connection,
    run_id: str,
    unit_id: Optional[str] = None,
    *,
    attempt_kinds: Optional[Iterable[AttemptKind]] = None,
    attempts_since: Optional[float] = None,
) -> List[sqlite3.Row]:
    clauses = ["run_id = ?"]
    params: List[object] = [run_id]
    if unit_id is not None:
        clauses.append("unit_id = ?")
        params.append(unit_id)
    if attempt_kinds is not None:
        kinds = [k.value for k in attempt_kinds]
        if not kinds:
            return []
        clauses.append(f"kind IN ({', '.join('?' for _ in kinds)})")
        params.extend(kinds)
    if attempts_since is not None:
        clauses.append("at >= ?")
        params.append(attempts_since)
    query = "SELECT * FROM attempts WHERE " + " AND ".join(clauses) + " ORDER BY id"
    return conn.execute(query, params).fetchall()


class ExecutionStore:
    """A durable run store backed by one SQLite database file.

    ``db_path`` must be a real filesystem path. ``":memory:"`` is refused at
    construction (see :meth:`__init__`) -- a private in-memory database is not
    shared across connections, and this store deliberately opens a fresh
    connection per verb, so an in-memory store would silently lose everything
    written by the previous verb call.
    """

    def __init__(
        self,
        db_path: Union[str, Path],
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        warn_on_network_path: bool = True,
    ) -> None:
        if str(db_path) == ":memory:":
            raise ValueError(
                "ExecutionStore does not support ':memory:'. Every public method "
                "opens and closes its own connection (see the module docstring), "
                "and a private in-memory SQLite database is NOT shared across "
                "connections -- each sqlite3.connect(':memory:') call gets its own "
                "empty database, so state written by one verb would be invisible "
                "to the next. Use a real file path (a temp-directory path is fine "
                "for tests)."
            )

        self.db_path = Path(db_path)
        self.busy_timeout_ms = busy_timeout_ms

        if warn_on_network_path and looks_like_network_path(self.db_path):
            warnings.warn(
                f"ExecutionStore database path {self.db_path} looks like a network "
                "filesystem. WAL-mode SQLite on a network share is a known "
                "corruption vector; a local path is strongly preferred. "
                "Fix: point db_path at local disk (or accept the risk if this "
                "path is known to support proper byte-range locking).",
                RuntimeWarning,
                stacklevel=2,
            )

        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._migrate()

    # -- connection plumbing --------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open one connection, apply per-connection pragmas, close on exit."""
        conn = sqlite3.connect(str(self.db_path), timeout=self.busy_timeout_ms / 1000.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
            self._ensure_wal(conn)
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    def _ensure_wal(self, conn: sqlite3.Connection) -> None:
        """Put ``conn`` into WAL mode, without racing concurrent first-opens.

        ``PRAGMA journal_mode`` with no argument is a plain read (current
        mode) and never takes a lock. ``PRAGMA journal_mode = WAL`` -- the
        form that actually SETS the mode -- takes a brief write lock even
        when the database is already in WAL mode, and critically does **not**
        honor ``busy_timeout``: it fails almost instantly with "database is
        locked" instead of waiting, which used to be the entire first-open
        flake under concurrent opens (every observed failure was this
        statement, never a migration statement).

        So: read the current mode first (lock-free). If it is already
        ``wal``, there is nothing to do -- skip the write entirely, which is
        the common case for every open after the very first. Only when the
        mode is not yet ``wal`` do we need the write, and then we retry it
        ourselves against the busy-timeout budget (since SQLite won't), on
        the theory that whoever holds the lock is another connection about to
        finish setting WAL mode too.
        """
        current = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if isinstance(current, str) and current.lower() == "wal":
            return

        deadline = time.monotonic() + (self.busy_timeout_ms / 1000.0)
        while True:
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                if time.monotonic() >= deadline:
                    raise sqlite3.OperationalError(
                        f"could not set WAL journal mode within "
                        f"{self.busy_timeout_ms} ms (database is locked): {exc}"
                    ) from exc
                time.sleep(0.005)

    @contextmanager
    def _writer(self) -> Iterator[sqlite3.Connection]:
        """A connection inside a ``BEGIN IMMEDIATE`` transaction.

        ``BEGIN IMMEDIATE`` takes the write lock up front rather than on the
        first write statement, so two concurrent claimants against the same
        unit serialize instead of racing to a lost-update. Commits on a clean
        exit, rolls back on any exception.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        """One connection, one consistent read transaction.

        For a caller that must run several queries none of which may observe
        a write that lands in between them (e.g. :meth:`snapshot`, which the
        status digest depends on for invariant-consistent counts). In WAL
        mode a plain ``BEGIN`` (deferred) transaction establishes its
        snapshot at the first read and holds it for the life of the
        transaction without blocking concurrent writers -- so this never
        contends with a worker's claim/submit transaction, it just doesn't
        see a write that commits after the snapshot was taken.

        Read-only by contract: never execute an INSERT/UPDATE/DELETE inside
        this transaction.
        """
        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _read_schema_version(conn: sqlite3.Connection) -> int:
        """Read the current schema version with plain reads -- no write lock.

        Safe to call outside any transaction (autocommit reads) or inside
        one; either way it takes no write lock itself, so it is the
        lock-free fast path in :meth:`_migrate` AND the authoritative
        recheck once ``BEGIN IMMEDIATE`` is actually held.
        """
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        if existing is None:
            return 0
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return row["v"] if row and row["v"] is not None else 0

    def _migrate(self) -> None:
        """Apply any migration steps not yet applied.

        Check-then-lock-then-recheck. The version read is a plain read (see
        :meth:`_read_schema_version`) and takes no write lock -- so the
        overwhelmingly common case, a database that is already at the
        current schema version, opens :meth:`ExecutionStore` without ever
        taking ``BEGIN IMMEDIATE``. This matters beyond throughput: an
        already-current database used to take a write lock on every open
        regardless, which meant constructing a fresh ``ExecutionStore``
        while another connection held an open write transaction (a stuck or
        slow writer) would block for the full busy-timeout and then fail --
        including a status-probe process whose entire point is to stay cheap
        against a live run.

        Only when a migration is actually needed do we take ``BEGIN
        IMMEDIATE`` -- and the version is read AGAIN inside that transaction
        before applying any step, because another connection may have raced
        us and already migrated between our lock-free check and acquiring
        the lock. That recheck is what preserves the original migration fix:
        two concurrent first-opens can both observe version 0 outside the
        lock, but only one of them does any work once the lock is held.

        ``execute()`` per statement, never ``executescript`` -- on this
        interpreter ``executescript`` commits (and releases the write lock
        held by ``BEGIN IMMEDIATE``) before running its script, which is what
        let a failure partway through a multi-step migration leave partial
        DDL permanently committed (a wedge: a retry then failed with "table
        schema_version already exists" because step 0's CREATE TABLE had
        already landed while a later step's failure was never applied).
        Plain ``execute()`` inside one held transaction means a failure at
        any step rolls back everything applied so far in THIS call, leaving
        the database exactly as it was before -- a retry starts clean.
        """
        with self._connect() as conn:
            if self._read_schema_version(conn) >= len(_MIGRATIONS):
                return

            conn.execute("BEGIN IMMEDIATE")
            try:
                current_version = self._read_schema_version(conn)

                if current_version >= len(_MIGRATIONS):
                    conn.commit()
                    return

                for step_index in range(current_version, len(_MIGRATIONS)):
                    for statement in _MIGRATIONS[step_index]:
                        conn.execute(statement)
                    new_version = step_index + 1
                    if step_index == 0:
                        conn.execute(
                            "INSERT INTO schema_version(version) VALUES (?)", (new_version,)
                        )
                    else:
                        conn.execute("UPDATE schema_version SET version = ?", (new_version,))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    # -- runs ------------------------------------------------------------------

    def create_run(
        self,
        run_id: str,
        *,
        driver: str,
        backend: str,
        model: str,
        adapter_version: str,
        created_at: Optional[float] = None,
        environment: Optional[Mapping[str, str]] = None,
    ) -> RunRecord:
        """Create a new run row. Raises on a duplicate ``run_id``.

        ``environment`` (item 5) is the create-time snapshot
        (``execution.adapter.WorkerEnvironment.snapshot()``); stored as JSON
        text. When omitted (the default -- an adapter-less create, or a
        mount whose adapter declared nothing), the INSERT never references
        the ``environment`` column at all, the same "optional, so a caller
        that never passes it writes exactly the row it always has" pattern
        :meth:`accept_unit` already uses for ``text``.
        """
        at = time.time() if created_at is None else created_at
        with self._writer() as conn:
            if environment is not None:
                conn.execute(
                    "INSERT INTO runs(id, driver, backend, model, adapter_version, created_at, "
                    "environment) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        driver,
                        backend,
                        model,
                        adapter_version,
                        at,
                        json.dumps(dict(environment)),
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO runs(id, driver, backend, model, adapter_version, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (run_id, driver, backend, model, adapter_version, at),
                )
        return self.get_run(run_id)  # type: ignore[return-value]

    def get_run(self, run_id: str) -> Optional[RunRecord]:
        with self._connect() as conn:
            row = _fetch_run_row(conn, run_id)
        return _row_to_run(row) if row is not None else None

    def _require_run(self, conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = _fetch_run_row(conn, run_id)
        if row is None:
            raise UnknownRunError(run_id)
        return row

    def set_halt(self, run_id: str, kind: str, detail: str = "", *, at: Optional[float] = None) -> None:
        """Set the run's halt state. Never rejects a claim already in flight."""
        when = time.time() if at is None else at
        with self._writer() as conn:
            self._require_run(conn, run_id)
            conn.execute(
                "UPDATE runs SET halted_kind = ?, halted_detail = ?, halted_at = ? WHERE id = ?",
                (kind, detail[:_ERROR_TRUNCATE], when, run_id),
            )

    def clear_halt(self, run_id: str) -> None:
        with self._writer() as conn:
            self._require_run(conn, run_id)
            conn.execute(
                "UPDATE runs SET halted_kind = NULL, halted_detail = NULL, halted_at = NULL "
                "WHERE id = ?",
                (run_id,),
            )

    # -- units -------------------------------------------------------------------

    def register_units(
        self,
        run_id: str,
        unit_ids: Sequence[str],
        *,
        at: Optional[float] = None,
    ) -> None:
        """Register ``unit_ids`` as PENDING, ordinal-numbered in argument order.

        Raises :class:`DuplicateUnitError` if any id already exists for this
        run (including a duplicate within ``unit_ids`` itself) -- reported
        with every colliding id, not just the first, so the caller fixes them
        all in one pass.
        """
        when = time.time() if at is None else at
        with self._writer() as conn:
            self._require_run(conn, run_id)
            existing = {
                r["unit_id"]
                for r in conn.execute("SELECT unit_id FROM units WHERE run_id = ?", (run_id,))
            }
            seen: set = set()
            collisions: List[str] = []
            for uid in unit_ids:
                if uid in existing or uid in seen:
                    collisions.append(uid)
                seen.add(uid)
            if collisions:
                raise DuplicateUnitError(
                    f"unit id(s) already registered for run {run_id!r}: {sorted(set(collisions))}"
                )
            base_ordinal = len(existing)
            conn.executemany(
                "INSERT INTO units(run_id, unit_id, ordinal, state, created_at, updated_at, "
                "fencing_token) VALUES (?, ?, ?, ?, ?, ?, 0)",
                [
                    (run_id, uid, base_ordinal + i, UnitState.PENDING.value, when, when)
                    for i, uid in enumerate(unit_ids)
                ],
            )

    def get_unit(self, run_id: str, unit_id: str) -> Optional[UnitRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM units WHERE run_id = ? AND unit_id = ?", (run_id, unit_id)
            ).fetchone()
        return _row_to_unit(row) if row is not None else None

    def list_units(self, run_id: str) -> List[UnitRecord]:
        with self._connect() as conn:
            rows = _fetch_unit_rows(conn, run_id)
        return [_row_to_unit(r) for r in rows]

    def _require_unit(self, conn: sqlite3.Connection, run_id: str, unit_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM units WHERE run_id = ? AND unit_id = ?", (run_id, unit_id)
        ).fetchone()
        if row is None:
            raise UnknownUnitError(f"{run_id!r}/{unit_id!r}")
        return row

    def _record_attempt(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        unit_id: str,
        kind: AttemptKind,
        *,
        at: float,
        worker_id: Optional[str] = None,
        fencing_token: Optional[int] = None,
        error: Union[str, Mapping, Sequence, None] = None,
        usage: Optional[UsageRecord] = None,
    ) -> None:
        if error is not None and not isinstance(error, str):
            # A structured failure detail (a JSON object or list from a
            # worker) is stored as its JSON text, so the fail is recorded
            # rather than refused; the same length cap applies below.
            try:
                error = json.dumps(error, sort_keys=True, default=str)
            except (TypeError, ValueError):
                error = str(error)
        conn.execute(
            "INSERT INTO attempts(run_id, unit_id, kind, at, worker_id, fencing_token, error, "
            "input_tokens, output_tokens, cache_hit_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                unit_id,
                kind.value,
                at,
                worker_id,
                fencing_token,
                error[:_ERROR_TRUNCATE] if error else error,
                usage.input_tokens if usage else None,
                usage.output_tokens if usage else None,
                usage.cache_hit_tokens if usage else None,
            ),
        )

    # -- claims / leases -----------------------------------------------------

    def claim_unit(
        self,
        run_id: str,
        unit_id: str,
        worker_id: str,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        at: Optional[float] = None,
    ) -> ClaimResult:
        """Atomically claim ``unit_id`` for ``worker_id``.

        A PENDING unit claims cleanly. A CLAIMED unit whose lease has expired
        is transparently reclaimed (an EXPIRE attempt is recorded first, so
        the reclaim is visible in the log). A CLAIMED unit with a live lease
        raises :class:`AlreadyClaimedError`; a terminal unit raises
        :class:`TerminalStateError`; a halted run raises
        :class:`RunHaltedError` (halt blocks new claims, never a
        fenced-valid submission already in flight). A WAITING unit raises
        :class:`~content_pipeline.execution.model.UnitWaitingError`, a
        subclass of :class:`AlreadyClaimedError`, naming its open interrupt.
        """
        now = time.time() if at is None else at
        with self._writer() as conn:
            run_row = self._require_run(conn, run_id)
            if run_row["halted_kind"] is not None:
                raise RunHaltedError(run_id, run_row["halted_kind"])

            unit_row = self._require_unit(conn, run_id, unit_id)
            state = UnitState(unit_row["state"])

            if state in TERMINAL_STATES:
                raise TerminalStateError(f"{run_id!r}/{unit_id!r} is already {state.value}")

            if state is UnitState.WAITING:
                # A waiting unit holds no lease, so without this branch it
                # would be claimed like a PENDING unit and the wait would be
                # bypassed.
                open_row = self._open_interrupt_row(conn, run_id, unit_id)
                raise UnitWaitingError(
                    run_id, unit_id, str(open_row["id"]) if open_row is not None else None
                )

            if state is UnitState.CLAIMED:
                lease_expires_at = unit_row["lease_expires_at"]
                if lease_expires_at is not None and lease_expires_at > now:
                    raise AlreadyClaimedError(
                        f"{run_id!r}/{unit_id!r} is claimed by "
                        f"{unit_row['claimed_by']!r} until {lease_expires_at}"
                    )
                # Lease expired: reclaim. Record the expiry before the new claim.
                self._record_attempt(
                    conn,
                    run_id,
                    unit_id,
                    AttemptKind.EXPIRE,
                    at=now,
                    worker_id=unit_row["claimed_by"],
                    fencing_token=unit_row["fencing_token"],
                )

            new_token = unit_row["fencing_token"] + 1
            lease_expires_at = now + lease_seconds
            conn.execute(
                "UPDATE units SET state = ?, claimed_by = ?, claimed_at = ?, fencing_token = ?, "
                "lease_expires_at = ?, updated_at = ? WHERE run_id = ? AND unit_id = ?",
                (
                    UnitState.CLAIMED.value,
                    worker_id,
                    now,
                    new_token,
                    lease_expires_at,
                    now,
                    run_id,
                    unit_id,
                ),
            )
            self._record_attempt(
                conn,
                run_id,
                unit_id,
                AttemptKind.CLAIM,
                at=now,
                worker_id=worker_id,
                fencing_token=new_token,
            )
        return ClaimResult(fencing_token=new_token, lease_expires_at=lease_expires_at)

    def renew_lease(
        self,
        run_id: str,
        unit_id: str,
        fencing_token: int,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        at: Optional[float] = None,
    ) -> float:
        """Extend a live claim's lease. Returns the new ``lease_expires_at``.

        The fencing check happens FIRST, before any state check (see the
        module docstring): a presented token that does not match the unit's
        current token always raises :class:`StaleFenceError`, even if the
        unit is now terminal or otherwise not CLAIMED.

        No further claim, renew, accept, or fail is legal against a terminal
        unit (model.py's promise): a matching (still-current) token against
        a terminal unit raises :class:`TerminalStateError`, checked before
        the not-CLAIMED check below, mirroring :meth:`accept_unit` and
        :meth:`fail_unit`.
        """
        now = time.time() if at is None else at
        with self._writer() as conn:
            self._require_run(conn, run_id)
            unit_row = self._require_unit(conn, run_id, unit_id)
            current = unit_row["fencing_token"]
            if fencing_token != current:
                raise StaleFenceError(run_id, unit_id, fencing_token, current)

            state = UnitState(unit_row["state"])
            if state in TERMINAL_STATES:
                raise TerminalStateError(f"{run_id!r}/{unit_id!r} is already {state.value}")
            if state is not UnitState.CLAIMED:
                raise NotClaimedError(f"{run_id!r}/{unit_id!r} is {state.value}, not claimed")

            lease_expires_at = now + lease_seconds
            conn.execute(
                "UPDATE units SET lease_expires_at = ?, updated_at = ? "
                "WHERE run_id = ? AND unit_id = ?",
                (lease_expires_at, now, run_id, unit_id),
            )
            self._record_attempt(
                conn, run_id, unit_id, AttemptKind.RENEW, at=now, fencing_token=fencing_token
            )
        return lease_expires_at

    def accept_unit(
        self,
        run_id: str,
        unit_id: str,
        fencing_token: int,
        *,
        text: Optional[str] = None,
        usage: Optional[UsageRecord] = None,
        at: Optional[float] = None,
    ) -> None:
        """Terminally accept a unit (a valid-fence acceptance ignores halt).

        Deliberately does not consult the run's halt state -- a submission
        carrying a valid fencing token is completed, paid-for work and is
        recorded exactly as if no halt existed (halt blocks claims, never valid-fence submissions). Only a STALE fencing
        token is rejected, regardless of halt state.

        The fencing check happens FIRST, before any state check (a late
        fenced-out submission is superseded, never applied): a presented token that does not match the unit's
        current token is a fenced-out late submission -- it always raises
        :class:`StaleFenceError`, even when the unit is now ACCEPTED (by the
        winning claimant) or otherwise not the state a naive check would
        expect. Before raising, a payload-free
        :class:`~content_pipeline.execution.model.AttemptKind.SUPERSEDED`
        attempt row is appended (presented token, timestamp) so this late,
        rejected, duplicated-spend submission is a durable, visible fact
        instead of being silently discarded. Unit state is never touched by
        this path.

        ``text``, when supplied, is written to ``accepted_text`` as part of
        the same terminal UPDATE. When omitted (the default), the column is
        left UNTOUCHED rather than written as NULL -- ``text`` is optional
        precisely so an existing caller that never passes it goes on
        producing byte-identical writes, and so a future caller cannot
        accidentally blank out a previously recorded value by calling
        :meth:`accept_unit` again without re-supplying it.
        """
        now = time.time() if at is None else at
        # Note: a stale-fence branch below records the SUPERSEDED attempt and
        # then falls through to a clean (non-exception) exit of the `with`
        # block, raising StaleFenceError only AFTER it closes. Raising
        # WHILE still inside `self._writer()` would trigger that context
        # manager's own rollback (see `_writer`'s docstring) and undo the
        # very row this path exists to make durable -- exactly the
        # discard-on-reject bug the fencing rule requires NOT happen.
        stale: Optional[Tuple[int, int]] = None  # (presented, current)
        with self._writer() as conn:
            self._require_run(conn, run_id)
            unit_row = self._require_unit(conn, run_id, unit_id)
            current = unit_row["fencing_token"]
            if fencing_token != current:
                self._record_attempt(
                    conn,
                    run_id,
                    unit_id,
                    AttemptKind.SUPERSEDED,
                    at=now,
                    fencing_token=fencing_token,
                )
                stale = (fencing_token, current)
            else:
                state = UnitState(unit_row["state"])
                if state in TERMINAL_STATES:
                    raise TerminalStateError(f"{run_id!r}/{unit_id!r} is already {state.value}")
                if state is not UnitState.CLAIMED:
                    raise NotClaimedError(f"{run_id!r}/{unit_id!r} is {state.value}, not claimed")

                if text is not None:
                    conn.execute(
                        "UPDATE units SET state = ?, accepted_at = ?, updated_at = ?, "
                        "accepted_text = ? WHERE run_id = ? AND unit_id = ?",
                        (UnitState.ACCEPTED.value, now, now, text, run_id, unit_id),
                    )
                else:
                    conn.execute(
                        "UPDATE units SET state = ?, accepted_at = ?, updated_at = ? "
                        "WHERE run_id = ? AND unit_id = ?",
                        (UnitState.ACCEPTED.value, now, now, run_id, unit_id),
                    )
                self._record_attempt(
                    conn,
                    run_id,
                    unit_id,
                    AttemptKind.ACCEPT,
                    at=now,
                    fencing_token=fencing_token,
                    usage=usage,
                )
        if stale is not None:
            raise StaleFenceError(run_id, unit_id, stale[0], stale[1])

    def fail_unit(
        self,
        run_id: str,
        unit_id: str,
        fencing_token: int,
        *,
        error: Union[str, Mapping, Sequence] = "",
        terminal: bool = False,
        terminal_state: UnitState = UnitState.FAILED,
        usage: Optional[UsageRecord] = None,
        at: Optional[float] = None,
    ) -> None:
        """Record a failed attempt. ``terminal=False`` (default) returns the
        unit to PENDING for retry; ``terminal=True`` fails it permanently.

        Fencing is checked FIRST, same as :meth:`accept_unit`: a stale token
        always raises :class:`StaleFenceError` (and records a SUPERSEDED
        attempt first) regardless of the unit's current state.

        ``terminal_state`` (A-min.2) selects WHICH terminal state a
        ``terminal=True`` call lands the unit in. It defaults to
        ``UnitState.FAILED`` -- so every existing caller, which never passes
        this argument, writes exactly the row it always has -- and is the
        seam ``execution.controller``'s terminal-skip path uses to land a
        unit in ``UnitState.SKIPPED`` instead, without a near-duplicate
        method. Ignored when ``terminal=False`` (a retry always returns to
        PENDING, unchanged). Must be a member of ``TERMINAL_STATES``.
        """
        if terminal and terminal_state not in TERMINAL_STATES:
            raise ValueError(
                f"terminal_state must be one of {TERMINAL_STATES}, got {terminal_state!r}"
            )
        now = time.time() if at is None else at
        # See the matching comment in accept_unit: the stale branch must not
        # raise while still inside `self._writer()`, or its own rollback
        # would discard the SUPERSEDED row this path exists to make durable.
        stale: Optional[Tuple[int, int]] = None  # (presented, current)
        with self._writer() as conn:
            self._require_run(conn, run_id)
            unit_row = self._require_unit(conn, run_id, unit_id)
            current = unit_row["fencing_token"]
            if fencing_token != current:
                self._record_attempt(
                    conn,
                    run_id,
                    unit_id,
                    AttemptKind.SUPERSEDED,
                    at=now,
                    fencing_token=fencing_token,
                )
                stale = (fencing_token, current)
            else:
                state = UnitState(unit_row["state"])
                if state in TERMINAL_STATES:
                    raise TerminalStateError(f"{run_id!r}/{unit_id!r} is already {state.value}")
                if state is not UnitState.CLAIMED:
                    raise NotClaimedError(f"{run_id!r}/{unit_id!r} is {state.value}, not claimed")

                if terminal:
                    conn.execute(
                        "UPDATE units SET state = ?, failed_at = ?, updated_at = ?, claimed_by = NULL, "
                        "claimed_at = NULL, lease_expires_at = NULL WHERE run_id = ? AND unit_id = ?",
                        (terminal_state.value, now, now, run_id, unit_id),
                    )
                else:
                    conn.execute(
                        "UPDATE units SET state = ?, updated_at = ?, claimed_by = NULL, "
                        "claimed_at = NULL, lease_expires_at = NULL WHERE run_id = ? AND unit_id = ?",
                        (UnitState.PENDING.value, now, run_id, unit_id),
                    )
                self._record_attempt(
                    conn,
                    run_id,
                    unit_id,
                    AttemptKind.FAIL,
                    at=now,
                    fencing_token=fencing_token,
                    error=error,
                    usage=usage,
                )
        if stale is not None:
            raise StaleFenceError(run_id, unit_id, stale[0], stale[1])

    def _record_apply_event(
        self,
        run_id: str,
        unit_id: str,
        kind: AttemptKind,
        *,
        error: Optional[str] = None,
        at: Optional[float] = None,
    ) -> None:
        """Shared body of :meth:`record_apply_started`,
        :meth:`record_apply_succeeded`, and :meth:`record_apply_rejected`:
        require the unit ACCEPTED, then append one apply-kind attempt row.
        No fencing check in any of the three -- apply runs after acceptance,
        under the dispatcher's documented single-writer discipline (see the
        module docstring), not under worker-claim contention, so there is no
        competing fence to validate against here the way there is in
        claim/accept/fail.
        """
        now = time.time() if at is None else at
        with self._writer() as conn:
            self._require_run(conn, run_id)
            unit_row = self._require_unit(conn, run_id, unit_id)
            state = UnitState(unit_row["state"])
            if state is not UnitState.ACCEPTED:
                raise NotAcceptedError(
                    f"{run_id!r}/{unit_id!r} is {state.value}, not accepted"
                )
            self._record_attempt(
                conn,
                run_id,
                unit_id,
                kind,
                at=now,
                fencing_token=unit_row["fencing_token"],
                error=error,
            )

    def record_apply_started(
        self, run_id: str, unit_id: str, *, at: Optional[float] = None
    ) -> None:
        """Record that finalize is about to call the adapter's apply.

        Requires the unit to be ACCEPTED; raises :class:`NotAcceptedError`
        otherwise -- finalize only ever applies accepted units. See
        :meth:`_record_apply_event` for the no-fencing rationale shared with
        :meth:`record_apply_succeeded` and :meth:`record_apply_rejected`.
        """
        self._record_apply_event(run_id, unit_id, AttemptKind.APPLY_STARTED, at=at)

    def record_apply_succeeded(
        self, run_id: str, unit_id: str, *, at: Optional[float] = None
    ) -> None:
        """Record that the adapter's apply returned without raising.

        Same ACCEPTED requirement and no-fencing rationale as
        :meth:`record_apply_started` -- see :meth:`_record_apply_event`.
        Recording this twice (e.g. a retried finalize pass) simply appends a
        second attempt row; it is not itself the idempotence mechanism.
        Finalize idempotence is derived by scanning the attempt log for the
        LAST apply-kind attempt (a trailing APPLY_STARTED is an interrupted
        apply that the next finalize applies again, per the model module
        docstring), not enforced by this method.
        """
        self._record_apply_event(run_id, unit_id, AttemptKind.APPLY_SUCCEEDED, at=at)

    def record_apply_rejected(
        self,
        run_id: str,
        unit_id: str,
        reason: str,
        *,
        at: Optional[float] = None,
    ) -> None:
        """Record an apply refusal whose adapter guaranteed no side effect.

        The unit must remain ACCEPTED. This is a terminal disposition on the
        apply axis, not a unit-state transition, so a later finalize pass can
        skip it without risking a duplicate apply. See
        :meth:`_record_apply_event` for the shared no-fencing rationale.
        """
        self._record_apply_event(
            run_id, unit_id, AttemptKind.APPLY_REJECTED, error=reason, at=at
        )

    # -- attempts ----------------------------------------------------------------

    def list_attempts(self, run_id: str, unit_id: Optional[str] = None) -> List[AttemptRecord]:
        with self._connect() as conn:
            rows = _fetch_attempt_rows(conn, run_id, unit_id)
        return [_row_to_attempt(r) for r in rows]

    # -- consistent multi-query snapshot ------------------------------------------

    def snapshot(
        self,
        run_id: str,
        *,
        attempt_kinds: Optional[Iterable[AttemptKind]] = None,
        attempts_since: Optional[float] = None,
    ) -> Tuple[Optional[RunRecord], List[UnitRecord], List[AttemptRecord]]:
        """One consistent read-transaction view of a run, its units, and its attempts.

        Used by :func:`~content_pipeline.execution.status.compute_status` so
        that a write landing between "read units" and "read attempts" cannot
        produce a torn digest (e.g. a count that reflects the unit's new
        state but a failure-group tally computed from the attempt that caused
        it, or vice versa). All three queries run inside one
        :meth:`read_transaction`.

        ``attempt_kinds``/``attempts_since`` (both keyword-only, both
        default ``None``) narrow the ATTEMPT rows read, pushed into the SQL
        inside the same single read transaction -- units and the run row are
        always read in full; only the attempt query is filtered. A caller
        that needs only a subset of attempt kinds within a recent window
        (``status.compute_status``'s ``FAIL``-only, within-window read;
        ``execution.wave``'s apply-kind readers) would otherwise pay for
        materializing and objectifying every attempt row of the run just to
        discard most of them in Python -- the same O(N) (or, looped over a
        graph run's lifetime, O(N^2)) cost either way, but now paid once, in
        SQL, instead of twice (once in SQLite's row fetch, once in
        :func:`_row_to_attempt`). Neither filter changes what ``units`` or
        ``run`` return, and omitting both is byte-for-byte the prior
        behavior (every attempt row, unfiltered).
        """
        with self.read_transaction() as conn:
            run_row = _fetch_run_row(conn, run_id)
            run = _row_to_run(run_row) if run_row is not None else None
            units = [_row_to_unit(r) for r in _fetch_unit_rows(conn, run_id)]
            attempts = [
                _row_to_attempt(r)
                for r in _fetch_attempt_rows(
                    conn,
                    run_id,
                    attempt_kinds=attempt_kinds,
                    attempts_since=attempts_since,
                )
            ]
        return run, units, attempts

    # -- interrupts: a unit waits on a typed request ------------------------------
    #
    # The rules of a wait (request shape, limits, canonical form, decision and
    # outcome words, replay test, lapse rule) execute in the shared interrupt
    # contract, reached only through `execution.interrupts`. The three writing
    # verbs probe that edge BEFORE opening a transaction, so a machine without
    # it refuses and writes nothing. The reads below decode rows and need no
    # edge. No other verb of this store calls into `execution.interrupts`.

    @staticmethod
    def _open_interrupt_row(
        conn: sqlite3.Connection, run_id: str, unit_id: str
    ) -> Optional[sqlite3.Row]:
        """The unit's one unresolved interrupt row, or ``None``."""
        return conn.execute(
            _INTERRUPT_SELECT + "WHERE i.run_id = ? AND i.unit_id = ? AND r.interrupt_id IS NULL "
            "ORDER BY i.id DESC LIMIT 1",
            (run_id, unit_id),
        ).fetchone()

    @staticmethod
    def _interrupt_row(conn: sqlite3.Connection, row_id: int) -> Optional[sqlite3.Row]:
        return conn.execute(_INTERRUPT_SELECT + "WHERE i.id = ?", (row_id,)).fetchone()

    def _close_waiting_unit(
        self,
        conn: sqlite3.Connection,
        interrupt_row: sqlite3.Row,
        kind: AttemptKind,
        *,
        stopped_state: Optional[UnitState],
        at: float,
    ) -> None:
        """Move a WAITING unit on and append the closing attempt row.

        ``stopped_state`` is the terminal state the unit ends in, or ``None``
        to return it to PENDING. The attempt row carries the fencing token of
        the claim that made the request, which is still the unit's token: no
        claim can happen while a unit waits.
        """
        run_id = interrupt_row["run_id"]
        unit_id = interrupt_row["unit_id"]
        if stopped_state is None:
            conn.execute(
                "UPDATE units SET state = ?, updated_at = ? WHERE run_id = ? AND unit_id = ?",
                (UnitState.PENDING.value, at, run_id, unit_id),
            )
        else:
            conn.execute(
                "UPDATE units SET state = ?, failed_at = ?, updated_at = ? "
                "WHERE run_id = ? AND unit_id = ?",
                (stopped_state.value, at, at, run_id, unit_id),
            )
        self._record_attempt(
            conn,
            run_id,
            unit_id,
            kind,
            at=at,
            fencing_token=interrupt_row["fencing_token"],
        )

    def _record_expiry(
        self, conn: sqlite3.Connection, interrupt_row: sqlite3.Row, at: float
    ) -> None:
        """Write the ``expired`` resolution of a lapsed interrupt and apply
        the request's ``on_expired`` policy to its WAITING unit."""
        conn.execute(
            "INSERT INTO interrupt_resolutions(interrupt_id, outcome, input_json, reason, "
            "resolved_at) VALUES (?, 'expired', NULL, NULL, ?)",
            (interrupt_row["id"], at),
        )
        self._close_waiting_unit(
            conn,
            interrupt_row,
            AttemptKind.INTERRUPT_EXPIRED,
            stopped_state=(
                UnitState.INTERRUPT_EXPIRED
                if interrupt_row["on_expired"] == POLICY_STOP
                else None
            ),
            at=at,
        )

    def request_interrupt(
        self,
        run_id: str,
        unit_id: str,
        fencing_token: int,
        request: InterruptRequest,
        *,
        on_rejected: str = POLICY_STOP,
        on_expired: str = POLICY_STOP,
        usage: Optional[UsageRecord] = None,
        at: Optional[float] = None,
    ) -> InterruptRecord:
        """Record a typed request for a person's answer; the unit waits.

        Called by the holder of the unit's claim, with the claim's fencing
        token. In one transaction the unit goes CLAIMED -> WAITING, its
        claimant and lease are cleared, the interrupt row is written with both
        policies, and an ``interrupt_requested`` attempt row records the
        token, the claimant and ``usage``. The token is not bumped: the claim
        after a resolution takes the next one. Returns the interrupt record.

        ``on_rejected`` and ``on_expired`` say what the unit does after a
        rejection or a lapse: ``stop`` (the default) ends it in a terminal
        state, ``release`` returns it to PENDING so the next attempt can read
        the outcome and continue, skip or ask again.

        Refusals, in order. A policy outside ``stop``/``release`` and a
        request the interrupt contract refuses raise
        :class:`~content_pipeline.execution.model.InterruptRequestError`; a
        missing contract or validator raises
        :class:`~content_pipeline.execution.interrupts.InterruptSupportError`.
        All of these happen before the transaction opens, so nothing is
        written. Inside it the fencing check comes first, as for
        :meth:`accept_unit`: a stale token records a SUPERSEDED attempt row
        and raises :class:`StaleFenceError` once the row is committed. Then a
        terminal unit raises :class:`TerminalStateError`, any other state than
        CLAIMED raises :class:`NotClaimedError`, and a unit with an open
        dispatch raises
        :class:`~content_pipeline.execution.model.WaitUnderDispatchError`.
        A valid-fence request ignores the run's halt state, as a valid-fence
        acceptance does.
        """
        for name, value in (("on_rejected", on_rejected), ("on_expired", on_expired)):
            if value not in INTERRUPT_POLICIES:
                raise InterruptRequestError(
                    f"{name} must be one of {', '.join(INTERRUPT_POLICIES)}, got {value!r}"
                )
        _interrupts.support()
        checked = _interrupts.check_request(request)
        now = time.time() if at is None else at
        expires_at = _interrupts.expiry(now, checked.expires_in_s)
        schema_json = _interrupts.canonical_json(checked.request_schema)
        payload_json = _interrupts.canonical_json(checked.payload)
        # As in accept_unit: the stale branch leaves the transaction cleanly
        # and raises afterwards, so its SUPERSEDED row is committed.
        stale: Optional[Tuple[int, int]] = None  # (presented, current)
        record: Optional[InterruptRecord] = None
        with self._writer() as conn:
            self._require_run(conn, run_id)
            unit_row = self._require_unit(conn, run_id, unit_id)
            current = unit_row["fencing_token"]
            if fencing_token != current:
                self._record_attempt(
                    conn,
                    run_id,
                    unit_id,
                    AttemptKind.SUPERSEDED,
                    at=now,
                    fencing_token=fencing_token,
                )
                stale = (fencing_token, current)
            else:
                state = UnitState(unit_row["state"])
                if state in TERMINAL_STATES:
                    raise TerminalStateError(f"{run_id!r}/{unit_id!r} is already {state.value}")
                if state is not UnitState.CLAIMED:
                    raise NotClaimedError(f"{run_id!r}/{unit_id!r} is {state.value}, not claimed")
                open_dispatch = conn.execute(
                    "SELECT 1 FROM dispatches WHERE run_id = ? AND unit_id = ? "
                    "AND settled_at IS NULL",
                    (run_id, unit_id),
                ).fetchone()
                if open_dispatch is not None:
                    raise WaitUnderDispatchError(run_id, unit_id)

                cursor = conn.execute(
                    "INSERT INTO interrupts(run_id, unit_id, fencing_token, envelope, kind, "
                    "request_schema_json, payload_json, created_at, expires_at, on_rejected, "
                    "on_expired) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        unit_id,
                        fencing_token,
                        checked.envelope,
                        checked.kind,
                        schema_json,
                        payload_json,
                        now,
                        expires_at,
                        on_rejected,
                        on_expired,
                    ),
                )
                conn.execute(
                    "UPDATE units SET state = ?, updated_at = ?, claimed_by = NULL, "
                    "claimed_at = NULL, lease_expires_at = NULL WHERE run_id = ? AND unit_id = ?",
                    (UnitState.WAITING.value, now, run_id, unit_id),
                )
                self._record_attempt(
                    conn,
                    run_id,
                    unit_id,
                    AttemptKind.INTERRUPT_REQUESTED,
                    at=now,
                    worker_id=unit_row["claimed_by"],
                    fencing_token=fencing_token,
                    usage=usage,
                )
                record = _row_to_interrupt(self._interrupt_row(conn, cursor.lastrowid))
        if stale is not None:
            raise StaleFenceError(run_id, unit_id, stale[0], stale[1])
        return record  # type: ignore[return-value]

    def resolve_interrupt(
        self,
        run_id: str,
        interrupt_id: str,
        *,
        decision: str,
        input: object = None,
        reason: Optional[str] = None,
        now: Optional[float] = None,
    ) -> InterruptResolution:
        """Resolve one interrupt, idempotently, in one transaction.

        ``decision`` is ``answer`` (``input`` is validated against the schema
        the request declared) or ``reject`` (``reason`` is optional free
        text). An answer returns the unit to PENDING; the next claim is a
        fresh attempt that reads the answer with
        :func:`~content_pipeline.execution.interrupts.unit_resolutions`. A
        rejection applies the request's ``on_rejected`` policy: ``stop`` ends
        the unit OPERATOR_REJECTED, ``release`` returns it to PENDING. The
        resolution row is written whatever the policy.

        Replaying a resolution equal to the stored one -- same decision, same
        canonical input, same reason -- returns it with ``replayed`` set and
        writes nothing, even after the unit moved on. A different one raises
        :class:`~content_pipeline.execution.model.ResolutionConflictError`
        and the stored resolution is kept. An interrupt found lapsed is
        recorded as expired (its ``on_expired`` policy applied), committed,
        and then refused with
        :class:`~content_pipeline.execution.model.InterruptExpiredError`.
        An unknown run or interrupt id raises
        :class:`~content_pipeline.execution.model.UnknownInterruptError`, and
        a refused input raises
        :class:`~content_pipeline.execution.model.ResolutionInputError`.
        A decision other than ``answer`` or ``reject``, a rejection given an
        input, and an answer given a reason raise ``ValueError``. The edge
        probe runs before the transaction opens.
        """
        _interrupts.support()
        outcome = _interrupts.decision_outcome(decision, input=input, reason=reason)
        bounded_reason = _interrupts.bound_reason(reason)
        when = time.time() if now is None else now
        row_id = _interrupt_row_id(interrupt_id)
        if row_id is None:
            raise UnknownInterruptError(
                f"interrupt {interrupt_id!r} is not an interrupt id in run {run_id!r}"
            )
        # The lapse branch commits the expiry and raises afterwards, for the
        # reason the stale branch of accept_unit does.
        expired: Optional[InterruptExpiredError] = None
        with self._writer() as conn:
            if _fetch_run_row(conn, run_id) is None:
                raise UnknownInterruptError(f"run {run_id!r} does not exist")
            row = self._interrupt_row(conn, row_id)
            if row is None or row["run_id"] != run_id:
                raise UnknownInterruptError(
                    f"interrupt {row_id} is not an interrupt in run {run_id!r}"
                )
            stored = _row_to_interrupt(row).resolution
            if stored is not None:
                if stored.outcome == "expired":
                    raise InterruptExpiredError(str(row_id), row["expires_at"])
                if not _interrupts.same_resolution(
                    stored_outcome=stored.outcome,
                    stored_input_json=row["r_input_json"],
                    stored_reason=stored.reason,
                    outcome=outcome,
                    input=input,
                    reason=reason,
                ):
                    raise ResolutionConflictError(str(row_id), stored.outcome)
                return replace(stored, replayed=True)
            unit_row = self._require_unit(conn, run_id, row["unit_id"])
            state = UnitState(unit_row["state"])
            if _interrupts.lapsed(row["expires_at"], when) and state is UnitState.WAITING:
                self._record_expiry(conn, row, when)
                expired = InterruptExpiredError(str(row_id), row["expires_at"])
            else:
                if state is not UnitState.WAITING:
                    raise ExecutionError(
                        f"interrupt {row_id} is unresolved but its unit "
                        f"{run_id!r}/{row['unit_id']!r} is {state.value}, not waiting; "
                        "nothing was written"
                    )
                input_json = None
                if outcome == "answered":
                    input_json = _interrupts.validate_input(
                        json.loads(row["request_schema_json"]), input
                    )
                conn.execute(
                    "INSERT INTO interrupt_resolutions(interrupt_id, outcome, input_json, "
                    "reason, resolved_at) VALUES (?, ?, ?, ?, ?)",
                    (row_id, outcome, input_json, bounded_reason, when),
                )
                if outcome == "answered":
                    self._close_waiting_unit(
                        conn, row, AttemptKind.INTERRUPT_RESOLVED, stopped_state=None, at=when
                    )
                else:
                    self._close_waiting_unit(
                        conn,
                        row,
                        AttemptKind.INTERRUPT_REJECTED,
                        stopped_state=(
                            UnitState.OPERATOR_REJECTED
                            if row["on_rejected"] == POLICY_STOP
                            else None
                        ),
                        at=when,
                    )
                stored = _row_to_interrupt(self._interrupt_row(conn, row_id)).resolution
        if expired is not None:
            raise expired
        return stored  # type: ignore[return-value]

    def expire_interrupts(self, run_id: str, now: Optional[float] = None) -> List[InterruptRecord]:
        """Record every lapsed, unresolved interrupt of a WAITING unit.

        One transaction. Each lapse writes an ``expired`` resolution and
        applies the request's ``on_expired`` policy: ``stop`` ends the unit
        INTERRUPT_EXPIRED, ``release`` returns it to PENDING. An interrupt
        lapses AT its ``expires_at``; one with no expiry never lapses. An
        interrupt that is not yet lapsed, already resolved, or whose unit is
        not WAITING is left alone, so a second call records nothing. Returns
        the interrupts expired by this call.

        No timer calls this, and no other function of the library does: a
        lapse is recorded only by this verb and by a :meth:`resolve_interrupt`
        that observes one. The edge probe runs before the transaction opens.
        """
        _interrupts.support()
        when = time.time() if now is None else now
        expired_ids: List[int] = []
        with self._writer() as conn:
            self._require_run(conn, run_id)
            for row in _fetch_interrupt_rows(conn, run_id):
                if row["r_outcome"] is not None:
                    continue
                if not _interrupts.lapsed(row["expires_at"], when):
                    continue
                unit_row = self._require_unit(conn, run_id, row["unit_id"])
                if UnitState(unit_row["state"]) is not UnitState.WAITING:
                    continue
                self._record_expiry(conn, row, when)
                expired_ids.append(row["id"])
            records = [
                _row_to_interrupt(self._interrupt_row(conn, row_id)) for row_id in expired_ids
            ]
        return records

    def list_interrupts(
        self, run_id: str, unit_id: Optional[str] = None
    ) -> List[InterruptRecord]:
        """A run's (or one unit's) interrupts, oldest first, each with its
        policies and its resolution or ``None``. A read; it needs no edge."""
        with self._connect() as conn:
            rows = _fetch_interrupt_rows(conn, run_id, unit_id)
        return [_row_to_interrupt(r) for r in rows]

    def get_interrupt(self, run_id: str, interrupt_id: str) -> Optional[InterruptRecord]:
        """The interrupt with this id in ``run_id``, or ``None``. A read."""
        row_id = _interrupt_row_id(interrupt_id)
        if row_id is None:
            return None
        with self._connect() as conn:
            row = self._interrupt_row(conn, row_id)
        if row is None or row["run_id"] != run_id:
            return None
        return _row_to_interrupt(row)

    def open_interrupt(self, run_id: str, unit_id: str) -> Optional[InterruptRecord]:
        """The unit's one unresolved interrupt, or ``None``. A read."""
        with self._connect() as conn:
            row = self._open_interrupt_row(conn, run_id, unit_id)
        return _row_to_interrupt(row) if row is not None else None

    # -- dispatcher (launcher-election) lease, B1 ---------------------------------
    #
    # A SEPARATE lease from a per-unit claim lease (`claim_unit`/`renew_lease`
    # above): this one is held by at most one background-lane DISPATCHER
    # process for the whole run, so an accidental second dispatcher exits
    # without launching duplicate work. Same fencing shape as a unit's own
    # `fencing_token` -- `dispatcher_fence` is bumped on every successful
    # acquire and a stale (dispatcher_id, fence) pair on renew/release is
    # rejected the same way a stale unit fence is (checked FIRST, before any
    # other validation).

    def acquire_dispatcher_lease(
        self,
        run_id: str,
        dispatcher_id: str,
        *,
        lease_seconds: float,
        at: Optional[float] = None,
    ) -> Optional[int]:
        """Attempt to become (or remain) ``run_id``'s dispatcher.

        Succeeds -- returns the new ``dispatcher_fence`` -- when nobody
        currently holds a live lease, the current holder's lease has
        expired, or ``dispatcher_id`` already IS the current holder (a
        same-dispatcher re-acquire always succeeds, whether or not its own
        lease was still live; this is what lets the SAME dispatcher_id
        re-acquire after its own lease expired, per the author ruling this
        method ships against). Fails -- returns ``None``, never raises --
        only when a DIFFERENT dispatcher_id holds a still-live lease; a
        failed acquire is an ordinary, expected outcome for "an accidental
        second dispatcher", not an error.
        """
        now = time.time() if at is None else at
        with self._writer() as conn:
            run_row = self._require_run(conn, run_id)
            current_id = run_row["dispatcher_id"] if "dispatcher_id" in run_row.keys() else None
            current_expires = (
                run_row["dispatcher_lease_expires_at"]
                if "dispatcher_lease_expires_at" in run_row.keys()
                else None
            )
            current_fence = (
                (run_row["dispatcher_fence"] or 0) if "dispatcher_fence" in run_row.keys() else 0
            )
            held_by_other_live = (
                current_id is not None
                and current_id != dispatcher_id
                and current_expires is not None
                and current_expires > now
            )
            if held_by_other_live:
                return None
            new_fence = current_fence + 1
            new_expires = now + lease_seconds
            conn.execute(
                "UPDATE runs SET dispatcher_id = ?, dispatcher_lease_expires_at = ?, "
                "dispatcher_fence = ? WHERE id = ?",
                (dispatcher_id, new_expires, new_fence, run_id),
            )
        return new_fence

    def _require_dispatcher_lease(
        self, conn: sqlite3.Connection, run_id: str, dispatcher_id: str, fence: int
    ) -> sqlite3.Row:
        """Require ``run_id``'s run row to exist AND its current dispatcher
        lease to be held by exactly ``(dispatcher_id, fence)``, raising
        :class:`StaleDispatcherLeaseError` on any mismatch -- checked first,
        before anything else, same convention as :meth:`renew_lease`. Shared
        by :meth:`renew_dispatcher_lease` and :meth:`release_dispatcher_lease`,
        which differ only in what they do once the lease checks out.
        """
        run_row = self._require_run(conn, run_id)
        current_id = run_row["dispatcher_id"]
        current_fence = run_row["dispatcher_fence"] or 0
        if current_id != dispatcher_id or fence != current_fence:
            raise StaleDispatcherLeaseError(
                run_id, dispatcher_id, fence, current_id, current_fence
            )
        return run_row

    def renew_dispatcher_lease(
        self,
        run_id: str,
        dispatcher_id: str,
        fence: int,
        *,
        lease_seconds: float,
        at: Optional[float] = None,
    ) -> float:
        """Extend the live dispatcher lease. Returns the new expiry.

        Raises :class:`StaleDispatcherLeaseError` when ``(dispatcher_id,
        fence)`` does not match the run's current holder -- see
        :meth:`_require_dispatcher_lease`.
        """
        now = time.time() if at is None else at
        with self._writer() as conn:
            self._require_dispatcher_lease(conn, run_id, dispatcher_id, fence)
            new_expires = now + lease_seconds
            conn.execute(
                "UPDATE runs SET dispatcher_lease_expires_at = ? WHERE id = ?",
                (new_expires, run_id),
            )
        return new_expires

    def release_dispatcher_lease(
        self, run_id: str, dispatcher_id: str, fence: int, *, at: Optional[float] = None
    ) -> None:
        """Voluntarily give up the dispatcher lease (a clean dispatcher exit).

        Raises :class:`StaleDispatcherLeaseError` on a ``(dispatcher_id,
        fence)`` mismatch -- see :meth:`_require_dispatcher_lease`.
        ``dispatcher_fence`` itself is left unchanged (it is a monotonic
        counter, never reset) -- only ``dispatcher_id`` and
        ``dispatcher_lease_expires_at`` are cleared, so a later acquire by
        anyone still gets a strictly higher fence than this one.
        """
        with self._writer() as conn:
            self._require_dispatcher_lease(conn, run_id, dispatcher_id, fence)
            conn.execute(
                "UPDATE runs SET dispatcher_id = NULL, dispatcher_lease_expires_at = NULL "
                "WHERE id = ?",
                (run_id,),
            )

    # -- dispatches (background-session launches), B1 -----------------------------

    def record_dispatch(
        self,
        run_id: str,
        unit_id: str,
        worker_id: str,
        *,
        session_id: Optional[str] = None,
        cli_version: Optional[str] = None,
        at: Optional[float] = None,
    ) -> int:
        """Record a new background-session launch of ``unit_id``.

        ``worker_id`` is minted by the DRIVER before launch (the author
        ruling this ships against); ``session_id`` -- the Claude session id
        -- is usually not yet known at this point (``claude --bg`` reports it
        only after the process spawns) and may be attached later via
        :meth:`settle_dispatch`. Raises ``sqlite3.IntegrityError`` if
        ``unit_id`` already has an OPEN dispatch (the guarded uniqueness
        index) -- the database-level half of "one agent claims one unit".
        Returns the new ``dispatches.id``.
        """
        now = time.time() if at is None else at
        with self._writer() as conn:
            self._require_run(conn, run_id)
            self._require_unit(conn, run_id, unit_id)
            cursor = conn.execute(
                "INSERT INTO dispatches(run_id, unit_id, worker_id, session_id, launched_at, "
                "cli_version) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, unit_id, worker_id, session_id, now, cli_version),
            )
            dispatch_id = cursor.lastrowid
        return dispatch_id

    def _open_dispatch_row(
        self, conn: sqlite3.Connection, run_id: str, unit_id: str
    ) -> sqlite3.Row:
        """Require ``run_id``/``unit_id`` to exist AND have a currently OPEN
        (``settled_at IS NULL``) dispatch, returning its ``id``/``session_id``
        row (most recent by id, though the guarded uniqueness index means
        there is ever only one). Raises :class:`NoOpenDispatchError`
        otherwise. Shared by :meth:`settle_dispatch` and
        :meth:`attach_dispatch_session`, which differ only in what they do
        with the row once found.
        """
        self._require_run(conn, run_id)
        self._require_unit(conn, run_id, unit_id)
        row = conn.execute(
            "SELECT id, session_id FROM dispatches WHERE run_id = ? AND unit_id = ? "
            "AND settled_at IS NULL ORDER BY id DESC LIMIT 1",
            (run_id, unit_id),
        ).fetchone()
        if row is None:
            raise NoOpenDispatchError(run_id, unit_id)
        return row

    def settle_dispatch(
        self,
        run_id: str,
        unit_id: str,
        *,
        outcome: str,
        session_id: Optional[str] = None,
        at: Optional[float] = None,
    ) -> None:
        """Close the currently OPEN dispatch for ``unit_id`` (most recent by
        id, though the guarded uniqueness index means there is ever only
        one). ``session_id``, when supplied, overwrites whatever was
        recorded at :meth:`record_dispatch` time -- the "recorded alongside
        once known" half of the identity contract. Raises
        :class:`NoOpenDispatchError` when there is no open dispatch to
        settle.
        """
        now = time.time() if at is None else at
        with self._writer() as conn:
            row = self._open_dispatch_row(conn, run_id, unit_id)
            new_session_id = session_id if session_id is not None else row["session_id"]
            conn.execute(
                "UPDATE dispatches SET settled_at = ?, outcome = ?, session_id = ? WHERE id = ?",
                (now, outcome, new_session_id, row["id"]),
            )

    def attach_dispatch_session(self, run_id: str, unit_id: str, session_id: str) -> None:
        """Attach the confirmed session id to the open dispatch row.

        An empty session id can be filled once. Repeating the same attachment
        is idempotent. A different id is rejected to preserve launch identity.
        """
        if not session_id:
            raise ValueError("session_id must be non-empty")
        with self._writer() as conn:
            row = self._open_dispatch_row(conn, run_id, unit_id)
            if row["session_id"] not in (None, session_id):
                raise ValueError(
                    f"dispatch {run_id!r}/{unit_id!r} already has session "
                    f"{row['session_id']!r}"
                )
            conn.execute(
                "UPDATE dispatches SET session_id = ? WHERE id = ?",
                (session_id, row["id"]),
            )

    def open_dispatches(self, run_id: str) -> List[DispatchRecord]:
        """Every currently OPEN (``settled_at IS NULL``) dispatch for
        ``run_id``, ordinal by insertion order."""
        with self._connect() as conn:
            self._require_run(conn, run_id)
            rows = conn.execute(
                "SELECT * FROM dispatches WHERE run_id = ? AND settled_at IS NULL ORDER BY id",
                (run_id,),
            ).fetchall()
        return [_row_to_dispatch(r) for r in rows]


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "DEFAULT_LEASE_SECONDS",
    "LEASE_HEADROOM_FACTOR",
    "lease_for",
    "looks_like_network_path",
    "ExecutionStore",
]
