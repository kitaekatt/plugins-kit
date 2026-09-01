"""Tests for advertisement-filtered deterministic endpoint choice."""

from __future__ import annotations

from pathlib import Path

from llm_scripting_kit.completion import (
    BackendSelection,
    Capabilities,
    ParamCapability,
    match_capabilities,
)
from llm_scripting_kit.models import EndpointResolveError

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
