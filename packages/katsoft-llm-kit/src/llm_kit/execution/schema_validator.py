"""The JSON Schema validator probe for the interrupt contract.

The contract checks request schemas and validates answers with a validator
the CALLER passes in. This probe loads that validator and tells an ABSENT one
from a TOO OLD or STALE one. It is the shared form of the two legacy
``_schema_validator`` loaders.

In llm-kit the validator is ``llm_kit.schema.json_schema``
(:data:`VALIDATOR_MODULE`). Bootstrap's own ``VALIDATOR_MODULE`` constant
still names the legacy ``llm_scripting_kit.completion.json_schema``; it is a
name the contract never imports, and it stays as it is while both plugin
families coexist. A spec may name another package and module (the parity
tests drive the legacy validator this way).

A usable validator advertises the contract's frozen subset literal in
``SUPPORTED_SUBSETS`` (a frozenset, set, tuple or list), and its
``check_schema`` and ``validate`` accept ``subset=``.

``subset`` on the spec is the literal; ``None`` means the caller reads
``VALIDATOR_SUBSET`` from the probed contract and passes it to
:func:`probe_schema_validator` (jobs do this, so one probe call checks both
edges).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from . import probe
from .probe import CallShape, Importer, ProbeFailure, Render

#: The package and module llm-kit's validator lives in.
VALIDATOR_PACKAGE = "llm_kit"
VALIDATOR_MODULE = "llm_kit.schema.json_schema"

#: The frozen JSON Schema subset the interrupt contract requires. This is the
#: contract's own ``VALIDATOR_SUBSET`` literal; the llm-kit validator advertises it.
VALIDATOR_SUBSET = "llm-scripting-kit.json-schema-subset/v1"

CHECK_SCHEMA_SHAPE = CallShape("check_schema", 1, ("subset",))
VALIDATE_SHAPE = CallShape("validate", 2, ("subset",))


@dataclass(frozen=True)
class ValidatorProbeSpec:
    """One product's description of the validator it needs."""

    error: type[ImportError]
    render: Render
    floor: str
    package: str = VALIDATOR_PACKAGE
    module: str = VALIDATOR_MODULE
    subset: Optional[str] = VALIDATOR_SUBSET
    #: The product's import seam; ``None`` is ``importlib.import_module``.
    importer: Optional[Importer] = None

    def __post_init__(self) -> None:
        probe.require_text("ValidatorProbeSpec", "floor", self.floor)
        probe.require_text("ValidatorProbeSpec", "package", self.package)
        probe.require_text("ValidatorProbeSpec", "module", self.module)


def probe_schema_validator(spec: ValidatorProbeSpec, subset: Optional[str] = None) -> Any:
    """Return the usable validator module, or raise ``spec.error``.

    ``subset`` overrides ``spec.subset``; one of them must be set.
    """
    required = subset if subset is not None else spec.subset
    if required is None:
        raise ValueError("a validator probe needs a subset literal")
    probe.import_package(
        spec.package, spec.error, spec.render, spec.floor, spec.importer
    )
    module = probe.import_module(
        spec.module, spec.error, spec.render, spec.floor, spec.importer
    )
    subsets = getattr(module, "SUPPORTED_SUBSETS", None)
    if not isinstance(subsets, (frozenset, set, tuple, list)) or required not in subsets:
        probe.refuse(
            spec.error,
            spec.render,
            ProbeFailure(probe.UNSUPPORTED, required, spec.floor),
        )
    for shape in (CHECK_SCHEMA_SHAPE, VALIDATE_SHAPE):
        function = probe.require_callable(
            module, shape.name, spec.error, spec.render, spec.floor
        )
        probe.require_binding(function, shape, spec.error, spec.render, spec.floor)
    return module
