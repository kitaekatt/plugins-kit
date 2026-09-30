"""Read-side projection of an execution store's attempt log into execution events.

``project_run`` turns one run's rows in ``ExecutionStore``'s append-only
``attempts`` log into the shared execution-event envelope
(``bootstrap_lib.execution_event``, schema v1). Nothing is written to the
store, and no other plugin's store or domain model is imported.

Ordering. An event's ``seq`` is ``attempts.id * 4 + phase``. Row ids come from
one AUTOINCREMENT column assigned under ``BEGIN IMMEDIATE``, so they follow
commit order, and ``seq`` stays the same when a run is projected again after
more rows were appended. A row yields at most three events: phase 0 ``usage``,
phase 1 ``result`` (or the row's single event), phase 2 ``terminal``. The
run-created event has ``seq`` 0, because row ids start at 1. ``at`` is
informational; no ordering uses it.

Mapping (one row kind to its events):

- run row: ``content-pipeline-kit:run-created``
- ``claim``: ``call-started``
- ``renew``: ``content-pipeline-kit:lease-renewed``
- ``expire`` (records the OLD token): ``result`` with status ``expired``
- ``superseded``: ``content-pipeline-kit:submission-superseded`` (not a
  ``result``, so an attempt keeps one result)
- ``accept``: ``usage`` when any usage field is known, ``result`` with status
  ``accepted``, ``terminal`` with state ``accepted``
- ``fail``: ``usage`` likewise, ``result`` with status ``failed``, and
  ``terminal`` only when the unit is failed or skipped and this row is the
  unit's last claim, expire, accept or fail row
- ``apply_started`` / ``apply_succeeded`` / ``apply_rejected``:
  ``content-pipeline-kit:apply-started`` / ``apply-succeeded`` /
  ``apply-rejected`` (unit scope, no attempt)
- ``interrupt_requested``: ``usage`` when any usage field is known, ``result``
  with status ``interrupt_requested``, then ``interrupt`` with phase
  ``requested`` (phase 2 of the row)
- ``interrupt_resolved``: ``interrupt`` with phase ``resolved``
- ``interrupt_rejected``: ``interrupt`` with phase ``rejected``, then
  ``terminal`` with state ``operator_rejected`` only when the request's
  ``on_rejected`` policy is ``stop``
- ``interrupt_expired``: ``interrupt`` with phase ``expired``, then
  ``terminal`` with state ``interrupt_expired`` only when the request's
  ``on_expired`` policy is ``stop``

The attempt id is ``str(fencing_token)``. Usage goes through the shared
``usage_payload`` rule, so an unknown count is null, never 0.

Interrupts. Only ``interrupt`` events are written under schema v2; every
other event stays v1. An ``interrupt`` event carries the owning claim's token
as its attempt id on every phase, and its payload holds the interrupt id, the
kind, the phase and (on the request) the expiry: never the request payload,
the request schema or the answer. A rejection's reason appears only in the
``terminal`` event, cut to 1000 characters. The interrupt id, kind, expiry
and policies are read with ``store.list_interrupts`` after the snapshot, and
only when the snapshot holds an interrupt row: interrupt rows are immutable
and are written in the transaction that writes their attempt row, so the
later read always contains them. A run with no interrupt row is projected
exactly as before, under the v1 probe alone.

Not projected: the ``dispatches`` table (a second sequence, so no
``dispatch-selected`` event is produced) and the audit reasoning chain (it has
no run or attempt identity, and its payload is model content).

Edge: this module needs ``bootstrap_lib.execution_event``, which a foreign
project interpreter running ``content_pipeline`` may not link. The events
functions refuse with ``ExecutionEventSupportError`` and a diagnosis; nothing
else in the package imports it. A run that has interrupt rows also needs
schema v2 of that module (bootstrap 0.136.0) and is refused, with its own
message, by a module that supports v1 only. The projection never calls the
interrupt contract.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any, Dict, List, Optional, Tuple

from content_pipeline.execution.model import (
    POLICY_STOP,
    TERMINAL_STATES,
    AttemptKind,
    AttemptRecord,
    UnitState,
    UnknownRunError,
)

PLUGIN = "content-pipeline-kit"
REQUIRED_SCHEMA = "plugins-kit.execution-event/v1"
# The bootstrap version that ships the execution-event module.
EXECUTION_EVENT_BOOTSTRAP = "0.135.0"
_OWNER = "bootstrap@plugins-kit"
_REQUIRED_CALLABLES = ("make_event", "utc_timestamp", "usage_payload", "validate_stream")
# The exact keywords this module passes to ``make_event``.
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
)

_STRIDE = 4
_PHASE_USAGE = 0
_PHASE_EVENT = 1
_PHASE_TERMINAL = 2
# A request row's `interrupt` event follows its `result`, and a request row
# never yields a `terminal`, so it takes the row's last phase.
_PHASE_REQUESTED = 2

# Interrupt rows are projected as `interrupt` events under schema v2.
REQUIRED_SCHEMA_V2 = "plugins-kit.execution-event/v2"
# The bootstrap version that ships schema v2 and the `schema` keyword.
EXECUTION_EVENT_V2_BOOTSTRAP = "0.136.0"

# The `interrupt` phase each interrupt row kind is projected as. The phase is
# a constant of the row kind; the names are those of the v2 event vocabulary.
_INTERRUPT_PHASES = {
    AttemptKind.INTERRUPT_REQUESTED: "requested",
    AttemptKind.INTERRUPT_RESOLVED: "resolved",
    AttemptKind.INTERRUPT_REJECTED: "rejected",
    AttemptKind.INTERRUPT_EXPIRED: "expired",
}

# The longest `reason` a `terminal` event carries.
_TERMINAL_REASON_LIMIT = 1000


class ExecutionEventSupportError(ImportError):
    """The bootstrap execution-event module is absent or too old."""


def _import_module(name: str) -> Any:
    return importlib.import_module(name)


def _too_old(reason: str) -> ExecutionEventSupportError:
    return ExecutionEventSupportError(
        f"content-pipeline-kit execution events need bootstrap "
        f"{EXECUTION_EVENT_BOOTSTRAP} or newer: {reason}. Run "
        f"`claude plugin update {_OWNER}` and restart the session."
    )


def _execution_event() -> Any:
    """Return ``bootstrap_lib.execution_event`` after checking it is usable.

    Three states: ``bootstrap_lib`` absent (install message), too old or stale
    (update message with this module's own version constant), or usable.
    """
    try:
        _import_module("bootstrap_lib")
    except ModuleNotFoundError as exc:
        if exc.name in (None, "bootstrap_lib"):
            raise ExecutionEventSupportError(
                "content-pipeline-kit execution events need bootstrap_lib, which "
                f"this interpreter cannot import. Run `claude plugin install {_OWNER}`."
            ) from exc
        raise _too_old(f"bootstrap_lib failed to import ({exc})") from exc
    try:
        module = _import_module("bootstrap_lib.execution_event")
    except ImportError as exc:
        raise _too_old("bootstrap_lib.execution_event does not import") from exc
    schemas = getattr(module, "SUPPORTED_SCHEMAS", None)
    try:
        supported = REQUIRED_SCHEMA in schemas
    except TypeError:
        supported = False
    if not supported:
        raise _too_old(f"the installed module does not support {REQUIRED_SCHEMA}")
    for name in _REQUIRED_CALLABLES:
        if not callable(getattr(module, name, None)):
            raise _too_old(f"the installed module has no {name}")
    try:
        inspect.signature(module.make_event).bind(**{k: None for k in _MAKE_EVENT_KEYWORDS})
    except (TypeError, ValueError) as exc:
        raise _too_old("the installed make_event does not accept this call shape") from exc
    return module


def _execution_event_v2() -> Any:
    """Return the event module after checking it can write schema v2.

    Called only for a run that has an interrupt row. A module that supports
    schema v1 only is usable for every other run, so it gets a message of its
    own, naming the version that ships v2.
    """
    module = _execution_event()
    reason = None
    if REQUIRED_SCHEMA_V2 not in module.SUPPORTED_SCHEMAS:
        reason = f"the installed module does not support {REQUIRED_SCHEMA_V2}"
    else:
        try:
            inspect.signature(module.make_event).bind(
                **{k: None for k in _MAKE_EVENT_KEYWORDS + ("schema",)}
            )
        except (TypeError, ValueError):
            reason = "the installed make_event does not accept a schema keyword"
    if reason is not None:
        raise ExecutionEventSupportError(
            f"this run has interrupt rows, which content-pipeline-kit projects as "
            f"interrupt events under {REQUIRED_SCHEMA_V2}; that needs bootstrap "
            f"{EXECUTION_EVENT_V2_BOOTSTRAP} or newer: {reason}. Run "
            f"`claude plugin update {_OWNER}` and restart the session."
        )
    return module


def _last_attempt_rows(attempts: List[AttemptRecord]) -> Dict[str, int]:
    """Per unit, the id of its last claim/expire/accept/fail row."""
    last: Dict[str, int] = {}
    for row in attempts:
        if row.kind in (
            AttemptKind.CLAIM,
            AttemptKind.EXPIRE,
            AttemptKind.ACCEPT,
            AttemptKind.FAIL,
        ):
            last[row.unit_id] = max(last.get(row.unit_id, 0), row.id)
    return last


def project_run(store: Any, run_id: str) -> Tuple[dict, ...]:
    """Project one run's store rows into a validated tuple of events.

    Every event is schema v1 except ``interrupt``, which is v2. Reads exactly
    one ``store.snapshot(run_id)`` (one read transaction), and, only when
    that snapshot holds an interrupt row, ``store.list_interrupts(run_id)``.
    Raises ``UnknownRunError`` for an unknown run and
    ``ExecutionEventSupportError`` when the event module is unavailable, or
    supports v1 only and the run has an interrupt row.
    """
    ee = _execution_event()
    run, units, attempts = store.snapshot(run_id)
    if run is None:
        raise UnknownRunError(run_id)
    attempts = sorted(attempts, key=lambda row: row.id)
    unit_state: Dict[str, UnitState] = {u.unit_id: u.state for u in units}
    last_row = _last_attempt_rows(attempts)
    # Keyed by the owning claim: one request per (unit, fencing token).
    interrupts: Dict[Tuple[str, Optional[int]], Any] = {}
    if any(row.kind in _INTERRUPT_PHASES for row in attempts):
        ee = _execution_event_v2()
        interrupts = {
            (record.unit_id, record.fencing_token): record
            for record in store.list_interrupts(run_id)
        }

    def build(
        seq: int,
        event: str,
        at: float,
        *,
        unit_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        payload: Optional[dict] = None,
        schema: Optional[str] = None,
    ) -> dict:
        # `schema` is passed only for a v2 event, so a run with no interrupt
        # row makes the same call it always has.
        revision = {} if schema is None else {"schema": schema}
        return ee.make_event(
            seq=seq,
            run_id=run_id,
            event=event,
            plugin=PLUGIN,
            at=ee.utc_timestamp(at),
            unit_id=unit_id,
            attempt_id=attempt_id,
            adapter=run.backend or None,
            model=run.model or None,
            payload=payload,
            **revision,
        )

    events: List[dict] = [
        build(
            0,
            f"{PLUGIN}:run-created",
            run.created_at,
            payload={"driver": run.driver, "adapter_version": run.adapter_version},
        )
    ]

    for row in attempts:
        base = row.id * _STRIDE
        unit = row.unit_id
        attempt = None if row.fencing_token is None else str(row.fencing_token)
        worker = {"worker_id": row.worker_id} if row.worker_id else {}
        kind = row.kind

        def one(name: str, payload: Optional[dict] = None, *, attempt_scoped: bool = True) -> None:
            events.append(
                build(
                    base + _PHASE_EVENT,
                    name,
                    row.at,
                    unit_id=unit,
                    attempt_id=attempt if attempt_scoped else None,
                    payload=payload,
                )
            )

        def usage_event() -> None:
            usage = None
            if row.usage is not None:
                usage = ee.usage_payload(
                    input_tokens=row.usage.input_tokens,
                    output_tokens=row.usage.output_tokens,
                    cache_hit_tokens=row.usage.cache_hit_tokens,
                )
            if usage is not None:
                events.append(
                    build(
                        base + _PHASE_USAGE,
                        "usage",
                        row.at,
                        unit_id=unit,
                        attempt_id=attempt,
                        payload=usage,
                    )
                )

        if kind is AttemptKind.CLAIM:
            one("call-started", worker)
        elif kind is AttemptKind.RENEW:
            one(f"{PLUGIN}:lease-renewed", worker)
        elif kind is AttemptKind.EXPIRE:
            one("result", {"status": "expired"})
        elif kind is AttemptKind.SUPERSEDED:
            one(f"{PLUGIN}:submission-superseded", worker)
        elif kind in _INTERRUPT_PHASES:
            record = interrupts[(unit, row.fencing_token)]
            interrupt: dict = {
                "interrupt_id": record.id,
                "kind": record.kind,
                "phase": _INTERRUPT_PHASES[kind],
            }
            closing: Optional[dict] = None
            if kind is AttemptKind.INTERRUPT_REQUESTED:
                usage_event()
                one("result", {"status": "interrupt_requested"})
                if record.expires_at is not None:
                    interrupt["expires_at"] = ee.utc_timestamp(record.expires_at)
                interrupt_phase = _PHASE_REQUESTED
            else:
                interrupt_phase = _PHASE_EVENT
                if (
                    kind is AttemptKind.INTERRUPT_REJECTED
                    and record.on_rejected == POLICY_STOP
                ):
                    closing = {"state": UnitState.OPERATOR_REJECTED.value}
                    reason = record.resolution.reason if record.resolution else None
                    if reason:
                        closing["reason"] = reason[:_TERMINAL_REASON_LIMIT]
                elif (
                    kind is AttemptKind.INTERRUPT_EXPIRED
                    and record.on_expired == POLICY_STOP
                ):
                    closing = {"state": UnitState.INTERRUPT_EXPIRED.value}
            events.append(
                build(
                    base + interrupt_phase,
                    "interrupt",
                    row.at,
                    unit_id=unit,
                    attempt_id=attempt,
                    payload=interrupt,
                    schema=REQUIRED_SCHEMA_V2,
                )
            )
            if closing is not None:
                events.append(
                    build(
                        base + _PHASE_TERMINAL,
                        "terminal",
                        row.at,
                        unit_id=unit,
                        payload=closing,
                    )
                )
        elif kind in (AttemptKind.ACCEPT, AttemptKind.FAIL):
            usage_event()
            terminal_state: Optional[str]
            if kind is AttemptKind.ACCEPT:
                one("result", {"status": "accepted"})
                terminal_state = UnitState.ACCEPTED.value
            else:
                result: dict = {"status": "failed"}
                if row.error:
                    result["error"] = row.error
                one("result", result)
                state = unit_state.get(unit)
                is_terminal = (
                    state in TERMINAL_STATES
                    and state is not UnitState.ACCEPTED
                    and last_row.get(unit) == row.id
                )
                terminal_state = state.value if is_terminal else None
            if terminal_state is not None:
                events.append(
                    build(
                        base + _PHASE_TERMINAL,
                        "terminal",
                        row.at,
                        unit_id=unit,
                        payload={"state": terminal_state},
                    )
                )
        elif kind is AttemptKind.APPLY_STARTED:
            one(f"{PLUGIN}:apply-started", attempt_scoped=False)
        elif kind is AttemptKind.APPLY_SUCCEEDED:
            one(f"{PLUGIN}:apply-succeeded", attempt_scoped=False)
        elif kind is AttemptKind.APPLY_REJECTED:
            one(
                f"{PLUGIN}:apply-rejected",
                {"reason": row.error} if row.error else None,
                attempt_scoped=False,
            )
    return ee.validate_stream(events)


def write_run_events(store: Any, run_id: str, sink: Any) -> int:
    """Write ``project_run``'s events to ``sink`` in order; return the count.

    The whole projection is built and validated before the first write, so a
    refusal leaves the sink untouched.
    """
    events = project_run(store, run_id)
    for event in events:
        sink.write(event)
    return len(events)
