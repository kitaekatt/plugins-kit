"""The interrupt contract adapters both execution products share.

Each product keeps a thin ``interrupts`` module over
``bootstrap_lib.interrupt_contract``: it probes the contract and the schema
validator, calls the contract, and maps the contract's errors to its own
classes. The calls and the mapping are the same delegation in both; only the
literals, the error classes and the wording of a refusal differ. This module
is that delegation, once, as :class:`InterruptAdapter` over an
:class:`InterruptAdapterSpec`.

What the adapter does NOT hold: the interrupt request and record dataclasses,
the request file read (jobs), the state transitions inside resolve and expire
(each store's own), and message wording. A request is read from any object
with ``envelope``, ``kind``, ``request_schema``, ``payload`` and
``expires_in_s`` attributes; the adapter returns the contract's checked
mapping and the product builds its own dataclass from it.

The probing policy is the product's: ``acquire`` returns ``(contract,
validator)`` and defaults to :meth:`InterruptAdapter.support`, which probes
the contract and then the validator and refuses on either. Jobs pass an
``acquire`` that swaps an unusable validator for a stand-in so a malformed
request is still refused on its own merits first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from . import probe
from .contract_probe import ContractProbeSpec, probe_interrupt_contract
from .schema_validator import ValidatorProbeSpec, probe_schema_validator

#: The largest resolution input, in bytes of canonical JSON.
INPUT_LIMIT = 65536

#: The longest interrupt ``kind``.
KIND_LIMIT = 64

#: The largest ``expires_in_s``.
EXPIRES_IN_S_MAX = 2**31 - 1

#: The longest recorded rejection reason, in characters.
REASON_LIMIT = 2000


@dataclass(frozen=True)
class InterruptAdapterSpec:
    """One product's literals, error classes and probes for the adapter."""

    owner: str
    contract: ContractProbeSpec
    validator: ValidatorProbeSpec
    accepted_envelopes: frozenset[str]
    resolution_envelope: str
    request_error: type[Exception]
    input_error: type[Exception]
    #: Maps the contract's ``ValidatorError`` to the product's refusal. ``None``
    #: lets it propagate (the product probes the validator first, so it cannot
    #: arise).
    map_validator_error: Optional[Callable[[Exception], Exception]] = None
    #: Hash the decision before probing, so an unhashable decision raises
    #: ``TypeError`` ahead of any import.
    hash_decision: bool = False

    def __post_init__(self) -> None:
        probe.require_text("InterruptAdapterSpec", "owner", self.owner)
        probe.require_text(
            "InterruptAdapterSpec", "resolution_envelope", self.resolution_envelope
        )
        if not self.accepted_envelopes:
            raise ValueError("InterruptAdapterSpec.accepted_envelopes must not be empty")


class InterruptAdapter:
    """The contract delegation for one product."""

    def __init__(
        self,
        spec: InterruptAdapterSpec,
        *,
        acquire: Optional[Callable[[], tuple[Any, Any]]] = None,
    ) -> None:
        self.spec = spec
        self._acquire = acquire if acquire is not None else self.support

    # -- probes ---------------------------------------------------------------

    def contract(self) -> Any:
        """The usable interrupt-contract module, or the product's refusal."""
        return probe_interrupt_contract(self.spec.contract)

    def validator(self) -> Any:
        """The usable schema validator, or the product's refusal.

        With no subset literal on the spec, the contract is probed first and
        its ``VALIDATOR_SUBSET`` is the literal, so one call probes both edges.
        """
        subset = self.spec.validator.subset
        if subset is None:
            subset = self.contract().VALIDATOR_SUBSET
        return probe_schema_validator(self.spec.validator, subset)

    def support(self) -> tuple[Any, Any]:
        """Probe the contract, then the validator; return both."""
        return self.contract(), self.validator()

    # -- adapters -------------------------------------------------------------

    def _map(self, contract: Any, exc: Exception) -> Optional[Exception]:
        mapper = self.spec.map_validator_error
        if mapper is not None and isinstance(exc, contract.ValidatorError):
            return mapper(exc)
        return None

    def check_request(
        self, request: Any, *, expected_type: Optional[type] = None
    ) -> Mapping[str, Any]:
        """Validate a request; return the contract's checked mapping.

        The mapping holds ``envelope``, ``kind``, ``request_schema`` and
        ``payload`` as canonical deep copies, and ``expires_in_s``. With
        ``expected_type``, a request of another type is refused AFTER the
        probes, with the request error. Raises the spec's ``request_error``
        naming the first fault.
        """
        contract, validator = self._acquire()
        if expected_type is not None and not isinstance(request, expected_type):
            raise self.spec.request_error(
                f"interrupt request must be an {expected_type.__name__}, "
                f"got {type(request).__name__}"
            )
        try:
            return contract.check_request(
                envelope=request.envelope,
                kind=request.kind,
                request_schema=request.request_schema,
                payload=request.payload,
                expires_in_s=request.expires_in_s,
                accepted_envelopes=self.spec.accepted_envelopes,
                owner=self.spec.owner,
                validator=validator,
            )
        except Exception as exc:
            self._reraise_request(contract, exc)
            raise

    def check_request_mapping(self, raw: Any) -> Mapping[str, Any]:
        """Validate a request given as a mapping with the request's own keys."""
        contract, validator = self._acquire()
        try:
            return contract.check_request_mapping(
                raw,
                accepted_envelopes=self.spec.accepted_envelopes,
                owner=self.spec.owner,
                validator=validator,
            )
        except Exception as exc:
            self._reraise_request(contract, exc)
            raise

    def parse_request_document(self, data: bytes) -> Mapping[str, Any]:
        """Validate a request document given as bytes (jobs read the file)."""
        contract, validator = self._acquire()
        try:
            return contract.parse_request_document(
                data,
                accepted_envelopes=self.spec.accepted_envelopes,
                owner=self.spec.owner,
                validator=validator,
            )
        except Exception as exc:
            self._reraise_request(contract, exc)
            raise

    def _reraise_request(self, contract: Any, exc: Exception) -> None:
        mapped = self._map(contract, exc)
        if mapped is not None:
            raise mapped from exc
        if isinstance(exc, contract.RequestError):
            raise self.spec.request_error(str(exc)) from exc

    def validate_input(self, schema: Mapping[str, object], value: object) -> str:
        """Validate a resolution input; return its canonical JSON text.

        Raises the spec's ``input_error(message, errors)``: with empty
        ``errors`` for an input that is not JSON-native or is over
        :data:`INPUT_LIMIT` bytes, and with the validator's ``(json_pointer,
        keyword)`` tuples when the input fails the request schema.
        """
        contract, validator = self._acquire()
        try:
            return contract.validate_input(schema, value, validator=validator)
        except Exception as exc:
            mapped = self._map(contract, exc)
            if mapped is not None:
                raise mapped from exc
            if isinstance(exc, contract.InputError):
                raise self.spec.input_error(str(exc), exc.errors) from exc
            raise

    def decision_outcome(
        self, decision: object, *, input: object = None, reason: object = None
    ) -> str:
        """The outcome a decision records.

        Raises ``ValueError`` for an unknown decision and for crossed
        arguments.
        """
        if self.spec.hash_decision:
            hash(decision)
        contract = self.contract()
        try:
            return contract.decision_outcome(decision, input=input, reason=reason)
        except contract.DecisionError as exc:
            raise ValueError(str(exc)) from exc

    def same_resolution(
        self,
        *,
        stored_outcome: str,
        stored_input_json: Optional[str],
        stored_reason: Optional[str],
        outcome: str,
        input: object = None,
        reason: object = None,
    ) -> bool:
        """Whether a resolve call replays the stored resolution."""
        return self.contract().same_resolution(
            stored_outcome=stored_outcome,
            stored_input_json=stored_input_json,
            stored_reason=stored_reason,
            outcome=outcome,
            input=input,
            reason=reason,
        )

    def expiry(self, created_at: float, expires_in_s: Optional[int]) -> Optional[float]:
        """When a request recorded at ``created_at`` lapses, or ``None`` for never."""
        return self.contract().expiry(created_at, expires_in_s)

    def lapsed(self, expires_at: Optional[float], now: float) -> bool:
        """Whether an interrupt expiring at ``expires_at`` has lapsed at ``now``."""
        return self.contract().lapsed(expires_at, now)

    def bound_reason(self, reason: object) -> Optional[str]:
        """The reason as recorded: its text cut to the contract's reason limit."""
        return self.contract().bound_reason(reason)

    def canonical_json(self, value: object) -> str:
        """The canonical text of a JSON value, through the contract."""
        return self.contract().canonical_json(value)

    def resolution_document(self, record: Any) -> str:
        """The canonical resolution document for a resolved interrupt.

        ``record`` has ``id``, ``kind``, ``payload`` and a ``resolution`` (with
        ``outcome``, ``input`` and ``resolved_at``). Built only from those
        immutable values, so every call for one interrupt is byte-identical.
        The ``schema`` literal is the spec's ``resolution_envelope``, taken
        from no caller.
        """
        resolution = record.resolution
        if resolution is None:
            raise ValueError(f"interrupt {record.id} has no resolution")
        return self.contract().resolution_document(
            resolution_envelope=self.spec.resolution_envelope,
            interrupt_id=record.id,
            kind=record.kind,
            outcome=resolution.outcome,
            input=resolution.input,
            payload=record.payload,
            resolved_at=resolution.resolved_at,
        )
