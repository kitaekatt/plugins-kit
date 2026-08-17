"""Prepare / finalize lifecycle over the durable run store (A-min.2).

Two entry points bracket a run:

- :func:`prepare_run` -- runs the gate sequence and a freshness check over
  every still-``PENDING`` unit, records terminal skips for the ones that
  will never be generated, and returns the currently claimable wave (via
  :func:`~content_pipeline.execution.wave.ready_wave`).
- :func:`finalize_run` -- applies every ``ACCEPTED`` unit's recorded text,
  serially, in ordinal order, recording ``apply_started``/``apply_succeeded``
  around each call (plan D6). It never re-adjudicates a verdict (D1,
  invariant 5): the adapter's ``parse_fn`` is called mechanically to recover
  the payload object from the durably recorded ``accepted_text``, and no
  validator ever runs again.

Also here: :func:`unfinished_units` (every unit without a terminal state, a
SET with holes preserved, the halt-triggering unit included), the thin
``pause_run`` / ``resume_run`` wrappers over ``store.set_halt`` /
``store.clear_halt`` (D4 -- an operator pause is just another halt kind; no
new store method exists for it), and :func:`record_halt` -- the D4 halt
response (``set_halt`` then return the triggering unit to ``PENDING``) shared
by every driver, so a driver never re-derives it (see "Halt handling is a
driver-shared helper" below).

The ``RunAdapter``-shaped seam
-------------------------------

:class:`RunAdapter` here is a **local, minimal** dataclass of callables -- NOT
the versioned JSON worker protocol A-min.3 ships (``execution/adapter.py`` /
``execution/protocol.py`` do not exist yet; do not build them here). It is the
one object that carries a consumer's full contract with both
:func:`~content_pipeline.execution.drivers.inline.run_wave` (production:
``unit_for``, ``system_for``, ``user_for``, ``parse_fn``, ``validators``) and
:func:`finalize_run` (``parse_fn``, ``apply``, ``reconcile``) -- ``parse_fn``
is a single field shared by both call sites, not two independently-supplied
copies, which is what makes D1's "finalize re-parses with the SAME function
the driver submitted under" requirement hold BY CONSTRUCTION rather than by
the caller remembering to pass the same callable twice (see
``drivers/inline.py``'s module docstring, "The adapter is one object").

Of A-min.3's five documented ``RunAdapter`` responsibilities (plan of record,
phase A-min.3: reconstruct unit by ID, build a prepared request, provide the
``ValidationSpec``, apply a payload, optionally reconcile), this local seam
covers "reconstruct unit by ID" (``unit_for``) and "apply a payload"
(``parse_fn`` + ``apply``, with ``reconcile`` for the ``apply_unknown`` case).
Still absent: a first-class "build a prepared request" step (today split
across ``system_for``/``user_for``, not a single request object) and a typed
``ValidationSpec`` field (today ``validators``, a bare sequence). Both are
widenings -- new fields, not signature changes -- when A-min.3 lands.

``parse_fn`` MUST be deterministic and store-independent for tracked runs:
finalize re-runs it on text recorded at submit time, potentially long after
and in a different process, so any dependence on ambient state (clock,
filesystem, network) would make a replay diverge from what the worker that
recorded the text actually saw (plan D1's adapter contract).

Halt handling is a driver-shared helper
------------------------------------------

:func:`record_halt` implements D4's halt response once, here, rather than
inside a driver: ``store.set_halt`` followed by returning the triggering unit
to ``PENDING`` via ``store.fail_unit(..., terminal=False)``. It does not stop
a driver's own claim loop -- that control flow is the driver's, since only the
driver knows what "stop claiming" means for its own concurrency model (a
``break`` for the inline driver's serial loop; something else for a background
dispatcher). Extracted from ``drivers/inline.py`` so the two planned drivers
(phases B and C) that need byte-identical D4 semantics inherit this instead of
re-deriving it.

The gate seam: a direct import, not a re-derived shape
--------------------------------------------------------

``prepare_run`` runs gates through
:class:`content_pipeline.pipeline.single_pass.Gate` and
:func:`content_pipeline.pipeline.single_pass.run_gates` directly, rather than
redefining an equivalent local shape. ``pipeline/single_pass.py`` is
read-only for A-min.2 (no edits, no behavior change to the untracked
``run_single_pass`` loop), but importing its ``Gate`` dataclass and
``run_gates`` helper is not an edit -- it is reuse, and it is the only way a
consumer's existing gate list (already built against that exact shape) works
unchanged against the tracked path. Redefining an equivalent local
``Gate``/`run_gates`` would fork the shape for no benefit and cost every
consumer a second, subtly-different gate protocol to satisfy. This import is
why this package's docstring can no longer claim "depends on no other
subpackage" -- see ``execution/__init__.py``.

Terminal skips: a dedicated terminal state, ``UnitState.SKIPPED``
----------------------------------------------------------------------

A unit a gate or freshness check decides will never be generated is recorded
as a terminal **skip**, not a terminal **failure**: ``store.claim_unit`` then
``store.fail_unit(terminal=True, terminal_state=UnitState.SKIPPED,
error="skip:...")``. Landing in its own terminal state (rather than
``UnitState.FAILED``, the original A-min.2 shape) is what fixes two defects
that shape had:

- ``execution.wave``'s graph-strategy readiness treats a ``FAILED``
  predecessor as a permanent block. Before ``SKIPPED`` existed, a skipped
  unit 0 (up-to-date / gated / unsupported) permanently wedged every
  ordinally-later unit in a ``GraphWalkStrategy`` run -- they could never
  become claimable. ``execution.wave`` now treats ``SKIPPED`` exactly like
  ``ACCEPTED`` for this purpose: it satisfies a successor.
- ``execution.status``'s digest counted a skip as a real failure --
  inflating ``counts_by_state["failed"]``, ``failed_in_window``, and burning
  a ``recent_failures`` slot with skip noise. With its own state,
  ``counts_by_state["skipped"]`` carries it instead (a plain ``Counter`` over
  unit state already tells the two apart, no status.py logic change needed
  for that field); the attempt-level counts still needed an explicit
  exclusion, since a skip is still recorded through the same
  ``AttemptKind.FAIL`` write as a real failure -- see ``execution.status``.

The error-string convention is unchanged (``model.SKIP_ERROR_PREFIX``, so
``execution.status`` can recognize it without importing this module), and
still exists so ``execution.status``'s failure-code classifier groups skips
sensibly instead of hashing indistinguishable text, and so a reader of
``list_attempts`` can tell a skip's reason apart from a real failure's:

- ``"skip:gate:<gate-name>:<reason>"`` -- a non-sticky gate fired.
- ``"skip:unsupported:<gate-name>:<reason>"`` -- a sticky gate fired (the
  unit is structurally unsupported, mirroring ``Disposition.UNSUPPORTED``).
- ``"skip:up_to_date"`` -- freshness said no generation is needed.

``UnitState`` is a ``str`` Enum over a ``TEXT`` column, so adding
``SKIPPED`` needed no store migration -- see ``execution.model``'s
``UnitState`` docstring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import zip_longest
from typing import Any, Callable, List, Optional, Sequence

from content_pipeline.execution.model import (
    AttemptKind,
    AttemptRecord,
    ExecutionError,
    SKIP_ERROR_PREFIX,
    TERMINAL_STATES,
    UnitRecord,
    UnitState,
)
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.execution.wave import is_graph_strategy, ready_wave
from content_pipeline.freshness.classify import FreshnessState, needs_generation
from content_pipeline.llm.platform import HaltError
from content_pipeline.pipeline.single_pass import Gate, run_gates
from content_pipeline.pipeline.workunit import WorkUnit, WorkUnitStrategy
from content_pipeline.validate import contract

DEFAULT_PREPARE_WORKER_ID = "prepare"
DEFAULT_FINALIZE_WORKER_ID = "finalize"


def _default_unit_for(unit_id: str) -> WorkUnit:
    """The ``RunAdapter.unit_for`` default: a bare ``WorkUnit`` carrying only
    the id, no payload or context -- sufficient for a ``generate`` callable
    that needs neither."""
    return WorkUnit(id=unit_id)


class ApplyUnknownError(ExecutionError):
    """Finalize refuses to proceed while any unit is ``apply_unknown`` (D6).

    Raised when the adapter supplies no ``reconcile`` hook (fail closed) --
    or when ``reconcile`` is supplied but this unit still resolves to
    ``apply_unknown`` after asking it (i.e. ``reconcile`` returned ``False``
    and finalize chose to re-apply rather than silently proceeding without
    ever closing the record -- see :func:`finalize_run`).
    """

    def __init__(self, unit_id: str) -> None:
        self.unit_id = unit_id
        super().__init__(
            f"unit {unit_id!r} is apply_unknown (an APPLY_STARTED attempt with "
            "no following APPLY_SUCCEEDED) and the adapter supplies no "
            "reconcile hook; finalize refuses to proceed (D6, fail closed)"
        )


class MissingAcceptedTextError(ExecutionError):
    """Finalize refuses an ACCEPTED unit with no recorded ``accepted_text`` (D6, fail closed).

    ``store.accept_unit`` still permits omitting ``text`` (an optional
    parameter -- see its docstring -- and a pre-0.7.2 row migrated to a
    ``NULL`` ``accepted_text`` column). Calling ``adapter.parse_fn(None)``
    and treating whatever it returns as a real payload would be fail-OPEN:
    a unit could be marked applied with no payload ever having existed.
    Raised before ``adapter.parse_fn`` is called and before
    ``record_apply_started`` -- so ``adapter.apply`` is never invoked, and no
    apply-attempt row is written, for a unit refused this way. Mirrors
    :class:`ApplyUnknownError`'s refusal shape.
    """

    def __init__(self, unit_id: str) -> None:
        self.unit_id = unit_id
        super().__init__(
            f"unit {unit_id!r} is ACCEPTED with no recorded accepted_text; "
            "finalize refuses to apply a None payload (D6, fail closed)"
        )


class GraphOrderMismatchError(ExecutionError):
    """``prepare_run`` refuses a graph-strategy run whose ``strategy.order()``
    disagrees with the store's registration order (user decision, 2026-08-17).

    ``store.register_units`` assigns ordinals in argument order;
    ``GraphWalkStrategy.order`` independently claims to own the traversal
    order. Nothing coupled the two before this check, and ``execution.wave``'s
    ``_graph_ready_wave`` linearizes purely by registered ordinal -- so a
    consumer who registers units in a different order than ``order()`` walks
    them gets a successor unit generated before its predecessor is applied,
    silently, with plausible-looking output. That defeats the one-unit-wave
    guarantee a graph strategy exists to provide. Raised naming the FIRST
    position at which the two sequences diverge, so a caller can find the
    mismatched registration call rather than guessing.
    """

    def __init__(
        self,
        run_id: str,
        position: int,
        expected: Optional[str],
        actual: Optional[str],
    ) -> None:
        self.run_id = run_id
        self.position = position
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"run {run_id!r}: graph order and registration order diverge at "
            f"position {position} -- strategy.order() says {expected!r}, "
            f"but the unit registered at that ordinal is {actual!r}"
        )


@dataclass
class RunAdapter:
    """The local, minimal seam :func:`~content_pipeline.execution.drivers.inline.run_wave`
    and :func:`finalize_run` both call through -- see the module docstring's
    "The ``RunAdapter``-shaped seam" section for which A-min.3 responsibility
    each field covers and which are still absent.

    Production fields (consumed by ``drivers.inline.run_wave``):

    - ``unit_for`` -- ``unit_id -> WorkUnit``. Reconstructs the work unit a
      driver claimed, by id. Defaults to a bare ``WorkUnit(id=unit_id)``, the
      right shape for a ``generate`` callable that needs neither payload nor
      context.
    - ``system_for`` / ``user_for`` -- optional ``WorkUnit -> str``. Build the
      backend-path prompt; ``user_for`` is required (and ``system_for``
      defaults to an empty system prompt) whenever ``run_wave`` is given a
      ``backend`` rather than a plain ``generate`` callable.
    - ``validators`` -- passed straight through to ``submit_validated``'s
      validate-until-valid loop.

    Finalize fields (consumed by :func:`finalize_run`):

    - ``parse_fn`` -- ``text -> payload``. Called mechanically on the durably
      recorded ``accepted_text``; never re-validates (D1). THE SAME callable
      a ``backend``-path ``run_wave`` call submitted under, by construction --
      not a second, independently-supplied copy (D1's re-parse requirement).
    - ``apply`` -- ``(unit_id, payload) -> None``. The consumer's delivery
      side effect (e.g. a ``deliver.*`` write).
    - ``reconcile`` -- optional ``unit_id -> bool``. Answers "did this
      unit's apply already land" for a unit found ``apply_unknown``. Absent
      means finalize refuses to proceed past any ``apply_unknown`` unit
      (D6, fail closed).

    Every field defaults so a consumer exercising only one of the two call
    sites (e.g. a ``generate``-only ``run_wave`` call that never finalizes)
    supplies only what it uses; a call site that actually needs a field it
    was not given fails at the point of use (a ``None`` called as a function,
    or ``run_wave``'s own explicit ``ValueError`` for the backend path's
    required ``user_for``/``parse_fn``), not at construction time.
    """

    unit_for: Callable[[str], WorkUnit] = _default_unit_for
    system_for: Optional[Callable[[WorkUnit], str]] = None
    user_for: Optional[Callable[[WorkUnit], str]] = None
    parse_fn: Optional[Callable[[str], Any]] = None
    validators: Sequence[contract.Validator] = field(default_factory=tuple)
    apply: Optional[Callable[[str, Any], None]] = None
    reconcile: Optional[Callable[[str], bool]] = None


# ---------------------------------------------------------------------------
# prepare_run
# ---------------------------------------------------------------------------


def _record_terminal_skip(
    store: ExecutionStore,
    run_id: str,
    unit_id: str,
    worker_id: str,
    error: str,
    *,
    at: Optional[float] = None,
) -> None:
    """Claim, then terminally SKIP with a ``skip:...`` error (see module docstring)."""
    claim = store.claim_unit(run_id, unit_id, worker_id, at=at)
    store.fail_unit(
        run_id,
        unit_id,
        claim.fencing_token,
        error=error,
        terminal=True,
        terminal_state=UnitState.SKIPPED,
        at=at,
    )


def _validate_graph_order(
    store: ExecutionStore,
    run_id: str,
    strategy: WorkUnitStrategy,
    graph_source: Any,
) -> None:
    """Refuse loudly when ``strategy.order()`` disagrees with registration
    order -- see :class:`GraphOrderMismatchError` and the "why" note on
    :func:`prepare_run`."""
    order_ids = [str(node_id) for node_id in strategy.order(graph_source)]  # type: ignore[attr-defined]
    registered_ids = [
        u.unit_id for u in sorted(store.list_units(run_id), key=lambda u: u.ordinal)
    ]
    for position, (expected, actual) in enumerate(zip_longest(order_ids, registered_ids)):
        if expected != actual:
            raise GraphOrderMismatchError(run_id, position, expected, actual)


def prepare_run(
    store: ExecutionStore,
    run_id: str,
    strategy: WorkUnitStrategy,
    work_units: Sequence[WorkUnit],
    *,
    graph_source: Any = None,
    gates: Sequence[Gate] = (),
    freshness_of: Optional[Callable[[WorkUnit], FreshnessState]] = None,
    include_stale: bool = True,
    mark_unsupported: Optional[Callable[[str, str], None]] = None,
    max_wave_size: Optional[int] = None,
    worker_id: str = DEFAULT_PREPARE_WORKER_ID,
    at: Optional[float] = None,
) -> List[UnitRecord]:
    """Evaluate gates and freshness, record terminal skips, materialize a wave.

    ``work_units`` supplies the ``WorkUnit`` (payload + context) for every
    unit currently registered ``PENDING`` in the store, keyed by
    ``WorkUnit.id`` matching the store's ``unit_id``. A ``PENDING`` unit with
    no matching entry in ``work_units`` is left untouched (out of scope for
    this prepare call -- e.g. a caller preparing one chunk of a larger run).

    When ``strategy`` is a graph strategy
    (:func:`~content_pipeline.execution.wave.is_graph_strategy`), this call
    FIRST validates that ``strategy.order(graph_source)`` -- the traversal
    order the strategy itself claims to own -- agrees with the ids
    ``store.register_units`` recorded, in ordinal order. ``graph_source`` is
    the CONSUMER's own store (whatever object the caller's
    ``GraphWalkStrategy.order``/``payload_of``/``context_of`` callables
    expect -- see ``pipeline/workunit.py``), NOT the ``ExecutionStore`` this
    function's own ``store`` parameter is. On any divergence this raises
    :class:`GraphOrderMismatchError` naming the first mismatched position
    before touching a single unit's state.

    Why this check exists: ``execution.wave``'s ``_graph_ready_wave``
    linearizes a graph strategy's wave purely from registered ordinal --
    it never consults ``strategy.order()`` at all. ``GraphWalkStrategy.order``'s
    own docstring says "the caller owns the traversal order", and
    ``store.register_units`` assigns ordinals "in argument order" -- two
    independent claims about the same sequence, with nothing coupling them
    before this check. A consumer who registers units in a different order
    than ``order()`` walks them gets a successor generated before its
    predecessor is applied, silently, with plausible-looking output --
    exactly the failure the one-unit-wave machinery exists to prevent. A flat
    strategy carries no such ordering claim (the flat shape asserts units are
    independent), so it is unaffected: no validation, no behavior change.

    For each matched ``PENDING`` unit, in the order ``store.list_units``
    returns (ordinal order):

    1. Run ``gates`` via :func:`~content_pipeline.pipeline.single_pass.run_gates`.
       A firing gate records a terminal skip (see module docstring for the
       error-string convention) and, if ``sticky``, also calls
       ``mark_unsupported(unit_id, reason)`` when supplied.
    2. Otherwise, when ``freshness_of`` is supplied, classify the unit and
       skip (terminally, ``"skip:up_to_date"``) when
       :func:`~content_pipeline.freshness.classify.needs_generation` says no
       generation is needed.

    Neither step is store-invented: it is exactly ``claim`` then
    ``fail_unit(terminal=True, ...)`` against the existing store API.

    Returns the ready wave via
    :func:`~content_pipeline.execution.wave.ready_wave` computed AFTER all
    skips above have landed, so a just-skipped unit is not reported as
    claimable to a caller reading the returned wave.
    """
    if is_graph_strategy(strategy):
        _validate_graph_order(store, run_id, strategy, graph_source)

    by_id = {wu.id: wu for wu in work_units}
    for unit in store.list_units(run_id):
        if unit.state is not UnitState.PENDING:
            continue
        work_unit = by_id.get(unit.unit_id)
        if work_unit is None:
            continue

        fired = run_gates(gates, work_unit)
        if fired is not None:
            gate, reason = fired
            if gate.sticky:
                if mark_unsupported is not None:
                    mark_unsupported(unit.unit_id, reason)
                error = f"{SKIP_ERROR_PREFIX}unsupported:{gate.name}:{reason}"
            else:
                error = f"{SKIP_ERROR_PREFIX}gate:{gate.name}:{reason}"
            _record_terminal_skip(store, run_id, unit.unit_id, worker_id, error, at=at)
            continue

        if freshness_of is not None:
            state = freshness_of(work_unit)
            if not needs_generation(state, include_stale=include_stale):
                _record_terminal_skip(
                    store, run_id, unit.unit_id, worker_id, f"{SKIP_ERROR_PREFIX}up_to_date", at=at
                )
                continue

    return ready_wave(store, run_id, strategy, max_wave_size=max_wave_size)


# ---------------------------------------------------------------------------
# finalize_run
# ---------------------------------------------------------------------------


def _last_apply_kind(attempts: Sequence[AttemptRecord]) -> Optional[AttemptKind]:
    """The most recent apply-related attempt kind, or ``None`` if never applied."""
    last: Optional[AttemptKind] = None
    for attempt in attempts:
        if attempt.kind in (AttemptKind.APPLY_STARTED, AttemptKind.APPLY_SUCCEEDED):
            last = attempt.kind
    return last


def finalize_run(
    store: ExecutionStore,
    run_id: str,
    adapter: RunAdapter,
    *,
    worker_id: str = DEFAULT_FINALIZE_WORKER_ID,
    at: Optional[float] = None,
) -> List[str]:
    """Apply every ``ACCEPTED`` unit's recorded text, serially, in ordinal order.

    Idempotent (invariant 3): apply state is derived, never stored, by
    scanning :meth:`~content_pipeline.execution.store.ExecutionStore.list_attempts`
    for the last apply-kind attempt per unit (see :func:`_last_apply_kind`):

    - No apply-kind attempt -- not yet applied; apply now.
    - Last is ``APPLY_SUCCEEDED`` -- already applied; skipped (never replayed).
    - Last is ``APPLY_STARTED`` with no following ``APPLY_SUCCEEDED`` --
      ``apply_unknown``. Refuses via :class:`ApplyUnknownError` unless
      ``adapter.reconcile`` is supplied. When it is: ``reconcile(unit_id)``
      returning ``True`` means the apply already landed -- record
      ``apply_succeeded`` and move on WITHOUT calling ``apply`` again (D6:
      never risk a duplicate side effect once reconciliation confirms it
      landed). Returning ``False`` means it did not land -- fall through to
      a normal re-apply (a fresh ``apply_started``/``apply_succeeded`` pair),
      since only the record, not the side effect, was confirmed absent.

    Returns the ids of units whose ``adapter.apply`` was actually invoked
    during THIS call -- a reconciled-as-landed unit is not included, since
    its side effect was not (re)run here.

    Never re-adjudicates a verdict (D1, invariant 5): ``adapter.parse_fn`` is
    called mechanically on the durably recorded ``accepted_text`` to recover
    the payload object; no validator runs again.

    Fails closed on a ``None`` payload (D6): an ACCEPTED unit whose
    ``accepted_text`` is ``None`` (``accept_unit`` permits omitting ``text``;
    a pre-0.7.2 row may also have migrated to ``NULL``) raises
    :class:`MissingAcceptedTextError` rather than calling
    ``adapter.parse_fn(None)`` and applying whatever it returns.
    """
    applied: List[str] = []
    units = sorted(store.list_units(run_id), key=lambda u: u.ordinal)
    for unit in units:
        if unit.state is not UnitState.ACCEPTED:
            continue

        attempts = store.list_attempts(run_id, unit.unit_id)
        last_kind = _last_apply_kind(attempts)

        if last_kind is AttemptKind.APPLY_SUCCEEDED:
            continue  # already applied; idempotence

        if last_kind is AttemptKind.APPLY_STARTED:
            if adapter.reconcile is None:
                raise ApplyUnknownError(unit.unit_id)
            landed = adapter.reconcile(unit.unit_id)
            if landed:
                store.record_apply_succeeded(run_id, unit.unit_id, at=at)
                continue
            # Not landed: fall through to a fresh apply below.

        if unit.accepted_text is None:
            raise MissingAcceptedTextError(unit.unit_id)

        payload = adapter.parse_fn(unit.accepted_text)
        store.record_apply_started(run_id, unit.unit_id, at=at)
        adapter.apply(unit.unit_id, payload)
        store.record_apply_succeeded(run_id, unit.unit_id, at=at)
        applied.append(unit.unit_id)

    return applied


# ---------------------------------------------------------------------------
# unfinished_units
# ---------------------------------------------------------------------------


def unfinished_units(store: ExecutionStore, run_id: str) -> List[UnitRecord]:
    """Every unit WITHOUT a terminal state, ordinal order, holes included.

    A SET, not a queue: the halt-triggering unit (returned to ``PENDING`` by
    the driver on halt, per D4/invariant 2) is included, and a unit's
    original ordinal is preserved regardless of which ordinals around it are
    terminal -- there is no renumbering, so a caller can report "unit 7 of
    12 is unfinished" meaningfully even when units 3 and 5 are done.
    """
    units = sorted(store.list_units(run_id), key=lambda u: u.ordinal)
    return [u for u in units if u.state not in TERMINAL_STATES]


# ---------------------------------------------------------------------------
# record_halt
# ---------------------------------------------------------------------------


def record_halt(
    store: ExecutionStore,
    run_id: str,
    unit_id: str,
    fencing_token: int,
    exc: HaltError,
    *,
    at: Optional[float] = None,
) -> None:
    """D4's halt response, shared by every driver: ``set_halt`` the run, then
    return the triggering unit to ``PENDING`` (not a terminal failure) via
    ``fail_unit(terminal=False)`` -- it is unfinished work, not a permanent
    failure, and stays eligible for a future wave once the run resumes.

    Does not stop the caller's own claim loop; a driver calls this from its
    ``except HaltError`` handler and is responsible for not claiming further
    units afterward (see ``drivers/inline.py``'s "Halt handling" section for
    the concurrency-one shape of that responsibility -- a later driver with a
    different concurrency model satisfies the same "stop claiming" contract
    differently, but the store-side response captured here is byte-identical
    for all of them, per the module docstring's "Halt handling is a
    driver-shared helper" section).
    """
    store.set_halt(run_id, kind=exc.kind, detail=exc.detail, at=at)
    store.fail_unit(
        run_id,
        unit_id,
        fencing_token,
        error=f"halt:{exc.kind}",
        terminal=False,
        at=at,
    )


# ---------------------------------------------------------------------------
# pause_run / resume_run
# ---------------------------------------------------------------------------


def pause_run(
    store: ExecutionStore, run_id: str, *, detail: str = "", at: Optional[float] = None
) -> None:
    """Halt ``run_id`` with kind ``"pause"`` (D4: an operator pause is just
    another halt kind -- new claims stop; a valid-fence submission already
    in flight is still accepted; no new store method exists for this)."""
    store.set_halt(run_id, kind="pause", detail=detail, at=at)


def resume_run(store: ExecutionStore, run_id: str) -> None:
    """Clear any halt (pause or otherwise) on ``run_id``."""
    store.clear_halt(run_id)


__all__ = [
    "ApplyUnknownError",
    "GraphOrderMismatchError",
    "MissingAcceptedTextError",
    "RunAdapter",
    "prepare_run",
    "finalize_run",
    "unfinished_units",
    "record_halt",
    "pause_run",
    "resume_run",
]
