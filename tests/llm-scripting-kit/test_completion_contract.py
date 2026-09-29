"""The output contract: construction, identity, judgment, and the seam helpers.

Each guard here has a named behavior it protects, and each was shown to go red
with that behavior removed.
"""
from __future__ import annotations

import ast
import copy
import dataclasses
import inspect
import json
import pickle
import typing
from pathlib import Path

import pytest

import llm_scripting_kit.completion as completion_pkg
from llm_scripting_kit.completion import contract_types
from llm_scripting_kit.completion.capabilities import (
    Capabilities,
    StructuredOutputCapability,
)
from llm_scripting_kit.completion.contract import (
    POLICY_NATIVE_REQUIRED,
    POLICY_TEXT_ONLY,
    POLICY_VALIDATED_RESULT,
    SCHEMA_INSTRUCTION_PREFIX,
    ContractReport,
    DeliveryPlan,
    OutputContract,
    OutputContractUnsatisfiable,
    OutputContractViolation,
    canonical_schema_json,
    contract_requirements,
    evaluate_output,
    finalize_contract,
    merge_requirements,
    prepare_contract,
    render_schema_instruction,
)
from llm_scripting_kit.completion.halt import classify_halt_text
from llm_scripting_kit.completion.types import (
    COMPLETED,
    ERROR,
    BackendOptions,
    LLMResponse,
)

_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "score": {"type": "integer"}},
    "required": ["title"],
    "additionalProperties": False,
}


def _contract(policy=POLICY_VALIDATED_RESULT, schema=None, **kw):
    if policy == POLICY_TEXT_ONLY:
        return OutputContract("t.c", policy, **kw)
    return OutputContract("t.c", policy, _SCHEMA if schema is None else schema, **kw)


def _record(policies, delivery=None, emits=None, adapter="fake"):
    return Capabilities(
        adapter=adapter,
        structured_output=StructuredOutputCapability(
            policies=tuple(policies), contract_delivery=delivery, contract_emits=emits
        ),
    )


# -- the public surface ----------------------------------------------------

CONTRACT_EXPORTS = (
    "OutputContract",
    "OutputContractViolation",
    "contract_requirements",
    "evaluate_output",
    "POLICY_NATIVE_REQUIRED",
    "POLICY_VALIDATED_RESULT",
    "POLICY_TEXT_ONLY",
)

_INTERNAL_ONLY = (
    "ContractReport",
    "ContractOutcome",
    "DeliveryPlan",
    "prepare_contract",
    "finalize_contract",
    "merge_requirements",
    "OutputContractUnsatisfiable",
    "canonical_schema_json",
    "render_schema_instruction",
    "SCHEMA_INSTRUCTION_PREFIX",
)


def test_completion_exports_contract_surface_exactly():
    """The package exports exactly the consumer set content-pipeline-kit probes.

    The literal is pinned on BOTH sides of the plugin boundary; a dropped name
    breaks that consumer's probe, and an added one widens a surface nobody
    agreed to support.
    """
    exported = set(completion_pkg.__all__)
    for name in CONTRACT_EXPORTS:
        assert name in exported, name
        assert hasattr(completion_pkg, name), name
    for name in _INTERNAL_ONLY:
        assert name not in exported, name
        assert not hasattr(completion_pkg, name), name
    from llm_scripting_kit.completion import contract as contract_mod

    for name in CONTRACT_EXPORTS + _INTERNAL_ONLY:
        assert hasattr(contract_mod, name), name
    assert completion_pkg.OutputContract is contract_types.OutputContract


def test_get_type_hints_resolves_output_contract_fields():
    options_hints = typing.get_type_hints(BackendOptions)
    response_hints = typing.get_type_hints(LLMResponse)
    assert options_hints["output_contract"] == typing.Optional[OutputContract]
    assert response_hints["output_contract"] == typing.Optional[ContractReport]
    assert BackendOptions().output_contract is None
    last = dataclasses.fields(LLMResponse)[-1]
    assert last.name == "output_contract" and last.default is None


def test_contract_types_is_a_leaf_module():
    """contract_types imports the stdlib and json_schema only -- never .types,
    which imports IT at runtime, so the reverse edge would be a cycle."""
    source = Path(inspect.getsourcefile(contract_types)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    relative = set()
    absolute = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                relative.add(node.module or "")
            else:
                absolute.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                absolute.add(alias.name.split(".")[0])
    assert relative == {"json_schema"}, relative
    assert "llm_scripting_kit" not in absolute
    import sys

    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    assert absolute <= stdlib, absolute - stdlib


# -- construction ------------------------------------------------------------


def test_policy_must_be_known():
    with pytest.raises(ValueError, match="unknown output contract policy"):
        OutputContract("x", "strict", _SCHEMA)


@pytest.mark.parametrize("policy", [POLICY_NATIVE_REQUIRED, POLICY_VALIDATED_RESULT])
def test_schema_policies_require_a_schema(policy):
    with pytest.raises(ValueError, match="requires a schema"):
        OutputContract("x", policy)


def test_text_only_forbids_a_schema():
    with pytest.raises(ValueError, match="takes no schema"):
        OutputContract("x", POLICY_TEXT_ONLY, _SCHEMA)


def test_id_must_be_a_non_empty_string():
    with pytest.raises(ValueError):
        OutputContract("", POLICY_TEXT_ONLY)


@pytest.mark.parametrize("keyword", ["pattern", "oneOf", "allOf", "format", "uniqueItems"])
def test_unsupported_keyword_is_refused(keyword):
    schema = {"type": "object", "properties": {"a": {"type": "string", keyword: "x"}}}
    with pytest.raises(ValueError, match=keyword):
        OutputContract("x", POLICY_VALIDATED_RESULT, schema)


class _Opaque:
    pass


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "required": ("a",)},
        {"type": "object", "required": {"a"}},
        {"type": "object", "properties": {1: {"type": "string"}}},
        {"type": "object", "properties": {True: {"type": "string"}}},
        {"type": "number", "maximum": float("nan")},
        {"type": "object", "default": _Opaque()},
    ],
    ids=["tuple", "set", "int_key", "bool_key", "nan", "object"],
)
def test_non_json_native_schema_is_refused(schema):
    """json.dumps would silently turn the tuple into a list and the int key
    into a string; the walk must refuse them before any serialization."""
    with pytest.raises(ValueError):
        OutputContract("x", POLICY_VALIDATED_RESULT, schema)


@pytest.mark.parametrize(
    "schema",
    [
        {},
        {"title": "t", "description": "d"},
        {"type": ["object", "null"]},
        {"$ref": "#/$defs/a", "$defs": {"a": {"type": ["string", "null"]}}},
        {"anyOf": [{"type": "string"}, {"type": "null"}]},
        {"const": None},
        {"$ref": "#/$defs/a", "$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}}},
    ],
    ids=[
        "empty",
        "annotations_only",
        "type_list_with_null",
        "ref_to_nullable",
        "anyof_with_null_branch",
        "const_null",
        "ref_cycle",
    ],
)
def test_null_root_schema_is_refused(schema):
    with pytest.raises(ValueError, match=r"root schema may accept null|\$ref cycle at"):
        OutputContract("x", POLICY_VALIDATED_RESULT, schema)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object"},
        {"$ref": "#/$defs/a", "$defs": {"a": {"type": "object"}}},
        {"anyOf": [{"type": "string"}, {"type": "integer"}]},
        {"enum": ["a", "b"]},
    ],
    ids=["type_object", "local_ref_to_object", "anyof_all_non_null", "enum_without_null"],
)
def test_non_null_root_schema_is_accepted(schema):
    contract = OutputContract("x", POLICY_VALIDATED_RESULT, schema)
    assert contract.schema_json() == schema


def test_null_decision_refuses_a_ref_cycle_as_undecidable():
    """The decision's own cycle guard, reached directly: check_schema refuses
    a non-consuming cycle first, so a contract never gets here, but the
    decision must not recurse forever if it ever does."""
    schema = {"$ref": "#/$defs/a", "$defs": {"a": {"$ref": "#/$defs/a"}}}
    with pytest.raises(
        ValueError,
        match=r"cannot decide whether the root excludes null: \$ref cycle at #/\$defs/a",
    ):
        contract_types._excludes_null(schema, schema)


# -- freeze, digest, identity ------------------------------------------------


def test_mutating_caller_schema_does_not_change_contract():
    schema = json.loads(json.dumps(_SCHEMA))
    contract = OutputContract("x", POLICY_VALIDATED_RESULT, schema)
    digest = contract.schema_digest
    schema["properties"]["title"]["type"] = "integer"
    schema["required"].append("score")
    assert contract.schema_json() == _SCHEMA
    assert contract.schema_digest == digest
    assert evaluate_output(contract, '{"title": "ok"}').disposition == "valid"


def test_the_stored_schema_is_read_only():
    contract = _contract()
    with pytest.raises(TypeError):
        contract.schema["type"] = "array"  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        contract.policy = POLICY_TEXT_ONLY  # type: ignore[misc]


def test_digest_is_sha256_of_canonical_json_and_labels_default_to_it():
    import hashlib

    contract = _contract()
    canonical = json.dumps(_SCHEMA, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert canonical_schema_json(contract) == canonical
    assert contract.schema_digest == hashlib.sha256(canonical.encode("ascii")).hexdigest()
    assert contract.schema_version == contract.schema_digest[:12]


def test_text_only_digest_is_the_digest_of_null():
    import hashlib

    contract = OutputContract("x", POLICY_TEXT_ONLY)
    assert contract.schema_digest == hashlib.sha256(b"null").hexdigest()
    assert canonical_schema_json(contract) == "null"


def test_digest_ignores_key_order_but_not_content():
    reordered = dict(reversed(list(_SCHEMA.items())))
    assert _contract(schema=reordered).schema_digest == _contract().schema_digest
    other = dict(_SCHEMA, required=["title", "score"])
    assert _contract(schema=other).schema_digest != _contract().schema_digest


def test_same_version_label_different_schema_differs_in_identity():
    a = _contract(schema_version="v1")
    b = _contract(schema=dict(_SCHEMA, required=["score"]), schema_version="v1")
    assert a.schema_version == b.schema_version == "v1"
    assert a.identity() != b.identity()


def test_changed_version_label_changes_identity():
    a = _contract(schema_version="v1")
    b = _contract(schema_version="v2")
    assert a.schema_digest == b.schema_digest
    assert a.identity() != b.identity()


def test_identity_never_carries_the_schema_body():
    contract = _contract()
    assert contract.identity() == (
        "t.c",
        POLICY_VALIDATED_RESULT,
        contract.schema_digest,
        contract.schema_version,
    )


def test_to_json_is_a_fresh_copy_and_round_trips():
    contract = _contract(schema_version="v7")
    payload = contract.to_json()
    payload["schema"]["type"] = "array"
    assert contract.schema["type"] == "object"
    again = OutputContract.from_json(contract.to_json())
    assert again == contract and again.identity() == contract.identity()
    text_only = OutputContract("x", POLICY_TEXT_ONLY)
    assert OutputContract.from_json(text_only.to_json()) == text_only


def test_from_json_refuses_unknown_and_missing_keys():
    with pytest.raises(ValueError, match="unknown output contract key"):
        OutputContract.from_json({"id": "x", "policy": POLICY_TEXT_ONLY, "digest": "y"})
    with pytest.raises(ValueError, match="missing 'policy'"):
        OutputContract.from_json({"id": "x"})


def test_contract_is_hashable_copyable_and_picklable():
    contract = _contract()
    assert hash(contract) == hash(_contract())
    assert copy.deepcopy(contract) is contract
    assert pickle.loads(pickle.dumps(contract)) == contract
    options = BackendOptions(output_contract=contract)
    assert dataclasses.replace(options, effort="high").output_contract is contract


# -- evaluate_output ------------------------------------------------------


def test_valid_answer_returns_the_parsed_value():
    outcome = evaluate_output(_contract(), '{"title": "t", "score": 3}')
    assert outcome.disposition == "valid"
    assert outcome.value == {"title": "t", "score": 3}
    assert outcome.errors == ()


def test_schema_mismatch_is_invalid():
    outcome = evaluate_output(_contract(), '{"score": "high", "extra": 1}')
    assert outcome.disposition == "schema-mismatch"
    assert outcome.value is None
    assert outcome.errors == (
        ("/extra", "additionalProperties"),
        ("/score", "type"),
        ("/title", "required"),
    )


@pytest.mark.parametrize(
    "text",
    ["not json", '```json\n{"title": "t"}\n```', '{"title": NaN}',
     '{"title": "a", "title": "b"}', "", '{"title": "t"} trailing'],
    ids=["prose", "code-fence", "nan", "duplicate-key", "empty", "trailing"],
)
def test_non_json_is_unparseable(text):
    outcome = evaluate_output(_contract(), text)
    assert outcome.disposition == "unparseable"
    assert outcome.value is None


def test_a_json_null_answer_fails_the_non_null_root():
    assert evaluate_output(_contract(), "null").disposition == "schema-mismatch"


def test_text_only_is_judged_text_only():
    outcome = evaluate_output(OutputContract("x", POLICY_TEXT_ONLY), "anything")
    assert outcome.disposition == "text-only"


# -- requirements ------------------------------------------------------------


@pytest.mark.parametrize(
    "policy", [POLICY_NATIVE_REQUIRED, POLICY_VALIDATED_RESULT, POLICY_TEXT_ONLY]
)
def test_contract_requirements_name_the_policy(policy):
    assert contract_requirements(_contract(policy)) == {
        "structured_output": {"policies": [policy]}
    }


def test_no_contract_requires_nothing():
    assert contract_requirements(None) == {}


def test_merge_requirements_joins_and_refuses_conflicts():
    contract_req = contract_requirements(_contract())
    assert merge_requirements({"params": ["effort"]}, contract_req) == {
        "params": ["effort"],
        **contract_req,
    }
    assert merge_requirements(["effort"], None) == {"params": ["effort"]}
    assert merge_requirements(contract_req, contract_req) == contract_req
    with pytest.raises(ValueError, match="structured_output"):
        merge_requirements(
            {"structured": "native"}, contract_requirements(_contract())
        )
    with pytest.raises(ValueError, match="structured_output"):
        merge_requirements(
            contract_requirements(_contract(POLICY_NATIVE_REQUIRED)), contract_req
        )


# -- prepare_contract ----------------------------------------------------------


def test_prepare_without_a_contract_returns_none():
    assert prepare_contract(_record(()), BackendOptions()) is None
    assert prepare_contract(_record(()), None) is None


def test_prepare_refuses_unadvertised_policy():
    native_only = _record((POLICY_NATIVE_REQUIRED,), "native", "--output-schema")
    with pytest.raises(OutputContractUnsatisfiable, match="validated-result"):
        prepare_contract(native_only, BackendOptions(output_contract=_contract()))
    prompt_record = _record((POLICY_VALIDATED_RESULT,), "prompt", "stdin")
    with pytest.raises(OutputContractUnsatisfiable, match="text-only"):
        prepare_contract(
            prompt_record,
            BackendOptions(output_contract=OutputContract("x", POLICY_TEXT_ONLY)),
        )
    with pytest.raises(OutputContractUnsatisfiable, match="advertised policies: none"):
        prepare_contract(_record(()), BackendOptions(output_contract=_contract()))


def test_prepare_plans_each_delivery_from_the_record():
    contract = _contract()
    native = prepare_contract(
        _record((POLICY_VALIDATED_RESULT,), "native", "--output-schema"),
        BackendOptions(output_contract=contract),
    )
    assert native == DeliveryPlan(
        contract=contract,
        adapter="fake",
        delivery="native",
        schema_json=canonical_schema_json(contract),
    )
    prompt = prepare_contract(
        _record((POLICY_VALIDATED_RESULT,), "prompt", "stdin"),
        BackendOptions(output_contract=contract),
    )
    assert prompt.delivery == "prompt"
    assert prompt.instruction == render_schema_instruction(contract)
    text_only = OutputContract("x", POLICY_TEXT_ONLY)
    none = prepare_contract(
        _record((POLICY_TEXT_ONLY,)), BackendOptions(output_contract=text_only)
    )
    assert none.delivery == "none" and none.instruction is None


def test_prepare_refuses_native_required_over_prompt_delivery():
    record = _record((POLICY_NATIVE_REQUIRED,), "prompt", "stdin")
    with pytest.raises(OutputContractUnsatisfiable, match="by prompt"):
        prepare_contract(
            record, BackendOptions(output_contract=_contract(POLICY_NATIVE_REQUIRED))
        )


@pytest.mark.parametrize("key", ["output_schema", "response_format"])
def test_prepare_refuses_a_contract_beside_a_legacy_schema_key(key):
    record = _record((POLICY_VALIDATED_RESULT,), "prompt", "stdin")
    with pytest.raises(OutputContractUnsatisfiable, match=f"extras.{key}"):
        prepare_contract(
            record, BackendOptions(output_contract=_contract(), extras={key: "x"})
        )


def test_prepare_refuses_a_non_contract_value():
    with pytest.raises(TypeError):
        prepare_contract(_record(()), BackendOptions(output_contract={"id": "x"}))


def test_render_schema_instruction_is_exact_ascii():
    contract = _contract()
    rendered = render_schema_instruction(contract)
    assert rendered == "\n\n" + SCHEMA_INSTRUCTION_PREFIX + canonical_schema_json(contract)
    rendered.encode("ascii")
    with pytest.raises(ValueError):
        render_schema_instruction(OutputContract("x", POLICY_TEXT_ONLY))


# -- finalize_contract ---------------------------------------------------------


def _response(text, **kw):
    return LLMResponse(
        text=text,
        model="m",
        input_tokens=11,
        output_tokens=7,
        cache_hit_tokens=3,
        dropped_params=("temperature",),
        execution_controls_applied=("sandbox-mode",),
        **kw,
    )


def _prompt_plan(contract=None):
    return prepare_contract(
        _record((POLICY_VALIDATED_RESULT, POLICY_TEXT_ONLY), "prompt", "stdin"),
        BackendOptions(output_contract=contract or _contract()),
    )


def test_finalize_without_a_plan_is_the_identity():
    response = _response("x")
    assert finalize_contract(None, response) is response


def test_finalize_valid_sets_structured_and_the_report():
    contract = _contract()
    out = finalize_contract(_prompt_plan(contract), _response('{"title": "t"}'))
    assert out.status == COMPLETED
    assert out.structured == {"title": "t"}
    assert out.output_contract == ContractReport(
        contract_id="t.c",
        schema_version=contract.schema_version,
        schema_digest=contract.schema_digest,
        policy=POLICY_VALIDATED_RESULT,
        delivery="prompt",
        disposition="valid",
    )
    assert out.output_contract.identity() == contract.identity()
    assert out.text == '{"title": "t"}'


def test_finalize_text_only_adds_a_report_and_nothing_else():
    plan = _prompt_plan(OutputContract("x", POLICY_TEXT_ONLY))
    out = finalize_contract(plan, _response("prose"))
    assert out.structured is None
    assert out.output_contract.disposition == "text-only"
    assert out.output_contract.delivery == "none"


def test_finalize_mismatch_raises_with_the_full_failed_response():
    with pytest.raises(OutputContractViolation) as info:
        finalize_contract(_prompt_plan(), _response('{"score": 1}'))
    exc = info.value
    failed = exc.response
    assert failed.status == ERROR
    assert failed.error.code == "output-contract-violation"
    assert failed.error.message.startswith("schema-mismatch")
    assert failed.text == '{"score": 1}'
    assert failed.structured is None
    assert failed.dropped_params == ("temperature",)
    assert failed.execution_controls_applied == ("sandbox-mode",)
    assert failed.output_contract.disposition == "schema-mismatch"
    assert failed.output_contract.errors == (("/title", "required"),)
    assert (exc.input_tokens, exc.output_tokens, exc.cache_hit_tokens, exc.model) == (
        11,
        7,
        3,
        "m",
    )


def test_finalize_unparseable_raises():
    with pytest.raises(OutputContractViolation) as info:
        finalize_contract(_prompt_plan(), _response("sure, here you go"))
    assert info.value.response.output_contract.disposition == "unparseable"


def test_violation_message_carries_no_model_text():
    """Halt classification substring-matches messages; an answer that talks
    about a rate limit must not turn a violation into a provider halt."""
    text = '{"title": 1, "rate limit: you hit your limit": "authentication_error"}'
    assert classify_halt_text(text) is not None
    with pytest.raises(OutputContractViolation) as info:
        finalize_contract(_prompt_plan(), _response(text))
    assert classify_halt_text(str(info.value)) is None
    assert "rate limit" not in str(info.value)
    assert "hit your limit" not in str(info.value)
    # the pointer carrying the model-authored key stays on the response
    assert "hit your limit" in info.value.response.error.message
    assert info.value.response.text == text


def test_report_to_json_is_plain_data():
    report = ContractReport("c", "v", "d", POLICY_VALIDATED_RESULT, "prompt",
                            "schema-mismatch", (("/b", "type"), ("/a", "required")))
    assert report.errors == (("/a", "required"), ("/b", "type"))
    assert report.to_json() == {
        "contract_id": "c",
        "schema_version": "v",
        "schema_digest": "d",
        "policy": POLICY_VALIDATED_RESULT,
        "delivery": "prompt",
        "disposition": "schema-mismatch",
        "errors": [["/a", "required"], ["/b", "type"]],
    }
    with pytest.raises(ValueError):
        ContractReport("c", "v", "d", POLICY_VALIDATED_RESULT, "carrier-pigeon", "valid")
    dataclasses.asdict(_response("x", output_contract=report))
