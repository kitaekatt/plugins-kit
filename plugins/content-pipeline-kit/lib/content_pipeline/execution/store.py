"""SQLite-backed durable run store: runs, units, attempts, claims, leases.

The store is the ONLY place run truth lives. Everything above it -- a
prepare/finalize controller, a worker protocol, a driver -- is a later phase;
this module owns just the primitives the plan settles for A-min.1: run
identity, unit registration, atomic claims with monotonically increasing
fencing tokens, lease expiry, and an append-only attempt/event log with
nullable usage.

Operational posture (settled by the plan, not redesigned here):

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
(invariant 4).
"""

from __future__ import annotations

import ctypes
import sqlite3
import sys
import time
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple, Union

from content_pipeline.execution.model import (
    AlreadyClaimedError,
    AttemptKind,
    AttemptRecord,
    ClaimResult,
    DuplicateUnitError,
    NotAcceptedError,
    NotClaimedError,
    RunHaltedError,
    RunRecord,
    StaleFenceError,
    TERMINAL_STATES,
    TerminalStateError,
    UnitRecord,
    UnitState,
    UnknownRunError,
    UnknownUnitError,
    UsageRecord,
)

DEFAULT_BUSY_TIMEOUT_MS = 5000
DEFAULT_LEASE_SECONDS = 300.0
_ERROR_TRUNCATE = 500  # a defensive cap; error text is operational, not content

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
]


def _row_to_run(row: sqlite3.Row) -> RunRecord:
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


def _fetch_run_row(conn: sqlite3.Connection, run_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def _fetch_unit_rows(conn: sqlite3.Connection, run_id: str) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM units WHERE run_id = ? ORDER BY ordinal", (run_id,)
    ).fetchall()


def _fetch_attempt_rows(
    conn: sqlite3.Connection, run_id: str, unit_id: Optional[str] = None
) -> List[sqlite3.Row]:
    if unit_id is None:
        return conn.execute(
            "SELECT * FROM attempts WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM attempts WHERE run_id = ? AND unit_id = ? ORDER BY id",
        (run_id, unit_id),
    ).fetchall()


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
    ) -> RunRecord:
        """Create a new run row. Raises on a duplicate ``run_id``."""
        at = time.time() if created_at is None else created_at
        with self._writer() as conn:
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
        error: Optional[str] = None,
        usage: Optional[UsageRecord] = None,
    ) -> None:
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
        :class:`RunHaltedError` (D4: halt blocks new claims, never a
        fenced-valid submission already in flight).
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
        """
        now = time.time() if at is None else at
        with self._writer() as conn:
            self._require_run(conn, run_id)
            unit_row = self._require_unit(conn, run_id, unit_id)
            current = unit_row["fencing_token"]
            if fencing_token != current:
                raise StaleFenceError(run_id, unit_id, fencing_token, current)

            state = UnitState(unit_row["state"])
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
        """Terminally accept a unit (D4: valid-fence acceptance ignores halt).

        Deliberately does not consult the run's halt state -- a submission
        carrying a valid fencing token is completed, paid-for work and is
        recorded exactly as if no halt existed (D4). Only a STALE fencing
        token is rejected, regardless of halt state.

        The fencing check happens FIRST, before any state check (invariant 4
        / defect fix): a presented token that does not match the unit's
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
        # discard-on-reject bug invariant 4 requires NOT happen.
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
        error: str = "",
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

    def record_apply_started(
        self, run_id: str, unit_id: str, *, at: Optional[float] = None
    ) -> None:
        """Record that finalize is about to call the adapter's apply (D6).

        Requires the unit to be ACCEPTED; raises :class:`NotAcceptedError`
        otherwise -- finalize only ever applies accepted units. No fencing
        check: apply runs after acceptance, under the dispatcher's
        documented single-writer discipline (see the module docstring), not
        under worker-claim contention, so there is no competing fence to
        validate against here the way there is in claim/accept/fail.
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
                AttemptKind.APPLY_STARTED,
                at=now,
                fencing_token=unit_row["fencing_token"],
            )

    def record_apply_succeeded(
        self, run_id: str, unit_id: str, *, at: Optional[float] = None
    ) -> None:
        """Record that the adapter's apply returned without raising (D6).

        Same ACCEPTED requirement and no-fencing rationale as
        :meth:`record_apply_started` -- see that docstring. Recording this
        twice (e.g. a retried finalize pass) simply appends a second
        attempt row; it is not itself the idempotence mechanism. Finalize
        idempotence is derived by scanning the attempt log for an
        APPLY_STARTED with no following APPLY_SUCCEEDED (``apply_unknown``,
        per the model module docstring), not enforced by this method.
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
                AttemptKind.APPLY_SUCCEEDED,
                at=now,
                fencing_token=unit_row["fencing_token"],
            )

    # -- attempts ----------------------------------------------------------------

    def list_attempts(self, run_id: str, unit_id: Optional[str] = None) -> List[AttemptRecord]:
        with self._connect() as conn:
            rows = _fetch_attempt_rows(conn, run_id, unit_id)
        return [_row_to_attempt(r) for r in rows]

    # -- consistent multi-query snapshot ------------------------------------------

    def snapshot(
        self, run_id: str
    ) -> Tuple[Optional[RunRecord], List[UnitRecord], List[AttemptRecord]]:
        """One consistent read-transaction view of a run, its units, and its attempts.

        Used by :func:`~content_pipeline.execution.status.compute_status` so
        that a write landing between "read units" and "read attempts" cannot
        produce a torn digest (e.g. a count that reflects the unit's new
        state but a failure-group tally computed from the attempt that caused
        it, or vice versa). All three queries run inside one
        :meth:`read_transaction`.
        """
        with self.read_transaction() as conn:
            run_row = _fetch_run_row(conn, run_id)
            run = _row_to_run(run_row) if run_row is not None else None
            units = [_row_to_unit(r) for r in _fetch_unit_rows(conn, run_id)]
            attempts = [_row_to_attempt(r) for r in _fetch_attempt_rows(conn, run_id)]
        return run, units, attempts


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "DEFAULT_LEASE_SECONDS",
    "looks_like_network_path",
    "ExecutionStore",
]
