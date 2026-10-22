"""The ``bootstrap_lib.execution_event`` probe and the ``make_event`` call.

Both execution products record or project events in the shared envelope of
``bootstrap_lib.execution_event`` (schema ``plugins-kit.execution-event/v1``;
the ``interrupt`` event alone is written under ``/v2``). Both reach it only
through a probe that tells an ABSENT module from a TOO OLD or STALE one, so a
machine without it gets a diagnosis instead of an ``ImportError`` or a
``TypeError`` in mid-write. This module is that probe, once.

What differs between the products is data, held in an :class:`EventProbeSpec`:

- the event source literal (``plugin``): ``"job-kit"`` for jobs,
  ``"content-pipeline-kit"`` for pipeline execution. FROZEN per product; event
  bytes depend on it.
- the schemas, callables and ``make_event`` call shapes the product needs;
- the bootstrap release the product's wording names (``floor``), and the
  release that ships schema v2 (``v2_floor``);
- the error class a refusal raises and the ``render`` function that words it.

Floors. The probe imposes none. Each spec names its own, and the shared
constants below record the two releases that exist: ``EXECUTION_EVENT_V1_BOOTSTRAP``
(the execution-event module with schema v1) and ``EXECUTION_EVENT_V2_BOOTSTRAP``
(schema v2 and the ``schema=`` keyword). A product that needs both schemas up
front (jobs) names the v2 release as its only floor; a product that probes v2
only for a run holding interrupt rows (pipeline execution) names v1 as its
floor and v2 as ``v2_floor``. Neither effective floor changes.

Probe order (the refusal that wins is the first that applies): package
import, submodule import, each required schema, each required callable, each
required call shape. The v2 stage runs the base probe, then the v2 schema,
then the ``make_event`` call shape that includes ``schema=``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from llm_kit import _runtime

from . import probe
from .probe import CallShape, Importer, ProbeFailure, Render

PACKAGE = "bootstrap_lib"
MODULE = "bootstrap_lib.execution_event"

SCHEMA_V1 = "plugins-kit.execution-event/v1"
SCHEMA_V2 = "plugins-kit.execution-event/v2"

#: The bootstrap release that ships ``bootstrap_lib.execution_event`` (schema v1).
EXECUTION_EVENT_V1_BOOTSTRAP = "0.135.0"

#: The bootstrap release that ships schema v2 and ``make_event(schema=)``.
EXECUTION_EVENT_V2_BOOTSTRAP = "0.136.0"

#: The keywords ``make_event`` takes for the fields every event carries.
MAKE_EVENT_KEYWORDS = (
    "seq",
    "run_id",
    "event",
    "plugin",
    "at",
    "unit_id",
    "attempt_id",
    "adapter",
    "model",
    "payload",
)


@dataclass(frozen=True)
class EventProbeSpec:
    """One product's description of what it needs from the event module."""

    plugin: str
    error: type[ImportError]
    render: Render
    floor: str
    required_schemas: tuple[str, ...]
    required_callables: tuple[str, ...]
    shapes: tuple[CallShape, ...]
    v2_schema: Optional[str] = None
    v2_floor: str = ""
    v2_shape: Optional[CallShape] = None
    package: str = PACKAGE
    module: str = MODULE
    #: The product's import seam; ``None`` is ``importlib.import_module``.
    importer: Optional[Importer] = None

    def __post_init__(self) -> None:
        probe.require_text("EventProbeSpec", "plugin", self.plugin)
        probe.require_text("EventProbeSpec", "floor", self.floor)
        if not self.required_schemas:
            raise ValueError("EventProbeSpec.required_schemas must name a schema")
        if self.v2_schema is not None:
            probe.require_text("EventProbeSpec", "v2_floor", self.v2_floor)
            if self.v2_shape is None:
                raise ValueError("EventProbeSpec.v2_shape is required with v2_schema")


def probe_execution_event(spec: EventProbeSpec) -> Any:
    """Return the usable execution-event module, or raise ``spec.error``.

    Package mode returns the routed execution-event contract and probes
    nothing: the pinned dependency guarantees the schemas and call shapes.
    """
    if spec.module == MODULE and _runtime.mode() == "package":
        return _runtime.load("execution_event", error=spec.error)
    probe.import_package(
        spec.package, spec.error, spec.render, spec.floor, spec.importer
    )
    module = probe.import_module(
        spec.module, spec.error, spec.render, spec.floor, spec.importer
    )
    schemas = getattr(module, "SUPPORTED_SCHEMAS", None)
    for schema in spec.required_schemas:
        if not probe.advertises(schemas, schema):
            probe.refuse(
                spec.error,
                spec.render,
                ProbeFailure(probe.UNSUPPORTED, schema, spec.floor),
            )
    for name in spec.required_callables:
        probe.require_callable(module, name, spec.error, spec.render, spec.floor)
    for shape in spec.shapes:
        probe.require_binding(
            getattr(module, shape.name), shape, spec.error, spec.render, spec.floor
        )
    return module


def probe_execution_event_v2(spec: EventProbeSpec) -> Any:
    """Return the module after the base probe and the schema-v2 stage.

    For a product that needs v2 only when a run has interrupt rows. A spec
    that declares no ``v2_schema`` is refused loudly.
    """
    if spec.v2_schema is None or spec.v2_shape is None:
        raise ValueError("this EventProbeSpec declares no schema-v2 stage")
    module = probe_execution_event(spec)
    if spec.module == MODULE and _runtime.mode() == "package":
        return module
    if not probe.advertises(getattr(module, "SUPPORTED_SCHEMAS", None), spec.v2_schema):
        probe.refuse(
            spec.error,
            spec.render,
            ProbeFailure(
                probe.UNSUPPORTED, spec.v2_schema, spec.v2_floor, stage=probe.STAGE_V2
            ),
        )
    probe.require_binding(
        module.make_event,
        spec.v2_shape,
        spec.error,
        spec.render,
        spec.v2_floor,
        stage=probe.STAGE_V2,
    )
    return module


def build_event(
    module: Any,
    spec: EventProbeSpec,
    *,
    seq: int,
    run_id: str,
    event: str,
    at: str,
    unit_id: Optional[str] = None,
    attempt_id: Optional[str] = None,
    adapter: Optional[str] = None,
    model: Optional[str] = None,
    payload: Optional[Mapping[str, Any]] = None,
    schema: Optional[str] = None,
) -> dict:
    """Build one event with the product's source literal.

    ``module`` is a probed module. ``schema=None`` omits the keyword, so the
    call is the one a v1-only module accepts; a product that always names its
    schema passes it. ``adapter`` and ``model`` are passed as given: the
    product decides what an empty value means.
    """
    revision = {} if schema is None else {"schema": schema}
    return module.make_event(
        seq=seq,
        run_id=run_id,
        event=event,
        plugin=spec.plugin,
        at=at,
        unit_id=unit_id,
        attempt_id=attempt_id,
        adapter=adapter,
        model=model,
        payload=payload,
        **revision,
    )
