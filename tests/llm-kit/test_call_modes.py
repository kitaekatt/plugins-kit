"""Call modes: which adapters honor ``completion`` and ``workspace``.

``Capabilities.call_modes`` is an adapter-owned fact, shown by ``models endpoints``
and ``models catalog`` and enforced by ``require_call_mode``, which refuses before
anything is spawned. The library names two modes only; an image or a build is a
caller's meaning for what a workspace call writes, never a mode.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from llm_kit.adapters.capabilities import adapter_capabilities
from llm_kit.adapters.endpoint_profile import endpoint_capabilities
from llm_kit.completion import capabilities as caps
from llm_kit.registry.effort import EffortDelivery

ADAPTERS = ("claude-cli", "codex-cli", "opencode-cli", "openrouter")


def test_the_modes_are_completion_and_workspace_only():
    assert caps.CALL_MODES == ("completion", "workspace")
    for banned in ("image", "build"):
        assert banned not in caps.CALL_MODES


def test_every_adapter_honors_completion_and_only_codex_honors_workspace():
    table = adapter_capabilities()
    assert sorted(table) == list(ADAPTERS)
    assert {name: record.call_modes for name, record in table.items()} == {
        "claude-cli": ("completion",),
        "codex-cli": ("completion", "workspace"),
        "opencode-cli": ("completion",),
        "openrouter": ("completion",),
    }


def test_call_modes_are_serialized_in_every_record():
    for name, record in adapter_capabilities().items():
        assert record.to_json()["call_modes"] == list(record.call_modes), name


def test_an_endpoint_specialization_keeps_the_family_modes():
    record = adapter_capabilities()["openrouter"]
    deliver = type("P", (), {"effort": EffortDelivery("top-level", "endpoint"), "endpoint": "gpu"})()
    specialized = endpoint_capabilities(record, deliver)
    assert specialized is not record and specialized.call_modes == record.call_modes


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_completion_is_accepted_everywhere(adapter):
    assert caps.require_call_mode(adapter_capabilities()[adapter], "completion") == "completion"


def test_workspace_is_accepted_by_codex():
    assert caps.require_call_mode(adapter_capabilities()["codex-cli"], "workspace") == "workspace"


@pytest.mark.parametrize("adapter", ["openrouter", "claude-cli", "opencode-cli"])
def test_workspace_is_refused_by_every_other_adapter_before_any_spawn(adapter):
    with pytest.raises(caps.CallModeError) as caught:
        caps.require_call_mode(adapter_capabilities()[adapter], "workspace")
    message = str(caught.value)
    assert adapter in message and "workspace" in message and "supports: completion" in message
    assert "nothing was dispatched" in message
    assert isinstance(caught.value, ValueError)


@pytest.mark.parametrize("mode", ["image", "build", "", "Workspace", " completion", None, 5, ["completion"]])
@pytest.mark.parametrize("adapter", ADAPTERS)
def test_an_unknown_mode_is_refused_by_every_adapter(adapter, mode):
    with pytest.raises(caps.CallModeError, match="unknown call mode.*no image or build mode"):
        caps.require_call_mode(adapter_capabilities()[adapter], mode)


def test_a_record_that_lists_no_workspace_refuses_it_even_if_it_is_codex_shaped():
    record = replace(adapter_capabilities()["codex-cli"], call_modes=("completion",))
    with pytest.raises(caps.CallModeError):
        caps.require_call_mode(record, "workspace")
