"""Tests for content_pipeline.execution.events (read-side execution-event projection).

Pins the identity mapping, the row-id ``seq`` scheme, terminal derivation, the
usage rule, the refuse-with-diagnosis probe, and the import boundary.
"""

from __future__ import annotations

import ast
import json
import types
from pathlib import Path

import pytest

from content_pipeline.execution import events as ev
from content_pipeline.execution.events import (
    ExecutionEventSupportError,
    project_run,
    write_run_events,
)
from content_pipeline.execution.model import (
    AttemptKind,
    StaleFenceError,
    UnitState,
    UnknownRunError,
    UsageRecord,
)
from content_pipeline.execution.store import ExecutionStore

from bootstrap_lib import execution_event as real_ee

RUN = "r1"
PLUGIN = "content-pipeline-kit"


def _store(tmp_path, units=("u0", "u1")) -> ExecutionStore:
    store = ExecutionStore(tmp_path / "run.db")
    store.create_run(
        RUN, driver="inline", backend="mock", model="m1", adapter_version="7", created_at=1000.0
    )
    store.register_units(RUN, list(units), at=1000.0)
    return store


def _names(events):
    return [(e["event"], e["identity"].get("unit_id"), e["seq"]) for e in events]


def _by_event(events, name):
    return [e for e in events if e["event"] == name]


# -- identity ------------------------------------------------------------------


def test_identity_maps_run_unit_fencing_token(tmp_path):
    store = _store(tmp_path)
    claim = store.claim_unit(RUN, "u1", "w", at=1001.0)
    store.accept_unit(RUN, "u1", claim.fencing_token, at=1002.0)
    events = project_run(store, RUN)
    started = _by_event(events, "call-started")[0]
    assert started["identity"] == {
        "run_id": RUN,
        "unit_id": "u1",
        "attempt_id": str(claim.fencing_token),
    }
    assert _by_event(events, "result")[0]["identity"]["attempt_id"] == str(claim.fencing_token)
    assert started["source"] == {"plugin": PLUGIN, "adapter": "mock", "model": "m1"}
    assert started["payload"] == {"worker_id": "w"}
    assert started["at"] == real_ee.utc_timestamp(1001.0)


def test_second_claim_has_next_fencing_token(tmp_path):
    store = _store(tmp_path)
    first = store.claim_unit(RUN, "u0", "w", lease_seconds=1, at=100.0)
    second = store.claim_unit(RUN, "u0", "w2", at=200.0)
    ids = [e["identity"]["attempt_id"] for e in _by_event(project_run(store, RUN), "call-started")]
    assert ids == [str(first.fencing_token), str(second.fencing_token)]
    assert first.fencing_token != second.fencing_token


# -- row-kind mapping -----------------------------------------------------------


def _scenario_for(store, kind: AttemptKind):
    """Drive the store so at least one row of ``kind`` exists; return unit id."""
    if kind is AttemptKind.CLAIM:
        store.claim_unit(RUN, "u0", "w", at=1001.0)
    elif kind is AttemptKind.RENEW:
        tok = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
        store.renew_lease(RUN, "u0", tok, at=1002.0)
    elif kind is AttemptKind.EXPIRE:
        store.claim_unit(RUN, "u0", "w", lease_seconds=1, at=1001.0)
        store.claim_unit(RUN, "u0", "w2", at=2000.0)
    elif kind is AttemptKind.ACCEPT:
        tok = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
        store.accept_unit(RUN, "u0", tok, at=1002.0)
    elif kind is AttemptKind.FAIL:
        tok = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
        store.fail_unit(RUN, "u0", tok, error="boom", at=1002.0)
    elif kind is AttemptKind.SUPERSEDED:
        tok = store.claim_unit(RUN, "u0", "w", lease_seconds=1, at=1001.0).fencing_token
        store.claim_unit(RUN, "u0", "w2", at=2000.0)
        with pytest.raises(StaleFenceError):
            store.accept_unit(RUN, "u0", tok, at=2001.0)
    else:
        tok = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
        store.accept_unit(RUN, "u0", tok, at=1002.0)
        if kind is AttemptKind.APPLY_STARTED:
            store.record_apply_started(RUN, "u0", at=1003.0)
        elif kind is AttemptKind.APPLY_SUCCEEDED:
            store.record_apply_succeeded(RUN, "u0", at=1003.0)
        else:
            store.record_apply_rejected(RUN, "u0", "no", at=1003.0)


_EXPECTED = {
    AttemptKind.CLAIM: {"call-started"},
    AttemptKind.RENEW: {f"{PLUGIN}:lease-renewed"},
    AttemptKind.EXPIRE: {"result"},
    AttemptKind.ACCEPT: {"result", "terminal"},
    AttemptKind.FAIL: {"result"},
    AttemptKind.SUPERSEDED: {f"{PLUGIN}:submission-superseded"},
    AttemptKind.APPLY_STARTED: {f"{PLUGIN}:apply-started"},
    AttemptKind.APPLY_SUCCEEDED: {f"{PLUGIN}:apply-succeeded"},
    AttemptKind.APPLY_REJECTED: {f"{PLUGIN}:apply-rejected"},
}


@pytest.mark.parametrize("kind", list(AttemptKind), ids=lambda k: k.value)
def test_every_row_kind_maps(tmp_path, kind):
    store = _store(tmp_path)
    _scenario_for(store, kind)
    rows = [r for r in store.list_attempts(RUN) if r.kind is kind]
    assert rows, "scenario must record a row of this kind"
    row = rows[0]
    events = project_run(store, RUN)
    from_row = [e for e in events if e["seq"] // 4 == row.id and e["seq"] != 0]
    assert from_row, f"no event derived from a {kind.value} row"
    assert _EXPECTED[kind] <= {e["event"] for e in from_row}
    if kind is AttemptKind.EXPIRE:
        result = [e for e in from_row if e["event"] == "result"][0]
        assert result["payload"] == {"status": "expired"}
        assert result["identity"]["attempt_id"] == str(row.fencing_token)
    if kind is AttemptKind.APPLY_REJECTED:
        assert from_row[0]["payload"] == {"reason": "no"}
    if kind.value.startswith("apply_"):
        assert "attempt_id" not in from_row[0]["identity"]


def test_run_created_is_first(tmp_path):
    store = _store(tmp_path)
    tok = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    store.accept_unit(RUN, "u0", tok, at=1002.0)
    events = project_run(store, RUN)
    assert events[0]["event"] == f"{PLUGIN}:run-created"
    assert events[0]["seq"] == 0
    assert "unit_id" not in events[0]["identity"]
    assert events[0]["payload"] == {"driver": "inline", "adapter_version": "7"}
    assert min(e["seq"] for e in events[1:]) >= 4


# -- ordering -------------------------------------------------------------------


def test_order_follows_commit_not_timestamp(tmp_path):
    store = _store(tmp_path)
    # Caller-supplied times run backwards; commit order is u0 then u1.
    store.claim_unit(RUN, "u0", "w", at=900.0)
    store.claim_unit(RUN, "u1", "w", at=100.0)
    started = _by_event(project_run(store, RUN), "call-started")
    assert [e["identity"]["unit_id"] for e in started] == ["u0", "u1"]
    assert started[0]["seq"] < started[1]["seq"]
    assert started[0]["at"] > started[1]["at"]


def test_seq_scheme_is_row_id_times_four_plus_phase(tmp_path):
    store = _store(tmp_path)
    tok = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    store.accept_unit(RUN, "u0", tok, usage=UsageRecord(5, 6, None), at=1002.0)
    accept = [r for r in store.list_attempts(RUN) if r.kind is AttemptKind.ACCEPT][0]
    seqs = {e["event"]: e["seq"] for e in project_run(store, RUN) if e["seq"] // 4 == accept.id}
    assert seqs == {"usage": accept.id * 4, "result": accept.id * 4 + 1, "terminal": accept.id * 4 + 2}


def test_seq_stable_across_reprojection_after_interleaved_appends(tmp_path):
    store = _store(tmp_path)
    # A second run in the same store shares the attempts id sequence, so this
    # run's row ids have gaps that position-based numbering would close.
    store.create_run("r2", driver="inline", backend="mock", model="m1", adapter_version="7")
    store.register_units("r2", ["x"])
    store.claim_unit("r2", "x", "w", at=1000.5)
    t0 = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    before = project_run(store, RUN)
    # Interleaved appends: the other run, another unit, then u0 finishes.
    store.renew_lease("r2", "x", 1, at=1001.5)
    store.claim_unit(RUN, "u1", "w", at=1002.0)
    store.renew_lease("r2", "x", 1, at=1002.5)
    store.accept_unit(RUN, "u0", t0, at=1003.0)
    after = project_run(store, RUN)
    assert after[: len(before)] == before
    assert len(after) > len(before)
    seqs = [e["seq"] for e in after]
    assert len(set(seqs)) == len(seqs)
    ids = {r.id for r in store.list_attempts(RUN)}
    assert {s // 4 for s in seqs if s} == ids


def test_projection_is_idempotent(tmp_path):
    store = _store(tmp_path)
    tok = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    store.fail_unit(RUN, "u0", tok, error="x", terminal=True, at=1002.0)
    assert project_run(store, RUN) == project_run(store, RUN)


# -- terminal derivation ------------------------------------------------------------


def test_retryable_fail_has_no_terminal(tmp_path):
    store = _store(tmp_path)
    tok = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    store.fail_unit(RUN, "u0", tok, error="again", at=1002.0)
    events = project_run(store, RUN)
    assert _by_event(events, "result")[0]["payload"] == {"status": "failed", "error": "again"}
    assert _by_event(events, "terminal") == []


def test_terminal_fail_has_terminal(tmp_path):
    store = _store(tmp_path)
    tok = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    store.fail_unit(RUN, "u0", tok, error="dead", terminal=True, at=1002.0)
    terminal = _by_event(project_run(store, RUN), "terminal")
    assert [t["payload"] for t in terminal] == [{"state": "failed"}]
    assert "attempt_id" not in terminal[0]["identity"]


def test_skip_lands_terminal_skipped(tmp_path):
    store = _store(tmp_path)
    tok = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    store.fail_unit(
        RUN,
        "u0",
        tok,
        error="skip:up_to_date",
        terminal=True,
        terminal_state=UnitState.SKIPPED,
        at=1002.0,
    )
    terminal = _by_event(project_run(store, RUN), "terminal")
    assert [t["payload"] for t in terminal] == [{"state": "skipped"}]


def test_retried_then_terminal_fail_only_last_has_terminal(tmp_path):
    store = _store(tmp_path)
    t1 = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    store.fail_unit(RUN, "u0", t1, error="one", at=1002.0)
    t2 = store.claim_unit(RUN, "u0", "w", at=1003.0).fencing_token
    store.fail_unit(RUN, "u0", t2, error="two", terminal=True, at=1004.0)
    events = project_run(store, RUN)
    assert len(_by_event(events, "result")) == 2
    terminal = _by_event(events, "terminal")
    assert len(terminal) == 1
    fails = [r for r in store.list_attempts(RUN) if r.kind is AttemptKind.FAIL]
    assert terminal[0]["seq"] // 4 == fails[-1].id


def test_expired_then_superseded_yields_one_result(tmp_path):
    store = _store(tmp_path)
    old = store.claim_unit(RUN, "u0", "w", lease_seconds=1, at=1001.0).fencing_token
    new = store.claim_unit(RUN, "u0", "w2", at=2000.0).fencing_token
    with pytest.raises(StaleFenceError):
        store.accept_unit(RUN, "u0", old, at=2001.0)
    events = project_run(store, RUN)
    old_results = [
        e
        for e in _by_event(events, "result")
        if e["identity"]["attempt_id"] == str(old)
    ]
    assert len(old_results) == 1 and old_results[0]["payload"] == {"status": "expired"}
    assert len(_by_event(events, f"{PLUGIN}:submission-superseded")) == 1
    assert not [e for e in _by_event(events, "result") if e["identity"]["attempt_id"] == str(new)]


# -- usage ------------------------------------------------------------------------


def test_usage_null_is_unknown_not_zero(tmp_path):
    store = _store(tmp_path)
    tok = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    store.accept_unit(RUN, "u0", tok, usage=UsageRecord(10, None, None), at=1002.0)
    usage = _by_event(project_run(store, RUN), "usage")
    assert len(usage) == 1
    assert usage[0]["payload"] == {
        "input_tokens": 10,
        "output_tokens": None,
        "cache_hit_tokens": None,
        "total_tokens": None,
    }


def test_no_usage_event_when_all_unknown(tmp_path):
    store = _store(tmp_path)
    tok = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    store.accept_unit(RUN, "u0", tok, usage=UsageRecord(None, None, None), at=1002.0)
    assert _by_event(project_run(store, RUN), "usage") == []


def test_usage_zero_pair_is_unknown(tmp_path):
    store = _store(tmp_path)
    tok = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    store.fail_unit(RUN, "u0", tok, usage=UsageRecord(0, 0, 0), at=1002.0)
    assert _by_event(project_run(store, RUN), "usage") == []


# -- stream, boundary, snapshot ---------------------------------------------------------


def test_projection_validates_as_stream(tmp_path):
    store = _store(tmp_path, units=("u0", "u1", "u2"))
    t = store.claim_unit(RUN, "u0", "w", lease_seconds=1, at=1001.0).fencing_token
    t2 = store.claim_unit(RUN, "u0", "w2", at=2000.0).fencing_token
    with pytest.raises(StaleFenceError):
        store.fail_unit(RUN, "u0", t, error="late", at=2001.0)
    store.renew_lease(RUN, "u0", t2, at=2002.0)
    store.accept_unit(RUN, "u0", t2, usage=UsageRecord(1, 2, 3), at=2003.0)
    store.record_apply_started(RUN, "u0", at=2004.0)
    store.record_apply_succeeded(RUN, "u0", at=2005.0)
    t3 = store.claim_unit(RUN, "u1", "w", at=2006.0).fencing_token
    store.fail_unit(RUN, "u1", t3, error="x", terminal=True, at=2007.0)
    events = project_run(store, RUN)
    assert real_ee.validate_stream(events) == events


def test_write_run_events_writes_in_order_to_sink(tmp_path):
    store = _store(tmp_path)
    tok = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    store.accept_unit(RUN, "u0", tok, at=1002.0)
    sink = real_ee.InMemorySink()
    count = write_run_events(store, RUN, sink)
    assert count == len(sink.events) == len(project_run(store, RUN))
    assert tuple(sink.events) == project_run(store, RUN)


def test_write_run_events_jsonl_round_trips(tmp_path):
    store = _store(tmp_path)
    tok = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    store.accept_unit(RUN, "u0", tok, at=1002.0)
    path = tmp_path / "events.jsonl"
    write_run_events(store, RUN, real_ee.JsonlSink(path))
    assert real_ee.read_jsonl(path) == project_run(store, RUN)
    assert json.loads(path.read_text().splitlines()[0])["seq"] == 0


def test_unknown_run_raises(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(UnknownRunError):
        project_run(store, "nope")


def test_no_later_revision_names_emitted(tmp_path):
    store = _store(tmp_path)
    tok = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    store.renew_lease(RUN, "u0", tok, at=1002.0)
    store.accept_unit(RUN, "u0", tok, at=1003.0)
    store.record_apply_started(RUN, "u0", at=1004.0)
    names = {e["event"] for e in project_run(store, RUN)}
    assert not names & real_ee.LATER_REVISION_NAMES
    assert not names & {f"{PLUGIN}:{n}" for n in real_ee.LATER_REVISION_NAMES}
    assert "dispatch-selected" not in names


def test_projection_uses_one_snapshot(tmp_path):
    store = _store(tmp_path)
    tok = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    store.accept_unit(RUN, "u0", tok, at=1002.0)
    calls = []

    class Spy:
        def snapshot(self, run_id, **kwargs):
            calls.append("snapshot")
            return store.snapshot(run_id, **kwargs)

        def list_attempts(self, *a, **k):  # pragma: no cover - must not run
            calls.append("list_attempts")
            raise AssertionError("separate read")

        def get_run(self, *a, **k):  # pragma: no cover - must not run
            calls.append("get_run")
            raise AssertionError("separate read")

        def list_units(self, *a, **k):  # pragma: no cover - must not run
            calls.append("list_units")
            raise AssertionError("separate read")

    project_run(Spy(), RUN)
    assert calls == ["snapshot"]


def test_events_module_imports_no_other_plugin_store():
    source = Path(ev.__file__).read_text(encoding="utf-8")
    forbidden = ("job_kit", "llm_scripting_kit", "workflow_kit")
    imported = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert imported
    for name in imported:
        assert name.split(".")[0] not in forbidden, name
        # bootstrap_lib is reached only through the lazy probe, never a static import
        assert not name.startswith("bootstrap_lib"), name
    literals = [
        n.value
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    assert not any(v.split(".")[0] in forbidden for v in literals if v.isidentifier() or "." in v)


# -- the probe ---------------------------------------------------------------------------


def _fake_module(**overrides):
    attrs = {
        "SUPPORTED_SCHEMAS": real_ee.SUPPORTED_SCHEMAS,
        "make_event": real_ee.make_event,
        "utc_timestamp": real_ee.utc_timestamp,
        "usage_payload": real_ee.usage_payload,
        "validate_stream": real_ee.validate_stream,
    }
    attrs.update(overrides)
    attrs = {k: v for k, v in attrs.items() if v is not ...}
    return types.SimpleNamespace(**attrs)


def _patch_import(monkeypatch, module=None, *, absent=False, no_submodule=False):
    def fake(name):
        if name == "bootstrap_lib":
            if absent:
                raise ModuleNotFoundError("No module named 'bootstrap_lib'", name="bootstrap_lib")
            return types.SimpleNamespace()
        if no_submodule:
            raise ModuleNotFoundError(
                "No module named 'bootstrap_lib.execution_event'",
                name="bootstrap_lib.execution_event",
            )
        return module

    monkeypatch.setattr(ev, "_import_module", fake)


def test_probe_usable_returns_module(monkeypatch):
    module = _fake_module()
    _patch_import(monkeypatch, module)
    assert ev._execution_event() is module


def test_probe_absent_message(monkeypatch, tmp_path):
    _patch_import(monkeypatch, absent=True)
    with pytest.raises(ExecutionEventSupportError) as info:
        project_run(_store(tmp_path), RUN)
    text = str(info.value)
    assert "claude plugin install bootstrap@plugins-kit" in text
    assert "update" not in text
    assert isinstance(info.value, ImportError)


def test_probe_too_old_message(monkeypatch):
    _patch_import(monkeypatch, no_submodule=True)
    with pytest.raises(ExecutionEventSupportError) as info:
        ev._execution_event()
    text = str(info.value)
    assert "claude plugin update bootstrap@plugins-kit" in text
    assert ev.EXECUTION_EVENT_BOOTSTRAP in text
    assert "plugin install" not in text


def test_probe_message_version_is_consumer_constant(monkeypatch):
    _patch_import(monkeypatch, _fake_module(SUPPORTED_SCHEMAS=frozenset(), OWNER="x", VERSION="9.9.9"))
    with pytest.raises(ExecutionEventSupportError) as info:
        ev._execution_event()
    assert ev.EXECUTION_EVENT_BOOTSTRAP in str(info.value)
    assert "9.9.9" not in str(info.value)


def test_probe_rejects_module_without_schema_v1(monkeypatch):
    _patch_import(monkeypatch, _fake_module(SUPPORTED_SCHEMAS=frozenset({"plugins-kit.execution-event/v0"})))
    with pytest.raises(ExecutionEventSupportError, match="plugin update"):
        ev._execution_event()


def test_probe_rejects_module_missing_schema_marker(monkeypatch):
    _patch_import(monkeypatch, _fake_module(SUPPORTED_SCHEMAS=...))
    with pytest.raises(ExecutionEventSupportError, match="plugin update"):
        ev._execution_event()


@pytest.mark.parametrize("name", ["make_event", "utc_timestamp", "usage_payload", "validate_stream"])
def test_probe_rejects_missing_callable(monkeypatch, name):
    _patch_import(monkeypatch, _fake_module(**{name: ...}))
    with pytest.raises(ExecutionEventSupportError, match=name):
        ev._execution_event()


def test_probe_rejects_make_event_that_cannot_bind_cpk_keywords(monkeypatch):
    def narrow(*, seq, run_id, event, plugin, at=None, payload=None):  # no attempt_id
        raise AssertionError("never called")

    _patch_import(monkeypatch, _fake_module(make_event=narrow))
    with pytest.raises(ExecutionEventSupportError, match="call shape"):
        ev._execution_event()
