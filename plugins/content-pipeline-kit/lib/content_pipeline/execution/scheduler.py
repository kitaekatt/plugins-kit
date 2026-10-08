"""A bounded, in-memory dependency-graph scheduler. Standard library only.

:class:`Scheduler` admits ready nodes of a DAG up to a fixed concurrent
``capacity``. A node is ready when every direct dependency has completed. A
failed node gates its transitive dependents and nothing else: unrelated nodes
keep running. The scheduler holds no domain, store or transport concept; a
node is an id (any hashable, usually a string), its direct dependency ids,
and a ``deferred`` flag.

Ordering is FIFO in the order the nodes were given, with one priority class:
a ready ``deferred`` node is admitted only after every ready node that is not
deferred. A consumer uses it to push work it expects to be slow (a node whose
last attempt hit its deadline, say) behind work it expects to finish.

:func:`run_graph` drives a scheduler through a thread pool: it admits nodes
as slots free (no wave barrier), completes a node whose executor returns,
and fails one whose executor raises :class:`NodeFailed`. Any other exception
stops admission, waits for the nodes already running, and is re-raised.

The graph is validated when it is built: a duplicate node, a duplicate or
unknown dependency, a self-dependency or a cycle raises
:class:`GraphError` (a ``ValueError``). A cycle would otherwise leave its
nodes pending forever with nothing running.

The scheduler is not thread-safe. :func:`run_graph` calls it only from its
own scheduling thread; a consumer driving it by hand does the same.

The durable-store counterpart is
:class:`~content_pipeline.execution.parallel_graph.ParallelGraphStrategy`,
which :func:`~content_pipeline.execution.wave.ready_wave` schedules with the
same readiness and gating rule over units recorded in an ``ExecutionStore``.
:func:`validate_graph` and :func:`dependents_closure` are the rule both share.
"""

from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    Hashable,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

NodeId = Hashable


class GraphError(ValueError):
    """The dependency graph is malformed: duplicate, unknown, self or cyclic edges."""


class NodeFailed(Exception):
    """Raised by a :func:`run_graph` executor to fail its node.

    The node is recorded failed and its transitive dependents are gated. The
    exception is kept in :attr:`GraphRun.failures`. Any other exception from
    an executor is an error of the run, not of the node, and is re-raised.
    """


@dataclass(frozen=True)
class SchedulerNode:
    """One DAG node: its id, its direct dependency ids, and its priority class."""

    node_id: NodeId
    dependencies: Tuple[NodeId, ...] = ()
    deferred: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "dependencies", tuple(self.dependencies))
        if len(set(self.dependencies)) != len(self.dependencies):
            raise GraphError(f"duplicate dependency on node {self.node_id!r}")
        if self.node_id in self.dependencies:
            raise GraphError(f"node {self.node_id!r} depends on itself")


@dataclass(frozen=True)
class SchedulerState:
    """Immutable view of the scheduler after an event."""

    pending: Tuple[NodeId, ...] = ()
    running: Tuple[NodeId, ...] = ()
    completed: Tuple[NodeId, ...] = ()
    failed: Tuple[NodeId, ...] = ()
    gated: Tuple[NodeId, ...] = ()

    @property
    def finished(self) -> bool:
        """True when nothing is pending or running."""
        return not self.pending and not self.running


@dataclass(frozen=True)
class SchedulerResult:
    """The event a state change recorded and the state after it."""

    event: str  # "complete" | "fail" | "requeue"
    node_id: NodeId
    state: SchedulerState


def validate_graph(dependencies: Mapping[NodeId, Sequence[NodeId]]) -> None:
    """Raise :class:`GraphError` unless ``dependencies`` is a DAG over its own keys.

    ``dependencies`` maps every node id to its direct dependency ids. Every
    dependency must itself be a key; no node may list a dependency twice or
    depend on itself; and there may be no cycle.
    """
    for node_id, deps in dependencies.items():
        deps = tuple(deps)
        if len(set(deps)) != len(deps):
            raise GraphError(f"duplicate dependency on node {node_id!r}")
        if node_id in deps:
            raise GraphError(f"node {node_id!r} depends on itself")
        unknown = [d for d in deps if d not in dependencies]
        if unknown:
            raise GraphError(f"node {node_id!r} has unknown dependencies: {unknown!r}")
    # Kahn's algorithm: whatever cannot be peeled off sits on a cycle.
    remaining = {node_id: len(tuple(deps)) for node_id, deps in dependencies.items()}
    dependents = _dependents_index(dependencies)
    ready = [node_id for node_id, count in remaining.items() if count == 0]
    while ready:
        node_id = ready.pop()
        del remaining[node_id]
        for dependent in dependents.get(node_id, ()):
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                ready.append(dependent)
    if remaining:
        raise GraphError(f"dependency cycle among nodes: {sorted(map(repr, remaining))}")


def dependents_closure(
    dependencies: Mapping[NodeId, Sequence[NodeId]], roots: Iterable[NodeId]
) -> Set[NodeId]:
    """Every node that depends, directly or transitively, on any of ``roots``.

    The roots themselves are not included unless one depends on another.
    """
    dependents = _dependents_index(dependencies)
    found: Set[NodeId] = set()
    stack = list(roots)
    while stack:
        for dependent in dependents.get(stack.pop(), ()):
            if dependent not in found:
                found.add(dependent)
                stack.append(dependent)
    return found


def _dependents_index(
    dependencies: Mapping[NodeId, Sequence[NodeId]]
) -> Dict[NodeId, List[NodeId]]:
    index: Dict[NodeId, List[NodeId]] = {}
    for node_id, deps in dependencies.items():
        for dep in deps:
            index.setdefault(dep, []).append(node_id)
    return index


class Scheduler:
    """Admit ready DAG nodes up to a fixed concurrent capacity."""

    def __init__(self, nodes: Sequence[SchedulerNode] = (), *, capacity: int = 1) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._nodes: Dict[NodeId, SchedulerNode] = {}
        for node in nodes:
            if node.node_id in self._nodes:
                raise GraphError(f"duplicate scheduler node {node.node_id!r}")
            self._nodes[node.node_id] = node
        self._graph = {node_id: node.dependencies for node_id, node in self._nodes.items()}
        validate_graph(self._graph)
        self._queue: List[NodeId] = list(self._nodes)
        self._running: List[NodeId] = []
        self._completed: List[NodeId] = []
        self._failed: List[NodeId] = []
        self._gated: List[NodeId] = []
        self._quiet_until: Dict[NodeId, float] = {}

    @property
    def state(self) -> SchedulerState:
        return SchedulerState(
            pending=tuple(self._queue),
            running=tuple(self._running),
            completed=tuple(self._completed),
            failed=tuple(self._failed),
            gated=tuple(self._gated),
        )

    def node(self, node_id: NodeId) -> SchedulerNode:
        return self._nodes[node_id]

    def admit_ready(self, *, now: Optional[float] = None) -> Tuple[SchedulerNode, ...]:
        """Move ready nodes to running, up to the free capacity, and return them.

        FIFO within each priority class; deferred nodes follow the others.
        """
        current = time.monotonic() if now is None else now
        slots = self.capacity - len(self._running)
        if slots <= 0:
            return ()
        ready = [node_id for node_id in self._queue if self._is_ready(node_id, current)]
        ready.sort(key=lambda node_id: 1 if self._nodes[node_id].deferred else 0)
        admitted: List[SchedulerNode] = []
        for node_id in ready[:slots]:
            self._queue.remove(node_id)
            self._running.append(node_id)
            self._quiet_until.pop(node_id, None)
            admitted.append(self._nodes[node_id])
        return tuple(admitted)

    def next_wakeup(self) -> Optional[float]:
        """The earliest end of a pending node's quiet period, or ``None``."""
        times = [self._quiet_until[n] for n in self._queue if n in self._quiet_until]
        return min(times) if times else None

    def complete(self, node_id: NodeId) -> SchedulerResult:
        """Mark a running node successful; its dependents may become ready."""
        self._require_running(node_id)
        self._running.remove(node_id)
        self._completed.append(node_id)
        return SchedulerResult("complete", node_id, self.state)

    succeed = complete

    def fail(self, node_id: NodeId) -> SchedulerResult:
        """Fail a running or pending node and gate only its transitive dependents."""
        if node_id in self._running:
            self._running.remove(node_id)
        elif node_id in self._queue:
            self._queue.remove(node_id)
        else:
            raise ValueError(f"node is not active: {node_id!r}")
        self._quiet_until.pop(node_id, None)
        self._failed.append(node_id)
        blocked = dependents_closure(self._graph, [node_id])
        settled = set(self._completed) | set(self._failed) | set(self._gated)
        newly = [n for n in self._nodes if n in blocked and n not in settled]
        # A dependent cannot be running: it is admitted only after this node
        # completed, and a completed node cannot fail.
        for gated in newly:
            self._queue.remove(gated)
            self._quiet_until.pop(gated, None)
        self._gated.extend(newly)
        return SchedulerResult("fail", node_id, self.state)

    def requeue(
        self, node_id: NodeId, *, quiet_period: float = 0.0, now: Optional[float] = None
    ) -> SchedulerResult:
        """Return a running node to the queue tail, not ready for ``quiet_period``."""
        self._require_running(node_id)
        if quiet_period < 0:
            raise ValueError("quiet_period must not be negative")
        self._running.remove(node_id)
        self._queue.append(node_id)
        self._quiet_until[node_id] = (time.monotonic() if now is None else now) + quiet_period
        return SchedulerResult("requeue", node_id, self.state)

    def _is_ready(self, node_id: NodeId, now: float) -> bool:
        return self._quiet_until.get(node_id, 0.0) <= now and all(
            dep in self._completed for dep in self._nodes[node_id].dependencies
        )

    def _require_running(self, node_id: NodeId) -> None:
        if node_id not in self._running:
            raise ValueError(f"node is not running: {node_id!r}")


@dataclass(frozen=True)
class GraphRun:
    """What :func:`run_graph` returns: the final state, and each node's outcome."""

    state: SchedulerState
    results: Dict[NodeId, Any] = field(default_factory=dict)
    failures: Dict[NodeId, NodeFailed] = field(default_factory=dict)


def run_graph(
    scheduler: Scheduler,
    execute: Callable[[SchedulerNode], Any],
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> GraphRun:
    """Run every node of ``scheduler`` through ``execute`` on a thread pool.

    Up to ``scheduler.capacity`` nodes run at once, and a node is admitted as
    soon as a slot frees and its dependencies have completed. ``execute``
    returning completes the node (its return value lands in
    :attr:`GraphRun.results`); raising :class:`NodeFailed` fails it and gates
    its transitive dependents. Any other exception stops admission, waits for
    the running nodes to end, and is re-raised.
    """
    results: Dict[NodeId, Any] = {}
    failures: Dict[NodeId, NodeFailed] = {}
    error: Optional[BaseException] = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=scheduler.capacity) as pool:
        running: Dict[concurrent.futures.Future, NodeId] = {}
        while True:
            if error is None:
                for node in scheduler.admit_ready(now=clock()):
                    running[pool.submit(execute, node)] = node.node_id
            if not running:
                if error is not None or not scheduler.state.pending:
                    break
                wakeup = scheduler.next_wakeup()
                if wakeup is None:
                    raise RuntimeError(
                        "scheduler has pending nodes but none can become ready"
                    )
                sleep(max(0.0, wakeup - clock()))
                continue
            done, _ = concurrent.futures.wait(
                running, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in done:
                node_id = running.pop(future)
                exc = future.exception()
                if exc is None:
                    results[node_id] = future.result()
                    scheduler.complete(node_id)
                elif isinstance(exc, NodeFailed):
                    failures[node_id] = exc
                    scheduler.fail(node_id)
                elif error is None:
                    error = exc
                    scheduler.fail(node_id)
                else:
                    scheduler.fail(node_id)
    if error is not None:
        raise error
    return GraphRun(scheduler.state, results, failures)


__all__ = [
    "GraphError",
    "NodeFailed",
    "SchedulerNode",
    "SchedulerState",
    "SchedulerResult",
    "Scheduler",
    "GraphRun",
    "validate_graph",
    "dependents_closure",
    "run_graph",
]
