"""The ``validate`` verb (spec section 9).

Checks a task against its Task Type schema and the structural rules,
classifies it, and emits findings. Errors make the task ``invalid``; warnings
do not, but both gate ``work`` (later step).

Classification outcomes:
- ``remote``   -- tmp path + non-matching host on the ref. SHORT-CIRCUITS:
                  the folder/task.yaml is never read or validated locally.
- ``invalid``  -- any error.
- ``archived`` / ``orphaned`` -- the absent-folder tri-state (spec section 4):
  non-tmp path + no folder reads as ``archived`` (expected end state, git is
  the record, no findings); tmp path (local host or no host) + no folder is
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


@dataclass
class ValidationResult:
    ref: str
    classification: str
    canonical: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

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

    # Absent-folder tri-state (spec section 4).
    if not folder.is_dir():
        if resolved.location == resolve.LOCATION_DEV_TASKS:
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
                "durable work; archive refuses until committed"
            )

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
                "enumerate the open items -- add the task_items unit "
                "(task-items contract)"
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
