"""Differential parity: llm_kit.execution vs the two legacy execution products.

Legacy sources driven here:

- job_kit.events (_execution_event, build_event) and
  content_pipeline.execution.events (_execution_event, _execution_event_v2,
  project_run): the execution-event envelope probe and the make_event call.
- job_kit.interrupts and content_pipeline.execution.interrupts: the
  contract probe, both _schema_validator loaders, and the adapter functions
  (check_request, validate_input, decision_outcome, same_resolution, expiry,
  lapse, bound_reason, canonical_json, resolution_document, the request
  parsers).
- job_kit.model and content_pipeline.execution.model: InterruptResolution and
  the lapse rule.

The shared probes decide WHAT is wrong; each product words it. The specs in
this file reuse the legacy error class and reproduce the legacy wording, so an
equal (error class, message) proves the shared classification
matches the legacy probe for the same environment. Environments are the real
modules, plus fake modules installed in ``sys.modules`` for every way a probe
can refuse.

Documented divergences (shared behaviour is the stricter, pipeline-execution
one; each is a pathological stub, and each is asserted below in
``TestDocumentedDivergences`` so it cannot change silently):

- a non-container ``SUPPORTED_SCHEMAS`` is "unsupported" (job_kit leaked a
  ``TypeError``);
- a validator whose ``SUPPORTED_SUBSETS`` is a string containing the subset
  literal is refused (job_kit accepted any ``in``-able value).
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_kit.execution import contract_probe, event_probe, probe, schema_validator, values
from llm_kit.execution import interrupts as shared_interrupts
from llm_kit.execution.contract_probe import ContractProbeSpec
from llm_kit.execution.event_probe import EventProbeSpec
from llm_kit.execution.interrupts import InterruptAdapter, InterruptAdapterSpec
from llm_kit.execution.probe import CallShape
from llm_kit.execution.schema_validator import ValidatorProbeSpec

_REPO = Path(__file__).resolve().parents[2]
_LEGACY_PATHS = [
    _REPO / "plugins" / "content-pipeline-kit" / "lib",
    _REPO / "plugins" / "job-kit" / "lib",
    _REPO / "plugins" / "llm-scripting-kit" / "lib",
]
_LEGACY_ROOTS = ("job_kit", "content_pipeline", "llm_scripting_kit")
_LSK_VALIDATOR = "llm_scripting_kit.completion.json_schema"


@pytest.fixture(scope="module")
def legacy():
    """Import the legacy modules; keep them importable until the module ends.

    The legacy probes import lazily, at call time, so the sys.path entries
    stay for the whole module and every module this fixture added is removed
    afterwards.
    """
    before = set(sys.modules)
    for entry in map(str, _LEGACY_PATHS):
        sys.path.insert(0, entry)
    try:
        ns = SimpleNamespace(
            job_events=importlib.import_module("job_kit.events"),
            job_interrupts=importlib.import_module("job_kit.interrupts"),
            job_model=importlib.import_module("job_kit.model"),
            cpk_events=importlib.import_module("content_pipeline.execution.events"),
            cpk_interrupts=importlib.import_module("content_pipeline.execution.interrupts"),
            cpk_model=importlib.import_module("content_pipeline.execution.model"),
            lsk_schema=importlib.import_module(_LSK_VALIDATOR),
            contract=importlib.import_module("bootstrap_lib.interrupt_contract"),
            event_module=importlib.import_module("bootstrap_lib.execution_event"),
        )
        yield ns
    finally:
        for entry in map(str, _LEGACY_PATHS):
            if entry in sys.path:
                sys.path.remove(entry)
        for name in list(sys.modules):
            if name not in before and name.split(".")[0] in _LEGACY_ROOTS:
                del sys.modules[name]


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


def outcome(fn):
    """Normalise a call: its value, or the exception class, text and errors.

    The exception cause is not compared: job_kit chains a bind failure and
    pipeline execution does not; the shared probe always chains
    (``TestProbeCauses``).
    """
    try:
        value = fn()
    except Exception as exc:  # noqa: BLE001 - the point is to compare everything
        return ("raise", type(exc), str(exc), getattr(exc, "errors", None))
    if isinstance(value, types.ModuleType):
        return ("module", id(value))
    return ("ok", value)


# ---------------------------------------------------------------------------
# Legacy wording, reproduced per product (the products own this in the real
# jobs / pipeline.execution packages)
# ---------------------------------------------------------------------------

_BOOTSTRAP_OWNER = "bootstrap@plugins-kit"


def job_event_render(failure):
    if failure.kind in (probe.ABSENT, probe.BROKEN_IMPORT):
        return (
            "job-kit records execution events with bootstrap_lib, which is not "
            "linked into this environment: install or enable the bootstrap "
            "plugin (`claude plugin install bootstrap@plugins-kit`) and start a "
            "new session so it links job-kit's shared libs."
        )
    if failure.kind == probe.UNSUPPORTED and failure.subject == event_probe.SCHEMA_V2:
        return (
            "the linked bootstrap_lib.execution_event supports "
            f"{event_probe.SCHEMA_V1} but not /v2, which job-kit needs to record "
            "interrupts: update the bootstrap plugin to >= "
            f"{failure.floor} "
            "(`claude plugin update bootstrap@plugins-kit`) and restart."
        )
    return (
        "job-kit records execution events with bootstrap_lib.execution_event "
        f"({event_probe.SCHEMA_V1} and {event_probe.SCHEMA_V2}), which the linked "
        "bootstrap_lib predates or lacks: update the bootstrap plugin to >= "
        f"{failure.floor} "
        "(`claude plugin update bootstrap@plugins-kit`) and restart."
    )


def cpk_event_render(failure):
    def too_old(reason):
        return (
            f"content-pipeline-kit execution events need bootstrap "
            f"{failure.floor} or newer: {reason}. Run "
            f"`claude plugin update {_BOOTSTRAP_OWNER}` and restart the session."
        )

    if failure.kind == probe.ABSENT:
        return (
            "content-pipeline-kit execution events need bootstrap_lib, which "
            f"this interpreter cannot import. Run `claude plugin install {_BOOTSTRAP_OWNER}`."
        )
    if failure.kind == probe.BROKEN_IMPORT:
        return too_old(f"bootstrap_lib failed to import ({failure.detail})")
    if failure.stage == probe.STAGE_V2:
        reason = (
            f"the installed module does not support {failure.subject}"
            if failure.kind == probe.UNSUPPORTED
            else "the installed make_event does not accept a schema keyword"
        )
        return (
            f"this run has interrupt rows, which content-pipeline-kit projects as "
            f"interrupt events under {event_probe.SCHEMA_V2}; that needs bootstrap "
            f"{failure.floor} or newer: {reason}. Run "
            f"`claude plugin update {_BOOTSTRAP_OWNER}` and restart the session."
        )
    if failure.kind == probe.MODULE_UNIMPORTABLE:
        return too_old(f"{failure.subject} does not import")
    if failure.kind == probe.UNSUPPORTED:
        return too_old(f"the installed module does not support {failure.subject}")
    if failure.kind == probe.MISSING_NAME:
        return too_old(f"the installed module has no {failure.subject}")
    return too_old("the installed make_event does not accept this call shape")


def job_contract_render(failure):
    if failure.kind in (probe.ABSENT, probe.BROKEN_IMPORT):
        return (
            "job-kit checks interrupt requests and resolutions with "
            "bootstrap_lib.interrupt_contract, which is not linked into this "
            "environment: install or enable the bootstrap plugin "
            "(`claude plugin install bootstrap@plugins-kit`) and start a new "
            "session so it links job-kit's shared libs."
        )
    return (
        "job-kit checks interrupt requests and resolutions with "
        f"bootstrap_lib.interrupt_contract ({contract_probe.CONTRACT_V1}), which the linked "
        "bootstrap_lib predates or lacks: update the bootstrap plugin to >= "
        f"{failure.floor} "
        "(`claude plugin update bootstrap@plugins-kit`) and restart."
    )


def job_validator_render(failure):
    if failure.kind in (probe.ABSENT, probe.BROKEN_IMPORT):
        return (
            "job-kit validates interrupt requests with llm-scripting-kit, which "
            "is not linked into this environment: install or enable it "
            "(`claude plugin install llm-scripting-kit@plugins-kit`) and start a "
            "new session so bootstrap links job-kit's shared libs."
        )
    return (
        "job-kit validates interrupt requests with "
        "llm_scripting_kit.completion.json_schema (check_schema and validate "
        "with the subset= selector), which the linked llm-scripting-kit "
        "predates or lacks: update "
        f"llm-scripting-kit to >= {failure.floor} "
        "(`claude plugin update llm-scripting-kit@plugins-kit`) and restart."
    )


def cpk_contract_render(failure):
    def too_old(reason):
        return (
            f"content-pipeline-kit interrupts need bootstrap "
            f"{failure.floor} or newer: {reason}. Run "
            f"`claude plugin update {_BOOTSTRAP_OWNER}` and restart the session."
        )

    if failure.kind == probe.ABSENT:
        return (
            "content-pipeline-kit interrupts need bootstrap_lib, which this "
            f"interpreter cannot import. Run `claude plugin install {_BOOTSTRAP_OWNER}`."
        )
    if failure.kind == probe.BROKEN_IMPORT:
        return too_old(f"bootstrap_lib failed to import ({failure.detail})")
    if failure.kind == probe.MODULE_UNIMPORTABLE:
        return too_old(f"{failure.subject} does not import")
    if failure.kind == probe.UNSUPPORTED:
        return too_old(f"the installed module does not support {failure.subject}")
    if failure.kind == probe.MISSING_NAME:
        return too_old(f"the installed module has no {failure.subject}")
    return too_old(f"the installed {failure.subject} does not accept this call shape")


def cpk_validator_render(failure):
    def too_old(reason):
        return (
            f"content-pipeline-kit interrupts need llm-scripting-kit "
            f"{failure.floor} or newer: {reason}. Run "
            f"`claude plugin update llm-scripting-kit@plugins-kit` and restart the session."
        )

    if failure.kind == probe.ABSENT:
        return (
            "content-pipeline-kit interrupts validate requests and answers "
            "with llm_scripting_kit, which this interpreter cannot import. "
            "Run `claude plugin install llm-scripting-kit@plugins-kit`."
        )
    if failure.kind == probe.BROKEN_IMPORT:
        return too_old(f"llm_scripting_kit failed to import ({failure.detail})")
    if failure.kind == probe.MODULE_UNIMPORTABLE:
        return too_old(f"{failure.subject} does not import")
    if failure.kind == probe.UNSUPPORTED:
        return too_old(f"the installed validator does not advertise {failure.subject}")
    if failure.kind == probe.MISSING_NAME:
        return too_old(f"the installed validator has no {failure.subject}")
    return too_old(f"the installed {failure.subject} does not accept this call shape")


# ---------------------------------------------------------------------------
# Specs, built from the legacy modules so the error classes and literals are
# the legacy ones
# ---------------------------------------------------------------------------

_EVENT_CALLABLES_JOB = ("make_event", "utc_timestamp", "usage_payload", "validate_stream", "JsonlSink")
_EVENT_CALLABLES_CPK = ("make_event", "utc_timestamp", "usage_payload", "validate_stream")


def job_event_spec(legacy) -> EventProbeSpec:
    return EventProbeSpec(
        plugin=legacy.job_events.PLUGIN,
        error=legacy.job_events.ExecutionEventSupportError,
        render=job_event_render,
        floor=event_probe.EXECUTION_EVENT_V2_BOOTSTRAP,
        required_schemas=(event_probe.SCHEMA_V1, event_probe.SCHEMA_V2),
        required_callables=_EVENT_CALLABLES_JOB,
        shapes=(
            CallShape("make_event", 0, event_probe.MAKE_EVENT_KEYWORDS + ("schema",)),
            CallShape("JsonlSink", 1, ("mode",)),
        ),
    )


def cpk_event_spec(legacy) -> EventProbeSpec:
    return EventProbeSpec(
        plugin=legacy.cpk_events.PLUGIN,
        error=legacy.cpk_events.ExecutionEventSupportError,
        render=cpk_event_render,
        floor=event_probe.EXECUTION_EVENT_V1_BOOTSTRAP,
        required_schemas=(event_probe.SCHEMA_V1,),
        required_callables=_EVENT_CALLABLES_CPK,
        shapes=(CallShape("make_event", 0, event_probe.MAKE_EVENT_KEYWORDS),),
        v2_schema=event_probe.SCHEMA_V2,
        v2_floor=event_probe.EXECUTION_EVENT_V2_BOOTSTRAP,
        v2_shape=CallShape("make_event", 0, event_probe.MAKE_EVENT_KEYWORDS + ("schema",)),
    )


C = contract_probe

_JOB_CONTRACT_SHAPES = (
    C.CHECK_REQUEST_SHAPE,
    C.PARSE_REQUEST_DOCUMENT_SHAPE,
    C.VALIDATE_INPUT_SHAPE,
    C.DECISION_OUTCOME_SHAPE,
    C.SAME_RESOLUTION_SHAPE,
    C.EXPIRY_SHAPE,
    C.BOUND_REASON_SHAPE,
    C.RESOLUTION_DOCUMENT_SHAPE,
)
_CPK_CONTRACT_SHAPES = (
    C.CANONICAL_JSON_SHAPE,
    C.CHECK_REQUEST_SHAPE,
    C.CHECK_REQUEST_MAPPING_SHAPE,
    C.VALIDATE_INPUT_SHAPE,
    C.DECISION_OUTCOME_SHAPE,
    C.SAME_RESOLUTION_SHAPE,
    C.EXPIRY_SHAPE,
    C.LAPSED_SHAPE,
    C.BOUND_REASON_SHAPE,
    C.RESOLUTION_DOCUMENT_SHAPE,
)
_CPK_REQUIRED_ERRORS = ("RequestError", "InputError", "DecisionError", "ValidatorError")


def job_contract_spec(legacy) -> ContractProbeSpec:
    return ContractProbeSpec(
        error=legacy.job_interrupts.InterruptContractSupportError,
        render=job_contract_render,
        shapes=_JOB_CONTRACT_SHAPES,
    )


def cpk_contract_spec(legacy) -> ContractProbeSpec:
    return ContractProbeSpec(
        error=legacy.cpk_interrupts.InterruptSupportError,
        render=cpk_contract_render,
        shapes=_CPK_CONTRACT_SHAPES,
        required_errors=_CPK_REQUIRED_ERRORS,
    )


def job_validator_spec(legacy, *, llm_kit: bool = False) -> ValidatorProbeSpec:
    extra = {} if llm_kit else {"package": "llm_scripting_kit", "module": _LSK_VALIDATOR}
    return ValidatorProbeSpec(
        error=legacy.job_interrupts.JsonSchemaSupportError,
        render=job_validator_render,
        floor=legacy.job_interrupts._JSON_SCHEMA_LSK_VERSION,
        subset=None,
        **extra,
    )


def cpk_validator_spec(legacy, *, llm_kit: bool = False) -> ValidatorProbeSpec:
    extra = {} if llm_kit else {"package": "llm_scripting_kit", "module": _LSK_VALIDATOR}
    return ValidatorProbeSpec(
        error=legacy.cpk_interrupts.InterruptSupportError,
        render=cpk_validator_render,
        floor=legacy.cpk_interrupts.JSON_SCHEMA_LSK_VERSION,
        subset=legacy.cpk_interrupts.VALIDATOR_SUBSET,
        **extra,
    )


def job_adapter_spec(legacy, *, llm_kit: bool = False) -> InterruptAdapterSpec:
    job = legacy.job_interrupts
    return InterruptAdapterSpec(
        owner="job-kit",
        contract=job_contract_spec(legacy),
        validator=job_validator_spec(legacy, llm_kit=llm_kit),
        accepted_envelopes=job.REQUEST_ENVELOPES,
        resolution_envelope=job.RESOLUTION_ENVELOPE_V1,
        request_error=job.InterruptRequestError,
        input_error=job.InterruptInputError,
        hash_decision=True,
    )


def cpk_adapter_spec(legacy, *, llm_kit: bool = False) -> InterruptAdapterSpec:
    cpk = legacy.cpk_interrupts
    return InterruptAdapterSpec(
        owner=cpk.OWNER,
        contract=cpk_contract_spec(legacy),
        validator=cpk_validator_spec(legacy, llm_kit=llm_kit),
        accepted_envelopes=cpk.REQUEST_ENVELOPES,
        resolution_envelope=cpk.RESOLUTION_ENVELOPE,
        request_error=legacy.cpk_model.InterruptRequestError,
        input_error=legacy.cpk_model.ResolutionInputError,
        map_validator_error=lambda exc: cpk._validator_too_old(str(exc)),
    )


def job_adapter(legacy, *, llm_kit: bool = False) -> InterruptAdapter:
    """The job adapter with job-kit's stand-in policy (see _contract_and_validator)."""
    job = legacy.job_interrupts

    def acquire():
        contract = adapter.contract()
        try:
            validator = adapter.validator()
        except job.JsonSchemaSupportError as exc:
            validator = job._UnusableValidator(contract, exc)
        return contract, validator

    adapter = InterruptAdapter(job_adapter_spec(legacy, llm_kit=llm_kit), acquire=acquire)
    return adapter


def cpk_adapter(legacy, *, llm_kit: bool = False) -> InterruptAdapter:
    return InterruptAdapter(cpk_adapter_spec(legacy, llm_kit=llm_kit))


# ---------------------------------------------------------------------------
# Environments
# ---------------------------------------------------------------------------

_SKIP = ("__name__", "__spec__", "__loader__", "__file__", "__package__", "__cached__")


def clone(module: types.ModuleType) -> types.ModuleType:
    fake = types.ModuleType(module.__name__)
    fake.__dict__.update({k: v for k, v in vars(module).items() if k not in _SKIP})
    return fake


def install(mp, dotted: str, module) -> None:
    """Put ``module`` (or ``None``: unimportable) where ``dotted`` imports from."""
    mp.setitem(sys.modules, dotted, module)
    parent, _, leaf = dotted.rpartition(".")
    parent_module = sys.modules.get(parent)
    if parent_module is not None and hasattr(parent_module, leaf):
        if module is None:
            mp.delattr(parent_module, leaf)
        else:
            mp.setattr(parent_module, leaf, module)


def broken_package(mp, tmp_path: Path, name: str) -> None:
    """Make ``import <name>`` raise ModuleNotFoundError naming another module."""
    package = tmp_path / name
    package.mkdir()
    (package / "__init__.py").write_text(
        "import _parity_missing_dependency\n", encoding="ascii"
    )
    mp.syspath_prepend(str(tmp_path))
    mp.delitem(sys.modules, name)


def _no_schema_make_event(*, seq, run_id, event, plugin, at, unit_id, attempt_id, adapter, model, payload):
    raise AssertionError("never called")


def _narrow_make_event(*, seq, run_id):
    raise AssertionError("never called")


def _sink_without_mode(path):
    raise AssertionError("never called")


def _remover(name):
    def mutate(module):
        delattr(module, name)

    return mutate


def _setter(name, value):
    def mutate(module):
        setattr(module, name, value)

    return mutate


EVENT_MODULE_VARIANTS = {
    "intact": lambda m: None,
    "schemas-missing": _remover("SUPPORTED_SCHEMAS"),
    "schemas-v1-only": _setter("SUPPORTED_SCHEMAS", frozenset({event_probe.SCHEMA_V1})),
    "schemas-v2-only": _setter("SUPPORTED_SCHEMAS", frozenset({event_probe.SCHEMA_V2})),
    "schemas-empty": _setter("SUPPORTED_SCHEMAS", frozenset()),
    "schemas-none": _setter("SUPPORTED_SCHEMAS", None),
    "no-make_event": _remover("make_event"),
    "no-utc_timestamp": _remover("utc_timestamp"),
    "no-usage_payload": _remover("usage_payload"),
    "no-validate_stream": _remover("validate_stream"),
    "no-JsonlSink": _remover("JsonlSink"),
    "make_event-not-callable": _setter("make_event", None),
    "make_event-narrow": _setter("make_event", _narrow_make_event),
    "make_event-no-schema-keyword": _setter("make_event", _no_schema_make_event),
    "sink-without-mode": _setter("JsonlSink", _sink_without_mode),
}

EVENT_PACKAGE_VARIANTS = ("package-absent", "package-broken", "module-absent")


def apply_event_environment(mp, tmp_path, name: str) -> None:
    if name == "real":
        return
    if name == "package-absent":
        mp.setitem(sys.modules, "bootstrap_lib", None)
    elif name == "package-broken":
        broken_package(mp, tmp_path, "bootstrap_lib")
    elif name == "module-absent":
        install(mp, "bootstrap_lib.execution_event", None)
    else:
        fake = clone(importlib.import_module("bootstrap_lib.execution_event"))
        EVENT_MODULE_VARIANTS[name](fake)
        install(mp, "bootstrap_lib.execution_event", fake)


EVENT_ENVIRONMENTS = ["real", *EVENT_PACKAGE_VARIANTS, *EVENT_MODULE_VARIANTS]


CONTRACT_CALLABLES = sorted(
    {shape.name for shape in _JOB_CONTRACT_SHAPES + _CPK_CONTRACT_SHAPES}
)


def _bad_shape(*, unrelated_keyword):
    raise AssertionError("never called")


CONTRACT_MODULE_VARIANTS = {
    "intact": lambda m: None,
    "contracts-missing": _remover("SUPPORTED_CONTRACTS"),
    "contracts-wrong": _setter("SUPPORTED_CONTRACTS", frozenset({"plugins-kit.interrupt-contract/v0"})),
    "contracts-none": _setter("SUPPORTED_CONTRACTS", None),
    "contracts-int": _setter("SUPPORTED_CONTRACTS", 7),
}
for _name in CONTRACT_CALLABLES:
    CONTRACT_MODULE_VARIANTS[f"no-{_name}"] = _remover(_name)
    CONTRACT_MODULE_VARIANTS[f"bad-shape-{_name}"] = _setter(_name, _bad_shape)
for _name in _CPK_REQUIRED_ERRORS:
    CONTRACT_MODULE_VARIANTS[f"no-error-{_name}"] = _remover(_name)
    CONTRACT_MODULE_VARIANTS[f"error-not-a-class-{_name}"] = _setter(_name, "x")

CONTRACT_PACKAGE_VARIANTS = ("package-absent", "package-broken", "module-absent")
CONTRACT_ENVIRONMENTS = ["real", *CONTRACT_PACKAGE_VARIANTS, *CONTRACT_MODULE_VARIANTS]


def apply_contract_environment(mp, tmp_path, name: str) -> None:
    if name == "real":
        return
    if name == "package-absent":
        mp.setitem(sys.modules, "bootstrap_lib", None)
    elif name == "package-broken":
        broken_package(mp, tmp_path, "bootstrap_lib")
    elif name == "module-absent":
        install(mp, "bootstrap_lib.interrupt_contract", None)
    else:
        fake = clone(importlib.import_module("bootstrap_lib.interrupt_contract"))
        CONTRACT_MODULE_VARIANTS[name](fake)
        install(mp, "bootstrap_lib.interrupt_contract", fake)


def _check_schema_bad(schema):
    raise AssertionError("never called")


def _validate_bad(schema, value):
    raise AssertionError("never called")


VALIDATOR_MODULE_VARIANTS = {
    "intact": lambda m: None,
    "subsets-missing": _remover("SUPPORTED_SUBSETS"),
    "subsets-empty": _setter("SUPPORTED_SUBSETS", frozenset()),
    "subsets-other": _setter("SUPPORTED_SUBSETS", frozenset({"some-other-subset"})),
    "subsets-tuple": _setter(
        "SUPPORTED_SUBSETS", ("x", schema_validator.VALIDATOR_SUBSET)
    ),
    "subsets-none": _setter("SUPPORTED_SUBSETS", None),
    "subsets-int": _setter("SUPPORTED_SUBSETS", 3),
    "no-check_schema": _remover("check_schema"),
    "no-validate": _remover("validate"),
    "check_schema-bad-shape": _setter("check_schema", _check_schema_bad),
    "validate-bad-shape": _setter("validate", _validate_bad),
    "both-missing": lambda m: (_remover("check_schema")(m), _remover("validate")(m)),
}
VALIDATOR_PACKAGE_VARIANTS = ("package-absent", "package-broken", "module-absent")
VALIDATOR_ENVIRONMENTS = ["real", *VALIDATOR_PACKAGE_VARIANTS, *VALIDATOR_MODULE_VARIANTS]


def apply_validator_environment(mp, tmp_path, name: str) -> None:
    if name == "real":
        return
    if name == "package-absent":
        mp.setitem(sys.modules, "llm_scripting_kit", None)
    elif name == "package-broken":
        broken_package(mp, tmp_path, "llm_scripting_kit")
    elif name == "module-absent":
        install(mp, _LSK_VALIDATOR, None)
    else:
        fake = clone(importlib.import_module(_LSK_VALIDATOR))
        VALIDATOR_MODULE_VARIANTS[name](fake)
        install(mp, _LSK_VALIDATOR, fake)


# ---------------------------------------------------------------------------
# Probes: execution event
# ---------------------------------------------------------------------------

# Cases where the legacy job probe leaks a TypeError; see TestDocumentedDivergences.
_JOB_EVENT_DIVERGENT = {"schemas-none"}


class TestEventProbe:
    @pytest.mark.parametrize("environment", EVENT_ENVIRONMENTS)
    def test_job_probe_matches_job_kit(self, legacy, monkeypatch, tmp_path, environment):
        apply_event_environment(monkeypatch, tmp_path, environment)
        expected = outcome(legacy.job_events._execution_event)
        actual = outcome(lambda: event_probe.probe_execution_event(job_event_spec(legacy)))
        if environment in _JOB_EVENT_DIVERGENT:
            assert expected[0] == "ok" or expected[1] is not None
            pytest.skip("documented divergence, asserted in TestDocumentedDivergences")
        assert actual == expected

    @pytest.mark.parametrize("environment", EVENT_ENVIRONMENTS)
    def test_cpk_probe_matches_content_pipeline(self, legacy, monkeypatch, tmp_path, environment):
        apply_event_environment(monkeypatch, tmp_path, environment)
        expected = outcome(legacy.cpk_events._execution_event)
        actual = outcome(lambda: event_probe.probe_execution_event(cpk_event_spec(legacy)))
        assert actual == expected

    @pytest.mark.parametrize("environment", EVENT_ENVIRONMENTS)
    def test_cpk_v2_stage_matches_content_pipeline(self, legacy, monkeypatch, tmp_path, environment):
        apply_event_environment(monkeypatch, tmp_path, environment)
        expected = outcome(legacy.cpk_events._execution_event_v2)
        actual = outcome(lambda: event_probe.probe_execution_event_v2(cpk_event_spec(legacy)))
        assert actual == expected

    def test_probe_refusals_are_not_vacuous(self, legacy, monkeypatch, tmp_path):
        """The absent, too-old and v2-only messages are all reachable and distinct."""
        seen = set()
        for environment in ("package-absent", "schemas-v1-only", "no-make_event"):
            with monkeypatch.context() as mp:
                directory = tmp_path / environment
                directory.mkdir()
                apply_event_environment(mp, directory, environment)
                result = outcome(lambda: event_probe.probe_execution_event(job_event_spec(legacy)))
                assert result[0] == "raise", environment
                seen.add(result[2])
        assert len(seen) == 3

    def test_v2_stage_needs_a_v2_spec(self, legacy):
        with pytest.raises(ValueError, match="declares no schema-v2 stage"):
            event_probe.probe_execution_event_v2(job_event_spec(legacy))


# ---------------------------------------------------------------------------
# Probes: interrupt contract and the two _schema_validator loaders
# ---------------------------------------------------------------------------


class TestContractProbe:
    @pytest.mark.parametrize("environment", CONTRACT_ENVIRONMENTS)
    def test_job_contract_probe(self, legacy, monkeypatch, tmp_path, environment):
        apply_contract_environment(monkeypatch, tmp_path, environment)
        expected = outcome(legacy.job_interrupts._interrupt_contract)
        actual = outcome(
            lambda: contract_probe.probe_interrupt_contract(job_contract_spec(legacy))
        )
        assert actual == expected

    @pytest.mark.parametrize("environment", CONTRACT_ENVIRONMENTS)
    def test_cpk_contract_probe(self, legacy, monkeypatch, tmp_path, environment):
        apply_contract_environment(monkeypatch, tmp_path, environment)
        expected = outcome(legacy.cpk_interrupts._interrupt_contract)
        actual = outcome(
            lambda: contract_probe.probe_interrupt_contract(cpk_contract_spec(legacy))
        )
        assert actual == expected


class TestSchemaValidatorProbe:
    @pytest.mark.parametrize("environment", VALIDATOR_ENVIRONMENTS)
    def test_job_validator_loader(self, legacy, monkeypatch, tmp_path, environment):
        apply_validator_environment(monkeypatch, tmp_path, environment)
        expected = outcome(legacy.job_interrupts._schema_validator)
        actual = outcome(InterruptAdapter(job_adapter_spec(legacy)).validator)
        assert actual == expected

    @pytest.mark.parametrize("environment", VALIDATOR_ENVIRONMENTS)
    def test_cpk_validator_loader(self, legacy, monkeypatch, tmp_path, environment):
        apply_validator_environment(monkeypatch, tmp_path, environment)
        expected = outcome(legacy.cpk_interrupts._schema_validator)
        actual = outcome(InterruptAdapter(cpk_adapter_spec(legacy)).validator)
        assert actual == expected

    @pytest.mark.parametrize("environment", CONTRACT_ENVIRONMENTS)
    def test_job_loader_probes_the_contract_first(
        self, legacy, monkeypatch, tmp_path, environment
    ):
        """The job loader reads the subset from the contract, so a bad contract wins."""
        apply_contract_environment(monkeypatch, tmp_path, environment)
        expected = outcome(legacy.job_interrupts._schema_validator)
        actual = outcome(InterruptAdapter(job_adapter_spec(legacy)).validator)
        assert actual == expected

    @pytest.mark.parametrize("environment", CONTRACT_ENVIRONMENTS)
    def test_cpk_support_probe(self, legacy, monkeypatch, tmp_path, environment):
        apply_contract_environment(monkeypatch, tmp_path, environment)
        expected = outcome(legacy.cpk_interrupts.support)
        actual = outcome(InterruptAdapter(cpk_adapter_spec(legacy)).support)
        assert actual == expected

    def test_llm_kit_validator_is_named_and_usable(self):
        assert schema_validator.VALIDATOR_MODULE == "llm_kit.schema.json_schema"
        assert schema_validator.VALIDATOR_PACKAGE == "llm_kit"
        module = importlib.import_module("llm_kit.schema.json_schema")
        spec = ValidatorProbeSpec(
            error=ImportError,
            render=lambda failure: failure.kind,
            floor="0.0.0",
        )
        assert schema_validator.probe_schema_validator(spec) is module
        assert module.SUPPORTED_SUBSETS >= {schema_validator.VALIDATOR_SUBSET}

    def test_subset_literal_is_the_contracts(self, legacy):
        assert schema_validator.VALIDATOR_SUBSET == legacy.contract.VALIDATOR_SUBSET
        assert schema_validator.VALIDATOR_SUBSET == legacy.cpk_interrupts.VALIDATOR_SUBSET

    def test_a_spec_without_a_subset_must_be_given_one(self, legacy):
        with pytest.raises(ValueError, match="needs a subset literal"):
            schema_validator.probe_schema_validator(job_validator_spec(legacy))


class TestProbeCauses:
    """The shared probe chains the original exception wherever there is one."""

    def test_absent_module_and_bad_shape_are_chained(self, legacy, monkeypatch, tmp_path):
        for environment, cause in (
            ("package-absent", ModuleNotFoundError),
            ("module-absent", ModuleNotFoundError),
            ("make_event-narrow", TypeError),
        ):
            with monkeypatch.context() as mp:
                directory = tmp_path / environment
                directory.mkdir()
                apply_event_environment(mp, directory, environment)
                with pytest.raises(legacy.cpk_events.ExecutionEventSupportError) as caught:
                    event_probe.probe_execution_event(cpk_event_spec(legacy))
                assert type(caught.value.__cause__) is cause, environment


class TestFloors:
    """The floors are per-spec data; the shared constants record the two releases."""

    def test_event_floors_equal_each_products_own(self, legacy):
        assert event_probe.EXECUTION_EVENT_V2_BOOTSTRAP == legacy.job_events._EXECUTION_EVENT_BOOTSTRAP
        assert event_probe.EXECUTION_EVENT_V1_BOOTSTRAP == legacy.cpk_events.EXECUTION_EVENT_BOOTSTRAP
        assert event_probe.EXECUTION_EVENT_V2_BOOTSTRAP == legacy.cpk_events.EXECUTION_EVENT_V2_BOOTSTRAP
        assert job_event_spec(legacy).floor == "0.136.0"
        assert cpk_event_spec(legacy).floor == "0.135.0"
        assert cpk_event_spec(legacy).v2_floor == "0.136.0"

    def test_contract_floor_is_shared_by_both_products(self, legacy):
        floor = contract_probe.INTERRUPT_CONTRACT_BOOTSTRAP
        assert floor == legacy.job_interrupts._INTERRUPT_CONTRACT_BOOTSTRAP
        assert floor == legacy.cpk_interrupts.INTERRUPT_CONTRACT_BOOTSTRAP
        assert contract_probe.CONTRACT_V1 == legacy.job_interrupts._CONTRACT_V1
        assert contract_probe.CONTRACT_V1 == legacy.cpk_interrupts.CONTRACT

    def test_a_spec_without_a_floor_is_refused(self, legacy):
        with pytest.raises(ValueError, match="floor must be a non-empty string"):
            dataclasses.replace(job_event_spec(legacy), floor="")
        with pytest.raises(ValueError, match="v2_floor must be a non-empty string"):
            dataclasses.replace(cpk_event_spec(legacy), v2_floor="")


# ---------------------------------------------------------------------------
# Events against the real module
# ---------------------------------------------------------------------------


class TestEventBytes:
    @pytest.mark.parametrize(
        "kwargs",
        [
            dict(seq=0, run_id="r1", event="job-kit:run-created"),
            dict(seq=3, run_id="r1", event="call-started", job_id="j1", attempt_no=2,
                 adapter="claude", model="m", payload={"k": "v"}),
            dict(seq=4, run_id="r1", event="result", job_id="j1", attempt_no=0,
                 adapter="", model="", payload={}),
            dict(seq=5, run_id="r1", event="interrupt", job_id="j1", attempt_no=1,
                 payload={"interrupt_id": "7", "kind": "ask", "phase": "requested"},
                 schema=event_probe.SCHEMA_V2),
            dict(seq=-1, run_id="r1", event="result", job_id="j1"),
            dict(seq=1, run_id="", event="result", job_id="j1"),
            dict(seq=1, run_id="r1", event="Not An Event", job_id="j1"),
        ],
    )
    def test_job_build_event(self, legacy, kwargs):
        spec = job_event_spec(legacy)
        at = "2026-01-02T03:04:05Z"

        def shared():
            module = event_probe.probe_execution_event(spec)
            payload = kwargs.get("payload")
            attempt_no = kwargs.get("attempt_no")
            return event_probe.build_event(
                module,
                spec,
                seq=kwargs["seq"],
                run_id=kwargs["run_id"],
                event=kwargs["event"],
                at=at,
                unit_id=kwargs.get("job_id"),
                attempt_id=str(attempt_no) if attempt_no is not None else None,
                adapter=kwargs.get("adapter") or None,
                model=kwargs.get("model") or None,
                payload=dict(payload) if payload is not None else None,
                schema=kwargs.get("schema") or event_probe.SCHEMA_V1,
            )

        expected = outcome(lambda: legacy.job_events.build_event(at=at, **kwargs))
        assert outcome(shared) == expected
        assert expected[0] in ("ok", "raise")

    def test_source_literals_are_frozen(self, legacy):
        assert job_event_spec(legacy).plugin == "job-kit"
        assert cpk_event_spec(legacy).plugin == "content-pipeline-kit"

    def test_cpk_projection_events_match(self, legacy):
        """The v1 events project_run builds equal build_event's for the same facts."""
        cpk = legacy.cpk_events
        model = legacy.cpk_model
        spec = cpk_event_spec(legacy)
        ee = legacy.event_module
        run = SimpleNamespace(
            created_at=1_700_000_000.5,
            driver="inline",
            adapter_version="1",
            backend="claude",
            model="opus",
        )
        claim = model.AttemptRecord(
            id=1,
            run_id="r1",
            unit_id="u1",
            kind=model.AttemptKind.CLAIM,
            at=1_700_000_001.0,
            worker_id="w1",
            fencing_token=3,
        )

        class Store:
            def snapshot(self, run_id):
                return run, [], [claim]

        projected = cpk.project_run(Store(), "r1")
        module = event_probe.probe_execution_event(spec)
        built = (
            event_probe.build_event(
                module, spec, seq=0, run_id="r1", event="content-pipeline-kit:run-created",
                at=ee.utc_timestamp(run.created_at),
                adapter="claude", model="opus",
                payload={"driver": "inline", "adapter_version": "1"},
            ),
            event_probe.build_event(
                module, spec, seq=1 * 4 + 1, run_id="r1", event="call-started",
                at=ee.utc_timestamp(claim.at), unit_id="u1", attempt_id="3",
                adapter="claude", model="opus", payload={"worker_id": "w1"},
            ),
        )
        assert projected == module.validate_stream(built)


# ---------------------------------------------------------------------------
# Interrupt adapters against the real contract and validator
# ---------------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "integer"}},
    "required": ["a"],
    "additionalProperties": False,
}

REQUEST_CASES = {
    "minimal": dict(kind="ask", request_schema=SCHEMA, payload={"q": "why"}),
    "with-expiry": dict(kind="ask", request_schema=SCHEMA, payload={}, expires_in_s=30),
    "expiry-max": dict(kind="ask", request_schema=SCHEMA, payload={}, expires_in_s=2**31 - 1),
    "expiry-over": dict(kind="ask", request_schema=SCHEMA, payload={}, expires_in_s=2**31),
    "expiry-zero": dict(kind="ask", request_schema=SCHEMA, payload={}, expires_in_s=0),
    "expiry-negative": dict(kind="ask", request_schema=SCHEMA, payload={}, expires_in_s=-1),
    "expiry-bool": dict(kind="ask", request_schema=SCHEMA, payload={}, expires_in_s=True),
    "expiry-float": dict(kind="ask", request_schema=SCHEMA, payload={}, expires_in_s=1.5),
    "kind-long": dict(kind="k" * 65, request_schema=SCHEMA, payload={}),
    "kind-limit": dict(kind="k" * 64, request_schema=SCHEMA, payload={}),
    "kind-empty": dict(kind="", request_schema=SCHEMA, payload={}),
    "kind-not-str": dict(kind=5, request_schema=SCHEMA, payload={}),
    "schema-unsupported": dict(kind="ask", request_schema={"type": "object", "$ref": "#"}, payload={}),
    "schema-not-mapping": dict(kind="ask", request_schema=[1], payload={}),
    "payload-not-mapping": dict(kind="ask", request_schema=SCHEMA, payload=[1]),
    "payload-not-json": dict(kind="ask", request_schema=SCHEMA, payload={"s": {1, 2}}),
    "payload-huge": dict(kind="ask", request_schema=SCHEMA, payload={"s": "x" * 200_000}),
    "payload-nan": dict(kind="ask", request_schema=SCHEMA, payload={"n": float("nan")}),
    "envelope-wrong": dict(kind="ask", request_schema=SCHEMA, payload={}, envelope="other/v1"),
}


def _normalise_request(value):
    if isinstance(value, dict):
        keys = ("envelope", "kind", "request_schema", "payload", "expires_in_s")
        return tuple(value[key] for key in keys)
    return (value.envelope, value.kind, value.request_schema, value.payload, value.expires_in_s)


def _request(legacy_product, case, envelope_default):
    fields = dict(case)
    fields.setdefault("envelope", envelope_default)
    model = legacy_product
    return model.InterruptRequest(**fields)


@pytest.fixture(params=["legacy", "llm_kit"])
def flavor(request):
    return request.param == "llm_kit"


class TestInterruptAdapters:
    @pytest.mark.parametrize("case", REQUEST_CASES)
    def test_job_check_request(self, legacy, flavor, case):
        job = legacy.job_interrupts
        adapter = job_adapter(legacy, llm_kit=flavor)
        request = _request(legacy.job_model, REQUEST_CASES[case], job.REQUEST_ENVELOPE_V1)
        expected = outcome(lambda: _normalise_request(job.check_request(request)))
        actual = outcome(lambda: _normalise_request(adapter.check_request(request)))
        # The job wrapper builds its dataclass from the mapping; compare content.
        assert actual == expected

    @pytest.mark.parametrize("case", REQUEST_CASES)
    def test_cpk_check_request(self, legacy, flavor, case):
        cpk = legacy.cpk_interrupts
        adapter = cpk_adapter(legacy, llm_kit=flavor)
        request = _request(legacy.cpk_model, REQUEST_CASES[case], cpk.REQUEST_ENVELOPE)
        expected = outcome(lambda: _normalise_request(cpk.check_request(request)))
        actual = outcome(
            lambda: _normalise_request(
                adapter.check_request(request, expected_type=legacy.cpk_model.InterruptRequest)
            )
        )
        assert actual == expected

    def test_cpk_check_request_refuses_a_foreign_type_after_probing(self, legacy):
        cpk = legacy.cpk_interrupts
        adapter = cpk_adapter(legacy)
        expected = outcome(lambda: cpk.check_request({"kind": "x"}))
        actual = outcome(
            lambda: adapter.check_request(
                {"kind": "x"}, expected_type=legacy.cpk_model.InterruptRequest
            )
        )
        assert actual == expected
        assert expected[0] == "raise" and "must be an InterruptRequest, got dict" in expected[2]

    @pytest.mark.parametrize("environment", ["package-absent", "no-check_request"])
    def test_cpk_check_request_probes_before_the_type_check(
        self, legacy, monkeypatch, tmp_path, environment
    ):
        apply_contract_environment(monkeypatch, tmp_path, environment)
        cpk = legacy.cpk_interrupts
        expected = outcome(lambda: cpk.check_request({"kind": "x"}))
        actual = outcome(
            lambda: cpk_adapter(legacy).check_request(
                {"kind": "x"}, expected_type=legacy.cpk_model.InterruptRequest
            )
        )
        assert actual == expected
        assert expected[1] is cpk.InterruptSupportError

    MAPPING_CASES = {
        "valid": {"schema": "plugins-kit.interrupt-request/v1", "kind": "ask",
                  "request_schema": SCHEMA, "payload": {}},
        "valid-expiry": {"schema": "plugins-kit.interrupt-request/v1", "kind": "ask",
                         "request_schema": SCHEMA, "payload": {}, "expires_in_s": 9},
        "missing-key": {"schema": "plugins-kit.interrupt-request/v1", "kind": "ask"},
        "unknown-key": {"schema": "plugins-kit.interrupt-request/v1", "kind": "ask",
                        "request_schema": SCHEMA, "payload": {}, "extra": 1},
        "wrong-envelope": {"schema": "job-kit.interrupt-request/v1", "kind": "ask",
                           "request_schema": SCHEMA, "payload": {}},
        "not-a-mapping": ["x"],
    }

    @pytest.mark.parametrize("case", MAPPING_CASES)
    def test_cpk_request_from_mapping(self, legacy, flavor, case):
        cpk = legacy.cpk_interrupts
        adapter = cpk_adapter(legacy, llm_kit=flavor)
        raw = self.MAPPING_CASES[case]
        expected = outcome(lambda: _normalise_request(cpk.request_from_mapping(raw)))
        actual = outcome(lambda: _normalise_request(adapter.check_request_mapping(raw)))
        assert actual == expected

    @pytest.mark.parametrize(
        "name,data",
        [
            ("valid", json.dumps({"schema": "job-kit.interrupt-request/v1", "kind": "ask",
                                  "request_schema": SCHEMA, "payload": {"q": 1}}).encode()),
            ("valid-expiry", json.dumps({"schema": "job-kit.interrupt-request/v1", "kind": "ask",
                                         "request_schema": SCHEMA, "payload": {},
                                         "expires_in_s": 60}).encode()),
            ("not-json", b"{nope"),
            ("not-utf8", b"\xff\xfe"),
            ("empty", b""),
            ("nan", b'{"schema": "job-kit.interrupt-request/v1", "kind": "k", "request_schema": {}, "payload": NaN}'),
            ("repeated-key", b'{"kind": "a", "kind": "b"}'),
            ("array", b"[1]"),
            ("oversize", b" " * 131073),
            ("wrong-envelope", json.dumps({"schema": "plugins-kit.interrupt-request/v1", "kind": "ask",
                                           "request_schema": SCHEMA, "payload": {}}).encode()),
        ],
        ids=lambda value: value if isinstance(value, str) else "data",
    )
    def test_job_parse_request(self, legacy, flavor, tmp_path, name, data):
        job = legacy.job_interrupts
        adapter = job_adapter(legacy, llm_kit=flavor)
        path = tmp_path / f"{name}.json"
        path.write_bytes(data)
        with path.open("rb") as handle:
            read = handle.read(job.REQUEST_FILE_LIMIT + 1)
        expected = outcome(lambda: _normalise_request(job.parse_request(path)))
        actual = outcome(lambda: _normalise_request(adapter.parse_request_document(read)))
        assert actual == expected

    def test_job_stand_in_orders_refusals_like_job_kit(self, legacy, monkeypatch, tmp_path):
        """With an unusable validator, a bad request is refused first; a good one reports it."""
        apply_validator_environment(monkeypatch, tmp_path, "subsets-empty")
        job = legacy.job_interrupts
        adapter = job_adapter(legacy)
        for name in ("minimal", "kind-empty", "schema-unsupported"):
            request = _request(legacy.job_model, REQUEST_CASES[name], job.REQUEST_ENVELOPE_V1)
            expected = outcome(lambda: job.check_request(request))
            actual = outcome(lambda: adapter.check_request(request))
            assert (actual[1:4]) == (expected[1:4]), name
        for value in ({"a": 1}, {"a": "x"}, object()):
            expected = outcome(lambda: job.validate_input(SCHEMA, value))
            actual = outcome(lambda: adapter.validate_input(SCHEMA, value))
            assert actual == expected

    @pytest.mark.parametrize(
        "value",
        [
            {"a": 1},
            {"a": "x"},
            {},
            {"a": 1, "b": 2},
            {"a": float("nan")},
            {"a": {1, 2}},
            {"a": "z" * 70_000},
            [1, 2],
            None,
        ],
    )
    def test_validate_input(self, legacy, flavor, value):
        job = legacy.job_interrupts
        cpk = legacy.cpk_interrupts
        assert outcome(lambda: job_adapter(legacy, llm_kit=flavor).validate_input(SCHEMA, value)) == outcome(
            lambda: job.validate_input(SCHEMA, value)
        )
        assert outcome(lambda: cpk_adapter(legacy, llm_kit=flavor).validate_input(SCHEMA, value)) == outcome(
            lambda: cpk.validate_input(SCHEMA, value)
        )

    def test_validate_input_reports_validator_errors_verbatim(self, legacy):
        result = outcome(lambda: job_adapter(legacy).validate_input(SCHEMA, {"a": "x"}))
        assert result[0] == "raise" and result[3]  # (json_pointer, keyword) tuples

    DECISIONS = [
        (("answer",), {"input": {"a": 1}}),
        (("answer",), {}),
        (("answer",), {"reason": "no"}),
        (("reject",), {"reason": "no"}),
        (("reject",), {"reason": 12}),
        (("reject",), {"input": {"a": 1}}),
        (("reject",), {}),
        (("maybe",), {}),
        ((None,), {}),
        ((["answer"],), {}),
        (({"answer": 1},), {}),
    ]

    @pytest.mark.parametrize("args,kwargs", DECISIONS)
    def test_decision_outcome(self, legacy, args, kwargs):
        assert outcome(lambda: job_adapter(legacy).decision_outcome(*args, **kwargs)) == outcome(
            lambda: legacy.job_interrupts.decision_outcome(*args, **kwargs)
        )
        assert outcome(lambda: cpk_adapter(legacy).decision_outcome(*args, **kwargs)) == outcome(
            lambda: legacy.cpk_interrupts.decision_outcome(*args, **kwargs)
        )

    def test_unhashable_decision_is_a_type_error_before_the_probe_only_for_jobs(
        self, legacy, monkeypatch, tmp_path
    ):
        apply_contract_environment(monkeypatch, tmp_path, "package-absent")
        job = outcome(lambda: job_adapter(legacy).decision_outcome(["x"]))
        assert job == outcome(lambda: legacy.job_interrupts.decision_outcome(["x"]))
        assert job[1] is TypeError
        cpk = outcome(lambda: cpk_adapter(legacy).decision_outcome(["x"]))
        assert cpk == outcome(lambda: legacy.cpk_interrupts.decision_outcome(["x"]))
        assert cpk[1] is legacy.cpk_interrupts.InterruptSupportError

    STORED = [
        ("answered", '{"a":1}', None),
        ("answered", None, None),
        ("rejected", None, "no"),
        ("rejected", None, None),
        ("expired", None, None),
    ]
    NEW = [
        ("answered", {"a": 1}, None),
        ("answered", {"a": 2}, None),
        ("answered", {"b": 1, "a": 1}, None),
        ("answered", None, None),
        ("rejected", None, "no"),
        ("rejected", None, "n" + "o" * 3000),
        ("rejected", None, None),
        ("rejected", None, 7),
        ("expired", None, None),
    ]

    def test_same_resolution_replay_matrix(self, legacy):
        job = job_adapter(legacy)
        cpk = cpk_adapter(legacy)
        checked = 0
        for stored_outcome, stored_input, stored_reason in self.STORED:
            for new_outcome, new_input, new_reason in self.NEW:
                kwargs = dict(
                    stored_outcome=stored_outcome,
                    stored_input_json=stored_input,
                    stored_reason=stored_reason,
                    outcome=new_outcome,
                    input=new_input,
                    reason=new_reason,
                )
                assert outcome(lambda: job.same_resolution(**kwargs)) == outcome(
                    lambda: legacy.job_interrupts.same_resolution(**kwargs)
                )
                assert outcome(lambda: cpk.same_resolution(**kwargs)) == outcome(
                    lambda: legacy.cpk_interrupts.same_resolution(**kwargs)
                )
                checked += 1
        assert checked == len(self.STORED) * len(self.NEW)

    def test_same_resolution_reaches_both_answers(self, legacy):
        job = job_adapter(legacy)
        same = job.same_resolution(
            stored_outcome="answered", stored_input_json='{"a":1}', stored_reason=None,
            outcome="answered", input={"a": 1}, reason=None,
        )
        different = job.same_resolution(
            stored_outcome="answered", stored_input_json='{"a":1}', stored_reason=None,
            outcome="answered", input={"a": 2}, reason=None,
        )
        assert (same, different) == (True, False)

    EXPIRIES = [
        (100.0, None), (100.0, 0), (100.0, 1), (100.5, 30), (0.0, 2**31 - 1),
        (1.7e9, 3600), (-5.0, 5), (100, 2.5),
    ]

    @pytest.mark.parametrize("created_at,expires_in_s", EXPIRIES)
    def test_expiry(self, legacy, created_at, expires_in_s):
        assert outcome(lambda: job_adapter(legacy).expiry(created_at, expires_in_s)) == outcome(
            lambda: legacy.job_interrupts.expiry(created_at, expires_in_s)
        )
        assert outcome(lambda: cpk_adapter(legacy).expiry(created_at, expires_in_s)) == outcome(
            lambda: legacy.cpk_interrupts.expiry(created_at, expires_in_s)
        )

    LAPSES = [
        (None, 0.0), (None, 1e12), (100.0, 99.999), (100.0, 100.0), (100.0, 100.001),
        (0.0, 0.0), (-1.0, 0.0), (100, 100), (100.0, float("inf")),
    ]

    @pytest.mark.parametrize("expires_at,now", LAPSES)
    def test_lapse_rule_is_one_rule_in_four_forms(self, legacy, expires_at, now):
        contract_value = legacy.contract.lapsed(expires_at, now)
        assert values.interrupt_lapsed(expires_at, now) is contract_value
        assert legacy.job_model.interrupt_lapsed(expires_at, now) is contract_value
        assert cpk_adapter(legacy).lapsed(expires_at, now) is contract_value
        assert legacy.cpk_interrupts.lapsed(expires_at, now) is contract_value

    @pytest.mark.parametrize("expires_at,now", LAPSES)
    @pytest.mark.parametrize("resolved", [False, True])
    def test_record_lapse_matches_both_record_classes(self, legacy, expires_at, now, resolved):
        job_record = legacy.job_model.InterruptRecord(
            id="1", run_id="r", job_id="j", attempt_no=1, continuation_no=0,
            envelope="e", kind="k", request_schema={}, payload={}, created_at=1.0,
            expires_at=expires_at,
            resolution=legacy.job_model.InterruptResolution("1", "answered", 2.0) if resolved else None,
        )
        cpk_record = legacy.cpk_model.InterruptRecord(
            id="1", run_id="r", unit_id="u", fencing_token=1, envelope="e", kind="k",
            request_schema={}, payload={}, created_at=1.0, expires_at=expires_at,
            resolution=legacy.cpk_model.InterruptResolution("1", "answered", 2.0) if resolved else None,
        )
        shared = values.record_lapsed(job_record.resolution, expires_at, now)
        assert shared is job_record.lapsed(now)
        assert shared is cpk_record.lapsed(now)

    def test_lapse_rule_is_inclusive_and_never_for_no_expiry(self):
        assert values.interrupt_lapsed(5.0, 5.0) is True
        assert values.interrupt_lapsed(5.0, 4.999) is False
        assert values.interrupt_lapsed(None, 1e18) is False

    @pytest.mark.parametrize("reason", [None, "", "short", "x" * 2000, "x" * 2001, 12, ["a"], "\u00e9" * 2500])
    def test_bound_reason(self, legacy, reason):
        assert outcome(lambda: job_adapter(legacy).bound_reason(reason)) == outcome(
            lambda: legacy.job_interrupts.bound_reason(reason)
        )
        assert outcome(lambda: cpk_adapter(legacy).bound_reason(reason)) == outcome(
            lambda: legacy.cpk_interrupts.bound_reason(reason)
        )

    CANONICAL = [
        {"b": 1, "a": [3, 2, {"z": None, "y": True}]},
        {"u": "caf\u00e9 \U0001f600"},
        {},
        [1.0, 2, "x"],
        "text",
        None,
        {"n": float("nan")},
        {"n": float("inf")},
        {1: "int key"},
        {"s": {1, 2}},
    ]

    @pytest.mark.parametrize("value", CANONICAL, ids=range(len(CANONICAL)))
    def test_canonical_json(self, legacy, value):
        expected_job = outcome(lambda: legacy.job_interrupts.canonical_json(value))
        expected_cpk = outcome(lambda: legacy.cpk_interrupts.canonical_json(value))
        assert outcome(lambda: values.canonical_json(value)) == expected_job
        assert outcome(lambda: cpk_adapter(legacy).canonical_json(value)) == expected_cpk
        assert outcome(lambda: legacy.contract.canonical_json(value)) == expected_cpk

    def test_limits_are_the_contracts_and_both_products(self, legacy):
        contract = legacy.contract
        job = legacy.job_interrupts
        cpk = legacy.cpk_interrupts
        for name in ("INPUT_LIMIT", "KIND_LIMIT", "EXPIRES_IN_S_MAX", "REASON_LIMIT"):
            assert getattr(shared_interrupts, name) == getattr(contract, name), name
        for name in ("INPUT_LIMIT", "KIND_LIMIT", "EXPIRES_IN_S_MAX"):
            assert getattr(shared_interrupts, name) == getattr(job, name), name
        for name in ("INPUT_LIMIT", "KIND_LIMIT", "EXPIRES_IN_S_MAX", "REASON_LIMIT"):
            assert getattr(shared_interrupts, name) == getattr(cpk, name), name

    RESOLUTIONS = [
        ("answered", {"a": 1, "b": [1, 2]}, None),
        ("rejected", None, "not now"),
        ("rejected", None, None),
        ("expired", None, None),
        ("answered", {"u": "caf\u00e9"}, None),
    ]

    @pytest.mark.parametrize("outcome_word,answer,reason", RESOLUTIONS)
    def test_resolution_document_bytes(self, legacy, outcome_word, answer, reason):
        payload = {"q": "why", "n": [1, 2], "u": "\u00e9"}
        job_record = legacy.job_model.InterruptRecord(
            id="9", run_id="r", job_id="j", attempt_no=2, continuation_no=1,
            envelope=legacy.job_interrupts.REQUEST_ENVELOPE_V1, kind="ask",
            request_schema=SCHEMA, payload=payload, created_at=10.0, expires_at=40.5,
            resolution=legacy.job_model.InterruptResolution(
                "9", outcome_word, 12.25, input=answer, reason=reason
            ),
        )
        cpk_record = legacy.cpk_model.InterruptRecord(
            id="9", run_id="r", unit_id="u", fencing_token=4,
            envelope=legacy.cpk_interrupts.REQUEST_ENVELOPE, kind="ask",
            request_schema=SCHEMA, payload=payload, created_at=10.0, expires_at=40.5,
            resolution=legacy.cpk_model.InterruptResolution(
                "9", outcome_word, 12.25, input=answer, reason=reason
            ),
        )
        job_text = legacy.job_interrupts.resolution_document(job_record)
        cpk_text = legacy.cpk_interrupts.resolution_document(cpk_record)
        assert job_adapter(legacy).resolution_document(job_record) == job_text
        assert cpk_adapter(legacy).resolution_document(cpk_record) == cpk_text
        # The shared adapter reads only the immutable fields: either record
        # class gives the same bytes under the same envelope literal.
        assert job_adapter(legacy).resolution_document(cpk_record) == json.dumps(
            json.loads(job_text), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        assert json.loads(job_text)["schema"] == "job-kit.interrupt-resolution/v1"
        assert json.loads(cpk_text)["schema"] == "plugins-kit.interrupt-resolution/v1"

    def test_resolution_document_without_a_resolution(self, legacy):
        job_record = legacy.job_model.InterruptRecord(
            id="3", run_id="r", job_id="j", attempt_no=1, continuation_no=0,
            envelope="e", kind="k", request_schema={}, payload={}, created_at=1.0,
        )
        cpk_record = legacy.cpk_model.InterruptRecord(
            id="3", run_id="r", unit_id="u", fencing_token=1, envelope="e", kind="k",
            request_schema={}, payload={}, created_at=1.0,
        )
        assert outcome(lambda: job_adapter(legacy).resolution_document(job_record)) == outcome(
            lambda: legacy.job_interrupts.resolution_document(job_record)
        )
        assert outcome(lambda: cpk_adapter(legacy).resolution_document(cpk_record)) == outcome(
            lambda: legacy.cpk_interrupts.resolution_document(cpk_record)
        )

    def test_resolution_document_probes_before_reading_nothing_else(
        self, legacy, monkeypatch, tmp_path
    ):
        apply_contract_environment(monkeypatch, tmp_path, "no-resolution_document")
        record = legacy.job_model.InterruptRecord(
            id="3", run_id="r", job_id="j", attempt_no=1, continuation_no=0, envelope="e",
            kind="k", request_schema={}, payload={}, created_at=1.0,
            resolution=legacy.job_model.InterruptResolution("3", "answered", 2.0, input={}),
        )
        assert outcome(lambda: job_adapter(legacy).resolution_document(record)) == outcome(
            lambda: legacy.job_interrupts.resolution_document(record)
        )


class TestValidatorErrorMapping:
    """cpk maps the contract's ValidatorError to its support error; jobs do not."""

    def _contract_with_validator_error(self, monkeypatch, legacy):
        fake = clone(legacy.contract)

        def refuse_validator(*args, **kwargs):
            raise fake.ValidatorError("validator cannot be held to the subset")

        fake.check_request = refuse_validator
        fake.check_request_mapping = refuse_validator
        fake.parse_request_document = refuse_validator
        fake.validate_input = refuse_validator
        install(monkeypatch, "bootstrap_lib.interrupt_contract", fake)
        return fake

    def test_cpk_maps_to_the_support_error_everywhere(self, legacy, monkeypatch):
        self._contract_with_validator_error(monkeypatch, legacy)
        cpk = legacy.cpk_interrupts
        adapter = cpk_adapter(legacy)
        request = _request(legacy.cpk_model, REQUEST_CASES["minimal"], cpk.REQUEST_ENVELOPE)
        raw = TestInterruptAdapters.MAPPING_CASES["valid"]
        for shared, old in (
            (lambda: adapter.check_request(request), lambda: cpk.check_request(request)),
            (lambda: adapter.check_request_mapping(raw), lambda: cpk.request_from_mapping(raw)),
            (lambda: adapter.validate_input(SCHEMA, {"a": 1}), lambda: cpk.validate_input(SCHEMA, {"a": 1})),
        ):
            expected = outcome(old)
            assert outcome(shared) == expected
            assert expected[1] is cpk.InterruptSupportError

    def test_job_lets_it_propagate(self, legacy, monkeypatch):
        fake = self._contract_with_validator_error(monkeypatch, legacy)
        job = legacy.job_interrupts
        adapter = job_adapter(legacy)
        request = _request(legacy.job_model, REQUEST_CASES["minimal"], job.REQUEST_ENVELOPE_V1)
        for shared, old in (
            (lambda: adapter.check_request(request), lambda: job.check_request(request)),
            (lambda: adapter.validate_input(SCHEMA, {"a": 1}), lambda: job.validate_input(SCHEMA, {"a": 1})),
        ):
            expected = outcome(old)
            assert outcome(shared) == expected
            assert expected[1] is fake.ValidatorError


# ---------------------------------------------------------------------------
# Shared values
# ---------------------------------------------------------------------------


class TestInterruptResolution:
    @pytest.mark.parametrize("product", ["job_model", "cpk_model"])
    def test_same_fields_defaults_and_flags(self, legacy, product):
        old = getattr(legacy, product).InterruptResolution
        new = values.InterruptResolution

        def shape(cls):
            return [
                (f.name, f.default, f.compare, f.init, f.default_factory is dataclasses.MISSING)
                for f in dataclasses.fields(cls)
            ]

        assert shape(new) == shape(old)
        assert new.__dataclass_params__.frozen is old.__dataclass_params__.frozen
        assert new.__name__ == old.__name__

    @pytest.mark.parametrize("product", ["job_model", "cpk_model"])
    def test_behaviour(self, legacy, product):
        old = getattr(legacy, product).InterruptResolution
        new = values.InterruptResolution
        args = ("5", "answered", 3.5)
        kwargs = {"input": {"a": 1}, "reason": None}
        a, b = old(*args, **kwargs), new(*args, **kwargs)
        assert dataclasses.astuple(a) == dataclasses.astuple(b)
        assert repr(a) == repr(b)
        assert (a == old(*args, **kwargs)) is (b == new(*args, **kwargs))
        replayed_old, replayed_new = old(*args, replayed=True), new(*args, replayed=True)
        assert (replayed_old == old(*args)) is True
        assert (replayed_new == new(*args)) is True
        for mutate in (
            lambda r: setattr(r, "outcome", "x"),
            lambda r: setattr(r, "extra", 1),
        ):
            assert outcome(lambda: mutate(a)) [1:3] == outcome(lambda: mutate(b))[1:3]
        for bad in ((), ("5",), ("5", "answered")):
            assert outcome(lambda: old(*bad))[2].split("'")[0] == outcome(lambda: new(*bad))[2].split("'")[0]

    def test_to_mapping_matches_job_kit(self, legacy):
        old = legacy.job_model.InterruptResolution("5", "rejected", 1.5, reason="no")
        new = values.InterruptResolution("5", "rejected", 1.5, reason="no")
        assert new.to_mapping() == old.to_mapping()
        assert list(new.to_mapping()) == list(old.to_mapping())


# ---------------------------------------------------------------------------
# Documented divergences
# ---------------------------------------------------------------------------


class TestDocumentedDivergences:
    def test_event_probe_non_container_schemas(self, legacy, monkeypatch, tmp_path):
        """job_kit leaked TypeError for a non-container; shared (and cpk) refuse."""
        apply_event_environment(monkeypatch, tmp_path, "schemas-none")
        old = outcome(legacy.job_events._execution_event)
        new = outcome(lambda: event_probe.probe_execution_event(job_event_spec(legacy)))
        assert old[:2] == ("raise", legacy.job_events.ExecutionEventSupportError) or old[1] is TypeError
        assert new[1] is legacy.job_events.ExecutionEventSupportError
        with monkeypatch.context() as mp:
            install(mp, "bootstrap_lib.execution_event", self._schemas_int(legacy))
            old = outcome(legacy.job_events._execution_event)
            new = outcome(lambda: event_probe.probe_execution_event(job_event_spec(legacy)))
        assert old[1] is TypeError
        assert new[1] is legacy.job_events.ExecutionEventSupportError
        assert "predates or lacks" in new[2]

    @staticmethod
    def _schemas_int(legacy):
        fake = clone(legacy.event_module)
        fake.SUPPORTED_SCHEMAS = 5
        return fake

    def test_validator_subsets_as_a_string(self, legacy, monkeypatch):
        fake = clone(legacy.lsk_schema)
        fake.SUPPORTED_SUBSETS = schema_validator.VALIDATOR_SUBSET
        install(monkeypatch, _LSK_VALIDATOR, fake)
        old = outcome(legacy.job_interrupts._schema_validator)
        new = outcome(InterruptAdapter(job_adapter_spec(legacy)).validator)
        assert old == ("module", id(fake))
        assert new[1] is legacy.job_interrupts.JsonSchemaSupportError
        # pipeline execution already refused it, and still does.
        assert outcome(legacy.cpk_interrupts._schema_validator)[1] is legacy.cpk_interrupts.InterruptSupportError
        assert outcome(InterruptAdapter(cpk_adapter_spec(legacy)).validator) == outcome(
            legacy.cpk_interrupts._schema_validator
        )


# ---------------------------------------------------------------------------
# llm-kit validator and the legacy validator behave the same under the contract
# ---------------------------------------------------------------------------


def test_llm_kit_and_legacy_validators_agree_under_the_contract(legacy):
    for value in ({"a": 1}, {"a": "x"}, {}, {"a": 1, "z": 1}):
        old = outcome(lambda: job_adapter(legacy).validate_input(SCHEMA, value))
        new = outcome(lambda: job_adapter(legacy, llm_kit=True).validate_input(SCHEMA, value))
        assert old == new


def test_execution_package_names_no_legacy_module():
    package = Path(__file__).resolve().parents[2] / "packages" / "katsoft-llm-kit" / "src" / "llm_kit" / "execution"
    text = "\n".join(path.read_text(encoding="utf-8") for path in package.glob("*.py"))
    for forbidden in ("_copy", "job_kit", "content_pipeline", "from llm_scripting_kit", "import llm_scripting_kit"):
        assert forbidden not in text, forbidden
    # The only legacy validator spelling is the documentation of bootstrap's constant.
    assert text.count("llm_scripting_kit.completion.json_schema") == 1
    assert "llm_kit.schema.json_schema" in text


def test_every_execution_module_is_ascii():
    package = Path(__file__).resolve().parents[2] / "packages" / "katsoft-llm-kit" / "src" / "llm_kit" / "execution"
    for path in package.glob("*.py"):
        path.read_text(encoding="ascii")


# ---------------------------------------------------------------------------
# The environments really reach the refusals (a fixture that patched nothing
# would make every comparison above agree on "ok")
# ---------------------------------------------------------------------------


def _messages(apply, call, tmp_path, environments):
    found = {}
    for environment in environments:
        directory = tmp_path / environment
        directory.mkdir(parents=True)
        with pytest.MonkeyPatch.context() as mp:
            apply(mp, directory, environment)
            result = outcome(call)
        found[environment] = result
    return found


class TestEnvironmentsReachRefusals:
    def test_event_environments(self, legacy, tmp_path):
        job = _messages(apply_event_environment, legacy.job_events._execution_event, tmp_path / "j", EVENT_ENVIRONMENTS)
        cpk = _messages(apply_event_environment, legacy.cpk_events._execution_event, tmp_path / "c", EVENT_ENVIRONMENTS)
        v2 = _messages(apply_event_environment, legacy.cpk_events._execution_event_v2, tmp_path / "v", EVENT_ENVIRONMENTS)
        assert job["real"][0] == cpk["real"][0] == v2["real"][0] == "module"
        assert sum(1 for r in job.values() if r[0] == "raise") >= 14
        assert len({r[2] for r in job.values() if r[0] == "raise"}) >= 3
        assert len({r[2] for r in cpk.values() if r[0] == "raise"}) >= 9
        # v2-only refusals: the base probe passes, the v2 stage refuses.
        only_v2 = [e for e in EVENT_ENVIRONMENTS if cpk[e][0] != "raise" and v2[e][0] == "raise"]
        assert "schemas-v1-only" in only_v2 and "make_event-no-schema-keyword" in only_v2

    def test_contract_environments(self, legacy, tmp_path):
        job = _messages(apply_contract_environment, legacy.job_interrupts._interrupt_contract, tmp_path / "j", CONTRACT_ENVIRONMENTS)
        cpk = _messages(apply_contract_environment, legacy.cpk_interrupts._interrupt_contract, tmp_path / "c", CONTRACT_ENVIRONMENTS)
        assert job["real"][0] == cpk["real"][0] == "module"
        assert len({r[2] for r in cpk.values() if r[0] == "raise"}) >= 20
        # The two products refuse different sets: job-only names pass in cpk and back.
        assert job["no-canonical_json"][0] == "module" and cpk["no-canonical_json"][0] == "raise"
        assert job["no-parse_request_document"][0] == "raise" and cpk["no-parse_request_document"][0] == "module"
        assert job["no-error-RequestError"][0] == "module" and cpk["no-error-RequestError"][0] == "raise"

    def test_validator_environments(self, legacy, tmp_path):
        job = _messages(apply_validator_environment, legacy.job_interrupts._schema_validator, tmp_path / "j", VALIDATOR_ENVIRONMENTS)
        cpk = _messages(apply_validator_environment, legacy.cpk_interrupts._schema_validator, tmp_path / "c", VALIDATOR_ENVIRONMENTS)
        assert job["real"][0] == cpk["real"][0] == "module"
        assert len({r[2] for r in cpk.values() if r[0] == "raise"}) >= 7
        assert len({r[2] for r in job.values() if r[0] == "raise"}) >= 2
        passing = {"real", "intact", "subsets-tuple"}
        assert {e for e, r in job.items() if r[0] == "module"} == passing
        assert {e for e, r in cpk.items() if r[0] == "module"} == passing
