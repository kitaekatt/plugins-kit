"""The stdlib JSON Schema subset: what it accepts, refuses, and reports.

The subset is closed in both directions. A keyword outside it must be REFUSED
by ``check_schema`` -- never accepted and then ignored by ``validate``, which
would let an answer pass a constraint nobody checked.
"""
from __future__ import annotations

import pytest

from llm_scripting_kit.completion.json_schema import (
    ANNOTATION_KEYWORDS,
    SUPPORTED_KEYWORDS,
    check_schema,
    resolve_local_ref,
    validate,
)


# -- check_schema ----------------------------------------------------------


def test_every_supported_and_annotation_keyword_is_accepted():
    check_schema(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "urn:x",
            "title": "t",
            "description": "d",
            "default": {},
            "examples": [{}],
            "type": "object",
            "properties": {
                "s": {"type": "string", "minLength": 1, "maxLength": 5},
                "n": {
                    "type": ["number", "null"],
                    "minimum": 0,
                    "maximum": 10,
                    "exclusiveMinimum": -1,
                    "exclusiveMaximum": 11,
                },
                "a": {"type": "array", "items": {"$ref": "#/$defs/item"},
                      "minItems": 0, "maxItems": 3},
                "e": {"enum": ["x", 1, None]},
                "c": {"const": "fixed"},
                "u": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
            },
            "required": ["s"],
            "additionalProperties": False,
            "$defs": {"item": {"type": "object", "additionalProperties": {"type": "string"}}},
        }
    )


@pytest.mark.parametrize("keyword", ["pattern", "oneOf", "allOf", "format", "uniqueItems", "not"])
def test_unsupported_keywords_are_refused_with_their_pointer(keyword):
    schema = {"type": "object", "properties": {"a": {"type": "string", keyword: "x"}}}
    with pytest.raises(ValueError) as info:
        check_schema(schema)
    message = str(info.value)
    assert repr(keyword) in message
    assert f"/properties/a/{keyword}" in message


def test_the_first_unsupported_keyword_is_named_deterministically():
    with pytest.raises(ValueError, match="'allOf'"):
        check_schema({"type": "object", "pattern": "x", "allOf": []})


def test_the_keyword_sets_are_disjoint_and_exclude_the_refused_ones():
    assert not SUPPORTED_KEYWORDS & ANNOTATION_KEYWORDS
    for refused in ("pattern", "oneOf", "allOf", "format", "uniqueItems"):
        assert refused not in SUPPORTED_KEYWORDS | ANNOTATION_KEYWORDS


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "text"},
        {"type": []},
        {"required": "a"},
        {"minLength": -1},
        {"minLength": True},
        {"minimum": "0"},
        {"anyOf": []},
        {"items": [{"type": "string"}]},
        {"properties": {"a": True}},
        {"enum": "a"},
    ],
    ids=["unknown-type", "empty-type-list", "required-not-list", "negative-length",
         "bool-length", "string-minimum", "empty-anyof", "tuple-items",
         "boolean-subschema", "enum-not-list"],
)
def test_malformed_keyword_values_are_refused(schema):
    with pytest.raises(ValueError):
        check_schema(schema)


@pytest.mark.parametrize(
    "ref",
    ["http://example.com/s.json", "#/definitions/a", "#/$defs/missing", "#/$defs/a/b", "#"],
)
def test_non_local_or_unresolvable_refs_are_refused(ref):
    with pytest.raises(ValueError, match=r"\$ref"):
        check_schema({"$ref": ref, "$defs": {"a": {"type": "string"}}})


def test_a_non_consuming_ref_cycle_is_refused():
    schema = {
        "type": "object",
        "properties": {"x": {"$ref": "#/$defs/a"}},
        "$defs": {
            "a": {"anyOf": [{"$ref": "#/$defs/b"}, {"type": "string"}]},
            "b": {"$ref": "#/$defs/a"},
        },
    }
    with pytest.raises(ValueError, match=r"\$ref cycle at"):
        check_schema(schema)


def test_a_ref_cycle_through_the_instance_is_a_legal_recursive_schema():
    tree = {
        "$ref": "#/$defs/node",
        "$defs": {
            "node": {
                "type": "object",
                "properties": {"children": {"type": "array", "items": {"$ref": "#/$defs/node"}}},
            }
        },
    }
    check_schema(tree)
    assert validate(tree, {"children": [{"children": []}, {}]}) == ()
    assert validate(tree, {"children": [5]}) == (("/children/0", "type"),)


def test_resolve_local_ref_unescapes_the_name():
    root = {"$defs": {"a/b": {"type": "string"}}}
    assert resolve_local_ref(root, "#/$defs/a~1b") == {"type": "string"}


# -- validate --------------------------------------------------------------


_OBJ = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 2, "maxLength": 4},
        "score": {"type": "number", "minimum": 0, "maximum": 1},
        "count": {"type": "integer", "exclusiveMinimum": 0, "exclusiveMaximum": 10},
        "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 2},
        "kind": {"enum": ["a", "b"]},
        "fixed": {"const": True},
    },
    "required": ["name", "score"],
    "additionalProperties": False,
}


def test_a_conforming_value_has_no_errors():
    value = {"name": "abc", "score": 0.5, "count": 3, "tags": ["x"], "kind": "a", "fixed": True}
    assert validate(_OBJ, value) == ()


@pytest.mark.parametrize(
    "patch,expected",
    [
        ({"name": "a"}, ("/name", "minLength")),
        ({"name": "abcde"}, ("/name", "maxLength")),
        ({"name": 5}, ("/name", "type")),
        ({"score": -0.1}, ("/score", "minimum")),
        ({"score": 1.5}, ("/score", "maximum")),
        ({"count": 0}, ("/count", "exclusiveMinimum")),
        ({"count": 10}, ("/count", "exclusiveMaximum")),
        ({"count": 2.5}, ("/count", "type")),
        ({"tags": []}, ("/tags", "minItems")),
        ({"tags": ["a", "b", "c"]}, ("/tags", "maxItems")),
        ({"tags": [1]}, ("/tags/0", "type")),
        ({"kind": "c"}, ("/kind", "enum")),
        ({"fixed": 1}, ("/fixed", "const")),
        ({"extra": 1}, ("/extra", "additionalProperties")),
    ],
)
def test_each_keyword_reports_its_pointer_and_name(patch, expected):
    value = {"name": "abc", "score": 0.5}
    value.update(patch)
    assert validate(_OBJ, value) == (expected,)


def test_missing_required_properties_are_reported_per_property_sorted():
    assert validate(_OBJ, {}) == (("/name", "required"), ("/score", "required"))


def test_errors_are_sorted_and_deterministic():
    value = {"name": 1, "score": "x", "zz": 0, "aa": 0}
    first = validate(_OBJ, value)
    assert first == tuple(sorted(first))
    assert first == validate(_OBJ, dict(reversed(list(value.items()))))


def test_integer_accepts_an_integral_float_and_refuses_a_bool():
    schema = {"type": "integer"}
    assert validate(schema, 3.0) == ()
    assert validate(schema, True) == (("", "type"),)


def test_number_and_enum_do_not_confuse_bool_with_int():
    assert validate({"type": "number"}, False) == (("", "type"),)
    assert validate({"enum": [1]}, True) == (("", "enum"),)
    assert validate({"enum": [1]}, 1.0) == ()
    assert validate({"const": {"a": [1, 2]}}, {"a": [1, 2]}) == ()


def test_any_of_reports_once_at_its_own_location():
    schema = {"anyOf": [{"type": "string", "minLength": 3}, {"type": "integer"}]}
    assert validate(schema, "abcd") == ()
    assert validate(schema, 7) == ()
    assert validate(schema, "a") == (("", "anyOf"),)


def test_additional_properties_schema_validates_extra_keys():
    schema = {"type": "object", "additionalProperties": {"type": "string"}}
    assert validate(schema, {"a": "x"}) == ()
    assert validate(schema, {"a": 1}) == (("/a", "type"),)


def test_pointer_tokens_are_escaped():
    schema = {"type": "object", "additionalProperties": False}
    assert validate(schema, {"a/b~c": 1}) == (("/a~1b~0c", "additionalProperties"),)


def test_ref_and_siblings_both_apply():
    schema = {"$ref": "#/$defs/s", "maxLength": 2, "$defs": {"s": {"type": "string"}}}
    assert validate(schema, "abc") == (("", "maxLength"),)
    assert validate(schema, 5) == (("", "type"),)
