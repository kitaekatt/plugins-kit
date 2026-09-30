"""Common execution events for the job-kit ledger.

job-kit records one execution event beside each ledger fact, in the fact's own
transaction, using the shared envelope in ``bootstrap_lib.execution_event``
(schema ``plugins-kit.execution-event/v1``; the ``interrupt`` event alone is
written under ``plugins-kit.execution-event/v2``). This module is the only place
job-kit reaches that module, and it reaches it only through
:func:`_execution_event`, so importing ``job_kit`` never needs
``bootstrap_lib``.

Identity mapping: ``run_id`` is the run id, ``unit_id`` is the job id, and
``attempt_id`` is ``str(attempt_no)``. ``source.adapter`` is the attempt's
backend and ``source.model`` its model.

The edge is REQUIRED: every ledger transition that has an event writes it in
the same transaction, so job-kit cannot record a run without the module.
``bootstrap.json`` therefore sets ``requires_bootstrap`` to
``_EXECUTION_EVENT_BOOTSTRAP``, and the probe below diagnoses an absent module
apart from a too-old or stale one before any fact is written.
"""

from __future__ import annotations

import datetime as _dt
import inspect
import math
import re
from types import ModuleType
from typing import Any, Iterable, Mapping, Optional

#: The ``source.plugin`` of every event job-kit records.
PLUGIN = "job-kit"

#: The schemas job-kit writes. Literals, because the module that defines
#: ``SCHEMA_V1`` and ``SCHEMA_V2`` may be absent when this is read. Every
#: event is written under v1 except ``interrupt``, which v2 defines.
SCHEMA_V1 = "plugins-kit.execution-event/v1"
SCHEMA_V2 = "plugins-kit.execution-event/v2"
REQUIRED_SCHEMAS = (SCHEMA_V1, SCHEMA_V2)

#: The v1 literal, kept under its original name.
REQUIRED_SCHEMA = SCHEMA_V1

#: The bootstrap release that shipped ``bootstrap_lib.execution_event`` with
#: the call shape job-kit uses (``make_event(..., schema=)`` and
#: ``plugins-kit.execution-event/v2``). Messages name this constant, never a
#: value read from a possibly stale module.
_EXECUTION_EVENT_BOOTSTRAP = "0.136.0"

#: Every module attribute job-kit calls.
_REQUIRED_CALLABLES = (
    "make_event",
    "utc_timestamp",
    "usage_payload",
    "validate_stream",
    "JsonlSink",
)

#: The exact keywords job-kit passes to ``make_event``.
_MAKE_EVENT_KEYWORDS = (
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
    "schema",
)

#: A ``reason`` in an event payload is cut to this many characters. The ledger
#: keeps up to 2000; one astral character escapes to 12 ASCII bytes, so 1000
#: characters stay inside the envelope's 16384-byte payload cap.
REASON_LIMIT = 1000

_ISO_Z = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3,6})?Z")
_EPOCH = re.compile(r"\d+(?:\.\d+)?")


class ExecutionEventSupportError(ImportError):
    """``bootstrap_lib.execution_event`` is absent, too old, or stale."""


def _execution_event() -> ModuleType:
    """Return the usable execution-event module, or raise a diagnosis.

    Absent (``bootstrap_lib`` does not import) and too old or stale (the
    submodule, the schema, a callable, or the ``make_event`` call shape is
    missing) produce different messages with different remedies.
    """
    try:
        import bootstrap_lib  # noqa: F401
    except ModuleNotFoundError as exc:
        raise ExecutionEventSupportError(
            "job-kit records execution events with bootstrap_lib, which is not "
            "linked into this environment: install or enable the bootstrap "
            "plugin (`claude plugin install bootstrap@plugins-kit`) and start a "
            "new session so it links job-kit's shared libs."
        ) from exc
    too_old = (
        "job-kit records execution events with bootstrap_lib.execution_event "
        f"({SCHEMA_V1} and {SCHEMA_V2}), which the linked bootstrap_lib "
        "predates or lacks: update the bootstrap plugin to >= "
        f"{_EXECUTION_EVENT_BOOTSTRAP} "
        "(`claude plugin update bootstrap@plugins-kit`) and restart."
    )
    try:
        from bootstrap_lib import execution_event as module
    except ImportError as exc:
        raise ExecutionEventSupportError(too_old) from exc
    supported = getattr(module, "SUPPORTED_SCHEMAS", None)
    if supported is None or SCHEMA_V1 not in supported:
        raise ExecutionEventSupportError(too_old)
    if SCHEMA_V2 not in supported:
        raise ExecutionEventSupportError(
            "the linked bootstrap_lib.execution_event supports "
            f"{SCHEMA_V1} but not /v2, which job-kit needs to record "
            "interrupts: update the bootstrap plugin to >= "
            f"{_EXECUTION_EVENT_BOOTSTRAP} "
            "(`claude plugin update bootstrap@plugins-kit`) and restart."
        )
    for name in _REQUIRED_CALLABLES:
        if not callable(getattr(module, name, None)):
            raise ExecutionEventSupportError(too_old)
    try:
        inspect.signature(module.make_event).bind(
            **{keyword: None for keyword in _MAKE_EVENT_KEYWORDS}
        )
        inspect.signature(module.JsonlSink).bind("events.jsonl", mode="create")
    except (TypeError, ValueError) as exc:
        raise ExecutionEventSupportError(too_old) from exc
    return module


def _is_iso_z(value: str) -> bool:
    if not _ISO_Z.fullmatch(value):
        return False
    try:
        _dt.datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return False
    return True


def event_at(*candidates: object) -> str:
    """Return the event ``at`` for a fact from its own timestamps.

    The first candidate that is a UTC timestamp wins: an ISO-8601 string
    ending in ``Z`` is used as it is, and an epoch (a number, or a decimal
    string such as ``str(time.time())``) is rendered by ``utc_timestamp``.
    The ledger stores caller-supplied timestamp text verbatim, so a candidate
    in any other form is skipped; with none left, ``at`` is the time the
    fact is recorded. ``at`` is informational: ``seq`` orders events.
    """
    module = _execution_event()
    for value in candidates:
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            if math.isfinite(value) and value >= 0:
                return module.utc_timestamp(value)
            continue
        if isinstance(value, str):
            text = value.strip()
            if _is_iso_z(text):
                return text
            if _EPOCH.fullmatch(text):
                return module.utc_timestamp(text)
    return module.utc_timestamp()


def build_event(
    *,
    seq: int,
    run_id: str,
    event: str,
    at: str,
    job_id: Optional[str] = None,
    attempt_no: Optional[int] = None,
    adapter: Optional[str] = None,
    model: Optional[str] = None,
    payload: Optional[Mapping[str, Any]] = None,
    schema: Optional[str] = None,
) -> dict:
    """Build and validate one job-kit event from ledger values.

    ``schema`` is the revision the event is written under; ``None`` is v1.
    Raises ``EventError`` (a ``ValueError``) when the event breaks the
    envelope, and :class:`ExecutionEventSupportError` when the module is
    unusable.
    """
    module = _execution_event()
    return module.make_event(
        schema=SCHEMA_V1 if schema is None else schema,
        seq=seq,
        run_id=run_id,
        event=event,
        plugin=PLUGIN,
        at=at,
        unit_id=job_id,
        attempt_id=(str(attempt_no) if attempt_no is not None else None),
        adapter=adapter or None,
        model=model or None,
        payload=dict(payload) if payload is not None else None,
    )


def check_unit_identity(run_id: str, job_id: str) -> None:
    """Refuse a run or job id the envelope cannot carry, before any write.

    Raises the module's ``EventError`` naming the id and the envelope field.
    """
    try:
        build_event(
            seq=0,
            run_id=run_id,
            event=f"{PLUGIN}:run-created",
            at=event_at(None),
            job_id=job_id,
        )
    except ValueError as exc:
        pointer = getattr(exc, "pointer", "")
        subject = f"run id {run_id!r}" if pointer.endswith("run_id") else f"job id {job_id!r}"
        raise type(exc)(
            f"{subject} cannot be recorded as an execution event identity "
            f"({pointer or 'identity'}): {exc}",
            **({"pointer": pointer} if hasattr(exc, "pointer") else {}),
        ) from exc


def usage_payload(usage: object) -> Optional[dict]:
    """Return the ``usage`` payload for a job-kit ``Usage``, or None.

    Routed through the shared ``usage_payload`` so a zero that means
    "not reported" is recorded as unknown, never as a measured zero.
    """
    if usage is None:
        return None
    module = _execution_event()
    return module.usage_payload(
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        cache_hit_tokens=getattr(usage, "cache_hit_tokens", None),
        total_tokens=getattr(usage, "total_tokens", None),
    )


def reason_text(reason: object) -> str:
    """Return a payload ``reason`` bounded to :data:`REASON_LIMIT`."""
    return str(reason)[:REASON_LIMIT]


def validate_stream(events: Iterable[Mapping[str, Any]]) -> tuple[dict, ...]:
    """Validate rendered events as one stream."""
    return _execution_event().validate_stream(events)


def jsonl_sink(path: object) -> Any:
    """Open a JSONL sink that refuses an existing file (mode ``create``)."""
    return _execution_event().JsonlSink(path, mode="create")


__all__ = [
    "PLUGIN",
    "REASON_LIMIT",
    "REQUIRED_SCHEMA",
    "REQUIRED_SCHEMAS",
    "SCHEMA_V1",
    "SCHEMA_V2",
    "ExecutionEventSupportError",
    "build_event",
    "check_unit_identity",
    "event_at",
    "jsonl_sink",
    "reason_text",
    "usage_payload",
    "validate_stream",
]
