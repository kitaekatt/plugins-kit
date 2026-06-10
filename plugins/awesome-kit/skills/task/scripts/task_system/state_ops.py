"""State ops -- ``work`` / ``switch`` / ``update`` / ``close`` / ``reopen``
(spec section 7.1).

Library functions taking explicit ``project_root`` and ``pointer_path``
parameters; the CLI verbs in scripts/task.py are thin wrappers. Hard failures
raise StateOpError (carrying validate findings when relevant); successful
operations return result dataclasses the CLI renders.

The ``current`` pointer stores the ABSOLUTE path of the task folder (spec
2.6): the pointer file is user-global while canonical task ids are
project-relative, so a relative stored path would be ambiguous across
projects. ``derive_root_and_canonical`` recovers ``(project_root, canonical
id)`` from a stored absolute path -- the two known locations (``tmp/<stub>``
and ``dev/tasks/<stub>``) make the split unambiguous.

Readings chosen in Step 4 (flagged in the implementation report):

- **work has no status precondition.** The spec gates ``work`` on validate
  findings only (errors AND warnings both block) plus the remote error; a
  finding-free task whose stored status is ``closed``/``blocked``/``archived``
  can be worked without ``reopen`` (spec 7.1 names no status pre for work).
- **Auto-init promotion requires a verbatim-safe stub.** init derives folder
  names; a ref whose stub would be REWRITTEN by that derivation (e.g.
  ``tmp/UPPER``) cannot be auto-initialized at the requested path -- error,
  never a folder at a different path than the ref named.
- **A blocked work after auto-init keeps the initialized folder.** Promotion
  ran a real ``init`` (a verb with its own contract); the validate gate then
  blocks only the pointer write. E.g. ``work dev/tasks/x`` on a fresh path
  inits the folder, then blocks on the expected uncommitted-dev/tasks warning.
- **update is a write op; validate reports.** Field edits persist even when
  the re-validation has findings (the CLI exit code reflects findings; the
  write is not rolled back). Bad values (e.g. an out-of-vocabulary status)
  persist and surface as findings -- the fix-forward posture.
- **List-valued edits REPLACE the stored list** (no append/remove micro-ops
  in v1); ``None`` means "leave the field untouched", so a stored field
  cannot be cleared via the CLI in v1.
- **plan.md/log.md rotation, minimal v1 reading.** The hand-off template's
  rotation discipline (references/handoff-template.md in the task skill:
  move completed-step detail plan -> log, keep plan a
  moving window at/below 400 lines) is session-content authoring that needs
  inference -- the skill layer's job, not a script's. The script's mechanical
  share: append one dated entry to log.md recording the update (log.md is
  the history surface), and leave plan.md untouched.
- **close checks the STORED status** (``status: active`` in task.yaml), not
  the computed classification.
- **reopen sets ``status: active`` unconditionally** (its precondition is
  only that the folder exists); it re-validates and reports findings.
- **task.yaml read-modify-write** uses yaml.safe_load + safe_dump
  (sort_keys=False): unknown extra fields and the mapping's insertion order
  round-trip; YAML comments do not survive an edit (content, not bytes, is
  the contract). A missing/unparseable/mis-shaped task.yaml is a hard error
  for any verb that must read-modify-write it (no recovery; fix forward).
- **Remote guard scope.** ``work`` and ``update`` take ``ref_host`` and
  refuse a remote ref (spec 7.3) -- they are the verbs that would otherwise
  CREATE a local folder (auto-init/upsert) for a task living elsewhere.
  ``close``/``reopen`` require an existing local folder and the CLI has no
  host flag, so they take no ``ref_host`` in v1.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import pointer as pointer_mod
from . import resolve
from .discovery import read_task_block
from .init import InitError, derive_stub_and_title, init_task
from .validate import ValidationResult, validate_ref


class StateOpError(Exception):
    """A state op could not proceed. Carries validate findings when the
    failure is a validation block (the work gate)."""

    def __init__(
        self,
        message: str,
        *,
        errors: Sequence[str] = (),
        warnings: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.errors = list(errors)
        self.warnings = list(warnings)


@dataclass(frozen=True)
class WorkResult:
    """Outcome of a successful ``work``: the pointer is written; the skill
    layer acts on ``skills_to_invoke`` / ``agent_hint`` (the script only
    emits them)."""

    canonical: str
    folder: Path  # absolute
    initialized: bool  # auto-init promotion happened
    skills_to_invoke: tuple[str, ...]
    agent_hint: str | None


@dataclass(frozen=True)
class UpdateResult:
    """Outcome of ``update`` (and ``reopen``): the write persisted;
    ``validation`` carries the re-run classification + findings."""

    canonical: str
    folder: Path  # absolute
    initialized: bool  # upsert-init happened (always False for reopen)
    validation: ValidationResult


@dataclass(frozen=True)
class SwitchResult:
    previous: UpdateResult | None  # update of the previously-current task
    work: WorkResult


def derive_root_and_canonical(folder: Path) -> tuple[Path, str] | None:
    """Recover ``(project_root, canonical id)`` from an absolute task-folder
    path, or None when the path is not absolute / not a known task location
    shape (``.../tmp/<stub>`` or ``.../dev/tasks/<stub>``)."""
    if not folder.is_absolute():
        return None
    if folder.parent.name == "tmp":
        return folder.parent.parent, f"tmp/{folder.name}"
    if folder.parent.name == "tasks" and folder.parent.parent.name == "dev":
        return folder.parent.parent.parent, f"dev/tasks/{folder.name}"
    return None


# --- shared internals --------------------------------------------------------


def _resolve(ref: str, project_root: Path) -> resolve.ResolvedRef:
    try:
        return resolve.resolve_ref(ref, project_root)
    except resolve.RefResolutionError as exc:
        raise StateOpError(str(exc)) from exc


def _auto_init(resolved: resolve.ResolvedRef, project_root: Path) -> None:
    """Promotion (spec 7.1 work) / upsert (spec 7.1 update): init the folder
    at the ref's resolved path. The ref's stub must survive init's stub
    derivation verbatim, else init would create a different path."""
    try:
        stub, _ = derive_stub_and_title(resolved.stub)
    except InitError as exc:
        raise StateOpError(
            f"cannot auto-init {resolved.canonical}: {exc}"
        ) from exc
    if stub != resolved.stub:
        raise StateOpError(
            f"cannot auto-init {resolved.canonical}: {resolved.stub!r} is not "
            "a valid folder stub (init would rewrite it to "
            f"{stub!r}) -- run init explicitly"
        )
    try:
        init_task(resolved.stub, project_root, dest=resolved.location)
    except InitError as exc:
        raise StateOpError(
            f"auto-init failed for {resolved.canonical}: {exc}"
        ) from exc


def _read_task_yaml(folder: Path, canonical: str) -> dict:
    """Load task.yaml for read-modify-write. Hard error when it cannot be
    edited structurally (missing / unparseable / mis-shaped) -- fix forward."""
    path = folder / "task.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise StateOpError(
            f"{canonical}/task.yaml is missing or unparseable ({exc}) -- "
            "fix forward, then retry"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("task"), dict):
        raise StateOpError(
            f"{canonical}/task.yaml is mis-shaped (no 'task' mapping) -- "
            "fix forward, then retry"
        )
    return data


def _write_task_yaml(folder: Path, data: dict) -> None:
    (folder / "task.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def _append_log_entry(folder: Path, edits: dict[str, object]) -> None:
    """The minimal rotation share (module docstring): one dated log.md line
    recording the update; plan.md is untouched."""
    stamp = datetime.date.today().isoformat()
    detail = (
        "; ".join(f"{name} = {value!r}" for name, value in edits.items())
        if edits
        else "refresh (no field edits)"
    )
    with (folder / "log.md").open("a", encoding="utf-8") as fh:
        fh.write(f"- {stamp}: update: {detail}\n")


def _clear_pointer_if_names(pointer_path: Path, folder: Path) -> None:
    """Spec 7.1 "clear the pointer": blank it iff it names this task."""
    stored = pointer_mod.read_current(pointer_path)
    if stored is not None and Path(stored) == folder.resolve():
        pointer_mod.clear_current(pointer_path)


# --- the verbs ---------------------------------------------------------------


def work(
    ref: str,
    project_root: Path,
    pointer_path: Path,
    *,
    ref_host: str | None = None,
    local_host: str | None = None,
) -> WorkResult:
    """``work <ref>`` (spec 7.1): auto-init when the folder is absent
    (promotion), gate on validate (ANY error or warning blocks; remote
    errors), then write the pointer (absolute path) and surface the task's
    ``skills_to_invoke`` / ``agent_hint``. Raises StateOpError on any block."""
    resolved = _resolve(ref, project_root)
    result = validate_ref(
        resolved.canonical, project_root, ref_host=ref_host, local_host=local_host
    )
    if result.classification == "remote":
        raise StateOpError(
            f"{resolved.canonical} is remote (host {ref_host!r}); a remote "
            "task cannot be worked locally (spec 7.3)"
        )
    folder = resolved.folder(project_root)
    initialized = False
    if not folder.is_dir():
        _auto_init(resolved, project_root)
        initialized = True
        result = validate_ref(
            resolved.canonical,
            project_root,
            ref_host=ref_host,
            local_host=local_host,
        )
    if result.errors or result.warnings:
        raise StateOpError(
            f"validate blocks work on {resolved.canonical}: "
            f"{len(result.errors)} error(s), {len(result.warnings)} "
            "warning(s) (both gate work, spec 9)",
            errors=result.errors,
            warnings=result.warnings,
        )
    pointer_mod.write_current(pointer_path, folder)
    block = read_task_block(folder) or {}
    raw_skills = block.get("skills_to_invoke")
    skills = (
        tuple(s for s in raw_skills if isinstance(s, str))
        if isinstance(raw_skills, list)
        else ()
    )
    raw_hint = block.get("agent_hint")
    hint = raw_hint if isinstance(raw_hint, str) and raw_hint else None
    return WorkResult(
        canonical=resolved.canonical,
        folder=folder.resolve(),
        initialized=initialized,
        skills_to_invoke=skills,
        agent_hint=hint,
    )


def switch(
    ref: str,
    project_root: Path,
    pointer_path: Path,
    *,
    ref_host: str | None = None,
    local_host: str | None = None,
) -> SwitchResult:
    """``switch <ref>`` (spec 7.1): ``update`` the current task (it becomes a
    plain active task -- no lingering claim), then ``work <ref>``. With
    nothing current, or a stale pointer (missing folder / non-derivable
    content), identical to ``work`` (the stale pointer is cleared)."""
    previous: UpdateResult | None = None
    stored = pointer_mod.read_current(pointer_path)
    if stored is not None:
        prev_folder = Path(stored)
        derived = derive_root_and_canonical(prev_folder)
        if derived is not None and prev_folder.is_dir():
            prev_root, prev_canonical = derived
            previous = update(
                prev_canonical, prev_root, pointer_path, local_host=local_host
            )
        else:
            pointer_mod.clear_current(pointer_path)
    work_result = work(
        ref, project_root, pointer_path, ref_host=ref_host, local_host=local_host
    )
    return SwitchResult(previous=previous, work=work_result)


def update(
    ref: str | None,
    project_root: Path,
    pointer_path: Path,
    *,
    status: str | None = None,
    priority: str | None = None,
    description: str | None = None,
    depends_on: list[str] | None = None,
    blocked_by: list[str] | None = None,
    agent_hint: str | None = None,
    skills_to_invoke: list[str] | None = None,
    ref_host: str | None = None,
    local_host: str | None = None,
) -> UpdateResult:
    """``update [<ref>] [field edits]`` (spec 7.1): upsert (init when the
    folder is absent), apply task.yaml field edits (lists REPLACE), append
    the dated log.md entry, re-run validate. The write persists regardless of
    findings; the result carries classification + findings.

    ``ref=None`` defaults to the current task (error when nothing is
    current); the current task's project root derives from the pointer's
    absolute path, superseding ``project_root``.
    """
    if ref is None:
        stored = pointer_mod.read_current(pointer_path)
        prev_folder = Path(stored) if stored is not None else None
        derived = (
            derive_root_and_canonical(prev_folder)
            if prev_folder is not None
            else None
        )
        if derived is None or prev_folder is None or not prev_folder.is_dir():
            raise StateOpError(
                "no <ref> given and nothing is current -- pass a ref or run "
                "work first"
            )
        project_root, canonical = derived
        resolved = resolve.resolve_path(canonical, project_root)
    else:
        resolved = _resolve(ref, project_root)

    pre = validate_ref(
        resolved.canonical, project_root, ref_host=ref_host, local_host=local_host
    )
    if pre.classification == "remote":
        raise StateOpError(
            f"{resolved.canonical} is remote (host {ref_host!r}); a remote "
            "task cannot be updated locally (spec 7.3)"
        )

    folder = resolved.folder(project_root)
    initialized = False
    if not folder.is_dir():
        _auto_init(resolved, project_root)
        initialized = True

    edits: dict[str, object] = {}
    for name, value in (
        ("status", status),
        ("priority", priority),
        ("description", description),
        ("depends_on", depends_on),
        ("blocked_by", blocked_by),
        ("agent_hint", agent_hint),
        ("skills_to_invoke", skills_to_invoke),
    ):
        if value is not None:
            edits[name] = value

    if edits:
        data = _read_task_yaml(folder, resolved.canonical)
        data["task"].update(edits)
        _write_task_yaml(folder, data)
    _append_log_entry(folder, edits)

    result = validate_ref(
        resolved.canonical, project_root, ref_host=ref_host, local_host=local_host
    )
    return UpdateResult(
        canonical=resolved.canonical,
        folder=folder.resolve(),
        initialized=initialized,
        validation=result,
    )


def close(ref: str, project_root: Path, pointer_path: Path) -> str:
    """``close <ref>`` (spec 7.1): pre folder exists + stored status active;
    set ``status: closed``, keep the folder, clear the pointer iff it names
    this task. Returns the canonical id."""
    resolved = _resolve(ref, project_root)
    folder = resolved.folder(project_root)
    if not folder.is_dir():
        raise StateOpError(
            f"{resolved.canonical}: no task folder -- close requires an "
            "existing folder"
        )
    data = _read_task_yaml(folder, resolved.canonical)
    stored_status = data["task"].get("status")
    if stored_status != "active":
        raise StateOpError(
            f"close acts on an active task; {resolved.canonical} has "
            f"status {stored_status!r}"
        )
    data["task"]["status"] = "closed"
    _write_task_yaml(folder, data)
    _clear_pointer_if_names(pointer_path, folder)
    return resolved.canonical


def reopen(
    ref: str,
    project_root: Path,
    pointer_path: Path,
    *,
    local_host: str | None = None,
) -> UpdateResult:
    """``reopen <ref>`` (spec 7.1): pre folder exists (incl. a tmp archived
    folder; a missing folder cannot be reopened -- it is gone). Set
    ``status: active``; re-validate; the result carries the findings."""
    resolved = _resolve(ref, project_root)
    folder = resolved.folder(project_root)
    if not folder.is_dir():
        raise StateOpError(
            f"{resolved.canonical}: no task folder -- a missing folder "
            "cannot be reopened (the task is gone)"
        )
    data = _read_task_yaml(folder, resolved.canonical)
    data["task"]["status"] = "active"
    _write_task_yaml(folder, data)
    result = validate_ref(resolved.canonical, project_root, local_host=local_host)
    return UpdateResult(
        canonical=resolved.canonical,
        folder=folder.resolve(),
        initialized=False,
        validation=result,
    )
