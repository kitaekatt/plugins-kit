"""The requirement language over a capability advertisement.

A caller that needs a specific param, execution control, or structured-output
mode expresses that need as a small requirement mapping and asks whether one
advertised :class:`~.capabilities.Capabilities` record satisfies it. This
module owns that matching language so the schema owner (this package) also
owns how requirements against it are read -- a consumer selecting among
endpoints has no capability vocabulary of its own to maintain.

The named convenience keys describe the public advertisement shape: ``params``
(also spelled ``required_params`` or ``honors``), ``execution_controls``
(``controls``), ``dropped_params``, ``structured_output`` (``structured``) and
``system_prompt`` (``system_prompt_mode``). Any other key is read as a dotted
path over :meth:`~.capabilities.Capabilities.to_json`, so this function carries
no endpoint or capability table of its own -- it only knows how to walk the
advertisement's JSON shape.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Union

from .capabilities import Capabilities

_MISSING = object()


def _lookup(value: object, path: str) -> object:
    """Read a dotted path from the JSON-shaped advertisement."""
    current = value
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _matches(actual: object, expected: object) -> bool:
    """Match a requirement against one advertisement value."""
    if expected is True:
        return bool(actual)
    if expected is False:
        return not bool(actual)
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return False
        return all(
            key in actual and _matches(actual[key], requirement)
            for key, requirement in expected.items()
        )
    if isinstance(expected, Sequence) and not isinstance(
        expected, (str, bytes, bytearray)
    ):
        expected_values = tuple(expected)
        if isinstance(actual, Sequence) and not isinstance(
            actual, (str, bytes, bytearray)
        ):
            return all(item in actual for item in expected_values)
        return actual in expected_values
    return actual == expected


def _required_names(value: object) -> tuple[str, ...]:
    """Normalize a list or mapping of named capability requirements."""
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        return tuple(str(name) for name, wanted in value.items() if wanted is not False)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return tuple(str(item) for item in value)
    return ()


def match_capabilities(
    capabilities: Union[Capabilities, Mapping[str, object]], requirements: object
) -> bool:
    """Return whether an advertisement satisfies a requirement mapping.

    ``capabilities`` may be a :class:`~.capabilities.Capabilities` instance
    (serialized via ``to_json()``) or an already-serialized mapping in the
    same shape. ``requirements`` may be ``None`` or ``{}`` (match-all), a list
    (shorthand for ``{"params": [...]}``), or a mapping using the named
    convenience keys described in the module docstring, falling back to a
    dotted path over the advertisement for any other key.
    """
    if requirements is None or requirements == {}:
        return True
    if isinstance(requirements, Sequence) and not isinstance(
        requirements, (str, bytes, bytearray)
    ):
        requirements = {"params": list(requirements)}
    if not isinstance(requirements, Mapping):
        raise ValueError("requirements must be a mapping or list")

    advertised = (
        capabilities.to_json()
        if isinstance(capabilities, Capabilities)
        else capabilities
    )
    for raw_key, expected in requirements.items():
        key = str(raw_key)
        if key in {"params", "required_params", "honors"}:
            if isinstance(expected, Mapping):
                params = advertised.get("params", {})
                if not isinstance(params, Mapping):
                    return False
                for name, requirement in expected.items():
                    actual = params.get(str(name), _MISSING)
                    if requirement is False:
                        if actual is not _MISSING:
                            return False
                    elif actual is _MISSING or not _matches(actual, requirement):
                        return False
            else:
                params = advertised.get("params", {})
                if not isinstance(params, Mapping):
                    return False
                if any(name not in params for name in _required_names(expected)):
                    return False
            continue

        if key in {"execution_controls", "controls"}:
            controls = advertised.get("execution_controls", [])
            if not isinstance(controls, Sequence):
                return False
            control_ids = {
                item.get("id")
                for item in controls
                if isinstance(item, Mapping) and "id" in item
            }
            if any(name not in control_ids for name in _required_names(expected)):
                return False
            continue

        if key == "dropped_params":
            dropped = advertised.get("dropped_params", [])
            if any(name not in dropped for name in _required_names(expected)):
                return False
            continue

        if key in {"structured_output", "structured"}:
            structured = advertised.get("structured_output", _MISSING)
            if not isinstance(structured, Mapping):
                return False
            if isinstance(expected, str):
                if expected in {"native", "passthrough", "none"}:
                    if structured.get("mode") != expected:
                        return False
                elif structured.get("result") != expected:
                    return False
            elif not _matches(structured, expected):
                return False
            continue

        if key in {"system_prompt", "system_prompt_mode"}:
            system = advertised.get("system_prompt", _MISSING)
            if isinstance(expected, str):
                if not isinstance(system, Mapping) or system.get("mode") != expected:
                    return False
            elif not _matches(system, expected):
                return False
            continue

        actual = _lookup(advertised, key)
        if actual is _MISSING or not _matches(actual, expected):
            return False
    return True


__all__ = ["match_capabilities"]
