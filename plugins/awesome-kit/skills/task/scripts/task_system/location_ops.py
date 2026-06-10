"""Location ops -- ``archive`` / ``delete`` / ``move`` (spec sections 7.1,
7.2, 7.4).

The destructive + location verbs live in their own module (CCP): they change
for closure-policy / location reasons (spec 2.5 closure_policy, 7.4
locations), not for the status/pointer reasons state_ops changes for. They
reuse state_ops' shared internals (resolution, task.yaml read-modify-write,
pointer clearing) and its StateOpError -- one error type across all verbs, so
the CLI's error rendering is uniform.

Readings chosen in Step 5 (flagged in the implementation report):

- **archive precondition is the STORED status** (``status: active`` in
  task.yaml), matching close's reading. A ``closed`` task errors with a
  "reopen first" hint (spec 7.1); any other non-active status also errors.
- **Uncommitted-archive guard** (spec 7.4): a non-tmp folder that
  ``validate.is_uncommitted`` reads as uncommitted -- which includes "not in
  a git repo at all" (no git record exists) -- REFUSES with a "commit first;
  git is the record" error. No auto-commit (the user owns the commit). The
  guard shares validate's exact predicate, so the validate warning and the
  archive refusal can never diverge.
- **delete inherits BOTH archive's status-active precondition and the
  uncommitted guard** (spec 7.1: delete is "``archive`` semantics, then
  ensure the folder is removed even when tmp"). So a closed task must be
  reopened before delete, and an uncommitted dev/tasks folder refuses delete
  exactly as it refuses archive.
- **delete skips the intermediate tmp status write.** For a tmp folder,
  archive-then-remove would write ``status: archived`` into a folder removed
  moments later -- an unobservable intermediate state. delete goes straight
  to removal; the net post-state (folder gone, pointer cleared) is identical.
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
  every ``*.md`` under the project root (discovery's project document set)
  is scanned. Fenced YAML blocks are located span-accurately with the same
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

import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from skills_kit_lib.document_walker import YAML_BLOCK_RE

from . import pointer as pointer_mod
from . import resolve
from .state_ops import (
    StateOpError,
    _clear_pointer_if_names,
    _read_task_yaml,
    _resolve,
    _write_task_yaml,
)
from .validate import is_uncommitted


@dataclass(frozen=True)
class ArchiveResult:
    canonical: str
    folder_removed: bool  # non-tmp: True (git is the record); tmp: False


@dataclass(frozen=True)
class MoveResult:
    old_canonical: str
    new_canonical: str
    folder: Path  # absolute new location
    rewritten_docs: tuple[Path, ...]  # documents whose references were rewritten


# --- archive / delete --------------------------------------------------------


def _archive_preflight(
    ref: str, project_root: Path, verb: str
) -> tuple[resolve.ResolvedRef, Path, dict]:
    """The shared archive/delete preconditions + guard (module docstring):
    folder exists, stored status active (closed -> reopen-first hint),
    non-tmp uncommitted -> refuse."""
    resolved = _resolve(ref, project_root)
    folder = resolved.folder(project_root)
    if not folder.is_dir():
        raise StateOpError(
            f"{resolved.canonical}: no task folder -- {verb} requires an "
            "existing folder"
        )
    data = _read_task_yaml(folder, resolved.canonical)
    stored_status = data["task"].get("status")
    if stored_status != "active":
        hint = (
            " -- reopen it first" if stored_status == "closed" else ""
        )
        raise StateOpError(
            f"{verb} acts on an active task; {resolved.canonical} has "
            f"status {stored_status!r}{hint}"
        )
    if resolved.location == resolve.LOCATION_DEV_TASKS and is_uncommitted(folder):
        raise StateOpError(
            f"{resolved.canonical} has uncommitted changes (or no git repo "
            f"holds it) -- commit first; git is the record ({verb} refuses, "
            "spec 7.4; no auto-commit)"
        )
    return resolved, folder, data


def archive_task(
    ref: str, project_root: Path, pointer_path: Path
) -> ArchiveResult:
    """``archive <ref>`` (spec 7.1): pre folder exists + stored status
    active; non-tmp uncommitted refuses. Then per closure policy (spec 2.5):
    tmp -> ``status: archived``, keep folder; non-tmp -> delete the folder
    (git is the record). Clear the pointer iff it names this task."""
    resolved, folder, data = _archive_preflight(ref, project_root, "archive")
    folder_resolved = folder.resolve()
    if resolved.location == resolve.LOCATION_TMP:
        data["task"]["status"] = "archived"
        _write_task_yaml(folder, data)
        removed = False
    else:
        shutil.rmtree(folder)
        removed = True
    _clear_pointer_if_names(pointer_path, folder_resolved)
    return ArchiveResult(canonical=resolved.canonical, folder_removed=removed)


def delete_task(ref: str, project_root: Path, pointer_path: Path) -> str:
    """``delete <ref>`` (spec 7.1): archive semantics -- same precondition
    and uncommitted guard (module docstring) -- then remove the folder even
    when tmp (unconditional). Clear the pointer iff it names this task.
    Returns the canonical id."""
    resolved, folder, _ = _archive_preflight(ref, project_root, "delete")
    folder_resolved = folder.resolve()
    shutil.rmtree(folder)
    _clear_pointer_if_names(pointer_path, folder_resolved)
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
    across the project document set (all *.md under the project root, the
    same set discovery's project scope scans). Returns the docs rewritten."""
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
    pointer_path: Path,
    *,
    ref_host: str | None = None,
    local_host: str | None = None,
) -> MoveResult:
    """``move <ref> <dest>`` (spec 7.1/7.2): pre the folder exists LOCALLY
    (not remote, not absent) and ``<dest>/<stub>`` does not already exist.
    Relocate the folder, rewrite every task_list reference to the old path
    (project-relative form), and update the pointer iff it named the old
    path. ``dest`` is a location root (``tmp`` or ``dev/tasks``); the stub
    is preserved."""
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

    old_folder_resolved = old_folder.resolve()
    new_folder.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old_folder), str(new_folder))

    rewritten = _rewrite_references(
        project_root, resolved.canonical, new_canonical
    )

    stored = pointer_mod.read_current(pointer_path)
    if stored is not None and Path(stored) == old_folder_resolved:
        pointer_mod.write_current(pointer_path, new_folder)

    return MoveResult(
        old_canonical=resolved.canonical,
        new_canonical=new_canonical,
        folder=new_folder.resolve(),
        rewritten_docs=tuple(rewritten),
    )
