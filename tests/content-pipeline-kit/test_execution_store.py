"""Tests for content_pipeline.execution.store.

Pins the durable run store: reopen/migration, legal and illegal unit-state
transitions, thread AND separate-process claim contention, stale-fencing
rejection, lease expiry (via a deterministic ``at=`` clock, never a bare
sleep), nullable usage, WAL/busy_timeout actually in effect on every
connection, and the network-path warning. Also the plan's A-min.1 exit
criterion: one process holds a claim while a second process gets a bounded
status digest, and reopening the database preserves run truth.
"""

from __future__ import annotations

import os
import subprocess
import sys
import sqlite3
import threading
import time

import pytest

from content_pipeline.execution.model import (
    AlreadyClaimedError,
    AttemptKind,
    DuplicateUnitError,
    NotAcceptedError,
    NotClaimedError,
    RunHaltedError,
    StaleFenceError,
    TerminalStateError,
    UnitState,
    UnknownRunError,
    UnknownUnitError,
    UsageRecord,
)
from content_pipeline.execution.status import compute_status
from content_pipeline.execution.store import (
    DEFAULT_BUSY_TIMEOUT_MS,
    DEFAULT_LEASE_SECONDS,
    LEASE_HEADROOM_FACTOR,
    ExecutionStore,
    lease_for,
    looks_like_network_path,
)

LIB_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir,
                 "plugins", "content-pipeline-kit", "lib")
)


def _new_store(tmp_path, **kwargs) -> ExecutionStore:
    return ExecutionStore(tmp_path / "run.db", **kwargs)


def _seeded_store(tmp_path, *, unit_ids=("u0", "u1", "u2")):
    store = _new_store(tmp_path)
    store.create_run(
        "run-1", driver="inline", backend="mock", model="test-model", adapter_version="1"
    )
    store.register_units("run-1", list(unit_ids))
    return store


# -- reopen / migration --------------------------------------------------------

def test_reopen_preserves_run_and_unit_truth(tmp_path):
    db_path = tmp_path / "run.db"
    store = ExecutionStore(db_path)
    store.create_run("r1", driver="inline", backend="mock", model="m", adapter_version="1")
    store.register_units("r1", ["a", "b"])
    store.claim_unit("r1", "a", "w1")

    reopened = ExecutionStore(db_path)
    run = reopened.get_run("r1")
    assert run is not None
    assert run.driver == "inline"
    unit_a = reopened.get_unit("r1", "a")
    assert unit_a.state is UnitState.CLAIMED
    assert unit_a.claimed_by == "w1"
    unit_b = reopened.get_unit("r1", "b")
    assert unit_b.state is UnitState.PENDING


def test_migration_is_idempotent_across_opens(tmp_path):
    db_path = tmp_path / "run.db"
    ExecutionStore(db_path)
    ExecutionStore(db_path)  # second open must not fail or re-run migrations badly
    with sqlite3.connect(str(db_path)) as conn:
        (version,) = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    assert version is not None and version > 0
    with sqlite3.connect(str(db_path)) as conn:
        (row_count,) = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()
    assert row_count == 1  # migrations table holds one current-version row, not one per open


# -- legal transitions ----------------------------------------------------------

def test_claim_accept_is_a_legal_terminal_path(tmp_path):
    store = _seeded_store(tmp_path)
    claim = store.claim_unit("run-1", "u0", "worker-a")
    assert claim.fencing_token == 1

    store.accept_unit("run-1", "u0", claim.fencing_token, usage=UsageRecord(input_tokens=10))

    unit = store.get_unit("run-1", "u0")
    assert unit.state is UnitState.ACCEPTED
    assert unit.accepted_at is not None

    attempts = store.list_attempts("run-1", "u0")
    assert [a.kind for a in attempts] == [AttemptKind.CLAIM, AttemptKind.ACCEPT]
    assert attempts[-1].usage.input_tokens == 10
    assert attempts[-1].usage.output_tokens is None  # nullable, never 0


def test_claim_fail_nonterminal_returns_to_pending_for_retry(tmp_path):
    store = _seeded_store(tmp_path)
    claim = store.claim_unit("run-1", "u0", "worker-a")
    store.fail_unit("run-1", "u0", claim.fencing_token, error="transient", terminal=False)

    unit = store.get_unit("run-1", "u0")
    assert unit.state is UnitState.PENDING
    assert unit.claimed_by is None

    # A fresh claim is legal again (retry) and gets a NEW fencing token.
    claim2 = store.claim_unit("run-1", "u0", "worker-b")
    assert claim2.fencing_token == 2


def test_claim_fail_terminal_is_a_legal_terminal_path(tmp_path):
    store = _seeded_store(tmp_path)
    claim = store.claim_unit("run-1", "u0", "worker-a")
    store.fail_unit("run-1", "u0", claim.fencing_token, error="permanent", terminal=True)

    unit = store.get_unit("run-1", "u0")
    assert unit.state is UnitState.FAILED
    assert unit.failed_at is not None


def test_renew_extends_lease_under_the_same_fencing_token(tmp_path):
    store = _seeded_store(tmp_path)
    claim = store.claim_unit("run-1", "u0", "worker-a", lease_seconds=10, at=100.0)
    new_expiry = store.renew_lease("run-1", "u0", claim.fencing_token, lease_seconds=10, at=105.0)
    assert new_expiry == 115.0


# -- illegal transitions ---------------------------------------------------------

def test_claim_unknown_unit_raises(tmp_path):
    store = _seeded_store(tmp_path)
    with pytest.raises(UnknownUnitError):
        store.claim_unit("run-1", "does-not-exist", "w1")


def test_claim_unknown_run_raises(tmp_path):
    store = _new_store(tmp_path)
    with pytest.raises(UnknownRunError):
        store.claim_unit("no-such-run", "u0", "w1")


def test_double_register_same_unit_id_raises_duplicate(tmp_path):
    store = _seeded_store(tmp_path)
    with pytest.raises(DuplicateUnitError):
        store.register_units("run-1", ["u0"])  # already registered


def test_claim_already_claimed_with_live_lease_raises(tmp_path):
    store = _seeded_store(tmp_path)
    store.claim_unit("run-1", "u0", "worker-a", lease_seconds=300)
    with pytest.raises(AlreadyClaimedError):
        store.claim_unit("run-1", "u0", "worker-b")


def test_claim_terminal_unit_raises(tmp_path):
    store = _seeded_store(tmp_path)
    claim = store.claim_unit("run-1", "u0", "worker-a")
    store.accept_unit("run-1", "u0", claim.fencing_token)
    with pytest.raises(TerminalStateError):
        store.claim_unit("run-1", "u0", "worker-b")
    with pytest.raises(TerminalStateError):
        store.accept_unit("run-1", "u0", claim.fencing_token)
    with pytest.raises(TerminalStateError):
        store.fail_unit("run-1", "u0", claim.fencing_token, terminal=True)


def test_accept_without_a_claim_raises_not_claimed(tmp_path):
    """A never-claimed unit's fencing token is 0 -- presenting the CURRENT
    (matching) token 0 against a non-CLAIMED unit is NotClaimedError, not a
    fencing problem. (Presenting a token that does NOT match -- e.g. 1 -- is
    covered separately below: that is always StaleFenceError.)"""
    store = _seeded_store(tmp_path)
    with pytest.raises(NotClaimedError):
        store.accept_unit("run-1", "u0", 0)


def test_renew_without_a_claim_raises_not_claimed(tmp_path):
    store = _seeded_store(tmp_path)
    with pytest.raises(NotClaimedError):
        store.renew_lease("run-1", "u0", 0)


def test_renew_against_an_accepted_unit_raises_terminal_state(tmp_path):
    """model.py promises no further claim, renew, accept, or fail is legal
    against a terminal unit (execution.store raises TerminalStateError).
    accept_unit and fail_unit both check TERMINAL_STATES before the
    not-CLAIMED check; a renew_lease that skips straight to the not-CLAIMED
    check makes an accepted unit's own (still-current) fencing token raise
    NotClaimedError instead of TerminalStateError."""
    store = _seeded_store(tmp_path)
    claim = store.claim_unit("run-1", "u0", "worker-a")
    store.accept_unit("run-1", "u0", claim.fencing_token)
    with pytest.raises(TerminalStateError):
        store.renew_lease("run-1", "u0", claim.fencing_token)


def test_accept_with_a_nonmatching_token_on_an_unclaimed_unit_is_stale_fence(tmp_path):
    """Fence is checked BEFORE the state check (defect 7's fix): a presented
    token that does not match the unit's current token is always
    StaleFenceError, even against a PENDING unit that was never claimed with
    that token at all."""
    store = _seeded_store(tmp_path)
    with pytest.raises(StaleFenceError):
        store.accept_unit("run-1", "u0", 1)


def test_halted_run_blocks_new_claims(tmp_path):
    store = _seeded_store(tmp_path)
    store.set_halt("run-1", "rate_limit", "hit your limit")
    with pytest.raises(RunHaltedError):
        store.claim_unit("run-1", "u0", "worker-a")


def test_halt_never_blocks_a_valid_fence_acceptance(tmp_path):
    """D4: halt blocks new claims, never a submission with a valid fence."""
    store = _seeded_store(tmp_path)
    claim = store.claim_unit("run-1", "u0", "worker-a")
    store.set_halt("run-1", "rate_limit", "hit your limit")
    store.accept_unit("run-1", "u0", claim.fencing_token)  # must not raise
    assert store.get_unit("run-1", "u0").state is UnitState.ACCEPTED


def test_halt_combined_with_a_stale_fence_still_raises_stale_fence_error(tmp_path):
    """D4 is narrower than 'halt never blocks a submission' -- it never
    blocks a VALID-fence submission. A stale-fence submission is rejected
    regardless of halt state; halt must not relax the fence check."""
    store = _seeded_store(tmp_path)
    claim1 = store.claim_unit("run-1", "u0", "worker-a", lease_seconds=10, at=1000.0)
    claim2 = store.claim_unit("run-1", "u0", "worker-b", at=1011.0)  # expiry + reclaim
    store.set_halt("run-1", "rate_limit", "hit your limit")

    with pytest.raises(StaleFenceError):
        store.accept_unit("run-1", "u0", claim1.fencing_token, at=1015.0)

    # The winning claim can still accept under halt (D4's actual guarantee).
    store.accept_unit("run-1", "u0", claim2.fencing_token, at=1016.0)
    assert store.get_unit("run-1", "u0").state is UnitState.ACCEPTED


# -- stale fencing ----------------------------------------------------------------

def test_stale_fencing_token_is_rejected_on_renew_and_accept(tmp_path):
    store = _seeded_store(tmp_path)
    claim1 = store.claim_unit("run-1", "u0", "worker-a")
    store.fail_unit("run-1", "u0", claim1.fencing_token, terminal=False)  # -> PENDING
    claim2 = store.claim_unit("run-1", "u0", "worker-b")  # fresh, higher token
    assert claim2.fencing_token > claim1.fencing_token

    with pytest.raises(StaleFenceError):
        store.renew_lease("run-1", "u0", claim1.fencing_token)
    with pytest.raises(StaleFenceError):
        store.accept_unit("run-1", "u0", claim1.fencing_token)
    with pytest.raises(StaleFenceError):
        store.fail_unit("run-1", "u0", claim1.fencing_token, terminal=True)

    # The current, valid token still works.
    store.accept_unit("run-1", "u0", claim2.fencing_token)
    assert store.get_unit("run-1", "u0").state is UnitState.ACCEPTED


def test_stale_accept_after_the_winner_already_accepted_is_still_stale_fence_error(tmp_path):
    """Defect 7: BEFORE the fix, a stale submission that arrived after the
    winner's own terminal accept raised TerminalStateError (because the state
    check ran before the fence check) -- masking that the token was stale in
    the first place. A worker that only handles StaleFenceError as 'fenced,
    do not retry' would mis-handle that as an ordinary terminal-state error.
    The fence check must run first, so this is ALWAYS StaleFenceError."""
    store = _seeded_store(tmp_path)
    claim1 = store.claim_unit("run-1", "u0", "worker-a", lease_seconds=10, at=1000.0)
    claim2 = store.claim_unit("run-1", "u0", "worker-b", at=1011.0)  # expiry + reclaim
    store.accept_unit("run-1", "u0", claim2.fencing_token, at=1012.0)  # winner accepts

    with pytest.raises(StaleFenceError):
        store.accept_unit("run-1", "u0", claim1.fencing_token, at=1013.0)
    with pytest.raises(StaleFenceError):
        store.fail_unit("run-1", "u0", claim1.fencing_token, terminal=True, at=1014.0)


# -- lease expiry (deterministic clock, no sleep) ---------------------------------

def test_lease_expiry_allows_reclaim_with_a_new_higher_fencing_token(tmp_path):
    store = _seeded_store(tmp_path)
    claim1 = store.claim_unit("run-1", "u0", "worker-a", lease_seconds=10, at=1000.0)

    # Still live: a second claimant is rejected.
    with pytest.raises(AlreadyClaimedError):
        store.claim_unit("run-1", "u0", "worker-b", at=1005.0)

    # Past expiry: reclaim succeeds, fencing token strictly increases.
    claim2 = store.claim_unit("run-1", "u0", "worker-b", at=1011.0)
    assert claim2.fencing_token == claim1.fencing_token + 1

    attempts = store.list_attempts("run-1", "u0")
    assert AttemptKind.EXPIRE in [a.kind for a in attempts]

    # The old worker's fence is now stale.
    with pytest.raises(StaleFenceError):
        store.renew_lease("run-1", "u0", claim1.fencing_token, at=1012.0)


def test_expiry_reclaim_then_stale_accept_is_recorded_as_superseded_not_discarded(tmp_path):
    """Invariant 4: a fenced-out late submission is recorded as superseded,
    not silently discarded, and never applied (unit state/verdict are
    untouched by the rejected stale accept)."""
    store = _seeded_store(tmp_path)
    claim1 = store.claim_unit("run-1", "u0", "worker-a", lease_seconds=10, at=1000.0)
    claim2 = store.claim_unit("run-1", "u0", "worker-b", at=1011.0)  # expiry + reclaim

    with pytest.raises(StaleFenceError):
        store.accept_unit("run-1", "u0", claim1.fencing_token, at=1015.0)

    attempts = store.list_attempts("run-1", "u0")
    superseded = [a for a in attempts if a.kind is AttemptKind.SUPERSEDED]
    assert len(superseded) == 1
    assert superseded[0].fencing_token == claim1.fencing_token
    assert superseded[0].at == 1015.0

    # Not applied: the unit is untouched, still CLAIMED under the WINNING
    # (reclaiming) worker's fence.
    unit = store.get_unit("run-1", "u0")
    assert unit.state is UnitState.CLAIMED
    assert unit.claimed_by == "worker-b"
    assert unit.fencing_token == claim2.fencing_token


def test_stale_fail_is_also_recorded_as_superseded(tmp_path):
    store = _seeded_store(tmp_path)
    claim1 = store.claim_unit("run-1", "u0", "worker-a", lease_seconds=10, at=1000.0)
    store.claim_unit("run-1", "u0", "worker-b", at=1011.0)

    with pytest.raises(StaleFenceError):
        store.fail_unit("run-1", "u0", claim1.fencing_token, terminal=True, at=1015.0)

    attempts = store.list_attempts("run-1", "u0")
    superseded = [a for a in attempts if a.kind is AttemptKind.SUPERSEDED]
    assert len(superseded) == 1
    assert store.get_unit("run-1", "u0").state is UnitState.CLAIMED  # untouched


# -- nullable usage -----------------------------------------------------------------

def test_usage_is_none_not_zero_when_not_supplied(tmp_path):
    store = _seeded_store(tmp_path)
    claim = store.claim_unit("run-1", "u0", "worker-a")
    store.accept_unit("run-1", "u0", claim.fencing_token)  # no usage passed
    attempts = store.list_attempts("run-1", "u0")
    accept_attempt = attempts[-1]
    assert accept_attempt.usage is None  # not UsageRecord(0, 0, 0)


# -- WAL / busy_timeout actually in effect --------------------------------------

def test_wal_journal_mode_is_actually_set(tmp_path):
    db_path = tmp_path / "run.db"
    ExecutionStore(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        (mode,) = conn.execute("PRAGMA journal_mode").fetchone()
    assert mode.lower() == "wal"


def test_busy_timeout_is_set_on_every_connection(tmp_path, monkeypatch):
    """Strengthened: the weak version only ever called ``_connect()`` once,
    directly. This exercises the PUBLIC verbs -- each opens and closes its
    own connection (the store's documented per-verb connection model) -- and
    proves the busy_timeout PRAGMA is applied on every single one of them,
    not just the first.

    ``sqlite3.Connection`` is a C-extension type on this interpreter and
    refuses attribute assignment (both on the class and on an instance), so
    this spies at the ``sqlite3.connect`` boundary instead: every connection
    the store opens is wrapped in a thin proxy that records a
    ``PRAGMA busy_timeout`` call before delegating to the real connection.
    """
    import content_pipeline.execution.store as store_mod

    pragma_calls = []
    real_connect = sqlite3.connect

    class _SpyConn:
        def __init__(self, conn):
            object.__setattr__(self, "_conn", conn)

        def execute(self, sql, *args, **kwargs):
            if isinstance(sql, str) and sql.strip().upper().startswith("PRAGMA BUSY_TIMEOUT"):
                pragma_calls.append(sql)
            return self._conn.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def __setattr__(self, name, value):
            setattr(self._conn, name, value)

    def spy_connect(*args, **kwargs):
        return _SpyConn(real_connect(*args, **kwargs))

    monkeypatch.setattr(store_mod.sqlite3, "connect", spy_connect)

    store = ExecutionStore(tmp_path / "run.db", busy_timeout_ms=1234)
    pragma_calls.clear()  # ignore pragmas applied during __init__/_migrate

    store.create_run("r1", driver="inline", backend="mock", model="m", adapter_version="1")
    store.register_units("r1", ["u0", "u1"])
    claim = store.claim_unit("r1", "u0", "w1")
    store.renew_lease("r1", "u0", claim.fencing_token)
    store.get_run("r1")
    store.list_units("r1")
    store.get_unit("r1", "u0")
    store.accept_unit("r1", "u0", claim.fencing_token)
    claim1 = store.claim_unit("r1", "u1", "w2")
    store.fail_unit("r1", "u1", claim1.fencing_token, error="boom")
    store.set_halt("r1", "rate_limit", "hit your limit")
    store.clear_halt("r1")
    store.list_attempts("r1")
    store.snapshot("r1")

    # One PRAGMA busy_timeout per public verb call above (each opens its own
    # connection), all carrying the configured value. Every public verb that
    # opens a connection is exercised here -- including fail_unit, set_halt,
    # and clear_halt, which the weaker version of this test omitted.
    assert len(pragma_calls) >= 13, pragma_calls
    assert all("1234" in c for c in pragma_calls)


def test_default_busy_timeout_constant_is_5000ms():
    assert DEFAULT_BUSY_TIMEOUT_MS == 5000


def test_foreign_keys_pragma_is_enabled(tmp_path):
    store = ExecutionStore(tmp_path / "run.db")
    with store._connect() as conn:
        (enabled,) = conn.execute("PRAGMA foreign_keys").fetchone()
    assert enabled == 1


def test_foreign_key_violation_is_rejected_at_the_database_level(tmp_path):
    """Defect 8: with foreign_keys off, a raw insert of an attempt row for a
    unit that does not exist would silently succeed -- the API-level
    existence checks (_require_run/_require_unit) are the only thing
    stopping it, not SQLite. With the PRAGMA on, SQLite itself refuses."""
    store = _seeded_store(tmp_path)
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO attempts(run_id, unit_id, kind, at) VALUES (?, ?, ?, ?)",
                ("run-1", "does-not-exist", "claim", 1.0),
            )
        conn.rollback()


def test_memory_path_is_refused(tmp_path):
    """Decision: refuse ':memory:' outright rather than add shared-cache
    plumbing. Each public verb opens its own connection, and a private
    in-memory SQLite database is NOT shared across connections -- every
    verb call would see a distinct, empty database."""
    with pytest.raises(ValueError, match=r"(?i)memory"):
        ExecutionStore(":memory:")


# -- network-path warning --------------------------------------------------------

def test_unc_path_is_detected_as_network_without_os_calls():
    assert looks_like_network_path(r"\\fileserver\share\run.db") is True
    assert looks_like_network_path("//fileserver/share/run.db") is True


def test_local_path_is_not_flagged_as_network(tmp_path):
    assert looks_like_network_path(tmp_path / "run.db") is False


def test_store_open_warns_loudly_on_a_network_path(tmp_path, monkeypatch):
    import content_pipeline.execution.store as store_mod

    monkeypatch.setattr(store_mod, "looks_like_network_path", lambda p: True)
    with pytest.warns(RuntimeWarning, match="network filesystem"):
        store_mod.ExecutionStore(tmp_path / "run.db")


def test_store_open_does_not_warn_when_disabled(tmp_path, monkeypatch, recwarn):
    import content_pipeline.execution.store as store_mod

    monkeypatch.setattr(store_mod, "looks_like_network_path", lambda p: True)
    store_mod.ExecutionStore(tmp_path / "run.db", warn_on_network_path=False)
    assert not any(issubclass(w.category, RuntimeWarning) for w in recwarn.list)


# -- thread claim contention ------------------------------------------------------

def test_thread_contention_exactly_one_claimant_wins(tmp_path):
    store = _seeded_store(tmp_path)
    n_threads = 8
    barrier = threading.Barrier(n_threads)
    results = [None] * n_threads

    def attempt(index):
        barrier.wait()
        try:
            store.claim_unit("run-1", "u0", f"worker-{index}")
            results[index] = "ok"
        except AlreadyClaimedError:
            results[index] = "already-claimed"
        except Exception as exc:  # noqa: BLE001 -- surface unexpected failures
            results[index] = f"error: {exc}"

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert results.count("ok") == 1
    assert results.count("already-claimed") == n_threads - 1
    unit = store.get_unit("run-1", "u0")
    assert unit.state is UnitState.CLAIMED


# -- separate-process claim contention --------------------------------------------

_CLAIM_SCRIPT = """
import sys
sys.path.insert(0, sys.argv[1])
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.execution.model import AlreadyClaimedError

db_path, run_id, unit_id, worker_id = sys.argv[2:6]
store = ExecutionStore(db_path)
try:
    result = store.claim_unit(run_id, unit_id, worker_id)
    print("CLAIMED", result.fencing_token)
except AlreadyClaimedError:
    print("ALREADY_CLAIMED")
"""

# A variant that rendezvouses with a sibling process via a barrier directory
# before racing the claim, so the two claim attempts GENUINELY overlap
# instead of one process finishing and exiting before the other even starts
# (the weak version of this test's failure mode).
_RENDEZVOUS_CLAIM_SCRIPT = """
import os
import sys
import time
sys.path.insert(0, sys.argv[1])
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.execution.model import AlreadyClaimedError

db_path, run_id, unit_id, worker_id, barrier_dir = sys.argv[2:7]

# Open the store (and let migration run) BEFORE rendezvousing, so the
# barrier is immediately in front of claim_unit itself -- the two
# processes' claim ATTEMPTS overlap, not just their process lifetimes.
store = ExecutionStore(db_path)

open(os.path.join(barrier_dir, worker_id + ".ready"), "w").close()
deadline = time.time() + 10
while time.time() < deadline:
    if len(os.listdir(barrier_dir)) >= 2:
        break
    time.sleep(0.01)

try:
    result = store.claim_unit(run_id, unit_id, worker_id)
    print("CLAIMED", result.fencing_token)
except AlreadyClaimedError:
    print("ALREADY_CLAIMED")
"""


def _run_claim_subprocess(db_path, run_id, unit_id, worker_id) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", _CLAIM_SCRIPT, LIB_ROOT, str(db_path), run_id, unit_id, worker_id],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_two_separate_processes_contend_for_one_claim(tmp_path):
    """Strengthened: the weak version ran process A to completion (exit) and
    only THEN started process B -- no overlap, so it only proved that a
    committed claim is visible later (the same fact reopen already covers).
    This spawns both processes concurrently and makes them rendezvous on a
    shared barrier file AFTER opening the store but immediately BEFORE
    either calls claim_unit -- an earlier version put the barrier before
    ``ExecutionStore()``, which left plenty of room for one process to
    finish opening (and even claim) before the other reached the barrier,
    so the claim attempts overlapped in only 1 of 20 trials. Rendezvousing
    right before the claim call makes the two attempts genuinely race
    against SQLite's own locking."""
    db_path = tmp_path / "run.db"
    store = ExecutionStore(db_path)
    store.create_run("r1", driver="inline", backend="mock", model="m", adapter_version="1")
    store.register_units("r1", ["only-unit"])

    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir()

    procs = [
        subprocess.Popen(
            [
                sys.executable, "-c", _RENDEZVOUS_CLAIM_SCRIPT,
                LIB_ROOT, str(db_path), "r1", "only-unit", worker_id, str(barrier_dir),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for worker_id in ("proc-a", "proc-b")
    ]
    outputs = []
    for proc in procs:
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, err
        outputs.append(out.strip())

    claimed = [o for o in outputs if o.startswith("CLAIMED")]
    already_claimed = [o for o in outputs if o == "ALREADY_CLAIMED"]
    assert len(claimed) == 1, outputs
    assert len(already_claimed) == 1, outputs


# -- A-min.1 exit criterion: cross-process blocked claim + status + reopen -------

_CLAIM_AND_HOLD_SCRIPT = """
import sys
import time
sys.path.insert(0, sys.argv[1])
from content_pipeline.execution.store import ExecutionStore

db_path, run_id, unit_id, worker_id = sys.argv[2:6]
store = ExecutionStore(db_path)
result = store.claim_unit(run_id, unit_id, worker_id, lease_seconds=300)
print("CLAIMED", result.fencing_token, flush=True)
time.sleep(30)  # stay ALIVE -- "deliberately blocked", not exited
"""


def test_exit_criterion_blocked_claim_status_digest_and_reopen(tmp_path):
    """Strengthened: the weak version had the holder process EXIT before the
    digest was read, so the digest saw a dead worker's already-committed row
    -- indistinguishable from the plain reopen test. The plan's exit
    criterion requires a LIVE holder concurrent with the digest read. This
    keeps the holder process alive (blocked in a sleep) for the whole digest
    query and asserts it never exited during that window."""
    db_path = tmp_path / "run.db"
    store = ExecutionStore(db_path)
    store.create_run("r1", driver="inline", backend="mock", model="m", adapter_version="1")
    store.register_units("r1", ["u0", "u1", "u2"])

    proc = subprocess.Popen(
        [sys.executable, "-c", _CLAIM_AND_HOLD_SCRIPT, LIB_ROOT, str(db_path), "r1", "u0", "blocked-worker"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        line = proc.stdout.readline()
        assert line.startswith("CLAIMED"), proc.stderr.read()
        assert proc.poll() is None  # still alive: a genuinely blocked, live holder

        status_store = ExecutionStore(db_path)
        digest = compute_status(status_store, "r1")
        assert digest.counts_by_state["claimed"] == 1
        assert digest.counts_by_state["pending"] == 2
        assert digest.oldest_in_flight_age_s is not None
        assert digest.oldest_in_flight_age_s >= 0

        assert proc.poll() is None  # still alive throughout the digest read

        # Reopening the database preserves run truth.
        reopened = ExecutionStore(db_path)
        unit = reopened.get_unit("r1", "u0")
        assert unit.state is UnitState.CLAIMED
        assert unit.claimed_by == "blocked-worker"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


# -- migration transactionality ----------------------------------------------------

def test_mid_migration_failure_does_not_wedge_the_database(tmp_path, monkeypatch):
    """Defect 1: a failure applying a later migration step must roll back
    everything applied so far (including schema_version itself), so a retry
    starts from a clean, empty file -- never 'table schema_version already
    exists'."""
    import content_pipeline.execution.store as store_mod

    db_path = tmp_path / "run.db"
    original_migrations = store_mod._MIGRATIONS
    broken = list(original_migrations)
    broken[2] = ["THIS IS NOT VALID SQL AT ALL;"]  # fails applying the 3rd step
    monkeypatch.setattr(store_mod, "_MIGRATIONS", broken)

    with pytest.raises(sqlite3.OperationalError):
        store_mod.ExecutionStore(db_path)

    monkeypatch.setattr(store_mod, "_MIGRATIONS", original_migrations)

    # Reopening with the REAL migrations must succeed cleanly -- no leftover
    # partial DDL from the failed attempt.
    store = store_mod.ExecutionStore(db_path)
    store.create_run("r1", driver="inline", backend="mock", model="m", adapter_version="1")
    assert store.get_run("r1") is not None


_FIRST_OPEN_SCRIPT = """
import sys
sys.path.insert(0, sys.argv[1])
from content_pipeline.execution.store import ExecutionStore

db_path = sys.argv[2]
try:
    ExecutionStore(db_path)
    print("ok")
except Exception as exc:  # noqa: BLE001 -- surface unexpected failures
    print(f"error: {exc}")
"""


def test_opening_an_up_to_date_store_does_not_take_a_write_lock(tmp_path):
    """Regression: ``_migrate`` used to take ``BEGIN IMMEDIATE`` even when
    the schema was already current, so constructing a fresh
    ``ExecutionStore`` while ANY other connection held an open write
    transaction blocked for the full busy timeout and then failed --
    including a status-probe process, whose entire point is to stay cheap
    against a live run. The fix is check-then-lock-then-recheck: the version
    read is lock-free, and ``BEGIN IMMEDIATE`` is taken only when a
    migration is actually needed. This holds an unrelated write transaction
    open on a separate connection and asserts a second ``ExecutionStore()``
    plus a status digest still complete promptly -- not that they merely
    eventually succeed."""
    db_path = tmp_path / "run.db"
    setup_store = ExecutionStore(db_path)  # first open: brings the schema current
    setup_store.create_run("r1", driver="inline", backend="mock", model="m", adapter_version="1")
    setup_store.register_units("r1", ["u0"])

    # Hold an UNRELATED write transaction open on a separate connection --
    # standing in for a stuck or slow writer elsewhere in the process fleet.
    holder = sqlite3.connect(str(db_path), timeout=5.0)
    holder.execute("PRAGMA busy_timeout=5000")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("CREATE TABLE IF NOT EXISTS _unrelated_holder_marker(x)")
    try:
        start = time.monotonic()
        # Constructing a fresh ExecutionStore (schema already current) and
        # reading a status digest are both read-only against an unrelated
        # held write lock -- neither should need to wait for it at all.
        store = ExecutionStore(db_path)
        digest = compute_status(store, "r1")
        elapsed = time.monotonic() - start
        assert digest.total_units == 1
        # Well inside the 5s busy_timeout budget -- a blocked wait-then-fail
        # would take the full 5s and then raise, never complete this fast.
        assert elapsed < 1.0, elapsed
    finally:
        holder.rollback()
        holder.close()


def test_concurrent_first_open_does_not_lock(tmp_path):
    """Defect 1's second half: concurrent first-opens against a brand-new
    path must all succeed -- no 'database is locked' from a version-read/DDL
    race, and no 'database is locked' from the WAL-mode pragma either (the
    regression this test now also covers: PRAGMA journal_mode = WAL does not
    honor busy_timeout and used to fail in ~0.2ms under contention).

    A single 4-thread trial is a coin flip: the original bug this guards
    against failed 5 of 40 such trials, so one green run proves nothing.
    This uses SEPARATE PROCESSES (real independent connections, not just
    independent threads sharing one interpreter) and repeats the race many
    times, asserting every process in every trial succeeded."""
    n_trials = 15
    n_procs = 4
    failures = []

    for trial in range(n_trials):
        db_path = tmp_path / f"run-{trial}.db"
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", _FIRST_OPEN_SCRIPT, LIB_ROOT, str(db_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(n_procs)
        ]
        outputs = []
        for proc in procs:
            out, err = proc.communicate(timeout=30)
            assert proc.returncode == 0, err
            outputs.append(out.strip())
        if outputs != ["ok"] * n_procs:
            failures.append((trial, outputs))

    assert not failures, failures


# -- accepted_text (A-min.2) -------------------------------------------------

def test_accept_with_text_round_trips_through_a_store_reopen(tmp_path):
    db_path = tmp_path / "run.db"
    store = _seeded_store(tmp_path)
    claim = store.claim_unit("run-1", "u0", "worker-a")
    store.accept_unit("run-1", "u0", claim.fencing_token, text="the accepted body")

    unit = store.get_unit("run-1", "u0")
    assert unit.accepted_text == "the accepted body"

    reopened = ExecutionStore(db_path)
    reopened_unit = reopened.get_unit("run-1", "u0")
    assert reopened_unit.accepted_text == "the accepted body"


def test_accept_without_text_leaves_accepted_text_none(tmp_path):
    store = _seeded_store(tmp_path)
    claim = store.claim_unit("run-1", "u0", "worker-a")
    store.accept_unit("run-1", "u0", claim.fencing_token)  # no text passed

    unit = store.get_unit("run-1", "u0")
    assert unit.accepted_text is None


def test_migration_adds_accepted_text_column_to_an_existing_database(tmp_path):
    """A store created against the OLD schema (before the accepted_text
    migration step existed) must, on reopen, gain the new column AND still
    read its pre-existing rows -- not just a fresh database."""
    import content_pipeline.execution.store as store_mod

    db_path = tmp_path / "run.db"
    old_migrations = store_mod._MIGRATIONS[:-1]  # drop the accepted_text step
    original_migrations = store_mod._MIGRATIONS
    store_mod._MIGRATIONS = old_migrations
    try:
        old_store = ExecutionStore(db_path)
        old_store.create_run(
            "r1", driver="inline", backend="mock", model="m", adapter_version="1"
        )
        old_store.register_units("r1", ["u0"])
        claim = old_store.claim_unit("r1", "u0", "worker-a")
        old_store.accept_unit("r1", "u0", claim.fencing_token)
    finally:
        store_mod._MIGRATIONS = original_migrations

    # Reopen with the real (current) migrations -- must apply the new step
    # and still see the pre-existing row.
    reopened = ExecutionStore(db_path)
    unit = reopened.get_unit("r1", "u0")
    assert unit is not None
    assert unit.state is UnitState.ACCEPTED
    assert unit.accepted_text is None

    with sqlite3.connect(str(db_path)) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(units)")}
    assert "accepted_text" in cols


# -- apply attempts (A-min.2) -------------------------------------------------

def test_record_apply_started_and_succeeded_append_attempts(tmp_path):
    store = _seeded_store(tmp_path)
    claim = store.claim_unit("run-1", "u0", "worker-a")
    store.accept_unit("run-1", "u0", claim.fencing_token)

    store.record_apply_started("run-1", "u0")
    store.record_apply_succeeded("run-1", "u0")

    kinds = [a.kind for a in store.list_attempts("run-1", "u0")]
    assert kinds == [
        AttemptKind.CLAIM,
        AttemptKind.ACCEPT,
        AttemptKind.APPLY_STARTED,
        AttemptKind.APPLY_SUCCEEDED,
    ]


def test_record_apply_started_refuses_a_pending_unit(tmp_path):
    store = _seeded_store(tmp_path)
    with pytest.raises(NotAcceptedError):
        store.record_apply_started("run-1", "u0")


def test_record_apply_succeeded_refuses_a_pending_unit(tmp_path):
    store = _seeded_store(tmp_path)
    with pytest.raises(NotAcceptedError):
        store.record_apply_succeeded("run-1", "u0")


def test_record_apply_started_refuses_a_claimed_unit(tmp_path):
    store = _seeded_store(tmp_path)
    store.claim_unit("run-1", "u0", "worker-a")
    with pytest.raises(NotAcceptedError):
        store.record_apply_started("run-1", "u0")


def test_record_apply_succeeded_refuses_a_claimed_unit(tmp_path):
    store = _seeded_store(tmp_path)
    store.claim_unit("run-1", "u0", "worker-a")
    with pytest.raises(NotAcceptedError):
        store.record_apply_succeeded("run-1", "u0")


def test_record_apply_started_twice_appends_two_attempts_and_does_not_error(tmp_path):
    """Apply-record idempotence is derived by scanning the attempt log
    (per D6), not enforced by refusing a second call -- recording started
    twice must simply append two attempts."""
    store = _seeded_store(tmp_path)
    claim = store.claim_unit("run-1", "u0", "worker-a")
    store.accept_unit("run-1", "u0", claim.fencing_token)

    store.record_apply_started("run-1", "u0")
    store.record_apply_started("run-1", "u0")

    started = [
        a for a in store.list_attempts("run-1", "u0") if a.kind is AttemptKind.APPLY_STARTED
    ]
    assert len(started) == 2


def test_record_apply_succeeded_twice_appends_two_attempts_and_does_not_error(tmp_path):
    store = _seeded_store(tmp_path)
    claim = store.claim_unit("run-1", "u0", "worker-a")
    store.accept_unit("run-1", "u0", claim.fencing_token)

    store.record_apply_succeeded("run-1", "u0")
    store.record_apply_succeeded("run-1", "u0")

    succeeded = [
        a for a in store.list_attempts("run-1", "u0") if a.kind is AttemptKind.APPLY_SUCCEEDED
    ]
    assert len(succeeded) == 2


def test_record_apply_unknown_run_raises(tmp_path):
    store = _new_store(tmp_path)
    with pytest.raises(UnknownRunError):
        store.record_apply_started("no-such-run", "u0")


def test_record_apply_unknown_unit_raises(tmp_path):
    store = _seeded_store(tmp_path)
    with pytest.raises(UnknownUnitError):
        store.record_apply_started("run-1", "does-not-exist")


# -- environment snapshot (item 5, A-min.4) -----------------------------------


def test_create_run_with_no_environment_round_trips_none(tmp_path):
    store = _new_store(tmp_path)
    run = store.create_run(
        "run-1", driver="inline", backend="mock", model="m", adapter_version="1"
    )
    assert run.environment is None
    assert store.get_run("run-1").environment is None


def test_create_run_environment_round_trips_through_a_store_reopen(tmp_path):
    store = _new_store(tmp_path)
    store.create_run(
        "run-1",
        driver="inline",
        backend="mock",
        model="m",
        adapter_version="1",
        environment={"PWD": "D:\\dev\\proj"},
    )
    reopened = ExecutionStore(store.db_path)
    run = reopened.get_run("run-1")
    assert run.environment == {"PWD": "D:\\dev\\proj"}


def test_migration_adds_environment_column_to_an_existing_database(tmp_path):
    """Same pattern as test_migration_adds_accepted_text_column_to_an_existing_database:
    a store created against the OLD schema (before the environment column
    existed) must, on reopen, gain the new column AND still read its
    pre-existing rows."""
    import content_pipeline.execution.store as store_mod

    db_path = tmp_path / "run.db"
    old_migrations = store_mod._MIGRATIONS[:-1]  # drop the environment step
    original_migrations = store_mod._MIGRATIONS
    store_mod._MIGRATIONS = old_migrations
    try:
        old_store = ExecutionStore(db_path)
        old_store.create_run(
            "r1", driver="inline", backend="mock", model="m", adapter_version="1"
        )
    finally:
        store_mod._MIGRATIONS = original_migrations

    reopened = ExecutionStore(db_path)
    run = reopened.get_run("r1")
    assert run is not None
    assert run.environment is None

    with sqlite3.connect(str(db_path)) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    assert "environment" in cols


def test_older_reader_opening_a_migrated_database_does_not_break(tmp_path):
    """Schema-DOWNGRADE tolerance, the opposite direction from the migration
    test above: a database already migrated to the CURRENT (newer) schema
    must still open and read cleanly under an OLDER _MIGRATIONS list that
    knows nothing about the environment column -- the exact scenario an
    older library version opening a newer database hits. Must not raise."""
    import content_pipeline.execution.store as store_mod

    db_path = tmp_path / "run.db"
    # Fully migrate the database under the CURRENT (real) migrations first.
    current_store = ExecutionStore(db_path)
    current_store.create_run(
        "r1",
        driver="inline",
        backend="mock",
        model="m",
        adapter_version="1",
        environment={"PWD": "D:\\dev\\proj"},
    )
    current_store.register_units("r1", ["u0"])

    # Now reopen under a TRUNCATED migrations list (as an older reader
    # would ship) -- must not raise, and must still read the pre-existing
    # rows (the newer `environment` column is simply invisible to it).
    older_migrations = store_mod._MIGRATIONS[:-1]
    original_migrations = store_mod._MIGRATIONS
    store_mod._MIGRATIONS = older_migrations
    try:
        older_reader = ExecutionStore(db_path)
        run = older_reader.get_run("r1")
        assert run is not None
        # The column physically exists on disk (this DB was already fully
        # migrated before the truncated _MIGRATIONS list was installed) --
        # `_row_to_run`'s `"environment" in row.keys()` guard reads it fine;
        # the point of this test is that opening/reading raises nothing.
        assert run.environment == {"PWD": "D:\\dev\\proj"}
        units = older_reader.list_units("r1")
        assert [u.unit_id for u in units] == ["u0"]
    finally:
        store_mod._MIGRATIONS = original_migrations


# -- lease_for (item 2, A-min.4) -----------------------------------------------


def test_lease_for_undeclared_returns_exactly_the_default():
    assert lease_for(None) == DEFAULT_LEASE_SECONDS


def test_lease_for_declared_but_small_is_floored_at_the_default():
    assert lease_for(1.0) == DEFAULT_LEASE_SECONDS


def test_lease_for_213_seconds_yields_426():
    assert lease_for(213.0) == pytest.approx(213.0 * LEASE_HEADROOM_FACTOR)
    assert lease_for(213.0) == pytest.approx(426.0)


def test_lease_for_non_positive_declared_value_returns_default():
    assert lease_for(0.0) == DEFAULT_LEASE_SECONDS
    assert lease_for(-5.0) == DEFAULT_LEASE_SECONDS


def test_lease_for_custom_default_is_honored():
    assert lease_for(None, default=600.0) == 600.0
    assert lease_for(1.0, default=600.0) == 600.0
    assert lease_for(400.0, default=600.0) == 800.0
