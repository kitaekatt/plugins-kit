"""The requirement language over a capability advertisement.

A caller that needs a specific param, execution control, or structured-output
mode expresses that need as a small requirement mapping and asks whether one
advertised :class:`~.capabilities.Capabilities` record satisfies it. This
module owns that matching language so the schema owner (this package) also
owns how requirements against it are read -- a consumer selecting among
endpoints has no capability vocabulary of its own to maintain.

The named convenience keys describe the public advertisement shape: ``params``
(also spelled ``required_params`` or ``honors``), ``execution_controls``
(``controls``), ``guarantees`` (``denies``), ``dropped_params``,
``structured_output`` (``structured``) and ``system_prompt``
(``system_prompt_mode``). Any other key is read as a dotted
path over :meth:`~.capabilities.Capabilities.to_json`, so this function carries
no endpoint or capability table of its own -- it only knows how to walk the
advertisement's JSON shape.

A ``guarantees`` requirement must also be APPLIED, not only matched: an
adapter may satisfy it through a request-sourced control that does nothing
until something arms it. :func:`arm_call` arms every such control for one call
(the backend's text-only mode, or names added to ``disallowed_tools``) and
raises when a required subject has no control it can arm, so the entry a
requirement admits is the entry that runs under it. :func:`arm_requirements`
is its text-only half.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any, Optional, Union

from .capabilities import (
    DENY_TOOL_NAMES,
    TEXT_ONLY_MODE,
    TEXT_ONLY_PARAMETER,
    Capabilities,
)

_DENYING = {"deny", "confine"}

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

        if key in {"guarantees", "denies"}:
            # Require an OUTCOME rather than a mechanism: the caller names a
            # canonical subject (capabilities.FILESYSTEM_WRITE) and any adapter
            # that can deny it matches, whether structurally or through a
            # control it emits. This is what keeps a safety requirement
            # harness-independent -- naming a control id instead would silently
            # restrict the caller to the one adapter that spells it that way.
            structural = advertised.get("guarantees", [])
            if not isinstance(structural, Sequence) or isinstance(
                structural, (str, bytes, bytearray)
            ):
                structural = []
            controls = advertised.get("execution_controls", [])
            if not isinstance(controls, Sequence):
                return False
            denied = set(structural)
            for item in controls:
                if not isinstance(item, Mapping):
                    continue
                if item.get("effect") not in {"deny", "confine"}:
                    continue
                subjects = item.get("subjects", ())
                if isinstance(subjects, Sequence) and not isinstance(
                    subjects, (str, bytes, bytearray)
                ):
                    denied.update(str(subject) for subject in subjects)
            if any(name not in denied for name in _required_names(expected)):
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


def required_guarantees(requirements: object) -> frozenset:
    """The canonical subjects a requirement mapping names under ``guarantees``.

    Reads the ``guarantees`` key and its ``denies`` alias in every shape
    :func:`match_capabilities` accepts (a name, a list, or a mapping whose
    ``False`` values are not required). Anything that is not a mapping names
    no guarantee.
    """
    if not isinstance(requirements, Mapping):
        return frozenset()
    names: set = set()
    for key in ("guarantees", "denies"):
        if key in requirements:
            names.update(_required_names(requirements[key]))
    return frozenset(names)


def _advertised(record: object) -> Optional[Mapping]:
    advertised = record.to_json() if isinstance(record, Capabilities) else record
    return advertised if isinstance(advertised, Mapping) else None


def _sequence(value: object) -> tuple:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(value)
    return ()


def _text_only_control(advertised: Mapping, subjects: frozenset) -> Optional[Mapping]:
    """The record's text-only control when it denies every one of ``subjects``."""
    for control in _sequence(advertised.get("execution_controls")):
        if (
            isinstance(control, Mapping)
            and control.get("id") == TEXT_ONLY_MODE
            and control.get("parameter") == TEXT_ONLY_PARAMETER
            and control.get("effect") in _DENYING
            and subjects <= set(_sequence(control.get("subjects")))
        ):
            return control
    return None


def _unarmed_subjects(advertised: Mapping, subjects: frozenset) -> frozenset:
    """``subjects`` minus those the adapter denies without being armed:
    structurally (``guarantees``) or through a FIXED control."""
    remaining = set(subjects) - set(_sequence(advertised.get("guarantees")))
    for control in _sequence(advertised.get("execution_controls")):
        if (
            isinstance(control, Mapping)
            and control.get("source") == "fixed"
            and control.get("effect") in _DENYING
        ):
            remaining -= set(_sequence(control.get("subjects")))
    return frozenset(remaining)


def _with_text_only(backend: Any) -> Any:
    """``backend.with_text_only()``: the backend in text-only mode.

    The backend builds its own text-only copy rather than this module
    rewriting a dataclass field, so a wrapper (the CLI's recording proxy, for
    one) can arm its inner backend and stay in place. A dataclass with a
    ``text_only`` field and no method is copied with the field set, the 0.61.0
    contract. Anything else raises: the record says it can be armed and it
    cannot.
    """
    with_text_only = getattr(backend, "with_text_only", None)
    if callable(with_text_only):
        return with_text_only()
    if dataclasses.is_dataclass(backend) and "text_only" in {
        f.name for f in dataclasses.fields(backend)
    }:
        return backend if backend.text_only else dataclasses.replace(backend, text_only=True)
    raise TypeError(
        f"the record admitting {getattr(backend, 'name', type(backend).__name__)!r} "
        f"arms {TEXT_ONLY_MODE} through {TEXT_ONLY_PARAMETER}, but the backend "
        f"({type(backend).__name__}) has neither with_text_only() nor a text_only "
        "field; nothing was dispatched"
    )


def arm_requirements(
    backend: Any,
    requirements: object,
    capabilities: Optional[Union[Capabilities, Mapping[str, object]]] = None,
) -> Any:
    """Return ``backend`` in text-only mode when that is how it meets the requirement.

    When ``requirements`` names guarantees the record does not deliver
    unarmed, and the record's ``text-only-mode`` control (parameter
    ``backend.text_only``) denies every one of them, the result is a copy of
    ``backend`` with ``text_only=True``. Otherwise ``backend`` is returned
    unchanged. This is the text-only half of :func:`arm_call`, which a caller
    that dispatches should use instead: it also arms deny-list controls and
    raises when a guarantee cannot be armed.

    ``capabilities`` is the record that admitted the entry; it defaults to
    ``backend.capabilities``. A record that names the control for a backend
    with neither ``with_text_only()`` nor a ``text_only`` field raises
    :class:`TypeError`.
    """
    subjects = required_guarantees(requirements)
    if not subjects:
        return backend
    record = capabilities if capabilities is not None else getattr(backend, "capabilities", None)
    advertised = _advertised(record) if record is not None else None
    if advertised is None:
        return backend
    remaining = _unarmed_subjects(advertised, subjects)
    if remaining and _text_only_control(advertised, remaining) is not None:
        return _with_text_only(backend)
    return backend


@dataclasses.dataclass(frozen=True)
class ArmedCall:
    """One call armed for a requirement.

    ``controls`` are the ids of the execution controls arming switched on;
    the response's ``execution_controls_applied`` must contain each of them,
    which ``declaration.run`` checks after the call.
    """

    backend: Any
    options: Any
    controls: tuple = ()


def arm_call(
    backend: Any,
    options: Any,
    requirements: object,
    capabilities: Optional[Union[Capabilities, Mapping[str, object]]] = None,
) -> ArmedCall:
    """Arm every request-sourced control a ``guarantees`` requirement relies on.

    For each required subject the adapter does not deny unarmed (structurally
    or through a FIXED control):

    - when the record's ``text-only-mode`` control denies all of them, the
      backend is switched to text-only mode (``backend.text_only``);
    - otherwise each subject is armed through a ``disallowed_tools`` control
      that denies it, by adding that subject's :data:`DENY_TOOL_NAMES` to
      ``options.disallowed_tools``.

    A subject with no control of either kind raises :class:`ValueError`
    before anything runs: the entry would run without the guarantee that
    admitted it. ``capabilities`` defaults to ``backend.capabilities``; with
    no record at all a guarantee requirement raises too. Without a guarantee
    requirement the call is returned unchanged.
    """
    subjects = required_guarantees(requirements)
    if not subjects:
        return ArmedCall(backend, options)
    record = capabilities if capabilities is not None else getattr(backend, "capabilities", None)
    advertised = _advertised(record) if record is not None else None
    name = getattr(backend, "name", type(backend).__name__)
    if advertised is None:
        raise ValueError(
            f"no capability record for {name!r}, so the required guarantees "
            f"{sorted(subjects)} cannot be armed; nothing was dispatched"
        )
    remaining = _unarmed_subjects(advertised, subjects)
    if not remaining:
        return ArmedCall(backend, options)
    if _text_only_control(advertised, remaining) is not None:
        return ArmedCall(_with_text_only(backend), options, (TEXT_ONLY_MODE,))
    names = []
    controls = []
    for subject in sorted(remaining):
        control = next(
            (
                c for c in _sequence(advertised.get("execution_controls"))
                if isinstance(c, Mapping)
                and c.get("source") == "request"
                and c.get("effect") in _DENYING
                and c.get("parameter") == "disallowed_tools"
                and subject in _sequence(c.get("subjects"))
            ),
            None,
        )
        if control is None or subject not in DENY_TOOL_NAMES:
            raise ValueError(
                f"{name!r} was admitted for {subject!r} but advertises no control "
                "this call can arm (text-only mode or a disallowed_tools deny); "
                "nothing was dispatched"
            )
        names.extend(DENY_TOOL_NAMES[subject])
        if control.get("id") not in controls:
            controls.append(str(control.get("id")))
    existing = str(getattr(options, "disallowed_tools", None) or "").replace(",", " ").split()
    merged = list(dict.fromkeys([*existing, *names]))
    return ArmedCall(
        backend,
        dataclasses.replace(options, disallowed_tools=" ".join(merged)),
        tuple(controls),
    )


__all__ = [
    "ArmedCall",
    "arm_call",
    "arm_requirements",
    "match_capabilities",
    "required_guarantees",
]
