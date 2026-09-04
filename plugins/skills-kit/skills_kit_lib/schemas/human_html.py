"""human-html decision-record schema and path mapping (DR-1, DR-3, DR-4).

Owner of the record contract defined by `human-html-standards.md` rules DR-1
(one record per directory), DR-3 (schema ownership) and DR-4 (instructions
survive regeneration). Every producer and consumer -- the md-domain generation
lane, the lane scripts, and the host viewer -- reads records through this
module rather than parsing them.

Deliberately NOT registered with `schema_registry`. That registry describes
fenced typed-unit YAML blocks embedded in authored Markdown; a human-html
decision record is a standalone machine-written file with a fixed field list,
so it carries its own validator instead.

Stdlib only: CK-1 requires `skills_kit_lib.human_html` -- which re-exports this
module -- to stay importable from a plain interpreter with no pyyaml present.
Records are JSON-compatible YAML 1.2 written in JSON syntax (DR-1), so the
stdlib `json` module both reads and writes them.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


SCHEMA_VERSION = 1

DECISION_PAGE = "page"
DECISION_NONE = "none"
DECISIONS = (DECISION_PAGE, DECISION_NONE)

ROOT_DIRECTORY = "."

# DR-1 path mapping: `.databench/human/<relative-directory>/decision.yaml`,
# and `.databench/human/decision.yaml` for the repository root.
RECORD_ROOT = ".databench/human"
RECORD_FILENAME = "decision.yaml"

# PC-1 / RD-1 generated filenames.
PAGE_FILENAME = "human.html"
REFERENCE_PREFIX = "human."
REFERENCE_SUFFIX = ".html"

# RD-1 slug grammar.
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# DR-2 source stamp: a full lowercase 40-hex commit id.
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

RECORD_FIELDS = (
    "schema_version",
    "directory",
    "decision",
    "source_sha",
    "dirty",
    "identity",
    "instructions",
    "references",
)


class HumanHtmlError(Exception):
    """Base class for every error this contract raises."""


class RecordValidationError(HumanHtmlError):
    """A decision record violates DR-1. The message names the offending field."""


@dataclass(frozen=True)
class Reference:
    """One RD-1 sibling reference document listed by a record."""

    slug: str
    title: str
    file: str


@dataclass(frozen=True)
class Record:
    """One DR-1 decision record for one repository directory."""

    directory: str
    decision: str
    source_sha: str
    dirty: bool = False
    identity: str = ""
    instructions: str = ""
    references: tuple[Reference, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Return the record as a plain dict in DR-1 field order."""
        return {
            "schema_version": self.schema_version,
            "directory": self.directory,
            "decision": self.decision,
            "source_sha": self.source_sha,
            "dirty": self.dirty,
            "identity": self.identity,
            "instructions": self.instructions,
            "references": [asdict(ref) for ref in self.references],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Record":
        """Validate `data` under DR-1 and build a Record from it."""
        validated = validate_record(data)
        return cls(
            directory=validated["directory"],
            decision=validated["decision"],
            source_sha=validated["source_sha"],
            dirty=validated["dirty"],
            identity=validated["identity"],
            instructions=validated["instructions"],
            references=tuple(
                Reference(slug=ref["slug"], title=ref["title"], file=ref["file"])
                for ref in validated["references"]
            ),
            schema_version=validated["schema_version"],
        )

    def with_instructions(self, instructions: str) -> "Record":
        """Return a copy carrying `instructions` (the DR-4 human-managed field)."""
        return replace(self, instructions=instructions)


def normalize_directory(directory: str | Path) -> str:
    """Normalize a repository-relative directory to its DR-1 record value.

    POSIX separators, no leading `./`, no trailing slash, and `.` for the
    repository root (an empty string and `.` both normalize to `.`).
    """
    text = str(directory).replace("\\", "/").strip()
    if text in ("", ".", "./"):
        return ROOT_DIRECTORY
    parts = [part for part in PurePosixPath(text).parts if part not in ("", ".")]
    if not parts:
        return ROOT_DIRECTORY
    if parts[0] == "/" or text.startswith("/"):
        raise RecordValidationError(
            "directory: must be repository-relative, got an absolute path %r" % text
        )
    if ".." in parts:
        raise RecordValidationError(
            "directory: must not traverse above the repository root, got %r" % text
        )
    return "/".join(parts)


def record_path(repo_root: str | Path, directory: str | Path) -> Path:
    """Return the DR-1 record path for `directory` inside `repo_root`.

    The repository root maps to `.databench/human/decision.yaml`; every other
    directory maps to `.databench/human/<relative-directory>/decision.yaml`.
    """
    normalized = normalize_directory(directory)
    base = Path(repo_root) / Path(RECORD_ROOT)
    if normalized == ROOT_DIRECTORY:
        return base / RECORD_FILENAME
    return base.joinpath(*normalized.split("/")) / RECORD_FILENAME


def reference_filename(slug: str) -> str:
    """Return the RD-1 sibling filename `human.<slug>.html` for `slug`."""
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise RecordValidationError(
            "references[].slug: must match [a-z0-9]+(-[a-z0-9]+)*, got %r" % (slug,)
        )
    return REFERENCE_PREFIX + slug + REFERENCE_SUFFIX


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RecordValidationError(message)


def _validate_references(raw: Any, decision: str) -> list[dict[str, str]]:
    _require(isinstance(raw, list), "references: must be an array, got %s" % type(raw).__name__)
    if decision == DECISION_NONE:
        _require(not raw, "references: must be empty when decision is 'none'")
    out: list[dict[str, str]] = []
    seen_slugs: set[str] = set()
    seen_files: set[str] = set()
    for index, item in enumerate(raw):
        _require(
            isinstance(item, Mapping),
            "references[%d]: must be a mapping, got %s" % (index, type(item).__name__),
        )
        for key in ("slug", "title", "file"):
            _require(key in item, "references[%d].%s: is required" % (index, key))
            _require(
                isinstance(item[key], str) and item[key].strip() != "",
                "references[%d].%s: must be a nonempty string" % (index, key),
            )
        slug = item["slug"]
        _require(
            bool(SLUG_RE.match(slug)),
            "references[%d].slug: must match [a-z0-9]+(-[a-z0-9]+)*, got %r" % (index, slug),
        )
        _require(slug not in seen_slugs, "references[%d].slug: duplicate slug %r" % (index, slug))
        expected = reference_filename(slug)
        _require(
            item["file"] == expected,
            "references[%d].file: must be %r for slug %r, got %r"
            % (index, expected, slug, item["file"]),
        )
        _require(
            item["file"] not in seen_files,
            "references[%d].file: duplicate file %r" % (index, item["file"]),
        )
        seen_slugs.add(slug)
        seen_files.add(item["file"])
        out.append({"slug": slug, "title": item["title"], "file": item["file"]})
    return out


def validate_record(data: Any) -> dict[str, Any]:
    """Validate one decision record against DR-1.

    Returns the normalized record as a plain dict in DR-1 field order. Raises
    `RecordValidationError` whose message names the offending field.
    """
    _require(
        isinstance(data, Mapping),
        "record: must be a mapping, got %s" % type(data).__name__,
    )

    unknown = sorted(set(data) - set(RECORD_FIELDS))
    _require(not unknown, "record: unknown field(s) %s" % ", ".join(unknown))
    for name in RECORD_FIELDS:
        _require(name in data, "%s: is required" % name)

    version = data["schema_version"]
    _require(
        isinstance(version, int) and not isinstance(version, bool) and version == SCHEMA_VERSION,
        "schema_version: must be the integer %d, got %r" % (SCHEMA_VERSION, version),
    )

    raw_directory = data["directory"]
    _require(
        isinstance(raw_directory, str),
        "directory: must be a string, got %s" % type(raw_directory).__name__,
    )
    directory = normalize_directory(raw_directory)
    _require(
        directory == raw_directory,
        "directory: must be normalized to %r, got %r" % (directory, raw_directory),
    )

    decision = data["decision"]
    _require(
        decision in DECISIONS,
        "decision: must be one of %s, got %r" % (", ".join(DECISIONS), decision),
    )

    source_sha = data["source_sha"]
    _require(
        isinstance(source_sha, str) and bool(SHA_RE.match(source_sha)),
        "source_sha: must be a full lowercase 40-hex commit id, got %r" % (source_sha,),
    )

    dirty = data["dirty"]
    _require(isinstance(dirty, bool), "dirty: must be a boolean, got %r" % (dirty,))

    identity = data["identity"]
    _require(
        isinstance(identity, str),
        "identity: must be a string, got %s" % type(identity).__name__,
    )
    if decision == DECISION_PAGE:
        _require(identity.strip() != "", "identity: must be one nonempty line when decision is 'page'")
    _require("\n" not in identity, "identity: must be one line, got an embedded newline")

    instructions = data["instructions"]
    _require(
        isinstance(instructions, str),
        "instructions: must be a string, got %s" % type(instructions).__name__,
    )

    references = _validate_references(data["references"], decision)

    return {
        "schema_version": version,
        "directory": directory,
        "decision": decision,
        "source_sha": source_sha,
        "dirty": dirty,
        "identity": identity,
        "instructions": instructions,
        "references": references,
    }


def read_instructions(path: str | Path) -> str:
    """Return the `instructions` field of the record at `path`, or `""`.

    DR-4 input step: a regeneration reads the existing human-managed value
    before it rewrites every other field. A missing or unreadable file yields
    the empty string, because a record that does not exist steers nothing.
    """
    file_path = Path(path)
    if not file_path.is_file():
        return ""
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if isinstance(data, Mapping) and isinstance(data.get("instructions"), str):
        return data["instructions"]
    return ""


def load_record(path: str | Path) -> Record:
    """Load and validate the decision record stored at `path`."""
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RecordValidationError("record: cannot read %s (%s)" % (file_path, exc)) from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RecordValidationError(
            "record: %s is not JSON-syntax YAML 1.2 (%s)" % (file_path, exc)
        ) from exc
    return Record.from_dict(data)


def dumps_record(record: Record | Mapping[str, Any]) -> str:
    """Serialize a record to the DR-1 on-disk form: JSON syntax, ASCII, trailing newline."""
    data = record.to_dict() if isinstance(record, Record) else record
    validated = validate_record(data)
    return json.dumps(validated, indent=2, ensure_ascii=True, sort_keys=False) + "\n"


def write_record(
    path: str | Path,
    record: Record | Mapping[str, Any],
    preserve_instructions: bool = True,
) -> Record:
    """Write `record` to `path` in JSON syntax and return what was written.

    DR-4: the generator rewrites every field except `instructions`. With the
    default `preserve_instructions=True` the existing file's `instructions`
    value is read first and written back byte-identical, so a regeneration
    never clobbers the one human-managed field. Pass
    `preserve_instructions=False` to override it deliberately -- that is the
    only path by which a generator may change it.
    """
    file_path = Path(path)
    data = dict(record.to_dict() if isinstance(record, Record) else record)
    if preserve_instructions:
        data["instructions"] = read_instructions(file_path)
    validated = validate_record(data)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(dumps_record(validated), encoding="ascii", newline="\n")
    return Record.from_dict(validated)
