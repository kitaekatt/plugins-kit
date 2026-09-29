"""Apply-axis outcomes are visible in the status digest and the finalize verb."""

from __future__ import annotations

import json

import pytest

from content_pipeline.cli.run import build_commands
from content_pipeline.execution.adapter import RunAdapter
from content_pipeline.execution.model import ApplyRejected
from content_pipeline.execution.protocol import PROTOCOL_VERSION
from content_pipeline.execution.status import compute_status
from content_pipeline.execution.store import ExecutionStore

RUN = "r"
UNITS = ["applied", "rejected", "interrupted", "pending_apply", "not_accepted"]


def _accept(store, unit, text="t"):
    tok = store.claim_unit(RUN, unit, "w").fencing_token
    store.accept_unit(RUN, unit, tok, text=text)


@pytest.fixture
def store(tmp_path):
    s = ExecutionStore(tmp_path / "r.db")
    s.create_run(RUN, driver="inline", backend="b", model="m", adapter_version="")
    s.register_units(RUN, UNITS)
    for u in UNITS[:4]:
        _accept(s, u)
    s.record_apply_started(RUN, "applied")
    s.record_apply_succeeded(RUN, "applied")
    s.record_apply_started(RUN, "rejected")
    s.record_apply_rejected(RUN, "rejected", "SECRET-REASON-TEXT")
    s.record_apply_started(RUN, "interrupted")
    return s


def test_digest_counts_every_apply_state(store):
    d = compute_status(store, RUN)
    assert d.apply_counts == {
        "not_applied": 1,
        "applied": 1,
        "apply_rejected": 1,
        "apply_started": 1,
    }
    assert d.apply_rejected_unit_ids == ["rejected"]
    assert d.apply_started_unit_ids == ["interrupted"]
    assert "SECRET-REASON-TEXT" not in json.dumps(d.to_dict())


def test_a_later_success_after_rejection_history_uses_the_last_apply_kind(store):
    store.record_apply_started(RUN, "interrupted")
    store.record_apply_succeeded(RUN, "interrupted")
    d = compute_status(store, RUN)
    assert d.apply_counts["apply_started"] == 0
    assert d.apply_counts["applied"] == 2
    assert d.apply_started_unit_ids == []


def test_run_with_no_accepted_units_has_zero_counts(tmp_path):
    s = ExecutionStore(tmp_path / "e.db")
    s.create_run(RUN, driver="inline", backend="b", model="m", adapter_version="")
    s.register_units(RUN, ["a"])
    d = compute_status(s, RUN)
    assert d.apply_counts == {
        "not_applied": 0,
        "applied": 0,
        "apply_rejected": 0,
        "apply_started": 0,
    }


def test_finalize_verb_returns_rejected_unit_ids(tmp_path):
    def apply(uid, payload):
        if uid == "b":
            raise ApplyRejected("no thanks")

    adapter = RunAdapter(user_for=lambda u: "x", parse_fn=lambda t: t, apply=apply)
    s = ExecutionStore(tmp_path / "f.db")
    s.create_run(RUN, driver="inline", backend="b", model="m", adapter_version="")
    s.register_units(RUN, ["a", "b", "c"])
    for u in "abc":
        _accept(s, u)
    commands = build_commands(s, adapter=adapter)
    env = json.dumps({"protocol_version": PROTOCOL_VERSION, "verb": "finalize", "payload": {"run_id": RUN}})
    result = commands["protocol"].handler([env])
    assert result["ok"] is True
    assert result["result"]["applied"] == ["a", "c"]
    assert result["result"]["rejected"] == ["b"]
    # A second finalize applies nothing, but still reports the standing rejection.
    again = commands["protocol"].handler([env])["result"]
    assert again["applied"] == []
    assert again["rejected"] == ["b"]


def test_finalize_verb_replays_an_interrupted_apply(store):
    calls = []
    adapter = RunAdapter(
        user_for=lambda u: "x",
        parse_fn=lambda t: t,
        apply=lambda uid, payload: calls.append(uid),
    )
    commands = build_commands(store, adapter=adapter)
    env = json.dumps({"protocol_version": PROTOCOL_VERSION, "verb": "finalize", "payload": {"run_id": RUN}})
    result = commands["protocol"].handler([env])
    assert result["ok"] is True
    assert result["result"]["applied"] == ["interrupted", "pending_apply"]
    assert result["result"]["rejected"] == ["rejected"]
    assert calls == ["interrupted", "pending_apply"]
    d = compute_status(store, RUN)
    assert d.apply_counts["apply_started"] == 0
    assert d.apply_started_unit_ids == []
