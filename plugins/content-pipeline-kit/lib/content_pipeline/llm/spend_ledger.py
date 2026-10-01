"""spend_ledger -- a cross-process USD spend ledger over one SQLite file.

An in-process accumulator (``platform.CostBudget``) cannot stop two OS
processes from each spending the whole cap. This module holds the cap in a
single SQLite file in WAL mode and admits every reservation inside ONE
``BEGIN IMMEDIATE`` transaction that reads the outstanding total, compares it
to the cap, and inserts the row. SQLite permits one write transaction per
file, so a second admission's ``SUM`` either precedes the first
transaction's lock acquisition or follows its commit: two admissions never
interleave, and neither reads totals and then waits for a lock.

Amounts are integer nano-USD (``NANO_PER_USD``) rounded with
``decimal.ROUND_CEILING``, so the cap is exact and never drifts with binary
floating point.

Fail-closed, deliberately
-------------------------

``sqlite3.OperationalError`` raised after ``busy_timeout`` is exhausted
propagates unchanged: :meth:`SpendLedger.reserve` admits nothing, the
transaction helper does NOT retry (matching
``content_pipeline.execution.store.ExecutionStore._writer``; only the WAL
pragma retries, because ``PRAGMA journal_mode = WAL`` does not honour
``busy_timeout``), and the error is NOT wrapped in
:class:`~content_pipeline.llm.platform.BudgetExceededError` -- an unreachable
ledger is infrastructure, not a budget verdict.

A settle whose cost is unreadable HOLDS the reservation as ``unknown`` at the
reserved amount. There is no release-on-failure rule: an attempt that billed
the provider and then lost its cost must keep consuming the cap, or the cap
stops bounding real spend. :meth:`SpendLedger.release` exists for a caller
that knows no money was spent and is never called on an error path.

Two tables, five states
-----------------------

``ledger`` holds one row per admitted reservation, each in exactly one of
``open | unknown | settled | overbilled | reclaimed``, each carrying a
positive integer ``reserved`` and a non-NULL ``generation``. ``leaks`` is a
SEPARATE append-only table for a cost presented by a settle that lost its
compare-and-set; a leak is not a reservation (no ``reserved``, no lease, no
state), and keeping it out of ``ledger`` is what makes the five states
exhaustive.

What this module cannot enforce
-------------------------------

A caller that bills a provider without reserving spends outside the cap, and
nothing inside the ledger can see it. Reserve-before-pay is a property of the
CALL SITE, not of this file.

Import rules: standard library plus ``decimal`` only, and one one-way edge to
``content_pipeline.llm.platform`` for ``BudgetExceededError`` and
``estimate_cost``. Nothing from ``content_pipeline.execution`` is imported --
see :func:`looks_like_network_path`.
"""

from __future__ import annotations

import ctypes
import json
import os
import sqlite3
import sys
import time
import uuid
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Union

from content_pipeline.llm.platform import BudgetExceededError, estimate_cost

__all__ = [
    "NANO_PER_USD",
    "SCHEMA_VERSION",
    "LEDGER_STATES",
    "SpendLedger",
    "Reservation",
    "SpendStatus",
    "SpendCapExceeded",
    "SpendLedgerHalted",
    "StaleReservationError",
    "LedgerIdentityChanged",
    "LedgerStateInvalid",
    "create_ledger",
    "open_ledger",
    "spend_ledger_from_env",
    "estimate_reservation",
    "looks_like_network_path",
    "LEDGER_PATH_ENV",
]

#: Nano-USD per USD. Every stored amount is an integer count of these.
NANO_PER_USD = 1_000_000_000

#: The schema this module creates and the only one it opens.
SCHEMA_VERSION = 1

#: The five exhaustive states of a ``ledger`` row.
LEDGER_STATES = ("open", "unknown", "settled", "overbilled", "reclaimed")

#: Environment variable :func:`spend_ledger_from_env` reads.
LEDGER_PATH_ENV = "CONTENT_PIPELINE_SPEND_LEDGER"

_DEFAULT_BUSY_TIMEOUT_MS = 5000


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SpendCapExceeded(BudgetExceededError):
    """Admission refused: the reservation would push spend past the cap.

    A :class:`~content_pipeline.llm.platform.BudgetExceededError` subclass
    carrying ``identifier`` / ``measured`` / ``budget`` / ``model``, so an
    existing ``except BudgetExceededError`` keeps working. Also raised when a
    settle records a cost ABOVE its reservation (``measured`` is the settled
    cost and ``budget`` the reserved amount): the cap arithmetic that admitted
    the row is then known to have understated real spend.
    """


class SpendLedgerHalted(BudgetExceededError):
    """Admission refused: the ledger's halt row is set.

    Also a :class:`~content_pipeline.llm.platform.BudgetExceededError`
    subclass. ``settle`` never raises this -- money already spent must be
    recorded regardless of a halt.
    """

    def __init__(
        self,
        *,
        identifier: str,
        measured: float,
        budget: float,
        model: str = "",
        reason: str = "",
        detail: str = "",
    ) -> None:
        super().__init__(
            identifier=identifier, measured=measured, budget=budget, model=model
        )
        self.reason = reason
        self.detail = detail


class StaleReservationError(Exception):
    """A settle or release lost its compare-and-set.

    The row is no longer ``open`` at the presented ``generation`` -- it was
    already resolved, or an orphan sweep reclaimed it. Not a budget verdict.
    """


class LedgerIdentityChanged(Exception):
    """The ledger file's pinned policy no longer matches the open handle.

    Raised from inside the transaction, so nothing is admitted against a cap
    or a run the caller did not open. Not a budget verdict.
    """


class LedgerStateInvalid(Exception):
    """A per-transaction structural query found a malformed row.

    Raised before the transaction body runs, so a malformed ledger refuses
    further admission rather than computing a total over rows whose meaning is
    unknown. Not a budget verdict.
    """


# ---------------------------------------------------------------------------
# Nano-USD arithmetic
# ---------------------------------------------------------------------------


def _to_decimal(value: Union[int, float, Decimal]) -> Decimal:
    """Coerce a USD amount to ``Decimal`` without inheriting binary noise.

    A ``float`` goes through ``str`` first (``Decimal(0.1)`` is
    ``0.1000000000000000055511151231257827``, ``Decimal("0.1")`` is not), so
    ceiling rounding does not add a nano to a value the caller wrote exactly.
    """
    if isinstance(value, Decimal):
        decimal_value = value
    elif isinstance(value, bool):  # bool is an int subclass; refuse it outright
        raise TypeError(f"USD amount must be a number, not {value!r}")
    elif isinstance(value, (int, float)):
        decimal_value = Decimal(str(value))
    else:
        raise TypeError(f"USD amount must be a number, not {value!r}")
    if not decimal_value.is_finite():
        raise ValueError(f"USD amount must be finite, got {value!r}")
    return decimal_value


def usd_to_nano(value: Union[int, float, Decimal]) -> int:
    """Convert USD to integer nano-USD, rounding UP.

    Ceiling rounding is what makes the cap exact in the direction that matters:
    a cost the ledger cannot represent is charged as the next whole nano, never
    truncated toward free.
    """
    try:
        return int((_to_decimal(value) * NANO_PER_USD).to_integral_value(rounding=ROUND_CEILING))
    except InvalidOperation as exc:  # pragma: no cover -- non-finite is caught above
        raise ValueError(f"USD amount {value!r} is not convertible to nano-USD") from exc


def nano_to_usd(nano: int) -> float:
    """Convert integer nano-USD back to USD for reporting."""
    return float(Decimal(int(nano)) / NANO_PER_USD)


def estimate_reservation(
    model: str,
    *,
    input_tokens: int,
    max_output_tokens: int,
    pricing: Mapping[str, Any],
    cache_hit_tokens: int = 0,
) -> float:
    """Return the worst-case USD cost of one attempt, for use as a reservation.

    Prices ``max_output_tokens`` rather than an expected output length, because
    a reservation must cover what the attempt may actually bill. Delegates to
    ``platform.estimate_cost``, so an unknown model raises :class:`KeyError`
    exactly as the rest of the pricing path does -- a typo must never reserve
    0 and then bill.
    """
    return estimate_cost(
        model,
        input_tokens,
        max_output_tokens,
        cache_hit_tokens,
        pricing=pricing,
    )


# ---------------------------------------------------------------------------
# Network-path detection
# ---------------------------------------------------------------------------

_DRIVE_REMOTE = 4  # Windows GetDriveTypeW result for a mapped/UNC network drive


def looks_like_network_path(path: Union[str, Path]) -> bool:
    """Best-effort check whether ``path`` resolves to a network filesystem.

    A DUPLICATE of ``content_pipeline.execution.store.looks_like_network_path``
    and must stay behaviourally identical to it (there is a parity test). It is
    duplicated rather than imported because ``llm/`` may not import
    ``execution/`` -- keep both copies in step when either changes.

    WAL mode is unsafe on a network share, so the ledger warns. False positives
    are tolerated (the caller only warns, never refuses) and false negatives
    are expected: POSIX network mounts under a local-looking path are not
    detected.
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
# Schema -- the WHOLE schema, created in one transaction, never migrated
# ---------------------------------------------------------------------------

_SCHEMA: tuple = (
    "CREATE TABLE schema_version (version INTEGER NOT NULL)",
    # One pinned policy row. Every transaction re-reads it and compares it to
    # the values this handle was opened with, so a tampered or swapped file
    # cannot quietly widen the cap, change the run, or change the money unit.
    """CREATE TABLE policy (
           id           INTEGER PRIMARY KEY CHECK (id = 1),
           run_id       TEXT    NOT NULL,
           cap_nano     INTEGER NOT NULL,
           nano_per_usd INTEGER NOT NULL,
           created_at   TEXT    NOT NULL,
           meta         TEXT    NOT NULL
       )""",
    # One-row control table. `halted` is 0 or 1; reason/detail/since are set
    # together with it.
    """CREATE TABLE halt (
           id      INTEGER PRIMARY KEY CHECK (id = 1),
           halted  INTEGER NOT NULL,
           reason  TEXT,
           detail  TEXT,
           since   TEXT
       )""",
    """CREATE TABLE halt_history (
           seq    INTEGER PRIMARY KEY AUTOINCREMENT,
           action TEXT    NOT NULL,
           reason TEXT,
           detail TEXT,
           forced INTEGER NOT NULL DEFAULT 0,
           at     TEXT    NOT NULL
       )""",
    # One row per admitted reservation (one per provider attempt). Every row is
    # in exactly one of the five states, carries a positive integer `reserved`
    # and a non-NULL `generation`.
    """CREATE TABLE ledger (
           id               TEXT    PRIMARY KEY,
           generation       INTEGER NOT NULL,
           state            TEXT    NOT NULL,
           reserved         INTEGER NOT NULL,
           settled          INTEGER,
           scope            TEXT    NOT NULL DEFAULT '',
           identifier       TEXT    NOT NULL DEFAULT '',
           model            TEXT    NOT NULL DEFAULT '',
           created_at       TEXT    NOT NULL,
           lease_expires_at REAL,
           resolved_at      TEXT
       )""",
    "CREATE INDEX ledger_state ON ledger (state)",
    "CREATE INDEX ledger_lease ON ledger (lease_expires_at)",
    # Append-only. A cost presented by a settle that lost its compare-and-set.
    # No `reserved`, no lease, no state -- which is what keeps the five states
    # of `ledger` exhaustive.
    """CREATE TABLE leaks (
           seq           INTEGER PRIMARY KEY AUTOINCREMENT,
           ledger_id     TEXT    NOT NULL,
           reported_cost INTEGER NOT NULL,
           presented_at  TEXT    NOT NULL,
           generation    INTEGER NOT NULL
       )""",
    "CREATE INDEX leaks_ledger_id ON leaks (ledger_id)",
)

_STATE_LIST_SQL = ", ".join("'%s'" % s for s in LEDGER_STATES)

# The per-transaction structural query, split per table exactly as the design
# specifies. Every clause must return 0 rows; the first row returned names the
# table and the violated clause, so a failure is diagnosable without a second
# query. Keep each clause a separate SELECT: a test reverts one clause at a
# time to show it has teeth.
_LEDGER_CLAUSES: tuple = (
    ("reserved_not_integer", "SELECT id FROM ledger WHERE typeof(reserved) <> 'integer'"),
    ("reserved_not_positive", "SELECT id FROM ledger WHERE reserved <= 0"),
    ("state_outside_five", "SELECT id FROM ledger WHERE state NOT IN (%s)" % _STATE_LIST_SQL),
    (
        "settled_set_on_unresolved",
        "SELECT id FROM ledger WHERE settled IS NOT NULL AND state IN ('open', 'unknown')",
    ),
    (
        "settled_null_on_resolved",
        "SELECT id FROM ledger WHERE settled IS NULL "
        "AND state IN ('settled', 'overbilled', 'reclaimed')",
    ),
    (
        "settled_above_reserved_on_settled",
        "SELECT id FROM ledger WHERE state = 'settled' AND settled > reserved",
    ),
    (
        "settled_not_above_reserved_on_overbilled",
        "SELECT id FROM ledger WHERE state = 'overbilled' AND settled <= reserved",
    ),
    ("generation_null", "SELECT id FROM ledger WHERE generation IS NULL"),
)

_LEAKS_CLAUSES: tuple = (
    (
        "reported_cost_not_positive",
        "SELECT ledger_id FROM leaks WHERE reported_cost IS NULL OR reported_cost <= 0",
    ),
    (
        "ledger_id_unknown",
        "SELECT ledger_id FROM leaks WHERE ledger_id NOT IN (SELECT id FROM ledger)",
    ),
    (
        "ledger_row_not_reclaimed",
        "SELECT k.ledger_id FROM leaks k JOIN ledger l ON l.id = k.ledger_id "
        "WHERE l.state <> 'reclaimed'",
    ),
)

STRUCTURAL_CLAUSES: tuple = tuple(
    ("ledger", name, sql) for name, sql in _LEDGER_CLAUSES
) + tuple(("leaks", name, sql) for name, sql in _LEAKS_CLAUSES)


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reservation:
    """One admitted reservation: the receipt a settle or release presents back.

    ``scope`` is a recorded report label, NOT an admission input -- there is
    one cap per ledger file and admission reads the whole-file outstanding
    total. ``lease_expires_at`` is epoch seconds, or ``None`` when no deadline
    is known (an unbounded attempt), in which case no sweep may ever reclaim
    the row.
    """

    id: str
    generation: int
    amount_usd: float
    amount_nano: int
    scope: str
    identifier: str
    created_at: str
    lease_expires_at: Optional[float]


@dataclass(frozen=True)
class SpendStatus:
    """A consistent snapshot of the ledger, all amounts in USD.

    ``outstanding_usd`` is the admission quantity:
    ``settled + reserved + unknown + leaked``. ``remaining_usd`` is
    ``cap - outstanding`` and MAY BE NEGATIVE -- clamp only for display, never
    for arithmetic. ``reclaimed_usd`` and ``written_off_usd`` are disclosure
    only and contribute 0 to ``outstanding_usd``.
    """

    run_id: str
    cap_usd: float
    settled_usd: float
    reserved_usd: float
    unknown_usd: float
    leaked_usd: float
    reclaimed_usd: float
    written_off_usd: float
    outstanding_usd: float
    remaining_usd: float
    reservations_open: int
    calls_settled: int
    requests: int
    halted: bool
    halt_reason: str
    halt_detail: str
    as_of: str


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class SpendLedger:
    """A cap shared by every process that opens the same file.

    Construct through :func:`create_ledger` or :func:`open_ledger`, never
    directly: the first writes the pinned policy row, the second reads it, and
    both are what make ``cap_usd`` immutable for the life of the file.
    """

    def __init__(
        self,
        path: Union[str, Path],
        *,
        run_id: str,
        cap_nano: int,
        busy_timeout_ms: int = _DEFAULT_BUSY_TIMEOUT_MS,
        halt_on_overbilled: bool = True,
        synchronous: str = "FULL",
        warn_on_network_path: bool = True,
    ) -> None:
        self._path = Path(path)
        self._run_id = run_id
        self._cap_nano = int(cap_nano)
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._halt_on_overbilled = bool(halt_on_overbilled)
        self._synchronous = str(synchronous)
        if warn_on_network_path and looks_like_network_path(self._path):
            warnings.warn(
                f"spend ledger {self._path} looks like a network path; SQLite WAL "
                "is unsafe on a network filesystem and the cap may not hold",
                RuntimeWarning,
                stacklevel=3,
            )

    # -- immutable identity ---------------------------------------------------

    @property
    def path(self) -> Path:
        """The ledger file."""
        return self._path

    @property
    def run_id(self) -> str:
        """The pinned run identity. Re-validated inside every transaction."""
        return self._run_id

    @property
    def cap_usd(self) -> float:
        """The cap, in USD. Read-only BY DESIGN: there is no setter.

        The cap is written once by :func:`create_ledger`, held here, and
        re-validated against the policy row inside every transaction. It is
        never re-read from configuration or the environment, so a run cannot
        widen its own cap mid-flight. A different cap means a different file.
        """
        return nano_to_usd(self._cap_nano)

    @property
    def cap_nano(self) -> int:
        """The cap in integer nano-USD. Read-only, as :attr:`cap_usd` is."""
        return self._cap_nano

    @property
    def schema_version(self) -> int:
        """The schema version this handle speaks."""
        return SCHEMA_VERSION

    @property
    def busy_timeout_ms(self) -> int:
        """How long a connection waits for the write lock before failing."""
        return self._busy_timeout_ms

    @property
    def halt_on_overbilled(self) -> bool:
        """Whether a settle above its reservation also sets the halt row."""
        return self._halt_on_overbilled

    # -- connection plumbing --------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open one connection, apply per-connection pragmas, close on exit."""
        conn = sqlite3.connect(str(self._path), timeout=self._busy_timeout_ms / 1000.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.isolation_level = None  # explicit BEGIN; no implicit transaction
            conn.execute(f"PRAGMA busy_timeout = {int(self._busy_timeout_ms)}")
            self._ensure_wal(conn)
            conn.execute(f"PRAGMA synchronous = {self._synchronous}")
            yield conn
        finally:
            conn.close()

    def _ensure_wal(self, conn: sqlite3.Connection) -> None:
        """Put ``conn`` into WAL mode without racing concurrent first-opens.

        ``PRAGMA journal_mode`` with no argument is a lock-free read. The form
        that SETS the mode takes a brief write lock even when the database is
        already in WAL mode, and does NOT honour ``busy_timeout`` -- it fails
        almost instantly instead of waiting. So read first and skip the write
        in the common case; when the write is genuinely needed, retry it here
        against the busy-timeout budget. This pragma is the ONLY thing in this
        module that retries; no transaction does.
        """
        current = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if isinstance(current, str) and current.lower() == "wal":
            return
        deadline = time.monotonic() + (self._busy_timeout_ms / 1000.0)
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
                        f"{self._busy_timeout_ms} ms (database is locked): {exc}"
                    ) from exc
                time.sleep(0.005)

    @contextmanager
    def _writer(self) -> Iterator[sqlite3.Connection]:
        """A validated connection inside a ``BEGIN IMMEDIATE`` transaction.

        ``BEGIN IMMEDIATE`` takes the write lock BEFORE the first read, so an
        admission's read-compare-insert can never interleave with another's.
        It also WAITS against ``busy_timeout`` rather than failing fast, which
        makes contention a latency cost.

        NO RETRY: a ``sqlite3.OperationalError`` after the busy timeout is
        exhausted propagates to the caller unchanged, having admitted nothing.
        Identity and structure are validated BEFORE the body runs, so nothing
        is admitted against a cap, a run, or a row set whose meaning changed.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._validate_identity(conn)
                self._validate_structure(conn)
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        """A validated read-only snapshot under a plain deferred ``BEGIN``.

        The ONLY plain ``BEGIN`` in this module. In WAL mode the read snapshot
        is fixed at the first read and writers append without invalidating it,
        so every query in the body sees one state without blocking a writer.
        Read-only by contract: never execute an INSERT/UPDATE/DELETE here.
        """
        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                self._validate_identity(conn)
                self._validate_structure(conn)
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    # -- per-transaction validation ------------------------------------------

    def _validate_identity(self, conn: sqlite3.Connection) -> None:
        """Re-pin the policy row, or refuse the transaction.

        Carried over from the prior-art ledger: a handle must not act on a
        file whose pinned cap, run or money unit has changed under it, so the
        comparison runs inside EVERY transaction and not merely at open.
        """
        row = conn.execute(
            "SELECT run_id, cap_nano, nano_per_usd FROM policy WHERE id = 1"
        ).fetchone()
        if row is None:
            raise LedgerIdentityChanged(
                f"{self._path}: no policy row; this is not an initialized spend ledger"
            )
        if row["run_id"] != self._run_id:
            raise LedgerIdentityChanged(
                f"{self._path}: run_id changed from {self._run_id!r} to {row['run_id']!r}"
            )
        if int(row["cap_nano"]) != self._cap_nano:
            raise LedgerIdentityChanged(
                f"{self._path}: cap changed from {self._cap_nano} to "
                f"{int(row['cap_nano'])} nano-USD"
            )
        if int(row["nano_per_usd"]) != NANO_PER_USD:
            raise LedgerIdentityChanged(
                f"{self._path}: money unit is {int(row['nano_per_usd'])} nano per USD, "
                f"this build speaks {NANO_PER_USD}"
            )

    def _validate_structure(self, conn: sqlite3.Connection) -> None:
        """Run the malformed-state query; every clause must return 0 rows."""
        for table, clause, sql in STRUCTURAL_CLAUSES:
            row = conn.execute(sql + " LIMIT 1").fetchone()
            if row is not None:
                raise LedgerStateInvalid(
                    f"{self._path}: {table} violates {clause} (row {row[0]!r})"
                )

    # -- partition ------------------------------------------------------------

    @staticmethod
    def _partition_nano(conn: sqlite3.Connection) -> dict:
        """The partition terms, all integer nano-USD, all computed from ROWS.

        An ``overbilled`` row contributes ``reserved`` to SETTLED and
        ``settled - reserved`` to LEAKED, so it is counted exactly once at its
        full settled cost. A ``reclaimed`` row contributes 0 to all four terms:
        its headroom was freed by construction, and a cost arriving for it
        later is a ``leaks`` row, counted there.
        """

        def scalar(sql: str) -> int:
            return int(conn.execute(sql).fetchone()[0] or 0)

        settled = scalar(
            "SELECT COALESCE((SELECT SUM(settled) FROM ledger WHERE state = 'settled'), 0)"
            "     + COALESCE((SELECT SUM(reserved) FROM ledger WHERE state = 'overbilled'), 0)"
        )
        reserved = scalar(
            "SELECT COALESCE(SUM(reserved), 0) FROM ledger WHERE state = 'open'"
        )
        unknown = scalar(
            "SELECT COALESCE(SUM(reserved), 0) FROM ledger WHERE state = 'unknown'"
        )
        leaked = scalar(
            "SELECT COALESCE((SELECT SUM(settled - reserved) FROM ledger "
            "                 WHERE state = 'overbilled'), 0)"
            "     + COALESCE((SELECT SUM(reported_cost) FROM leaks), 0)"
        )
        reclaimed = scalar(
            "SELECT COALESCE(SUM(reserved), 0) FROM ledger WHERE state = 'reclaimed'"
        )
        written_off = scalar(
            "SELECT COALESCE(SUM(reserved), 0) FROM ledger AS l WHERE l.state = 'reclaimed' "
            "AND NOT EXISTS (SELECT 1 FROM leaks AS k WHERE k.ledger_id = l.id)"
        )
        return {
            "settled": settled,
            "reserved": reserved,
            "unknown": unknown,
            "leaked": leaked,
            "reclaimed": reclaimed,
            "written_off": written_off,
            "outstanding": settled + reserved + unknown + leaked,
        }

    # -- halt plumbing (the row this unit writes; the switch is a later unit) --

    @staticmethod
    def _read_halt(conn: sqlite3.Connection) -> sqlite3.Row:
        row = conn.execute("SELECT halted, reason, detail, since FROM halt WHERE id = 1").fetchone()
        if row is None:
            raise LedgerStateInvalid("halt control row is missing")
        return row

    @staticmethod
    def _record_halt(
        conn: sqlite3.Connection, *, reason: str, detail: str, forced: bool = False
    ) -> None:
        """Set the one-row control table and append to ``halt_history``."""
        now = _now_iso()
        conn.execute(
            "UPDATE halt SET halted = 1, reason = ?, detail = ?, since = ? WHERE id = 1",
            (reason, detail, now),
        )
        conn.execute(
            "INSERT INTO halt_history (action, reason, detail, forced, at) VALUES (?, ?, ?, ?, ?)",
            ("halt", reason, detail, 1 if forced else 0, now),
        )

    # -- public operations ----------------------------------------------------

    def reserve(
        self,
        amount_usd: Union[int, float, Decimal],
        *,
        scope: str = "",
        identifier: str = "",
        model: str = "",
        ttl_s: Optional[float] = None,
    ) -> Reservation:
        """Admit ``amount_usd`` against the cap, or refuse.

        SUM the outstanding total, compare it to the cap, and INSERT the row,
        all inside ONE ``BEGIN IMMEDIATE`` transaction. Admission is
        ``outstanding + amount <= cap`` -- INCLUSIVE at equality, so a
        reservation that exactly fills the cap is granted.

        ``ttl_s`` sets the lease: ``None`` (the default) stores a NULL lease
        that no sweep may ever reclaim, which is the only safe choice when no
        deadline bounds the work the reservation covers.

        Raises :class:`SpendLedgerHalted` when the halt row is set,
        :class:`SpendCapExceeded` when the amount does not fit, and propagates
        :class:`sqlite3.OperationalError` unchanged when the write lock could
        not be taken within ``busy_timeout_ms`` -- in every case admitting
        nothing.
        """
        amount_nano = usd_to_nano(amount_usd)
        if amount_nano <= 0:
            raise ValueError(
                f"reservation must be a positive USD amount, got {amount_usd!r} "
                f"({amount_nano} nano-USD)"
            )
        lease: Optional[float] = None
        if ttl_s is not None:
            if not (float(ttl_s) > 0):
                raise ValueError(f"ttl_s must be positive when given, got {ttl_s!r}")
            lease = time.time() + float(ttl_s)

        reservation_id = uuid.uuid4().hex
        created_at = _now_iso()
        with self._writer() as conn:
            halt = self._read_halt(conn)
            if int(halt["halted"]):
                raise SpendLedgerHalted(
                    identifier=identifier or scope or reservation_id,
                    measured=nano_to_usd(amount_nano),
                    budget=self.cap_usd,
                    model=model,
                    reason=halt["reason"] or "",
                    detail=halt["detail"] or "",
                )
            outstanding = self._partition_nano(conn)["outstanding"]
            if outstanding + amount_nano > self._cap_nano:
                raise SpendCapExceeded(
                    identifier=identifier or scope or reservation_id,
                    measured=nano_to_usd(outstanding + amount_nano),
                    budget=self.cap_usd,
                    model=model,
                )
            conn.execute(
                "INSERT INTO ledger (id, generation, state, reserved, settled, scope, "
                "identifier, model, created_at, lease_expires_at, resolved_at) "
                "VALUES (?, 0, 'open', ?, NULL, ?, ?, ?, ?, ?, NULL)",
                (reservation_id, amount_nano, scope, identifier, model, created_at, lease),
            )
        return Reservation(
            id=reservation_id,
            generation=0,
            amount_usd=nano_to_usd(amount_nano),
            amount_nano=amount_nano,
            scope=scope,
            identifier=identifier,
            created_at=created_at,
            lease_expires_at=lease,
        )

    def settle(
        self, reservation: Reservation, cost_usd: Optional[Union[int, float, Decimal]]
    ) -> None:
        """Resolve ``reservation`` with the cost the attempt actually billed.

        ``cost_usd=None`` means the spend is UNREADABLE, and the reservation is
        HELD as ``unknown`` at its reserved amount. It is never released to 0:
        the attempt may well have billed, so the cap must keep counting it.
        There is no release-on-failure rule.

        A cost at or below the reservation records ``settled``. A cost ABOVE it
        records ``overbilled``, keeps the excess visible as leaked money, sets
        the halt row when ``halt_on_overbilled`` is true, and raises
        :class:`SpendCapExceeded` -- the cap arithmetic is now known to have
        understated real spend.

        Compare-and-set on ``(id, generation, state='open')``. A settle that
        loses it raises :class:`StaleReservationError` and changes nothing, so
        a retried settle cannot double-charge.
        """
        cost_nano = None if cost_usd is None else usd_to_nano(cost_usd)
        if cost_nano is not None and cost_nano < 0:
            raise ValueError(f"settled cost must not be negative, got {cost_usd!r}")
        now = _now_iso()
        with self._writer() as conn:
            if cost_nano is None:
                state = "unknown"
                cursor = conn.execute(
                    "UPDATE ledger SET state = 'unknown', settled = NULL, resolved_at = ? "
                    "WHERE id = ? AND generation = ? AND state = 'open'",
                    (now, reservation.id, reservation.generation),
                )
            else:
                row = conn.execute(
                    "SELECT reserved FROM ledger WHERE id = ? AND generation = ? "
                    "AND state = 'open'",
                    (reservation.id, reservation.generation),
                ).fetchone()
                reserved_nano = reservation.amount_nano if row is None else int(row["reserved"])
                state = "overbilled" if cost_nano > reserved_nano else "settled"
                cursor = conn.execute(
                    "UPDATE ledger SET state = ?, settled = ?, resolved_at = ? "
                    "WHERE id = ? AND generation = ? AND state = 'open'",
                    (state, cost_nano, now, reservation.id, reservation.generation),
                )
            if cursor.rowcount == 0:
                # SL-3 adds the `leaks` write here: a late settle against an
                # already-`reclaimed` row presents a real cost that must be
                # recorded rather than discarded. Until reclaim exists, the
                # only way to lose this compare-and-set is a repeated settle,
                # which has nothing new to record.
                raise StaleReservationError(
                    f"reservation {reservation.id} is no longer open at generation "
                    f"{reservation.generation}; nothing was changed"
                )
            if state == "overbilled":
                detail = (
                    f"reservation {reservation.id} settled at {cost_nano} nano-USD "
                    f"over a reservation of {reserved_nano}"
                )
                if self._halt_on_overbilled:
                    self._record_halt(conn, reason="overbilled", detail=detail)
                overbilled = SpendCapExceeded(
                    identifier=reservation.identifier or reservation.id,
                    measured=nano_to_usd(cost_nano),
                    budget=nano_to_usd(reserved_nano),
                )
        if state == "overbilled":
            # Raised AFTER the commit: the overbilled row and its halt are the
            # record of real money, and must survive the exception.
            raise overbilled

    def release(self, reservation: Reservation) -> None:
        """Give back a reservation the caller KNOWS billed nothing.

        Resolves the row as ``settled`` at a cost of 0, which frees the
        headroom and keeps the five states exhaustive. Public API, and NOT an
        error path: an attempt whose cost is merely unreadable must go through
        ``settle(None)`` instead, which holds the reservation.

        Raises :class:`StaleReservationError` when the row is no longer open at
        the presented generation.
        """
        with self._writer() as conn:
            cursor = conn.execute(
                "UPDATE ledger SET state = 'settled', settled = 0, resolved_at = ? "
                "WHERE id = ? AND generation = ? AND state = 'open'",
                (_now_iso(), reservation.id, reservation.generation),
            )
            if cursor.rowcount == 0:
                raise StaleReservationError(
                    f"reservation {reservation.id} is no longer open at generation "
                    f"{reservation.generation}; nothing was released"
                )

    def status(self) -> SpendStatus:
        """One consistent snapshot of the ledger.

        Read-only: one connection, one plain deferred ``BEGIN``, no write
        statement inside. ``remaining_usd`` may be negative and is NOT clamped.
        """
        with self._reader() as conn:
            terms = self._partition_nano(conn)
            counts = conn.execute(
                "SELECT COUNT(*) AS requests,"
                " SUM(CASE WHEN state IN ('open', 'unknown') THEN 1 ELSE 0 END) AS open_count,"
                " SUM(CASE WHEN state = 'settled' THEN 1 ELSE 0 END) AS settled_count"
                " FROM ledger"
            ).fetchone()
            halt = self._read_halt(conn)
            return SpendStatus(
                run_id=self._run_id,
                cap_usd=self.cap_usd,
                settled_usd=nano_to_usd(terms["settled"]),
                reserved_usd=nano_to_usd(terms["reserved"]),
                unknown_usd=nano_to_usd(terms["unknown"]),
                leaked_usd=nano_to_usd(terms["leaked"]),
                reclaimed_usd=nano_to_usd(terms["reclaimed"]),
                written_off_usd=nano_to_usd(terms["written_off"]),
                outstanding_usd=nano_to_usd(terms["outstanding"]),
                remaining_usd=nano_to_usd(self._cap_nano - terms["outstanding"]),
                reservations_open=int(counts["open_count"] or 0),
                calls_settled=int(counts["settled_count"] or 0),
                requests=int(counts["requests"] or 0),
                halted=bool(int(halt["halted"])),
                halt_reason=halt["reason"] or "",
                halt_detail=halt["detail"] or "",
                as_of=_now_iso(),
            )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def create_ledger(
    path: Union[str, Path],
    *,
    cap_usd: Union[int, float, Decimal],
    run_id: str,
    meta: Optional[Mapping[str, Any]] = None,
    busy_timeout_ms: int = _DEFAULT_BUSY_TIMEOUT_MS,
    halt_on_overbilled: bool = True,
    synchronous: str = "FULL",
    warn_on_network_path: bool = True,
) -> SpendLedger:
    """Create a ledger file EXCLUSIVELY and write its pinned policy row.

    The file is created with ``open(path, "xb")``, so two processes racing this
    call yield exactly one winner; the loser gets :class:`FileExistsError` and
    calls :func:`open_ledger`. This function NEVER repairs an existing file: a
    half-written ledger is a state to diagnose, not to patch, and a silent
    repair is how a cap gets quietly replaced.

    ``cap_usd`` must be positive and finite, and is immutable thereafter.
    """
    cap_nano = usd_to_nano(cap_usd)
    if cap_nano <= 0:
        raise ValueError(f"cap_usd must be positive, got {cap_usd!r}")
    if not run_id:
        raise ValueError("run_id must be a non-empty string")

    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create. Raises FileExistsError for the loser of a race, and for
    # any pre-existing file -- including one this call would otherwise repair.
    with open(target, "xb"):
        pass

    ledger = SpendLedger(
        target,
        run_id=run_id,
        cap_nano=cap_nano,
        busy_timeout_ms=busy_timeout_ms,
        halt_on_overbilled=halt_on_overbilled,
        synchronous=synchronous,
        warn_on_network_path=warn_on_network_path,
    )
    # The WHOLE schema in one transaction: both tables, the control tables and
    # the declared version. A failure part-way rolls all of it back, so there
    # is no state in which a later open sees half a schema.
    with ledger._connect() as conn:  # noqa: SLF001 -- the owner of this handle
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            conn.execute(
                "INSERT INTO policy (id, run_id, cap_nano, nano_per_usd, created_at, meta) "
                "VALUES (1, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    cap_nano,
                    NANO_PER_USD,
                    _now_iso(),
                    json.dumps(dict(meta or {}), sort_keys=True, ensure_ascii=True),
                ),
            )
            conn.execute(
                "INSERT INTO halt (id, halted, reason, detail, since) "
                "VALUES (1, 0, NULL, NULL, NULL)"
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return ledger


def open_ledger(
    path: Union[str, Path],
    *,
    busy_timeout_ms: int = _DEFAULT_BUSY_TIMEOUT_MS,
    halt_on_overbilled: bool = True,
    synchronous: str = "FULL",
    warn_on_network_path: bool = True,
) -> SpendLedger:
    """Open an existing ledger, pinning its cap and run identity from the file.

    The cap comes from the policy row and NOWHERE else -- never from
    configuration, never from the environment -- which is what makes a cap
    impossible to widen by reopening. Raises :class:`FileNotFoundError` when
    the file is absent, :class:`LedgerStateInvalid` when it is not an
    initialized ledger or declares a schema this build does not speak.
    """
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"no spend ledger at {target}")

    probe = sqlite3.connect(str(target), timeout=busy_timeout_ms / 1000.0)
    try:
        probe.row_factory = sqlite3.Row
        probe.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        try:
            version_row = probe.execute("SELECT version FROM schema_version").fetchone()
            policy_row = probe.execute(
                "SELECT run_id, cap_nano, nano_per_usd FROM policy WHERE id = 1"
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise LedgerStateInvalid(
                f"{target} is not an initialized spend ledger: {exc}"
            ) from exc
        if version_row is None or policy_row is None:
            raise LedgerStateInvalid(f"{target} is not an initialized spend ledger")
        if int(version_row["version"]) != SCHEMA_VERSION:
            raise LedgerStateInvalid(
                f"{target} declares schema version {int(version_row['version'])}, "
                f"this build speaks {SCHEMA_VERSION}"
            )
        if int(policy_row["nano_per_usd"]) != NANO_PER_USD:
            raise LedgerStateInvalid(
                f"{target} stores {int(policy_row['nano_per_usd'])} nano per USD, "
                f"this build speaks {NANO_PER_USD}"
            )
        run_id = str(policy_row["run_id"])
        cap_nano = int(policy_row["cap_nano"])
    finally:
        probe.close()

    return SpendLedger(
        target,
        run_id=run_id,
        cap_nano=cap_nano,
        busy_timeout_ms=busy_timeout_ms,
        halt_on_overbilled=halt_on_overbilled,
        synchronous=synchronous,
        warn_on_network_path=warn_on_network_path,
    )


def spend_ledger_from_env(
    env: Optional[Mapping[str, str]] = None,
    *,
    busy_timeout_ms: int = _DEFAULT_BUSY_TIMEOUT_MS,
) -> Optional[SpendLedger]:
    """Open the ledger named by ``CONTENT_PIPELINE_SPEND_LEDGER``, or ``None``.

    Unset or empty means NO ledger and unchanged behaviour. The caller invokes
    this itself and passes the result on: nothing in the call path reads the
    variable implicitly, so a cap can never materialize mid-run out of an
    inherited environment.
    """
    source = os.environ if env is None else env
    raw = (source.get(LEDGER_PATH_ENV) or "").strip()
    if not raw:
        return None
    return open_ledger(raw, busy_timeout_ms=busy_timeout_ms)
