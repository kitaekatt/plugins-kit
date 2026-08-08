"""The Perforce VcsBackend implementation -- p4-kit's contribution to the seam.

Implements the ``VcsBackend`` protocol (defined in content-pipeline-kit as
``content_pipeline.vcs.seam.VcsBackend``) against a Perforce client workspace,
so ``content_pipeline.deliver.deliver_changeset`` can drive the changeset
choreography under p4 exactly as it does under git. This module imports nothing
from content_pipeline -- conformance is structural.

The mapping (the protocol is shaped around Perforce's pending changelist)
--------------------------------------------------------------------------

The ``VcsBackend`` protocol was designed around a Perforce pending changelist,
so the p4 mapping is the most direct of the backends. Each verb is the p4
operation the seam was abstracted from, with defensive handling for Perforce's
sharp edges:

- **make_changeset == ``p4 change -i`` with a minimal spec, NO ``Files:``
  section.** A brand-new pending CL is created up front from a hand-built spec
  (``Change: new`` + ``Description:`` with tab-prefixed body). Omitting the
  ``Files:`` section is deliberate: including it would sweep every file open in
  the default changelist into the new CL (the ``p4 change -o | ... | p4 change
  -i`` footgun). The ``P4Changeset`` carries the parsed CL number.
- **open_for_edit == ``p4 edit <path>``.** A real per-file checkout-for-edit
  (unlike git, where it is a no-op).
- **add == ``p4 add <path>``.**
- **move_into == ``p4 reopen -c <cl> <path>`` per exact path -- NEVER a
  wildcard, and VERIFIED.** A wildcard reopen (``p4 reopen -c <cl> <path>/...``)
  moves every opened file under the path, including files organized into other
  pending CLs, silently destroying CL organization that is recorded nowhere. A
  path containing ``...`` or ``*`` is rejected outright. Beyond that, ``p4
  reopen`` exits 0 even when it did nothing (file not open for edit) or landed
  the file in the wrong CL, so the stdout diagnostic is parsed -- a move is
  accepted only on ``"reopened"``, ``"currently opened for edit; change
  <this-cl>"``, or ``"nothing changed"`` (the file is already in this CL), and a
  silent no-op / wrong-CL outcome raises ``P4VcsError``
  (ported from ``cl_creation.move_files_to_changelist``'s verification).

P4-specific extensions (beyond the seam)
----------------------------------------

Two methods on :class:`P4Vcs` are NOT part of the ``VcsBackend`` protocol --
they expose Perforce capabilities the seam deliberately does not model, so a
consumer can drop its project-side copies:

- **owning_changeset == ``p4 -ztag opened <path>``** -- which pending CL
  currently holds an opened file (``"default"`` / a number / ``None``). Ports
  the owning-CL query from ``cl_creation.cl_open_for_edit``.
- **describe_changeset == ``p4 change -o <cl>`` + ``p4 -ztag opened -c <cl>``**
  -- the CL's description body and its opened ``clientFile`` list, enough for a
  description-vs-contents assertion. Ports the p4 usage from
  ``cl_creation.cl_assert_matches_description``.
- **finalize_description == dump-edit-restore.** ``p4 change -o <cl>`` dumps the
  full spec (including its auto-populated ``Files:`` section); ONLY the
  ``Description:`` block is replaced with the rebuilt message; ``p4 change -i``
  writes it back. Preserving ``Files:`` verbatim avoids the inverse footgun
  where a hand-written spec with ``Description:`` but no ``Files:`` silently
  moves every file back to the default changelist.
- **revert == ``p4 revert <path>`` per exact path.** Never a wildcard revert
  (``p4 revert //...`` discards work indiscriminately).
- **delete_if_empty == ``p4 change -d <cl>`` when the changeset moved no files.**
  A batch that moved nothing leaves no empty pending CL behind.

Everything routes through an injected ``runner`` seam (``(args, input, cwd) ->
(rc, out, err)``) so tests can drive a scripted fake p4 without spawning, and
this module never hardcodes ``subprocess``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

# A p4 runner: (args, input, cwd) -> (returncode, stdout, stderr). ``args`` is the
# p4 argument vector WITHOUT the leading "p4" (e.g. ["edit", "foo.txt"]); ``input``
# is stdin text for form commands (``p4 change -i``) or None; ``cwd`` is the
# directory to run in (for .p4config discovery) or None.
P4Runner = Callable[[List[str], Optional[str], Optional[str]], Tuple[int, str, str]]

_FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*:")
_CHANGE_CREATED_RE = re.compile(r"Change (\d+) created")


class P4VcsError(RuntimeError):
    """Raised when a p4 command fails non-recoverably (or a wildcard is refused)."""


def _default_runner(
    args: List[str], input: Optional[str] = None, cwd: Optional[str] = None
) -> Tuple[int, str, str]:
    """Run ``p4 <args>`` and return ``(rc, stdout, stderr)``.

    Injected as the ``runner`` seam so a test can substitute a scripted stub;
    the real path spawns ``p4`` with UTF-8 pipes. Imported locally so the module
    loads without ``subprocess`` being touched when a runner is injected.
    """
    import subprocess  # noqa: PLC0415

    proc = subprocess.run(
        ["p4", *args],
        input=input,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _reject_wildcard(path) -> str:
    """Return ``str(path)`` unless it carries a p4 wildcard (``...`` or ``*``).

    The never-wildcard discipline: seam ops touch exactly the paths handed to
    them, one at a time. A wildcard would let a single reopen/revert sweep in
    files the caller never named -- the CL-organization-destroying bug this seam
    was built to avoid.
    """
    s = str(path)
    if "..." in s or "*" in s:
        raise P4VcsError(
            f"refusing wildcard path (never-wildcard discipline): {s!r}"
        )
    return s


def _tab_prefix(description: str) -> str:
    """Return ``description`` with every line tab-prefixed for a p4 spec body.

    P4 forms require description lines indented with a leading tab; blank
    paragraph-separator lines become a lone tab. Mirrors the battle-tested
    ``cl_creation.create_changelist`` handling (``"\\t" + desc.replace("\\n",
    "\\n\\t")``).
    """
    return "\t" + description.replace("\n", "\n\t")


def _replace_description_block(spec: str, new_description: str) -> str:
    """Return ``spec`` with only its ``Description:`` block replaced.

    Everything else -- crucially the ``Files:`` section ``p4 change -o``
    auto-populates -- is preserved verbatim. The old description body (the
    blank-or-tab-indented lines following the ``Description:`` header, up to the
    next top-level ``Field:`` line) is dropped and the new, tab-prefixed body
    inserted in its place.
    """
    lines = spec.split("\n")
    out: List[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("Description:"):
            out.append(line)
            i += 1
            # Consume the existing description body: blank lines (paragraph
            # separators) and tab/space-indented content, up to the next
            # top-level field header (e.g. "Files:") or a comment line.
            while i < n and (lines[i] == "" or lines[i].startswith(("\t", " "))):
                i += 1
            for dl in new_description.split("\n"):
                out.append("\t" + dl)
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _extract_description_block(spec: str) -> str:
    """Return the de-indented body of a p4 spec's ``Description:`` block.

    Inverse of :func:`_replace_description_block`: gathers the tab/space-indented
    (and blank paragraph-separator) lines following the ``Description:`` header
    up to the next top-level ``Field:`` line, strips one leading tab (or space)
    from each, and joins them. Trailing blank lines are trimmed so the round-trip
    with ``_tab_prefix`` is stable. Returns ``""`` when the spec has no
    ``Description:`` block.
    """
    lines = spec.split("\n")
    body: List[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].startswith("Description:"):
            i += 1
            while i < n and (lines[i] == "" or lines[i].startswith(("\t", " "))):
                dl = lines[i]
                if dl.startswith("\t") or dl.startswith(" "):
                    dl = dl[1:]
                body.append(dl)
                i += 1
            break
        i += 1
    while body and body[-1] == "":
        body.pop()
    return "\n".join(body)


@dataclass
class P4Changeset:
    """The Perforce analogue of the seam's changeset: a pending CL + moved paths.

    Unlike git (where the changeset is purely in-memory until commit), a
    ``P4Changeset`` is backed by a real pending changelist created by
    :meth:`P4Vcs.make_changeset`; ``cl`` carries its number. ``paths``
    accumulates the exact paths moved into it (move order, de-duplicated), which
    :meth:`P4Vcs.delete_if_empty` consults to decide whether the CL ended up
    empty.
    """

    description: str = ""
    cl: Optional[str] = None
    paths: List[str] = field(default_factory=list)

    def _add_path(self, path: str) -> None:
        if path not in self.paths:
            self.paths.append(path)


@dataclass
class P4Vcs:
    """``VcsBackend`` over a Perforce client workspace.

    - ``cwd`` -- directory p4 commands run in (for ``.p4config`` discovery);
      ``None`` runs in the process cwd.
    - ``client`` / ``user`` -- values for the ``Client:`` / ``User:`` fields of a
      freshly created CL spec; ``None`` reads ``P4CLIENT`` / ``P4USER`` from the
      environment at spec-build time (matching ``cl_creation``).
    - ``runner`` -- the ``(args, input, cwd) -> (rc, out, err)`` seam (defaults
      to a real ``p4`` subprocess).
    """

    cwd: Optional[Path] = None
    client: Optional[str] = None
    user: Optional[str] = None
    runner: P4Runner = _default_runner

    def _p4(
        self, *args: str, input: Optional[str] = None, check: bool = True
    ) -> Tuple[int, str, str]:
        cwd = str(self.cwd) if self.cwd is not None else None
        rc, out, err = self.runner(list(args), input, cwd)
        if check and rc != 0:
            raise P4VcsError(
                f"p4 {' '.join(args)} failed (exit {rc}): "
                f"{err.strip() or out.strip()}"
            )
        return rc, out, err

    # -- VcsBackend protocol --------------------------------------------------

    def open_for_edit(self, path) -> None:
        """Open ``path`` for edit (``p4 edit <path>``)."""
        self._p4("edit", _reject_wildcard(path))

    def add(self, path) -> None:
        """Add ``path`` to the depot (``p4 add <path>``)."""
        self._p4("add", _reject_wildcard(path))

    def make_changeset(self, description: str) -> P4Changeset:
        """Create a fresh pending changelist and return its :class:`P4Changeset`.

        Builds a minimal spec (``Change: new`` + tab-prefixed ``Description:``,
        deliberately NO ``Files:`` section so no default-CL files are swept in)
        and creates it via ``p4 change -i``. Parses and records the new CL number.
        """
        client = self.client if self.client is not None else os.environ.get("P4CLIENT", "")
        user = self.user if self.user is not None else os.environ.get("P4USER", "")
        spec = (
            "Change: new\n"
            f"Client: {client}\n"
            f"User: {user}\n"
            "Status: pending\n"
            "Description:\n"
            f"{_tab_prefix(description)}\n"
        )
        _rc, out, _err = self._p4("change", "-i", input=spec)
        match = _CHANGE_CREATED_RE.search(out)
        if not match:
            raise P4VcsError(f"could not parse CL number from: {out.strip()!r}")
        return P4Changeset(description=description, cl=match.group(1))

    def move_into(self, changeset: P4Changeset, paths: list) -> None:
        """Reopen each of ``paths`` into ``changeset``'s CL and VERIFY the move.

        Exactly the given paths are reopened, one ``p4 reopen -c <cl> <path>``
        each -- never a wildcard -- so no file organized into another pending CL
        is swept in.

        ``p4 reopen`` exits 0 even when it did NOTHING (the file was not open
        for edit -- ``"... - file(s) not opened on this client"``) and even when
        the file ends up in the WRONG CL, so a bare ``rc == 0`` check is not
        proof the file landed in ``changeset``. The stdout diagnostic is parsed,
        and the move is accepted ONLY when it reports ``"reopened"`` (a real
        move), ``"currently opened for edit;
        change <cl>"`` carrying THIS changeset's number, or ``"<depotFile>#<rev>
        - nothing changed."`` -- the latter two both being idempotent re-moves
        of a file already in this CL, where the desired end state already holds.
        Any other outcome -- a silent no-op, or a "currently opened for edit;
        change <other>" naming a different CL -- raises :class:`P4VcsError`, and
        the path is NOT recorded on the changeset.
        """
        if changeset.cl is None:
            raise P4VcsError("move_into called on a changeset with no CL number")
        cl = changeset.cl
        for path in paths:
            p = _reject_wildcard(path)
            rc, out, err = self._p4("reopen", "-c", cl, p, check=False)
            stdout = out or ""
            reopened = "reopened" in stdout
            already_here = (
                f"currently opened for edit; change {cl}" in stdout
                or "nothing changed" in stdout
            )
            if rc != 0 or not (reopened or already_here):
                reason = (err.strip() or stdout.strip() or "unknown failure")
                raise P4VcsError(
                    f"p4 reopen did not move {p!r} into CL {cl} "
                    f"(exit {rc}): {reason}"
                )
            changeset._add_path(p)

    def finalize_description(
        self, changeset: P4Changeset, description: str
    ) -> Optional[str]:
        """Rewrite the CL's description via dump-edit-restore, preserving ``Files:``.

        ``p4 change -o <cl>`` dumps the full spec; only its ``Description:``
        block is replaced with ``description`` (tab-prefixed); ``p4 change -i``
        writes it back. The auto-populated ``Files:`` section is preserved
        verbatim -- dropping it would move every file back to the default CL.
        Returns the CL number.
        """
        changeset.description = description
        if changeset.cl is None:
            return None
        _rc, spec, _err = self._p4("change", "-o", changeset.cl)
        edited = _replace_description_block(spec, description)
        self._p4("change", "-i", input=edited)
        return changeset.cl

    def revert(self, path) -> None:
        """Revert exactly ``path`` (``p4 revert <path>``). Never a wildcard."""
        self._p4("revert", _reject_wildcard(path))

    def delete_if_empty(self, changeset: P4Changeset) -> None:
        """Delete the pending CL (``p4 change -d <cl>``) when it moved no files."""
        if changeset.cl is not None and not changeset.paths:
            self._p4("change", "-d", changeset.cl)

    # -- P4-specific extensions (BEYOND the VcsBackend protocol) --------------
    #
    # These two methods are NOT part of ``content_pipeline.vcs.seam.VcsBackend``
    # -- they are Perforce capabilities the seam deliberately does not model, so
    # consumers can use these hardened implementations without widening the
    # shared seam; the protocol is unchanged.

    def owning_changeset(self, path) -> Optional[str]:
        """[P4 extension] Return the CL currently holding ``path``, or ``None``.

        Ports the owning-CL query out of ``cl_open_for_edit`` (the ``p4 edit``
        half stays the seam's :meth:`open_for_edit`): runs ``p4 -ztag opened
        <path>`` and reads the ``... change <value>`` field. Returns
        ``"default"`` for the default changelist, the numeric CL string for a
        numbered CL, or ``None`` when the file is not open (``p4 opened`` exits
        non-zero or returns no row). Never a wildcard.

        This surfaces the discrepancy the seam's zero-trusting ``open_for_edit``
        cannot: a file already opened in ANOTHER pending CL is writable, but a
        later ``move_into`` must reopen it -- querying the owner first lets a
        caller warn before mutating on disk.
        """
        p = _reject_wildcard(path)
        rc, out, _err = self._p4("-ztag", "opened", p, check=False)
        if rc != 0:
            return None
        for line in (out or "").splitlines():
            match = re.match(r"\.\.\.\s+change\s+(\S+)", line)
            if match:
                return match.group(1)
        return None

    def describe_changeset(self, cl) -> "P4ChangesetContents":
        """[P4 extension] Retrieve a CL's description and its opened file list.

        Enough to support a description-vs-contents assertion (the drift the
        ``deliver`` finalize step guards against, verified after the fact):
        ``p4 change -o <cl>`` supplies the ground-truth description (its
        ``Description:`` block, de-indented), and ``p4 -ztag opened -c <cl>``
        supplies the ground-truth ``clientFile`` list -- the same p4 usage
        ``cl_assert_matches_description`` inspects. The caller compares the
        returned :class:`P4ChangesetContents` against what it believes it
        finalized; a mismatch means the description claims files the CL does not
        contain (or vice-versa).
        """
        cl_str = str(cl)
        _rc, spec, _err = self._p4("change", "-o", cl_str)
        description = _extract_description_block(spec)
        rc2, out2, _err2 = self._p4("-ztag", "opened", "-c", cl_str, check=False)
        paths: List[str] = []
        if rc2 == 0:
            for line in (out2 or "").splitlines():
                match = re.match(r"\.\.\.\s+clientFile\s+(.+)$", line)
                if match:
                    paths.append(match.group(1).strip())
        return P4ChangesetContents(cl=cl_str, description=description, paths=paths)


@dataclass
class P4ChangesetContents:
    """Ground-truth contents of a pending CL, read back from the server.

    Produced by :meth:`P4Vcs.describe_changeset` for a description-vs-contents
    assertion: ``description`` is the CL's actual ``Description:`` body and
    ``paths`` is the actual opened-file (``clientFile``) list -- neither derived
    from what the caller *believes* it wrote.
    """

    cl: str
    description: str = ""
    paths: List[str] = field(default_factory=list)


__all__ = [
    "P4Vcs",
    "P4Changeset",
    "P4ChangesetContents",
    "P4VcsError",
    "P4Runner",
]
