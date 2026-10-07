"""Cheap copies of expensive git trees, for the secrets-kit suite.

A plain uniquely-named module (NOT conftest): `from conftest import ...`
resolves to whichever suite's conftest.py lands first on sys.path when several
test directories run in one pytest invocation. See tests/workflow-kit/wk_testlib.py
for the same reasoning.

Most of this suite's wall time was `git` process startup in FIXTURES, not in
the code under test: `git init` + two `git config` costs ~170ms, and a bare
init + clone + seed commit + push costs ~1.4s -- several files paid that per
test. Building each tree ONCE and handing out a filesystem copy is ~20x
cheaper (~8ms) and changes nothing about what a test then does to it: every
test still gets its own independent, real, on-disk git repository.

Isolation: a template is built once per PROCESS by the session-scoped
`git_template` fixture in conftest.py, and is never handed to a test -- only
`copy_git_tree` results are. Under pytest-xdist each worker is a separate
process with its own `tmp_path_factory` basetemp, so N workers build N private
templates at N distinct paths; no path and no write is shared.
"""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

# Files inside .git that can embed an absolute path to the tree's own location
# (a clone's origin URL, most importantly). Relocating a copy means rewriting
# them, which git itself would otherwise do via `git remote set-url`.
_PATH_BEARING = ("config", "FETCH_HEAD", "ORIG_HEAD")


def _forms(path: Path):
    """Every spelling of `path` that can appear in a git metadata file, by kind.

    `.git/config` C-escapes its backslashes, so a Windows clone URL is written
    `C:\\\\Users\\\\...` -- matching only the unescaped form leaves the copy
    pointing at the template, which is exactly the shared-state bug this module
    exists to avoid.
    """
    raw = str(path)
    return {
        "raw": raw,
        "forward": raw.replace("\\", "/"),
        "escaped": raw.replace("\\", "\\\\"),
    }


def _rewrite_paths(path: Path, old: Path, new: Path) -> None:
    """Repoint any absolute reference to `old` at `new`, in place."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    # Each spelling is replaced by the SAME spelling of the copy destination.
    # secrets-kit binds a clone to its exact recorded origin string, so a copy
    # whose origin was respelled (backslashes to forward slashes) no longer
    # matches the `str(remote)` a fixture declares in secrets.json.
    old_forms, new_forms = _forms(old), _forms(new)
    updated = text
    for kind in sorted(old_forms, key=lambda k: len(old_forms[k]), reverse=True):
        updated = updated.replace(old_forms[kind], new_forms[kind])
    if updated != text:
        path.write_text(updated, encoding="utf-8")


def copy_git_tree(template: Path, dest: Path) -> Path:
    """Copy a prebuilt git tree to `dest` and repoint its absolute paths.

    The copy is a real, independent git repository (or tree of them): nothing
    is shared with the template or with any other copy.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template, dest, dirs_exist_ok=True)
    # A bare repo has no .git subdirectory -- it IS one -- so match by suffix
    # too, and include the destination root for the bare-template case.
    git_dirs = [p for p in dest.rglob("*.git") if p.is_dir()]
    git_dirs += [p for p in dest.rglob(".git") if p.is_dir()]
    git_dirs.append(dest)
    for git_dir in git_dirs:
        for name in _PATH_BEARING:
            candidate = git_dir / name
            if candidate.is_file():
                _rewrite_paths(candidate, template, dest)
        hooks = git_dir / "hooks"
        if hooks.is_dir():
            for hook in hooks.iterdir():
                if hook.is_file():
                    _rewrite_paths(hook, template, dest)
    _assert_detached(template, dest)
    return dest


def _assert_detached(template: Path, dest: Path) -> None:
    """Fail loudly if the copy still refers to the template.

    A missed rewrite does not error -- it makes the copy operate on the
    template (pushing into its remote, say), so tests silently share state and
    pass or fail depending on order. Cheap to check, unbounded to debug.
    """
    forms = _forms(template)
    for candidate in dest.rglob("*"):
        if not candidate.is_file() or candidate.name not in _PATH_BEARING:
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for form in forms.values():
            if form in text:
                raise AssertionError(
                    f"{candidate} still points at the template ({form!r}); "
                    "copies would share state. Teach _rewrite_paths this form."
                )


def is_owner_only_file(path: Path) -> bool:
    """Whether a file is private: mode 0600 on POSIX, an owner-only DACL on Windows.

    Windows modes are decorative (a writable file always reads 0o666), so the
    platform's real control is checked there, with the operation guard's own
    DACL reader.
    """
    if sys.platform.startswith("win"):
        from secrets_kit.operation_lock import OperationLockSetupError, _windows_private

        try:
            _windows_private(path, directory=False)
        except OperationLockSetupError:
            return False
        return True
    return stat.S_IMODE(path.stat().st_mode) == 0o600


def assert_owner_only_file(path: Path) -> None:
    """Fail unless `path` is private by `is_owner_only_file`."""
    assert is_owner_only_file(path), f"{path} is not owner-only"


def grant_everyone(path: Path, rights: str) -> None:
    """Windows analog of a group/world permission bit: a foreign DACL grant.

    ``rights`` is an icacls permission spec, e.g. ``(R)`` for a file or
    ``(OI)(CI)M`` for a directory.
    """
    proc = subprocess.run(["icacls", str(path), "/grant", "*S-1-1-0:" + rights],
                          capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# Seed and rotation write the identity cache in text mode (authoring
# `_finalize_cache`, `text=True`), so the cache they leave uses os.linesep.
AUTHORED_NEW_IDENTITY_CACHE = ("dummy new identity" + os.linesep).encode()

# unlock publishes age's stdout bytes unchanged (LF), so on Windows its cache
# differs from the CRLF digest seed/rotation recorded and reconciliation refuses
# it. Strict: this goes red once the two writers agree.
UNLOCK_CACHE_NEWLINE_GAP = pytest.mark.xfail(
    sys.platform.startswith("win"),
    strict=True,
    reason="Windows gap: seed/rotation record a CRLF text-mode identity-cache digest, "
    "unlock writes age's LF bytes, so reconciliation after unlock refuses the cache "
    "as not compatible -- owner decision pending",
)
