"""Durable interrupt requests and resolutions: the two file contracts.

A contract asks an operator a question by writing ONE JSON file, the
interrupt request (``job-kit.interrupt-request/v1``), at the path job-kit
exports as ``JOB_KIT_INTERRUPT_REQUEST``. stdout, stderr and exit text are
never read for state. job-kit records the request in its ledger, the job
waits, and an operator's answer is validated against the schema the request
declared. When job-kit re-runs the contract after an answer, it hands it the
resolution document (``job-kit.interrupt-resolution/v1``).

Schemas are checked and answers validated with llm-scripting-kit's
stdlib-only closed-subset validator (``llm_scripting_kit.completion.
json_schema``), reached only through :func:`_schema_validator`. That edge is
REQUIRED: a job-kit that recorded a wait it could not validate would record
an unchecked wait, so an absent or too-old validator refuses with a
diagnosis instead.
"""

from __future__ import annotations

import datetime as _dt
import importlib
import inspect
import json
import math
import re
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping, Optional

from .model import InterruptRecord, InterruptRequest


#: The request-file schema literal this job-kit accepts. FROZEN; a later
#: envelope version is a separate literal that :func:`parse_request` gains.
REQUEST_ENVELOPE_V1 = "job-kit.interrupt-request/v1"

#: The resolution-document schema literal job-kit writes. FROZEN.
RESOLUTION_ENVELOPE_V1 = "job-kit.interrupt-resolution/v1"

#: Every request envelope this job-kit accepts.
REQUEST_ENVELOPES = frozenset({REQUEST_ENVELOPE_V1})

#: The largest request file, in bytes.
REQUEST_FILE_LIMIT = 131072

#: The largest resolution input, in bytes of canonical JSON.
INPUT_LIMIT = 65536

#: The longest interrupt ``kind``.
KIND_LIMIT = 64

#: The largest ``expires_in_s``.
EXPIRES_IN_S_MAX = 2**31 - 1

#: The llm-scripting-kit release that shipped ``completion.json_schema`` with
#: ``check_schema(schema)`` and ``validate(schema, value)``. Messages name this
#: constant, never a value read from a possibly stale module.
_JSON_SCHEMA_LSK_VERSION = "0.56.0"

_REQUIRED_KEYS = ("schema", "kind", "request_schema", "payload")
_OPTIONAL_KEYS = ("expires_in_s",)
_KIND_RE = re.compile(r"[a-z][a-z0-9-]*")


class InterruptRequestError(ValueError):
    """An interrupt request file, or a request built by a caller, is invalid."""


class InterruptInputError(ValueError):
    """A resolution input is refused.

    ``errors`` holds the validator's ``(json_pointer, keyword)`` tuples when
    the input failed the request schema, and is empty when the input was
    refused before validation (not JSON-native, or over the size cap).
    """

    def __init__(self, message: str, errors: tuple = ()) -> None:
        self.errors = tuple(errors)
        super().__init__(message)


class JsonSchemaSupportError(ImportError):
    """``llm_scripting_kit.completion.json_schema`` is absent, too old, or stale."""


def _schema_validator() -> ModuleType:
    """Return the usable json_schema module, or raise a diagnosis.

    Absent (``llm_scripting_kit`` does not import) and too old or stale (the
    submodule, ``check_schema`` or ``validate`` missing, or either call shape
    job-kit uses not binding) produce different messages with different
    remedies. Callers run it before any ledger open or write.
    """
    try:
        importlib.import_module("llm_scripting_kit")
    except ModuleNotFoundError as exc:
        raise JsonSchemaSupportError(
            "job-kit validates interrupt requests with llm-scripting-kit, which "
            "is not linked into this environment: install or enable it "
            "(`claude plugin install llm-scripting-kit@plugins-kit`) and start a "
            "new session so bootstrap links job-kit's shared libs."
        ) from exc
    too_old = (
        "job-kit validates interrupt requests with "
        "llm_scripting_kit.completion.json_schema (check_schema and validate), "
        "which the linked llm-scripting-kit predates or lacks: update "
        f"llm-scripting-kit to >= {_JSON_SCHEMA_LSK_VERSION} "
        "(`claude plugin update llm-scripting-kit@plugins-kit`) and restart."
    )
    try:
        module = importlib.import_module("llm_scripting_kit.completion.json_schema")
    except ImportError as exc:
        raise JsonSchemaSupportError(too_old) from exc
    check_schema = getattr(module, "check_schema", None)
    validate = getattr(module, "validate", None)
    if not callable(check_schema) or not callable(validate):
        raise JsonSchemaSupportError(too_old)
    try:
        check_signature = inspect.signature(check_schema)
        validate_signature = inspect.signature(validate)
    except ValueError as exc:  # a callable with no introspectable signature
        raise JsonSchemaSupportError(too_old) from exc
    try:
        check_signature.bind({})
        validate_signature.bind({}, None)
    except TypeError as exc:
        raise JsonSchemaSupportError(too_old) from exc
    return module


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


def check_request(request: InterruptRequest) -> InterruptRequest:
    """Validate a request built from a file or by a library caller.

    Returns the request with ``request_schema`` and ``payload`` as canonical
    deep copies. Raises :class:`InterruptRequestError` naming the fault, and
    :class:`JsonSchemaSupportError` when the validator is unusable.
    """
    if not isinstance(request.envelope, str) or request.envelope not in REQUEST_ENVELOPES:
        raise InterruptRequestError(
            f"interrupt request schema {request.envelope!r} is not accepted; "
            f"this job-kit accepts {REQUEST_ENVELOPE_V1!r}"
        )
    kind = request.kind
    if not isinstance(kind, str) or not _KIND_RE.fullmatch(kind) or len(kind) > KIND_LIMIT:
        raise InterruptRequestError(
            f"interrupt kind must match [a-z][a-z0-9-]* and be at most "
            f"{KIND_LIMIT} characters, got {kind!r}"
        )
    if not isinstance(request.request_schema, dict):
        raise InterruptRequestError("interrupt request_schema must be a JSON object")
    if not isinstance(request.payload, dict):
        raise InterruptRequestError("interrupt payload must be a JSON object")
    for label, value in (
        ("request_schema", request.request_schema),
        ("payload", request.payload),
    ):
        fault = _json_native_fault(value)
        if fault is not None:
            raise InterruptRequestError(
                f"interrupt {label} is not JSON-native: {fault}"
            )
    expires = request.expires_in_s
    if expires is not None and (
        isinstance(expires, bool)
        or not isinstance(expires, int)
        or not 1 <= expires <= EXPIRES_IN_S_MAX
    ):
        raise InterruptRequestError(
            f"interrupt expires_in_s must be an int from 1 to {EXPIRES_IN_S_MAX}, "
            f"got {expires!r}"
        )
    validator = _schema_validator()
    try:
        validator.check_schema(request.request_schema)
    except ValueError as exc:
        raise InterruptRequestError(
            f"interrupt request_schema is outside the supported JSON Schema "
            f"subset: {exc}"
        ) from exc
    return InterruptRequest(
        envelope=request.envelope,
        kind=kind,
        request_schema=json.loads(canonical_json(request.request_schema)),
        payload=json.loads(canonical_json(request.payload)),
        expires_in_s=expires,
    )


def _refuse_constant(name: str) -> Any:
    raise InterruptRequestError(f"interrupt request holds {name}, which is not JSON")


def _refuse_duplicate_keys(pairs: list) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise InterruptRequestError(f"interrupt request repeats the key {key!r}")
        result[key] = value
    return result


def parse_request(path: str | Path) -> InterruptRequest:
    """Read, parse and validate one interrupt request file.

    The file is UTF-8 JSON of at most :data:`REQUEST_FILE_LIMIT` bytes whose
    top-level keys are exactly ``schema``, ``kind``, ``request_schema`` and
    ``payload``, plus an optional ``expires_in_s``. Raises
    :class:`InterruptRequestError` naming the fault.
    """
    file_path = Path(path)
    try:
        with file_path.open("rb") as handle:
            data = handle.read(REQUEST_FILE_LIMIT + 1)
    except OSError as exc:
        raise InterruptRequestError(
            f"interrupt request file cannot be read: {exc}"
        ) from exc
    if len(data) > REQUEST_FILE_LIMIT:
        raise InterruptRequestError(
            f"interrupt request file is larger than {REQUEST_FILE_LIMIT} bytes"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InterruptRequestError(
            f"interrupt request file is not UTF-8: {exc}"
        ) from exc
    try:
        raw = json.loads(
            text,
            parse_constant=_refuse_constant,
            object_pairs_hook=_refuse_duplicate_keys,
        )
    except json.JSONDecodeError as exc:
        raise InterruptRequestError(
            f"interrupt request file is not JSON: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise InterruptRequestError("interrupt request must be a JSON object")
    unknown = sorted(set(raw) - set(_REQUIRED_KEYS) - set(_OPTIONAL_KEYS))
    if unknown:
        raise InterruptRequestError(
            f"interrupt request has unknown keys {unknown}; allowed: "
            f"{', '.join(_REQUIRED_KEYS + _OPTIONAL_KEYS)}"
        )
    missing = [key for key in _REQUIRED_KEYS if key not in raw]
    if missing:
        raise InterruptRequestError(f"interrupt request is missing keys {missing}")
    return check_request(
        InterruptRequest(
            envelope=raw["schema"],
            kind=raw["kind"],
            request_schema=raw["request_schema"],
            payload=raw["payload"],
            expires_in_s=raw.get("expires_in_s"),
        )
    )


def validate_input(schema: Mapping[str, object], value: object) -> str:
    """Validate a resolution input against its request schema.

    Returns the input's canonical JSON text. Raises
    :class:`InterruptInputError`: with empty ``errors`` when the input is not
    JSON-native or its canonical form exceeds :data:`INPUT_LIMIT` bytes, and
    with the validator's ``(json_pointer, keyword)`` tuples verbatim when it
    fails the schema.
    """
    fault = _json_native_fault(value)
    if fault is not None:
        raise InterruptInputError(f"resolution input is not JSON-native: {fault}")
    text = canonical_json(value)
    size = len(text.encode("ascii"))
    if size > INPUT_LIMIT:
        raise InterruptInputError(
            f"resolution input is {size} bytes as canonical JSON; the cap is "
            f"{INPUT_LIMIT}"
        )
    errors = tuple(_schema_validator().validate(schema, value))
    if errors:
        raise InterruptInputError(
            "resolution input does not satisfy the request schema", errors
        )
    return text


def _utc_text(epoch: float) -> str:
    """Render a recorded epoch as an ISO-8601 UTC string ending in ``Z``."""
    moment = _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc)
    text = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond:
        text += f".{moment.microsecond:06d}"
    return text + "Z"


def resolution_document(record: InterruptRecord) -> str:
    """The canonical resolution document for a resolved interrupt.

    Built ONLY from the immutable interrupt row and its immutable resolution
    row (``resolved_at`` is the recorded value), so every run of a
    continuation for one interrupt receives byte-identical text.
    """
    resolution = record.resolution
    if resolution is None:
        raise ValueError(f"interrupt {record.id} has no resolution")
    return canonical_json(
        {
            "schema": RESOLUTION_ENVELOPE_V1,
            "interrupt_id": record.id,
            "kind": record.kind,
            "outcome": resolution.outcome,
            "input": resolution.input,
            "payload": record.payload,
            "resolved_at": _utc_text(resolution.resolved_at),
        }
    )


__all__ = [
    "EXPIRES_IN_S_MAX",
    "INPUT_LIMIT",
    "KIND_LIMIT",
    "REQUEST_ENVELOPES",
    "REQUEST_ENVELOPE_V1",
    "REQUEST_FILE_LIMIT",
    "RESOLUTION_ENVELOPE_V1",
    "InterruptInputError",
    "InterruptRequestError",
    "JsonSchemaSupportError",
    "canonical_json",
    "check_request",
    "parse_request",
    "resolution_document",
    "validate_input",
]
