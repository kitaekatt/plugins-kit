"""The versioned request half of the ``complete`` CLI protocol.

The result half already existed: the ``complete`` verb emits one envelope for a
completed, timed-out or failed call alike. This module is the other direction --
a structured request in, so a non-Python consumer has one surface instead of a
growing wall of flags.

Three things earn a module of its own rather than more argparse:

- **``extras`` cannot be a flag.** It is an open JSON map whose per-key handling
  is observable in the response (``dropped_params`` / ``forwarded_params``), and
  no flag spelling expresses it.
- **This is where declared types become enforced ones.**
  :mod:`.completion.capabilities` says outright that ``ParamCapability.type`` is
  "a DECLARED expectation, not an enforced one" and that "validation belongs at
  the CLI request boundary, not here". This is that boundary. Inside the seam
  every adapter passes raw values through, so a wrong type reaches the transport
  and fails there, wearing the transport's error rather than the caller's
  mistake.
- **An unknown key is an ERROR here, not a drop.** That looks like it
  contradicts the seam's drop-and-report rule, and the difference is the point:
  the seam drops a param the ADAPTER does not read, which is a fact about the
  adapter and is reported truthfully. A key that is not a
  :class:`~.completion.types.BackendOptions` field at all is a fact about the
  CALLER -- nothing advertises it, no adapter could read it, and there is no
  honest per-call report to make. Accepting it silently would be the exact
  silence this whole contract replaces.

Protocol errors are kept distinct from endpoint errors, which is what the
separate exit code is for. A malformed request never reached a model; a failed
call did. Collapsing them would tell a caller to retry a request that can only
fail again, or to fix a request that was fine.
"""
from __future__ import annotations

import collections.abc
import typing
from dataclasses import fields
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from .completion import BackendOptions

PROTOCOL_VERSION = 1
"""The only request/result protocol version this build speaks.

Bumped when a change would make an existing consumer misread a payload --
adding an OPTIONAL response field does not qualify, since the envelope has
always been open for reading. The version exists because the payload's shape has
already moved twice (truthful results, then per-key extras) with nothing on the
wire saying so.
"""


class ProtocolError(ValueError):
    """The request could not be understood, so no call was attempted.

    Deliberately distinct from every endpoint failure: a timeout or a provider
    error describes a call that RAN, and comes back inside the result envelope.
    This one says the caller's payload never got that far.
    """


#: Request keys that select WHAT to call rather than how, mirroring the flags
#: they replace. ``options`` is handled separately because it maps onto
#: BackendOptions field-for-field.
_SELECTION_KEYS = ("endpoint", "model", "cheap", "system", "prompt")

_REQUEST_KEYS = ("protocol",) + _SELECTION_KEYS + ("options",)

#: BackendOptions fields a request may NOT set, with the reason. ``log_prefix``
#: is a diagnostic tag the CLI derives from the resolved endpoint, so letting a
#: request set it would let a caller mislabel its own stderr.
_UNSETTABLE_OPTIONS = {
    "log_prefix": "derived by the CLI from the resolved endpoint",
}


def _option_fields() -> Dict[str, Any]:
    """Settable BackendOptions fields, READ FROM THE DATACLASS.

    Restating them would be a second source of truth free to fall behind the
    seam -- the same rule ``adapter_capabilities`` follows for
    ``dropped_params``. A field added to BackendOptions becomes settable here
    with no edit, which is what keeps the protocol from silently lagging the
    contract it exposes.
    """
    return {
        f.name: f for f in fields(BackendOptions) if f.name not in _UNSETTABLE_OPTIONS
    }


def _option_type_hints() -> Dict[str, Any]:
    """Settable BackendOptions fields' RESOLVED types (not the raw annotation
    string ``from __future__ import annotations`` leaves on ``Field.type``).

    ``_coerce`` / ``_classify`` dispatch on actual type objects
    (``typing.get_origin`` / ``get_args`` / ``isinstance``), which requires
    real types, not ``"Optional[float]"`` strings.
    """
    hints = typing.get_type_hints(BackendOptions)
    return {name: hints[name] for name in _option_fields()}


def _require_mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolError(f"{what} must be a JSON object, got {type(value).__name__}")
    return value


def _unwrap_optional(annotation: Any) -> "Tuple[Any, bool]":
    """Split ``Optional[X]`` (i.e. ``Union[X, None]``) into ``(X, True)``.

    Any other annotation, including a bare ``Union`` of two non-None types
    (unused by BackendOptions today), is returned unchanged with ``False`` --
    :func:`_classify` then raises on it rather than guessing.
    """
    if typing.get_origin(annotation) is typing.Union:
        args = typing.get_args(annotation)
        non_none = [a for a in args if a is not type(None)]  # noqa: E721
        if len(non_none) == 1 and type(None) in args:
            return non_none[0], True
    return annotation, False


def _classify(annotation: Any) -> str:
    """Return the coercion branch for ``annotation``, or raise ProtocolError.

    Dispatches on ``typing.get_origin`` / ``typing.get_args`` plus
    ``isinstance``-style type identity -- never on ``str(annotation)``
    substring matching, which silently accepted (returned unvalidated) any
    type it did not recognize. An annotation this function does not
    recognize is a coercer gap, not a caller mistake, so it raises
    unconditionally rather than falling through to "accept it".
    """
    base, _optional = _unwrap_optional(annotation)
    if base is Path:
        return "path"
    origin = typing.get_origin(base)
    if base in (dict, Mapping) or origin in (dict, Mapping, collections.abc.Mapping):
        return "mapping"
    if base is bool:
        return "bool"
    if base is int:
        return "int"
    if base is float:
        return "float"
    if base is str:
        return "str"
    raise ProtocolError(
        f"unsupported BackendOptions type ({annotation!r}); the request-protocol "
        "coercer has no branch for it -- this is a coercer gap, not a caller mistake"
    )


def _coerce(name: str, value: Any, annotation: Any) -> Any:
    """Check one option value against its declared type, coercing only numbers.

    JSON has one number type, so an integer literal arrives as ``int`` where a
    float is declared; that widening is lossless and is the single coercion
    allowed. Everything else must already be the right shape -- silently
    accepting ``"0.3"`` for a temperature would push the caller's mistake into
    the transport, where it surfaces as the provider's error rather than theirs.
    """
    base, optional = _unwrap_optional(annotation)
    if value is None:
        if optional:
            return None
        raise ProtocolError(f"options.{name} may not be null")

    kind = _classify(annotation)
    if kind == "path":
        if not isinstance(value, str):
            raise ProtocolError(f"options.{name} must be a string path")
        return Path(value)
    if kind == "mapping":
        return dict(_require_mapping(value, f"options.{name}"))
    if kind == "bool":
        if not isinstance(value, bool):
            raise ProtocolError(f"options.{name} must be a boolean")
        return value
    if kind == "int":
        # bool is an int subclass in Python; accepting True for max_tokens
        # would be a silent nonsense the caller never sees again.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProtocolError(f"options.{name} must be an integer")
        return value
    if kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProtocolError(f"options.{name} must be a number")
        return float(value)
    # kind == "str"
    if not isinstance(value, str):
        raise ProtocolError(f"options.{name} must be a string")
    return value


def _ensure_coercible(cls: Any = None) -> None:
    """Assert every field of ``cls`` (default: BackendOptions) resolves to a
    known coercion branch, raising ProtocolError at the first unhandled one.

    Run at import time against the real BackendOptions (see the bottom of
    this module) so a future field of an unhandled type (a bare List/Tuple, a
    non-Optional Union, ...) fails loudly at import/first use instead of
    silently accepting unvalidated request values for it.
    """
    target = cls if cls is not None else BackendOptions
    hints = typing.get_type_hints(target)
    for f in fields(target):
        _classify(hints[f.name])


def parse_options(raw: Any) -> Dict[str, Any]:
    """Validate a request's ``options`` object into BackendOptions kwargs."""
    mapping = _require_mapping(raw, "options")
    settable = _option_fields()
    unknown = sorted(set(mapping) - set(settable))
    if unknown:
        rejected = [
            f"{name} ({_UNSETTABLE_OPTIONS[name]})"
            if name in _UNSETTABLE_OPTIONS
            else name
            for name in unknown
        ]
        raise ProtocolError(
            "unknown option(s): "
            + ", ".join(rejected)
            + "; known options are "
            + ", ".join(sorted(settable))
        )
    type_hints = _option_type_hints()
    return {
        name: _coerce(name, value, type_hints[name])
        for name, value in mapping.items()
    }


class CompletionRequest:
    """One parsed, validated ``complete`` request.

    Plain attributes rather than a dataclass because the selection half is
    deliberately loose -- every field is optional and resolves through the same
    endpoint/model machinery the flags use, so there is nothing to validate
    beyond its type.
    """

    def __init__(
        self,
        *,
        endpoint: Optional[str] = None,
        model: Optional[str] = None,
        cheap: bool = False,
        system: str = "",
        prompt: str = "",
        options: Optional[BackendOptions] = None,
    ) -> None:
        self.endpoint = endpoint
        self.model = model
        self.cheap = cheap
        self.system = system
        self.prompt = prompt
        self.options = options or BackendOptions()


def parse_request(payload: Any) -> CompletionRequest:
    """Validate a decoded request payload.

    ``protocol`` is REQUIRED. An unversioned payload is rejected rather than
    assumed to be version 1: assuming would make the first genuinely
    incompatible version unable to tell a v1 caller apart from one who simply
    forgot, which is the failure a version field exists to prevent.
    """
    mapping = _require_mapping(payload, "request")

    if "protocol" not in mapping:
        raise ProtocolError(
            f"request is missing 'protocol'; this build speaks {PROTOCOL_VERSION}"
        )
    version = mapping["protocol"]
    if version != PROTOCOL_VERSION:
        raise ProtocolError(
            f"unsupported protocol {version!r}; this build speaks "
            f"{PROTOCOL_VERSION}"
        )

    unknown = sorted(set(mapping) - set(_REQUEST_KEYS))
    if unknown:
        raise ProtocolError(
            "unknown request key(s): "
            + ", ".join(unknown)
            + "; known keys are "
            + ", ".join(sorted(_REQUEST_KEYS))
        )

    for name in ("endpoint", "model", "system", "prompt"):
        value = mapping.get(name)
        if value is not None and not isinstance(value, str):
            raise ProtocolError(f"{name} must be a string")
    if "cheap" in mapping and not isinstance(mapping["cheap"], bool):
        raise ProtocolError("cheap must be a boolean")

    options = BackendOptions(**parse_options(mapping.get("options") or {}))
    return CompletionRequest(
        endpoint=mapping.get("endpoint"),
        model=mapping.get("model"),
        cheap=bool(mapping.get("cheap", False)),
        system=mapping.get("system") or "",
        prompt=mapping.get("prompt") or "",
        options=options,
    )


def protocol_error_envelope(message: str) -> Dict[str, Any]:
    """The failure shape for a request that never reached an endpoint.

    Shares the envelope's ``protocol`` field so one parser reads both, and
    carries NO ``response`` -- there is no call to describe, and an empty
    response object would read as a call that produced nothing.
    """
    return {
        "protocol": PROTOCOL_VERSION,
        "error": {"kind": "protocol", "message": message},
    }


def describe_request_schema() -> Dict[str, Any]:
    """The accepted request shape, derived from BackendOptions itself.

    Emitted by the CLI so a consumer can discover the surface instead of reading
    this source. Derived, not restated, for the same reason ``_option_fields``
    is: a hand-written schema is a second source of truth that can disagree with
    the dataclass the parser actually validates against.
    """
    settable = _option_fields()
    return {
        "protocol": PROTOCOL_VERSION,
        "keys": {
            "protocol": "integer, required, must equal " + str(PROTOCOL_VERSION),
            "endpoint": "string, optional",
            "model": "string, optional",
            "cheap": "boolean, optional",
            "system": "string, optional",
            "prompt": "string, optional",
            "options": "object, optional",
        },
        "options": {
            name: str(field.type) for name, field in sorted(settable.items())
        },
        "rejected_options": dict(_UNSETTABLE_OPTIONS),
    }


# Fail loudly at import time if a BackendOptions field ever gains a type this
# coercer has no branch for, rather than letting it reach a caller unvalidated
# the first time someone sets it in a request (see _ensure_coercible).
_ensure_coercible()


__all__ = [
    "PROTOCOL_VERSION",
    "ProtocolError",
    "CompletionRequest",
    "parse_request",
    "parse_options",
    "protocol_error_envelope",
    "describe_request_schema",
]
