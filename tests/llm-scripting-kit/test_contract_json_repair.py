"""Deterministic structural repair of a model's almost-JSON answer.

Ported from Databench's tested clean-up (`clean_json_structure`): the same
slip corpus, run against a schema of the same shape, now through the kit's
`repair_json_structure` and `finalize_contract`. The schema is Databench's
GENERATION_SCHEMA with `oneOf` spelled `anyOf` (the `const` discriminators
make them equivalent) and the slug `pattern` dropped (outside the subset).
"""
from __future__ import annotations

import json
import re

import pytest

from llm_scripting_kit.completion import json_repair
from llm_scripting_kit.completion.contract import (
    DELIVERY_PROMPT,
    POLICY_VALIDATED_RESULT,
    DeliveryPlan,
    OutputContract,
    OutputContractViolation,
    evaluate_output,
    finalize_contract,
    repair_and_evaluate_output,
)
from llm_scripting_kit.completion.json_repair import repair_json_structure
from llm_scripting_kit.completion.json_schema import check_schema
from llm_scripting_kit.completion.types import LLMResponse


_INLINE_SCHEMA = {
    "anyOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "required": [kind],
            "properties": {kind: {"type": "string", "minLength": 1}},
        }
        for kind in ("text", "path", "reference", "code")
    ]
}
# A paragraph, list item, route step or table cell is one keyed `runs` object,
# and a table row is one keyed `cells` object: no array holds another array
# directly, so every closing bracket sits under a distinct key or object and
# the repair can tell each nesting level from the next.
_LINE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["runs"],
    "properties": {
        "runs": {"type": "array", "minItems": 1, "items": _INLINE_SCHEMA},
    },
}
_ROW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["cells"],
    "properties": {
        "cells": {"type": "array", "minItems": 1, "items": _LINE_SCHEMA},
    },
}
_SECTION_SCHEMA = {
    "anyOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "heading", "paragraphs"],
            "properties": {
                "kind": {"const": "prose"},
                "heading": {"type": "string", "minLength": 1},
                "paragraphs": {
                    "type": "array", "minItems": 1, "items": _LINE_SCHEMA
                },
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "heading", "items"],
            "properties": {
                "kind": {"const": "list"},
                "heading": {"type": "string", "minLength": 1},
                "items": {"type": "array", "minItems": 1, "items": _LINE_SCHEMA},
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "items"],
            "properties": {
                "kind": {"const": "routes"},
                "items": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["label", "steps"],
                        "properties": {
                            "label": {"type": "string", "minLength": 1},
                            "steps": {
                                "type": "array", "minItems": 1,
                                "items": _LINE_SCHEMA,
                            },
                        },
                    },
                },
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "heading", "columns", "rows"],
            "properties": {
                "kind": {"const": "table"},
                "heading": {"type": "string", "minLength": 1},
                "columns": {
                    "type": "array", "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                },
                "rows": {"type": "array", "minItems": 1, "items": _ROW_SCHEMA},
            },
        },
    ]
}
_PAGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["orientation", "other_cards", "sections"],
    "properties": {
        "orientation": {"type": "array", "items": _INLINE_SCHEMA},
        "other_cards": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["target", "label", "identity"],
                "properties": {
                    name: {"type": "string", "minLength": 1}
                    for name in ("target", "label", "identity")
                },
            },
        },
        "sections": {"type": "array", "minItems": 1, "items": _SECTION_SCHEMA},
    },
}
GENERATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "main", "references"],
    "properties": {
        "schema_version": {"const": 1},
        "main": _PAGE_SCHEMA,
        "references": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["slug", "title", "orientation", "sections"],
                "properties": {
                    "slug": {"type": "string", "minLength": 1},
                    "title": {"type": "string", "minLength": 1},
                    "orientation": {"type": "array", "items": _INLINE_SCHEMA},
                    "sections": {"type": "array", "minItems": 1, "items": _SECTION_SCHEMA},
                },
            },
        },
    },
}



def valid_payload():
    return {
        "schema_version": 1,
        "main": {
            "orientation": [{"text": "Welcome <reader>"}, {"path": "docs/README.md"}],
            "other_cards": [{"target": "docs/README.md", "label": "Read me", "identity": "Start here"}],
            "sections": [
                {"kind": "prose", "heading": "Overview", "paragraphs": [{"runs": [{"text": "A guide."}, {"reference": "guide"}]}]},
                {"kind": "list", "heading": "Files", "items": [{"runs": [{"code": "source module"}]}]},
                {"kind": "routes", "items": [{"label": "Read", "steps": [{"runs": [{"path": "docs/README.md"}]}, {"runs": [{"text": "Done"}]}]}, {"label": "Use", "steps": [{"runs": [{"text": "Run"}]}, {"runs": [{"text": "Stop"}]}]}]},
                {"kind": "table", "heading": "Facts", "columns": ["Name", "Value"], "rows": [{"cells": [{"runs": [{"text": "A"}]}, {"runs": [{"text": "B"}]}]}]},
            ],
        },
        "references": [{
            "slug": "guide",
            "title": "Guide",
            "orientation": [{"text": "Reference"}],
            "sections": [{"kind": "prose", "heading": "Details", "paragraphs": [{"runs": [{"text": "More."}]}]}],
        }],
    }


def encoded(payload):
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


check_schema(GENERATION_SCHEMA)
GOOD = encoded(valid_payload())
STRUCTURAL = set("[]{},")


def repair(text, schema=GENERATION_SCHEMA, **kw):
    return repair_json_structure(schema, text, **kw)


def broken(old, new, text=GOOD):
    """One synthetic slip: `old` occurs exactly once and becomes `new`."""
    assert text.count(old) == 1, old
    result = text.replace(old, new)
    with pytest.raises(ValueError):
        json.loads(result)
    return result


def content(text):
    """Everything a repair may not touch: strings, numbers, literals, colons."""
    return re.findall(r'"(?:[^"\\]|\\.)*"|-?\d+|true|false|null|:', text)

# name -> the answer a model could have written for `valid_payload()`
SLIPS = {
    "runs closer dropped before the line closes": broken(
        '{"reference":"guide"}]}]}', '{"reference":"guide"}}]}'
    ),
    "line closer dropped before the paragraphs close": broken(
        '{"reference":"guide"}]}]}', '{"reference":"guide"}]]}'
    ),
    "paragraphs closer dropped before the section closes": broken(
        '{"reference":"guide"}]}]}', '{"reference":"guide"}]}}'
    ),
    "step closer dropped before the route closes": broken(
        '{"text":"Done"}]}]},{"label":"Use"', '{"text":"Done"}]}},{"label":"Use"'
    ),
    "row closer dropped at the deepest level": broken(
        '{"text":"B"}]}]}]}', '{"text":"B"}]}]}}'
    ),
    "cells closer dropped": broken('{"text":"B"}]}]}]}', '{"text":"B"}]}}]}'),
    "inline object closer swapped with its runs closer": broken(
        '{"text":"Done"}]}]}', '{"text":"Done"]}}]}'
    ),
    "line closer swapped with its paragraphs closer": broken(
        '{"reference":"guide"}]}]}', '{"reference":"guide"}]]}}'
    ),
    "surplus closer before the references key": broken(
        '}]},"references"', '}]}},"references"'
    ),
    "surplus closer inside a section": broken(
        '{"code":"source module"}]}]}', '{"code":"source module"}]}]]}'
    ),
    "main object never closed": broken('}]},"references"', '}],"references"'),
    "closers mismatched at the very end": GOOD[:-4] + "]}}]",
    "one closer too many at the very end": GOOD + "}",
    "comma missing between sections": broken(
        ']}]},{"kind":"list"', ']}]}\n  {"kind":"list"'
    ),
    "comma missing between members": broken(
        '"heading":"Files","items"', '"heading":"Files" "items"'
    ),
    "comma missing between inline objects": broken(
        '{"text":"A guide."},{"reference"', '{"text":"A guide."}{"reference"'
    ),
    "comma missing between steps": broken(
        '{"path":"docs/README.md"}]},{"runs"', '{"path":"docs/README.md"}]}{"runs"'
    ),
    "paragraphs opener dropped": broken(
        '"paragraphs":[{"runs":[{"text":"More."}]}]',
        '"paragraphs":{"runs":[{"text":"More."}]}]',
    ),
    "runs opener dropped": broken(
        '"paragraphs":[{"runs":[{"text":"More."}]}]',
        '"paragraphs":[{"runs":{"text":"More."}]}]',
    ),
    "cells opener dropped": broken(
        '"rows":[{"cells":[{"runs"', '"rows":[{"cells":{"runs"'
    ),
    "comma before a closer": broken(
        '{"reference":"guide"}]}]}', '{"reference":"guide"},]}]}'
    ),
    # On its own this one is still JSON (a repeated key), so it needs company.
    "section boundary dropped, then a closer": broken(
        '{"text":"B"}]}]}]}',
        '{"text":"B"}]}]}}',
        GOOD.replace(
            '{"reference":"guide"}]}]},{"kind":"list"',
            '{"reference":"guide"}]}],"kind":"list"',
        ),
    ),
    "surplus opener": broken(
        '"items":[{"runs":[{"code":"source module"}]}]',
        '"items":[[{"runs":[{"code":"source module"}]}]',
    ),
    "two separate slips": broken(
        '{"text":"B"}]}]}]}',
        '{"text":"B"}]}]}}',
        broken('{"reference":"guide"}]}]}', '{"reference":"guide"}}]}'),
    ),
    "code fence around the answer": "```json\n" + GOOD + "\n```\n",
    "prose before and after the answer": "Here is the page:\n" + GOOD + "\nDone.",
}



# The two key/colon slips: a comma written for the colon after a property
# name, and a property name whose closing quote moved past its colon.
KEY_COLON_SLIPS = {
    "comma for the colon after an inline key": broken(
        '{"text":"Done"}', '{"text","Done"}'
    ),
    "comma for the colon after a section key": broken(
        '"heading":"Files"', '"heading","Files"'
    ),
    "inline key quote moved past its colon": broken(
        '{"text":"A guide."}', '{"text:"A guide."}'
    ),
    "a value that starts with a comma after a misquoted key": broken(
        '{"text":", Run"}', '{"text:", Run"}',
        GOOD.replace('{"text":"Run"}', '{"text":", Run"}'),
    ),
    "both key/colon slips and a dropped closer": broken(
        '{"reference":"guide"}]}]}',
        '{"reference":"guide"}}]}',
        broken(
            '{"text":"Done"}',
            '{"text","Done"}',
            broken('{"text":"A guide."}', '{"text:"A guide."}'),
        ),
    ),
}




@pytest.mark.parametrize("name", sorted(SLIPS))
def test_each_class_of_structural_slip_is_recovered(name):
    raw = SLIPS[name]
    result = repair(raw)
    assert result.status == "repaired", result.reason
    assert json.loads(result.text) == valid_payload()
    assert result.edits and len(result.edits) <= json_repair.MAX_REPAIR_EDITS


@pytest.mark.parametrize("name", sorted(SLIPS))
def test_repair_never_alters_content(name):
    raw = SLIPS[name]
    result = repair(raw)
    stripped = [edit for edit in result.edits if edit.op == "strip"]
    assert content(result.text) == content(GOOD if stripped else raw)
    for edit in result.edits:
        if edit.op == "strip":
            assert edit.token in ("leading", "trailing", "fence")
        else:
            assert edit.op in ("insert", "delete") and edit.token in STRUCTURAL
            assert 0 <= edit.offset <= len(raw)
            if edit.op == "delete":
                assert raw[edit.offset] == edit.token


def test_a_valid_answer_is_returned_untouched():
    pretty = json.dumps(valid_payload(), indent=2)
    for text in (GOOD, pretty, '{"schema_version":1}', "[1, 2]", '{"main":{"x":[]}}'):
        result = repair(text)
        assert result.status == "valid"
        assert result.text is text and result.edits == ()


def test_brackets_and_quotes_inside_strings_are_not_structure():
    payload = valid_payload()
    payload["main"]["sections"][1]["items"] = [{"runs": [{"code": ']}" [{ \\ ,'}]}]
    good = encoded(payload)
    raw = broken('{"reference":"guide"}]}]}', '{"reference":"guide"}}]}', good)
    result = repair(raw)
    assert result.status == "repaired"
    assert json.loads(result.text) == payload
    assert content(result.text) == content(raw)
    assert len(result.edits) == 1


def test_a_missing_nesting_level_has_one_reading():
    for old, new in (
        ('"paragraphs":[{"runs":[{"text":"More."', '"paragraphs":{"runs":[{"text":"More."'),
        ('"cells":[{"runs"', '"cells":{"runs"'),
        ('"runs":[{"text":"More."}]', '"runs":{"text":"More."}]'),
    ):
        result = repair(broken(old, new))
        assert result.status == "repaired", (old, result)
        assert json.loads(result.text) == valid_payload()


def test_a_missing_object_opener_is_restored_at_every_level():
    for old in ('{"runs":[{"text":"More."}]}', '{"cells":[', '{"label":"Use"'):
        result = repair(broken(old, old[1:]))
        assert result.status == "repaired", (old, result)
        assert json.loads(result.text) == valid_payload()
        assert [edit.op for edit in result.edits] == ["insert"]


def test_two_structures_that_both_fit_are_declined():
    # A list of lists of strings can read `[["x"]"y"]]` two ways.
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["a"],
        "properties": {
            "a": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                },
            }
        },
    }
    raw = '{"a":[["x"]"y"]]}'
    result = repair(raw, schema)
    assert result.status == "ambiguous", result
    assert result.text == raw and result.edits == () and result.reason


def test_content_errors_are_not_repaired():
    for raw in (
        broken('{"text":"Done"}', '{"text":"Do"ne"}'),  # unescaped quote
        broken('"kind":"routes",', '"kind":"routes","title":"Routes",', GOOD[:-1]),
        GOOD[: len(GOOD) // 2].rsplit('"', 1)[0] + '"',  # cut mid-document
    ):
        result = repair(raw)
        assert result.status == "unrecoverable", raw
        assert result.text == raw and result.edits == () and result.reason


def test_an_answer_that_ends_early_is_never_closed():
    cuts = [GOOD[:-1], GOOD[:-2], GOOD[:-4]]
    cuts.append(GOOD[: GOOD.index(',"sections":[{"kind":"prose","heading":"Details"')])
    cuts.append(GOOD[: GOOD.index(',{"label":"Use"')])
    for raw in cuts:
        result = repair(raw)
        assert result.status == "unrecoverable", raw[-40:]
        assert result.text == raw and result.edits == ()


def test_the_edit_bound_is_respected():
    raw = SLIPS["two separate slips"]
    assert len(repair(raw).edits) == 2
    assert repair(raw, max_edits=1).status == "unrecoverable"
    assert repair(SLIPS["main object never closed"], max_edits=1).status == "repaired"
    assert repair(SLIPS["main object never closed"], max_edits=0).status == "unrecoverable"
    assert repair(GOOD, max_edits=0).status == "valid"


def test_the_search_budget_is_respected():
    result = repair(SLIPS["two separate slips"], max_walks=1)
    assert result.status == "unrecoverable" and "budget" in result.reason


def test_non_text_is_unrecoverable():
    assert repair(None).status == "unrecoverable"


@pytest.mark.parametrize("name", sorted(KEY_COLON_SLIPS))
def test_each_key_colon_slip_is_recovered(name):
    raw = KEY_COLON_SLIPS[name]
    expected = (
        json.loads(GOOD.replace('{"text":"Run"}', '{"text":", Run"}'))
        if "starts with a comma" in name
        else valid_payload()
    )
    result = repair(raw)
    assert result.status == "repaired", result.reason
    assert json.loads(result.text) == expected
    for edit in result.edits:
        if edit.op == "replace":
            assert edit.token == ":" and raw[edit.offset] == ","
        elif edit.token == '"':
            assert edit.op == "insert" and raw[edit.offset] == ":"
            assert raw[edit.offset - 1].isalpha()
        else:
            assert edit.op in ("insert", "delete") and edit.token in STRUCTURAL


def test_key_colon_repairs_touch_only_property_names_of_the_schema():
    raw = broken('{"text":"A guide."}', '{"bogus:"A guide."}')
    assert repair(raw).status == "unrecoverable"
    payload = valid_payload()
    payload["main"]["sections"][3]["columns"] = ["Name", "text:"]
    assert repair(encoded(payload)).status == "valid"


# -- beyond the ported corpus: fences, other schema shapes ---------------------


def test_a_fence_followed_by_prose_with_quotes_is_stripped_as_a_fence():
    raw = 'Sure:\n```json\n' + GOOD + '\n```\nI said "done" and {more}.'
    result = repair(raw)
    assert result.status == "repaired"
    assert [(e.op, e.token) for e in result.edits] == [("strip", "fence")]
    assert json.loads(result.text) == valid_payload()


def test_an_array_root_with_a_ref_and_anyof_is_repaired():
    schema = {
        "type": "array",
        "items": {"$ref": "#/$defs/item"},
        "$defs": {
            "item": {
                "anyOf": [
                    {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}}},
                    {"type": "string"},
                ]
            }
        },
    }
    check_schema(schema)
    assert repair('```json\n[{"n": 1} {"n": 2}, "x"]\n```', schema).status == "repaired"
    result = repair('[{"n": 1}, {"n": 2}, "x"', schema)
    assert result.status == "unrecoverable"  # cut off: never closed
    result = repair('[{"n": 1}, {"n": 2}, "x"}', schema)
    assert result.status == "repaired" and json.loads(result.text) == [{"n": 1}, {"n": 2}, "x"]


def test_a_scalar_root_gets_no_structural_edit():
    assert repair('"abc', {"type": "string"}).status == "unrecoverable"


# -- the seam: evaluate_output stays strict, finalize_contract repairs ----------


def _contract():
    return OutputContract("t.repair", POLICY_VALIDATED_RESULT, GENERATION_SCHEMA)


def _plan(contract=None):
    contract = contract or _contract()
    return DeliveryPlan(contract, "fake", DELIVERY_PROMPT, instruction="x")


def _response(text):
    return LLMResponse(text=text, model="m", input_tokens=1, output_tokens=1)


def test_evaluate_output_stays_strict():
    outcome = evaluate_output(_contract(), SLIPS["code fence around the answer"])
    assert outcome.disposition == "unparseable" and not outcome.repaired


@pytest.mark.parametrize("name", sorted(SLIPS))
def test_finalize_repairs_once_validates_and_keeps_the_raw_text(name):
    raw = SLIPS[name]
    out = finalize_contract(_plan(), _response(raw))
    assert out.text == raw
    assert out.structured == valid_payload()
    report = out.output_contract
    assert report.disposition == "valid" and report.repaired
    assert report.repair_edits and all(len(edit) == 3 for edit in report.repair_edits)
    assert report.to_json()["repair"]["applied"] is True


def test_finalize_of_a_valid_answer_records_no_repair():
    report = finalize_contract(_plan(), _response(GOOD)).output_contract
    assert not report.repaired and report.repair_edits == () and report.repair_note == ""
    assert "repair" not in report.to_json()


def test_finalize_declines_an_ambiguous_answer_and_raises_with_the_note():
    schema = {
        "type": "object",
        "required": ["a"],
        "additionalProperties": False,
        "properties": {
            "a": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            }
        },
    }
    contract = OutputContract("t.amb", POLICY_VALIDATED_RESULT, schema)
    raw = '{"a":[["x"]"y"]]}'
    with pytest.raises(OutputContractViolation) as caught:
        finalize_contract(_plan(contract), _response(raw))
    failed = caught.value.response
    assert failed.text == raw and failed.structured is None
    assert failed.output_contract.disposition == "unparseable"
    assert failed.output_contract.repair_note == "ambiguous"
    assert not failed.output_contract.repaired


def test_repair_never_skips_validation():
    # A repaired document still goes through evaluate_output; the one-slip
    # answer below repairs to a document the strict judgment accepts.
    outcome = repair_and_evaluate_output(_contract(), SLIPS["main object never closed"])
    assert outcome.disposition == "valid" and outcome.repaired
    assert outcome.value == valid_payload()


def test_cli_text_output_prints_the_repaired_value_not_the_raw_answer():
    import json as _json

    from llm_scripting_kit import cli

    raw = SLIPS["code fence around the answer"]
    repaired = finalize_contract(_plan(), _response(raw))
    printed = cli._answer_text(repaired)
    assert _json.loads(printed) == valid_payload()
    assert repaired.text == raw  # provenance stays raw

    clean = finalize_contract(_plan(), _response(GOOD))
    assert cli._answer_text(clean) == GOOD  # no repair: the model text, unchanged
    assert cli._answer_text(_response("plain")) == "plain"  # no contract
