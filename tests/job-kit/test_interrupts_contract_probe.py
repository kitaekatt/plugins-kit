"""The two probes job-kit runs before it touches an interrupt: the shared
contract (``bootstrap_lib.interrupt_contract``) and the validator it uses
(``llm_scripting_kit.completion.json_schema`` under the frozen subset).

Absent, too old and stale are three states an import cannot tell apart, so each
probe checks the newest symbol and call shape job-kit uses and names a
different remedy for absent than for too old.
"""

from __future__ import annotations

import ast
import sys
import types
from pathlib import Path
from typing import Any

import pytest

import job_kit.interrupts as interrupts
from job_kit import cli
from job_kit.interrupts import InterruptContractSupportError, JsonSchemaSupportError
from job_kit.run import run_job_file

from bootstrap_lib import interrupt_contract as real_contract
from llm_scripting_kit.completion import json_schema as real_json_schema


INTERRUPTS_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "job-kit"
    / "lib"
    / "job_kit"
    / "interrupts.py"
)

_MISSING = object()

#: One entry per contract callable job-kit calls (the plan's list; the probe's
#: table is checked against it below).
_CALLS = [
    "check_request",
    "parse_request_document",
    "validate_input",
    "decision_outcome",
    "same_resolution",
    "expiry",
    "bound_reason",
    "resolution_document",
]


def _install_fake_contract(monkeypatch: Any, **overrides: Any) -> None:
    fake = types.ModuleType("bootstrap_lib.interrupt_contract")
    for name in real_contract.__all__:
        setattr(fake, name, getattr(real_contract, name))
    for name, value in overrides.items():
        if value is _MISSING:
            delattr(fake, name)
        else:
            setattr(fake, name, value)
    monkeypatch.setitem(sys.modules, "bootstrap_lib.interrupt_contract", fake)


def _install_fake_json_schema(monkeypatch: Any, **overrides: Any) -> None:
    fake = types.ModuleType("llm_scripting_kit.completion.json_schema")
    fake.check_schema = real_json_schema.check_schema  # type: ignore[attr-defined]
    fake.validate = real_json_schema.validate  # type: ignore[attr-defined]
    fake.SUPPORTED_SUBSETS = real_json_schema.SUPPORTED_SUBSETS  # type: ignore[attr-defined]
    for name, value in overrides.items():
        if value is _MISSING:
            delattr(fake, name)
        else:
            setattr(fake, name, value)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.completion.json_schema", fake)


# --------------------------------------------------------------------------
# The contract probe
# --------------------------------------------------------------------------


def test_contract_probe_accepts_the_real_module() -> None:
    assert interrupts._interrupt_contract() is real_contract


def test_contract_probe_table_names_every_call_job_kit_makes() -> None:
    """A call added to the adapter without a bind in the probe is unprobed."""
    assert sorted(interrupts._CONTRACT_CALL_SHAPES) == sorted(_CALLS)
    tree = ast.parse(INTERRUPTS_SOURCE.read_text(encoding="utf-8"))
    called: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        owner = node.func.value
        via_name = isinstance(owner, ast.Name) and owner.id == "contract"
        via_probe = (
            isinstance(owner, ast.Call)
            and isinstance(owner.func, ast.Name)
            and owner.func.id == "_interrupt_contract"
        )
        if via_name or via_probe:
            called.add(node.func.attr)
    assert called == set(_CALLS)


def test_contract_probe_absent_names_install(monkeypatch: Any) -> None:
    monkeypatch.setitem(sys.modules, "bootstrap_lib", None)
    with pytest.raises(InterruptContractSupportError) as excinfo:
        interrupts._interrupt_contract()
    message = str(excinfo.value)
    assert "claude plugin install bootstrap@plugins-kit" in message
    assert "update" not in message
    assert isinstance(excinfo.value, ImportError)


@pytest.mark.parametrize("part", ["module", "marker", "callable"])
def test_contract_probe_too_old_names_the_jk_constant(monkeypatch: Any, part: str) -> None:
    monkeypatch.setattr(interrupts, "_INTERRUPT_CONTRACT_BOOTSTRAP", "9.8.7")
    if part == "module":
        monkeypatch.setitem(sys.modules, "bootstrap_lib.interrupt_contract", None)
    elif part == "marker":
        _install_fake_contract(monkeypatch, SUPPORTED_CONTRACTS=frozenset({"other/v9"}))
    else:
        _install_fake_contract(monkeypatch, bound_reason=_MISSING)
    with pytest.raises(InterruptContractSupportError) as excinfo:
        interrupts._interrupt_contract()
    message = str(excinfo.value)
    assert ">= 9.8.7" in message
    assert "claude plugin update bootstrap@plugins-kit" in message
    assert "install" not in message


def test_contract_probe_messages_differ_and_name_the_real_floor(monkeypatch: Any) -> None:
    monkeypatch.setitem(sys.modules, "bootstrap_lib", None)
    with pytest.raises(InterruptContractSupportError) as absent:
        interrupts._interrupt_contract()
    monkeypatch.undo()
    monkeypatch.setitem(sys.modules, "bootstrap_lib.interrupt_contract", None)
    with pytest.raises(InterruptContractSupportError) as too_old:
        interrupts._interrupt_contract()
    assert str(absent.value) != str(too_old.value)
    assert ">= 0.137.0" in str(too_old.value)


def _unbindable(name: str) -> Any:
    def stand_in() -> None:  # takes no argument: no call job-kit makes can bind
        raise AssertionError(f"{name} must never be called by the probe")

    return stand_in


@pytest.mark.parametrize("call", _CALLS)
def test_contract_probe_rejects_unbindable_call(monkeypatch: Any, call: str) -> None:
    _install_fake_contract(monkeypatch, **{call: _unbindable(call)})
    with pytest.raises(InterruptContractSupportError, match="update"):
        interrupts._interrupt_contract()


def test_contract_probe_rejects_a_renamed_keyword(monkeypatch: Any) -> None:
    """A later, incompatible shape: the same function, ``owner`` renamed."""

    def check_request(
        *, envelope, kind, request_schema, payload, expires_in_s=None,
        accepted_envelopes, store, validator,
    ):  # noqa: ANN001, ANN202
        raise AssertionError("never called")

    _install_fake_contract(monkeypatch, check_request=check_request)
    with pytest.raises(InterruptContractSupportError, match="update"):
        interrupts._interrupt_contract()


# --------------------------------------------------------------------------
# The validator probe, for the call shape the contract uses
# --------------------------------------------------------------------------


@pytest.mark.parametrize("marker", ["missing", "other_subset"])
def test_validator_probe_requires_the_subset_marker(monkeypatch: Any, marker: str) -> None:
    if marker == "missing":
        _install_fake_json_schema(monkeypatch, SUPPORTED_SUBSETS=_MISSING)
    else:
        _install_fake_json_schema(
            monkeypatch, SUPPORTED_SUBSETS=frozenset({"llm-scripting-kit.json-schema-subset/v9"})
        )
    with pytest.raises(JsonSchemaSupportError) as excinfo:
        interrupts._schema_validator()
    message = str(excinfo.value)
    assert ">= 0.56.0" in message
    assert "claude plugin update llm-scripting-kit@plugins-kit" in message
    assert "install" not in message


@pytest.mark.parametrize("function", ["check_schema", "validate"])
def test_validator_probe_binds_the_subset_keyword(monkeypatch: Any, function: str) -> None:
    """A validator still on the anchor shape (no ``subset=``) is refused."""

    def check_schema(schema):  # noqa: ANN001, ANN202
        raise AssertionError("never called")

    def validate(schema, value):  # noqa: ANN001, ANN202
        raise AssertionError("never called")

    _install_fake_json_schema(
        monkeypatch, **{function: {"check_schema": check_schema, "validate": validate}[function]}
    )
    with pytest.raises(JsonSchemaSupportError, match="update"):
        interrupts._schema_validator()


def test_validator_probe_accepts_the_real_module() -> None:
    assert interrupts._schema_validator() is real_json_schema


def test_validator_probe_runs_the_contract_probe_first(monkeypatch: Any) -> None:
    monkeypatch.setitem(sys.modules, "bootstrap_lib.interrupt_contract", None)
    with pytest.raises(InterruptContractSupportError):
        interrupts._schema_validator()


# --------------------------------------------------------------------------
# Both probes run before any ledger is opened
# --------------------------------------------------------------------------


def test_run_refuses_before_creating_the_store_without_the_contract(
    tmp_path: Path, monkeypatch: Any
) -> None:
    jobs_path = tmp_path / "jobs.yaml"
    jobs_path.write_text(
        "jobs:\n"
        "  - id: job\n"
        "    prompt: run\n"
        "    models: [fake-endpoint]\n"
        "    directory: .\n"
        "    contract:\n"
        "      command: [true]\n",
        encoding="utf-8",
    )
    store_path = tmp_path / "ledger" / "runs.sqlite3"
    monkeypatch.setitem(sys.modules, "bootstrap_lib.interrupt_contract", None)
    with pytest.raises(InterruptContractSupportError, match="0.137.0"):
        run_job_file(jobs_path, store_path=store_path)
    assert not store_path.exists()
    assert not store_path.parent.exists()


def test_resolve_refuses_before_opening_the_ledger_without_the_contract(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    missing = tmp_path / "nowhere" / "ledger.sqlite3"
    monkeypatch.setitem(sys.modules, "bootstrap_lib.interrupt_contract", None)
    argv = ["resolve", "run", "1", "--reject", "--store", str(missing)]
    assert cli.main(argv) == cli.EXIT_RUNNER_FAILURE
    err = capsys.readouterr().err
    assert "bootstrap_lib.interrupt_contract" in err
    assert "claude plugin update bootstrap@plugins-kit" in err
    assert not missing.parent.exists()


# --------------------------------------------------------------------------
# Import-time independence
# --------------------------------------------------------------------------


def test_interrupts_has_no_static_bootstrap_lib_import() -> None:
    """``import job_kit`` must succeed without ``bootstrap_lib``: nothing at
    module level, or inside a module-level ``if`` or ``try``, imports it."""
    tree = ast.parse(INTERRUPTS_SOURCE.read_text(encoding="utf-8"))

    def module_level(nodes: list) -> list:
        found = []
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            found.append(node)
            for field in ("body", "orelse", "finalbody", "handlers"):
                found.extend(module_level(getattr(node, field, []) or []))
        return found

    offenders = []
    for node in module_level(tree.body):
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if a.name.split(".")[0] == "bootstrap_lib"]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "bootstrap_lib":
                offenders.append(node.module)
    assert offenders == []
