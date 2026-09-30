"""Interrupt rules for the execution store: probes, adapters and read helpers.

A unit waits on an interrupt when its claim holder asks a person a typed
question (``ExecutionStore.request_interrupt``). The rules of that wait --
the request's shape, every field rule and limit, the canonical form, the
decision and outcome words, the replay test, the lapse rule and the
resolution document -- are the shared interrupt contract
``plugins-kit.interrupt-contract/v1``, implemented once in
``bootstrap_lib.interrupt_contract``. Request schemas are checked and answers
validated with ``llm_scripting_kit.completion.json_schema`` under its frozen
subset ``llm-scripting-kit.json-schema-subset/v1``. This module is the only
place ``content_pipeline`` reaches either one. The store calls the functions
here and never the contract.

Both edges are reached lazily, inside the functions, because
``content_pipeline`` runs in project interpreters that may link neither.
The three writing verbs (``request_interrupt``, ``resolve_interrupt``,
``expire_interrupts``) call :func:`support` before they open a transaction,
so a machine without the contract or the validator gets
:class:`InterruptSupportError` with a diagnosis and nothing is written: a
wait recorded without the contract would be read as a checked wait. Nothing
else needs them. Importing this module, reading interrupts
(``list_interrupts``, ``get_interrupt``, ``open_interrupt``,
:func:`waiting_units`, :func:`unit_resolutions`, ``InterruptRecord.lapsed``),
the status digest, the wave functions and the event projection work without
either edge.

The literals and limits below are copies, held here so this module imports
with neither edge present. ``bootstrap_lib.interrupt_contract`` holds the
normative values and executes every rule.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any, Dict, List, Mapping, Optional, Tuple

from content_pipeline.execution.model import (
    INTERRUPT_REQUEST_ENVELOPE,
    InterruptRecord,
    InterruptRequest,
    InterruptRequestError,
    ResolutionInputError,
    UnitRecord,
    UnitState,
)

OWNER = "content-pipeline-kit"

# The contract revision these adapters are written against.
CONTRACT = "plugins-kit.interrupt-contract/v1"

# The request `schema` literal this store accepts, and the only one.
REQUEST_ENVELOPE = INTERRUPT_REQUEST_ENVELOPE
REQUEST_ENVELOPES = frozenset({REQUEST_ENVELOPE})

# The `schema` literal of a resolution document this store writes.
RESOLUTION_ENVELOPE = "plugins-kit.interrupt-resolution/v1"

# The JSON Schema subset the contract requires of the validator.
VALIDATOR_SUBSET = "llm-scripting-kit.json-schema-subset/v1"

# The largest resolution input, in bytes of canonical JSON.
INPUT_LIMIT = 65536

# The longest interrupt `kind`.
KIND_LIMIT = 64

# The largest `expires_in_s`.
EXPIRES_IN_S_MAX = 2**31 - 1

# The longest recorded rejection reason, in characters.
REASON_LIMIT = 2000

# The bootstrap version that ships `bootstrap_lib.interrupt_contract` with the
# call shapes bound below. Messages name this constant, never a value read
# from a possibly stale module.
INTERRUPT_CONTRACT_BOOTSTRAP = "0.137.0"

# The llm-scripting-kit version that ships the validator's frozen subset
# marker and its `subset=` selector.
JSON_SCHEMA_LSK_VERSION = "0.56.0"

_CONTRACT_OWNER = "bootstrap@plugins-kit"
_VALIDATOR_OWNER = "llm-scripting-kit@plugins-kit"
_CONTRACT_MODULE = "bootstrap_lib.interrupt_contract"
_VALIDATOR_MODULE = "llm_scripting_kit.completion.json_schema"

# Every contract call this module makes, as (name, positional count, keywords).
# The probe binds each one, so an installed module that lacks a keyword this
# module passes is refused with a diagnosis instead of a TypeError mid-verb.
_CONTRACT_CALLS: Tuple[Tuple[str, int, Tuple[str, ...]], ...] = (
    ("canonical_json", 1, ()),
    (
        "check_request",
        0,
        (
            "envelope",
            "kind",
            "request_schema",
            "payload",
            "expires_in_s",
            "accepted_envelopes",
            "owner",
            "validator",
        ),
    ),
    ("check_request_mapping", 1, ("accepted_envelopes", "owner", "validator")),
    ("validate_input", 2, ("validator",)),
    ("decision_outcome", 1, ("input", "reason")),
    (
        "same_resolution",
        0,
        (
            "stored_outcome",
            "stored_input_json",
            "stored_reason",
            "outcome",
            "input",
            "reason",
        ),
    ),
    ("expiry", 2, ()),
    ("lapsed", 2, ()),
    ("bound_reason", 1, ()),
    (
        "resolution_document",
        0,
        (
            "resolution_envelope",
            "interrupt_id",
            "kind",
            "outcome",
            "input",
            "payload",
            "resolved_at",
        ),
    ),
)

# The contract's error classes this module catches.
_CONTRACT_ERRORS = ("RequestError", "InputError", "DecisionError", "ValidatorError")

# The two validator calls the contract makes, in the same form.
_VALIDATOR_CALLS: Tuple[Tuple[str, int, Tuple[str, ...]], ...] = (
    ("check_schema", 1, ("subset",)),
    ("validate", 2, ("subset",)),
)


class InterruptSupportError(ImportError):
    """The interrupt contract or the schema validator is absent or too old."""


def _import_module(name: str) -> Any:
    return importlib.import_module(name)


def _binds(function: Any, positional: int, keywords: Tuple[str, ...]) -> bool:
    try:
        inspect.signature(function).bind(
            *([None] * positional), **{name: None for name in keywords}
        )
    except (TypeError, ValueError):
        return False
    return True


def _contract_too_old(reason: str) -> InterruptSupportError:
    return InterruptSupportError(
        f"content-pipeline-kit interrupts need bootstrap "
        f"{INTERRUPT_CONTRACT_BOOTSTRAP} or newer: {reason}. Run "
        f"`claude plugin update {_CONTRACT_OWNER}` and restart the session."
    )


def _interrupt_contract() -> Any:
    """Return ``bootstrap_lib.interrupt_contract`` after checking it is usable.

    Three states: ``bootstrap_lib`` absent (install message), too old or
    stale (update message naming this module's own version constant), or
    usable. Usable means the module supports :data:`CONTRACT`, has the error
    classes this module catches, and accepts every call this module makes.
    """
    try:
        _import_module("bootstrap_lib")
    except ModuleNotFoundError as exc:
        if exc.name in (None, "bootstrap_lib"):
            raise InterruptSupportError(
                "content-pipeline-kit interrupts need bootstrap_lib, which this "
                f"interpreter cannot import. Run `claude plugin install "
                f"{_CONTRACT_OWNER}`."
            ) from exc
        raise _contract_too_old(f"bootstrap_lib failed to import ({exc})") from exc
    try:
        module = _import_module(_CONTRACT_MODULE)
    except ImportError as exc:
        raise _contract_too_old(f"{_CONTRACT_MODULE} does not import") from exc
    contracts = getattr(module, "SUPPORTED_CONTRACTS", None)
    try:
        supported = CONTRACT in contracts
    except TypeError:
        supported = False
    if not supported:
        raise _contract_too_old(f"the installed module does not support {CONTRACT}")
    for name in _CONTRACT_ERRORS:
        error = getattr(module, name, None)
        if not (isinstance(error, type) and issubclass(error, Exception)):
            raise _contract_too_old(f"the installed module has no {name}")
    for name, positional, keywords in _CONTRACT_CALLS:
        function = getattr(module, name, None)
        if not callable(function):
            raise _contract_too_old(f"the installed module has no {name}")
        if not _binds(function, positional, keywords):
            raise _contract_too_old(
                f"the installed {name} does not accept this call shape"
            )
    return module


def _validator_too_old(reason: str) -> InterruptSupportError:
    return InterruptSupportError(
        f"content-pipeline-kit interrupts need llm-scripting-kit "
        f"{JSON_SCHEMA_LSK_VERSION} or newer: {reason}. Run "
        f"`claude plugin update {_VALIDATOR_OWNER}` and restart the session."
    )


def _schema_validator() -> Any:
    """Return the usable schema validator module, or raise a diagnosis.

    Absent (``llm_scripting_kit`` does not import) and too old or stale (the
    submodule missing, the subset marker missing or without
    :data:`VALIDATOR_SUBSET`, or either call not accepting ``subset=``) give
    different messages with different remedies.
    """
    try:
        _import_module("llm_scripting_kit")
    except ModuleNotFoundError as exc:
        if exc.name in (None, "llm_scripting_kit"):
            raise InterruptSupportError(
                "content-pipeline-kit interrupts validate requests and answers "
                "with llm_scripting_kit, which this interpreter cannot import. "
                f"Run `claude plugin install {_VALIDATOR_OWNER}`."
            ) from exc
        raise _validator_too_old(f"llm_scripting_kit failed to import ({exc})") from exc
    try:
        module = _import_module(_VALIDATOR_MODULE)
    except ImportError as exc:
        raise _validator_too_old(f"{_VALIDATOR_MODULE} does not import") from exc
    subsets = getattr(module, "SUPPORTED_SUBSETS", None)
    if not isinstance(subsets, (frozenset, set, tuple, list)) or VALIDATOR_SUBSET not in subsets:
        raise _validator_too_old(
            f"the installed validator does not advertise {VALIDATOR_SUBSET}"
        )
    for name, positional, keywords in _VALIDATOR_CALLS:
        function = getattr(module, name, None)
        if not callable(function):
            raise _validator_too_old(f"the installed validator has no {name}")
        if not _binds(function, positional, keywords):
            raise _validator_too_old(
                f"the installed {name} does not accept this call shape"
            )
    return module


def support() -> Tuple[Any, Any]:
    """Probe both edges; return ``(contract module, validator module)``.

    Raises :class:`InterruptSupportError` when either is absent or too old.
    The writing verbs call this before they open a transaction.
    """
    return _interrupt_contract(), _schema_validator()


# -- adapters: every rule executes in the contract ---------------------------


def _as_request(checked: Mapping[str, Any]) -> InterruptRequest:
    return InterruptRequest(
        kind=checked["kind"],
        request_schema=checked["request_schema"],
        payload=checked["payload"],
        expires_in_s=checked["expires_in_s"],
        envelope=checked["envelope"],
    )


def check_request(request: InterruptRequest) -> InterruptRequest:
    """Validate a request; return it with canonical deep copies.

    Raises :class:`~content_pipeline.execution.model.InterruptRequestError`
    naming the first fault, and :class:`InterruptSupportError` when an edge is
    unusable.
    """
    contract, validator = support()
    if not isinstance(request, InterruptRequest):
        raise InterruptRequestError(
            f"interrupt request must be an InterruptRequest, got {type(request).__name__}"
        )
    try:
        checked = contract.check_request(
            envelope=request.envelope,
            kind=request.kind,
            request_schema=request.request_schema,
            payload=request.payload,
            expires_in_s=request.expires_in_s,
            accepted_envelopes=REQUEST_ENVELOPES,
            owner=OWNER,
            validator=validator,
        )
    except contract.ValidatorError as exc:
        raise _validator_too_old(str(exc)) from exc
    except contract.RequestError as exc:
        raise InterruptRequestError(str(exc)) from exc
    return _as_request(checked)


def request_from_mapping(raw: Any) -> InterruptRequest:
    """Build a validated request from a mapping with the request's own keys.

    The keys are exactly ``schema``, ``kind``, ``request_schema`` and
    ``payload``, plus an optional ``expires_in_s``. Raises as
    :func:`check_request` does.
    """
    contract, validator = support()
    try:
        checked = contract.check_request_mapping(
            raw,
            accepted_envelopes=REQUEST_ENVELOPES,
            owner=OWNER,
            validator=validator,
        )
    except contract.ValidatorError as exc:
        raise _validator_too_old(str(exc)) from exc
    except contract.RequestError as exc:
        raise InterruptRequestError(str(exc)) from exc
    return _as_request(checked)


def validate_input(schema: Mapping[str, object], value: object) -> str:
    """Validate a resolution input; return its canonical JSON text.

    Raises :class:`~content_pipeline.execution.model.ResolutionInputError`:
    with empty ``errors`` for an input that is not JSON-native or is over
    :data:`INPUT_LIMIT` bytes, and with the validator's ``(json_pointer,
    keyword)`` tuples when the input fails the request schema.
    """
    contract, validator = support()
    try:
        return contract.validate_input(schema, value, validator=validator)
    except contract.ValidatorError as exc:
        raise _validator_too_old(str(exc)) from exc
    except contract.InputError as exc:
        raise ResolutionInputError(str(exc), exc.errors) from exc


def resolution_document(record: InterruptRecord) -> str:
    """The canonical resolution document of a resolved interrupt.

    Built only from the immutable interrupt row and its immutable resolution
    row, so it is byte-identical on every call. Its ``schema`` is
    :data:`RESOLUTION_ENVELOPE`; a caller cannot choose another.
    """
    resolution = record.resolution
    if resolution is None:
        raise ValueError(f"interrupt {record.id} has no resolution")
    return _interrupt_contract().resolution_document(
        resolution_envelope=RESOLUTION_ENVELOPE,
        interrupt_id=record.id,
        kind=record.kind,
        outcome=resolution.outcome,
        input=resolution.input,
        payload=record.payload,
        resolved_at=resolution.resolved_at,
    )


def canonical_json(value: object) -> str:
    """The canonical text of a JSON value: sorted keys, compact, ASCII."""
    return _interrupt_contract().canonical_json(value)


def decision_outcome(decision: object, *, input: object = None, reason: object = None) -> str:
    """The outcome a decision records: ``answer`` or ``reject``.

    Raises ``ValueError`` for an unknown decision, a rejection given an
    input, or an answer given a reason.
    """
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
    """Whether a resolution replays the stored one (false is a conflict)."""
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


def lapsed(expires_at: Optional[float], now: float) -> bool:
    """Whether an interrupt expiring at ``expires_at`` has lapsed at ``now``."""
    return _interrupt_contract().lapsed(expires_at, now)


def bound_reason(reason: object) -> Optional[str]:
    """The reason as recorded: cut to :data:`REASON_LIMIT` characters."""
    return _interrupt_contract().bound_reason(reason)


# -- read helpers: no shared module ------------------------------------------


def waiting_units(store: Any, run_id: str) -> List[UnitRecord]:
    """The units of ``run_id`` that wait on an open interrupt, ordinal order.

    A run with a waiting unit is healthy and not finished: nothing can be
    claimed for that unit until its interrupt is resolved.
    """
    units = sorted(store.list_units(run_id), key=lambda unit: unit.ordinal)
    return [unit for unit in units if unit.state is UnitState.WAITING]


def unit_resolutions(store: Any, run_id: str, unit_id: str) -> List[Dict[str, Any]]:
    """The resolved interrupts of one unit, oldest first.

    Each entry is ``{"interrupt_id", "kind", "outcome", "input", "reason",
    "payload"}``. ``outcome`` is ``answered``, ``rejected`` or ``expired``.
    The attempt that follows a resolution reads this to continue with the
    answer, or to branch on a rejection or a lapse.
    """
    return [
        {
            "interrupt_id": record.id,
            "kind": record.kind,
            "outcome": record.resolution.outcome,
            "input": record.resolution.input,
            "reason": record.resolution.reason,
            "payload": record.payload,
        }
        for record in store.list_interrupts(run_id, unit_id)
        if record.resolution is not None
    ]


__all__ = [
    "CONTRACT",
    "EXPIRES_IN_S_MAX",
    "INPUT_LIMIT",
    "INTERRUPT_CONTRACT_BOOTSTRAP",
    "JSON_SCHEMA_LSK_VERSION",
    "KIND_LIMIT",
    "OWNER",
    "REASON_LIMIT",
    "REQUEST_ENVELOPE",
    "REQUEST_ENVELOPES",
    "RESOLUTION_ENVELOPE",
    "VALIDATOR_SUBSET",
    "InterruptSupportError",
    "bound_reason",
    "canonical_json",
    "check_request",
    "decision_outcome",
    "expiry",
    "lapsed",
    "request_from_mapping",
    "resolution_document",
    "same_resolution",
    "support",
    "unit_resolutions",
    "validate_input",
    "waiting_units",
]
