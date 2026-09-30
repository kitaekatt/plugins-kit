"""Tests for the output-contract (structured generation) path in llm.platform.

Pins the U5 consumer design of Completion Contract v2: the structural step
runs FIRST inside the existing ``ValidationSpec`` / ``evaluate_submission`` /
``submit_validated`` seams and the content-pipeline semantic validators run
only on a structurally valid object; a structural failure is one HARD
``schema_violation`` rejection that feeds the unchanged retry loop; the cache
isolates every declared contract by identity and serves a hit only with a
matching, successful report; and the one lazy probe (``_contract_seam``)
diagnoses an absent and a stale llm-scripting-kit apart.

Backend behavior is scripted through ``MockBackend``; the no-report path uses
the REAL llm-scripting-kit ``evaluate_output``.
"""

from __future__ import annotations

import dataclasses
import sys
import types
from pathlib import Path

import pytest

from content_pipeline.llm import platform
from content_pipeline.llm.backends import MockBackend
from content_pipeline.llm.platform import (
    BackendOptions,
    CostBudget,
    LLMResponse,
    ResponseCache,
    StructuralOutputError,
    StructuredContractSupportError,
    ValidationSpec,
    build_cache_key,
    call_llm,
    evaluate_submission,
    submit_validated,
)
from content_pipeline.validate import contract as vcontract

SHARED_LIB = Path(__file__).resolve().parents[2] / "plugins" / "llm-scripting-kit" / "lib"

SCHEMA = {
    "type": "object",
    "required": ["title"],
    "properties": {"title": {"type": "string", "minLength": 1}},
    "additionalProperties": False,
}

PRICING = {"m": {"input": 1.0, "output": 2.0}}


def _lsk_names():
    return {n for n in sys.modules if n == "llm_scripting_kit" or n.startswith("llm_scripting_kit.")}


@pytest.fixture
def lsk(monkeypatch):
    """The real ``llm_scripting_kit.completion``, unloaded again afterwards."""
    before = _lsk_names()
    monkeypatch.syspath_prepend(str(SHARED_LIB))
    import llm_scripting_kit.completion as completion  # noqa: PLC0415

    yield completion
    for name in _lsk_names() - before:
        del sys.modules[name]


def _contract(lsk, policy=None, *, schema=SCHEMA, version=None, cid="cpk.summary"):
    policy = policy or lsk.POLICY_VALIDATED_RESULT
    if policy == lsk.POLICY_TEXT_ONLY:
        schema = None
    return lsk.OutputContract(id=cid, policy=policy, schema=schema, schema_version=version)


def _seam_report(output_contract, disposition="valid", errors=(), delivery="native"):
    contract_id, policy, digest, version = output_contract.identity()
    return {
        "contract_id": contract_id,
        "schema_version": version,
        "schema_digest": digest,
        "policy": policy,
        "delivery": delivery,
        "disposition": disposition,
        "errors": [list(e) for e in errors],
    }


class _Recorder:
    """A validator that records every candidate it is shown."""

    def __init__(self, reject_kind=None):
        self.seen = []
        self.reject_kind = reject_kind

    def __call__(self, candidate, context):
        self.seen.append(candidate)
        if self.reject_kind:
            return [vcontract.Rejection(kind=self.reject_kind, detail="domain says no")]
        return []


# -- structural step ahead of the semantic validators -----------------------


def test_schema_failure_is_schema_violation_rejection(lsk):
    output_contract = _contract(lsk)
    spec = ValidationSpec(validators=[_Recorder()], output_contract=output_contract)

    evaluation = evaluate_submission('{"title": 5}', spec)

    assert evaluation.parsed is False
    assert evaluation.payload is None
    [rejection] = evaluation.rejections
    assert rejection.kind == "schema_violation"
    assert rejection.severity is vcontract.Severity.HARD
    assert rejection.rule_id == "cpk.summary"
    assert rejection.payload == {
        "contract_id": "cpk.summary",
        "schema_version": output_contract.schema_version,
        "policy": "validated-result",
        "delivery": "unreported",
        "disposition": "schema-mismatch",
        "errors": [["/title", "type"]],
        "raw_output": '{"title": 5}',
    }
    assert "/title fails type" in rejection.detail
    assert evaluation.structural["disposition"] == "schema-mismatch"


def test_unparseable_output_is_a_schema_violation_not_a_parse_error(lsk):
    spec = ValidationSpec(output_contract=_contract(lsk))
    evaluation = evaluate_submission('```json\n{"title": "x"}\n```', spec)
    [rejection] = evaluation.rejections
    assert rejection.kind == "schema_violation"
    assert rejection.payload["disposition"] == "unparseable"
    assert rejection.payload["errors"] == []


def test_domain_validators_skip_on_structural_failure(lsk):
    recorder = _Recorder()
    spec = ValidationSpec(validators=[recorder], output_contract=_contract(lsk))

    evaluate_submission('{"title": 5}', spec)
    evaluate_submission("not json", spec)

    assert recorder.seen == []


def test_domain_validators_skip_on_a_failed_seam_report(lsk):
    output_contract = _contract(lsk)
    recorder = _Recorder()
    spec = ValidationSpec(validators=[recorder], output_contract=output_contract)
    response = LLMResponse(
        text='{"title": ""}',
        model="m",
        output_contract=_seam_report(
            output_contract, "schema-mismatch", [("/title", "minLength")]
        ),
    )

    evaluation = evaluate_submission(response.text, spec, response=response)

    assert recorder.seen == []
    [rejection] = evaluation.rejections
    assert rejection.kind == "schema_violation"
    assert rejection.payload["delivery"] == "native"
    assert rejection.payload["errors"] == [["/title", "minLength"]]


def test_semantic_failure_after_a_valid_structure(lsk):
    recorder = _Recorder(reject_kind="too_vague")
    spec = ValidationSpec(validators=[recorder], output_contract=_contract(lsk))

    evaluation = evaluate_submission('{"title": "x"}', spec)

    assert evaluation.parsed is True
    assert evaluation.payload == {"title": "x"}
    assert [r.kind for r in evaluation.rejections] == ["too_vague"]
    assert recorder.seen == [{"title": "x"}]
    assert evaluation.structural["disposition"] == "valid"


def test_valid_seam_report_makes_the_structured_object_the_payload(lsk):
    output_contract = _contract(lsk)
    structured = {"title": "from the seam"}
    response = LLMResponse(
        text='{"title": "from the seam"}',
        model="m",
        structured=structured,
        output_contract=_seam_report(output_contract),
    )
    recorder = _Recorder()
    spec = ValidationSpec(validators=[recorder], output_contract=output_contract)

    evaluation = evaluate_submission(response.text, spec, response=response)

    assert evaluation.payload is structured
    assert recorder.seen[0] is structured
    assert evaluation.structural["delivery"] == "native"


def test_a_report_for_another_contract_is_not_trusted(lsk):
    output_contract = _contract(lsk)
    other = _contract(lsk, cid="cpk.other")
    response = LLMResponse(
        text='{"title": 5}',
        model="m",
        structured={"title": 5},
        output_contract=_seam_report(other),
    )
    spec = ValidationSpec(output_contract=output_contract)

    evaluation = evaluate_submission(response.text, spec, response=response)

    assert [r.kind for r in evaluation.rejections] == ["schema_violation"]
    assert evaluation.structural["delivery"] == "unreported"


def test_native_required_without_a_seam_report_refuses(lsk):
    output_contract = _contract(lsk, lsk.POLICY_NATIVE_REQUIRED)
    spec = ValidationSpec(output_contract=output_contract)

    with pytest.raises(StructuredContractSupportError, match="native-required"):
        evaluate_submission('{"title": "x"}', spec)

    response = LLMResponse(
        text='{"title": "x"}',
        model="m",
        structured={"title": "x"},
        output_contract=_seam_report(output_contract),
    )
    assert evaluate_submission(response.text, spec, response=response).payload == {"title": "x"}


def test_text_only_contract_keeps_the_parse_fn_path(lsk):
    text_only = _contract(lsk, lsk.POLICY_TEXT_ONLY)
    validators = [_Recorder(reject_kind="k")]
    plain = evaluate_submission("abc", ValidationSpec(parse_fn=str.upper, validators=validators))
    declared = evaluate_submission(
        "abc",
        ValidationSpec(parse_fn=str.upper, validators=validators, output_contract=text_only),
    )

    assert (declared.parsed, declared.payload, declared.rejections) == (
        plain.parsed,
        plain.payload,
        plain.rejections,
    )
    assert plain.structural is None
    assert declared.structural == {
        "contract_id": "cpk.summary",
        "schema_version": text_only.schema_version,
        "schema_digest": text_only.schema_digest,
        "policy": "text-only",
        "delivery": "unreported",
        "disposition": "text-only",
        "errors": [],
    }


def test_validation_spec_refuses_parse_fn_with_a_schema_contract(lsk):
    with pytest.raises(ValueError, match="replaces parse_fn"):
        ValidationSpec(parse_fn=str, output_contract=_contract(lsk))
    with pytest.raises(ValueError, match="needs a parse_fn"):
        ValidationSpec(output_contract=_contract(lsk, lsk.POLICY_TEXT_ONLY))
    with pytest.raises(ValueError, match="needs a parse_fn"):
        ValidationSpec()
    with pytest.raises(TypeError, match="OutputContract"):
        ValidationSpec(output_contract={"id": "x"})


def test_payload_from_text(lsk):
    assert ValidationSpec(output_contract=_contract(lsk)).payload_from_text(
        '{"title": "x"}'
    ) == {"title": "x"}
    assert ValidationSpec(parse_fn=str.upper).payload_from_text("ab") == "AB"


# -- submit_validated -------------------------------------------------------


def test_retry_feedback_contains_the_schema_errors(lsk):
    backend = MockBackend(responses=['{"title": 5}', '{"title": "ok"}'])

    result = submit_validated(
        backend=backend,
        system="s",
        user="write a title",
        model="m",
        output_contract=_contract(lsk),
    )

    assert result.accepted
    assert result.payload == {"title": "ok"}
    retry_prompt = backend.calls[1]["user"]
    assert "[schema_violation:cpk.summary]" in retry_prompt
    assert "/title fails type" in retry_prompt


def test_contract_rides_the_options_to_the_backend(lsk):
    output_contract = _contract(lsk)
    backend = MockBackend(responses=['{"title": "ok"}'])
    submit_validated(
        backend=backend, system="s", user="u", model="m", output_contract=output_contract
    )
    assert backend.calls[0]["options"].output_contract is output_contract


def test_submit_validated_refuses_two_different_contracts(lsk):
    with pytest.raises(ValueError, match="two different output contracts"):
        submit_validated(
            backend=MockBackend(responses=["x"]),
            system="s",
            user="u",
            model="m",
            output_contract=_contract(lsk),
            options=BackendOptions(output_contract=_contract(lsk, cid="cpk.other")),
        )


def test_structural_output_error_becomes_the_attempts_feedback(lsk):
    output_contract = _contract(lsk)
    failed = LLMResponse(
        text="Sure! Here is the title.",
        model="m",
        status="error",
        output_contract=_seam_report(output_contract, "unparseable"),
    )
    good = LLMResponse(
        text='{"title": "ok"}',
        model="m",
        structured={"title": "ok"},
        output_contract=_seam_report(output_contract),
    )
    backend = MockBackend(responses=[StructuralOutputError(failed), good])

    result = submit_validated(
        backend=backend, system="s", user="u", model="m", output_contract=output_contract
    )

    assert result.accepted
    assert result.payload == {"title": "ok"}
    assert result.responses[0] is failed
    assert "[schema_violation:cpk.summary]" in backend.calls[1]["user"]


def test_structural_output_error_message_carries_no_model_text(lsk):
    output_contract = _contract(lsk)
    error = StructuralOutputError(
        LLMResponse(
            text="rate limit exceeded, insufficient credit",
            model="m",
            output_tokens=7,
            output_contract=_seam_report(output_contract, "unparseable"),
        )
    )
    assert "rate limit" not in str(error)
    assert platform.classify_halt_text(str(error)) is None
    assert (error.output_tokens, error.model) == (7, "m")


def test_no_report_path_stores_unreported_report_on_response(lsk):
    output_contract = _contract(lsk)
    result = submit_validated(
        backend=MockBackend(responses=['{"title": "ok"}']),
        system="s",
        user="u",
        model="m",
        output_contract=output_contract,
    )

    [response] = result.responses
    assert response.output_contract == {
        "contract_id": "cpk.summary",
        "schema_version": output_contract.schema_version,
        "schema_digest": output_contract.schema_digest,
        "policy": "validated-result",
        "delivery": "unreported",
        "disposition": "valid",
        "errors": [],
    }
    assert response.structured == {"title": "ok"}
    assert response.structured == result.payload


def test_no_contract_path_needs_no_shared_lib(monkeypatch):
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", None)
    result = submit_validated(
        backend=MockBackend(responses=["hello"]),
        system="s",
        user="u",
        model="m",
        parse_fn=str.upper,
        validators=[],
    )
    assert result.payload == "HELLO"
    assert result.responses[0].output_contract is None


# -- call_llm ---------------------------------------------------------------


def test_structural_error_not_retried_in_call_llm(lsk):
    output_contract = _contract(lsk)
    failed = LLMResponse(
        text="nope",
        model="m",
        input_tokens=10,
        output_tokens=100,
        output_contract=_seam_report(output_contract, "unparseable"),
    )
    backend = MockBackend(responses=[StructuralOutputError(failed), '{"title": "ok"}'])
    budget = CostBudget()

    with pytest.raises(StructuralOutputError):
        call_llm(
            backend,
            "s",
            "u",
            model="m",
            options=BackendOptions(output_contract=output_contract),
            retries=2,
            pricing=PRICING,
            cost_budget=budget,
        )

    assert len(backend.calls) == 1
    assert budget.spent > 0


def test_call_llm_does_not_cache_a_reportless_contract_response(lsk, tmp_path):
    response = call_llm(
        MockBackend(responses=['{"title": "ok"}']),
        "s",
        "u",
        model="m",
        options=BackendOptions(output_contract=_contract(lsk)),
        cache_dir=tmp_path,
    )
    assert response.text == '{"title": "ok"}'
    assert list(tmp_path.glob("*.json")) == []


# -- the response cache under a contract ------------------------------------


@pytest.mark.parametrize(
    "case", ["seam_report", "reportless_validated", "reportless_text_only"]
)
def test_contract_repeat_call_served_from_cache_with_zero_backend_calls(lsk, tmp_path, case):
    if case == "reportless_text_only":
        output_contract = _contract(lsk, lsk.POLICY_TEXT_ONLY)
        entry = "plain answer"
        kwargs = {"parse_fn": str.upper}
    else:
        output_contract = _contract(lsk)
        entry = '{"title": "ok"}'
        kwargs = {}
        if case == "seam_report":
            entry = LLMResponse(
                text=entry,
                model="m",
                structured={"title": "ok"},
                output_contract=_seam_report(output_contract),
            )
    backend = MockBackend(responses=[entry])

    def run():
        return submit_validated(
            backend=backend,
            system="s",
            user="u",
            model="m",
            output_contract=output_contract,
            cache_dir=tmp_path,
            **kwargs,
        )

    first = run()
    second = run()

    assert len(backend.calls) == 1
    assert first.accepted and second.accepted
    assert second.payload == first.payload
    [cached] = second.responses
    assert cached.from_cache is True
    assert tuple(
        cached.output_contract[k]
        for k in ("contract_id", "policy", "schema_digest", "schema_version")
    ) == output_contract.identity()


@pytest.mark.parametrize("shape", ["reportless", "failed_report", "raised"])
def test_structural_failure_is_never_cached(lsk, tmp_path, shape):
    output_contract = _contract(lsk)
    failed = LLMResponse(
        text='{"title": 5}',
        model="m",
        output_contract=_seam_report(output_contract, "schema-mismatch", [("/title", "type")]),
    )
    entry = {
        "reportless": '{"title": 5}',
        "failed_report": failed,
        "raised": StructuralOutputError(failed),
    }[shape]

    result = submit_validated(
        backend=MockBackend(responses=[entry]),
        system="s",
        user="u",
        model="m",
        output_contract=output_contract,
        cache_dir=tmp_path,
        max_attempts=1,
    )

    assert not result.accepted
    assert list(tmp_path.glob("*.json")) == []


def _mutate_report(output_contract, how):
    report = _seam_report(output_contract)
    if how == "digest":
        report["schema_digest"] = "0" * 64
    elif how == "label":
        report["schema_version"] = "some-other-label"
    elif how == "contract_id":
        report["contract_id"] = "cpk.other"
    elif how == "policy":
        report["policy"] = "native-required"
    elif how == "disposition":
        report["disposition"] = "schema-mismatch"
    elif how == "no_report":
        report = None
    return report


@pytest.mark.parametrize(
    "how", ["digest", "label", "contract_id", "policy", "disposition", "no_report"]
)
def test_contract_cache_hit_with_mismatched_metadata_is_a_miss(lsk, tmp_path, how):
    output_contract = _contract(lsk)
    options = BackendOptions(output_contract=output_contract)
    key = build_cache_key(backend="mock", model="m", system="s", user="u", options=options)
    ResponseCache(tmp_path).store(
        key,
        LLMResponse(
            text='{"title": "stale"}',
            model="m",
            structured={"title": "stale"},
            output_contract=_mutate_report(output_contract, how),
        ),
    )
    live = LLMResponse(
        text='{"title": "live"}',
        model="m",
        structured={"title": "live"},
        output_contract=_seam_report(output_contract),
    )
    backend = MockBackend(responses=[live])

    response = call_llm(backend, "s", "u", model="m", options=options, cache_dir=tmp_path)

    assert len(backend.calls) == 1
    assert response.from_cache is False
    assert response.structured == {"title": "live"}
    # The live call overwrote the unusable entry with an acceptable one.
    assert ResponseCache(tmp_path).lookup(key).structured == {"title": "live"}


def test_contract_cache_hit_with_matching_report_is_served(lsk, tmp_path):
    output_contract = _contract(lsk)
    options = BackendOptions(output_contract=output_contract)
    key = build_cache_key(backend="mock", model="m", system="s", user="u", options=options)
    ResponseCache(tmp_path).store(
        key,
        LLMResponse(
            text='{"title": "cached"}',
            model="m",
            structured={"title": "cached"},
            output_contract=_seam_report(output_contract),
        ),
    )
    backend = MockBackend(responses=[])

    response = call_llm(backend, "s", "u", model="m", options=options, cache_dir=tmp_path)

    assert backend.calls == []
    assert response.from_cache is True
    assert response.output_contract == _seam_report(output_contract)


# -- the probe --------------------------------------------------------------

# A literal, not platform._CONTRACT_SYMBOLS: deriving the cases from the tuple
# under test would drop a case together with the symbol it guards.
FRONTIER = (
    "OutputContract",
    "OutputContractViolation",
    "contract_requirements",
    "evaluate_output",
    "POLICY_NATIVE_REQUIRED",
    "POLICY_VALIDATED_RESULT",
    "POLICY_TEXT_ONLY",
)


def _install_fake_completion(monkeypatch, *, omit=None, with_field=True):
    package = types.ModuleType("llm_scripting_kit")
    completion = types.ModuleType("llm_scripting_kit.completion")
    for name in FRONTIER:
        if name != omit:
            setattr(completion, name, object())
    fields = [("max_tokens", int, dataclasses.field(default=1))]
    if with_field:
        fields.append(("output_contract", object, dataclasses.field(default=None)))
    completion.BackendOptions = dataclasses.make_dataclass("BackendOptions", fields)
    package.completion = completion
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", package)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.completion", completion)
    return completion


def test_contract_probe_returns_the_frontier(lsk):
    seam = platform._contract_seam()
    for name in FRONTIER:
        assert getattr(seam, name) is getattr(lsk, name)
    assert seam.BackendOptions is lsk.BackendOptions


def test_contract_probe_accepts_a_complete_fake(monkeypatch):
    completion = _install_fake_completion(monkeypatch)
    assert platform._contract_seam().evaluate_output is completion.evaluate_output


def test_contract_probe_absent_message_says_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", None)
    with pytest.raises(StructuredContractSupportError) as excinfo:
        platform._contract_seam()
    message = str(excinfo.value)
    assert isinstance(excinfo.value, ImportError)
    assert "claude plugin install llm-scripting-kit@plugins-kit" in message
    assert "update" not in message


@pytest.mark.parametrize("symbol", FRONTIER)
def test_contract_probe_stale_when_symbol_missing(monkeypatch, symbol):
    _install_fake_completion(monkeypatch, omit=symbol)
    with pytest.raises(StructuredContractSupportError) as excinfo:
        platform._contract_seam()
    assert symbol in str(excinfo.value)


def test_contract_probe_stale_when_options_field_missing(monkeypatch):
    _install_fake_completion(monkeypatch, with_field=False)
    with pytest.raises(StructuredContractSupportError) as excinfo:
        platform._contract_seam()
    assert "BackendOptions.output_contract" in str(excinfo.value)


def test_contract_probe_stale_message_says_update(monkeypatch):
    _install_fake_completion(monkeypatch, omit="evaluate_output")
    with pytest.raises(StructuredContractSupportError) as excinfo:
        platform._contract_seam()
    message = str(excinfo.value)
    assert "claude plugin update llm-scripting-kit@plugins-kit" in message
    assert ">= 0.56.0" in message
    assert "install" not in message


def test_contract_call_refuses_before_dispatch_without_the_shared_lib(monkeypatch):
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", None)
    backend = MockBackend(responses=["x"])
    with pytest.raises(StructuredContractSupportError):
        call_llm(backend, "s", "u", model="m", options=BackendOptions(output_contract=object()))
    assert backend.calls == []


# -- the lazy package re-export ---------------------------------------------


def test_output_contract_is_reexported_lazily_through_the_probe(lsk):
    import content_pipeline.llm as llm

    from content_pipeline.llm import OutputContract

    assert OutputContract is lsk.OutputContract
    assert "OutputContract" not in llm.__all__
    assert llm.StructuralOutputError is StructuralOutputError


def test_output_contract_reexport_diagnoses_an_absent_shared_lib(monkeypatch):
    import content_pipeline.llm as llm

    monkeypatch.setitem(sys.modules, "llm_scripting_kit", None)
    with pytest.raises(StructuredContractSupportError, match="claude plugin install"):
        llm.OutputContract  # noqa: B018
