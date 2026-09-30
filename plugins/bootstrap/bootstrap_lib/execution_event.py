"""Build, validate, and record common execution events.

An execution event is one fact about running work -- an executor chosen, a
call started, usage observed, an attempt ended, a unit finished -- written in
one envelope that every plugin shares. Each plugin keeps its own store; only
the envelope and the event names are shared. The format is specified in the
plugin-dev skill's ``references/execution-events.md``; this module is its one
validator.

Schema ``plugins-kit.execution-event/v1`` (``SCHEMA_V1``) is frozen: its
names and validation rules never change. A later name enters only through a
later schema literal that ``SUPPORTED_SCHEMAS`` gains, selected by the
``schema=`` keyword of ``make_event`` and ``Emitter``. A module that does not
know a schema refuses the event at ``schema`` instead of accepting it under
weaker rules.

Schema ``plugins-kit.execution-event/v2`` (``SCHEMA_V2``) is v1 plus the
attempt-scoped ``interrupt`` event, whose payload key set is closed and holds
no free text. It is frozen on the same terms. Each event is checked against
the vocabulary of its own ``schema``, so a v1 event is validated exactly as
before and a stream may mix both revisions.

The module is stdlib-only and imports nothing from ``bootstrap_lib``: it is
linked into venvs that carry no third-party dependency, and a process that
holds an older copy of a sibling module cannot disagree with it.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
import re
import threading
import unicodedata
from typing import Any, Iterable, Mapping


OWNER = "bootstrap@plugins-kit"

# FROZEN. Never reassigned; a later revision adds its own constant.
SCHEMA_V1 = "plugins-kit.execution-event/v1"

# FROZEN. v1 plus the attempt-scoped `interrupt` event.
SCHEMA_V2 = "plugins-kit.execution-event/v2"

# The capability marker a consumer probes. It only ever grows.
SUPPORTED_SCHEMAS = frozenset({SCHEMA_V1, SCHEMA_V2})

# The v1 core vocabulary. FROZEN; a later revision has its own set.
CORE_EVENTS = frozenset(
    {"dispatch-selected", "call-started", "usage", "result", "terminal"}
)

# Events that describe one attempt: unit_id and attempt_id are both required.
# The v1 set. FROZEN.
ATTEMPT_SCOPED = frozenset({"dispatch-selected", "call-started", "usage", "result"})

# Names a later schema revision defines. Used only to word the v1 refusal.
LATER_REVISION_NAMES = frozenset({"contract", "interrupt"})

# The v2 vocabulary: every v1 name under the v1 rules, plus `interrupt`.
CORE_EVENTS_V2 = CORE_EVENTS | {"interrupt"}
ATTEMPT_SCOPED_V2 = ATTEMPT_SCOPED | {"interrupt"}

# The lifecycle of one interrupt: one request, then at most one close.
INTERRUPT_PHASES = frozenset({"requested", "resolved", "rejected", "expired"})
_INTERRUPT_CLOSING = frozenset({"resolved", "rejected", "expired"})

# The CLOSED `interrupt` payload key set. Every value is an identifier, a
# pattern-bound name, a closed-set phase, a timestamp, or an int, so no key
# can carry a request payload, a request schema, a resolution input, or free
# text such as a reason.
INTERRUPT_PAYLOAD_KEYS = frozenset(
    {"interrupt_id", "kind", "phase", "expires_at", "continuation_no"}
)
_INTERRUPT_REQUIRED_KEYS = ("interrupt_id", "kind", "phase")

# Per schema: (core names, attempt-scoped names, names a later revision defines).
_VOCABULARIES = {
    SCHEMA_V1: (CORE_EVENTS, ATTEMPT_SCOPED, LATER_REVISION_NAMES),
    SCHEMA_V2: (CORE_EVENTS_V2, ATTEMPT_SCOPED_V2, frozenset({"contract"})),
}

MAX_PAYLOAD_BYTES = 16384

_TOP_LEVEL_KEYS = ("schema", "seq", "identity", "event", "at", "source", "payload")
_IDENTITY_KEYS = ("run_id", "unit_id", "attempt_id")
_SOURCE_KEYS = ("plugin", "adapter", "model")
_USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_hit_tokens", "total_tokens")

_MAX_IDENTITY_CHARS = 200
_NAME_RE = re.compile(r"[a-z][a-z0-9-]*")
_AT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3,6})?Z")
_EPOCH_STRING_RE = re.compile(r"\d+(?:\.\d+)?")
_EPOCH_ZERO = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)


class EventError(ValueError):
    """An event, stream, or constructor argument breaks the envelope rules.

    ``pointer`` is the JSON pointer of the first fault: ``"/identity/run_id"``
    inside one event, ``"/3/seq"`` for the fourth event of a stream, and
    ``""`` when the fault is the value as a whole.
    """

    def __init__(self, message: str, *, pointer: str = "") -> None:
        super().__init__(message)
        self.pointer = pointer


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _ptr(*parts: Any) -> str:
    return "".join("/" + _escape(str(part)) for part in parts)


# --------------------------------------------------------------------------
# Timestamps
# --------------------------------------------------------------------------


def utc_timestamp(epoch: float | str | None = None) -> str:
    """Return an ISO-8601 UTC ``at`` value ending in ``Z``.

    ``epoch`` is seconds since the Unix epoch as an int, a float, or a decimal
    string such as ``str(time.time())``; ``None`` means now. Whole seconds
    render without a fraction; any other value renders to microseconds.
    """
    if epoch is None:
        moment = _dt.datetime.now(_dt.timezone.utc)
    else:
        if isinstance(epoch, bool):
            raise EventError("epoch must be a number or a decimal string, got bool")
        if isinstance(epoch, str):
            text = epoch.strip()
            if not _EPOCH_STRING_RE.fullmatch(text):
                raise EventError(
                    f"epoch string must be decimal seconds, got {epoch!r}"
                )
            seconds = float(text)
        elif isinstance(epoch, (int, float)):
            seconds = float(epoch)
        else:
            raise EventError(
                "epoch must be a number or a decimal string, "
                f"got {type(epoch).__name__}"
            )
        if not math.isfinite(seconds) or seconds < 0:
            raise EventError(f"epoch must be finite and >= 0, got {epoch!r}")
        moment = _EPOCH_ZERO + _dt.timedelta(seconds=seconds)
    text = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond:
        text += f".{moment.microsecond:06d}"
    return text + "Z"


def _check_utc_z(value: Any, label: str, pointer: str) -> str:
    if not isinstance(value, str) or not _AT_RE.fullmatch(value):
        raise EventError(
            f"{label} must be ISO-8601 UTC ending in Z, "
            f"YYYY-MM-DDTHH:MM:SS[.fff to .ffffff]Z, got {value!r}",
            pointer=pointer,
        )
    try:
        _dt.datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise EventError(
            f"{label} is not a calendar time: {value!r}", pointer=pointer
        ) from exc
    return value


def _check_at(value: Any) -> str:
    return _check_utc_z(value, "at", "/at")


# --------------------------------------------------------------------------
# Usage
# --------------------------------------------------------------------------


def _usage_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EventError(
            f"{name} must be an int >= 0 or None, got {value!r}",
            pointer=_ptr("payload", name),
        )
    return value


def usage_payload(
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_hit_tokens: int | None = None,
    total_tokens: int | None = None,
) -> dict | None:
    """Return the ``usage`` payload, or None when no field is known.

    A token count of 0 often means "not reported", so a zero is kept only
    where it is evidently a measurement: an input or output 0 is kept when
    the other directional field is non-zero, and a cache or total 0 always
    becomes None (unknown).
    """
    inp = _usage_int(input_tokens, "input_tokens")
    out = _usage_int(output_tokens, "output_tokens")
    cache = _usage_int(cache_hit_tokens, "cache_hit_tokens")
    total = _usage_int(total_tokens, "total_tokens")
    raw_in, raw_out = inp, out
    if raw_in == 0 and not raw_out:
        inp = None
    if raw_out == 0 and not raw_in:
        out = None
    if cache == 0:
        cache = None
    if total == 0:
        total = None
    payload = {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_hit_tokens": cache,
        "total_tokens": total,
    }
    if all(value is None for value in payload.values()):
        return None
    return payload


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _bounded(value: Any, pointer: str) -> str:
    if not isinstance(value, str):
        raise EventError(
            f"must be a string, got {type(value).__name__}", pointer=pointer
        )
    if not value:
        raise EventError("must be a non-empty string", pointer=pointer)
    if len(value) > _MAX_IDENTITY_CHARS:
        raise EventError(
            f"must be at most {_MAX_IDENTITY_CHARS} characters, got {len(value)}",
            pointer=pointer,
        )
    for char in value:
        if unicodedata.category(char) == "Cc":
            raise EventError(
                f"must not contain control characters, got {value!r}",
                pointer=pointer,
            )
    return value


def _sub_mapping(
    value: Any, name: str, allowed: tuple[str, ...], required: str
) -> dict:
    if not isinstance(value, Mapping):
        raise EventError(
            f"{name} must be a mapping, got {type(value).__name__}",
            pointer=_ptr(name),
        )
    for key in value:
        if key not in allowed:
            raise EventError(
                f"{name} has unknown key {key!r}; allowed: {', '.join(allowed)}",
                pointer=_ptr(name, key),
            )
    if required not in value:
        raise EventError(f"{name}.{required} is required", pointer=_ptr(name, required))
    for key in allowed:
        if key in value and value[key] is None:
            raise EventError(
                f"{name}.{key} is null; omit an absent key instead",
                pointer=_ptr(name, key),
            )
    return dict(value)


def _json_native(value: Any, path: tuple) -> Any:
    """Return a deep copy of a JSON-native value, or raise at the first fault."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EventError(
                f"payload value must be finite, got {value!r}", pointer=_ptr(*path)
            )
        return value
    if isinstance(value, dict):
        copy = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise EventError(
                    f"payload keys must be strings, got {type(key).__name__} {key!r}",
                    pointer=_ptr(*path),
                )
            copy[key] = _json_native(item, path + (key,))
        return copy
    if isinstance(value, list):
        return [_json_native(item, path + (index,)) for index, item in enumerate(value)]
    raise EventError(
        f"payload value must be JSON-native, got {type(value).__name__}",
        pointer=_ptr(*path),
    )


def _payload_bytes(payload: dict) -> int:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return len(text.encode("ascii"))


def _check_payload(value: Any) -> dict:
    if not isinstance(value, dict):
        raise EventError(
            f"payload must be a mapping, got {type(value).__name__}",
            pointer="/payload",
        )
    try:
        payload = _json_native(value, ("payload",))
        size = _payload_bytes(payload)
    except RecursionError as exc:
        raise EventError("payload is nested too deeply to serialize", pointer="/payload") from exc
    if size > MAX_PAYLOAD_BYTES:
        raise EventError(
            f"payload is {size} bytes serialized; the cap is {MAX_PAYLOAD_BYTES}",
            pointer="/payload",
        )
    return payload


def _vocabulary(schema: str) -> tuple[frozenset, frozenset, frozenset]:
    """Return (core names, attempt-scoped names, later-revision names) for a
    supported schema. v1 always resolves to the frozen v1 sets."""
    return _VOCABULARIES[schema]


def _check_event_name(name: Any, plugin: str, schema: str = SCHEMA_V1) -> str:
    core, _scoped, later = _vocabulary(schema)
    if not isinstance(name, str) or not name:
        raise EventError("event must be a non-empty string", pointer="/event")
    if name in core:
        return name
    if ":" in name:
        prefix, _, part = name.partition(":")
        if prefix != plugin:
            raise EventError(
                f"extension event {name!r} must use the emitting plugin's own "
                f"prefix {plugin + ':'!r}",
                pointer="/event",
            )
        if not _NAME_RE.fullmatch(part):
            raise EventError(
                f"extension name part {part!r} must match [a-z][a-z0-9-]*",
                pointer="/event",
            )
        return name
    if name in later:
        raise EventError(
            f"event {name!r} is defined by a later schema revision; "
            f"{schema} does not accept it",
            pointer="/event",
        )
    raise EventError(
        f"unknown event {name!r}: use a core name "
        f"({', '.join(sorted(core))}) or '<plugin>:<name>'",
        pointer="/event",
    )


def _check_interrupt_payload(payload: dict) -> None:
    for key in payload:
        if key not in INTERRUPT_PAYLOAD_KEYS:
            raise EventError(
                f"interrupt payload has unknown key {key!r}; allowed: "
                f"{', '.join(sorted(INTERRUPT_PAYLOAD_KEYS))}. The key set is "
                "closed: an interrupt event never carries the request payload, "
                "the request schema, the resolution input, or free text",
                pointer=_ptr("payload", key),
            )
    for key in _INTERRUPT_REQUIRED_KEYS:
        if key not in payload:
            raise EventError(
                f"interrupt payload needs {key!r}", pointer=_ptr("payload", key)
            )
    _bounded(payload["interrupt_id"], "/payload/interrupt_id")
    kind = payload["kind"]
    if not isinstance(kind, str) or not _NAME_RE.fullmatch(kind):
        raise EventError(
            f"interrupt kind must match [a-z][a-z0-9-]*, got {kind!r}",
            pointer="/payload/kind",
        )
    phase = payload["phase"]
    if not isinstance(phase, str) or phase not in INTERRUPT_PHASES:
        raise EventError(
            f"interrupt phase must be one of {', '.join(sorted(INTERRUPT_PHASES))}, "
            f"got {phase!r}",
            pointer="/payload/phase",
        )
    if "expires_at" in payload:
        _check_utc_z(payload["expires_at"], "interrupt expires_at", "/payload/expires_at")
    if "continuation_no" in payload:
        number = payload["continuation_no"]
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise EventError(
                f"interrupt continuation_no must be an int >= 0, got {number!r}",
                pointer="/payload/continuation_no",
            )


def _check_core_payload(event: str, payload: dict) -> None:
    if event == "usage":
        for key in payload:
            if key not in _USAGE_FIELDS:
                raise EventError(
                    f"usage payload has unknown key {key!r}; build it with usage_payload()",
                    pointer=_ptr("payload", key),
                )
        for key in _USAGE_FIELDS:
            if key not in payload:
                raise EventError(
                    f"usage payload is missing {key!r}; build it with usage_payload()",
                    pointer=_ptr("payload", key),
                )
            value = payload[key]
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise EventError(
                    f"usage {key} must be an int >= 0 or null, got {value!r}",
                    pointer=_ptr("payload", key),
                )
        if all(payload[key] is None for key in _USAGE_FIELDS):
            raise EventError(
                "usage payload needs at least one known token field; "
                "emit no usage event when usage_payload() returns None",
                pointer="/payload",
            )
    elif event == "result":
        status = payload.get("status")
        if not isinstance(status, str) or not status:
            raise EventError(
                "result payload needs status: a non-empty string",
                pointer="/payload/status",
            )
    elif event == "terminal":
        state = payload.get("state")
        if not isinstance(state, str) or not state:
            raise EventError(
                "terminal payload needs state: a non-empty string",
                pointer="/payload/state",
            )
    elif event == "interrupt":
        _check_interrupt_payload(payload)


def _is_supported(schema: Any) -> bool:
    return isinstance(schema, str) and schema in SUPPORTED_SCHEMAS


def _check_schema_selector(schema: Any) -> None:
    if not _is_supported(schema):
        raise EventError(
            f"schema selector {schema!r} is not a supported revision "
            f"({', '.join(sorted(SUPPORTED_SCHEMAS))}); update {OWNER}",
            pointer="/schema",
        )


def validate_event(value: Any) -> dict:
    """Validate one event and return a normalized deep copy.

    Raises ``EventError`` naming the JSON pointer of the first fault.
    """
    if not isinstance(value, Mapping):
        raise EventError(f"an event must be a mapping, got {type(value).__name__}")
    for key in value:
        if key not in _TOP_LEVEL_KEYS:
            raise EventError(
                f"unknown top-level key {key!r}; allowed: {', '.join(_TOP_LEVEL_KEYS)}",
                pointer=_ptr(key),
            )
    for key in _TOP_LEVEL_KEYS:
        if key not in value:
            raise EventError(f"missing required key {key!r}", pointer=_ptr(key))

    schema = value["schema"]
    if not _is_supported(schema):
        raise EventError(
            f"schema {schema!r} is not a revision this module supports "
            f"({', '.join(sorted(SUPPORTED_SCHEMAS))}); update {OWNER}",
            pointer="/schema",
        )

    seq = value["seq"]
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise EventError(f"seq must be an int >= 0, got {seq!r}", pointer="/seq")

    identity = _sub_mapping(value["identity"], "identity", _IDENTITY_KEYS, "run_id")
    for key in _IDENTITY_KEYS:
        if key in identity:
            _bounded(identity[key], _ptr("identity", key))
    if "attempt_id" in identity and "unit_id" not in identity:
        raise EventError(
            "identity.attempt_id requires identity.unit_id",
            pointer="/identity/attempt_id",
        )

    source = _sub_mapping(value["source"], "source", _SOURCE_KEYS, "plugin")
    plugin = source["plugin"]
    if not isinstance(plugin, str) or not _NAME_RE.fullmatch(plugin):
        raise EventError(
            f"source.plugin must match [a-z][a-z0-9-]*, got {plugin!r}",
            pointer="/source/plugin",
        )
    for key in ("adapter", "model"):
        if key in source and (not isinstance(source[key], str) or not source[key]):
            raise EventError(
                f"source.{key} must be a non-empty string", pointer=_ptr("source", key)
            )

    _core, attempt_scoped, _later = _vocabulary(schema)
    event = _check_event_name(value["event"], plugin, schema)
    if event in attempt_scoped and (
        "unit_id" not in identity or "attempt_id" not in identity
    ):
        raise EventError(
            f"{event!r} is attempt-scoped: identity needs unit_id and attempt_id",
            pointer="/identity",
        )
    if event == "terminal" and "attempt_id" in identity:
        raise EventError(
            "'terminal' is unit- or run-scoped: identity must not carry attempt_id",
            pointer="/identity/attempt_id",
        )

    at = _check_at(value["at"])
    payload = _check_payload(value["payload"])
    _check_core_payload(event, payload)

    return {
        "schema": schema,
        "seq": seq,
        "identity": {k: identity[k] for k in _IDENTITY_KEYS if k in identity},
        "event": event,
        "at": at,
        "source": {k: source[k] for k in _SOURCE_KEYS if k in source},
        "payload": payload,
    }


def make_event(
    *,
    seq: int,
    run_id: str,
    event: str,
    plugin: str,
    at: str | None = None,
    unit_id: str | None = None,
    attempt_id: str | None = None,
    adapter: str | None = None,
    model: str | None = None,
    payload: dict | None = None,
    schema: str = SCHEMA_V1,
) -> dict:
    """Build and validate one event. ``at`` defaults to now; ``payload`` to {}.

    ``schema`` selects the revision the event is written under and must be a
    member of ``SUPPORTED_SCHEMAS``.
    """
    _check_schema_selector(schema)
    identity = {"run_id": run_id}
    if unit_id is not None:
        identity["unit_id"] = unit_id
    if attempt_id is not None:
        identity["attempt_id"] = attempt_id
    source = {"plugin": plugin}
    if adapter is not None:
        source["adapter"] = adapter
    if model is not None:
        source["model"] = model
    return validate_event(
        {
            "schema": schema,
            "seq": seq,
            "identity": identity,
            "event": event,
            "at": utc_timestamp() if at is None else at,
            "source": source,
            "payload": {} if payload is None else payload,
        }
    )


def validate_stream(events: Iterable[Any]) -> tuple[dict, ...]:
    """Validate every event, then the ordering and lifecycle rules of a stream.

    Within one ordering group (plugin, run_id, unit_id or none) ``seq`` must
    strictly increase in the given order; the group does not include the
    schema, so ordering spans a stream that mixes revisions. An attempt has
    at most one ``result``; a unit (or the run) has at most one ``terminal``;
    no attempt-scoped event -- judged by the event's own schema -- follows its
    unit's ``terminal``.

    ``interrupt`` events (v2), keyed by (group, attempt_id, interrupt_id):
    the first must be ``requested``; a second ``requested`` is refused; at
    most one closing phase (``resolved``, ``rejected``, ``expired``) is
    allowed; and no ``interrupt`` event follows the close.
    """
    out: list[dict] = []
    seen: set[tuple] = set()
    last_seq: dict[tuple, int] = {}
    results: set[tuple] = set()
    terminals: set[tuple] = set()
    interrupts_open: set[tuple] = set()
    interrupts_closed: set[tuple] = set()
    for index, raw in enumerate(events):
        try:
            item = validate_event(raw)
        except EventError as exc:
            raise EventError(f"event {index}: {exc}", pointer=_ptr(index) + exc.pointer) from exc
        identity = item["identity"]
        group = (item["source"]["plugin"], identity["run_id"], identity.get("unit_id"))
        seq = item["seq"]
        if (group, seq) in seen:
            raise EventError(
                f"event {index}: duplicate seq {seq} in group {group!r}",
                pointer=_ptr(index, "seq"),
            )
        if group in last_seq and seq <= last_seq[group]:
            raise EventError(
                f"event {index}: seq {seq} does not increase after "
                f"{last_seq[group]} in group {group!r}",
                pointer=_ptr(index, "seq"),
            )
        seen.add((group, seq))
        last_seq[group] = seq
        name = item["event"]
        _core, attempt_scoped, _later = _vocabulary(item["schema"])
        if name in attempt_scoped and group in terminals:
            raise EventError(
                f"event {index}: attempt-scoped {name!r} after the unit's terminal "
                f"in group {group!r}",
                pointer=_ptr(index, "event"),
            )
        if name == "result":
            attempt = group + (identity["attempt_id"],)
            if attempt in results:
                raise EventError(
                    f"event {index}: second result for attempt {attempt!r}",
                    pointer=_ptr(index, "event"),
                )
            results.add(attempt)
        elif name == "terminal":
            if group in terminals:
                raise EventError(
                    f"event {index}: second terminal for {group!r}",
                    pointer=_ptr(index, "event"),
                )
            terminals.add(group)
        elif name == "interrupt":
            _check_interrupt_order(
                index, group, identity, item["payload"], interrupts_open, interrupts_closed
            )
        out.append(item)
    return tuple(out)


def _check_interrupt_order(
    index: int,
    group: tuple,
    identity: dict,
    payload: dict,
    opened: set[tuple],
    closed: set[tuple],
) -> None:
    key = group + (identity["attempt_id"], payload["interrupt_id"])
    phase = payload["phase"]
    pointer = _ptr(index, "payload", "phase")
    if key not in opened:
        if phase != "requested":
            raise EventError(
                f"event {index}: interrupt {phase!r} before its request for {key!r}",
                pointer=pointer,
            )
        opened.add(key)
        return
    if key in closed and phase in _INTERRUPT_CLOSING:
        raise EventError(
            f"event {index}: second interrupt close ({phase!r}) for {key!r}",
            pointer=pointer,
        )
    if key in closed:
        raise EventError(
            f"event {index}: interrupt {phase!r} after its close for {key!r}",
            pointer=pointer,
        )
    if phase == "requested":
        raise EventError(
            f"event {index}: second interrupt request for {key!r}",
            pointer=pointer,
        )
    closed.add(key)


# --------------------------------------------------------------------------
# Emitter and sinks
# --------------------------------------------------------------------------


class Emitter:
    """A stream bound to one plugin and run (and optionally one unit).

    ``emit`` assigns the next ``seq`` and writes the event to every sink.
    One lock is held across seq assignment and every sink write, so each
    sink receives events in seq order even when several threads emit.
    """

    def __init__(
        self,
        plugin: str,
        run_id: str,
        *,
        unit_id: str | None = None,
        sinks: Iterable[Any] = (),
        start_seq: int = 0,
        schema: str = SCHEMA_V1,
    ) -> None:
        _check_schema_selector(schema)
        if isinstance(start_seq, bool) or not isinstance(start_seq, int) or start_seq < 0:
            raise EventError(f"start_seq must be an int >= 0, got {start_seq!r}", pointer="/seq")
        if not isinstance(plugin, str) or not _NAME_RE.fullmatch(plugin):
            raise EventError(
                f"plugin must match [a-z][a-z0-9-]*, got {plugin!r}",
                pointer="/source/plugin",
            )
        _bounded(run_id, "/identity/run_id")
        if unit_id is not None:
            _bounded(unit_id, "/identity/unit_id")
        self._plugin = plugin
        self._run_id = run_id
        self._unit_id = unit_id
        self._sinks = tuple(sinks)
        self._schema = schema
        self._next_seq = start_seq
        self._lock = threading.Lock()

    def emit(
        self,
        event: str,
        *,
        unit_id: str | None = None,
        attempt_id: str | None = None,
        adapter: str | None = None,
        model: str | None = None,
        payload: dict | None = None,
        at: str | None = None,
    ) -> dict:
        """Build, number, and record one event; return it.

        ``unit_id`` overrides the bound unit for this event. An invalid event
        raises ``EventError`` and consumes no seq.
        """
        with self._lock:
            record = make_event(
                seq=self._next_seq,
                run_id=self._run_id,
                event=event,
                plugin=self._plugin,
                at=at,
                unit_id=self._unit_id if unit_id is None else unit_id,
                attempt_id=attempt_id,
                adapter=adapter,
                model=model,
                payload=payload,
                schema=self._schema,
            )
            self._next_seq += 1
            for sink in self._sinks:
                sink.write(record)
            return record


class InMemorySink:
    """Collects events in ``events``, in write order."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def write(self, event: dict) -> None:
        self.events.append(event)


_JSONL_MODES = ("create", "truncate")


class JsonlSink:
    """Writes one sorted-key ASCII JSON line per event, flushed per write.

    ``mode="create"`` (the default) refuses an existing file with
    ``FileExistsError``. ``mode="truncate"`` replaces any existing file, so
    the file records its writer's last execution. Appending to an earlier
    stream is not supported: it would hold two terminals for one unit. A file
    has one writer.
    """

    def __init__(self, path: str | os.PathLike, *, mode: str = "create") -> None:
        if mode not in _JSONL_MODES:
            raise ValueError(f"mode must be one of {_JSONL_MODES}, got {mode!r}")
        self._path = os.fspath(path)
        open_mode = "x" if mode == "create" else "w"
        with open(self._path, open_mode, encoding="ascii", newline="\n"):
            pass

    def write(self, event: dict) -> None:
        record = validate_event(event)
        line = json.dumps(record, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        with open(self._path, "a", encoding="ascii", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON-native")


def read_jsonl(path: str | os.PathLike) -> tuple[dict, ...]:
    """Read and validate a JSONL event file written by ``JsonlSink``.

    Every line is validated as an event, and the whole file as one stream.
    ``EventError`` names the 1-based line number of the first fault.
    """
    with open(os.fspath(path), encoding="utf-8", newline="") as handle:
        lines = handle.read().split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    events: list[dict] = []
    for number, line in enumerate(lines, start=1):
        try:
            value = json.loads(line, parse_constant=_refuse_constant)
        except ValueError as exc:
            raise EventError(f"line {number}: not a JSON value: {exc}") from exc
        try:
            events.append(validate_event(value))
        except EventError as exc:
            raise EventError(f"line {number}: {exc}", pointer=exc.pointer) from exc
    try:
        return validate_stream(events)
    except EventError as exc:
        index_text = exc.pointer.split("/")[1] if exc.pointer.count("/") >= 1 else ""
        line_text = f"line {int(index_text) + 1}" if index_text.isdigit() else "stream"
        raise EventError(f"{line_text}: {exc}", pointer=exc.pointer) from exc


__all__ = [
    "ATTEMPT_SCOPED",
    "ATTEMPT_SCOPED_V2",
    "CORE_EVENTS",
    "CORE_EVENTS_V2",
    "Emitter",
    "EventError",
    "INTERRUPT_PAYLOAD_KEYS",
    "INTERRUPT_PHASES",
    "InMemorySink",
    "JsonlSink",
    "LATER_REVISION_NAMES",
    "MAX_PAYLOAD_BYTES",
    "OWNER",
    "SCHEMA_V1",
    "SCHEMA_V2",
    "SUPPORTED_SCHEMAS",
    "make_event",
    "read_jsonl",
    "usage_payload",
    "utc_timestamp",
    "validate_event",
    "validate_stream",
]
