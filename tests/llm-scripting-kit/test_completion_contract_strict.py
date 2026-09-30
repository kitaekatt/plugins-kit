"""OpenAI strict-mode compatibility: detection, selection, and the codex refusal.

Codex's ``--output-schema`` rejects, with a non-zero exit, any object schema
that lacks ``additionalProperties: false`` or does not list every property in
``required`` (observed live on codex-cli 0.161.0-alpha.2). These tests pin the
three places that fact is carried:

- ``strict_schema_violations`` / ``OutputContract.strict_compatible`` -- one
  pure traversal over every schema-bearing position, checking only those two
  rules;
- selection -- a non-strict schema contract positively requires
  ``contract_schema_class: json-schema-subset``, which codex (``openai-strict``)
  does not advertise;
- dispatch -- codex refuses a non-strict schema before any file is written or
  the runner called, naming the sorted offending pointers.

Text-only contracts carry no schema and are unaffected throughout.
"""
from __future__ import annotations

import pytest

from llm_scripting_kit.completion import codex_backend as codex_mod
from llm_scripting_kit.completion.adapter_capabilities import (
    ADAPTER_CAPABILITIES,
    CODEX_CAPABILITIES,
    OPENROUTER_CAPABILITIES,
)
from llm_scripting_kit.completion.capabilities import (
    Capabilities,
    StructuredOutputCapability,
)
from llm_scripting_kit.completion.codex_backend import CodexCliBackend
from llm_scripting_kit.completion.contract import (
    POLICY_NATIVE_REQUIRED,
    POLICY_TEXT_ONLY,
    POLICY_VALIDATED_RESULT,
    SCHEMA_CLASS_OPENAI_STRICT,
    SCHEMA_CLASS_SUBSET,
    SCHEMA_CLASSES,
    OutputContract,
    OutputContractUnsatisfiable,
    contract_requirements,
    prepare_contract,
    strict_schema_violations,
)
from llm_scripting_kit.completion.requirements import match_capabilities
from llm_scripting_kit.completion.types import BackendOptions


def _closed(properties=None, **extra):
    properties = properties or {}
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(properties),
        "additionalProperties": False,
        **extra,
    }


STRICT = _closed({"answer": {"type": "string", "minLength": 1}})
NON_STRICT = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


# -- the traversal -------------------------------------------------------------

_VIOLATION_CASES = {
    "strict_root": (STRICT, ()),
    "missing_additional_properties": (NON_STRICT, ("",)),
    "additional_properties_true": (
        {**_closed({"a": {"type": "string"}}), "additionalProperties": True},
        ("",),
    ),
    "optional_property": (
        {**_closed({"a": {"type": "string"}, "b": {"type": "string"}}), "required": ["a"]},
        ("",),
    ),
    "nested_property_object": (
        _closed({"inner": {"type": "object", "properties": {}}}),
        ("/properties/inner",),
    ),
    "items_object": (
        _closed({"list": {"type": "array", "items": {"type": "object", "properties": {"x": {"type": "string"}}}}}),
        ("/properties/list/items",),
    ),
    "anyof_branch": (
        {"anyOf": [_closed({"a": {"type": "string"}}), {"type": "object", "properties": {}}]},
        ("/anyOf/1",),
    ),
    "defs_target": (
        {
            **_closed({"node": {"$ref": "#/$defs/node"}}),
            "$defs": {"node": {"type": "object", "properties": {"v": {"type": "string"}}, "required": ["v"]}},
        },
        ("/$defs/node",),
    ),
    "additional_properties_schema": (
        {"type": "object", "properties": {}, "additionalProperties": {"type": "object"}},
        ("", "/additionalProperties"),
    ),
    "several_sorted": (
        {
            "type": "object",
            "properties": {"z": {"type": "object"}, "a": {"type": "object"}},
            "required": ["a", "z"],
        },
        ("", "/properties/a", "/properties/z"),
    ),
    "properties_without_type": (
        _closed({"p": {"properties": {"a": {"type": "string"}}}}),
        ("/properties/p",),
    ),
}


@pytest.mark.parametrize("name", sorted(_VIOLATION_CASES))
def test_strict_schema_violations_names_every_offending_object(name):
    schema, expected = _VIOLATION_CASES[name]
    contract = OutputContract("t.strict", POLICY_VALIDATED_RESULT, schema)
    assert strict_schema_violations(contract.schema) == expected
    assert contract.strict_violations == expected
    assert contract.strict_compatible is (expected == ())


def test_untested_keywords_are_not_restricted():
    """Only the two observed rules count; no restriction is inferred for any
    other keyword."""
    schema = _closed(
        {
            "n": {"type": "integer", "minimum": 0, "maximum": 9},
            "s": {"type": "string", "minLength": 1, "maxLength": 4},
            "e": {"enum": ["a", "b"]},
            "l": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        },
        title="t",
        description="d",
    )
    assert OutputContract("t.strict", POLICY_NATIVE_REQUIRED, schema).strict_compatible


def test_text_only_is_unaffected_by_the_schema_class():
    text = OutputContract("t.strict", POLICY_TEXT_ONLY)
    assert text.strict_violations == ()
    assert text.strict_compatible is True
    assert contract_requirements(text) == {"structured_output": {"policies": [POLICY_TEXT_ONLY]}}
    for cap in (CODEX_CAPABILITIES, OPENROUTER_CAPABILITIES):
        assert match_capabilities(cap, contract_requirements(text)) is True
        plan = prepare_contract(cap, BackendOptions(output_contract=text))
        assert plan.delivery == "none"


# -- selection -------------------------------------------------------------------


def test_every_contract_capable_record_declares_a_schema_class():
    for cap in ADAPTER_CAPABILITIES.values():
        structured = cap.structured_output
        if structured.policies:
            assert structured.contract_schema_class in SCHEMA_CLASSES, cap.adapter
            assert cap.to_json()["structured_output"]["contract_schema_class"] == (
                structured.contract_schema_class
            )
        else:
            assert structured.contract_schema_class is None, cap.adapter
    assert CODEX_CAPABILITIES.structured_output.contract_schema_class == SCHEMA_CLASS_OPENAI_STRICT
    assert OPENROUTER_CAPABILITIES.structured_output.contract_schema_class == SCHEMA_CLASS_SUBSET


@pytest.mark.parametrize("policy", [POLICY_NATIVE_REQUIRED, POLICY_VALIDATED_RESULT])
def test_contract_requirements_express_the_schema_class_positively(policy):
    strict = OutputContract("t.strict", policy, STRICT)
    loose = OutputContract("t.strict", policy, NON_STRICT)
    assert contract_requirements(strict) == {"structured_output": {"policies": [policy]}}
    assert contract_requirements(loose) == {
        "structured_output": {"policies": [policy], "contract_schema_class": SCHEMA_CLASS_SUBSET}
    }


def test_selection_skips_codex_for_a_non_strict_schema():
    strict = OutputContract("t.strict", POLICY_VALIDATED_RESULT, STRICT)
    loose = OutputContract("t.strict", POLICY_VALIDATED_RESULT, NON_STRICT)
    assert match_capabilities(CODEX_CAPABILITIES, contract_requirements(strict)) is True
    assert match_capabilities(CODEX_CAPABILITIES, contract_requirements(loose)) is False
    # The prompt adapter accepts the whole subset, strict or not.
    assert match_capabilities(OPENROUTER_CAPABILITIES, contract_requirements(strict)) is True
    assert match_capabilities(OPENROUTER_CAPABILITIES, contract_requirements(loose)) is True
    # A native-required non-strict contract has no eligible adapter at all.
    native_loose = OutputContract("t.strict", POLICY_NATIVE_REQUIRED, NON_STRICT)
    assert not any(
        match_capabilities(cap, contract_requirements(native_loose))
        for cap in ADAPTER_CAPABILITIES.values()
    )


def test_a_record_declaring_no_class_does_not_match_a_non_strict_schema():
    """The requirement is positive: silence is not acceptance."""
    unclassed = Capabilities(
        adapter="fake",
        structured_output=StructuredOutputCapability(
            policies=(POLICY_VALIDATED_RESULT,), contract_delivery="prompt", contract_emits="x"
        ),
    )
    loose = OutputContract("t.strict", POLICY_VALIDATED_RESULT, NON_STRICT)
    assert match_capabilities(unclassed, contract_requirements(loose)) is False


# -- dispatch --------------------------------------------------------------------


@pytest.mark.parametrize("policy", [POLICY_NATIVE_REQUIRED, POLICY_VALIDATED_RESULT])
def test_codex_refuses_a_non_strict_schema_before_any_file_or_runner(policy, tmp_path, monkeypatch):
    runner_calls = []
    temp_files = []
    real_mkstemp = codex_mod.tempfile.mkstemp

    def _recording_mkstemp(*args, **kwargs):
        handle, path = real_mkstemp(*args, **kwargs)
        temp_files.append(path)
        return handle, path

    def _writer(*args, **kwargs):
        raise AssertionError("the schema file must not be written")

    monkeypatch.setattr(codex_mod.tempfile, "mkstemp", _recording_mkstemp)
    monkeypatch.setattr(CodexCliBackend, "_write_schema_file", staticmethod(_writer))

    def runner(cmd, request, cwd, **kwargs):
        runner_calls.append(cmd)
        return "", "", 0

    schema = {
        "type": "object",
        "properties": {"b": {"type": "object"}, "a": {"type": "string"}},
        "required": ["a", "b"],
    }
    contract = OutputContract("t.strict", policy, schema)
    work = tmp_path / "work"
    work.mkdir()
    backend = CodexCliBackend(runner=runner, argv_prefix=("codex",))
    with pytest.raises(OutputContractUnsatisfiable) as info:
        backend.complete(
            "sys", "usr", model="m", options=BackendOptions(cwd=work, output_contract=contract)
        )
    message = str(info.value)
    assert SCHEMA_CLASS_OPENAI_STRICT in message
    # The offending pointers, sorted (root first).
    assert "at /, /properties/b " in message
    assert runner_calls == []
    assert temp_files == []
    assert list(work.iterdir()) == []


def test_openrouter_delivers_a_non_strict_schema():
    """The subset class really is broader: no refusal on the prompt adapter."""
    contract = OutputContract("t.strict", POLICY_VALIDATED_RESULT, NON_STRICT)
    plan = prepare_contract(OPENROUTER_CAPABILITIES, BackendOptions(output_contract=contract))
    assert plan.delivery == "prompt"
