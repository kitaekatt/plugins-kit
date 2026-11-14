"""Bootstrap check: project-declared links to directories outside the project.

A project declares links in the ``links`` list of its TRACKED project manifest,
``<project>/.claude/bootstrap.json``::

    {"links": [{"path": "dev/tasks", "target": "$DEVROOT/tasks/plugins-kit"}]}

Bootstrap then makes ``<project>/<path>`` a directory symlink (an NTFS
junction on Windows when a symlink cannot be created) to ``target``. The case
it exists for is a Git worktree: ``dev/tasks`` is an ignored link into a
private repository, so a fresh checkout or worktree has no such link until
something makes it.

Safety rules, each one a status below rather than a silent skip:

- PROVENANCE. Only the project layer file is read, directly, by
  :func:`read_project_links`. A ``links`` key in the user layer, the
  ``.local`` layers, or a plugin manifest is never consulted: the merged
  manifest cannot say which layer a value came from, and a link target is
  authority over where the project's files land.
- RELATIVE, CONTAINED PATHS. ``path`` is a relative POSIX path with no empty,
  ``.`` or ``..`` component and no ``.git`` component. Every existing parent
  directory must be a real directory (no symlink, no junction), so a link can
  only ever be created inside the project.
- IGNORED ONLY. Bootstrap never creates a link at a path that
  ``git check-ignore`` does not report as ignored: a link that git sees as
  untracked content could be committed by the next ``git add -A``.
- NOTHING IS REPLACED. A wrong target or a non-link at the path is reported
  and left alone.
- DUPLICATES ARE REJECTED. Two entries naming the same path (compared case
  insensitively), or one path inside another, make every entry involved
  ``invalid``.

This module returns structured outcomes only; the exact log text lives in the
engine, like every other bootstrap check.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import dataclass, field

from .env_features import expand_env_path

MANIFEST_REL = os.path.join(".claude", "bootstrap.json")
LINKS_KEY = "links"

_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
_SUBPROCESS_TIMEOUT = 15.0

# WinError for an unelevated symlink attempt without SeCreateSymbolicLinkPrivilege.
_WINERROR_PRIVILEGE_NOT_HELD = 1314


# ---------------------------------------------------------------------------
# Declarations: read from the project layer only, then validate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkDeclaration:
    """A validated declaration: ``path`` is POSIX-relative, ``target`` absolute."""

    path: str
    target: str


@dataclass(frozen=True)
class LinkCheck:
    """Outcome of checking one declaration. Never carries a side effect.

    ``status`` is one of:

    - ``ok_same``        -- the path is already a link to the declared target.
    - ``fixable``        -- the path is absent, ignored, and the target exists.
    - ``wrong_target``   -- the path is a link to somewhere else; not replaced.
    - ``not_link``       -- the path exists and is not a link; not replaced.
    - ``target_missing`` -- the target is not an existing directory.
    - ``not_ignored``    -- git does not ignore the path (or could not say).
    - ``invalid``        -- the declaration, a parent component, or the path
      cannot be used.

    ``path``/``target`` are the declared values (``target`` is empty when it
    could not be expanded); ``detail`` is a reason string where relevant.
    """

    path: str
    status: str
    target: str = ""
    detail: str = ""


@dataclass(frozen=True)
class LinksRead:
    """What the project layer declares.

    ``status`` is ``absent`` (no manifest file), ``unreadable`` (the file does
    not parse as a JSON object; the engine already reports that as a manifest
    parse failure), ``no_links`` (the file has no ``links`` key) or ``ok``.
    ``declarations`` holds the usable entries, ``invalid`` one ``invalid``
    :class:`LinkCheck` per rejected entry. ``links_value`` is the raw
    ``links`` value of the project layer (``None`` when absent), kept so the
    engine can tell whether the merged manifest carries a ``links`` value that
    did not come from this layer.
    """

    status: str
    declarations: tuple = ()
    invalid: tuple = field(default_factory=tuple)
    detail: str = ""
    links_value: object = None


def read_project_links(project_dir):
    """Read and validate ``links`` from ``<project>/.claude/bootstrap.json`` only."""
    manifest_path = os.path.join(project_dir, MANIFEST_REL)
    if not os.path.isfile(manifest_path):
        return LinksRead("absent")
    try:
        with open(manifest_path, "r") as f:
            manifest = json.load(f)
    except (OSError, ValueError) as exc:
        return LinksRead("unreadable", detail=str(exc))
    if not isinstance(manifest, dict):
        return LinksRead("unreadable", detail="manifest is not a JSON object")
    if LINKS_KEY not in manifest or manifest[LINKS_KEY] is None:
        return LinksRead("no_links")

    raw = manifest[LINKS_KEY]
    if not isinstance(raw, list):
        return LinksRead(
            "ok", links_value=raw,
            invalid=(LinkCheck(
                LINKS_KEY, "invalid",
                detail="links must be a list of {path, target} objects, got %s"
                       % type(raw).__name__),))

    parsed = []   # (index, declaration)
    invalid = []
    for index, item in enumerate(raw):
        decl, problem = _validate_entry(item)
        if problem:
            invalid.append(LinkCheck(_entry_label(item, index), "invalid", detail=problem))
        else:
            parsed.append(decl)

    usable, rejected = _reject_duplicates(parsed)
    invalid.extend(rejected)
    return LinksRead("ok", declarations=tuple(usable), invalid=tuple(invalid),
                     links_value=raw)


def _entry_label(item, index):
    if isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"]:
        return item["path"]
    return "%s[%d]" % (LINKS_KEY, index)


def _validate_entry(item):
    """Returns (LinkDeclaration, None) or (None, problem)."""
    if not isinstance(item, dict):
        return None, "entry must be an object with path and target, got %s" % type(item).__name__
    extra = sorted(set(item) - {"path", "target"})
    if extra:
        return None, "unknown key(s) %s" % extra
    path, target = item.get("path"), item.get("target")
    if not isinstance(path, str) or not path:
        return None, "path must be a non-empty string"
    if not isinstance(target, str) or not target:
        return None, "target must be a non-empty string"

    problem = _path_problem(path)
    if problem:
        return None, problem

    try:
        expanded = expand_env_path(target)
    except ValueError as exc:
        return None, str(exc)
    if not os.path.isabs(expanded):
        return None, "target %r is not absolute after expansion (%r)" % (target, expanded)
    return LinkDeclaration(path, os.path.normpath(expanded)), None


def _path_problem(path):
    if "\0" in path or "\\" in path or ":" in path:
        return "path %r must be a plain POSIX relative path (no backslash, colon or NUL)" % path
    if path.startswith("/"):
        return "path %r must be relative" % path
    for part in path.split("/"):
        if part in ("", ".", ".."):
            return "path %r must not contain an empty, '.' or '..' component" % path
        if part.casefold() == ".git":
            return "path %r must not contain a .git component" % path
    return None


def _reject_duplicates(declarations):
    """Split into (usable, invalid checks): same path or nested path -> all invalid."""
    keys = [d.path.casefold() for d in declarations]
    bad = set()
    for i, a in enumerate(keys):
        for j in range(i + 1, len(keys)):
            b = keys[j]
            if a == b:
                bad.update((i, j))
            elif b.startswith(a + "/") or a.startswith(b + "/"):
                bad.update((i, j))
    usable = [d for i, d in enumerate(declarations) if i not in bad]
    rejected = [
        LinkCheck(d.path, "invalid", target=d.target,
                  detail="duplicate or nested path among the declared links")
        for i, d in enumerate(declarations) if i in bad
    ]
    return usable, rejected


# ---------------------------------------------------------------------------
# Side-effect-free check
# ---------------------------------------------------------------------------


def check_project_link(project_dir, decl):
    """The side-effect-free check for one validated declaration."""
    parts = decl.path.split("/")
    link_path = os.path.join(project_dir, *parts)

    parent_problem = _parent_problem(project_dir, parts[:-1])
    if parent_problem:
        return LinkCheck(decl.path, "invalid", decl.target, parent_problem)

    try:
        st = os.lstat(link_path)
    except FileNotFoundError:
        st = None
    except OSError as exc:
        return LinkCheck(decl.path, "invalid", decl.target, "cannot stat path: %s" % exc)

    if st is not None:
        if not _is_link_like(link_path, st):
            return LinkCheck(decl.path, "not_link", decl.target,
                             "path exists and is not a link; left untouched")
        if _same_location(link_path, decl.target):
            return LinkCheck(decl.path, "ok_same", decl.target)
        return LinkCheck(
            decl.path, "wrong_target", decl.target,
            "path links to %s, not the declared target; left untouched"
            % os.path.realpath(link_path))

    ignored, ignore_detail = _git_ignored(project_dir, decl.path)
    if not ignored:
        return LinkCheck(decl.path, "not_ignored", decl.target, ignore_detail)
    if not os.path.isdir(decl.target):
        return LinkCheck(decl.path, "target_missing", decl.target,
                         "target is not an existing directory")
    inside = _containment_problem(project_dir, decl.target)
    if inside:
        return LinkCheck(decl.path, "invalid", decl.target, inside)
    return LinkCheck(decl.path, "fixable", decl.target)


def _is_link_like(path, st):
    if stat.S_ISLNK(st.st_mode):
        return True
    if os.name == "nt":
        try:
            from pathlib import Path
            return Path(path).is_junction()
        except OSError:
            return False
    return False


def _same_location(link_path, target):
    return (os.path.normcase(os.path.realpath(link_path))
            == os.path.normcase(os.path.realpath(target)))


def _parent_problem(project_dir, parent_parts):
    """None when every EXISTING parent is a real directory inside the project."""
    root_real = os.path.normcase(os.path.realpath(project_dir))
    current = project_dir
    for part in parent_parts:
        current = os.path.join(current, part)
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            return None   # this and every deeper component is absent
        except OSError as exc:
            return "cannot stat parent %s: %s" % (part, exc)
        if _is_link_like(current, st):
            return "parent component %r is a link; refusing to create beneath it" % part
        if not stat.S_ISDIR(st.st_mode):
            return "parent component %r is not a directory" % part
        real = os.path.normcase(os.path.realpath(current))
        if real != root_real and not real.startswith(root_real + os.sep):
            return "parent component %r resolves outside the project" % part
    return None


def _containment_problem(project_dir, target):
    root_real = os.path.normcase(os.path.realpath(project_dir))
    target_real = os.path.normcase(os.path.realpath(target))
    for outer, inner, what in ((root_real, target_real, "inside the project"),
                               (target_real, root_real, "an ancestor of the project")):
        if inner == outer or inner.startswith(outer + os.sep):
            return "target is %s; a link must point outside the project" % what
    return None


def _git_ignored(project_dir, rel_posix):
    """Returns (ignored, detail). Anything but a clean 'ignored' answer is refusal."""
    try:
        proc = subprocess.run(
            ["git", "-C", project_dir, "check-ignore", "-q", "--", rel_posix],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=_GIT_ENV, timeout=_SUBPROCESS_TIMEOUT, text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "cannot ask git whether the path is ignored: %s" % exc
    if proc.returncode == 0:
        return True, ""
    if proc.returncode == 1:
        return False, "git does not ignore %s; add it to .gitignore first" % rel_posix
    stderr = (proc.stderr or "").strip()
    return False, "git check-ignore failed (exit %s): %s" % (proc.returncode, stderr)


# ---------------------------------------------------------------------------
# Fixer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkFixResult:
    """Outcome of :func:`create_project_link`.

    ``status`` is one of: ``created``, ``not_fixable`` (the authoritative
    re-check no longer says fixable; ``detail`` names why), ``mkdir_failed``,
    ``race_existing``, ``link_failed``, ``verify_error``.
    """

    ok: bool
    status: str
    mechanism: str = ""
    detail: str = ""


def create_project_link(project_dir, decl):
    """Create the missing parents and the link. Removes only what it created."""
    recheck = check_project_link(project_dir, decl)
    if recheck.status != "fixable":
        return LinkFixResult(False, "not_fixable",
                             detail="%s: %s" % (recheck.status, recheck.detail))

    parts = decl.path.split("/")
    link_path = os.path.join(project_dir, *parts)
    created_dirs = []

    def _rollback():
        for directory in reversed(created_dirs):
            try:
                os.rmdir(directory)
            except OSError:
                pass

    current = project_dir
    for part in parts[:-1]:
        current = os.path.join(current, part)
        if os.path.lexists(current):
            continue
        try:
            os.mkdir(current)
        except OSError as exc:
            _rollback()
            return LinkFixResult(False, "mkdir_failed", detail=str(exc))
        created_dirs.append(current)

    # Parents may have been swapped for links between check and mkdir.
    problem = _parent_problem(project_dir, parts[:-1])
    if problem:
        _rollback()
        return LinkFixResult(False, "mkdir_failed", detail=problem)

    if os.path.lexists(link_path):
        _rollback()
        return LinkFixResult(False, "race_existing")

    ok, mechanism, detail = _create_link(decl.target, link_path)
    if not ok:
        if os.path.lexists(link_path):
            return LinkFixResult(False, "race_existing")
        _rollback()
        return LinkFixResult(False, "link_failed", detail=detail)

    if not _same_location(link_path, decl.target):
        try:
            _remove_link_artifact(link_path)
        except OSError:
            pass
        _rollback()
        return LinkFixResult(False, "verify_error", mechanism=mechanism,
                             detail="created link does not resolve to the target")
    return LinkFixResult(True, "created", mechanism=mechanism)


def _remove_link_artifact(link_path):
    if os.path.islink(link_path):
        os.unlink(link_path)
        return
    if os.name == "nt":
        from pathlib import Path
        if Path(link_path).is_junction():
            os.rmdir(link_path)   # removes the reparse point, not the target
            return
    raise OSError("refusing to remove %s: not a link" % link_path)


def _create_link(target, link_path):
    """Returns (ok, mechanism, detail). Mirrors agent_skills_check._create_link."""
    try:
        os.symlink(target, link_path, target_is_directory=True)
        return True, "directory symlink", ""
    except OSError as exc:
        symlink_error = exc

    if os.name != "nt":
        return False, "", str(symlink_error)

    # Junction fallback only for the privilege error; any other failure is real.
    if getattr(symlink_error, "winerror", None) != _WINERROR_PRIVILEGE_NOT_HELD:
        return False, "", str(symlink_error)
    try:
        import _winapi
        create_junction = getattr(_winapi, "CreateJunction", None)
    except ImportError:
        create_junction = None
    if create_junction is None:
        return False, "", (
            "symlink failed (%s); junction fallback unavailable" % symlink_error)
    try:
        create_junction(os.path.abspath(target), link_path)
        return True, "NTFS junction", ""
    except OSError as junction_error:
        try:
            if os.path.lexists(link_path):
                _remove_link_artifact(link_path)
        except OSError:
            pass
        return False, "", "symlink failed (%s); junction failed (%s)" % (
            symlink_error, junction_error)
