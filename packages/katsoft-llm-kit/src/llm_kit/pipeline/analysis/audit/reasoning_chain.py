"""Per-item reasoning-chain sidecar.

Records, per entity, the append-only chain of reasoning that produced its final
value -- the generation inputs, each attempt, the rejections that bounced an
attempt, and the final accepted payload. This is the surface one of the two
source systems lost during consolidation (the retired ``*.prompts.yaml``
per-line sidecar); shipping it here rebuilds it for both consumers.

Kept deliberately DECOUPLED from the LLM stack: the pipeline stages call a
:class:`Recorder` (a two-method protocol -- ``record`` an event, ``chain`` read
one back), and this module never imports ``llm``. A stage that runs a validate-
until-valid submission records its result through :func:`record_submission`,
which duck-types the submission result (reads ``responses`` / ``rejections`` /
``payload`` / ``attempts`` off any object) rather than importing the
``SubmitResult`` type -- so ``audit`` stays within its dependency budget.

Two recorders ship: :class:`InMemoryRecorder` (append to a dict; the test /
in-process default) and :class:`NullRecorder` (a no-op for pipelines that do
not want the sidecar -- an opt-in surface, never forced). Persistence is a
caller concern: a :class:`SidecarRecorder` takes ``load`` / ``store`` callables
so the on-disk format (YAML sidecar, one file per entity) stays project-side.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol


class Recorder(Protocol):
    """Append-only per-entity event sink the pipeline stages call."""

    def record(self, entity_id: str, event: Mapping[str, Any]) -> None:
        ...

    def chain(self, entity_id: str) -> List[dict]:
        ...


def build_event(
    *,
    stage: str = "",
    inputs: Any = None,
    attempt: Optional[int] = None,
    rejections: Any = None,
    final: Any = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> dict:
    """Assemble one reasoning-chain event dict, dropping absent fields.

    Every field is optional so one call can record a generation input, a
    rejected attempt, or a final pick without carrying empty keys. ``at`` is a
    wall-clock stamp (``time.time()``), not a monotonic one -- a
    :class:`SidecarRecorder` persists a chain ACROSS process restarts, and
    ``time.monotonic()``'s epoch is per-process (arbitrary, reset on each
    process start), so a later event recorded by a freshly started process could
    carry a SMALLER monotonic reading than an earlier event from a
    longer-lived one. The ordering GUARANTEE a reader relies on is the list
    order within a chain (each recorder appends), not this field; ``at`` is a
    timestamp for display/diagnostics, not a sort key to trust across a
    restart.
    """
    event: dict = {"at": time.time()}
    if stage:
        event["stage"] = stage
    if inputs is not None:
        event["inputs"] = inputs
    if attempt is not None:
        event["attempt"] = attempt
    if rejections is not None:
        event["rejections"] = list(rejections) if not isinstance(rejections, Mapping) else rejections
    if final is not None:
        event["final"] = final
    if extra:
        event.update(dict(extra))
    return event


def record_chain(
    recorder: Recorder,
    entity_id: str,
    steps: List[Mapping[str, Any]],
) -> None:
    """Record each step in ``steps`` as an event on ``entity_id``'s chain."""
    for step in steps:
        recorder.record(entity_id, dict(step))


#: The contract-report keys recorded per attempt, as ``(recorded, report)``
#: pairs. The schema body is never among them: identity (``schema_digest``,
#: ``schema_version``) says which schema judged the attempt, and the body
#: stays with the contract's owner.
_CONTRACT_FIELDS = (
    ("id", "contract_id"),
    ("schema_version", "schema_version"),
    ("schema_digest", "schema_digest"),
    ("policy", "policy"),
    ("delivery", "delivery"),
    ("disposition", "disposition"),
)


def _contract_extra(response: Any) -> Optional[dict]:
    """The recorded contract identity of one response, or ``None``.

    Reads ``response.output_contract`` with ``getattr`` -- a mapping, or an
    object with ``to_json()`` -- so no LLM import is needed. Only the fields
    in ``_CONTRACT_FIELDS`` are copied; the schema body and the error list
    are not.
    """
    report = getattr(response, "output_contract", None)
    to_json = getattr(report, "to_json", None)
    if callable(to_json):
        report = to_json()
    if not isinstance(report, Mapping):
        return None
    return {name: report.get(key) for name, key in _CONTRACT_FIELDS}


def record_submission(
    recorder: Recorder,
    entity_id: str,
    submit_result: Any,
    *,
    stage: str = "generate",
    inputs: Any = None,
) -> None:
    """Record a validate-until-valid submission's per-attempt trail.

    Duck-types ``submit_result`` (reads ``responses`` / ``rejections`` /
    ``payload`` / ``attempts`` if present) so this module records an
    ``llm.submit_validated`` result WITHOUT importing the LLM package. One
    event per attempt is recorded (carrying that attempt's response text when
    available), then a final event carrying the accepted payload and the
    outstanding rejections. Decoupled by construction: a caller that does not
    use ``llm`` can pass any object with the same attribute names.

    Under a declared output contract each attempt event also carries
    ``contract`` -- ``{id, schema_version, schema_digest, policy, delivery,
    disposition}`` read from that attempt's stored response, never the schema
    body -- and the final event carries the last attempt's ``contract``, i.e.
    the final disposition. A response with no report adds no ``contract`` key.
    """
    responses = getattr(submit_result, "responses", None) or []
    last_contract: Optional[dict] = None
    for index, response in enumerate(responses):
        extra: dict = {"response_text": getattr(response, "text", "")}
        last_contract = _contract_extra(response)
        if last_contract is not None:
            extra["contract"] = last_contract
        recorder.record(
            entity_id,
            build_event(
                stage=stage,
                attempt=index + 1,
                inputs=inputs if index == 0 else None,
                extra=extra,
            ),
        )
    final_extra: dict = {"attempts": getattr(submit_result, "attempts", len(responses))}
    if last_contract is not None:
        final_extra["contract"] = last_contract
    recorder.record(
        entity_id,
        build_event(
            stage=stage,
            final=getattr(submit_result, "payload", None),
            rejections=[
                getattr(r, "kind", str(r))
                for r in (getattr(submit_result, "rejections", None) or [])
            ],
            extra=final_extra,
        ),
    )


@dataclass
class InMemoryRecorder:
    """A :class:`Recorder` that appends events to an in-memory dict."""

    chains: Dict[str, List[dict]] = field(default_factory=dict)

    def record(self, entity_id: str, event: Mapping[str, Any]) -> None:
        self.chains.setdefault(entity_id, []).append(dict(event))

    def chain(self, entity_id: str) -> List[dict]:
        return list(self.chains.get(entity_id, ()))


class NullRecorder:
    """A :class:`Recorder` that records nothing (the opt-out default)."""

    def record(self, entity_id: str, event: Mapping[str, Any]) -> None:
        pass

    def chain(self, entity_id: str) -> List[dict]:
        return []


@dataclass
class SidecarRecorder:
    """A :class:`Recorder` that persists append-only via injected I/O callables.

    - ``load`` -- ``entity_id -> list`` reads the existing chain (``[]`` when
      absent).
    - ``store`` -- ``(entity_id, list) -> None`` writes the whole chain back.

    The on-disk format is entirely the caller's; this class only guarantees the
    append-only discipline (read, append, write). Not built for high-frequency
    concurrent writers -- a bulk run that needs that wraps its own store behind
    the same two callables.
    """

    load: Callable[[str], List[dict]]
    store: Callable[[str, List[dict]], None]

    def record(self, entity_id: str, event: Mapping[str, Any]) -> None:
        chain = list(self.load(entity_id) or [])
        chain.append(dict(event))
        self.store(entity_id, chain)

    def chain(self, entity_id: str) -> List[dict]:
        return list(self.load(entity_id) or [])


__all__ = [
    "Recorder",
    "build_event",
    "record_chain",
    "record_submission",
    "InMemoryRecorder",
    "NullRecorder",
    "SidecarRecorder",
]
