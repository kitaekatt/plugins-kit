"""Per-endpoint profiles and the capability records they specialize."""
from __future__ import annotations

from dataclasses import replace

import pytest

from llm_scripting_kit.completion.adapter_capabilities import (
    CLAUDE_CAPABILITIES,
    OPENROUTER_CAPABILITIES,
)
from llm_scripting_kit.completion.capabilities import ParamCapability
from llm_scripting_kit.completion.endpoint_profile import (
    EndpointProfile,
    endpoint_capabilities,
    profile_from_entry,
    profile_from_resolved,
    resolve_endpoint_profile,
)
from llm_scripting_kit.effort import EffortDelivery
from llm_scripting_kit.model_endpoints import EndpointEntry, RoutingConfig

_REGISTRY = (
    "models:\n"
    "  gpu:\n    base_url: http://gpu.invalid/v1\n    model: gpu-m\n"
    "    reasoning_effort: high\n"
    "    routing: {group: q, effort_style: ninfer}\n"
    "  plain:\n    base_url: http://plain.invalid/v1\n    model: m\n"
    "  h:\n    harness: codex\n    model: m\n"
)


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / "reg.yaml"
    path.write_text(_REGISTRY, encoding="utf-8")
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(path))
    return path


def test_profile_carries_the_declared_effort_and_the_delivered_value(registry):
    profile = resolve_endpoint_profile("gpu")
    assert profile.endpoint == "gpu"
    assert profile.declared_effort == "high"
    assert (profile.effort.style, profile.effort.source) == ("ninfer", "routing")
    assert profile.delivered_effort == "xhigh"


def test_undeliverable_profile_delivers_nothing(registry):
    profile = resolve_endpoint_profile("plain")
    assert profile.declared_effort is None
    assert profile.effort.deliverable is False
    assert profile.delivered_effort is None


@pytest.mark.parametrize("name", ["no-such-entry", "h"])
def test_resolve_endpoint_profile_never_raises(registry, name):
    profile = resolve_endpoint_profile(name)
    assert profile.endpoint == name
    assert profile.effort.source == "none"
    assert profile.declared_effort is None


def test_resolve_endpoint_profile_survives_a_broken_registry(tmp_path, monkeypatch):
    path = tmp_path / "broken.yaml"
    path.write_text("models: [not, a, map]\n", encoding="utf-8")
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(path))
    assert resolve_endpoint_profile("gpu").effort.deliverable is False


def test_profile_from_entry_matches_the_resolved_profile(registry):
    entry = EndpointEntry(
        id="gpu", base_url="http://gpu.invalid/v1", model="gpu-m",
        reasoning_effort="high", routing=RoutingConfig(group="q", effort_style="ninfer",
                                                       effort_style_declared=True),
    )
    assert profile_from_entry(entry) == resolve_endpoint_profile("gpu")


def test_profile_from_resolved_reads_the_additive_keys():
    profile = profile_from_resolved(
        {"name": "x", "request_defaults": {"reasoning_effort": "low"},
         "effort_style": "top-level", "effort_style_source": "frontdoor"}
    )
    assert profile == EndpointProfile("x", EffortDelivery("top-level", "frontdoor"), "low")
    bare = profile_from_resolved({"name": "y"})
    assert bare == EndpointProfile("y", EffortDelivery(None, "none"), None)


def test_endpoint_capabilities_deliverable_moves_effort_into_params():
    profile = EndpointProfile("gpu", EffortDelivery("ninfer", "routing"), "medium")
    record = endpoint_capabilities(OPENROUTER_CAPABILITIES, profile)
    assert record.params["effort"].emits == "reasoning_effort"
    assert "xhigh" in record.params["effort"].note
    assert "effort" not in record.dropped_params
    assert record.endpoint == "gpu"
    # the family record itself is untouched
    assert "effort" in OPENROUTER_CAPABILITIES.dropped_params
    assert "effort" not in OPENROUTER_CAPABILITIES.params


@pytest.mark.parametrize(
    "profile",
    [None, EndpointProfile("p", EffortDelivery(None, "none")),
     EndpointProfile("p", EffortDelivery("unsupported", "endpoint"))],
    ids=["no-profile", "no-style", "unsupported"],
)
def test_endpoint_capabilities_not_deliverable_returns_the_record(profile):
    assert endpoint_capabilities(OPENROUTER_CAPABILITIES, profile) is OPENROUTER_CAPABILITIES


def test_endpoint_capabilities_ignores_a_record_without_conditional_effort():
    profile = EndpointProfile("p", EffortDelivery("top-level", "endpoint"))
    assert endpoint_capabilities(CLAUDE_CAPABILITIES, profile) is CLAUDE_CAPABILITIES


def test_specialization_removes_only_effort_from_conditional_params():
    other = ParamCapability(type="string", emits="x")
    record = replace(
        OPENROUTER_CAPABILITIES,
        conditional_params={**OPENROUTER_CAPABILITIES.conditional_params, "other": other},
    )
    profile = EndpointProfile("p", EffortDelivery("top-level", "endpoint"))
    specialized = endpoint_capabilities(record, profile)
    assert dict(specialized.conditional_params) == {"other": other}


def test_conditional_params_default_is_not_shared():
    from llm_scripting_kit.completion.capabilities import Capabilities

    a, b = Capabilities(adapter="a"), Capabilities(adapter="b")
    assert a.conditional_params == {} and a.conditional_params is not b.conditional_params
