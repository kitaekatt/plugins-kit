"""Skill material: caller-selected skill text for a prompt, with provenance.

A caller names skills by path and gets one text block to place in a system
prompt: each skill at level ``catalog`` (name and description) or ``full``
(name, description, instructions and resources), bounded by a token budget
the caller must state, with repeated skills and resources rendered once and
every file that was read recorded by hash in a report. The contract is
specified in the plugin-dev skill's ``references/skill-material.md``; this
module is its one implementation.

Three things here are frozen. Format ``"1"`` is the exact bytes ``_render_v1``
produces; a later format is a second entry in the renderer registry and never
an edit to the first. ``REPORT_SCHEMA_V1`` names the report document
``SkillMaterialReport.to_json()`` returns. ``SUPPORTED_REPORT_SCHEMAS`` is the
one capability marker a consumer probes, and it only grows.

The strict reader never degrades. Frontmatter that is missing, is not valid
YAML or is not a mapping raises ``FrontmatterError``; it never becomes empty
fields. PyYAML is imported inside the functions that parse, so importing this
module cannot fail for a missing package, and an interpreter without PyYAML
gets ``PyYamlUnavailableError`` by name. That error is about the environment
and is deliberately not a ``SkillMaterialError``: a caller that catches
refused input does not catch it.

A ``full`` skill loads the resources the skill itself declares: the ``path``
of each record in a ``references`` list, in the fenced YAML blocks of its
``SKILL.md``. Resources are confined to their skill's directory. The caller
is the authority for the skill paths it names; no root restricts them.

The module reads files and returns values. It writes nothing, imports the
stdlib only at module top, and imports no first-party package.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple


__all__ = [
    "REPORT_SCHEMA_V1",
    "SUPPORTED_REPORT_SCHEMAS",
    "SUPPORTED_FORMATS",
    "LEVEL_CATALOG",
    "LEVEL_FULL",
    "TOKEN_ESTIMATE",
    "FRONTMATTER_RE",
    "SkillMaterialError",
    "FrontmatterError",
    "SkillMaterialBudgetExceeded",
    "SkillMaterialUnavailableError",
    "PyYamlUnavailableError",
    "parse_frontmatter_strict",
    "SkillDocument",
    "read_skill",
    "SkillRef",
    "SkillSelection",
    "ResourceProvenance",
    "SkillProvenance",
    "SuppressedRef",
    "SkillMaterialReport",
    "MaterializedSkills",
    "materialize",
    "estimate_tokens",
]


# FROZEN. Never reassigned; a later report revision adds its own constant.
REPORT_SCHEMA_V1 = "plugins-kit.skill-material-report/v1"

# The capability marker a consumer probes. It only ever grows.
SUPPORTED_REPORT_SCHEMAS = frozenset({REPORT_SCHEMA_V1})

# The registered renderers. An unknown `format_version` is refused against it.
SUPPORTED_FORMATS = frozenset({"1"})

LEVEL_CATALOG = "catalog"
LEVEL_FULL = "full"

# How `estimated_tokens` is computed; reported verbatim in every report.
TOKEN_ESTIMATE = "chars/4"

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

_LEVELS = (LEVEL_CATALOG, LEVEL_FULL)
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_SEGMENT_RE = re.compile(r"[A-Za-z0-9._-]+")
_DRIVE_RE = re.compile(r"[A-Za-z]:")
_FENCE_OPEN_RE = re.compile(r"```(?:yaml|yml)[ \t]*")
_FENCE_CLOSE = "```"
_SKILL_FILE = "SKILL.md"
_BOM = chr(0xFEFF)

_REASON_SAME_FILE = "same-file"
_REASON_SAME_CONTENT = "same-content"
_REASON_DUPLICATE_RESOURCE = "duplicate-resource"

_SELECTION_KEYS = ("skills", "token_budget", "format_version")
_REF_KEYS = ("path", "level", "declared_resources", "resources")

_FIT_REMEDY = (
    'To fit: give a skill level "catalog", or set declared_resources=False '
    "on it and name a shorter list in resources."
)
# A catalog ref cannot be lowered, so its pre-read refusal names only the
# remedy that works.
_CATALOG_FIT_REMEDY = (
    'To fit: raise token_budget. The ref is already at level "catalog", '
    "and a SKILL.md is checked before it is read at either level."
)
_DECLARED_REMEDY = (
    "To load this skill without it, set declared_resources=False on the ref "
    "and name the files wanted in resources."
)
_PYYAML_MESSAGE = (
    "PyYAML is not importable in the interpreter running this call, and "
    "bootstrap_lib.skill_material reads a skill with PyYAML. Run the call "
    "from an environment that declares pyyaml (a plugin venv whose "
    "pyproject.toml lists it); the bare bootstrap standalone interpreter "
    "carries no third-party packages. No skill was parsed."
)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class SkillMaterialError(ValueError):
    """The INPUT is refused: a selection, a skill, or a resource."""


class FrontmatterError(SkillMaterialError):
    """A SKILL.md has no frontmatter block, or one the strict reader refuses."""


class SkillMaterialBudgetExceeded(SkillMaterialError):
    """The selection does not fit `token_budget`. Nothing is truncated."""


class SkillMaterialUnavailableError(RuntimeError):
    """The ENVIRONMENT cannot read skills. Not a refusal of the input."""


class PyYamlUnavailableError(SkillMaterialUnavailableError):
    """PyYAML cannot be imported in the running interpreter."""


# --------------------------------------------------------------------------
# Small pure helpers
# --------------------------------------------------------------------------


def _yaml() -> Any:
    try:
        import yaml
    except ImportError as exc:
        raise PyYamlUnavailableError(_PYYAML_MESSAGE) from exc
    return yaml


def estimate_tokens(text: str) -> int:
    """`ceil(len(text) / 4)` over Unicode code points. An estimate."""
    return (len(text) + 3) // 4


def _size_lower_bound(size_bytes: int) -> int:
    # A UTF-8 code point is at most 4 bytes, so a file of `size_bytes` holds
    # at least size/4 code points and estimates to at least ceil(size/16).
    return (size_bytes + 15) // 16


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")


def _escape_text(value: str) -> str:
    # `&` first: escaping it last would re-escape the entities just written.
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_attr(value: str) -> str:
    return _escape_text(value).replace('"', "&quot;")


def _one_line(value: Any) -> str:
    return " ".join(str(value).split())


def _unique_in_order(paths: Sequence[str]) -> List[str]:
    """Each distinct string once, at its first position."""
    seen = set()
    unique: List[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _contained(root_real: Path, candidate_real: Path) -> bool:
    """True when the resolved candidate lies inside the resolved root."""
    return candidate_real.is_relative_to(root_real)


def _check_resource_path(value: str, *, declared: bool) -> str:
    """Refuse a resource path outside the grammar; return it without the
    one trailing `/` a DECLARED path may carry."""
    if value == "":
        raise SkillMaterialError("a resource path is empty")
    if "\\" in value:
        raise SkillMaterialError(
            f"resource path {value!r} contains a backslash; "
            "write it with '/' separators"
        )
    if value.startswith("/"):
        raise SkillMaterialError(
            f"resource path {value!r} is absolute; "
            "it must be relative to the skill directory"
        )
    if _DRIVE_RE.match(value):
        raise SkillMaterialError(
            f"resource path {value!r} names a drive; "
            "it must be relative to the skill directory"
        )
    relative = value
    if relative.endswith("/"):
        if not declared:
            raise SkillMaterialError(
                f"resource path {value!r} ends with '/'; "
                "a named resource is one regular file"
            )
        relative = relative[:-1]
    for segment in relative.split("/"):
        if segment == "":
            raise SkillMaterialError(
                f"resource path {value!r} has an empty segment"
            )
        if segment == ".":
            raise SkillMaterialError(
                f"resource path {value!r} has a '.' segment"
            )
        if segment == "..":
            raise SkillMaterialError(
                f"resource path {value!r} has a '..' segment; "
                "a resource stays inside its skill directory"
            )
        if not _SEGMENT_RE.fullmatch(segment):
            raise SkillMaterialError(
                f"resource path {value!r} has a segment outside "
                f"[A-Za-z0-9._-]: {segment!r}"
            )
    return relative


# --------------------------------------------------------------------------
# The strict reader
# --------------------------------------------------------------------------


def parse_frontmatter_strict(content: str) -> Tuple[dict, str, str]:
    """Return `(fields, raw_block, body)` for text that opens with a
    frontmatter block.

    Raises `FrontmatterError` when there is no block, the block is not valid
    YAML, or it is not a mapping, and `PyYamlUnavailableError` when PyYAML
    cannot be imported. It never returns empty fields for a block it could
    not read. `content` is decoded text; a leading byte-order mark is the
    caller's to remove (`read_skill` and `materialize` remove it).
    """
    yaml = _yaml()
    if not isinstance(content, str):
        raise FrontmatterError(
            f"frontmatter is read from text, got {type(content).__name__}"
        )
    match = FRONTMATTER_RE.match(content)
    if match is None:
        raise FrontmatterError(
            "no frontmatter block: the text must open with a '---' line, "
            "YAML, and a closing '---' line"
        )
    raw_block = match.group(1)
    try:
        fields = yaml.safe_load(raw_block)
    except (yaml.YAMLError, RecursionError) as exc:
        raise FrontmatterError(
            f"frontmatter is not valid YAML: {_one_line(exc)}"
        ) from exc
    if not isinstance(fields, dict):
        raise FrontmatterError(
            "frontmatter must be a YAML mapping, got "
            f"{type(fields).__name__}"
        )
    return fields, raw_block, content[match.end():]


def _raw_declarations(value: Any) -> List[str]:
    """Every declared resource path in one parsed YAML block, in walk order.

    A pre-order walk with an explicit stack: a mapping's entries in the
    mapping's own order, a list's items by index. At an entry whose key is
    exactly `references` and whose value is a list, the list's own declaring
    items (a mapping whose `path` is a string) are recorded first, in index
    order, and only then is the list walked for anything nested in its items.

    Repeated strings are NOT removed here; `_unique_in_order` does that. Two
    identity sets make the walk safe under YAML aliases, merge keys and
    cycles, where one object is reachable more than once: a `references`
    list is recorded once, and a container is entered once.
    """
    found: List[str] = []
    entered = set()
    recorded = set()
    stack: List[Any] = []

    def children(node: Any) -> Any:
        if isinstance(node, dict):
            return iter(list(node.items()))
        return iter([(None, item) for item in node])

    if isinstance(value, (dict, list)):
        entered.add(id(value))
        stack.append(children(value))
    while stack:
        try:
            key, child = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        if (
            isinstance(key, str)
            and key == "references"
            and isinstance(child, list)
            and id(child) not in recorded
        ):
            recorded.add(id(child))
            for item in child:
                if isinstance(item, dict) and isinstance(item.get("path"), str):
                    found.append(item["path"])
        if isinstance(child, (dict, list)) and id(child) not in entered:
            entered.add(id(child))
            stack.append(children(child))
    return found


def _yaml_blocks(body: str) -> Tuple[List[str], int]:
    """The fenced YAML blocks of a normalized body, in order, and the number
    of fences that never close."""
    blocks: List[str] = []
    unterminated = 0
    lines = body.split("\n")
    index = 0
    while index < len(lines):
        if not _FENCE_OPEN_RE.fullmatch(lines[index]):
            index += 1
            continue
        close = index + 1
        while close < len(lines) and lines[close] != _FENCE_CLOSE:
            close += 1
        if close == len(lines):
            unterminated += 1
            break
        blocks.append("\n".join(lines[index + 1:close]))
        index = close + 1
    return blocks, unterminated


def _declarations(body: str) -> Tuple[Tuple[str, ...], int]:
    """`(declared, unparsed_yaml_blocks)` for a normalized body."""
    yaml = _yaml()
    blocks, unparsed = _yaml_blocks(body)
    raw: List[str] = []
    for block in blocks:
        try:
            parsed = yaml.safe_load(block)
        except (yaml.YAMLError, RecursionError):
            # Not a refusal: a body fence is not the skill's identity the
            # way frontmatter is. The count reaches the report.
            unparsed += 1
            continue
        raw.extend(_raw_declarations(parsed))
    return tuple(_unique_in_order(raw)), unparsed


@dataclass(frozen=True)
class SkillDocument:
    name: str
    description: str
    body: str                      # normalized, frontmatter stripped
    source: str                    # resolved SKILL.md path as str
    sha256: str                    # of the raw file bytes
    bytes: int
    declared: Tuple[str, ...]      # declared resource paths, as written
    unparsed_yaml_blocks: int      # fenced YAML blocks PyYAML did not parse


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _file_size(path: Path) -> int:
    return os.stat(path).st_size


def _decode(raw: bytes, what: str) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillMaterialError(
            f"{what} is not valid UTF-8 (first bad byte at offset {exc.start})"
        ) from exc
    if text.startswith(_BOM):
        text = text[1:]
    return text


def _base_dir(base_dir: Any) -> Path:
    return Path.cwd() if base_dir is None else Path(base_dir)


def _locate(path_value: Any, base_dir: Path, where: str) -> Tuple[Path, Path]:
    """`(skill_dir, skill_md)`, both resolved. No root restricts the path:
    the caller is its authority."""
    shown = os.fspath(path_value)
    candidate = Path(shown)
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    try:
        real = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SkillMaterialError(
            f"{where}: {shown!r} does not resolve to an existing path"
        ) from exc
    if real.is_dir():
        skill_dir = real
    else:
        if candidate.name != _SKILL_FILE:
            raise SkillMaterialError(
                f"{where}: {shown!r} is a file that is not named "
                f"{_SKILL_FILE}; name a skill directory or its {_SKILL_FILE}"
            )
        skill_dir = candidate.parent.resolve(strict=True)
    try:
        skill_md = (skill_dir / _SKILL_FILE).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SkillMaterialError(
            f"{where}: {shown!r} is a directory with no {_SKILL_FILE}"
        ) from exc
    if not skill_md.is_file():
        raise SkillMaterialError(
            f"{where}: {_SKILL_FILE} under {shown!r} is not a regular file"
        )
    return skill_dir, skill_md


def _required_text(fields: dict, key: str, where: str) -> str:
    if key not in fields:
        raise SkillMaterialError(f"{where}: frontmatter has no `{key}`")
    value = fields[key]
    if not isinstance(value, str):
        raise SkillMaterialError(
            f"{where}: frontmatter `{key}` must be a string, got "
            f"{type(value).__name__}"
        )
    if not value.strip():
        raise SkillMaterialError(f"{where}: frontmatter `{key}` is empty")
    return value


def _document(raw: bytes, skill_md: Path, where: str) -> SkillDocument:
    text = _decode(raw, f"{where}: {skill_md}")
    try:
        fields, _raw_block, body = parse_frontmatter_strict(text)
    except FrontmatterError as exc:
        raise FrontmatterError(f"{where}: {skill_md}: {exc}") from exc
    name = _required_text(fields, "name", where)
    if not _NAME_RE.fullmatch(name):
        raise SkillMaterialError(
            f"{where}: skill name {name!r} does not match "
            "[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
        )
    description = _required_text(fields, "description", where)
    body = _normalize(body)
    declared, unparsed = _declarations(body)
    return SkillDocument(
        name=name,
        description=description,
        body=body,
        source=str(skill_md),
        sha256=hashlib.sha256(raw).hexdigest(),
        bytes=len(raw),
        declared=declared,
        unparsed_yaml_blocks=unparsed,
    )


def _read_file(path: Path, what: str) -> bytes:
    try:
        return _read_bytes(path)
    except OSError as exc:
        raise SkillMaterialError(f"{what} could not be read") from exc


def read_skill(path: Any, *, base_dir: Optional[Path] = None) -> SkillDocument:
    """Read one skill strictly: its identity, body, hash and declarations.

    `path` is a skill directory or its `SKILL.md`; a relative path resolves
    against `base_dir`, default the process cwd. No resource is resolved.
    """
    _yaml()
    where = "skill"
    _skill_dir, skill_md = _locate(path, _base_dir(base_dir), where)
    return _document(_read_file(skill_md, f"{where}: {skill_md}"), skill_md, where)


# --------------------------------------------------------------------------
# The selection
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillRef:
    path: str                         # a skill directory or its SKILL.md
    level: str = LEVEL_FULL           # "catalog" or "full"
    declared_resources: bool = True   # load what the skill declares
    resources: Tuple[str, ...] = ()   # further POSIX paths relative to the skill dir

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path:
            raise SkillMaterialError("SkillRef.path must be a non-empty str")
        if not isinstance(self.level, str) or self.level not in _LEVELS:
            raise SkillMaterialError(
                f"SkillRef.level must be {LEVEL_CATALOG!r} or {LEVEL_FULL!r}, "
                f"got {self.level!r}"
            )
        if not isinstance(self.declared_resources, bool):
            raise SkillMaterialError(
                "SkillRef.declared_resources must be a bool"
            )
        if not isinstance(self.resources, tuple) or any(
            not isinstance(item, str) for item in self.resources
        ):
            raise SkillMaterialError("SkillRef.resources must be a tuple of str")
        for item in self.resources:
            _check_resource_path(item, declared=False)
        if self.resources and self.level == LEVEL_CATALOG:
            raise SkillMaterialError(
                "SkillRef.resources names files on a catalog ref; a catalog "
                "ref renders no resource"
            )

    def to_json(self) -> dict:
        return {
            "path": self.path,
            "level": self.level,
            "declared_resources": self.declared_resources,
            "resources": list(self.resources),
        }


def _refuse_unknown_keys(mapping: Mapping, known: Sequence[str], what: str) -> None:
    unknown = sorted(str(key) for key in mapping if key not in known)
    if unknown:
        raise SkillMaterialError(
            f"{what} has unknown key(s) {', '.join(unknown)}; "
            f"known: {', '.join(known)}"
        )


def _ref_from_json(item: Any, index: int) -> SkillRef:
    what = f"skills[{index}]"
    if not isinstance(item, Mapping):
        raise SkillMaterialError(f"{what} must be a mapping")
    _refuse_unknown_keys(item, _REF_KEYS, what)
    if "path" not in item:
        raise SkillMaterialError(f"{what} has no `path`")
    kwargs: Dict[str, Any] = {}
    for key in ("level", "declared_resources"):
        if key in item:
            kwargs[key] = item[key]
    if "resources" in item:
        resources = item["resources"]
        if not isinstance(resources, (list, tuple)):
            raise SkillMaterialError(f"{what}.resources must be a list")
        kwargs["resources"] = tuple(resources)
    return SkillRef(path=item["path"], **kwargs)


@dataclass(frozen=True)
class SkillSelection:
    skills: Tuple[SkillRef, ...]
    token_budget: int                 # required, > 0, no default
    format_version: str = "1"         # a registered renderer; "1" forever

    def __post_init__(self) -> None:
        if not isinstance(self.skills, tuple) or not self.skills:
            raise SkillMaterialError(
                "SkillSelection.skills must be a non-empty tuple of SkillRef"
            )
        if any(not isinstance(ref, SkillRef) for ref in self.skills):
            raise SkillMaterialError(
                "SkillSelection.skills must hold SkillRef values only"
            )
        if isinstance(self.token_budget, bool) or not isinstance(
            self.token_budget, int
        ):
            raise SkillMaterialError(
                "SkillSelection.token_budget must be an int"
            )
        if self.token_budget <= 0:
            raise SkillMaterialError(
                "SkillSelection.token_budget must be greater than 0"
            )
        if not isinstance(self.format_version, str):
            raise SkillMaterialError(
                "SkillSelection.format_version must be a str"
            )

    def to_json(self) -> dict:
        return {
            "skills": [ref.to_json() for ref in self.skills],
            "token_budget": self.token_budget,
            "format_version": self.format_version,
        }

    @classmethod
    def from_json(cls, mapping: Any) -> "SkillSelection":
        """Build a selection from its JSON document. Unknown keys are
        refused at both levels; `format_version` and each ref's `level`,
        `declared_resources` and `resources` are optional."""
        if not isinstance(mapping, Mapping):
            raise SkillMaterialError("a selection document must be a mapping")
        _refuse_unknown_keys(mapping, _SELECTION_KEYS, "the selection")
        for key in ("skills", "token_budget"):
            if key not in mapping:
                raise SkillMaterialError(f"the selection has no `{key}`")
        skills = mapping["skills"]
        if not isinstance(skills, (list, tuple)):
            raise SkillMaterialError("the selection's `skills` must be a list")
        refs = tuple(_ref_from_json(item, index) for index, item in enumerate(skills))
        kwargs: Dict[str, Any] = {}
        if "format_version" in mapping:
            kwargs["format_version"] = mapping["format_version"]
        return cls(skills=refs, token_budget=mapping["token_budget"], **kwargs)


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceProvenance:
    path: str             # POSIX path relative to the skill directory
    ref_index: int        # the ref it came from (first when deduplicated)
    declared: bool        # True when the skill's own declaration supplied it
    source: str           # resolved local file path as str (never rendered)
    sha256: str           # of the raw file bytes
    bytes: int
    estimated_tokens: int          # of this resource's rendered element

    def to_json(self) -> dict:
        return {
            "path": self.path,
            "ref_index": self.ref_index,
            "declared": self.declared,
            "source": self.source,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "estimated_tokens": self.estimated_tokens,
        }


@dataclass(frozen=True)
class SkillProvenance:
    name: str
    level: str
    source: str
    sha256: str
    bytes: int
    estimated_tokens: int          # this skill's rendered element
    declared: Tuple[str, ...]      # what the skill declares, as written
    unparsed_yaml_blocks: int
    resources: Tuple[ResourceProvenance, ...]   # what was rendered

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "level": self.level,
            "source": self.source,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "estimated_tokens": self.estimated_tokens,
            "declared": list(self.declared),
            "unparsed_yaml_blocks": self.unparsed_yaml_blocks,
            "resources": [resource.to_json() for resource in self.resources],
        }


@dataclass(frozen=True)
class SuppressedRef:
    ref_index: int
    name: str
    reason: str        # "same-file" | "same-content" | "duplicate-resource"
    kept_index: int
    detail: str = ""

    def to_json(self) -> dict:
        return {
            "ref_index": self.ref_index,
            "name": self.name,
            "reason": self.reason,
            "kept_index": self.kept_index,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class SkillMaterialReport:
    schema: str                  # REPORT_SCHEMA_V1
    format_version: str
    skills: Tuple[SkillProvenance, ...]
    suppressed: Tuple[SuppressedRef, ...]
    estimated_tokens: int        # of the whole rendered block
    token_budget: int
    token_estimate: str          # TOKEN_ESTIMATE
    digest: str                  # sha256 hex of the rendered text (UTF-8)

    def to_json(self) -> dict:
        """The report document: JSON-native values, lists and not tuples."""
        return {
            "schema": self.schema,
            "format_version": self.format_version,
            "skills": [skill.to_json() for skill in self.skills],
            "suppressed": [item.to_json() for item in self.suppressed],
            "estimated_tokens": self.estimated_tokens,
            "token_budget": self.token_budget,
            "token_estimate": self.token_estimate,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class MaterializedSkills:
    text: str
    report: SkillMaterialReport


# --------------------------------------------------------------------------
# Format "1". FROZEN: a later format is a second registry entry.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _RenderSkill:
    name: str
    level: str
    description: str                          # normalized
    body: str                                 # normalized
    resources: Tuple[Tuple[str, str], ...]    # (relative path, normalized content)


@dataclass(frozen=True)
class _Rendered:
    text: str
    skills: Tuple[str, ...]                   # each skill's element
    resources: Tuple[Tuple[str, ...], ...]    # each skill's resource elements


_V1_OPEN = '<skill_context version="1">'
_V1_PREAMBLE = (
    "Skill material selected by the caller for this request. A skill at "
    'level "catalog" is listed by name and description only; its '
    "instructions are not included and cannot be loaded in this request."
)
_V1_CLOSE = "</skill_context>"


def _resource_element_v1(path: str, content: str) -> str:
    return "\n".join([
        f'<resource path="{_escape_attr(path)}">',
        _escape_text(content),
        "</resource>",
    ])


def _render_v1(skills: Sequence[_RenderSkill]) -> _Rendered:
    skill_elements: List[str] = []
    resource_elements: List[Tuple[str, ...]] = []
    for skill in skills:
        lines = [
            f'<skill name="{_escape_attr(skill.name)}" '
            f'level="{_escape_attr(skill.level)}">',
            f"<description>{_escape_text(skill.description)}</description>",
        ]
        own: List[str] = []
        if skill.level == LEVEL_FULL:
            lines.extend(["<instructions>", _escape_text(skill.body), "</instructions>"])
            own = [
                _resource_element_v1(path, content)
                for path, content in skill.resources
            ]
            lines.extend(own)
        lines.append("</skill>")
        skill_elements.append("\n".join(lines))
        resource_elements.append(tuple(own))
    text = "\n".join([_V1_OPEN, _V1_PREAMBLE] + skill_elements + [_V1_CLOSE])
    return _Rendered(
        text=text,
        skills=tuple(skill_elements),
        resources=tuple(resource_elements),
    )


_RENDERERS: Dict[str, Callable[[Sequence[_RenderSkill]], _Rendered]] = {
    "1": _render_v1,
}


# --------------------------------------------------------------------------
# The materializer
# --------------------------------------------------------------------------


class _Files:
    """Each file is read once per `materialize` call."""

    def __init__(self) -> None:
        self._raw: Dict[Path, bytes] = {}

    def size(self, path: Path, what: str) -> int:
        if path in self._raw:
            return len(self._raw[path])
        try:
            return _file_size(path)
        except OSError as exc:
            raise SkillMaterialError(f"{what} could not be read") from exc

    def read(self, path: Path, what: str) -> bytes:
        if path not in self._raw:
            self._raw[path] = _read_file(path, what)
        return self._raw[path]


def _guard_size(size: int, budget: int, what: str,
                level: str = LEVEL_FULL) -> None:
    lower = _size_lower_bound(size)
    if lower > budget:
        remedy = _CATALOG_FIT_REMEDY if level == LEVEL_CATALOG else _FIT_REMEDY
        raise SkillMaterialBudgetExceeded(
            f"{what} is {size} bytes, so it estimates to at least {lower} "
            f"tokens ({TOKEN_ESTIMATE}), over the budget of {budget}. It was "
            f"not read and nothing is returned. {remedy}"
        )


class _Member:
    """One skill directory among the refs that name one skill."""

    def __init__(self, skill_dir: Path, skill_md: Path, index: int) -> None:
        self.skill_dir = skill_dir
        self.skill_md = skill_md
        self.first_index = index
        self.declared_index: Optional[int] = None   # first ref loading declared
        self.named: List[Tuple[str, int]] = []      # (relative path, ref index)

    def absorb(self, ref: SkillRef, index: int) -> None:
        # declared_resources is true if either ref sets it, a catalog ref
        # included; it takes effect only when the merged skill is full.
        if ref.declared_resources and self.declared_index is None:
            self.declared_index = index
        if ref.level != LEVEL_FULL:
            return
        known = {path for path, _ in self.named}
        for path in ref.resources:
            if path not in known:
                known.add(path)
                self.named.append((path, index))


class _Group:
    """The refs that name one skill, at the first ref's position."""

    def __init__(self, document: SkillDocument, index: int) -> None:
        self.document = document
        self.first_index = index
        self.level = LEVEL_CATALOG
        self.members: List[_Member] = []


class _Resource:
    def __init__(self, path: str, declared: bool, real: Path, raw: bytes,
                 content: str, ref_index: int) -> None:
        self.path = path
        self.declared = declared
        self.real = real
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.bytes = len(raw)
        self.content = content
        self.ref_index = ref_index


def _resolve_inside(skill_dir: Path, relative: str, refusal: Callable[[str], SkillMaterialError]) -> Path:
    try:
        real = skill_dir.joinpath(*relative.split("/")).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise refusal("does not exist") from exc
    if not _contained(skill_dir, real):
        raise refusal("resolves outside the skill directory")
    return real


def _expand_declared(skill_dir: Path, written: str, name: str, where: str) -> List[Tuple[str, Path]]:
    """The regular files one declared path supplies, as `(relative, real)`."""

    def refusal(reason: str) -> SkillMaterialError:
        return SkillMaterialError(
            f"{where}: skill {name!r} declares resource {written!r}, "
            f"which {reason}. {_DECLARED_REMEDY}"
        )

    try:
        relative = _check_resource_path(written, declared=True)
    except SkillMaterialError as exc:
        raise refusal(f"is refused ({exc})") from exc
    real = _resolve_inside(skill_dir, relative, refusal)
    if real.is_file():
        return [(relative, real)]
    if not real.is_dir():
        raise refusal("is neither a regular file nor a directory")
    root = skill_dir.joinpath(*relative.split("/"))
    found: List[Tuple[str, Path]] = []
    for directory, _subdirs, filenames in os.walk(root):
        for filename in filenames:
            entry = Path(directory, filename)
            if not entry.is_file():
                continue
            entry_real = entry.resolve(strict=True)
            below = entry.relative_to(root).as_posix()
            entry_relative = f"{relative}/{below}"
            if not _contained(skill_dir, entry_real):
                raise refusal(
                    f"holds {entry_relative!r}, which resolves outside the "
                    "skill directory"
                )
            found.append((entry_relative, entry_real))
    found.sort(key=lambda pair: pair[0])
    return found


def _resolve_named(skill_dir: Path, relative: str, name: str, where: str) -> Path:
    def refusal(reason: str) -> SkillMaterialError:
        return SkillMaterialError(
            f"{where}: resource {relative!r} of skill {name!r} {reason}"
        )

    real = _resolve_inside(skill_dir, relative, refusal)
    if not real.is_file():
        raise refusal("is not a regular file; a named resource is one file")
    return real


def _group_resources(group: _Group, files: _Files, budget: int,
                     suppressed: List[SuppressedRef]) -> List[_Resource]:
    """What a full skill renders: the declared files of each member that
    loads them, in declaration order, then the named additions in the
    caller's order. A relative path supplied again is rendered once."""
    name = group.document.name
    kept: Dict[str, _Resource] = {}
    ordered: List[_Resource] = []

    def add(relative: str, declared: bool, real: Path, index: int) -> None:
        what = f"skills[{index}]: resource {relative!r} of skill {name!r} ({real})"
        prior = kept.get(relative)
        if prior is not None:
            if prior.real != real:
                # A mirrored copy of the skill supplies the same relative
                # path from its own directory: equal bytes or a refusal.
                same = files.size(real, what) == prior.bytes and (
                    hashlib.sha256(files.read(real, what)).hexdigest()
                    == prior.sha256
                )
                if not same:
                    raise SkillMaterialError(
                        f"skills[{index}]: resource {relative!r} of skill "
                        f"{name!r} is ambiguous: {prior.real} and {real} "
                        "differ. Name one copy of the skill."
                    )
            suppressed.append(SuppressedRef(
                ref_index=index,
                name=name,
                reason=_REASON_DUPLICATE_RESOURCE,
                kept_index=prior.ref_index,
                detail=relative,
            ))
            return
        _guard_size(files.size(real, what), budget, what)
        raw = files.read(real, what)
        resource = _Resource(
            relative, declared, real, raw, _normalize(_decode(raw, what)), index
        )
        kept[relative] = resource
        ordered.append(resource)

    for member in group.members:
        if member.declared_index is None:
            continue
        where = f"skills[{member.declared_index}]"
        for written in group.document.declared:
            for relative, real in _expand_declared(member.skill_dir, written, name, where):
                add(relative, True, real, member.declared_index)
    for member in group.members:
        for relative, index in member.named:
            real = _resolve_named(member.skill_dir, relative, name, f"skills[{index}]")
            add(relative, False, real, index)
    return ordered


def _over_budget(skills: Sequence[SkillProvenance], total: int, budget: int) -> SkillMaterialBudgetExceeded:
    lines = [
        f"skill material is over budget: estimated {total} tokens "
        f"({TOKEN_ESTIMATE}), budget {budget}."
    ]
    for skill in skills:
        lines.append(
            f"  skill {skill.name!r} ({skill.level}): {skill.estimated_tokens}"
        )
        for resource in skill.resources:
            lines.append(
                f"    resource {resource.path!r}: {resource.estimated_tokens}"
            )
    lines.append(
        f"{_FIT_REMEDY} Nothing was truncated and nothing is returned."
    )
    return SkillMaterialBudgetExceeded("\n".join(lines))


def materialize(selection: SkillSelection, *,
                base_dir: Optional[Path] = None) -> MaterializedSkills:
    """Read every selected skill once and return the rendered block with its
    report, or raise. Nothing partial is ever returned.

    Raises `SkillMaterialError` (or a subclass) when the selection, a skill
    or a resource is refused, `SkillMaterialBudgetExceeded` when the block
    does not fit `token_budget`, and `PyYamlUnavailableError` when PyYAML
    cannot be imported.
    """
    if not isinstance(selection, SkillSelection):
        raise SkillMaterialError(
            "materialize takes a SkillSelection, got "
            f"{type(selection).__name__}"
        )
    renderer = _RENDERERS.get(selection.format_version)
    if renderer is None:
        raise SkillMaterialError(
            f"format_version {selection.format_version!r} is not registered; "
            f"registered: {', '.join(sorted(_RENDERERS))}"
        )
    _yaml()
    base = _base_dir(base_dir)
    budget = selection.token_budget
    files = _Files()
    documents: Dict[Path, SkillDocument] = {}
    groups: List[_Group] = []
    by_name: Dict[str, _Group] = {}
    suppressed: List[SuppressedRef] = []

    for index, ref in enumerate(selection.skills):
        where = f"skills[{index}]"
        skill_dir, skill_md = _locate(ref.path, base, where)
        what = f"{where}: {skill_md}"
        # Every selected SKILL.md is guarded before it is read, at either level.
        _guard_size(files.size(skill_md, what), budget, what, ref.level)
        if skill_md not in documents:
            documents[skill_md] = _document(files.read(skill_md, what), skill_md, where)
        document = documents[skill_md]

        group = by_name.get(document.name)
        if group is None:
            group = _Group(document, index)
            by_name[document.name] = group
            groups.append(group)
            member = _Member(skill_dir, skill_md, index)
            group.members.append(member)
        else:
            member = next(
                (
                    known for known in group.members
                    if known.skill_md == skill_md and known.skill_dir == skill_dir
                ),
                None,
            )
            if member is not None:
                suppressed.append(SuppressedRef(
                    ref_index=index,
                    name=document.name,
                    reason=_REASON_SAME_FILE,
                    kept_index=member.first_index,
                ))
            elif document.sha256 == group.document.sha256:
                member = _Member(skill_dir, skill_md, index)
                group.members.append(member)
                suppressed.append(SuppressedRef(
                    ref_index=index,
                    name=document.name,
                    reason=_REASON_SAME_CONTENT,
                    kept_index=group.first_index,
                ))
            else:
                raise SkillMaterialError(
                    f"{where}: skill name {document.name!r} is defined with "
                    f"different content by {group.document.source} "
                    f"(skills[{group.first_index}]) and {document.source}. "
                    "Name one of them."
                )
        if ref.level == LEVEL_FULL:
            group.level = LEVEL_FULL
        member.absorb(ref, index)

    resources: List[List[_Resource]] = []
    for group in groups:
        if group.level == LEVEL_FULL:
            resources.append(_group_resources(group, files, budget, suppressed))
        else:
            resources.append([])

    rendered = renderer([
        _RenderSkill(
            name=group.document.name,
            level=group.level,
            description=_normalize(group.document.description),
            body=group.document.body,
            resources=tuple((item.path, item.content) for item in own),
        )
        for group, own in zip(groups, resources)
    ])

    provenance = tuple(
        SkillProvenance(
            name=group.document.name,
            level=group.level,
            source=group.document.source,
            sha256=group.document.sha256,
            bytes=group.document.bytes,
            estimated_tokens=estimate_tokens(element),
            declared=group.document.declared,
            unparsed_yaml_blocks=group.document.unparsed_yaml_blocks,
            resources=tuple(
                ResourceProvenance(
                    path=item.path,
                    ref_index=item.ref_index,
                    declared=item.declared,
                    source=str(item.real),
                    sha256=item.sha256,
                    bytes=item.bytes,
                    estimated_tokens=estimate_tokens(resource_element),
                )
                for item, resource_element in zip(own, resource_elements)
            ),
        )
        for group, own, element, resource_elements in zip(
            groups, resources, rendered.skills, rendered.resources
        )
    )

    total = estimate_tokens(rendered.text)
    if total > budget:
        raise _over_budget(provenance, total, budget)

    report = SkillMaterialReport(
        schema=REPORT_SCHEMA_V1,
        format_version=selection.format_version,
        skills=provenance,
        suppressed=tuple(suppressed),
        estimated_tokens=total,
        token_budget=budget,
        token_estimate=TOKEN_ESTIMATE,
        digest=hashlib.sha256(rendered.text.encode("utf-8")).hexdigest(),
    )
    return MaterializedSkills(text=rendered.text, report=report)
