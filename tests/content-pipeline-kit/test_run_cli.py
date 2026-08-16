"""Tests for content_pipeline.cli.run -- command adapters over execution.

Pins that each handler is a thin argv-parse-then-store-call adapter: it
returns a YAML-able result through cli.scaffold.dispatch, and every state
transition, fencing check, and status computation is delegated to
``execution`` (no logic duplicated here).
"""

from __future__ import annotations

import io

import pytest

from content_pipeline.cli.run import build_commands
from content_pipeline.cli.scaffold import EXIT_ERROR, EXIT_OK, EXIT_USAGE, dispatch
from content_pipeline.execution.model import UnitState
from content_pipeline.execution.store import ExecutionStore


@pytest.fixture
def store(tmp_path) -> ExecutionStore:
    return ExecutionStore(tmp_path / "run.db")


@pytest.fixture
def commands(store):
    return build_commands(store)


def _dispatch(commands, argv):
    out, err = io.StringIO(), io.StringIO()
    code = dispatch(argv, commands, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_create_run_then_register_units(commands, store):
    code, out, _err = _dispatch(commands, ["create-run", "r1", "inline", "mock", "m", "v1"])
    assert code == EXIT_OK
    assert "id: r1" in out

    code, out, _err = _dispatch(commands, ["register-units", "r1", "u0", "u1"])
    assert code == EXIT_OK
    assert "registered: 2" in out
    assert len(store.list_units("r1")) == 2


def test_claim_renew_accept_round_trip(commands, store):
    store.create_run("r1", driver="inline", backend="mock", model="m", adapter_version="v1")
    store.register_units("r1", ["u0"])

    code, out, _err = _dispatch(commands, ["claim", "r1", "u0", "worker-a"])
    assert code == EXIT_OK
    assert "fencing_token: 1" in out

    code, out, _err = _dispatch(commands, ["renew", "r1", "u0", "1", "--lease-seconds=60"])
    assert code == EXIT_OK
    assert "lease_expires_at" in out

    code, out, _err = _dispatch(
        commands, ["accept", "r1", "u0", "1", "--input-tokens=5", "--output-tokens=7"]
    )
    assert code == EXIT_OK
    assert "state: accepted" in out
    unit = store.get_unit("r1", "u0")
    assert unit.state is UnitState.ACCEPTED


def test_fail_terminal_vs_retry(commands, store):
    store.create_run("r1", driver="inline", backend="mock", model="m", adapter_version="v1")
    store.register_units("r1", ["u0", "u1"])

    store.claim_unit("r1", "u0", "worker-a")
    code, out, _err = _dispatch(commands, ["fail", "r1", "u0", "1", "--error=oops"])
    assert code == EXIT_OK
    assert "state: pending" in out
    assert store.get_unit("r1", "u0").state is UnitState.PENDING

    store.claim_unit("r1", "u1", "worker-a")
    code, out, _err = _dispatch(commands, ["fail", "r1", "u1", "1", "--error=dead", "--terminal"])
    assert code == EXIT_OK
    assert "state: failed" in out
    assert store.get_unit("r1", "u1").state is UnitState.FAILED


def test_halt_and_clear_halt(commands, store):
    """Pause/resume are deliberately not shipped here -- A-min.1's
    ``claim_unit`` has no gate for a 'paused' bit, and shipping a run-control
    verb that silently does not block claims would be worse than not
    shipping it. Pause/halt semantics land together in A-min.2 (D4)."""
    store.create_run("r1", driver="inline", backend="mock", model="m", adapter_version="v1")

    code, out, _err = _dispatch(commands, ["halt", "r1", "rate_limit", "--detail=hit your limit"])
    assert code == EXIT_OK
    assert store.get_run("r1").halted_kind == "rate_limit"

    code, out, _err = _dispatch(commands, ["clear-halt", "r1"])
    assert code == EXIT_OK
    assert store.get_run("r1").halted_kind is None


def test_pause_and_resume_are_not_shipped_commands(commands):
    code, _out, err = _dispatch(commands, ["pause", "r1"])
    assert code == EXIT_USAGE
    assert "unknown command" in err
    code, _out, err = _dispatch(commands, ["resume", "r1"])
    assert code == EXIT_USAGE
    assert "unknown command" in err


def test_status_renders_bounded_digest(commands, store):
    store.create_run("r1", driver="inline", backend="mock", model="m", adapter_version="v1")
    store.register_units("r1", ["u0", "u1"])
    store.claim_unit("r1", "u0", "worker-a")

    code, out, _err = _dispatch(commands, ["status", "r1"])
    assert code == EXIT_OK
    assert "run_id: r1" in out
    assert "counts_by_state" in out


def test_missing_required_argument_maps_to_exit_error(commands):
    code, _out, err = _dispatch(commands, ["create-run", "r1"])
    assert code == EXIT_ERROR
    assert "missing required argument" in err


def test_unknown_command_gets_did_you_mean(commands):
    code, _out, err = _dispatch(commands, ["statu"])
    assert "Did you mean: status" in err
