"""SL-3: leases, orphan reclamation, and the leak accounting that follows.

Design: `dev/tasks/loc-pipeline-consumer-needs/design-cross-process-spend-ledger.md`
section 5 ("Orphans, leases and the bounded hole") and section 7 (the partition).

Every load-bearing assertion here was shown RED by reverting the production line
it protects (docs/reference/vacuous-checks.md); each revert is named in the
test's docstring so the counterfactual can be re-run. Where the property is the
ABSENCE of behaviour -- a NULL lease never swept, no renewal thread -- the revert
INSERTS the forbidden code instead of removing code, because there is no line to
take away.

Scope boundaries, so a reader does not look here for them: the ledger core
(schema, admission, settle/release, the partition with `leaks` EMPTY) is SL-1's
`test_llm_spend_ledger.py`; the halt switch is SL-2's
`test_llm_spend_ledger_halt.py`; the cross-process cap acceptance suite, the
busy-timeout exhaustion test and the `BEGIN IMMEDIATE` source guard are SL-4's.

This file RE-ASSERTS the partition with `leaks` rows PRESENT, which SL-1 could
only do with the table empty because the write path is this unit's. The two do
not collide: SL-1's `test_leaks_is_empty_in_this_unit` is a statement about ITS
fixture, not about the table, and the invariants asserted over both fixtures are
the same ones. A leak row here is produced by the REAL path -- reclaim, then a
late settle -- never injected, which is what makes (d) a statement about
production behaviour rather than about raw SQL.
"""

import ast
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from content_pipeline.llm import spend_ledger as sl

MODULE_PATH = Path(sl.__file__)
LIB_ROOT = str(
    Path(__file__).resolve().parents[2] / "plugins" / "content-pipeline-kit" / "lib"
)

#: Short enough that a test is not slow, long enough that the reservation is
#: genuinely live when it is taken out. Tests WAIT for the stored deadline to
#: pass rather than sleeping for this value, so a slow machine lengthens the
#: wait instead of changing the outcome.
TTL_S = 0.05

#: A lease no test may outlive, for a reservation that must stay live.
LIVE_TTL_S = 3600.0


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


def rows(ledger):
    conn = raw(ledger)
    try:
        return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM ledger")}
    finally:
        conn.close()


def leaks(ledger):
    conn = raw(ledger)
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT ledger_id, reported_cost, presented_at, generation FROM leaks "
                "ORDER BY seq"
            )
        ]
    finally:
        conn.close()


def _wait_for(predicate, *, timeout_s, what):
    """Poll a causal observable. Never a bare sleep sized for an idle machine."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    raise AssertionError(f"timed out after {timeout_s}s waiting for {what}")


def wait_past(*deadlines, timeout_s=30):
    """Wait until wall-clock time is strictly past every given lease deadline.

    The observable is the deadline the ledger actually STORED, read back from the
    reservation, not a duration guessed by the test -- so the sweep's
    `lease_expires_at < now` comparison is known to be satisfiable before the
    sweep is asked to make it.
    """
    latest = max(float(d) for d in deadlines)
    _wait_for(
        lambda: time.time() > latest,
        timeout_s=timeout_s,
        what=f"wall clock to pass the stored lease deadline {latest}",
    )


def independent_partition(ledger):
    """Recompute the partition from rows with SQL written independently of the
    production queries, so comparing the two is a cross-check rather than a
    restatement of one implementation. Mirrors SL-1's helper, extended with the
    two disclosure fields this unit is the first to be able to produce."""
    conn = raw(ledger)
    try:
        ledger_rows = conn.execute("SELECT id, state, reserved, settled FROM ledger").fetchall()
        leak_rows = conn.execute("SELECT ledger_id, reported_cost FROM leaks").fetchall()
    finally:
        conn.close()
    settled = reserved = unknown = leaked = reclaimed = written_off = 0
    leaked_ids = {str(k["ledger_id"]) for k in leak_rows}
    for r in ledger_rows:
        if r["state"] == "settled":
            settled += int(r["settled"])
        elif r["state"] == "overbilled":
            settled += int(r["reserved"])
            leaked += int(r["settled"]) - int(r["reserved"])
        elif r["state"] == "open":
            reserved += int(r["reserved"])
        elif r["state"] == "unknown":
            unknown += int(r["reserved"])
        elif r["state"] == "reclaimed":
            reclaimed += int(r["reserved"])
            if str(r["id"]) not in leaked_ids:
                written_off += int(r["reserved"])
    leaked += sum(int(k["reported_cost"]) for k in leak_rows)
    return {
        "settled": settled,
        "reserved": reserved,
        "unknown": unknown,
        "leaked": leaked,
        "reclaimed": reclaimed,
        "written_off": written_off,
        "outstanding": settled + reserved + unknown + leaked,
    }


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
# Leases: what reserve stores, and what a sweep is allowed to look at
# ---------------------------------------------------------------------------


class TestLeaseStorage:
    def test_a_ttl_stores_a_future_deadline_and_no_ttl_stores_null(self, tmp_path):
        """Section 5: with a deadline known, `lease_expires_at = now + ttl`; with
        `timeout_s is None` the lease is NULL.

        Revert shown red: made `reserve` ignore `ttl_s` and always insert a NULL
        lease -> this test failed with `assert None is not None` on the leased
        row's stored deadline. Restored.
        """
        ledger = make(tmp_path, cap_usd=10.0)
        before = time.time()
        leased = ledger.reserve(1.0, ttl_s=LIVE_TTL_S)
        unleased = ledger.reserve(1.0)

        assert leased.lease_expires_at is not None
        assert before + LIVE_TTL_S <= leased.lease_expires_at <= time.time() + LIVE_TTL_S
        assert unleased.lease_expires_at is None

        stored = rows(ledger)
        assert stored[leased.id]["lease_expires_at"] == pytest.approx(leased.lease_expires_at)
        assert stored[unleased.id]["lease_expires_at"] is None

    def test_reserve_refuses_a_non_positive_ttl(self, tmp_path):
        ledger = make(tmp_path, cap_usd=10.0)
        for bad in (0, -1.0):
            with pytest.raises(ValueError, match="ttl_s must be positive"):
                ledger.reserve(1.0, ttl_s=bad)

    def test_a_sweep_takes_the_expired_lease_and_leaves_the_live_one(self, tmp_path):
        """The deadline is honoured in BOTH directions inside one sweep, which is
        what makes it a deadline rather than an age.

        Deterministic without waiting on the live row: one reservation is given a
        TTL the test waits out, the other a TTL no test run can outlive, so the
        sweep's verdict on each is fixed by construction.

        Revert shown red: dropped `AND lease_expires_at < ?` from the sweep's
        eligibility query (leaving `state = 'open' AND lease_expires_at IS NOT
        NULL`) -> this test failed with `assert 2 == 1` on the reclaimed count,
        the live reservation having been swept 3600 seconds before its deadline.
        Restored.
        """
        ledger = make(tmp_path, cap_usd=10.0)
        expired = ledger.reserve(1.0, identifier="expired", ttl_s=TTL_S)
        live = ledger.reserve(1.0, identifier="live", ttl_s=LIVE_TTL_S)
        wait_past(expired.lease_expires_at)

        report = ledger.reclaim_orphans(batch_limit=0)
        assert len(report.reclaimed_ids) == 1
        assert report.reclaimed_ids == (expired.id,)
        stored = rows(ledger)
        assert stored[expired.id]["state"] == "reclaimed"
        assert stored[live.id]["state"] == "open"


class TestNullLeaseIsNeverSwept:
    def test_a_null_lease_is_never_swept_at_any_batch_limit(self, tmp_path):
        """Section 5 / B2: with no deadline known the lease is NULL, and no
        elapsed time is evidence of anything, so no sweep may ever take the row.
        This is half of what stops B2 (two processes at cap, both past a TTL they
        never had, a third sweeping both and freeing the whole cap).

        PREDICTION CORRECTED. The revert first written for this test was
        "delete `AND lease_expires_at IS NOT NULL` from the sweep's eligibility
        query", and it ran GREEN -- SQLite's three-valued logic already excludes
        NULL from `lease_expires_at < ?`, which evaluates to NULL and not to
        true. That clause is belt and braces, so removing it cannot show this
        test red and a revert-check against it would have been vacuous.

        Revert shown red (an INSERTION, the property being an absence): widened
        the production predicate to

            AND (lease_expires_at IS NULL OR lease_expires_at < ?)

        -> this test failed with `assert ('858c7a2a...',) == ()` on
        `report.reclaimed_ids`, the unleased reservation having been swept; the
        companion test below failed with
        `DID NOT RAISE <class 'SpendCapExceeded'>`, the same row having been
        swept by a `"lease"`-mode `reserve`. Removed again.
        """
        ledger = make(tmp_path, cap_usd=10.0)
        unleased = ledger.reserve(1.0, identifier="no-deadline")
        assert unleased.lease_expires_at is None
        # Any amount of elapsed time, then every batch limit there is.
        wait_past(time.time())
        for limit in (0, 1, 50):
            report = ledger.reclaim_orphans(batch_limit=limit)
            assert report.reclaimed_ids == ()
            assert report.expired_total == 0
        assert rows(ledger)[unleased.id]["state"] == "open"
        assert ledger.status().reserved_usd == 1.0

    def test_lease_mode_reserve_also_never_sweeps_a_null_lease(self, tmp_path):
        """The same property through the OTHER sweep entry point. A ledger in
        `"lease"` mode sweeps inside every `reserve`, and must still leave an
        unleased row alone -- otherwise B2's stop would hold for the manual lever
        and not for the automatic one.
        """
        ledger = make(
            tmp_path, cap_usd=1.0, orphan_reclaim="lease", reclaim_batch_limit=0
        )
        unleased = ledger.reserve(1.0)
        wait_past(time.time())
        with pytest.raises(sl.SpendCapExceeded):
            ledger.reserve(1.0)
        assert rows(ledger)[unleased.id]["state"] == "open"


# ---------------------------------------------------------------------------
# Retention, and the state a reclaimed row is left in
# ---------------------------------------------------------------------------


class TestReclaimedRowRetention:
    def test_a_reclaimed_row_is_retained_permanently(self, tmp_path):
        """Section 5: the sweep RETAINS the row, so a late settle can be told
        apart from a settle against an id that never existed -- and `requests`
        still counts the attempt that really was admitted.

        Revert shown red: changed the sweep's UPDATE to
        `DELETE FROM ledger WHERE state = 'open' AND id IN (...)` -> this test
        failed with `assert 0 == 1` on `status().requests`, the admitted attempt
        having vanished from the ledger's own count of attempts. Restored.
        (The same revert also breaks the late-settle leak tests below, because a
        leak row must name an existing reclaimed row.)
        """
        ledger = make(tmp_path, cap_usd=1.0)
        reservation = ledger.reserve(0.4, identifier="orphan", ttl_s=TTL_S)
        wait_past(reservation.lease_expires_at)
        ledger.reclaim_orphans()

        status = ledger.status()
        assert status.requests == 1
        assert status.calls_settled == 0
        assert status.reservations_open == 0
        stored = rows(ledger)
        assert set(stored) == {reservation.id}
        assert stored[reservation.id]["state"] == "reclaimed"
        assert stored[reservation.id]["identifier"] == "orphan"
        assert int(stored[reservation.id]["reserved"]) == reservation.amount_nano

    def test_a_reclaimed_row_carries_settled_zero_not_null(self, tmp_path):
        """SL-1's structural clause `settled_null_on_resolved` covers
        `reclaimed`, so the sweep must write `settled = 0`. A NULL there makes
        the file structurally invalid and the NEXT transaction refuses -- the
        sweep's own transaction validates BEFORE its body, so the damage surfaces
        one call later, which is exactly why this is asserted on the value and on
        a following read.

        The STRUCTURAL consequence is asserted first, deliberately: the raw
        value is only evidence about one row, while the following `status()` call
        is where a NULL actually stops the ledger working.

        Revert shown red: changed the sweep's UPDATE to write `settled = NULL`
        -> this test failed with
        `LedgerStateInvalid: ... ledger violates settled_null_on_resolved (row
        '...')` raised out of `ledger.status()`. Restored.
        """
        ledger = make(tmp_path, cap_usd=1.0)
        reservation = ledger.reserve(0.4, ttl_s=TTL_S)
        wait_past(reservation.lease_expires_at)
        ledger.reclaim_orphans()

        # This transaction validates the file; a NULL refuses here, one call
        # after the sweep, because a transaction validates BEFORE its own body.
        assert ledger.status().reclaimed_usd == 0.4
        assert rows(ledger)[reservation.id]["settled"] == 0
        # And `settled = 0` on a `reclaimed` row contributes nothing to SETTLED,
        # which is counted over `state = 'settled'` only.
        assert ledger.status().settled_usd == 0.0

    def test_the_sweep_bumps_the_generation(self, tmp_path):
        """The bump is what makes a late settle LOSE its compare-and-set. The
        settle CASes on `(id, generation, state='open')`, so the state change
        alone would already defeat it -- the bump is what makes the reservation
        receipt the caller still holds unambiguously stale, including for
        `renew`.

        Revert shown red: dropped `generation = generation + 1` from the sweep's
        UPDATE -> this test failed with `assert 0 == 1` on the stored
        generation. Restored.
        """
        ledger = make(tmp_path, cap_usd=1.0)
        reservation = ledger.reserve(0.4, ttl_s=TTL_S)
        assert reservation.generation == 0
        wait_past(reservation.lease_expires_at)
        ledger.reclaim_orphans()
        assert int(rows(ledger)[reservation.id]["generation"]) == 1


# ---------------------------------------------------------------------------
# The bounded hole: headroom, the swept SUM, and the batch limit
# ---------------------------------------------------------------------------


class TestHeadroomAndTheOvershootBound:
    def test_an_expired_lease_holds_its_headroom_until_something_sweeps(self, tmp_path):
        """An expired lease is NOT self-cleaning: in the default `"manual"` mode
        the row keeps consuming the cap until the caller sweeps. That is what
        makes the overshoot a bounded, deliberate act rather than something time
        does on its own.

        Revert shown red: made `reserve` sweep unconditionally (ignoring
        `self._orphan_reclaim`) -> this test failed with
        `DID NOT RAISE <class 'SpendCapExceeded'>`, the second reservation having
        been admitted against headroom a manual-mode ledger had not been asked to
        free. Restored.
        """
        ledger = make(tmp_path, cap_usd=1.0)
        reservation = ledger.reserve(1.0, ttl_s=TTL_S)
        wait_past(reservation.lease_expires_at)

        assert ledger.status().reserved_usd == 1.0
        assert ledger.status().outstanding_usd == 1.0
        with pytest.raises(sl.SpendCapExceeded):
            ledger.reserve(1.0)
        assert rows(ledger)[reservation.id]["state"] == "open"

        report = ledger.reclaim_orphans()
        assert report.freed_usd == 1.0
        assert ledger.status().outstanding_usd == 0.0
        ledger.reserve(1.0)  # the freed headroom is now admissible

    def test_the_bound_of_one_pass_is_the_sum_over_every_row_it_swept(self, tmp_path):
        """Section 5, the load-bearing arithmetic: the overshoot bound is the SUM
        of `reserved` over ALL expired-but-live rows a pass swept, NOT one row.
        An expiry sweep is `WHERE lease_expires_at < now`, so N expired rows all
        go in ONE pass once the batch limit allows it, and the bound is at most
        concurrency times the largest reservation.

        This test deliberately ENTERS the reclaim window partition assertion (a)
        excludes: the three swept reservations are still live by construction (no
        settle has run), so the $0.90 they may yet bill plus the $0.90 admitted
        after the sweep is $1.80 against a $0.90 cap. That overshoot is the
        bound, measured.

        Revert shown red: hardcoded the sweep to take at most one row
        (`taken = eligible[:1]`, ignoring `batch_limit`) -> this test failed with
        `assert 1 == 3` on `len(report.reclaimed_ids)`, the report then carrying
        `freed_nano=300000000` and understating the bound by two thirds.
        Restored.
        """
        ledger = make(tmp_path, cap_usd=0.9)
        held = [ledger.reserve(0.3, identifier=f"a{i}", ttl_s=TTL_S) for i in range(3)]
        assert ledger.status().outstanding_usd == pytest.approx(0.9)
        wait_past(*[r.lease_expires_at for r in held])

        report = ledger.reclaim_orphans(batch_limit=0)  # 0 == no limit
        assert len(report.reclaimed_ids) == 3
        assert report.expired_total == 3
        # THE BOUND: the sum over every swept row, not the largest one.
        assert report.freed_nano == sum(r.amount_nano for r in held) == sl.usd_to_nano(0.9)
        assert report.freed_nano > max(r.amount_nano for r in held)
        assert report.freed_usd == pytest.approx(0.9)
        assert ledger.status().outstanding_usd == 0.0

        # The freed headroom is really admissible, so the overshoot is real:
        # $0.90 of still-live work plus $0.90 newly admitted against a $0.90 cap.
        ledger.reserve(0.9)
        assert ledger.status().outstanding_usd == pytest.approx(0.9)
        still_live = sum(r.amount_nano for r in held)
        assert still_live + sl.usd_to_nano(0.9) == 2 * ledger.cap_nano

    def test_reclaim_batch_limit_one_takes_one_of_three_and_converges(self, tmp_path):
        """Section 9: `reclaim_batch_limit` defaults to 1, which holds the bound
        at the largest single reclaimed row while still converging -- every later
        sweep takes the next expired row.

        Revert shown red: ignored the batch limit in the sweep
        (`taken = eligible`) -> this test failed with `assert 3 == 1` on the first
        pass's reclaimed count, the default having freed the whole $0.90 in one
        go and widened the bound by 3x. Restored.
        """
        ledger = make(tmp_path, cap_usd=0.9)
        assert ledger.reclaim_batch_limit == 1  # the shipped default
        held = [ledger.reserve(0.3, identifier=f"a{i}", ttl_s=TTL_S) for i in range(3)]
        wait_past(*[r.lease_expires_at for r in held])

        first = ledger.reclaim_orphans()
        assert len(first.reclaimed_ids) == 1
        assert first.freed_nano == sl.usd_to_nano(0.3)
        # The report says more remain, so a caller can tell "nothing was
        # orphaned" from "the next sweep will take the next one".
        assert first.expired_total == 3

        second = ledger.reclaim_orphans()
        third = ledger.reclaim_orphans()
        fourth = ledger.reclaim_orphans()
        assert [len(r.reclaimed_ids) for r in (second, third, fourth)] == [1, 1, 0]
        assert fourth.expired_total == 0
        # Converged, and every row was taken exactly once.
        taken = first.reclaimed_ids + second.reclaimed_ids + third.reclaimed_ids
        assert sorted(taken) == sorted(r.id for r in held)
        assert ledger.status().outstanding_usd == 0.0

    def test_lease_mode_sweeps_inside_reserve_under_the_same_batch_limit(self, tmp_path):
        """B2's other half: where a lease DOES exist, `reclaim_batch_limit=1`
        bounds what one `reserve` can free. The sweep runs in the SAME
        `BEGIN IMMEDIATE` transaction as the admission, so headroom one process
        frees cannot be read by it and spent by another.

        Revert shown red: removed the `"lease"`-mode sweep from `reserve` -> this
        test failed with
        `SpendCapExceeded: budget exceeded for 'new-1': measured 1.2 > budget
        0.9` raised out of `ledger.reserve(0.3, identifier="new-1")`, nothing
        having freed the expired rows. Restored.
        """
        ledger = make(tmp_path, cap_usd=0.9, orphan_reclaim="lease")
        held = [ledger.reserve(0.3, identifier=f"a{i}", ttl_s=TTL_S) for i in range(3)]
        wait_past(*[r.lease_expires_at for r in held])

        # One sweep of one row happens inside this admission, freeing exactly
        # $0.30 -- enough for this reservation and no more.
        ledger.reserve(0.3, identifier="new-1")
        reclaimed_after_one = [
            r for r in rows(ledger).values() if r["state"] == "reclaimed"
        ]
        assert len(reclaimed_after_one) == 1
        # The cap is full again, so the next admission sweeps one more row.
        ledger.reserve(0.3, identifier="new-2")
        assert len([r for r in rows(ledger).values() if r["state"] == "reclaimed"]) == 2

    def test_reclaim_refuses_a_negative_batch_limit(self, tmp_path):
        ledger = make(tmp_path, cap_usd=1.0)
        with pytest.raises(ValueError, match="batch_limit must be >= 0"):
            ledger.reclaim_orphans(batch_limit=-1)
        with pytest.raises(ValueError, match="reclaim_batch_limit must be >= 0"):
            make(tmp_path, cap_usd=1.0, name="bad.sqlite", reclaim_batch_limit=-1)

    def test_an_unknown_orphan_reclaim_mode_is_refused_at_construction(self, tmp_path):
        """A mode seam that silently accepted a typo would disable sweeping
        without saying so, which is the failure the seam exists to make
        explicit."""
        with pytest.raises(ValueError, match="orphan_reclaim must be one of"):
            make(tmp_path, cap_usd=1.0, name="typo.sqlite", orphan_reclaim="leases")
        assert sl.ORPHAN_RECLAIM_MODES == ("manual", "lease")

    def test_only_open_rows_are_swept(self, tmp_path):
        """A resolved row has no headroom left to free. `unknown` is the case
        that matters: it is HELD at its reserved amount deliberately (section 5's
        no-release-on-failure rule), so sweeping it would undo exactly that hold.

        Revert shown red: widened the sweep's eligibility AND its UPDATE to
        `state IN ('open', 'unknown')` -> this test failed with
        `assert ('850b39c9...',) == ()` on `report.reclaimed_ids`, the held
        reservation having been reclaimed and its hold on the cap released.
        (Both halves are needed: the eligibility query alone selects the row, and
        the UPDATE's own `state = 'open'` would then decline to change it -- a
        reminder that this predicate is written twice.) Restored.
        """
        ledger = make(tmp_path, cap_usd=2.0)
        held = ledger.reserve(0.5, ttl_s=TTL_S)
        ledger.settle(held, None)  # -> unknown, held at 0.5
        assert ledger.status().unknown_usd == 0.5
        wait_past(held.lease_expires_at)

        report = ledger.reclaim_orphans(batch_limit=0)
        assert report.reclaimed_ids == ()
        assert ledger.status().unknown_usd == 0.5
        assert rows(ledger)[held.id]["state"] == "unknown"


# ---------------------------------------------------------------------------
# renew
# ---------------------------------------------------------------------------


class TestRenew:
    def test_renew_extends_a_lease_past_a_sweep(self, tmp_path):
        """Section 5: `renew` is what a caller's own watchdog uses, there being
        no renewal thread in the library. A renewed reservation is not an orphan.

        Revert shown red: made `renew`'s UPDATE write `lease_expires_at =
        lease_expires_at` (still matching its row, so still returning
        rowcount 1) -> this test failed at the STORED-lease assertion with
        `assert 1790895644.33 == 1790899244.33 +- 1.8e+03`: the returned receipt
        advertised a new deadline the database had never been told about.
        Restored.
        """
        ledger = make(tmp_path, cap_usd=1.0)
        reservation = ledger.reserve(0.4, ttl_s=TTL_S)
        original_deadline = reservation.lease_expires_at
        wait_past(original_deadline)

        renewed = ledger.renew(reservation, ttl_s=LIVE_TTL_S)
        assert renewed.lease_expires_at > original_deadline
        assert rows(ledger)[reservation.id]["lease_expires_at"] == pytest.approx(
            renewed.lease_expires_at
        )

        report = ledger.reclaim_orphans(batch_limit=0)
        assert report.reclaimed_ids == ()
        assert rows(ledger)[reservation.id]["state"] == "open"
        # The returned receipt is the live one; identity and money are unchanged.
        assert (renewed.id, renewed.generation, renewed.amount_nano) == (
            reservation.id, reservation.generation, reservation.amount_nano,
        )
        # The old frozen value keeps the superseded deadline, as documented.
        assert reservation.lease_expires_at == original_deadline

    def test_renew_measures_from_now_not_from_the_old_deadline(self, tmp_path):
        """A renewal that arrives late must not inherit the lateness it was sent
        to correct, so the new deadline is `now + ttl_s`.

        Revert shown red: changed `renew` to compute
        `lease = reservation.lease_expires_at + float(ttl_s)` -> this test failed
        with the renewed deadline landing before `now + ttl_s` (the stale base
        being older than `now`). Restored.
        """
        ledger = make(tmp_path, cap_usd=1.0)
        reservation = ledger.reserve(0.4, ttl_s=TTL_S)
        wait_past(reservation.lease_expires_at)
        before = time.time()
        renewed = ledger.renew(reservation, ttl_s=10.0)
        assert before + 10.0 <= renewed.lease_expires_at <= time.time() + 10.0

    def test_renew_gives_a_null_lease_row_a_deadline(self, tmp_path):
        """The documented way a caller that had no deadline at `reserve` time
        opts into sweeping later."""
        ledger = make(tmp_path, cap_usd=1.0)
        reservation = ledger.reserve(0.4)
        assert reservation.lease_expires_at is None
        renewed = ledger.renew(reservation, ttl_s=TTL_S)
        assert renewed.lease_expires_at is not None
        wait_past(renewed.lease_expires_at)
        assert ledger.reclaim_orphans().reclaimed_ids == (reservation.id,)

    def test_renew_of_a_reclaimed_reservation_raises_and_changes_nothing(self, tmp_path):
        """A watchdog must not be able to resurrect headroom the ledger already
        freed, so `renew` compare-and-sets on `(id, generation, state='open')`
        exactly as `settle` does.

        Revert shown red: dropped `AND generation = ? AND state = 'open'` from
        `renew`'s UPDATE -> this test failed with
        `DID NOT RAISE <class 'StaleReservationError'>`, and the reclaimed row's
        lease had been pushed into the future. Restored.
        """
        ledger = make(tmp_path, cap_usd=1.0)
        reservation = ledger.reserve(0.4, ttl_s=TTL_S)
        wait_past(reservation.lease_expires_at)
        ledger.reclaim_orphans()
        before = rows(ledger)[reservation.id]

        with pytest.raises(sl.StaleReservationError, match="lease was not renewed"):
            ledger.renew(reservation, ttl_s=LIVE_TTL_S)
        assert rows(ledger)[reservation.id] == before

    def test_renew_of_a_settled_reservation_raises(self, tmp_path):
        ledger = make(tmp_path, cap_usd=1.0)
        reservation = ledger.reserve(0.4, ttl_s=LIVE_TTL_S)
        ledger.settle(reservation, 0.2)
        with pytest.raises(sl.StaleReservationError):
            ledger.renew(reservation, ttl_s=LIVE_TTL_S)

    def test_renew_refuses_a_non_positive_ttl(self, tmp_path):
        ledger = make(tmp_path, cap_usd=1.0)
        reservation = ledger.reserve(0.4, ttl_s=LIVE_TTL_S)
        for bad in (0, -1.0):
            with pytest.raises(ValueError, match="ttl_s must be positive"):
                ledger.renew(reservation, ttl_s=bad)


# ---------------------------------------------------------------------------
# No renewal thread, no poller
# ---------------------------------------------------------------------------


def test_the_lease_surface_has_no_thread_poller_or_sleep():
    """Section 5: "No renewal thread ships, so a lease exists only when a
    deadline is known." A background thread, a poller or a sleep inside the lease
    surface would make a lease a liveness heartbeat instead of a deadline, and
    the bound would then depend on a tuning constant.

    Checked over the SOURCE, because a runtime test cannot distinguish "returned
    promptly" from "polled once and got lucky". `threading` and `signal` are
    covered for the whole module by test_llm_spend_ledger.py's import-hygiene
    test, which allowlists the stdlib modules this file may import.

    Revert shown red (an INSERTION, the property being an absence): added

        while self._reclaim_batch_limit == 99:
            time.sleep(0.01)

    at the top of `reclaim_orphans` -> this test failed with
    `AssertionError: SpendLedger.reclaim_orphans contains a loop or a sleep:
    ['While', 'time.sleep']`. Removed again.
    """
    offenders = {}
    for name in ("renew", "reclaim_orphans", "_sweep_expired"):
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
# The stale-generation path: CAS loss -> raise -> leak row
# ---------------------------------------------------------------------------


@pytest.fixture
def reclaimed(tmp_path):
    """A ledger holding exactly one reclaimed row, and its stale receipt."""
    ledger = make(tmp_path, cap_usd=1.0, name="reclaimed.sqlite")
    reservation = ledger.reserve(0.4, identifier="orphan", ttl_s=TTL_S)
    wait_past(reservation.lease_expires_at)
    assert ledger.reclaim_orphans().reclaimed_ids == (reservation.id,)
    return ledger, reservation


class TestLateSettleWritesALeak:
    def test_a_late_settle_loses_the_cas_raises_and_records_the_cost(self, reclaimed):
        """Section 5 and 7, end to end: the settle's compare-and-set on
        `(id, generation, state='open')` fails against the reclaimed row (whose
        generation the sweep bumped), `StaleReservationError` is raised, and the
        presented cost is appended to `leaks` rather than discarded -- because
        that money was really spent against headroom the sweep gave away.

        The leak row must SURVIVE the raise. `_writer` rolls back on any
        exception leaving its body, so the raise is deferred past the transaction
        exactly as the overbilled raise is.

        Revert shown red, twice over:
        (1) deleted the `INSERT INTO leaks ...` from `settle`'s lost-CAS branch
            -> this test failed with `assert 0 == 1` on `len(recorded)`, the real
            cost having been discarded.
        (2) restored the INSERT but raised `stale` INSIDE the `with self._writer()`
            block instead of after it -> this test failed with `assert 0 == 1` on
            the same assertion, the rollback having erased the row the INSERT had
            just written. This is the one a reviewer would not predict: the code
            LOOKS like it records the cost either way.
        Both restored.
        """
        ledger, reservation = reclaimed
        with pytest.raises(sl.StaleReservationError, match="no longer open at generation"):
            ledger.settle(reservation, 0.3)

        recorded = leaks(ledger)
        assert len(recorded) == 1
        assert recorded[0]["ledger_id"] == reservation.id
        assert recorded[0]["reported_cost"] == sl.usd_to_nano(0.3)
        assert recorded[0]["generation"] == reservation.generation
        assert recorded[0]["presented_at"]
        # The `ledger` row is untouched: no double charge, no sixth state.
        assert rows(ledger)[reservation.id]["state"] == "reclaimed"
        assert rows(ledger)[reservation.id]["settled"] == 0

    def test_the_leaked_cost_appears_in_leaked_and_outstanding(self, reclaimed):
        """Section 7: LEAKED sums `reported_cost` over ALL leaks rows, and LEAKED
        is one of the four terms of OUTSTANDING -- so a cost that arrived too late
        to be a reservation still charges the cap.

        Revert shown red: same revert as above, (1) deleting the `leaks` INSERT
        -> this test failed with `assert 0.0 == 0.3` on `leaked_usd`: the cap
        stopped counting money that had really been spent. Restored.
        """
        ledger, reservation = reclaimed
        before = ledger.status()
        assert (before.leaked_usd, before.outstanding_usd) == (0.0, 0.0)

        with pytest.raises(sl.StaleReservationError):
            ledger.settle(reservation, 0.3)

        after = ledger.status()
        assert after.leaked_usd == pytest.approx(0.3)
        assert after.outstanding_usd == pytest.approx(0.3)
        assert after.remaining_usd == pytest.approx(0.7)
        # Still not a reservation, and still not a settled call.
        assert (after.reservations_open, after.calls_settled, after.requests) == (0, 0, 1)

    def test_written_off_names_the_crash_case_and_drops_to_zero_on_a_late_settle(
        self, reclaimed
    ):
        """Section 7: `written_off_usd = sum(reserved)` over reclaimed rows having
        NO leaks row -- the honest name for money a reclaim freed and no settle
        ever accounted for. Once a late settle arrives for that row it is no
        longer written off, and `reclaimed_usd` minus `written_off_usd` is the
        part that WAS accounted for.

        Revert shown red: deleted the `leaks` INSERT from `settle`'s lost-CAS
        branch -> this test failed with `assert 0.4 == 0.0` on
        `written_off_usd`: a row whose cost had just been presented went on being
        reported as unaccounted-for crash money forever. Restored.
        """
        ledger, reservation = reclaimed
        before = ledger.status()
        assert before.reclaimed_usd == pytest.approx(0.4)
        assert before.written_off_usd == pytest.approx(0.4)  # the crash case

        with pytest.raises(sl.StaleReservationError):
            ledger.settle(reservation, 0.3)

        after = ledger.status()
        assert after.written_off_usd == 0.0
        assert after.reclaimed_usd == pytest.approx(0.4)  # disclosure is unchanged
        assert after.reclaimed_usd - after.written_off_usd == pytest.approx(0.4)

    def test_a_late_settle_above_its_reservation_is_stale_not_overbilled(self, reclaimed):
        """A cost above the reservation is only `overbilled` when the row was
        still the caller's to resolve. Against a reclaimed row the compare-and-set
        is lost first, so the verdict is STALE: no `overbilled` row, no
        `overbilled` halt, and the whole presented cost recorded as a leak.

        This is the ordering the code makes explicit by raising `stale` before
        the overbilled check -- `settle` computes `state = "overbilled"` from a
        pre-update SELECT that found nothing, and that computed state must not
        become a halt. A ledger halted as `overbilled` needs `resume(force=True)`,
        so getting this wrong would wedge a run on account of a dead process's
        receipt.

        Revert shown red: changed the trailing `if stale is not None: raise stale`
        to run AFTER the `if state == "overbilled": raise overbilled` check ->
        this test failed with
        `UnboundLocalError: cannot access local variable 'overbilled' where it is
        not associated with a value`, which is the same defect arriving as a
        crash. Restored.
        """
        ledger, reservation = reclaimed  # reserved 0.4, already reclaimed
        with pytest.raises(sl.StaleReservationError):
            ledger.settle(reservation, 0.9)  # more than the 0.4 reserved

        status = ledger.status()
        assert status.halted is False
        assert status.halt_reason == ""
        assert rows(ledger)[reservation.id]["state"] == "reclaimed"
        assert [leak["reported_cost"] for leak in leaks(ledger)] == [sl.usd_to_nano(0.9)]
        # The whole presented cost is leaked, and it may exceed the cap: that is
        # the overshoot being reported rather than hidden.
        assert status.leaked_usd == pytest.approx(0.9)
        assert status.remaining_usd == pytest.approx(0.1)

    def test_a_repeated_settle_against_a_resolved_row_writes_no_leak(self, tmp_path):
        """A lost CAS is not by itself a leak. A retried settle against an
        already-`settled` row has nothing new to record, and writing one would
        double-charge AND violate the structural clause
        `ledger_row_not_reclaimed`.

        Revert shown red: removed the
        `SELECT 1 FROM ledger WHERE id = ? AND state = 'reclaimed'` guard, so
        every lost CAS wrote a leak -> this test failed with
        `LedgerStateInvalid: ...: leaks violates ledger_row_not_reclaimed` out of
        the following `ledger.status()`, the ledger having been left structurally
        invalid by a retry. Restored.
        """
        ledger = make(tmp_path, cap_usd=1.0)
        reservation = ledger.reserve(0.4, ttl_s=LIVE_TTL_S)
        ledger.settle(reservation, 0.2)
        with pytest.raises(sl.StaleReservationError):
            ledger.settle(reservation, 0.2)
        assert leaks(ledger) == []
        assert ledger.status().leaked_usd == 0.0
        assert ledger.status().settled_usd == pytest.approx(0.2)

    def test_a_late_settle_with_an_unreadable_cost_writes_no_leak(self, reclaimed):
        """`settle(None)` presents NO cost, so there is nothing to record: the
        structural clause `reported_cost_not_positive` refuses a NULL or
        non-positive `reported_cost`, and inventing the reserved amount would
        charge the cap for money nobody measured.

        Revert shown red: dropped the `cost_nano is not None and cost_nano > 0`
        condition and inserted `cost_nano` directly -> this test failed with
        `sqlite3.IntegrityError: NOT NULL constraint failed:
        leaks.reported_cost`. Restored.
        """
        ledger, reservation = reclaimed
        with pytest.raises(sl.StaleReservationError):
            ledger.settle(reservation, None)
        assert leaks(ledger) == []
        assert ledger.status().written_off_usd == pytest.approx(0.4)

    def test_a_late_settle_of_zero_writes_no_leak(self, reclaimed):
        """Zero is a measured cost and not a leak: there is no money to account
        for, and `reported_cost <= 0` is structurally refused.

        Revert shown red: dropped the `cost_nano > 0` half of the guard -> this
        test failed with the leak list holding `{'reported_cost': 0, ...}`, a row
        the clause `reported_cost_not_positive` would refuse on the next read.
        Restored.
        """
        ledger, reservation = reclaimed
        with pytest.raises(sl.StaleReservationError):
            ledger.settle(reservation, 0.0)
        assert leaks(ledger) == []

    def test_a_late_release_writes_no_leak(self, reclaimed):
        """`release` asserts that NOTHING was billed, so a lost release presents
        no cost -- the leak path belongs to `settle` alone."""
        ledger, reservation = reclaimed
        with pytest.raises(sl.StaleReservationError, match="nothing was released"):
            ledger.release(reservation)
        assert leaks(ledger) == []

    def test_a_late_settle_is_recorded_while_the_ledger_is_halted(self, reclaimed):
        """SL-2: `settle` is never gated by the halt, because money already spent
        must be recorded regardless. That extends to the leak write -- a halt is
        exactly when a reclaimed orphan's late cost is most likely to arrive."""
        ledger, reservation = reclaimed
        ledger.halt("operator", "stopping the run")
        with pytest.raises(sl.StaleReservationError):
            ledger.settle(reservation, 0.3)
        assert len(leaks(ledger)) == 1
        status = ledger.status()
        assert status.halted is True
        assert status.leaked_usd == pytest.approx(0.3)

    def test_reclaim_is_not_gated_by_the_halt(self, tmp_path):
        """Reclaiming is accounting, not admission: a halted ledger must still be
        able to tell freed headroom from money written off."""
        ledger = make(tmp_path, cap_usd=1.0)
        reservation = ledger.reserve(0.4, ttl_s=TTL_S)
        ledger.halt("operator", "stopping the run")
        wait_past(reservation.lease_expires_at)
        assert ledger.reclaim_orphans().reclaimed_ids == (reservation.id,)
        assert ledger.status().reclaimed_usd == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# The partition, RE-ASSERTED with `leaks` rows PRESENT -- section 7 (a)..(e)
# ---------------------------------------------------------------------------


@pytest.fixture
def mixed_with_leaks(tmp_path):
    """A row in every one of the five states, plus REAL leak rows.

    SL-1's equivalent fixture injects its `reclaimed` row with raw SQL and keeps
    `leaks` empty, because the write path is this unit's. Here every row is
    produced by the public API: the reclaimed row by `reclaim_orphans` and the
    leak row by a late `settle` that lost its compare-and-set.
    """
    ledger = make(tmp_path, cap_usd=100.0, halt_on_overbilled=False, name="mixed.sqlite")
    ledger.reserve(1.0, identifier="still-open")  # open:       reserved 1.0
    ledger.settle(ledger.reserve(2.0), None)  # unknown:    reserved 2.0
    ledger.settle(ledger.reserve(3.0), 1.5)  # settled:    settled  1.5
    over = ledger.reserve(4.0)
    with pytest.raises(sl.SpendCapExceeded):
        ledger.settle(over, 5.0)  # overbilled: reserved 4.0, settled 5.0

    # reclaimed (0.5) + one leak row (0.25), both by the real path.
    orphan = ledger.reserve(0.5, identifier="orphan", ttl_s=TTL_S)
    wait_past(orphan.lease_expires_at)
    assert ledger.reclaim_orphans().reclaimed_ids == (orphan.id,)
    with pytest.raises(sl.StaleReservationError):
        ledger.settle(orphan, 0.25)

    # A second reclaimed row with NO leak row, so `written_off_usd` is non-zero
    # and assertion (d)'s quantifier is tested over a mixed set rather than a
    # uniform one.
    written_off = ledger.reserve(0.75, identifier="never-settled", ttl_s=TTL_S)
    wait_past(written_off.lease_expires_at)
    assert ledger.reclaim_orphans().reclaimed_ids == (written_off.id,)
    return ledger


class TestPartitionWithLeaksPresent:
    def test_leaks_is_NOT_empty_in_this_unit(self, mixed_with_leaks):
        """The premise of this whole class, and the one thing that distinguishes
        it from SL-1's partition tests. SL-1 asserts `leaks` is empty in ITS
        fixture; that is a statement about that fixture, not about the table, so
        the two assertions do not collide -- they bracket the same invariants
        over the two cases that exist.
        """
        conn = raw(mixed_with_leaks)
        try:
            assert conn.execute("SELECT COUNT(*) FROM leaks").fetchone()[0] == 1
        finally:
            conn.close()

    def test_status_matches_an_independently_computed_partition(self, mixed_with_leaks):
        """All six terms, cross-checked against SQL written separately from the
        production queries, with leaks rows contributing.

        Revert shown red: deleted the `leaks` INSERT from `settle` -> this test
        failed on `status.leaked_usd == ... == 1.25` (reported
        `1.0 = nano_to_usd(1000000000)`, the overbilled excess alone).

        PREDICTION CORRECTED, and it is vacuous-checks shape 1. The independent
        recomputation does NOT carry this property: it reads the same rows, so
        with the INSERT deleted BOTH sides drop to 1.0 and agree. What went red is
        the LITERAL expected value on each line. Every `== <number>` in this test
        is therefore load-bearing and must not be relaxed to a comparison against
        `expect` alone.
        """
        expect = independent_partition(mixed_with_leaks)
        status = mixed_with_leaks.status()
        assert status.settled_usd == sl.nano_to_usd(expect["settled"]) == 5.5
        assert status.reserved_usd == sl.nano_to_usd(expect["reserved"]) == 1.0
        assert status.unknown_usd == sl.nano_to_usd(expect["unknown"]) == 2.0
        # 1.0 overbilled excess + 0.25 from the leak row.
        assert status.leaked_usd == sl.nano_to_usd(expect["leaked"]) == 1.25
        assert status.reclaimed_usd == sl.nano_to_usd(expect["reclaimed"]) == 1.25
        assert status.written_off_usd == sl.nano_to_usd(expect["written_off"]) == 0.75
        assert status.outstanding_usd == sl.nano_to_usd(expect["outstanding"]) == 9.75

    def test_a_reclaimed_row_still_contributes_zero_to_all_four_terms(
        self, mixed_with_leaks
    ):
        """Its headroom was freed by construction; the cost that arrived later
        counts as a `leaks` row, not as the reclaimed reservation. Both reclaimed
        rows together are $1.25 of `reclaimed_usd` and 0 of OUTSTANDING; only the
        $0.25 leak charges the cap.
        """
        status = mixed_with_leaks.status()
        assert status.reclaimed_usd == pytest.approx(1.25)
        # The four terms account for exactly the non-reclaimed money plus the leak.
        assert (
            status.settled_usd + status.reserved_usd + status.unknown_usd
            + status.leaked_usd
        ) == pytest.approx(status.outstanding_usd)
        assert status.outstanding_usd == pytest.approx(5.5 + 1.0 + 2.0 + 1.25)

    # -- (a) ----------------------------------------------------------------

    def test_a_outstanding_never_exceeds_the_cap_with_leaks_present(self, tmp_path):
        """(a) `OUTSTANDING <= cap` at every snapshot, OUTSIDE a declared reclaim
        window. A leak row is counted in OUTSTANDING, so this is checked across
        the whole reclaim-then-late-settle sequence -- and the sequence is kept
        out of the excluded window by never reserving against the freed headroom.
        """
        ledger = make(tmp_path, cap_usd=1.0)
        reservation = ledger.reserve(0.4, ttl_s=TTL_S)
        assert ledger.status().outstanding_usd <= ledger.cap_usd
        wait_past(reservation.lease_expires_at)
        ledger.reclaim_orphans()
        assert ledger.status().outstanding_usd <= ledger.cap_usd
        with pytest.raises(sl.StaleReservationError):
            ledger.settle(reservation, 0.3)
        assert ledger.status().outstanding_usd <= ledger.cap_usd

    # -- (b) ----------------------------------------------------------------

    def test_b_the_five_state_counts_sum_to_the_ledger_row_count(self, mixed_with_leaks):
        """(b) over `ledger` ALONE the five per-state counts sum to `COUNT(*)`,
        so no row is in two states and none in none. `leaks` is a DIFFERENT
        table, so a leak row neither inflates the count nor becomes a sixth
        state -- which is the half SL-1 could not check, its table being empty.

        Revert shown red: changed the sweep's UPDATE to write
        `state = 'orphaned'` -> this test ERRORED in its fixture with
        `LedgerStateInvalid: ... ledger violates state_outside_five`, the
        structural clause firing on the transaction after the sweep. An error is
        red, and it lands one step EARLIER than the count assertion this test was
        written around -- which is itself the finding: a sixth state cannot
        survive long enough to be counted. Restored.
        """
        mixed_with_leaks.release(mixed_with_leaks.reserve(0.5))
        conn = raw(mixed_with_leaks)
        try:
            total = conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
            per_state = {
                state: conn.execute(
                    "SELECT COUNT(*) FROM ledger WHERE state = ?", (state,)
                ).fetchone()[0]
                for state in sl.LEDGER_STATES
            }
            leak_count = conn.execute("SELECT COUNT(*) FROM leaks").fetchone()[0]
        finally:
            conn.close()
        assert total == 7  # 5 states + a second reclaimed row + the release
        assert sum(per_state.values()) == total
        assert all(count >= 1 for count in per_state.values())
        assert leak_count == 1  # present, and NOT counted in `ledger`

    # -- (c) ----------------------------------------------------------------

    def test_c_the_three_counts_are_over_ledger_only(self, mixed_with_leaks):
        """(c) all three counts are over `ledger`, so a leak row moves none of
        them. A reclaimed row is not open, not settled, and still a request.
        """
        conn = raw(mixed_with_leaks)
        try:
            expect_open = conn.execute(
                "SELECT COUNT(*) FROM ledger WHERE state IN ('open', 'unknown')"
            ).fetchone()[0]
            expect_requests = conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
            expect_settled = conn.execute(
                "SELECT COUNT(*) FROM ledger WHERE state = 'settled'"
            ).fetchone()[0]
        finally:
            conn.close()
        status = mixed_with_leaks.status()
        assert status.reservations_open == expect_open == 2
        assert status.requests == expect_requests == 6
        assert status.calls_settled == expect_settled == 1
        # The one leak row is in none of the three.
        assert status.requests == 6 and len(leaks(mixed_with_leaks)) == 1

    # -- (d) ----------------------------------------------------------------

    def test_d_leaked_is_the_overbilled_excess_plus_every_leak_row(
        self, mixed_with_leaks
    ):
        """(d) first half, with rows PRESENT: LEAKED is the overbilled excess
        ($1.00) plus every `reported_cost` ($0.25).

        Revert shown red: deleted the `leaks` INSERT from `settle` -> this test
        failed with `assert 0.0 == 0.25 +- 2.5e-07` on the summed
        `reported_cost`. Restored. (SL-1 showed the production TERM red by
        injecting a row; this shows the WRITE red, which is the half SL-1 could
        not reach.)
        """
        conn = raw(mixed_with_leaks)
        try:
            excess = conn.execute(
                "SELECT COALESCE(SUM(settled - reserved), 0) FROM ledger "
                "WHERE state = 'overbilled'"
            ).fetchone()[0]
            reported = conn.execute(
                "SELECT COALESCE(SUM(reported_cost), 0) FROM leaks"
            ).fetchone()[0]
        finally:
            conn.close()
        assert sl.nano_to_usd(excess) == pytest.approx(1.0)
        assert sl.nano_to_usd(reported) == pytest.approx(0.25)
        assert mixed_with_leaks.status().leaked_usd == pytest.approx(
            sl.nano_to_usd(excess + reported)
        )

    def test_d_every_leak_row_names_an_existing_reclaimed_ledger_row(
        self, mixed_with_leaks
    ):
        """(d) second half, with the quantifier NON-VACUOUS for the first time:
        every `leaks.ledger_id` names an existing `ledger` row in state
        `reclaimed`. SL-1 could only drive the two structural clauses directly;
        here the rows the production write path produced are checked against them.

        Revert shown red: changed the sweep's UPDATE to a
        `DELETE FROM ledger ...` -> this test failed with `assert []` on
        `assert recorded`, the fixture's late settle having found no reclaimed row
        to attribute its cost to and written nothing. Restored.

        PREDICTION CORRECTED. The revert first written here was "remove the
        `state = 'reclaimed'` guard from the leak write", and against this fixture
        it ran GREEN -- the fixture's only lost CAS is already against a reclaimed
        row, so removing the guard changes nothing it does. That guard IS
        load-bearing, but its carrier is
        `test_a_repeated_settle_against_a_resolved_row_writes_no_leak`, which went
        red on exactly that revert. Retention is what THIS test carries.
        """
        recorded = leaks(mixed_with_leaks)
        assert recorded  # non-vacuous
        stored = rows(mixed_with_leaks)
        for leak in recorded:
            assert leak["ledger_id"] in stored
            assert stored[leak["ledger_id"]]["state"] == "reclaimed"
            assert int(leak["reported_cost"]) > 0
        conn = raw(mixed_with_leaks)
        try:
            dangling = conn.execute(
                "SELECT COUNT(*) FROM leaks WHERE ledger_id NOT IN "
                "(SELECT id FROM ledger WHERE state = 'reclaimed')"
            ).fetchone()[0]
        finally:
            conn.close()
        assert dangling == 0

    # -- (e) ----------------------------------------------------------------

    def test_e_the_structural_query_returns_no_rows_over_both_tables(
        self, mixed_with_leaks
    ):
        """(e) the structural query of section 5, over BOTH tables, returns 0
        rows -- now with the `leaks` clauses applied to real rows. `status()`
        runs it, so a clean read IS the assertion; the per-clause check below
        makes the failure diagnosable.
        """
        mixed_with_leaks.status()  # validates both tables, raises if malformed
        conn = raw(mixed_with_leaks)
        try:
            for table, clause, query in sl.STRUCTURAL_CLAUSES:
                found = conn.execute(query + " LIMIT 1").fetchone()
                assert found is None, f"{table} violates {clause}: {found}"
        finally:
            conn.close()
        # And the leaks clauses had something to run over.
        assert len(leaks(mixed_with_leaks)) == 1


# ---------------------------------------------------------------------------
# The crash case: a REAL process killed between reserve and settle
# ---------------------------------------------------------------------------

# A real OS process, not a mock and not a thread: reclaim exists for a process
# that DIES holding a reservation, and no in-process construct reproduces that --
# a thread cannot leave a committed row behind with its own stack gone, and a
# mock proves nothing about what SQLite kept. The child opens the ledger,
# reserves with a lease, publishes the receipt through an atomically-renamed file
# the parent polls for, and then blocks forever so the parent can kill it at
# exactly the point between `reserve` and `settle`.
_CRASH_SCRIPT = """
import os
import sys
import time
sys.path.insert(0, sys.argv[1])
from content_pipeline.llm import spend_ledger as sl

db_path, receipt_path, amount, ttl = sys.argv[2:6]
ledger = sl.open_ledger(db_path, busy_timeout_ms=30000)
reservation = ledger.reserve(
    float(amount), identifier="crashed-attempt", scope="attempt", ttl_s=float(ttl)
)
# Publish the COMMITTED receipt atomically, so the parent never reads a partial
# line and the file's existence means the row is really in the database.
partial = receipt_path + ".part"
with open(partial, "w") as handle:
    handle.write("%s %d %d\\n" % (
        reservation.id, reservation.generation, reservation.amount_nano
    ))
os.replace(partial, receipt_path)
# Between reserve and settle. The parent kills this process here; it must never
# exit on its own, or the test would be observing an orderly shutdown instead.
while True:
    time.sleep(3600)
"""


def _read_receipt(path):
    reservation_id, generation, amount_nano = path.read_text(encoding="utf-8").split()
    return sl.Reservation(
        id=reservation_id,
        generation=int(generation),
        amount_usd=sl.nano_to_usd(int(amount_nano)),
        amount_nano=int(amount_nano),
        scope="attempt",
        identifier="crashed-attempt",
        created_at="",
        lease_expires_at=None,
    )


class TestCrashBetweenReserveAndSettle:
    def test_a_killed_process_leaves_an_open_row_that_reclaim_resolves(self, tmp_path):
        """The crash reclaim exists for, end to end, with a real process.

        A process killed between `reserve` and `settle` leaves a committed `open`
        row that nothing will ever resolve. The sequence asserted:

        1. the child's reservation is committed and fills the cap;
        2. the child is KILLED -- no settle, no release, no `finally`;
        3. the expired lease is NOT self-cleaning: the row stays `open` and
           keeps consuming the cap until something sweeps (so an admission is
           still refused);
        4. `reclaim_orphans` frees exactly the reserved amount and RETAINS the
           row;
        5. the child's cost, had it ever been measured, would land as a `leaks`
           row -- asserted by presenting the dead process's own receipt.

        The causal observable the parent polls is the RECEIPT FILE the child
        renames into place after its reserve transaction commits: its existence
        means the row is in the database, which a sleep could only guess at. The
        parent additionally polls `proc.poll()` after the kill, so the assertions
        run against a process that is really gone.

        Revert shown red: changed the sweep's UPDATE to
        `DELETE FROM ledger WHERE state = 'open' AND id IN (...)` -> this test
        failed with `assert 0 == 1` on `status().requests` at step 4, and then on
        the leak row at step 5, a deleted row being unable to carry the
        attribution. Restored.
        """
        ledger = make(tmp_path, cap_usd=1.0, name="crash.sqlite")
        receipt = tmp_path / "receipt.txt"
        proc = subprocess.Popen(
            [
                sys.executable, "-c", _CRASH_SCRIPT,
                LIB_ROOT, str(ledger.path), str(receipt), "1.0", str(TTL_S),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # 1. The reserve really committed -- the file appears only after it.
            _wait_for(
                lambda: receipt.exists() or proc.poll() is not None,
                timeout_s=120,
                what="the child process to commit its reservation",
            )
            assert proc.poll() is None, (
                "the child exited instead of blocking between reserve and settle: "
                + proc.communicate(timeout=60)[1]
            )
            reservation = _read_receipt(receipt)
            assert ledger.status().reserved_usd == 1.0
            assert ledger.status().reservations_open == 1

            # 2. Kill it. No settle, no release, no `finally`.
            proc.kill()
            _wait_for(
                lambda: proc.poll() is not None,
                timeout_s=120,
                what="the killed child process to be reaped",
            )
        finally:
            if proc.poll() is None:  # any failure path above must not leak it
                proc.kill()
            proc.communicate(timeout=120)

        # 3. The orphan holds its headroom: an expired lease is not self-cleaning.
        wait_past(time.time() + TTL_S)
        assert rows(ledger)[reservation.id]["state"] == "open"
        assert ledger.status().outstanding_usd == 1.0
        with pytest.raises(sl.SpendCapExceeded):
            ledger.reserve(1.0)

        # 4. The sweep frees exactly the reserved amount, and RETAINS the row.
        report = ledger.reclaim_orphans()
        assert report.reclaimed_ids == (reservation.id,)
        assert report.freed_nano == reservation.amount_nano
        status = ledger.status()
        assert status.outstanding_usd == 0.0
        assert status.requests == 1  # retained
        assert status.reclaimed_usd == 1.0
        assert status.written_off_usd == 1.0  # nothing ever accounted for it
        assert rows(ledger)[reservation.id]["state"] == "reclaimed"

        # 5. The dead process's receipt is still attributable.
        with pytest.raises(sl.StaleReservationError):
            ledger.settle(reservation, 0.6)
        assert [leak["ledger_id"] for leak in leaks(ledger)] == [reservation.id]
        assert ledger.status().written_off_usd == 0.0
        assert ledger.status().leaked_usd == pytest.approx(0.6)

    def test_a_second_process_in_lease_mode_reclaims_the_dead_ones_row(self, tmp_path):
        """The cross-process shape of the same crash: the row a dead process left
        is swept by ANOTHER process's `reserve`, which is how a run recovers
        without an operator. The sweep runs inside that admission's own
        `BEGIN IMMEDIATE` transaction, so the headroom it frees cannot be read by
        one process and spent by another.

        Revert shown red: removed the `"lease"`-mode sweep from `reserve` -> this
        test failed with `SpendCapExceeded` out of the surviving process's
        `reserve`, nothing having freed the dead process's row. Restored.
        """
        ledger = make(tmp_path, cap_usd=1.0, name="handoff.sqlite")
        receipt = tmp_path / "receipt.txt"
        proc = subprocess.Popen(
            [
                sys.executable, "-c", _CRASH_SCRIPT,
                LIB_ROOT, str(ledger.path), str(receipt), "1.0", str(TTL_S),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _wait_for(
                lambda: receipt.exists() or proc.poll() is not None,
                timeout_s=120,
                what="the child process to commit its reservation",
            )
            assert proc.poll() is None, proc.communicate(timeout=60)[1]
            reservation = _read_receipt(receipt)
            proc.kill()
            _wait_for(
                lambda: proc.poll() is not None,
                timeout_s=120,
                what="the killed child process to be reaped",
            )
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=120)

        wait_past(time.time() + TTL_S)
        survivor = sl.open_ledger(
            ledger.path, busy_timeout_ms=30000, orphan_reclaim="lease"
        )
        granted = survivor.reserve(1.0, identifier="survivor", ttl_s=LIVE_TTL_S)
        assert granted.id != reservation.id
        assert rows(ledger)[reservation.id]["state"] == "reclaimed"
        assert survivor.status().outstanding_usd == 1.0
        assert survivor.status().reclaimed_usd == 1.0
