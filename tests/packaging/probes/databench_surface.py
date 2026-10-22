"""Closure probe: the library surface databench's index-pages runner uses.

Derived from databench's src/databench/index_pages_runner.py (the compatibility
probe at its top, `_require_shared_libraries`, and the calls it makes), with the
one intended change of the databench handoff: `execution_event` comes from
`katsoft_contracts`, not `bootstrap_lib`. Run with the venv's interpreter in
isolated mode. Any import of `bootstrap_lib` raises AssertionError (not an
ImportError, so no library can swallow it as "optional"). Each check records
{"ok": true} or {"error": ...}; the test judges the report.

argv[1]: a scratch directory for the files the checks write; argv[2]: JSON list of
llm_kit module names.
"""
import importlib
import importlib.util
import inspect
import json
import sys
from pathlib import Path

BLOCKED = ("bootstrap_lib",)


class _Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise AssertionError("blocked import: " + name)
        return None


sys.meta_path.insert(0, _Block())
scratch = Path(sys.argv[1])
results = {}

EXECUTION_EVENT_SCHEMA = "plugins-kit.execution-event/v1"
# The llm_kit modules databench imports or hashes; the test passes them (argv[2], JSON)
# from closure_support.KIT_MODULES, which the databench-drift test keeps in step.
KIT_MODULES = tuple(json.loads(sys.argv[2]))


def check(name):
    def wrap(fn):
        try:
            results[name] = {"ok": True, "value": fn()}
        except BaseException as exc:  # noqa: BLE001 - the report names it
            results[name] = {"error": f"{type(exc).__name__}: {exc}"}
        return fn
    return wrap


@check("kit_modules_import_and_locate")
def _():
    origins = {}
    for name in KIT_MODULES:
        importlib.import_module(name)
        spec = importlib.util.find_spec(name)  # databench hashes the file at spec.origin
        assert spec is not None and spec.origin and Path(spec.origin).is_file(), name
        origins[name] = spec.origin
    return origins


@check("kit_probe_signatures")
def _():
    gate = importlib.import_module("llm_kit.pipeline.runtime.gate")
    deadlines = importlib.import_module("llm_kit.pipeline.execution.deadlines")
    for name in ("refused_before_dispatch", "call_within_budget"):
        assert callable(getattr(deadlines, name, None)), name
    for name in ("NotDispatched", "BudgetExhausted", "CircuitOpen", "DeadlineExpired"):
        assert isinstance(getattr(deadlines, name, None), type), name
    assert isinstance(getattr(gate, "AdaptiveGate", None), type)
    inspect.signature(gate.AdaptiveGate).bind(
        1, None, is_backpressure=None, retry_after=None, not_admitted=None)
    inspect.signature(gate.AdaptiveGate.run).bind(None, None, label=None)
    inspect.signature(deadlines.call_within_budget).bind(
        None, 1.0, None, is_timeout=None, circuit=None, uncharged=None)


@check("execution_event_from_contracts")
def _():
    events = importlib.import_module("katsoft_contracts.execution_event")
    assert EXECUTION_EVENT_SCHEMA in getattr(events, "SUPPORTED_SCHEMAS", ())
    for name in ("Emitter", "JsonlSink", "usage_payload", "read_jsonl"):
        assert callable(getattr(events, name, None)), name
    assert isinstance(getattr(events, "MAX_PAYLOAD_BYTES", None), int)
    inspect.signature(events.Emitter).bind("databench", "run", sinks=())
    inspect.signature(events.Emitter.emit).bind(
        None, "databench:x", unit_id=None, attempt_id=None, payload=None)
    inspect.signature(events.JsonlSink).bind("events.jsonl")
    inspect.signature(events.usage_payload).bind(input_tokens=None, output_tokens=None)


@check("execution_event_stream_round_trip")
def _():
    events = importlib.import_module("katsoft_contracts.execution_event")
    path = scratch / "run" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    emitter = events.Emitter("databench", "run", sinks=(events.JsonlSink(path),))
    emitter.emit("databench:plan", unit_id=None, attempt_id=None, payload={"pages": 2})
    usage = events.usage_payload(input_tokens=3, output_tokens=2)
    assert usage is not None
    emitter.emit("usage", unit_id="u1", attempt_id="plan-1", payload=usage)
    read = events.read_jsonl(path)
    assert [e["event"] for e in read] == ["databench:plan", "usage"], read
    return len(read)


@check("scheduler_admits_a_graph")
def _():
    scheduler = importlib.import_module("llm_kit.pipeline.execution.scheduler")
    nodes = [scheduler.SchedulerNode("a"), scheduler.SchedulerNode("b", ("a",)),
             scheduler.SchedulerNode("c")]
    admitted = scheduler.Scheduler(nodes, capacity=2).admit_ready()
    assert [n.node_id for n in admitted] == ["a", "c"], admitted


@check("gate_runs_a_call_within_a_budget")
def _():
    gate = importlib.import_module("llm_kit.pipeline.runtime.gate")
    deadlines = importlib.import_module("llm_kit.pipeline.execution.deadlines")
    circuit = deadlines.DeadlineCircuit()
    budget = deadlines.UnitBudget(30.0)
    admission = gate.AdaptiveGate(
        2, None, is_backpressure=lambda exc: False, retry_after=None,
        not_admitted=deadlines.refused_before_dispatch)
    value = admission.run(
        lambda: deadlines.call_within_budget(
            budget, 10.0, lambda timeout: ("done", timeout),
            is_timeout=lambda exc: False, circuit=circuit,
            uncharged=lambda exc: False),
        label="probe")
    assert value[0] == "done", value
    assert budget.spent >= 0.0 and not circuit.is_open(), (budget.spent, circuit.is_open())


@check("failure_cache_and_deadline_notes_persist")
def _():
    cache_mod = importlib.import_module("llm_kit.pipeline.execution.failure_cache")
    deadlines = importlib.import_module("llm_kit.pipeline.execution.deadlines")
    cache = cache_mod.FailureCache(scratch / "state" / "failures.json")
    fingerprint = cache_mod.error_fingerprint("bad-page", [{"path": "a"}])
    cache.record(cache_mod.FailureRecord(
        stage="generation", unit_id="docs", attempt_key="k1", error_fingerprint=fingerprint,
        fingerprint_version=cache_mod.FINGERPRINT_VERSION, code="bad-page", summary="s"))
    again = cache_mod.FailureCache(scratch / "state" / "failures.json")
    found = again.lookup("generation", "docs", "k1")
    assert found is not None and found.error_fingerprint == fingerprint
    again.clear("generation", "docs")
    assert again.lookup("generation", "docs", "k1") is None
    notes = deadlines.DeadlineNotes(scratch / "state" / "deadlines.json")
    assert notes.record_expiry("k1", "docs", "run-1").expiries == 1
    assert deadlines.DeadlineNotes(scratch / "state" / "deadlines.json").lookup("k1").expiries == 1


@check("atomic_json_round_trip")
def _():
    atomic = importlib.import_module("llm_kit.storage.atomic_json")
    path = scratch / "atomic" / "x.json"
    atomic.write(path, {"a": 1})
    payload, report = atomic.read(path, {}, lambda value: None, "x")
    assert payload == {"a": 1} and report is None, (payload, report)


results["_bootstrap_lib_modules"] = sorted(m for m in sys.modules if m.split(".")[0] in BLOCKED)
print(json.dumps(results))
