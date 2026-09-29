"""A stdlib-only validator for a declared subset of JSON Schema.

An output contract (:mod:`.contract_types`) validates a model's answer against
a caller schema. No JSON Schema library is a dependency of this package, and
adding one would oblige every consumer that imports
``llm_scripting_kit.completion`` to declare it too, so this module validates a
SUBSET instead -- and says exactly which one.

The subset is closed in both directions:

- :data:`SUPPORTED_KEYWORDS` constrain an instance and are enforced by
  :func:`validate`.
- :data:`ANNOTATION_KEYWORDS` are accepted and constrain nothing.
- EVERY other keyword is refused by :func:`check_schema` -- ``pattern``,
  ``oneOf``, ``allOf``, ``format``, ``uniqueItems`` included. A keyword that
  was accepted and then ignored during validation would let an answer pass a
  constraint nobody checked, which is the overclaim a contract exists to
  prevent. Each is added deliberately or not at all.

``$ref`` is local only: ``#/$defs/<name>``, resolved against the ROOT schema's
``$defs``. A cycle of ``$ref`` / ``anyOf`` edges that never descends into the
instance (``a`` -> ``b`` -> ``a``) is refused at check time, because no
finite validation of it exists.

Errors are a sorted, de-duplicated tuple of ``(json_pointer, keyword)``: the
pointer names the INSTANCE location, the keyword the constraint it failed. No
message text is produced, so the result is deterministic for a given schema
and value.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, List, Tuple

#: Keywords that constrain an instance. Each is enforced by :func:`validate`.
SUPPORTED_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minItems",
        "maxItems",
        "anyOf",
        "$ref",
    }
)

#: Keywords accepted as annotations: they describe, and constrain nothing.
ANNOTATION_KEYWORDS = frozenset(
    {"title", "description", "$schema", "$id", "default", "examples", "$defs"}
)

#: The instance types ``type`` may name.
TYPE_NAMES = frozenset(
    {"null", "boolean", "integer", "number", "string", "array", "object"}
)

_REF_PREFIX = "#/$defs/"

Error = Tuple[str, str]


def _escape(token: str) -> str:
    """One JSON Pointer reference token (RFC 6901)."""
    return token.replace("~", "~0").replace("/", "~1")


def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _child(pointer: str, token: Any) -> str:
    return f"{pointer}/{_escape(str(token))}"


def _is_array(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def resolve_local_ref(root: Mapping, ref: Any) -> Mapping:
    """The schema a local ``#/$defs/<name>`` reference names.

    Raises :class:`ValueError` for a non-local, malformed or unresolvable
    reference.
    """
    if not isinstance(ref, str) or not ref.startswith(_REF_PREFIX):
        raise ValueError(
            f"$ref {ref!r} is not supported; only local #/$defs/<name> "
            "references are"
        )
    token = ref[len(_REF_PREFIX):]
    if not token or "/" in token:
        raise ValueError(f"$ref {ref!r} must name exactly one #/$defs entry")
    defs = root.get("$defs")
    name = _unescape(token)
    if not isinstance(defs, Mapping) or name not in defs:
        raise ValueError(f"$ref {ref!r} does not resolve to a #/$defs entry")
    target = defs[name]
    if not isinstance(target, Mapping):
        raise ValueError(f"$ref {ref!r} resolves to a non-object schema")
    return target


# -- check ----------------------------------------------------------------


def _require(condition: bool, pointer: str, keyword: str, expectation: str) -> None:
    if not condition:
        raise ValueError(f"schema keyword {keyword!r} at {pointer or '/'} {expectation}")


def _check_non_negative_int(value: Any, pointer: str, keyword: str) -> None:
    _require(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0,
        pointer,
        keyword,
        "must be a non-negative integer",
    )


def _check_node(node: Any, pointer: str, root: Mapping) -> None:
    if not isinstance(node, Mapping):
        raise ValueError(f"schema at {pointer or '/'} must be a JSON object")
    for keyword in sorted(node):
        if keyword not in SUPPORTED_KEYWORDS and keyword not in ANNOTATION_KEYWORDS:
            raise ValueError(
                f"unsupported schema keyword {keyword!r} at "
                f"{_child(pointer, keyword)}; supported: "
                f"{', '.join(sorted(SUPPORTED_KEYWORDS))}; annotations: "
                f"{', '.join(sorted(ANNOTATION_KEYWORDS))}"
            )
    for keyword in sorted(node):
        value = node[keyword]
        here = _child(pointer, keyword)
        if keyword == "type":
            names = (value,) if isinstance(value, str) else value
            _require(
                _is_array(names)
                and len(names) > 0
                and all(isinstance(n, str) and n in TYPE_NAMES for n in names),
                pointer,
                keyword,
                f"must be one of {sorted(TYPE_NAMES)} or a non-empty list of them",
            )
        elif keyword == "properties":
            _require(isinstance(value, Mapping), pointer, keyword, "must be an object")
            for name in sorted(value):
                _check_node(value[name], _child(here, name), root)
        elif keyword == "required":
            _require(
                _is_array(value) and all(isinstance(n, str) for n in value),
                pointer,
                keyword,
                "must be a list of strings",
            )
        elif keyword == "additionalProperties":
            if not isinstance(value, bool):
                _check_node(value, here, root)
        elif keyword == "items":
            # Only the single-schema form. The list (tuple-validation) form
            # changed meaning between drafts, so it is refused rather than
            # guessed at.
            _check_node(value, here, root)
        elif keyword == "enum":
            _require(_is_array(value), pointer, keyword, "must be a list")
        elif keyword in ("minLength", "maxLength", "minItems", "maxItems"):
            _check_non_negative_int(value, pointer, keyword)
        elif keyword in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
            _require(
                _is_number(value) and math.isfinite(value),
                pointer,
                keyword,
                "must be a finite number",
            )
        elif keyword == "anyOf":
            _require(
                _is_array(value) and len(value) > 0,
                pointer,
                keyword,
                "must be a non-empty list of schemas",
            )
            for index, branch in enumerate(value):
                _check_node(branch, _child(here, index), root)
        elif keyword == "$ref":
            resolve_local_ref(root, value)
        elif keyword == "$defs":
            _require(isinstance(value, Mapping), pointer, keyword, "must be an object")
            for name in sorted(value):
                _check_node(value[name], _child(here, name), root)


def _non_consuming_refs(node: Mapping) -> List[str]:
    """``$ref`` targets reachable from ``node`` without descending the instance.

    Only ``$ref`` and ``anyOf`` apply a subschema to the SAME instance
    location; every other keyword either descends into a child or constrains
    a scalar.
    """
    refs: List[str] = []
    ref = node.get("$ref")
    if isinstance(ref, str):
        refs.append(ref)
    branches = node.get("anyOf")
    if _is_array(branches):
        for branch in branches:
            if isinstance(branch, Mapping):
                refs.extend(_non_consuming_refs(branch))
    return refs


def _check_no_ref_cycle(root: Mapping) -> None:
    defs = root.get("$defs")
    if not isinstance(defs, Mapping):
        return
    graph = {
        _REF_PREFIX + _escape(name): _non_consuming_refs(target)
        for name, target in defs.items()
        if isinstance(target, Mapping)
    }
    done: set = set()

    def visit(ref: str, path: Tuple[str, ...]) -> None:
        if ref in path:
            raise ValueError(f"$ref cycle at {ref}")
        if ref in done:
            return
        for nxt in graph.get(ref, ()):
            visit(nxt, path + (ref,))
        done.add(ref)

    for start in sorted(graph):
        visit(start, ())


def check_schema(schema: Any) -> None:
    """Refuse a schema outside the supported subset.

    Raises :class:`ValueError` naming the first offending keyword (keys are
    visited in sorted order, so "first" is deterministic) and its JSON
    pointer within the schema. Also refuses a malformed keyword value, an
    unresolvable ``$ref``, and a ``$ref`` cycle that never descends into the
    instance.
    """
    if not isinstance(schema, Mapping):
        raise ValueError("schema root must be a JSON object")
    _check_node(schema, "", schema)
    _check_no_ref_cycle(schema)


# -- validate -------------------------------------------------------------


def _json_equal(a: Any, b: Any) -> bool:
    """JSON value equality: ``true`` is not ``1``, and ``1`` equals ``1.0``."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _is_number(a) or _is_number(b):
        return _is_number(a) and _is_number(b) and a == b
    if isinstance(a, Mapping) or isinstance(b, Mapping):
        return (
            isinstance(a, Mapping)
            and isinstance(b, Mapping)
            and set(a) == set(b)
            and all(_json_equal(a[k], b[k]) for k in a)
        )
    if _is_array(a) or _is_array(b):
        return (
            _is_array(a)
            and _is_array(b)
            and len(a) == len(b)
            and all(_json_equal(x, y) for x, y in zip(a, b))
        )
    return type(a) is type(b) and a == b


def _type_matches(name: str, value: Any) -> bool:
    if name == "null":
        return value is None
    if name == "boolean":
        return isinstance(value, bool)
    if name == "integer":
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        return isinstance(value, float) and math.isfinite(value) and value.is_integer()
    if name == "number":
        return _is_number(value)
    if name == "string":
        return isinstance(value, str)
    if name == "array":
        return _is_array(value)
    if name == "object":
        return isinstance(value, Mapping)
    return False


def _validate(node: Mapping, value: Any, pointer: str, root: Mapping, errors: set) -> None:
    if "$ref" in node:
        _validate(resolve_local_ref(root, node["$ref"]), value, pointer, root, errors)

    if "type" in node:
        names = (node["type"],) if isinstance(node["type"], str) else node["type"]
        if not any(_type_matches(name, value) for name in names):
            errors.add((pointer, "type"))

    if "const" in node and not _json_equal(value, node["const"]):
        errors.add((pointer, "const"))

    if "enum" in node and not any(_json_equal(value, m) for m in node["enum"]):
        errors.add((pointer, "enum"))

    if "anyOf" in node:
        passed = False
        for branch in node["anyOf"]:
            branch_errors: set = set()
            _validate(branch, value, pointer, root, branch_errors)
            if not branch_errors:
                passed = True
                break
        if not passed:
            errors.add((pointer, "anyOf"))

    if isinstance(value, str):
        if "minLength" in node and len(value) < node["minLength"]:
            errors.add((pointer, "minLength"))
        if "maxLength" in node and len(value) > node["maxLength"]:
            errors.add((pointer, "maxLength"))

    if _is_number(value):
        if "minimum" in node and value < node["minimum"]:
            errors.add((pointer, "minimum"))
        if "maximum" in node and value > node["maximum"]:
            errors.add((pointer, "maximum"))
        if "exclusiveMinimum" in node and value <= node["exclusiveMinimum"]:
            errors.add((pointer, "exclusiveMinimum"))
        if "exclusiveMaximum" in node and value >= node["exclusiveMaximum"]:
            errors.add((pointer, "exclusiveMaximum"))

    if _is_array(value):
        if "minItems" in node and len(value) < node["minItems"]:
            errors.add((pointer, "minItems"))
        if "maxItems" in node and len(value) > node["maxItems"]:
            errors.add((pointer, "maxItems"))
        if "items" in node:
            for index, item in enumerate(value):
                _validate(node["items"], item, _child(pointer, index), root, errors)

    if isinstance(value, Mapping):
        properties = node.get("properties") or {}
        for name in node.get("required") or ():
            if name not in value:
                errors.add((_child(pointer, name), "required"))
        for name, item in value.items():
            here = _child(pointer, name)
            if name in properties:
                _validate(properties[name], item, here, root, errors)
                continue
            extra = node.get("additionalProperties", True)
            if extra is False:
                errors.add((here, "additionalProperties"))
            elif isinstance(extra, Mapping):
                _validate(extra, item, here, root, errors)


def validate(schema: Mapping, value: Any) -> Tuple[Error, ...]:
    """Every ``(json_pointer, keyword)`` the value fails, sorted.

    ``schema`` must already have passed :func:`check_schema`. An empty tuple
    means the value conforms. ``anyOf`` reports one error at its own
    location when no branch passes, never the branches' inner errors, so the
    result does not depend on the branch order.
    """
    errors: set = set()
    _validate(schema, value, "", schema, errors)
    return tuple(sorted(errors))


__all__ = [
    "SUPPORTED_KEYWORDS",
    "ANNOTATION_KEYWORDS",
    "TYPE_NAMES",
    "check_schema",
    "validate",
    "resolve_local_ref",
]
