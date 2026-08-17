"""Tests for content_pipeline.execution.adapter.

Pins the A-min.3 widening of ``RunAdapter``: it now lives in
``execution.adapter`` (``execution.controller`` re-exports the identical
class -- both call sites, ``drivers.inline.run_wave`` and ``finalize_run``,
still share the SAME object, D1's re-parse requirement holding by
construction); the two new first-class steps (``resolve_prepared_request``,
``resolve_validation_spec``) fall back to the A-min.2 fields
(``system_for``/``user_for``, ``parse_fn``/``validators``) when the new
optional fields are not supplied; and ``require_compatible_adapter``
refuses an incompatible resume (D1) without ever running automatically
inside ``prepare_run``/``finalize_run`` themselves.
"""

from __future__ import annotations

import pytest

from content_pipeline.execution import controller
from content_pipeline.execution.adapter import (
    AdapterVersionMismatchError,
    PreparedRequest,
    RunAdapter,
    require_compatible_adapter,
)
from content_pipeline.execution.model import RunRecord
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.llm.platform import ValidationSpec
from content_pipeline.pipeline.workunit import WorkUnit
from content_pipeline.validate import contract


# -- identity: controller.RunAdapter IS adapter.RunAdapter -------------------


def test_controller_reexports_the_identical_class():
    assert controller.RunAdapter is RunAdapter


def test_default_adapter_still_constructs_with_no_arguments():
    """A-min.2 callers construct ``RunAdapter()`` with zero arguments
    (``drivers.inline.run_wave``'s own default). The A-min.3 widenings must
    all default too, or every such call site breaks."""
    adapter = RunAdapter()
    assert adapter.unit_for("u0") == WorkUnit(id="u0")
    assert adapter.adapter_version == ""
    assert adapter.build_request is None
    assert adapter.validation_spec_for is None


# -- resolve_prepared_request (responsibility 2) ------------------------------


def test_resolve_prepared_request_uses_build_request_when_supplied():
    unit = WorkUnit(id="u0", payload="p")
    adapter = RunAdapter(
        build_request=lambda u: PreparedRequest(unit=u, system="sys", user=f"user:{u.id}")
    )
    request = adapter.resolve_prepared_request(unit)
    assert request == PreparedRequest(unit=unit, system="sys", user="user:u0")


def test_resolve_prepared_request_falls_back_to_system_for_user_for():
    unit = WorkUnit(id="u0")
    adapter = RunAdapter(
        system_for=lambda u: f"system:{u.id}",
        user_for=lambda u: f"user:{u.id}",
    )
    request = adapter.resolve_prepared_request(unit)
    assert request == PreparedRequest(unit=unit, system="system:u0", user="user:u0")


def test_resolve_prepared_request_defaults_system_to_empty_string():
    unit = WorkUnit(id="u0")
    adapter = RunAdapter(user_for=lambda u: "user text")
    request = adapter.resolve_prepared_request(unit)
    assert request.system == ""
    assert request.user == "user text"


def test_resolve_prepared_request_raises_without_build_request_or_user_for():
    unit = WorkUnit(id="u0")
    adapter = RunAdapter()
    with pytest.raises(ValueError):
        adapter.resolve_prepared_request(unit)


# -- resolve_validation_spec (responsibility 3) -------------------------------


def test_resolve_validation_spec_uses_validation_spec_for_when_supplied():
    unit = WorkUnit(id="u0")
    spec = ValidationSpec(parse_fn=lambda t: t, validators=(), context="ctx")
    adapter = RunAdapter(validation_spec_for=lambda u: spec)
    assert adapter.resolve_validation_spec(unit) is spec


def test_resolve_validation_spec_falls_back_to_parse_fn_and_validators():
    unit = WorkUnit(id="u0")

    def validator(candidate, context):
        return [contract.Rejection(kind="bad")] if candidate != "ok" else []

    adapter = RunAdapter(parse_fn=lambda t: t, validators=(validator,), validation_context="ctx")
    spec = adapter.resolve_validation_spec(unit)
    assert spec.parse_fn("x") == "x"
    assert spec.validators == (validator,)
    assert spec.context == "ctx"


def test_resolve_validation_spec_raises_without_validation_spec_for_or_parse_fn():
    unit = WorkUnit(id="u0")
    adapter = RunAdapter()
    with pytest.raises(ValueError):
        adapter.resolve_validation_spec(unit)


# -- require_compatible_adapter (D1: incompatible resume refused) ------------


def _run(adapter_version: str) -> RunRecord:
    return RunRecord(
        id="run-1",
        driver="inline",
        backend="mock",
        model="m",
        adapter_version=adapter_version,
        created_at=0.0,
    )


def test_require_compatible_adapter_passes_on_matching_version():
    require_compatible_adapter(_run("v1"), RunAdapter(adapter_version="v1"))


def test_require_compatible_adapter_passes_when_both_blank():
    """The A-min.1/A-min.2 default: neither side ever populated a real
    adapter_version. Must not spuriously refuse a run that never opted in."""
    require_compatible_adapter(_run(""), RunAdapter())


def test_require_compatible_adapter_refuses_on_mismatch():
    with pytest.raises(AdapterVersionMismatchError) as exc_info:
        require_compatible_adapter(_run("v1"), RunAdapter(adapter_version="v2"))
    assert exc_info.value.run_id == "run-1"
    assert exc_info.value.run_adapter_version == "v1"
    assert exc_info.value.adapter_version == "v2"


def test_require_compatible_adapter_not_invoked_automatically_by_prepare_or_finalize(tmp_path):
    """A-min.2's prepare_run/finalize_run behavior and tests are unchanged --
    this check is a NEW, opt-in call the protocol layer makes, not something
    those two functions run on the caller's behalf. Covers BOTH functions
    named in the test's own name -- a prior version of this test called only
    finalize_run, claiming coverage of prepare_run it did not have."""
    from content_pipeline.execution.controller import finalize_run, prepare_run
    from content_pipeline.pipeline.workunit import FlatChunkStrategy

    store = ExecutionStore(tmp_path / "run.db")
    store.create_run("run-1", driver="inline", backend="mock", model="m", adapter_version="v1")
    store.register_units("run-1", ["u0"])
    # A mismatched adapter_version ("mismatch" != "v1") must NOT stop
    # finalize_run from running (it has nothing to apply here, but if the
    # check were wired in automatically this would raise before even
    # reaching the empty-loop no-op).
    adapter = RunAdapter(
        parse_fn=lambda t: t, apply=lambda uid, payload: None, adapter_version="mismatch"
    )
    applied = finalize_run(store, "run-1", adapter)
    assert applied == []

    # Same mismatch, same non-automatic-check claim, for prepare_run: it
    # runs to completion and returns the ready wave (u0, still PENDING --
    # no gates/freshness were configured to skip it) rather than raising
    # before ever computing one, which is what would happen if the check
    # were wired in automatically.
    flat_strategy = FlatChunkStrategy(select=lambda store: [])
    wave = prepare_run(store, "run-1", flat_strategy, [])
    assert [u.unit_id for u in wave] == ["u0"]
