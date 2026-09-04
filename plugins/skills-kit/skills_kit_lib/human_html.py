"""The stable consumer interface for the human-html contract (DR-3).

Generators, the md-domain lane scripts, and the host viewer import this module
and nothing deeper. It re-exports the record schema and path mapping owned by
`skills_kit_lib.schemas.human_html` (DR-1, DR-4) and adds the surfaces the
contract defines around it:

- `source_stamp` -- the DR-2 subtree source stamp and dirty state.
- `asset_css` -- the SA-1 packaged dark style asset.
- `marker` / `parse_marker` -- the PC-1 and RD-2 generated-page marker.
- `announce_script` -- the PC-3 viewer-agnostic announce snippet.
- `navigation_targets` -- the PC-2 nearest-page ancestor and descendants.

Standard library only. CK-1 and CK-2 require their scripts to run on a plain
interpreter with `skills_kit_lib` importable and nothing else installed, so
this module must never grow a third-party import -- pyyaml included. Records
are JSON-syntax YAML 1.2 (DR-1), which the stdlib `json` module reads.
"""

from __future__ import annotations

import json
import re
import subprocess
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Mapping

from .schemas.human_html import (
    DECISION_NONE,
    DECISION_PAGE,
    DECISIONS,
    PAGE_FILENAME,
    RECORD_FILENAME,
    RECORD_FIELDS,
    RECORD_ROOT,
    REFERENCE_PREFIX,
    REFERENCE_SUFFIX,
    ROOT_DIRECTORY,
    SCHEMA_VERSION,
    SHA_RE,
    SLUG_RE,
    HumanHtmlError,
    Record,
    Reference,
    RecordValidationError,
    dumps_record,
    load_record,
    normalize_directory,
    read_instructions,
    record_path,
    reference_filename,
    validate_record,
    write_record,
)


__all__ = [
    "ANNOUNCE_TYPE",
    "ANNOUNCE_VERSION",
    "ASSET_PACKAGE",
    "CSS_ASSET_NAME",
    "DECISION_NONE",
    "DECISION_PAGE",
    "DECISIONS",
    "GENERATED_BY",
    "KIND_PAGE",
    "KIND_REFERENCE",
    "KINDS",
    "MARKER_MAX_LINE",
    "MARKER_PREFIX",
    "PAGE_FILENAME",
    "RECORD_FIELDS",
    "RECORD_FILENAME",
    "RECORD_ROOT",
    "REFERENCE_PREFIX",
    "REFERENCE_SUFFIX",
    "ROOT_DIRECTORY",
    "SCHEMA_VERSION",
    "SHA_RE",
    "SLUG_RE",
    "SOURCE_EXCLUDE_GLOBS",
    "HumanHtmlError",
    "MarkerError",
    "Record",
    "Reference",
    "RecordValidationError",
    "SourceStampError",
    "announce_script",
    "asset_css",
    "dumps_record",
    "load_record",
    "marker",
    "navigation_targets",
    "normalize_directory",
    "parse_marker",
    "read_instructions",
    "record_path",
    "reference_filename",
    "source_stamp",
    "validate_record",
    "write_record",
]


# PC-1 / RD-2 marker.
GENERATED_BY = "md-domain"
KIND_PAGE = "page"
KIND_REFERENCE = "reference"
KINDS = (KIND_PAGE, KIND_REFERENCE)
MARKER_PREFIX = "human-html:"
MARKER_MAX_LINE = 20
_MARKER_RE = re.compile(r"<!--\s*human-html:\s*(?P<json>\{.*?\})\s*-->", re.DOTALL)

# PC-3 announce message.
ANNOUNCE_TYPE = "human-html:announce"
ANNOUNCE_VERSION = 1

# SA-1 packaged asset.
ASSET_PACKAGE = "skills_kit_lib.assets"
CSS_ASSET_NAME = "human-html.css"

# DR-2 input exclusions, as basename/segment globs relative to the subtree.
SOURCE_EXCLUDE_GLOBS = (".databench", PAGE_FILENAME, "human.*.html")


class MarkerError(HumanHtmlError):
    """A generated page's PC-1 marker is missing, duplicated, or malformed."""


class SourceStampError(HumanHtmlError):
    """The DR-2 source stamp could not be computed for a directory."""


# --------------------------------------------------------------------------
# SA-1 style asset
# --------------------------------------------------------------------------

def asset_css() -> str:
    """Return the SA-1 packaged style asset as text.

    Read through `importlib.resources` so an installed wheel and a source
    checkout both resolve, and so the generator (PC-4) and the host viewer
    (HV-3) provably read the same bytes.
    """
    return resources.files(ASSET_PACKAGE).joinpath(CSS_ASSET_NAME).read_text(encoding="ascii")


# --------------------------------------------------------------------------
# DR-2 subtree source stamp
# --------------------------------------------------------------------------

_GLOB_META_RE = re.compile(r"([*?\[\]\\])")


def _escape_glob(text: str) -> str:
    """Backslash-escape the wildmatch metacharacters in a literal path.

    A directory NAME may itself contain `[`, `]`, `*`, `?` or a backslash --
    bracketed routing folders such as `app/[slug]` are ordinary in several web
    frameworks. Interpolated raw into a `glob` pathspec, `[slug]` reads as a
    character class, matches no real path, and every DR-2 exclusion below it
    silently misses.
    """
    return _GLOB_META_RE.sub(r"\\\1", text)


def _subtree_pathspecs(directory: str) -> list[str]:
    """Build the DR-2 pathspec list limiting git to a directory's analysis inputs.

    One positive pathspec for the subtree, then one negative `:(exclude,glob)`
    pathspec per excluded form at every depth. Git's glob magic makes an
    embedded `/**/` match zero or more directories, so a single spec per form
    covers the directory itself and every descendant.

    The positive spec takes `:(literal)` magic and the negative specs take an
    escaped base, because git treats an unqualified pathspec as a pattern: both
    forms must match a directory whose own name contains a metacharacter.
    """
    if directory == ROOT_DIRECTORY:
        base = ""
        specs = ["."]
    else:
        base = _escape_glob(directory) + "/"
        specs = [":(literal)" + directory]
    specs.append(":(exclude,glob)%s**/.databench/**" % base)
    specs.append(":(exclude,glob)%s**/%s" % (base, PAGE_FILENAME))
    specs.append(":(exclude,glob)%s**/human.*.html" % base)
    return specs


def _git(repo_root: str | Path, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SourceStampError(
            "git %s failed in %s: %s" % (" ".join(args), repo_root, proc.stderr.strip())
        )
    return proc.stdout


def source_stamp(repo_root: str | Path, directory: str | Path) -> tuple[str, bool]:
    """Return the DR-2 `(source_sha, dirty)` pair for one directory subtree.

    `source_sha` is the newest commit at or before `HEAD` that changed a
    tracked analysis input inside the subtree. `dirty` is true when any
    analysis input in the subtree has uncommitted or untracked content, which
    means no commit identifies the judged content (DR-2 reports `INFO DIRTY`
    for that state and does not block).

    Analysis inputs exclude `.databench/`, `human.html`, and
    `human.<slug>.html` at every depth, so regenerating a page never restamps
    the record that describes it.

    Exact commands, run with `cwd` set to `repo_root` and with PATHSPECS being
    the subtree pathspec plus one `:(exclude,glob)` spec per excluded form:

        git log -1 --format=%H HEAD -- PATHSPECS
        git status --porcelain --untracked-files=all --no-renames -z -- PATHSPECS

    An empty `git log` result means no tracked analysis input in the subtree
    has ever been committed; `SourceStampError` is raised rather than
    inventing a sha.
    """
    normalized = normalize_directory(directory)
    pathspecs = _subtree_pathspecs(normalized)

    log_out = _git(repo_root, ["log", "-1", "--format=%H", "HEAD", "--", *pathspecs]).strip()
    if not SHA_RE.match(log_out):
        raise SourceStampError(
            "no committed analysis input under %r in %s" % (normalized, repo_root)
        )

    status_out = _git(
        repo_root,
        ["status", "--porcelain", "--untracked-files=all", "--no-renames", "-z", "--", *pathspecs],
    )
    dirty = any(entry for entry in status_out.split("\0") if entry.strip())
    return log_out, dirty


# --------------------------------------------------------------------------
# PC-1 / RD-2 marker
# --------------------------------------------------------------------------

def marker(
    record: Record | Mapping[str, Any],
    kind: str,
    reference: str | None = None,
) -> str:
    """Build the PC-1 generated-page marker comment for `record`.

    `kind` is `page` for `human.html` and `reference` for a `human.<slug>.html`
    sibling; a reference marker also carries `"reference":"<slug>"` (RD-2). The
    field order is the one the standard prints, and the JSON is compact so the
    marker is one line inside the first 20 of the file.
    """
    data = record.to_dict() if isinstance(record, Record) else validate_record(record)
    if kind not in KINDS:
        raise MarkerError("kind: must be one of %s, got %r" % (", ".join(KINDS), kind))
    payload: dict[str, Any] = {
        "generated_by": GENERATED_BY,
        "source_sha": data["source_sha"],
        "directory": data["directory"],
        "kind": kind,
    }
    if kind == KIND_REFERENCE:
        if not isinstance(reference, str) or not SLUG_RE.match(reference):
            raise MarkerError(
                "reference: a reference marker needs a slug matching "
                "[a-z0-9]+(-[a-z0-9]+)*, got %r" % (reference,)
            )
        payload["reference"] = reference
    elif reference is not None:
        raise MarkerError("reference: only a reference marker carries a slug, got %r" % (reference,))
    return "<!-- human-html: %s -->" % json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


def parse_marker(html_text: str) -> dict[str, Any]:
    """Parse the PC-1 marker out of a generated page and return its fields.

    Raises `MarkerError` when the marker is missing, appears more than once,
    sits below line 20 (MARKER_MAX_LINE), is not valid JSON, or carries a
    malformed field --
    each of which CK-1 reports as a `FAIL`.
    """
    matches = list(_MARKER_RE.finditer(html_text))
    if not matches:
        raise MarkerError("marker: no `<!-- human-html: {...} -->` comment found")
    if len(matches) > 1:
        raise MarkerError("marker: %d markers found, exactly one is allowed" % len(matches))
    match = matches[0]
    line = html_text.count("\n", 0, match.start()) + 1
    if line > MARKER_MAX_LINE:
        raise MarkerError(
            "marker: must appear within the first %d lines, found on line %d"
            % (MARKER_MAX_LINE, line)
        )
    try:
        payload = json.loads(match.group("json"))
    except ValueError as exc:
        raise MarkerError("marker: payload is not valid JSON (%s)" % exc) from exc
    if not isinstance(payload, dict):
        raise MarkerError("marker: payload must be a JSON object")

    for name in ("generated_by", "source_sha", "directory", "kind"):
        if name not in payload:
            raise MarkerError("marker.%s: is required" % name)
    if payload["generated_by"] != GENERATED_BY:
        raise MarkerError(
            "marker.generated_by: must be %r, got %r" % (GENERATED_BY, payload["generated_by"])
        )
    if not isinstance(payload["source_sha"], str) or not SHA_RE.match(payload["source_sha"]):
        raise MarkerError(
            "marker.source_sha: must be a full lowercase 40-hex commit id, got %r"
            % (payload["source_sha"],)
        )
    if payload["directory"] != normalize_directory(payload["directory"]):
        raise MarkerError("marker.directory: must be normalized, got %r" % (payload["directory"],))
    if payload["kind"] not in KINDS:
        raise MarkerError("marker.kind: must be one of %s, got %r" % (", ".join(KINDS), payload["kind"]))
    if payload["kind"] == KIND_REFERENCE:
        slug = payload.get("reference")
        if not isinstance(slug, str) or not SLUG_RE.match(slug):
            raise MarkerError("marker.reference: a reference marker needs a valid slug, got %r" % (slug,))
    elif "reference" in payload:
        raise MarkerError("marker.reference: only a reference marker carries a slug")
    return payload


# --------------------------------------------------------------------------
# PC-3 announce message
# --------------------------------------------------------------------------

def announce_script(
    record: Record | Mapping[str, Any],
    file: str,
    kind: str,
    reference: str | None = None,
) -> str:
    """Build the PC-3 announce snippet for one generated file.

    `file` is the generated file's basename, `kind` matches its marker, and a
    reference file adds `reference: "<slug>"` (RD-2). The snippet posts once
    after parsing and only when the page has a parent frame, so a standalone
    page stays inert. Return the JavaScript body without a `<script>` wrapper
    -- the generator owns page structure (PC-5).
    """
    data = record.to_dict() if isinstance(record, Record) else validate_record(record)
    if kind not in KINDS:
        raise MarkerError("kind: must be one of %s, got %r" % (", ".join(KINDS), kind))
    if "/" in file or "\\" in file or not file:
        raise MarkerError("file: must be a bare relative basename, got %r" % (file,))
    fields = [
        ("type", ANNOUNCE_TYPE),
        ("version", ANNOUNCE_VERSION),
        ("directory", data["directory"]),
        ("file", file),
        ("kind", kind),
    ]
    if kind == KIND_REFERENCE:
        if not isinstance(reference, str) or not SLUG_RE.match(reference):
            raise MarkerError("reference: a reference announce needs a valid slug, got %r" % (reference,))
        fields.append(("reference", reference))
    elif reference is not None:
        raise MarkerError("reference: only a reference announce carries a slug, got %r" % (reference,))
    fields.append(("source_sha", data["source_sha"]))

    lines = [
        "    %s: %s%s"
        % (name, json.dumps(value, ensure_ascii=True), "," if index < len(fields) - 1 else "")
        for index, (name, value) in enumerate(fields)
    ]
    return "\n".join(
        [
            "if (window.parent !== window) {",
            "  window.parent.postMessage({",
            *lines,
            '  }, "*");',
            "}",
        ]
    )


# --------------------------------------------------------------------------
# PC-2 navigation spine
# --------------------------------------------------------------------------

def _decision_of(record: Any) -> str:
    if isinstance(record, Record):
        return record.decision
    if isinstance(record, Mapping):
        return record.get("decision", DECISION_NONE)
    raise HumanHtmlError("records: values must be Record or mapping, got %s" % type(record).__name__)


def _ancestors(directory: str) -> Iterable[str]:
    """Yield `directory`'s ancestors nearest first, ending at the repository root."""
    if directory == ROOT_DIRECTORY:
        return
    parts = directory.split("/")
    for depth in range(len(parts) - 1, 0, -1):
        yield "/".join(parts[:depth])
    yield ROOT_DIRECTORY


def _is_descendant(candidate: str, ancestor: str) -> bool:
    if candidate == ancestor:
        return False
    if ancestor == ROOT_DIRECTORY:
        return True
    return candidate.startswith(ancestor + "/")


def navigation_targets(
    records: Mapping[str, Any],
    directory: str | Path,
) -> tuple[str | None, list[str]]:
    """Return the PC-2 `(up, down)` navigation targets for `directory`.

    `records` maps a normalized directory path to that directory's `Record`
    (or an equivalent mapping). `up` is the nearest ancestor whose decision is
    `page`, or `None` at the repository root or when no ancestor is a page.
    `down` lists every nearest descendant whose decision is `page`: traversal
    passes through `none` directories and stops a branch at its first page, so
    a page below another page is not a target. `down` is sorted, and is empty
    when no descendant page exists -- the caller omits the section then.
    """
    normalized = normalize_directory(directory)
    known = {normalize_directory(key): value for key, value in records.items()}

    up: str | None = None
    for ancestor in _ancestors(normalized):
        record = known.get(ancestor)
        if record is not None and _decision_of(record) == DECISION_PAGE:
            up = ancestor
            break

    pages = sorted(
        candidate
        for candidate, record in known.items()
        if _is_descendant(candidate, normalized) and _decision_of(record) == DECISION_PAGE
    )
    down = [
        candidate
        for candidate in pages
        if not any(_is_descendant(candidate, page) for page in pages)
    ]
    return up, down
