"""Tests for the observer seam of llm_scripting_kit.declaration.run.

Every test injects its entry map, reachability results and backends, so nothing
here reads the host's config, spawns a CLI, or opens a socket. The envelope
comes from the real ``bootstrap_lib.execution_event`` module, so a stream that
passes ``validate_stream`` is a conforming stream.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
import textwrap
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pytest

from bootstrap_lib import execution_event as ee
from llm_scripting_kit import declaration as decl
from llm_scripting_kit.completion.contract import OutputContractViolation
from llm_scripting_kit.completion.halt import HALT_AUTH
from llm_scripting_kit.completion.types import LLMResponse
from llm_scripting_kit.model_endpoints import HARNESS_KIND, EndpointEntry
from llm_scripting_kit.models import EndpointResolveError
from llm_scripting_kit.reachability import STATUS_REACHABLE, Reachability

PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "llm-scripting-kit"
BOOTSTRAP_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "bootstrap"


def _harness(entry_id, harness="claude"):
    return EndpointEntry(
        id=entry_id, base_url=None, model=f"{entry_id}-model", kind=HARNESS_KIND,
        harness=harness, tier=None, family=None, conserve_usage=None,
    )


def _reach():
    return Reachability(status=STATUS_REACHABLE, checked="cli-version", detail=STATUS_REACHABLE)


class _Halt(Exception):
    def __init__(self, kind):
        super().__init__(f"halt {kind}")
        self.kind = kind


class _Backend:
    def __init__(self, name, outcomes, on_call=None):
        self.name = name
        self.outcomes = list(outcomes)
        self.calls = 0
        self.on_call = on_call

    def complete(self, system, user, *, model, options=None):
        self.calls += 1
        if self.on_call:
            self.on_call()
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def classify_halt(self, exc):
        return getattr(exc, "kind", None)


@dataclass
class _Selection:
    endpoint: str
    kind: str
    backend: Any
    model: str
    effort: Optional[str] = None


def _factory_for(backends):
    def factory(name, **_kw):
        if name not in backends:
            raise EndpointResolveError(f"unknown endpoint '{name}'")
        return _Selection(name, HARNESS_KIND, backends[name], f"{name}-model")

    return factory


def _entries():
    return {"sol": _harness("sol", "codex"), "opus": _harness("opus")}


UNIT = "unit-1"


@pytest.fixture
def sink():
    return ee.InMemorySink()


@pytest.fixture
def observer(sink):
    return ee.Emitter("job-kit", "run-1", unit_id=UNIT, sinks=[sink])


def _run(backends, observer, *, names=("sol", "opus"), max_attempts=1, **kwargs):
    return decl.run(
        list(names), decl.RunRequest(system="s", prompt="p"),
        entries=_entries(), backend_factory=_factory_for(backends),
        reachability_cache={"sol": _reach(), "opus": _reach()},
        max_attempts=max_attempts, observer=observer, **kwargs,
    )


def _names(sink):
    return [e["event"] for e in sink.events]


def _response(**fields):
    return LLMResponse(text="ok", model="served-model", **fields)


class TestEmissions:
    def test_success_emits_selected_started_usage_result_terminal(self, observer, sink):
        seen_at_call = []
        backend = _Backend(
            "codex-cli", [_response(input_tokens=11, output_tokens=7, cache_hit_tokens=3)],
            on_call=lambda: seen_at_call.append(_names(sink)),
        )
        result = _run({"sol": backend, "opus": _Backend("claude-cli", [])}, observer)
        assert result.status == decl.RUN_COMPLETED
        assert _names(sink) == ["dispatch-selected", "call-started", "usage", "result", "terminal"]
        # Both moments precede the call itself.
        assert seen_at_call == [["dispatch-selected", "call-started"]]
        selected, started, usage, outcome, terminal = sink.events
        assert selected["payload"] == {"entry": "sol"}
        assert started["payload"] == {}
        assert usage["payload"] == {
            "input_tokens": 11, "output_tokens": 7, "cache_hit_tokens": 3, "total_tokens": None,
        }
        assert outcome["payload"] == {"status": "completed"}
        assert terminal["payload"] == {"state": "completed"}
        ee.validate_stream(sink.events)

    def test_attempt_id_is_run_attempt_number(self, observer, sink):
        backends = {
            "sol": _Backend("codex-cli", [_Halt(HALT_AUTH)]),
            "opus": _Backend("claude-cli", [_response(input_tokens=1, output_tokens=1)]),
        }
        _run(backends, observer, max_attempts=3)
        attempt_ids = [e["identity"].get("attempt_id") for e in sink.events]
        assert attempt_ids == ["1", "1", "1", "2", "2", "2", "2", None]
        assert {e["identity"]["unit_id"] for e in sink.events} == {UNIT}
        assert {e["identity"]["run_id"] for e in sink.events} == {"run-1"}

    def test_identity_maps_backend_model_and_plugin(self, observer, sink):
        _run({"sol": _Backend("codex-cli", [_response(input_tokens=1, output_tokens=1)]),
              "opus": _Backend("claude-cli", [])}, observer)
        attempt = [e for e in sink.events if e["event"] != "terminal"]
        assert {e["source"]["adapter"] for e in attempt} == {"codex-cli"}
        assert {e["source"]["model"] for e in attempt} == {"sol-model"}
        assert {e["source"]["plugin"] for e in sink.events} == {"job-kit"}
        assert "adapter" not in sink.events[-1]["source"]

    def test_halt_then_reselect_emits_two_attempts_one_terminal(self, observer, sink):
        backends = {
            "sol": _Backend("codex-cli", [_Halt(HALT_AUTH)]),
            "opus": _Backend("claude-cli", [_response(input_tokens=2, output_tokens=3)]),
        }
        result = _run(backends, observer, max_attempts=2)
        assert result.entry == "opus"
        assert _names(sink) == [
            "dispatch-selected", "call-started", "result",
            "dispatch-selected", "call-started", "usage", "result", "terminal",
        ]
        halted = sink.events[2]["payload"]
        assert halted == {"status": "halted", "halt": HALT_AUTH}
        assert [e["event"] for e in sink.events].count("terminal") == 1
        assert sink.events[3]["payload"] == {"entry": "opus"}
        ee.validate_stream(sink.events)

    def test_launch_failure_result_carries_reason_launch(self, observer, sink):
        backends = {
            "sol": _Backend("codex-cli", [FileNotFoundError("codex")]),
            "opus": _Backend("claude-cli", [_response(input_tokens=1, output_tokens=1)]),
        }
        _run(backends, observer, max_attempts=2)
        assert sink.events[2]["payload"] == {"status": "halted", "reason": "launch"}
        ee.validate_stream(sink.events)

    def test_task_error_emits_failed_result_without_usage(self, observer, sink):
        backends = {"sol": _Backend("codex-cli", [RuntimeError("bad")]), "opus": _Backend("claude-cli", [])}
        result = _run(backends, observer)
        assert result.status == decl.RUN_FAILED
        assert _names(sink) == ["dispatch-selected", "call-started", "result", "terminal"]
        assert sink.events[2]["payload"] == {"status": "failed", "reason": "task-error"}
        assert sink.events[3]["payload"] == {"state": "failed"}
        ee.validate_stream(sink.events)

    def test_contract_violation_emits_usage_and_failed_result(self, observer, sink):
        violation = OutputContractViolation(
            _response(input_tokens=40, output_tokens=9, status="error")
        )
        backends = {"sol": _Backend("codex-cli", [violation]), "opus": _Backend("claude-cli", [])}
        result = _run(backends, observer)
        assert result.status == decl.RUN_FAILED
        assert _names(sink) == ["dispatch-selected", "call-started", "usage", "result", "terminal"]
        assert sink.events[2]["payload"]["input_tokens"] == 40
        assert sink.events[2]["payload"]["output_tokens"] == 9
        assert sink.events[3]["payload"] == {"status": "failed", "reason": "output-contract"}
        ee.validate_stream(sink.events)

    def test_attempt_limit_emits_halted_result_then_attempt_limit_terminal(self, observer, sink):
        backends = {"sol": _Backend("codex-cli", [_Halt(HALT_AUTH)]), "opus": _Backend("claude-cli", [])}
        result = _run(backends, observer, max_attempts=1)
        assert result.status == decl.RUN_ATTEMPT_LIMIT
        assert _names(sink) == ["dispatch-selected", "call-started", "result", "terminal"]
        assert sink.events[-1]["payload"] == {"state": "attempt-limit"}
        ee.validate_stream(sink.events)

    def test_unroutable_emits_terminal_then_raises(self, observer, sink):
        with pytest.raises(decl.NoUsableRoutingTarget):
            decl.run(
                ["typo"], decl.RunRequest(system="s", prompt="p"), entries={},
                reachability_cache={}, observer=observer,
            )
        assert _names(sink) == ["terminal"]
        assert sink.events[0]["payload"] == {"state": "unroutable"}
        ee.validate_stream(sink.events)

    def test_floor_after_a_halt_ends_the_stream_with_unroutable(self, observer, sink):
        backends = {
            "sol": _Backend("codex-cli", [_Halt(HALT_AUTH)]),
            "opus": _Backend("claude-cli", [_Halt(HALT_AUTH)]),
        }
        with pytest.raises(decl.NoUsableRoutingTarget):
            _run(backends, observer, max_attempts=5)
        assert _names(sink)[-1] == "terminal"
        assert sink.events[-1]["payload"] == {"state": "unroutable"}
        ee.validate_stream(sink.events)


class TestUsageNormalization:
    def test_codex_total_only_usage_emits_total_not_zero_split(self, observer, sink):
        backends = {"sol": _Backend("codex-cli", [_response(total_tokens=1234)]), "opus": _Backend("claude-cli", [])}
        _run(backends, observer)
        usage = [e for e in sink.events if e["event"] == "usage"]
        assert len(usage) == 1
        assert usage[0]["payload"] == {
            "input_tokens": None, "output_tokens": None,
            "cache_hit_tokens": None, "total_tokens": 1234,
        }

    def test_all_zero_usage_emits_no_usage_event(self, observer, sink):
        backends = {"sol": _Backend("codex-cli", [_response()]), "opus": _Backend("claude-cli", [])}
        _run(backends, observer)
        assert "usage" not in _names(sink)
        assert _names(sink) == ["dispatch-selected", "call-started", "result", "terminal"]

    def test_split_usage_keeps_directional_counts_and_drops_zero_cache(self, observer, sink):
        backends = {
            "sol": _Backend("codex-cli", [_response(input_tokens=0, output_tokens=9)]),
            "opus": _Backend("claude-cli", []),
        }
        _run(backends, observer)
        usage = [e for e in sink.events if e["event"] == "usage"][0]["payload"]
        assert usage == {
            "input_tokens": 0, "output_tokens": 9, "cache_hit_tokens": None, "total_tokens": None,
        }

    def test_a_response_without_usage_fields_emits_no_usage_event(self, observer, sink):
        backends = {"sol": _Backend("codex-cli", ["plain text"]), "opus": _Backend("claude-cli", [])}
        _run(backends, observer)
        assert "usage" not in _names(sink)


class TestVocabulary:
    def test_no_later_revision_names_emitted(self, observer, sink):
        backends = {
            "sol": _Backend("codex-cli", [_Halt(HALT_AUTH)]),
            "opus": _Backend("claude-cli", [_response(input_tokens=1, output_tokens=1)]),
        }
        _run(backends, observer, max_attempts=2)
        emitted = set(_names(sink))
        assert emitted <= ee.CORE_EVENTS
        assert not emitted & ee.LATER_REVISION_NAMES


class TestPropagation:
    def test_observer_exception_propagates(self, sink):
        class Boom(RuntimeError):
            pass

        class Exploding:
            def emit(self, event, **_fields):
                if event == "result":
                    raise Boom("observer failed")

        backends = {"sol": _Backend("codex-cli", [_response(input_tokens=1, output_tokens=1)]), "opus": _Backend("claude-cli", [])}
        with pytest.raises(Boom):
            _run(backends, Exploding())

    def test_observer_exception_on_a_halt_result_propagates(self):
        class Boom(RuntimeError):
            pass

        class Exploding:
            def emit(self, event, **_fields):
                if event == "result":
                    raise Boom("observer failed")

        backends = {"sol": _Backend("codex-cli", [_Halt(HALT_AUTH)]), "opus": _Backend("claude-cli", [])}
        with pytest.raises(Boom):
            _run(backends, Exploding(), max_attempts=2)


_NO_OBSERVER_SCRIPT = textwrap.dedent(
    """
    import sys
    sys.path[:0] = [{lib!r}, {boot!r}]
    if {block}:
        sys.modules["bootstrap_lib.execution_event"] = None
    from llm_scripting_kit import declaration as decl
    from llm_scripting_kit.model_endpoints import EndpointEntry, HARNESS_KIND
    from llm_scripting_kit.reachability import Reachability

    class B:
        name = "claude-cli"
        def complete(self, system, user, *, model, options=None):
            return "ok"
        def classify_halt(self, exc):
            return None

    class S:
        endpoint = "opus"; kind = HARNESS_KIND; backend = B(); model = "m"; effort = None

    entry = EndpointEntry(id="opus", base_url=None, model="m", kind=HARNESS_KIND,
                          harness="claude", tier=None, family=None, conserve_usage=None)
    result = decl.run(
        ["opus"], decl.RunRequest(system="s", prompt="p"), entries={{"opus": entry}},
        backend_factory=lambda name, **_: S(),
        reachability_cache={{"opus": Reachability(status="reachable", checked="x", detail="x")}},
    )
    assert result.status == "completed", result
    if not {block}:
        assert "bootstrap_lib.execution_event" not in sys.modules, "imported without an observer"
    print("ok")
    """
)


class TestLazyImport:
    @pytest.mark.parametrize("block", [True, False], ids=["module-blocked", "module-not-imported"])
    def test_no_observer_needs_no_execution_event(self, block):
        script = _NO_OBSERVER_SCRIPT.format(
            lib=str(PLUGIN_ROOT / "lib"), boot=str(BOOTSTRAP_ROOT), block=block
        )
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "ok"


class TestProbe:
    def _refuses(self, observer, sink, match):
        backend = _Backend("codex-cli", [_response(input_tokens=1, output_tokens=1)])
        with pytest.raises(decl.DeclarationSupportError, match=match):
            _run({"sol": backend, "opus": _Backend("claude-cli", [])}, observer)
        assert backend.calls == 0
        assert sink.events == []

    def test_observer_with_absent_module_refuses_before_dispatch(self, observer, sink, monkeypatch):
        monkeypatch.setitem(sys.modules, "bootstrap_lib", None)
        self._refuses(observer, sink, r"execution_event.*claude plugin install bootstrap@plugins-kit")

    def test_observer_with_too_old_module_refuses_before_dispatch(self, observer, sink, monkeypatch):
        monkeypatch.setitem(sys.modules, "bootstrap_lib.execution_event", None)
        self._refuses(observer, sink, rf"claude plugin update bootstrap@plugins-kit.*{decl.EXECUTION_EVENT_BOOTSTRAP}|{decl.EXECUTION_EVENT_BOOTSTRAP}.*claude plugin update")

    def test_observer_with_module_lacking_schema_v1_refuses_before_dispatch(self, observer, sink, monkeypatch):
        stale = types.SimpleNamespace(
            SUPPORTED_SCHEMAS=frozenset({"plugins-kit.execution-event/v0"}),
            usage_payload=ee.usage_payload,
        )
        monkeypatch.setitem(sys.modules, "bootstrap_lib.execution_event", stale)
        self._refuses(observer, sink, "claude plugin update bootstrap@plugins-kit")

    def test_observer_with_module_lacking_the_capability_marker_refuses(self, observer, sink, monkeypatch):
        stale = types.SimpleNamespace(usage_payload=ee.usage_payload)
        monkeypatch.setitem(sys.modules, "bootstrap_lib.execution_event", stale)
        self._refuses(observer, sink, "claude plugin update bootstrap@plugins-kit")

    def test_observer_with_unbindable_usage_payload_refuses(self, observer, sink, monkeypatch):
        stale = types.SimpleNamespace(
            SUPPORTED_SCHEMAS=ee.SUPPORTED_SCHEMAS,
            usage_payload=lambda *, input_tokens=None, output_tokens=None: None,
        )
        monkeypatch.setitem(sys.modules, "bootstrap_lib.execution_event", stale)
        self._refuses(observer, sink, "claude plugin update bootstrap@plugins-kit")

    def test_observer_with_missing_usage_payload_refuses(self, observer, sink, monkeypatch):
        stale = types.SimpleNamespace(SUPPORTED_SCHEMAS=ee.SUPPORTED_SCHEMAS)
        monkeypatch.setitem(sys.modules, "bootstrap_lib.execution_event", stale)
        self._refuses(observer, sink, "claude plugin update bootstrap@plugins-kit")


class TestSurface:
    def test_observer_is_keyword_only(self):
        param = inspect.signature(decl.run).parameters["observer"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is None

    def test_protocol_is_compatible_with_bootstrap_emitter(self):
        from llm_scripting_kit.observer import ExecutionObserver

        def shape(func):
            params = list(inspect.signature(func).parameters.values())[1:]  # drop self
            return [(p.name, p.kind) for p in params]

        assert shape(ExecutionObserver.emit) == shape(ee.Emitter.emit)

    def test_observer_protocol_is_exported(self):
        import llm_scripting_kit
        from llm_scripting_kit.observer import ExecutionObserver

        assert llm_scripting_kit.ExecutionObserver is ExecutionObserver
        assert decl.ExecutionObserver is ExecutionObserver
        assert "ExecutionObserver" in llm_scripting_kit.__all__
        assert "ExecutionObserver" in decl.__all__

    def test_observer_module_is_a_leaf(self):
        path = PLUGIN_ROOT / "lib" / "llm_scripting_kit" / "observer.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, "relative import in a leaf module"
                imported.add((node.module or "").split(".")[0])
        assert imported <= {"__future__", "typing"}
