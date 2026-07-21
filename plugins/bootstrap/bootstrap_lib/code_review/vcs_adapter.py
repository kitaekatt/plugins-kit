"""The de-facto VCS-adapter interface behind the shared review pipeline.

`pipeline.assemble_bundle` / `split_sections` are VCS-neutral: they take a raw
diff, a header-parsing callable, and a list of per-file dicts, and know nothing
about git or Perforce. The knowledge of *how to produce those inputs* lives in
each kit's ``scripts/prepare_review.py`` front-half:

    git-kit/scripts/prepare_review.py   (git ranges, `git diff`, quote-aware headers)
    p4-kit/scripts/prepare_review.py    (p4 changelists, `p4 describe`, ==== headers)

Those two front-halves already implement the SAME conceptual interface -- they
just do it as module-level functions rather than a class. This module writes
that interface down as a `typing.Protocol` so the contract is documented,
greppable, and stable, and so a future third VCS adapter has a checklist to
implement against.

FORMALIZATION ONLY -- nothing in this module runs at review time. The existing
front-halves conform *by shape*, not by inheritance: they are collections of
functions, not `VcsAdapter` subclasses, and are deliberately left that way (a
`Protocol` needs no runtime base class, and restructuring working prep scripts
into classes would be churn for no behavioural gain). Each front-half carries a
comment pointing here. If a front-half is ever refactored into a class, it can
declare `VcsAdapter` conformance for a static check at no cost.

--------------------------------------------------------------------------------
Method-to-implementation map (read this as the spec):

    VcsAdapter method          git-kit front-half            p4-kit front-half
    -----------------          ------------------            -----------------
    workspace_root             get_repo_root                 get_workspace_root
    resolve_target             detect_default_range /        (CL number from arg
                               parse_range_arg               / pending-CL prompt)
    fetch_diff                 fetch_diff                    extract_diff (over
                                                             fetch_describe)
    parse_header               _parse_git_header             _parse_p4_header
    diff_to_sections           _git_diff_to_sections         _p4_diff_to_sections
    enumerate_changed_files    fetch_changed_files           parse_file_actions +
                                                             resolve_local_paths
    hygiene_unincluded         find_untracked_or_unstaged    find_unreconciled
    hygiene_unresolved         find_merge_conflicts          find_unresolved

Optional capability (p4 implements; git does not):

    snapshot_change            (not implemented -- git has     auto_shelve_cl +
                                nothing to snapshot; commits    fetch_shelf_fingerprint
                                are already durable)
    cleanup                    (not implemented)              cleanup_auto_shelve

The optional pair models Perforce's auto-shelve: a pending CL with no shelved
content is not diffable, so p4-kit shelves it to fetch the diff, records a
fingerprint, and deletes the shelf afterwards iff it still matches. Git needs
no equivalent -- the range is always diffable from committed/indexed state -- so
a git adapter legitimately omits both. They are OPTIONAL, not mandatory: a
consumer must feature-detect (``hasattr`` / ``is not None``) before calling.
--------------------------------------------------------------------------------
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from bootstrap_lib.code_review.chunking import DiffSection


@runtime_checkable
class VcsAdapter(Protocol):
    """The front-half a code-review kit provides to the shared pipeline.

    A conforming adapter turns a user-supplied target (a git range/ref, a
    Perforce CL number) into the four inputs the shared back-half consumes:
    a raw diff, a header parser, a list of vendor-neutral diff sections, and
    a list of per-file dicts (each carrying at least ``identifier`` and
    ``local``). It also answers the two workspace-hygiene questions the skill
    surfaces before reviewers spawn (files not yet included; files not yet
    resolved).

    All methods are synchronous and side-effect-free EXCEPT the optional
    ``snapshot_change`` (which may mutate VCS state, e.g. create a shelf) and
    its paired ``cleanup``.
    """

    def workspace_root(self) -> Optional[Path]:
        """Absolute path to the workspace/repository root, or None if the cwd
        is not inside one.

        Anchors the CLAUDE.md ancestor walk and submit-gate scope matching in
        ``pipeline.assemble_bundle``. git: `git rev-parse --show-toplevel`.
        p4: `p4 info` -> ``clientRoot``.
        """
        ...

    def resolve_target(self, arg: Optional[str]) -> tuple[str, Optional[str]]:
        """Resolve the user's argument into a canonical target identifier.

        Returns ``(target, reason)`` where ``target`` is an opaque string the
        adapter's own ``fetch_diff`` / ``enumerate_changed_files`` understand
        (a git range spec like ``origin/main..HEAD`` or a sentinel; a p4 CL
        number) and ``reason`` is a human-readable note on how it was chosen
        (git auto-detection reason; None when the target was explicit).

        ``arg`` is None when the user gave no argument: git auto-detects from
        workspace state, p4 lists pending CLs and prompts. Raise ``ValueError``
        when no sensible target exists (git: detached HEAD, no upstream, no
        main/master fallback).
        """
        ...

    def fetch_diff(self, target: str) -> str:
        """Return the raw unified-diff text for ``target``.

        The text is fed to ``diff_to_sections`` / ``split_sections``; it is
        never inlined into the emitted bundle (the pipeline chunks it to disk).
        git: `git diff <range>`. p4: `p4 describe -du` with add/delete hunks
        synthesized via `p4 print` (pure-add / pure-delete CLs have no native
        hunks). Raise ``ValueError`` on a hard fetch failure.
        """
        ...

    def parse_header(self, line: str) -> Optional[dict]:
        """Header matcher passed to ``pipeline.split_sections``.

        Receives one diff line (trailing newline stripped). Returns None for a
        non-header line, or a dict of the kit's identifying fields for a file-
        header line (git: ``{"path": ...}``; p4: ``{"depot", "rev", "type"}``).
        A callable rather than a regex because git's header parsing is quote-
        aware (spaced and C-quoted paths) and cannot be one pattern.
        """
        ...

    def diff_to_sections(self, diff_text: str) -> tuple[str, list[DiffSection]]:
        """Split raw diff text into ``(preamble, sections)``.

        ``sections`` is the vendor-neutral ``[{identifier, text}]`` shape the
        chunker consumes -- ``identifier`` is the chunk-map key (git path /
        depot path) and must equal the matching ``enumerate_changed_files``
        entry's ``identifier``; ``text`` is header+hunks. Typically implemented
        as ``split_sections(diff_text, self.parse_header)`` followed by mapping
        each section onto the neutral shape.
        """
        ...

    def enumerate_changed_files(self, target: str) -> list[dict]:
        """List the files changed by ``target``, one dict each.

        Each dict MUST carry:
          - ``identifier``: the chunk-map key (== the section identifier).
          - ``local``: absolute local path as str, or None/'' when the file
            has no workspace mapping (skips its CLAUDE.md walk).
        Any other keys (git: ``path``, ``status``; p4: ``depot``) pass through
        verbatim into the bundle's ``changed_files`` entries. This is a
        SEPARATE enumeration from the diff parse: files can be changed but
        absent from the diff body (p4 pure-adds in mixed shelved CLs), so the
        canonical file list comes from here, not from the section identifiers.
        """
        ...

    def hygiene_unincluded(
        self, workspace_root: Path, changed_locals: list[str]
    ) -> list[dict]:
        """Files in the touched directories that are NOT yet in the review.

        The skill surfaces these before reviewers spawn so the author can fold
        forgotten work in. git: `git status --porcelain` filtered to the
        touched dirs (untracked / unstaged / staged-uncommitted). p4:
        `p4 reconcile -n` over the minimal covering directory set (adds / edits
        / deletes not yet opened). Best-effort: return ``[]`` on VCS error --
        the review still proceeds.
        """
        ...

    def hygiene_unresolved(self, target: str, workspace_root: Path) -> list[dict]:
        """Files in the change with pending merges/resolves.

        Informational, not findings -- the change is not completable/submittable
        until each is resolved, but the review still renders. git:
        `git ls-files -u` (unmerged paths). p4: `p4 resolve -n -c <CL>`
        (pending content/branch/delete resolves). Best-effort: ``[]`` on error.
        """
        ...

    # -- Optional capability: mutate-then-restore to make the diff fetchable. --
    # A git adapter omits BOTH (its range is always diffable). Consumers must
    # feature-detect before calling; they are not part of the mandatory surface.

    def snapshot_change(self, target: str) -> dict:
        """OPTIONAL. Make ``target`` diffable, returning a restore fingerprint.

        p4-kit only: a pending CL with no shelved content is not diffable, so
        it runs `p4 shelve -c <CL>` and returns the ``{depot: digest}`` shelf
        fingerprint. The paired ``cleanup`` later deletes the shelf iff the
        live fingerprint still matches (never overwriting the author's work).
        A git adapter does not implement this.
        """
        ...

    def cleanup(self, bundle_dir: Path) -> int:
        """OPTIONAL. Undo a prior ``snapshot_change`` for ``bundle_dir``'s target.

        p4-kit only: reads the bundle's recorded fingerprint and, if the live
        shelf still matches exactly, deletes it; any mismatch is a safe no-op.
        Returns a process exit code. A git adapter does not implement this.
        """
        ...
