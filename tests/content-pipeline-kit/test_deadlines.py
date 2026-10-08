"""Per-unit budgets, the run-wide deadline circuit, and deadline notes.

Generalized from a consumer's tested generation-budget suite: the budget
cases there ran through that consumer's concurrency gate; here they run
through ``call_within_budget``, the per-admitted-call piece the gate used.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from content_pipeline.execution import deadlines
from content_pipeline.execution.deadlines import (
    CircuitOpen,
    DeadlineCircuit,
    DeadlineExpired,
    DeadlineNotes,
    UnitBudget,
    call_within_budget,
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CallTimeout(Exception):
    """A transport's own timeout, as a consumer's call would raise it."""


class Refused(Exception):
    """An endpoint refusing admission; the consumer retries it."""


def is_timeout(exc):
    return isinstance(exc, CallTimeout)


def test_the_call_charges_wall_time_from_dispatch():
    clock = Clock()
    budget = UnitBudget(10, clock=clock)

    def call(timeout):
        clock.advance(3.25)
        return "answer"

    assert call_within_budget(budget, 30, call, is_timeout=is_timeout) == "answer"
    assert budget.spent == 3.25
    assert budget.remaining() == 6.75


def test_each_call_gets_the_smaller_of_its_timeout_and_the_remaining_budget():
    clock = Clock()
    seen = []
    first = UnitBudget(10, clock=clock)
    first.charge(7)

    call_within_budget(first, 5, lambda t: seen.append(t), is_timeout=is_timeout)
    call_within_budget(UnitBudget(10, clock=clock), 4, lambda t: seen.append(t), is_timeout=is_timeout)

    assert seen == [3.0, 4]


def test_an_uncharged_refusal_does_not_spend_the_budget():
    clock = Clock()
    budget = UnitBudget(20, clock=clock)

    def refused(timeout):
        clock.advance(2)
        raise Refused("queue full")

    with pytest.raises(Refused):
        call_within_budget(
            budget, 30, refused, is_timeout=is_timeout,
            uncharged=lambda exc: isinstance(exc, Refused),
        )
    clock.advance(4)  # the consumer's backoff

    def answered(timeout):
        clock.advance(3)
        return "answer"

    assert call_within_budget(budget, 30, answered, is_timeout=is_timeout) == "answer"
    assert budget.spent == 3


def test_a_deadline_during_a_repair_counts_once_for_the_unit():
    clock = Clock()
    budget = UnitBudget(5, clock=clock)
    circuit = DeadlineCircuit()

    def first(timeout):
        clock.advance(4)
        return "invalid answer"

    def repair(timeout):
        clock.advance(timeout)
        raise CallTimeout("repair timed out")

    assert call_within_budget(budget, 30, first, is_timeout=is_timeout, circuit=circuit) == "invalid answer"
    with pytest.raises(DeadlineExpired) as caught:
        call_within_budget(budget, 30, repair, is_timeout=is_timeout, circuit=circuit)
    assert isinstance(caught.value.__cause__, CallTimeout)

    circuit.record_outcome(deadline_expired=True)
    assert circuit.consecutive_deadlines == 1
    assert not circuit.is_open()


def test_a_call_timeout_before_the_budget_runs_out_stays_an_ordinary_timeout():
    clock = Clock()
    budget = UnitBudget(30, clock=clock)

    def call(timeout):
        clock.advance(timeout)
        raise CallTimeout("request timed out")

    with pytest.raises(CallTimeout):
        call_within_budget(budget, 5, call, is_timeout=is_timeout)
    assert budget.spent == 5


def test_an_exhausted_budget_refuses_before_dispatch():
    budget = UnitBudget(1, clock=Clock())
    budget.charge(1)

    def call(timeout):  # pragma: no cover - must not run
        raise AssertionError("dispatched with no budget left")

    with pytest.raises(DeadlineExpired):
        call_within_budget(budget, 30, call, is_timeout=is_timeout)


def test_an_open_circuit_refuses_before_dispatch():
    circuit = DeadlineCircuit(limit=1)
    circuit.record_outcome(deadline_expired=True)

    def call(timeout):  # pragma: no cover - must not run
        raise AssertionError("dispatched with the circuit open")

    with pytest.raises(CircuitOpen):
        call_within_budget(UnitBudget(10), 30, call, is_timeout=is_timeout, circuit=circuit)


def test_consecutive_deadlines_open_the_circuit_and_it_stays_open():
    circuit = DeadlineCircuit()

    assert circuit.record_outcome(deadline_expired=True) is False
    assert circuit.record_outcome(deadline_expired=True) is True
    assert circuit.record_outcome(deadline_expired=False) is True
    assert circuit.is_open()


def test_an_interleaved_other_outcome_resets_the_count():
    circuit = DeadlineCircuit()

    circuit.record_outcome(deadline_expired=True)
    circuit.record_outcome(deadline_expired=False)
    circuit.record_outcome(deadline_expired=True)

    assert circuit.consecutive_deadlines == 1
    assert not circuit.is_open()


def test_budget_and_circuit_refuse_bad_settings():
    with pytest.raises(ValueError):
        UnitBudget(0)
    with pytest.raises(ValueError):
        UnitBudget(1).charge(-1)
    with pytest.raises(ValueError):
        DeadlineCircuit(limit=0)


def test_deadline_notes_are_atomic_ascii_schema_v1_and_thread_safe(tmp_path: Path):
    path = tmp_path / "deadline-notes.json"
    notes = DeadlineNotes(path)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: notes.record_expiry("attempt-%d" % i, "unit-%d" % i, "run"), range(8)))

    payload = json.loads(path.read_bytes().decode("ascii"))
    assert payload["schema_version"] == deadlines.SCHEMA_VERSION == 1
    assert len(payload["notes"]) == 8
    assert set(payload["notes"]["attempt-0"]) == {"subject", "expiries", "last_run_id"}
    assert not list(tmp_path.glob(".deadline-notes.json.*")), "temporary file left behind"


def test_a_changed_attempt_key_does_not_inherit_a_note(tmp_path: Path):
    notes = DeadlineNotes(tmp_path / "deadline-notes.json")
    first = notes.record_expiry("old", "unit", "run-1")
    second = notes.record_expiry("old", "unit", "run-2")

    assert first.expiries == 1 and second.expiries == 2
    assert notes.lookup("new") is None
    assert notes.lookup("old") == second


def test_clear_removes_one_note(tmp_path: Path):
    notes = DeadlineNotes(tmp_path / "deadline-notes.json")
    notes.record_expiry("a", "unit-a", "run")
    notes.record_expiry("b", "unit-b", "run")

    notes.clear("a")

    assert notes.lookup("a") is None
    assert notes.lookup("b").subject == "unit-b"


def test_a_corrupt_notes_file_reads_as_empty_and_reports(tmp_path: Path):
    path = tmp_path / "deadline-notes.json"
    path.write_text('{"schema_version": 1, "notes": {"k": {"subject": 3}}}', encoding="ascii")
    notes = DeadlineNotes(path)

    assert notes.lookup("k") is None
    assert "deadline notes ignored" in notes.report
