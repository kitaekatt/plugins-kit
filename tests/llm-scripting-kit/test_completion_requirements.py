"""Tests for the capability-requirement matching language.

Ports the matcher-level cases job-kit's tests/job-kit/test_select.py (lines
~32-84) exercised indirectly through select_endpoint, plus direct coverage of
every convenience key and fallback path documented in
llm_scripting_kit.completion.requirements.match_capabilities.
"""
from __future__ import annotations

import pytest

from llm_scripting_kit.completion import (
    Capabilities,
    ExecutionControl,
    ParamCapability,
    StructuredOutputCapability,
    SystemPromptCapability,
    match_capabilities,
)
from llm_scripting_kit.completion.requirements import match_capabilities as direct_match


def test_match_capabilities_is_reexported_identically() -> None:
    """The package-level and module-level exports are the same function."""
    assert match_capabilities is direct_match


# -- match-all / shorthand ---------------------------------------------------


def test_none_requirements_match_everything() -> None:
    assert match_capabilities(Capabilities(adapter="fake"), None) is True


def test_empty_mapping_requirements_match_everything() -> None:
    assert match_capabilities(Capabilities(adapter="fake"), {}) is True


def test_list_requirements_are_params_shorthand() -> None:
    caps = Capabilities(adapter="fake", params={"effort": ParamCapability(type="string")})
    assert match_capabilities(caps, ["effort"]) is True
    assert match_capabilities(caps, ["missing"]) is False


def test_non_mapping_non_list_requirements_raise() -> None:
    with pytest.raises(ValueError):
        match_capabilities(Capabilities(adapter="fake"), 5)


# -- accepts Capabilities or an already-serialized mapping -------------------


def test_accepts_a_serialized_mapping_directly() -> None:
    caps = Capabilities(adapter="fake", params={"effort": ParamCapability(type="string")})
    serialized = caps.to_json()
    assert match_capabilities(serialized, {"params": ["effort"]}) is True


# -- params / required_params / honors ---------------------------------------


def test_params_list_requires_presence() -> None:
    caps = Capabilities(adapter="fake", params={"effort": ParamCapability(type="string")})
    assert match_capabilities(caps, {"params": ["effort"]}) is True
    assert match_capabilities(caps, {"params": ["missing"]}) is False


def test_required_params_and_honors_are_aliases_of_params() -> None:
    caps = Capabilities(adapter="fake", params={"effort": ParamCapability(type="string")})
    assert match_capabilities(caps, {"required_params": ["effort"]}) is True
    assert match_capabilities(caps, {"honors": ["effort"]}) is True


def test_params_mapping_matches_nested_requirement() -> None:
    caps = Capabilities(
        adapter="fake",
        params={"effort": ParamCapability(type="string", values=("low", "high"))},
    )
    assert match_capabilities(caps, {"params": {"effort": {"values": ["low"]}}}) is True
    assert (
        match_capabilities(caps, {"params": {"effort": {"values": ["absent"]}}}) is False
    )


def test_params_mapping_false_requires_absence() -> None:
    caps = Capabilities(adapter="fake", params={"effort": ParamCapability(type="string")})
    assert match_capabilities(caps, {"params": {"effort": False}}) is False
    assert match_capabilities(caps, {"params": {"missing": False}}) is True


def test_params_mapping_missing_required_param_fails() -> None:
    caps = Capabilities(adapter="fake")
    assert match_capabilities(caps, {"params": {"effort": True}}) is False


# -- execution_controls / controls -------------------------------------------


def test_execution_controls_require_named_ids() -> None:
    caps = Capabilities(
        adapter="fake",
        execution_controls=(
            ExecutionControl(id="sandbox", emits="-s", effect="confine"),
        ),
    )
    assert match_capabilities(caps, {"execution_controls": ["sandbox"]}) is True
    assert match_capabilities(caps, {"controls": ["sandbox"]}) is True
    assert match_capabilities(caps, {"execution_controls": ["missing"]}) is False


# -- dropped_params -----------------------------------------------------------


def test_dropped_params_require_named_membership() -> None:
    caps = Capabilities(adapter="fake", dropped_params=("temperature",))
    assert match_capabilities(caps, {"dropped_params": ["temperature"]}) is True
    assert match_capabilities(caps, {"dropped_params": ["max_tokens"]}) is False


# -- structured_output / structured -------------------------------------------


def test_structured_output_matches_by_mode_string() -> None:
    caps = Capabilities(
        adapter="fake", structured_output=StructuredOutputCapability(mode="native")
    )
    assert match_capabilities(caps, {"structured_output": "native"}) is True
    assert match_capabilities(caps, {"structured": "native"}) is True
    assert match_capabilities(caps, {"structured_output": "passthrough"}) is False


def test_structured_output_matches_by_result_string_when_not_a_mode() -> None:
    caps = Capabilities(
        adapter="fake",
        structured_output=StructuredOutputCapability(mode="native", result="parsed"),
    )
    assert match_capabilities(caps, {"structured_output": "parsed"}) is True
    assert match_capabilities(caps, {"structured_output": "text"}) is False


def test_structured_output_matches_by_mapping() -> None:
    caps = Capabilities(
        adapter="fake",
        structured_output=StructuredOutputCapability(mode="native", result="parsed"),
    )
    assert (
        match_capabilities(
            caps, {"structured_output": {"mode": "native", "result": "parsed"}}
        )
        is True
    )
    assert match_capabilities(caps, {"structured_output": {"mode": "none"}}) is False


def test_structured_output_absent_fails() -> None:
    # structured_output always serializes to a mapping, but a caller-provided
    # advertisement mapping missing the key must fail closed.
    assert match_capabilities({"adapter": "fake"}, {"structured_output": "native"}) is False


# -- system_prompt / system_prompt_mode ---------------------------------------


def test_system_prompt_matches_by_mode_string() -> None:
    caps = Capabilities(
        adapter="fake", system_prompt=SystemPromptCapability(mode="native-role")
    )
    assert match_capabilities(caps, {"system_prompt": "native-role"}) is True
    assert match_capabilities(caps, {"system_prompt_mode": "native-role"}) is True
    assert match_capabilities(caps, {"system_prompt": "prompt-fold"}) is False


def test_system_prompt_matches_by_mapping() -> None:
    caps = Capabilities(
        adapter="fake", system_prompt=SystemPromptCapability(mode="native-role")
    )
    assert match_capabilities(caps, {"system_prompt": {"mode": "native-role"}}) is True
    assert match_capabilities(caps, {"system_prompt": {"mode": "append"}}) is False


# -- dotted-path fallback ------------------------------------------------------


def test_dotted_path_falls_back_over_to_json() -> None:
    caps = Capabilities(adapter="my-adapter")
    assert match_capabilities(caps, {"adapter": "my-adapter"}) is True
    assert match_capabilities(caps, {"adapter": "other"}) is False


def test_dotted_path_reads_nested_fields() -> None:
    caps = Capabilities(
        adapter="fake", structured_output=StructuredOutputCapability(mode="native")
    )
    assert match_capabilities(caps, {"structured_output.mode": "native"}) is True
    assert match_capabilities(caps, {"structured_output.mode": "none"}) is False


def test_dotted_path_missing_key_fails() -> None:
    caps = Capabilities(adapter="fake")
    assert match_capabilities(caps, {"nonexistent.path": "x"}) is False


# -- boolean / sequence expected-value semantics via _matches -----------------


def test_boolean_true_expected_matches_truthy_actual() -> None:
    caps = Capabilities(adapter="fake", dropped_params=("temperature",))
    # dropped_params is a non-empty tuple -> truthy
    assert match_capabilities(caps, {"dropped_params": True}) is True


def test_boolean_false_expected_matches_falsy_actual() -> None:
    caps = Capabilities(adapter="fake")
    assert match_capabilities(caps, {"dropped_params": False}) is True


# -- ported select-level scenarios (job-kit test_select.py ~32-84) -----------


def test_ported_scenario_endpoint_lacking_param_is_rejected() -> None:
    """Mirrors test_selection_uses_order_and_stubbed_advertisement's advertisement."""
    without_effort = Capabilities(adapter="without-effort")
    fake = Capabilities(adapter="fake", params={"effort": ParamCapability(type="string")})
    requirements = {"params": ["effort"]}

    assert match_capabilities(without_effort, requirements) is False
    assert match_capabilities(fake, requirements) is True


def test_ported_scenario_bare_capabilities_matches_no_requirements() -> None:
    """Mirrors the halted/unknown-endpoint tests' plain Capabilities(adapter="fake")."""
    caps = Capabilities(adapter="fake")
    assert match_capabilities(caps, None) is True
