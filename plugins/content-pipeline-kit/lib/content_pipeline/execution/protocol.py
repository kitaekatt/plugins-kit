"""Versioned JSON worker protocol (A-min.3): mountable handlers, not a tool.

The plan's exact verb list -- ``prepare | claim | read | submit | fail |
renew | status | pause | resume | finalize`` -- as a ``{verb: handler}``
mapping a consumer wires onto ITS OWN entry point (a CLI subcommand, an MCP
tool, a background-agent skill's shell-out, a workflow node). This module
ships no runnable tool of its own: the no-console-script boundary
(``plugins/content-pipeline-kit/CLAUDE.md``) holds exactly as it does for
``cli.run`` -- ``cli.scaffold.dispatch`` remains the human-facing helper for
argv; this is the machine-facing equivalent for a JSON envelope, and neither
one is an entry point by itself.

Envelope shape and versioning
----------------------------------
One JSON object in, one JSON object out::

    {"protocol_version": "1", "verb": "claim", "payload": {...}}
    -> {"ok": true, "result": {...}}
    -> {"ok": false, "error": {"type": "...", "message": "..."}}

``protocol_version`` is a single string constant per installed library
version (:data:`PROTOCOL_VERSION`, currently ``"1"``) -- not the plugin
version, not the adapter version (see ``execution.adapter``'s
``AdapterVersionMismatchError`` for that, a distinct compatibility axis: the
WIRE FORMAT versus the CONSUMER'S PARSER/PROMPT code). A mismatched
``protocol_version`` is refused (:class:`ProtocolVersionError`) rather than
silently interpreted under the wrong schema -- a version bump to this
envelope shape is a breaking change to every out-of-process worker at once,
so guessing compatibility is exactly the failure D1's adapter-identity
refusal already rejects for the adapter axis; the wire axis gets the same
discipline.

:func:`dispatch` never lets an :class:`Exception` escape across this
boundary. Every failure -- a malformed envelope, an unknown verb, a version
mismatch, or any ``Exception`` a verb handler raises (a stale fencing token,
an unknown run, a store error) -- is caught and rendered as ``{"ok": false,
"error": {...}}``, the same "catch broadly, report the outcome, never a raw
traceback" discipline ``cli.scaffold.dispatch`` already applies to argv
dispatch (see that module). This is what "malformed envelopes must be
refused loudly" means here: an explicit, typed, machine-readable refusal in
the reply -- never a silent no-op, never a default verb, never a stack trace
leaked to an untrusted worker process. A :class:`BaseException` that is not
an ``Exception`` (``SystemExit``, ``KeyboardInterrupt``) is deliberately NOT
caught -- it signals the process should stop, not that this one request
failed -- and propagates normally; see :func:`dispatch`'s own docstring.

Security posture (plan A-min.3, restated for this module specifically)
----------------------------------------------------------------------------
Trusted policy -- which ``strategy``/``gates``/``freshness_of``/adapter a
mount serves -- is supplied by the CONSUMER'S OWN entry point at
:func:`build_handlers` call time; it is local configuration, never carried
in an envelope. A ``payload`` is data a worker submits (unit ids, fencing
tokens, response text) -- it is evaluated (parsed, validated) or stored
verbatim, never executed, never interpolated into a gate/strategy/adapter
selection. Unit content flowing through ``read``'s reply and ``submit``'s
request is untrusted data end to end: this module never emits protocol
instruction text into that content, and a worker's authority to act comes
from the mounting consumer's own command and the run descriptor it was
launched against, never from a claim embedded in the JSON itself.

Mounting: one store, one adapter, one policy, many envelopes
------------------------------------------------------------------
:func:`build_handlers` closes over one already-open
:class:`~content_pipeline.execution.store.ExecutionStore`, one
:class:`~content_pipeline.execution.adapter.RunAdapter`, and the
``prepare_run``-shaped policy (``strategy``, ``gates``, ``freshness_of``,
``mark_unsupported``, ``max_wave_size``, ``graph_source``) a consumer's
mount needs for the ``prepare`` verb specifically -- every other verb needs
only ``store``/``adapter``. A consumer with no graph/gate policy (a flat,
gate-free run, or a mount that never calls ``prepare`` because the consumer
prepares from its own process) passes none of it; ``prepare`` then raises a
plain ``ValueError`` naming the missing ``strategy`` rather than silently
no-op'ing.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from content_pipeline.execution.adapter import (
    RunAdapter,
    require_compatible_adapter,
    require_compatible_environment,
)
from content_pipeline.execution.controller import (
    finalize_run,
    pause_run,
    prepare_run,
    resume_run,
)
from content_pipeline.execution.model import (
    ExecutionError,
    StaleFenceError,
    UnitRecord,
    UnknownRunError,
    UsageRecord,
)
from content_pipeline.execution.status import compute_status
from content_pipeline.execution.store import ExecutionStore, lease_for
from content_pipeline.freshness.classify import FreshnessState
from content_pipeline.llm.platform import evaluate_submission
from content_pipeline.pipeline.single_pass import Gate
from content_pipeline.pipeline.workunit import WorkUnit, WorkUnitStrategy
from content_pipeline.validate import contract

PROTOCOL_VERSION = "1"

VERBS = (
    "prepare",
    "claim",
    "read",
    "submit",
    "fail",
    "renew",
    "status",
    "pause",
    "resume",
    "finalize",
)

# One envelope in, one JSON-able result out. `dispatch` wraps the result (or
# any raised exception) into the uniform `{"ok": ..., ...}` reply -- a
# handler itself just returns plain dicts/lists/primitives, or raises.
ProtocolHandler = Callable[[Mapping[str, Any]], Any]


class ProtocolError(ExecutionError):
    """Base class for protocol-envelope-level errors (malformed shape,
    unknown verb, version mismatch) -- distinct from the store/controller
    `ExecutionError` subclasses a verb handler may also raise. Both families
    are caught identically by :func:`dispatch` and rendered the same way;
    the split exists only so a caller inspecting `error.type` can tell "the
    envelope itself was bad" apart from "the envelope was fine but the
    operation it named failed"."""


class MalformedEnvelopeError(ProtocolError):
    """The envelope is not a well-formed protocol request (not an object,
    missing `verb`/`protocol_version`, or a non-object `payload`)."""


class UnknownVerbError(ProtocolError):
    """`verb` is not one of :data:`VERBS` (or not registered on this mount)."""


class ProtocolVersionError(ProtocolError):
    """`protocol_version` does not match :data:`PROTOCOL_VERSION` -- refused,
    never silently interpreted under a schema this library version does not
    speak."""


def _validate_envelope(envelope: Any) -> Mapping[str, Any]:
    if not isinstance(envelope, Mapping):
        raise MalformedEnvelopeError(
            f"envelope must be a JSON object, got {type(envelope).__name__}"
        )
    if "protocol_version" not in envelope:
        raise MalformedEnvelopeError("envelope missing required key 'protocol_version'")
    if "verb" not in envelope:
        raise MalformedEnvelopeError("envelope missing required key 'verb'")
    if not isinstance(envelope["verb"], str) or not envelope["verb"]:
        raise MalformedEnvelopeError("envelope 'verb' must be a non-empty string")
    payload = envelope.get("payload", {})
    if not isinstance(payload, Mapping):
        raise MalformedEnvelopeError("envelope 'payload' must be a JSON object (or absent)")
    return envelope


def _require(payload: Mapping[str, Any], key: str) -> Any:
    if key not in payload or payload[key] in (None, ""):
        raise MalformedEnvelopeError(f"payload missing required key {key!r}")
    return payload[key]


def _require_text(payload: Mapping[str, Any]) -> str:
    """Require ``payload["text"]`` as a JSON string, allowing ``""`` (defect
    5, grok-4.6 review of 46d4a2b). The general :func:`_require` treats
    ``""`` as missing -- right for ``run_id``/``unit_id``/``worker_id``, but
    wrong for ``text``: ``store.accept_unit(text="")`` and the inline driver
    both permit empty text, so routing ``text`` through :func:`_require`
    wrongly refused an otherwise-valid empty submission. Also refuses any
    non-string value (a JSON number or boolean) rather than passing it
    through to :func:`~content_pipeline.llm.platform.evaluate_submission`
    and the SQLite ``TEXT`` column, where it would silently read back as a
    different Python value than what was submitted."""
    if "text" not in payload:
        raise MalformedEnvelopeError("payload missing required key 'text'")
    text = payload["text"]
    if not isinstance(text, str):
        raise MalformedEnvelopeError(
            f"payload 'text' must be a JSON string, got {type(text).__name__}"
        )
    return text


def _require_bool_flag(payload: Mapping[str, Any], key: str, *, default: bool) -> bool:
    """Require ``payload[key]`` to be a real JSON boolean when present
    (defect 4, grok-4.6 review of 46d4a2b). Plain ``bool(value)`` coerces
    Python-truthiness: ``bool("false")`` and ``bool("0")`` are both
    ``True``, so a model emitting the JSON string ``"terminal": "false"``
    (instead of the JSON boolean ``false``) would permanently fail a unit
    that should have been retried -- and ``FAILED`` is terminal, so the
    retry is gone for good. The argv surface (``cli/run.py``) already avoids
    this with an exact ``== "1"`` comparison; this is the JSON-native
    equivalent: refuse loudly rather than silently coerce."""
    if key not in payload:
        return default
    value = payload[key]
    if not isinstance(value, bool):
        raise MalformedEnvelopeError(
            f"payload {key!r} must be a JSON boolean, got {type(value).__name__}"
        )
    return value


def _resolve_lease_seconds(payload: Mapping[str, Any], default: float) -> float:
    """Resolve the lease duration for ``claim``/``renew``, bounded by the
    mount's own configured ``lease_seconds`` policy (defect 3, grok-4.6
    review of 46d4a2b).

    Mount-time ``lease_seconds`` is TRUSTED policy (the module's own
    "Security posture" section: policy is supplied by the consumer's own
    entry point, a payload is untrusted data); a worker-supplied override
    used to be honored UNBOUNDED. ``json.loads("1e309")`` parses to
    ``float("inf")``; ``now + inf`` is ``inf``; ``store.claim_unit`` treats
    ``lease_expires_at > now`` as live, so such a lease never expires. Once
    the worker holding it died, nothing on the protocol surface could
    reclaim the unit: ``fail``/``renew``/``submit`` all need the (now
    permanently live) fencing token, ``status`` does not return one,
    ``pause`` only blocks NEW claims, and ``finalize`` skips ``CLAIMED``
    units outright. This matters more than an ordinary input-validation gap
    because in phase B this envelope is MODEL-authored -- untrusted data by
    construction.

    A worker MAY request a SHORTER lease than policy (a legitimate "this
    unit is fast, don't hold a slow-lease-sized lock" request); it may never
    request a LONGER one -- the payload never overrides policy upward, only
    within it, via ``min(requested, default)``. Also refuses a non-finite or
    non-positive value outright, so the failure is a loud, typed
    :class:`MalformedEnvelopeError` rather than a silently clamped one.

    ``default`` here is still exactly "policy, closed over at mount time" --
    restating rather than weakening the contract above (item 2, A-min.4):
    it is no longer always the literal mount-time ``lease_seconds`` number,
    it may now be a PER-UNIT ceiling the mount derived from the adapter's
    declared ``expected_unit_seconds`` via
    :func:`~content_pipeline.execution.store.lease_for` (see
    :func:`build_handlers`'s ``lease_seconds=None`` "derive" behavior). The
    original security rationale above was about an UNTRUSTED PAYLOAD setting
    an unbounded lease; the adapter is trusted policy closed over at mount
    time, same as a fixed ``lease_seconds`` always was, so a derived
    per-unit ceiling is inside that rationale, not a regression of it -- the
    payload STILL only ever shortens, never lengthens, whatever ``default``
    this call was given.
    """
    if "lease_seconds" not in payload:
        return default
    raw = payload["lease_seconds"]
    try:
        seconds = float(raw)
    except (TypeError, ValueError) as exc:
        raise MalformedEnvelopeError(
            f"payload 'lease_seconds' must be a finite positive number, got {raw!r}"
        ) from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise MalformedEnvelopeError(
            f"payload 'lease_seconds' must be a finite positive number, got {raw!r}"
        )
    return min(seconds, default)


def _usage_from_payload(payload: Mapping[str, Any]) -> Optional[UsageRecord]:
    keys = ("input_tokens", "output_tokens", "cache_hit_tokens")
    if not any(k in payload for k in keys):
        return None
    return UsageRecord(
        input_tokens=payload.get("input_tokens"),
        output_tokens=payload.get("output_tokens"),
        cache_hit_tokens=payload.get("cache_hit_tokens"),
    )


def _unit_summary(unit: UnitRecord) -> Dict[str, Any]:
    """A JSON-safe subset of ``UnitRecord`` -- identity, ordering, and state
    only. Deliberately excludes ``accepted_text`` (invariant 6's spirit:
    this module's replies are worker/orchestrator-facing operational data,
    not a channel for re-surfacing accepted content wholesale)."""
    return {"unit_id": unit.unit_id, "ordinal": unit.ordinal, "state": unit.state.value}


def _get_run_or_raise(store: ExecutionStore, run_id: str):
    run = store.get_run(run_id)
    if run is None:
        raise UnknownRunError(run_id)
    return run


def build_handlers(
    store: ExecutionStore,
    adapter: RunAdapter,
    *,
    strategy: Optional[WorkUnitStrategy] = None,
    gates: Sequence[Gate] = (),
    freshness_of: Optional[Callable[[WorkUnit], FreshnessState]] = None,
    include_stale: bool = True,
    mark_unsupported: Optional[Callable[[str, str], None]] = None,
    max_wave_size: Optional[int] = None,
    graph_source: Any = None,
    lease_seconds: Optional[float] = None,
) -> Dict[str, ProtocolHandler]:
    """Build the ``{verb: handler}`` registry a consumer mounts on its own
    entry point and drives through :func:`dispatch`.

    ``store`` and ``adapter`` are the caller's already-open
    :class:`~content_pipeline.execution.store.ExecutionStore` and
    :class:`~content_pipeline.execution.adapter.RunAdapter`; every handler
    closes over them, needing no separate wiring step per verb (mirrors
    ``cli.run.build_commands``'s shape for the argv surface).

    ``strategy``/``gates``/``freshness_of``/``include_stale``/
    ``mark_unsupported``/``max_wave_size``/``graph_source`` are
    :func:`~content_pipeline.execution.controller.prepare_run`'s own policy
    parameters, fixed for this mount -- only the ``prepare`` verb consults
    them. A mount that never calls ``prepare`` (e.g. a worker-only mount
    whose consumer prepares from its own orchestrating process) may omit
    all of them; ``strategy`` staying ``None`` makes ``prepare`` raise a
    plain ``ValueError`` naming the gap instead of silently no-op'ing.

    ``lease_seconds`` (item 2, A-min.4): ``None`` (the default) means DERIVE
    the lease ceiling per unit, per ``claim``/``renew`` call, from
    ``adapter.resolve_expected_unit_seconds(unit)`` via
    :func:`~content_pipeline.execution.store.lease_for` -- an adapter that
    declares no cost falls back to the unchanged 300s default, no warning.
    An explicit ``lease_seconds`` still wins outright over derivation, same
    as before this feature: trusted mount policy stays trusted, and D5's
    "consumer-configured lease per run" stays GUIDANCE the mount may still
    fix directly, never something derivation overrides.
    """

    def _require_compatible_run(run_id: str) -> None:
        """D1's incompatible-resume refusal, extended to the WORKER verbs
        (defect/design-gap, grok-4.6 review of 46d4a2b): ``require_compatible_adapter``
        used to run only on the orchestrator verbs (``prepare``/``resume``/
        ``finalize``) -- never on ``claim``/``read``/``submit``/``fail``/
        ``renew``, which are the only verbs a short-lived worker actually
        sends. A process mounting adapter ``v2`` against a ``v1`` run could
        accept text ``v1`` would have rejected, and a later ``v1`` finalize
        would re-parse it without ever re-validating -- precisely the hazard
        the version tag exists to prevent. Cheap (one extra ``store.get_run``
        read already paid for by ``claim``/``fail``/``renew`` internally, new
        only for ``read``/``submit``) and safe (every existing caller uses
        matching ``adapter_version`` on both sides, the A-min.1/A-min.2
        default included), so it is applied uniformly rather than reported
        as a design question.

        Item 5 (A-min.4) adds ``require_compatible_environment`` alongside
        ``require_compatible_adapter`` here, same rationale: this is the
        single call site every WORKER verb (claim/read/submit/fail/renew)
        passes through, so one line here covers all of them with no
        per-verb work, refusing a worker process whose environment
        disagrees with the run's create-time snapshot."""
        run = _get_run_or_raise(store, run_id)
        require_compatible_adapter(run, adapter)
        require_compatible_environment(run, adapter)

    def _lease_ceiling(unit_id: str) -> float:
        """Item 2: the mount-fixed ``lease_seconds`` still wins outright
        when set; ``None`` (the default) derives a per-unit ceiling from the
        adapter's declared cost via
        :func:`~content_pipeline.execution.store.lease_for`. Used by both
        ``claim`` and ``renew`` so a slow unit's renewal is never capped
        below what its own claim was allowed."""
        if lease_seconds is not None:
            return lease_seconds
        unit = adapter.unit_for(unit_id)
        return lease_for(adapter.resolve_expected_unit_seconds(unit))

    def _prepare(payload: Mapping[str, Any]) -> Any:
        run_id = _require(payload, "run_id")
        unit_ids = payload.get("unit_ids")
        if not isinstance(unit_ids, Sequence) or isinstance(unit_ids, (str, bytes)):
            raise MalformedEnvelopeError("'prepare' payload requires a list 'unit_ids'")
        run = _get_run_or_raise(store, run_id)
        require_compatible_adapter(run, adapter)
        if strategy is None:
            raise ValueError(
                "this protocol mount has no `strategy` configured "
                "(build_handlers was called without one); 'prepare' is unavailable"
            )
        work_units = [adapter.unit_for(str(uid)) for uid in unit_ids]
        wave = prepare_run(
            store,
            run_id,
            strategy,
            work_units,
            graph_source=graph_source,
            gates=gates,
            freshness_of=freshness_of,
            include_stale=include_stale,
            mark_unsupported=mark_unsupported,
            max_wave_size=max_wave_size,
        )
        return {"run_id": run_id, "wave": [_unit_summary(u) for u in wave]}

    def _claim(payload: Mapping[str, Any]) -> Any:
        run_id = _require(payload, "run_id")
        unit_id = _require(payload, "unit_id")
        worker_id = _require(payload, "worker_id")
        _require_compatible_run(run_id)
        seconds = _resolve_lease_seconds(payload, _lease_ceiling(unit_id))
        result = store.claim_unit(run_id, unit_id, worker_id, lease_seconds=seconds)
        return {
            "run_id": run_id,
            "unit_id": unit_id,
            "fencing_token": result.fencing_token,
            "lease_expires_at": result.lease_expires_at,
        }

    def _read(payload: Mapping[str, Any]) -> Any:
        run_id = _require(payload, "run_id")
        unit_id = _require(payload, "unit_id")
        _require_compatible_run(run_id)
        unit = adapter.unit_for(unit_id)
        request = adapter.resolve_prepared_request(unit)
        return {
            "run_id": run_id,
            "unit_id": unit_id,
            "system": request.system,
            "user": request.user,
        }

    def _submit(payload: Mapping[str, Any]) -> Any:
        run_id = _require(payload, "run_id")
        unit_id = _require(payload, "unit_id")
        _require_compatible_run(run_id)
        fencing_token = int(_require(payload, "fencing_token"))
        text = _require_text(payload)
        # Defect 1 (grok-4.6 review of 46d4a2b): the fencing check used to
        # happen only on the ACCEPT path, inside `store.accept_unit` -- the
        # REJECT path (invalid text) never touched the store at all, so a
        # stale token paired with invalid text returned an `{"ok": true,
        # "accepted": false}` retry-feedback reply, telling a worker it
        # still owned a claim it had already lost to a reclaiming worker.
        # `store.renew_lease`/`fail_unit` check the fence FIRST, before any
        # other work; do the same here, before spending an evaluation on
        # text a stale claimant no longer has standing to submit.
        current_unit = store.get_unit(run_id, unit_id)
        if current_unit is not None and fencing_token != current_unit.fencing_token:
            raise StaleFenceError(run_id, unit_id, fencing_token, current_unit.fencing_token)
        unit = adapter.unit_for(unit_id)
        spec = adapter.resolve_validation_spec(unit)
        evaluation = evaluate_submission(text, spec)
        if evaluation.parsed and not contract.is_rejecting(
            evaluation.rejections, block_soft=spec.block_soft
        ):
            store.accept_unit(
                run_id, unit_id, fencing_token, text=text, usage=_usage_from_payload(payload)
            )
            return {"run_id": run_id, "unit_id": unit_id, "accepted": True}
        feedback = contract.format_rejections(evaluation.rejections, block_soft=spec.block_soft)
        return {
            "run_id": run_id,
            "unit_id": unit_id,
            "accepted": False,
            "feedback": feedback,
        }

    def _fail(payload: Mapping[str, Any]) -> Any:
        run_id = _require(payload, "run_id")
        unit_id = _require(payload, "unit_id")
        _require_compatible_run(run_id)
        fencing_token = int(_require(payload, "fencing_token"))
        terminal = _require_bool_flag(payload, "terminal", default=False)
        store.fail_unit(
            run_id,
            unit_id,
            fencing_token,
            error=payload.get("error", ""),
            terminal=terminal,
            usage=_usage_from_payload(payload),
        )
        return {"run_id": run_id, "unit_id": unit_id, "state": "failed" if terminal else "pending"}

    def _renew(payload: Mapping[str, Any]) -> Any:
        run_id = _require(payload, "run_id")
        unit_id = _require(payload, "unit_id")
        _require_compatible_run(run_id)
        fencing_token = int(_require(payload, "fencing_token"))
        seconds = _resolve_lease_seconds(payload, _lease_ceiling(unit_id))
        lease_expires_at = store.renew_lease(run_id, unit_id, fencing_token, lease_seconds=seconds)
        return {"run_id": run_id, "unit_id": unit_id, "lease_expires_at": lease_expires_at}

    def _status(payload: Mapping[str, Any]) -> Any:
        run_id = _require(payload, "run_id")
        # `compute_status` raises a plain `KeyError` for an unknown run by its
        # own design (see that function's docstring: "a caller that needs a
        # typed error should call store.get_run first") -- every sibling verb
        # goes through `_get_run_or_raise` and surfaces `UnknownRunError`;
        # `status` used to be the one verb that did not, so a missing run
        # replied with a raw `KeyError` instead of the typed error every
        # other verb gives for the identical situation.
        _get_run_or_raise(store, run_id)
        kwargs: Dict[str, Any] = {}
        if "window" in payload:
            kwargs["throughput_window_s"] = float(payload["window"])
        if "max_failure_groups" in payload:
            kwargs["max_failure_groups"] = int(payload["max_failure_groups"])
        digest = compute_status(store, run_id, **kwargs)
        return digest.to_dict()

    def _pause(payload: Mapping[str, Any]) -> Any:
        run_id = _require(payload, "run_id")
        pause_run(store, run_id, detail=payload.get("detail", ""))
        return {"run_id": run_id, "halted": "pause"}

    def _resume(payload: Mapping[str, Any]) -> Any:
        run_id = _require(payload, "run_id")
        run = _get_run_or_raise(store, run_id)
        require_compatible_adapter(run, adapter)
        resume_run(store, run_id)
        return {"run_id": run_id, "halted": None}

    def _finalize(payload: Mapping[str, Any]) -> Any:
        run_id = _require(payload, "run_id")
        run = _get_run_or_raise(store, run_id)
        require_compatible_adapter(run, adapter)
        applied = finalize_run(store, run_id, adapter)
        return {"run_id": run_id, "applied": applied}

    return {
        "prepare": _prepare,
        "claim": _claim,
        "read": _read,
        "submit": _submit,
        "fail": _fail,
        "renew": _renew,
        "status": _status,
        "pause": _pause,
        "resume": _resume,
        "finalize": _finalize,
    }


def dispatch(envelope: Mapping[str, Any], handlers: Mapping[str, ProtocolHandler]) -> Dict[str, Any]:
    """Route one JSON envelope to its verb handler; never raises ``Exception``.

    Validates envelope shape and ``protocol_version`` first (see the module
    docstring), then looks up ``verb`` in ``handlers`` (typically
    :func:`build_handlers`'s return value -- a caller MAY pass a narrowed or
    wrapped subset, e.g. to expose only worker-safe verbs to an untrusted
    process). Every failure -- malformed envelope, unknown verb, version
    mismatch, or any :class:`Exception` the handler itself raises -- is
    caught and rendered as ``{"ok": False, "error": {"type": ...,
    "message": ...}}``; success is always ``{"ok": True, "result": ...}``.
    This function catches ``Exception`` only, deliberately: a
    :class:`BaseException` that is not an :class:`Exception` (``SystemExit``,
    ``KeyboardInterrupt``, ``GeneratorExit``) still propagates, on the
    reasoning that those signal the PROCESS should stop, not that this one
    request failed -- swallowing them here would make an operator's Ctrl-C
    or a deliberate shutdown look like a normal protocol error instead of
    interrupting the process. So "never raises" means never raises for a
    malformed envelope or a handler's own logic failure -- the ordinary
    "loud, never a traceback for an untrusted worker" contract this module
    exists to provide -- not an unconditional guarantee for every Python
    exception.
    """
    try:
        validated = _validate_envelope(envelope)
        version = str(validated["protocol_version"])
        if version != PROTOCOL_VERSION:
            raise ProtocolVersionError(
                f"unsupported protocol_version {version!r}; this mount serves "
                f"{PROTOCOL_VERSION!r}"
            )
        verb = validated["verb"]
        handler = handlers.get(verb)
        if handler is None:
            raise UnknownVerbError(f"unknown verb {verb!r}; available: {sorted(handlers)}")
        payload = validated.get("payload", {})
        result = handler(payload)
    except Exception as exc:  # noqa: BLE001 -- protocol boundary: never raise, always a typed reply
        return {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
    return {"ok": True, "result": result}


__all__ = [
    "PROTOCOL_VERSION",
    "VERBS",
    "ProtocolHandler",
    "ProtocolError",
    "MalformedEnvelopeError",
    "UnknownVerbError",
    "ProtocolVersionError",
    "build_handlers",
    "dispatch",
]
