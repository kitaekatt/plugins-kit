"""SL-4: the cross-process acceptance suite for the spend ledger.

Design: `dev/tasks/loc-pipeline-consumer-needs/design-cross-process-spend-ledger.md`
sections 4 (the concurrency primitive and its safety argument), 6 (halt), 7 (the
status view) and 11/11.1 (the test plan and the source guard).

THIS FILE IS THE ACCEPTANCE CHECK for the whole spend-ledger strand, quoted from
plan.md: "two concurrent processes reserving against one cap never exceed it;
halt stops both; status reports reserved and spent". Each clause is one test
class below, plus a busy-timeout exhaustion test and the `BEGIN IMMEDIATE`
source guard of section 11.1.

Nothing here is mocked and nothing is simulated with threads. Every test drives
real `sys.executable` subprocesses against a real temp-file ledger, because the
property under test is precisely the one an in-process construct cannot
reproduce: two OS processes contending for one SQLite write lock. Subprocesses
receive the library path in argv -- `conftest.py` patches only the parent's
`sys.path`.

Contention is ASSERTED, not assumed. A concurrency test that passes because its
processes never actually raced is worse than no test, so every multi-process
test here carries an explicit contention floor: a write lock held by a gate
process until every child is AT its `reserve` call (the cap tests), rows
committed inside the halting process's own window (the halt test), a
raced-commit floor plus an observed-open-row floor (the status test). Those
floors fail the test rather than letting a green run certify an unexercised
property.

The rendezvous is the barrier DIRECTORY of `test_execution_store.py`'s
`_RENDEZVOUS_CLAIM_SCRIPT`, not `threading.Barrier` -- a thread barrier cannot
span processes, and the comment on that script records the failure mode the
placement fixes: with the barrier before the store was opened, one process
routinely finished before the other arrived. Here the ledger is opened BEFORE
the barrier so the barrier sits immediately in front of `reserve`.

Every load-bearing assertion was shown RED by reverting the production line it
protects (docs/reference/vacuous-checks.md); each revert is named in the test's
docstring so the counterfactual can be re-run.

Scope boundaries, so a reader does not look here for them: the ledger core is
SL-1's `test_llm_spend_ledger.py`; the halt surface in ONE process is SL-2's
`test_llm_spend_ledger_halt.py`, which also carries the cross-query consistency
("torn read") property of `status()` under live writers -- this file asserts the
different thing the acceptance clause asks for, that the VALUES status reports
are the reserved and spent money of other processes; leases and reclaim are
SL-3's `test_llm_spend_ledger_orphans.py`. `cli.budget.spend_stop` is SL-6's and
does not exist yet, so the pass-through clause of design test 4 is asserted here
only as far as it can be: that the error a caller would have to translate is
outside the `BudgetExceededError` hierarchy.
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

#: Every amount in this file is an exact binary fraction of a dollar, so the
#: USD <-> nano round trip through `SpendStatus`'s floats is exact and a test
#: never fails on representation. 0.125 USD is exactly 125,000,000 nano-USD.
AMOUNT_USD = 0.125
AMOUNT_NANO = 125_000_000
#: Half the reservation, so a settle is never `overbilled`.
COST_USD = 0.0625
COST_NANO = 62_500_000

#: Bounds on every wait. Generous enough that a loaded machine does not fail,
#: finite so a defect cannot hang the suite.
READY_TIMEOUT_S = 120
RACE_TIMEOUT_S = 120


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make(tmp_path, *, cap_usd=1.0, run_id="run-1", name="ledger.sqlite", **kwargs):
    return sl.create_ledger(tmp_path / name, cap_usd=cap_usd, run_id=run_id, **kwargs)


def _wait_for(predicate, *, timeout_s, what):
    """Poll a causal observable. Never a bare sleep sized for an idle machine."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    raise AssertionError(f"timed out after {timeout_s}s waiting for {what}")


def _kill_all(procs):
    """Kill every child, unconditionally. Called from a `finally` in every test.

    A test must not be able to leave a spinning child behind, and a failed
    assertion must not be able to hang the suite on a `communicate()` that
    waits for a process designed never to exit on its own.
    """
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
    for proc in procs:
        try:
            proc.communicate(timeout=30)
        except Exception:  # noqa: BLE001 -- teardown must never mask the real failure
            pass


def _identifier_counts(ledger):
    """Rows per `identifier`, read raw: which process committed how many."""
    conn = sqlite3.connect(str(ledger.path))
    try:
        return {
            str(row[0]): int(row[1])
            for row in conn.execute(
                "SELECT identifier, COUNT(*) FROM ledger GROUP BY identifier"
            )
        }
    finally:
        conn.close()


# A wall-clock interval-overlap check used to live here and was REMOVED, which
# is worth recording because the replacement is the point. Each child reported
# `time.time()` either side of its own `reserve` and the test asserted that two
# intervals intersected. With eight processes that held 8-of-8 on every run; with
# TWO it failed in 4 of 10 runs, because the loser can leave the barrier a
# millisecond late and the winner's whole reserve fits inside that. The flake was
# in the EVIDENCE, not in the ledger -- the cap assertions passed every time --
# but an intermittent floor is a defect either way, and widening the tolerance
# would have turned the floor into decoration. The deterministic replacement is
# the lock holder below: overlap is now constructed and proven without a clock.


# ---------------------------------------------------------------------------
# Child scripts
# ---------------------------------------------------------------------------

# Opens the ledger, rendezvouses on the barrier DIRECTORY, then reserves exactly
# once. The ledger is opened BEFORE the barrier so the barrier sits immediately
# in front of `reserve`: the processes' reserve ATTEMPTS overlap, not merely
# their lifetimes. `busy_timeout_ms` is large on purpose -- this test is about
# the CAP, so a lock wait must be a latency cost and never an error (the
# exhaustion path has its own test below).
_RESERVE_SCRIPT = """
import os
import sys
import time
sys.path.insert(0, sys.argv[1])
from content_pipeline.llm import spend_ledger as sl

db_path, worker_id, barrier_dir, amount, expected, entering_dir = sys.argv[2:8]
ledger = sl.open_ledger(db_path, busy_timeout_ms=60000)

open(os.path.join(barrier_dir, worker_id + ".ready"), "w").close()
deadline = time.time() + 120
while time.time() < deadline:
    if len(os.listdir(barrier_dir)) >= int(expected):
        break
    time.sleep(0.005)

# The LAST statement before `reserve`. The holder process releases the write
# lock only once every child has written this marker, so no child can COMPLETE
# a reserve before every other child has reached one.
open(os.path.join(entering_dir, worker_id + ".entering"), "w").close()
start = time.time()
try:
    reservation = ledger.reserve(float(amount), identifier=worker_id, scope="attempt")
    print("GRANTED %s %.6f %.6f" % (reservation.id, start, time.time()), flush=True)
except sl.SpendCapExceeded:
    print("REFUSED - %.6f %.6f" % (start, time.time()), flush=True)
"""

# Holds the write lock until every child is AT its `reserve` call, then lets
# go. This is what makes the overlap deterministic instead of a matter of
# scheduling luck: a child cannot complete `reserve` while this process holds
# the lock, and this process does not let go until all `expected` children have
# announced that their next statement is `reserve`. So "one process finished
# before the other even started" -- the failure mode the barrier-directory
# comment in `test_execution_store.py` records -- is excluded by construction,
# and the proof is this process's own report of how many markers it saw.
_LOCK_GATE_SCRIPT = """
import os
import sqlite3
import sys
import time

db_path, locked_path, entering_dir, expected = sys.argv[2:6]
conn = sqlite3.connect(db_path, timeout=60)
conn.isolation_level = None
conn.execute("PRAGMA busy_timeout = 60000")
conn.execute("BEGIN IMMEDIATE")
conn.execute(
    "INSERT INTO halt_history (action, reason, detail, forced, at) "
    "VALUES ('probe', NULL, NULL, 0, 'lock-gate')"
)
open(locked_path, "w").close()

seen = 0
deadline = time.time() + 120
while time.time() < deadline:
    seen = len(os.listdir(entering_dir))
    if seen >= int(expected):
        break
    time.sleep(0.005)
conn.rollback()
conn.close()
print("RELEASED %d" % seen, flush=True)
"""

# A separate OBSERVER process. It samples `status()` across the whole race and
# asserts OUTSTANDING <= cap on EVERY sample, from outside every reserving
# process -- so the bound is checked by something that holds no reservation of
# its own and takes no write lock (`status()` runs under the plain deferred
# BEGIN). It publishes its first-sample and last-sample wall clocks so the
# parent can prove the sampling window really spanned the race.
_OBSERVER_SCRIPT = """
import os
import sys
import time
sys.path.insert(0, sys.argv[1])
from content_pipeline.llm import spend_ledger as sl

db_path, ready_path, stop_path, cap_nano = sys.argv[2:6]
ledger = sl.open_ledger(db_path, busy_timeout_ms=60000)
cap = int(cap_nano)

samples = 0
worst = 0
violations = 0
first = 0.0
last = 0.0
deadline = time.time() + 300
while time.time() < deadline:
    status = ledger.status()
    outstanding = sl.usd_to_nano(status.outstanding_usd)
    now = time.time()
    if samples == 0:
        first = now
        open(ready_path, "w").close()
    last = now
    samples += 1
    if outstanding > worst:
        worst = outstanding
    if outstanding > cap:
        violations += 1
    if os.path.exists(stop_path):
        break
print("SAMPLES %d WORST %d VIOLATIONS %d FIRST %.6f LAST %.6f" % (
    samples, worst, violations, first, last), flush=True)
"""

# Reserves in a tight loop, settling each row at zero so headroom never runs
# out and the only thing that can stop it is the halt. Exits at the FIRST
# `SpendLedgerHalted`, reporting how many grants it got.
_HALT_RESERVER_SCRIPT = """
import sys
import time
sys.path.insert(0, sys.argv[1])
from content_pipeline.llm import spend_ledger as sl

db_path, worker_id, ready_path, amount = sys.argv[2:6]
ledger = sl.open_ledger(db_path, busy_timeout_ms=60000)
open(ready_path, "w").close()

grants = 0
# Shorter than the parent's own wait, so a BROKEN halt gate ends with this
# child printing DEADLINE and the parent asserting on it, rather than with the
# parent timing out on communicate() and losing the diagnosis.
deadline = time.time() + 90
try:
    while time.time() < deadline:
        reservation = ledger.reserve(float(amount), identifier=worker_id)
        grants += 1
        ledger.settle(reservation, 0.0)
    print("DEADLINE %d" % grants, flush=True)
except sl.SpendLedgerHalted as exc:
    print("HALTED %d %s" % (grants, exc.reason), flush=True)
"""

# Commits the halt from a DIFFERENT process than the reservers. Run to
# completion by the parent, so its exit means the halt transaction committed.
_HALT_SCRIPT = """
import sys
sys.path.insert(0, sys.argv[1])
from content_pipeline.llm import spend_ledger as sl

db_path, reason = sys.argv[2:4]
ledger = sl.open_ledger(db_path, busy_timeout_ms=60000)
ledger.halt(reason, "committed from another process")
print("HALT_COMMITTED", flush=True)
"""

# Two phases. Phase 1 reserves and settles in a loop, so the parent's
# `status()` reads overlap live commits by other processes. Phase 2 leaves
# exactly ONE reservation OPEN and then blocks, so once every writer is held
# the parent can assert an EXACT reserved total and an EXACT settled total
# against a quiesced file.
_STATUS_WRITER_SCRIPT = """
import os
import sys
import time
sys.path.insert(0, sys.argv[1])
from content_pipeline.llm import spend_ledger as sl

db_path, ready_path, hold_path, held_path, stop_path, amount, cost = sys.argv[2:9]
ledger = sl.open_ledger(db_path, busy_timeout_ms=60000)
open(ready_path, "w").close()

written = 0
deadline = time.time() + 300
while not os.path.exists(hold_path) and time.time() < deadline:
    reservation = ledger.reserve(float(amount), identifier="writer")
    ledger.settle(reservation, float(cost))
    written += 1

ledger.reserve(float(amount), identifier="held")
open(held_path, "w").close()
while not os.path.exists(stop_path) and time.time() < deadline:
    time.sleep(0.005)
print("WROTE %d" % written, flush=True)
"""

# Holds the SQLite write lock on the ledger file from another process, for as
# long as the parent wants it held. `BEGIN IMMEDIATE` plus a real INSERT, so
# the write lock is definitely HELD rather than merely requested; the row is
# rolled back, so the ledger's own tables are untouched.
_LOCK_HOLDER_SCRIPT = """
import os
import sqlite3
import sys
import time
sys.path.insert(0, sys.argv[1])

db_path, locked_path, release_path = sys.argv[2:5]
conn = sqlite3.connect(db_path, timeout=30)
conn.isolation_level = None
conn.execute("PRAGMA busy_timeout = 30000")
conn.execute("BEGIN IMMEDIATE")
conn.execute(
    "INSERT INTO halt_history (action, reason, detail, forced, at) "
    "VALUES ('probe', NULL, NULL, 0, 'lock-holder')"
)
open(locked_path, "w").close()

deadline = time.time() + 240
while not os.path.exists(release_path) and time.time() < deadline:
    time.sleep(0.005)
conn.rollback()
conn.close()
print("RELEASED", flush=True)
"""


# ---------------------------------------------------------------------------
# Acceptance clause 1: the cap, across processes
# ---------------------------------------------------------------------------


class TestCapHoldsAcrossProcesses:
    """plan.md clause 1: two concurrent processes reserving against one cap
    never exceed it. Design section 4's safety argument, and B2 of section 11.2.
    """

    @pytest.mark.parametrize(
        "processes,grants",
        [
            (2, 1),  # the acceptance clause's own shape: two processes, one fits
            (8, 3),  # wider fan-out, several winners
            (8, 1),  # maximum contention: seven processes lose the same race
        ],
    )
    def test_exactly_the_cap_is_granted(self, tmp_path, processes, grants):
        """`processes` children reserve the same amount against a cap of exactly
        `grants` times it. Exactly `grants` are admitted, with no duplicate
        reservation id, while a separate OBSERVER process asserts
        OUTSTANDING <= cap on every sample it takes across the race.

        The admission is `SUM(outstanding) -> compare -> INSERT` inside one
        `BEGIN IMMEDIATE` transaction, and SQLite permits one write transaction
        per file, so a loser's SUM either precedes the winner's lock acquisition
        or follows its commit. There is no interleaving in which two processes
        both read headroom that only one of them can have.

        Evidence that contention really occurred, asserted and not assumed:
        - the grant/refusal split is exactly `grants` / `processes - grants`,
          which can only arise from a shared durable total;
        - a GATE process holds the write lock until every child has announced
          that its next statement is `reserve`, and reports how many it saw, so
          no child's reserve completed before every other child reached one --
          deterministic, and with no cross-process clock comparison in it;
        - the observer's first sample precedes every child's `reserve` and its
          last sample follows every child's `reserve`, so the OUTSTANDING <= cap
          samples really span the race rather than inspecting the aftermath.

        Reverts shown red, all three parametrized cases each time:
        1. DELETED the admission test in `reserve` (`if outstanding +
           amount_nano > self._cap_nano: raise SpendCapExceeded(...)`) -> the
           OBSERVER's assertion is the one that fired, which is why it is read
           first: `AssertionError: observer saw OUTSTANDING > cap 375000000 on
           147 of 256 samples (worst 1000000000)` for the (8, 3) case, and
           `observer saw OUTSTANDING > cap 125000000 on 236 of 407 samples
           (worst 1000000000)` for a grants=1 case -- the sample counts vary per
           run, the shape does not, and `worst` is the whole 1.0 USD eight
           children can bill when nothing stops them. Every child was granted,
           so the grant-count assertion would have fired next. Restored.
        2. WEAKENED the comparison from `>` to `>=`, making admission exclusive
           at equality -> `AssertionError: granted 2 != 3` for (8, 3) and
           `granted 0 != 1` for a grants=1 case, because the reservation that
           exactly fills the remaining headroom is the one the design admits.
           Restored.

        A prediction that was wrong, recorded because the ordering above is its
        fix: the first draft asserted the grant counts BEFORE the observer's
        verdict, so revert 1 failed on `granted 8 != 3` and the observer's
        violation count was never read at all -- the assertion that carries
        "never exceed it" went unexercised by the very revert meant to
        demonstrate it. Reading the observer first is what makes that assertion
        non-vacuous.
        """
        cap_usd = sl.nano_to_usd(grants * AMOUNT_NANO)
        cap_nano = grants * AMOUNT_NANO
        ledger = make(tmp_path, cap_usd=cap_usd, name="cap.sqlite")
        assert ledger.cap_nano == cap_nano

        barrier_dir = tmp_path / "barrier"
        barrier_dir.mkdir()
        entering_dir = tmp_path / "entering"
        entering_dir.mkdir()
        observer_ready = tmp_path / "observer.ready"
        locked = tmp_path / "gate.locked"
        stop = tmp_path / "observer.stop"

        observer = subprocess.Popen(
            [
                sys.executable, "-c", _OBSERVER_SCRIPT,
                LIB_ROOT, str(ledger.path), str(observer_ready), str(stop), str(cap_nano),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        gate = subprocess.Popen(
            [
                sys.executable, "-c", _LOCK_GATE_SCRIPT,
                LIB_ROOT, str(ledger.path), str(locked), str(entering_dir),
                str(processes),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        children = []
        try:
            # The observer must be sampling BEFORE any child reserves, or its
            # samples say nothing about the race.
            _wait_for(
                lambda: observer_ready.exists() or observer.poll() is not None,
                timeout_s=READY_TIMEOUT_S,
                what="the observer process's first status() sample",
            )
            assert observer.poll() is None, "the observer exited before sampling"
            # And the gate must hold the write lock before any child reserves.
            _wait_for(
                lambda: locked.exists() or gate.poll() is not None,
                timeout_s=READY_TIMEOUT_S,
                what="the gate process to take the write lock",
            )
            assert gate.poll() is None, "the gate exited before taking the lock"

            children = [
                subprocess.Popen(
                    [
                        sys.executable, "-c", _RESERVE_SCRIPT,
                        LIB_ROOT, str(ledger.path), f"proc-{i}", str(barrier_dir),
                        str(AMOUNT_USD), str(processes), str(entering_dir),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for i in range(processes)
            ]
            outputs = []
            for child in children:
                out, err = child.communicate(timeout=RACE_TIMEOUT_S)
                assert child.returncode == 0, err
                outputs.append(out.strip())
            gate_out, gate_err = gate.communicate(timeout=RACE_TIMEOUT_S)
            assert gate.returncode == 0, gate_err
        finally:
            stop.write_text("stop\n", encoding="utf-8")
            try:
                observer_out, observer_err = observer.communicate(timeout=RACE_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                observer.kill()
                observer_out, observer_err = observer.communicate(timeout=30)
            _kill_all(children + [observer, gate])

        assert observer.returncode == 0, observer_err
        granted = [line for line in outputs if line.startswith("GRANTED")]
        refused = [line for line in outputs if line.startswith("REFUSED")]
        fields = observer_out.split()
        assert fields[0] == "SAMPLES", observer_out
        samples, worst, violations = int(fields[1]), int(fields[3]), int(fields[5])
        observer_first, observer_last = float(fields[7]), float(fields[9])

        # --- "never exceed it", asserted FIRST ------------------------------
        # The observer is the direct witness for the acceptance clause, so its
        # verdict is read before the grant counts: a revert that over-admits
        # must be reported as an exceeded cap, not merely as a surprising
        # number of winners.
        assert violations == 0, (
            f"observer saw OUTSTANDING > cap {cap_nano} on {violations} of "
            f"{samples} samples (worst {worst})"
        )

        # --- the acceptance clause itself -----------------------------------
        assert len(granted) == grants, (
            f"granted {len(granted)} != {grants}; outputs={outputs}"
        )
        assert len(refused) == processes - grants, outputs
        ids = [line.split()[1] for line in granted]
        assert len(set(ids)) == len(ids), f"duplicate reservation id: {ids}"

        status = ledger.status()
        assert status.requests == grants
        assert status.reservations_open == grants
        assert sl.usd_to_nano(status.outstanding_usd) == cap_nano
        assert sl.usd_to_nano(status.remaining_usd) == 0
        # The cap is now exactly full, so one more of the same amount refuses --
        # in THIS process, against rows every byte of which another wrote.
        with pytest.raises(sl.SpendCapExceeded):
            ledger.reserve(AMOUNT_USD)

        # --- the observer's sampling coverage -------------------------------
        assert samples > 0, observer_out
        assert worst == cap_nano, (
            f"observer never saw the cap filled (worst {worst} of {cap_nano}); "
            "its samples cannot have covered the race"
        )

        # --- evidence that the processes really contended -------------------
        # Deterministic, and clock-free: the gate held the write lock until all
        # `processes` children had announced that their next statement was
        # `reserve`, so no child's reserve COMPLETED before every other child
        # had reached one. Without this the whole test could pass on a run in
        # which each process reserved and exited before the next one started.
        assert gate_out.strip() == f"RELEASED {processes}", (
            f"the gate released the write lock having seen only "
            f"{gate_out.strip()!r} of {processes} children at their reserve "
            "call, so the reserves were not held against one another"
        )
        intervals = [(float(line.split()[2]), float(line.split()[3])) for line in outputs]
        assert observer_first <= min(start for start, _ in intervals), (
            "the observer's first sample came after a child had already reserved"
        )
        assert observer_last >= max(end for _, end in intervals), (
            "the observer stopped sampling before the last child finished reserving"
        )


# ---------------------------------------------------------------------------
# Acceptance clause 2: halt stops both
# ---------------------------------------------------------------------------


class TestHaltStopsEveryProcess:
    """plan.md clause 2: halt stops both. Design section 6, and B3 of 11.2."""

    def test_a_halt_committed_elsewhere_grants_nothing_afterwards(self, tmp_path):
        """Two reserving processes loop against one ledger; a THIRD process
        commits the halt. After that commit, zero grants are admitted in either
        reserver, and both observe the halt and stop.

        "Zero grants after the commit" is exact rather than bounded, and the
        reason is the write lock: a row inserted after the halt transaction
        committed must have taken the write lock after it, so its `reserve`
        read `halted = 1` inside its own `BEGIN IMMEDIATE` and refused. The
        latency bound of section 6 ("at most ONE in-flight ATTEMPT per
        process") is about the provider call a reservation covers, not about the
        reservation -- so the ledger-row count is frozen at the commit, which is
        what this test asserts.

        Evidence that contention really occurred: `requests` grew between the
        parent's read just before launching the halting process and its read
        just after that process exited. Those rows were committed while the
        halting process was alive and contending for the same write lock, so the
        halt landed in the middle of live reserve traffic rather than against an
        idle file. The per-identifier counts additionally show BOTH reservers
        were granting, not just one.

        Reverts shown red:
        1. DELETED the halt gate from `reserve` (the `if int(halt["halted"]):
           raise SpendLedgerHalted(...)` block) -> neither child ever saw the
           halt, both ran to their own deadline, and the test failed with
           `AssertionError: expected HALTED, got 'DEADLINE 6436'` (6,436 grants
           admitted after the halt, in that child alone). Restored.
        2. MOVED the halt read OUT of the admission transaction -- read on a
           separate `self._connect()` before `with self._writer()`, the result
           carried in -> FAILED with `AssertionError: 1 reservations were
           admitted AFTER the halt committed; assert 48 == 47`. That was a
           PREDICTION THIS FILE GOT WRONG: the draft docstring claimed this
           revert would stay green because reading the halt a moment early only
           widens the window by one reserve. It does widen it by one reserve,
           and this test's frozen-count assertion is exact rather than bounded,
           so one is enough. The in-transaction halt read is therefore covered
           here, not merely by the source guard. Restored.
        """
        ledger = make(
            tmp_path,
            cap_usd=1.0,
            name="halt.sqlite",
            synchronous="OFF",  # faster commits: more rows inside the halt window
        )
        ready = [tmp_path / f"reserver-{i}.ready" for i in range(2)]
        reservers = []
        try:
            reservers = [
                subprocess.Popen(
                    [
                        sys.executable, "-c", _HALT_RESERVER_SCRIPT,
                        LIB_ROOT, str(ledger.path), f"reserver-{i}", str(ready[i]),
                        str(AMOUNT_USD),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for i in range(2)
            ]
            _wait_for(
                lambda: all(p.exists() for p in ready)
                or any(p.poll() is not None for p in reservers),
                timeout_s=READY_TIMEOUT_S,
                what="both reserver processes to open the ledger",
            )
            for proc in reservers:
                assert proc.poll() is None, "a reserver exited before reserving"

            # Causal observable: BOTH processes have committed rows, so the halt
            # is about to land on two live writers rather than one.
            _wait_for(
                lambda: len(
                    [
                        identifier
                        for identifier, count in _identifier_counts(ledger).items()
                        if identifier.startswith("reserver-") and count >= 5
                    ]
                )
                == 2,
                timeout_s=RACE_TIMEOUT_S,
                what="both reservers to commit at least five reservations each",
            )

            requests_before_halt = ledger.status().requests
            halt_proc = subprocess.run(
                [sys.executable, "-c", _HALT_SCRIPT, LIB_ROOT, str(ledger.path), "operator"],
                capture_output=True,
                text=True,
                timeout=RACE_TIMEOUT_S,
            )
            assert halt_proc.returncode == 0, halt_proc.stderr
            assert halt_proc.stdout.strip() == "HALT_COMMITTED", halt_proc.stdout
            # Read AFTER the halting process exited, so the halt is committed.
            # Nothing can be admitted from here on, which is why this value is
            # the final one.
            requests_at_halt = ledger.status().requests

            outputs = []
            for proc in reservers:
                out, err = proc.communicate(timeout=RACE_TIMEOUT_S)
                assert proc.returncode == 0, err
                outputs.append(out.strip())
        finally:
            _kill_all(reservers)

        # --- the acceptance clause itself -----------------------------------
        for out in outputs:
            assert out.startswith("HALTED "), f"expected HALTED, got {out!r}"
            grants = int(out.split()[1])
            assert grants > 0, f"a reserver never got a grant: {out!r}"
            assert out.split()[2] == "operator", out
        assert ledger.status().requests == requests_at_halt, (
            f"{ledger.status().requests - requests_at_halt} reservations were "
            "admitted AFTER the halt committed"
        )
        status = ledger.status()
        assert status.halted is True
        assert status.halt_reason == "operator"
        assert status.reservations_open == 0  # every grant was settled

        # --- evidence that the halt landed on live traffic ------------------
        assert requests_at_halt > requests_before_halt, (
            "no reservation was committed while the halting process was alive; "
            "the halt did not race live reserve traffic"
        )
        counts = _identifier_counts(ledger)
        assert counts.get("reserver-0", 0) > 0 and counts.get("reserver-1", 0) > 0, counts


# ---------------------------------------------------------------------------
# Acceptance clause 3: status reports reserved and spent
# ---------------------------------------------------------------------------

#: Writer processes racing the reader in the status test.
STATUS_WRITERS = 3
#: The floor on commits raced before the live phase may pass. Below this the
#: reader never overlapped enough writes for its samples to mean anything, and
#: the test fails rather than going green on an unexercised property.
COMMITS_TO_RACE = 150


class TestStatusReportsReservedAndSpent:
    """plan.md clause 3: status reports reserved and spent. Design section 7.

    SL-2's `TestStatusUnderLiveWriters` already asserts that `status()`'s
    separate queries see ONE snapshot (the torn-read property). This asserts the
    different thing the acceptance clause asks for: that the VALUES are the
    reserved and the spent money of OTHER processes, in the right term each.
    """

    def test_reserved_and_spent_are_reported_from_other_processes_rows(self, tmp_path):
        """Three writer processes reserve and settle in a loop while this
        process samples `status()`; then each writer leaves exactly ONE
        reservation OPEN and blocks, and the quiesced file is asserted exactly.

        Live phase. Every writer only ever reserves AMOUNT and settles COST, so
        each row contributes AMOUNT to RESERVED while open and COST to SETTLED
        once resolved, and nothing to any other term. Both reported amounts are
        therefore whole multiples of a literal this test owns, and

            settled / COST + reserved / AMOUNT == requests

        at every real snapshot. The literals are what carry the assertion: the
        expected values are not recomputed from the production queries, so the
        check cannot go green by production and recomputation moving together
        (vacuous-checks.md shape 1).

        Held phase. Once all three writers are blocked holding one open
        reservation each, the file is quiescent and the numbers are exact:
        `reserved_usd` is 3 x AMOUNT and `calls_settled` x COST is
        `settled_usd`. This is the clause's sharpest form -- an exact dollar
        figure for money held and money spent, every cent of it written by a
        different process.

        Evidence that contention really occurred: at least COMMITS_TO_RACE
        commits landed between the first and last sample of the live phase, and
        at least one sample reported `reserved_usd > 0`, which can only mean the
        reader saw a row another process had opened and not yet settled.

        Reverts shown red. All three fired in the LIVE phase, before the held
        phase was reached, so the exact held-phase figures are additional cover
        rather than the demonstrated carrier -- stated plainly because a
        docstring claiming the held phase caught them would be a claim this run
        does not support:
        1. NEUTERED the RESERVED term in `_partition_nano` (`WHERE state =
           'open'` -> `WHERE 0`) -> `AssertionError: settled 7 + reserved 0 !=
           requests 8`. Restored.
        2. NEUTERED the SETTLED term the same way -> `AssertionError: settled 0
           + reserved 0 != requests 3`. Restored.
        3. SWAPPED the two terms in the `SpendStatus` constructor
           (`settled_usd=...terms["reserved"]` and vice versa) -> `AssertionError:
           assert (812500000 % 125000000) == 0`, the reported `reserved_usd` of
           0.8125 not being a whole multiple of AMOUNT. This is why AMOUNT and
           COST are deliberately unequal: with equal amounts a swap would
           satisfy both multiples and the identity, and only the held phase's
           exact figures would have caught it. Restored.
        """
        ledger = make(
            tmp_path,
            cap_usd=1000.0,  # far more headroom than the sampling window needs
            name="status.sqlite",
            synchronous="OFF",
        )
        ready = [tmp_path / f"writer-{i}.ready" for i in range(STATUS_WRITERS)]
        held = [tmp_path / f"writer-{i}.held" for i in range(STATUS_WRITERS)]
        hold = tmp_path / "writers.hold"
        stop = tmp_path / "writers.stop"

        writers = []
        samples = 0
        saw_open_row = False
        saw_spent = False
        first_requests = None
        last_requests = 0
        try:
            writers = [
                subprocess.Popen(
                    [
                        sys.executable, "-c", _STATUS_WRITER_SCRIPT,
                        LIB_ROOT, str(ledger.path), str(ready[i]), str(hold),
                        str(held[i]), str(stop), str(AMOUNT_USD), str(COST_USD),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for i in range(STATUS_WRITERS)
            ]
            _wait_for(
                lambda: all(p.exists() for p in ready)
                or any(p.poll() is not None for p in writers),
                timeout_s=READY_TIMEOUT_S,
                what="every writer process to open the ledger",
            )
            for proc in writers:
                assert proc.poll() is None, "a writer exited before writing anything"
            _wait_for(
                lambda: ledger.status().requests > 0,
                timeout_s=RACE_TIMEOUT_S,
                what="the writers' first committed reservation",
            )

            # --- live phase -------------------------------------------------
            deadline = time.monotonic() + RACE_TIMEOUT_S
            while time.monotonic() < deadline:
                status = ledger.status()
                settled_nano = sl.usd_to_nano(status.settled_usd)
                reserved_nano = sl.usd_to_nano(status.reserved_usd)
                assert settled_nano % COST_NANO == 0, (settled_nano, status)
                assert reserved_nano % AMOUNT_NANO == 0, (reserved_nano, status)
                assert (
                    settled_nano // COST_NANO + reserved_nano // AMOUNT_NANO
                    == status.requests
                ), (
                    f"settled {settled_nano // COST_NANO} + reserved "
                    f"{reserved_nano // AMOUNT_NANO} != requests {status.requests}"
                )
                assert (status.unknown_usd, status.leaked_usd, status.reclaimed_usd) == (
                    0.0, 0.0, 0.0,
                )
                assert status.requests == status.calls_settled + status.reservations_open
                samples += 1
                if first_requests is None:
                    first_requests = status.requests
                last_requests = status.requests
                if reserved_nano > 0:
                    saw_open_row = True
                if settled_nano > 0:
                    saw_spent = True
                if (
                    last_requests - first_requests >= COMMITS_TO_RACE
                    and saw_open_row
                    and saw_spent
                ):
                    break
                for proc in writers:
                    assert proc.poll() is None, "a writer exited mid-sampling"

            assert last_requests - first_requests >= COMMITS_TO_RACE, (
                f"only {last_requests - first_requests} commits were raced over "
                f"{samples} samples; the reads did not overlap live writes"
            )
            assert saw_open_row, (
                "no sample reported reserved_usd > 0, so the RESERVED term was "
                "never exercised against another process's open row"
            )
            assert saw_spent, "no sample reported settled_usd > 0"

            # --- held phase: exact figures over a quiesced file --------------
            hold.write_text("hold\n", encoding="utf-8")
            _wait_for(
                lambda: all(p.exists() for p in held)
                or any(p.poll() is not None for p in writers),
                timeout_s=RACE_TIMEOUT_S,
                what="every writer to hold one open reservation",
            )
            for proc in writers:
                assert proc.poll() is None, "a writer exited before holding"

            status = ledger.status()
            assert status.reservations_open == STATUS_WRITERS, status
            assert sl.usd_to_nano(status.reserved_usd) == STATUS_WRITERS * AMOUNT_NANO, (
                f"reserved_usd {status.reserved_usd} != "
                f"{sl.nano_to_usd(STATUS_WRITERS * AMOUNT_NANO)}"
            )
            assert status.calls_settled == status.requests - STATUS_WRITERS
            assert sl.usd_to_nano(status.settled_usd) == status.calls_settled * COST_NANO, (
                f"settled_usd {status.settled_usd} != "
                f"{sl.nano_to_usd(status.calls_settled * COST_NANO)}"
            )
            assert sl.usd_to_nano(status.outstanding_usd) == (
                status.calls_settled * COST_NANO + STATUS_WRITERS * AMOUNT_NANO
            )
            assert sl.usd_to_nano(status.remaining_usd) == (
                ledger.cap_nano - sl.usd_to_nano(status.outstanding_usd)
            )
            assert status.halted is False
        finally:
            hold.write_text("hold\n", encoding="utf-8")
            stop.write_text("stop\n", encoding="utf-8")
            outputs = []
            for proc in writers:
                try:
                    outputs.append(proc.communicate(timeout=RACE_TIMEOUT_S))
                except subprocess.TimeoutExpired:
                    proc.kill()
                    outputs.append(proc.communicate(timeout=30))
            _kill_all(writers)

        for proc, (out, err) in zip(writers, outputs):
            assert proc.returncode == 0, err
            assert out.startswith("WROTE "), out
            assert int(out.split()[1]) > 0, out


# ---------------------------------------------------------------------------
# Busy-timeout exhaustion: fail closed
# ---------------------------------------------------------------------------


class TestBusyTimeoutExhaustionFailsClosed:
    """Design section 4's error behaviour, and design test 4 of section 11.

    The second cross-check found this uncovered across processes. SL-1's
    `TestFailClosedOnLockContention` drives it from an in-process blocker
    connection; this drives it from another OS process, which is the shape a
    consumer actually meets.
    """

    def test_reserve_refuses_and_is_not_a_budget_verdict(self, tmp_path):
        """A subprocess holds the write lock past `busy_timeout_ms`; `reserve`
        raises `sqlite3.OperationalError`, admits nothing, and the error is NOT
        in the `BudgetExceededError` hierarchy.

        This is DETERMINISTIC, not probabilistic. The lock holder publishes its
        marker file only after `BEGIN IMMEDIATE` plus a real INSERT has
        succeeded, so the marker's existence means the write lock is HELD, not
        requested; the parent waits for the marker and only then reserves. There
        is no window in which the lock might not be taken, so no flake to bound.

        Fail-closed evidence, three ways:
        - the raised error is `sqlite3.OperationalError` and
          `isinstance(exc, BudgetExceededError)` is False, so a consumer's
          `except BudgetExceededError` does NOT swallow an unreachable ledger as
          a cap verdict. (`cli.budget.spend_stop` is SL-6 and does not exist
          yet; this is the precondition that makes its pass-through possible,
          and the translation itself is SL-6's test.)
        - the wait really exhausted the timeout rather than failing fast: the
          elapsed time is at least most of `busy_timeout_ms`. That is what
          distinguishes `BEGIN IMMEDIATE` waiting against the busy timeout from
          a deferred transaction failing instantly.
        - nothing was admitted: `requests` is still 0 after the lock clears,
          and the same reserve then succeeds, so the refusal was the lock and
          not a broken ledger.

        `settle` is asserted on the same held lock, because section 4 gives it
        the same fail-closed propagation.

        Revert shown red: wrapped the `BEGIN IMMEDIATE` in `_writer` with
        `except sqlite3.OperationalError as exc: raise SpendCapExceeded(
        identifier="", measured=0.0, budget=self.cap_usd) from exc` -> FAILED
        with `content_pipeline.llm.spend_ledger.SpendCapExceeded: budget
        exceeded for '': measured 0.0 > budget 10.0` escaping the
        `pytest.raises(sqlite3.OperationalError)`, chained from
        `sqlite3.OperationalError: database is locked`. That `SpendCapExceeded`
        is a `BudgetExceededError`, so a consumer's `except BudgetExceededError`
        would have reported an unreachable ledger as a cap verdict -- exactly
        the translation the design forbids. Restored.
        """
        busy_timeout_ms = 300
        ledger = make(
            tmp_path,
            cap_usd=10.0,
            name="locked.sqlite",
            busy_timeout_ms=busy_timeout_ms,
        )
        reservation = ledger.reserve(AMOUNT_USD)
        ledger.settle(reservation, COST_USD)
        settled_requests = ledger.status().requests

        locked = tmp_path / "holder.locked"
        release = tmp_path / "holder.release"
        holder = subprocess.Popen(
            [
                sys.executable, "-c", _LOCK_HOLDER_SCRIPT,
                LIB_ROOT, str(ledger.path), str(locked), str(release),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _wait_for(
                lambda: locked.exists() or holder.poll() is not None,
                timeout_s=READY_TIMEOUT_S,
                what="the holder process to take the write lock",
            )
            assert holder.poll() is None, "the lock holder exited before locking"

            started = time.monotonic()
            with pytest.raises(sqlite3.OperationalError) as caught:
                ledger.reserve(AMOUNT_USD)
            elapsed = time.monotonic() - started

            assert not isinstance(caught.value, BudgetExceededError), (
                "an unreachable ledger was reported as a budget verdict"
            )
            assert not isinstance(caught.value, sl.SpendCapExceeded)
            assert not isinstance(caught.value, sl.SpendLedgerHalted)
            assert "lock" in str(caught.value).lower(), str(caught.value)
            assert elapsed >= (busy_timeout_ms / 1000.0) * 0.8, (
                f"reserve failed after {elapsed:.3f}s, well inside the "
                f"{busy_timeout_ms}ms busy timeout: it did not WAIT for the "
                "write lock, so this was a fail-fast and not an exhaustion"
            )

            # `settle` propagates the same way (design section 4).
            with pytest.raises(sqlite3.OperationalError):
                ledger.settle(reservation, COST_USD)

            # Admitted nothing, while the lock is still held.
            assert ledger.status().requests == settled_requests
        finally:
            release.write_text("release\n", encoding="utf-8")
            try:
                holder_out, holder_err = holder.communicate(timeout=RACE_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder_out, holder_err = holder.communicate(timeout=30)
            _kill_all([holder])

        assert holder.returncode == 0, holder_err
        assert holder_out.strip() == "RELEASED", holder_out
        # The refusal was the lock, not a broken ledger: the identical call
        # succeeds once the lock clears, and the row count moves by exactly one.
        assert ledger.status().requests == settled_requests
        ledger.reserve(AMOUNT_USD)
        assert ledger.status().requests == settled_requests + 1


# ---------------------------------------------------------------------------
# The `BEGIN IMMEDIATE` source guard (design section 11.1)
# ---------------------------------------------------------------------------

#: Statement keywords that make an `execute` a WRITE.
_WRITE_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "REPLACE", "DROP", "ALTER")

#: Helpers that write inside the CALLER's transaction: they take a `conn` and
#: open no transaction of their own. Every call site of each must sit inside a
#: `with self._writer()` block, which is asserted below -- that is what makes
#: "takes no transaction of its own" safe rather than merely true.
_CONN_WRITE_HELPERS = frozenset({"_record_halt", "_sweep_expired"})

#: Functions that legitimately execute a write statement. Equality is asserted,
#: so a new write path cannot appear without a reader of this guard noticing.
_EXPECTED_WRITE_FUNCTIONS = frozenset(
    {
        "_record_halt",
        "_sweep_expired",
        "reserve",
        "settle",
        "release",
        "renew",
        "resume",
        "create_ledger",
    }
)


def _module_tree():
    return ast.parse(MODULE_PATH.read_text(encoding="utf-8"))


def _owners(tree):
    """Map every node to the name of the innermost function containing it."""
    owners = {}

    def walk(node, owner):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child.name)
            else:
                owners[id(child)] = owner
                walk(child, owner)

    walk(tree, "<module>")
    return owners


def _string_constants(tree, owners, prefixes):
    """Every string literal starting with one of `prefixes`, with its owner.

    Reads ALL string constants, not only `execute` arguments, so a literal
    smuggled through a variable (`sql = "BEGIN"; conn.execute(sql)`) is still
    seen. No docstring in this module begins with any of these keywords, so none
    needs excluding.
    """
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        text = node.value.strip()
        if text.upper().startswith(prefixes):
            found.append((owners.get(id(node), "<module>"), text))
    return found


def _has_writer_block(function_node):
    """Whether the function body contains a `with self._writer()` block."""
    for node in ast.walk(function_node):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            call = item.context_expr
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "_writer"
            ):
                return True
    return False


def _functions(tree):
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


class TestBeginImmediateSourceGuard:
    """Design section 11.1: `BEGIN IMMEDIATE` has ONE carrier and it is a
    SOURCE guard, because no runtime test can carry the property.

    Quoting the design's reasoning, which this class exists to implement rather
    than to re-argue: with no whole-transaction retry anywhere in this module, a
    deferred admission that lost its snapshot surfaces as
    `sqlite3.OperationalError` and admits nothing, so reverting `IMMEDIATE` to a
    plain `BEGIN` yields a fail-closed error and not an over-admission. There is
    no wrong NUMBER for a runtime test to observe, and a test that built a
    deferred transaction by hand would merely assert SQLite's own behaviour --
    true with the design and without it (vacuous-checks.md shape 2). Stating
    that plainly is the correct outcome here, not a shortfall.

    The three `BEGIN` literals in the module, which these tests pin by name:
    `_writer` -> "BEGIN IMMEDIATE", `create_ledger` -> "BEGIN IMMEDIATE",
    `_reader` -> "BEGIN". `_sweep_expired` adds none: it runs in the caller's
    transaction.
    """

    def test_exactly_one_plain_begin_and_it_is_the_reader(self):
        """The whole property in one assertion: every `BEGIN` on a write path is
        `BEGIN IMMEDIATE`, and the only plain `BEGIN` is the read-only snapshot
        helper that `status()` and `check_halted()` use.

        Revert shown red (the one the design names): changed `_writer`'s literal
        from `"BEGIN IMMEDIATE"` to `"BEGIN"` -> FAILED, naming the call site:
        `AssertionError: plain BEGIN outside the reader: [('_writer', 'BEGIN')]`
        and `assert [('_reader', 'BEGIN'), ('_writer', 'BEGIN')] == [('_reader',
        'BEGIN')]`. Restored.

        Second revert shown red: changed `create_ledger`'s literal to `"BEGIN"`
        -> FAILED naming `plain BEGIN outside the reader: [('create_ledger',
        'BEGIN')]`, and `test_every_write_statement_runs_under_an_immediate_
        transaction` went red too with `write paths not under BEGIN IMMEDIATE:
        ['create_ledger']`. The schema create is a write path as much as
        `_writer` is, and a guard that only knew about `_writer` would have
        missed it.

        Third revert shown red: DELETED `_reader`'s `conn.execute("BEGIN")`
        -> FAILED with `assert [] == [('_reader', 'BEGIN')]`, so the guard also
        notices the reader losing its snapshot rather than only policing the
        writers.
        """
        tree = _module_tree()
        owners = _owners(tree)
        sites = _string_constants(tree, owners, ("BEGIN",))

        plain = sorted(site for site in sites if site[1].upper() == "BEGIN")
        immediate = sorted(site for site in sites if site[1].upper() == "BEGIN IMMEDIATE")
        other = sorted(set(sites) - set(plain) - set(immediate))

        assert other == [], f"a BEGIN variant that is neither plain nor IMMEDIATE: {other}"
        assert plain == [("_reader", "BEGIN")], (
            f"plain BEGIN outside the reader: "
            f"{[site for site in plain if site[0] != '_reader']}; all plain "
            f"sites = {plain}"
        )
        assert sorted({owner for owner, _ in immediate}) == ["_writer", "create_ledger"], (
            f"BEGIN IMMEDIATE sites changed: {immediate}"
        )

    def test_the_one_plain_begin_opens_a_read_only_transaction(self):
        """The assertion above is about a NAME; this is about the BEHAVIOUR, so
        the two together say "the only plain BEGIN is a reader" rather than "the
        only plain BEGIN is in a function we call `_reader`".

        Without this, moving a write under the plain `BEGIN` would leave the
        guard green -- the plain site would still be `_reader`.

        Revert shown red: added `conn.execute("UPDATE halt SET halted = halted
        WHERE id = 1")` to `_reader` -> FAILED with `AssertionError: _reader
        executes write statements: ['UPDATE halt SET halted = halted WHERE id =
        1']`, and the reachability test below went red as well, reporting
        `_reader` as a new member of the write-path set. Restored.
        """
        tree = _module_tree()
        owners = _owners(tree)
        writes = [
            text
            for owner, text in _string_constants(tree, owners, _WRITE_KEYWORDS)
            if owner == "_reader"
        ]
        assert writes == [], f"_reader executes write statements: {writes}"

    def test_every_write_statement_runs_under_an_immediate_transaction(self):
        """The reachability half: a `BEGIN IMMEDIATE` literal in `_writer` only
        protects the writes that actually go THROUGH `_writer`. Every function
        that executes a write statement must therefore be one of three things --
        a `with self._writer()` user, `create_ledger` (which opens its own
        `BEGIN IMMEDIATE`), or a `conn`-taking helper whose every call site is
        itself inside a `with self._writer()` block.

        That last clause is what pins the design's claim that `_sweep_expired`
        "adds NO new BEGIN because it runs in the caller's transaction": the
        guard checks the callers, so the claim is enforced rather than trusted.

        Reverts shown red:
        1. changed `release` to use `self._reader()` instead of `self._writer()`
           -> FAILED with `write paths not under BEGIN IMMEDIATE: ['release']`.
           Restored.
        2. moved `reclaim_orphans`'s `self._sweep_expired(...)` call out of its
           `with self._writer()` block (onto a bare `self._connect()`
           connection) -> FAILED with `_sweep_expired called outside a
           self._writer() block, from: ['reclaim_orphans']`. Restored.
        3. added a `conn.execute("DELETE FROM leaks")` to `check_halted` -> FAILED
           on the SET-EQUALITY assertion first: `the set of functions executing
           write statements changed: ['_record_halt', '_sweep_expired',
           'check_halted', 'create_ledger', 'release', 'renew', 'reserve',
           'resume', 'settle']`. A new write path therefore cannot appear on a
           reader -- or anywhere else -- without a reader of this guard having
           to re-read it. Restored.
        """
        tree = _module_tree()
        owners = _owners(tree)
        functions = _functions(tree)

        write_owners = {
            owner for owner, _ in _string_constants(tree, owners, _WRITE_KEYWORDS)
        }
        # The `CREATE TABLE`/`CREATE INDEX` statements of `_SCHEMA` are a
        # module-level tuple executed by `create_ledger` through a loop
        # variable, so they are not attributed to a function; `create_ledger`
        # is covered by its own INSERT literals below.
        assert write_owners - {"<module>"} == _EXPECTED_WRITE_FUNCTIONS, (
            f"the set of functions executing write statements changed: "
            f"{sorted(write_owners - {'<module>'})}"
        )

        unprotected = []
        for name in sorted(write_owners - {"<module>"}):
            if name in _CONN_WRITE_HELPERS:
                continue
            node = functions[name]
            if _has_writer_block(node):
                continue
            own_begin = [
                text
                for owner, text in _string_constants(tree, _owners(tree), ("BEGIN",))
                if owner == name and text.upper() == "BEGIN IMMEDIATE"
            ]
            if own_begin:
                continue
            unprotected.append(name)
        assert unprotected == [], (
            f"write paths not under BEGIN IMMEDIATE: {unprotected}"
        )

        # Every call site of a conn-taking write helper is inside a
        # `with self._writer()` block (or inside another such helper).
        bad_callers = set()
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _CONN_WRITE_HELPERS
            ):
                continue
            caller = owners.get(id(node), "<module>")
            if caller in _CONN_WRITE_HELPERS:
                continue
            if caller in functions and _has_writer_block(functions[caller]):
                continue
            bad_callers.add((node.func.attr, caller))
        assert bad_callers == set(), (
            f"a conn-taking write helper is called outside a self._writer() "
            f"block: {sorted(bad_callers)}"
        )

    def test_sweep_expired_opens_no_transaction_of_its_own(self):
        """The premise this unit inherited from SL-3, asserted rather than
        assumed: `_sweep_expired` runs in the caller's transaction, so the guard
        above sees exactly the existing write paths plus `status()`'s reader.

        Revert shown red: added `conn.execute("BEGIN IMMEDIATE")` to
        `_sweep_expired` -> FAILED with `_sweep_expired opens its own
        transaction: ['BEGIN IMMEDIATE']`, and
        `test_exactly_one_plain_begin_and_it_is_the_reader` ALSO went red on the
        changed IMMEDIATE site set -- which is the point: a new transaction
        anywhere is visible to this guard. Restored.
        """
        tree = _module_tree()
        owners = _owners(tree)
        begins = [
            text
            for owner, text in _string_constants(tree, owners, ("BEGIN",))
            if owner == "_sweep_expired"
        ]
        assert begins == [], f"_sweep_expired opens its own transaction: {begins}"
