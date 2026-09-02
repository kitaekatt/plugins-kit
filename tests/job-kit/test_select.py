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
        endpoint_preference=("first", "second"),
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
        endpoint_preference=("first", "second"),
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
        endpoint_preference=("unknown", "second"),
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


def test_import_raises_shared_lib_too_old_when_match_capabilities_missing() -> None:
    """An old llm-scripting-kit shared lib fails import with a clear message.

    Simulates a job-kit venv linked (by the bootstrap shared-lib linker,
    which pins no version) against an llm-scripting-kit older than 0.23.0,
    where llm_scripting_kit.completion has no match_capabilities. Re-imports
    job_kit.select against a stub module missing that symbol and restores
    the real module afterward so later tests are unaffected.
    """
    stub = types.ModuleType("llm_scripting_kit.completion")
    stub.BackendSelection = llm_scripting_kit_completion.BackendSelection
    stub.Capabilities = llm_scripting_kit_completion.Capabilities
    stub.adapter_capabilities = llm_scripting_kit_completion.adapter_capabilities
    stub.create_backend = llm_scripting_kit_completion.create_backend
    # match_capabilities deliberately omitted: the symbol added in 0.23.0.

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
    assert "0.23.0" in message
    assert "match_capabilities" in message
