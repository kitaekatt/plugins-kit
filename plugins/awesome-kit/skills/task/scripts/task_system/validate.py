"""The ``validate`` verb (spec section 9).

Checks a task against its Task Type schema and the structural rules,
classifies it, and emits findings. Errors make the task ``invalid``; warnings
do not, but both gate ``work`` (later step). A third, advisory tier --
``notes`` -- carries the early document-size signals (approaching-budget,
dominant-section, session-diary; constants below): notes are not findings,
never gate anything, and leave the exit code untouched.

Classification outcomes:
- ``remote``   -- tmp path + non-matching host on the ref. SHORT-CIRCUITS:
                  the folder/task.yaml is never read or validated locally.
- ``invalid``  -- any error.
- ``archived`` / ``orphaned`` -- the absent-folder tri-state (spec section 4):
  non-tmp path + no folder reads as ``archived`` (expected end state, git is
  the record, no findings); a tmp path whose folder is parked at
  ``tmp/archived-tasks/<stub>`` (archive's tmp closure policy) also reads as
  ``archived``; tmp path (local host or no host) + no folder otherwise is
  ``orphaned`` with the "orphaned tmp reference" warning.
- otherwise the stored status from task.yaml (``active`` / ``blocked`` /
  ``closed`` / ``archived``), except a non-empty ``blocked_by`` reads as
  ``blocked`` regardless of the stored status.

Minimal readings chosen in Step 1 (flagged in the implementation report):
- Uncommitted dev/tasks detection runs ``git status --porcelain`` scoped to
  the folder. A dev/tasks folder NOT inside any git repo (or where git itself
  fails / is unavailable) also counts as uncommitted -- there is no git record
  of it, which is exactly the unsaved-durable-work condition the warning
  exists for.
- Dangling ``depends_on`` / ``blocked_by``: an entry is dangling when it is
  not a path string, is malformed / outside the known roots, or resolves to
  ``orphaned`` (tmp path, local, no folder). A non-tmp absent path reads as
  ``archived`` per the tri-state and is NOT dangling. Entries are reference
  PATHS (spec 2.2: list[path]) -- no stub search, no host field, treated as
  local; only folder existence is checked (the dep's own task.yaml is not
  recursively validated).
- Warnings are computed even when errors are present (guarded against
  unusable YAML); classification is ``invalid`` whenever any error exists.
- A ref host is only expressible through the API (``ref_host``); the Step 1
  CLI surface has no way to pass one (refs embedded in documents carry it;
  discovery wires it in a later step).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from skills_kit_lib import schema_engine

from . import resolve
from .task_items import read_task_items, stale_priority_refs
from .types import DEFAULT_TYPE_NAME, TaskType, get_type

# --- document-size budgets (handoff-template.md, enforced here) --------------
# Role-based line budgets, two tiers. NOTE at the healthy target (advisory:
# not a finding, exit code unaffected -- "rotate now while it is cheap");
# WARNING at the ceiling (a real finding: gates ``work`` like any other
# warning). Measured in lines so an agent can self-check with ``wc -l``.
# EXEMPT: log.md and split logs (log-*.md) -- the log is the append-only
# history sink that rotation TARGETS; a big log is the system working, and it
# is only loaded on demand.
# Other markdown docs (on-demand reference docs) get a ceiling only, and a
# loose one: decomposition needs somewhere cheap to put content -- a tight
# gate on reference docs would just chase displaced content around.
CLAUDE_MD_TARGET = 250
CLAUDE_MD_CEILING = 400
PLAN_MD_TARGET = 300
PLAN_MD_CEILING = 400
OTHER_DOC_CEILING = 800
_DOC_BUDGETS: dict[str, tuple[int | None, int]] = {
    "CLAUDE.md": (CLAUDE_MD_TARGET, CLAUDE_MD_CEILING),
    "plan.md": (PLAN_MD_TARGET, PLAN_MD_CEILING),
}
# A single ``##`` section dominating CLAUDE.md/plan.md is the accretion
# pattern ("Where we are today" / "Accomplished" growing without rotation) --
# note (advisory) when one section exceeds half the document, once the doc is
# past the floor (below it, halves are noise). Catches the metastasis long
# before the file ceiling.
SECTION_NOTE_MIN_DOC_LINES = 150
_SECTION_CHECKED_DOCS = ("CLAUDE.md", "plan.md")
# Diary detector: dated session-narrative markers in CLAUDE.md (paragraphs
# opening with a bold date, e.g. ``**2026-07-18...``). More than the cap ->
# advisory note naming the session-diary anti-pattern (history -> log.md).
DIARY_MARKER_MAX = 3
_DIARY_RE = re.compile(r"^\s*\*\*\s?20\d{2}-\d{2}-\d{2}")
_LOG_EXEMPT_RE = re.compile(r"^log(-[A-Za-z0-9._-]+)?\.md$")


@dataclass
class ValidationResult:
    ref: str
    classification: str
    canonical: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Advisory only -- NOT findings. Notes never gate work and never affect
    # the exit code; they surface early doc-size drift (the two-tier budget
    # above) while acting on it is still cheap.
    notes: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.errors and not self.warnings


def _git_status_porcelain(folder: Path) -> str | None:
    """``git status --porcelain`` scoped to folder; None when not in a repo
    (or git is unavailable/fails) -- read as uncommitted (module docstring)."""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--", str(folder)],
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def is_uncommitted(folder: Path) -> bool:
    """True when the folder has uncommitted git changes OR is not inside a
    git repo (no git record of it -- the unsaved-durable-work condition; see
    the module docstring). Public: location_ops.archive/delete reuse this
    exact reading for the spec 7.4 uncommitted-archive guard, so the warning
    and the refusal can never diverge."""
    out = _git_status_porcelain(folder)
    return out is None or out.strip() != ""


def _doc_sections(lines: list[str]) -> list[tuple[str, int]]:
    """``(title, line count)`` per ``##`` section, in document order. A
    section runs from its heading to the next ``#``/``##`` heading (deeper
    headings belong to it); the heading line counts. Fenced code blocks are
    opaque (a ``##`` inside one is not a heading)."""
    sections: list[tuple[str, int]] = []
    fence = False
    title: str | None = None
    count = 0
    for line in lines:
        if line.lstrip().startswith("```"):
            fence = not fence
        if not fence and line.startswith("#"):
            if line.startswith("## ") or line.rstrip() == "##":
                if title is not None:
                    sections.append((title, count))
                title = line[2:].strip()
                count = 1
                continue
            if not line.startswith("###"):  # a top-level # heading ends it
                if title is not None:
                    sections.append((title, count))
                title, count = None, 0
                continue
        if title is not None:
            count += 1
    if title is not None:
        sections.append((title, count))
    return sections


def _largest_sections(sections: list[tuple[str, int]], k: int = 3) -> str:
    """The remedy clause: the k largest ``##`` sections with line counts, so
    a length finding says exactly what to move where."""
    top = sorted(sections, key=lambda s: s[1], reverse=True)[:k]
    return ", ".join(f"'## {title}' {count}" for title, count in top)


def _doc_size_findings(
    folder: Path, canonical: str
) -> tuple[list[str], list[str]]:
    """The document-size budgets (constants above): ``(warnings, notes)``
    over every top-level ``*.md`` in the folder, log.md/log-*.md exempt."""
    warnings: list[str] = []
    notes: list[str] = []
    for path in sorted(folder.glob("*.md")):
        name = path.name
        if _LOG_EXEMPT_RE.match(name):
            continue  # the history sink rotation targets -- never budgeted
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        n = len(lines)
        target, ceiling = _DOC_BUDGETS.get(name, (None, OTHER_DOC_CEILING))
        sections = _doc_sections(lines)
        top = _largest_sections(sections)
        detail = f"; largest sections: {top}" if top else ""
        if n > ceiling:
            warnings.append(
                f"oversized document: {canonical}/{name} is {n} lines "
                f"(ceiling {ceiling}){detail} -- decompose, do not just "
                "trim: rotate history to log.md and split detail into "
                "referenced docs (task skill references/handoff-template.md, "
                "'Rotation strategy')"
            )
        elif target is not None and n > target:
            notes.append(
                f"approaching budget: {canonical}/{name} is {n} lines "
                f"(healthy target {target}, ceiling {ceiling}){detail} -- "
                "rotate now while it is cheap (task skill "
                "references/handoff-template.md, 'Rotation strategy')"
            )
        if name in _SECTION_CHECKED_DOCS and n >= SECTION_NOTE_MIN_DOC_LINES:
            for title, count in sections:
                if count * 2 > n:
                    notes.append(
                        f"dominant section: {canonical}/{name} '## {title}' "
                        f"is {count} of {n} lines (over half the document) "
                        "-- rotate its history to log.md or split it into a "
                        "referenced doc (task skill "
                        "references/handoff-template.md, 'Rotation strategy')"
                    )
        if name == "CLAUDE.md":
            marks = sum(1 for line in lines if _DIARY_RE.match(line))
            if marks > DIARY_MARKER_MAX:
                notes.append(
                    f"session diary in CLAUDE.md: {canonical}/CLAUDE.md has "
                    f"{marks} dated session-narrative markers (**YYYY-MM-DD) "
                    "-- CLAUDE.md is live state, not history; move dated "
                    "narrative to log.md (task skill "
                    "references/handoff-template.md, 'log.md -- history')"
                )
    return warnings, notes


def _dangling_reason(entry: object, project_root: Path) -> str | None:
    """Why a depends_on/blocked_by entry is dangling; None when it is not."""
    if not isinstance(entry, str):
        return f"not a path string (got {type(entry).__name__})"
    try:
        resolved = resolve.resolve_path(entry, project_root)
    except resolve.RefResolutionError as exc:
        return str(exc)
    if resolved.folder(project_root).is_dir():
        return None
    if resolved.location == resolve.LOCATION_TMP:
        if resolve.archived_tmp_folder(project_root, resolved.stub).is_dir():
            return None  # parked archive: reads as archived, not dangling
        return "orphaned tmp reference (no folder on this host)"
    return None  # non-tmp absent reads as archived per the tri-state


def validate_ref(
    ref: str,
    project_root: Path,
    *,
    ref_host: str | None = None,
    local_host: str | None = None,
) -> ValidationResult:
    """Validate one task reference. See the module docstring for the contract.

    ``ref_host`` is the optional host field carried by the reference;
    ``local_host`` overrides host detection (injectable for tests).
    """
    host = local_host if local_host is not None else resolve.short_hostname()

    try:
        resolved = resolve.resolve_ref(ref, project_root)
    except resolve.RefResolutionError as exc:
        return ValidationResult(ref=ref, classification="invalid", errors=[str(exc)])

    result = ValidationResult(
        ref=ref, classification="invalid", canonical=resolved.canonical
    )
    folder = resolved.folder(project_root)

    # Remote short-circuit: tmp path + non-matching host. Nothing local is
    # read -- the folder is assumed to exist on `host` (spec 7.3). A host on a
    # non-tmp path is not meaningful (spec 2.3) and is ignored.
    if (
        resolved.location == resolve.LOCATION_TMP
        and ref_host is not None
        and ref_host != host
    ):
        result.classification = "remote"
        return result

    # Absent-folder tri-state (spec section 4). A tmp folder parked at
    # tmp/archived-tasks/<stub> is a PROPER archive, not an orphan.
    if not folder.is_dir():
        if resolved.location == resolve.LOCATION_DEV_TASKS:
            result.classification = "archived"
        elif resolve.archived_tmp_folder(project_root, resolved.stub).is_dir():
            result.classification = "archived"
        else:
            result.classification = "orphaned"
            result.warnings.append(
                f"orphaned tmp reference: {resolved.canonical} has no folder on "
                "this host (cleaned up without a proper archive)"
            )
        return result

    errors = result.errors
    warnings = result.warnings

    # --- read + parse task.yaml -------------------------------------------
    task_yaml = folder / "task.yaml"
    data: dict | None = None
    if not task_yaml.is_file():
        errors.append(f"missing task.yaml: {resolved.canonical} has no record")
    else:
        try:
            loaded = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            errors.append(
                f"unparseable YAML: {resolved.canonical}/task.yaml: {exc}"
            )
        else:
            if isinstance(loaded, dict):
                data = loaded
            else:
                errors.append(
                    "schema violation: task.yaml root must be a mapping with a "
                    f"'task' key, got {type(loaded).__name__}"
                )

    # --- schema + vocabulary checks ----------------------------------------
    task_block: dict = {}
    ttype: TaskType | None = None
    if data is not None:
        block = data.get("task")
        if isinstance(block, dict):
            task_block = block

        type_name = task_block.get("type")
        if isinstance(type_name, str):
            ttype = get_type(type_name)
            if ttype is None:
                errors.append(
                    f"unknown type: {type_name!r} names no registered task type"
                )

        # Structural walk. With an unknown/missing type, walk against the
        # default type's schema so structural findings still surface; the
        # missing/wrong `type` itself is already reported above / by the walk.
        schema_type = ttype if ttype is not None else get_type(DEFAULT_TYPE_NAME)
        assert schema_type is not None
        fails, _ = schema_engine.validate(data, schema_type.schema)
        for path, msg in fails:
            errors.append(f"schema violation: {path}: {msg}")

        # Post-walker vocabulary checks -- type-dependent, so only meaningful
        # against a registered type (the layering described in schemas.py).
        if ttype is not None:
            status_val = task_block.get("status")
            if isinstance(status_val, str) and status_val not in ttype.state_vocabulary:
                errors.append(
                    f"schema violation: task.status: {status_val!r} not in state "
                    f"vocabulary {list(ttype.state_vocabulary)}"
                )
            prio = task_block.get("priority")
            if isinstance(prio, str) and not re.match(ttype.priority_pattern, prio):
                errors.append(
                    f"schema violation: task.priority: {prio!r} does not match "
                    f"{ttype.priority_pattern!r}"
                )
            ver = task_block.get("_schema_version")
            if isinstance(ver, str) and ver not in ttype.schema_versions:
                errors.append(
                    f"schema violation: task._schema_version: unknown version "
                    f"{ver!r} (known: {list(ttype.schema_versions)})"
                )
            # Scaffolding (type-defined file set).
            for fname in ttype.scaffolding:
                if not (folder / fname).is_file():
                    errors.append(f"missing scaffolding file: {fname}")

    # --- warnings ------------------------------------------------------------
    raw_status = task_block.get("status")
    status = raw_status if isinstance(raw_status, str) else None

    if resolved.location == resolve.LOCATION_DEV_TASKS:
        if status == "archived":
            warnings.append(
                f"non-tmp folder with status: archived: {resolved.canonical} "
                "should have been deleted (git is the record) -- run archive/delete"
            )
        if is_uncommitted(folder):
            warnings.append(
                f"uncommitted dev/tasks folder: {resolved.canonical} has unsaved "
                "durable work -- commit it (git is the record; archive commits "
                "the final state itself, delete refuses until committed)"
            )

    size_warnings, size_notes = _doc_size_findings(folder, resolved.canonical)
    warnings.extend(size_warnings)
    result.notes.extend(size_notes)

    for field_name in ("depends_on", "blocked_by"):
        entries = task_block.get(field_name)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            reason = _dangling_reason(entry, project_root)
            if reason is not None:
                warnings.append(f"dangling {field_name} entry {entry!r}: {reason}")

    # --- task_items unit (design/task-items-design.md section 9) -------------
    # For types whose scaffolding includes plan.md: the block's structural /
    # vocabulary findings are errors; a missing block is the pre-contract
    # warning (gates work, prompting the one-time forward conversion); a
    # CLAUDE.md Immediate Priorities id reference that resolves to no item is
    # the stale-reference warning.
    if ttype is not None and "plan.md" in ttype.scaffolding:
        items_result = read_task_items(folder, ttype)
        errors.extend(items_result.errors)
        if not items_result.block_found:
            warnings.append(
                f"no task_items block: {resolved.canonical}/plan.md does not "
                "enumerate the open items -- run the one-time conversion "
                "(task skill references/handoff-template.md, 'Converting a "
                "pre-contract folder')"
            )
        elif not items_result.errors:
            claude_md = folder / "CLAUDE.md"
            if claude_md.is_file():
                try:
                    text = claude_md.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    text = ""
                ids = {item.id for item in items_result.items}
                for token in stale_priority_refs(text, ids):
                    warnings.append(
                        f"stale item reference in CLAUDE.md Immediate "
                        f"Priorities: `{token}` matches no task_items id"
                    )

    # --- classification -------------------------------------------------------
    if errors:
        result.classification = "invalid"
        return result

    # No errors implies a registered type, a present status inside the type's
    # vocabulary, and intact scaffolding.
    blocked_by = task_block.get("blocked_by")
    if isinstance(blocked_by, list) and blocked_by:
        result.classification = "blocked"
    else:
        assert status is not None
        result.classification = status
    return result
