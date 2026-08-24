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

import os

import pytest

from content_pipeline.execution import controller
from content_pipeline.execution.adapter import (
    AdapterVersionMismatchError,
    PreparedRequest,
    RunAdapter,
    WorkerEnvironment,
    WorkerEnvironmentDeclarationError,
    WorkerEnvironmentMismatchError,
    require_compatible_adapter,
    require_compatible_environment,
    require_creatable_environment,
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


# -- WorkerEnvironment (item 5, A-min.4) --------------------------------------


def test_default_worker_environment_snapshot_is_empty(monkeypatch):
    monkeypatch.setenv("SOME_VAR", "x")
    env = WorkerEnvironment()
    assert env.snapshot() == {}


def test_default_worker_environment_check_is_a_no_op_regardless_of_recorded():
    """An adapter declaring nothing must behave exactly as today: `check`
    never raises, no matter what `recorded` contains."""
    env = WorkerEnvironment()
    env.check({"PWD": "anything"}, run_id="run-1")  # must not raise
    env.check({}, run_id="run-1")  # must not raise


def test_default_adapter_environment_field_is_a_no_op_worker_environment():
    adapter = RunAdapter()
    assert adapter.environment == WorkerEnvironment()


# -- WorkerEnvironment: forbidden vs required/cwd overlap is refused ---------


def test_overlap_between_required_and_forbidden_raises_at_construction(monkeypatch):
    """The exact repro from the security report: a name in BOTH
    required_vars and forbidden_vars must never reach snapshot() -- refuse
    the declaration outright, before any environment is even read."""
    monkeypatch.setenv("SECRET", "sk-leak-abc123")
    with pytest.raises(WorkerEnvironmentDeclarationError) as exc_info:
        WorkerEnvironment(required_vars=("SECRET",), forbidden_vars=("SECRET",))
    err = exc_info.value
    assert err.names == ("SECRET",)
    message = str(err)
    assert "SECRET" in message
    assert "sk-leak-abc123" not in message


def test_overlap_between_cwd_vars_and_forbidden_raises_at_construction():
    """The overlap check must cover cwd_vars too, not just required_vars --
    a cwd_var is captured by snapshot() exactly like a required_var."""
    with pytest.raises(WorkerEnvironmentDeclarationError) as exc_info:
        WorkerEnvironment(cwd_vars=("PWD",), forbidden_vars=("PWD",))
    assert exc_info.value.names == ("PWD",)


def test_overlap_of_several_names_names_all_of_them():
    with pytest.raises(WorkerEnvironmentDeclarationError) as exc_info:
        WorkerEnvironment(
            required_vars=("SECRET", "TOKEN"),
            cwd_vars=("PWD",),
            forbidden_vars=("SECRET", "TOKEN", "PWD"),
        )
    err = exc_info.value
    assert set(err.names) == {"SECRET", "TOKEN", "PWD"}
    message = str(err)
    for name in ("SECRET", "TOKEN", "PWD"):
        assert name in message


def test_non_overlapping_declaration_with_all_fields_still_constructs_and_snapshots(
    monkeypatch,
):
    """Regression guard: a legitimate, non-overlapping declaration using all
    three fields together must construct and snapshot exactly as before this
    fix -- this is the normal, untouched case."""
    monkeypatch.setenv("APP_ROOT", "D:\\dev\\proj")
    monkeypatch.setenv("PWD", "D:\\dev\\proj")
    monkeypatch.setenv("SECRET", "sk-leak-abc123")
    monkeypatch.setattr(os, "getcwd", lambda: "D:\\dev\\proj")
    env = WorkerEnvironment(
        required_vars=("APP_ROOT",),
        cwd_vars=("PWD",),
        forbidden_vars=("SECRET",),
        require_cwd=True,
    )
    snap = env.snapshot()
    assert snap == {
        "APP_ROOT": "D:\\dev\\proj",
        "PWD": "D:\\dev\\proj",
        "__cwd__": "D:\\dev\\proj",
    }


def test_snapshot_on_legitimate_declaration_never_contains_a_forbidden_value(
    monkeypatch,
):
    monkeypatch.setenv("SECRET", "sk-leak-abc123")
    monkeypatch.setenv("APP_ROOT", "D:\\dev\\proj")
    env = WorkerEnvironment(required_vars=("APP_ROOT",), forbidden_vars=("SECRET",))
    snap = env.snapshot()
    assert "SECRET" not in snap
    assert "sk-leak-abc123" not in snap.values()


def test_required_var_exact_match_passes(monkeypatch):
    monkeypatch.setenv("APP_ROOT", "D:\\dev\\proj")
    env = WorkerEnvironment(required_vars=("APP_ROOT",))
    env.check({"APP_ROOT": "D:\\dev\\proj"}, run_id="run-1")  # must not raise


def test_required_var_mismatch_refuses(monkeypatch):
    monkeypatch.setenv("APP_ROOT", "D:\\dev\\other")
    env = WorkerEnvironment(required_vars=("APP_ROOT",))
    with pytest.raises(WorkerEnvironmentMismatchError) as exc_info:
        env.check({"APP_ROOT": "D:\\dev\\proj"}, run_id="fp-2026-08-18")
    err = exc_info.value
    assert err.run_id == "fp-2026-08-18"
    assert err.var_name == "APP_ROOT"
    assert err.recorded_value == "D:\\dev\\proj"
    assert err.actual_value == "D:\\dev\\other"
    # Different locations entirely -- never mislabeled as a flavour mismatch.
    assert err.likely_path_flavour_mismatch is False


def test_required_var_git_bash_pwd_flavour_mismatch_is_flagged(monkeypatch):
    """The concrete case DECIDED point 3 targets: a POSIX-style Git Bash
    PWD value that resolves to the same location as a recorded native
    Windows path, but differs as a raw string. Comparison stays exact
    string equality (still refuses) -- only the computed
    `likely_path_flavour_mismatch` attribute changes."""
    monkeypatch.setenv("PWD", "/d/dev/example-project/main")
    env = WorkerEnvironment(required_vars=("PWD",))
    with pytest.raises(WorkerEnvironmentMismatchError) as exc_info:
        env.check({"PWD": "D:\\dev\\example-project\\main"}, run_id="fp-2026-08-18")
    err = exc_info.value
    assert err.likely_path_flavour_mismatch is True
    assert "different path flavour" in str(err)


def test_forbidden_var_present_refuses_without_leaking_its_value(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-super-secret-value")
    env = WorkerEnvironment(forbidden_vars=("OPENROUTER_API_KEY",))
    with pytest.raises(WorkerEnvironmentMismatchError) as exc_info:
        env.check({}, run_id="run-1")
    err = exc_info.value
    assert err.forbidden is True
    assert err.var_name == "OPENROUTER_API_KEY"
    # The value itself must never appear in the message or on the error.
    assert "sk-super-secret-value" not in str(err)
    assert err.recorded_value is None
    assert err.actual_value is None


def test_forbidden_var_absent_passes(monkeypatch):
    monkeypatch.delenv("SOME_FORBIDDEN_VAR", raising=False)
    env = WorkerEnvironment(forbidden_vars=("SOME_FORBIDDEN_VAR",))
    env.check({}, run_id="run-1")  # must not raise


def test_forbidden_var_names_never_appear_in_snapshot(monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "s3cr3t")
    env = WorkerEnvironment(forbidden_vars=("SECRET_TOKEN",))
    snapshot = env.snapshot()
    assert "SECRET_TOKEN" not in snapshot
    assert "s3cr3t" not in str(snapshot)


def test_require_cwd_mismatch_refuses(monkeypatch, tmp_path):
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path / "sub")
    env = WorkerEnvironment(require_cwd=True)
    with pytest.raises(WorkerEnvironmentMismatchError):
        env.check({"__cwd__": str(tmp_path)}, run_id="run-1")


def test_require_cwd_match_passes(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    env = WorkerEnvironment(require_cwd=True)
    recorded = env.snapshot()
    env.check(recorded, run_id="run-1")  # must not raise


def test_materialize_returns_pure_dict_math_no_subprocess(monkeypatch):
    env = WorkerEnvironment(required_vars=("A", "B"), require_cwd=True)
    recorded = {"A": "1", "B": "2", "__cwd__": "D:\\proj"}
    overlay, cwd = env.materialize(recorded)
    assert overlay == {"A": "1", "B": "2"}
    assert cwd == "D:\\proj"


def test_materialize_omits_required_vars_missing_from_recorded():
    env = WorkerEnvironment(required_vars=("A", "B"))
    overlay, cwd = env.materialize({"A": "1"})
    assert overlay == {"A": "1"}
    assert cwd is None


# -- require_compatible_environment (protocol-mount enforcement half) --------


def _run_with_env(environment):
    return RunRecord(
        id="run-1",
        driver="inline",
        backend="mock",
        model="m",
        adapter_version="",
        created_at=0.0,
        environment=environment,
    )


def test_require_compatible_environment_default_adapter_is_unaffected(monkeypatch):
    """An adapter declaring nothing must be unaffected by whatever the run
    recorded -- even a wildly different environment."""
    monkeypatch.setenv("APP_ROOT", "D:\\dev\\here")
    run = _run_with_env({"APP_ROOT": "D:\\dev\\elsewhere"})
    require_compatible_environment(run, RunAdapter())  # must not raise


def test_require_compatible_environment_passes_on_matching_environment(monkeypatch):
    monkeypatch.setenv("APP_ROOT", "D:\\dev\\proj")
    run = _run_with_env({"APP_ROOT": "D:\\dev\\proj"})
    adapter = RunAdapter(environment=WorkerEnvironment(required_vars=("APP_ROOT",)))
    require_compatible_environment(run, adapter)  # must not raise


def test_require_compatible_environment_refuses_on_mismatch(monkeypatch):
    monkeypatch.setenv("APP_ROOT", "D:\\dev\\wrong")
    run = _run_with_env({"APP_ROOT": "D:\\dev\\proj"})
    adapter = RunAdapter(environment=WorkerEnvironment(required_vars=("APP_ROOT",)))
    with pytest.raises(WorkerEnvironmentMismatchError):
        require_compatible_environment(run, adapter)


def test_require_compatible_environment_treats_none_recorded_as_empty(monkeypatch):
    monkeypatch.setenv("APP_ROOT", "D:\\dev\\proj")
    run = _run_with_env(None)
    adapter = RunAdapter(environment=WorkerEnvironment(required_vars=("APP_ROOT",)))
    with pytest.raises(WorkerEnvironmentMismatchError):
        require_compatible_environment(run, adapter)


# -- require_creatable_environment (create-run anchor half) ------------------
#
# Verification-pass fix: create-run compares ONLY `cwd_vars` (never a
# heuristic over `required_vars`), and BY RESOLVED LOCATION (ntpath-based,
# no OS calls) rather than raw string equality -- so a trailing separator
# or a drive-letter-case difference passes, while the Git Bash POSIX case
# still refuses because it resolves to a genuinely DIFFERENT location once
# joined against cwd. The six cases below are the probe that found the
# original defect; FAKE_CWD is built from the REAL os.getcwd() so they hold
# on any machine, not just one hardcoded path.

FAKE_CWD = "D:\\dev\\example-project\\main"


def test_probe_1_git_bash_posix_pwd_refuses(monkeypatch):
    """The target bug: PWD is Git-Bash-POSIX-flavoured for the same
    location as cwd. Must still refuse -- resolving it against cwd via
    ntpath.join takes the drive from cwd and appends the POSIX segments
    UNCHANGED, landing on a DIFFERENT location than cwd itself."""
    monkeypatch.setattr(os, "getcwd", lambda: FAKE_CWD)
    snapshot = {"PWD": "/d/dev/example-project/main"}
    env = WorkerEnvironment(cwd_vars=("PWD",))
    with pytest.raises(WorkerEnvironmentMismatchError) as exc_info:
        require_creatable_environment("fp-2026-08-18", env, snapshot)
    err = exc_info.value
    assert err.likely_path_flavour_mismatch is True
    assert err.recorded_value == FAKE_CWD
    assert err.actual_value == "/d/dev/example-project/main"


def test_probe_1_resolved_value_lands_on_a_different_location_than_cwd(monkeypatch):
    """Pin the ACTUAL resolved value, not just that a mismatch occurred --
    confirms the Git Bash catch survives moving from exact-string to
    resolved-location comparison rather than assuming it."""
    from content_pipeline.execution.adapter import _resolve_against_cwd

    resolved = _resolve_against_cwd(FAKE_CWD, "/d/dev/example-project/main")
    assert resolved == "d:\\d\\dev\\example-project\\main"
    assert resolved != _resolve_against_cwd(FAKE_CWD, FAKE_CWD)


def test_probe_2_native_pwd_equal_to_cwd_passes(monkeypatch):
    monkeypatch.setattr(os, "getcwd", lambda: FAKE_CWD)
    snapshot = {"PWD": FAKE_CWD}
    env = WorkerEnvironment(cwd_vars=("PWD",))
    require_creatable_environment("run-1", env, snapshot)  # must not raise


def test_probe_3_pwd_equal_to_cwd_plus_trailing_separator_passes(monkeypatch):
    monkeypatch.setattr(os, "getcwd", lambda: FAKE_CWD)
    snapshot = {"PWD": FAKE_CWD + "\\"}
    env = WorkerEnvironment(cwd_vars=("PWD",))
    require_creatable_environment("run-1", env, snapshot)  # must not raise


def test_probe_4_pwd_equal_to_cwd_drive_letter_case_swapped_passes(monkeypatch):
    monkeypatch.setattr(os, "getcwd", lambda: FAKE_CWD)
    snapshot = {"PWD": "d:\\dev\\example-project\\main"}
    env = WorkerEnvironment(cwd_vars=("PWD",))
    require_creatable_environment("run-1", env, snapshot)  # must not raise


def test_probe_5_content_root_legitimately_different_from_cwd_passes(monkeypatch):
    """A real content root that is NOT the cwd must never be refused --
    it is not named in `cwd_vars`, so it is never compared to os.getcwd()
    at all, no matter how path-like its value looks."""
    monkeypatch.setattr(os, "getcwd", lambda: FAKE_CWD)
    snapshot = {"CONTENT_ROOT": FAKE_CWD + "\\plugins"}
    env = WorkerEnvironment(cwd_vars=())  # CONTENT_ROOT not declared as a cwd_var
    require_creatable_environment("run-1", env, snapshot)  # must not raise


def test_probe_6_non_path_token_not_a_cwd_var_passes(monkeypatch):
    monkeypatch.setattr(os, "getcwd", lambda: FAKE_CWD)
    snapshot = {"SOME_TOKEN": "sk-abc123"}
    env = WorkerEnvironment(cwd_vars=())
    require_creatable_environment("run-1", env, snapshot)  # must not raise


def test_required_var_not_in_cwd_vars_is_never_compared_to_cwd(monkeypatch):
    """Explicit coverage for the fix's core claim: a variable named in
    `required_vars` but NOT in `cwd_vars` is never compared against
    os.getcwd() by `require_creatable_environment`, even when its value
    looks exactly like a path and even when it is a genuinely different
    location from cwd."""
    monkeypatch.setattr(os, "getcwd", lambda: FAKE_CWD)
    snapshot = {"CONTENT_ROOT": FAKE_CWD + "\\plugins"}
    env = WorkerEnvironment(required_vars=("CONTENT_ROOT",), cwd_vars=())
    require_creatable_environment("run-1", env, snapshot)  # must not raise


def test_a_var_may_be_in_both_required_vars_and_cwd_vars(monkeypatch):
    """A name may be declared in both -- worker-side exact match via
    `required_vars` AND the create-time resolved-location anchor via
    `cwd_vars` -- and the create-time check still passes on a same-location
    spelling difference."""
    monkeypatch.setattr(os, "getcwd", lambda: FAKE_CWD)
    snapshot = {"PWD": FAKE_CWD + "\\"}
    env = WorkerEnvironment(required_vars=("PWD",), cwd_vars=("PWD",))
    require_creatable_environment("run-1", env, snapshot)  # must not raise


# -- likely_path_flavour_mismatch: only True for a GENUINE flavour mismatch --


def test_likely_path_flavour_mismatch_false_for_trailing_separator_difference(monkeypatch):
    """A trailing-separator difference is not a Git-Bash-flavour mismatch --
    exercised via the worker-side `check()` (still exact string equality,
    so this DOES raise) to inspect the flag on a real raised error."""
    monkeypatch.setenv("PWD", FAKE_CWD)
    env = WorkerEnvironment(required_vars=("PWD",))
    with pytest.raises(WorkerEnvironmentMismatchError) as exc_info:
        env.check({"PWD": FAKE_CWD + "\\"}, run_id="run-1")
    assert exc_info.value.likely_path_flavour_mismatch is False
    assert "path flavour" not in str(exc_info.value)


def test_likely_path_flavour_mismatch_false_for_case_only_difference(monkeypatch):
    monkeypatch.setenv("PWD", FAKE_CWD)
    env = WorkerEnvironment(required_vars=("PWD",))
    with pytest.raises(WorkerEnvironmentMismatchError) as exc_info:
        env.check({"PWD": "d:\\dev\\example-project\\main"}, run_id="run-1")
    assert exc_info.value.likely_path_flavour_mismatch is False
    assert "path flavour" not in str(exc_info.value)


def test_likely_path_flavour_mismatch_true_for_the_git_bash_case(monkeypatch):
    monkeypatch.setenv("PWD", "/d/dev/example-project/main")
    env = WorkerEnvironment(required_vars=("PWD",))
    with pytest.raises(WorkerEnvironmentMismatchError) as exc_info:
        env.check({"PWD": FAKE_CWD}, run_id="run-1")
    assert exc_info.value.likely_path_flavour_mismatch is True
    assert "path flavour" in str(exc_info.value)


# -- WorkerEnvironment.check: worker-side cwd_vars enforcement ---------------
#
# Closes the gap: previously `cwd_vars` was enforced ONLY at create-run time
# (`require_creatable_environment`, in the ORCHESTRATOR's process). A worker
# declaring only `cwd_vars` (no `required_vars`, `require_cwd=False`) got
# zero worker-side enforcement -- the value was snapshotted and never
# compared again. `check()` now also verifies, in the WORKER's own process,
# that each declared `cwd_var`'s LIVE value resolves to THIS worker's own
# `os.getcwd()` -- same resolved-location comparison
# `require_creatable_environment` performs, reusing the same helpers, but
# comparing against the worker's own cwd rather than a recorded snapshot.


def test_worker_side_cwd_var_matching_own_cwd_passes(monkeypatch):
    monkeypatch.setattr(os, "getcwd", lambda: FAKE_CWD)
    monkeypatch.setenv("PWD", FAKE_CWD)
    env = WorkerEnvironment(cwd_vars=("PWD",))
    env.check({}, run_id="run-1")  # must not raise


def test_worker_side_cwd_var_trailing_separator_tolerated(monkeypatch):
    monkeypatch.setattr(os, "getcwd", lambda: FAKE_CWD)
    monkeypatch.setenv("PWD", FAKE_CWD + "\\")
    env = WorkerEnvironment(cwd_vars=("PWD",))
    env.check({}, run_id="run-1")  # must not raise


def test_worker_side_cwd_var_drive_letter_case_tolerated(monkeypatch):
    monkeypatch.setattr(os, "getcwd", lambda: FAKE_CWD)
    monkeypatch.setenv("PWD", "d:\\dev\\example-project\\main")
    env = WorkerEnvironment(cwd_vars=("PWD",))
    env.check({}, run_id="run-1")  # must not raise


def test_worker_side_cwd_var_pointing_elsewhere_is_refused(monkeypatch):
    monkeypatch.setattr(os, "getcwd", lambda: FAKE_CWD)
    monkeypatch.setenv("PWD", "D:\\dev\\somewhere\\else")
    env = WorkerEnvironment(cwd_vars=("PWD",))
    with pytest.raises(WorkerEnvironmentMismatchError) as exc_info:
        env.check({}, run_id="run-1")
    err = exc_info.value
    assert err.var_name == "PWD"
    assert err.actual_value == "D:\\dev\\somewhere\\else"
    assert err.worker_cwd == FAKE_CWD
    message = str(err)
    assert "PWD" in message
    assert repr("D:\\dev\\somewhere\\else") in message
    assert repr(FAKE_CWD) in message
    assert "wrong directory" in message


def test_worker_side_cwd_var_git_bash_posix_path_is_refused(monkeypatch):
    """A Git Bash POSIX-style `PWD` (`/d/dev/x`) for a worker whose real cwd
    is `D:\\dev\\x` (native) must still refuse -- it resolves against the
    worker's own cwd to a DIFFERENT location, exactly the same reasoning as
    the create-run anchor check's Git Bash probe."""
    monkeypatch.setattr(os, "getcwd", lambda: FAKE_CWD)
    monkeypatch.setenv("PWD", "/d/dev/example-project/main")
    env = WorkerEnvironment(cwd_vars=("PWD",))
    with pytest.raises(WorkerEnvironmentMismatchError) as exc_info:
        env.check({}, run_id="run-1")
    assert exc_info.value.likely_path_flavour_mismatch is True


def test_cwd_vars_only_declaration_gets_real_worker_side_enforcement(monkeypatch):
    """Pin the exact configuration the finding was about: a `cwd_vars`-only
    declaration (no `required_vars`, `require_cwd=False`) previously passed
    `check()` unconditionally no matter what the worker's environment was --
    it now gets the same worker-side enforcement as any other cwd_var."""
    monkeypatch.setattr(os, "getcwd", lambda: FAKE_CWD)
    monkeypatch.setenv("PWD", "D:\\dev\\somewhere\\else")
    env = WorkerEnvironment(cwd_vars=("PWD",), required_vars=(), require_cwd=False)
    assert env.required_vars == ()
    assert env.require_cwd is False
    with pytest.raises(WorkerEnvironmentMismatchError):
        env.check({}, run_id="run-1")


def test_worker_side_cwd_var_unset_is_skipped_not_raised(monkeypatch):
    """An unset/empty cwd_var has nothing to compare -- same skip behaviour
    as `require_creatable_environment`'s own snapshot-side skip."""
    monkeypatch.setattr(os, "getcwd", lambda: FAKE_CWD)
    monkeypatch.delenv("PWD", raising=False)
    env = WorkerEnvironment(cwd_vars=("PWD",))
    env.check({}, run_id="run-1")  # must not raise


def test_worker_side_cwd_var_check_does_not_affect_required_vars_exact_match(monkeypatch):
    """Regression: a name in BOTH `required_vars` and `cwd_vars` still gets
    exact-string matching against the recorded snapshot for the
    `required_vars` half, unaffected by the new resolved-location cwd_vars
    check running alongside it."""
    monkeypatch.setattr(os, "getcwd", lambda: FAKE_CWD)
    monkeypatch.setenv("PWD", FAKE_CWD + "\\")  # same location, different spelling
    env = WorkerEnvironment(required_vars=("PWD",), cwd_vars=("PWD",))
    # cwd_vars half passes (resolved-location), but required_vars half is
    # exact string equality against the recorded snapshot and must still
    # refuse on a spelling difference.
    with pytest.raises(WorkerEnvironmentMismatchError) as exc_info:
        env.check({"PWD": FAKE_CWD}, run_id="run-1")
    assert exc_info.value.recorded_value == FAKE_CWD
    assert exc_info.value.actual_value == FAKE_CWD + "\\"


# -- resolve_expected_unit_seconds (item 2, A-min.4) --------------------------


def test_resolve_expected_unit_seconds_undeclared_returns_none():
    adapter = RunAdapter()
    assert adapter.resolve_expected_unit_seconds(WorkUnit(id="u0")) is None
    assert adapter.resolve_expected_unit_seconds(None) is None


def test_resolve_expected_unit_seconds_falls_back_to_flat_value():
    adapter = RunAdapter(expected_unit_seconds=213.0)
    assert adapter.resolve_expected_unit_seconds(WorkUnit(id="u0")) == 213.0
    assert adapter.resolve_expected_unit_seconds(None) == 213.0


def test_resolve_expected_unit_seconds_prefers_per_unit_callable():
    adapter = RunAdapter(
        expected_unit_seconds=100.0,
        unit_seconds_for=lambda u: 213.0 if u.id == "slow" else None,
    )
    assert adapter.resolve_expected_unit_seconds(WorkUnit(id="slow")) == 213.0
    # Per-unit callable returns None for this unit -> falls back to the flat value.
    assert adapter.resolve_expected_unit_seconds(WorkUnit(id="fast")) == 100.0


def test_resolve_expected_unit_seconds_per_unit_callable_needs_a_unit():
    """unit_seconds_for is only consulted when a unit is actually
    available -- with unit=None it falls straight to expected_unit_seconds."""
    adapter = RunAdapter(
        expected_unit_seconds=100.0,
        unit_seconds_for=lambda u: 999.0,
    )
    assert adapter.resolve_expected_unit_seconds(None) == 100.0
