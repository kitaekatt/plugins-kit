"""Helpers for adapting work-unit payloads and values."""

from __future__ import annotations

from collections.abc import Mapping
import json

import yaml
from typing import Any

from yaml_data_editor_kit.schema.corpus import ABSENT


def unit_targets(unit: Any) -> list[Mapping[str, Any]]:
    """Return the target records carried by either unit shape."""
    payload = unit.payload if hasattr(unit, "payload") else unit
    targets = payload.get("targets") if isinstance(payload, Mapping) else None
    if isinstance(targets, list):
        return targets
    if isinstance(payload, Mapping):
        return [payload]
    raise TypeError("unit payload must be a mapping")


def plain_value(value: Any, *, strict: bool = False) -> Any:
    """Convert structural values to JSON-compatible plain values.

    ``strict`` rejects a non-text mapping key instead of passing it through. Only
    the planner wants that: a key it cannot serialize means the agentic grouping
    input is unusable, and raising is how it selects the mechanical fallback. The
    prompt renderer and the plan writer stay permissive, because the schema layer
    accepts non-text keys and a corpus that has one must still render and dispatch.
    """
    if value is ABSENT:
        return {"__absent__": True}
    if isinstance(value, Mapping):
        converted: dict[Any, Any] = {}
        for key, item in value.items():
            if strict and not isinstance(key, str):
                raise TypeError("mapping keys must be text")
            converted[key] = plain_value(item, strict=strict)
        return converted
    if isinstance(value, (list, tuple)):
        return [plain_value(item, strict=strict) for item in value]
    return value

def parse_agentic_result(
    text: str, targets: list[Mapping[str, Any]], unit_id: str
) -> dict[str, Any]:
    payload = json.loads(text, object_pairs_hook=_unique_object)
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "results"}:
        raise ValueError("unit {!r} result has the wrong shape".format(unit_id))
    if payload["schema_version"] != "1" or not isinstance(payload["results"], list):
        raise ValueError("unit {!r} result has an invalid schema".format(unit_id))
    expected = {target["anchor"] for target in targets}
    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    for item in payload["results"]:
        if not isinstance(item, dict) or set(item) != {"anchor", "machine"}:
            raise ValueError("unit {!r} result has an invalid target".format(unit_id))
        anchor = item["anchor"]
        if not isinstance(anchor, str) or anchor not in expected or anchor in seen:
            raise ValueError("unit {!r} result does not partition targets".format(unit_id))
        seen.add(anchor)
        results.append({"anchor": anchor, "machine": item["machine"]})
    if seen != expected:
        raise ValueError("unit {!r} result does not partition targets".format(unit_id))
    results.sort(key=lambda item: item["anchor"])
    return {"schema_version": "1", "results": results}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key {!r}".format(key))
        result[key] = value
    return result



def validation_spec_for_unit(unit: Any) -> Any:
    """The ValidationSpec for one unit, shared by the inline lane and the mount.

    Both paths must judge a worker response identically, so this is the single
    source: an agentic multi-target unit parses the ``results`` schema, and a
    mechanical single-target unit passes its text through.
    """
    from content_pipeline.llm.platform import ValidationSpec

    targets = unit_targets(unit)
    if "targets" in unit.payload:
        return ValidationSpec(
            parse_fn=lambda text: parse_agentic_result(text, targets, unit.id),
            validators=(),
        )
    return ValidationSpec(parse_fn=lambda text: text, validators=())


def prompt_for_payload(payload: Mapping[str, Any]) -> str:
    """The canonical user prompt for either unit shape.

    A mechanical unit carries one anchored slice; an agentic unit carries an
    instruction and a list of targets. Both the inline generator and the worker
    mount render through here, so a worker is asked the same question whichever
    lane reaches it -- and a multi-target unit is actually shown the targets whose
    anchors its response is required to partition.
    """
    if "targets" in payload:
        return yaml.safe_dump(
            {
                "instruction": payload.get("instruction"),
                "targets": [
                    {
                        "anchor": target.get("anchor"),
                        "slice": plain_value(target.get("anchored_slice")),
                        "comments": list(target.get("comments", [])),
                        "content_hash": target.get("content_hash"),
                    }
                    for target in payload["targets"]
                ],
            },
            sort_keys=False,
            allow_unicode=True,
        )
    return yaml.safe_dump(
        {
            "anchor": payload.get("anchor"),
            "slice": plain_value(payload.get("anchored_slice")),
            "comments": list(payload.get("comments", [])),
            "content_hash": payload.get("content_hash"),
        },
        sort_keys=False,
        allow_unicode=True,
    )


__all__ = [
    "parse_agentic_result",
    "plain_value",
    "prompt_for_payload",
    "unit_targets",
    "validation_spec_for_unit",
]
