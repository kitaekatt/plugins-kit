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
- **A git-IGNORED folder is neither of those cases** (added 2026-08-08). Git
  is present and can see the folder, but is configured never to carry it, so
  the commits cannot succeed -- ``git add`` refuses an ignored path -- and no
  later commit can either. "Version control is the record, therefore the
  folder may go" simply does not hold, so archive records the final state and
  KEEPS the folder (``vcs_ignored``), reporting that ``delete`` is
  unrecoverable there. This is a supported configuration, not a
  misconfiguration: a project may deliberately gitignore its task root to
  keep task folders local scratch. Previously this crashed mid-write, after
  the final-state writes and before the commit that justified them.
- **archive's pre-commit writes are rolled back when the commit fails.** The
  final state has to exist ON DISK to be committed, so the writes precede the
  git phase; a failure there would otherwise leave a log line asserting the
  folder was committed and removed, in a folder that is still present and
  still unrecorded. After the final-state commit succeeds there is nothing to
  undo -- the record is in history and a later failure leaves a recoverable
  folder.
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
from .validate import git_ignores_path, git_vcs_state


@dataclass(frozen=True)
class ArchiveResult:
    canonical: str
    folder_removed: bool  # non-tmp in git: True (VC is the record); else False
    archived_to: str | None = None  # tmp: parking path (project-relative)
    # Non-tmp, outside any git repo: final state recorded, folder KEPT --
    # submission to the workspace's VCS (and the finishing delete) is the
    # agent/user's to do.
    vcs_pending: bool = False
    # Non-tmp, inside a git repo that IGNORES the folder: no commit is
    # possible and none ever will be, so the final state is recorded and the
    # folder KEPT. Distinct from vcs_pending -- there is nothing to submit,
    # and `delete` destroys the folder with no version-control copy.
    vcs_ignored: bool = False
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
    inside a repo (or git is unavailable/fails).

    ``folder`` is resolved first: git itself prints a resolved (symlink- and
    junction-free) toplevel, so resolving here keeps ``folder`` consistent
    with that toplevel for every downstream pathspec-limited call (see
    ``_unheld_files_in``, ``_git_commit_folder``, ``_snapshot_index``,
    ``_restore_index``) -- an unresolved folder handed to git as a pathspec
    against the resolved toplevel is reported as outside the repository."""
    folder = folder.resolve()
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


def _git_commit_folder(
    repo_root: Path, folder: Path, message: str, *, tracked_only: bool = False
) -> None:
    """Stage and commit exactly the task folder's changes (adds, edits, AND
    deletions). Pathspec-limited commit: pre-staged unrelated index content
    is never swept in. Raises StateOpError on any git failure.

    ``tracked_only`` swaps ``add -A`` for ``add -u`` -- required when the
    folder is git-IGNORED but holds force-added tracked files: ``-A`` refuses
    an ignored pathspec outright, while ``-u`` stages exactly the changes to
    what git already tracks. That is also the right semantic, not just the
    working one: an ignored folder's NEW files were deliberately excluded,
    and archive must not quietly start tracking them.

    Both ``repo_root`` and ``folder`` are resolved first, so a task folder
    reached through a symlink/junction is never handed to git as a pathspec
    against a differently-spelled (but identical) repo root -- see
    ``_git_toplevel``."""
    repo_root = repo_root.resolve()
    folder = folder.resolve()
    add = ["add", "-u"] if tracked_only else ["add", "-A"]
    for cmd in (
        ["git", "-C", str(repo_root), *add, "--", str(folder)],
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


#: The documents archive writes BEFORE it can commit them. The final state
#: has to exist on disk to be committed, so these writes necessarily precede
#: the git phase -- which makes them the thing to undo when git then fails.
_ARCHIVE_WRITTEN_DOCS = ("task.yaml", "log.md")


def _snapshot_docs(folder: Path) -> dict[str, bytes | None]:
    """Byte snapshot of the documents archive is about to rewrite (None for
    one that does not exist yet)."""
    snap: dict[str, bytes | None] = {}
    for name in _ARCHIVE_WRITTEN_DOCS:
        path = folder / name
        try:
            snap[name] = path.read_bytes()
        except OSError:
            snap[name] = None
    return snap


def _unheld_files_in(repo_root: Path, folder: Path) -> list[str]:
    """Files inside the folder that git's ignore rules EXCLUDE -- i.e. the
    ones ``git add`` refuses, so no commit will ever carry them.

    This is the safety predicate for removing the folder. ``git add -A``
    stages every non-ignored file, so after the final-state commit the only
    content still outside version control is exactly this set. If it is
    non-empty, removing the folder destroys files that exist in no commit
    and are recoverable from nowhere.

    It deliberately generalizes the fully-ignored case: a folder where only
    SOME files were force-added reports ``clean`` from git_vcs_state (the
    porcelain is quiet and something IS tracked), yet its remaining files are
    just as unrecoverable.

    Both ``repo_root`` and ``folder`` are resolved first -- see
    ``_git_toplevel``."""
    repo_root = repo_root.resolve()
    folder = folder.resolve()
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "--",
                str(folder),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Cannot prove the folder is fully held -> assume it is not. Failing
        # closed keeps a folder; failing open destroys one.
        return ["<git could not enumerate ignored files>"]
    if proc.returncode != 0:
        return ["<git could not enumerate ignored files>"]
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _snapshot_index(repo_root: Path, folder: Path) -> str | None:
    """The folder's staged index entries (``git ls-files --stage`` format),
    or None when git cannot report them.

    Restoring the working tree is not enough to undo a failed archive:
    ``git add`` succeeds independently of ``git commit``, so a commit that
    fails (pre-commit hook, unset identity, signing) leaves the archived
    content STAGED while the tree is reverted -- and the next unrelated
    commit ships it.

    Both ``repo_root`` and ``folder`` are resolved first -- see
    ``_git_toplevel``."""
    repo_root = repo_root.resolve()
    folder = folder.resolve()
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "--stage", "--", str(folder)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _restore_index(repo_root: Path, folder: Path, snapshot: str | None) -> None:
    """Put the folder's index entries back exactly as ``_snapshot_index``
    found them. Pathspec-limited, so another session's staged work outside
    this folder is never touched. Best-effort: a restore that itself fails
    must not replace the original error.

    Both ``repo_root`` and ``folder`` are resolved first -- see
    ``_git_toplevel``."""
    if snapshot is None:
        return
    repo_root = repo_root.resolve()
    folder = folder.resolve()
    try:
        subprocess.run(
            ["git", "-C", str(repo_root), "reset", "-q", "--", str(folder)],
            capture_output=True,
            timeout=30,
        )
        if snapshot.strip():
            subprocess.run(
                ["git", "-C", str(repo_root), "update-index", "--index-info"],
                input=snapshot,
                capture_output=True,
                text=True,
                timeout=30,
            )
    except (OSError, subprocess.TimeoutExpired):
        return


def _restore_docs(folder: Path, snap: dict[str, bytes | None]) -> None:
    """Put the snapshotted documents back. Best-effort: a restore that
    itself fails must not replace the original error, which is the one that
    explains what went wrong."""
    for name, original in snap.items():
        path = folder / name
        try:
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)
        except OSError:
            continue


def archive_task(ref: str, project_root: Path) -> ArchiveResult:
    """``archive <ref>`` (spec 7.1): pre folder exists + stored status
    active. Then per closure policy (spec 2.5, revised): tmp ->
    ``status: archived`` and park the folder at ``tmp/archived-tasks/<stub>``;
    non-tmp in a git repo -> record the final state (status + log entry),
    commit it, remove the folder, commit the removal (version control is the
    record; two pathspec-limited commits); non-tmp outside any git repo ->
    record the final state and KEEP the folder (``vcs_pending``: the agent
    submits it with the workspace's VCS, then runs delete); non-tmp inside a
    git repo that IGNORES the folder -> record the final state and KEEP the
    folder (``vcs_ignored``: no commit is possible, so removal would be
    unrecoverable -- ``delete`` is the user's explicit call)."""
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
    vcs_ignored = False
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
        unheld = _unheld_files_in(repo_root, folder) if repo_root else []
        if repo_root is not None and unheld:
            # Inside a repo, but git's ignore rules exclude some or all of
            # this folder, so no commit will ever carry those files. "Version
            # control is the record, therefore the folder may go" does not
            # hold: removing it would destroy content that exists nowhere
            # else. Record the final state, keep the folder, and say plainly
            # that `delete` is unrecoverable here.
            # This covers BOTH shapes, and the partial one is the dangerous
            # one: when only SOME files were force-added, git_vcs_state reads
            # `clean` (the porcelain is quiet and something IS tracked) and
            # `add -u` SUCCEEDS -- so a check keyed on the fully-ignored case
            # would sail past here and rmtree the untracked remainder.
            # NB: `repo_root` is deliberately left set. It records a fact that
            # is true (we ARE in a repo); routing is `vcs_ignored`'s job.
            # Clearing it here would read as vcs_pending -- "submit it with
            # your VCS" -- which is precisely the wrong advice for a folder
            # no VCS will ever accept.
            vcs_ignored = True
        data["task"]["status"] = "archived"
        pre_write = _snapshot_docs(folder)
        _write_task_yaml(folder, data)
        if vcs_ignored:
            shown = ", ".join(unheld[:3]) + (
                f" (+{len(unheld) - 3} more)" if len(unheld) > 3 else ""
            )
            _append_archive_log_entry(
                folder,
                "final state recorded; folder KEPT -- git is configured to "
                f"ignore {len(unheld)} file(s) here ({shown}), so version "
                "control holds no copy of them (delete removes them "
                "permanently)",
            )
            removed = False
        elif repo_root is None:
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
            # The final state must be ON DISK to be committed, so these
            # writes necessarily precede the commit that validates them. If
            # the commit then fails, the writes are a LIE the failure leaves
            # behind -- a log line asserting the folder was committed and
            # removed, in a folder that is still there and still unrecorded.
            # Undo them, so a failed archive is a no-op rather than a folder
            # whose own history denies its existence.
            _append_archive_log_entry(
                folder,
                "final state committed; folder removed (version control "
                "is the record)",
            )
            # Ignored-but-tracked (force-added): git IS the record for what
            # it already carries, so the normal path runs -- with `-u`, the
            # only staging mode git permits on an ignored pathspec.
            tracked_only = git_ignores_path(folder)
            # `git add` succeeds independently of `git commit`, so the index
            # is part of what a failed archive must undo -- restoring only
            # the working tree leaves the archived content STAGED, and the
            # next unrelated commit ships it.
            pre_index = _snapshot_index(repo_root, folder_resolved)
            try:
                _git_commit_folder(
                    repo_root,
                    folder_resolved,
                    f"task archive: {resolved.canonical} (final state)",
                    tracked_only=tracked_only,
                )
            except StateOpError:
                _restore_docs(folder, pre_write)
                _restore_index(repo_root, folder_resolved, pre_index)
                raise
            # Past this point the final state IS in git history, so the
            # writes are no longer unbacked and there is nothing to undo:
            # a failure of the removal commit leaves a recoverable folder.
            shutil.rmtree(folder)
            _git_commit_folder(
                repo_root,
                folder_resolved,
                f"task archive: {resolved.canonical} (remove folder; "
                "version control is the record)",
                tracked_only=tracked_only,
            )
            removed = True
    return ArchiveResult(
        canonical=resolved.canonical,
        folder_removed=removed,
        archived_to=archived_to,
        vcs_pending=vcs_pending,
        vcs_ignored=vcs_ignored,
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
