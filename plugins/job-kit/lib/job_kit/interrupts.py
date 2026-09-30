"""Durable interrupt requests and resolutions: the two file contracts.

A contract asks an operator a question by writing ONE JSON file, the
interrupt request (``job-kit.interrupt-request/v1``), at the path job-kit
exports as ``JOB_KIT_INTERRUPT_REQUEST``. stdout, stderr and exit text are
never read for state. job-kit records the request in its ledger, the job
waits, and an operator's answer is validated against the schema the request
declared. When job-kit re-runs the contract after an answer, it hands it the
resolution document (``job-kit.interrupt-resolution/v1``).

The rules -- the request's shape and field checks, answer validation, the
decision words, the replay test, expiry arithmetic and the resolution
document -- execute in ``bootstrap_lib.interrupt_contract``, the contract
every plugin that implements a wait shares. This module is job-kit's adapter
over it: it keeps job-kit's names, its two frozen envelope literals, its
limits, its error classes and the request file's path handling, and it
reaches the contract only through :func:`_interrupt_contract`, a probe. That
edge is REQUIRED: a job-kit that recorded a wait under rules it could not
load would record an unchecked wait, so an absent or too-old contract
refuses with a diagnosis instead. ``canonical_json`` stays a local stdlib
function so that ``import job_kit`` never needs ``bootstrap_lib``.

Schemas are checked and answers validated with llm-scripting-kit's
stdlib-only closed-subset validator (``llm_scripting_kit.completion.
json_schema``), reached only through :func:`_schema_validator` and handed to
the contract. That edge is REQUIRED for the same reason.
"""

from __future__ import annotations

import importlib
import inspect
import json
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping, Optional

from .model import InterruptRecord, InterruptRequest


#: The request-file schema literal this job-kit accepts. FROZEN; a later
#: envelope version is a separate literal that :func:`parse_request` gains.
REQUEST_ENVELOPE_V1 = "job-kit.interrupt-request/v1"

#: The resolution-document schema literal job-kit writes. FROZEN.
RESOLUTION_ENVELOPE_V1 = "job-kit.interrupt-resolution/v1"

#: Every request envelope this job-kit accepts.
REQUEST_ENVELOPES = frozenset({REQUEST_ENVELOPE_V1})

#: The largest request file, in bytes.
REQUEST_FILE_LIMIT = 131072

#: The largest resolution input, in bytes of canonical JSON.
INPUT_LIMIT = 65536

#: The longest interrupt ``kind``.
KIND_LIMIT = 64

#: The largest ``expires_in_s``.
EXPIRES_IN_S_MAX = 2**31 - 1

#: The llm-scripting-kit release that shipped ``completion.json_schema`` with
#: the frozen subset selector, ``check_schema(schema, subset=)`` and
#: ``validate(schema, value, subset=)``. Messages name this constant, never a
#: value read from a possibly stale module.
_JSON_SCHEMA_LSK_VERSION = "0.56.0"

#: The bootstrap release that shipped ``bootstrap_lib.interrupt_contract`` with
#: the call shapes in :data:`_CONTRACT_CALL_SHAPES`. Messages name this
#: constant, never a value read from a possibly stale module.
_INTERRUPT_CONTRACT_BOOTSTRAP = "0.137.0"

#: The contract revision job-kit needs, held as a literal because
#: ``bootstrap_lib`` may be absent when this module is read.
_CONTRACT_V1 = "plugins-kit.interrupt-contract/v1"

#: The store name a contract refusal uses for this plugin.
_OWNER = "job-kit"

#: Every contract callable job-kit calls, with the exact call shape it uses:
#: positional placeholders, then keyword placeholders. The probe binds each.
_CONTRACT_CALL_SHAPES: dict[str, tuple[tuple, dict[str, None]]] = {
    "check_request": (
        (),
        dict.fromkeys(
            (
                "envelope",
                "kind",
                "request_schema",
                "payload",
                "expires_in_s",
                "accepted_envelopes",
                "owner",
                "validator",
            )
        ),
    ),
    "parse_request_document": (
        (None,),
        dict.fromkeys(("accepted_envelopes", "owner", "validator")),
    ),
    "validate_input": ((None, None), {"validator": None}),
    "decision_outcome": ((None,), {"input": None, "reason": None}),
    "same_resolution": (
        (),
        dict.fromkeys(
            (
                "stored_outcome",
                "stored_input_json",
                "stored_reason",
                "outcome",
                "input",
                "reason",
            )
        ),
    ),
    "expiry": ((None, None), {}),
    "bound_reason": ((None,), {}),
    "resolution_document": (
        (),
        dict.fromkeys(
            (
                "resolution_envelope",
                "interrupt_id",
                "kind",
                "outcome",
                "input",
                "payload",
                "resolved_at",
            )
        ),
    ),
}


class InterruptRequestError(ValueError):
    """An interrupt request file, or a request built by a caller, is invalid."""


class InterruptInputError(ValueError):
    """A resolution input is refused.

    ``errors`` holds the validator's ``(json_pointer, keyword)`` tuples when
    the input failed the request schema, and is empty when the input was
    refused before validation (not JSON-native, or over the size cap).
    """

    def __init__(self, message: str, errors: tuple = ()) -> None:
        self.errors = tuple(errors)
        super().__init__(message)


class JsonSchemaSupportError(ImportError):
    """``llm_scripting_kit.completion.json_schema`` is absent, too old, or stale."""


class InterruptContractSupportError(ImportError):
    """``bootstrap_lib.interrupt_contract`` is absent, too old, or stale."""


def _interrupt_contract() -> ModuleType:
    """Return the usable interrupt-contract module, or raise a diagnosis.

    Absent (``bootstrap_lib`` does not import) and too old or stale (the
    submodule, the contract revision, a callable, or a call shape job-kit
    uses is missing) produce different messages with different remedies.
    """
    try:
        import bootstrap_lib  # noqa: F401
    except ModuleNotFoundError as exc:
        raise InterruptContractSupportError(
            "job-kit checks interrupt requests and resolutions with "
            "bootstrap_lib.interrupt_contract, which is not linked into this "
            "environment: install or enable the bootstrap plugin "
            "(`claude plugin install bootstrap@plugins-kit`) and start a new "
            "session so it links job-kit's shared libs."
        ) from exc
    too_old = (
        "job-kit checks interrupt requests and resolutions with "
        f"bootstrap_lib.interrupt_contract ({_CONTRACT_V1}), which the linked "
        "bootstrap_lib predates or lacks: update the bootstrap plugin to >= "
        f"{_INTERRUPT_CONTRACT_BOOTSTRAP} "
        "(`claude plugin update bootstrap@plugins-kit`) and restart."
    )
    try:
        module = importlib.import_module("bootstrap_lib.interrupt_contract")
    except ImportError as exc:
        raise InterruptContractSupportError(too_old) from exc
    supported = getattr(module, "SUPPORTED_CONTRACTS", None)
    try:
        current = supported is not None and _CONTRACT_V1 in supported
    except TypeError:
        current = False
    if not current:
        raise InterruptContractSupportError(too_old)
    for name, (positional, keywords) in _CONTRACT_CALL_SHAPES.items():
        function = getattr(module, name, None)
        if not callable(function):
            raise InterruptContractSupportError(too_old)
        try:
            inspect.signature(function).bind(*positional, **keywords)
        except (TypeError, ValueError) as exc:
            raise InterruptContractSupportError(too_old) from exc
    return module


def _schema_validator() -> ModuleType:
    """Return the usable json_schema module, or raise a diagnosis.

    Runs :func:`_interrupt_contract` first, so one call before any ledger
    open probes both edges. Absent (``llm_scripting_kit`` does not import) and
    too old or stale (the submodule, the subset marker the contract requires,
    ``check_schema`` or ``validate`` missing, or either call shape the
    contract uses not binding) produce different messages with different
    remedies.
    """
    subset = _interrupt_contract().VALIDATOR_SUBSET
    try:
        importlib.import_module("llm_scripting_kit")
    except ModuleNotFoundError as exc:
        raise JsonSchemaSupportError(
            "job-kit validates interrupt requests with llm-scripting-kit, which "
            "is not linked into this environment: install or enable it "
            "(`claude plugin install llm-scripting-kit@plugins-kit`) and start a "
            "new session so bootstrap links job-kit's shared libs."
        ) from exc
    too_old = (
        "job-kit validates interrupt requests with "
        "llm_scripting_kit.completion.json_schema (check_schema and validate "
        "with the subset= selector), which the linked llm-scripting-kit "
        "predates or lacks: update "
        f"llm-scripting-kit to >= {_JSON_SCHEMA_LSK_VERSION} "
        "(`claude plugin update llm-scripting-kit@plugins-kit`) and restart."
    )
    try:
        module = importlib.import_module("llm_scripting_kit.completion.json_schema")
    except ImportError as exc:
        raise JsonSchemaSupportError(too_old) from exc
    try:
        advertised = getattr(module, "SUPPORTED_SUBSETS", None)
        marked = advertised is not None and subset in advertised
    except TypeError:
        marked = False
    if not marked:
        raise JsonSchemaSupportError(too_old)
    check_schema = getattr(module, "check_schema", None)
    validate = getattr(module, "validate", None)
    if not callable(check_schema) or not callable(validate):
        raise JsonSchemaSupportError(too_old)
    try:
        check_signature = inspect.signature(check_schema)
        validate_signature = inspect.signature(validate)
    except ValueError as exc:  # a callable with no introspectable signature
        raise JsonSchemaSupportError(too_old) from exc
    try:
        check_signature.bind({}, subset=subset)
        validate_signature.bind({}, None, subset=subset)
    except TypeError as exc:
        raise JsonSchemaSupportError(too_old) from exc
    return module


class _UnusableValidator:
    """Stands in for a validator that failed its probe.

    job-kit has always refused a malformed request or a non-JSON-native answer
    on its own merits before it needed the validator, and only then reported
    the validator unusable. The contract reads the validator last as well, so
    handing it this stand-in keeps that order: the stored diagnosis is raised
    the moment a schema is actually checked or an answer validated.
    """

    def __init__(self, contract: ModuleType, error: JsonSchemaSupportError) -> None:
        self.SUPPORTED_SUBSETS = frozenset({contract.VALIDATOR_SUBSET})
        self._error = error

    def check_schema(self, schema: object, *, subset: str) -> None:
        raise self._error

    def validate(self, schema: object, value: object, *, subset: str) -> tuple:
        raise self._error


def _contract_and_validator() -> tuple[ModuleType, Any]:
    """The probed contract, and the validator to hand it (see the stand-in)."""
    contract = _interrupt_contract()
    try:
        validator: Any = _schema_validator()
    except JsonSchemaSupportError as exc:
        validator = _UnusableValidator(contract, exc)
    return contract, validator


def canonical_json(value: object) -> str:
    """The canonical text of a JSON value: sorted keys, compact, ASCII.

    Identical values give byte-identical text, whatever their key order.
    Raises ``ValueError`` for NaN or infinity. This stays a local stdlib
    function, pinned to the contract's by a parity test, so that job-kit
    holds no hidden ``bootstrap_lib`` requirement for a helper.
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def check_request(request: InterruptRequest) -> InterruptRequest:
    """Validate a request built from a file or by a library caller.

    Returns the request with ``request_schema`` and ``payload`` as canonical
    deep copies. Raises :class:`InterruptRequestError` naming the fault,
    :class:`JsonSchemaSupportError` when the validator is unusable, and
    :class:`InterruptContractSupportError` when the contract is.
    """
    contract, validator = _contract_and_validator()
    try:
        checked = contract.check_request(
            envelope=request.envelope,
            kind=request.kind,
            request_schema=request.request_schema,
            payload=request.payload,
            expires_in_s=request.expires_in_s,
            accepted_envelopes=REQUEST_ENVELOPES,
            owner=_OWNER,
            validator=validator,
        )
    except contract.RequestError as exc:
        raise InterruptRequestError(str(exc)) from exc
    return _request_from(checked)


def _request_from(checked: Mapping[str, Any]) -> InterruptRequest:
    return InterruptRequest(
        envelope=checked["envelope"],
        kind=checked["kind"],
        request_schema=checked["request_schema"],
        payload=checked["payload"],
        expires_in_s=checked["expires_in_s"],
    )


def parse_request(path: str | Path) -> InterruptRequest:
    """Read, parse and validate one interrupt request file.

    The file is UTF-8 JSON of at most :data:`REQUEST_FILE_LIMIT` bytes whose
    top-level keys are exactly ``schema``, ``kind``, ``request_schema`` and
    ``payload``, plus an optional ``expires_in_s``. Reading the file and
    wording its ``OSError`` refusal are job-kit's own; the bytes go to the
    contract. Raises :class:`InterruptRequestError` naming the fault.
    """
    file_path = Path(path)
    try:
        with file_path.open("rb") as handle:
            data = handle.read(REQUEST_FILE_LIMIT + 1)
    except OSError as exc:
        raise InterruptRequestError(
            f"interrupt request file cannot be read: {exc}"
        ) from exc
    contract, validator = _contract_and_validator()
    try:
        checked = contract.parse_request_document(
            data,
            accepted_envelopes=REQUEST_ENVELOPES,
            owner=_OWNER,
            validator=validator,
        )
    except contract.RequestError as exc:
        raise InterruptRequestError(str(exc)) from exc
    return _request_from(checked)


def validate_input(schema: Mapping[str, object], value: object) -> str:
    """Validate a resolution input against its request schema.

    Returns the input's canonical JSON text. Raises
    :class:`InterruptInputError`: with empty ``errors`` when the input is not
    JSON-native or its canonical form exceeds :data:`INPUT_LIMIT` bytes, and
    with the validator's ``(json_pointer, keyword)`` tuples verbatim when it
    fails the schema.
    """
    contract, validator = _contract_and_validator()
    try:
        return contract.validate_input(schema, value, validator=validator)
    except contract.InputError as exc:
        raise InterruptInputError(str(exc), exc.errors) from exc


def decision_outcome(
    decision: object, *, input: object = None, reason: object = None
) -> str:
    """The outcome a resolve call records: ``answered`` or ``rejected``.

    Raises ``ValueError`` for an unknown decision and for crossed arguments.
    An unhashable decision raises ``TypeError``, as it always did here.
    """
    hash(decision)
    contract = _interrupt_contract()
    try:
        return contract.decision_outcome(decision, input=input, reason=reason)
    except contract.DecisionError as exc:
        raise ValueError(str(exc)) from exc


def same_resolution(
    *,
    stored_outcome: str,
    stored_input_json: Optional[str],
    stored_reason: Optional[str],
    outcome: str,
    input: object = None,
    reason: object = None,
) -> bool:
    """Whether a resolve call replays the stored resolution."""
    return _interrupt_contract().same_resolution(
        stored_outcome=stored_outcome,
        stored_input_json=stored_input_json,
        stored_reason=stored_reason,
        outcome=outcome,
        input=input,
        reason=reason,
    )


def expiry(created_at: float, expires_in_s: Optional[int]) -> Optional[float]:
    """When a request recorded at ``created_at`` lapses, or ``None`` for never."""
    return _interrupt_contract().expiry(created_at, expires_in_s)


def bound_reason(reason: object) -> Optional[str]:
    """The reason as recorded: its text cut to the contract's reason limit."""
    return _interrupt_contract().bound_reason(reason)


def resolution_document(record: InterruptRecord) -> str:
    """The canonical resolution document for a resolved interrupt.

    Built ONLY from the immutable interrupt row and its immutable resolution
    row (``resolved_at`` is the recorded value), so every run of a
    continuation for one interrupt receives byte-identical text. The written
    ``schema`` literal is :data:`RESOLUTION_ENVELOPE_V1`, fixed here and
    taken from no caller.
    """
    resolution = record.resolution
    if resolution is None:
        raise ValueError(f"interrupt {record.id} has no resolution")
    return _interrupt_contract().resolution_document(
        resolution_envelope=RESOLUTION_ENVELOPE_V1,
        interrupt_id=record.id,
        kind=record.kind,
        outcome=resolution.outcome,
        input=resolution.input,
        payload=record.payload,
        resolved_at=resolution.resolved_at,
    )


__all__ = [
    "EXPIRES_IN_S_MAX",
    "INPUT_LIMIT",
    "KIND_LIMIT",
    "REQUEST_ENVELOPES",
    "REQUEST_ENVELOPE_V1",
    "REQUEST_FILE_LIMIT",
    "RESOLUTION_ENVELOPE_V1",
    "InterruptContractSupportError",
    "InterruptInputError",
    "InterruptRequestError",
    "JsonSchemaSupportError",
    "bound_reason",
    "canonical_json",
    "check_request",
    "decision_outcome",
    "expiry",
    "parse_request",
    "resolution_document",
    "same_resolution",
    "validate_input",
]
