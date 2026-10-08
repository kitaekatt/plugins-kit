"""Per-unit deadlines, the run-wide deadline circuit, and deadline notes.

Standard library only. Three independent pieces a consumer adopts one at a
time:

- :class:`UnitBudget` -- a thread-safe wall-time budget for ONE unit, shared
  by every call the unit makes (a first attempt and its repairs spend the
  same budget). :func:`call_within_budget` runs one admitted call under it:
  the call's timeout is the smaller of its own timeout and what remains, the
  wall time from dispatch to return is charged (success or failure), and a
  timeout that the budget, not the call's own timeout, imposed is re-raised
  as :class:`DeadlineExpired`. A call made with no budget left raises
  :class:`DeadlineExpired` without dispatching.
- :class:`DeadlineCircuit` -- sticky and run-wide. The consumer records each
  unit's TERMINAL outcome (did the unit end on a deadline or not); after
  ``limit`` consecutive deadline outcomes it opens and stays open, and
  :func:`call_within_budget` refuses every later budgeted call with
  :class:`CircuitOpen` before dispatch. Any other outcome resets the count.
  A deadline is a property of the endpoint as much as the unit, so a run of
  them means the endpoint, not the units, is the problem.
- :class:`DeadlineNotes` -- an external, atomically written JSON file that
  counts deadline expiries per attempt key. A note is diagnostic and ordering
  state: it never suppresses an attempt. A consumer typically marks a noted
  unit ``deferred`` so it runs after units expected to finish
  (:class:`~content_pipeline.execution.scheduler.SchedulerNode`,
  :class:`~content_pipeline.execution.parallel_graph.ParallelGraphStrategy`).
  A changed attempt key starts with no note.

A deadline or open-circuit result is transient: a consumer must not record it
in the negative failure cache
(:mod:`content_pipeline.execution.failure_cache`).
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional, TypeVar, Union

from content_pipeline.execution import _atomic_json

T = TypeVar("T")

SCHEMA_VERSION = 1
DEFAULT_CIRCUIT_LIMIT = 2
_NOTE_FIELDS = ("subject", "expiries", "last_run_id")
_STORE_LOCK = threading.RLock()


class DeadlineExpired(Exception):
    """The unit's budget ran out before or during a call."""


class CircuitOpen(Exception):
    """The run-wide deadline circuit is open; no budgeted call is dispatched."""


class UnitBudget:
    """Thread-safe wall-time budget for one unit, shared across its calls."""

    def __init__(self, seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        if seconds <= 0:
            raise ValueError("unit budget must be positive")
        self.seconds = float(seconds)
        self.clock = clock
        self._spent = 0.0
        self._lock = threading.Lock()

    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self.seconds - self._spent)

    def charge(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("unit budget charge must not be negative")
        with self._lock:
            self._spent += seconds

    @property
    def spent(self) -> float:
        with self._lock:
            return self._spent


class DeadlineCircuit:
    """Sticky run-wide circuit, opened by consecutive terminal deadline outcomes."""

    def __init__(self, limit: int = DEFAULT_CIRCUIT_LIMIT) -> None:
        if limit <= 0:
            raise ValueError("deadline circuit limit must be positive")
        self.limit = limit
        self._consecutive = 0
        self._open = False
        self._lock = threading.Lock()

    def is_open(self) -> bool:
        with self._lock:
            return self._open

    def record_outcome(self, deadline_expired: bool) -> bool:
        """Record one unit's terminal outcome; return whether the circuit is open."""
        with self._lock:
            if self._open:
                return True
            if deadline_expired:
                self._consecutive += 1
                if self._consecutive >= self.limit:
                    self._open = True
            else:
                self._consecutive = 0
            return self._open

    @property
    def consecutive_deadlines(self) -> int:
        with self._lock:
            return self._consecutive


def call_within_budget(
    budget: UnitBudget,
    timeout_seconds: float,
    call: Callable[[float], T],
    *,
    is_timeout: Callable[[BaseException], bool],
    circuit: Optional[DeadlineCircuit] = None,
    uncharged: Callable[[BaseException], bool] = lambda exc: False,
) -> T:
    """Run ``call(effective_timeout)`` under ``budget`` and return its result.

    Call this once per ADMITTED call -- after any concurrency gate has let it
    through -- so time spent queued or backing off is not charged.
    ``effective_timeout`` is ``min(timeout_seconds, budget.remaining())``.
    The wall time from dispatch to return is charged to the budget whether
    the call returns or raises, except for an exception ``uncharged`` accepts
    (an endpoint refusing admission, say, which the caller retries). An
    exception ``is_timeout`` accepts is re-raised as :class:`DeadlineExpired`
    when the budget was the binding limit, and unchanged when the call's own
    timeout was. Raises :class:`CircuitOpen` without dispatching when
    ``circuit`` is open, and :class:`DeadlineExpired` when nothing remains.
    """
    if circuit is not None and circuit.is_open():
        raise CircuitOpen("the deadline circuit opened after consecutive unit deadlines")
    remaining = budget.remaining()
    if remaining <= 0:
        raise DeadlineExpired("the unit budget is exhausted")
    budget_limited = remaining <= timeout_seconds
    dispatched = budget.clock()
    try:
        result = call(min(timeout_seconds, remaining))
    except BaseException as exc:
        if not uncharged(exc):
            budget.charge(max(0.0, budget.clock() - dispatched))
        if budget_limited and is_timeout(exc):
            raise DeadlineExpired(
                "the unit budget expired after %.1fs" % budget.spent
            ) from exc
        raise
    budget.charge(max(0.0, budget.clock() - dispatched))
    return result


@dataclass(frozen=True)
class DeadlineNote:
    subject: str
    expiries: int
    last_run_id: str


class DeadlineNotes:
    """Read and atomically update a schema-v1 deadline-notes file.

    Keyed by the consumer's attempt key; ``subject`` names the unit for a
    reader. A missing file is empty. An unreadable or malformed file is read
    as empty and :attr:`report` says why: the notes only order work, so a bad
    file must not stop a run. The next write replaces it.

    Home: the consumer supplies ``path``; nothing is derived from this
    module's location, and the atomic write puts its temporary file in the
    target's directory. The notes describe the consuming project and only
    order work, so they are PROJECT-EPHEMERAL (``.local-data/<plugin>/``);
    losing them costs ordering, not correctness.
    """

    def __init__(self, path: Union[Path, str]) -> None:
        self.path = Path(path)
        self.report: Optional[str] = None

    def lookup(self, attempt_key: str) -> Optional[DeadlineNote]:
        with _STORE_LOCK:
            value = self._load()["notes"].get(attempt_key)
            return None if value is None else DeadlineNote(**value)

    def record_expiry(self, attempt_key: str, subject: str, last_run_id: str) -> DeadlineNote:
        with _STORE_LOCK:
            payload = self._load()
            previous = payload["notes"].get(attempt_key)
            note = DeadlineNote(
                subject=subject,
                expiries=1 if previous is None else previous["expiries"] + 1,
                last_run_id=last_run_id,
            )
            _validate_note(note)
            payload["notes"][attempt_key] = asdict(note)
            _atomic_json.write(self.path, payload)
            return note

    def clear(self, attempt_key: str) -> None:
        with _STORE_LOCK:
            payload = self._load()
            if payload["notes"].pop(attempt_key, None) is not None:
                _atomic_json.write(self.path, payload)

    def _load(self) -> dict:
        empty = {"schema_version": SCHEMA_VERSION, "notes": {}}
        payload, self.report = _atomic_json.read(
            self.path, empty, _validate_payload, "deadline notes"
        )
        return payload


def _validate_note(note: DeadlineNote) -> None:
    if not isinstance(note.subject, str) or not isinstance(note.last_run_id, str):
        raise TypeError("deadline note strings are invalid")
    if type(note.expiries) is not int or note.expiries <= 0:
        raise TypeError("deadline note expiries must be a positive integer")


def _validate_payload(payload: object) -> None:
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("wrong deadline-notes schema")
    notes = payload.get("notes")
    if not isinstance(notes, dict):
        raise ValueError("invalid deadline-note entries")
    for key, value in notes.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            raise ValueError("invalid deadline-note entry")
        if set(value) != set(_NOTE_FIELDS):
            raise ValueError("invalid deadline-note fields")
        _validate_note(DeadlineNote(**value))


__all__ = [
    "DeadlineExpired",
    "CircuitOpen",
    "UnitBudget",
    "DeadlineCircuit",
    "call_within_budget",
    "DeadlineNote",
    "DeadlineNotes",
    "DEFAULT_CIRCUIT_LIMIT",
]
