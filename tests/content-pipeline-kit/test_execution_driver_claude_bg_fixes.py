"""Store-truth reporting, escape-proof classification, and leaked-session
reporting in content_pipeline.execution.drivers.claude_bg.

Every test drives the REAL ExecutionStore; the only fakes are the `claude`
process (a scripted runner) and, where named, the transcript classifier.
"""

from __future__ import annotations

import inspect
import json

import pytest

from content_pipeline.execution.adapter import RunAdapter
from content_pipeline.execution.drivers import claude_bg
from content_pipeline.execution.drivers.claude_bg import (
    ClaudeCli,
    dispatch_wave,
    supervise_tick,
)
from content_pipeline.execution.model import UnitState
from test_execution_driver_claude_bg import (
    _claim_and_open,
    _seeded_dispatch_store,
    _worker_command,
)
from test_execution_driver_claude_bg_banner import real_launch_response
from test_support.claude_bg_fakes import FakeRunner, _bg_record, _healthy_runner


@pytest.fixture(autouse=True)
def _no_real_claude_subprocess(monkeypatch):
    def _raise(*args, **kwargs):
        raise AssertionError("a test reached the REAL default claude runner")

    monkeypatch.setattr(claude_bg, "_default_runner", _raise)


def _cli(runner) -> ClaudeCli:
    return ClaudeCli(executable="claude", runner=runner)


class _WaveRunner(FakeRunner):
    """Serves `agents --json` from a function of the call index, so a test can
    perform a real store write (the worker's accept) at a chosen call."""

    def __init__(self, agents_fn):
        super().__init__(scripts=dict(_healthy_runner().scripts))
        self.agents_fn = agents_fn
        self.agents_calls = 0

    def __call__(self, argv, **kwargs):
        if list(argv)[1:3] == ["agents", "--json"]:
            self.calls.append((list(argv), kwargs))
            self.agents_calls += 1
            return (self.agents_fn(self.agents_calls), "", 0)
        return super().__call__(argv, **kwargs)


def _records(state, short_id="abc12345"):
    return json.dumps([_bg_record(id=short_id, session_id="sess-1", state=state)])


def _wave_runner(agents_fn, *, rm=("", "", 0)):
    runner = _WaveRunner(agents_fn)
    runner.script(("claude", "--bg"), real_launch_response("abc12345"))
    runner.script(("claude", "stop"), ("", "", 0))
    runner.script(("claude", "rm"), rm)
    return runner


def _wave_with_accept_then(store, final_state, *, rm=("", "", 0)):
    """Call 1 preflight, 2 before-launch snapshot, 3 launch confirmation
    (working), 4 first supervise tick: the worker has accepted and the
    session reports `final_state`."""

    def agents_fn(n):
        if n <= 2:
            return "[]"
        if n == 3:
            return _records("working")
        unit = store.get_unit("run-1", "u0")
        if unit.state is UnitState.CLAIMED:
            store.accept_unit("run-1", "u0", unit.fencing_token, text="answer", at=1001.0)
        return _records(final_state)

    return _wave_runner(agents_fn, rm=rm)


def _run_wave(store, tmp_path, runner, **kw):
    return dispatch_wave(
        store, "run-1", store.list_units("run-1"), RunAdapter(), cli=_cli(runner),
        worker_command=_worker_command(tmp_path), max_agents=1, at=1000.0,
        sleep_fn=lambda s: None, clock_fn=lambda: 1000.0, **kw,
    )


# -- accepted reflects the store ---------------------------------------------


def test_accepted_lists_a_unit_whose_session_then_blocked(tmp_path):
    store = _seeded_dispatch_store(tmp_path)
    report = _run_wave(store, tmp_path, _wave_with_accept_then(store, "blocked"))
    assert report.settled["u0"] == "blocked"  # the session's fate
    assert store.get_unit("run-1", "u0").state is UnitState.ACCEPTED  # the truth
    assert report.accepted == ("u0",)


def test_accepted_lists_a_unit_whose_session_lingered(tmp_path):
    store = _seeded_dispatch_store(tmp_path)
    report = _run_wave(
        store, tmp_path, _wave_with_accept_then(store, "working"), terminal_exit_grace_seconds=0.0
    )
    assert report.settled["u0"] == "session_lingering"
    assert report.accepted == ("u0",)


def test_accepted_omits_a_blocked_unit_that_never_accepted(tmp_path):
    store = _seeded_dispatch_store(tmp_path)

    def agents_fn(n):
        if n <= 2:
            return "[]"
        return _records("working" if n == 3 else "blocked")

    report = _run_wave(store, tmp_path, _wave_runner(agents_fn), stall_timeout_seconds=1e9)
    assert report.settled["u0"] == "blocked"
    assert report.accepted == ()


# -- lifecycle return codes surface -------------------------------------------


def test_report_lists_a_session_whose_rm_failed(tmp_path):
    store = _seeded_dispatch_store(tmp_path)
    runner = _wave_with_accept_then(store, "blocked", rm=("", "no such session", 1))
    report = _run_wave(store, tmp_path, runner)
    assert report.leaked_sessions == ("abc12345",)


def test_report_lists_no_leak_when_rm_succeeds(tmp_path):
    store = _seeded_dispatch_store(tmp_path)
    report = _run_wave(store, tmp_path, _wave_with_accept_then(store, "blocked"))
    assert report.leaked_sessions == ()


def test_a_failing_stop_alone_is_not_a_leak(tmp_path):
    store = _seeded_dispatch_store(tmp_path)
    runner = _wave_with_accept_then(store, "blocked")
    runner.script(("claude", "stop"), ("", "already finished", 1))
    report = _run_wave(store, tmp_path, runner)
    assert report.leaked_sessions == ()


# -- classify-and-halt cannot escape the tick ---------------------------------


def _session_records(state):
    return json.dumps([_bg_record(id="short1", session_id="sess-1", state=state)])


def _tick_runner(agents_body):
    runner = FakeRunner()
    runner.script(("claude", "agents", "--json"), (agents_body, "", 0))
    runner.script(("claude", "stop"), ("", "", 0))
    runner.script(("claude", "rm"), ("", "", 0))
    return runner


def test_stopped_session_of_an_accepted_unit_is_not_classified_or_halted(tmp_path, monkeypatch):
    """Reachable from the snapshot alone: the worker accepted, then its
    session was stopped. Classification would call fail_unit on a terminal
    unit (TerminalStateError) after halting the run over a success."""
    store = _seeded_dispatch_store(tmp_path)
    od = _claim_and_open(store, "run-1", "u0", "worker-a", "sess-1", "short1", at=1000.0)
    store.accept_unit("run-1", "u0", od.fencing_token, text="answer", at=1001.0)
    monkeypatch.setattr(claude_bg, "classify_settled_failure", lambda *a, **k: "rate_limit")

    result = supervise_tick(
        store, "run-1", _cli(_tick_runner(_session_records("stopped"))), RunAdapter(),
        {"u0": od}, at=1010.0,
    )

    assert result.settled == {"u0": "stopped"}
    assert result.halted is None
    assert store.get_run("run-1").halted_kind is None


def test_missing_session_of_an_accepted_unit_is_not_classified_or_halted(tmp_path, monkeypatch):
    store = _seeded_dispatch_store(tmp_path)
    od = _claim_and_open(store, "run-1", "u0", "worker-a", "sess-1", "short1", at=1000.0)
    store.accept_unit("run-1", "u0", od.fencing_token, text="answer", at=1001.0)
    monkeypatch.setattr(claude_bg, "classify_settled_failure", lambda *a, **k: "auth")

    result = supervise_tick(
        store, "run-1", _cli(_tick_runner("[]")), RunAdapter(), {"u0": od}, at=1010.0
    )

    assert result.settled == {"u0": "missing"}
    assert result.halted is None


def test_accept_inside_the_classify_window_does_not_escape_the_tick(tmp_path, monkeypatch):
    """The unit is CLAIMED at the tick's read; the worker's accept lands
    while the transcript is classified (the real ExecutionStore.accept_unit,
    run from inside the classifier seam); the halt release then hits a
    terminal unit. The tick must report the halt it recorded, not raise."""
    store = _seeded_dispatch_store(tmp_path)
    od = _claim_and_open(store, "run-1", "u0", "worker-a", "sess-1", "short1", at=1000.0)

    def classify(*a, **k):
        store.accept_unit("run-1", "u0", od.fencing_token, text="answer", at=1005.0)
        return "rate_limit"

    monkeypatch.setattr(claude_bg, "classify_settled_failure", classify)

    result = supervise_tick(
        store, "run-1", _cli(_tick_runner(_session_records("failed"))), RunAdapter(),
        {"u0": od}, at=1010.0,
    )

    assert result.settled == {"u0": "failed"}
    assert result.halted == "rate_limit"
    assert store.get_run("run-1").halted_kind == "rate_limit"
    assert store.get_unit("run-1", "u0").state is UnitState.ACCEPTED


def test_already_settled_dispatch_row_is_dropped_not_raised(tmp_path):
    """Another dispatcher adopted and settled this row after this
    dispatcher's lease lapsed; settling it again raised NoOpenDispatchError
    out of the tick."""
    store = _seeded_dispatch_store(tmp_path)
    od = _claim_and_open(store, "run-1", "u0", "worker-a", "sess-1", "short1", at=1000.0)
    store.accept_unit("run-1", "u0", od.fencing_token, text="answer", at=1001.0)
    store.settle_dispatch("run-1", "u0", outcome="superseded", at=1002.0)

    result = supervise_tick(
        store, "run-1", _cli(_tick_runner(_session_records("done"))), RunAdapter(),
        {"u0": od}, at=1010.0,
    )

    assert result.settled == {}
    assert result.dropped == ("u0",)


# -- dead parameter -----------------------------------------------------------


def test_dispatch_wave_has_no_lease_seconds_parameter():
    assert "lease_seconds" not in inspect.signature(dispatch_wave).parameters
