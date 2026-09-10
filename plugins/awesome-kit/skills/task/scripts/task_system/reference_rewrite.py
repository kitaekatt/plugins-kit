"""``move`` (spec 7.1/7.2) and its task_list reference rewrite.

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

import shutil
from pathlib import Path

import yaml

from skills_kit_lib.document_walker import YAML_BLOCK_RE

from . import resolve
from .location_ops import MoveResult
from .state_ops import StateOpError, _resolve


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
