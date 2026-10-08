"""A dependency-graph work-unit strategy that runs independent units in parallel.

:class:`ParallelGraphStrategy` names each unit's direct dependencies and a
``capacity``. Against it, :func:`~content_pipeline.execution.wave.ready_wave`
releases every unit whose dependencies are all settled, up to ``capacity``
units in flight at once, instead of the one-at-a-time ordinal chain a
:class:`~content_pipeline.pipeline.workunit.GraphWalkStrategy` gets:

- A dependency is SETTLED when it is ``SKIPPED``, or ``ACCEPTED`` and applied
  (its last apply-kind attempt is ``APPLY_SUCCEEDED``) -- the same rule the
  sequential graph applies to its predecessor. A drain loop therefore still
  interleaves ``finalize_run`` between waves (see ``execution.wave``).
- A dependency is BROKEN when it is ``FAILED``, ``OPERATOR_REJECTED``,
  ``INTERRUPT_EXPIRED``, or ``ACCEPTED`` with its apply refused. Every
  transitive dependent of a broken unit is GATED: never released, reported by
  :func:`~content_pipeline.execution.wave.gated_units`. Unrelated units keep
  running.
- In flight means ``CLAIMED`` behind a live lease. With ``reclaim_at``, a
  claim whose lease expired counts as ready rather than in flight.
- Release order is registration (ordinal) order, with ``deferred`` units after
  every ready unit that is not deferred.

The set of registered unit ids must equal the graph's node ids; anything else
raises :class:`~content_pipeline.execution.wave.GraphRegistrationMismatchError`.
``node_ids`` gives an order to register them in.

The graph is validated at construction (unknown, duplicate and self
dependencies, cycles) by
:func:`~content_pipeline.execution.scheduler.validate_graph`, the rule the
in-memory :class:`~content_pipeline.execution.scheduler.Scheduler` shares.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import AbstractSet, Any, Callable, List, Mapping, Optional, Sequence, Tuple

from content_pipeline.execution.scheduler import validate_graph
from content_pipeline.pipeline.workunit import WorkUnit


@dataclass(frozen=True)
class ParallelGraphStrategy:
    """Units with explicit direct dependencies, run up to ``capacity`` at once.

    - ``dependencies`` -- unit id -> its direct dependency ids. Every unit of
      the run is a key, including units with no dependencies.
    - ``capacity`` -- the most units in flight at once (``>= 1``).
    - ``payload_of`` -- optional ``(source, unit_id) -> payload`` for
      :meth:`units`.
    - ``deferred`` -- unit ids released only after every ready unit that is
      not deferred (work expected to be slow, such as a unit whose last
      attempt hit its deadline).
    """

    dependencies: Mapping[str, Sequence[str]]
    capacity: int = 1
    payload_of: Optional[Callable[[Any, str], Any]] = None
    deferred: AbstractSet[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        graph = {
            str(node_id): tuple(str(dep) for dep in deps)
            for node_id, deps in self.dependencies.items()
        }
        if len(graph) != len(self.dependencies):
            raise ValueError("unit ids collide after str()")
        validate_graph(graph)
        if self.capacity < 1:
            raise ValueError("capacity must be positive")
        deferred = frozenset(str(node_id) for node_id in self.deferred)
        unknown = sorted(deferred - set(graph))
        if unknown:
            raise ValueError(f"deferred names units outside the graph: {unknown!r}")
        object.__setattr__(self, "dependencies", MappingProxyType(graph))
        object.__setattr__(self, "deferred", deferred)

    @property
    def node_ids(self) -> Tuple[str, ...]:
        """Every unit id, in the order ``dependencies`` gave them."""
        return tuple(self.dependencies)

    def units(self, source: Any) -> List[WorkUnit]:
        """One :class:`WorkUnit` per node; ``context`` carries its dependencies."""
        return [
            WorkUnit(
                id=node_id,
                payload=self.payload_of(source, node_id) if self.payload_of else None,
                context={"dependencies": list(deps)},
            )
            for node_id, deps in self.dependencies.items()
        ]


def is_parallel_graph_strategy(strategy: Any) -> bool:
    return isinstance(strategy, ParallelGraphStrategy)


__all__ = ["ParallelGraphStrategy", "is_parallel_graph_strategy"]
