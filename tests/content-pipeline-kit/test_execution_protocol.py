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
import time

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
    never propagated to the caller. Also pins that `status` surfaces the
    SAME typed error every sibling verb does for an unknown run
    (`UnknownRunError`), not a raw `KeyError` -- `_status` used to call
    `compute_status` directly instead of `_get_run_or_raise` the way every
    other verb does, and `compute_status`'s own docstring says a caller
    wanting a typed error must call `store.get_run` first. This test
    previously pinned that inconsistency (asserting `KeyError`) instead of
    catching it."""
    store = _seeded_store(tmp_path)
    handlers = build_handlers(store, _adapter(), strategy=FLAT_STRATEGY)
    result = dispatch(_envelope("status", {"run_id": "no-such-run"}), handlers)
    assert result["ok"] is False
    assert result["error"]["type"] == "UnknownRunError"


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


def test_fail_verb_rejects_a_truthy_non_boolean_terminal_flag(tmp_path):
    """Defect 4 (grok-4.6 review of 46d4a2b): `_fail` used to compute
    `terminal = bool(payload.get("terminal", False))`. `bool("false")` and
    `bool("0")` are both `True` in Python -- so a model emitting the JSON
    string `"terminal": "false"` (rather than the JSON boolean `false`)
    would permanently fail a unit that should have been retried, with no
    way back once `FAILED` is terminal. The JSON verb must refuse a
    non-boolean `terminal` loudly instead of silently coercing it."""
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    handlers = build_handlers(store, _adapter(), strategy=FLAT_STRATEGY)

    claimed = dispatch(
        _envelope("claim", {"run_id": "run-1", "unit_id": "u0", "worker_id": "w1"}), handlers
    )
    token = claimed["result"]["fencing_token"]
    result = dispatch(
        _envelope(
            "fail",
            {
                "run_id": "run-1",
                "unit_id": "u0",
                "fencing_token": token,
                "error": "boom",
                "terminal": "false",
            },
        ),
        handlers,
    )
    assert result["ok"] is False
    assert result["error"]["type"] == "MalformedEnvelopeError"
    # The unit must not have been permanently failed by the rejected call.
    assert store.get_unit("run-1", "u0").state is UnitState.CLAIMED


# -- worker-controlled lease_seconds is bounded, never unbounded (defect 3) --


def test_claim_rejects_a_non_finite_lease_seconds_override(tmp_path):
    """Defect 3 (grok-4.6 review of 46d4a2b): mount-time `lease_seconds` is
    policy (default 300), but `_claim`/`_renew` used to honor an UNBOUNDED
    payload override. `json.loads("1e309")` parses to `inf`; `now + inf` is
    `inf`; `store.claim_unit` treats `lease_expires_at > now` as live
    forever -- so a worker (in phase B, a MODEL) could pin a unit with no
    protocol-surface recovery once it died. This matters more in phase B
    because the envelope carrying this value is MODEL-authored, i.e.
    untrusted data by the module's own stated security posture."""
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    handlers = build_handlers(store, _adapter(), strategy=FLAT_STRATEGY)
    result = dispatch(
        _envelope(
            "claim",
            {"run_id": "run-1", "unit_id": "u0", "worker_id": "w1", "lease_seconds": 1e309},
        ),
        handlers,
    )
    assert result["ok"] is False
    assert result["error"]["type"] == "MalformedEnvelopeError"


def test_claim_rejects_a_non_positive_lease_seconds_override(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    handlers = build_handlers(store, _adapter(), strategy=FLAT_STRATEGY)
    result = dispatch(
        _envelope(
            "claim",
            {"run_id": "run-1", "unit_id": "u0", "worker_id": "w1", "lease_seconds": 0},
        ),
        handlers,
    )
    assert result["ok"] is False
    assert result["error"]["type"] == "MalformedEnvelopeError"


def test_claim_lease_seconds_override_cannot_exceed_mount_policy(tmp_path):
    """A worker may request a SHORTER lease than mount policy, but never a
    longer one -- mount-time `lease_seconds` is trusted policy (the module's
    own security posture section); a payload is untrusted data and must
    never be able to override policy upward, only within it."""
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    handlers = build_handlers(store, _adapter(), strategy=FLAT_STRATEGY, lease_seconds=300)
    result = dispatch(
        _envelope(
            "claim",
            {"run_id": "run-1", "unit_id": "u0", "worker_id": "w1", "lease_seconds": 10_000_000},
        ),
        handlers,
    )
    assert result["ok"] is True
    assert result["result"]["lease_expires_at"] <= time.time() + 300 + 1


def test_renew_rejects_a_non_finite_lease_seconds_override(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    handlers = build_handlers(store, _adapter(), strategy=FLAT_STRATEGY)
    claimed = dispatch(
        _envelope("claim", {"run_id": "run-1", "unit_id": "u0", "worker_id": "w1"}), handlers
    )
    token = claimed["result"]["fencing_token"]
    result = dispatch(
        _envelope(
            "renew",
            {"run_id": "run-1", "unit_id": "u0", "fencing_token": token, "lease_seconds": float("inf")},
        ),
        handlers,
    )
    assert result["ok"] is False
    assert result["error"]["type"] == "MalformedEnvelopeError"


# -- submit's text: str only, "" allowed (defect 5) --------------------------


def test_submit_allows_empty_text(tmp_path):
    """Defect 5 (grok-4.6 review of 46d4a2b): the general `_require` helper
    treats `""` as missing, and `_submit` used to run `text` through it --
    but `store.accept_unit(text="")` and the inline driver both permit empty
    text. An otherwise-valid empty submission was wrongly refused as a
    malformed envelope."""
    store = _seeded_store(tmp_path, unit_ids=("u0",))

    def parse_fn(text):
        return text

    adapter = RunAdapter(user_for=lambda u: f"user:{u.id}", parse_fn=parse_fn, validators=())
    handlers = build_handlers(store, adapter, strategy=FLAT_STRATEGY)
    claimed = dispatch(
        _envelope("claim", {"run_id": "run-1", "unit_id": "u0", "worker_id": "w1"}), handlers
    )
    token = claimed["result"]["fencing_token"]
    result = dispatch(
        _envelope(
            "submit",
            {"run_id": "run-1", "unit_id": "u0", "fencing_token": token, "text": ""},
        ),
        handlers,
    )
    assert result["ok"] is True
    assert result["result"]["accepted"] is True


def test_submit_rejects_a_non_string_text(tmp_path):
    """The other half of defect 5: any truthy non-string value (a JSON
    number or boolean) used to pass straight through `_require` and into
    `evaluate_submission` as `text` -- an identity `parse_fn` would happily
    accept it, and it would come back from the SQLite TEXT column as a
    different Python value than what was submitted (e.g. `True` stored,
    `"1"` read back)."""
    store = _seeded_store(tmp_path, unit_ids=("u0",))

    def parse_fn(text):
        return text

    adapter = RunAdapter(user_for=lambda u: f"user:{u.id}", parse_fn=parse_fn, validators=())
    handlers = build_handlers(store, adapter, strategy=FLAT_STRATEGY)
    claimed = dispatch(
        _envelope("claim", {"run_id": "run-1", "unit_id": "u0", "worker_id": "w1"}), handlers
    )
    token = claimed["result"]["fencing_token"]
    result = dispatch(
        _envelope(
            "submit",
            {"run_id": "run-1", "unit_id": "u0", "fencing_token": token, "text": True},
        ),
        handlers,
    )
    assert result["ok"] is False
    assert result["error"]["type"] == "MalformedEnvelopeError"


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


def test_submit_with_stale_fencing_token_and_invalid_text_is_still_refused(tmp_path):
    """Defect 1 (grok-4.6 review of 46d4a2b): `_submit` used to reach the
    store's fencing check only on the ACCEPT path (`store.accept_unit`) --
    the reject path (invalid text) never touched the store at all, so a
    stale token paired with invalid text returned `{"ok": true, "accepted":
    false}` with retry feedback, telling a worker it still owned a claim it
    had already lost to a reclaiming worker. `renew`/`fail` fence-check via
    the store on every call regardless of outcome; `submit` must too."""
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

    # `_adapter()`'s validator rejects anything but "ok" -- "bad" fails validation.
    result = dispatch(
        _envelope(
            "submit",
            {"run_id": "run-1", "unit_id": "u0", "fencing_token": stale_token, "text": "bad"},
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


# -- finalize reparses with the SAME parse_fn submit evaluated under (D1) ----


def test_finalize_reparses_with_the_same_parse_fn_submit_used_when_validation_spec_for_is_set(tmp_path):
    """Defect 2 (grok-4.6 review of 46d4a2b): when `validation_spec_for` is
    set, `_submit` evaluates via `adapter.resolve_validation_spec(unit)` --
    which uses `validation_spec_for(unit).parse_fn` when that field is set --
    but `finalize_run` used to call `adapter.parse_fn` directly, ALWAYS,
    ignoring `validation_spec_for` entirely. So D1 ("finalize re-parses with
    the SAME function the driver submitted under") only held when
    `validation_spec_for` was None, even though that field is documented for
    a consumer whose parse behavior varies per unit. `finalize_only_parse`
    below stands in for `adapter.parse_fn`: it must never be called, because
    the unit was submitted (and must be re-parsed) under `submit_parse`."""
    from content_pipeline.llm.platform import ValidationSpec

    def submit_parse(text):
        return {"via": "submit_parse", "text": text}

    def finalize_only_parse(text):
        raise AssertionError(
            "finalize must not fall back to adapter.parse_fn when "
            "validation_spec_for supplies a different parse_fn"
        )

    store = _seeded_store(tmp_path, unit_ids=("u0",))
    applied_payloads = []
    adapter = RunAdapter(
        user_for=lambda u: f"user:{u.id}",
        parse_fn=finalize_only_parse,
        validation_spec_for=lambda u: ValidationSpec(parse_fn=submit_parse, validators=()),
        apply=lambda uid, payload: applied_payloads.append(payload),
    )
    handlers = build_handlers(store, adapter, strategy=FLAT_STRATEGY)

    claimed = dispatch(
        _envelope("claim", {"run_id": "run-1", "unit_id": "u0", "worker_id": "w1"}), handlers
    )
    token = claimed["result"]["fencing_token"]
    submitted = dispatch(
        _envelope(
            "submit",
            {"run_id": "run-1", "unit_id": "u0", "fencing_token": token, "text": "hi"},
        ),
        handlers,
    )
    assert submitted["ok"] is True
    assert submitted["result"]["accepted"] is True

    finalized = dispatch(_envelope("finalize", {"run_id": "run-1"}), handlers)
    assert finalized["ok"] is True
    assert finalized["result"]["applied"] == ["u0"]
    assert applied_payloads == [{"via": "submit_parse", "text": "hi"}]


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

_STATUS_SCRIPT = """
import json
import sys
sys.path.insert(0, sys.argv[1])
from content_pipeline.execution.adapter import RunAdapter
from content_pipeline.execution.protocol import PROTOCOL_VERSION, build_handlers, dispatch
from content_pipeline.execution.store import ExecutionStore

db_path, run_id = sys.argv[2:4]
store = ExecutionStore(db_path)
adapter = RunAdapter(user_for=lambda u: "user:" + u.id, parse_fn=lambda t: t)
handlers = build_handlers(store, adapter)
envelope = {
    "protocol_version": PROTOCOL_VERSION,
    "verb": "status",
    "payload": {"run_id": run_id},
}
print(json.dumps(dispatch(envelope, handlers)))
"""

_FINALIZE_SCRIPT = """
import json
import sys
sys.path.insert(0, sys.argv[1])
from content_pipeline.execution.adapter import RunAdapter
from content_pipeline.execution.protocol import PROTOCOL_VERSION, build_handlers, dispatch
from content_pipeline.execution.store import ExecutionStore

db_path, run_id = sys.argv[2:4]
store = ExecutionStore(db_path)
adapter = RunAdapter(
    user_for=lambda u: "user:" + u.id, parse_fn=lambda t: t, apply=lambda uid, payload: None
)
handlers = build_handlers(store, adapter)
envelope = {
    "protocol_version": PROTOCOL_VERSION,
    "verb": "finalize",
    "payload": {"run_id": run_id},
}
print(json.dumps(dispatch(envelope, handlers)))
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
    """The A-min.3 exit criterion is "several short-lived local processes
    claim, read, submit, INSPECT STATUS, and FINALIZE one run with no
    process-local continuity" -- this test used to stop after submit,
    overstating what it actually covered (the review named this a test
    defect: neither `status` nor `finalize` was exercised cross-process at
    all). Now it drives the full verb sequence, each verb in its OWN
    subprocess, and confirms the durable effects from a fourth, independent
    store handle."""
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

    # The only channel between the two subprocesses so far was the db file --
    # confirm the accept landed durably, observed from a THIRD store handle
    # (this test process's own, never used by either subprocess).
    unit = store.get_unit("run-1", "u0")
    assert unit.state is UnitState.ACCEPTED
    assert unit.accepted_text == "hello"

    # A FOURTH process inspects status -- sees the ACCEPTED unit the second
    # process (submit) recorded, with no in-process handle shared between them.
    status_reply = _run_subprocess(_STATUS_SCRIPT, str(db_path), "run-1")
    assert status_reply["ok"] is True
    assert status_reply["result"]["counts_by_state"]["accepted"] == 1

    # A FIFTH process finalizes -- applies the text the second process
    # submitted, again with no process-local continuity.
    finalize_reply = _run_subprocess(_FINALIZE_SCRIPT, str(db_path), "run-1")
    assert finalize_reply["ok"] is True
    assert finalize_reply["result"]["applied"] == ["u0"]


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


def test_subprocesses_stale_token_submit_is_refused_with_no_process_local_continuity(tmp_path):
    """The review named this gap explicitly: no test covered a STALE-TOKEN
    submit across separate processes -- only the in-process
    `test_submit_with_stale_fencing_token_is_refused` did. Both claims here
    run in their OWN subprocess; the reclaim (simulating a lease-expiry
    takeover) is forced from this test process's own store handle, exactly
    as the in-process version does; the final, stale submit runs in a
    THIRD subprocess with no access to either claim's in-memory state."""
    db_path = tmp_path / "run.db"
    store = ExecutionStore(db_path)
    store.create_run("run-1", driver="inline", backend="mock", model="m", adapter_version="")
    store.register_units("run-1", ["u0"])

    claim1_reply = _run_subprocess(_CLAIM_SCRIPT, str(db_path), "run-1", "u0", "worker-a")
    assert claim1_reply["ok"] is True
    stale_token = claim1_reply["result"]["fencing_token"]

    # Force a lease-expiry reclaim so the fencing token advances past `stale_token`.
    store.fail_unit("run-1", "u0", stale_token, terminal=False)
    claim2_reply = _run_subprocess(_CLAIM_SCRIPT, str(db_path), "run-1", "u0", "worker-b")
    assert claim2_reply["ok"] is True
    assert claim2_reply["result"]["fencing_token"] != stale_token

    submit_reply = _run_subprocess(
        _READ_THEN_SUBMIT_SCRIPT, str(db_path), "run-1", "u0", str(stale_token), "hello"
    )
    assert submit_reply["submit"]["ok"] is False
    assert submit_reply["submit"]["error"]["type"] == "StaleFenceError"
    # The winning claimant's own submission must still be possible afterward --
    # a stale rejection must not have corrupted the live claim.
    live_submit_reply = _run_subprocess(
        _READ_THEN_SUBMIT_SCRIPT,
        str(db_path),
        "run-1",
        "u0",
        str(claim2_reply["result"]["fencing_token"]),
        "hello",
    )
    assert live_submit_reply["submit"]["ok"] is True
    assert live_submit_reply["submit"]["result"]["accepted"] is True


# -- environment enforcement on worker verbs (item 5, A-min.4) ----------------


def _run_with_env(store, run_id, environment, *, adapter_version=""):
    store.create_run(
        run_id, driver="inline", backend="mock", model="m", adapter_version=adapter_version,
        environment=environment,
    )


def test_claim_refuses_when_worker_environment_mismatches(tmp_path, monkeypatch):
    from content_pipeline.execution.adapter import WorkerEnvironment

    monkeypatch.setenv("APP_ROOT", "D:\\dev\\wrong")
    store = _new_store(tmp_path)
    _run_with_env(store, "run-1", {"APP_ROOT": "D:\\dev\\proj"})
    store.register_units("run-1", ["u0"])
    adapter = RunAdapter(
        user_for=lambda u: f"user:{u.id}",
        parse_fn=lambda t: t,
        apply=lambda uid, payload: None,
        environment=WorkerEnvironment(required_vars=("APP_ROOT",)),
    )
    handlers = build_handlers(store, adapter, strategy=FLAT_STRATEGY)
    result = dispatch(
        _envelope("claim", {"run_id": "run-1", "unit_id": "u0", "worker_id": "w1"}), handlers
    )
    assert result["ok"] is False
    assert result["error"]["type"] == "WorkerEnvironmentMismatchError"
    # Nothing was actually claimed.
    assert store.get_unit("run-1", "u0").state is UnitState.PENDING


def test_claim_passes_when_worker_environment_matches(tmp_path, monkeypatch):
    from content_pipeline.execution.adapter import WorkerEnvironment

    monkeypatch.setenv("APP_ROOT", "D:\\dev\\proj")
    store = _new_store(tmp_path)
    _run_with_env(store, "run-1", {"APP_ROOT": "D:\\dev\\proj"})
    store.register_units("run-1", ["u0"])
    adapter = RunAdapter(
        user_for=lambda u: f"user:{u.id}",
        parse_fn=lambda t: t,
        apply=lambda uid, payload: None,
        environment=WorkerEnvironment(required_vars=("APP_ROOT",)),
    )
    handlers = build_handlers(store, adapter, strategy=FLAT_STRATEGY)
    result = dispatch(
        _envelope("claim", {"run_id": "run-1", "unit_id": "u0", "worker_id": "w1"}), handlers
    )
    assert result["ok"] is True


def test_claim_unaffected_when_adapter_declares_no_environment(tmp_path, monkeypatch):
    """An adapter declaring nothing must behave exactly as today, regardless
    of what the run's recorded environment (if any) looks like."""
    monkeypatch.setenv("APP_ROOT", "D:\\dev\\here")
    store = _new_store(tmp_path)
    _run_with_env(store, "run-1", {"APP_ROOT": "D:\\dev\\entirely-different"})
    store.register_units("run-1", ["u0"])
    handlers = build_handlers(store, _adapter(), strategy=FLAT_STRATEGY)
    result = dispatch(
        _envelope("claim", {"run_id": "run-1", "unit_id": "u0", "worker_id": "w1"}), handlers
    )
    assert result["ok"] is True


# -- lease derivation from the adapter's declared cost (item 2, A-min.4) -----


def test_claim_with_no_declared_cost_and_no_mount_lease_uses_the_300s_default(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    handlers = build_handlers(store, _adapter(), strategy=FLAT_STRATEGY)
    before = time.time()
    result = dispatch(
        _envelope("claim", {"run_id": "run-1", "unit_id": "u0", "worker_id": "w1"}), handlers
    )
    assert result["ok"] is True
    # 300s default, not the 900s+ a mis-derivation would produce, and not
    # some arbitrarily short value either.
    expires = result["result"]["lease_expires_at"]
    assert 300 - 2 <= expires - before <= 300 + 2


def test_claim_derives_a_longer_lease_from_the_adapters_declared_cost(tmp_path):
    """The clamp trap (protocol.py's `_resolve_lease_seconds` computes
    `min(requested, default)`): the derived 426s ceiling must actually
    REACH `store.claim_unit` -- assert the value that LANDS, not merely
    that the call succeeded."""
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    base = _adapter()
    adapter = RunAdapter(
        user_for=base.user_for,
        parse_fn=base.parse_fn,
        validators=base.validators,
        apply=base.apply,
        expected_unit_seconds=213.0,
    )
    handlers = build_handlers(store, adapter, strategy=FLAT_STRATEGY)
    before = time.time()
    result = dispatch(
        _envelope("claim", {"run_id": "run-1", "unit_id": "u0", "worker_id": "w1"}), handlers
    )
    assert result["ok"] is True
    expires = result["result"]["lease_expires_at"]
    # 213.0 * 2.0 = 426.0 -- must land close to 426s, not the 300s default.
    assert 426 - 2 <= expires - before <= 426 + 2

    # Confirm it actually reached the store's own record too, not just the
    # dispatch reply.
    unit = store.get_unit("run-1", "u0")
    assert 426 - 2 <= unit.lease_expires_at - before <= 426 + 2


def test_explicit_mount_lease_seconds_still_wins_over_a_derived_one(tmp_path):
    """An explicit mount-time `lease_seconds` wins outright over derivation
    -- trusted mount policy stays trusted even when the adapter also
    declares a cost that would derive a different value."""
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    base = _adapter()
    adapter = RunAdapter(
        user_for=base.user_for,
        parse_fn=base.parse_fn,
        validators=base.validators,
        apply=base.apply,
        expected_unit_seconds=213.0,  # would derive 426s if not overridden
    )
    handlers = build_handlers(store, adapter, strategy=FLAT_STRATEGY, lease_seconds=100.0)
    before = time.time()
    result = dispatch(
        _envelope("claim", {"run_id": "run-1", "unit_id": "u0", "worker_id": "w1"}), handlers
    )
    assert result["ok"] is True
    expires = result["result"]["lease_expires_at"]
    assert 100 - 2 <= expires - before <= 100 + 2
