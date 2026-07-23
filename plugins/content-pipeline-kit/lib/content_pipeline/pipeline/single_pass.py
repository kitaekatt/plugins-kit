"""The regenerate-on-stale pipeline shape: two-phase generate / apply.

For every unit ``freshness.classify`` marks stale or missing, a generate phase
produces a candidate and an apply phase writes it through ``deliver``. The two
phases are kept distinct so a caller can inspect (or gate) everything about to
be applied before any delivery side effect runs -- exactly the source system's
``generate_conversation`` (brief only, never touches the delivered file) vs.
``apply_conversation`` (writes the file) split, which lets a bulk run
front-load all generation across a corpus and apply later or on a peer machine.

Before the freshness check, each unit runs an ordered **gate sequence** (the
source pipeline's auto-marker / single-speaker / speaker-policy / missing-data
gates). A gate returns a reason to stop or ``None`` to pass; the first firing
gate short-circuits. A gate flagged ``sticky`` records the unit as unsupported
via the caller's registry hook (see ``cli.unsupported``) so a bulk run drops
it next pass instead of re-paying to rediscover a structural failure every run
-- the sticky-unsupported-stub concept. A non-sticky gate is a transient skip,
re-evaluated every run.

Deterministic seeding: :func:`seed_for` derives a per-unit RNG seed from the
unit id via ``freshness.seed``, so any stochastic decision inside a stage rolls
the same way every run -- the invariant that keeps stochastic gating from
perpetually invalidating a freshness hash.

Deviation from the skeleton: the placeholder ``run(store, stages)`` is kept as
a thin stage-fold (delegating to ``stage.compose``) for the simple case, but
the load-bearing entry point is :func:`run_single_pass`, which drives the
two-phase generate/apply with gates over an iterable of ``WorkUnit`` rather
than folding opaque stages -- the shape a single-pass consumer actually ports
onto.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, List, Optional, Sequence

from content_pipeline.freshness import seed as _seed
from content_pipeline.freshness.classify import FreshnessState, needs_generation
from content_pipeline.pipeline import stage as _stage
from content_pipeline.pipeline.workunit import WorkUnit


def run(store: Any, stages: Sequence) -> Any:
    """Fold ``stages`` over ``store`` once (the simple stage-composition case).

    Kept for the skeleton's signature; delegates to
    :func:`content_pipeline.pipeline.stage.compose`. The richer two-phase
    generate/apply entry point is :func:`run_single_pass`.
    """
    return _stage.compose(store, list(stages))


def seed_for(unit_id: str, *, salt: str = "") -> int:
    """Deterministic per-unit RNG seed derived from the unit id.

    A thin pass-through to ``freshness.seed.deterministic_seed`` so a stage
    that makes a stochastic decision seeds it from stable identity, not
    run-local entropy -- the same roll every run.
    """
    return _seed.deterministic_seed(unit_id, salt)


class Disposition(str, Enum):
    """What the pipeline decided for one unit."""

    GENERATED = "generated"  # a candidate was produced (and applied unless dry-run)
    SKIPPED = "skipped"  # a non-sticky gate fired; re-evaluated next run
    UNSUPPORTED = "unsupported"  # a sticky gate fired; recorded, dropped next run
    UP_TO_DATE = "up_to_date"  # freshness said no generation needed
    ERROR = "error"  # generate/apply raised


@dataclass(frozen=True)
class Gate:
    """One ordered pre-generation gate.

    - ``name`` -- diagnostic label (surfaced on the outcome).
    - ``predicate`` -- ``WorkUnit -> Optional[str]``: a reason string stops the
      unit; ``None`` passes it to the next gate.
    - ``sticky`` -- when True, a firing gate marks the unit unsupported (a
      structural "this pipeline cannot handle this shape" verdict) rather than
      a transient skip.
    """

    name: str
    predicate: Callable[[WorkUnit], Optional[str]]
    sticky: bool = False


@dataclass
class UnitOutcome:
    """The result of processing one unit through the two-phase pipeline."""

    unit_id: str
    disposition: Disposition
    state: Optional[FreshnessState] = None
    gate: Optional[str] = None  # the gate that fired (skip / unsupported)
    reason: str = ""
    payload: Any = None  # the generated candidate (generate phase output)
    applied: bool = False
    error: Optional[str] = None


def run_gates(gates: Sequence[Gate], unit: WorkUnit) -> Optional[tuple]:
    """Run ``gates`` in order; return the first ``(Gate, reason)`` that fires.

    Returns ``None`` when every gate passes -- the unit proceeds to the
    freshness check. First-firing gate wins (order is significant: the source
    pipeline runs its override marker before its structural checks so the
    override reason wins when both apply).
    """
    for gate in gates:
        reason = gate.predicate(unit)
        if reason is not None:
            return gate, reason
    return None


def run_single_pass(
    units: Sequence[WorkUnit],
    *,
    freshness_of: Callable[[WorkUnit], FreshnessState],
    generate: Callable[[WorkUnit], Any],
    apply: Optional[Callable[[WorkUnit, Any], None]] = None,
    gates: Sequence[Gate] = (),
    include_stale: bool = True,
    dry_run: bool = False,
    mark_unsupported: Optional[Callable[[str, str], None]] = None,
) -> List[UnitOutcome]:
    """Two-phase generate/apply over ``units``, with an ordered gate sequence.

    For each unit, in order:

    1. **Gates** -- run :func:`run_gates`. A sticky gate records the unit via
       ``mark_unsupported(unit.id, reason)`` (when supplied) and yields an
       ``UNSUPPORTED`` outcome; a non-sticky gate yields ``SKIPPED``. Either
       short-circuits (no freshness check, no generation).
    2. **Freshness** -- classify via ``freshness_of``. When
       ``needs_generation`` (``MISSING`` always; ``STALE`` when
       ``include_stale``) is False, the unit is ``UP_TO_DATE``.
    3. **Generate** -- call ``generate(unit)`` to produce a candidate (skipped
       under ``dry_run``; the outcome still reports ``GENERATED`` with the
       decision visible and ``applied=False``).
    4. **Apply** -- when ``apply`` is given and not ``dry_run``, write the
       candidate via ``apply(unit, payload)`` (the ``deliver`` seam).

    ``generate`` / ``apply`` exceptions are caught per unit and surfaced as an
    ``ERROR`` outcome so one bad unit never aborts the sweep -- the caller's
    bulk driver (see ``cli.bulk`` / ``cli.budget``) decides whether a given
    error class should halt. A generate that returns ``None`` is treated as
    "nothing to apply" and no apply runs.
    """
    outcomes: List[UnitOutcome] = []
    for unit in units:
        fired = run_gates(gates, unit)
        if fired is not None:
            gate, reason = fired
            if gate.sticky:
                if mark_unsupported is not None and not dry_run:
                    mark_unsupported(unit.id, reason)
                outcomes.append(
                    UnitOutcome(
                        unit_id=unit.id,
                        disposition=Disposition.UNSUPPORTED,
                        gate=gate.name,
                        reason=reason,
                    )
                )
            else:
                outcomes.append(
                    UnitOutcome(
                        unit_id=unit.id,
                        disposition=Disposition.SKIPPED,
                        gate=gate.name,
                        reason=reason,
                    )
                )
            continue

        state = freshness_of(unit)
        if not needs_generation(state, include_stale=include_stale):
            outcomes.append(
                UnitOutcome(
                    unit_id=unit.id,
                    disposition=Disposition.UP_TO_DATE,
                    state=state,
                )
            )
            continue

        if dry_run:
            outcomes.append(
                UnitOutcome(
                    unit_id=unit.id,
                    disposition=Disposition.GENERATED,
                    state=state,
                    applied=False,
                )
            )
            continue

        try:
            payload = generate(unit)
        except Exception as exc:  # noqa: BLE001 -- isolate per unit; caller halts
            outcomes.append(
                UnitOutcome(
                    unit_id=unit.id,
                    disposition=Disposition.ERROR,
                    state=state,
                    error=str(exc),
                )
            )
            continue

        applied = False
        if payload is not None and apply is not None:
            try:
                apply(unit, payload)
                applied = True
            except Exception as exc:  # noqa: BLE001 -- isolate per unit
                outcomes.append(
                    UnitOutcome(
                        unit_id=unit.id,
                        disposition=Disposition.ERROR,
                        state=state,
                        payload=payload,
                        error=str(exc),
                    )
                )
                continue

        outcomes.append(
            UnitOutcome(
                unit_id=unit.id,
                disposition=Disposition.GENERATED,
                state=state,
                payload=payload,
                applied=applied,
            )
        )
    return outcomes


__all__ = [
    "run",
    "seed_for",
    "Disposition",
    "Gate",
    "UnitOutcome",
    "run_gates",
    "run_single_pass",
]
