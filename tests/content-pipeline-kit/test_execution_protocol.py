"""Tests for content_pipeline.execution.protocol.

Pins the A-min.3 worker protocol: every verb (prepare, claim, read, submit,
fail, renew, status, pause, resume, finalize); malformed-envelope refusal;
protocol-version-incompatibility refusal; adapter-version-incompatibility
refusal (D1) on resume/finalize/prepare; claim fencing surfaced through
``submit``; and -- the A-min.3 exit criterion -- several SEPARATE, short-lived
subprocesses driving one run through the protocol with no process-local
continuity (each subprocess constructs its own ``ExecutionStore`` and
``RunAdapter`` from scratch; only the SQLite file is shared).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from content_pipeline.execution.adapter import RunAdapter
from content_pipeline.execution.model import StaleFenceError, UnitState
from content_pipeline.execution.protocol import (
    PROTOCOL_VERSION,
    MalformedEnvelopeError,
    ProtocolVersionError,
    UnknownVerbError,
    build_handlers,
    dispatch,
)
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.pipeline.workunit import FlatChunkStrategy, WorkUnit
from content_pipeline.validate import contract

LIB_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir,
                 "plugins", "content-pipeline-kit", "lib")
)

FLAT_STRATEGY = FlatChunkStrategy(select=lambda store: [])


def _new_store(tmp_path, name="run.db") -> ExecutionStore:
    return ExecutionStore(tmp_path / name)


def _seeded_store(tmp_path, *, unit_ids=("u0", "u1"), adapter_version="") -> ExecutionStore:
    store = _new_store(tmp_path)
    store.create_run(
        "run-1", driver="inline", backend="mock", model="m", adapter_version=adapter_version
    )
    store.register_units("run-1", list(unit_ids))
    return store


def _adapter(*, adapter_version="", reject_until="ok"):
    def validator(candidate, context):
        if candidate != reject_until:
            return [contract.Rejection(kind="not_ok", severity=contract.Severity.HARD, detail="nope")]
        return []

    return RunAdapter(
        user_for=lambda u: f"user:{u.id}",
        parse_fn=lambda t: t,
        validators=(validator,),
        apply=lambda uid, payload: None,
        adapter_version=adapter_version,
    )


def _envelope(verb, payload=None, *, version=PROTOCOL_VERSION):
    return {"protocol_version": version, "verb": verb, "payload": payload or {}}


# -- malformed envelopes -------------------------------------------------------


@pytest.mark.parametrize(
    "envelope",
    [
        "not a dict",
        [],
        123,
        {"verb": "claim"},  # missing protocol_version
        {"protocol_version": PROTOCOL_VERSION},  # missing verb
        {"protocol_version": PROTOCOL_VERSION, "verb": ""},  # empty verb
        {"protocol_version": PROTOCOL_VERSION, "verb": "claim", "payload": "not a dict"},
    ],
)
def test_dispatch_refuses_malformed_envelope(envelope, tmp_path):
    store = _seeded_store(tmp_path)
    handlers = build_handlers(store, _adapter(), strategy=FLAT_STRATEGY)
    result = dispatch(envelope, handlers)
    assert result["ok"] is False
    assert result["error"]["type"] == "MalformedEnvelopeError"


def test_dispatch_refuses_unknown_verb(tmp_path):
    store = _seeded_store(tmp_path)
    handlers = build_handlers(store, _adapter(), strategy=FLAT_STRATEGY)
    result = dispatch(_envelope("teleport"), handlers)
    assert result["ok"] is False
    assert result["error"]["type"] == "UnknownVerbError"


def test_dispatch_refuses_protocol_version_mismatch(tmp_path):
    store = _seeded_store(tmp_path)
    handlers = build_handlers(store, _adapter(), strategy=FLAT_STRATEGY)
    result = dispatch(_envelope("status", {"run_id": "run-1"}, version="999"), handlers)
    assert result["ok"] is False
    assert result["error"]["type"] == "ProtocolVersionError"


def test_dispatch_never_raises_it_always_returns_a_typed_reply(tmp_path):
    """The core 'refused loudly, not a traceback' contract: a handler
    exception (here, an unknown run) is caught by dispatch and rendered,
    never propagated to the caller."""
    store = _seeded_store(tmp_path)
    handlers = build_handlers(store, _adapter(), strategy=FLAT_STRATEGY)
    result = dispatch(_envelope("status", {"run_id": "no-such-run"}), handlers)
    assert result["ok"] is False
    assert result["error"]["type"] == "KeyError"


# -- direct handler exceptions unaffected by dispatch's catch-all ------------


def test_direct_handler_call_raises_normally_outside_dispatch(tmp_path):
    """A caller may bypass `dispatch` and call a handler directly (dispatch
    is a convenience, not a requirement) -- Python exceptions propagate
    normally in that mode."""
    store = _seeded_store(tmp_path)
    handlers = build_handlers(store, _adapter(), strategy=FLAT_STRATEGY)
    with pytest.raises(Exception):
        handlers["claim"]({"run_id": "run-1"})  # missing unit_id/worker_id


# -- one full lifecycle through every verb, in one process --------------------


def test_full_lifecycle_through_every_verb(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    adapter = _adapter()
    handlers = build_handlers(store, adapter, strategy=FLAT_STRATEGY)

    prepared = dispatch(_envelope("prepare", {"run_id": "run-1", "unit_ids": ["u0"]}), handlers)
    assert prepared["ok"] is True
    assert [u["unit_id"] for u in prepared["result"]["wave"]] == ["u0"]

    claimed = dispatch(
        _envelope("claim", {"run_id": "run-1", "unit_id": "u0", "worker_id": "w1"}), handlers
    )
    assert claimed["ok"] is True
    fencing_token = claimed["result"]["fencing_token"]

    read = dispatch(_envelope("read", {"run_id": "run-1", "unit_id": "u0"}), handlers)
    assert read["ok"] is True
    assert read["result"]["user"] == "user:u0"
    assert read["result"]["system"] == ""

    renewed = dispatch(
        _envelope(
            "renew",
            {"run_id": "run-1", "unit_id": "u0", "fencing_token": fencing_token},
        ),
        handlers,
    )
    assert renewed["ok"] is True

    rejected = dispatch(
        _envelope(
            "submit",
            {"run_id": "run-1", "unit_id": "u0", "fencing_token": fencing_token, "text": "bad"},
        ),
        handlers,
    )
    assert rejected["ok"] is True
    assert rejected["result"]["accepted"] is False
    assert "not_ok" in rejected["result"]["feedback"]
    # Rejected: the unit is still claimed under the SAME fencing token (D1:
    # adjudication happened, but nothing was accepted -- the worker retries).
    assert store.get_unit("run-1", "u0").state is UnitState.CLAIMED

    accepted = dispatch(
        _envelope(
            "submit",
            {"run_id": "run-1", "unit_id": "u0", "fencing_token": fencing_token, "text": "ok"},
        ),
        handlers,
    )
    assert accepted["ok"] is True
    assert accepted["result"]["accepted"] is True
    assert store.get_unit("run-1", "u0").state is UnitState.ACCEPTED

    status = dispatch(_envelope("status", {"run_id": "run-1"}), handlers)
    assert status["ok"] is True
    assert status["result"]["counts_by_state"]["accepted"] == 1

    paused = dispatch(_envelope("pause", {"run_id": "run-1", "detail": "operator"}), handlers)
    assert paused["ok"] is True
    assert store.get_run("run-1").halted_kind == "pause"

    resumed = dispatch(_envelope("resume", {"run_id": "run-1"}), handlers)
    assert resumed["ok"] is True
    assert store.get_run("run-1").halted_kind is None

    finalized = dispatch(_envelope("finalize", {"run_id": "run-1"}), handlers)
    assert finalized["ok"] is True
    assert finalized["result"]["applied"] == ["u0"]


def test_fail_verb_terminal_and_nonterminal(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0", "u1"))
    handlers = build_handlers(store, _adapter(), strategy=FLAT_STRATEGY)

    claimed = dispatch(
        _envelope("claim", {"run_id": "run-1", "unit_id": "u0", "worker_id": "w1"}), handlers
    )
    token = claimed["result"]["fencing_token"]
    failed = dispatch(
        _envelope(
            "fail",
            {"run_id": "run-1", "unit_id": "u0", "fencing_token": token, "error": "boom"},
        ),
        handlers,
    )
    assert failed["ok"] is True
    assert failed["result"]["state"] == "pending"
    assert store.get_unit("run-1", "u0").state is UnitState.PENDING

    claimed2 = dispatch(
        _envelope("claim", {"run_id": "run-1", "unit_id": "u1", "worker_id": "w1"}), handlers
    )
    token2 = claimed2["result"]["fencing_token"]
    failed2 = dispatch(
        _envelope(
            "fail",
            {
                "run_id": "run-1",
                "unit_id": "u1",
                "fencing_token": token2,
                "error": "boom",
                "terminal": True,
            },
        ),
        handlers,
    )
    assert failed2["result"]["state"] == "failed"
    assert store.get_unit("run-1", "u1").state is UnitState.FAILED


def test_prepare_without_strategy_configured_is_a_clean_refusal(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    handlers = build_handlers(store, _adapter())  # no strategy=
    result = dispatch(_envelope("prepare", {"run_id": "run-1", "unit_ids": ["u0"]}), handlers)
    assert result["ok"] is False
    assert result["error"]["type"] == "ValueError"


def test_prepare_surfaces_unapplied_predecessor_refusal_cleanly(tmp_path):
    """prepare_run's existing UnappliedPredecessorError refusal (committed at
    8fff1cc) must come back through `prepare` as a clean typed protocol
    error, not an unhandled traceback -- per this task's stated premise."""
    from content_pipeline.pipeline.workunit import GraphWalkStrategy

    store = _new_store(tmp_path)
    store.create_run("run-1", driver="inline", backend="mock", model="m", adapter_version="")
    store.register_units("run-1", ["u0", "u1"])
    graph_strategy = GraphWalkStrategy(order=lambda src: ["u0", "u1"])
    handlers = build_handlers(store, _adapter(), strategy=graph_strategy, graph_source=object())

    claim = store.claim_unit("run-1", "u0", "w1")
    store.accept_unit("run-1", "u0", claim.fencing_token, text="ok")  # ACCEPTED, never applied

    result = dispatch(_envelope("prepare", {"run_id": "run-1", "unit_ids": ["u1"]}), handlers)
    assert result["ok"] is False
    assert result["error"]["type"] == "UnappliedPredecessorError"


# -- claim fencing surfaced through submit ------------------------------------


def test_submit_with_stale_fencing_token_is_refused(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    handlers = build_handlers(store, _adapter(), strategy=FLAT_STRATEGY)

    claimed = dispatch(
        _envelope("claim", {"run_id": "run-1", "unit_id": "u0", "worker_id": "w1"}), handlers
    )
    stale_token = claimed["result"]["fencing_token"]

    # Force a lease-expiry reclaim so the fencing token advances past `stale_token`.
    store.fail_unit("run-1", "u0", stale_token, terminal=False)
    reclaimed = dispatch(
        _envelope("claim", {"run_id": "run-1", "unit_id": "u0", "worker_id": "w2"}), handlers
    )
    assert reclaimed["result"]["fencing_token"] != stale_token

    result = dispatch(
        _envelope(
            "submit",
            {"run_id": "run-1", "unit_id": "u0", "fencing_token": stale_token, "text": "ok"},
        ),
        handlers,
    )
    assert result["ok"] is False
    assert result["error"]["type"] == "StaleFenceError"


# -- adapter-version incompatible resume (D1) ---------------------------------


@pytest.mark.parametrize("verb,payload", [
    ("resume", {"run_id": "run-1"}),
    ("finalize", {"run_id": "run-1"}),
])
def test_incompatible_adapter_version_is_refused(verb, payload, tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0",), adapter_version="v1")
    handlers = build_handlers(store, _adapter(adapter_version="v2"), strategy=FLAT_STRATEGY)
    result = dispatch(_envelope(verb, payload), handlers)
    assert result["ok"] is False
    assert result["error"]["type"] == "AdapterVersionMismatchError"


def test_prepare_also_refuses_incompatible_adapter_version(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0",), adapter_version="v1")
    handlers = build_handlers(store, _adapter(adapter_version="v2"), strategy=FLAT_STRATEGY)
    result = dispatch(_envelope("prepare", {"run_id": "run-1", "unit_ids": ["u0"]}), handlers)
    assert result["ok"] is False
    assert result["error"]["type"] == "AdapterVersionMismatchError"


def test_matching_adapter_version_is_accepted(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0",), adapter_version="v1")
    handlers = build_handlers(store, _adapter(adapter_version="v1"), strategy=FLAT_STRATEGY)
    result = dispatch(_envelope("resume", {"run_id": "run-1"}), handlers)
    assert result["ok"] is True


# -- exit criterion: several SEPARATE short-lived processes, no shared state --
# except the SQLite file. Each subprocess script constructs its own store AND
# its own RunAdapter from scratch (a fresh interpreter has no access to any
# Python object from another process) -- only durable rows in the db file are
# what makes the second process's `submit` legal against the first process's
# `claim`.

_CLAIM_SCRIPT = """
import json
import sys
sys.path.insert(0, sys.argv[1])
from content_pipeline.execution.adapter import RunAdapter
from content_pipeline.execution.protocol import PROTOCOL_VERSION, build_handlers, dispatch
from content_pipeline.execution.store import ExecutionStore

db_path, run_id, unit_id, worker_id = sys.argv[2:6]
store = ExecutionStore(db_path)
adapter = RunAdapter(user_for=lambda u: "user:" + u.id, parse_fn=lambda t: t)
handlers = build_handlers(store, adapter)
envelope = {
    "protocol_version": PROTOCOL_VERSION,
    "verb": "claim",
    "payload": {"run_id": run_id, "unit_id": unit_id, "worker_id": worker_id},
}
print(json.dumps(dispatch(envelope, handlers)))
"""

_READ_THEN_SUBMIT_SCRIPT = """
import json
import sys
sys.path.insert(0, sys.argv[1])
from content_pipeline.execution.adapter import RunAdapter
from content_pipeline.execution.protocol import PROTOCOL_VERSION, build_handlers, dispatch
from content_pipeline.execution.store import ExecutionStore

db_path, run_id, unit_id, fencing_token, text = sys.argv[2:7]
store = ExecutionStore(db_path)
adapter = RunAdapter(user_for=lambda u: "user:" + u.id, parse_fn=lambda t: t)
handlers = build_handlers(store, adapter)
read_envelope = {
    "protocol_version": PROTOCOL_VERSION,
    "verb": "read",
    "payload": {"run_id": run_id, "unit_id": unit_id},
}
read_result = dispatch(read_envelope, handlers)
submit_envelope = {
    "protocol_version": PROTOCOL_VERSION,
    "verb": "submit",
    "payload": {
        "run_id": run_id,
        "unit_id": unit_id,
        "fencing_token": int(fencing_token),
        "text": text,
    },
}
submit_result = dispatch(submit_envelope, handlers)
print(json.dumps({"read": read_result, "submit": submit_result}))
"""


def _run_subprocess(script, *args) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", script, LIB_ROOT, *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


def test_subprocesses_claim_and_submit_with_no_process_local_continuity(tmp_path):
    db_path = tmp_path / "run.db"
    store = ExecutionStore(db_path)
    store.create_run("run-1", driver="inline", backend="mock", model="m", adapter_version="")
    store.register_units("run-1", ["u0"])

    claim_reply = _run_subprocess(_CLAIM_SCRIPT, str(db_path), "run-1", "u0", "worker-a")
    assert claim_reply["ok"] is True
    fencing_token = claim_reply["result"]["fencing_token"]

    submit_reply = _run_subprocess(
        _READ_THEN_SUBMIT_SCRIPT, str(db_path), "run-1", "u0", str(fencing_token), "hello"
    )
    assert submit_reply["read"]["ok"] is True
    assert submit_reply["read"]["result"]["user"] == "user:u0"
    assert submit_reply["submit"]["ok"] is True
    assert submit_reply["submit"]["result"]["accepted"] is True

    # The only channel between the two subprocesses was the db file -- confirm
    # the accept landed durably, observed from a THIRD store handle (this
    # test process's own, never used by either subprocess).
    unit = store.get_unit("run-1", "u0")
    assert unit.state is UnitState.ACCEPTED
    assert unit.accepted_text == "hello"


def test_subprocesses_work_against_a_windows_path_containing_a_space(tmp_path):
    """A-min.3's stated test list names Windows paths explicitly. Directory
    names with spaces are the classic Windows-argv-quoting trap; `subprocess.run`
    with a list (no shell=True) must pass it through as one argv token
    regardless."""
    db_dir = tmp_path / "release notes (v1)"
    db_dir.mkdir()
    db_path = db_dir / "run.db"
    store = ExecutionStore(db_path)
    store.create_run("run-1", driver="inline", backend="mock", model="m", adapter_version="")
    store.register_units("run-1", ["u0"])

    claim_reply = _run_subprocess(_CLAIM_SCRIPT, str(db_path), "run-1", "u0", "worker-a")
    assert claim_reply["ok"] is True
    fencing_token = claim_reply["result"]["fencing_token"]

    submit_reply = _run_subprocess(
        _READ_THEN_SUBMIT_SCRIPT, str(db_path), "run-1", "u0", str(fencing_token), "hello"
    )
    assert submit_reply["submit"]["ok"] is True
    assert store.get_unit("run-1", "u0").state is UnitState.ACCEPTED
