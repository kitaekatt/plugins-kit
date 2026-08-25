"""bootstrap_guard.py -- detect whether the bootstrap plugin has provisioned a
plugin, and fail gracefully (with an actionable message) when it has not.

CANONICAL SOURCE. This module is **vendored** (copied byte-for-byte) into each
plugin that needs a runtime bootstrap-presence guard, the same way path_repair.py
is vendored. A drift test asserts the copies match this canonical.

CRITICAL CONSTRAINT: this module must be **stdlib-only** and must **never import
bootstrap_lib** -- the whole point is to run when bootstrap (and therefore
bootstrap_lib) may be absent. The vendored copies live next to the script that
imports them (e.g. `<plugin>/scripts/bootstrap_guard.py`) and are imported as a
plain top-level module (`from bootstrap_guard import require_bootstrap`), NOT via
the bootstrap_lib package.

Detection signal: bootstrap writes a per-plugin log at
`~/.claude/plugins/data/<marketplace>/<plugin>/bootstrap.log` the first time its
engine processes that plugin (see root CLAUDE.md: "if the log doesn't exist,
bootstrap never reached that plugin"). Its absence is the cheap, reliable proxy
for "the bootstrap plugin has not run for this plugin" -- whether because the
bootstrap plugin is not installed at all, or is installed but never provisioned
this plugin.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

EXIT_BOOTSTRAP_MISSING = 3


def data_dir(plugin: str, marketplace: str = "plugins-kit") -> Path:
    """The per-plugin bootstrap data directory.

    CLAUDE_BOOTSTRAP_DATA_ROOT redirects the whole tree for one session (set by
    the claude-plugin-test launcher). Honoring it here is load-bearing rather
    than cosmetic: reexec_under_plugin_venv resolves the venv through this
    function, so a guard that ignored the redirect would re-exec every
    shared-lib script into the PRODUCTION venv inside a test session -- running
    the installed release's code while appearing to test the working copy.
    """
    root = os.environ.get("CLAUDE_BOOTSTRAP_DATA_ROOT")
    base = Path(root) if root else Path(os.path.expanduser("~")) / ".claude" / "plugins" / "data"
    return base / marketplace / plugin


def is_provisioned(plugin: str, marketplace: str = "plugins-kit") -> bool:
    """True if bootstrap appears to have run for this plugin at least once."""
    return (data_dir(plugin, marketplace) / "bootstrap.log").exists()


def message(plugin: str, marketplace: str = "plugins-kit", feature: str | None = None,
            missing: str | None = None) -> str:
    """The canonical, actionable bootstrap-absence message."""
    what = f"{plugin}'s setup" if not feature else f"{plugin}'s {feature}"
    miss = f" (missing: {missing})" if missing else ""
    return (
        f"[{plugin}] the '{marketplace}:bootstrap' plugin has not provisioned "
        f"{what}{miss}. Install/enable the bootstrap plugin and start a new "
        f"session so it can build this plugin's dependencies, then retry."
    )


def require_bootstrap(plugin: str, marketplace: str = "plugins-kit",
                      feature: str | None = None, missing: str | None = None,
                      force: bool = False) -> None:
    """Exit with the canonical message if bootstrap has not provisioned this plugin.

    Pass force=True to emit the message unconditionally -- use this in an
    `except ImportError` handler around a bootstrap_lib import, where the failed
    import is itself proof bootstrap did not provision the venv.
    """
    if force or not is_provisioned(plugin, marketplace):
        print(message(plugin, marketplace, feature, missing), file=sys.stderr)
        sys.exit(EXIT_BOOTSTRAP_MISSING)


def plugin_venv_python(plugin: str, marketplace: str = "plugins-kit") -> Path | None:
    """Path to the interpreter inside the plugin's bootstrap-provisioned venv.

    Bootstrap creates a dedicated venv at `<data_dir>/.venv` and links shared
    libraries (e.g. bootstrap_lib) onto it via a `.pth` file. A script that
    needs those shared libs must run under THIS interpreter -- a bare `python`
    or `uv run` lands in a different environment that has no such `.pth`.

    Returns None when the venv (or its interpreter) does not exist.
    """
    venv = data_dir(plugin, marketplace) / ".venv"
    for rel in (("Scripts", "python.exe"), ("bin", "python"), ("bin", "python3")):
        candidate = venv.joinpath(*rel)
        if candidate.is_file():
            return candidate
    return None


# Env flag set across the re-exec boundary so a missing/already-active venv can
# never cause an exec loop.
_REEXEC_GUARD_ENV = "_BOOTSTRAP_GUARD_VENV_REEXEC"


def _is_windows() -> bool:
    """Platform test, behind a seam so tests can exercise both branches.

    Deliberately NOT an inline `os.name == "nt"`: pathlib reads `os.name` at
    call time to decide between PosixPath and WindowsPath, so monkeypatching
    it to cover the Windows branch on a POSIX runner breaks every Path() in
    this module. A function is the only substitutable surface.
    """
    return os.name == "nt"


def reexec_under_plugin_venv(plugin: str, marketplace: str = "plugins-kit") -> None:
    """Re-exec the current process under the plugin's provisioned venv if needed.

    Call this at the TOP of a script that imports a bootstrap-provisioned shared
    lib (e.g. `bootstrap_lib`), BEFORE the import:

        from bootstrap_guard import reexec_under_plugin_venv
        reexec_under_plugin_venv("p4-kit")
        from bootstrap_lib... import ...   # now resolvable

    Plugins define their own venv and should run under it preferentially. A
    script launched by a bare `python` or `uv run` would otherwise miss the
    shared-lib `.pth` and fail the import even though bootstrap provisioned the
    venv correctly -- surfacing a misleading "not provisioned" error.

    No-op when already running under the provisioned venv, so it is safe to call
    unconditionally. Stdlib-only; loop-guarded via an env flag. If the venv
    cannot be located, returns quietly and lets the caller's normal
    `require_bootstrap()` guard report the genuine absence.
    """
    if os.environ.get(_REEXEC_GUARD_ENV):
        return  # already re-exec'd once in this process tree
    target = plugin_venv_python(plugin, marketplace)
    if target is None:
        return  # not provisioned; let require_bootstrap() report it
    # "Already in the venv" is decided by sys.prefix, NOT by comparing
    # interpreter paths. uv builds `.venv/bin/python` as a SYMLINK to the base
    # interpreter, so resolving both sides collapses them to the same file and
    # the comparison returns a false positive -- and it does so precisely when
    # the caller is the standalone python that every plugin's launcher shim
    # uses, i.e. in the common case rather than a corner one. The process then
    # continues under the BASE interpreter with none of the venv's
    # site-packages, and the provisioned dependency is reported missing even
    # though bootstrap installed it correctly: the exact misleading
    # "not provisioned" error this function exists to prevent.
    #
    # sys.prefix is the authoritative signal. Python sets it from the path the
    # interpreter was INVOKED by (PEP 405 finds pyvenv.cfg next to argv[0]
    # without resolving symlinks), so it says which environment is actually
    # active rather than which file is executing.
    try:
        if Path(sys.prefix).resolve() == target.parent.parent.resolve():
            return  # already running inside the provisioned venv
    except OSError:
        return
    os.environ[_REEXEC_GUARD_ENV] = "1"
    # Windows has no exec. CPython implements os.execv there over the CRT
    # _execv, which SPAWNS the replacement and terminates the caller
    # immediately: the parent returns exit 0 before the child has done any
    # work, and the child is orphaned rather than inherited by the launcher.
    # A caller that reads our stdout therefore sees an empty stream and a
    # success code, while the child writes its real output moments later into
    # a pipe nobody is reading any more. Observed as: prepare_review.py exits 0
    # with no JSON on stdout, having correctly written bundle.json to disk.
    # Spawn and WAIT instead, propagating the child's exit code.
    if _is_windows():
        import subprocess

        completed = subprocess.run([str(target), *sys.argv])
        sys.exit(completed.returncode)
    os.execv(str(target), [str(target), *sys.argv])
