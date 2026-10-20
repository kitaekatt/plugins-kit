"""Machine-scoped runtime state: ``dashboard.sqlite3``.

Runtime truth (queue entries, runs, interactive holds, events) lives here, in
the dashboard's data directory, and never in a task folder. Task folders stay
the only truth for what a task and its items mean; a run record keeps the task
ref, item id and the source revision it was queued against and never overrides
a changed or missing item.

One server owns the database. Access is serialized by a lock around a single
connection; a project lease is taken in one conditional UPDATE, so two ticks
cannot start two runs in the same project.

Run states: ``queued`` (waiting), ``starting`` / ``running`` / ``cancelling``
(the lease is held; ``cancelling`` is a stop request the runner has not finished
carrying out), then one terminal state: ``accepted``, ``blocked`` (the task
folder now says why), ``failed`` / ``timeout`` / ``cancelled`` (no acceptance
verdict; Retry may follow, and the shared tree may hold partial work),
``crashed`` (crash inspection: the runner vanished, or what it left could not be
trusted; the task is blocked for a person to inspect), ``skipped`` (the task
changed under the queue entry, so it was dropped), ``removed`` (the user took a
queued entry out of the queue before it started; unlike ``cancelled`` it is not
retryable, and the item simply shows Queue again).

The schema stays 1: the new states are values of an existing text column, and
code that predates ``removed`` selects runs only by explicit state lists, so it
never meets one.

Endpoint helper authority lives here too, in two additive tables (``IF NOT EXISTS``; a task-kit that
predates them opens the database, checks only ``meta.schema`` and never reads them). ``helper_grants``
holds, per run, the SHA-256 of the run-scoped token the server issued at dispatch and the helpers
offered to that run (immutable from then on); ``helper_calls`` holds one row per call or refusal and
is the budget the server counts. Nothing the worker can write decides either: a run directory is
never read for them.

Two single transactions carry the guarantee. A TERMINAL ``set_state`` writes the run's state, revokes
its grant and records the event in one ``BEGIN IMMEDIATE``. ``reserve_helper_call`` checks the token,
the revocation, the run's liveness, the offer, the request size and the per-helper budget and inserts
the reservation in one ``BEGIN IMMEDIATE``, under the connection lock. A reservation is therefore
either committed before the revocation or refused as revoked; none lands on a revoked grant.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import proctree

SCHEMA_VERSION = 1
DB_FILE = "dashboard.sqlite3"

QUEUED, STARTING, RUNNING, CANCELLING = "queued", "starting", "running", "cancelling"
ACCEPTED, BLOCKED, FAILED, TIMEOUT, CRASHED, SKIPPED, CANCELLED, REMOVED = (
    "accepted", "blocked", "failed", "timeout", "crashed", "skipped", "cancelled", "removed",
)
ACTIVE_STATES = (STARTING, RUNNING, CANCELLING)
LIVE_STATES = (QUEUED, STARTING, RUNNING, CANCELLING)
TERMINAL_STATES = (ACCEPTED, BLOCKED, FAILED, TIMEOUT, CRASHED, SKIPPED, CANCELLED, REMOVED)
# The terminal states the run history shows: a removed entry never ran, so it is not history.
HISTORY_STATES = tuple(state for state in TERMINAL_STATES if state != REMOVED)
RETRYABLE_STATES = (FAILED, TIMEOUT, CANCELLED)
CANCELLABLE_STATES = (QUEUED, STARTING, RUNNING)

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    project_root TEXT NOT NULL,
    task_ref TEXT NOT NULL,
    item_id TEXT NOT NULL,
    item_title TEXT NOT NULL,
    state TEXT NOT NULL,
    code TEXT,
    detail TEXT,
    options_json TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    run_dir TEXT,
    runner_pid INTEGER,
    runner_ctime REAL,
    retry_of TEXT
);
CREATE INDEX IF NOT EXISTS runs_project_state ON runs (project_id, state);
CREATE TABLE IF NOT EXISTS holds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder TEXT NOT NULL,
    session TEXT NOT NULL,
    pid INTEGER NOT NULL,
    ctime REAL NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (folder, session)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_run ON events (run_id, id);
CREATE TABLE IF NOT EXISTS helper_grants (
    run_id TEXT PRIMARY KEY,
    token_sha256 TEXT NOT NULL UNIQUE,
    offers_json TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS helper_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    helper TEXT NOT NULL,
    state TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    ended_at TEXT,
    entry TEXT,
    status TEXT,
    input_chars INTEGER NOT NULL DEFAULT 0,
    output_chars INTEGER,
    usage_json TEXT,
    reported_cost_usd REAL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS helper_calls_run ON helper_calls (run_id, helper, id);
"""

# Helper call states. The first four consume the helper's budget (a reservation that never ended still
# counts: the call may have reached the endpoint); a refusal does not.
CALL_RESERVED, CALL_COMPLETED, CALL_FAILED, CALL_INTERRUPTED, CALL_REFUSED = (
    "reserved", "completed", "failed", "interrupted", "refused")
CALL_CONSUMING = (CALL_RESERVED, CALL_COMPLETED, CALL_FAILED, CALL_INTERRUPTED)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def precise_now() -> str:
    """A timestamp that orders two writes made within one second (the helper tables compare them)."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def folder_key(folder: str | Path) -> str:
    """The identity a hold and a lookup agree on for a task folder."""
    return os.path.normcase(os.path.realpath(str(folder)))


@dataclass(frozen=True)
class Run:
    seq: int
    id: str
    project_id: str
    project_root: str
    task_ref: str
    item_id: str
    item_title: str
    state: str
    code: str | None
    detail: str | None
    options: dict
    source_revision: str
    queued_at: str
    started_at: str | None
    ended_at: str | None
    run_dir: str | None
    runner_pid: int | None
    runner_ctime: float | None
    retry_of: str | None

    @property
    def folder(self) -> str:
        return str(Path(self.project_root) / self.task_ref)


@dataclass(frozen=True)
class Reservation:
    """A helper call the server authorized and recorded as ``reserved`` (its budget is spent)."""

    call_id: int
    run_id: str
    helper: str
    offer: dict


@dataclass(frozen=True)
class Refusal:
    """A helper call refused. ``status`` is the HTTP status; ``call_id`` names the ``refused`` row (None: no run known)."""

    status: int
    code: str
    message: str
    call_id: int | None = None
    offered: tuple[str, ...] = ()


_COLUMNS = (
    "seq id project_id project_root task_ref item_id item_title state code detail options_json "
    "source_revision queued_at started_at ended_at run_dir runner_pid runner_ctime retry_of"
).split()


def _run(row: sqlite3.Row) -> Run:
    values = dict(zip(_COLUMNS, row))
    values["options"] = json.loads(values.pop("options_json"))
    return Run(**values)


class RuntimeDB:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self._conn.executescript(_DDL)
            row = self._conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
            elif int(row[0]) != SCHEMA_VERSION:
                raise RuntimeError(
                    f"{self.path} has runtime schema {row[0]}, but this task-kit reads schema {SCHEMA_VERSION}; "
                    "it was left alone"
                )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- runs ---------------------------------------------------------------

    def enqueue(self, *, run_id: str, project_id: str, project_root: str, task_ref: str, item_id: str,
                item_title: str, options: dict, source_revision: str, retry_of: str | None = None) -> Run:
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs (id, project_id, project_root, task_ref, item_id, item_title, state, options_json,"
                " source_revision, queued_at, retry_of) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, project_id, project_root, task_ref, item_id, item_title, QUEUED,
                 json.dumps(options, sort_keys=True), source_revision, now(), retry_of),
            )
            self.add_event(run_id, "queued", f"{task_ref} / {item_id}")
            return self.get(run_id)  # type: ignore[return-value]

    def enqueue_many(self, entries: list[dict]) -> list[Run]:
        """Enqueue several runs in one transaction: all of them or none. Each entry takes ``enqueue``'s keywords."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for entry in entries:
                    self.enqueue(**entry)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            return [self.get(entry["run_id"]) for entry in entries]  # type: ignore[misc]

    def set_options(self, run_id: str, options: dict) -> bool:
        """Replace a QUEUED run's options; False when the run is no longer queued (it started or was removed)."""
        with self._lock:
            changed = self._conn.execute("UPDATE runs SET options_json=? WHERE id=? AND state=?",
                                         (json.dumps(options, sort_keys=True), run_id, QUEUED)).rowcount
            if changed:
                self.add_event(run_id, "options", json.dumps(options, sort_keys=True))
            return changed == 1

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            row = self._conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM runs WHERE id=?", (run_id,)).fetchone()
        return _run(row) if row else None

    def runs(self, states: tuple[str, ...] | None = None, *, limit: int | None = None, newest_first: bool = False) -> list[Run]:
        query = f"SELECT {', '.join(_COLUMNS)} FROM runs"
        args: tuple = ()
        if states:
            query += f" WHERE state IN ({','.join('?' * len(states))})"
            args = tuple(states)
        query += " ORDER BY seq " + ("DESC" if newest_first else "ASC")
        if limit:
            query += f" LIMIT {int(limit)}"
        with self._lock:
            return [_run(r) for r in self._conn.execute(query, args).fetchall()]

    def live_for(self, project_id: str, task_ref: str, item_id: str) -> Run | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM runs WHERE project_id=? AND task_ref=? AND item_id=? "
                f"AND state IN ({','.join('?' * len(LIVE_STATES))}) ORDER BY seq LIMIT 1",
                (project_id, task_ref, item_id, *LIVE_STATES),
            ).fetchone()
        return _run(row) if row else None

    def last_started_by_checkout(self) -> dict[str, str]:
        """``{project_id: newest started_at}`` over every run that ever started (no schema change).

        The scheduler orders checkouts by this: the one that started least recently goes first.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT project_id, MAX(started_at) FROM runs WHERE started_at IS NOT NULL GROUP BY project_id"
            ).fetchall()
        return {project_id: started for project_id, started in rows}

    def active_for_folder(self, folder: str) -> Run | None:
        key = folder_key(folder)
        for run in self.runs(ACTIVE_STATES):
            if folder_key(run.folder) == key:
                return run
        return None

    def try_lease(self, run_id: str, project_id: str, *, max_projects: int) -> bool:
        """Move a queued run to ``starting`` if its project holds no lease and a slot is free."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                busy = self._conn.execute(
                    f"SELECT COUNT(DISTINCT project_id) FROM runs WHERE state IN ({','.join('?' * len(ACTIVE_STATES))})",
                    ACTIVE_STATES,
                ).fetchone()[0]
                mine = self._conn.execute(
                    f"SELECT 1 FROM runs WHERE project_id=? AND state IN ({','.join('?' * len(ACTIVE_STATES))}) LIMIT 1",
                    (project_id, *ACTIVE_STATES),
                ).fetchone()
                if mine or busy >= max_projects:
                    self._conn.execute("ROLLBACK")
                    return False
                changed = self._conn.execute(
                    "UPDATE runs SET state=?, started_at=? WHERE id=? AND state=?", (STARTING, now(), run_id, QUEUED)
                ).rowcount
                self._conn.execute("COMMIT")
                return changed == 1
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    @contextlib.contextmanager
    def _transaction(self):
        """One ``BEGIN IMMEDIATE`` ... ``COMMIT`` (``ROLLBACK`` on any error); joins a transaction the caller already
        holds. The caller holds ``self._lock``, so no other thread of this process writes in between."""
        if self._conn.in_transaction:
            yield
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def set_state(self, run_id: str, state: str, *, code: str | None = None, detail: str | None = None,
                  run_dir: str | None = None, runner: proctree.ProcIdent | None = None) -> None:
        """Move a run to ``state``. A TERMINAL state is one transaction: the run's update, the revocation of its
        helper grant and the event, so a helper call can never be authorized after the run ended."""
        terminal = state in TERMINAL_STATES
        with self._lock, (self._transaction() if terminal else contextlib.nullcontext()):
            self._conn.execute(
                "UPDATE runs SET state=?, code=COALESCE(?, code), detail=COALESCE(?, detail),"
                " run_dir=COALESCE(?, run_dir), runner_pid=COALESCE(?, runner_pid), runner_ctime=COALESCE(?, runner_ctime),"
                " ended_at=CASE WHEN ? THEN ? ELSE ended_at END WHERE id=?",
                (state, code, detail, run_dir, runner.pid if runner else None, runner.create_time if runner else None,
                 1 if terminal else 0, now(), run_id),
            )
            if terminal:
                self._revoke_grant(run_id)
            self.add_event(run_id, state, (code + ": " if code else "") + (detail or ""))

    def transition(self, run_id: str, expected: tuple[str, ...], state: str, *, code: str | None = None,
                   detail: str | None = None, runner: proctree.ProcIdent | None = None) -> bool:
        """``set_state`` only if the run is still in one of ``expected`` states; whether it moved."""
        with self._lock:
            current = self.get(run_id)
            if current is None or current.state not in expected:
                return False
            self.set_state(run_id, state, code=code, detail=detail, runner=runner)
            return True

    def release_to_queue(self, run_id: str) -> None:
        with self._lock, self._transaction():
            if self._conn.execute("UPDATE runs SET state=?, started_at=NULL WHERE id=? AND state=?",
                                  (QUEUED, run_id, STARTING)).rowcount:
                self._revoke_grant(run_id)
                self.add_event(run_id, "requeued", "the server stopped before it spawned a runner; the run is queued again")

    # -- endpoint helper grants and calls -------------------------------------

    def issue_helper_grant(self, run_id: str, token_sha256: str, offers: list[dict]) -> None:
        """Record the helpers offered to an ACTIVE run and the hash of its token. The offers never change afterwards.

        A run that was released to the queue and dispatched again gets a fresh token; the earlier one stops working.
        """
        with self._lock, self._transaction():
            row = self._conn.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None or row[0] not in ACTIVE_STATES:
                raise RuntimeError(f"run {run_id} is not active, so it gets no helper grant")
            self._conn.execute(
                "INSERT INTO helper_grants (run_id, token_sha256, offers_json, issued_at) VALUES (?,?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET token_sha256=excluded.token_sha256, offers_json=excluded.offers_json,"
                " issued_at=excluded.issued_at, revoked_at=NULL",
                (run_id, token_sha256, json.dumps(offers, sort_keys=True), precise_now()),
            )

    def _revoke_grant(self, run_id: str) -> None:
        self._conn.execute("UPDATE helper_grants SET revoked_at=? WHERE run_id=? AND revoked_at IS NULL", (precise_now(), run_id))

    def revoke_helper_grant(self, run_id: str) -> None:
        with self._lock, self._transaction():
            self._revoke_grant(run_id)

    def helper_grant(self, run_id: str) -> dict | None:
        """``{"offers", "issued_at", "revoked_at"}`` for a run's grant, or None when it was offered no helper."""
        with self._lock:
            row = self._conn.execute("SELECT offers_json, issued_at, revoked_at FROM helper_grants WHERE run_id=?", (run_id,)).fetchone()
        return None if row is None else {"offers": json.loads(row[0]), "issued_at": row[1], "revoked_at": row[2]}

    def _insert_call(self, run_id: str, helper: str, state: str, *, input_chars: int, detail: str | None = None) -> int:
        cursor = self._conn.execute(
            "INSERT INTO helper_calls (run_id, helper, state, reserved_at, input_chars, detail) VALUES (?,?,?,?,?,?)",
            (run_id, helper, state, precise_now(), input_chars, detail),
        )
        return int(cursor.lastrowid)

    def _helper_budget_used(self, run_id: str, helper: str) -> int:
        """How many of ``helper``'s calls this run has spent (a reservation counts until proven otherwise)."""
        return int(self._conn.execute(
            f"SELECT COUNT(*) FROM helper_calls WHERE run_id=? AND helper=? AND state IN ({','.join('?' * len(CALL_CONSUMING))})",
            (run_id, helper, *CALL_CONSUMING),
        ).fetchone()[0])

    def reserve_helper_call(self, token_sha256: str, helper: str, request_chars: int, *,
                            problem: tuple[str, str] | None = None) -> "Reservation | Refusal":
        """Authorize one helper call and record it, in ONE transaction under the connection lock.

        In order: the token's grant (401), not revoked (410), the grant's run is still active (410), the helper
        was offered to this run (404), the request is usable (422; ``problem`` is the ``(code, message)`` of an empty
        or malformed body), its size (input plus system characters) is within the offer's limit (422), and the run
        has calls left for this helper (429). Then the ``reserved``
        row is inserted. Every refusal after the grant was found is recorded as a ``refused`` row. Because the
        terminal ``set_state`` revokes in a transaction of its own under the same lock, a reservation is either
        committed before the revocation or refused 410.
        """
        with self._lock, self._transaction():
            grant = self._conn.execute(
                "SELECT run_id, offers_json, revoked_at FROM helper_grants WHERE token_sha256=?", (token_sha256,)).fetchone()
            if grant is None:
                return Refusal(401, "unknown-token", "the run token is not known to this dashboard")
            run_id, offers_json, revoked_at = grant
            offers = json.loads(offers_json)

            def refuse(status: int, code: str, message: str, offered: tuple[str, ...] = ()) -> Refusal:
                call_id = self._insert_call(run_id, helper, CALL_REFUSED, input_chars=request_chars, detail=f"{code}: {message}")
                return Refusal(status, code, message, call_id, offered)

            if revoked_at is not None:
                return refuse(410, "revoked", "this run has ended, so its helper access was revoked")
            state = self._conn.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
            if state is None or state[0] not in ACTIVE_STATES:
                return refuse(410, "revoked", "this run is not active, so it may not call a helper")
            offer = next((o for o in offers if o.get("name") == helper), None)
            if offer is None:
                names = tuple(o.get("name", "") for o in offers)
                return refuse(404, "not-offered", f"helper {helper!r} is not offered to this run; offered: {', '.join(names) or 'none'}", names)
            if problem is not None:
                return refuse(422, problem[0], problem[1])
            if request_chars > int(offer["max_input_chars"]):
                return refuse(422, "too-large", f"input plus system is {request_chars} characters; this helper accepts {offer['max_input_chars']}")
            used = self._helper_budget_used(run_id, helper)
            if used >= int(offer["max_calls"]):
                return refuse(429, "budget-spent", f"{used} of {offer['max_calls']} calls for {helper} are spent in this run")
            return Reservation(self._insert_call(run_id, helper, CALL_RESERVED, input_chars=request_chars), run_id, helper, offer)

    def end_helper_call(self, call_id: int, *, state: str, entry: str | None = None, status: str | None = None,
                        output_chars: int | None = None, usage: dict | None = None, reported_cost_usd: float | None = None,
                        detail: str | None = None) -> None:
        """Record how a reserved call ended (``completed`` or ``failed``)."""
        if state not in (CALL_COMPLETED, CALL_FAILED):
            raise ValueError(f"a call ends completed or failed, not {state!r}")
        with self._lock:
            self._conn.execute(
                "UPDATE helper_calls SET state=?, ended_at=?, entry=?, status=?, output_chars=?, usage_json=?, reported_cost_usd=?,"
                " detail=COALESCE(?, detail) WHERE id=?",
                (state, precise_now(), entry, status, output_chars, json.dumps(usage, sort_keys=True) if usage else None,
                 reported_cost_usd, detail, call_id),
            )

    def helper_calls(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, helper, state, reserved_at, ended_at, entry, status, input_chars, output_chars, usage_json,"
                " reported_cost_usd, detail FROM helper_calls WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        keys = ("id", "helper", "state", "reserved_at", "ended_at", "entry", "status", "input_chars", "output_chars", "usage",
                "reported_cost_usd", "detail")
        out = []
        for row in rows:
            record = dict(zip(keys, row))
            record["usage"] = json.loads(record["usage"]) if record["usage"] else None
            out.append(record)
        return out

    def interrupt_open_calls(self) -> int:
        """On server start: a call still ``reserved`` lost its server, so it is ``interrupted`` (and still counts)."""
        with self._lock:
            return self._conn.execute(
                "UPDATE helper_calls SET state=?, ended_at=?, detail=COALESCE(detail, 'the server stopped while this call was in flight') "
                "WHERE state=?", (CALL_INTERRUPTED, precise_now(), CALL_RESERVED)).rowcount

    # -- events -------------------------------------------------------------

    def add_event(self, run_id: str, kind: str, detail: str) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO events (run_id, at, kind, detail) VALUES (?,?,?,?)", (run_id, now(), kind, detail))

    def events(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT at, kind, detail FROM events WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        return [{"at": a, "kind": k, "detail": d} for a, k, d in rows]

    def last_event_kind(self, run_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT kind, detail FROM events WHERE run_id=? ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
        return f"{row[0]}|{row[1]}" if row else None

    # -- holds --------------------------------------------------------------

    def add_hold(self, folder: str, session: str, ident: proctree.ProcIdent) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO holds (folder, session, pid, ctime, created_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(folder, session) DO UPDATE SET pid=excluded.pid, ctime=excluded.ctime",
                (folder_key(folder), session, ident.pid, ident.create_time, now()),
            )

    def live_holds(self, folder: str) -> list[dict]:
        """Holds on ``folder`` whose Claude session process is still the same process.

        A hold ends with its session: a dead or reused PID deletes the row.
        """
        key = folder_key(folder)
        with self._lock:
            rows = self._conn.execute("SELECT id, session, pid, ctime, created_at FROM holds WHERE folder=?", (key,)).fetchall()
            alive = []
            for hold_id, session, pid, ctime, created in rows:
                if proctree.live(proctree.ProcIdent(pid, ctime)) is None:
                    self._conn.execute("DELETE FROM holds WHERE id=?", (hold_id,))
                else:
                    alive.append({"session": session, "pid": pid, "created_at": created})
        return alive

    def held_folders(self) -> set[str]:
        """The ``folder_key`` of every folder with a live hold (dead holds are deleted, as in ``live_holds``)."""
        with self._lock:
            rows = self._conn.execute("SELECT id, folder, pid, ctime FROM holds").fetchall()
            held = set()
            for hold_id, folder, pid, ctime in rows:
                if proctree.live(proctree.ProcIdent(pid, ctime)) is None:
                    self._conn.execute("DELETE FROM holds WHERE id=?", (hold_id,))
                else:
                    held.add(folder)
        return held

    def clear_holds(self, folder: str) -> int:
        with self._lock:
            return self._conn.execute("DELETE FROM holds WHERE folder=?", (folder_key(folder),)).rowcount
