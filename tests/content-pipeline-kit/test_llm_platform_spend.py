"""SL-5: `call_llm` reserves and settles the spend ledger PER PROVIDER ATTEMPT.

Design: `dev/tasks/loc-pipeline-consumer-needs/design-cross-process-spend-ledger.md`
section 5 (the reserve/settle lifecycle and its exit-path table), section 6
(what the ledger cannot enforce) and section 11.2 (breaking sequences B1, B3,
B4).

The breach this unit closes is B1: `call_llm` bills up to `retries + 1` provider
attempts inside ONE invocation, so a single reservation per invocation lets a
$1.00 cap bill $3.60. `test_b1_*` below reproduces it, and its docstring names
the revert that turns it red.

Every load-bearing assertion here was shown RED by reverting the production line
it protects (docs/reference/vacuous-checks.md); each revert is named in the
test's docstring. Where the property is an ABSENCE -- `call_llm` never calling
`release`, never reading the environment -- the revert INSERTS the forbidden
code, because there is no line to take away.

Scope boundaries: the ledger's own behaviour (admission arithmetic, the five
states, the partition, leases, halt mechanics, cross-process acceptance) is
SL-1..SL-4's four files. This file only pins what `platform.call_llm` does WITH
a ledger, and reads row states directly rather than re-deriving ledger
invariants.

No output contract is declared anywhere here, so nothing in this file needs
llm-scripting-kit: `call_llm` catches `StructuralOutputError` and
`submit_validated` re-enters on it whether or not a contract was declared.
"""

import ast
import sqlite3
from pathlib import Path

import pytest

from content_pipeline.llm import platform
from content_pipeline.llm import spend_ledger as sl
from content_pipeline.llm.backends import MockBackend
from content_pipeline.llm.platform import (
    BackendOptions,
    BudgetExceededError,
    CostBudget,
    EmptyCompletionError,
    LLMResponse,
    PipelineHaltError,
    StructuralOutputError,
    call_llm,
    submit_validated,
)

MODEL = "test/model"
OTHER_MODEL = "unpriced/model"
PRICING = {
    MODEL: {"input": 0.30, "cache_hit": 0.10, "output": 1.20},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_ledger(tmp_path, *, cap_usd=100.0, name="spend.sqlite", **kwargs):
    return sl.create_ledger(
        tmp_path / name, cap_usd=cap_usd, run_id="sl5-run", **kwargs
    )


def rows(ledger):
    """Every `ledger` row as a list of dicts, oldest first."""
    conn = sqlite3.connect(str(ledger.path))
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT id, state, reserved, settled, created_at FROM ledger "
                "ORDER BY created_at, rowid"
            )
        ]
    finally:
        conn.close()


def states(ledger):
    return [r["state"] for r in rows(ledger)]


class Recorder:
    """A `SpendGuard` that forwards to a real ledger and records every call.

    `release` RAISES rather than forwarding: `call_llm` must never reach it (no
    code sits between its `reserve` and `backend.complete`, so no exit path can
    know an attempt billed nothing). A proxy that merely counted the call would
    let the defect through on every path whose assertions do not mention
    release; raising makes any such call fail the test that provoked it.
    """

    def __init__(self, ledger):
        self.ledger = ledger
        self.reserve_calls = []
        self.settle_calls = []
        self.check_halted_calls = 0

    def reserve(self, amount_usd, *, scope="", identifier="", model="", ttl_s=None):
        self.reserve_calls.append(
            {
                "amount_usd": amount_usd,
                "scope": scope,
                "identifier": identifier,
                "model": model,
                "ttl_s": ttl_s,
            }
        )
        return self.ledger.reserve(
            amount_usd, scope=scope, identifier=identifier, model=model, ttl_s=ttl_s
        )

    def settle(self, reservation, cost_usd):
        self.settle_calls.append((reservation.id, cost_usd))
        return self.ledger.settle(reservation, cost_usd)

    def release(self, reservation):  # pragma: no cover -- must never run
        raise AssertionError("call_llm must never call SpendGuard.release")

    def check_halted(self, *, identifier=""):  # pragma: no cover -- must never run
        self.check_halted_calls += 1
        raise AssertionError("call_llm must never call SpendGuard.check_halted")


def priced(text, *, cost, model=MODEL, **extra):
    """A response carrying an AUTHORITATIVE cost, so `response_cost` is exact.

    Used instead of token counts wherever a test needs an exact dollar figure:
    `response_cost` prefers a valid reported cost over the pricing estimate, so
    the settled amount is the literal number written here.
    """
    return LLMResponse(
        text=text,
        model=model,
        reported_cost_usd=cost,
        reported_cost_source="provider",
        **extra,
    )


class Boom(Exception):
    """A transport failure. With `output_tokens` set it is priceable."""

    def __init__(self, message="boom", **attrs):
        super().__init__(message)
        for key, value in attrs.items():
            setattr(self, key, value)


# ---------------------------------------------------------------------------
# One reserve and one settle per ATTEMPT
# ---------------------------------------------------------------------------


def test_each_provider_attempt_gets_its_own_row(tmp_path):
    """Three attempts inside ONE call_llm produce THREE ledger rows.

    Asserted by ROW COUNT, not by call order: the property is that the ledger
    saw one reservation per paid hit, which a call-order assertion would not
    distinguish from one reservation reused three times.

    REVERT: move `reserve` out of the loop to a single pre-loop call. Observed:
    `requests` is 1, not 3, and this test fails on the row count.
    """
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)
    backend = MockBackend(
        responses=[priced("  ", cost=0.01), priced("\t", cost=0.02), priced("ok", cost=0.03)]
    )

    out = call_llm(
        backend,
        "sys",
        "user",
        model=MODEL,
        pricing=PRICING,
        retries=2,
        spend=guard,
        spend_reserve_usd=0.50,
    )

    assert out.text == "ok"
    assert len(backend.calls) == 3
    assert len(guard.reserve_calls) == 3
    assert len(guard.settle_calls) == 3
    status = ledger.status()
    assert status.requests == 3
    assert status.calls_settled == 3
    assert status.reservations_open == 0
    assert states(ledger) == ["settled", "settled", "settled"]
    assert round(status.settled_usd, 6) == 0.06


def test_reserve_runs_before_the_provider_is_asked(tmp_path):
    """A reserve that REFUSES means the provider is never called at all.

    REVERT: move the reserve below `backend.complete`. Observed: the backend
    records one call before the refusal and `len(backend.calls) == 0` fails.
    """
    ledger = make_ledger(tmp_path, cap_usd=0.10)
    guard = Recorder(ledger)
    backend = MockBackend(responses=[priced("ok", cost=0.01)])

    with pytest.raises(sl.SpendCapExceeded):
        call_llm(
            backend,
            "sys",
            "user",
            model=MODEL,
            pricing=PRICING,
            spend=guard,
            spend_reserve_usd=1.00,
        )

    assert backend.calls == []
    assert rows(ledger) == []


def test_a_raising_reserve_admits_nothing_to_settle(tmp_path):
    """A `reserve` that refuses leaves NO settle behind.

    The runtime half of the property. It is deliberately paired with the source
    guard below, because this assertion alone is VACUOUS: moving the `reserve`
    inside the try leaves it GREEN, since `reservation = None` above the try
    means the finally still settles nothing. That was observed, not predicted --
    the first draft of this file claimed an `UnboundLocalError` would surface.
    See `test_reserve_is_lexically_outside_the_settling_try`.
    """
    ledger = make_ledger(tmp_path, cap_usd=0.10)
    guard = Recorder(ledger)
    backend = MockBackend(responses=[priced("ok", cost=0.01)])

    with pytest.raises(sl.SpendCapExceeded):
        call_llm(
            backend,
            "sys",
            "user",
            model=MODEL,
            pricing=PRICING,
            spend=guard,
            spend_reserve_usd=1.00,
        )

    assert guard.settle_calls == []
    assert rows(ledger) == []


def test_reserve_is_lexically_outside_the_settling_try():
    """The structural contract: `reserve` is not inside the try that settles.

    NO RUNTIME TEST CAN CARRY THIS. Moving the reserve inside the try changes
    nothing observable as long as `reservation` is pre-initialized to None: the
    refusal still propagates, the finally still settles nothing, and every
    runtime assertion stays green. What the placement actually buys is that the
    arrangement cannot DRIFT into the shape where it matters -- a `reservation
    = spend.reserve(...)` inside the try, whose finally then raises
    `UnboundLocalError` over the caller's real exception.

    REVERT: move the `spend.reserve(...)` call inside the try. Observed: this
    test fails naming the settling try, while the runtime test above stays
    green -- which is why both exist.
    """
    fn = _function("call_llm")
    settling_tries = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Try)
        and any("settle" in _attr_calls_on(stmt, "spend") for stmt in node.finalbody)
    ]
    assert len(settling_tries) == 1, "expected exactly one settling try/finally"
    try_node = settling_tries[0]

    guarded = []
    for region in (try_node.body, try_node.handlers, try_node.orelse):
        for stmt in region:
            guarded.extend(_attr_calls_on(stmt, "spend"))
    assert "reserve" not in guarded, (
        "spend.reserve is inside the try whose finally settles: a reserve that "
        "raises must have nothing to settle"
    )
    # ... and it is somewhere in the attempt loop, so this guard cannot pass by
    # the reserve having vanished.
    assert "reserve" in _attr_calls_on(fn, "spend")


# ---------------------------------------------------------------------------
# The exit paths of one attempt iteration, derived from platform.py
# ---------------------------------------------------------------------------


def test_exit1_pipeline_halt_from_the_backend_holds_as_unknown(tmp_path):
    """Exit 1: `PipelineHaltError` out of `backend.complete`.

    No `_charge_reporting` runs on that handler, so no cost is known and the row
    HOLDS as `unknown` at its reserved amount -- it is not released, because the
    attempt may well have billed.

    REVERT: delete the `finally` (settle only on the success path). Observed:
    the row stays `open` and `states(...) == ["unknown"]` fails with `["open"]`.
    """
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)
    backend = MockBackend(responses=[PipelineHaltError("rate_limit", "slow down")])

    with pytest.raises(PipelineHaltError):
        call_llm(
            backend,
            "sys",
            "user",
            model=MODEL,
            pricing=PRICING,
            retries=3,
            spend=guard,
            spend_reserve_usd=0.50,
        )

    assert states(ledger) == ["unknown"]
    assert guard.settle_calls == [(rows(ledger)[0]["id"], None)]
    status = ledger.status()
    assert round(status.unknown_usd, 6) == 0.50
    assert round(status.outstanding_usd, 6) == 0.50


def test_exit2_structural_output_error_is_charged_then_settled(tmp_path):
    """Exit 2: `StructuralOutputError` -- a billed call whose answer broke its
    contract. `_charge_reporting` prices it, then it is re-raised, so the row
    settles at the priced cost rather than holding as unknown.

    REVERT: remove the `record=_record_charged` argument from
    `_charge_reporting`'s `_charge_exception` call. Observed: the row is
    `unknown` and the state assertion fails -- the money the exception reported
    is no longer recorded against the cap.
    """
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)
    failed = LLMResponse(text="{bad", model=MODEL)
    exc = StructuralOutputError(failed, "schema violation")
    exc.output_tokens = 1_000_000  # 1M output tokens at 1.20/M == $1.20
    exc.input_tokens = 0
    backend = MockBackend(responses=[exc])

    with pytest.raises(StructuralOutputError):
        call_llm(
            backend,
            "sys",
            "user",
            model=MODEL,
            pricing=PRICING,
            retries=3,
            spend=guard,
            spend_reserve_usd=2.00,
        )

    assert states(ledger) == ["settled"]
    assert round(ledger.status().settled_usd, 6) == 1.20
    # Never retried here: one attempt, one row.
    assert len(backend.calls) == 1


def test_exit2_unpriceable_structural_error_holds_as_unknown(tmp_path):
    """Exit 2, the other half of 'the priced cost, else None'.

    PREDICTION CORRECTED. This case was first written with `output_tokens=0`,
    on the reading that an exception reporting no output tokens is unpriceable.
    It is not: `StructuralOutputError.__init__` MIRRORS the response's token
    counts and model onto itself, and `_charge_exception`'s guard is
    `output_tokens is None`, which 0 passes -- so that shape prices at $0.00
    and settles. The genuinely unpriceable shape is a response naming a model
    ABSENT from `pricing` (`exception_model not in pricing`), which is
    reachable for real: the exception names whatever model the provider
    actually served, which need not be in the caller's own table.

    REVERT: change the `finally`'s settle to `spend.release(reservation)` on a
    failure path. Observed: the row is `settled` at 0 and
    `unknown_usd == 0.50` fails with 0.0 -- which is exactly B1's second half,
    a cap reporting its full headroom after real money was spent.
    """
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)
    exc = StructuralOutputError(
        LLMResponse(text="{bad", model=OTHER_MODEL), "nope"
    )
    backend = MockBackend(responses=[exc])

    with pytest.raises(StructuralOutputError):
        call_llm(
            backend,
            "sys",
            "user",
            model=MODEL,
            pricing=PRICING,
            spend=guard,
            spend_reserve_usd=0.50,
        )

    assert states(ledger) == ["unknown"]
    assert round(ledger.status().unknown_usd, 6) == 0.50


def test_exit3_charge_reporting_raising_still_settles_the_priced_cost(tmp_path):
    """Exit 3: `_charge_reporting` ITSELF raises out of its own handler.

    `CostBudget.charge` raises `BudgetExceededError` from inside
    `_charge_exception`, so the attempt leaves through an exception the handler
    produced rather than the one it was pricing. The cost is still known --
    `record` runs BEFORE the charge -- so the row settles at it.

    REVERT: move the `record(cost)` call in `_charge_exception` to AFTER
    `cost_budget.charge(...)`. Observed: the charge raises first, `record` never
    runs, the row is `unknown`, and the `settled` assertion fails.
    """
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)
    exc = Boom(output_tokens=1_000_000, input_tokens=0)
    backend = MockBackend(responses=[exc])

    with pytest.raises(BudgetExceededError):
        call_llm(
            backend,
            "sys",
            "user",
            model=MODEL,
            pricing=PRICING,
            cost_budget=CostBudget(limit=0.01),
            retries=3,
            spend=guard,
            spend_reserve_usd=2.00,
        )

    assert states(ledger) == ["settled"]
    assert round(ledger.status().settled_usd, 6) == 1.20
    assert len(backend.calls) == 1  # the budget raise is not retried


def test_exit4a_classified_halt_leaves_one_row(tmp_path):
    """Exit 4a: `except BaseException` -> `classify_halt` non-None ->
    `PipelineHaltError` out of the loop. Unpriceable here, so `unknown`.

    REVERT: delete the `finally`. Observed: the row stays `open`.
    """
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)
    backend = MockBackend(responses=[Boom("halt:rate_limit", halt_kind="rate_limit")])

    with pytest.raises(PipelineHaltError):
        call_llm(
            backend,
            "sys",
            "user",
            model=MODEL,
            pricing=PRICING,
            retries=3,
            spend=guard,
            spend_reserve_usd=0.50,
        )

    assert states(ledger) == ["unknown"]


def test_exit4b_retry_settles_before_reserving_again(tmp_path):
    """Exit 4b: halt None and `attempt < retries` -> `continue`.

    The settle must happen on the way out of the iteration, BEFORE the next
    attempt reserves, or the two reservations stack against the cap. Shown with
    a cap that admits exactly ONE reservation at a time: the second attempt is
    admitted only because the first row no longer counts as `open`.

    Attempt 1's failure must be PRICEABLE for this to work: an unreadable
    cost settles as `unknown` and HOLDS its headroom, which is the design's
    rule and would refuse attempt 2 on this cap by itself. So the first entry
    reports 0 output tokens, which prices at $0.00 -- a real shape, since a
    transport can fail after reporting its usage.

    REVERT: delete the `finally`. Observed: attempt 1's row stays `open`,
    attempt 2's reserve raises SpendCapExceeded, and `len(backend.calls) == 2`
    fails with 1.
    """
    ledger = make_ledger(tmp_path, cap_usd=0.50)
    guard = Recorder(ledger)
    backend = MockBackend(
        responses=[
            Boom("transient", output_tokens=0, input_tokens=0),
            priced("ok", cost=0.0),
        ]
    )

    out = call_llm(
        backend,
        "sys",
        "user",
        model=MODEL,
        pricing=PRICING,
        retries=1,
        spend=guard,
        spend_reserve_usd=0.50,
    )

    assert out.text == "ok"
    assert len(backend.calls) == 2
    assert states(ledger) == ["settled", "settled"]


def test_exit4c_last_attempt_reraises_with_its_row_resolved(tmp_path):
    """Exit 4c: halt None on the LAST attempt -> the original error propagates.

    REVERT: delete the `finally`. Observed: both rows stay `open`.
    """
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)
    backend = MockBackend(responses=[Boom("t1"), Boom("t2")])

    with pytest.raises(Boom):
        call_llm(
            backend,
            "sys",
            "user",
            model=MODEL,
            pricing=PRICING,
            retries=1,
            spend=guard,
            spend_reserve_usd=0.50,
        )

    assert states(ledger) == ["unknown", "unknown"]
    assert ledger.status().reservations_open == 2  # open + unknown, per SL-1


def test_exit5a_unpriceable_empty_response_holds_as_unknown(tmp_path):
    """Exit 5, first cause: the empty-text branch's `response_cost` RAISES.

    The response names a model absent from `pricing`, so `estimate_cost` raises
    `KeyError` before any cost is known. `charged` is still None, so the row
    holds.

    REVERT: delete the `finally`. Observed: the row stays `open`.
    """
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)
    backend = MockBackend(responses=[{"text": "   ", "model": OTHER_MODEL}])

    with pytest.raises(KeyError):
        call_llm(
            backend,
            "sys",
            "user",
            model=MODEL,
            pricing=PRICING,
            retries=3,
            spend=guard,
            spend_reserve_usd=0.50,
        )

    assert states(ledger) == ["unknown"]
    assert guard.settle_calls == [(rows(ledger)[0]["id"], None)]


def test_exit5b_empty_response_whose_charge_raises_settles_its_cost(tmp_path):
    """Exit 5, second cause: the empty-text branch's `cost_budget.charge` raises.

    The cost WAS priced before the raise, so it is settled rather than held.

    REVERT: move `charged[0] = cost` below the `cost_budget.charge(...)` call in
    the empty-text branch. Observed: the charge raises first, the assignment
    never runs, the row is `unknown`, and the `settled` assertion fails.
    """
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)
    backend = MockBackend(responses=[priced("  ", cost=0.75)])

    with pytest.raises(BudgetExceededError):
        call_llm(
            backend,
            "sys",
            "user",
            model=MODEL,
            pricing=PRICING,
            cost_budget=CostBudget(limit=0.01),
            retries=3,
            spend=guard,
            spend_reserve_usd=1.00,
        )

    assert states(ledger) == ["settled"]
    assert round(ledger.status().settled_usd, 6) == 0.75


def test_exit6_empty_then_retry_settles_each_attempt_at_its_own_cost(tmp_path):
    """Exit 6: empty text charged, `attempt < retries` -> `continue`.

    REVERT: hoist the reserve out of the loop. Observed: one row holding one
    amount instead of two rows holding $0.10 and $0.20, so both the row count
    and `settled_usd` fail.
    """
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)
    backend = MockBackend(responses=[priced(" ", cost=0.10), priced("ok", cost=0.20)])

    call_llm(
        backend,
        "sys",
        "user",
        model=MODEL,
        pricing=PRICING,
        retries=1,
        spend=guard,
        spend_reserve_usd=0.50,
    )

    assert states(ledger) == ["settled", "settled"]
    assert [r["settled"] for r in rows(ledger)] == [100_000_000, 200_000_000]


def test_exit7_empty_exhausted_settles_its_last_attempt(tmp_path):
    """Exit 7: empty text, retries exhausted -> `EmptyCompletionError`.

    REVERT: delete the `finally`. Observed: both rows stay `open`.
    """
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)
    backend = MockBackend(responses=[priced(" ", cost=0.10), priced("\n", cost=0.20)])

    with pytest.raises(EmptyCompletionError):
        call_llm(
            backend,
            "sys",
            "user",
            model=MODEL,
            pricing=PRICING,
            retries=1,
            spend=guard,
            spend_reserve_usd=0.50,
        )

    assert states(ledger) == ["settled", "settled"]
    assert round(ledger.status().settled_usd, 6) == 0.30


def test_exit8_success_settles_the_response_cost(tmp_path):
    """Exit 8: text returned -> `break`, settled at `response_cost`.

    REVERT: delete the `charged[0] = response_cost(...)` line before the break.
    Observed: the success row is `unknown` at its reserved $0.50 instead of
    `settled` at $0.07, so a successful run reports 7x its real spend against
    the cap.
    """
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)
    backend = MockBackend(responses=[priced("hello", cost=0.07)])

    out = call_llm(
        backend,
        "sys",
        "user",
        model=MODEL,
        pricing=PRICING,
        spend=guard,
        spend_reserve_usd=0.50,
    )

    assert out.text == "hello"
    assert states(ledger) == ["settled"]
    assert round(ledger.status().settled_usd, 6) == 0.07


def test_exit8b_unpriceable_success_holds_and_still_raises(tmp_path):
    """Exit 8b -- an exit the design's table does NOT list, found in the code.

    The design placed the success path's pricing AFTER the loop, so a `KeyError`
    from it could not reach the settle. This implementation reads the cost
    BEFORE the break (the settle needs it), so an unpriceable success is an
    eleventh exit: the row holds as `unknown` and the same `KeyError` the
    post-loop call would have raised propagates.

    PREDICTION CORRECTED. This docstring first named the `charged[0] =
    response_cost(...)` line as the revert. That revert leaves this test
    GREEN, and was observed doing so: the post-loop pricing call raises the
    same KeyError one statement later, and the row is `unknown` either way.
    The line is therefore NOT what this exit pins.

    REVERT: delete the `finally`. Observed: the row stays `open`, so this test
    fails -- what it carries is that an unpriceable success resolves its row at
    all, which is the only thing about this exit that is not already true
    without the integration.
    """
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)
    backend = MockBackend(responses=[{"text": "hi", "model": OTHER_MODEL}])

    with pytest.raises(KeyError):
        call_llm(
            backend,
            "sys",
            "user",
            model=MODEL,
            pricing=PRICING,
            spend=guard,
            spend_reserve_usd=0.50,
        )

    assert states(ledger) == ["unknown"]


# ---------------------------------------------------------------------------
# B1 -- the breach that justified the design
# ---------------------------------------------------------------------------


def test_b1_four_whitespace_attempts_cannot_bill_past_a_one_dollar_cap(tmp_path):
    """B1: cap $1.00, `retries=3`, four whitespace responses at $0.90 each.

    Under a SINGLE pre-loop reservation this invocation bills $3.60 against a
    $1.00 cap and, with the deleted release-on-failure rule, then reports the
    full $1.00 still remaining. Per attempt, attempt 1 settles at $0.90 and
    attempt 2's reserve sees OUTSTANDING $0.90, so $0.90 + $0.90 > $1.00 and it
    raises before the provider is asked again.

    REVERT (the one that matters most in this unit): replace the per-attempt
    reserve with a single reserve before `for attempt in range(retries + 1)`
    and settle once after it. Observed: `EmptyCompletionError` instead of
    `SpendCapExceeded`, FOUR `backend.complete` calls instead of one, and
    `requests == 1` -- i.e. $3.60 billed under a $1.00 cap, which is the breach
    verbatim.
    """
    ledger = make_ledger(tmp_path, cap_usd=1.00)
    guard = Recorder(ledger)
    backend = MockBackend(responses=[priced("   ", cost=0.90)] * 4)

    with pytest.raises(sl.SpendCapExceeded) as caught:
        call_llm(
            backend,
            "sys",
            "user",
            model=MODEL,
            pricing=PRICING,
            retries=3,
            spend=guard,
            spend_reserve_usd=0.90,
        )

    # Exactly ONE paid attempt reached the provider.
    assert len(backend.calls) == 1
    # The refusal is attempt 2's reserve, and it measured the real outstanding.
    assert len(guard.reserve_calls) == 2
    assert round(caught.value.measured, 6) == 1.80
    assert round(caught.value.budget, 6) == 1.00

    status = ledger.status()
    assert status.requests == 1  # the refused reserve admitted nothing
    assert round(status.settled_usd, 6) == 0.90  # settled == what was charged
    assert round(status.outstanding_usd, 6) == 0.90
    assert round(status.remaining_usd, 6) == 0.10  # NOT the full cap
    assert states(ledger) == ["settled"]


# ---------------------------------------------------------------------------
# B3 -- a halt committed mid-invocation stops the next attempt
# ---------------------------------------------------------------------------


class HaltingBackend:
    """Commits a ledger halt from INSIDE the first `complete`, then succeeds.

    This is the real B3 shape: the halt lands while attempt 1 is in flight, as
    another process would commit it, rather than between two `call_llm` calls.
    """

    name = "halting"

    def __init__(self, ledger):
        self.ledger = ledger
        self.calls = 0

    def complete(self, system, user, *, model, options):
        self.calls += 1
        if self.calls == 1:
            self.ledger.halt("manual", "operator stopped the run")
        return priced("   ", cost=0.01)  # whitespace: call_llm will retry

    def classify_halt(self, exc):
        return None


def test_b3_a_halt_mid_invocation_stops_the_next_attempt(tmp_path):
    """B3: exactly ONE `backend.complete` runs after the halt commits.

    `reserve` reads the halt row inside its own IMMEDIATE transaction, and
    `reserve` is per attempt, so attempt 2 is refused. Without the per-attempt
    reserve, attempts 2-4 would hit the provider unexamined.

    REVERT: hoist the reserve out of the loop. Observed: four `complete` calls
    and `EmptyCompletionError` instead of one call and `SpendLedgerHalted` --
    three paid hits after the stop signal was already durable.
    """
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)
    backend = HaltingBackend(ledger)

    with pytest.raises(sl.SpendLedgerHalted):
        call_llm(
            backend,
            "sys",
            "user",
            model=MODEL,
            pricing=PRICING,
            retries=3,
            spend=guard,
            spend_reserve_usd=0.50,
        )

    assert backend.calls == 1
    assert len(guard.reserve_calls) == 2
    assert states(ledger) == ["settled"]  # attempt 1 still settled its real cost


def test_settle_never_refuses_while_halted(tmp_path):
    """The attempt in flight when a halt lands must still RECORD its spend.

    Money already spent is recorded whatever the halt says; only admission
    stops. Shown by B3's own run above: its one row is `settled`, not `open`.

    REVERT (run): delete the `finally`. Observed: the row stays `open` with a
    NULL `settled`, so the settled-amount assertion fails -- the attempt that
    was in flight when the halt landed records nothing.

    The complementary revert -- making `settle` itself refuse while halted --
    lives in `spend_ledger.settle`, which SL-2 owns and this unit does not edit,
    so it is NOT claimed as observed here. Its predicted effect, stated as a
    prediction: the settle raises out of the `finally` and replaces
    `SpendLedgerHalted`, so B3's `pytest.raises` fails with the wrong type.
    """
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)
    backend = HaltingBackend(ledger)

    with pytest.raises(sl.SpendLedgerHalted):
        call_llm(
            backend,
            "sys",
            "user",
            model=MODEL,
            pricing=PRICING,
            retries=3,
            spend=guard,
            spend_reserve_usd=0.50,
        )

    assert [r["settled"] for r in rows(ledger)] == [10_000_000]
    assert ledger.status().halted is True


# ---------------------------------------------------------------------------
# B4 -- phantom reservations under submit_validated
# ---------------------------------------------------------------------------


def test_b4_submit_validated_leaves_no_open_row(tmp_path):
    """B4: three contract-failing validation attempts leave THREE rows, none open.

    `submit_validated` forwards `**call_kwargs` verbatim, so `spend=` reaches
    `call_llm` with no signature change there; it catches
    `StructuralOutputError` and re-enters `call_llm`, once per validation
    attempt. Without the `finally`, each of those entries leaves a permanently
    `open` row that a NULL lease never sweeps -- cap consumed by reservations
    for calls that are long finished.

    The three rows settle at $0.00 rather than holding as `unknown`, because
    `StructuralOutputError` mirrors its response's token counts (0 here) and
    its model is priced -- see the exit-2 unpriceable test for the prediction
    that corrected. What B4 is about is unaffected: NO row is left `open`.

    REVERT: delete the `finally`. Observed: three rows, all three `open`,
    `reservations_open == 3`, and `outstanding_usd` $1.50 of pure phantom while
    the real spend lives only in `CostBudget`.
    """
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)

    def never_parses(text):
        raise ValueError("unparseable")

    exc = StructuralOutputError(LLMResponse(text="{bad", model=MODEL), "nope")
    backend = MockBackend(responses=[exc, exc, exc])

    result = submit_validated(
        backend=backend,
        system="sys",
        user="user",
        model=MODEL,
        parse_fn=never_parses,
        max_attempts=3,
        pricing=PRICING,
        spend=guard,
        spend_reserve_usd=0.50,
    )

    assert not result.accepted
    assert len(backend.calls) == 3
    assert len(rows(ledger)) == 3
    assert "open" not in states(ledger)
    assert ledger.status().reservations_open == 0
    assert states(ledger) == ["settled", "settled", "settled"]


def test_b4_spend_reaches_call_llm_through_call_kwargs(tmp_path):
    """The passthrough itself, with no contract in play: a plain
    `submit_validated` success produces exactly one row.

    REVERT: add `spend` as an explicit named parameter of `submit_validated`
    that is NOT forwarded. Observed: zero rows, and the row-count assertion
    fails -- the shape that would make every consumer's cap silently inert
    under the validate loop.
    """
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)
    backend = MockBackend(responses=[priced("fine", cost=0.04)])

    result = submit_validated(
        backend=backend,
        system="sys",
        user="user",
        model=MODEL,
        parse_fn=lambda text: text,
        max_attempts=3,
        pricing=PRICING,
        spend=guard,
        spend_reserve_usd=0.50,
    )

    assert result.accepted
    assert states(ledger) == ["settled"]
    assert round(ledger.status().settled_usd, 6) == 0.04


# ---------------------------------------------------------------------------
# The cache hit: no reserve, no row
# ---------------------------------------------------------------------------


def test_a_cache_hit_reserves_nothing_and_creates_no_row(tmp_path):
    """A hit returns BEFORE the attempt loop: no provider, no reserve, no row.

    Shown on a ledger whose cap ($0.01) is far too small to admit the $0.50
    reservation the live path would ask for, so the hit is served only because
    nothing was reserved at all. A test that merely counted rows would also
    pass if the reserve had been attempted and happened to fit.

    REVERT: move the cache lookup below the reserve. Observed: the lookup never
    runs, `reserve` raises `SpendCapExceeded`, and the call fails instead of
    returning the cached text.
    """
    cache_dir = tmp_path / "cache"
    ledger = make_ledger(tmp_path, cap_usd=0.01)
    guard = Recorder(ledger)

    warm = MockBackend(responses=[priced("cached answer", cost=0.02)])
    call_llm(warm, "sys", "user", model=MODEL, pricing=PRICING, cache_dir=cache_dir)

    cold = MockBackend(responses=[])  # any provider call raises "exhausted"
    out = call_llm(
        cold,
        "sys",
        "user",
        model=MODEL,
        pricing=PRICING,
        cache_dir=cache_dir,
        spend=guard,
        spend_reserve_usd=0.50,
    )

    assert out.from_cache is True
    assert out.text == "cached answer"
    assert cold.calls == []
    assert guard.reserve_calls == []
    assert rows(ledger) == []
    assert ledger.status().requests == 0


def test_response_cost_is_zero_for_a_cache_hit():
    """The premise behind the row above: a cached response costs 0.0 on this run.

    REVERT: delete the `if response.from_cache: return 0.0` branch in
    `response_cost`. Observed: the hit is priced from its reported cost ($0.02)
    instead of 0.0, so this assertion fails -- and a cached run would charge the
    cap for spend that happened on an earlier one.
    """
    hit = LLMResponse(
        text="x",
        model=MODEL,
        from_cache=True,
        reported_cost_usd=0.02,
        reported_cost_source="provider",
        output_tokens=1_000_000,
    )
    assert platform.response_cost(MODEL, hit, pricing=PRICING) == 0.0


# ---------------------------------------------------------------------------
# What call_llm must NEVER do
# ---------------------------------------------------------------------------


SOURCE = Path(platform.__file__).read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _function(name):
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in platform.py")


def _attr_calls_on(node, receiver):
    found = []
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and isinstance(sub.func.value, ast.Name)
            and sub.func.value.id == receiver
        ):
            found.append(sub.func.attr)
    return found


def test_call_llm_never_calls_release_or_check_halted():
    """An ABSENCE, so the counterfactual INSERTS the forbidden call.

    `release` would free a reservation whose attempt may have billed -- the
    deleted release-on-failure rule, and B1's second half. `check_halted` is a
    consumer's between-units probe; inside `call_llm` it would be a second,
    non-transactional halt read racing the one `reserve` already does.

    REVERT: add `spend.release(reservation)` to any exit inside `call_llm`.
    Observed: this test fails naming `release`. The runtime half is `Recorder`,
    whose `release` raises -- it is reached by every exit-path test above, so a
    release on any one of those paths fails that test too. Both carriers are
    needed: the source guard sees paths no test exercises, and the runtime proxy
    sees a release reached through a name this AST walk does not track.
    """
    called = _attr_calls_on(_function("call_llm"), "spend")
    assert "release" not in called
    assert "check_halted" not in called
    assert sorted(set(called)) == ["reserve", "settle"]


def test_call_llm_never_reads_the_ledger_environment():
    """`spend=None` means no ledger, and `spend_ledger_from_env` is not consulted.

    An implicit read would let a cap materialize mid-run out of an inherited
    variable (design section 8), and `platform` importing `spend_ledger` would
    invert the one-way import edge.

    Read over the AST, not over the file text: the docstring that DOCUMENTS
    this promise names `spend_ledger_from_env`, so a substring check over the
    source is red whether or not the code calls it. A check that fails for the
    wrong reason is as useless as one that passes for the wrong reason -- the
    first draft of this test did exactly that and had to be rewritten.

    REVERT: add `spend = spend or spend_ledger_from_env()` to `call_llm`.
    Observed: this test fails naming `spend_ledger_from_env` among the
    identifiers `call_llm` references, and the module-import assertion too,
    since the name has to come from somewhere.
    """
    body = _function("call_llm")
    referenced = {
        node.id for node in ast.walk(body) if isinstance(node, ast.Name)
    } | {
        node.attr for node in ast.walk(body) if isinstance(node, ast.Attribute)
    }
    assert "spend_ledger_from_env" not in referenced
    assert not {"environ", "getenv"} & referenced
    # platform must not import the ledger at all: the one-way edge is
    # spend_ledger -> platform.
    imported = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any("spend_ledger" in name for name in imported), imported


def test_spend_none_bills_with_no_ledger_row(tmp_path):
    """The section 6 enforcement gap, pinned as BEHAVIOUR rather than a promise.

    A call without `spend=` reaches the provider and the ledger is untouched.
    This is not a defect to fix in the library -- reserve-before-pay is a
    property of the CALL SITE -- and it is pinned so nobody reads the ledger's
    numbers as covering a run.

    REVERT: make `spend=None` fall back to a ledger. Observed: the row count
    stops being 0, or (via the environment) the call refuses outright.
    """
    ledger = make_ledger(tmp_path)
    backend = MockBackend(responses=[priced("billed anyway", cost=5.00)])

    out = call_llm(backend, "sys", "user", model=MODEL, pricing=PRICING)

    assert out.text == "billed anyway"
    assert rows(ledger) == []
    assert ledger.status().outstanding_usd == 0.0


# ---------------------------------------------------------------------------
# Sizing the reservation
# ---------------------------------------------------------------------------


def test_refuses_a_spend_with_neither_pricing_nor_an_explicit_amount(tmp_path):
    """Refuse, rather than reserve zero: a 0 reservation fits every cap.

    The refusal lands BEFORE the provider is called and before the cache is
    consulted, so a misconfigured call fails the same way whether or not its
    answer happens to be cached.

    REVERT: replace the refusal with `return 0.0` in `_spend_reservation_usd`.
    Observed: no ValueError, the call succeeds, and the ledger holds one row
    reserving 0 -- so `pytest.raises(ValueError)` fails and the cap is inert.
    """
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)
    backend = MockBackend(responses=[priced("ok", cost=0.01)])

    with pytest.raises(ValueError) as caught:
        call_llm(backend, "sys", "user", model=MODEL, spend=guard)

    assert "spend_reserve_usd" in str(caught.value)
    assert backend.calls == []
    assert rows(ledger) == []


def test_refusal_precedes_the_cache_lookup(tmp_path):
    """The misconfiguration is deterministic, not cache-dependent.

    REVERT: move the refusal below the cache block. Observed: the cached answer
    is returned and no ValueError is raised, so an identical misconfiguration
    reports itself on a cold run and stays silent on a warm one.
    """
    cache_dir = tmp_path / "cache"
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)
    warm = MockBackend(responses=[priced("cached", cost=0.02)])
    call_llm(warm, "sys", "user", model=MODEL, pricing=PRICING, cache_dir=cache_dir)

    with pytest.raises(ValueError):
        call_llm(
            MockBackend(responses=[]),
            "sys",
            "user",
            model=MODEL,
            cache_dir=cache_dir,
            spend=guard,
        )


def test_an_explicit_amount_needs_no_pricing_table(tmp_path):
    """`spend_reserve_usd` alone is enough, and it is used verbatim.

    REVERT: make `_spend_reservation_usd` ignore `explicit` and always price.
    Observed: with `pricing=None` it raises ValueError, so the call fails
    instead of reserving $0.25.
    """
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)
    backend = MockBackend(responses=[priced("ok", cost=0.01)])

    call_llm(
        backend, "sys", "user", model=MODEL, spend=guard, spend_reserve_usd=0.25
    )

    assert guard.reserve_calls[0]["amount_usd"] == 0.25
    assert rows(ledger)[0]["reserved"] == 250_000_000


def test_a_priced_reservation_covers_the_full_max_tokens(tmp_path):
    """Derived from pricing, the reservation prices the FULL output allowance.

    A reservation sized on an EXPECTED output length under-reserves exactly
    when the model runs long, which is the case a cap most needs to catch.

    REVERT: pass `0` instead of `max_output_tokens` to `estimate_cost` in
    `_spend_reservation_usd`. Observed: the amount drops to the input-only
    $0.00000090 and this assertion fails -- a near-zero reservation that any
    cap admits.
    """
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)
    backend = MockBackend(responses=[priced("ok", cost=0.01)])

    call_llm(
        backend,
        "sys",
        "user",
        model=MODEL,
        pricing=PRICING,
        options=BackendOptions(max_tokens=1_000_000),
        spend=guard,
    )

    # 1M output tokens at $1.20/M, plus the 2-token prompt estimate at $0.30/M.
    amount = guard.reserve_calls[0]["amount_usd"]
    assert 1.20 < amount < 1.21


def test_a_rejected_zero_or_negative_explicit_amount(tmp_path):
    """`spend_reserve_usd=0` is the zero reservation under another name.

    REVERT: drop the `amount > 0` check. Observed: no ValueError; the ledger's
    own `reserve` then raises for the same reason, which is the fallback, not
    the contract -- the point of checking here is that the message names
    `spend_reserve_usd` rather than nano-USD.
    """
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)
    backend = MockBackend(responses=[priced("ok", cost=0.01)])

    with pytest.raises(ValueError, match="spend_reserve_usd"):
        call_llm(
            backend,
            "sys",
            "user",
            model=MODEL,
            pricing=PRICING,
            spend=guard,
            spend_reserve_usd=0.0,
        )


# ---------------------------------------------------------------------------
# The lease exists only when a deadline is known
# ---------------------------------------------------------------------------


def test_no_timeout_means_a_null_lease(tmp_path):
    """`options.timeout_s is None` -> `ttl_s=None` -> a NULL lease no sweep takes.

    This is B2's stop: with no deadline bounding the attempt, nothing may
    reclaim its headroom on a guess.

    REVERT: derive a ttl unconditionally (e.g. `ttl_s=60.0`). Observed:
    `ttl_s is None` fails, and the stored `lease_expires_at` becomes a real
    deadline a sweep is entitled to take while the attempt is still live.
    """
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)
    backend = MockBackend(responses=[priced("ok", cost=0.01)])

    call_llm(
        backend, "sys", "user", model=MODEL, pricing=PRICING,
        spend=guard, spend_reserve_usd=0.50,
    )

    assert guard.reserve_calls[0]["ttl_s"] is None
    conn = sqlite3.connect(str(ledger.path))
    try:
        assert conn.execute("SELECT lease_expires_at FROM ledger").fetchone()[0] is None
    finally:
        conn.close()


def test_a_timeout_derives_the_lease(tmp_path):
    """With `timeout_s` set the lease is `2 * timeout_s + 60` seconds out.

    REVERT: pass `ttl_s=None` unconditionally. Observed: `ttl_s == 160.0` fails
    with None, and a crashed process's reservation would hold its headroom
    forever despite a known deadline.
    """
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)
    backend = MockBackend(responses=[priced("ok", cost=0.01)])

    call_llm(
        backend,
        "sys",
        "user",
        model=MODEL,
        pricing=PRICING,
        options=BackendOptions(timeout_s=50),
        spend=guard,
        spend_reserve_usd=0.50,
    )

    assert guard.reserve_calls[0]["ttl_s"] == 160.0


def test_the_guard_is_identified_by_the_callers_identifier(tmp_path):
    """Each row names the unit it was reserved for, so a report can attribute it.

    REVERT: stop passing `identifier=`. Observed: the row's identifier is empty
    and this assertion fails, leaving every row in a multi-unit run
    indistinguishable.
    """
    ledger = make_ledger(tmp_path)
    guard = Recorder(ledger)
    backend = MockBackend(responses=[priced("ok", cost=0.01)])

    call_llm(
        backend,
        "sys",
        "user",
        model=MODEL,
        pricing=PRICING,
        identifier="unit-42",
        spend=guard,
        spend_reserve_usd=0.50,
    )

    assert guard.reserve_calls[0]["identifier"] == "unit-42"
    conn = sqlite3.connect(str(ledger.path))
    try:
        row = conn.execute("SELECT identifier, model FROM ledger").fetchone()
    finally:
        conn.close()
    assert row[0] == "unit-42"
    assert row[1] == MODEL


# ---------------------------------------------------------------------------
# The Protocol
# ---------------------------------------------------------------------------


def test_spend_ledger_satisfies_the_spend_guard_protocol(tmp_path):
    """The real ledger is what `call_llm` is typed against.

    The first assertion alone would be vacuous if the Protocol declared nothing:
    a `runtime_checkable` Protocol with no members accepts every object. So the
    second half removes one method at a time from a stand-in and checks the
    isinstance verdict NOTICES -- that is the counterfactual, run here, in place
    of renaming a method on `SpendLedger` (SL-1's file, which this unit does not
    edit).
    """
    ledger = make_ledger(tmp_path)
    assert isinstance(ledger, platform.SpendGuard)
    assert "SpendGuard" in platform.__all__

    required = ("reserve", "settle", "release", "check_halted")
    full = {name: (lambda *a, **k: None) for name in required}
    assert isinstance(type("Full", (), full)(), platform.SpendGuard)
    for missing in required:
        partial = {k: v for k, v in full.items() if k != missing}
        stub = type("Partial", (), partial)()
        assert not isinstance(stub, platform.SpendGuard), (
            f"SpendGuard accepted a guard with no {missing!r}"
        )
