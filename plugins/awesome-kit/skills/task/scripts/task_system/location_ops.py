"""Location ops -- ``archive`` / ``delete`` / ``move`` (spec sections 7.1,
7.2, 7.4).

The destructive + location verbs live in their own module (CCP): they change
for closure-policy / location reasons (spec 2.5 closure_policy, 7.4
locations), not for the status reasons state_ops changes for. They reuse
state_ops' shared internals (resolution, task.yaml read-modify-write) and its
StateOpError -- one error type across all verbs, so the CLI's error rendering
is uniform.

Readings chosen in Step 5 (flagged in the implementation report):

- **archive precondition is the STORED status** (``status: active`` in
  task.yaml), matching close's reading. A ``closed`` task errors with a
  "reopen first" hint (spec 7.1); any other non-active status also errors.
- **Non-tmp archive SUBMITS to version control, then removes** (spec 7.4,
  revised 2026-07-22): the durable record must be in version-control history
  before the folder can go. The task system has NO dependency on git --
  version control is the record; git is merely the VCS this script can
  detect and automate. In a git repo, archive writes the final state
  (``status: archived`` + a dated log.md entry), commits it, removes the
  folder, and commits the removal -- two commits, both pathspec-limited to
  the task folder (``git commit -- <folder>``) so pre-staged unrelated index
  content never rides along; any git failure aborts BEFORE the folder is
  removed. OUTSIDE a git repo, no git command runs: archive records the
  final state and KEEPS the folder (``vcs_pending``), leaving submission to
  the agent/user who knows the workspace's VCS (e.g. ``p4 submit``) --
  finish with ``delete`` once submitted.
- **delete keeps the commit-first guard where git can verify it**
  (``validate.git_vcs_state``, shared predicate): delete is the no-ceremony
  removal verb -- in a git repo it refuses a dirty dev/tasks folder rather
  than auto-committing. Outside a git repo the script cannot verify any VCS
  state, so delete proceeds -- the agent owns version control there (this is
  the second half of the non-git archive flow). Preconditions: delete
  accepts stored status ``active`` OR ``archived`` -- ``archived`` because a
  still-present archived folder (a ``vcs_pending`` archive output, or the
  validate warning's "should have been deleted" case) is exactly what
  delete finishes off; a ``closed`` task still errors with the
  reopen-first hint.
- **tmp archive PARKS the folder** (spec 2.5, revised 2026-07-22): sets
  ``status: archived`` and moves the folder to
  ``tmp/archived-tasks/<stub>`` (resolve.archived_tmp_folder) so tmp/ stays
  a working set; the user purges the parking directory at will. An occupied
  parking spot (a previously-archived same-stub task) refuses -- remove the
  old copy first. validate reads a parked folder as ``archived`` (not
  orphaned) and reopen restores it to ``tmp/<stub>``.
 - **delete skips the intermediate tmp status write.** For a tmp folder,
  archive-then-remove would write ``status: archived`` into a folder removed
  moments later -- an unobservable intermediate state. delete goes straight
  to removal; the net post-state is the folder being gone.
- **move takes dest-root-only** (``tmp`` or ``dev/tasks``); the stub is
  preserved. A full ``<dest>/<stub2>`` rename-move is out of scope (spec 7.1
  names only the dest; renaming would be a second identity change).
- **move has no status precondition and no uncommitted guard** -- the spec
  pre is only "folder exists locally (not remote)". Any stored status moves.
- **move's remote guard is library-level** (``ref_host``/``local_host``
  params, same pattern as work/update): a tmp ref tagged with a non-matching
  host refuses (spec 7.3) even when a same-named local folder exists. The
  CLI has no host flag in v1 (consistent with Steps 1-4).
- **Reference-rewrite mechanism** (spec 7.2): after relocating the folder,
  every ``*.md`` under the project root is scanned. This is DELIBERATELY
  broader than discovery's project document set, which covers only the task
  roots (spec 8 step 1): a stale reference is wrong wherever it lives, so
  the rewrite must reach documents ``list`` never enumerates. Fenced YAML blocks are located span-accurately with the same
  compiled regex document_walker's ``iter_yaml_blocks`` uses (imported, not
  duplicated -- a public span API in skills-kit would be a cross-plugin
  change out of this step's scope). Each block is parsed with
  ``yaml.compose`` (node marks carry exact character spans); only the scalar
  value nodes at ``task_list.refs[].path`` whose value CANONICALIZES to the
  old path are replaced, by exact-span splice with the new canonical
  project-relative path. Everything outside those value spans -- prose,
  comments, quoting of other scalars, whitespace -- is preserved
  byte-for-byte. A prose mention of the old path, a ``path:`` under some
  other YAML root, or a ref to a different task is never touched. A
  quoted matching scalar is replaced quotes-and-all with the bare canonical
  form (canonical paths need no quoting).
- **Unparseable blocks / unreadable docs are skipped silently by move** --
  they cannot contain a structurally-recognizable task_list ref; surfacing
  them is validate/discovery's job, not move's.
- **``host`` fields ride along untouched.** move rewrites the ``path`` value
  only; a ``host`` tag on a promoted ref becomes inert (spec 2.3: host is
  only meaningful for tmp paths) but is the document author's to clean up.
"""

from __future__ import annotations

import datetime
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath

import yaml

from skills_kit_lib.document_walker import YAML_BLOCK_RE

from . import resolve
from .state_ops import (
    StateOpError,
    _read_task_yaml,
    _resolve,
    _write_task_yaml,
)
from .validate import git_vcs_state


@dataclass(frozen=True)
class ArchiveResult:
    canonical: str
    folder_removed: bool  # non-tmp in git: True (VC is the record); else False
    archived_to: str | None = None  # tmp: parking path (project-relative)
    # Non-tmp, outside any git repo: final state recorded, folder KEPT --
    # submission to the workspace's VCS (and the finishing delete) is the
    # agent/user's to do.
    vcs_pending: bool = False
    # Absent `durable_outputs` (every task predating the field): the rule
    # degrades to this note rather than refusing -- manifests here stay
    # backwards-READABLE.
    durable_note: str | None = None


@dataclass(frozen=True)
class MoveResult:
    old_canonical: str
    new_canonical: str
    folder: Path  # absolute new location
    rewritten_docs: tuple[Path, ...]  # documents whose references were rewritten


# --- durable outputs ---------------------------------------------------------


def _verify_durable_outputs(
    data: dict, project_root: Path, folder: Path, canonical: str
) -> str | None:
    """Archive's durable-outputs check (spec 2.7). Returns a note when the
    field is absent; raises StateOpError naming every offender when a
    declared path has no durable home.

    Deliberately MECHANICAL -- existence plus outside-the-folder, no
    assessment of what a document is. Archive can ask the user nothing, so
    the judgment lives at authoring time (the declaration); this only
    confirms the declaration was honored. A path INSIDE the folder is the
    load-bearing failure: the folder is about to be parked or deleted, so
    such a document has no durable home at all -- exactly the mistake the
    rule exists to catch.
    """
    declared = data.get("task", {}).get("durable_outputs")
    if declared is None:
        return (
            f"{canonical} declares no durable_outputs -- if this task produced "
            "a document that outlives it, its home is the repo it describes, "
            "not this folder (references/handoff-template.md 'Durable "
            "outputs')"
        )
    if not isinstance(declared, list):
        raise StateOpError(
            f"{canonical}: durable_outputs must be a list of repo-relative "
            f"paths, got {type(declared).__name__}"
        )
    folder_resolved = folder.resolve()
    root_resolved = project_root.resolve()
    problems: list[str] = []
    for entry in declared:
        if not isinstance(entry, str) or not entry.strip():
            problems.append(f"{entry!r}: not a non-empty path string")
            continue
        # Containment is enforced, not assumed. `project_root / entry` DISCARDS
        # project_root when entry is absolute, and a "../" entry resolves
        # outside it -- either would declare a durable home the repo does not
        # carry, which defeats the point (version control is the record).
        if PurePath(entry).is_absolute() or PureWindowsPath(entry).is_absolute():
            problems.append(
                f"{entry}: must be RELATIVE to the project root (a durable "
                "home outside the repo is not carried by version control)"
            )
            continue
        target = (project_root / entry).resolve()
        if not target.is_relative_to(root_resolved):
            problems.append(
                f"{entry}: resolves OUTSIDE the project root -- a durable "
                "home must live in the repo that records it"
            )
            continue
        if not target.exists():
            problems.append(f"{entry}: no such path under the project root")
            continue
        if target == folder_resolved or folder_resolved in target.parents:
            problems.append(
                f"{entry}: lives INSIDE the task folder, which archive "
                "parks or deletes -- move it to its durable home first"
            )
    if problems:
        raise StateOpError(
            f"{canonical}: declared durable_outputs have no durable home:\n"
            + "\n".join(f"  - {p}" for p in problems)
            + "\nRelocate each document to the repo it describes, update the "
            "declaration (update --durable-output PATH ...), then archive."
        )
    return None


# --- archive / delete --------------------------------------------------------


def _archive_preflight(
    ref: str,
    project_root: Path,
    verb: str,
    *,
    allowed_statuses: tuple[str, ...],
    require_committed: bool,
) -> tuple[resolve.ResolvedRef, Path, dict]:
    """The shared archive/delete preconditions (module docstring): folder
    exists, stored status in ``allowed_statuses`` (closed -> reopen-first
    hint). With ``require_committed`` (delete), a non-tmp folder that git
    can see is dirty refuses -- archive instead commits the final state
    itself, and outside a git repo the script cannot verify VCS state, so
    no guard applies (the agent owns version control there)."""
    resolved = _resolve(ref, project_root)
    folder = resolved.folder(project_root)
    if not folder.is_dir():
        raise StateOpError(
            f"{resolved.canonical}: no task folder -- {verb} requires an "
            "existing folder"
        )
    data = _read_task_yaml(folder, resolved.canonical)
    stored_status = data["task"].get("status")
    if stored_status not in allowed_statuses:
        hint = (
            " -- reopen it first" if stored_status == "closed" else ""
        )
        wanted = " or ".join(allowed_statuses)
        raise StateOpError(
            f"{verb} acts on an {wanted} task; {resolved.canonical} has "
            f"status {stored_status!r}{hint}"
        )
    if (
        require_committed
        and resolved.location == resolve.LOCATION_DEV_TASKS
        and git_vcs_state(folder) == "dirty"
    ):
        raise StateOpError(
            f"{resolved.canonical} has uncommitted git changes -- commit "
            f"first; version control is the record ({verb} refuses, spec "
            "7.4; use archive to record the final state)"
        )
    return resolved, folder, data


def _git_toplevel(folder: Path) -> Path | None:
    """The git working-tree root holding ``folder``, or None when it is not
    inside a repo (or git is unavailable/fails)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(folder), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    top = proc.stdout.strip()
    return Path(top) if top else None


def _git_commit_folder(repo_root: Path, folder: Path, message: str) -> None:
    """Stage and commit exactly the task folder's changes (adds, edits, AND
    deletions). Pathspec-limited commit: pre-staged unrelated index content
    is never swept in. Raises StateOpError on any git failure."""
    for cmd in (
        ["git", "-C", str(repo_root), "add", "-A", "--", str(folder)],
        [
            "git",
            "-C",
            str(repo_root),
            "commit",
            "-q",
            "-m",
            message,
            "--",
            str(folder),
        ],
    ):
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StateOpError(f"git failed during archive: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr.strip() or proc.stdout.strip() or
                      f"exit {proc.returncode}")
            raise StateOpError(
                f"git failed during archive ({' '.join(cmd[3:5])}): {detail}"
            )


def _append_archive_log_entry(folder: Path, detail: str) -> None:
    """The dated log.md line recording the archival (mirrors state_ops'
    update log discipline)."""
    stamp = datetime.date.today().isoformat()
    with (folder / "log.md").open("a", encoding="utf-8") as fh:
        fh.write(f"- {stamp}: archive: {detail}\n")


def archive_task(ref: str, project_root: Path) -> ArchiveResult:
    """``archive <ref>`` (spec 7.1): pre folder exists + stored status
    active. Then per closure policy (spec 2.5, revised): tmp ->
    ``status: archived`` and park the folder at ``tmp/archived-tasks/<stub>``;
    non-tmp in a git repo -> record the final state (status + log entry),
    commit it, remove the folder, commit the removal (version control is the
    record; two pathspec-limited commits); non-tmp outside any git repo ->
    record the final state and KEEP the folder (``vcs_pending``: the agent
    submits it with the workspace's VCS, then runs delete)."""
    resolved, folder, data = _archive_preflight(
        ref,
        project_root,
        "archive",
        allowed_statuses=("active",),
        require_committed=False,
    )
    # Before anything is parked, committed, or removed: a declared durable
    # output with no home outside the folder must block, while it can still
    # be relocated.
    durable_note = _verify_durable_outputs(
        data, project_root, folder, resolved.canonical
    )
    folder_resolved = folder.resolve()
    archived_to: str | None = None
    vcs_pending = False
    if resolved.location == resolve.LOCATION_TMP:
        parking = resolve.archived_tmp_folder(project_root, resolved.stub)
        if parking.exists():
            raise StateOpError(
                f"archive parking spot already occupied: "
                f"{resolve.LOCATION_TMP}/{resolve.ARCHIVED_TMP_DIRNAME}/"
                f"{resolved.stub} exists -- remove (purge) the old archived "
                "copy first"
            )
        data["task"]["status"] = "archived"
        _write_task_yaml(folder, data)
        parking.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(folder), str(parking))
        removed = False
        archived_to = (
            f"{resolve.LOCATION_TMP}/{resolve.ARCHIVED_TMP_DIRNAME}/"
            f"{resolved.stub}"
        )
    else:
        repo_root = _git_toplevel(folder)
        data["task"]["status"] = "archived"
        _write_task_yaml(folder, data)
        if repo_root is None:
            # Not a git workspace: no git command runs. The final state is
            # recorded on disk; submitting it with the workspace's VCS (e.g.
            # p4 submit) and then removing the folder (delete) is the
            # agent/user's to do -- version control is the record.
            _append_archive_log_entry(
                folder,
                "final state recorded; submit to version control, then "
                "delete the folder (version control is the record)",
            )
            removed = False
            vcs_pending = True
        else:
            _append_archive_log_entry(
                folder,
                "final state committed; folder removed (version control "
                "is the record)",
            )
            _git_commit_folder(
                repo_root,
                folder_resolved,
                f"task archive: {resolved.canonical} (final state)",
            )
            shutil.rmtree(folder)
            _git_commit_folder(
                repo_root,
                folder_resolved,
                f"task archive: {resolved.canonical} (remove folder; "
                "version control is the record)",
            )
            removed = True
    return ArchiveResult(
        canonical=resolved.canonical,
        folder_removed=removed,
        archived_to=archived_to,
        vcs_pending=vcs_pending,
        durable_note=durable_note,
    )


def delete_task(ref: str, project_root: Path) -> str:
    """``delete <ref>`` (spec 7.1): accepts stored status active OR archived
    (a still-present archived folder is what delete finishes off), plus the
    commit-first guard where git can verify it (module docstring -- delete
    never auto-commits; outside a git repo the agent owns VCS state), then
    remove the folder even when tmp (unconditional). Returns the canonical id."""
    resolved, folder, _ = _archive_preflight(
        ref,
        project_root,
        "delete",
        allowed_statuses=("active", "archived"),
        require_committed=True,
    )
    shutil.rmtree(folder)
    return resolved.canonical


# --- move --------------------------------------------------------------------


def _matching_path_spans(
    block_text: str, old_canonical: str, project_root: Path
) -> list[tuple[int, int]]:
    """Character spans (within block_text) of every scalar value node at
    ``task_list.refs[].path`` whose value canonicalizes to old_canonical.
    Structural targeting via yaml.compose node marks -- never a string
    search. Unparseable / differently-shaped blocks yield no spans."""
    try:
        node = yaml.compose(block_text)
    except yaml.YAMLError:
        return []
    if not isinstance(node, yaml.MappingNode):
        return []
    spans: list[tuple[int, int]] = []
    for key_node, task_list_node in node.value:
        if not (
            isinstance(key_node, yaml.ScalarNode)
            and key_node.value == "task_list"
            and isinstance(task_list_node, yaml.MappingNode)
        ):
            continue
        for refs_key, refs_node in task_list_node.value:
            if not (
                isinstance(refs_key, yaml.ScalarNode)
                and refs_key.value == "refs"
                and isinstance(refs_node, yaml.SequenceNode)
            ):
                continue
            for item in refs_node.value:
                if not isinstance(item, yaml.MappingNode):
                    continue
                for path_key, path_value in item.value:
                    if not (
                        isinstance(path_key, yaml.ScalarNode)
                        and path_key.value == "path"
                        and isinstance(path_value, yaml.ScalarNode)
                    ):
                        continue
                    try:
                        resolved = resolve.resolve_path(
                            path_value.value, project_root
                        )
                    except resolve.RefResolutionError:
                        continue
                    if resolved.canonical == old_canonical:
                        spans.append(
                            (
                                path_value.start_mark.index,
                                path_value.end_mark.index,
                            )
                        )
    return spans


def _rewrite_doc_text(
    text: str, old_canonical: str, new_canonical: str, project_root: Path
) -> str | None:
    """The rewritten document text, or None when no reference matches.
    Splices the new canonical path into the matching value spans only;
    everything outside those spans is preserved byte-for-byte."""
    edits: list[tuple[int, int]] = []
    for match in YAML_BLOCK_RE.finditer(text):
        block_start = match.start(1)
        for start, end in _matching_path_spans(
            match.group(1), old_canonical, project_root
        ):
            edits.append((block_start + start, block_start + end))
    if not edits:
        return None
    for start, end in sorted(edits, reverse=True):
        text = text[:start] + new_canonical + text[end:]
    return text


def _rewrite_references(
    project_root: Path, old_canonical: str, new_canonical: str
) -> list[Path]:
    """Spec 7.2 step 2: rewrite every task_list reference to the old path
    across ALL *.md under the project root -- intentionally WIDER than
    discovery's project document set, which is scoped to the task roots: a
    stale reference is wrong wherever it lives, including in documents
    ``list`` never enumerates. Returns the docs rewritten."""
    rewritten: list[Path] = []
    for doc in sorted(project_root.rglob("*.md")):
        try:
            text = doc.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # unreadable docs cannot be rewritten (module docstring)
        new_text = _rewrite_doc_text(
            text, old_canonical, new_canonical, project_root
        )
        if new_text is not None:
            doc.write_text(new_text, encoding="utf-8")
            rewritten.append(doc)
    return rewritten


def move_task(
    ref: str,
    dest: str,
    project_root: Path,
    *,
    ref_host: str | None = None,
    local_host: str | None = None,
) -> MoveResult:
    """``move <ref> <dest>`` (spec 7.1/7.2): pre the folder exists LOCALLY
    (not remote, not absent) and ``<dest>/<stub>`` does not already exist.
    Relocate the folder and rewrite every task_list reference to the old path
    (project-relative form). ``dest`` is a location root (``tmp`` or
    ``dev/tasks``); the stub is preserved."""
    if dest not in resolve.KNOWN_ROOTS:
        raise StateOpError(
            f"unknown dest {dest!r} (expected one of: "
            + ", ".join(resolve.KNOWN_ROOTS)
            + ")"
        )
    resolved = _resolve(ref, project_root)
    host = local_host if local_host is not None else resolve.short_hostname()
    if (
        resolved.location == resolve.LOCATION_TMP
        and ref_host is not None
        and ref_host != host
    ):
        raise StateOpError(
            f"{resolved.canonical} is remote (host {ref_host!r}); a remote "
            "task cannot be moved locally (spec 7.3)"
        )
    old_folder = resolved.folder(project_root)
    if not old_folder.is_dir():
        raise StateOpError(
            f"{resolved.canonical}: no local task folder -- move requires "
            "an existing local folder"
        )
    new_canonical = f"{dest}/{resolved.stub}"
    if new_canonical == resolved.canonical:
        raise StateOpError(f"{resolved.canonical} is already in {dest}")
    new_folder = project_root / new_canonical
    if new_folder.exists():
        raise StateOpError(
            f"destination {new_canonical} already exists -- move refuses"
        )

    new_folder.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old_folder), str(new_folder))

    rewritten = _rewrite_references(
        project_root, resolved.canonical, new_canonical
    )

    return MoveResult(
        old_canonical=resolved.canonical,
        new_canonical=new_canonical,
        folder=new_folder.resolve(),
        rewritten_docs=tuple(rewritten),
    )
