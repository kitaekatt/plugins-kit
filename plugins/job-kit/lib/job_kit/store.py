"""Durable SQLite ledger for job-kit runs.

The schema is deliberately job-shaped and independent of content-pipeline-kit.
Every public operation opens its own connection. Each connection enables WAL,
``busy_timeout`` and foreign-key enforcement. Attempt seam records are inserts
only; GC may annotate their workspace lifecycle. A job in a terminal state
refuses every later attempt.

Each lifecycle fact also records a common execution event (see ``events.py``)
in the ``events`` table, inside the fact's own transaction: the event exists
if and only if the fact committed.

Durable interrupts (schema 12): an attempt whose contract requested an
interrupt leaves its job ``waiting`` with one append-only interrupt row. The
row's one resolution (answered, rejected or expired) is written once and never
changed, both enforced by the database. An answered interrupt is continued by
re-running the attempt's contract, recorded as a continuation row.

:class:`LedgerReader` is the second, read-only way to open a ledger: it never
migrates, sets no journal mode, and reads a snapshot in one transaction.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterator, Mapping, Optional, Sequence

from . import events as _events
from . import interrupts as _interrupts
from .model import (
    Acceptance,
    Attempt,
    AttemptError,
    AttemptReservation,
    Continuation,
    InterruptRecord,
    InterruptRequest,
    InterruptResolution,
    Job,
    JobRecord,
    JobState,
    RunRecord,
    RunSnapshot,
    RunState,
    TERMINAL_STATES,
    Usage,
    interrupt_lapsed,
    validate_max_parallel,
)


DEFAULT_BUSY_TIMEOUT_MS = 5000
ERROR_LIMIT = 2000
# "quota" is llm-scripting-kit's HALT_QUOTA: a spent subscription pool, which
# persists until the pool resets, so it narrows the run like the other three.
_PERSISTENT_HALT_KINDS = ("auth", "rate_limit", "insufficient_credit", "quota")


class StoreError(Exception):
    """Base class for job-kit ledger errors."""


class StoreNotFoundError(StoreError):
    """The requested ledger file does not exist."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        super().__init__(f"store does not exist: {db_path}")


class UnknownRunError(StoreError):
    """No run with the requested identifier exists."""


class UnknownJobError(StoreError):
    """No job with the requested run and job identifiers exists."""


class DuplicateJobError(StoreError):
    """A run contains a repeated job identifier."""


class TerminalStateError(StoreError):
    """A transition was attempted after a job reached a terminal state."""


class EventsNotRecordedError(StoreError):
    """A run predates the event log, so its stream is not its full history."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(
            f"run {run_id!r} was created before job-kit recorded execution "
            "events, so its event stream would be partial; it is not exported"
        )


class UnknownInterruptError(StoreError):
    """No interrupt with the requested identifier exists in the named run."""


class ResolutionConflictError(StoreError):
    """The interrupt already has a different resolution; nothing was written."""

    def __init__(self, interrupt_id: str, stored_outcome: str) -> None:
        self.interrupt_id = interrupt_id
        self.stored_outcome = stored_outcome
        super().__init__(
            f"interrupt {interrupt_id} is already resolved ({stored_outcome}) "
            "with a different decision; the original resolution is kept"
        )


class InterruptExpiredError(StoreError):
    """The interrupt lapsed before it was resolved; the expiry is recorded."""

    def __init__(self, interrupt_id: str, expires_at: Optional[float]) -> None:
        self.interrupt_id = interrupt_id
        self.expires_at = expires_at
        super().__init__(
            f"interrupt {interrupt_id} expired at {expires_at} before it was "
            "resolved; the expiry is recorded and the job is expired"
        )


class ResolutionInputError(StoreError):
    """The resolution input was refused; nothing was written.

    ``errors`` holds the validator's ``(json_pointer, keyword)`` tuples when
    the input failed the request schema.
    """

    def __init__(self, message: str, errors: tuple = ()) -> None:
        self.errors = tuple(errors)
        super().__init__(message)


#: The lowest ledger schema :class:`LedgerReader` reads: the ledger the
#: published job-kit 0.9.1 writes (its last step adds ``pace_readings_json``).
READER_MIN_SCHEMA = 10

#: The first ledger schema that has the interrupt tables.
INTERRUPT_SCHEMA = 12

#: The decision a resolve call names, and the outcome it records.
_DECISION_OUTCOMES = {"answer": "answered", "reject": "rejected"}


_MIGRATIONS: list[list[str]] = [
    [
        """
        CREATE TABLE schema_version (
            version INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            created_at REAL NOT NULL,
            jobs_path TEXT,
            max_parallel INTEGER NOT NULL,
            workspace_root TEXT
        )
        """,
        """
        CREATE TABLE jobs (
            run_id TEXT NOT NULL REFERENCES runs(id),
            id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            definition_json TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (run_id, id)
        )
        """,
        "CREATE INDEX idx_jobs_run_state ON jobs(run_id, state)",
        """
        CREATE TABLE attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            attempt_no INTEGER NOT NULL,
            endpoint TEXT NOT NULL,
            backend TEXT NOT NULL,
            model TEXT NOT NULL,
            status TEXT NOT NULL,
            error_code TEXT,
            error_message TEXT,
            halt_kind TEXT,
            dropped_params_json TEXT,
            execution_controls_applied_json TEXT,
            started_at TEXT,
            ended_at TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_hit_tokens INTEGER,
            total_tokens INTEGER,
            response_text TEXT,
            workspace TEXT,
            acceptance_json TEXT,
            FOREIGN KEY (run_id, job_id) REFERENCES jobs(run_id, id)
        )
        """,
        "CREATE INDEX idx_attempts_run_job ON attempts(run_id, job_id, id)",
        "INSERT INTO schema_version(version) VALUES (1)",
    ],
    [
        "ALTER TABLE jobs ADD COLUMN error_message TEXT",
    ],
    [
        "ALTER TABLE attempts ADD COLUMN base_ref TEXT",
        "ALTER TABLE attempts ADD COLUMN workspace_status TEXT NOT NULL DEFAULT 'none'",
        "ALTER TABLE attempts ADD COLUMN workspace_reason TEXT",
        "ALTER TABLE attempts ADD COLUMN workspace_removed_at REAL",
    ],
    [
        "ALTER TABLE runs ADD COLUMN workspace_base_refs_json TEXT",
    ],
    [
        "ALTER TABLE attempts ADD COLUMN workspace_removal_forced INTEGER NOT NULL DEFAULT 0",
    ],
    [
        "ALTER TABLE runs ADD COLUMN disallowed_tools TEXT",
    ],
    [
        "ALTER TABLE attempts ADD COLUMN forwarded_params_json TEXT",
    ],
    [
        "ALTER TABLE attempts ADD COLUMN reasoning TEXT",
        "ALTER TABLE attempts ADD COLUMN finish_reason TEXT",
    ],
    [
        """
        CREATE TABLE reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            attempt_no INTEGER NOT NULL,
            budget_no INTEGER NOT NULL,
            endpoint TEXT NOT NULL,
            backend TEXT NOT NULL,
            model TEXT NOT NULL,
            workspace_path TEXT,
            reserved_at TEXT NOT NULL,
            invoke_armed_at TEXT,
            disposition TEXT,
            resolved_at TEXT,
            lost_at TEXT,
            loss_reason TEXT,
            workspace_status TEXT NOT NULL DEFAULT 'none',
            workspace_reason TEXT,
            workspace_removed_at REAL,
            workspace_removal_forced INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (run_id, job_id) REFERENCES jobs(run_id, id),
            UNIQUE (run_id, job_id, attempt_no)
        )
        """,
        "CREATE INDEX idx_reservations_run_job ON reservations(run_id, job_id, id)",
        "CREATE UNIQUE INDEX idx_attempts_run_job_no ON attempts(run_id, job_id, attempt_no)",
    ],
    [
        "ALTER TABLE attempts ADD COLUMN pace_readings_json TEXT",
    ],
    [
        # Execution events: one row per ledger fact, inserted in the fact's
        # transaction. The AUTOINCREMENT seq is the event's order: writers
        # are serialized by BEGIN IMMEDIATE, so seq is commit order across
        # transactions and insertion order within one, run-wide.
        """
        CREATE TABLE events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id),
            job_id TEXT,
            attempt_no INTEGER,
            event TEXT NOT NULL,
            at TEXT NOT NULL,
            adapter TEXT,
            model TEXT,
            payload_json TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_events_run_seq ON events(run_id, seq)",
        # A run created before this step has no events for its earlier facts;
        # 0 marks it so its partial stream is never exported as its history.
        "ALTER TABLE runs ADD COLUMN events_recorded INTEGER NOT NULL DEFAULT 0",
    ],
    [
        # Durable interrupts. Additive: every earlier table and row is kept.
        """
        CREATE TABLE interrupts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            attempt_no INTEGER NOT NULL,
            continuation_no INTEGER NOT NULL,
            envelope TEXT NOT NULL,
            kind TEXT NOT NULL,
            request_schema_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL,
            FOREIGN KEY (run_id, job_id) REFERENCES jobs(run_id, id),
            FOREIGN KEY (run_id, job_id, attempt_no)
                REFERENCES attempts(run_id, job_id, attempt_no),
            UNIQUE (run_id, job_id, attempt_no, continuation_no)
        )
        """,
        "CREATE INDEX idx_interrupts_run_job ON interrupts(run_id, job_id, id)",
        """
        CREATE TABLE interrupt_resolutions (
            interrupt_id INTEGER PRIMARY KEY REFERENCES interrupts(id),
            outcome TEXT NOT NULL
                CHECK (outcome IN ('answered', 'rejected', 'expired')),
            input_json TEXT,
            reason TEXT,
            resolved_at REAL NOT NULL
        )
        """,
        """
        CREATE TABLE continuations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            attempt_no INTEGER NOT NULL,
            continuation_no INTEGER NOT NULL,
            interrupt_id INTEGER NOT NULL REFERENCES interrupts(id),
            started_at REAL NOT NULL,
            ended_at REAL,
            disposition TEXT,
            acceptance_json TEXT,
            FOREIGN KEY (run_id, job_id, attempt_no)
                REFERENCES attempts(run_id, job_id, attempt_no),
            UNIQUE (run_id, job_id, attempt_no, continuation_no)
        )
        """,
        "CREATE INDEX idx_continuations_run_job ON continuations(run_id, job_id, id)",
        # At most one unresolved interrupt per job, whatever writes the row.
        """
        CREATE TRIGGER interrupts_one_open_per_job BEFORE INSERT ON interrupts
            WHEN EXISTS (
                SELECT 1 FROM interrupts AS open
                WHERE open.run_id = NEW.run_id AND open.job_id = NEW.job_id
                  AND NOT EXISTS (
                      SELECT 1 FROM interrupt_resolutions AS r
                      WHERE r.interrupt_id = open.id))
            BEGIN SELECT RAISE(ABORT, 'job already has an unresolved interrupt'); END
        """,
        # Immutability, enforced by the database rather than by convention.
        """
        CREATE TRIGGER interrupts_immutable_update BEFORE UPDATE ON interrupts
            BEGIN SELECT RAISE(ABORT, 'interrupt records are immutable'); END
        """,
        """
        CREATE TRIGGER interrupts_immutable_delete BEFORE DELETE ON interrupts
            BEGIN SELECT RAISE(ABORT, 'interrupt records are immutable'); END
        """,
        """
        CREATE TRIGGER resolutions_immutable_update BEFORE UPDATE ON interrupt_resolutions
            BEGIN SELECT RAISE(ABORT, 'interrupt resolutions are immutable'); END
        """,
        """
        CREATE TRIGGER resolutions_immutable_delete BEFORE DELETE ON interrupt_resolutions
            BEGIN SELECT RAISE(ABORT, 'interrupt resolutions are immutable'); END
        """,
        # The revision each event row was built under; NULL (rows written
        # before this step) renders as v1.
        "ALTER TABLE events ADD COLUMN schema TEXT",
    ],
]


def _json_or_none(value: object) -> Optional[str]:
    """Serialize a nullable value, preserving ``None`` as SQL NULL."""
    if value is None:
        return None
    return json.dumps(value, sort_keys=True)


def _load_json(value: Optional[str]) -> object:
    """Decode a JSON column and fail loudly when the ledger is corrupt."""
    if value is None:
        return None
    return json.loads(value)


def _optional_tuple(value: Optional[str]) -> Optional[tuple[str, ...]]:
    """Decode a nullable JSON list without turning NULL into an empty list."""
    if value is None:
        return None
    raw = _load_json(value)
    if not isinstance(raw, list):
        raise StoreError("ledger sequence column is not a JSON list")
    return tuple(str(item) for item in raw)


def _acceptance_from_json(value: Optional[str]) -> Optional[Acceptance]:
    """Rebuild an acceptance record from its JSON column."""
    if value is None:
        return None
    raw = _load_json(value)
    if not isinstance(raw, Mapping):
        raise StoreError("ledger acceptance column is not a JSON mapping")
    command = raw.get("command")
    if not isinstance(command, list):
        raise StoreError("ledger acceptance command is not a JSON list")
    directory = raw.get("directory")
    if not isinstance(directory, str):
        raise StoreError("ledger acceptance directory is missing")
    return Acceptance(
        command=tuple(str(part) for part in command),
        directory=Path(directory),
        exit_code=(int(raw["exit_code"]) if raw.get("exit_code") is not None else None),
        stdout=str(raw.get("stdout", "")),
        stderr=str(raw.get("stderr", "")),
        wall_ms=int(raw.get("wall_ms", 0)),
        accepted=bool(raw.get("accepted", False)),
        outcome=str(raw.get("outcome", "observed")),
    )


def _pace_readings_from_json(
    value: Optional[str],
) -> Optional[tuple[Mapping[str, object], ...]]:
    """Rebuild the logged pace readings; NULL on rows that predate them."""
    if value is None:
        return None
    raw = _load_json(value)
    if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
        raise StoreError("ledger pace readings column is not a JSON list of mappings")
    return tuple(dict(item) for item in raw)


def _row_to_job(row: sqlite3.Row) -> JobRecord:
    """Convert a jobs row into its public record."""
    raw = _load_json(row["definition_json"])
    if not isinstance(raw, Mapping):
        raise StoreError("ledger job definition is not a JSON mapping")
    job = Job.from_mapping(raw)
    return JobRecord(
        job=job,
        state=JobState(row["state"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        error=(str(row["error_message"]) if row["error_message"] is not None else None),
    )


def _row_to_attempt(row: sqlite3.Row) -> Attempt:
    """Convert an attempts row into its public record."""
    error = None
    if row["error_code"] is not None:
        error = AttemptError(
            code=str(row["error_code"]),
            message=str(row["error_message"] or ""),
        )
    usage = None
    usage_values = (
        row["input_tokens"],
        row["output_tokens"],
        row["cache_hit_tokens"],
        row["total_tokens"],
    )
    if any(value is not None for value in usage_values):
        usage = Usage(
            input_tokens=(int(row["input_tokens"]) if row["input_tokens"] is not None else None),
            output_tokens=(int(row["output_tokens"]) if row["output_tokens"] is not None else None),
            cache_hit_tokens=(
                int(row["cache_hit_tokens"])
                if row["cache_hit_tokens"] is not None
                else None
            ),
            total_tokens=(int(row["total_tokens"]) if row["total_tokens"] is not None else None),
        )
    workspace = row["workspace"]
    return Attempt(
        id=int(row["id"]),
        run_id=str(row["run_id"]),
        job_id=str(row["job_id"]),
        attempt_no=int(row["attempt_no"]),
        endpoint=str(row["endpoint"]),
        backend=str(row["backend"]),
        model=str(row["model"]),
        status=str(row["status"]),
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        error=error,
        halt_kind=row["halt_kind"],
        dropped_params=_optional_tuple(row["dropped_params_json"]),
        forwarded_params=_optional_tuple(row["forwarded_params_json"]),
        execution_controls_applied=_optional_tuple(
            row["execution_controls_applied_json"]
        ),
        usage=usage,
        response_text=row["response_text"],
        reasoning=(str(row["reasoning"]) if row["reasoning"] is not None else None),
        finish_reason=(
            str(row["finish_reason"])
            if row["finish_reason"] is not None
            else None
        ),
        workspace=Path(workspace) if workspace is not None else None,
        base_ref=(str(row["base_ref"]) if row["base_ref"] is not None else None),
        workspace_status=str(row["workspace_status"] or "none"),
        workspace_reason=(
            str(row["workspace_reason"])
            if row["workspace_reason"] is not None
            else None
        ),
        workspace_removed_at=(
            float(row["workspace_removed_at"])
            if row["workspace_removed_at"] is not None
            else None
        ),
        workspace_removal_forced=bool(row["workspace_removal_forced"]),
        acceptance=_acceptance_from_json(row["acceptance_json"]),
        pace_readings=_pace_readings_from_json(row["pace_readings_json"]),
    )


def _row_to_reservation(row: sqlite3.Row) -> AttemptReservation:
    """Convert a reservations row into its public record."""
    workspace_path = row["workspace_path"]
    return AttemptReservation(
        id=int(row["id"]),
        run_id=str(row["run_id"]),
        job_id=str(row["job_id"]),
        attempt_no=int(row["attempt_no"]),
        budget_no=int(row["budget_no"]),
        endpoint=str(row["endpoint"]),
        backend=str(row["backend"]),
        model=str(row["model"]),
        workspace_path=(Path(workspace_path) if workspace_path is not None else None),
        reserved_at=str(row["reserved_at"]),
        invoke_armed_at=(
            str(row["invoke_armed_at"])
            if row["invoke_armed_at"] is not None
            else None
        ),
        disposition=(
            str(row["disposition"]) if row["disposition"] is not None else None
        ),
        resolved_at=(
            str(row["resolved_at"]) if row["resolved_at"] is not None else None
        ),
        lost_at=(str(row["lost_at"]) if row["lost_at"] is not None else None),
        loss_reason=(
            str(row["loss_reason"]) if row["loss_reason"] is not None else None
        ),
        workspace_status=str(row["workspace_status"] or "none"),
        workspace_reason=(
            str(row["workspace_reason"])
            if row["workspace_reason"] is not None
            else None
        ),
        workspace_removed_at=(
            float(row["workspace_removed_at"])
            if row["workspace_removed_at"] is not None
            else None
        ),
        workspace_removal_forced=bool(row["workspace_removal_forced"]),
    )


def _row_to_resolution(row: sqlite3.Row) -> InterruptResolution:
    """Convert an interrupt_resolutions row into its public record."""
    return InterruptResolution(
        interrupt_id=str(row["interrupt_id"]),
        outcome=str(row["outcome"]),
        resolved_at=float(row["resolved_at"]),
        input=_load_json(row["input_json"]),
        reason=(str(row["reason"]) if row["reason"] is not None else None),
    )


def _row_to_interrupt(
    row: sqlite3.Row, resolution: Optional[InterruptResolution] = None
) -> InterruptRecord:
    """Convert an interrupts row (and its resolution) into its public record."""
    request_schema = _load_json(row["request_schema_json"])
    payload = _load_json(row["payload_json"])
    if not isinstance(request_schema, Mapping) or not isinstance(payload, Mapping):
        raise StoreError("ledger interrupt schema or payload is not a JSON mapping")
    return InterruptRecord(
        id=str(row["id"]),
        run_id=str(row["run_id"]),
        job_id=str(row["job_id"]),
        attempt_no=int(row["attempt_no"]),
        continuation_no=int(row["continuation_no"]),
        envelope=str(row["envelope"]),
        kind=str(row["kind"]),
        request_schema=dict(request_schema),
        payload=dict(payload),
        created_at=float(row["created_at"]),
        expires_at=(float(row["expires_at"]) if row["expires_at"] is not None else None),
        resolution=resolution,
    )


def _row_to_continuation(row: sqlite3.Row) -> Continuation:
    """Convert a continuations row into its public record."""
    return Continuation(
        id=int(row["id"]),
        run_id=str(row["run_id"]),
        job_id=str(row["job_id"]),
        attempt_no=int(row["attempt_no"]),
        continuation_no=int(row["continuation_no"]),
        interrupt_id=str(row["interrupt_id"]),
        started_at=float(row["started_at"]),
        ended_at=(float(row["ended_at"]) if row["ended_at"] is not None else None),
        disposition=(
            str(row["disposition"]) if row["disposition"] is not None else None
        ),
        acceptance=_acceptance_from_json(row["acceptance_json"]),
    )


def _read_interrupts(
    conn: sqlite3.Connection, run_id: str, job_id: Optional[str] = None
) -> list[InterruptRecord]:
    """Read a run's (or one job's) interrupts with their resolutions, in id order."""
    query = (
        "SELECT i.*, r.interrupt_id AS r_interrupt_id, r.outcome AS r_outcome, "
        "r.input_json AS r_input_json, r.reason AS r_reason, "
        "r.resolved_at AS r_resolved_at "
        "FROM interrupts AS i LEFT JOIN interrupt_resolutions AS r "
        "ON r.interrupt_id = i.id WHERE i.run_id = ?"
    )
    parameters: tuple = (run_id,)
    if job_id is not None:
        query += " AND i.job_id = ?"
        parameters += (job_id,)
    rows = conn.execute(query + " ORDER BY i.id", parameters).fetchall()
    records = []
    for row in rows:
        resolution = None
        if row["r_interrupt_id"] is not None:
            resolution = InterruptResolution(
                interrupt_id=str(row["r_interrupt_id"]),
                outcome=str(row["r_outcome"]),
                resolved_at=float(row["r_resolved_at"]),
                input=_load_json(row["r_input_json"]),
                reason=(str(row["r_reason"]) if row["r_reason"] is not None else None),
            )
        records.append(_row_to_interrupt(row, resolution))
    return records


def _read_continuations(
    conn: sqlite3.Connection, run_id: str, job_id: Optional[str] = None
) -> list[Continuation]:
    """Read a run's (or one job's) continuations in id order."""
    if job_id is None:
        rows = conn.execute(
            "SELECT * FROM continuations WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM continuations WHERE run_id = ? AND job_id = ? ORDER BY id",
            (run_id, job_id),
        ).fetchall()
    return [_row_to_continuation(row) for row in rows]


def _derive_run_state(job_rows: Sequence[sqlite3.Row]) -> RunState:
    """Derive run state from job states rather than duplicating state.

    All terminal -> completed; any running -> running; any pending ->
    pending; otherwise (only waiting and terminal jobs) -> waiting.
    """
    if not job_rows:
        return RunState.COMPLETED
    states = [JobState(row["state"]) for row in job_rows]
    if all(state in TERMINAL_STATES for state in states):
        return RunState.COMPLETED
    if any(state is JobState.RUNNING for state in states):
        return RunState.RUNNING
    if any(state is JobState.PENDING for state in states):
        return RunState.PENDING
    return RunState.WAITING


def _future_schema_message(stored_version: int) -> str:
    """The refusal for a ledger written by a newer job-kit."""
    return (
        f"database schema version {stored_version} is newer than this "
        f"job-kit supports (max {len(_MIGRATIONS)}); refusing to open "
        "it -- update job-kit, or point at a database this version "
        "understands"
    )


def _read_snapshot(
    conn: sqlite3.Connection, run_id: str, *, version: int, now: float
) -> RunSnapshot:
    """Read one run inside the caller's read transaction.

    ``version`` is the ledger schema, read in the same transaction: below
    :data:`INTERRUPT_SCHEMA` the interrupt tables do not exist and are not
    queried. Shared by :meth:`JobStore.snapshot` and
    :meth:`LedgerReader.snapshot`.
    """
    run_row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run_row is None:
        raise UnknownRunError(run_id)
    job_rows = conn.execute(
        "SELECT * FROM jobs WHERE run_id = ? ORDER BY ordinal", (run_id,)
    ).fetchall()
    attempt_rows = conn.execute(
        "SELECT * FROM attempts WHERE run_id = ? ORDER BY id", (run_id,)
    ).fetchall()
    reservation_rows = conn.execute(
        "SELECT * FROM reservations WHERE run_id = ? ORDER BY id", (run_id,)
    ).fetchall()
    interrupts: tuple[InterruptRecord, ...] = ()
    continuations: tuple[Continuation, ...] = ()
    if version >= INTERRUPT_SCHEMA:
        interrupts = tuple(_read_interrupts(conn, run_id))
        continuations = tuple(_read_continuations(conn, run_id))
    return RunSnapshot(
        run=JobStore._run_record(run_row, job_rows),
        jobs=tuple(_row_to_job(row) for row in job_rows),
        attempts=tuple(_row_to_attempt(row) for row in attempt_rows),
        reservations=tuple(_row_to_reservation(row) for row in reservation_rows),
        interrupts=interrupts,
        continuations=continuations,
        read_at=now,
    )


class JobStore:
    """A durable job-shaped SQLite ledger."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        create: bool = True,
    ) -> None:
        if str(db_path) == ":memory:":
            raise ValueError(
                "JobStore requires a filesystem path because each verb uses a "
                "separate SQLite connection"
            )
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self.db_path = Path(db_path).expanduser()
        self.busy_timeout_ms = int(busy_timeout_ms)
        if create:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        elif not self.db_path.is_file():
            raise StoreNotFoundError(self.db_path)
        self._migrate()

    def scale_busy_timeout(self, max_parallel: int) -> int:
        """Raise the per-connection busy timeout to cover a wider worker pool.

        Every write verb takes the write lock upfront (``BEGIN IMMEDIATE``) and
        holds it for one short statement group, so ``max_parallel`` writers
        queue rather than collide. The wait a writer can face is therefore
        linear in the pool width, and the default budget is scaled by it so a
        wide pool cannot exhaust a bound sized for one worker. The timeout is
        only ever raised, never lowered below an explicit caller value.
        """
        bound = validate_max_parallel(max_parallel)
        scaled = DEFAULT_BUSY_TIMEOUT_MS * bound
        if scaled > self.busy_timeout_ms:
            self.busy_timeout_ms = scaled
        return self.busy_timeout_ms

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a connection and apply all per-connection pragmas."""
        conn = sqlite3.connect(
            str(self.db_path), timeout=self.busy_timeout_ms / 1000.0
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            self._ensure_wal(conn)
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    def _ensure_wal(self, conn: sqlite3.Connection) -> None:
        """Set WAL mode, retrying the one pragma that ignores busy timeout."""
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if isinstance(mode, str) and mode.lower() == "wal":
            return
        deadline = time.monotonic() + self.busy_timeout_ms / 1000.0
        while True:
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.005)

    @contextmanager
    def _writer(self) -> Iterator[sqlite3.Connection]:
        """Run one write verb inside a transaction with an upfront lock."""
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
        """Expose one consistent read snapshot for status operations."""
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
        """Read schema version without taking a write lock."""
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        if table is None:
            return 0
        row = conn.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
        return int(row["version"]) if row["version"] is not None else 0

    def _migrate(self) -> None:
        """Apply all pending schema steps atomically."""
        # Read the stored version through a connection that changes nothing --
        # no pragma, no row -- so a database from a NEWER job-kit is refused
        # before anything is written, not merely before the migration loop
        # (which runs inside _connect(), which itself writes pragmas).
        probe = sqlite3.connect(
            str(self.db_path), timeout=self.busy_timeout_ms / 1000.0
        )
        try:
            probe.row_factory = sqlite3.Row
            stored_version = self._read_schema_version(probe)
        finally:
            probe.close()
        if stored_version > len(_MIGRATIONS):
            raise StoreError(_future_schema_message(stored_version))
        with self._connect() as conn:
            if self._read_schema_version(conn) >= len(_MIGRATIONS):
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                version = self._read_schema_version(conn)
                for index in range(version, len(_MIGRATIONS)):
                    for statement in _MIGRATIONS[index]:
                        conn.execute(statement)
                    new_version = index + 1
                    if index == 0:
                        continue
                    conn.execute(
                        "UPDATE schema_version SET version = ?", (new_version,)
                    )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _require_run(self, conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        """Return a run row or raise a typed store error."""
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise UnknownRunError(run_id)
        return row

    def _require_job(
        self, conn: sqlite3.Connection, run_id: str, job_id: str
    ) -> sqlite3.Row:
        """Return a job row or raise a typed store error."""
        row = conn.execute(
            "SELECT * FROM jobs WHERE run_id = ? AND id = ?", (run_id, job_id)
        ).fetchone()
        if row is None:
            raise UnknownJobError(f"{run_id!r}/{job_id!r}")
        return row

    @staticmethod
    def _record_event(
        conn: sqlite3.Connection,
        *,
        run_id: str,
        event: str,
        at: str,
        job_id: Optional[str] = None,
        attempt_no: Optional[int] = None,
        adapter: Optional[str] = None,
        model: Optional[str] = None,
        payload: Optional[Mapping[str, object]] = None,
        schema: Optional[str] = None,
    ) -> int:
        """Validate one execution event and insert it on the fact's connection.

        The caller is inside a ``_writer()`` transaction, so the event commits
        or rolls back with its fact. The envelope is validated before the
        insert with a placeholder ``seq``; the row's AUTOINCREMENT value is
        the authoritative ``seq``. ``schema`` selects the revision (``None``
        is v1) and is stored with the row. Returns the ``seq``.
        """
        rendered = _events.build_event(
            seq=0,
            run_id=run_id,
            event=event,
            at=at,
            job_id=job_id,
            attempt_no=attempt_no,
            adapter=adapter,
            model=model,
            payload=payload,
            schema=schema,
        )
        source = rendered["source"]
        cursor = conn.execute(
            "INSERT INTO events(run_id, job_id, attempt_no, event, at, adapter, "
            "model, payload_json, schema) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                job_id,
                attempt_no,
                rendered["event"],
                rendered["at"],
                source.get("adapter"),
                source.get("model"),
                json.dumps(rendered["payload"], sort_keys=True),
                rendered["schema"],
            ),
        )
        return int(cursor.lastrowid)

    def _record_interrupt_event(
        self,
        conn: sqlite3.Connection,
        interrupt_row: sqlite3.Row,
        *,
        phase: str,
        at: str,
    ) -> int:
        """Record one v2 ``interrupt`` event for an interrupt row.

        The identity is the owning attempt and the source its backend and
        model. The payload holds identifiers, the kind, the phase, the
        expiry and the continuation number only: never the request payload,
        the request schema, the resolution input, or free text.
        """
        attempt = conn.execute(
            "SELECT backend, model FROM attempts WHERE run_id = ? AND job_id = ? "
            "AND attempt_no = ?",
            (interrupt_row["run_id"], interrupt_row["job_id"], interrupt_row["attempt_no"]),
        ).fetchone()
        payload: dict[str, object] = {
            "interrupt_id": str(interrupt_row["id"]),
            "kind": str(interrupt_row["kind"]),
            "phase": phase,
            "continuation_no": int(interrupt_row["continuation_no"]),
        }
        if interrupt_row["expires_at"] is not None:
            payload["expires_at"] = _events.event_at(float(interrupt_row["expires_at"]))
        return self._record_event(
            conn,
            run_id=str(interrupt_row["run_id"]),
            event="interrupt",
            at=at,
            job_id=str(interrupt_row["job_id"]),
            attempt_no=int(interrupt_row["attempt_no"]),
            adapter=(str(attempt["backend"]) if attempt is not None else None),
            model=(str(attempt["model"]) if attempt is not None else None),
            payload=payload,
            schema=_events.SCHEMA_V2,
        )

    @staticmethod
    def _run_record(
        row: sqlite3.Row, job_rows: Sequence[sqlite3.Row]
    ) -> RunRecord:
        """Convert a run row and its state rows into a public record."""
        raw_base_refs = _load_json(row["workspace_base_refs_json"])
        if raw_base_refs is None:
            base_refs: Mapping[str, str] = {}
        elif isinstance(raw_base_refs, Mapping):
            base_refs = {
                str(job_id): str(base_ref)
                for job_id, base_ref in raw_base_refs.items()
            }
        else:
            raise StoreError("ledger workspace base refs are not a JSON mapping")
        return RunRecord(
            id=str(row["id"]),
            created_at=float(row["created_at"]),
            jobs_path=Path(row["jobs_path"]) if row["jobs_path"] else None,
            max_parallel=int(row["max_parallel"]),
            workspace_root=(Path(row["workspace_root"]) if row["workspace_root"] else None),
            status=_derive_run_state(job_rows),
            workspace_base_refs=base_refs,
            disallowed_tools=(
                str(row["disallowed_tools"])
                if row["disallowed_tools"] is not None
                else None
            ),
        )

    def create_run(
        self,
        run_id: str,
        jobs: Sequence[Job],
        *,
        jobs_path: Optional[str | Path] = None,
        max_parallel: int = 1,
        workspace_root: Optional[str | Path] = None,
        workspace_base_refs: Optional[Mapping[str, str]] = None,
        disallowed_tools: Optional[str] = None,
        created_at: Optional[float] = None,
    ) -> RunRecord:
        """Create a run and register all job definitions in one transaction."""
        bound = validate_max_parallel(max_parallel)
        if not run_id.strip():
            raise ValueError("run_id must not be empty")
        if disallowed_tools is not None and not isinstance(disallowed_tools, str):
            raise ValueError("run disallowed_tools must be a string or null")
        ids = [job.id for job in jobs]
        if len(ids) != len(set(ids)):
            raise DuplicateJobError("job ids must be unique within a run")
        when = time.time() if created_at is None else created_at
        path = Path(jobs_path).expanduser().resolve() if jobs_path is not None else None
        root = (
            Path(workspace_root).expanduser().resolve()
            if workspace_root is not None
            else None
        )
        if workspace_base_refs is None:
            from .workspace import capture_base_refs

            base_refs = capture_base_refs(jobs)
        else:
            base_refs = {
                str(job_id): str(base_ref)
                for job_id, base_ref in workspace_base_refs.items()
            }
        # Every job id becomes an event unit id; refuse one the envelope
        # cannot carry now, before anything is written, not mid-run.
        for job_id in ids:
            _events.check_unit_identity(run_id, job_id)
        with self._writer() as conn:
            conn.execute(
                "INSERT INTO runs(id, created_at, jobs_path, max_parallel, workspace_root, "
                "workspace_base_refs_json, disallowed_tools, events_recorded) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    run_id,
                    when,
                    str(path) if path is not None else None,
                    bound,
                    str(root) if root is not None else None,
                    _json_or_none(base_refs),
                    disallowed_tools,
                ),
            )
            conn.executemany(
                "INSERT INTO jobs(run_id, id, ordinal, definition_json, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        job.id,
                        ordinal,
                        json.dumps(job.to_mapping(), sort_keys=True),
                        JobState.PENDING.value,
                        when,
                        when,
                    )
                    for ordinal, job in enumerate(jobs)
                ],
            )
            self._record_event(
                conn,
                run_id=run_id,
                event=f"{_events.PLUGIN}:run-created",
                at=_events.event_at(when),
                payload={"max_parallel": bound, "job_count": len(ids)},
            )
        record = self.get_run(run_id)
        if record is None:  # pragma: no cover - the insert is in the same store
            raise UnknownRunError(run_id)
        return record

    def get_run(self, run_id: str) -> Optional[RunRecord]:
        """Read a run header and derive its state from jobs."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            jobs = conn.execute(
                "SELECT state FROM jobs WHERE run_id = ? ORDER BY ordinal", (run_id,)
            ).fetchall()
        return self._run_record(row, jobs)

    def ensure_workspace_root(
        self, run_id: str, workspace_root: str | Path
    ) -> RunRecord:
        """Bind a run to one workspace root without changing an existing binding."""
        root = Path(workspace_root).expanduser().resolve()
        with self._writer() as conn:
            row = self._require_run(conn, run_id)
            existing = row["workspace_root"]
            if existing is not None and Path(existing).expanduser().resolve() != root:
                raise StoreError(
                    f"run {run_id!r} already uses workspace root {existing}"
                )
            if existing is None:
                conn.execute(
                    "UPDATE runs SET workspace_root = ? WHERE id = ?",
                    (str(root), run_id),
                )
        record = self.get_run(run_id)
        if record is None:  # pragma: no cover - protected by the transaction
            raise UnknownRunError(run_id)
        return record

    def list_jobs(self, run_id: str) -> list[JobRecord]:
        """List jobs in declaration order."""
        with self._connect() as conn:
            self._require_run(conn, run_id)
            rows = conn.execute(
                "SELECT * FROM jobs WHERE run_id = ? ORDER BY ordinal", (run_id,)
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def get_job(self, run_id: str, job_id: str) -> Optional[JobRecord]:
        """Read one job, returning ``None`` when it is absent."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE run_id = ? AND id = ?", (run_id, job_id)
            ).fetchone()
        return _row_to_job(row) if row is not None else None

    def _transition(
        self,
        run_id: str,
        job_id: str,
        target: JobState,
        reason: Optional[str],
        *,
        at: Optional[float],
        require_attempt: bool = False,
    ) -> JobRecord:
        """Move a non-terminal job to ``target``, writing its error message.

        ``reason`` of ``None`` clears the error message to NULL; any other
        value is truncated to ``ERROR_LIMIT`` and stored. When
        ``require_attempt`` is set, the job must already have a recorded
        attempt row, checked inside the same transaction before the update.
        """
        when = time.time() if at is None else at
        error_message = None if reason is None else str(reason)[:ERROR_LIMIT]
        with self._writer() as conn:
            self._require_run(conn, run_id)
            row = self._require_job(conn, run_id, job_id)
            state = JobState(row["state"])
            if state in TERMINAL_STATES:
                raise TerminalStateError(f"{run_id!r}/{job_id!r} is already {state.value}")
            if require_attempt:
                attempt = conn.execute(
                    "SELECT 1 FROM attempts WHERE run_id = ? AND job_id = ? LIMIT 1",
                    (run_id, job_id),
                ).fetchone()
                if attempt is None:
                    raise ValueError("halted jobs must be marked with an attempt")
            conn.execute(
                "UPDATE jobs SET state = ?, error_message = ?, updated_at = ? "
                "WHERE run_id = ? AND id = ?",
                (target.value, error_message, when, run_id, job_id),
            )
            if target in TERMINAL_STATES:
                payload: dict[str, object] = {"state": target.value}
                if error_message is not None:
                    payload["reason"] = _events.reason_text(error_message)
                self._record_event(
                    conn,
                    run_id=run_id,
                    event="terminal",
                    at=_events.event_at(when),
                    job_id=job_id,
                    payload=payload,
                )
            updated = conn.execute(
                "SELECT * FROM jobs WHERE run_id = ? AND id = ?", (run_id, job_id)
            ).fetchone()
        if updated is None:  # pragma: no cover - protected by the transaction
            raise UnknownJobError(f"{run_id!r}/{job_id!r}")
        return _row_to_job(updated)

    def mark_running(self, run_id: str, job_id: str, *, at: Optional[float] = None) -> JobRecord:
        """Mark a non-terminal job as running before its seam invocation."""
        return self._transition(run_id, job_id, JobState.RUNNING, None, at=at)

    def mark_unroutable(
        self,
        run_id: str,
        job_id: str,
        reason: str,
        *,
        at: Optional[float] = None,
    ) -> JobRecord:
        """Terminalize a job whose seam invocation could not be selected."""
        return self._transition(run_id, job_id, JobState.UNROUTABLE, reason, at=at)

    def mark_halted(
        self,
        run_id: str,
        job_id: str,
        reason: str,
        *,
        at: Optional[float] = None,
    ) -> JobRecord:
        """Terminalize a job whose prior attempts exhausted endpoint eligibility."""
        return self._transition(
            run_id, job_id, JobState.HALTED, reason, at=at, require_attempt=True
        )

    def mark_failed(
        self,
        run_id: str,
        job_id: str,
        reason: str,
        *,
        at: Optional[float] = None,
    ) -> JobRecord:
        """Terminalize a job failure that happened before seam invocation."""
        return self._transition(run_id, job_id, JobState.FAILED, reason, at=at)

    @staticmethod
    def _reservation_budget_count(
        conn: sqlite3.Connection, run_id: str, job_id: str
    ) -> int:
        """Count observed invocations and armed process losses for one job."""
        attempt_count = conn.execute(
            "SELECT COUNT(*) AS count FROM attempts WHERE run_id = ? AND job_id = ?",
            (run_id, job_id),
        ).fetchone()
        loss_count = conn.execute(
            """
            SELECT COUNT(*) AS count FROM reservations
            WHERE run_id = ? AND job_id = ?
              AND disposition = 'process_lost'
              AND invoke_armed_at IS NOT NULL
            """,
            (run_id, job_id),
        ).fetchone()
        return int(attempt_count["count"]) + int(loss_count["count"])

    @staticmethod
    def _next_reservation_attempt_no(
        conn: sqlite3.Connection, run_id: str, job_id: str
    ) -> int:
        """Return a free positive number, preserving gaps occupied by losses."""
        row = conn.execute(
            """
            SELECT MAX(attempt_no) AS attempt_no FROM (
                SELECT attempt_no FROM attempts WHERE run_id = ? AND job_id = ?
                UNION ALL
                SELECT attempt_no FROM reservations WHERE run_id = ? AND job_id = ?
            )
            """,
            (run_id, job_id, run_id, job_id),
        ).fetchone()
        return int(row["attempt_no"] or 0) + 1

    def reserve_attempt(
        self,
        run_id: str,
        job_id: str,
        *,
        endpoint: str,
        backend: str,
        model: str,
        reserved_at: str,
        workspace_path: Optional[str | Path] = None,
    ) -> AttemptReservation:
        """Reserve one seam invocation and mark its job running atomically."""
        path = Path(workspace_path).expanduser().resolve() if workspace_path is not None else None
        with self._writer() as conn:
            self._require_run(conn, run_id)
            job_row = self._require_job(conn, run_id, job_id)
            state = JobState(job_row["state"])
            if state in TERMINAL_STATES:
                raise TerminalStateError(f"{run_id!r}/{job_id!r} is already {state.value}")
            if state is not JobState.PENDING:
                raise StoreError(
                    f"{run_id!r}/{job_id!r} cannot reserve from {state.value}"
                )
            budget_no = self._reservation_budget_count(conn, run_id, job_id) + 1
            attempt_no = self._next_reservation_attempt_no(conn, run_id, job_id)
            conn.execute(
                """
                INSERT INTO reservations(
                    run_id, job_id, attempt_no, budget_no, endpoint, backend, model,
                    workspace_path, reserved_at, workspace_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    job_id,
                    attempt_no,
                    budget_no,
                    endpoint,
                    backend,
                    model,
                    str(path) if path is not None else None,
                    str(reserved_at),
                    "isolated" if path is not None else "none",
                ),
            )
            conn.execute(
                "UPDATE jobs SET state = ?, error_message = NULL, updated_at = ? "
                "WHERE run_id = ? AND id = ?",
                (JobState.RUNNING.value, time.time(), run_id, job_id),
            )
            self._record_event(
                conn,
                run_id=run_id,
                event="dispatch-selected",
                at=_events.event_at(reserved_at),
                job_id=job_id,
                attempt_no=attempt_no,
                adapter=backend,
                model=model,
                payload={"endpoint": endpoint, "budget_no": budget_no},
            )
            row = conn.execute(
                "SELECT * FROM reservations WHERE run_id = ? AND job_id = ? "
                "AND attempt_no = ?",
                (run_id, job_id, attempt_no),
            ).fetchone()
        if row is None:  # pragma: no cover - protected by the transaction
            raise StoreError("reservation was not persisted")
        return _row_to_reservation(row)

    def get_reservation(
        self, run_id: str, job_id: str, attempt_no: int
    ) -> Optional[AttemptReservation]:
        """Read one reservation by its run, job and allocated number."""
        with self._connect() as conn:
            self._require_run(conn, run_id)
            row = conn.execute(
                "SELECT * FROM reservations WHERE run_id = ? AND job_id = ? "
                "AND attempt_no = ?",
                (run_id, job_id, attempt_no),
            ).fetchone()
        return _row_to_reservation(row) if row is not None else None

    def list_reservations(
        self, run_id: str, job_id: Optional[str] = None
    ) -> list[AttemptReservation]:
        """Read reservations in durable insertion order."""
        with self._connect() as conn:
            self._require_run(conn, run_id)
            if job_id is None:
                rows = conn.execute(
                    "SELECT * FROM reservations WHERE run_id = ? ORDER BY id",
                    (run_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM reservations WHERE run_id = ? AND job_id = ? "
                    "ORDER BY id",
                    (run_id, job_id),
                ).fetchall()
        return [_row_to_reservation(row) for row in rows]

    def record_reservation_workspace(
        self,
        run_id: str,
        job_id: str,
        attempt_no: int,
        *,
        workspace_path: Optional[str | Path],
        workspace_reason: Optional[str] = None,
    ) -> AttemptReservation:
        """Persist a workspace path before Git is asked to create it."""
        path = Path(workspace_path).expanduser().resolve() if workspace_path is not None else None
        with self._writer() as conn:
            row = conn.execute(
                "SELECT * FROM reservations WHERE run_id = ? AND job_id = ? "
                "AND attempt_no = ?",
                (run_id, job_id, attempt_no),
            ).fetchone()
            if row is None:
                raise StoreError("reservation does not exist")
            if row["disposition"] is not None:
                raise StoreError("reservation is already resolved")
            conn.execute(
                "UPDATE reservations SET workspace_path = ?, workspace_status = ?, "
                "workspace_reason = ? WHERE run_id = ? AND job_id = ? AND attempt_no = ?",
                (
                    str(path) if path is not None else None,
                    "isolated" if path is not None else "none",
                    workspace_reason,
                    run_id,
                    job_id,
                    attempt_no,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM reservations WHERE run_id = ? AND job_id = ? "
                "AND attempt_no = ?",
                (run_id, job_id, attempt_no),
            ).fetchone()
        if updated is None:  # pragma: no cover - protected by the transaction
            raise StoreError("reservation disappeared")
        return _row_to_reservation(updated)

    def arm_reservation(
        self,
        run_id: str,
        job_id: str,
        attempt_no: int,
        *,
        invoke_armed_at: str,
    ) -> AttemptReservation:
        """Commit the seam-invocation uncertainty marker before the seam call."""
        with self._writer() as conn:
            row = conn.execute(
                "SELECT * FROM reservations WHERE run_id = ? AND job_id = ? "
                "AND attempt_no = ?",
                (run_id, job_id, attempt_no),
            ).fetchone()
            if row is None:
                raise StoreError("reservation does not exist")
            if row["disposition"] is not None:
                raise StoreError("reservation is already resolved")
            conn.execute(
                "UPDATE reservations SET invoke_armed_at = ? "
                "WHERE run_id = ? AND job_id = ? AND attempt_no = ?",
                (str(invoke_armed_at), run_id, job_id, attempt_no),
            )
            self._record_event(
                conn,
                run_id=run_id,
                event="call-started",
                at=_events.event_at(invoke_armed_at),
                job_id=job_id,
                attempt_no=attempt_no,
                adapter=str(row["backend"]),
                model=str(row["model"]),
            )
            updated = conn.execute(
                "SELECT * FROM reservations WHERE run_id = ? AND job_id = ? "
                "AND attempt_no = ?",
                (run_id, job_id, attempt_no),
            ).fetchone()
        if updated is None:  # pragma: no cover - protected by the transaction
            raise StoreError("reservation disappeared")
        return _row_to_reservation(updated)

    def resolve_reservation_before_invoke(
        self,
        run_id: str,
        job_id: str,
        attempt_no: int,
        *,
        reason: str,
        at: Optional[str] = None,
    ) -> AttemptReservation:
        """Resolve a normal pre-seam failure without consuming the attempt budget."""
        when = str(at) if at is not None else str(time.time())
        with self._writer() as conn:
            row = conn.execute(
                "SELECT * FROM reservations WHERE run_id = ? AND job_id = ? "
                "AND attempt_no = ?",
                (run_id, job_id, attempt_no),
            ).fetchone()
            if row is None or row["disposition"] is not None:
                raise StoreError("reservation is not active")
            if row["invoke_armed_at"] is not None:
                raise StoreError("armed reservation cannot be resolved before invoke")
            conn.execute(
                "UPDATE reservations SET disposition = ?, resolved_at = ?, "
                "loss_reason = ? WHERE run_id = ? AND job_id = ? AND attempt_no = ?",
                (
                    "pre_invoke_failure",
                    when,
                    str(reason)[:ERROR_LIMIT],
                    run_id,
                    job_id,
                    attempt_no,
                ),
            )
            conn.execute(
                "UPDATE jobs SET state = ?, error_message = ?, updated_at = ? "
                "WHERE run_id = ? AND id = ?",
                (
                    JobState.FAILED.value,
                    str(reason)[:ERROR_LIMIT],
                    time.time(),
                    run_id,
                    job_id,
                ),
            )
            # The reservation ends without an invocation, and its job becomes
            # FAILED: one attempt result and the job's one terminal.
            event_at = _events.event_at(when)
            self._record_event(
                conn,
                run_id=run_id,
                event="result",
                at=event_at,
                job_id=job_id,
                attempt_no=attempt_no,
                adapter=str(row["backend"]),
                model=str(row["model"]),
                payload={
                    "status": "not-invoked",
                    "reason": _events.reason_text(reason),
                },
            )
            self._record_event(
                conn,
                run_id=run_id,
                event="terminal",
                at=event_at,
                job_id=job_id,
                payload={
                    "state": JobState.FAILED.value,
                    "reason": _events.reason_text(reason),
                },
            )
            updated = conn.execute(
                "SELECT * FROM reservations WHERE run_id = ? AND job_id = ? "
                "AND attempt_no = ?",
                (run_id, job_id, attempt_no),
            ).fetchone()
        if updated is None:  # pragma: no cover - protected by the transaction
            raise StoreError("reservation disappeared")
        return _row_to_reservation(updated)

    def recover_reservations(
        self, run_id: str, *, at: Optional[str] = None
    ) -> list[AttemptReservation]:
        """Resolve every live reservation left by a lost runner process."""
        when = str(at) if at is not None else str(time.time())
        recovered: list[AttemptReservation] = []
        with self._writer() as conn:
            self._require_run(conn, run_id)
            # A live continuation holds its job RUNNING with no live
            # reservation, which the legacy branch below would reset to
            # PENDING -- a second model call for an attempt that already
            # completed. Resolve continuations FIRST: the job returns to
            # WAITING with its answer intact, and the next pass re-runs it.
            self._recover_continuations(conn, run_id, when)
            rows = conn.execute(
                "SELECT * FROM reservations WHERE run_id = ? AND disposition IS NULL "
                "ORDER BY id",
                (run_id,),
            ).fetchall()
            reserved_jobs = {str(row["job_id"]) for row in rows}
            # A ledger written before reservations existed can hold a RUNNING
            # job with no reservation row. Nothing above resolves it, and a
            # reservation cannot be taken from RUNNING, so the run would be
            # unresumable forever. Its seam invocation was never recorded, so
            # it returns to PENDING exactly as such a ledger always retried it.
            legacy = conn.execute(
                "SELECT id FROM jobs WHERE run_id = ? AND state = ?",
                (run_id, JobState.RUNNING.value),
            ).fetchall()
            for job_row in legacy:
                if str(job_row["id"]) in reserved_jobs:
                    continue
                conn.execute(
                    "UPDATE jobs SET state = ?, updated_at = ? "
                    "WHERE run_id = ? AND id = ?",
                    (JobState.PENDING.value, time.time(), run_id, str(job_row["id"])),
                )
            for row in rows:
                run_job = (str(row["run_id"]), str(row["job_id"]))
                if row["invoke_armed_at"] is None:
                    disposition = "process_lost_before_invoke"
                    loss_reason = "process lost before seam invocation"
                    terminal_state = JobState.PENDING
                    error_message = None
                    lost_at = None
                else:
                    disposition = "process_lost"
                    loss_reason = "process lost after seam invocation was armed"
                    lost_at = when
                    job_row = self._require_job(conn, *run_job)
                    budget_count = self._reservation_budget_count(conn, *run_job) + 1
                    max_attempts = int(
                        Job.from_mapping(json.loads(job_row["definition_json"])).max_attempts
                    )
                    terminal_state = (
                        JobState.FAILED if budget_count >= max_attempts else JobState.PENDING
                    )
                    error_message = (
                        "process lost after seam invocation was armed"
                        if terminal_state is JobState.FAILED
                        else None
                    )
                conn.execute(
                    "UPDATE reservations SET disposition = ?, resolved_at = ?, "
                    "lost_at = ?, loss_reason = ? WHERE id = ?",
                    (disposition, when, lost_at, loss_reason, int(row["id"])),
                )
                conn.execute(
                    "UPDATE jobs SET state = ?, error_message = ?, updated_at = ? "
                    "WHERE run_id = ? AND id = ?",
                    (
                        terminal_state.value,
                        error_message,
                        time.time(),
                        run_job[0],
                        run_job[1],
                    ),
                )
                event_at = _events.event_at(when)
                self._record_event(
                    conn,
                    run_id=run_job[0],
                    event="result",
                    at=event_at,
                    job_id=run_job[1],
                    attempt_no=int(row["attempt_no"]),
                    adapter=str(row["backend"]),
                    model=str(row["model"]),
                    payload={"status": "lost", "reason": loss_reason},
                )
                if terminal_state in TERMINAL_STATES:
                    self._record_event(
                        conn,
                        run_id=run_job[0],
                        event="terminal",
                        at=event_at,
                        job_id=run_job[1],
                        payload={
                            "state": terminal_state.value,
                            "reason": _events.reason_text(error_message or loss_reason),
                        },
                    )
                updated = conn.execute(
                    "SELECT * FROM reservations WHERE id = ?", (int(row["id"]),)
                ).fetchone()
                if updated is not None:
                    recovered.append(_row_to_reservation(updated))
        return recovered

    def append_attempt(
        self,
        attempt: Attempt,
        *,
        terminal_state: Optional[JobState] = None,
        at: Optional[float] = None,
        reason: Optional[str] = None,
        interrupt: Optional[InterruptRequest] = None,
    ) -> Attempt:
        """Append one attempt and update its job state atomically.

        A live reservation supplies the allocated attempt number. The legacy
        direct-append path accepts the next available number for stores that
        predate reservations. The method never updates an attempt row and
        refuses a terminal job. A null ``terminal_state`` leaves the job
        pending for another attempt. ``reason`` becomes the job's error
        message when the attempt terminalizes it, and is ignored otherwise.

        ``interrupt`` records the contract's interrupt request in the same
        transaction and leaves the job ``waiting``. It needs a null
        ``terminal_state``, an acceptance whose outcome is
        ``interrupt_requested``, a ``running`` job, and the attempt's own
        live, armed reservation; the legacy direct-append path refuses it.
        """
        if terminal_state is not None and terminal_state not in TERMINAL_STATES:
            raise ValueError("append_attempt requires a terminal or null job state")
        if terminal_state is JobState.UNROUTABLE:
            raise ValueError("unroutable jobs must be marked without an attempt")
        if interrupt is not None:
            if terminal_state is not None:
                raise ValueError(
                    "an attempt that requests an interrupt leaves its job "
                    "waiting; it cannot also name a terminal state"
                )
            if attempt.acceptance is None or attempt.acceptance.outcome != "interrupt_requested":
                raise ValueError(
                    "an attempt that requests an interrupt needs an acceptance "
                    "whose outcome is interrupt_requested"
                )
            interrupt = _interrupts.check_request(interrupt)
        elif (
            attempt.acceptance is not None
            and attempt.acceptance.outcome == "interrupt_requested"
        ):
            raise ValueError(
                "an acceptance outcome of interrupt_requested needs its interrupt request"
            )
        next_state = (
            JobState.WAITING if interrupt is not None else terminal_state or JobState.PENDING
        )
        when = time.time() if at is None else at
        with self._writer() as conn:
            self._require_run(conn, attempt.run_id)
            job_row = self._require_job(conn, attempt.run_id, attempt.job_id)
            state = JobState(job_row["state"])
            if state in TERMINAL_STATES:
                raise TerminalStateError(
                    f"{attempt.run_id!r}/{attempt.job_id!r} is already {state.value}"
                )
            reservation = conn.execute(
                "SELECT * FROM reservations WHERE run_id = ? AND job_id = ? "
                "AND attempt_no = ?",
                (attempt.run_id, attempt.job_id, attempt.attempt_no),
            ).fetchone()
            if interrupt is not None:
                if state is not JobState.RUNNING:
                    raise StoreError(
                        f"{attempt.run_id!r}/{attempt.job_id!r} is {state.value}; "
                        "an interrupt request needs a running job"
                    )
                if (
                    reservation is None
                    or reservation["disposition"] is not None
                    or reservation["invoke_armed_at"] is None
                ):
                    raise StoreError(
                        "an interrupt request needs the attempt's own live "
                        f"armed reservation ({attempt.run_id!r}/{attempt.job_id!r} "
                        f"attempt {attempt.attempt_no})"
                    )
            active_reservation = conn.execute(
                "SELECT 1 FROM reservations WHERE run_id = ? AND job_id = ? "
                "AND disposition IS NULL LIMIT 1",
                (attempt.run_id, attempt.job_id),
            ).fetchone()
            if reservation is not None and reservation["disposition"] is not None:
                raise StoreError("reservation is already resolved")
            if reservation is not None and reservation["disposition"] is None:
                if reservation["invoke_armed_at"] is None:
                    raise StoreError("cannot append an unarmed reservation")
                attempt = replace(
                    attempt, started_at=str(reservation["invoke_armed_at"])
                )
            elif active_reservation is not None:
                raise StoreError("cannot append beside a live reservation")
            else:
                count_row = conn.execute(
                    "SELECT MAX(attempt_no) AS attempt_no FROM attempts "
                    "WHERE run_id = ? AND job_id = ?",
                    (attempt.run_id, attempt.job_id),
                ).fetchone()
                expected = int(count_row["attempt_no"] or 0) + 1
                if attempt.attempt_no != expected:
                    raise ValueError(
                        f"attempt_no {attempt.attempt_no} is not the next available "
                        f"number {expected} for {attempt.run_id!r}/{attempt.job_id!r}"
                    )
            error_code = attempt.error.code if attempt.error is not None else None
            error_message = (
                attempt.error.message[:ERROR_LIMIT]
                if attempt.error is not None
                else None
            )
            usage = attempt.usage
            conn.execute(
                """
                INSERT INTO attempts(
                    run_id, job_id, attempt_no, endpoint, backend, model, status,
                    error_code, error_message, halt_kind, dropped_params_json,
                    forwarded_params_json, execution_controls_applied_json,
                    started_at, ended_at,
                    input_tokens, output_tokens, cache_hit_tokens, total_tokens,
                    response_text, reasoning, finish_reason, workspace, base_ref,
                    workspace_status,
                    workspace_reason, workspace_removed_at, workspace_removal_forced,
                    acceptance_json, pace_readings_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    attempt.run_id,
                    attempt.job_id,
                    attempt.attempt_no,
                    attempt.endpoint,
                    attempt.backend,
                    attempt.model,
                    attempt.status,
                    error_code,
                    error_message,
                    attempt.halt_kind,
                    _json_or_none(
                        list(attempt.dropped_params)
                        if attempt.dropped_params is not None
                        else None
                    ),
                    _json_or_none(
                        list(attempt.forwarded_params)
                        if attempt.forwarded_params is not None
                        else None
                    ),
                    _json_or_none(
                        list(attempt.execution_controls_applied)
                        if attempt.execution_controls_applied is not None
                        else None
                    ),
                    attempt.started_at,
                    attempt.ended_at,
                    usage.input_tokens if usage is not None else None,
                    usage.output_tokens if usage is not None else None,
                    usage.cache_hit_tokens if usage is not None else None,
                    usage.total_tokens if usage is not None else None,
                    attempt.response_text,
                    attempt.reasoning,
                    attempt.finish_reason,
                    str(attempt.workspace) if attempt.workspace is not None else None,
                    attempt.base_ref,
                    attempt.workspace_status,
                    attempt.workspace_reason,
                    attempt.workspace_removed_at,
                    int(attempt.workspace_removal_forced),
                    _json_or_none(
                        attempt.acceptance.to_mapping()
                        if attempt.acceptance is not None
                        else None
                    ),
                    _json_or_none(
                        [dict(reading) for reading in attempt.pace_readings]
                        if attempt.pace_readings is not None
                        else None
                    ),
                ),
            )
            attempt_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            if reservation is not None and reservation["disposition"] is None:
                conn.execute(
                    "UPDATE reservations SET disposition = ?, resolved_at = ? "
                    "WHERE id = ?",
                    ("completed", str(when), int(reservation["id"])),
                )
            job_reason = (
                str(reason)[:ERROR_LIMIT]
                if reason is not None and terminal_state is not None
                else None
            )
            conn.execute(
                "UPDATE jobs SET state = ?, error_message = ?, updated_at = ? "
                "WHERE run_id = ? AND id = ?",
                (next_state.value, job_reason, when, attempt.run_id, attempt.job_id),
            )
            self._record_attempt_events(
                conn, attempt, terminal_state=terminal_state, when=when
            )
            if interrupt is not None:
                interrupt_row = self._insert_interrupt(
                    conn,
                    run_id=attempt.run_id,
                    job_id=attempt.job_id,
                    attempt_no=attempt.attempt_no,
                    continuation_no=0,
                    request=interrupt,
                    created_at=when,
                )
                self._record_interrupt_event(
                    conn,
                    interrupt_row,
                    phase="requested",
                    at=_events.event_at(attempt.ended_at, when),
                )
        return replace(attempt, id=attempt_id)

    @staticmethod
    def _insert_interrupt(
        conn: sqlite3.Connection,
        *,
        run_id: str,
        job_id: str,
        attempt_no: int,
        continuation_no: int,
        request: InterruptRequest,
        created_at: float,
    ) -> sqlite3.Row:
        """Insert one interrupt row; ``expires_at`` is job-kit's own clock."""
        expires_at = (
            created_at + request.expires_in_s
            if request.expires_in_s is not None
            else None
        )
        cursor = conn.execute(
            "INSERT INTO interrupts(run_id, job_id, attempt_no, continuation_no, "
            "envelope, kind, request_schema_json, payload_json, created_at, "
            "expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                job_id,
                attempt_no,
                continuation_no,
                request.envelope,
                request.kind,
                _interrupts.canonical_json(request.request_schema),
                _interrupts.canonical_json(request.payload),
                created_at,
                expires_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM interrupts WHERE id = ?", (int(cursor.lastrowid),)
        ).fetchone()
        if row is None:  # pragma: no cover - protected by the transaction
            raise StoreError("interrupt was not persisted")
        return row

    def _record_attempt_events(
        self,
        conn: sqlite3.Connection,
        attempt: Attempt,
        *,
        terminal_state: Optional[JobState],
        when: float,
    ) -> None:
        """Record an appended attempt's usage, result and (maybe) terminal."""
        at = _events.event_at(attempt.ended_at, when)
        identity = dict(
            run_id=attempt.run_id,
            job_id=attempt.job_id,
            attempt_no=attempt.attempt_no,
            adapter=attempt.backend,
            model=attempt.model,
        )
        usage = _events.usage_payload(attempt.usage)
        if usage is not None:
            self._record_event(conn, event="usage", at=at, payload=usage, **identity)
        result: dict[str, object] = {"status": attempt.status}
        if attempt.error is not None:
            result["error_code"] = attempt.error.code
        if attempt.halt_kind is not None:
            result["halt_kind"] = attempt.halt_kind
        if attempt.acceptance is not None:
            if attempt.acceptance.outcome in ("not_run", "interrupt_requested"):
                result["acceptance"] = attempt.acceptance.outcome
            elif attempt.acceptance.accepted:
                result["acceptance"] = "accepted"
            else:
                result["acceptance"] = "rejected"
        self._record_event(conn, event="result", at=at, payload=result, **identity)
        if terminal_state is not None:
            self._record_event(
                conn,
                run_id=attempt.run_id,
                event="terminal",
                at=at,
                job_id=attempt.job_id,
                payload={"state": terminal_state.value},
            )

    # ------------------------------------------------------------------
    # Durable interrupts
    # ------------------------------------------------------------------

    def _record_expiry(
        self, conn: sqlite3.Connection, interrupt_row: sqlite3.Row, now: float
    ) -> None:
        """Record one lapsed interrupt: resolution, job expired, events."""
        interrupt_id = str(interrupt_row["id"])
        conn.execute(
            "INSERT INTO interrupt_resolutions(interrupt_id, outcome, input_json, "
            "reason, resolved_at) VALUES (?, 'expired', NULL, NULL, ?)",
            (int(interrupt_row["id"]), now),
        )
        message = f"interrupt {interrupt_id} expired before it was resolved"
        conn.execute(
            "UPDATE jobs SET state = ?, error_message = ?, updated_at = ? "
            "WHERE run_id = ? AND id = ?",
            (
                JobState.EXPIRED.value,
                message,
                now,
                interrupt_row["run_id"],
                interrupt_row["job_id"],
            ),
        )
        at = _events.event_at(now)
        self._record_interrupt_event(conn, interrupt_row, phase="expired", at=at)
        self._record_event(
            conn,
            run_id=str(interrupt_row["run_id"]),
            event="terminal",
            at=at,
            job_id=str(interrupt_row["job_id"]),
            payload={"state": JobState.EXPIRED.value, "reason": message},
        )

    @staticmethod
    def _open_interrupt_rows(
        conn: sqlite3.Connection, run_id: str, job_id: Optional[str] = None
    ) -> list[sqlite3.Row]:
        """The unresolved interrupt rows of a run (or one job), in id order."""
        query = (
            "SELECT i.* FROM interrupts AS i WHERE i.run_id = ? AND NOT EXISTS ("
            "SELECT 1 FROM interrupt_resolutions AS r WHERE r.interrupt_id = i.id)"
        )
        parameters: tuple = (run_id,)
        if job_id is not None:
            query += " AND i.job_id = ?"
            parameters += (job_id,)
        return conn.execute(query + " ORDER BY i.id", parameters).fetchall()

    def expire_interrupts(
        self, run_id: str, now: Optional[float] = None
    ) -> list[InterruptRecord]:
        """Record every lapsed, unresolved interrupt of a waiting job.

        One transaction. Each lapse writes an ``expired`` resolution, moves
        the job ``waiting`` -> ``expired``, and records the ``interrupt``
        (expired) and ``terminal`` events. A lapse is recorded exactly once:
        an interrupt with a resolution is never considered again. Returns
        the interrupts expired by this call.
        """
        when = time.time() if now is None else float(now)
        expired_ids: list[int] = []
        with self._writer() as conn:
            self._require_run(conn, run_id)
            for row in self._open_interrupt_rows(conn, run_id):
                if not interrupt_lapsed(
                    float(row["expires_at"]) if row["expires_at"] is not None else None,
                    when,
                ):
                    continue
                job_row = self._require_job(conn, run_id, str(row["job_id"]))
                if JobState(job_row["state"]) is not JobState.WAITING:
                    continue
                self._record_expiry(conn, row, when)
                expired_ids.append(int(row["id"]))
            records = [
                record
                for record in _read_interrupts(conn, run_id)
                if int(record.id) in expired_ids
            ]
        return records

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

        ``decision`` is ``answer`` (``input`` is validated against the
        request schema the interrupt row declared) or ``reject`` (``reason``
        is the operator's optional free text). Replaying a resolution equal to
        the stored one -- same decision, same canonical input, same reason --
        returns it with ``replayed`` set and writes nothing, even after the
        job moved on; a different one raises :class:`ResolutionConflictError`.
        An interrupt found lapsed is recorded as expired, committed, and then
        refused with :class:`InterruptExpiredError`. The validator probe runs
        before the transaction opens.
        """
        _interrupts._schema_validator()
        outcome = _DECISION_OUTCOMES.get(decision)
        if outcome is None:
            raise ValueError(
                f"decision must be one of {', '.join(sorted(_DECISION_OUTCOMES))}, "
                f"got {decision!r}"
            )
        if outcome == "rejected" and input is not None:
            raise ValueError("a rejection carries a reason, not an input")
        if outcome == "answered" and reason is not None:
            raise ValueError("an answer carries an input, not a reason")
        bounded_reason = str(reason)[:ERROR_LIMIT] if reason is not None else None
        when = time.time() if now is None else float(now)
        text = str(interrupt_id).strip()
        if not text.isdecimal() or not text.isascii():
            raise UnknownInterruptError(
                f"interrupt {interrupt_id!r} is not an interrupt id in run {run_id!r}"
            )
        expired: Optional[InterruptExpiredError] = None
        with self._writer() as conn:
            if conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is None:
                raise UnknownInterruptError(f"run {run_id!r} does not exist")
            row = conn.execute(
                "SELECT * FROM interrupts WHERE id = ?", (int(text),)
            ).fetchone()
            if row is None or str(row["run_id"]) != run_id:
                raise UnknownInterruptError(
                    f"interrupt {text} is not an interrupt in run {run_id!r}"
                )
            stored = conn.execute(
                "SELECT * FROM interrupt_resolutions WHERE interrupt_id = ?",
                (int(text),),
            ).fetchone()
            if stored is not None:
                resolution = _row_to_resolution(stored)
                if resolution.outcome == "expired":
                    raise InterruptExpiredError(
                        text,
                        float(row["expires_at"]) if row["expires_at"] is not None else None,
                    )
                if outcome == "answered":
                    try:
                        candidate = _interrupts.canonical_json(input)
                    except (TypeError, ValueError):
                        candidate = None
                    same = (
                        resolution.outcome == outcome
                        and candidate is not None
                        and candidate == stored["input_json"]
                    )
                else:
                    same = (
                        resolution.outcome == outcome
                        and bounded_reason == resolution.reason
                    )
                if not same:
                    raise ResolutionConflictError(text, resolution.outcome)
                return replace(resolution, replayed=True)
            expires_at = float(row["expires_at"]) if row["expires_at"] is not None else None
            job_row = self._require_job(conn, run_id, str(row["job_id"]))
            state = JobState(job_row["state"])
            if interrupt_lapsed(expires_at, when) and state is JobState.WAITING:
                self._record_expiry(conn, row, when)
                expired = InterruptExpiredError(text, expires_at)
            else:
                if state is not JobState.WAITING:
                    raise StoreError(
                        f"interrupt {text} is unresolved but its job "
                        f"{run_id!r}/{row['job_id']!r} is {state.value}, not waiting; "
                        "nothing was written"
                    )
                input_json = None
                if outcome == "answered":
                    request_schema = _load_json(row["request_schema_json"])
                    try:
                        input_json = _interrupts.validate_input(request_schema, input)
                    except _interrupts.InterruptInputError as exc:
                        raise ResolutionInputError(str(exc), exc.errors) from exc
                conn.execute(
                    "INSERT INTO interrupt_resolutions(interrupt_id, outcome, "
                    "input_json, reason, resolved_at) VALUES (?, ?, ?, ?, ?)",
                    (int(text), outcome, input_json, bounded_reason, when),
                )
                at = _events.event_at(when)
                if outcome == "answered":
                    self._record_interrupt_event(conn, row, phase="resolved", at=at)
                else:
                    job_message = bounded_reason or f"interrupt {text} rejected by the operator"
                    conn.execute(
                        "UPDATE jobs SET state = ?, error_message = ?, updated_at = ? "
                        "WHERE run_id = ? AND id = ?",
                        (
                            JobState.OPERATOR_REJECTED.value,
                            job_message,
                            when,
                            run_id,
                            str(row["job_id"]),
                        ),
                    )
                    self._record_interrupt_event(conn, row, phase="rejected", at=at)
                    terminal: dict[str, object] = {
                        "state": JobState.OPERATOR_REJECTED.value
                    }
                    if bounded_reason is not None:
                        terminal["reason"] = _events.reason_text(bounded_reason)
                    self._record_event(
                        conn,
                        run_id=run_id,
                        event="terminal",
                        at=at,
                        job_id=str(row["job_id"]),
                        payload=terminal,
                    )
                stored = conn.execute(
                    "SELECT * FROM interrupt_resolutions WHERE interrupt_id = ?",
                    (int(text),),
                ).fetchone()
        if expired is not None:
            raise expired
        return _row_to_resolution(stored)

    def begin_continuation(
        self, run_id: str, job_id: str, *, now: Optional[float] = None
    ) -> Continuation:
        """Start re-running the owning attempt's contract after an answer.

        One transaction: the job's latest interrupt must be resolved
        ``answered``, the job must have no live continuation, and the job
        must be ``waiting``. The continuation is numbered after every earlier
        continuation of that attempt, the job becomes ``running``, and
        ``job-kit:continuation-started`` is recorded. No reservation and no
        attempt row is written, so the attempt budget is untouched.
        """
        when = time.time() if now is None else float(now)
        with self._writer() as conn:
            self._require_run(conn, run_id)
            job_row = self._require_job(conn, run_id, job_id)
            latest = _read_interrupts(conn, run_id, job_id)
            if not latest:
                raise StoreError(f"{run_id!r}/{job_id!r} has no interrupt to continue")
            record = latest[-1]
            if record.resolution is None or record.resolution.outcome != "answered":
                status = (
                    record.resolution.outcome if record.resolution is not None else "unresolved"
                )
                raise StoreError(
                    f"interrupt {record.id} of {run_id!r}/{job_id!r} is {status}, "
                    "not answered; only an answered interrupt is continued"
                )
            live = conn.execute(
                "SELECT continuation_no FROM continuations WHERE run_id = ? "
                "AND job_id = ? AND disposition IS NULL",
                (run_id, job_id),
            ).fetchone()
            if live is not None:
                raise StoreError(
                    f"{run_id!r}/{job_id!r} already has a live continuation "
                    f"({int(live['continuation_no'])})"
                )
            state = JobState(job_row["state"])
            if state is not JobState.WAITING:
                raise StoreError(
                    f"{run_id!r}/{job_id!r} is {state.value}; a continuation "
                    "starts only from waiting"
                )
            number_row = conn.execute(
                "SELECT MAX(continuation_no) AS number FROM continuations "
                "WHERE run_id = ? AND job_id = ? AND attempt_no = ?",
                (run_id, job_id, record.attempt_no),
            ).fetchone()
            continuation_no = int(number_row["number"] or 0) + 1
            cursor = conn.execute(
                "INSERT INTO continuations(run_id, job_id, attempt_no, "
                "continuation_no, interrupt_id, started_at) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, job_id, record.attempt_no, continuation_no, int(record.id), when),
            )
            conn.execute(
                "UPDATE jobs SET state = ?, error_message = NULL, updated_at = ? "
                "WHERE run_id = ? AND id = ?",
                (JobState.RUNNING.value, when, run_id, job_id),
            )
            self._record_continuation_event(
                conn,
                run_id=run_id,
                job_id=job_id,
                attempt_no=record.attempt_no,
                event=f"{_events.PLUGIN}:continuation-started",
                at=_events.event_at(when),
                payload={"interrupt_id": record.id, "continuation_no": continuation_no},
            )
            row = conn.execute(
                "SELECT * FROM continuations WHERE id = ?", (int(cursor.lastrowid),)
            ).fetchone()
        if row is None:  # pragma: no cover - protected by the transaction
            raise StoreError("continuation was not persisted")
        return _row_to_continuation(row)

    def _record_continuation_event(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        job_id: str,
        attempt_no: int,
        event: str,
        at: str,
        payload: Mapping[str, object],
    ) -> int:
        """Record a ``job-kit:`` continuation event under the owning attempt."""
        attempt = conn.execute(
            "SELECT backend, model FROM attempts WHERE run_id = ? AND job_id = ? "
            "AND attempt_no = ?",
            (run_id, job_id, attempt_no),
        ).fetchone()
        return self._record_event(
            conn,
            run_id=run_id,
            event=event,
            at=at,
            job_id=job_id,
            attempt_no=attempt_no,
            adapter=(str(attempt["backend"]) if attempt is not None else None),
            model=(str(attempt["model"]) if attempt is not None else None),
            payload=payload,
        )

    def finish_continuation(
        self,
        run_id: str,
        job_id: str,
        attempt_no: int,
        continuation_no: int,
        *,
        acceptance: Optional[Acceptance] = None,
        disposition: str = "completed",
        terminal_state: Optional[JobState] = None,
        interrupt: Optional[InterruptRequest] = None,
        reason: Optional[str] = None,
        now: Optional[float] = None,
    ) -> Continuation:
        """Record how a live continuation ended, and move its job, atomically.

        The named continuation must be the job's live one, for that attempt.
        ``disposition`` ``interrupted`` (Ctrl-C) returns the job to
        ``waiting`` with its answer intact. ``completed`` moves the job to
        ``terminal_state`` (then ``terminal`` is recorded), to ``waiting`` on
        a follow-up ``interrupt`` raised by this continuation (whose
        ``continuation_no`` it carries), or back to ``pending``. ``reason``
        becomes the job's error message when it terminalizes the job.
        """
        if disposition not in ("completed", "interrupted"):
            raise ValueError("disposition must be completed or interrupted")
        if terminal_state is not None and terminal_state not in TERMINAL_STATES:
            raise ValueError("finish_continuation requires a terminal or null job state")
        if disposition == "interrupted" and (terminal_state is not None or interrupt is not None):
            raise ValueError("an interrupted continuation returns its job to waiting")
        if interrupt is not None:
            if terminal_state is not None:
                raise ValueError(
                    "a continuation that requests a follow-up interrupt leaves its "
                    "job waiting; it cannot also name a terminal state"
                )
            if acceptance is None or acceptance.outcome != "interrupt_requested":
                raise ValueError(
                    "a follow-up interrupt request needs an acceptance whose "
                    "outcome is interrupt_requested"
                )
            interrupt = _interrupts.check_request(interrupt)
        if disposition == "interrupted":
            next_state = JobState.WAITING
        elif interrupt is not None:
            next_state = JobState.WAITING
        else:
            next_state = terminal_state or JobState.PENDING
        when = time.time() if now is None else float(now)
        with self._writer() as conn:
            self._require_run(conn, run_id)
            self._require_job(conn, run_id, job_id)
            live = conn.execute(
                "SELECT * FROM continuations WHERE run_id = ? AND job_id = ? "
                "AND disposition IS NULL",
                (run_id, job_id),
            ).fetchone()
            if live is None:
                raise StoreError(
                    f"{run_id!r}/{job_id!r} has no live continuation to finish"
                )
            if int(live["attempt_no"]) != attempt_no or int(live["continuation_no"]) != continuation_no:
                raise StoreError(
                    f"the live continuation of {run_id!r}/{job_id!r} is attempt "
                    f"{int(live['attempt_no'])} continuation "
                    f"{int(live['continuation_no'])}, not attempt {attempt_no} "
                    f"continuation {continuation_no}"
                )
            conn.execute(
                "UPDATE continuations SET ended_at = ?, disposition = ?, "
                "acceptance_json = ? WHERE id = ?",
                (
                    when,
                    disposition,
                    _json_or_none(acceptance.to_mapping() if acceptance is not None else None),
                    int(live["id"]),
                ),
            )
            job_reason = (
                str(reason)[:ERROR_LIMIT]
                if reason is not None and terminal_state is not None
                else None
            )
            conn.execute(
                "UPDATE jobs SET state = ?, error_message = ?, updated_at = ? "
                "WHERE run_id = ? AND id = ?",
                (next_state.value, job_reason, when, run_id, job_id),
            )
            at = _events.event_at(when)
            result: dict[str, object] = {
                "status": disposition,
                "continuation_no": continuation_no,
            }
            if acceptance is not None:
                if acceptance.outcome in ("not_run", "timed_out", "interrupt_requested"):
                    result["acceptance"] = acceptance.outcome
                else:
                    result["acceptance"] = "accepted" if acceptance.accepted else "rejected"
            self._record_continuation_event(
                conn,
                run_id=run_id,
                job_id=job_id,
                attempt_no=attempt_no,
                event=f"{_events.PLUGIN}:continuation-result",
                at=at,
                payload=result,
            )
            if interrupt is not None:
                interrupt_row = self._insert_interrupt(
                    conn,
                    run_id=run_id,
                    job_id=job_id,
                    attempt_no=attempt_no,
                    continuation_no=continuation_no,
                    request=interrupt,
                    created_at=when,
                )
                self._record_interrupt_event(conn, interrupt_row, phase="requested", at=at)
            elif terminal_state is not None:
                terminal: dict[str, object] = {"state": terminal_state.value}
                if job_reason is not None:
                    terminal["reason"] = _events.reason_text(job_reason)
                self._record_event(
                    conn,
                    run_id=run_id,
                    event="terminal",
                    at=at,
                    job_id=job_id,
                    payload=terminal,
                )
            row = conn.execute(
                "SELECT * FROM continuations WHERE id = ?", (int(live["id"]),)
            ).fetchone()
        if row is None:  # pragma: no cover - protected by the transaction
            raise StoreError("continuation disappeared")
        return _row_to_continuation(row)

    def _recover_continuations(
        self, conn: sqlite3.Connection, run_id: str, at: str
    ) -> None:
        """Return every job whose live continuation lost its process to waiting.

        Runs inside :meth:`recover_reservations`' transaction, before its
        legacy branch. A job moves only while it is still ``running``.
        """
        live = conn.execute(
            "SELECT * FROM continuations WHERE run_id = ? AND disposition IS NULL "
            "ORDER BY id",
            (run_id,),
        ).fetchall()
        when = time.time()
        for row in live:
            conn.execute(
                "UPDATE continuations SET ended_at = ?, disposition = 'process_lost' "
                "WHERE id = ?",
                (when, int(row["id"])),
            )
            conn.execute(
                "UPDATE jobs SET state = ?, updated_at = ? "
                "WHERE run_id = ? AND id = ? AND state = ?",
                (
                    JobState.WAITING.value,
                    when,
                    run_id,
                    str(row["job_id"]),
                    JobState.RUNNING.value,
                ),
            )
            self._record_continuation_event(
                conn,
                run_id=run_id,
                job_id=str(row["job_id"]),
                attempt_no=int(row["attempt_no"]),
                event=f"{_events.PLUGIN}:continuation-result",
                at=_events.event_at(at),
                payload={
                    "status": "lost",
                    "continuation_no": int(row["continuation_no"]),
                },
            )

    def list_interrupts(
        self, run_id: str, job_id: Optional[str] = None
    ) -> list[InterruptRecord]:
        """Read a run's (or one job's) interrupts with their resolutions."""
        with self.read_transaction() as conn:
            self._require_run(conn, run_id)
            return _read_interrupts(conn, run_id, job_id)

    def open_interrupt(self, run_id: str, job_id: str) -> Optional[InterruptRecord]:
        """The job's one unresolved interrupt, or ``None``."""
        with self.read_transaction() as conn:
            self._require_run(conn, run_id)
            for record in _read_interrupts(conn, run_id, job_id):
                if record.resolution is None:
                    return record
        return None

    def list_continuations(
        self, run_id: str, job_id: Optional[str] = None
    ) -> list[Continuation]:
        """Read a run's (or one job's) continuations in insertion order."""
        with self.read_transaction() as conn:
            self._require_run(conn, run_id)
            return _read_continuations(conn, run_id, job_id)

    def list_attempts(
        self, run_id: str, job_id: Optional[str] = None
    ) -> list[Attempt]:
        """Read append-only attempts in insertion order."""
        with self._connect() as conn:
            self._require_run(conn, run_id)
            if job_id is None:
                rows = conn.execute(
                    "SELECT * FROM attempts WHERE run_id = ? ORDER BY id", (run_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM attempts WHERE run_id = ? AND job_id = ? ORDER BY id",
                    (run_id, job_id),
                ).fetchall()
        return [_row_to_attempt(row) for row in rows]

    def list_events(self, run_id: str) -> tuple[dict, ...]:
        """Render a run's execution events in ``seq`` order.

        Each row renders under the revision stored with it; a row with no
        stored revision (written before ledger schema 12) renders as v1. The
        stream is validated as a whole before it is returned. A run
        created before the event log existed raises
        :class:`EventsNotRecordedError`: its stream would miss the facts
        recorded before the upgrade and must not stand in for its history.
        """
        with self.read_transaction() as conn:
            run_row = self._require_run(conn, run_id)
            if not int(run_row["events_recorded"]):
                raise EventsNotRecordedError(run_id)
            rows = conn.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()
        rendered = [
            _events.build_event(
                seq=int(row["seq"]),
                run_id=str(row["run_id"]),
                event=str(row["event"]),
                at=str(row["at"]),
                job_id=(str(row["job_id"]) if row["job_id"] is not None else None),
                attempt_no=(
                    int(row["attempt_no"]) if row["attempt_no"] is not None else None
                ),
                adapter=row["adapter"],
                model=row["model"],
                payload=_load_json(row["payload_json"]),
                schema=row["schema"],
            )
            for row in rows
        ]
        return _events.validate_stream(rendered)

    def record_workspace_removed(
        self,
        attempt_id: int,
        *,
        at: Optional[float] = None,
        forced: bool = False,
    ) -> Attempt:
        """Annotate an attempt after its worktree was removed by GC."""
        when = time.time() if at is None else at
        with self._writer() as conn:
            row = conn.execute(
                "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"attempt does not exist: {attempt_id}")
            if row["workspace"] is None:
                raise StoreError(f"attempt has no workspace: {attempt_id}")
            if row["workspace_status"] == "removed":
                return _row_to_attempt(row)
            if row["workspace_status"] not in {"isolated", "removing"}:
                raise StoreError(f"attempt has no removable workspace: {attempt_id}")
            removal_forced = bool(forced) or bool(row["workspace_removal_forced"])
            conn.execute(
                "UPDATE attempts SET workspace_status = ?, workspace_removed_at = ?, "
                "workspace_removal_forced = ? "
                "WHERE id = ?",
                ("removed", when, int(removal_forced), attempt_id),
            )
            updated = conn.execute(
                "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
        if updated is None:  # pragma: no cover - protected by the transaction
            raise StoreError(f"attempt does not exist: {attempt_id}")
        return _row_to_attempt(updated)

    def mark_workspace_removing(
        self, attempt_id: int, *, forced: bool = False
    ) -> Attempt:
        """Record cleanup intent, including whether a forced removal was requested."""
        with self._writer() as conn:
            row = conn.execute(
                "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"attempt does not exist: {attempt_id}")
            if row["workspace"] is None:
                raise StoreError(f"attempt has no workspace: {attempt_id}")
            if row["workspace_status"] == "removing":
                if forced and not bool(row["workspace_removal_forced"]):
                    conn.execute(
                        "UPDATE attempts SET workspace_removal_forced = ? WHERE id = ?",
                        (1, attempt_id),
                    )
                    updated = conn.execute(
                        "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
                    ).fetchone()
                    if updated is None:  # pragma: no cover - protected by the transaction
                        raise StoreError(f"attempt does not exist: {attempt_id}")
                    return _row_to_attempt(updated)
                return _row_to_attempt(row)
            if row["workspace_status"] != "isolated":
                raise StoreError(f"attempt has no isolated workspace: {attempt_id}")
            conn.execute(
                "UPDATE attempts SET workspace_status = ?, workspace_removal_forced = ? "
                "WHERE id = ?",
                ("removing", int(forced), attempt_id),
            )
            updated = conn.execute(
                "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
        if updated is None:  # pragma: no cover - protected by the transaction
            raise StoreError(f"attempt does not exist: {attempt_id}")
        return _row_to_attempt(updated)

    def restore_workspace_isolated(self, attempt_id: int) -> Attempt:
        """Clear cleanup intent when Git refused to remove a worktree."""
        with self._writer() as conn:
            row = conn.execute(
                "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"attempt does not exist: {attempt_id}")
            if row["workspace_status"] != "removing":
                raise StoreError(f"attempt is not being removed: {attempt_id}")
            conn.execute(
                "UPDATE attempts SET workspace_status = ?, workspace_removal_forced = ? "
                "WHERE id = ?",
                ("isolated", 0, attempt_id),
            )
            updated = conn.execute(
                "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
        if updated is None:  # pragma: no cover - protected by the transaction
            raise StoreError(f"attempt does not exist: {attempt_id}")
        return _row_to_attempt(updated)

    def mark_reservation_workspace_removing(
        self, reservation_id: int, *, forced: bool = False
    ) -> AttemptReservation:
        """Record cleanup intent for a lost reservation workspace."""
        with self._writer() as conn:
            row = conn.execute(
                "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"reservation does not exist: {reservation_id}")
            if row["workspace_path"] is None:
                raise StoreError(f"reservation has no workspace: {reservation_id}")
            if row["workspace_status"] == "removing":
                if forced and not bool(row["workspace_removal_forced"]):
                    conn.execute(
                        "UPDATE reservations SET workspace_removal_forced = ? WHERE id = ?",
                        (1, reservation_id),
                    )
                    row = conn.execute(
                        "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
                    ).fetchone()
                if row is None:  # pragma: no cover - protected by the transaction
                    raise StoreError(f"reservation does not exist: {reservation_id}")
                return _row_to_reservation(row)
            if row["workspace_status"] != "isolated":
                raise StoreError(f"reservation has no isolated workspace: {reservation_id}")
            conn.execute(
                "UPDATE reservations SET workspace_status = ?, "
                "workspace_removal_forced = ? WHERE id = ?",
                ("removing", int(forced), reservation_id),
            )
            updated = conn.execute(
                "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
            ).fetchone()
        if updated is None:  # pragma: no cover - protected by the transaction
            raise StoreError(f"reservation does not exist: {reservation_id}")
        return _row_to_reservation(updated)

    def restore_reservation_workspace_isolated(
        self, reservation_id: int
    ) -> AttemptReservation:
        """Clear reservation workspace cleanup intent after a refusal."""
        with self._writer() as conn:
            row = conn.execute(
                "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"reservation does not exist: {reservation_id}")
            if row["workspace_status"] != "removing":
                raise StoreError(f"reservation is not being removed: {reservation_id}")
            conn.execute(
                "UPDATE reservations SET workspace_status = ?, "
                "workspace_removal_forced = ? WHERE id = ?",
                ("isolated", 0, reservation_id),
            )
            updated = conn.execute(
                "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
            ).fetchone()
        if updated is None:  # pragma: no cover - protected by the transaction
            raise StoreError(f"reservation does not exist: {reservation_id}")
        return _row_to_reservation(updated)

    def record_reservation_workspace_removed(
        self,
        reservation_id: int,
        *,
        at: Optional[float] = None,
        forced: bool = False,
    ) -> AttemptReservation:
        """Record successful cleanup without changing loss evidence."""
        when = time.time() if at is None else at
        with self._writer() as conn:
            row = conn.execute(
                "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"reservation does not exist: {reservation_id}")
            if row["workspace_path"] is None:
                raise StoreError(f"reservation has no workspace: {reservation_id}")
            if row["workspace_status"] == "removed":
                return _row_to_reservation(row)
            if row["workspace_status"] not in {"isolated", "removing"}:
                raise StoreError(f"reservation has no removable workspace: {reservation_id}")
            conn.execute(
                "UPDATE reservations SET workspace_status = ?, "
                "workspace_removed_at = ?, workspace_removal_forced = ? WHERE id = ?",
                (
                    "removed",
                    when,
                    int(bool(forced) or bool(row["workspace_removal_forced"])),
                    reservation_id,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
            ).fetchone()
        if updated is None:  # pragma: no cover - protected by the transaction
            raise StoreError(f"reservation does not exist: {reservation_id}")
        return _row_to_reservation(updated)

    def get_attempt(self, attempt_id: int) -> Optional[Attempt]:
        """Read one attempt by its database identifier."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        return _row_to_attempt(row) if row is not None else None

    def list_run_ids(self) -> list[str]:
        """List run identifiers in creation order for all-run GC."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM runs ORDER BY created_at, id"
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def halted_endpoints(self, run_id: str) -> frozenset[str]:
        """Return endpoints excluded for persistent halts or a confirmed outage."""
        with self._connect() as conn:
            self._require_run(conn, run_id)
            persistent_placeholders = ", ".join("?" for _ in _PERSISTENT_HALT_KINDS)
            rows = conn.execute(
                f"""
                SELECT DISTINCT endpoint
                FROM attempts
                WHERE run_id = ? AND halt_kind IN ({persistent_placeholders})
                UNION
                SELECT DISTINCT later.endpoint
                FROM attempts AS earlier
                JOIN attempts AS later
                  ON later.run_id = earlier.run_id
                 AND later.endpoint = earlier.endpoint
                 AND later.halt_kind = 'unreachable'
                 AND later.started_at IS NOT NULL
                 AND later.ended_at IS NOT NULL
                 AND earlier.halt_kind = 'unreachable'
                 AND earlier.started_at IS NOT NULL
                 AND earlier.ended_at IS NOT NULL
                 AND later.started_at > earlier.started_at
                 AND later.started_at > earlier.ended_at
                WHERE earlier.run_id = ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM attempts AS between_attempt
                      WHERE between_attempt.run_id = earlier.run_id
                        AND between_attempt.endpoint = earlier.endpoint
                        AND between_attempt.started_at > earlier.started_at
                        AND between_attempt.started_at < later.started_at
                        AND (
                            between_attempt.halt_kind IS NULL
                            OR between_attempt.halt_kind <> 'unreachable'
                        )
                  )
                """,
                (run_id, *_PERSISTENT_HALT_KINDS, run_id),
            ).fetchall()
        return frozenset(str(row["endpoint"]) for row in rows)

    def snapshot(self, run_id: str, *, now: Optional[float] = None) -> RunSnapshot:
        """Read a run, its jobs, attempts, reservations, interrupts and
        continuations from one transaction snapshot.

        ``now`` (default: the time of the read) becomes the snapshot's
        ``read_at``. Nothing is written: a lapse is reported, not recorded.
        """
        with self.read_transaction() as conn:
            return _read_snapshot(
                conn,
                run_id,
                version=len(_MIGRATIONS),
                now=time.time() if now is None else float(now),
            )


class LedgerReader:
    """A read-only view of a job-kit ledger, for ``status``.

    It opens the file with ``mode=ro`` (a URI) and ``PRAGMA query_only``,
    sets no journal mode, and never migrates, so reading a ledger never
    upgrades it. It reads ledger schemas :data:`READER_MIN_SCHEMA` through
    the current one; below :data:`INTERRUPT_SCHEMA` a snapshot has no
    interrupts or continuations. A read-only open of a WAL ledger may create
    SQLite's ``-shm`` sidecar when the directory is writable: that is SQLite
    runtime state, not a ledger write.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self.db_path = Path(db_path).expanduser()
        self.busy_timeout_ms = int(busy_timeout_ms)
        if not self.db_path.is_file():
            raise StoreNotFoundError(self.db_path)

    def _uri(self) -> str:
        path = urllib.parse.quote(self.db_path.resolve().as_posix())
        return f"file:{path}?mode=ro"

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a read-only connection that cannot write, even by pragma."""
        conn = sqlite3.connect(
            self._uri(), uri=True, timeout=self.busy_timeout_ms / 1000.0
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        """One read transaction: every read inside it sees one snapshot."""
        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                yield conn
            finally:
                conn.rollback()

    @staticmethod
    def _checked_version(conn: sqlite3.Connection) -> int:
        """Read and validate the schema version inside the caller's transaction."""
        version = JobStore._read_schema_version(conn)
        if version > len(_MIGRATIONS):
            raise StoreError(_future_schema_message(version))
        if version < READER_MIN_SCHEMA:
            raise StoreError(
                f"database schema version {version} is older than status reads "
                f"(min {READER_MIN_SCHEMA}); status never migrates a ledger -- "
                "run any writing verb, for example `job-kit resume <run-id>`, to "
                "migrate it first"
            )
        return version

    def schema_version(self) -> int:
        """The ledger's stored schema version, validated as :meth:`snapshot` does."""
        with self.read_transaction() as conn:
            return self._checked_version(conn)

    def snapshot(self, run_id: str, *, now: Optional[float] = None) -> RunSnapshot:
        """Read one run in ONE read transaction.

        The schema version is read and validated inside the transaction, and
        every table is read in it, so the version and the rows come from one
        snapshot even beside a writer committing between the reads.
        """
        with self.read_transaction() as conn:
            version = self._checked_version(conn)
            return _read_snapshot(
                conn,
                run_id,
                version=version,
                now=time.time() if now is None else float(now),
            )


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "INTERRUPT_SCHEMA",
    "READER_MIN_SCHEMA",
    "StoreError",
    "StoreNotFoundError",
    "UnknownRunError",
    "UnknownJobError",
    "DuplicateJobError",
    "TerminalStateError",
    "EventsNotRecordedError",
    "UnknownInterruptError",
    "ResolutionConflictError",
    "InterruptExpiredError",
    "ResolutionInputError",
    "AttemptReservation",
    "JobStore",
    "LedgerReader",
]
