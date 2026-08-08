#!/usr/bin/env python3
"""Generate the decision half of awesome-kit's orchestration policy.

The decision data comes only from the fenced YAML blocks in
``tier-principles.md`` and the term records in ``lexicon.md``. The policy's
header comment and machine half are spliced around that generated data as raw
bytes so their comments and formatting remain untouched.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence, TextIO

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
PRINCIPLES_REL = (
    "plugins/awesome-kit/skills/orchestrate/references/tier-principles.md"
)
LEXICON_REL = "plugins/awesome-kit/skills/orchestrate/references/lexicon.md"
POLICY_REL = "plugins/awesome-kit/skills/orchestrate/defaults/orchestration.yaml"

PRINCIPLES_PATH = REPO_ROOT / PRINCIPLES_REL
LEXICON_PATH = REPO_ROOT / LEXICON_REL
POLICY_PATH = REPO_ROOT / POLICY_REL

SCHEMA_VERSION = 2
_BANNER_RULE = "-" * 75
_MACHINE_MARKER = b"# MACHINE HALF -- not derived from the principles."

_YAML_FENCE_RE = re.compile(
    r"^```yaml[ \t]*\r?\n(?P<body>.*?)^```[ \t]*(?:\r?\n|\Z)",
    re.MULTILINE | re.DOTALL,
)
_PATH_PART_RE = re.compile(
    r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*)(?:\[(?P<member>[^.\[\]]+)\])?$"
)
_LEXICON_HEADING_RE = re.compile(
    r"^###\s+`(?P<id>[^`]+)`\s+`\[(?P<kind>skill|concept)\]`"
    r"(?:\s+`render:\s*(?P<render>[^`]+)`)?.*$",
    re.MULTILINE,
)
_LEXICON_FIELD_RE = re.compile(r"^\*\*(Test|Gloss):\*\*\s*(.*)$")
_EMPHASIS_RE = re.compile(r"(?<!\*)\*([^*\r\n]+)\*(?!\*)")


class GenerationError(ValueError):
    """The declarative inputs cannot be generated without guessing."""


class _AddressedList:
    """Members accumulated through a dotted-path ``field[id]`` selector."""

    def __init__(self) -> None:
        self.members: dict[str, dict[str, Any]] = {}


class _OrchestrationDumper(yaml.SafeDumper):
    """Stable, readable YAML without aliases."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def _represent_string(
    dumper: _OrchestrationDumper, value: str
) -> yaml.nodes.ScalarNode:
    if "\n" in value:
        style = "|"
    elif len(value) > 88 and " " in value:
        style = ">"
    else:
        style = None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


def _represent_list(
    dumper: _OrchestrationDumper, value: list[Any]
) -> yaml.nodes.SequenceNode:
    node = yaml.SafeDumper.represent_list(dumper, value)
    if value and all(
        item is None or isinstance(item, (str, int, float, bool)) for item in value
    ):
        rendered_length = sum(len(str(item)) for item in value) + (2 * len(value))
        if rendered_length <= 72:
            node.flow_style = True
    return node


_OrchestrationDumper.add_representer(str, _represent_string)
_OrchestrationDumper.add_representer(list, _represent_list)


def parse_principles(
    text: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return all ``emits`` mappings and the single ``generator`` mapping.

    Every YAML fence is parsed. Fences whose sole root key is neither
    ``emits`` nor ``generator`` are intentionally ignored.
    """

    emits: list[dict[str, Any]] = []
    generators: list[dict[str, Any]] = []

    for match in _YAML_FENCE_RE.finditer(text):
        body = match.group("body")
        try:
            parsed = yaml.safe_load(body)
        except yaml.YAMLError as exc:
            line = text.count("\n", 0, match.start()) + 1
            raise GenerationError(
                f"tier-principles.md:{line}: invalid fenced YAML: {exc}"
            ) from exc

        if not isinstance(parsed, dict) or len(parsed) != 1:
            continue
        root_key = next(iter(parsed))
        value = parsed[root_key]
        if root_key == "emits":
            if not isinstance(value, dict):
                raise GenerationError("an emits block must contain a mapping")
            if not all(isinstance(path, str) for path in value):
                raise GenerationError("every emits target must be a string")
            emits.append(value)
        elif root_key == "generator":
            if not isinstance(value, dict):
                raise GenerationError("the generator block must contain a mapping")
            generators.append(value)

    if not emits:
        raise GenerationError("tier-principles.md contains no emits blocks")
    if len(generators) != 1:
        raise GenerationError(
            "tier-principles.md must contain exactly one generator block"
        )
    return emits, generators[0]


def _parse_path(path: str) -> list[tuple[str, str | None]]:
    parts: list[tuple[str, str | None]] = []
    for raw_part in path.split("."):
        match = _PATH_PART_RE.fullmatch(raw_part)
        if match is None:
            raise GenerationError(f"invalid emits target: {path!r}")
        parts.append((match.group("key"), match.group("member")))
    return parts


def _merge_values(left: Any, right: Any, path: str) -> Any:
    if isinstance(left, dict) and isinstance(right, dict):
        for key, value in right.items():
            child_path = f"{path}.{key}" if path else str(key)
            if key in left:
                left[key] = _merge_values(left[key], copy.deepcopy(value), child_path)
            else:
                left[key] = copy.deepcopy(value)
        return left
    if isinstance(left, list) and isinstance(right, list):
        left.extend(copy.deepcopy(right))
        return left
    if left == right:
        return left
    raise GenerationError(f"conflicting emits values at {path}")


def _insert_emit(root: dict[str, Any], path: str, value: Any) -> None:
    parts = _parse_path(path)
    current: dict[str, Any] = root

    for index, (key, member_id) in enumerate(parts):
        is_last = index == len(parts) - 1
        current_path = ".".join(part for part, _ in parts[: index + 1])

        if member_id is not None:
            collection = current.get(key)
            if collection is None:
                collection = _AddressedList()
                current[key] = collection
            if not isinstance(collection, _AddressedList):
                raise GenerationError(
                    f"{current_path} is used as both a mapping and an addressed list"
                )

            member = collection.members.setdefault(member_id, {})
            if is_last:
                if not isinstance(value, dict):
                    raise GenerationError(
                        f"addressed target {path!r} must emit a mapping"
                    )
                merged = _merge_values(member, copy.deepcopy(value), path)
                emitted_id = merged.get("id")
                if emitted_id is None:
                    merged["id"] = member_id
                elif emitted_id != member_id:
                    raise GenerationError(
                        f"{path!r} addresses id {member_id!r} but emits {emitted_id!r}"
                    )
                collection.members[member_id] = merged
                return
            current = member
            continue

        if is_last:
            if key in current:
                current[key] = _merge_values(current[key], copy.deepcopy(value), path)
            else:
                current[key] = copy.deepcopy(value)
            return

        child = current.get(key)
        if child is None:
            child = {}
            current[key] = child
        if not isinstance(child, dict):
            raise GenerationError(
                f"cannot descend through non-mapping target {current_path}"
            )
        current = child


def _order_of(record: Mapping[str, Any], path: str) -> int:
    order = record.get("order")
    if isinstance(order, bool) or not isinstance(order, int):
        raise GenerationError(f"{path} requires an integer order")
    return order


def _ensure_unique_orders(records: Iterable[Mapping[str, Any]], path: str) -> None:
    seen: set[int] = set()
    for record in records:
        order = _order_of(record, path)
        if order in seen:
            raise GenerationError(f"duplicate order {order} at {path}")
        seen.add(order)


def _is_keyed_ordered_collection(value: dict[str, Any]) -> bool:
    return bool(value) and all(
        isinstance(record, dict)
        and "order" in record
        and record.get("id") == member_id
        for member_id, record in value.items()
    )


def _normalize(value: Any, path: str = "") -> Any:
    if isinstance(value, _AddressedList):
        records = list(value.members.values())
        _ensure_unique_orders(records, path)
        ordered = sorted(records, key=lambda record: _order_of(record, path))
        return [_normalize(record, path) for record in ordered]

    if isinstance(value, dict):
        if _is_keyed_ordered_collection(value):
            records = list(value.values())
            _ensure_unique_orders(records, path)
            ordered = sorted(records, key=lambda record: _order_of(record, path))
            return [_normalize(record, path) for record in ordered]

        normalized: dict[str, Any] = {}
        for key, child in value.items():
            if key == "order":
                continue
            child_path = f"{path}.{key}" if path else str(key)
            normalized[key] = _normalize(child, child_path)
        return normalized

    if isinstance(value, list):
        ordered_flags = [isinstance(item, dict) and "order" in item for item in value]
        if any(ordered_flags):
            if not all(ordered_flags):
                raise GenerationError(
                    f"ordered and unordered entries are mixed at {path}"
                )
            records = [item for item in value if isinstance(item, dict)]
            _ensure_unique_orders(records, path)
            records.sort(key=lambda record: _order_of(record, path))
            result: list[Any] = []
            for record in records:
                without_order = {
                    key: child for key, child in record.items() if key != "order"
                }
                if set(without_order) == {"value"}:
                    result.append(_normalize(without_order["value"], path))
                else:
                    result.append(_normalize(without_order, path))
            return result
        return [_normalize(item, path) for item in value]

    return value


def merge_emits(emit_mappings: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Merge dotted emit targets, resolve addressed members, and order lists."""

    root: dict[str, Any] = {}
    for emit_mapping in emit_mappings:
        for path, value in emit_mapping.items():
            _insert_emit(root, path, value)
    normalized = _normalize(root)
    if not isinstance(normalized, dict):
        raise GenerationError("merged emits root must be a mapping")
    return normalized


def _field_value(lines: list[str], start: int, initial: str) -> str:
    parts = [initial.strip()]
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped:
            break
        if _LEXICON_FIELD_RE.match(line):
            break
        if stripped.startswith(("#", "*", "- ", ">", "```")):
            break
        parts.append(stripped)
    return " ".join(part for part in parts if part)


def _normalize_test(value: str) -> str:
    value = _EMPHASIS_RE.sub(r"`\1`", value)
    return value[:1].upper() + value[1:]


def _normalize_gloss(value: str) -> str:
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def parse_lexicon(text: str) -> list[dict[str, str]]:
    """Parse controlled-vocabulary term records in Markdown document order."""

    headings = list(_LEXICON_HEADING_RE.finditer(text))
    if not headings:
        raise GenerationError("lexicon.md contains no term headings")

    records: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for index, heading in enumerate(headings):
        term_id = heading.group("id")
        if term_id in seen_ids:
            raise GenerationError(f"duplicate lexicon term {term_id!r}")
        seen_ids.add(term_id)

        section_end = (
            headings[index + 1].start() if index + 1 < len(headings) else len(text)
        )
        lines = text[heading.end() : section_end].splitlines()
        fields: dict[str, str] = {}
        for line_index, line in enumerate(lines):
            field_match = _LEXICON_FIELD_RE.match(line)
            if field_match is None:
                continue
            field_name = field_match.group(1).lower()
            if field_name in fields:
                raise GenerationError(
                    f"lexicon term {term_id!r} repeats the {field_name} field"
                )
            fields[field_name] = _field_value(
                lines, line_index, field_match.group(2)
            )

        kind = heading.group("kind")
        render = heading.group("render")
        if "test" not in fields:
            raise GenerationError(f"lexicon term {term_id!r} has no Test field")

        record = {"id": term_id, "kind": kind}
        if kind == "concept":
            if render is not None or "gloss" in fields:
                raise GenerationError(
                    f"concept term {term_id!r} may not declare render or Gloss"
                )
        else:
            if render is None:
                raise GenerationError(f"skill term {term_id!r} has no render flag")
            render = render.strip()
            if render not in {"bare", "glossed"}:
                raise GenerationError(
                    f"skill term {term_id!r} has invalid render flag {render!r}"
                )
            record["render"] = render
            if render == "glossed" and "gloss" not in fields:
                raise GenerationError(f"glossed term {term_id!r} has no Gloss field")

        record["test"] = _normalize_test(fields["test"])
        if kind == "skill" and render == "glossed":
            record["gloss"] = _normalize_gloss(fields["gloss"])
        records.append(record)

    return records


def _generator_blocks(generator: Mapping[str, Any]) -> list[dict[str, str]]:
    if generator.get("intra_block_order") != "principle-number":
        raise GenerationError("unsupported generator intra_block_order")
    if generator.get("intra_block_order_scope") != "slot":
        raise GenerationError("unsupported generator intra_block_order_scope")

    raw_blocks = generator.get("blocks")
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raise GenerationError("generator.blocks must be a non-empty list")
    if not all(isinstance(block, dict) for block in raw_blocks):
        raise GenerationError("every generator block must be a mapping")

    blocks = [block for block in raw_blocks if isinstance(block, dict)]
    _ensure_unique_orders(blocks, "generator.blocks")
    blocks.sort(key=lambda block: _order_of(block, "generator.blocks"))

    result: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for block in blocks:
        path = block.get("path")
        label = block.get("label")
        if not isinstance(path, str) or _PATH_PART_RE.fullmatch(path) is None:
            raise GenerationError(f"invalid top-level generator block path {path!r}")
        if "[" in path or "." in path:
            raise GenerationError(f"generator block path must be top-level: {path!r}")
        if not isinstance(label, str) or not label:
            raise GenerationError(f"generator block {path!r} requires a label")
        if not label.isascii():
            raise GenerationError(f"generator block label must be ASCII: {label!r}")
        if path in seen_paths:
            raise GenerationError(f"duplicate generator block path {path!r}")
        seen_paths.add(path)
        result.append({"path": path, "label": label})
    return result


def build_decision(
    principles_text: str, lexicon_text: str
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Build the generated decision mapping and ordered block metadata."""

    emit_mappings, generator = parse_principles(principles_text)
    content = merge_emits(emit_mappings)
    blocks = _generator_blocks(generator)

    required_preamble = {"default_backend", "resolution"}
    missing_preamble = sorted(required_preamble - content.keys())
    if missing_preamble:
        raise GenerationError(
            "emits blocks are missing: " + ", ".join(missing_preamble)
        )

    block_paths = {block["path"] for block in blocks}
    expected_paths = required_preamble | block_paths
    unexpected = sorted(content.keys() - expected_paths)
    missing_blocks = sorted(block_paths - content.keys())
    if unexpected:
        raise GenerationError(
            "emits blocks produce unstructured top-level paths: "
            + ", ".join(unexpected)
        )
    if missing_blocks:
        raise GenerationError(
            "generator blocks have no emitted content: " + ", ".join(missing_blocks)
        )

    decision: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "default_backend": content["default_backend"],
        "resolution": content["resolution"],
        "lexicon": parse_lexicon(lexicon_text),
    }
    for block in blocks:
        path = block["path"]
        value = copy.deepcopy(content[path])
        if isinstance(value, dict):
            existing_title = value.get("title")
            if existing_title is not None and existing_title != block["label"]:
                raise GenerationError(
                    f"{path}.title conflicts with generator label {block['label']!r}"
                )
            value = {"title": block["label"], **value}
        decision[path] = value
    return decision, blocks


def _dump_mapping(value: Mapping[str, Any]) -> str:
    return yaml.dump(
        dict(value),
        Dumper=_OrchestrationDumper,
        allow_unicode=False,
        default_flow_style=False,
        indent=2,
        sort_keys=False,
        width=88,
    ).rstrip("\n")


def _banner(label: str) -> str:
    return f"# {_BANNER_RULE}\n# {label}\n# {_BANNER_RULE}"


def render_decision(
    decision: Mapping[str, Any], blocks: Sequence[Mapping[str, str]], newline: str
) -> bytes:
    """Render the generated policy portion using the target newline style."""

    chunks = [
        _dump_mapping(
            {
                "schema_version": decision["schema_version"],
                "default_backend": decision["default_backend"],
            }
        ),
        _banner("Resolution semantics")
        + "\n"
        + _dump_mapping({"resolution": decision["resolution"]}),
        _banner("Lexicon") + "\n" + _dump_mapping({"lexicon": decision["lexicon"]}),
    ]
    for block in blocks:
        path = block["path"]
        chunks.append(
            _banner(block["label"]) + "\n" + _dump_mapping({path: decision[path]})
        )

    rendered = "\n\n".join(chunks) + "\n\n"
    if newline != "\n":
        rendered = rendered.replace("\n", newline)
    try:
        return rendered.encode("ascii")
    except UnicodeEncodeError as exc:
        raise GenerationError("generated orchestration YAML is not ASCII") from exc


def split_policy_bytes(data: bytes) -> tuple[bytes, bytes, bytes, str]:
    """Split a policy into verbatim header, decision, and machine-half bytes."""

    schema_matches = list(re.finditer(rb"(?m)^schema_version\s*:", data))
    if len(schema_matches) != 1:
        raise GenerationError("policy must contain exactly one schema_version line")
    decision_start = schema_matches[0].start()

    marker_indexes = [
        match.start() for match in re.finditer(re.escape(_MACHINE_MARKER), data)
    ]
    if len(marker_indexes) != 1:
        raise GenerationError("policy must contain exactly one MACHINE HALF marker")
    marker_index = marker_indexes[0]
    banner_match = None
    for match in re.finditer(rb"(?m)^# ={10,}\r?$", data[:marker_index]):
        banner_match = match
    if banner_match is None:
        raise GenerationError("MACHINE HALF marker has no opening banner")
    machine_start = banner_match.start()
    if not decision_start < machine_start:
        raise GenerationError("MACHINE HALF marker precedes the decision half")

    newline = "\r\n" if b"\r\n" in data else "\n"
    return (
        data[:decision_start],
        data[decision_start:machine_start],
        data[machine_start:],
        newline,
    )


def generate_policy_bytes(
    current_policy: bytes, principles_text: str, lexicon_text: str
) -> bytes:
    """Return the complete policy with only its decision half regenerated."""

    header, _old_decision, machine, newline = split_policy_bytes(current_policy)
    decision, blocks = build_decision(principles_text, lexicon_text)
    return header + render_decision(decision, blocks, newline) + machine


def expected_policy_bytes(
    policy_path: Path = POLICY_PATH,
    principles_path: Path = PRINCIPLES_PATH,
    lexicon_path: Path = LEXICON_PATH,
) -> bytes:
    """Read generator inputs and return the expected complete policy bytes."""

    current = policy_path.read_bytes()
    principles = principles_path.read_text(encoding="utf-8")
    lexicon = lexicon_path.read_text(encoding="utf-8")
    return generate_policy_bytes(current, principles, lexicon)


def write_policy(
    policy_path: Path = POLICY_PATH,
    principles_path: Path = PRINCIPLES_PATH,
    lexicon_path: Path = LEXICON_PATH,
) -> bool:
    """Rewrite the policy when needed; return whether its bytes changed."""

    current = policy_path.read_bytes()
    expected = generate_policy_bytes(
        current,
        principles_path.read_text(encoding="utf-8"),
        lexicon_path.read_text(encoding="utf-8"),
    )
    if current == expected:
        return False
    policy_path.write_bytes(expected)
    return True


def check_policy(
    policy_path: Path = POLICY_PATH,
    principles_path: Path = PRINCIPLES_PATH,
    lexicon_path: Path = LEXICON_PATH,
    output: TextIO = sys.stdout,
) -> int:
    """Return zero on exact agreement, or print a unified diff and return one."""

    current = policy_path.read_bytes()
    expected = generate_policy_bytes(
        current,
        principles_path.read_text(encoding="utf-8"),
        lexicon_path.read_text(encoding="utf-8"),
    )
    if current == expected:
        return 0

    current_lines = current.decode("utf-8").splitlines(keepends=True)
    expected_lines = expected.decode("utf-8").splitlines(keepends=True)
    diff = difflib.unified_diff(
        current_lines,
        expected_lines,
        fromfile=POLICY_REL,
        tofile=f"{POLICY_REL} (generated)",
    )
    output.writelines(diff)
    return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate awesome-kit's orchestration decision half."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_const",
        const="check",
        dest="mode",
        help="check for drift and print a unified diff (default)",
    )
    mode.add_argument(
        "--write",
        action="store_const",
        const="write",
        dest="mode",
        help="rewrite orchestration.yaml in place",
    )
    parser.set_defaults(mode="check")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""

    args = _parser().parse_args(argv)
    try:
        if args.mode == "write":
            write_policy(POLICY_PATH, PRINCIPLES_PATH, LEXICON_PATH)
            return 0
        return check_policy(
            POLICY_PATH, PRINCIPLES_PATH, LEXICON_PATH, output=sys.stdout
        )
    except (GenerationError, OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
