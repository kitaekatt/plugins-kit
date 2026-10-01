"""SL-2: the halt surface -- `halt`, `resume`, `check_halted`, and status under writers.

Design: `dev/tasks/loc-pipeline-consumer-needs/design-cross-process-spend-ledger.md`
sections 6 (halt and its latency bound) and 7 (the status view and the
partition).

Every load-bearing assertion here was shown RED by reverting the production line
it protects (docs/reference/vacuous-checks.md); each revert is named in the
test's docstring so the counterfactual can be re-run. Where the property is the
ABSENCE of behaviour -- settle never refusing while halted, no poller -- the
revert INSERTS the forbidden code instead of removing code, because there is no
line to take away.

Scope boundaries, so a reader does not look here for them: the ledger core
(schema, admission, settle/release, the partition with `leaks` empty) is SL-1's
`test_llm_spend_ledger.py`; leases, `reclaim_orphans` and `leaks` WRITES are
SL-3's; the cross-PROCESS halt acceptance test (halt in process B, zero grants
in process A), the busy-timeout exhaustion test and the `BEGIN IMMEDIATE` source
guard are SL-4's. This file's one multi-process test is the status-under-live-
writers case of section 7, which belongs to the halt unit because `status` is
also the only non-raising way to read a halt.

The halt surface is three METHODS on `SpendLedger`, so the module's `__all__` is
unchanged and `contract/public-surface.json` needs no new entry; the contract
guard reads `__all__` only. A module-level name added by a later unit does need
one.
"""

import ast
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from content_pipeline.llm import spend_ledger as sl
from content_pipeline.llm.platform import BudgetExceededError

MODULE_PATH = Path(sl.__file__)
LIB_ROOT = str(
    Path(__file__).resolve().parents[2] / "plugins" / "content-pipeline-kit" / "lib"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make(tmp_path, *, cap_usd=1.0, run_id="run-1", name="ledger.sqlite", **kwargs):
    return sl.create_ledger(tmp_path / name, cap_usd=cap_usd, run_id=run_id, **kwargs)


def raw(ledger):
    """A direct connection, for reading state the public API does not expose."""
    conn = sqlite3.connect(str(ledger.path))
    conn.row_factory = sqlite3.Row
    return conn


def halt_row(ledger):
    conn = raw(ledger)
    try:
        return dict(
            conn.execute("SELECT halted, reason, detail, since FROM halt WHERE id = 1").fetchone()
        )
    finally:
        conn.close()


def history(ledger):
    conn = raw(ledger)
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT action, reason, detail, forced, at FROM halt_history ORDER BY seq"
            )
        ]
    finally:
        conn.close()


def method_source(name):
    """The AST of one `SpendLedger` method, for the source-level guards."""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "SpendLedger":
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name:
                    return item
    raise AssertionError(f"SpendLedger.{name} not found")


# ---------------------------------------------------------------------------
# halt
# ---------------------------------------------------------------------------


class TestHalt:
    def test_halt_sets_the_control_row_and_logs_it(self, tmp_path):
        """Section 6: a one-row control table written under IMMEDIATE, plus an
        append-only `halt_history`.

        Revert shown red: deleted the `_record_halt(...)` call from `halt`, so
        `halt` became a validated no-op transaction -> this test failed on
        `assert row["halted"] == 1` with `assert 0 == 1`. Restored.
        """
        ledger = make(tmp_path)
        ledger.halt("operator", "stopping the run by hand")
        row = halt_row(ledger)
        assert row["halted"] == 1
        assert row["reason"] == "operator"
        assert row["detail"] == "stopping the run by hand"
        assert row["since"]
        entries = history(ledger)
        assert [(e["action"], e["reason"], e["forced"]) for e in entries] == [
            ("halt", "operator", 0)
        ]

    def test_halt_needs_no_detail(self, tmp_path):
        ledger = make(tmp_path)
        ledger.halt("operator")
        assert halt_row(ledger)["detail"] == ""

    def test_halt_refuses_an_empty_reason(self, tmp_path):
        """An unexplainable halt is worse than none: `status()` and the refusal
        exception both carry the reason, and a consumer acts on it.

        Revert shown red: deleted the `if not reason: raise ValueError(...)`
        guard -> this test failed with `DID NOT RAISE <class 'ValueError'>` and
        the ledger halted with an empty reason. Restored.
        """
        ledger = make(tmp_path)
        with pytest.raises(ValueError):
            ledger.halt("")
        assert halt_row(ledger)["halted"] == 0
        # The refusal admits nothing and changes nothing: the ledger still works.
        ledger.reserve(0.1)

    def test_halt_is_visible_to_status_without_raising(self, tmp_path):
        """`status()` is the only way to READ a halt without obeying it, which is
        what lets an operator inspect one.

        Revert shown red: hardcoded `halted=False` in the `SpendStatus`
        `status()` returns -> this test failed with `assert False is True`.
        Restored.
        """
        ledger = make(tmp_path)
        ledger.halt("overspend-suspected", "two runs on one key")
        status = ledger.status()
        assert status.halted is True
        assert status.halt_reason == "overspend-suspected"
        assert status.halt_detail == "two runs on one key"

    def test_a_second_halt_records_both_causes(self, tmp_path):
        """The control row holds the latest reason; the history keeps the first,
        so a second cause never erases the record of the first.

        Revert shown red: made `_record_halt` skip its `halt_history` INSERT when
        the row already says halted -> this test failed with
        `assert [] == ['first', 'second']`. Restored.
        """
        ledger = make(tmp_path)
        ledger.halt("first", "d1")
        ledger.halt("second", "d2")
        assert halt_row(ledger)["reason"] == "second"
        assert [e["reason"] for e in history(ledger)] == ["first", "second"]

    def test_halt_is_fail_closed_under_lock_contention(self, tmp_path):
        """`halt` propagates `sqlite3.OperationalError` and sets NOTHING when the
        write lock cannot be taken within `busy_timeout_ms`: a halt that silently
        failed would be the worst outcome in the module, since the caller would
        believe spending had stopped.

        This test does NOT carry "the halt row is written under BEGIN
        IMMEDIATE". That revert was run and left this test GREEN: changing `halt`
        to use the deferred `self._reader()` still raises
        `sqlite3.OperationalError` -- the UPDATE inside the deferred transaction
        simply takes the write lock later and fails there instead. Deferred and
        IMMEDIATE are indistinguishable from the outside here, which is section
        11.1's point: the `BEGIN IMMEDIATE` property has ONE carrier, SL-4's AST
        source guard, and no runtime test can take it.

        Revert shown red: wrapped `halt`'s `with self._writer() as conn:` body in
        `try: ... except sqlite3.OperationalError: pass` -> this test failed with
        `DID NOT RAISE <class 'sqlite3.OperationalError'>`. Restored.
        """
        ledger = make(tmp_path, busy_timeout_ms=60)
        blocker = sqlite3.connect(str(ledger.path), timeout=0.06)
        try:
            blocker.execute("PRAGMA busy_timeout = 60")
            blocker.execute("BEGIN IMMEDIATE")
            blocker.execute(
                "INSERT INTO halt_history (action, reason, detail, forced, at) "
                "VALUES ('probe', NULL, NULL, 0, 'now')"
            )
            with pytest.raises(sqlite3.OperationalError):
                ledger.halt("operator")
        finally:
            blocker.rollback()
            blocker.close()
        assert halt_row(ledger)["halted"] == 0


# ---------------------------------------------------------------------------
# reserve under a halt -- the enforcement point
# ---------------------------------------------------------------------------


class TestReserveUnderHalt:
    def test_reserve_refuses_while_halted(self, tmp_path):
        """Section 6: every `reserve` reads the halt row inside its own
        `BEGIN IMMEDIATE` transaction and raises `SpendLedgerHalted`. This is the
        whole enforcement mechanism -- nothing else stops a process.

        Revert shown red: deleted the `if int(halt["halted"]): raise
        SpendLedgerHalted(...)` block from `reserve` -> this test failed with
        `DID NOT RAISE <class 'SpendLedgerHalted'>` and the halted ledger
        admitted the reservation (requests became 1). Restored.
        """
        ledger = make(tmp_path, cap_usd=10.0)
        ledger.halt("operator", "halt detail")
        with pytest.raises(sl.SpendLedgerHalted) as exc:
            ledger.reserve(0.5, identifier="unit-3", model="m-1")
        assert exc.value.reason == "operator"
        assert exc.value.detail == "halt detail"
        assert exc.value.identifier == "unit-3"
        assert exc.value.model == "m-1"
        assert exc.value.budget == 10.0
        # Fail-closed: nothing admitted.
        assert ledger.status().requests == 0

    def test_the_refusal_is_a_budget_exceeded_error(self, tmp_path):
        """An existing `except BudgetExceededError` must catch a halt refusal, or
        a consumer upgrading to the ledger silently loses its stop path."""
        ledger = make(tmp_path)
        ledger.halt("operator")
        with pytest.raises(BudgetExceededError):
            ledger.reserve(0.1)

    def test_a_halt_refuses_even_a_reservation_that_fits(self, tmp_path):
        """The halt is not a cap verdict: headroom is irrelevant to it."""
        ledger = make(tmp_path, cap_usd=100.0)
        ledger.halt("operator")
        assert ledger.status().remaining_usd == 100.0
        with pytest.raises(sl.SpendLedgerHalted):
            ledger.reserve(0.000001)

    def test_a_separate_handle_observes_the_halt(self, tmp_path):
        """The halt lives in the FILE, not in the handle that set it, so a second
        handle over the same file is refused without being told anything.
        (SL-4 carries the same property across OS processes.)

        Revert shown red: changed `reserve`'s gate to read an in-memory
        `self._halted` flag set by `halt()` instead of the halt row -> this test
        failed with `DID NOT RAISE <class
        'content_pipeline.llm.spend_ledger.SpendLedgerHalted'>`, i.e. the halt
        bound only the handle that set it. Restored.
        """
        ledger = make(tmp_path, cap_usd=10.0)
        other = sl.open_ledger(ledger.path)
        ledger.halt("operator", "set through the first handle")
        with pytest.raises(sl.SpendLedgerHalted) as exc:
            other.reserve(0.5)
        assert exc.value.reason == "operator"
        assert other.status().halted is True


# ---------------------------------------------------------------------------
# settle under a halt -- the one thing a halt must NOT stop
# ---------------------------------------------------------------------------


class TestSettleUnderHalt:
    def test_settle_never_refuses_while_halted(self, tmp_path):
        """Section 6: `settle` never refuses -- money already spent must be
        recorded. A halted ledger that dropped settles would under-report real
        spend, which is the opposite of what a halt is for.

        Revert shown red (an INSERTION, because the property is an absence):
        added, as the first statement inside `settle`'s `_writer()` block,

            if int(self._read_halt(conn)["halted"]):
                raise SpendLedgerHalted(identifier="", measured=0.0, budget=self.cap_usd)

        -> this test failed with `SpendLedgerHalted: budget exceeded for '':
        measured 0.0 > budget 10.0` raised out of
        `ledger.settle(reservation, 0.25)`, and the 0.25 never reached the
        ledger. It took the next two tests in this class down with it and left
        `test_release_still_works_while_halted` green, because the inserted gate
        was in `settle` only -- that test carries its own revert. Removed again.
        """
        ledger = make(tmp_path, cap_usd=10.0)
        reservation = ledger.reserve(0.5)
        ledger.halt("operator", "halted while the attempt was in flight")
        ledger.settle(reservation, 0.25)
        status = ledger.status()
        assert status.settled_usd == 0.25
        assert status.calls_settled == 1
        assert status.reservations_open == 0
        assert status.halted is True

    def test_an_unreadable_cost_is_still_held_while_halted(self, tmp_path):
        """`settle(None)` holds the row as `unknown` at its reserved amount
        whether or not the ledger is halted. Shown red by the same inserted
        settle-side halt gate as the test above (`SpendLedgerHalted` raised out of
        `ledger.settle(reservation, None)`)."""
        ledger = make(tmp_path, cap_usd=10.0)
        reservation = ledger.reserve(0.5)
        ledger.halt("operator")
        ledger.settle(reservation, None)
        status = ledger.status()
        assert status.unknown_usd == 0.5
        assert status.outstanding_usd == 0.5

    def test_release_still_works_while_halted(self, tmp_path):
        """`release` is the caller stating no money was spent; a halt has no
        reason to refuse the correction, and refusing it would leave headroom
        permanently consumed by an attempt that cost nothing.

        Revert shown red (an INSERTION): added the same halt gate to `release`'s
        `_writer()` block -> this test failed with `SpendLedgerHalted: budget
        exceeded for '': measured 0.0 > budget 10.0` raised out of
        `ledger.release(reservation)`. Removed again.
        """
        ledger = make(tmp_path, cap_usd=10.0)
        reservation = ledger.reserve(0.5)
        ledger.halt("operator")
        ledger.release(reservation)
        assert ledger.status().outstanding_usd == 0.0

    def test_the_latency_bound_is_one_attempt_not_zero(self, tmp_path):
        """Section 6's bound, as the code realizes it: a process observes the
        halt at its NEXT `reserve`, so the attempt already in flight when the
        halt commits runs to completion and charges the cap. The bound is one
        attempt's duration plus `busy_timeout_ms`; it is not zero, and no poller
        or signal exists to make it zero.

        This is the bound stated as a behaviour rather than as a duration: the
        in-flight reservation settles (it is not cancelled), and the attempt
        AFTER it is refused.

        Revert shown red: BOTH halves fail. The inserted settle-side halt gate
        (see `test_settle_never_refuses_while_halted`) made this fail with
        `SpendLedgerHalted` out of `ledger.settle(in_flight, 0.5)`, which is the
        bound collapsing to zero by cancelling in-flight work; deleting
        `reserve`'s halt gate made it fail with `DID NOT RAISE` on the trailing
        reserve, which is the bound becoming unbounded. Both restored.
        """
        ledger = make(tmp_path, cap_usd=10.0)
        in_flight = ledger.reserve(0.5)  # the attempt already running
        ledger.halt("operator")
        ledger.settle(in_flight, 0.5)  # it completes and is charged
        assert ledger.status().settled_usd == 0.5
        with pytest.raises(sl.SpendLedgerHalted):
            ledger.reserve(0.5)  # the next attempt is where the halt is observed


# ---------------------------------------------------------------------------
# the overbilled halt
# ---------------------------------------------------------------------------


class TestOverbilledHalt:
    def test_an_overbilled_settle_sets_the_halt(self, tmp_path):
        """`halt_on_overbilled` defaults True: a settle above its reservation
        means the cap arithmetic already understated real spend, so admission
        stops until an operator rules on it.

        Revert shown red: changed settle's `if self._halt_on_overbilled:
        self._record_halt(...)` to `if False:` -> this test failed on
        `assert row["halted"] == 1` with `assert 0 == 1`, and the following
        `reserve` was admitted. Restored.
        """
        ledger = make(tmp_path, cap_usd=10.0)
        reservation = ledger.reserve(0.5)
        with pytest.raises(sl.SpendCapExceeded):
            ledger.settle(reservation, 0.9)
        row = halt_row(ledger)
        assert row["halted"] == 1
        assert row["reason"] == "overbilled"
        assert reservation.id in row["detail"]
        assert [(e["action"], e["reason"], e["forced"]) for e in history(ledger)] == [
            ("halt", "overbilled", 0)
        ]
        # The halt is live: further admission stops even though headroom remains.
        with pytest.raises(sl.SpendLedgerHalted):
            ledger.reserve(0.1)

    def test_the_overbilled_halt_survives_the_raise(self, tmp_path):
        """The settle raises AFTER committing, so the halt and the `overbilled`
        row are both durable -- they are the record of real money."""
        ledger = make(tmp_path, cap_usd=10.0)
        reservation = ledger.reserve(0.5)
        with pytest.raises(sl.SpendCapExceeded):
            ledger.settle(reservation, 0.9)
        assert sl.open_ledger(ledger.path).status().halted is True

    def test_halt_on_overbilled_false_leaves_the_ledger_admitting(self, tmp_path):
        """The seam of section 9: a consumer that wants the overbilled row
        recorded without stopping the run can have it, and the raise still
        happens so the condition is never silent."""
        ledger = make(tmp_path, cap_usd=10.0, halt_on_overbilled=False)
        reservation = ledger.reserve(0.5)
        with pytest.raises(sl.SpendCapExceeded):
            ledger.settle(reservation, 0.9)
        assert halt_row(ledger)["halted"] == 0
        assert history(ledger) == []
        ledger.reserve(0.1)


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


class TestResume:
    def test_resume_clears_the_halt_and_admission_works_again(self, tmp_path):
        """Revert shown red: deleted the `UPDATE halt SET halted = 0 ...`
        statement from `resume`, leaving only the history INSERT -> this test
        failed on `assert row["halted"] == 0` with `assert 1 == 0`, and the
        following `reserve` raised `SpendLedgerHalted`. Restored.
        """
        ledger = make(tmp_path, cap_usd=10.0)
        ledger.halt("operator", "paused")
        ledger.resume()
        row = halt_row(ledger)
        assert row["halted"] == 0
        assert row["reason"] is None
        assert row["detail"] is None
        assert ledger.status().halted is False
        assert ledger.status().halt_reason == ""
        ledger.reserve(0.5)

    def test_resume_logs_the_action_with_the_reason_it_cleared(self, tmp_path):
        """The history is the audit trail: a cleared halt must still say what it
        was, because the control row no longer does."""
        ledger = make(tmp_path)
        ledger.halt("operator", "paused")
        ledger.resume()
        entries = history(ledger)
        assert [(e["action"], e["reason"], e["detail"], e["forced"]) for e in entries] == [
            ("halt", "operator", "paused", 0),
            ("resume", "operator", "paused", 0),
        ]

    def test_resume_on_a_running_ledger_writes_nothing(self, tmp_path):
        """A no-op rather than a refusal, and it leaves no history row: a resume
        that cleared nothing is not an event.

        Revert shown red: deleted the `if not int(halt["halted"]): return` early
        exit -> this test failed on `assert history(ledger) == []` with a
        `("resume", "", "", 0)` row, i.e. the history gained an event for a halt
        that never happened. Restored.
        """
        ledger = make(tmp_path)
        ledger.resume()
        assert halt_row(ledger)["halted"] == 0
        assert history(ledger) == []

    def test_resume_refuses_an_overbilled_halt_without_force(self, tmp_path):
        """Section 6: an `overbilled` halt means recorded spend exceeded a
        reservation, so the cap arithmetic is already known wrong and resuming
        would admit against a cap that bounds nothing.

        Revert shown red: deleted the `if reason == "overbilled" and not force:
        raise ValueError(...)` guard -> this test failed with `DID NOT RAISE
        <class 'ValueError'>`, the halt was cleared, and the trailing
        `reserve` was admitted against the already-wrong cap. Restored.
        """
        ledger = make(tmp_path, cap_usd=10.0)
        with pytest.raises(sl.SpendCapExceeded):
            ledger.settle(ledger.reserve(0.5), 0.9)
        with pytest.raises(ValueError) as exc:
            ledger.resume()
        assert "force=True" in str(exc.value)
        # The refusal changed nothing: still halted, still refusing admission.
        assert halt_row(ledger)["halted"] == 1
        with pytest.raises(sl.SpendLedgerHalted):
            ledger.reserve(0.1)
        assert [e["action"] for e in history(ledger)] == ["halt"]

    def test_the_resume_refusal_is_not_a_budget_verdict(self, tmp_path):
        """`cli.budget.spend_stop` translates `BudgetExceededError` subclasses
        into a `BudgetStop`. A refusal to resume is an operator verdict about a
        control switch, so it must stay outside that hierarchy or a run would
        report hitting its cap when an operator merely declined a force."""
        ledger = make(tmp_path, cap_usd=10.0)
        with pytest.raises(sl.SpendCapExceeded):
            ledger.settle(ledger.reserve(0.5), 0.9)
        with pytest.raises(ValueError) as exc:
            ledger.resume()
        assert not isinstance(exc.value, BudgetExceededError)

    def test_a_forced_resume_clears_it_and_is_logged_as_forced(self, tmp_path):
        """Section 6: a forced resume writes a `halt_history` row naming the
        force, so the decision to run on against a cap known to be wrong is on
        the record.

        Revert shown red: changed the resume history INSERT to pass a literal 0
        for `forced` -> this test failed on `assert entries[-1]["forced"] == 1`
        with `assert 0 == 1`; the halt cleared and the forcing left no trace,
        which is the one thing the design asks this row to record. Restored.
        """
        ledger = make(tmp_path, cap_usd=10.0)
        with pytest.raises(sl.SpendCapExceeded):
            ledger.settle(ledger.reserve(0.5), 0.9)
        ledger.resume(force=True)
        assert halt_row(ledger)["halted"] == 0
        entries = history(ledger)
        assert [(e["action"], e["reason"]) for e in entries] == [
            ("halt", "overbilled"),
            ("resume", "overbilled"),
        ]
        assert entries[-1]["forced"] == 1
        # Admission resumes against the cap the operator chose to trust.
        ledger.reserve(0.1)

    def test_force_is_keyword_only(self, tmp_path):
        """A positional `True` must not be able to force a resume by accident.

        Revert shown red: dropped the `*` from `resume(self, *, force=...)` ->
        this test failed with `DID NOT RAISE <class 'TypeError'>`. Restored.
        """
        ledger = make(tmp_path, cap_usd=10.0)
        with pytest.raises(sl.SpendCapExceeded):
            ledger.settle(ledger.reserve(0.5), 0.9)
        with pytest.raises(TypeError):
            ledger.resume(True)  # type: ignore[misc]

    def test_an_ordinary_halt_resumes_without_force_and_logs_unforced(self, tmp_path):
        """The gate is scoped to `overbilled`: an operator halt is a decision the
        same operator may simply undo, and forcing it would make `force=True` the
        habitual spelling, which would defeat the gate that matters.

        Revert shown red: widened the gate to `if not force:` -> this test failed
        with the overbilled `ValueError` raised out of `ledger.resume()` for an
        `operator` halt, naming a reservation overrun that never happened.
        Restored.
        """
        ledger = make(tmp_path, cap_usd=10.0)
        ledger.halt("operator", "lunch")
        ledger.resume()
        assert history(ledger)[-1]["forced"] == 0
        ledger.reserve(0.1)

    def test_a_forced_resume_of_an_ordinary_halt_is_allowed_and_recorded(self, tmp_path):
        """`force=True` is never refused; it is only ever REQUIRED."""
        ledger = make(tmp_path)
        ledger.halt("operator")
        ledger.resume(force=True)
        assert halt_row(ledger)["halted"] == 0
        assert history(ledger)[-1]["forced"] == 1


# ---------------------------------------------------------------------------
# check_halted
# ---------------------------------------------------------------------------


class TestCheckHalted:
    def test_check_halted_returns_quietly_on_a_running_ledger(self, tmp_path):
        assert make(tmp_path).check_halted() is None

    def test_check_halted_raises_when_halted(self, tmp_path):
        """Section 6: the probe exists so a consumer loop can stop BETWEEN units
        rather than waiting for its next reserve. It raises, so the consumer
        reaches the same handler as a refused reserve.

        Revert shown red: replaced `check_halted`'s body with a bare `return
        None` -> this test failed with `DID NOT RAISE <class
        'SpendLedgerHalted'>`, i.e. a consumer loop would have run on to its next
        unit and only stopped at the next reserve. Restored.
        """
        ledger = make(tmp_path)
        ledger.halt("operator", "between-units stop")
        with pytest.raises(sl.SpendLedgerHalted) as exc:
            ledger.check_halted(identifier="unit-9")
        assert exc.value.reason == "operator"
        assert exc.value.detail == "between-units stop"
        assert exc.value.identifier == "unit-9"
        assert exc.value.budget == 1.0

    def test_check_halted_is_a_budget_exceeded_error(self, tmp_path):
        ledger = make(tmp_path)
        ledger.halt("operator")
        with pytest.raises(BudgetExceededError):
            ledger.check_halted()

    def test_check_halted_sees_a_halt_set_through_another_handle(self, tmp_path):
        ledger = make(tmp_path)
        other = sl.open_ledger(ledger.path)
        ledger.halt("operator")
        with pytest.raises(sl.SpendLedgerHalted):
            other.check_halted()

    def test_check_halted_writes_nothing(self, tmp_path):
        """A probe a loop calls between every unit must not write: it would
        contend for the write lock with the work it is probing on behalf of.

        Revert shown red: made `check_halted` write a `('probe', ...)`
        `halt_history` row in its OWN committed `_writer()` transaction before
        reading the halt -> this test failed on `assert after == before` with
        `assert 4 == 1`. Removed again.

        A FIRST, WRONG prediction is worth recording: writing that row INSIDE the
        same `_writer()` block that then raises left this test GREEN, because
        `_writer` rolls back on any exception, so the probe row never committed.
        It was the companion test below -- the lock-contention one -- that went
        red for that revert. A write under a transaction that is about to raise is
        not observable here; only a committed one is.
        """
        ledger = make(tmp_path)
        ledger.halt("operator")
        conn = raw(ledger)
        try:
            before = conn.execute("SELECT COUNT(*) FROM halt_history").fetchone()[0]
            before_halt = dict(conn.execute("SELECT * FROM halt WHERE id = 1").fetchone())
        finally:
            conn.close()
        for _ in range(3):
            with pytest.raises(sl.SpendLedgerHalted):
                ledger.check_halted()
        conn = raw(ledger)
        try:
            after = conn.execute("SELECT COUNT(*) FROM halt_history").fetchone()[0]
            after_halt = dict(conn.execute("SELECT * FROM halt WHERE id = 1").fetchone())
        finally:
            conn.close()
        assert after == before
        assert after_halt == before_halt

    def test_check_halted_does_not_block_a_concurrent_write(self, tmp_path):
        """The probe runs under the plain deferred `BEGIN` of `status()`, so it
        takes no write lock -- it can be called while another process holds one.

        Revert shown red: changed `check_halted` to `self._writer()` -> this test
        failed with `sqlite3.OperationalError: database is locked` raised out of
        `check_halted`, because the probe then had to wait for a write lock it
        never needed. Restored.
        """
        ledger = make(tmp_path, busy_timeout_ms=60)
        blocker = sqlite3.connect(str(ledger.path), timeout=0.06)
        try:
            blocker.execute("PRAGMA busy_timeout = 60")
            blocker.execute("BEGIN IMMEDIATE")
            blocker.execute(
                "INSERT INTO halt_history (action, reason, detail, forced, at) "
                "VALUES ('probe', NULL, NULL, 0, 'now')"
            )
            assert ledger.check_halted() is None
        finally:
            blocker.rollback()
            blocker.close()

    def test_identifier_is_keyword_only(self, tmp_path):
        """Revert shown red: dropped the `*` from
        `check_halted(self, *, identifier=...)` -> this test failed with `DID NOT
        RAISE <class 'TypeError'>`, the positional string having been accepted as
        the identifier. Restored.
        """
        ledger = make(tmp_path)
        ledger.halt("operator")
        with pytest.raises(TypeError):
            ledger.check_halted("unit-9")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# No poller, no signal, no background thread
# ---------------------------------------------------------------------------


class TestNoPoller:
    def test_the_halt_surface_runs_one_transaction_and_loops_nowhere(self):
        """Section 6: "No poller, no signal." A halt is observed by PULL, at the
        next reserve; a loop or a sleep inside the halt surface would be a
        poller, and its latency bound would then be a tuning constant rather
        than one attempt's duration.

        Checked over the SOURCE of the three methods, because a runtime test
        cannot distinguish "returned promptly" from "polled once and got lucky".
        `threading` and `signal` are covered for the whole module by
        test_llm_spend_ledger.py's import-hygiene test, which allowlists the
        stdlib modules this file may import.

        Revert shown red (an INSERTION, the property being an absence): added

            while int(self._read_halt(conn)["halted"]) == 2:
                time.sleep(0.01)

        inside `check_halted` -> this test failed with
        `AssertionError: SpendLedger.check_halted contains a loop or a sleep:
        ['While', 'time.sleep']`. Removed again.
        """
        offenders = {}
        for name in ("halt", "resume", "check_halted"):
            found = []
            for node in ast.walk(method_source(name)):
                if isinstance(node, (ast.While, ast.For, ast.AsyncFor)):
                    found.append(type(node).__name__)
                if isinstance(node, ast.Call):
                    target = node.func
                    if (
                        isinstance(target, ast.Attribute)
                        and target.attr == "sleep"
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "time"
                    ):
                        found.append("time.sleep")
            if found:
                offenders[name] = found
        assert not offenders, "; ".join(
            f"SpendLedger.{k} contains a loop or a sleep: {v}" for k, v in offenders.items()
        )


# ---------------------------------------------------------------------------
# status() under live writers -- section 7
# ---------------------------------------------------------------------------

# REAL concurrent writers in separate OS processes (no mock, no thread): each
# opens the ledger, announces readiness through a file the parent polls for, and
# then reserves and settles the SAME amount over and over until the parent drops
# a stop file. Every row therefore ends up `settled` at exactly its reserved
# amount, and a row is `open` only for the window between its two commits -- so
# at every REAL snapshot the identity asserted by the parent holds, and a torn
# read across `status()`'s separate queries breaks it.
#
# THREE writers rather than one, on purpose. A tear is only observable when a
# commit lands between two of `status()`'s queries, so the test's sensitivity is
# the commit rate divided by the per-sample duration. With a single writer
# (~1 commit per 7 ms against a ~0.4 ms sample) the revert below ran GREEN; the
# test looked like coverage and was not. Three writers plus a floor on the
# number of commits raced (COMMITS_TO_RACE) is what makes it fail.
_WRITER_SCRIPT = """
import os
import sys
sys.path.insert(0, sys.argv[1])
from content_pipeline.llm import spend_ledger as sl

db_path, ready_path, stop_path, amount = sys.argv[2:6]
amount = float(amount)
# Open BEFORE announcing readiness, so the parent's first sample races real
# reserve/settle traffic rather than this process's own start-up.
ledger = sl.open_ledger(db_path, busy_timeout_ms=30000)
open(ready_path, "w").close()
written = 0
while not os.path.exists(stop_path):
    reservation = ledger.reserve(amount, scope="writer")
    ledger.settle(reservation, amount)
    written += 1
print("WROTE", written, flush=True)
"""


def _wait_for(predicate, *, timeout_s, what):
    """Poll a causal observable. Never a bare sleep sized for an idle machine."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    raise AssertionError(f"timed out after {timeout_s}s waiting for {what}")


#: How many writer processes race the reader. See the comment above.
WRITERS = 3
#: The floor on commits raced before the test may pass. Below this the sample
#: set is too small for a torn read to be observable, and a green run would mean
#: nothing; the test fails rather than passing on an unexercised property.
COMMITS_TO_RACE = 250


class TestStatusUnderLiveWriters:
    def test_status_is_consistent_while_other_processes_write(self, tmp_path):
        """Section 7: `status()` opens one connection and a plain deferred
        `BEGIN`. In WAL the read snapshot is fixed at the first read and writers
        append without invalidating it, so every one of `status()`'s separate
        queries sees ONE state.

        The property asserted is cross-QUERY consistency, which is the only thing
        the snapshot buys: `_partition_nano` issues six independent SELECTs and
        the counts are a seventh. The writers only ever reserve and settle the
        same amount, so each row contributes that amount to RESERVED while open
        and to SETTLED once resolved, and never to anything else. Hence at any
        real snapshot

            settled + reserved == requests * amount

        Without a fixed snapshot a row can be seen as neither -- settled after
        the SETTLED sum was taken and before the RESERVED sum, or inserted after
        both and still counted by `requests`, which is read later still -- and the
        equality fails.

        Two floors keep the test from going green on an unexercised property: it
        must observe a snapshot with a row genuinely OPEN, and it must race at
        least COMMITS_TO_RACE commits.

        Revert shown red, 3 trials out of 3: deleted `conn.execute("BEGIN")`
        from `_reader`, leaving the connection in autocommit so every SELECT got
        its own snapshot -> this test failed with `AssertionError: torn read:
        settled 113000000 + reserved 0 != requests 114 * 1000000`, then
        `102000000 + 1000000 != 104 * 1000000`, then `186000000 + 0 != 187 *
        1000000` (the numbers vary per run; the shape does not). Restored, and
        the file then ran green 5 times in a row.

        That revert first ran GREEN against a ONE-writer version of this test,
        which is why there are three: a tear is only visible when a commit lands
        between two of `status()`'s queries, so sensitivity is the commit rate
        over the per-sample duration, and one writer did not supply it. The
        green-with-the-revert-applied run is the reason COMMITS_TO_RACE exists.

        NOTE that the plain `BEGIN` must stay plain: `BEGIN IMMEDIATE` here would
        take the write lock and serialize `status()` against the writers it
        exists to observe.
        """
        amount_usd = 0.001
        amount_nano = sl.usd_to_nano(amount_usd)
        ledger = make(
            tmp_path,
            cap_usd=1000.0,  # room for far more rows than the sampling window needs
            name="live.sqlite",
            synchronous="OFF",  # faster commits mean more chances to tear across
        )
        stop = tmp_path / "writers.stop"
        ready = [tmp_path / f"writer-{i}.ready" for i in range(WRITERS)]

        procs = [
            subprocess.Popen(
                [
                    sys.executable, "-c", _WRITER_SCRIPT,
                    LIB_ROOT, str(ledger.path), str(ready[i]), str(stop), str(amount_usd),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for i in range(WRITERS)
        ]
        samples = 0
        saw_open_row = False
        first_requests = None
        last_requests = 0
        try:
            _wait_for(
                lambda: all(p.exists() for p in ready) or any(p.poll() is not None for p in procs),
                timeout_s=120,
                what="every writer process to open the ledger",
            )
            for proc in procs:
                assert proc.poll() is None, "a writer exited before writing anything"
            # Poll until the writers have actually committed something -- a causal
            # observable, not a guessed delay.
            _wait_for(
                lambda: ledger.status().requests > 0,
                timeout_s=120,
                what="the writers' first committed reservation",
            )

            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                status = ledger.status()
                settled_nano = sl.usd_to_nano(status.settled_usd)
                reserved_nano = sl.usd_to_nano(status.reserved_usd)
                assert settled_nano + reserved_nano == status.requests * amount_nano, (
                    f"torn read: settled {settled_nano} + reserved {reserved_nano} "
                    f"!= requests {status.requests} * {amount_nano}"
                )
                # Nothing else may appear: the writers never hold, never leak,
                # and nothing reclaims. A torn read could also surface here.
                assert (status.unknown_usd, status.leaked_usd, status.reclaimed_usd) == (
                    0.0, 0.0, 0.0,
                )
                # Within ONE query, so not a tear detector -- it pins the counts
                # against the states instead.
                assert status.requests == status.calls_settled + status.reservations_open
                assert status.halted is False
                samples += 1
                if first_requests is None:
                    first_requests = status.requests
                last_requests = status.requests
                if status.reserved_usd > 0:
                    saw_open_row = True
                if last_requests - first_requests >= COMMITS_TO_RACE and saw_open_row:
                    break
                for proc in procs:
                    assert proc.poll() is None, "a writer exited mid-sampling"
        finally:
            stop.write_text("stop\n", encoding="utf-8")
            outputs = [proc.communicate(timeout=120) for proc in procs]

        for proc, (out, err) in zip(procs, outputs):
            assert proc.returncode == 0, err
            assert out.startswith("WROTE "), out
            assert int(out.split()[1]) > 0, out
        # The reads really did overlap live writes, twice over.
        assert last_requests - first_requests >= COMMITS_TO_RACE, (
            f"raced only {last_requests - first_requests} commits over {samples} "
            f"samples (floor {COMMITS_TO_RACE}); a torn read would not have been "
            "observable, so a pass here would mean nothing"
        )
        assert saw_open_row, (
            f"never sampled a snapshot with an OPEN row over {samples} samples, so the "
            "cross-query window this test exists to probe was never entered"
        )

    def test_a_halt_set_mid_run_is_reported_by_status(self, tmp_path):
        """The operator's read path during a live run: `status()` reports the
        halt without raising, while `reserve` is what refuses. Shown red by the
        same hardcoded `halted=False` revert as
        `test_halt_is_visible_to_status_without_raising`
        (`assert (False, 'operator', 'mid-run') == (True, 'operator',
        'mid-run')`)."""
        ledger = make(tmp_path, cap_usd=10.0)
        ledger.settle(ledger.reserve(0.5), 0.25)
        ledger.halt("operator", "mid-run")
        status = ledger.status()
        assert (status.halted, status.halt_reason, status.halt_detail) == (
            True, "operator", "mid-run",
        )
        # The halt changes no money term.
        assert status.settled_usd == 0.25
        assert status.outstanding_usd == 0.25
        assert status.remaining_usd == 9.75
        assert status.requests == 1


def test_the_halt_surface_is_present_and_callable():
    """The complement of SL-1's "not half-shipped" guard, which was narrowed to
    SL-3's `renew` / `reclaim_orphans` when this unit landed."""
    for name in ("halt", "resume", "check_halted"):
        assert callable(getattr(sl.SpendLedger, name)), name


