"""The parallel dependency graph: the in-memory Scheduler, run_graph, and
ParallelGraphStrategy scheduled by ready_wave over the durable store.

The Scheduler cases are generalized from a consumer's tested bounded DAG
scheduler; node ids here are plain strings with no domain meaning.
"""

from __future__ import annotations

import ast
import sys
import threading
from pathlib import Path

import pytest

from content_pipeline.execution.controller import prepare_run, unfinished_units
from content_pipeline.execution.model import UnitState
from content_pipeline.execution.parallel_graph import (
    ParallelGraphStrategy,
    is_parallel_graph_strategy,
)
from content_pipeline.execution.scheduler import (
    GraphError,
    NodeFailed,
    Scheduler,
    SchedulerNode,
    dependents_closure,
    run_graph,
    validate_graph,
)
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.execution.wave import (
    GraphRegistrationMismatchError,
    UnsafeGraphParallelismError,
    gated_units,
    is_dependency_ordered,
    is_graph_strategy,
    ready_wave,
)
from content_pipeline.pipeline.workunit import FlatChunkStrategy, GraphWalkStrategy

EXECUTION = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "content-pipeline-kit"
    / "lib"
    / "content_pipeline"
    / "execution"
)


def node(node_id, dependencies=(), deferred=False):
    return SchedulerNode(node_id, tuple(dependencies), deferred)


def ids(nodes):
    return [n.node_id for n in nodes]


# -- Scheduler ---------------------------------------------------------------


def test_ready_order_is_fifo():
    scheduler = Scheduler([node("a"), node("b")], capacity=2)
    assert ids(scheduler.admit_ready()) == ["a", "b"]


def test_failed_node_gates_only_transitive_dependents():
    scheduler = Scheduler(
        [node("root"), node("child", ["root"]), node("grandchild", ["child"]), node("other")],
        capacity=2,
    )
    scheduler.admit_ready()

    scheduler.fail("root")
    state = scheduler.state

    assert state.gated == ("child", "grandchild")
    assert "other" in state.running
    assert state.failed == ("root",)


def test_second_failed_dependency_does_not_duplicate_gated_node():
    scheduler = Scheduler([node("a"), node("b"), node("c", ["a", "b"])], capacity=2)
    scheduler.admit_ready()

    scheduler.fail("a")
    scheduler.fail("b")

    assert scheduler.state.gated == ("c",)


def test_a_pending_node_can_fail_and_a_settled_one_cannot():
    scheduler = Scheduler([node("a"), node("b", ["a"])], capacity=1)
    scheduler.fail("a")
    assert scheduler.state.gated == ("b",)
    with pytest.raises(ValueError):
        scheduler.fail("a")
    with pytest.raises(ValueError):
        scheduler.fail("b")


def test_requeued_node_moves_to_tail_and_dependents_wait():
    scheduler = Scheduler([node("first"), node("dependent", ["first"]), node("second")], capacity=1)

    assert ids(scheduler.admit_ready()) == ["first"]
    scheduler.requeue("first")
    assert ids(scheduler.admit_ready()) == ["second"]
    scheduler.complete("second")
    assert ids(scheduler.admit_ready()) == ["first"]
    scheduler.complete("first")
    assert ids(scheduler.admit_ready()) == ["dependent"]


def test_requeue_quiet_period_holds_the_node_until_it_ends():
    scheduler = Scheduler([node("a")], capacity=1)
    scheduler.admit_ready(now=0.0)
    scheduler.requeue("a", quiet_period=5.0, now=0.0)

    assert scheduler.admit_ready(now=4.0) == ()
    assert scheduler.next_wakeup() == 5.0
    assert ids(scheduler.admit_ready(now=5.0)) == ["a"]


def test_deferred_ready_node_follows_ordinary_ready_nodes():
    scheduler = Scheduler([node("noted", deferred=True), node("ordinary")], capacity=1)

    assert ids(scheduler.admit_ready()) == ["ordinary"]
    scheduler.complete("ordinary")
    assert ids(scheduler.admit_ready()) == ["noted"]


def test_completed_dependency_releases_its_dependent():
    scheduler = Scheduler([node("parent"), node("child", ["parent"])], capacity=2)
    assert ids(scheduler.admit_ready()) == ["parent"]
    assert scheduler.admit_ready() == ()
    scheduler.complete("parent")
    assert ids(scheduler.admit_ready()) == ["child"]


def test_scheduler_never_exceeds_capacity():
    scheduler = Scheduler([node("a"), node("b"), node("c")], capacity=2)

    assert len(scheduler.admit_ready()) == 2
    assert scheduler.admit_ready() == ()
    scheduler.complete(scheduler.state.running[0])
    assert len(scheduler.admit_ready()) == 1
    assert len(scheduler.state.running) == 2


def test_failed_leaf_does_not_stop_unrelated_nodes():
    scheduler = Scheduler(
        [node("left"), node("top", ["left"]), node("right"), node("elsewhere")], capacity=3
    )
    scheduler.admit_ready()
    scheduler.fail("left")
    assert scheduler.state.gated == ("top",)
    assert set(scheduler.state.running) == {"right", "elsewhere"}


def test_requeue_preserves_dependency_order():
    scheduler = Scheduler([node("child"), node("parent", ["child"]), node("other")], capacity=1)
    assert ids(scheduler.admit_ready()) == ["child"]
    scheduler.requeue("child")
    assert ids(scheduler.admit_ready()) == ["other"]
    scheduler.complete("other")
    assert ids(scheduler.admit_ready()) == ["child"]
    scheduler.complete("child")
    assert ids(scheduler.admit_ready()) == ["parent"]


@pytest.mark.parametrize(
    "nodes",
    [
        [node("a"), node("a")],
        [node("a", ["missing"])],
        [node("a", ["b"]), node("b", ["a"])],
        [node("a", ["c"]), node("b", ["a"]), node("c", ["b"])],
    ],
    ids=["duplicate-node", "unknown-dependency", "two-cycle", "three-cycle"],
)
def test_malformed_graph_is_refused_at_construction(nodes):
    with pytest.raises(GraphError):
        Scheduler(nodes)


def test_node_refuses_duplicate_and_self_dependencies():
    with pytest.raises(GraphError):
        node("a", ["b", "b"])
    with pytest.raises(GraphError):
        node("a", ["a"])


def test_capacity_must_be_positive():
    with pytest.raises(ValueError):
        Scheduler([node("a")], capacity=0)


def test_validate_graph_and_closure_are_the_shared_rule():
    graph = {"a": [], "b": ["a"], "c": ["b"], "d": []}
    validate_graph(graph)
    assert dependents_closure(graph, ["a"]) == {"b", "c"}
    assert dependents_closure(graph, ["d"]) == set()


# -- run_graph ---------------------------------------------------------------


def test_run_graph_runs_independent_nodes_concurrently_within_capacity():
    lock = threading.Lock()
    active = 0
    peak = 0
    barrier = threading.Barrier(2, timeout=10)

    def execute(n):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait()  # breaks (and errors the run) unless two run at once
        with lock:
            active -= 1
        return n.node_id.upper()

    scheduler = Scheduler([node(x) for x in "abcd"], capacity=2)
    run = run_graph(scheduler, execute)

    assert peak == 2
    assert run.results == {"a": "A", "b": "B", "c": "C", "d": "D"}
    assert set(run.state.completed) == set("abcd")
    assert run.state.finished


def test_run_graph_starts_a_node_only_after_its_dependencies_complete():
    order = []
    lock = threading.Lock()

    def execute(n):
        with lock:
            order.append(("start", n.node_id))
        with lock:
            order.append(("end", n.node_id))

    scheduler = Scheduler(
        [node("leaf-1"), node("leaf-2"), node("top", ["leaf-1", "leaf-2"])], capacity=3
    )
    run_graph(scheduler, execute)

    assert order.index(("start", "top")) > order.index(("end", "leaf-1"))
    assert order.index(("start", "top")) > order.index(("end", "leaf-2"))


def test_run_graph_node_failure_gates_dependents_and_others_finish():
    executed = []

    def execute(n):
        executed.append(n.node_id)
        if n.node_id == "bad":
            raise NodeFailed("deterministic failure")

    scheduler = Scheduler(
        [node("bad"), node("needs-bad", ["bad"]), node("fine"), node("needs-fine", ["fine"])],
        capacity=1,
    )
    run = run_graph(scheduler, execute)

    assert "needs-bad" not in executed
    assert run.state.gated == ("needs-bad",)
    assert run.state.failed == ("bad",)
    assert set(run.state.completed) == {"fine", "needs-fine"}
    assert str(run.failures["bad"]) == "deterministic failure"


def test_run_graph_reraises_an_unexpected_error_and_admits_nothing_more():
    executed = []

    def execute(n):
        executed.append(n.node_id)
        if n.node_id == "boom":
            raise KeyError("not a node failure")

    scheduler = Scheduler([node("boom"), node("later")], capacity=1)
    with pytest.raises(KeyError):
        run_graph(scheduler, execute)
    assert executed == ["boom"]


def test_run_graph_waits_for_running_nodes_before_reraising():
    boom_started = threading.Event()
    finished = []

    def execute(n):
        if n.node_id == "boom":
            boom_started.set()
            raise KeyError("not a node failure")
        assert boom_started.wait(timeout=10)
        finished.append(n.node_id)

    scheduler = Scheduler([node("slow"), node("boom")], capacity=2)
    with pytest.raises(KeyError):
        run_graph(scheduler, execute)
    assert finished == ["slow"]


# -- ParallelGraphStrategy over the durable store ----------------------------

RUN = "run-1"


def _store(tmp_path, strategy):
    store = ExecutionStore(tmp_path / "run.db")
    store.create_run(RUN, driver="inline", backend="mock", model="m", adapter_version="1")
    store.register_units(RUN, list(strategy.node_ids))
    return store


def _accept(store, unit_id, *, apply=True):
    claim = store.claim_unit(RUN, unit_id, "w", at=0.0)
    store.accept_unit(RUN, unit_id, claim.fencing_token, text="ok", at=0.0)
    if apply:
        store.record_apply_started(RUN, unit_id, at=0.0)
        store.record_apply_succeeded(RUN, unit_id, at=0.0)


def _fail(store, unit_id):
    claim = store.claim_unit(RUN, unit_id, "w", at=0.0)
    store.fail_unit(RUN, unit_id, claim.fencing_token, error="bad", terminal=True, at=0.0)


def _ids(units):
    return [u.unit_id for u in units]


DIAMOND = {"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"], "x": []}


def test_independent_units_are_released_together_up_to_capacity(tmp_path):
    strategy = ParallelGraphStrategy(DIAMOND, capacity=4)
    store = _store(tmp_path, strategy)

    assert _ids(ready_wave(store, RUN, strategy)) == ["a", "x"]


def test_capacity_counts_units_in_flight(tmp_path):
    strategy = ParallelGraphStrategy({"a": [], "b": [], "c": []}, capacity=2)
    store = _store(tmp_path, strategy)
    store.claim_unit(RUN, "a", "w", at=0.0, lease_seconds=100)

    assert _ids(ready_wave(store, RUN, strategy)) == ["b"]
    assert _ids(ready_wave(store, RUN, strategy, max_wave_size=0)) == []


def test_a_dependency_counts_only_once_applied(tmp_path):
    strategy = ParallelGraphStrategy(DIAMOND, capacity=4)
    store = _store(tmp_path, strategy)
    _accept(store, "a", apply=False)

    assert _ids(ready_wave(store, RUN, strategy)) == ["x"]
    store.record_apply_started(RUN, "a", at=0.0)
    store.record_apply_succeeded(RUN, "a", at=0.0)
    assert _ids(ready_wave(store, RUN, strategy)) == ["b", "c", "x"]


def test_a_skipped_dependency_is_settled(tmp_path):
    strategy = ParallelGraphStrategy({"a": [], "b": ["a"]}, capacity=2)
    store = _store(tmp_path, strategy)
    claim = store.claim_unit(RUN, "a", "w", at=0.0)
    store.fail_unit(
        RUN, "a", claim.fencing_token, error="skip:up_to_date", terminal=True,
        terminal_state=UnitState.SKIPPED, at=0.0,
    )

    assert _ids(ready_wave(store, RUN, strategy)) == ["b"]


def test_a_failed_unit_gates_only_its_transitive_dependents(tmp_path):
    strategy = ParallelGraphStrategy(DIAMOND, capacity=4)
    store = _store(tmp_path, strategy)
    _accept(store, "a")
    _fail(store, "b")

    assert _ids(ready_wave(store, RUN, strategy)) == ["c", "x"]
    assert _ids(gated_units(store, RUN, strategy)) == ["d"]


def test_a_refused_apply_breaks_the_unit(tmp_path):
    strategy = ParallelGraphStrategy({"a": [], "b": ["a"], "c": []}, capacity=3)
    store = _store(tmp_path, strategy)
    _accept(store, "a", apply=False)
    store.record_apply_rejected(RUN, "a", "refused", at=0.0)

    assert _ids(ready_wave(store, RUN, strategy)) == ["c"]
    assert _ids(gated_units(store, RUN, strategy)) == ["b"]


def test_drain_ends_when_every_unfinished_unit_is_gated(tmp_path):
    strategy = ParallelGraphStrategy({"a": [], "b": ["a"], "c": ["b"]}, capacity=2)
    store = _store(tmp_path, strategy)
    _fail(store, "a")

    assert ready_wave(store, RUN, strategy) == []
    assert _ids(unfinished_units(store, RUN)) == _ids(gated_units(store, RUN, strategy))


def test_deferred_units_follow_ordinary_ready_units(tmp_path):
    strategy = ParallelGraphStrategy(
        {"slow": [], "fast-1": [], "fast-2": []}, capacity=2, deferred={"slow"}
    )
    store = _store(tmp_path, strategy)

    assert _ids(ready_wave(store, RUN, strategy)) == ["fast-1", "fast-2"]


def test_an_expired_claim_is_reoffered_only_with_reclaim_at(tmp_path):
    strategy = ParallelGraphStrategy({"a": [], "b": []}, capacity=1)
    store = _store(tmp_path, strategy)
    store.claim_unit(RUN, "a", "w", at=0.0, lease_seconds=10)

    assert ready_wave(store, RUN, strategy) == []
    assert _ids(ready_wave(store, RUN, strategy, reclaim_at=5.0)) == []
    assert _ids(ready_wave(store, RUN, strategy, reclaim_at=20.0)) == ["a"]


def test_registration_must_match_the_graph(tmp_path):
    strategy = ParallelGraphStrategy({"a": [], "b": ["a"]})
    store = ExecutionStore(tmp_path / "run.db")
    store.create_run(RUN, driver="inline", backend="mock", model="m", adapter_version="1")
    store.register_units(RUN, ["a", "stray"])

    with pytest.raises(GraphRegistrationMismatchError) as caught:
        ready_wave(store, RUN, strategy)
    assert caught.value.unregistered == ["b"]
    assert caught.value.ungraphed == ["stray"]


def test_prepare_run_returns_the_parallel_wave(tmp_path):
    strategy = ParallelGraphStrategy(DIAMOND, capacity=4)
    store = _store(tmp_path, strategy)

    wave = prepare_run(store, RUN, strategy, strategy.units(None))
    assert _ids(wave) == ["a", "x"]


def test_prepare_run_reclaim_keeps_the_capacity_rule(tmp_path):
    strategy = ParallelGraphStrategy({"a": [], "b": [], "c": []}, capacity=1)
    store = _store(tmp_path, strategy)
    store.claim_unit(RUN, "a", "w", at=0.0, lease_seconds=10)
    store.claim_unit(RUN, "b", "w", at=0.0, lease_seconds=10)

    wave = prepare_run(store, RUN, strategy, strategy.units(None), reclaim_at=20.0)
    assert _ids(wave) == ["a"]


def test_the_workflow_wave_packer_admits_only_the_parallel_wave(tmp_path):
    from content_pipeline.execution.adapter import PreparedRequest, RunAdapter
    from content_pipeline.execution.workerpack import WorkerCommand, build_wave_args

    strategy = ParallelGraphStrategy(DIAMOND, capacity=4)
    store = _store(tmp_path, strategy)
    adapter = RunAdapter(
        build_request=lambda unit: PreparedRequest(unit=unit, system="", user=unit.id),
        parse_fn=lambda t: t,
        apply=lambda uid, payload: None,
        adapter_version="1",
        expected_unit_seconds=60.0,
    )
    (tmp_path / "answers").mkdir()
    (tmp_path / "envelopes").mkdir()
    command = WorkerCommand(
        argv=("python", "mount.py", "run"),
        answer_dir=str(tmp_path / "answers"),
        envelope_dir=str(tmp_path / "envelopes"),
    )

    wave = build_wave_args(store, RUN, adapter, command, 5, strategy=strategy)

    assert [pack["unitId"] for pack in wave["units"]] == ["a", "x"]


def test_strategy_validates_its_graph():
    with pytest.raises(GraphError):
        ParallelGraphStrategy({"a": ["b"], "b": ["a"]})
    with pytest.raises(GraphError):
        ParallelGraphStrategy({"a": ["missing"]})
    with pytest.raises(ValueError):
        ParallelGraphStrategy({"a": []}, capacity=0)
    with pytest.raises(ValueError):
        ParallelGraphStrategy({"a": []}, deferred={"b"})


def test_strategy_units_carry_payload_and_dependencies():
    strategy = ParallelGraphStrategy(
        {"a": [], "b": ["a"]}, payload_of=lambda source, unit_id: source[unit_id]
    )
    units = strategy.units({"a": 1, "b": 2})
    assert [(u.id, u.payload, dict(u.context)) for u in units] == [
        ("a", 1, {"dependencies": []}),
        ("b", 2, {"dependencies": ["a"]}),
    ]


def test_strategy_kinds_are_told_apart():
    parallel = ParallelGraphStrategy({"a": []})
    walk = GraphWalkStrategy(order=lambda store: [])
    flat = FlatChunkStrategy(select=lambda store: [])

    assert is_parallel_graph_strategy(parallel) and not is_graph_strategy(parallel)
    assert is_dependency_ordered(parallel) and is_dependency_ordered(walk)
    assert not is_dependency_ordered(flat)


def test_the_sequential_graph_walk_is_unchanged(tmp_path):
    walk = GraphWalkStrategy(order=lambda store: ["a", "b"])
    store = ExecutionStore(tmp_path / "run.db")
    store.create_run(RUN, driver="inline", backend="mock", model="m", adapter_version="1")
    store.register_units(RUN, ["a", "b"])

    assert _ids(ready_wave(store, RUN, walk)) == ["a"]
    with pytest.raises(UnsafeGraphParallelismError):
        ready_wave(store, RUN, walk, max_wave_size=2)


# -- stdlib only -------------------------------------------------------------

STDLIB_ONLY = ["scheduler.py", "deadlines.py", "failure_cache.py", "_atomic_json.py"]
ALLOWED_INTERNAL = {"content_pipeline.execution", "content_pipeline.execution._atomic_json"}


@pytest.mark.parametrize("filename", STDLIB_ONLY)
def test_ported_run_modules_import_only_the_standard_library(filename):
    tree = ast.parse((EXECUTION / filename).read_text(encoding="utf-8"))
    foreign = []
    for item in ast.walk(tree):
        if isinstance(item, ast.Import):
            names = [alias.name for alias in item.names]
        elif isinstance(item, ast.ImportFrom):
            names = [item.module or ""]
            if item.module == "content_pipeline.execution":
                names = [f"content_pipeline.execution.{a.name}" for a in item.names]
        else:
            continue
        for name in names:
            if name in ALLOWED_INTERNAL or name == "__future__":
                continue
            if name.split(".")[0] not in sys.stdlib_module_names:
                foreign.append(name)
    assert not foreign, f"{filename} imports outside the standard library: {foreign}"
