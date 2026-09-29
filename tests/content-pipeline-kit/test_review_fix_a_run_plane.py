"""Run-plane readiness and store fixes: skip look-back, expired-claim
reclaim in the graph rule, non-string fail detail, and inline resolver use."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from content_pipeline.execution.adapter import PreparedRequest, RunAdapter
from content_pipeline.execution.drivers.inline import run_wave
from content_pipeline.execution.model import AttemptKind, UnitState
from content_pipeline.execution.status import compute_status
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.execution.wave import graph_block_reason, ready_wave
from content_pipeline.execution.workerpack import (
    DEFAULT_MAX_RECLAIMS_PER_UNIT,
    WorkerCommand,
    build_wave_args,
)
from content_pipeline.llm.platform import ValidationSpec
from content_pipeline.pipeline.workunit import FlatChunkStrategy, GraphWalkStrategy

G = GraphWalkStrategy(order=lambda s: [])
FLAT = FlatChunkStrategy(select=lambda s: [])


def _store(tmp_path, units=("A", "B", "C"), name="s.db"):
    s = ExecutionStore(tmp_path / name)
    s.create_run("r", driver="inline", backend="mock", model="m", adapter_version="")
    s.register_units("r", list(units))
    return s


def _skip(s, unit_id):
    c = s.claim_unit("r", unit_id, "prepare")
    s.fail_unit(
        "r", unit_id, c.fencing_token, error="skip:up_to_date", terminal=True,
        terminal_state=UnitState.SKIPPED,
    )


def _ids(wave):
    return [u.unit_id for u in wave]


# -- finding 1: skip look-back ------------------------------------------------


def test_skipped_middle_unit_does_not_release_successor_over_claimed_predecessor(tmp_path):
    s = _store(tmp_path)
    _skip(s, "B")
    s.claim_unit("r", "A", "w1")
    assert _ids(ready_wave(s, "r", G)) == []


def test_skipped_middle_unit_does_not_release_successor_over_unapplied_accepted(tmp_path):
    s = _store(tmp_path)
    _skip(s, "B")
    t = s.claim_unit("r", "A", "w1")
    s.accept_unit("r", "A", t.fencing_token, text="x")
    assert _ids(ready_wave(s, "r", G)) == []


def test_skipped_middle_unit_does_not_release_successor_over_failed_predecessor(tmp_path):
    s = _store(tmp_path)
    _skip(s, "B")
    t = s.claim_unit("r", "A", "w1")
    s.fail_unit("r", "A", t.fencing_token, terminal=True)
    assert _ids(ready_wave(s, "r", G)) == []
    reason = graph_block_reason(s, "r", G)
    assert reason is not None and "'A'" in reason and "FAILED" in reason


def test_skip_look_back_accepts_applied_predecessor_and_leading_skips(tmp_path):
    s = _store(tmp_path)
    _skip(s, "B")
    t = s.claim_unit("r", "A", "w1")
    s.accept_unit("r", "A", t.fencing_token, text="x")
    s.record_apply_started("r", "A")
    s.record_apply_succeeded("r", "A")
    assert _ids(ready_wave(s, "r", G)) == ["C"]
    assert graph_block_reason(s, "r", G) is None

    s2 = _store(tmp_path, name="s2.db")
    _skip(s2, "A")
    _skip(s2, "B")
    assert _ids(ready_wave(s2, "r", G)) == ["C"]

    s3 = _store(tmp_path, units=("A", "B", "C", "D"), name="s3.db")
    _skip(s3, "B")
    _skip(s3, "C")
    assert _ids(ready_wave(s3, "r", G)) == ["A"]


# -- finding 2: expired claim is ready in the graph rule ---------------------


def _wave_ids(store, tmp_path, strategy, at):
    ad = RunAdapter(expected_unit_seconds=100.0)
    wc = WorkerCommand(argv=("python", "mount.py"), answer_dir=str(tmp_path / "ans"))
    args = build_wave_args(store, "r", ad, wc, 2, strategy=strategy, at=at)
    return [u["unitId"] for u in args["units"]]


def test_graph_wave_reoffers_a_unit_whose_claim_expired(tmp_path):
    s = _store(tmp_path, units=("A", "B"))
    s.claim_unit("r", "A", "wf-dead", lease_seconds=10, at=1000.0)
    assert _wave_ids(s, tmp_path, G, 2000.0) == ["A"]
    assert _wave_ids(s, tmp_path, FLAT, 2000.0) == ["A", "B"]


def test_graph_wave_never_releases_over_a_live_claim(tmp_path):
    s = _store(tmp_path, units=("A", "B"))
    s.claim_unit("r", "A", "wf-live", lease_seconds=10_000, at=1000.0)
    assert _wave_ids(s, tmp_path, G, 2000.0) == []
    # an expired claim on B behind a live claim on A is not released either
    s2 = _store(tmp_path, units=("A", "B", "C"), name="s2.db")
    s2.claim_unit("r", "A", "wf-live", lease_seconds=10_000, at=1000.0)
    _skip(s2, "B")
    assert _wave_ids(s2, tmp_path, G, 2000.0) == []
    s3 = _store(tmp_path, units=("A", "B"), name="s3.db")
    s3.claim_unit("r", "A", "wf-live", lease_seconds=10_000, at=1000.0)
    s3.claim_unit("r", "B", "wf-x", lease_seconds=10, at=1000.0)
    assert _wave_ids(s3, tmp_path, G, 2000.0) == []


def test_graph_reclaim_fences_and_honours_the_reclaim_limit(tmp_path):
    s = _store(tmp_path, units=("A", "B"))
    t = 1000.0
    fence = s.claim_unit("r", "A", "w0", lease_seconds=10, at=t).fencing_token
    for i in range(DEFAULT_MAX_RECLAIMS_PER_UNIT):
        t += 100
        assert _wave_ids(s, tmp_path, G, t) == ["A"]
        c = s.claim_unit("r", "A", f"w{i + 1}", lease_seconds=10, at=t)  # the agent's claim
        assert c.fencing_token == fence + 1
        fence = c.fencing_token
    t += 100
    assert _wave_ids(s, tmp_path, G, t) == []
    assert s.get_unit("r", "A").state is UnitState.FAILED
    assert "FAILED" in graph_block_reason(s, "r", G, at=t)


def test_block_reason_names_an_expired_predecessor_claim(tmp_path):
    s = _store(tmp_path, units=("A", "B"))
    s.claim_unit("r", "A", "wf-dead", lease_seconds=10, at=1000.0)
    live = graph_block_reason(s, "r", G, at=1005.0)
    assert live is not None and "expired" not in live
    dead = graph_block_reason(s, "r", G, at=2000.0)
    assert dead is not None and "expired" in dead


def test_default_ready_wave_still_returns_pending_units_only(tmp_path):
    s = _store(tmp_path, units=("A", "B"))
    s.claim_unit("r", "A", "wf-dead", lease_seconds=10, at=1000.0)
    assert _ids(ready_wave(s, "r", G)) == []
    assert _ids(ready_wave(s, "r", G, reclaim_at=2000.0)) == ["A"]
    assert _ids(ready_wave(s, "r", G, reclaim_at=1005.0)) == []


# -- finding 10: non-string fail detail ---------------------------------------


def test_fail_with_an_object_detail_records_the_attempt_and_releases_the_unit(tmp_path):
    s = _store(tmp_path, units=("A",))
    c = s.claim_unit("r", "A", "w")
    s.fail_unit("r", "A", c.fencing_token, error={"reason": "bad", "n": 3})
    assert s.get_unit("r", "A").state is UnitState.PENDING
    fails = [a for a in s.list_attempts("r", "A") if a.kind is AttemptKind.FAIL]
    assert json.loads(fails[0].error) == {"reason": "bad", "n": 3}


def test_fail_detail_accept_cases_string_empty_and_list(tmp_path):
    s = _store(tmp_path, units=("A",))
    for detail in ("plain text", "", ["a", 1]):
        c = s.claim_unit("r", "A", "w")
        s.fail_unit("r", "A", c.fencing_token, error=detail)
    errs = [a.error for a in s.list_attempts("r", "A") if a.kind is AttemptKind.FAIL]
    assert errs[0] == "plain text"
    assert not errs[1]
    assert json.loads(errs[2]) == ["a", 1]


def test_object_fail_detail_stays_out_of_status(tmp_path):
    s = _store(tmp_path, units=("A",))
    c = s.claim_unit("r", "A", "w")
    s.fail_unit("r", "A", c.fencing_token, error={"secret": "RAWTEXT" * 200}, terminal=True)
    text = json.dumps(compute_status(s, "r").to_dict(), default=str)
    assert "RAWTEXT" not in text


# -- finding 13: inline backend path uses the adapter resolvers --------------


class _Backend:
    name = "fake"


def _patch_submit(monkeypatch, seen):
    from content_pipeline.execution.drivers import inline

    def fake(**kw):
        seen.update(kw)
        return SimpleNamespace(
            accepted=True, responses=[SimpleNamespace(text="out")], rejections=[]
        )

    monkeypatch.setattr(inline, "submit_validated", fake)


def test_inline_backend_uses_build_request_and_validation_spec_for(tmp_path, monkeypatch):
    seen = {}
    _patch_submit(monkeypatch, seen)
    s = _store(tmp_path, units=("A",))

    def parse(text):
        return text

    ad = RunAdapter(
        build_request=lambda u: PreparedRequest(unit=u, system="SYS-BR", user="USER-BR"),
        validation_spec_for=lambda u: ValidationSpec(
            parse_fn=parse, validators=("v1",), context={"k": 1}, block_soft=False
        ),
    )
    assert run_wave(s, "r", ready_wave(s, "r", FLAT), ad, backend=_Backend()) == ["A"]
    assert seen["system"] == "SYS-BR" and seen["user"] == "USER-BR"
    assert seen["parse_fn"] is parse
    assert tuple(seen["validators"]) == ("v1",)
    assert seen["context"] == {"k": 1}
    assert seen["block_soft"] is False


def test_inline_backend_plain_adapter_and_caller_overrides_unchanged(tmp_path, monkeypatch):
    seen = {}
    _patch_submit(monkeypatch, seen)
    s = _store(tmp_path, units=("A",))
    ad = RunAdapter(
        system_for=lambda u: "S", user_for=lambda u: "U", parse_fn=lambda t: t,
        validators=("v",), validation_context="ctx",
    )
    run_wave(s, "r", ready_wave(s, "r", FLAT), ad, backend=_Backend(), context="caller")
    assert (seen["system"], seen["user"]) == ("S", "U")
    assert seen["context"] == "caller"
    assert tuple(seen["validators"]) == ("v",)


def test_inline_backend_still_refuses_an_adapter_with_no_prompt_or_parse(tmp_path):
    s = _store(tmp_path, units=("A",))
    with pytest.raises(ValueError):
        run_wave(s, "r", ready_wave(s, "r", FLAT), RunAdapter(), backend=_Backend())
