"""SL-6: `cli.budget.spend_stop` -- the ledger verdict -> `BudgetStop` seam.

Design: `dev/tasks/loc-pipeline-consumer-needs/design-cross-process-spend-ledger.md`
section 8 ("Where BudgetStop is raised"). `BudgetStop` is raised from
`cli.budget` and NOT from `llm/`, because `cli/budget.py` may import `llm` plus
stdlib only, so `llm/` importing `BudgetStop` would invert the layer. The ledger
raises its own `SpendCapExceeded` / `SpendLedgerHalted` and this seam translates
them, mirroring `preflight_check`'s `PipelineHaltError` -> `BudgetStop` mapping.

Scope boundaries, so a reader does not look here for them: the existing budget
guard (auth-expiry preflight, text-channel hard stop) is `test_cli_budget.py`,
which this unit leaves untouched; the ledger's own refusals are SL-1..SL-3's
three files; the `call_llm` integration is SL-5's `test_llm_platform_spend.py`.

Every load-bearing assertion here was shown RED by a named revert
(`docs/reference/vacuous-checks.md`); each revert is named in the test's
docstring so the counterfactual can be re-run. For the PASS-THROUGH properties
the revert is an INSERTION -- widening an `except` clause to catch the type --
because the property is the ABSENCE of a branch and there is no line to remove.

A pass-through test is the shape most at risk of being vacuous here: "this
exception is not translated" can be green because the exception never reached
`spend_stop` at all. Two independent mechanics stop that, and every
pass-through case carries both:

1. `test_..._is_handed_to_spend_stop_and_declined` drives the context manager's
   `__exit__` with the exception DIRECTLY, so delivery is not an assumption:
   a `False` return means `spend_stop` was handed that exact exception and
   neither suppressed nor replaced it.
2. `test_..._emerges_from_the_with_block_unchanged` runs the real `with` shape
   and asserts object identity plus `inspect.getgeneratorstate(...) ==
   GEN_CLOSED`, i.e. the generator ran past its `yield` rather than being left
   suspended, and the class-level control
   (`test_the_same_with_shape_does_translate_a_cap_verdict`) shows the
   translating branches are live for exactly this construction -- so the
   difference between a translated and a pass-through case is the TYPE, not a
   route that was never taken.
"""

import ast
import inspect
import sqlite3
import sys
from pathlib import Path

import pytest

from content_pipeline.cli import budget as bud
from content_pipeline.cli.budget import SPEND_CAP, SPEND_HALT, BudgetStop, spend_stop
from content_pipeline.llm import spend_ledger as sl
from content_pipeline.llm.platform import (
    HALT_AUTH,
    HALT_INSUFFICIENT_CREDIT,
    HALT_RATE_LIMIT,
    BudgetExceededError,
)

MODULE_PATH = Path(bud.__file__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make(tmp_path, *, cap_usd=1.0, run_id="run-1", **kwargs):
    return sl.create_ledger(
        tmp_path / "ledger.sqlite", cap_usd=cap_usd, run_id=run_id, **kwargs
    )


def raised(exc):
    """Return ``exc`` carrying a real traceback, as a caught exception would."""
    try:
        raise exc
    except BaseException as caught:  # noqa: BLE001 -- re-handing the same object
        return caught


def hand_to_exit(cm, exc):
    """Enter ``cm``, hand it ``exc`` through ``__exit__``, return the verdict.

    This is the delivery proof: the exception cannot have failed to reach
    `spend_stop` because this function reaches into the seam and gives it to
    the context manager itself. ``False`` means "received, not suppressed, not
    replaced"; a raise means the seam translated it.
    """
    cm.__enter__()
    return cm.__exit__(type(exc), exc, exc.__traceback__)


#: Every pass-through case, as ``(id, factory)``. A FACTORY rather than a shared
#: instance, so no case inherits another's traceback or ``__context__``. Derived
#: from the design's section 8 list plus what SL-2, SL-4 and SL-5 report
#: escaping: `ValueError` (misconfiguration), `sqlite3.OperationalError` (busy
#: timeout), `KeyError` (unknown model in the pricing table), and the three
#: ledger-state faults that are not `BudgetExceededError` subclasses.
PASS_THROUGH = [
    ("value_error", lambda: ValueError("resume() needs force=True")),
    ("operational_error", lambda: sqlite3.OperationalError("database is locked")),
    ("key_error", lambda: KeyError("no-such-model")),
    ("stale_reservation", lambda: sl.StaleReservationError("generation moved")),
    ("ledger_state_invalid", lambda: sl.LedgerStateInvalid("malformed row")),
    ("ledger_identity_changed", lambda: sl.LedgerIdentityChanged("cap changed")),
]
PASS_THROUGH_IDS = [name for name, _ in PASS_THROUGH]
PASS_THROUGH_FACTORIES = [factory for _, factory in PASS_THROUGH]


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------


class TestTranslation:
    def test_a_real_cap_refusal_becomes_a_spend_cap_budget_stop(self, tmp_path):
        """End to end against a REAL ledger: a reserve the cap refuses leaves
        the block as `BudgetStop(SPEND_CAP)` carrying the partial progress.

        Revert shown red: deleted the `except SpendCapExceeded` branch ->
        FAILED with `SpendCapExceeded` escaping the `with`, so
        `pytest.raises(BudgetStop)` saw the wrong type.
        """
        ledger = make(tmp_path, cap_usd=1.0)
        with pytest.raises(BudgetStop) as exc:
            with spend_stop(["u-1"], ["u-2", "u-3"], unit_id="u-2"):
                ledger.reserve(2.0, identifier="u-2", model="m-1")
        assert exc.value.reason == SPEND_CAP
        assert exc.value.unit_id == "u-2"
        assert exc.value.done == ["u-1"]
        assert exc.value.remaining == ["u-2", "u-3"]
        assert isinstance(exc.value.__cause__, sl.SpendCapExceeded)

    def test_a_real_halt_becomes_a_spend_halt_budget_stop(self, tmp_path):
        """End to end: once the halt row is set, the refused reserve leaves the
        block as `BudgetStop(SPEND_HALT)` -- a DIFFERENT reason from the cap
        case, because an operator stop and a full cap are different verdicts.

        Revert shown red: made the `SpendLedgerHalted` branch raise
        `BudgetStop(SPEND_CAP, ...)` -> FAILED on
        `assert exc.value.reason == SPEND_HALT`.
        """
        ledger = make(tmp_path, cap_usd=10.0)
        ledger.halt("operator", "stopping the run")
        with pytest.raises(BudgetStop) as exc:
            with spend_stop([], ["u-9"], unit_id="u-9"):
                ledger.reserve(0.1, identifier="u-9")
        assert exc.value.reason == SPEND_HALT
        assert exc.value.unit_id == "u-9"
        assert isinstance(exc.value.__cause__, sl.SpendLedgerHalted)

    def test_both_reasons_are_distinct_new_values_spelled_like_a_halt_kind(self):
        """`BudgetStop.reason` already carries `PipelineHaltError.kind` values
        (`auth`, `rate_limit`, `insufficient_credit`): lowercase machine-readable
        tokens. The two added values follow that spelling and collide with none
        of them, so a consumer switching on `reason` can tell a spend verdict
        from a credential halt.

        Revert shown red: set `SPEND_CAP = HALT_RATE_LIMIT` -> FAILED on the
        disjointness assertion, because a cap stop would then be
        indistinguishable from a 429.
        """
        assert SPEND_CAP == "spend_cap"
        assert SPEND_HALT == "spend_halt"
        for value in (SPEND_CAP, SPEND_HALT):
            assert value == value.lower()
            assert value.replace("_", "").isalpha()
        halt_kinds = {HALT_AUTH, HALT_RATE_LIMIT, HALT_INSUFFICIENT_CREDIT}
        assert {SPEND_CAP, SPEND_HALT}.isdisjoint(halt_kinds)
        assert SPEND_CAP != SPEND_HALT

    def test_unit_id_is_optional_and_defaults_to_empty(self, tmp_path):
        """`BudgetStop.unit_id` already existed (`""` for a preflight stop), so
        `spend_stop` propagates it rather than adding a field; omitting it is
        the "no unit in hand" case, as in `preflight_check`.

        Revert shown red: changed the keyword to `unit_id="?"` -> FAILED on
        `assert exc.value.unit_id == ""`.
        """
        ledger = make(tmp_path, cap_usd=1.0)
        with pytest.raises(BudgetStop) as exc:
            with spend_stop([], []):
                ledger.reserve(5.0)
        assert exc.value.unit_id == ""
        assert "unit_id" in inspect.signature(spend_stop).parameters

    def test_done_and_remaining_are_snapshotted_not_aliased(self, tmp_path):
        """The driver keeps mutating its own lists after the stop; the
        `BudgetStop` must report the counts AT the stop.

        Revert shown red: this is `BudgetStop.__init__`'s `list(done or [])`,
        pinned here against a future `spend_stop` that passes a live list
        through some other carrier. Replacing both copies with the raw objects
        -> FAILED on `assert stop.done == ["u-1"]`, which had become
        `["u-1", "u-2"]`.
        """
        ledger = make(tmp_path, cap_usd=1.0)
        done = ["u-1"]
        remaining = ["u-2"]
        with pytest.raises(BudgetStop) as exc:
            with spend_stop(done, remaining, unit_id="u-2"):
                ledger.reserve(5.0)
        done.append("u-2")
        remaining.clear()
        assert exc.value.done == ["u-1"]
        assert exc.value.remaining == ["u-2"]

    def test_a_clean_block_raises_nothing(self, tmp_path):
        """An admitted reservation is not a stop; the seam is invisible on the
        happy path, like `preflight_check` with a clean probe."""
        ledger = make(tmp_path, cap_usd=1.0)
        with spend_stop([], ["u-1"], unit_id="u-1"):
            reservation = ledger.reserve(0.5, identifier="u-1")
        ledger.settle(reservation, 0.4)
        assert ledger.status().settled_usd == pytest.approx(0.4)

    def test_the_block_result_is_not_swallowed_on_a_cap_stop(self, tmp_path):
        """A `BudgetStop` must REPLACE the ledger verdict, not ride alongside
        it: the original exception stays reachable as `__cause__` so an operator
        can read `identifier` / `measured` / `budget` off it.

        Revert shown red: dropped `from exc` on the cap branch -> FAILED on
        `assert exc.value.__cause__ is not None`.
        """
        ledger = make(tmp_path, cap_usd=1.0)
        with pytest.raises(BudgetStop) as exc:
            with spend_stop([], [], unit_id="u-5"):
                ledger.reserve(4.0, identifier="u-5", model="m-2")
        cause = exc.value.__cause__
        assert cause is not None
        assert cause.identifier == "u-5"
        assert cause.budget == 1.0
        assert cause.model == "m-2"


# ---------------------------------------------------------------------------
# Pass-through
# ---------------------------------------------------------------------------


class TestPassThrough:
    def test_the_same_with_shape_does_translate_a_cap_verdict(self):
        """The control for every pass-through case below: the identical
        construction, differing only in the raised TYPE, DOES translate. So a
        pass-through result cannot be explained by a route that was never
        taken.

        Revert shown red: deleted both `except` branches -> FAILED here with
        `SpendCapExceeded` escaping, which is what tells a reader the
        pass-through tests in this class were not green for free.
        """
        verdict = sl.SpendCapExceeded(
            identifier="u-1", measured=2.0, budget=1.0, model="m-1"
        )
        with pytest.raises(BudgetStop):
            with spend_stop([], []):
                raise verdict

    @pytest.mark.parametrize("factory", PASS_THROUGH_FACTORIES, ids=PASS_THROUGH_IDS)
    def test_it_is_handed_to_spend_stop_and_declined(self, factory):
        """DELIVERY PROOF. `__exit__` is called with the exception directly, so
        it cannot have missed the seam: a `False` return means `spend_stop`
        received this exact object and neither suppressed nor replaced it.

        Revert shown red -- an INSERTION, because the property is the absence of
        a branch: widened the cap branch to
        `except (SpendCapExceeded, ValueError, sqlite3.OperationalError,
        KeyError, StaleReservationError, LedgerStateInvalid,
        LedgerIdentityChanged) as exc:`. All six cases FAILED, each with
        `BudgetStop: budget stop (spend_cap) at 'u-2': 1 done, 1 remaining`
        raised out of `__exit__` where `False` was expected. (The predicted
        message said `0 done, 0 remaining`; the real one carries this test's own
        `done`/`remaining`, which is itself evidence the translation ran with
        this call's arguments.)
        """
        cm = spend_stop(["u-1"], ["u-2"], unit_id="u-2")
        assert hand_to_exit(cm, raised(factory())) is False
        assert inspect.getgeneratorstate(cm.gen) == inspect.GEN_CLOSED

    @pytest.mark.parametrize("factory", PASS_THROUGH_FACTORIES, ids=PASS_THROUGH_IDS)
    def test_it_emerges_from_the_with_block_unchanged(self, factory):
        """TRAVEL PROOF in the real `with` shape: the SAME object comes out
        (identity, not just type), and the generator is CLOSED rather than left
        suspended at its `yield`, so the exception was thrown into the seam's
        body and propagated from there.

        Revert shown red -- the same INSERTION as above. All six cases FAILED
        with `BudgetStop` raised where the original type was expected.
        """
        exc = factory()
        cm = spend_stop([], [])
        with pytest.raises(type(exc)) as caught:
            with cm:
                raise exc
        assert caught.value is exc
        assert not isinstance(caught.value, BudgetStop)
        assert inspect.getgeneratorstate(cm.gen) == inspect.GEN_CLOSED

    def test_a_real_resume_refusal_passes_through(self, tmp_path):
        """End to end on SL-2's refusal: an overbilled halt refuses `resume()`
        with `ValueError`, and declining to force is an operator verdict about a
        control switch, not a budget verdict. Translating it would make a run
        report hitting its cap when nothing of the sort happened.

        Revert shown red -- INSERTION: added
        `except ValueError as exc: raise BudgetStop(SPEND_HALT, ...) from exc`
        -> FAILED with `BudgetStop` raised where `ValueError` was expected.
        """
        ledger = make(tmp_path, cap_usd=10.0)
        with pytest.raises(sl.SpendCapExceeded):
            ledger.settle(ledger.reserve(0.5), 0.9)
        with pytest.raises(ValueError) as caught:
            with spend_stop([], [], unit_id="u-1"):
                ledger.resume()
        assert "force=True" in str(caught.value)
        assert not isinstance(caught.value, BudgetStop)
        assert not isinstance(caught.value, BudgetExceededError)

    def test_the_translate_set_is_exactly_the_budget_exceeded_subclasses(self):
        """Completeness, derived from the ledger rather than from a hand list: a
        LATER ledger exception that subclasses `BudgetExceededError` would be a
        budget verdict `spend_stop` silently did not translate, and one of the
        two translated types ceasing to be a subclass would break every
        existing `except BudgetExceededError`.

        Revert shown red -- INSERTION in `spend_ledger.py`: made
        `StaleReservationError` subclass `BudgetExceededError` -> FAILED with
        the set holding three names against the expected two, naming the new
        one.
        """
        exported = [getattr(sl, name) for name in sl.__all__]
        verdicts = {
            obj.__name__
            for obj in exported
            if isinstance(obj, type)
            and issubclass(obj, BaseException)
            and issubclass(obj, BudgetExceededError)
        }
        assert verdicts == {"SpendCapExceeded", "SpendLedgerHalted"}
        for name in ("StaleReservationError", "LedgerStateInvalid", "LedgerIdentityChanged"):
            assert not issubclass(getattr(sl, name), BudgetExceededError)
        assert not issubclass(sqlite3.OperationalError, BudgetExceededError)


# ---------------------------------------------------------------------------
# The import-scope rule
# ---------------------------------------------------------------------------


def _imported_modules(path):
    """Every absolute module name `path`'s source imports, read with `ast`.

    Over the SOURCE, not over a successful import: importing the module proves
    nothing about which layer it reached, since a forbidden import that happens
    to resolve is exactly the defect. A relative import is recorded as a
    sentinel so the caller rejects it -- inside `content_pipeline.cli`, any
    relative import is a sibling of `cli`, which the rule forbids.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names.append("." * node.level + (node.module or ""))
            else:
                names.append(node.module or "")
    return names


class TestImportScope:
    """`cli/budget.py`'s own docstring states the rule this class enforces:

        This module (per the dependency contract) may import ``llm`` for the
        PipelineHaltError taxonomy and stdlib -- nothing else from
        ``content_pipeline``.

    It is the whole reason `spend_stop` lives in `cli.budget` instead of beside
    the ledger: `llm/` importing `BudgetStop` would invert the layer.
    """

    def test_budget_imports_only_llm_and_stdlib(self):
        """Asserts over the module's IMPORT GRAPH as parsed from its source, per
        `_imported_modules` -- a test that merely imports `cli.budget` would
        pass with any import that resolves.

        Revert shown red -- INSERTION of a forbidden import: added
        `from content_pipeline.execution.store import ExecutionStore` at the top
        of `cli/budget.py` -> FAILED with
        `forbidden imports in cli/budget.py: ['content_pipeline.execution.store']`.
        A second insertion, `import yaml`, FAILED the same way, so the stdlib
        half of the rule is live too.
        """
        forbidden = []
        for module in _imported_modules(MODULE_PATH):
            if module.startswith("."):
                forbidden.append(module)
            elif module == "content_pipeline.llm" or module.startswith(
                "content_pipeline.llm."
            ):
                continue
            elif module.split(".")[0] in sys.stdlib_module_names:
                continue
            else:
                forbidden.append(module)
        assert not forbidden, (
            "forbidden imports in cli/budget.py: "
            + repr(sorted(forbidden))
            + " -- this module may import content_pipeline.llm and the standard "
            "library only (see its docstring). Raising BudgetStop here is what "
            "keeps the layer one-way."
        )

    def test_the_rule_is_stated_in_the_module_docstring(self):
        """The rule is only checkable by a future reader if the module still
        says it. The guard above would otherwise look like an arbitrary
        restriction someone could relax.

        Revert shown red: deleted the sentence from the docstring -> FAILED on
        the `may import` assertion.
        """
        doc = ast.get_docstring(ast.parse(MODULE_PATH.read_text(encoding="utf-8")))
        assert doc is not None
        assert "may import ``llm``" in doc
        assert "stdlib" in doc
        assert "invert the layer" in doc
