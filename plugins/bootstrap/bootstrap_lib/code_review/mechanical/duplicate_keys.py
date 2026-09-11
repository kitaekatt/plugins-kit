"""Detect repeated explicit mapping keys in structured data."""

from __future__ import annotations

import re
import tomllib
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from . import MechanicalFinding, MechanicalSnapshot


def _kind(file: str) -> str | None:
    suffix = file.lower().rsplit(".", 1)[-1] if "." in file else ""
    return {"json": "json", "yaml": "yaml", "yml": "yaml", "toml": "toml"}.get(suffix)


def precondition(snapshot: MechanicalSnapshot) -> bool:
    """Run only for complete, non-templated structured-data images."""
    return snapshot.post_image_text is not None and _kind(snapshot.file) is not None and not re.search(
        r"\{\{|\}\}|\{%|%\}|\$\{", snapshot.post_image_text
    )


def _composed_duplicates(text: str) -> list[tuple[str, int]]:
    duplicates: list[tuple[str, int]] = []
    for node in yaml.compose_all(text):
        for mapping in _walk_yaml(node):
            seen: set[str] = set()
            for key_node, _ in mapping.value:
                key = str(key_node.value)
                if key in seen and key != "<<":
                    duplicates.append((key, key_node.start_mark.line + 1))
                seen.add(key)
    return duplicates


def _walk_yaml(node: Any) -> list[Any]:
    if isinstance(node, yaml.MappingNode):
        return [node, *[item for key, value in node.value for item in _walk_yaml(value)]]
    if isinstance(node, yaml.SequenceNode):
        return [item for child in node.value for item in _walk_yaml(child)]
    return []


def _toml_duplicates(text: str) -> list[tuple[str, int]]:
    seen: set[tuple[str, str]] = set()
    table = ""
    result: list[tuple[str, int]] = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            table = line.strip("[]").strip()
        elif "=" in line:
            key = line.split("=", 1)[0].strip().strip('"')
            identity = (table, key)
            if identity in seen:
                result.append((key, line_no))
            seen.add(identity)
    return result


def scan(snapshot: MechanicalSnapshot) -> tuple[MechanicalFinding, ...]:
    """Return later-key locations, preserving explicit key events."""
    assert snapshot.post_image_text is not None
    kind = _kind(snapshot.file)
    try:
        if kind in ("json", "yaml"):
            # JSON goes through the SAME node composer as YAML, not through
            # json.loads. A pairs hook reports WHICH key repeated but not
            # WHERE, and recovering the position by grepping the file for that
            # key name cannot tell a genuine repeat inside one object from the
            # same name used legitimately in a sibling object -- which is the
            # common case in real JSON and produced both duplicated and
            # outright false findings. The composer carries a start mark per
            # key occurrence, so the location is read rather than guessed.
            # A JSON file the composer cannot parse raises and we emit
            # nothing, which is the fail-closed direction.
            locations = _composed_duplicates(snapshot.post_image_text)
        else:
            locations = _toml_duplicates(snapshot.post_image_text)
    except (yaml.YAMLError, tomllib.TOMLDecodeError):
        return ()
    added = {line for line, _ in snapshot.added_lines or ()}
    seen_locations: set[tuple[str, int]] = set()
    findings: list[MechanicalFinding] = []
    for key, line in locations:
        if line not in added or (key, line) in seen_locations:
            continue
        seen_locations.add((key, line))
        findings.append(
            {
                "check": "duplicate_keys",
                "line": line,
                "detail": f"repeated explicit key {key!r}",
            }
        )
    return tuple(findings)
