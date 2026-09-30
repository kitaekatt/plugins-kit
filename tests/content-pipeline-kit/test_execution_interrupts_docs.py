"""The durable-wait documentation stays true to the code it describes.

The consumer docs (the "Durable waits (opt-in)" subsection of step 9 in
``building-a-pipeline.md``, the README section, the skill vocabulary and the
plugin CLAUDE.md paragraph) are checked against the real store, the real inline
driver and the real worker protocol. The fenced examples in the subsection are
executed as written. Expected values are literals.
"""

from __future__ import annotations

import builtins
import dataclasses
import importlib
import re
import sys
from pathlib import Path

import pytest

from content_pipeline.execution import (
    adapter as adapter_mod,
    controller,
    interrupts,
    model,
    protocol,
    status,
    wave,
)
from content_pipeline.execution.adapter import RunAdapter
from content_pipeline.execution.drivers import inline
from content_pipeline.execution.model import InterruptRequest, UnitState
from content_pipeline.execution.protocol import build_handlers
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.pipeline.workunit import FlatChunkStrategy, GraphWalkStrategy

_PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "content-pipeline-kit"
_REFERENCE = (
    _PLUGIN / "skills" / "content-pipeline-domain" / "references" / "building-a-pipeline.md"
)
_SKILL = _PLUGIN / "skills" / "content-pipeline-domain" / "SKILL.md"
_README = _PLUGIN / "README.md"
_PLUGIN_CLAUDE = _PLUGIN / "CLAUDE.md"
_SHARED_LIB = Path(__file__).resolve().parents[2] / "plugins" / "llm-scripting-kit" / "lib"

RUN = "r1"
FLAT = FlatChunkStrategy(select=lambda store: [])
GRAPH = GraphWalkStrategy(order=lambda store: [])

_HEADING = "### Durable waits (opt-in)"


def _lsk_names():
    return {n for n in sys.modules if n == "llm_scripting_kit" or n.startswith("llm_scripting_kit.")}


@pytest.fixture(autouse=True)
def lsk(monkeypatch):
    """Link llm-scripting-kit's validator for one test; unload it afterwards."""
    before = _lsk_names()
    monkeypatch.syspath_prepend(str(_SHARED_LIB))
    yield
    for name in _lsk_names() - before:
        del sys.modules[name]


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _subsection() -> str:
    """The "Durable waits (opt-in)" subsection: from its heading to the next step."""
    body = _text(_REFERENCE)
    start = body.index(_HEADING)
    end = body.index("\n## ", start)
    return body[start:end]


def _flat(text: str) -> str:
    """Whitespace collapsed to single spaces, so a phrase may wrap across lines."""
    return re.sub(r"\s+", " ", text)


def _fenced_python(section: str) -> list:
    return re.findall(r"```python\n(.*?)```", section, flags=re.DOTALL)


def _store(tmp_path, units=("u0", "u1", "u2")) -> ExecutionStore:
    store = ExecutionStore(tmp_path / "run.db")
    store.create_run(
        RUN, driver="inline", backend="mock", model="m1", adapter_version="7", created_at=1000.0
    )
    store.register_units(RUN, list(units), at=1000.0)
    return store


# -- every named symbol exists -------------------------------------------------

_SPAN = re.compile(r"`([^`\n]+)`")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_MODULES = (model, interrupts, inline, controller, wave, adapter_mod, status)


def _checked_name(span: str):
    """The dotted name a backticked span asserts exists, or None when it asserts none.

    A span is checked when it is called (``name(...)``), reached through
    ``store.`` / ``interrupts.`` / ``adapter.`` / ``execution.``, or is a
    class or constant (leading capital). Plain lower-case words and keyword
    arguments (``stop``, ``decision="answer"``) are values, not symbols.
    """
    called = "(" in span
    name = span.split("(", 1)[0].strip()
    if not _NAME.match(name):
        return None
    head = name.split(".")[0]
    reached = "." in name and head in ("store", "interrupts", "adapter", "execution")
    if called or reached or head[0].isupper():
        return name
    return None


def _resolves(name: str) -> bool:
    if hasattr(builtins, name):
        return True
    head, *rest = name.split(".")
    if head == "store":
        roots = [ExecutionStore]
    elif head == "adapter":
        roots = [RunAdapter]
        if rest and rest[0] in {f.name for f in dataclasses.fields(RunAdapter)}:
            return len(rest) == 1
    elif head == "execution":
        parts = ["content_pipeline", "execution"] + rest
        for cut in range(len(parts), 1, -1):
            try:
                obj = importlib.import_module(".".join(parts[:cut]))
            except ImportError:
                continue
            for attr in parts[cut:]:
                if not hasattr(obj, attr):
                    return False
                obj = getattr(obj, attr)
            return True
        return False
    else:
        roots = [m for m in _MODULES if hasattr(m, head)]
        if not roots:
            return False
        roots = [getattr(m, head) for m in roots]
        rest_ok = []
        for obj in roots:
            for attr in rest:
                if not hasattr(obj, attr):
                    break
                obj = getattr(obj, attr)
            else:
                rest_ok.append(obj)
        return bool(rest_ok)
    obj = roots[0]
    for attr in rest:
        if not hasattr(obj, attr):
            return False
        obj = getattr(obj, attr)
    return True


def test_documented_interrupt_symbols_exist():
    names = sorted(
        {n for n in (_checked_name(s) for s in _SPAN.findall(_subsection())) if n is not None}
    )
    # The subsection names the verbs, the signal and the errors it teaches.
    for expected in (
        "store.request_interrupt",
        "store.resolve_interrupt",
        "store.expire_interrupts",
        "store.list_interrupts",
        "InterruptRequested",
        "InterruptRequest",
        "WaitUnderDispatchError",
        "InterruptSupportError",
        "unit_resolutions",
    ):
        assert expected in names, expected
    missing = [n for n in names if not _resolves(n)]
    assert missing == []


# -- the fenced examples run as written ------------------------------------------


def _load_examples(store):
    blocks = _fenced_python(_subsection())
    assert len(blocks) == 2
    asking, draining = blocks
    ns_ask = {"store": store, "run_id": RUN}
    exec(compile(asking, "<asking example>", "exec"), ns_ask)
    ns_drain: dict = {}
    exec(compile(draining, "<drain example>", "exec"), ns_drain)
    return ns_ask["generate"], ns_drain["drain"]


@pytest.mark.parametrize("strategy_name", ["flat", "graph"])
def test_documented_drain_loop_terminates_with_a_waiting_unit(tmp_path, strategy_name):
    store = _store(tmp_path)
    strategy = {"flat": FLAT, "graph": GRAPH}[strategy_name]
    generate, drain = _load_examples(store)
    applied = []
    adapter = RunAdapter(
        parse_fn=lambda text: text, apply=lambda unit_id, payload: applied.append(unit_id)
    )

    decisions = {
        "u0": {"decision": "answer", "input": {"approved": True}},
        "u1": {"decision": "reject", "reason": "not this one"},
        "u2": {"decision": "answer", "input": {"approved": False}},
    }
    results = []
    for _ in range(6):
        result = drain(store, RUN, strategy, adapter, generate)
        results.append(result)
        if result == "complete":
            break
        assert result == "waiting"
        for unit in interrupts.waiting_units(store, RUN):
            record = store.open_interrupt(RUN, unit.unit_id)
            store.resolve_interrupt(RUN, record.id, **decisions[unit.unit_id])
    assert results[0] == "waiting"
    assert results[-1] == "complete"
    assert sorted(applied) == ["u0", "u1", "u2"]
    assert store.get_unit(RUN, "u0").accepted_text == "published copy for u0"
    assert store.get_unit(RUN, "u1").accepted_text == "draft copy for u1"
    assert store.get_unit(RUN, "u2").accepted_text == "draft copy for u2"
    assert interrupts.waiting_units(store, RUN) == []
    # The released rejection reached the next attempt as an outcome, not an answer.
    assert [r["outcome"] for r in interrupts.unit_resolutions(store, RUN, "u1")] == ["rejected"]


# -- the lane scope --------------------------------------------------------------

_LANE_PHRASES = (
    "the inline lane can ask",
    "the background lane refuses a wait under its open dispatch",
    "waitunderdispatcherror",
    "the workflow lane has no supported request surface",
)


@pytest.mark.parametrize("path", [_REFERENCE, _README], ids=["reference", "readme"])
def test_docs_state_which_lanes_can_ask(path):
    text = _flat(_subsection() if path == _REFERENCE else _text(path)).lower()
    for phrase in _LANE_PHRASES:
        assert phrase in text, f"{path.name}: {phrase}"


_REFUSAL_CLAIMS = (
    r"workflow lane\s+(?:also\s+|likewise\s+)?(?:refuses|rejects|raises|errors|fails)",
    r"(?:background|workflow)\s+(?:and|or)\s+(?:the\s+)?(?:workflow|background)\s+lanes?\s+"
    r"(?:both\s+)?(?:refuse|reject)",
    r"both\s+(?:the\s+)?(?:background|workflow)\s+(?:and|or)\s+(?:the\s+)?(?:workflow|background)"
    r"\s+lanes?\s+(?:refuse|reject)",
)


@pytest.mark.parametrize(
    "path", [_REFERENCE, _README, _SKILL, _PLUGIN_CLAUDE], ids=lambda p: p.name
)
def test_docs_do_not_claim_a_workflow_refusal(path):
    text = _flat(_text(path)).lower()
    for pattern in _REFUSAL_CLAIMS:
        assert re.search(pattern, text) is None, f"{path.name}: {pattern}"


def test_worker_protocol_has_no_wait_verb(tmp_path):
    assert protocol.VERBS == (
        "prepare",
        "claim",
        "read",
        "submit",
        "fail",
        "renew",
        "status",
        "pause",
        "resume",
        "finalize",
    )
    store = _store(tmp_path)
    adapter = RunAdapter(parse_fn=lambda text: text, apply=lambda unit_id, payload: None)
    handlers = build_handlers(store, adapter, strategy=FLAT)
    assert sorted(handlers) == sorted(protocol.VERBS)


def test_store_request_without_a_dispatch_row_is_not_refused(tmp_path):
    """The state a consumer-mounted workflow verb would reach: a claim, no dispatch row."""
    store = _store(tmp_path)
    token = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    assert store.open_dispatches(RUN) == []
    record = store.request_interrupt(
        RUN,
        "u0",
        token,
        InterruptRequest(
            kind="approval",
            request_schema={"type": "object"},
            payload={"question": "ship it?"},
        ),
        at=1002.0,
    )
    assert record.id == "1"
    assert store.get_unit(RUN, "u0").state is UnitState.WAITING
    assert store.open_dispatches(RUN) == []


# -- policies, opt-in, limits ----------------------------------------------------


def test_docs_name_both_policies_and_the_default():
    section = _flat(_subsection())
    for token in ("`on_rejected`", "`on_expired`", "`stop`", "`release`"):
        assert token in section, token
    assert re.search(r"`stop` is the default", section)
    assert model.POLICY_STOP == "stop" and model.POLICY_RELEASE == "release"
    assert model.INTERRUPT_POLICIES == ("stop", "release")
    assert "unit_resolutions" in section and "MUST read `unit_resolutions`" in section


@pytest.mark.parametrize(
    "path", [_REFERENCE, _README, _SKILL, _PLUGIN_CLAUDE], ids=lambda p: p.name
)
def test_docs_state_the_wait_is_opt_in_and_leaves_roundtrip_alone(path):
    text = _flat(_subsection() if path == _REFERENCE else _text(path)).lower()
    assert "durable wait" in text
    assert "roundtrip" in text or "round-trip" in text
    assert "opt-in" in text or "opt in" in text or "opts in" in text


def test_documented_limits_match_the_code():
    section = _flat(_subsection())
    assert interrupts.INPUT_LIMIT == 65536 and "65536 bytes" in section
    assert interrupts.KIND_LIMIT == 64 and "at most 64 characters" in section
    assert interrupts.EXPIRES_IN_S_MAX == 2147483647 and "2147483647" in section
    assert interrupts.REASON_LIMIT == 2000 and "cut to 2000 characters" in section
    assert interrupts.INTERRUPT_CONTRACT_BOOTSTRAP == "0.137.0"
    assert interrupts.JSON_SCHEMA_LSK_VERSION == "0.56.0"
    assert "bootstrap 0.137.0 or later" in section
    assert "llm-scripting-kit 0.56.0 or later" in section
    assert "0.56.0 or later" in _flat(_text(_README))
    assert "0.137.0 or later" in _flat(_text(_README))


def test_documented_drain_loop_returns_on_a_halted_run(tmp_path):
    """A halted run still offers its pending unit; the loop must not spin on it."""
    store = _store(tmp_path, units=("u0",))
    _, drain = _load_examples(store)
    store.set_halt(RUN, "rate_limit", "slow down", at=1001.0)
    adapter = RunAdapter(parse_fn=lambda text: text, apply=lambda unit_id, payload: None)
    calls = []

    def generate(work_unit):
        calls.append(work_unit.id)
        return "text"

    # Bound the loop: a spin on the halted run becomes a failure, not a hang.
    original = inline.run_wave
    count = {"n": 0}

    def bounded(*args, **kwargs):
        count["n"] += 1
        assert count["n"] <= 20, "drain spun on a halted run"
        return original(*args, **kwargs)

    drain.__globals__["run_wave"] = bounded
    assert drain(store, RUN, FLAT, adapter, generate) == "blocked"
    assert calls == []
    assert store.get_unit(RUN, "u0").state is UnitState.PENDING
    controller.resume_run(store, RUN)
    drain.__globals__["run_wave"] = original
    assert drain(store, RUN, FLAT, adapter, generate) == "complete"
    assert calls == ["u0"]
