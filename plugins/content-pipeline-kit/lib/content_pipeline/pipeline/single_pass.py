"""Stage-fold entry point, per-unit seeding, and the gate re-exports.

``run(store, stages)`` folds opaque stages over a store once (delegating to
``stage.compose``). :func:`seed_for` derives a per-unit RNG seed from the unit
id via ``freshness.seed``, so any stochastic decision inside a stage rolls the
same way every run -- the invariant that keeps stochastic gating from
perpetually invalidating a freshness hash.

``Gate`` and ``run_gates`` live in ``pipeline.gate`` and are re-exported here,
so ``from content_pipeline.pipeline.single_pass import Gate, run_gates`` keeps
working. The untracked ``run_single_pass`` loop is removed; the tracked path is
``execution.controller.prepare_run`` + ``execution.drivers.inline.run_wave`` +
``execution.controller.finalize_run``.
"""

from __future__ import annotations

from typing import Any, Sequence

from content_pipeline.freshness import seed as _seed
from content_pipeline.pipeline import stage as _stage
from content_pipeline.pipeline.gate import Gate, run_gates


def run(store: Any, stages: Sequence) -> Any:
    """Fold ``stages`` over ``store`` once (the simple stage-composition case).

    Delegates to :func:`content_pipeline.pipeline.stage.compose`.
    """
    return _stage.compose(store, list(stages))


def seed_for(unit_id: str, *, salt: str = "") -> int:
    """Deterministic per-unit RNG seed derived from the unit id.

    A thin pass-through to ``freshness.seed.deterministic_seed`` so a stage
    that makes a stochastic decision seeds it from stable identity, not
    run-local entropy -- the same roll every run.
    """
    return _seed.deterministic_seed(unit_id, salt)


__all__ = [
    "run",
    "seed_for",
    "Gate",
    "run_gates",
]
