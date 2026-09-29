"""A fail envelope's optional fixed code, the breaker that counts only
systemic codes, and adopted-failure classification."""

from __future__ import annotations

import json

import pytest

from content_pipeline.cli.run import build_commands
from content_pipeline.execution import model
from content_pipeline.execution.adapter import RunAdapter
from content_pipeline.execution.controller import resume_run
from content_pipeline.execution.drivers.claude_bg import dispatch_wave
from content_pipeline.execution.model import HALT_REPEATED_FAILURE, UnitState
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.execution.workerpack import WorkerCommand, worker_envelopes_for
from test_execution_driver_claude_bg import _seeded_dispatch_store, _worker_command
from test_review_fix_h_bg_followups import _LiveRunner, _cli, _no_real_claude_subprocess  # noqa: F401
from test_support.claude_bg_fakes import _bg_record, _healthy_runner

RUN = "r"


# --- protocol: the fail envelope's code -------------------------------------


@pytest.fixture
def mount(tmp_path):
    store = ExecutionStore(tmp_path / "r.db")
    store.create_run(RUN, driver="claude-bg", backend="b", model="m", adapter_version="")
    store.register_units(RUN, ["A"])
    wc = WorkerCommand(argv=("python", "mount.py"), answer_dir=str(tmp_path))
    return store, build_commands(store, adapter=RunAdapter()), wc


def _send_fail(store, commands, wc, **extra):
    token = store.claim_unit(RUN, "A", "wA").fencing_token
    path, _ = worker_envelopes_for(wc, RUN, "A", "wA")["fail"]
    payload = {"run_id": RUN, "unit_id": "A", "worker_id": "wA", "fencing_token": token,
               "terminal": True, "error": "cwd differs from the run's"}
    payload.update(extra)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"protocol_version": "1", "verb": "fail", "payload": payload}))
    return commands["protocol"].handler(["@" + path])


def _last_fail_error(store):
    return [a for a in store.list_attempts(RUN, "A") if a.kind.value == "fail"][-1].error


def test_fail_code_is_recorded_structurally(mount):
    store, commands, wc = mount
    reply = _send_fail(store, commands, wc, code="env_mismatch")
    assert reply["ok"] is True
    assert model.failure_code(_last_fail_error(store)) == "env_mismatch"
    assert "cwd differs" in _last_fail_error(store)


def test_fail_code_survives_a_long_detail(mount):
    store, commands, wc = mount
    _send_fail(store, commands, wc, code="env_mismatch", error="x" * 5000)
    error = _last_fail_error(store)
    assert len(error) <= 500
    assert model.failure_code(error) == "env_mismatch"


def test_unknown_fail_code_is_refused_and_the_unit_stays_claimed(mount):
    store, commands, wc = mount
    reply = _send_fail(store, commands, wc, code="because")
    assert reply["ok"] is False
    assert "code" in json.dumps(reply)
    assert store.get_unit(RUN, "A").state is UnitState.CLAIMED


def test_non_string_fail_code_is_refused(mount):
    store, commands, wc = mount
    assert _send_fail(store, commands, wc, code=7)["ok"] is False


def test_fail_without_a_code_is_unchanged(mount):
    store, commands, wc = mount
    assert _send_fail(store, commands, wc)["ok"] is True
    assert _last_fail_error(store) == "cwd differs from the run's"
    assert model.failure_code(_last_fail_error(store)) is None


@pytest.mark.parametrize("text", [None, "", "plain text", "{not json", '["env_mismatch"]',
                                  '{"code": "made_up"}', '{"code": 3}'])
def test_failure_code_reads_only_a_known_code(text):
    assert model.failure_code(text) is None


# --- breaker ---------------------------------------------------------------


def _run_failing_wave(tmp_path, failures, **kwargs):
    """``failures``: one ``(code, detail)`` per unit."""
    ids = tuple(f"u{i}" for i in range(len(failures)))
    store = _seeded_dispatch_store(tmp_path, unit_ids=ids)
    by_unit = dict(zip(ids, failures))

    def _fail_claimed(_seconds):
        for u in store.list_units("run-1"):
            if u.state is UnitState.CLAIMED:
                code, detail = by_unit[u.unit_id]
                error = model.encode_failure(code, detail) if code else detail
                store.fail_unit("run-1", u.unit_id, u.fencing_token, error=error, terminal=True)

    report = dispatch_wave(
        store, "run-1", store.list_units("run-1"), RunAdapter(), cli=_cli(_LiveRunner()),
        worker_command=_worker_command(tmp_path), max_agents=1,
        sleep_fn=_fail_claimed, clock_fn=lambda: 1000.0, **kwargs,
    )
    return store, report


def test_env_mismatch_failures_halt_at_the_threshold(tmp_path):
    store, report = _run_failing_wave(tmp_path, [("env_mismatch", f"d{i}") for i in range(5)])
    assert report.halted == HALT_REPEATED_FAILURE
    assert len(report.dispatched) == 3
    assert store.get_run("run-1").halted_kind == HALT_REPEATED_FAILURE
    resume_run(store, "run-1")
    assert store.get_run("run-1").halted_kind is None


def test_threshold_is_configurable(tmp_path):
    _, report = _run_failing_wave(
        tmp_path, [("env_mismatch", "d")] * 5, systemic_failure_halt_threshold=2
    )
    assert report.halted == HALT_REPEATED_FAILURE
    assert len(report.dispatched) == 2


def test_identical_uncoded_failures_do_not_halt(tmp_path):
    store, report = _run_failing_wave(tmp_path, [(None, "same explanation")] * 6)
    assert report.halted is None
    assert len(report.dispatched) == 6


def test_long_failures_sharing_a_prefix_do_not_halt(tmp_path):
    _, report = _run_failing_wave(tmp_path, [(None, "p" * 600 + str(i)) for i in range(6)])
    assert report.halted is None


def test_fewer_coded_failures_than_threshold_do_not_halt(tmp_path):
    _, report = _run_failing_wave(
        tmp_path, [("env_mismatch", "a"), ("env_mismatch", "b"), (None, "c"), (None, "d")]
    )
    assert report.halted is None


@pytest.mark.parametrize("disabled", [0, None])
def test_threshold_disabled_never_halts(tmp_path, disabled):
    _, report = _run_failing_wave(
        tmp_path, [("env_mismatch", "d")] * 5, systemic_failure_halt_threshold=disabled
    )
    assert report.halted is None
    assert len(report.dispatched) == 5


@pytest.mark.parametrize("bad", [-1, -5, True, 2.5, "3"])
def test_invalid_threshold_is_refused_before_any_work(tmp_path, bad):
    runner = _LiveRunner()
    store = _seeded_dispatch_store(tmp_path)
    with pytest.raises(ValueError):
        dispatch_wave(
            store, "run-1", store.list_units("run-1"), RunAdapter(), cli=_cli(runner),
            worker_command=_worker_command(tmp_path), systemic_failure_halt_threshold=bad,
            clock_fn=lambda: 1000.0, sleep_fn=lambda s: None,
        )
    assert runner.launches == 0


# --- adopted failures ------------------------------------------------------


def _adopt(tmp_path, *, terminal_action, n_units=1, wave=False):
    """Durable open dispatch rows whose sessions are still listed, with
    each unit already terminal by the dead dispatcher's worker."""
    ids = tuple(f"u{i}" for i in range(n_units))
    store = _seeded_dispatch_store(tmp_path, unit_ids=ids)
    sessions = []
    for i, uid in enumerate(ids):
        worker = f"worker-{i}"
        token = store.claim_unit("run-1", uid, worker, lease_seconds=100.0, at=1000.0).fencing_token
        store.record_dispatch("run-1", uid, worker, session_id=f"sess-{i}", at=1000.0)
        sessions.append(_bg_record(id=f"{i + 1:08x}", session_id=f"sess-{i}", state="done"))
        terminal_action(store, uid, token, i)
    runner = _healthy_runner(agents_json_body=json.dumps(sessions))
    return store, dispatch_wave(
        store, "run-1", [], RunAdapter(), cli=_cli(runner),
        worker_command=_worker_command(tmp_path), at=1001.0,
        sleep_fn=lambda s: None, clock_fn=lambda: 1001.0,
    )


def test_adopted_failed_unit_is_reported_worker_failed_not_superseded(tmp_path):
    def fail(store, uid, token, i):
        store.fail_unit("run-1", uid, token, error="boom", terminal=True, at=1000.5)

    store, report = _adopt(tmp_path, terminal_action=fail)
    assert report.settled == {"u0": "worker_failed"}


def test_adopted_accepted_unit_is_reported_accepted_not_superseded(tmp_path):
    def accept(store, uid, token, i):
        store.accept_unit("run-1", uid, token, text="t", at=1000.5)

    store, report = _adopt(tmp_path, terminal_action=accept)
    assert report.settled == {"u0": "accepted"}
    assert report.accepted == ("u0",)


def test_adopted_unit_reclaimed_by_another_worker_is_still_superseded(tmp_path):
    def reclaim_and_fail(store, uid, token, i):
        token2 = store.claim_unit("run-1", uid, "other", lease_seconds=100.0, at=1200.0).fencing_token
        store.fail_unit("run-1", uid, token2, error="x", terminal=True, at=1201.0)

    store, report = _adopt(tmp_path, terminal_action=reclaim_and_fail)
    assert report.settled == {"u0": "superseded"}


def test_adopted_env_mismatch_failures_feed_the_breaker(tmp_path):
    def fail(store, uid, token, i):
        store.fail_unit(
            "run-1", uid, token, error=model.encode_failure("env_mismatch", "cwd"),
            terminal=True, at=1000.5,
        )

    store, report = _adopt(tmp_path, terminal_action=fail, n_units=3)
    assert report.halted == HALT_REPEATED_FAILURE
    assert store.get_run("run-1").halted_kind == HALT_REPEATED_FAILURE
