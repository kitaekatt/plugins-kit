"""Make a settings file writable before a FOREIGN writer touches it.

`claude plugin install|uninstall|update --scope project` rewrites
``<project>/.claude/settings.json`` with an atomic tmp+rename. On Windows a
Perforce-controlled file is read-only on disk unless it is open for edit, and
Windows refuses to rename over a read-only destination -- so the CLI dies with

    EPERM: operation not permitted, rename '...settings.json.tmp.NNNN' -> '...settings.json'

and bootstrap surfaces an unactionable fix-all item on every single session.

Bootstrap already clears the read-only bit for its OWN writes (``_legacy_replace``
in engine.py). This module extends that competence to writes performed by a
subprocess we invoke, which we cannot retry from the inside -- the file has to be
writable BEFORE the CLI runs.

Two behaviours worth knowing about:

* ``p4 edit`` is preferred over a bare ``chmod`` -- but ONLY inside a Perforce
  workspace, detected from a local marker file before any p4 process is
  spawned. Where Perforce does apply, clearing the read-only bit alone leaves
  the file writable-but-not-opened, and the next ``p4 sync`` then refuses to
  clobber it -- the workspace silently stops receiving teammates' settings
  updates. ``p4 edit`` makes it writable AND puts it in a pending changelist
  the user can see and submit. Everywhere else, chmod is the whole answer.
* ``preserve_line_endings`` exists because the CLI reserialises the whole file
  (LF endings, keys reordered). On a CRLF working copy that turns a two-line
  semantic change into a whole-file diff -- unmergeable for teammates who have
  the same shared file open. We restore the original dominant ending so the
  diff stays the size of the actual change.

Nothing here raises: a failure to make the file writable degrades to the
pre-existing behaviour (the CLI fails and bootstrap reports it).
"""

import os
import re
import shutil
import stat
import subprocess
from contextlib import contextmanager
from typing import NamedTuple, Optional

from .path_check import _home

_P4_TIMEOUT = 10

# Files that mark a Perforce workspace. P4CONFIG/P4IGNORE name them, and both
# sit at the workspace root, so finding one at or above a path means the tree
# is Perforce-managed -- checked before any p4 process is spawned.
_P4_MARKERS = (".p4config.txt", ".p4config", ".p4ignore.txt", ".p4ignore")

# Marker used to find (and reuse) the pending changelist bootstrap parks its
# settings edits in, so repeated passes don't accumulate one CL each.
_CL_MARKER = "[claude-bootstrap] Claude Code settings maintained by plugin bootstrap"
_CHANGE_LINE_RE = re.compile(r"^Change (\d+) on ")
_CHANGE_CREATED_RE = re.compile(r"Change (\d+) created")


class WritableResult(NamedTuple):
    ok: bool
    method: str  # absent | already-writable | p4-edit | chmod | failed
    detail: str


def settings_path_for_scope(scope: str, project_dir: Optional[str]) -> Optional[str]:
    """Return the settings file `claude plugin --scope <scope>` will write.

    Mirrors the resolution in ``check_plugin_enabled_at_scope``. Returns None
    when the scope has no single well-known target (or project_dir is missing),
    in which case the caller simply skips the writability guard.
    """
    home = _home()
    if scope == "user":
        return os.path.join(home, ".claude", "settings.json")
    if scope == "project" and project_dir:
        return os.path.join(project_dir, ".claude", "settings.json")
    if scope == "local" and project_dir:
        return os.path.join(project_dir, ".claude", "settings.local.json")
    return None


def _is_read_only(path: str) -> bool:
    try:
        return not (os.stat(path).st_mode & stat.S_IWRITE)
    except OSError:
        return False


def _in_p4_workspace(path: str) -> bool:
    """True if a Perforce workspace marker sits at or above `path`.

    Gate for touching p4 AT ALL. Without it, every read-only file on every
    machine would spawn `p4 fstat` -- pointless in a git checkout, slow where
    p4 is installed but unconfigured, and actively misleading where an ambient
    P4CLIENT points somewhere unrelated to this file. A marker file is a local,
    zero-cost answer to "is this tree even Perforce?".
    """
    directory = os.path.dirname(os.path.abspath(path))
    while True:
        for marker in _P4_MARKERS:
            if os.path.exists(os.path.join(directory, marker)):
                return True
        parent = os.path.dirname(directory)
        if parent == directory:
            return False
        directory = parent


def _p4_tracked(path: str) -> bool:
    """True if `p4 fstat` recognises this path as a depot file in the client view.

    The workspace marker says the TREE is Perforce; this says the FILE is
    actually under depot control (a p4ignored or newly added file is not, and
    wants a plain chmod instead).
    """
    if not _in_p4_workspace(path) or shutil.which("p4") is None:
        return False
    proc = _p4(["fstat", path], os.path.dirname(path))
    return proc is not None and proc.returncode == 0 and "depotFile" in (proc.stdout or "")


def _p4(args, cwd: Optional[str], input: Optional[str] = None):
    """Run `p4 <args>`; return the CompletedProcess, or None if p4 is unusable.

    p4 locates .p4config.txt from the ``PWD`` environment variable when one is
    set, IN PREFERENCE to the process's actual working directory. Git Bash
    exports PWD, and bootstrap runs from hooks launched by it -- so without
    realigning PWD here, every p4 call resolves against the shell's directory
    instead of `cwd`, silently reports "must create client '<hostname>'", and
    the caller concludes the file simply isn't under Perforce control.
    """
    env = dict(os.environ)
    if cwd:
        env["PWD"] = cwd
    try:
        return subprocess.run(
            ["p4", *args],
            input=input,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=cwd or None,
            env=env,
            timeout=_P4_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError):
        return None


def _find_or_make_changelist(cwd: str) -> Optional[str]:
    """Return a pending CL number to park bootstrap's settings edits in.

    Files left in the DEFAULT changelist are ungrouped and easy to sweep into
    an unrelated submit, so bootstrap keeps its own described CL and reuses it
    across sessions (matched by _CL_MARKER) rather than creating one per pass.
    Returns None if p4 can't tell us -- the caller then falls back to a plain
    `p4 edit`, which is still better than leaving the file read-only.
    """
    listed = _p4(["changes", "-s", "pending", "-l", "-c", os.environ.get("P4CLIENT", "")], cwd) \
        if os.environ.get("P4CLIENT") else _p4(["changes", "-s", "pending", "-l"], cwd)
    if listed is not None and listed.returncode == 0:
        current = None
        for line in (listed.stdout or "").splitlines():
            match = _CHANGE_LINE_RE.match(line)
            if match:
                current = match.group(1)
            elif current and _CL_MARKER in line:
                return current

    info = _p4(["info"], cwd)
    if info is None or info.returncode != 0:
        return None
    fields = {}
    for line in (info.stdout or "").splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    client, user = fields.get("Client name", ""), fields.get("User name", "")
    if not client or not user:
        return None

    # Deliberately NO Files: section -- including one would sweep every file
    # currently open in the default changelist into this new CL.
    spec = (
        "Change: new\n"
        f"Client: {client}\n"
        f"User: {user}\n"
        "Status: new\n"
        "Description:\n"
        f"\t{_CL_MARKER}\n"
        "\n"
        "\tClaude Code plugin bootstrap opened this file to record plugin\n"
        "\tenablement declared in .claude/bootstrap.json. Review and submit,\n"
        "\tor revert if the change is not wanted.\n"
    )
    created = _p4(["change", "-i"], cwd, input=spec)
    if created is None or created.returncode != 0:
        return None
    match = _CHANGE_CREATED_RE.search(created.stdout or "")
    return match.group(1) if match else None


def _p4_edit(path: str) -> bool:
    """Open `path` for edit. True only if the file is actually writable after."""
    cwd = os.path.dirname(path) or None
    changelist = _find_or_make_changelist(cwd)
    args = ["edit", "-c", changelist, path] if changelist else ["edit", path]
    proc = _p4(args, cwd)
    if proc is None:
        return False
    # p4 can exit 0 having done nothing useful (e.g. "not on client"), so the
    # writability of the file -- not the exit code -- is the authority.
    return proc.returncode == 0 and not _is_read_only(path)


def ensure_writable(path: Optional[str]) -> WritableResult:
    """Best-effort: leave `path` writable so a foreign writer can replace it."""
    if not path:
        return WritableResult(True, "absent", "")
    if not os.path.isfile(path):
        # The CLI creates it; the parent directory governs that write.
        return WritableResult(True, "absent", "")
    if not _is_read_only(path):
        return WritableResult(True, "already-writable", "")

    if _p4_tracked(path):
        if _p4_edit(path):
            return WritableResult(True, "p4-edit", "opened for edit in Perforce")
        return WritableResult(
            False, "failed", "Perforce edit failed; file remains read-only",
        )

    try:
        current_mode = os.stat(path).st_mode
        os.chmod(path, current_mode | stat.S_IWRITE)
    except OSError as exc:
        return WritableResult(False, "failed", f"could not clear read-only bit: {exc}")
    if _is_read_only(path):
        return WritableResult(False, "failed", "read-only bit survived chmod")
    return WritableResult(True, "chmod", "cleared read-only bit (not tracked by Perforce)")


def _dominant_eol(data: bytes) -> Optional[bytes]:
    """Return b'\\r\\n' or b'\\n' -- whichever this file mostly uses; None if no newlines."""
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    if crlf == 0 and lf == 0:
        return None
    return b"\r\n" if crlf >= lf else b"\n"


def _rewrite_eol(path: str, want: bytes) -> None:
    with open(path, "rb") as f:
        data = f.read()
    normalized = data.replace(b"\r\n", b"\n")
    if want == b"\r\n":
        normalized = normalized.replace(b"\n", b"\r\n")
    if normalized != data:
        with open(path, "wb") as f:
            f.write(normalized)


@contextmanager
def preserve_line_endings(path: Optional[str]):
    """Restore `path`'s original dominant line ending if the body rewrites it.

    The Claude CLI reserialises settings.json with LF. On a CRLF checkout that
    makes every line of a shared, source-controlled file show as modified.
    Never raises -- a failure here just leaves the CLI's formatting in place.
    """
    original = None
    if path and os.path.isfile(path):
        try:
            with open(path, "rb") as f:
                original = _dominant_eol(f.read())
        except OSError:
            original = None
    try:
        yield
    finally:
        if original is not None and os.path.isfile(path):
            try:
                with open(path, "rb") as f:
                    if _dominant_eol(f.read()) != original:
                        _rewrite_eol(path, original)
            except OSError:
                pass
