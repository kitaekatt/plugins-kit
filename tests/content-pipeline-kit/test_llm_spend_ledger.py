"""SL-1: the spend-ledger core -- schema, admission, settle/release, partition.

Every load-bearing assertion here was shown RED by reverting the production
line it protects (docs/reference/vacuous-checks.md); the revert is named in the
test's docstring so the counterfactual can be re-run.

Scope boundaries, so a reader does not look here for them: the halt switch
(`halt`, `resume`, `check_halted`) is SL-2's file; leases, `reclaim_orphans`,
`renew` and `leaks` WRITES are SL-3's; the cross-process acceptance suite and
the `BEGIN IMMEDIATE` source guard are SL-4's. This file asserts the partition
with the `leaks` table EMPTY -- SL-3 re-asserts it with rows present -- and
injects `reclaimed` rows and `leaks` rows with raw SQL where it needs them, to
exercise validation without depending on reclaim behaviour that does not exist
yet.
"""

import ast
import sqlite3
import sys
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from content_pipeline.llm import spend_ledger as sl
from content_pipeline.llm.platform import BudgetExceededError

MODULE_PATH = Path(sl.__file__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make(tmp_path, *, cap_usd=1.0, run_id="run-1", name="ledger.sqlite", **kwargs):
    return sl.create_ledger(tmp_path / name, cap_usd=cap_usd, run_id=run_id, **kwargs)


def raw(ledger):
    """A direct connection, for injecting state the public API will not write."""
    conn = sqlite3.connect(str(ledger.path))
    conn.row_factory = sqlite3.Row
    return conn


def insert_row(ledger, **fields):
    """Insert one `ledger` row directly, bypassing admission and validation."""
    row = {
        "id": fields.pop("id", "injected"),
        "generation": 0,
        "state": "open",
        "reserved": 1,
        "settled": None,
        "scope": "",
        "identifier": "",
        "model": "",
        "created_at": "2026-01-01T00:00:00+00:00",
        "lease_expires_at": None,
        "resolved_at": None,
    }
    row.update(fields)
    conn = raw(ledger)
    try:
        conn.execute(
            "INSERT INTO ledger (id, generation, state, reserved, settled, scope, identifier, "
            "model, created_at, lease_expires_at, resolved_at) "
            "VALUES (:id, :generation, :state, :reserved, :settled, :scope, :identifier, "
            ":model, :created_at, :lease_expires_at, :resolved_at)",
            row,
        )
        conn.commit()
    finally:
        conn.close()
    return row["id"]


def insert_leak(ledger, *, ledger_id, reported_cost=1, generation=1):
    conn = raw(ledger)
    try:
        conn.execute(
            "INSERT INTO leaks (ledger_id, reported_cost, presented_at, generation) "
            "VALUES (?, ?, ?, ?)",
            (ledger_id, reported_cost, "2026-01-01T00:00:00+00:00", generation),
        )
        conn.commit()
    finally:
        conn.close()


def independent_partition(ledger):
    """Recompute the partition from rows with SQL written independently of the
    production queries, so comparing the two is a cross-check rather than a
    restatement of one implementation."""
    conn = raw(ledger)
    try:
        rows = conn.execute("SELECT state, reserved, settled FROM ledger").fetchall()
        leaks = conn.execute("SELECT reported_cost FROM leaks").fetchall()
    finally:
        conn.close()
    settled = reserved = unknown = leaked = reclaimed = 0
    for r in rows:
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
    leaked += sum(int(k["reported_cost"]) for k in leaks)
    return {
        "settled": settled,
        "reserved": reserved,
        "unknown": unknown,
        "leaked": leaked,
        "reclaimed": reclaimed,
        "outstanding": settled + reserved + unknown + leaked,
    }


# ---------------------------------------------------------------------------
# Import hygiene
# ---------------------------------------------------------------------------


class TestImportHygiene:
    def test_module_imports_only_stdlib_and_platform(self):
        """Design section 3: stdlib plus `decimal` only, and one one-way edge to
        `llm.platform`. Nothing from `content_pipeline.execution`.

        Revert shown red: added `import yaml` at module top -> this test names
        `yaml` as a non-stdlib import. Restored.
        """
        allowed_stdlib = {
            "__future__", "ctypes", "json", "os", "sqlite3", "sys", "time", "uuid",
            "warnings", "contextlib", "dataclasses", "datetime", "decimal", "pathlib",
            "typing",
        }
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root not in allowed_stdlib:
                        offenders.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                name = node.module or ""
                root = name.split(".")[0]
                if root == "content_pipeline":
                    if name != "content_pipeline.llm.platform":
                        offenders.append(name)
                elif root not in allowed_stdlib:
                    offenders.append(name)
        assert not offenders, f"disallowed imports: {sorted(set(offenders))}"

    def test_module_never_reaches_into_execution(self):
        """`llm/` may not import `execution/`; the network-path helper is a
        deliberate duplicate instead. Checked over IMPORT NODES, not over the
        text -- the docstrings name `content_pipeline.execution` on purpose, to
        point a reader at the original the duplicate must track.

        Revert shown red: added
        `from content_pipeline.execution.store import looks_like_network_path`
        -> this test failed naming that module. Restored.
        """
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        assert not [m for m in imported if m.startswith("content_pipeline.execution")]

    def test_carries_no_consumer_vocabulary(self):
        """The library is domain-free: no consumer-specific provider, model or
        environment vocabulary leaks in from the prior-art ledger."""
        text = MODULE_PATH.read_text(encoding="utf-8").lower()
        for term in ("loc_", "openrouter", "deepseek", "glossary"):
            assert term not in text, f"consumer vocabulary {term!r} in spend_ledger.py"

    def test_module_is_ascii(self):
        MODULE_PATH.read_text(encoding="utf-8").encode("ascii")


# ---------------------------------------------------------------------------
# Create / open
# ---------------------------------------------------------------------------


class TestCreateAndOpen:
    def test_full_schema_in_one_create(self, tmp_path):
        """Design section 13: SL-1 ships BOTH tables, the control tables and a
        declared `schema_version` in one schema, so no later unit migrates."""
        ledger = make(tmp_path)
        conn = raw(ledger)
        try:
            names = {
                r["name"]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            cols = {
                table: {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                for table in ("policy", "ledger", "leaks", "halt", "halt_history")
            }
            version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        finally:
            conn.close()
        assert {"schema_version", "policy", "halt", "halt_history", "ledger", "leaks"} <= names
        assert {"run_id", "cap_nano", "nano_per_usd", "meta"} <= cols["policy"]
        assert {"generation", "lease_expires_at", "state", "reserved", "settled"} <= cols["ledger"]
        # A leak is not a reservation: no `reserved`, no lease, no state. That is
        # what keeps the five `ledger` states exhaustive.
        assert cols["leaks"] >= {"ledger_id", "reported_cost", "presented_at", "generation"}
        assert not (cols["leaks"] & {"reserved", "state", "lease_expires_at"})
        assert {"halted", "reason", "detail", "since"} <= cols["halt"]
        assert {"action", "reason", "detail", "forced", "at"} <= cols["halt_history"]
        assert version == sl.SCHEMA_VERSION == ledger.schema_version == 1

    def test_five_states_are_the_declared_set(self):
        assert sl.LEDGER_STATES == ("open", "unknown", "settled", "overbilled", "reclaimed")

    def test_open_pins_cap_and_run_from_the_file(self, tmp_path):
        make(tmp_path, cap_usd=2.5, run_id="run-xyz")
        reopened = sl.open_ledger(tmp_path / "ledger.sqlite")
        assert reopened.cap_usd == 2.5
        assert reopened.run_id == "run-xyz"

    def test_open_refuses_a_file_that_is_not_a_ledger(self, tmp_path):
        stray = tmp_path / "stray.sqlite"
        stray.write_bytes(b"")
        with pytest.raises(sl.LedgerStateInvalid):
            sl.open_ledger(stray)

    def test_open_refuses_a_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sl.open_ledger(tmp_path / "absent.sqlite")

    def test_open_refuses_a_foreign_schema_version(self, tmp_path):
        ledger = make(tmp_path)
        conn = raw(ledger)
        try:
            conn.execute("UPDATE schema_version SET version = 99")
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(sl.LedgerStateInvalid):
            sl.open_ledger(ledger.path)

    @pytest.mark.parametrize("cap", [0, -1, 0.0, Decimal("-0.5"), float("inf"), float("nan")])
    def test_create_refuses_a_cap_that_is_not_positive_and_finite(self, tmp_path, cap):
        with pytest.raises(ValueError):
            sl.create_ledger(tmp_path / f"c{cap}.sqlite", cap_usd=cap, run_id="r")

    def test_create_refuses_an_empty_run_id(self, tmp_path):
        with pytest.raises(ValueError):
            sl.create_ledger(tmp_path / "e.sqlite", cap_usd=1.0, run_id="")

    def test_from_env_unset_is_none(self):
        assert sl.spend_ledger_from_env(env={}) is None
        assert sl.spend_ledger_from_env(env={sl.LEDGER_PATH_ENV: "  "}) is None

    def test_from_env_opens_the_named_file(self, tmp_path):
        make(tmp_path, cap_usd=3.0)
        got = sl.spend_ledger_from_env(env={sl.LEDGER_PATH_ENV: str(tmp_path / "ledger.sqlite")})
        assert got is not None and got.cap_usd == 3.0


class TestExclusiveCreate:
    def test_create_refuses_an_existing_file_and_never_repairs(self, tmp_path):
        """Design section 5: `create_ledger` opens with "xb" and never repairs.

        Revert shown red: changed `open(target, "xb")` to `open(target, "ab")`
        -> this test failed with
        `sqlite3.OperationalError: table schema_version already exists`, because
        the second create then opened the live file and tried to lay its schema
        over the first one's. A repair attempt in place of a clean refusal is
        exactly what the exclusive open exists to prevent. Restored.
        """
        first = make(tmp_path, cap_usd=1.0, run_id="first")
        with pytest.raises(FileExistsError):
            sl.create_ledger(first.path, cap_usd=999.0, run_id="second")
        # The loser's cap never touched the file.
        assert sl.open_ledger(first.path).cap_usd == 1.0
        assert sl.open_ledger(first.path).run_id == "first"

    def test_concurrent_create_yields_exactly_one_winner(self, tmp_path):
        """Two racers, one winner, one FileExistsError -- the loser then opens."""
        target = tmp_path / "raced.sqlite"
        barrier = threading.Barrier(6)
        outcomes = []
        lock = threading.Lock()

        def racer(n):
            barrier.wait()
            try:
                sl.create_ledger(target, cap_usd=1.0, run_id=f"run-{n}")
                result = "created"
            except FileExistsError:
                result = "exists"
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=racer, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive()
        assert outcomes.count("created") == 1
        assert outcomes.count("exists") == 5
        assert sl.open_ledger(target).cap_usd == 1.0


# ---------------------------------------------------------------------------
# Nano arithmetic
# ---------------------------------------------------------------------------


class TestNanoArithmetic:
    def test_nano_per_usd(self):
        assert sl.NANO_PER_USD == 1_000_000_000

    @pytest.mark.parametrize(
        "usd,nano",
        [
            (1, 1_000_000_000),
            (0.1, 100_000_000),
            (Decimal("0.000000001"), 1),
            (0.0000000001, 1),  # a tenth of a nano rounds UP to one nano
            (Decimal("1.0000000001"), 1_000_000_001),
            (0.3, 300_000_000),  # not 300000000.000000044, which Decimal(float) gives
        ],
    )
    def test_usd_to_nano_rounds_up(self, usd, nano):
        """Ceiling rounding: a cost the ledger cannot represent is charged as the
        next whole nano, never truncated toward free.

        Revert shown red: changed `ROUND_CEILING` to `ROUND_DOWN` -> 2 of the
        6 cases failed (`assert 0 == 1`) and 4 passed, the two being the ones
        whose value is not a whole number of nano. Read per CASE: a case with an
        exactly-representable amount cannot show rounding direction at all, so
        the sub-nano cases are the ones carrying this property. Restored.
        """
        assert sl.usd_to_nano(usd) == nano

    @pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
    def test_usd_to_nano_refuses_non_finite(self, bad):
        with pytest.raises(ValueError):
            sl.usd_to_nano(bad)

    @pytest.mark.parametrize("bad", ["1.0", None, True])
    def test_usd_to_nano_refuses_a_non_number(self, bad):
        with pytest.raises(TypeError):
            sl.usd_to_nano(bad)

    def test_estimate_reservation_prices_the_output_ceiling(self):
        pricing = {"m": {"input": 1.0, "output": 2.0}}
        # 1000 input at $1/1M + 500 output at $2/1M
        assert sl.estimate_reservation(
            "m", input_tokens=1000, max_output_tokens=500, pricing=pricing
        ) == pytest.approx((1000 * 1.0 + 500 * 2.0) / 1e6)

    def test_estimate_reservation_keeps_the_unknown_model_keyerror(self):
        """A typo must never reserve 0 and then bill."""
        with pytest.raises(KeyError):
            sl.estimate_reservation("typo", input_tokens=1, max_output_tokens=1, pricing={})


# ---------------------------------------------------------------------------
# Cap immutability and identity pinning
# ---------------------------------------------------------------------------


class TestCapImmutability:
    def test_there_is_no_cap_setter(self, tmp_path):
        """The cap is write-once. A run must not be able to widen its own cap."""
        ledger = make(tmp_path, cap_usd=1.0)
        with pytest.raises(AttributeError):
            ledger.cap_usd = 99.0
        with pytest.raises(AttributeError):
            ledger.cap_nano = 99
        assert ledger.cap_usd == 1.0
        assert not any(
            name in dir(type(ledger)) for name in ("set_cap", "set_cap_usd", "update_cap")
        )

    def test_a_tampered_cap_row_refuses_the_transaction(self, tmp_path):
        """Design section 5: the pinned cap is re-validated every transaction,
        so a file whose cap changed under the handle admits nothing.

        Revert shown red: deleted the `cap_nano` comparison from
        `_validate_identity` -> this test failed with
        `SpendCapExceeded: budget exceeded ... measured 10.0 > budget 1.0`.
        Note what that shows, because it is not what one would guess: the
        in-memory cap still bounded the reserve, so nothing was over-admitted --
        what was lost is the DIAGNOSIS. A file whose cap no longer matches the
        handle reported an ordinary budget verdict instead of the tampering, and
        a caller catching `BudgetExceededError` would have swallowed it.
        Restored.
        """
        ledger = make(tmp_path, cap_usd=1.0)
        conn = raw(ledger)
        try:
            conn.execute("UPDATE policy SET cap_nano = 500000000000 WHERE id = 1")
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(sl.LedgerIdentityChanged):
            ledger.reserve(10.0)
        with pytest.raises(sl.LedgerIdentityChanged):
            ledger.status()
        conn = raw(ledger)
        try:
            assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0
        finally:
            conn.close()

    def test_a_tampered_run_id_refuses_the_transaction(self, tmp_path):
        """Revert shown red: deleted the `run_id` comparison from
        `_validate_identity` -> this test failed with `DID NOT RAISE
        LedgerIdentityChanged`. Restored.
        """
        ledger = make(tmp_path, run_id="run-1")
        conn = raw(ledger)
        try:
            conn.execute("UPDATE policy SET run_id = 'someone-elses-run' WHERE id = 1")
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(sl.LedgerIdentityChanged):
            ledger.reserve(0.1)

    def test_a_tampered_money_unit_refuses_the_transaction(self, tmp_path):
        ledger = make(tmp_path)
        conn = raw(ledger)
        try:
            conn.execute("UPDATE policy SET nano_per_usd = 1000000 WHERE id = 1")
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(sl.LedgerIdentityChanged):
            ledger.reserve(0.1)

    def test_a_deleted_policy_row_refuses_the_transaction(self, tmp_path):
        ledger = make(tmp_path)
        conn = raw(ledger)
        try:
            conn.execute("DELETE FROM policy")
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(sl.LedgerIdentityChanged):
            ledger.reserve(0.1)


# ---------------------------------------------------------------------------
# Structural validation -- one case per malformed combination
# ---------------------------------------------------------------------------


#: Clauses whose malformation CANNOT be injected, because a column constraint
#: refuses the write first. They are shown to hold by the write being refused
#: (`test_a_null_generation_cannot_be_written_at_all`), not by the clause
#: firing. The clause stays in the query as defence against a file some other
#: writer produced, and is reported plainly as unreachable at runtime rather
#: than given a test that looks like coverage.
CONSTRAINT_CARRIED = {"generation_null"}


class TestStructuralValidation:
    """Every clause of the per-transaction malformed-state query.

    Each of the 10 injectable cases was shown red by making
    `_validate_structure` SKIP that one clause (`if clause == "<name>":
    continue`) and re-running only that case: each failed with `DID NOT RAISE
    LedgerStateInvalid`, so no case is carried by another clause. The clause is
    neutered rather than deleted on purpose -- deleting it would shrink
    `STRUCTURAL_CLAUSES` and make the parametrized case VANISH instead of fail,
    which is not a counterfactual. Enforcement restored.
    """

    def test_every_declared_clause_has_a_case_here(self):
        """A new clause without a case here would be unprotected, so the clause
        list and this file's coverage are pinned to each other."""
        declared = {name for _table, name, _sql in sl.STRUCTURAL_CLAUSES}
        assert declared == set(MALFORMED_CASES) | CONSTRAINT_CARRIED, (
            f"clauses with no case: {sorted(declared - set(MALFORMED_CASES))}; "
            f"cases with no clause: {sorted(set(MALFORMED_CASES) - declared)}"
        )

    @pytest.mark.parametrize(
        "clause",
        sorted(c for _t, c, _s in sl.STRUCTURAL_CLAUSES if c not in CONSTRAINT_CARRIED),
    )
    def test_malformed_state_refuses_every_operation(self, tmp_path, clause):
        ledger = make(tmp_path, name=f"{clause}.sqlite")
        MALFORMED_CASES[clause](ledger)
        with pytest.raises(sl.LedgerStateInvalid) as read_exc:
            ledger.status()
        assert clause in str(read_exc.value)
        with pytest.raises(sl.LedgerStateInvalid):
            ledger.reserve(0.01)

    def test_a_well_formed_row_in_every_state_validates(self, tmp_path):
        """The counterpart: the same shapes, well formed, pass every clause --
        so the cases above fail for their malformation, not for existing."""
        ledger = make(tmp_path, cap_usd=100.0)
        insert_row(ledger, id="a", state="open", reserved=5, settled=None)
        insert_row(ledger, id="b", state="unknown", reserved=5, settled=None)
        insert_row(ledger, id="c", state="settled", reserved=5, settled=5)
        insert_row(ledger, id="d", state="overbilled", reserved=5, settled=6)
        insert_row(ledger, id="e", state="reclaimed", reserved=5, settled=0)
        insert_leak(ledger, ledger_id="e", reported_cost=3)
        assert ledger.status().requests == 5


# `insert_row`/`insert_leak` write one violation each; a case is named by the
# clause it must trip.
MALFORMED_CASES = {
    #: SQLite INTEGER affinity converts "5" to 5, so a non-integer case needs a
    #: value that cannot convert losslessly -- 2.5 stays REAL.
    "reserved_not_integer": lambda L: insert_row(L, id="x", reserved=2.5),
    "reserved_not_positive": lambda L: insert_row(L, id="x", reserved=0),
    "state_outside_five": lambda L: insert_row(L, id="x", state="anomaly"),
    "settled_set_on_unresolved": lambda L: insert_row(L, id="x", state="open", settled=1),
    "settled_null_on_resolved": lambda L: insert_row(L, id="x", state="settled", settled=None),
    "settled_above_reserved_on_settled": lambda L: insert_row(
        L, id="x", state="settled", reserved=5, settled=6
    ),
    "settled_not_above_reserved_on_overbilled": lambda L: insert_row(
        L, id="x", state="overbilled", reserved=5, settled=5
    ),
    "reported_cost_not_positive": lambda L: (
        insert_row(L, id="r", state="reclaimed", reserved=5, settled=0),
        insert_leak(L, ledger_id="r", reported_cost=0),
    ),
    "ledger_id_unknown": lambda L: insert_leak(L, ledger_id="nobody", reported_cost=1),
    "ledger_row_not_reclaimed": lambda L: (
        insert_row(L, id="s", state="settled", reserved=5, settled=5),
        insert_leak(L, ledger_id="s", reported_cost=1),
    ),
}


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


class TestAdmission:
    def test_a_reservation_that_exactly_fills_the_cap_is_granted(self, tmp_path):
        """Admission is `OUTSTANDING + amount <= cap`, INCLUSIVE at equality.

        Revert shown red: changed the admission comparison from
        `outstanding + amount_nano > self._cap_nano` to `>=` -> this test failed
        with `SpendCapExceeded: budget exceeded for ... measured 1.0 > budget
        1.0`, i.e. the exactly-fitting reservation was refused. The
        one-nano-over sibling failed too, since its own setup reserves the whole
        cap first -- so this case is the one that names the property, and the
        sibling is what stops the boundary being moved the other way. Restored.
        """
        ledger = make(tmp_path, cap_usd=1.0)
        reservation = ledger.reserve(1.0)
        assert reservation.amount_nano == 1_000_000_000
        status = ledger.status()
        assert status.outstanding_usd == 1.0
        assert status.remaining_usd == 0.0

    def test_one_nano_past_the_cap_is_refused(self, tmp_path):
        ledger = make(tmp_path, cap_usd=1.0)
        ledger.reserve(1.0)
        with pytest.raises(sl.SpendCapExceeded):
            ledger.reserve(Decimal("0.000000001"))

    def test_the_boundary_is_exact_at_one_nano(self, tmp_path):
        """A cap of a single nano: the first reservation fits it exactly, the
        second cannot, and ceiling rounding is what makes a sub-nano amount
        consume the whole nano."""
        ledger = make(tmp_path, cap_usd=Decimal("0.000000001"))
        first = ledger.reserve(Decimal("0.0000000005"))
        assert first.amount_nano == 1
        with pytest.raises(sl.SpendCapExceeded):
            ledger.reserve(Decimal("0.0000000001"))

    def test_reservations_sum_to_the_cap_across_several_grants(self, tmp_path):
        ledger = make(tmp_path, cap_usd=1.0)
        ledger.reserve(0.6)
        ledger.reserve(0.4)
        assert ledger.status().outstanding_usd == 1.0
        with pytest.raises(sl.SpendCapExceeded):
            ledger.reserve(Decimal("0.000000001"))

    def test_a_refused_reservation_admits_nothing(self, tmp_path):
        ledger = make(tmp_path, cap_usd=1.0)
        ledger.reserve(1.0)
        with pytest.raises(sl.SpendCapExceeded):
            ledger.reserve(0.5)
        assert ledger.status().requests == 1

    def test_cap_exceeded_is_a_budget_exceeded_error(self, tmp_path):
        """An existing `except BudgetExceededError` must keep working, and the
        four metadata fields must be populated."""
        ledger = make(tmp_path, cap_usd=1.0)
        with pytest.raises(BudgetExceededError) as exc:
            ledger.reserve(2.0, identifier="unit-7", model="m-1")
        assert isinstance(exc.value, sl.SpendCapExceeded)
        assert exc.value.identifier == "unit-7"
        assert exc.value.budget == 1.0
        assert exc.value.measured == 2.0
        assert exc.value.model == "m-1"

    def test_halted_is_also_a_budget_exceeded_error(self):
        assert issubclass(sl.SpendLedgerHalted, BudgetExceededError)

    def test_operational_error_is_not_a_budget_verdict(self):
        """`sqlite3.OperationalError` must stay outside the budget hierarchy, so
        `spend_stop` cannot translate an unreachable ledger into a cap verdict."""
        assert not issubclass(sqlite3.OperationalError, BudgetExceededError)

    @pytest.mark.parametrize("amount", [0, 0.0, -1.0, Decimal("-0.5")])
    def test_reserve_refuses_a_non_positive_amount(self, tmp_path, amount):
        """Every `ledger` row carries a POSITIVE integer `reserved`; admitting 0
        would break that and the structural clause that pins it."""
        ledger = make(tmp_path)
        with pytest.raises(ValueError):
            ledger.reserve(amount)

    def test_reserve_records_the_scope_label_without_using_it_for_admission(self, tmp_path):
        """One cap per file: `scope` is a report label, never an admission input,
        so two scopes contend for the same headroom."""
        ledger = make(tmp_path, cap_usd=1.0)
        ledger.reserve(0.6, scope="stage-a")
        with pytest.raises(sl.SpendCapExceeded):
            ledger.reserve(0.6, scope="stage-b")

    def test_a_null_lease_is_stored_when_no_deadline_is_known(self, tmp_path):
        """With no TTL the lease is NULL, which is what stops any later sweep
        from freeing headroom an unbounded attempt may still consume."""
        ledger = make(tmp_path)
        reservation = ledger.reserve(0.1)
        assert reservation.lease_expires_at is None
        conn = raw(ledger)
        try:
            assert conn.execute("SELECT lease_expires_at FROM ledger").fetchone()[0] is None
        finally:
            conn.close()

    def test_a_ttl_stores_a_future_lease(self, tmp_path):
        ledger = make(tmp_path)
        reservation = ledger.reserve(0.1, ttl_s=120)
        assert reservation.lease_expires_at is not None
        conn = raw(ledger)
        try:
            stored = conn.execute("SELECT lease_expires_at FROM ledger").fetchone()[0]
        finally:
            conn.close()
        assert stored == pytest.approx(reservation.lease_expires_at)


class TestFailClosedOnLockContention:
    def test_reserve_propagates_operational_error_and_admits_nothing(self, tmp_path):
        """Design section 4: an `OperationalError` after `busy_timeout` is
        exhausted is FAIL-CLOSED -- reserve refuses, admits nothing, does not
        retry, and the error is NOT wrapped as a budget verdict.

        Revert shown red: wrapped the `BEGIN IMMEDIATE` in `_writer` with
        `except sqlite3.OperationalError: raise SpendCapExceeded(...) from exc`
        -> this test failed with that `SpendCapExceeded`, chained from
        `sqlite3.OperationalError: database is locked`. An `except
        BudgetExceededError` would have swallowed an unreachable ledger as a cap
        verdict, which is precisely the translation the design forbids.
        Restored.
        """
        ledger = make(tmp_path, cap_usd=10.0, busy_timeout_ms=60)
        blocker = sqlite3.connect(str(ledger.path), timeout=0.06)
        try:
            blocker.execute("PRAGMA busy_timeout = 60")
            blocker.execute("BEGIN IMMEDIATE")
            blocker.execute(
                "INSERT INTO halt_history (action, reason, detail, forced, at) "
                "VALUES ('probe', NULL, NULL, 0, 'now')"
            )
            with pytest.raises(sqlite3.OperationalError) as exc:
                ledger.reserve(1.0)
            assert not isinstance(exc.value, BudgetExceededError)
        finally:
            blocker.rollback()
            blocker.close()
        # Nothing was admitted, and the ledger still works once the lock clears.
        assert ledger.status().requests == 0
        ledger.reserve(1.0)
        assert ledger.status().requests == 1

    def test_no_transaction_level_retry_exists(self):
        """The ONLY retry in the module is the WAL pragma's, which SQLite will
        not do itself. A transaction-level retry would turn the fail-closed
        error above into an over-admission window.

        Revert shown red: added a two-attempt retry loop around the
        `BEGIN IMMEDIATE` in `_writer` -> this test failed naming `_writer` as
        a retrying helper. Restored.
        """
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        retrying = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.dump(node)
            if "'BEGIN" in body and ("While" in body or "sleep" in body):
                retrying.append(node.name)
        assert retrying == [], f"transaction helpers that retry: {retrying}"


# ---------------------------------------------------------------------------
# Settle / release
# ---------------------------------------------------------------------------


class TestSettle:
    def test_a_known_cost_settles_and_frees_the_difference(self, tmp_path):
        ledger = make(tmp_path, cap_usd=1.0)
        reservation = ledger.reserve(0.8)
        ledger.settle(reservation, 0.2)
        status = ledger.status()
        assert status.settled_usd == 0.2
        assert status.reserved_usd == 0.0
        assert status.outstanding_usd == 0.2
        assert status.calls_settled == 1
        # The freed headroom is immediately reservable.
        ledger.reserve(0.8)

    def test_settle_none_holds_the_reservation_as_unknown(self, tmp_path):
        """Design section 5: a settle with an unreadable cost HOLDS the
        reservation as `unknown` at its reserved amount and never releases it to
        0. There is no release-on-failure rule -- the attempt may well have
        billed, so the cap must keep counting it.

        Revert shown red: changed the `cost_nano is None` branch to write
        `state = 'settled', settled = 0` -- a release-on-failure rule -> this
        test failed with `assert 'settled' == 'unknown'`, the row having handed
        back the whole $0.80 of headroom for an attempt that may well have
        billed. Restored.
        """
        ledger = make(tmp_path, cap_usd=1.0)
        reservation = ledger.reserve(0.8)
        ledger.settle(reservation, None)
        conn = raw(ledger)
        try:
            row = conn.execute("SELECT state, settled FROM ledger").fetchone()
        finally:
            conn.close()
        assert row["state"] == "unknown"
        assert row["settled"] is None
        status = ledger.status()
        assert status.unknown_usd == 0.8
        assert status.outstanding_usd == 0.8
        assert status.reserved_usd == 0.0
        assert status.calls_settled == 0
        assert status.reservations_open == 1
        # The held amount still bounds admission.
        with pytest.raises(sl.SpendCapExceeded):
            ledger.reserve(0.3)

    def test_settle_rounds_the_cost_up(self, tmp_path):
        ledger = make(tmp_path, cap_usd=1.0)
        reservation = ledger.reserve(0.5)
        ledger.settle(reservation, Decimal("0.0000000001"))
        conn = raw(ledger)
        try:
            assert conn.execute("SELECT settled FROM ledger").fetchone()[0] == 1
        finally:
            conn.close()

    def test_a_cost_above_the_reservation_records_overbilled_and_raises(self, tmp_path):
        """The excess is kept visible as leaked money rather than silently
        absorbed, and the settle raises because the cap arithmetic that admitted
        the row is now known to have understated real spend. (SL-2 owns the halt
        this also sets.)"""
        ledger = make(tmp_path, cap_usd=10.0, halt_on_overbilled=False)
        reservation = ledger.reserve(0.5)
        with pytest.raises(sl.SpendCapExceeded) as exc:
            ledger.settle(reservation, 0.9)
        assert exc.value.budget == 0.5
        assert exc.value.measured == 0.9
        conn = raw(ledger)
        try:
            row = conn.execute("SELECT state, reserved, settled FROM ledger").fetchone()
        finally:
            conn.close()
        assert row["state"] == "overbilled"
        # The row is COMMITTED despite the raise: it is the record of real money.
        status = ledger.status()
        assert status.settled_usd == 0.5  # the reserved part
        assert status.leaked_usd == pytest.approx(0.4)  # the excess
        assert status.outstanding_usd == pytest.approx(0.9)  # counted once, at cost

    def test_a_repeated_settle_changes_nothing(self, tmp_path):
        """Compare-and-set on (id, generation, state='open'): a retried settle
        cannot double-charge.

        Revert shown red: dropped `AND state = 'open'` from the settle UPDATE ->
        this test failed with `DID NOT RAISE StaleReservationError`: the second
        settle matched the already-resolved row and overwrote its cost, charging
        one attempt twice. Restored.
        """
        ledger = make(tmp_path, cap_usd=1.0)
        reservation = ledger.reserve(0.5)
        ledger.settle(reservation, 0.2)
        before = ledger.status()
        with pytest.raises(sl.StaleReservationError):
            ledger.settle(reservation, 0.3)
        after = ledger.status()
        assert after.settled_usd == before.settled_usd == 0.2
        assert after.requests == before.requests == 1

    def test_settle_against_an_unknown_id_raises_stale(self, tmp_path):
        ledger = make(tmp_path)
        phantom = sl.Reservation(
            id="never-admitted",
            generation=0,
            amount_usd=0.1,
            amount_nano=100_000_000,
            scope="",
            identifier="",
            created_at="2026-01-01T00:00:00+00:00",
            lease_expires_at=None,
        )
        with pytest.raises(sl.StaleReservationError):
            ledger.settle(phantom, 0.1)

    def test_a_stale_generation_loses_the_compare_and_set(self, tmp_path):
        """`generation` is the other half of the compare-and-set, and is what
        lets a later unit's sweep invalidate a reservation it reclaimed.

        Revert shown red: neutered the generation guard in the settle UPDATE to
        `AND ? IS NOT NULL` (keeping the placeholder, so the parameter count
        still matches and the test fails on behaviour rather than on a
        `ProgrammingError`) -> this test failed with `DID NOT RAISE
        StaleReservationError`. Restored.
        """
        ledger = make(tmp_path, cap_usd=1.0)
        reservation = ledger.reserve(0.5)
        bumped = sl.Reservation(
            id=reservation.id,
            generation=reservation.generation + 1,
            amount_usd=reservation.amount_usd,
            amount_nano=reservation.amount_nano,
            scope=reservation.scope,
            identifier=reservation.identifier,
            created_at=reservation.created_at,
            lease_expires_at=reservation.lease_expires_at,
        )
        with pytest.raises(sl.StaleReservationError):
            ledger.settle(bumped, 0.1)

    def test_settle_refuses_a_negative_cost(self, tmp_path):
        ledger = make(tmp_path)
        reservation = ledger.reserve(0.1)
        with pytest.raises(ValueError):
            ledger.settle(reservation, -0.1)

    def test_a_losing_settle_writes_no_leak_row_in_this_unit(self, tmp_path):
        """SL-1 keeps `leaks` EMPTY: until reclaim exists, the only way to lose
        the compare-and-set is a repeated settle, which has nothing new to
        record. SL-3 adds the write for the late-settle-against-reclaimed case.
        """
        ledger = make(tmp_path, cap_usd=1.0)
        reservation = ledger.reserve(0.5)
        ledger.settle(reservation, 0.2)
        with pytest.raises(sl.StaleReservationError):
            ledger.settle(reservation, 0.4)
        conn = raw(ledger)
        try:
            assert conn.execute("SELECT COUNT(*) FROM leaks").fetchone()[0] == 0
        finally:
            conn.close()


class TestRelease:
    def test_release_frees_the_headroom(self, tmp_path):
        """`release` is public API and is tested directly here. `call_llm` never
        calls it (SL-5 pins that): an attempt whose cost is merely unreadable
        goes through `settle(None)`, which HOLDS."""
        ledger = make(tmp_path, cap_usd=1.0)
        reservation = ledger.reserve(1.0)
        with pytest.raises(sl.SpendCapExceeded):
            ledger.reserve(0.5)
        ledger.release(reservation)
        status = ledger.status()
        assert status.outstanding_usd == 0.0
        assert status.remaining_usd == 1.0
        assert status.reservations_open == 0
        ledger.reserve(1.0)  # the whole cap is available again

    def test_release_resolves_the_row_into_one_of_the_five_states(self, tmp_path):
        ledger = make(tmp_path, cap_usd=1.0)
        ledger.release(ledger.reserve(0.5))
        conn = raw(ledger)
        try:
            row = conn.execute("SELECT state, settled FROM ledger").fetchone()
        finally:
            conn.close()
        assert row["state"] in sl.LEDGER_STATES
        assert row["state"] != "open"
        assert row["settled"] == 0

    def test_release_twice_raises_stale(self, tmp_path):
        ledger = make(tmp_path, cap_usd=1.0)
        reservation = ledger.reserve(0.5)
        ledger.release(reservation)
        with pytest.raises(sl.StaleReservationError):
            ledger.release(reservation)

    def test_release_after_settle_raises_stale(self, tmp_path):
        ledger = make(tmp_path, cap_usd=1.0)
        reservation = ledger.reserve(0.5)
        ledger.settle(reservation, 0.2)
        with pytest.raises(sl.StaleReservationError):
            ledger.release(reservation)
        assert ledger.status().settled_usd == 0.2


# ---------------------------------------------------------------------------
# The partition -- assertions (a) to (e), with `leaks` EMPTY
# ---------------------------------------------------------------------------


@pytest.fixture
def mixed(tmp_path):
    """A ledger holding a row in every one of the five states, `leaks` empty.

    `reclaimed` is injected with raw SQL because reclaim behaviour is SL-3's;
    the partition must still account for it, since it is one of the five states
    SL-1's schema declares.
    """
    ledger = make(tmp_path, cap_usd=100.0, halt_on_overbilled=False, name="mixed.sqlite")
    ledger.reserve(1.0, identifier="still-open")  # open:       reserved 1.0
    ledger.settle(ledger.reserve(2.0), None)  # unknown:    reserved 2.0
    ledger.settle(ledger.reserve(3.0), 1.5)  # settled:    settled  1.5
    over = ledger.reserve(4.0)
    with pytest.raises(sl.SpendCapExceeded):
        ledger.settle(over, 5.0)  # overbilled: reserved 4.0, settled 5.0
    insert_row(ledger, id="swept", state="reclaimed", reserved=9, settled=0)
    return ledger


class TestPartition:
    def test_leaks_is_empty_in_this_unit(self, mixed):
        conn = raw(mixed)
        try:
            assert conn.execute("SELECT COUNT(*) FROM leaks").fetchone()[0] == 0
        finally:
            conn.close()

    def test_status_matches_an_independently_computed_partition(self, mixed):
        """The four terms, cross-checked against SQL written separately from the
        production queries.

        Revert shown red: dropped the `+ SUM(reserved) over overbilled` half of
        the production SETTLED term -> this test failed with `assert 1.5 == 5.5`
        on `settled_usd`. Restored.
        """
        expect = independent_partition(mixed)
        status = mixed.status()
        assert status.settled_usd == sl.nano_to_usd(expect["settled"]) == 5.5
        assert status.reserved_usd == sl.nano_to_usd(expect["reserved"]) == 1.0
        assert status.unknown_usd == sl.nano_to_usd(expect["unknown"]) == 2.0
        assert status.leaked_usd == sl.nano_to_usd(expect["leaked"]) == 1.0
        assert status.outstanding_usd == sl.nano_to_usd(expect["outstanding"]) == 9.5

    def test_an_overbilled_row_is_counted_once_at_its_settled_cost(self, mixed):
        """`reserved + (settled - reserved) == settled`, exactly once."""
        status = mixed.status()
        # settled 1.5 (the settled row) + 4.0 (the overbilled row's reserved)
        # leaked  1.0 (the overbilled row's excess)
        assert status.settled_usd + status.leaked_usd == pytest.approx(1.5 + 5.0)

    def test_a_reclaimed_row_contributes_zero_to_all_four_terms(self, mixed):
        """Its headroom was freed by construction; a cost arriving for it later
        is a `leaks` row, counted there.

        Revert shown red: widened the production RESERVED term to
        `state IN ('open', 'reclaimed')` -> this test failed with
        `assert 1.000000016 == 1.000000009` on `reserved_usd`, the two injected
        reclaimed rows (9 and 7 nano-USD) having leaked into a term they must
        contribute 0 to. Restored.
        """
        before = mixed.status()
        insert_row(mixed, id="swept-2", state="reclaimed", reserved=7, settled=0)
        after = mixed.status()
        for field in ("settled_usd", "reserved_usd", "unknown_usd", "leaked_usd"):
            assert getattr(after, field) == getattr(before, field)
        assert after.outstanding_usd == before.outstanding_usd
        # Disclosure only, outside the four terms.
        assert after.reclaimed_usd > before.reclaimed_usd

    def test_remaining_is_cap_minus_outstanding_and_is_not_clamped(self, tmp_path):
        """A negative `remaining_usd` is reported as such: clamping it here
        would hide exactly the overshoot an operator needs to see.

        Revert shown red: clamped the production value with
        `max(0, cap - outstanding)` -> this test failed with
        `assert 0.0 == -0.5`, hiding a $0.50 overshoot behind a reassuring zero.
        Restored.
        """
        ledger = make(tmp_path, cap_usd=1.0, halt_on_overbilled=False)
        reservation = ledger.reserve(1.0)
        with pytest.raises(sl.SpendCapExceeded):
            ledger.settle(reservation, 1.5)
        status = ledger.status()
        assert status.outstanding_usd == 1.5
        assert status.remaining_usd == -0.5

    # -- (a) ----------------------------------------------------------------

    def test_a_outstanding_never_exceeds_the_cap(self, tmp_path):
        """(a) `OUTSTANDING <= cap` at every snapshot, outside a declared
        reclaim window.

        Revert shown red: disabled the admission check in `reserve` -> this test
        failed on the first over-cap grant with `assert 1.2 <= 1.0` on
        `outstanding_usd`, the fourth $0.30 reservation having been admitted
        against a $1.00 cap. Restored.
        """
        ledger = make(tmp_path, cap_usd=1.0)
        granted = []
        refused = 0
        for _ in range(12):
            try:
                granted.append(ledger.reserve(0.3))
            except sl.SpendCapExceeded:
                refused += 1
            assert ledger.status().outstanding_usd <= ledger.cap_usd
        # Three grants of 0.3 fit under 1.0; a fourth does not.
        assert len(granted) == 3 and refused == 9
        for reservation in granted:
            ledger.settle(reservation, 0.1)
            assert ledger.status().outstanding_usd <= ledger.cap_usd

    # -- (b) ----------------------------------------------------------------

    def test_b_the_five_state_counts_sum_to_the_ledger_row_count(self, mixed):
        """(b) over `ledger` ALONE the five per-state counts sum to `COUNT(*)`,
        so no row is in two states and none in none. `leaks` is a different
        table, so the count is not inflated by one.

        The rows are produced by every state-writing path there is, so a path
        that invented a sixth state would be caught here rather than only by the
        structural clause.

        Revert shown red: changed `release` to write `state = 'released'` -> this
        test failed with `assert 5 == 6`, the six rows no longer accounted for
        by the five declared states. Restored.
        """
        mixed.release(mixed.reserve(0.5))  # the one remaining state-writing path
        conn = raw(mixed)
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
        assert total == 6
        assert sum(per_state.values()) == total
        assert all(count >= 1 for count in per_state.values())  # none in no state
        assert leak_count == 0  # the `ledger` count is not inflated by a leak

    # -- (c) ----------------------------------------------------------------

    def test_c_the_three_counts_are_over_ledger_only(self, mixed):
        """(c) `reservations_open == count(state IN ('open','unknown'))`,
        `requests == COUNT(*)`, `calls_settled == count(state='settled')`.

        Revert shown red: changed the production `reservations_open` expression
        to count only `state = 'open'` -> this test failed with `assert 1 == 2`:
        a held `unknown` reservation, still consuming the cap, stopped being
        reported as outstanding at all. Restored.
        """
        conn = raw(mixed)
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
        status = mixed.status()
        assert status.reservations_open == expect_open == 2
        assert status.requests == expect_requests == 5
        assert status.calls_settled == expect_settled == 1

    def test_c_requests_and_calls_settled_differ_by_the_unresolved_rows(self, mixed):
        """`requests` counts every reservation ever admitted, one per attempt;
        `calls_settled` only the subset that resolved to a known cost. They
        differ by the `open`, `unknown`, `overbilled` and `reclaimed` rows, and
        neither counts `leaks`."""
        status = mixed.status()
        assert status.requests - status.calls_settled == 4

    # -- (d) ----------------------------------------------------------------

    def test_d_leaked_is_the_overbilled_excess_plus_every_leak_row(self, mixed):
        """(d) first half, with `leaks` empty: LEAKED is the overbilled excess.

        Revert shown red: dropped the `SUM(reported_cost) FROM leaks` half of
        the production LEAKED term -> this test failed with `assert 1.0 == 4.0`
        on the injected leak row below. The empty-table half alone could NOT
        show it red, which is why the injection is here rather than left to
        SL-3.
        """
        assert mixed.status().leaked_usd == pytest.approx(1.0)
        # Inject one leak row against the reclaimed row, so the second half of
        # the term has something to sum. SL-3 owns the WRITE path that produces
        # these rows in production.
        insert_leak(mixed, ledger_id="swept", reported_cost=3_000_000_000)
        assert mixed.status().leaked_usd == pytest.approx(4.0)
        assert mixed.status().outstanding_usd == pytest.approx(12.5)

    def test_d_every_leak_row_must_name_a_reclaimed_ledger_row(self, mixed):
        """(d) second half: the referential rule. With `leaks` empty the
        quantifier is vacuous, so the teeth are in the two structural clauses
        `ledger_id_unknown` and `ledger_row_not_reclaimed`, which this test
        drives directly rather than asserting over an empty table.
        """
        insert_leak(mixed, ledger_id="swept", reported_cost=5)  # well formed
        conn = raw(mixed)
        try:
            dangling = conn.execute(
                "SELECT COUNT(*) FROM leaks WHERE ledger_id NOT IN "
                "(SELECT id FROM ledger WHERE state = 'reclaimed')"
            ).fetchone()[0]
        finally:
            conn.close()
        assert dangling == 0
        mixed.status()  # validates, so the well-formed leak passes both clauses

        insert_leak(mixed, ledger_id="nobody-at-all", reported_cost=5)
        with pytest.raises(sl.LedgerStateInvalid) as exc:
            mixed.status()
        assert "ledger_id_unknown" in str(exc.value)

    # -- (e) ----------------------------------------------------------------

    def test_e_the_structural_query_runs_in_every_transaction(self, mixed):
        """(e) the structural query of section 5, over BOTH tables, returns 0
        rows -- and it runs inside every transaction, not only at open.

        Revert shown red: removed the `self._validate_structure(conn)` call from
        `_writer` -> this test failed with `DID NOT RAISE LedgerStateInvalid`,
        the `status()` leg (which validates through `_reader`) having passed
        first and the `reserve` leg then admitting against a malformed row set.
        That the two legs fail independently is why both are asserted here.
        Restored.
        """
        mixed.status()  # clean: 0 rows from every clause
        mixed.reserve(0.1)
        insert_row(mixed, id="malformed", state="open", settled=1)
        with pytest.raises(sl.LedgerStateInvalid):
            mixed.status()
        with pytest.raises(sl.LedgerStateInvalid):
            mixed.reserve(0.1)
        with pytest.raises(sl.LedgerStateInvalid):
            mixed.release(
                sl.Reservation(
                    id="malformed",
                    generation=0,
                    amount_usd=0.0,
                    amount_nano=1,
                    scope="",
                    identifier="",
                    created_at="2026-01-01T00:00:00+00:00",
                    lease_expires_at=None,
                )
            )

    def test_a_null_generation_cannot_be_written_at_all(self, mixed):
        """`generation` is `NOT NULL` in the column definition, so the matching
        structural clause CANNOT be shown red: the write is refused before any
        transaction could run the query.

        Stated plainly rather than wrapped in a test that looks like coverage --
        the carrier of this property is the column constraint, demonstrated
        here, and the clause is defence against a file written by some other
        tool. Revert shown red: dropped `NOT NULL` from the `generation` column
        -> this test failed with `DID NOT RAISE <class 'sqlite3.IntegrityError'>`.
        Restored, because a refused write beats a refused transaction.
        """
        with pytest.raises(sqlite3.IntegrityError):
            insert_row(mixed, id="nogen", generation=None)

    # The identity `SETTLED + RESERVED + UNKNOWN + LEAKED + REMAINING == cap` is
    # deliberately NOT asserted anywhere in this file: it holds by construction
    # whenever remaining is derived as `cap - OUTSTANDING`, so a test of it would
    # be green regardless of every term above (docs/reference/vacuous-checks.md,
    # shape 1). Assertions (a) to (e) are the non-vacuous ones.


# ---------------------------------------------------------------------------
# Status view
# ---------------------------------------------------------------------------


class TestStatus:
    def test_an_empty_ledger_reports_the_whole_cap_remaining(self, tmp_path):
        status = make(tmp_path, cap_usd=2.0, run_id="r9").status()
        assert status.run_id == "r9"
        assert status.cap_usd == 2.0
        assert status.outstanding_usd == 0.0
        assert status.remaining_usd == 2.0
        assert (status.requests, status.calls_settled, status.reservations_open) == (0, 0, 0)
        assert status.halted is False
        assert (status.halt_reason, status.halt_detail) == ("", "")
        assert status.as_of

    def test_status_writes_nothing(self, tmp_path):
        """Read-only by contract: one plain deferred `BEGIN`, no write
        statement inside."""
        ledger = make(tmp_path, cap_usd=1.0)
        ledger.settle(ledger.reserve(0.5), 0.25)
        conn = raw(ledger)
        try:
            before = conn.execute(
                "SELECT state, settled FROM ledger ORDER BY id"
            ).fetchall()
            before_history = conn.execute("SELECT COUNT(*) FROM halt_history").fetchone()[0]
        finally:
            conn.close()
        for _ in range(3):
            ledger.status()
        conn = raw(ledger)
        try:
            after = conn.execute("SELECT state, settled FROM ledger ORDER BY id").fetchall()
            after_history = conn.execute("SELECT COUNT(*) FROM halt_history").fetchone()[0]
        finally:
            conn.close()
        assert [tuple(r) for r in after] == [tuple(r) for r in before]
        assert after_history == before_history

    def test_status_declares_every_field_the_design_names(self):
        fields = set(SpendStatusFields := set(sl.SpendStatus.__dataclass_fields__))
        assert fields == {
            "run_id", "cap_usd", "settled_usd", "reserved_usd", "unknown_usd", "leaked_usd",
            "reclaimed_usd", "written_off_usd", "outstanding_usd", "remaining_usd",
            "reservations_open", "calls_settled", "requests", "halted", "halt_reason",
            "halt_detail", "as_of",
        }
        assert SpendStatusFields  # the superset a prior-art adapter projects from

    def test_reservation_declares_every_field_the_design_names(self):
        assert set(sl.Reservation.__dataclass_fields__) == {
            "id", "generation", "amount_usd", "amount_nano", "scope", "identifier",
            "created_at", "lease_expires_at",
        }

    def test_reservation_is_frozen(self, tmp_path):
        reservation = make(tmp_path).reserve(0.1)
        with pytest.raises(Exception):
            reservation.amount_nano = 0


# ---------------------------------------------------------------------------
# The duplicated network-path probe
# ---------------------------------------------------------------------------


class TestNetworkPathParity:
    def test_the_duplicate_matches_the_execution_store_original(self, tmp_path):
        """Design section 4: `llm/` may not import `execution/`, so the probe is
        duplicated with a pointer comment and this parity test. WAL on a network
        share is unsafe, so a drift between the copies would silently stop the
        warning on one path.

        Revert shown red: dropped the POSIX `//` form from the duplicate's UNC
        branch -> this test failed with
        `AssertionError: //server/share/ledger.sqlite`, the case label it
        reports on mismatch. Restored.
        """
        sys.path.insert(
            0, str(Path(__file__).resolve().parents[2] / "plugins" / "content-pipeline-kit" / "lib")
        )
        from content_pipeline.execution.store import looks_like_network_path as original

        cases = [
            "//server/share/ledger.sqlite",
            "\\\\server\\share\\ledger.sqlite",
            "/tmp/ledger.sqlite",
            "relative/ledger.sqlite",
            str(tmp_path / "ledger.sqlite"),
            "",
        ]
        for case in cases:
            assert sl.looks_like_network_path(case) == original(case), case
            assert sl.looks_like_network_path(Path(case) if case else Path(".")) == original(
                Path(case) if case else Path(".")
            ), case

    def test_a_network_path_warns_on_create(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sl, "looks_like_network_path", lambda _p: True)
        with pytest.warns(RuntimeWarning, match="network path"):
            sl.create_ledger(tmp_path / "net.sqlite", cap_usd=1.0, run_id="r")

    def test_the_warning_is_overridable(self, tmp_path, monkeypatch):
        import warnings as _warnings

        monkeypatch.setattr(sl, "looks_like_network_path", lambda _p: True)
        with _warnings.catch_warnings():
            _warnings.simplefilter("error")
            sl.create_ledger(
                tmp_path / "quiet.sqlite",
                cap_usd=1.0,
                run_id="r",
                warn_on_network_path=False,
            )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


class TestPublicSurface:
    def test_every_advertised_name_exists(self):
        for name in sl.__all__:
            assert hasattr(sl, name), name

    def test_the_later_units_surfaces_are_not_half_shipped(self):
        """SL-2 owns `halt` / `resume` / `check_halted`, SL-3 owns `renew` /
        `reclaim_orphans`. SL-1 ships their SCHEMA, not their behaviour, so a
        caller gets an AttributeError rather than a method that silently does
        nothing."""
        for name in ("halt", "resume", "check_halted", "renew", "reclaim_orphans"):
            assert not hasattr(sl.SpendLedger, name), (
                f"SpendLedger.{name} is a later unit's; SL-1 must not stub it"
            )
