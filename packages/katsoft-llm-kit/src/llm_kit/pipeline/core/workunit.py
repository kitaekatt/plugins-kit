"""Pluggable work-unit strategy: graph-walk vs. flat-chunk.

A pipeline needs to decide what "one unit of work" is before it can iterate.
A graph-walk strategy treats work units as nodes in a dependency or cadence
graph (structural adjacency matters -- e.g. an animate/hold cadence, where a
unit's context depends on the units walked before it). A flat-chunk strategy
treats work units as an unordered, independently processable list, optionally
split into fixed-size batches. Both strategies expose the same ``units``
iteration interface so ``single_pass`` and ``convergence_loop`` never need to
know which one a given pipeline chose.

Both are pure functions over the store; every reach into the store's shape is a
caller-supplied callable, so this module carries zero domain vocabulary and no
knowledge of what a "unit" means to a given consumer.

Generalized from two source techniques:

- The graph-walker's ordered traversal with per-item context (each node
  carries context accumulated from its predecessors) is the *shape* lifted
  here; the domain cadence rules (which node animates, which holds) stay
  project-side in the caller's ``context_of`` callable.
- The flat chunking is the localization seeding shape: an unordered list of
  independently processable items, batched for a bulk worker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, List, Mapping, Optional, Protocol, Sequence


@dataclass(frozen=True)
class WorkUnit:
    """One unit of work a pipeline shape iterates over.

    - ``id`` -- stable identity (used for deterministic seeding, freshness
      keying, and audit trails). Non-empty by convention.
    - ``payload`` -- the opaque per-unit data the caller's stages consume.
    - ``context`` -- per-unit context a strategy attached at walk time (e.g.
      predecessor state for a graph walk). Empty for the flat-chunk shape.
    """

    id: str
    payload: Any = None
    context: Mapping[str, Any] = field(default_factory=dict)


class WorkUnitStrategy(Protocol):
    """Turns a store into an ordered list of :class:`WorkUnit`."""

    def units(self, store: Any) -> List[WorkUnit]:
        ...


@dataclass(frozen=True)
class FlatChunkStrategy:
    """Unordered, independently-processable units, optionally batched.

    ``select`` maps the store to an iterable of ``(id, payload)`` pairs -- the
    flat work-unit shape. ``chunk_size`` (0 == one chunk of everything) drives
    :meth:`chunks`, the batching a bulk worker consumes. Order is the order
    ``select`` yields; nothing here reorders, because a flat strategy asserts
    the units are independent.
    """

    select: Callable[[Any], Sequence]
    chunk_size: int = 0

    def units(self, store: Any) -> List[WorkUnit]:
        """Return every unit as a flat list, in ``select`` order."""
        return [
            WorkUnit(id=str(uid), payload=payload)
            for uid, payload in self.select(store)
        ]

    def chunks(self, store: Any) -> List[List[WorkUnit]]:
        """Return the units split into ``chunk_size`` batches.

        ``chunk_size <= 0`` yields a single chunk (or no chunks when there is
        no work); otherwise the units are sliced into contiguous batches of at
        most ``chunk_size``.
        """
        units = self.units(store)
        if self.chunk_size <= 0:
            return [units] if units else []
        return [
            units[i : i + self.chunk_size]
            for i in range(0, len(units), self.chunk_size)
        ]


@dataclass(frozen=True)
class GraphWalkStrategy:
    """Ordered traversal where each unit carries per-predecessor context.

    - ``order`` -- store -> ordered sequence of node ids. The caller owns the
      traversal order (topological, cadence-driven, whatever); this strategy
      only guarantees each node is visited once, in that order.
    - ``payload_of`` -- optional ``(store, node_id) -> payload``.
    - ``context_of`` -- optional ``(store, node_id, walked_so_far) -> mapping``.
      Receives the units already walked (in order) so a node's context can
      depend on its predecessors -- the structural-adjacency property the
      flat shape deliberately lacks. When ``None``, ``predecessors_of`` (if
      given) populates a ``{"predecessors": [...]}`` context instead.
    - ``predecessors_of`` -- optional ``(store, node_id) -> sequence`` used
      only when ``context_of`` is absent, a convenience for the common
      "context is just my predecessor ids" case.
    """

    order: Callable[[Any], Sequence]
    payload_of: Optional[Callable[[Any, str], Any]] = None
    context_of: Optional[Callable[[Any, str, List[WorkUnit]], Mapping[str, Any]]] = None
    predecessors_of: Optional[Callable[[Any, str], Sequence]] = None

    def units(self, store: Any) -> List[WorkUnit]:
        """Walk ``order`` once, attaching per-node payload and context."""
        walked: List[WorkUnit] = []
        for raw_id in self.order(store):
            node_id = str(raw_id)
            payload = self.payload_of(store, node_id) if self.payload_of else None
            if self.context_of is not None:
                context = dict(self.context_of(store, node_id, walked))
            elif self.predecessors_of is not None:
                context = {"predecessors": list(self.predecessors_of(store, node_id))}
            else:
                context = {}
            walked.append(WorkUnit(id=node_id, payload=payload, context=context))
        return walked


__all__ = [
    "WorkUnit",
    "WorkUnitStrategy",
    "FlatChunkStrategy",
    "GraphWalkStrategy",
]
