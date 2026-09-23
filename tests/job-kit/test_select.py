"""Tests for advertisement-filtered deterministic endpoint choice."""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

import llm_scripting_kit.completion as llm_scripting_kit_completion
from llm_scripting_kit.completion import (
    BackendSelection,
    Capabilities,
    ParamCapability,
    match_capabilities,
)
from llm_scripting_kit.models import EndpointResolveError

import job_kit.select as job_kit_select
from job_kit.model import Contract, Job, Prompt
from job_kit.select import requirements_match, select_endpoint


class FakeBackend:
    """Minimal backend identity used by the selection seam."""

    name = "fake"


def _job(tmp_path: Path) -> Job:
    """Build a job that requires an advertised parameter."""
    return Job(
        id="select-me",
        prompt=Prompt(user="hello"),
        models=("first", "second"),
        requirements={"params": ["effort"]},
        directory=tmp_path,
        contract=Contract(command=("true",), directory=tmp_path),
    )


def test_selection_uses_order_and_stubbed_advertisement(tmp_path: Path) -> None:
    """The first endpoint without the required advertised param is skipped."""
    calls: list[str] = []

    def factory(endpoint: str) -> BackendSelection:
        calls.append(endpoint)
        backend = FakeBackend()
        backend.name = "without-effort" if endpoint == "first" else "fake"
        return BackendSelection(endpoint, "fake", backend, "fake-model")

    advertisement = {
        "without-effort": Capabilities(adapter="without-effort"),
        "fake": Capabilities(
            adapter="fake", params={"effort": ParamCapability(type="string")}
        ),
    }
    selected = select_endpoint(
        _job(tmp_path),
        capabilities=advertisement,
        backend_factory=factory,
    )

    assert selected.endpoint == "second"
    assert calls == ["first", "second"]


def test_selection_skips_a_persistently_halted_endpoint(tmp_path: Path) -> None:
    """A halted endpoint is excluded before the preference walk."""
    calls: list[str] = []

    def factory(endpoint: str) -> BackendSelection:
        calls.append(endpoint)
        return BackendSelection(endpoint, "fake", FakeBackend(), "fake-model")

    advertisement = {"fake": Capabilities(adapter="fake")}
    job = Job(
        id="halted",
        prompt=Prompt(user="hello"),
        models=("first", "second"),
        directory=tmp_path,
        contract=Contract(command=("true",), directory=tmp_path),
    )
    selected = select_endpoint(
        job,
        halted_endpoints=("first",),
        capabilities=advertisement,
        backend_factory=factory,
    )
    assert selected.endpoint == "second"
    assert calls == ["second"]


def test_selection_skips_an_unknown_endpoint_name(tmp_path: Path) -> None:
    """An unresolvable preference is not a compatible endpoint."""
    calls: list[str] = []

    def factory(endpoint: str) -> BackendSelection:
        calls.append(endpoint)
        if endpoint == "unknown":
            raise EndpointResolveError("unknown endpoint")
        return BackendSelection(endpoint, "fake", FakeBackend(), "fake-model")

    job = Job(
        id="unknown-first",
        prompt=Prompt(user="hello"),
        models=("unknown", "second"),
        directory=tmp_path,
        contract=Contract(command=("true",), directory=tmp_path),
    )
    selected = select_endpoint(
        job,
        capabilities={"fake": Capabilities(adapter="fake")},
        backend_factory=factory,
    )

    assert selected.endpoint == "second"
    assert calls == ["unknown", "second"]


def test_selection_does_not_fall_back_to_an_endpoint_keyed_advertisement(
    tmp_path: Path,
) -> None:
    """The returned backend name owns the advertisement -- an endpoint-keyed
    record for a DIFFERENT (stale/colliding) name must not satisfy selection,
    or execution (which looks up only by backend name) can find nothing where
    selection thought it found a match."""

    class RenamedBackend(FakeBackend):
        name = "unadvertised-v2"

    def factory(endpoint: str) -> BackendSelection:
        return BackendSelection(endpoint, "fake", RenamedBackend(), "fake-model")

    job = Job(
        id="stale-key",
        prompt=Prompt(user="hello"),
        models=("old-name",),
        directory=tmp_path,
        contract=Contract(command=("true",), directory=tmp_path),
    )
    # A satisfying record keyed by the ENDPOINT name only -- no
    # "unadvertised-v2" entry exists, which is what execution would look up.
    advertisement = {"old-name": Capabilities(adapter="old-name")}

    with pytest.raises(job_kit_select.NoCompatibleEndpointError):
        select_endpoint(
            job,
            capabilities=advertisement,
            backend_factory=factory,
        )


def test_requirements_match_delegates_to_llm_scripting_kit_list_shorthand() -> None:
    """The list shorthand ({"params": [...]}) matches through the LSK matcher."""
    capabilities = Capabilities(
        adapter="fake", params={"effort": ParamCapability(type="string")}
    )
    requirements = ["effort"]

    assert requirements_match(capabilities, requirements) == match_capabilities(
        capabilities, requirements
    )
    assert requirements_match(capabilities, requirements) is True


def test_requirements_match_delegates_to_llm_scripting_kit_dotted_path() -> None:
    """A dotted-path key matches through the LSK matcher's advertisement walk."""
    capabilities = Capabilities(adapter="fake")
    requirements = {"adapter": "fake"}

    assert requirements_match(capabilities, requirements) == match_capabilities(
        capabilities, requirements
    )
    assert requirements_match(capabilities, requirements) is True

    mismatched = {"adapter": "other"}
    assert requirements_match(capabilities, mismatched) == match_capabilities(
        capabilities, mismatched
    )
    assert requirements_match(capabilities, mismatched) is False


def test_import_raises_shared_lib_too_old_when_frontier_symbol_missing() -> None:
    """An old llm-scripting-kit shared lib fails import with a clear message.

    Simulates a job-kit venv linked (by the bootstrap shared-lib linker,
    which pins no version) against an llm-scripting-kit older than
    _MIN_LLM_SCRIPTING_KIT_VERSION, where llm_scripting_kit.completion has
    every symbol job-kit uses EXCEPT the frontier one,
    subjects_for_disallowed_tools (added with the effect-based deny floor).
    Re-imports job_kit.select against a stub module missing that symbol and
    restores the real module afterward so later tests are unaffected.
    """
    stub = types.ModuleType("llm_scripting_kit.completion")
    stub.BackendSelection = llm_scripting_kit_completion.BackendSelection
    stub.Capabilities = llm_scripting_kit_completion.Capabilities
    stub.adapter_capabilities = llm_scripting_kit_completion.adapter_capabilities
    stub.create_backend = llm_scripting_kit_completion.create_backend
    stub.match_capabilities = llm_scripting_kit_completion.match_capabilities
    # subjects_for_disallowed_tools deliberately omitted: the frontier symbol.

    real_completion = sys.modules["llm_scripting_kit.completion"]
    real_select = sys.modules["job_kit.select"]
    sys.modules["llm_scripting_kit.completion"] = stub
    try:
        # importlib.reload redefines job_kit_select.SharedLibTooOldError as a
        # NEW class object mid-reload, so a reference captured before the
        # call (as pytest.raises(job_kit_select.SharedLibTooOldError) would
        # capture it) would not match the raised instance's type. Catch the
        # stable base (ImportError) and check the class name afterward.
        with pytest.raises(ImportError) as excinfo:
            importlib.reload(job_kit_select)
    finally:
        sys.modules["llm_scripting_kit.completion"] = real_completion
        sys.modules["job_kit.select"] = real_select
        importlib.reload(job_kit_select)

    assert type(excinfo.value).__name__ == "SharedLibTooOldError"
    message = str(excinfo.value)
    assert "llm-scripting-kit" in message
    # Assert the DECLARED minimum rather than a literal: the constant tracks the
    # frontier symbol, so it moves whenever job-kit starts using a newer one, and
    # a hardcoded version here would have to be hand-edited on every such move.
    # What the contract actually requires is that the message name the owning
    # plugin, a version the user can act on, and the missing symbol.
    assert job_kit_select._MIN_LLM_SCRIPTING_KIT_VERSION in message
    assert "subjects_for_disallowed_tools" in message


def test_readme_version_floor_matches_the_declared_frontier() -> None:
    """The README's minimum-version claim tracks the code, not a stale number."""
    readme = (
        Path(__file__).resolve().parents[2] / "plugins" / "job-kit" / "README.md"
    ).read_text(encoding="utf-8")
    assert (
        f"llm-scripting-kit >= {job_kit_select._MIN_LLM_SCRIPTING_KIT_VERSION}"
        in readme
    )
    assert "subjects_for_disallowed_tools" in readme


# ---------------------------------------------------------------------------
# Selection through llm-scripting-kit's describe(caller="process")
# ---------------------------------------------------------------------------

import types as _types

import llm_scripting_kit.declaration as lsk_declaration
import llm_scripting_kit.models as lsk_models
from llm_scripting_kit.declaration import NoUsableRoutingTarget
from llm_scripting_kit.reachability import Reachability


def _paced_entry(pool: str = "seven_day") -> _types.SimpleNamespace:
    """A merged registry entry that declares conserve_usage on ``pool``."""
    return _types.SimpleNamespace(
        kind="fake",
        harness=None,
        model="fake-model",
        family=None,
        tier=None,
        base_url=None,
        conserve_usage=_types.SimpleNamespace(pool=pool, display_name=None),
    )


def _install_paces(
    monkeypatch: pytest.MonkeyPatch,
    paces: dict[str, float],
    *,
    out_of_quota: tuple[str, ...] = (),
) -> None:
    """Give each named entry a pinned verdict and a fresh pace reading."""
    monkeypatch.setattr(
        lsk_models,
        "discover_model_entries",
        lambda **_: {name: _paced_entry(name) for name in [*paces, *out_of_quota]},
    )

    def pinned(name: str, spec: object, harness: object) -> object:
        spent = name in out_of_quota
        return _types.SimpleNamespace(
            usable=not spent,
            status="out-of-quota" if spent else "available",
            detail="spent" if spent else "",
            resets_at=1_900_000_000 if spent else None,
        )

    def fresh(name: str, spec: object, harness: object) -> object:
        return _types.SimpleNamespace(remaining=paces.get(name, 0.5), window_remaining=1.0)

    monkeypatch.setattr(lsk_declaration, "pinned_evaluate", pinned)
    monkeypatch.setattr(lsk_declaration, "_fresh_reading", fresh)


def _fake_factory(calls: list[str] | None = None, missing: tuple[str, ...] = ()):
    def factory(endpoint: str, **_: object) -> BackendSelection:
        if calls is not None:
            calls.append(endpoint)
        if endpoint in missing:
            raise EndpointResolveError(f"unknown endpoint {endpoint}")
        return BackendSelection(endpoint, "fake", FakeBackend(), "fake-model")

    return factory


def _declared_job(tmp_path: Path, models: tuple[str, ...]) -> Job:
    return Job(
        id="declared",
        prompt=Prompt(user="hello"),
        models=models,
        directory=tmp_path,
        contract=Contract(command=("true",), directory=tmp_path),
    )


def test_selection_takes_the_first_usable_entry_of_the_pace_ordered_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paced entries re-sort by pace, highest first; the first usable runs."""
    _install_paces(monkeypatch, {"behind": 0.4, "ahead": 1.2})

    selected = select_endpoint(
        _declared_job(tmp_path, ("behind", "ahead")),
        capabilities={"fake": Capabilities(adapter="fake")},
        backend_factory=_fake_factory(),
    )

    assert selected.endpoint == "ahead"


def test_selection_reports_the_pace_readings_it_chose_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The readings a run logs are the rendered entries, in pace order."""
    _install_paces(monkeypatch, {"behind": 0.4, "ahead": 1.2})

    selection, readings = job_kit_select.select_endpoint_with_readings(
        _declared_job(tmp_path, ("behind", "ahead")),
        capabilities={"fake": Capabilities(adapter="fake")},
        backend_factory=_fake_factory(),
    )

    assert selection.endpoint == "ahead"
    assert [reading["id"] for reading in readings] == ["ahead", "behind"]
    assert [reading["pace"] for reading in readings] == [1.2, 0.4]
    assert [reading["usable"] for reading in readings] == [True, True]


def test_an_out_of_quota_entry_is_skipped_before_the_first_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pinned OUT-OF-QUOTA verdict skips the entry, silently."""
    _install_paces(monkeypatch, {"second": 1.0}, out_of_quota=("first",))

    selected = select_endpoint(
        _declared_job(tmp_path, ("first", "second")),
        capabilities={"fake": Capabilities(adapter="fake")},
        backend_factory=_fake_factory(),
    )

    assert selected.endpoint == "second"
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert caplog.records == []


def test_an_unreachable_entry_is_skipped_before_the_first_attempt(
    tmp_path: Path,
) -> None:
    """A run-scoped probe verdict of unreachable skips the entry."""
    cache = {
        "first": Reachability(status="unreachable", checked="test", detail="down"),
    }

    selected = select_endpoint(
        _declared_job(tmp_path, ("first", "second")),
        capabilities={"fake": Capabilities(adapter="fake")},
        backend_factory=_fake_factory(),
        reachability_cache=cache,
    )

    assert selected.endpoint == "second"


def test_an_unknown_id_is_skipped_silently_and_named_only_by_the_floor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture,
) -> None:
    """Skip is silent; the readings never name a hidden id."""
    selection, readings = job_kit_select.select_endpoint_with_readings(
        _declared_job(tmp_path, ("ghost", "second")),
        capabilities={"fake": Capabilities(adapter="fake")},
        backend_factory=_fake_factory(missing=("ghost",)),
    )

    assert selection.endpoint == "second"
    assert "ghost" not in repr(readings)
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert caplog.records == []


def test_no_usable_entry_raises_the_typed_floor_itemising_every_declared_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The floor is llm-scripting-kit's NoUsableRoutingTarget, still a SelectionError."""
    _install_paces(monkeypatch, {}, out_of_quota=("spent",))

    class Unadvertised(FakeBackend):
        name = "unadvertised-backend"

    def factory(endpoint: str, **_: object) -> BackendSelection:
        if endpoint == "ghost":
            raise EndpointResolveError("unknown endpoint ghost")
        backend = Unadvertised() if endpoint == "unadvertised" else FakeBackend()
        return BackendSelection(endpoint, "fake", backend, "fake-model")

    with pytest.raises(NoUsableRoutingTarget) as excinfo:
        select_endpoint(
            _declared_job(tmp_path, ("ghost", "spent", "unadvertised")),
            capabilities={"fake": Capabilities(adapter="fake")},
            backend_factory=factory,
        )

    floor = excinfo.value
    assert isinstance(floor, job_kit_select.SelectionError)
    assert isinstance(floor, job_kit_select.NoCompatibleEndpointError)
    assert [d.id for d in floor.dispositions] == ["ghost", "spent", "unadvertised"]
    assert [d.disposition for d in floor.dispositions] == [
        "unresolved",
        "out-of-quota",
        "requirements-mismatch",
    ]
    assert "declared" in str(floor)


def test_selection_classifies_each_declared_id_once_per_selection(
    tmp_path: Path,
) -> None:
    """The chosen entry's backend is the one describe resolved, not a second resolve."""
    calls: list[str] = []

    select_endpoint(
        _declared_job(tmp_path, ("first", "second")),
        capabilities={"fake": Capabilities(adapter="fake")},
        backend_factory=_fake_factory(calls),
    )

    assert calls == ["first", "second"]


def test_import_raises_shared_lib_too_old_when_describe_is_missing() -> None:
    """A linked llm-scripting-kit without describe() is diagnosed as too old."""
    stub = types.ModuleType("llm_scripting_kit.declaration")
    stub.NoUsableRoutingTarget = lsk_declaration.NoUsableRoutingTarget
    stub.CALLER_PROCESS = lsk_declaration.CALLER_PROCESS
    # describe deliberately omitted: the frontier symbol.

    real_declaration = sys.modules["llm_scripting_kit.declaration"]
    real_select = sys.modules["job_kit.select"]
    sys.modules["llm_scripting_kit.declaration"] = stub
    try:
        with pytest.raises(ImportError) as excinfo:
            importlib.reload(job_kit_select)
    finally:
        sys.modules["llm_scripting_kit.declaration"] = real_declaration
        sys.modules["job_kit.select"] = real_select
        importlib.reload(job_kit_select)

    assert type(excinfo.value).__name__ == "SharedLibTooOldError"
    message = str(excinfo.value)
    assert "llm-scripting-kit >= 0.46.0" in message
    assert "'describe'" in message
    assert "claude plugin update llm-scripting-kit@plugins-kit" in message
