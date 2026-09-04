"""State ops -- ``work`` / ``update`` / ``close`` / ``reopen``
(spec section 7.1).

Library functions take an explicit ``project_root``; the CLI verbs in
scripts/task.py are thin wrappers. Hard failures raise StateOpError (carrying
validate findings when relevant); successful operations return result
dataclasses the CLI renders.

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
  blocks the operation. E.g. ``work dev/tasks/x`` on a fresh path inits the
  folder, then blocks on the expected uncommitted-dev/tasks warning.
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
  only that the folder exists -- a folder parked at
  ``<location>/archived-tasks/<stub>`` counts and is restored to
  ``<location>/<stub>`` first, under either root); it re-validates and
  reports findings.
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
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

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


#: Skills every worked task needs, regardless of what it declares. Merged
#: ahead of the task's own ``skills_to_invoke`` so ``work`` emits ONE
#: initialization list -- adherence tracks what the script emits, not what
#: prose elsewhere requires, and a requirement that lives only in prose is
#: the one that gets skipped. ``orchestrate`` gates its own applicability
#: (its step 1 is "confirm the task warrants orchestration"), so emitting it
#: unconditionally costs a skill load on a trivial task and nothing else.
BASELINE_SKILLS: tuple[str, ...] = ("awesome-kit:orchestrate",)


@dataclass(frozen=True)
class WorkResult:
    """Outcome of a successful ``work``; the skill layer acts on
    ``skills_to_invoke`` / ``agent_hint`` (the script only emits them).
    ``skills_to_invoke`` is the MERGED list --
    ``BASELINE_SKILLS`` followed by the task's own entries, deduped."""

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


def _merge_skills(declared: Sequence[str]) -> tuple[str, ...]:
    """BASELINE_SKILLS then the task's own entries, order-preserving and
    deduped (a task that already declares a baseline skill is not emitted
    twice). Baseline first: it governs HOW the task-declared skills get
    used, so it belongs ahead of them."""
    merged: list[str] = []
    for skill in (*BASELINE_SKILLS, *declared):
        if skill not in merged:
            merged.append(skill)
    return tuple(merged)


# --- the verbs ---------------------------------------------------------------


def work(
    ref: str,
    project_root: Path,
    *,
    ref_host: str | None = None,
    local_host: str | None = None,
) -> WorkResult:
    """``work <ref>`` (spec 7.1): auto-init when the folder is absent
    (promotion), gate on validate (ANY error or warning blocks; remote
    errors), then surface the task's ``skills_to_invoke`` / ``agent_hint``.
    Raises StateOpError on any block."""
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
    block = read_task_block(folder) or {}
    raw_skills = block.get("skills_to_invoke")
    declared = (
        tuple(s for s in raw_skills if isinstance(s, str))
        if isinstance(raw_skills, list)
        else ()
    )
    skills = _merge_skills(declared)
    raw_hint = block.get("agent_hint")
    hint = raw_hint if isinstance(raw_hint, str) and raw_hint else None
    return WorkResult(
        canonical=resolved.canonical,
        folder=folder.resolve(),
        initialized=initialized,
        skills_to_invoke=skills,
        agent_hint=hint,
    )


def update(
    ref: str,
    project_root: Path,
    *,
    status: str | None = None,
    priority: str | None = None,
    description: str | None = None,
    depends_on: list[str] | None = None,
    blocked_by: list[str] | None = None,
    agent_hint: str | None = None,
    skills_to_invoke: list[str] | None = None,
    durable_outputs: list[str] | None = None,
    ref_host: str | None = None,
    local_host: str | None = None,
) -> UpdateResult:
    """``update <ref> [field edits]`` (spec 7.1): upsert (init when the
    folder is absent), apply task.yaml field edits (lists REPLACE), append
    the dated log.md entry, re-run validate. The write persists regardless of
    findings; the result carries classification + findings.
    """
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
        ("durable_outputs", durable_outputs),
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


def close(ref: str, project_root: Path) -> str:
    """``close <ref>`` (spec 7.1): pre folder exists + stored status active;
    set ``status: closed`` and keep the folder. Returns the canonical id."""
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
    return resolved.canonical


def reopen(
    ref: str,
    project_root: Path,
    *,
    local_host: str | None = None,
) -> UpdateResult:
    """``reopen <ref>`` (spec 7.1): pre folder exists -- including an
    archived folder parked at ``<location>/archived-tasks/<stub>``, which is
    first RESTORED to ``<location>/<stub>``; a missing folder cannot be
    reopened -- it is gone. Set ``status: active``; re-validate; the result
    carries the findings.

    Both roots park: tmp always, and dev/tasks when git is configured to
    IGNORE the folder (archive's ``vcs_ignored`` disposition), so reopen
    looks in the parking directory for either."""
    resolved = _resolve(ref, project_root)
    folder = resolved.folder(project_root)
    if not folder.is_dir():
        parked = resolve.archived_folder(
            project_root, resolved.location, resolved.stub
        )
        if parked.is_dir():
            folder.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(parked), str(folder))
        else:
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
