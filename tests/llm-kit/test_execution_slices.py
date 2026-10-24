"""Join test: the execution product modules say what the legacy products say.

``test_execution_parity.py`` pins ``llm_kit.execution`` to the legacy products
through reference wording held in that file. This file closes the other half:
the product wording, error classes, floors and literals live in the product
modules (``llm_kit.jobs`` and ``llm_kit.pipeline.execution`` events and
interrupts), so those modules are driven directly against the legacy ones.

- Every render function equals the reference wording in the parity file, failure
  by failure, in its final wording. A changed word goes red here, because the
  legacy suites assert only fragments of these messages.
- The probes refuse exactly as the legacy probes do across the same
  environments, except the documented divergences.
- Floors and literals equal the legacy constants, except the named differences
  in ``final_wording`` (the validator floor and owner name llm-kit).
"""

from __future__ import annotations

import importlib
import itertools

import pytest

import test_execution_parity as P
from test_execution_parity import legacy  # noqa: F401 - the module-scoped fixture

from llm_kit.execution import probe
from llm_kit.execution.probe import ProbeFailure

from llm_kit.jobs import events as job_events
from llm_kit.jobs import interrupts as job_interrupts
from llm_kit.pipeline.execution import events as cpk_events
from llm_kit.pipeline.execution import interrupts as cpk_interrupts

from final_wording import floor_to_final, floors_in_text_to_final, to_final

_FINAL_VALIDATOR = "llm_kit.schema.json_schema"


def by_name(result):
    """An outcome with its exception class replaced by the class name."""
    if result[0] == "raise":
        message = floors_in_text_to_final(to_final(result[2]))
        return ("raise", result[1].__name__, message, *result[3:])
    return result


def _failures():
    subjects = ("subject", P.event_probe.SCHEMA_V2, P.event_probe.SCHEMA_V1)
    for kind, stage, subject in itertools.product(
        sorted(probe.KINDS), (probe.STAGE_BASE, probe.STAGE_V2), subjects
    ):
        yield ProbeFailure(kind, subject, "7.7.7", "detail (x)", stage)


@pytest.mark.parametrize(
    "render, reference",
    [
        (job_events._render_support_failure, P.job_event_render),
        (cpk_events._render_support_failure, P.cpk_event_render),
        (job_interrupts._render_contract_failure, P.job_contract_render),
        (job_interrupts._render_validator_failure, P.job_validator_render),
        (cpk_interrupts._render_contract_failure, P.cpk_contract_render),
        (cpk_interrupts._render_validator_failure, P.cpk_validator_render),
    ],
    ids=["job-event", "cpk-event", "job-contract", "job-validator", "cpk-contract", "cpk-validator"],
)
def test_render_functions_equal_the_reference_wording(render, reference):
    count = 0
    for failure in _failures():
        assert render(failure) == floors_in_text_to_final(to_final(reference(failure))), failure
        count += 1
    assert count == len(probe.KINDS) * 2 * 3


class TestEventProbes:
    @pytest.mark.parametrize("environment", P.EVENT_ENVIRONMENTS)
    def test_job(self, legacy, monkeypatch, tmp_path, environment):  # noqa: F811
        P.apply_event_environment(monkeypatch, tmp_path, environment)
        if environment in P._JOB_EVENT_DIVERGENT:
            pytest.skip("documented divergence, asserted in test_execution_parity")
        expected = by_name(P.outcome(legacy.job_events._execution_event))
        assert by_name(P.outcome(job_events._execution_event)) == expected

    @pytest.mark.parametrize("environment", P.EVENT_ENVIRONMENTS)
    def test_cpk(self, legacy, monkeypatch, tmp_path, environment):  # noqa: F811
        P.apply_event_environment(monkeypatch, tmp_path, environment)
        expected = by_name(P.outcome(legacy.cpk_events._execution_event))
        assert by_name(P.outcome(cpk_events._execution_event)) == expected

    @pytest.mark.parametrize("environment", P.EVENT_ENVIRONMENTS)
    def test_cpk_v2_stage(self, legacy, monkeypatch, tmp_path, environment):  # noqa: F811
        P.apply_event_environment(monkeypatch, tmp_path, environment)
        expected = by_name(P.outcome(legacy.cpk_events._execution_event_v2))
        assert by_name(P.outcome(cpk_events._execution_event_v2)) == expected


class TestContractProbes:
    @pytest.mark.parametrize("environment", P.CONTRACT_ENVIRONMENTS)
    def test_job(self, legacy, monkeypatch, tmp_path, environment):  # noqa: F811
        P.apply_contract_environment(monkeypatch, tmp_path, environment)
        expected = by_name(P.outcome(legacy.job_interrupts._interrupt_contract))
        assert by_name(P.outcome(job_interrupts._interrupt_contract)) == expected

    @pytest.mark.parametrize("environment", P.CONTRACT_ENVIRONMENTS)
    def test_cpk(self, legacy, monkeypatch, tmp_path, environment):  # noqa: F811
        P.apply_contract_environment(monkeypatch, tmp_path, environment)
        expected = by_name(P.outcome(legacy.cpk_interrupts._interrupt_contract))
        assert by_name(P.outcome(cpk_interrupts._interrupt_contract)) == expected


# The validator is llm_kit.schema.json_schema now, so the package-level
# environments (a separate llm_scripting_kit package that is absent or broken)
# have no counterpart; the module-level ones are the same variants installed
# under the module the slice asks for.
_VALIDATOR_MODULE_ENVIRONMENTS = [
    name for name in P.VALIDATOR_ENVIRONMENTS if name not in P.VALIDATOR_PACKAGE_VARIANTS[:2]
]


def _validator_outcomes(legacy_fn, slice_fn, environment, tmp_path, monkeypatch):
    with monkeypatch.context() as mp:
        P.apply_validator_environment(mp, tmp_path / "legacy", environment)
        expected = by_name(P.outcome(legacy_fn))
    with monkeypatch.context() as mp:
        mp.setattr(P, "_LSK_VALIDATOR", _FINAL_VALIDATOR)
        P.apply_validator_environment(mp, tmp_path / "slice", environment)
        actual = by_name(P.outcome(slice_fn))
    if expected[0] == "raise":
        # the validator module is llm_kit.schema.json_schema in the final package
        expected = (*expected[:2], expected[2].replace(P._LSK_VALIDATOR, _FINAL_VALIDATOR), *expected[3:])
    return expected, actual


class TestValidatorLoaders:
    @pytest.mark.parametrize("environment", _VALIDATOR_MODULE_ENVIRONMENTS)
    def test_job(self, legacy, monkeypatch, tmp_path, environment):  # noqa: F811
        (tmp_path / "legacy").mkdir()
        (tmp_path / "slice").mkdir()
        expected, actual = _validator_outcomes(
            legacy.job_interrupts._schema_validator,
            job_interrupts._schema_validator,
            environment, tmp_path, monkeypatch,
        )
        if expected[0] == "module":
            assert actual[0] == "module" and expected[0] == actual[0]
        else:
            assert actual == expected

    @pytest.mark.parametrize("environment", _VALIDATOR_MODULE_ENVIRONMENTS)
    def test_cpk(self, legacy, monkeypatch, tmp_path, environment):  # noqa: F811
        (tmp_path / "legacy").mkdir()
        (tmp_path / "slice").mkdir()
        expected, actual = _validator_outcomes(
            legacy.cpk_interrupts._schema_validator,
            cpk_interrupts._schema_validator,
            environment, tmp_path, monkeypatch,
        )
        if expected[0] == "module":
            assert actual[0] == "module"
        else:
            assert actual == expected

    def test_both_loaders_name_the_llm_kit_validator(self):
        assert job_interrupts._schema_validator() is importlib.import_module(_FINAL_VALIDATOR)
        assert cpk_interrupts._schema_validator() is importlib.import_module(_FINAL_VALIDATOR)
        assert cpk_interrupts._VALIDATOR_MODULE == _FINAL_VALIDATOR


class TestLiteralsAndFloors:
    def test_job_events(self, legacy):  # noqa: F811
        old, new = legacy.job_events, job_events
        for name in ("PLUGIN", "SCHEMA_V1", "SCHEMA_V2", "REQUIRED_SCHEMAS", "REQUIRED_SCHEMA",
                     "_EXECUTION_EVENT_BOOTSTRAP", "_REQUIRED_CALLABLES", "_MAKE_EVENT_KEYWORDS",
                     "REASON_LIMIT"):
            assert getattr(new, name) == getattr(old, name), name

    def test_cpk_events(self, legacy):  # noqa: F811
        old, new = legacy.cpk_events, cpk_events
        for name in ("PLUGIN", "REQUIRED_SCHEMA", "REQUIRED_SCHEMA_V2", "EXECUTION_EVENT_BOOTSTRAP",
                     "EXECUTION_EVENT_V2_BOOTSTRAP", "_OWNER", "_REQUIRED_CALLABLES",
                     "_MAKE_EVENT_KEYWORDS"):
            assert getattr(new, name) == getattr(old, name), name

    def test_job_interrupts(self, legacy):  # noqa: F811
        old, new = legacy.job_interrupts, job_interrupts
        for name in ("REQUEST_ENVELOPE_V1", "RESOLUTION_ENVELOPE_V1", "REQUEST_ENVELOPES",
                     "REQUEST_FILE_LIMIT", "INPUT_LIMIT", "KIND_LIMIT", "EXPIRES_IN_S_MAX",
                     "_INTERRUPT_CONTRACT_BOOTSTRAP", "_CONTRACT_V1", "_OWNER"):
            assert getattr(new, name) == getattr(old, name), name
        assert new._JSON_SCHEMA_LSK_VERSION == floor_to_final(old._JSON_SCHEMA_LSK_VERSION)
        assert new._CONTRACT_CALL_SHAPES == old._CONTRACT_CALL_SHAPES

    def test_cpk_interrupts(self, legacy):  # noqa: F811
        old, new = legacy.cpk_interrupts, cpk_interrupts
        for name in ("OWNER", "CONTRACT", "REQUEST_ENVELOPE", "REQUEST_ENVELOPES",
                     "RESOLUTION_ENVELOPE", "VALIDATOR_SUBSET", "INPUT_LIMIT", "KIND_LIMIT",
                     "EXPIRES_IN_S_MAX", "REASON_LIMIT", "INTERRUPT_CONTRACT_BOOTSTRAP",
                     "_CONTRACT_OWNER", "_CONTRACT_MODULE", "_CONTRACT_CALLS", "_CONTRACT_ERRORS",
                     "_VALIDATOR_CALLS"):
            assert getattr(new, name) == getattr(old, name), name
        # named differences (final_wording): the validator is llm-kit's own
        assert new.JSON_SCHEMA_LSK_VERSION == floor_to_final(old.JSON_SCHEMA_LSK_VERSION)
        assert new._VALIDATOR_OWNER == to_final(old._VALIDATOR_OWNER) == "llm-kit@plugins-kit"
        assert cpk_interrupts._VALIDATOR_MODULE == _FINAL_VALIDATOR
        assert old._VALIDATOR_MODULE == P._LSK_VALIDATOR


class TestImporterSeam:
    """The probes import through the product's seam when it supplies one."""

    @staticmethod
    def _absent(calls):
        def importer(name):
            calls.append(name)
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)

        return importer

    def test_event_probe(self):
        calls = []
        spec = P.EventProbeSpec(
            plugin="p", error=ImportError, render=lambda f: f.kind, floor="1.0.0",
            required_schemas=("s",), required_callables=(), shapes=(),
            importer=self._absent(calls),
        )
        with pytest.raises(ImportError, match=probe.ABSENT):
            P.event_probe.probe_execution_event(spec)
        assert calls == ["bootstrap_lib"]

    def test_contract_probe(self):
        calls = []
        spec = P.ContractProbeSpec(
            error=ImportError, render=lambda f: f.kind, shapes=(P.CallShape("x"),),
            importer=self._absent(calls),
        )
        with pytest.raises(ImportError, match=probe.ABSENT):
            P.contract_probe.probe_interrupt_contract(spec)
        assert calls == ["bootstrap_lib"]

    def test_validator_probe_names_package_then_module(self):
        calls = []

        def importer(name):
            calls.append(name)
            if name == "llm_kit":
                return object()
            raise ImportError(name)

        spec = P.ValidatorProbeSpec(
            error=ImportError, render=lambda f: f.kind, floor="1.0.0", importer=importer,
        )
        with pytest.raises(ImportError, match=probe.MODULE_UNIMPORTABLE):
            P.schema_validator.probe_schema_validator(spec)
        assert calls == ["llm_kit", "llm_kit.schema.json_schema"]

    def test_no_importer_means_importlib_looked_up_at_call_time(self, monkeypatch):
        calls = []

        def recorder(name):
            calls.append(name)
            return object()

        monkeypatch.setattr(importlib, "import_module", recorder)
        probe.import_package("some_package", ImportError, lambda f: f.kind, "1.0.0")
        assert probe.import_module("some.module", ImportError, lambda f: f.kind, "1.0.0") is not None
        assert calls == ["some_package", "some.module"]


@pytest.fixture(autouse=True)
def _compatibility_probe_branch(monkeypatch):
    # Slices compare the legacy bootstrap compatibility path; installed package
    # independence is asserted by test_execution_parity and katsoft-libs closure.
    from llm_kit import _runtime
    monkeypatch.setattr(_runtime, "_MODE", "legacy")
