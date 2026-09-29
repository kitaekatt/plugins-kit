"""claude_bg follow-ups: launch stderr on the error and report, the
repeated-identical-failure breaker, and the dispatcher lease across a slow
refill."""

from __future__ import annotations

import json

import pytest

from content_pipeline.execution.adapter import RunAdapter
from content_pipeline.execution.drivers import claude_bg
from content_pipeline.execution.drivers.claude_bg import (
    ClaudeCli,
    LaunchMisconfigurationError,
    dispatch_unit,
    dispatch_wave,
)
from test_execution_driver_claude_bg import (
    _pending_unit,
    _seeded_dispatch_store,
    _worker_command,
)
from test_support.claude_bg_fakes import _bg_record, _healthy_runner


@pytest.fixture(autouse=True)
def _no_real_claude_subprocess(monkeypatch):
    def _raise(*args, **kwargs):
        raise AssertionError("reached the real claude runner")

    monkeypatch.setattr(claude_bg, "_default_runner", _raise)
    monkeypatch.setattr(claude_bg, "classify_settled_failure", lambda *a, **k: None)


def _cli(runner):
    return ClaudeCli(executable="claude", runner=runner)


class _LiveRunner:
    """A claude fake whose launches create sessions in ``state``."""

    def __init__(self, on_launch=None, state="working"):
        self.base = _healthy_runner()
        self.sessions = []
        self.on_launch = on_launch
        self.state = state
        self.launches = 0

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        if len(argv) >= 3 and argv[1] == "--bg" and argv[2] != "-p":
            self.launches += 1
            short = f"{self.launches:08x}"
            self.sessions.append(
                _bg_record(id=short, session_id=f"sess-{self.launches}", state=self.state)
            )
            if self.on_launch is not None:
                self.on_launch(self.launches)
            return (f"backgrounded - {short}", "", 0)
        if argv[1:3] == ["agents", "--json"]:
            return (json.dumps(self.sessions), "", 0)
        if len(argv) >= 2 and argv[1] in ("stop", "rm") and "--help" not in argv:
            return ("ok", "", 0)
        return self.base(argv, **kwargs)


# --- item 1: launch stderr and exit code -----------------------------------


def _untrusted_runner(stderr="Workspace not trusted: run claude here once\nand accept the prompt", rc=1):
    runner = _healthy_runner()
    runner.script(("claude", "--bg"), ("", stderr, rc))
    runner.script(("claude", "agents", "--json"), ("[]", "", 0))
    return runner


def _launch_error(tmp_path, runner):
    store = _seeded_dispatch_store(tmp_path)
    wc = _worker_command(tmp_path)
    unit = _pending_unit(store, "run-1", "u0")
    clock = {"t": 1000.0}
    with pytest.raises(LaunchMisconfigurationError) as info:
        dispatch_unit(
            store, "run-1", unit, _cli(runner), wc, worker_id="w",
            launch_confirm_seconds=2.0, poll_interval_s=1.0,
            clock_fn=lambda: clock["t"],
            sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
        )
    return info.value


def test_launch_error_carries_bounded_one_line_stderr_and_rc(tmp_path):
    err = _launch_error(tmp_path, _untrusted_runner())
    assert err.launch_rc == 1
    assert "Workspace not trusted" in err.launch_stderr
    assert "\n" not in err.launch_stderr
    assert "Workspace not trusted" in str(err)


def test_launch_stderr_is_bounded(tmp_path):
    err = _launch_error(tmp_path, _untrusted_runner("x" * 5000))
    assert 0 < len(err.launch_stderr) <= 300


def test_launch_error_without_stderr_has_no_excerpt(tmp_path):
    err = _launch_error(tmp_path, _untrusted_runner("", 0))
    assert err.launch_stderr == ""
    assert err.launch_rc == 0


def test_report_carries_launch_stderr_and_rc(tmp_path):
    store = _seeded_dispatch_store(tmp_path)
    wc = _worker_command(tmp_path)
    clock = {"t": 1000.0}
    report = dispatch_wave(
        store, "run-1", store.list_units("run-1"), RunAdapter(), cli=_cli(_untrusted_runner()),
        worker_command=wc, launch_confirm_seconds=2.0,
        sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s), clock_fn=lambda: clock["t"],
    )
    assert report.aborted_reason == "launch_misconfiguration"
    assert report.launch_rc == 1
    assert "Workspace not trusted" in report.launch_stderr


def test_report_launch_fields_default_empty_on_ordinary_wave(tmp_path):
    store = _seeded_dispatch_store(tmp_path, unit_ids=())
    report = dispatch_wave(
        store, "run-1", [], RunAdapter(), cli=_cli(_healthy_runner()),
        worker_command=_worker_command(tmp_path), at=1000.0,
        sleep_fn=lambda s: None, clock_fn=lambda: 1000.0,
    )
    assert report.launch_rc is None
    assert report.launch_stderr is None


# --- item 3: the dispatcher lease across a slow refill ---------------------


def test_dispatcher_lease_holds_across_slow_launches(tmp_path):
    ids = ("u0", "u1", "u2", "u3")
    store = _seeded_dispatch_store(tmp_path, unit_ids=ids)
    wc = _worker_command(tmp_path)
    clock = {"t": 1000.0}
    rivals = []

    def _slow_launch(n):
        clock["t"] += 70.0  # each launch takes over half the dispatcher lease
        rivals.append(
            store.acquire_dispatcher_lease("run-1", "rival", lease_seconds=120.0, at=clock["t"])
        )

    report = dispatch_wave(
        store, "run-1", store.list_units("run-1"), RunAdapter(),
        cli=_cli(_LiveRunner(on_launch=_slow_launch, state="done")), worker_command=wc,
        max_agents=4, sleep_fn=lambda s: None, clock_fn=lambda: clock["t"],
    )
    assert len(rivals) == 4
    assert all(r is None for r in rivals), "a rival dispatcher took the lease mid-refill"
    assert len(report.dispatched) == 4


def test_refill_stops_when_another_dispatcher_holds_the_lease(tmp_path):
    store = _seeded_dispatch_store(tmp_path, unit_ids=("u0", "u1"))
    wc = _worker_command(tmp_path)
    runner = _LiveRunner()
    calls = {"n": 0}
    real_acquire = store.acquire_dispatcher_lease

    def _lose_after_first(run_id, dispatcher_id, **kw):
        calls["n"] += 1
        if calls["n"] >= 2:
            return None
        return real_acquire(run_id, dispatcher_id, **kw)

    store.acquire_dispatcher_lease = _lose_after_first
    report = dispatch_wave(
        store, "run-1", store.list_units("run-1"), RunAdapter(), cli=_cli(runner),
        worker_command=wc, max_agents=2, at=1000.0,
        sleep_fn=lambda s: None, clock_fn=lambda: 1000.0,
    )
    assert report.aborted_reason == "dispatcher_lease_lost"
    assert runner.launches == 0
