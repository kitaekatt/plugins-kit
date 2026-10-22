"""The shared interrupt contract: request shape, validation rules, resolution.

A durable interrupt is a healthy wait: running work asks a typed question,
its store records the request, and a later answer, rejection or lapse is
recorded once. Each plugin that implements such a wait keeps its own store.
What the stores share is this contract: the request's shape, every field
rule and limit, the canonical form, the decision and outcome words, the
replay test, the lapse rule and the resolution document. The contract is
specified in the plugin-dev skill's ``references/interrupt-contract.md``;
this module is its one implementation.

Contract ``plugins-kit.interrupt-contract/v1`` (``CONTRACT_V1``) is frozen:
its rules, limits and message text never change. A later revision enters
only through a later literal that ``SUPPORTED_CONTRACTS`` gains.

Every function takes plain JSON-native values and returns plain values. The
module defines no record type, opens no file, knows no store, and emits no
event: which event a store writes for a recorded fact is that store's own
choice.

Request schemas are checked and answers validated by a validator the CALLER
passes in. ``VALIDATOR_MODULE`` names the module that is meant and
``VALIDATOR_SUBSET`` the frozen JSON Schema subset ``CONTRACT_V1`` requires;
both are strings, and this module imports neither. A validator that does not
advertise that subset in its ``SUPPORTED_SUBSETS`` is refused whatever its
signatures, and every validator call selects the subset by name.

The module is stdlib-only and imports nothing from ``bootstrap_lib``: it is
linked into venvs that carry no third-party dependency, and a process that
holds an older copy of a sibling module cannot disagree with it.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import re
from typing import Any, Collection, Mapping, Optional


OWNER = "bootstrap@plugins-kit"

# FROZEN. Never reassigned; a later revision adds its own constant.
CONTRACT_V1 = "plugins-kit.interrupt-contract/v1"

# The capability marker a consumer probes. It only ever grows.
SUPPORTED_CONTRACTS = frozenset({CONTRACT_V1})

# FROZEN. The request `schema` literal a store with no literal of its own
# accepts for the v1 request shape.
REQUEST_ENVELOPE_V1 = "plugins-kit.interrupt-request/v1"

# FROZEN. The resolution-document `schema` literal such a store writes.
RESOLUTION_ENVELOPE_V1 = "plugins-kit.interrupt-resolution/v1"

# The validator CONTRACT_V1 is validated with. A name, never imported here.
VALIDATOR_MODULE = "llm_scripting_kit.completion.json_schema"

# The JSON Schema subset CONTRACT_V1 requires of that validator. FROZEN.
VALIDATOR_SUBSET = "llm-scripting-kit.json-schema-subset/v1"

# The top-level keys of a request: all of the first, any of the second.
REQUEST_KEYS = ("schema", "kind", "request_schema", "payload")
OPTIONAL_REQUEST_KEYS = ("expires_in_s",)

# The largest request document, in bytes.
REQUEST_DOCUMENT_LIMIT = 131072

# The largest resolution input, in bytes of canonical JSON.
INPUT_LIMIT = 65536

# The longest interrupt `kind`.
KIND_LIMIT = 64

# The largest `expires_in_s`.
EXPIRES_IN_S_MAX = 2**31 - 1

# The longest recorded rejection reason, in characters.
REASON_LIMIT = 2000

# The three ways an interrupt closes.
OUTCOMES = frozenset({"answered", "rejected", "expired"})

# The decision a resolver names, and the outcome it records.
DECISIONS = (("answer", "answered"), ("reject", "rejected"))

_KIND_RE = re.compile(r"[a-z][a-z0-9-]*")
_RESOLUTION_ENVELOPE_RE = re.compile(r"[a-z][a-z0-9-]*\.interrupt-resolution/v1")


class ContractError(ValueError):
    """A value breaks the interrupt contract."""


class RequestError(ContractError):
    """An interrupt request, or a request document, is invalid."""


class InputError(ContractError):
    """A resolution input is refused.

    ``errors`` holds the validator's ``(json_pointer, keyword)`` tuples when
    the input failed the request schema, and is empty when the input was
    refused before validation (not JSON-native, or over the size cap).
    """

    def __init__(self, message: str, errors: tuple = ()) -> None:
        self.errors = tuple(errors)
        super().__init__(message)


class DecisionError(ContractError):
    """A decision word, or the arguments given with it, is refused."""


class ValidatorError(ContractError):
    """The validator passed in cannot validate under ``VALIDATOR_SUBSET``."""


def check_validator(validator: Any) -> None:
    """Refuse a validator that cannot be held to ``VALIDATOR_SUBSET``.

    The validator must advertise ``VALIDATOR_SUBSET`` in a
    ``SUPPORTED_SUBSETS`` collection (a set, frozenset, tuple or list; a bare
    string is not one) and have callable ``check_schema`` and
    ``validate``. Matching signatures are not enough: a validator with no
    marker is refused, because nothing says which keyword set it applies.
    Raises :class:`ValidatorError` naming the required literal and what the
    validator advertises.
    """
    advertised = getattr(validator, "SUPPORTED_SUBSETS", None)
    required = (
        f"interrupt contract {CONTRACT_V1} requires a schema validator that "
        f"advertises {VALIDATOR_SUBSET!r} in SUPPORTED_SUBSETS"
    )
    if not isinstance(advertised, (frozenset, set, tuple, list)):
        raise ValidatorError(
            f"{required}; this validator has no SUPPORTED_SUBSETS collection"
        )
    if VALIDATOR_SUBSET not in advertised:
        named = ", ".join(sorted(repr(item) for item in advertised)) or "nothing"
        raise ValidatorError(f"{required}; this validator advertises {named}")
    missing = [
        name
        for name in ("check_schema", "validate")
        if not callable(getattr(validator, name, None))
    ]
    if missing:
        raise ValidatorError(
            f"interrupt contract {CONTRACT_V1} requires a schema validator with "
            f"callable check_schema and validate; this validator lacks a "
            f"callable {' and '.join(missing)}"
        )


def canonical_json(value: object) -> str:
    """The canonical text of a JSON value: sorted keys, compact, ASCII.

    Identical values give byte-identical text, whatever their key order.
    Raises ``ValueError`` for NaN or infinity.
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _json_native_fault(value: Any, pointer: str = "") -> Optional[str]:
    """Name the first non-JSON-native part of ``value``, or ``None``."""
    if value is None or isinstance(value, (bool, str)):
        return None
    if isinstance(value, int):
        return None
    if isinstance(value, float):
        return None if math.isfinite(value) else f"{pointer or '/'}: {value!r} is not finite"
    if isinstance(value, list):
        for index, item in enumerate(value):
            fault = _json_native_fault(item, f"{pointer}/{index}")
            if fault is not None:
                return fault
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                return f"{pointer or '/'}: key {key!r} is not a string"
            escaped = key.replace("~", "~0").replace("/", "~1")
            fault = _json_native_fault(item, f"{pointer}/{escaped}")
            if fault is not None:
                return fault
        return None
    return f"{pointer or '/'}: {type(value).__name__} is not a JSON value"


def check_request(
    *,
    envelope: Any,
    kind: Any,
    request_schema: Any,
    payload: Any,
    expires_in_s: Any = None,
    accepted_envelopes: Collection[str],
    owner: str,
    validator: Any,
) -> dict:
    """Validate the fields of one interrupt request.

    ``accepted_envelopes`` is the set of request ``schema`` literals the
    calling store accepts, and ``owner`` names that store's plugin in a
    refusal. Returns ``envelope``, ``kind``, ``request_schema``, ``payload``
    and ``expires_in_s`` in a new dict, with ``request_schema`` and
    ``payload`` as canonical deep copies. Raises :class:`ValidatorError`
    before any field is read when the validator is unusable, and
    :class:`RequestError` naming the first fault otherwise.
    """
    check_validator(validator)
    if isinstance(accepted_envelopes, str):
        raise TypeError("accepted_envelopes must be a collection of literals, not a string")
    if not isinstance(envelope, str) or envelope not in accepted_envelopes:
        accepted = ", ".join(repr(item) for item in sorted(accepted_envelopes))
        raise RequestError(
            f"interrupt request schema {envelope!r} is not accepted; "
            f"this {owner} accepts {accepted}"
        )
    if not isinstance(kind, str) or not _KIND_RE.fullmatch(kind) or len(kind) > KIND_LIMIT:
        raise RequestError(
            f"interrupt kind must match [a-z][a-z0-9-]* and be at most "
            f"{KIND_LIMIT} characters, got {kind!r}"
        )
    if not isinstance(request_schema, dict):
        raise RequestError("interrupt request_schema must be a JSON object")
    if not isinstance(payload, dict):
        raise RequestError("interrupt payload must be a JSON object")
    for label, value in (("request_schema", request_schema), ("payload", payload)):
        fault = _json_native_fault(value)
        if fault is not None:
            raise RequestError(f"interrupt {label} is not JSON-native: {fault}")
    if expires_in_s is not None and (
        isinstance(expires_in_s, bool)
        or not isinstance(expires_in_s, int)
        or not 1 <= expires_in_s <= EXPIRES_IN_S_MAX
    ):
        raise RequestError(
            f"interrupt expires_in_s must be an int from 1 to {EXPIRES_IN_S_MAX}, "
            f"got {expires_in_s!r}"
        )
    try:
        validator.check_schema(request_schema, subset=VALIDATOR_SUBSET)
    except ValueError as exc:
        raise RequestError(
            f"interrupt request_schema is outside the supported JSON Schema "
            f"subset: {exc}"
        ) from exc
    return {
        "envelope": envelope,
        "kind": kind,
        "request_schema": json.loads(canonical_json(request_schema)),
        "payload": json.loads(canonical_json(payload)),
        "expires_in_s": expires_in_s,
    }


def check_request_mapping(
    raw: Any,
    *,
    accepted_envelopes: Collection[str],
    owner: str,
    validator: Any,
) -> dict:
    """Validate one request given as a mapping with the request's own keys.

    The top-level keys are exactly ``REQUEST_KEYS``, plus any of
    ``OPTIONAL_REQUEST_KEYS``; the key set is closed. The fields then go to
    :func:`check_request`, whose result this returns.
    """
    if not isinstance(raw, Mapping):
        raise RequestError("interrupt request must be a JSON object")
    allowed = REQUEST_KEYS + OPTIONAL_REQUEST_KEYS
    unknown = sorted(
        (key for key in raw if key not in allowed),
        key=lambda key: (not isinstance(key, str), str(key)),
    )
    if unknown:
        raise RequestError(
            f"interrupt request has unknown keys {unknown}; allowed: "
            f"{', '.join(allowed)}"
        )
    missing = [key for key in REQUEST_KEYS if key not in raw]
    if missing:
        raise RequestError(f"interrupt request is missing keys {missing}")
    return check_request(
        envelope=raw["schema"],
        kind=raw["kind"],
        request_schema=raw["request_schema"],
        payload=raw["payload"],
        expires_in_s=raw.get("expires_in_s"),
        accepted_envelopes=accepted_envelopes,
        owner=owner,
        validator=validator,
    )


def _refuse_constant(name: str) -> Any:
    raise RequestError(f"interrupt request holds {name}, which is not JSON")


def _refuse_duplicate_keys(pairs: list) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise RequestError(f"interrupt request repeats the key {key!r}")
        result[key] = value
    return result


def parse_request_document(
    data: bytes,
    *,
    accepted_envelopes: Collection[str],
    owner: str,
    validator: Any,
) -> dict:
    """Parse and validate the bytes of one interrupt request document.

    The document is UTF-8 JSON of at most ``REQUEST_DOCUMENT_LIMIT`` bytes
    holding one object; NaN, infinity and a repeated key are refused. The
    object then goes to :func:`check_request_mapping`, whose result this
    returns. Reading the bytes from wherever they are kept is the caller's.
    """
    if len(data) > REQUEST_DOCUMENT_LIMIT:
        raise RequestError(
            f"interrupt request file is larger than {REQUEST_DOCUMENT_LIMIT} bytes"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RequestError(f"interrupt request file is not UTF-8: {exc}") from exc
    try:
        raw = json.loads(
            text,
            parse_constant=_refuse_constant,
            object_pairs_hook=_refuse_duplicate_keys,
        )
    except json.JSONDecodeError as exc:
        raise RequestError(f"interrupt request file is not JSON: {exc}") from exc
    return check_request_mapping(
        raw,
        accepted_envelopes=accepted_envelopes,
        owner=owner,
        validator=validator,
    )


def validate_input(request_schema: Mapping[str, object], value: object, *, validator: Any) -> str:
    """Validate a resolution input against its request schema.

    Returns the input's canonical JSON text. Raises :class:`InputError`:
    with empty ``errors`` when the input is not JSON-native or its canonical
    form exceeds ``INPUT_LIMIT`` bytes, and with the validator's
    ``(json_pointer, keyword)`` tuples verbatim when it fails the schema.
    Raises :class:`ValidatorError` first when the validator is unusable.
    """
    check_validator(validator)
    fault = _json_native_fault(value)
    if fault is not None:
        raise InputError(f"resolution input is not JSON-native: {fault}")
    text = canonical_json(value)
    size = len(text.encode("ascii"))
    if size > INPUT_LIMIT:
        raise InputError(
            f"resolution input is {size} bytes as canonical JSON; the cap is "
            f"{INPUT_LIMIT}"
        )
    errors = tuple(validator.validate(request_schema, value, subset=VALIDATOR_SUBSET))
    if errors:
        raise InputError("resolution input does not satisfy the request schema", errors)
    return text


def expiry(created_at: float, expires_in_s: Optional[int]) -> Optional[float]:
    """When a request recorded at ``created_at`` lapses, or ``None`` for never."""
    return created_at + expires_in_s if expires_in_s is not None else None


def lapsed(expires_at: Optional[float], now: float) -> bool:
    """Whether an interrupt expiring at ``expires_at`` has lapsed at ``now``.

    Inclusive: an interrupt lapses AT its ``expires_at``. An interrupt with
    no expiry never lapses.
    """
    return expires_at is not None and now >= expires_at


def bound_reason(reason: object) -> Optional[str]:
    """The reason as recorded: its text cut to ``REASON_LIMIT`` characters."""
    return str(reason)[:REASON_LIMIT] if reason is not None else None


def decision_outcome(decision: object, *, input: object = None, reason: object = None) -> str:
    """The outcome a decision records: ``answer`` or ``reject``.

    An answer carries an input and no reason; a rejection carries an
    optional reason and no input. Raises :class:`DecisionError` for an
    unknown decision and for crossed arguments.
    """
    outcomes = dict(DECISIONS)
    outcome = outcomes.get(decision) if isinstance(decision, str) else None
    if outcome is None:
        raise DecisionError(
            f"decision must be one of {', '.join(sorted(outcomes))}, "
            f"got {decision!r}"
        )
    if outcome == "rejected" and input is not None:
        raise DecisionError("a rejection carries a reason, not an input")
    if outcome == "answered" and reason is not None:
        raise DecisionError("an answer carries an input, not a reason")
    return outcome


def same_resolution(
    *,
    stored_outcome: str,
    stored_input_json: Optional[str],
    stored_reason: Optional[str],
    outcome: str,
    input: object = None,
    reason: object = None,
) -> bool:
    """Whether a resolution replays the stored one.

    True when the outcome is the stored outcome and, for an answer, the
    input's canonical JSON equals the stored text; otherwise, the bounded
    reason equals the stored reason. An input with no canonical form
    compares unequal. False is a conflict, never a second resolution.
    """
    if stored_outcome != outcome:
        return False
    if outcome == "answered":
        try:
            candidate = canonical_json(input)
        except (TypeError, ValueError):
            return False
        return candidate == stored_input_json
    return bound_reason(reason) == stored_reason


def _utc_text(epoch: float) -> str:
    """Render a recorded epoch as an ISO-8601 UTC string ending in ``Z``."""
    moment = _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc)
    text = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond:
        text += f".{moment.microsecond:06d}"
    return text + "Z"


def resolution_document(
    *,
    resolution_envelope: str,
    interrupt_id: object,
    kind: str,
    outcome: str,
    input: object,
    payload: object,
    resolved_at: float,
) -> str:
    """The canonical resolution document for a resolved interrupt.

    Exactly seven keys: ``schema`` (``resolution_envelope``),
    ``interrupt_id``, ``kind``, ``outcome``, ``input``, ``payload`` and
    ``resolved_at`` (the recorded epoch, rendered as UTC ending in ``Z``).
    Built only from recorded values, it is byte-identical on every call.
    ``resolution_envelope`` is the store's own v1 resolution literal,
    ``<name>.interrupt-resolution/v1``; anything else raises
    :class:`RequestError`. An ``outcome`` outside ``OUTCOMES`` raises
    :class:`ContractError`.
    """
    if not isinstance(resolution_envelope, str) or not _RESOLUTION_ENVELOPE_RE.fullmatch(
        resolution_envelope
    ):
        raise RequestError(
            f"interrupt resolution schema {resolution_envelope!r} is not a v1 "
            f"resolution literal: <name>.interrupt-resolution/v1, where <name> "
            f"matches [a-z][a-z0-9-]*"
        )
    if not isinstance(outcome, str) or outcome not in OUTCOMES:
        raise ContractError(
            f"interrupt outcome must be one of {', '.join(sorted(OUTCOMES))}, "
            f"got {outcome!r}"
        )
    return canonical_json(
        {
            "schema": resolution_envelope,
            "interrupt_id": interrupt_id,
            "kind": kind,
            "outcome": outcome,
            "input": input,
            "payload": payload,
            "resolved_at": _utc_text(resolved_at),
        }
    )


__all__ = [
    "CONTRACT_V1",
    "DECISIONS",
    "EXPIRES_IN_S_MAX",
    "INPUT_LIMIT",
    "KIND_LIMIT",
    "OPTIONAL_REQUEST_KEYS",
    "OUTCOMES",
    "OWNER",
    "REASON_LIMIT",
    "REQUEST_DOCUMENT_LIMIT",
    "REQUEST_ENVELOPE_V1",
    "REQUEST_KEYS",
    "RESOLUTION_ENVELOPE_V1",
    "SUPPORTED_CONTRACTS",
    "VALIDATOR_MODULE",
    "VALIDATOR_SUBSET",
    "ContractError",
    "DecisionError",
    "InputError",
    "RequestError",
    "ValidatorError",
    "bound_reason",
    "canonical_json",
    "check_request",
    "check_request_mapping",
    "check_validator",
    "decision_outcome",
    "expiry",
    "lapsed",
    "parse_request_document",
    "resolution_document",
    "same_resolution",
    "validate_input",
]
