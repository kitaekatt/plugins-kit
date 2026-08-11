"""Shared-library publishing + linking for the bootstrap engine.

The ``shared_libs`` capability lets an owner plugin publish a first-party Python
package to a stable, version-independent location, and lets consuming plugins
import it WITHOUT declaring a dependency on the owner plugin (reuse-by-availability).

Mechanism (a generalization of llm-scripting-kit's B1 prototype):

- The owner declares ``shared_libs: [{ "name": <pkg>, "src": <dir> }]`` in its
  bootstrap.json. The engine syncs ``<plugin_root>/<src>/<pkg>/`` to a per-lib,
  version-independent path-entry dir::

      <shared_root>/<pkg>/<pkg>/     # the package itself
      <shared_root>/<pkg>/.src.sha256  # content hash for skip-caching

  and registers a ``<pkg>.pth`` (pointing at ``<shared_root>/<pkg>/``) on the
  standalone Python so any process using that interpreter can ``import <pkg>``.

- A consumer declares ``shared_lib_imports: ["<pkg>", ...]``; the engine writes the
  same ``<pkg>.pth`` into THAT plugin's own venv. The per-lib path-entry dir means
  the ``.pth`` exposes only that one package (opt-in isolation).

This module shares first-party SOURCE only. Third-party deps the package needs
(e.g. ``openai`` for ``llm_scripting_kit``) are the importing plugin's own concern,
declared in its ``pyproject.toml`` -- NOT installed here. A separate static test
(tests/bootstrap/test_dependency_completeness.py) catches missing declarations.

Stdlib-only. Functions return ``SharedLibResult`` so the engine can map outcomes to
its logging discipline (cached -> log_ok; published/linked -> action log; skipped ->
log; failed -> action log + failure).
"""

import hashlib
import os
import shutil
import subprocess
import sys
from typing import NamedTuple, Optional


class SharedLibResult(NamedTuple):
    name: str
    status: str   # "cached" | "published" | "linked" | "skipped" | "failed"
    message: str


def find_standalone_python() -> Optional[str]:
    """Locate the bootstrap-managed standalone Python (the shared interpreter).

    Returns the interpreter path, or None if it is not present yet.
    """
    base = os.path.join(
        os.path.expanduser("~"), ".local", "share", "python-standalone", "python"
    )
    candidate = (
        os.path.join(base, "python.exe")
        if sys.platform == "win32"
        else os.path.join(base, "bin", "python3")
    )
    return candidate if os.path.exists(candidate) else None


def purelib_of(python: str) -> Optional[str]:
    """Return the site-packages (purelib) dir of the given interpreter, or None."""
    try:
        proc = subprocess.run(
            [python, "-c", "import sysconfig;print(sysconfig.get_path('purelib'))"],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _hash_tree(root: str) -> str:
    """Deterministic content hash of every file under ``root`` (relpath + bytes).

    Captures additions, deletions, renames, and content changes -- so the owner
    sync can both skip when unchanged and prune stale modules when it does run.
    """
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fname in sorted(filenames):
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            try:
                with open(full, "rb") as f:
                    h.update(f.read())
            except OSError:
                h.update(b"UNREADABLE")
            h.update(b"\0")
    return h.hexdigest()


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def _verify_import(python: str, name: str) -> bool:
    """Return True if ``import <name>`` succeeds under ``python``."""
    try:
        proc = subprocess.run(
            [python, "-c", f"import {name}"],
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def sync_shared_lib(name: str, src: str, plugin_root: str, shared_root: str) -> SharedLibResult:
    """Publish an owner package's SOURCE to the shared location.

    Syncs ``<plugin_root>/<src>/<name>/`` -> ``<shared_root>/<name>/<name>/`` with a
    clean re-sync (remove-then-copy) so renamed/deleted modules are pruned. Skips
    when the source tree hash is unchanged.

    Returns "published" (synced), "cached" (unchanged), or "failed" (no source).
    """
    src_pkg = os.path.join(plugin_root, src, name)
    if not os.path.isdir(src_pkg):
        return SharedLibResult(name, "failed", f"shared-lib source not found: {src_pkg}")

    entry_dir = os.path.join(shared_root, name)
    dest_pkg = os.path.join(entry_dir, name)
    hash_file = os.path.join(entry_dir, ".src.sha256")

    current = _hash_tree(src_pkg)
    if os.path.isdir(dest_pkg) and _read_text(hash_file) == current:
        return SharedLibResult(name, "cached", f"synced (cached, {dest_pkg})")

    os.makedirs(entry_dir, exist_ok=True)
    shutil.rmtree(dest_pkg, ignore_errors=True)
    shutil.copytree(src_pkg, dest_pkg)
    with open(hash_file, "w", encoding="utf-8") as f:
        f.write(current + "\n")
    return SharedLibResult(name, "published", f"synced -> {dest_pkg}")


def link_shared_lib(name: str, python: Optional[str], shared_root: str) -> SharedLibResult:
    """Register ``<name>.pth`` (pointing at ``<shared_root>/<name>/``) on ``python``.

    Used both for the standalone broadcast (owner phase) and for a consumer venv
    (consumer phase) -- the operation is identical. Soft-skips (not a failure) when
    the interpreter or its site-packages can't be resolved, or when the shared lib
    has not been published yet (eventual consistency across the per-plugin loop).

    Returns "cached" (.pth already correct), "linked" (written + import verified),
    "skipped" (interpreter/source not ready), or "failed" (import check failed).
    """
    entry_dir = os.path.join(shared_root, name)
    if not os.path.isdir(os.path.join(entry_dir, name)):
        return SharedLibResult(name, "skipped", f"shared lib {name} not yet published; will retry next session")

    if not python or not os.path.exists(python):
        return SharedLibResult(name, "skipped", f"interpreter not found; skipped linking {name}")

    site = purelib_of(python)
    if site is None:
        return SharedLibResult(name, "skipped", f"could not resolve site-packages; skipped linking {name}")

    pth = os.path.join(site, f"{name}.pth")
    # Executable .pth that PREPENDS the shared dir to sys.path. A plain-path .pth
    # only APPENDS (after this interpreter's own site-packages), so a stale
    # pip-installed copy of <name> sitting in site-packages -- e.g. left over from
    # a former `bootstrap @ git+` dependency that uv sync didn't prune -- would
    # shadow the shared copy. Prepending makes the shared copy authoritative (the
    # single source of truth) regardless of any such leftover. site.py executes
    # .pth lines that begin with "import".
    desired = 'import sys; sys.path.insert(0, r"%s")' % entry_dir
    if _read_text(pth) == desired:
        return SharedLibResult(name, "cached", f"linked (cached, {pth})")

    try:
        with open(pth, "w", encoding="utf-8") as f:
            f.write(desired + "\n")
    except OSError as e:
        return SharedLibResult(name, "failed", f"failed to write {pth}: {e}")

    if not _verify_import(python, name):
        return SharedLibResult(name, "failed", f"wrote {pth} but `import {name}` still fails")
    return SharedLibResult(name, "linked", f"linked -> {pth}")
