"""Durable SQLite ledger for job-kit runs.

The schema is deliberately job-shaped and independent of content-pipeline-kit.
Every public operation opens its own connection. Each connection enables WAL,
``busy_timeout`` and foreign-key enforcement. Attempt seam records are inserts
only; GC may annotate their workspace lifecycle. A job in a terminal state
refuses every later attempt.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterator, Mapping, Optional, Sequence

from .model import (
    Acceptance,
    Attempt,
    AttemptError,
    Job,
    JobRecord,
    JobState,
    RunRecord,
    RunSnapshot,
    RunState,
    TERMINAL_STATES,
    Usage,
    validate_max_parallel,
)


DEFAULT_BUSY_TIMEOUT_MS = 5000
ERROR_LIMIT = 2000


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
    )


def _derive_run_state(job_rows: Sequence[sqlite3.Row]) -> RunState:
    """Derive run state from job states rather than duplicating state."""
    if not job_rows:
        return RunState.COMPLETED
    states = [JobState(row["state"]) for row in job_rows]
    if all(state in TERMINAL_STATES for state in states):
        return RunState.COMPLETED
    if any(state is JobState.RUNNING for state in states):
        return RunState.RUNNING
    return RunState.PENDING


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
        with self._writer() as conn:
            conn.execute(
                "INSERT INTO runs(id, created_at, jobs_path, max_parallel, workspace_root, "
                "workspace_base_refs_json, disallowed_tools) VALUES (?, ?, ?, ?, ?, ?, ?)",
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

    def mark_running(self, run_id: str, job_id: str, *, at: Optional[float] = None) -> JobRecord:
        """Mark a non-terminal job as running before its seam invocation."""
        when = time.time() if at is None else at
        with self._writer() as conn:
            self._require_run(conn, run_id)
            row = self._require_job(conn, run_id, job_id)
            state = JobState(row["state"])
            if state in TERMINAL_STATES:
                raise TerminalStateError(f"{run_id!r}/{job_id!r} is already {state.value}")
            conn.execute(
                "UPDATE jobs SET state = ?, error_message = NULL, updated_at = ? "
                "WHERE run_id = ? AND id = ?",
                (JobState.RUNNING.value, when, run_id, job_id),
            )
            updated = conn.execute(
                "SELECT * FROM jobs WHERE run_id = ? AND id = ?", (run_id, job_id)
            ).fetchone()
        if updated is None:  # pragma: no cover - protected by the transaction
            raise UnknownJobError(f"{run_id!r}/{job_id!r}")
        return _row_to_job(updated)

    def mark_unroutable(
        self,
        run_id: str,
        job_id: str,
        reason: str,
        *,
        at: Optional[float] = None,
    ) -> JobRecord:
        """Terminalize a job whose seam invocation could not be selected."""
        when = time.time() if at is None else at
        with self._writer() as conn:
            self._require_run(conn, run_id)
            row = self._require_job(conn, run_id, job_id)
            state = JobState(row["state"])
            if state in TERMINAL_STATES:
                raise TerminalStateError(
                    f"{run_id!r}/{job_id!r} is already {state.value}"
                )
            conn.execute(
                "UPDATE jobs SET state = ?, error_message = ?, updated_at = ? "
                "WHERE run_id = ? AND id = ?",
                (
                    JobState.UNROUTABLE.value,
                    str(reason)[:ERROR_LIMIT],
                    when,
                    run_id,
                    job_id,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM jobs WHERE run_id = ? AND id = ?", (run_id, job_id)
            ).fetchone()
        if updated is None:  # pragma: no cover - protected by the transaction
            raise UnknownJobError(f"{run_id!r}/{job_id!r}")
        return _row_to_job(updated)

    def mark_halted(
        self,
        run_id: str,
        job_id: str,
        reason: str,
        *,
        at: Optional[float] = None,
    ) -> JobRecord:
        """Terminalize a job whose prior attempts exhausted endpoint eligibility."""
        when = time.time() if at is None else at
        with self._writer() as conn:
            self._require_run(conn, run_id)
            row = self._require_job(conn, run_id, job_id)
            state = JobState(row["state"])
            if state in TERMINAL_STATES:
                raise TerminalStateError(
                    f"{run_id!r}/{job_id!r} is already {state.value}"
                )
            attempt = conn.execute(
                "SELECT 1 FROM attempts WHERE run_id = ? AND job_id = ? LIMIT 1",
                (run_id, job_id),
            ).fetchone()
            if attempt is None:
                raise ValueError("halted jobs must be marked with an attempt")
            conn.execute(
                "UPDATE jobs SET state = ?, error_message = ?, updated_at = ? "
                "WHERE run_id = ? AND id = ?",
                (
                    JobState.HALTED.value,
                    str(reason)[:ERROR_LIMIT],
                    when,
                    run_id,
                    job_id,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM jobs WHERE run_id = ? AND id = ?", (run_id, job_id)
            ).fetchone()
        if updated is None:  # pragma: no cover - protected by the transaction
            raise UnknownJobError(f"{run_id!r}/{job_id!r}")
        return _row_to_job(updated)

    def mark_failed(
        self,
        run_id: str,
        job_id: str,
        reason: str,
        *,
        at: Optional[float] = None,
    ) -> JobRecord:
        """Terminalize a job failure that happened before seam invocation."""
        when = time.time() if at is None else at
        with self._writer() as conn:
            self._require_run(conn, run_id)
            row = self._require_job(conn, run_id, job_id)
            state = JobState(row["state"])
            if state in TERMINAL_STATES:
                raise TerminalStateError(
                    f"{run_id!r}/{job_id!r} is already {state.value}"
                )
            conn.execute(
                "UPDATE jobs SET state = ?, error_message = ?, updated_at = ? "
                "WHERE run_id = ? AND id = ?",
                (
                    JobState.FAILED.value,
                    str(reason)[:ERROR_LIMIT],
                    when,
                    run_id,
                    job_id,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM jobs WHERE run_id = ? AND id = ?", (run_id, job_id)
            ).fetchone()
        if updated is None:  # pragma: no cover - protected by the transaction
            raise UnknownJobError(f"{run_id!r}/{job_id!r}")
        return _row_to_job(updated)

    def append_attempt(
        self,
        attempt: Attempt,
        *,
        terminal_state: Optional[JobState] = None,
        at: Optional[float] = None,
    ) -> Attempt:
        """Append one attempt and update its job state atomically.

        The attempt number must be the next append-only number. The method
        never updates an attempt row and refuses a terminal job. A null
        ``terminal_state`` leaves the job pending for another attempt.
        """
        if terminal_state is not None and terminal_state not in TERMINAL_STATES:
            raise ValueError("append_attempt requires a terminal or null job state")
        if terminal_state is JobState.UNROUTABLE:
            raise ValueError("unroutable jobs must be marked without an attempt")
        next_state = terminal_state or JobState.PENDING
        when = time.time() if at is None else at
        with self._writer() as conn:
            self._require_run(conn, attempt.run_id)
            job_row = self._require_job(conn, attempt.run_id, attempt.job_id)
            state = JobState(job_row["state"])
            if state in TERMINAL_STATES:
                raise TerminalStateError(
                    f"{attempt.run_id!r}/{attempt.job_id!r} is already {state.value}"
                )
            count_row = conn.execute(
                "SELECT COUNT(*) AS count FROM attempts WHERE run_id = ? AND job_id = ?",
                (attempt.run_id, attempt.job_id),
            ).fetchone()
            expected = int(count_row["count"]) + 1
            if attempt.attempt_no != expected:
                raise ValueError(
                    f"attempt_no {attempt.attempt_no} is not the append-only next "
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
                    acceptance_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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
                ),
            )
            attempt_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                "UPDATE jobs SET state = ?, error_message = NULL, updated_at = ? "
                "WHERE run_id = ? AND id = ?",
                (next_state.value, when, attempt.run_id, attempt.job_id),
            )
        return replace(attempt, id=attempt_id)

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
        """Return endpoints with a persistent halt recorded in this run."""
        with self._connect() as conn:
            self._require_run(conn, run_id)
            rows = conn.execute(
                "SELECT DISTINCT endpoint FROM attempts "
                "WHERE run_id = ? AND halt_kind IS NOT NULL",
                (run_id,),
            ).fetchall()
        return frozenset(str(row["endpoint"]) for row in rows)

    def snapshot(self, run_id: str) -> RunSnapshot:
        """Read a run, jobs and attempts from one transaction snapshot."""
        with self.read_transaction() as conn:
            run_row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run_row is None:
                raise UnknownRunError(run_id)
            job_rows = conn.execute(
                "SELECT * FROM jobs WHERE run_id = ? ORDER BY ordinal", (run_id,)
            ).fetchall()
            attempt_rows = conn.execute(
                "SELECT * FROM attempts WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
            run = self._run_record(run_row, job_rows)
            jobs = tuple(_row_to_job(row) for row in job_rows)
            attempts = tuple(_row_to_attempt(row) for row in attempt_rows)
        return RunSnapshot(run=run, jobs=jobs, attempts=attempts)

__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "StoreError",
    "StoreNotFoundError",
    "UnknownRunError",
    "UnknownJobError",
    "DuplicateJobError",
    "TerminalStateError",
    "JobStore",
]
