"""The ``bootstrap_lib.interrupt_contract`` probe.

Every durable-interrupt rule -- the request's shape, answer validation, the
decision words, the replay test, expiry arithmetic and the resolution
document -- executes once, in ``bootstrap_lib.interrupt_contract`` (contract
``plugins-kit.interrupt-contract/v1``). The two execution products reach it
only through this probe, which tells an ABSENT contract from a TOO OLD or
STALE one: a store that recorded a wait under rules it could not load would
record an unchecked wait.

A :class:`ContractProbeSpec` holds what differs per product: the call shapes
the product makes (jobs call ``parse_request_document``; pipeline execution
calls ``check_request_mapping``, ``lapsed`` and ``canonical_json``), the error
classes it catches, the error class a refusal raises, and the ``render``
function that words it.

The floor. ``INTERRUPT_CONTRACT_BOOTSTRAP`` is the bootstrap release that
ships the contract with these call shapes; both products name the same one.

Probe order: package import, submodule import, the contract literal, each
required error class, then for each call shape its callable and its binding.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from llm_kit import _runtime

from . import probe
from .probe import CallShape, Importer, ProbeFailure, Render

PACKAGE = "bootstrap_lib"
MODULE = "bootstrap_lib.interrupt_contract"

#: The contract revision both products are written against.
CONTRACT_V1 = "plugins-kit.interrupt-contract/v1"

#: The bootstrap release that ships ``bootstrap_lib.interrupt_contract``.
INTERRUPT_CONTRACT_BOOTSTRAP = "0.137.0"

# The call shapes the products make, one constant each so a product can list
# them in the order it probes them (the first missing name is the one reported).
CHECK_REQUEST_SHAPE = CallShape(
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
)
VALIDATE_INPUT_SHAPE = CallShape("validate_input", 2, ("validator",))
DECISION_OUTCOME_SHAPE = CallShape("decision_outcome", 1, ("input", "reason"))
SAME_RESOLUTION_SHAPE = CallShape(
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
)
EXPIRY_SHAPE = CallShape("expiry", 2, ())
BOUND_REASON_SHAPE = CallShape("bound_reason", 1, ())
RESOLUTION_DOCUMENT_SHAPE = CallShape(
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
)

#: The shapes both products make.
SHARED_SHAPES: tuple[CallShape, ...] = (
    CHECK_REQUEST_SHAPE,
    VALIDATE_INPUT_SHAPE,
    DECISION_OUTCOME_SHAPE,
    SAME_RESOLUTION_SHAPE,
    EXPIRY_SHAPE,
    BOUND_REASON_SHAPE,
    RESOLUTION_DOCUMENT_SHAPE,
)

#: ``parse_request_document``: a request read from file bytes (jobs).
PARSE_REQUEST_DOCUMENT_SHAPE = CallShape(
    "parse_request_document", 1, ("accepted_envelopes", "owner", "validator")
)

#: ``check_request_mapping``: a request read from a mapping (pipeline execution).
CHECK_REQUEST_MAPPING_SHAPE = CallShape(
    "check_request_mapping", 1, ("accepted_envelopes", "owner", "validator")
)

#: ``lapsed`` and ``canonical_json`` (pipeline execution delegates to both).
LAPSED_SHAPE = CallShape("lapsed", 2, ())
CANONICAL_JSON_SHAPE = CallShape("canonical_json", 1, ())


@dataclass(frozen=True)
class ContractProbeSpec:
    """One product's description of what it needs from the contract."""

    error: type[ImportError]
    render: Render
    shapes: tuple[CallShape, ...]
    floor: str = INTERRUPT_CONTRACT_BOOTSTRAP
    required_errors: tuple[str, ...] = ()
    contract: str = CONTRACT_V1
    package: str = PACKAGE
    module: str = MODULE
    #: The product's import seam; ``None`` is ``importlib.import_module``.
    importer: Optional[Importer] = None

    def __post_init__(self) -> None:
        probe.require_text("ContractProbeSpec", "floor", self.floor)
        probe.require_text("ContractProbeSpec", "contract", self.contract)
        if not self.shapes:
            raise ValueError("ContractProbeSpec.shapes must name a call shape")


def probe_interrupt_contract(spec: ContractProbeSpec) -> Any:
    """Return the usable interrupt-contract module, or raise ``spec.error``.

    Package mode returns the routed interrupt contract and probes
    nothing: the pinned dependency guarantees the contract and call shapes.
    """
    if spec.module == MODULE and _runtime.mode() == "package":
        return _runtime.load("interrupt_contract", error=spec.error)
    probe.import_package(
        spec.package, spec.error, spec.render, spec.floor, spec.importer
    )
    module = probe.import_module(
        spec.module, spec.error, spec.render, spec.floor, spec.importer
    )
    if not probe.advertises(getattr(module, "SUPPORTED_CONTRACTS", None), spec.contract):
        probe.refuse(
            spec.error,
            spec.render,
            ProbeFailure(probe.UNSUPPORTED, spec.contract, spec.floor),
        )
    for name in spec.required_errors:
        error = getattr(module, name, None)
        if not (isinstance(error, type) and issubclass(error, Exception)):
            probe.refuse(
                spec.error,
                spec.render,
                ProbeFailure(probe.MISSING_NAME, name, spec.floor),
            )
    for shape in spec.shapes:
        function = probe.require_callable(
            module, shape.name, spec.error, spec.render, spec.floor
        )
        probe.require_binding(function, shape, spec.error, spec.render, spec.floor)
    return module
