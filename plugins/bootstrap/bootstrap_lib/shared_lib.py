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

For the full set of supported ways a consumer can reach a published shared
library -- another plugin, the standalone Python above, or a project running
its own interpreter -- see
plugins/bootstrap/skills/bootstrap/references/library-consumption.md.

Stdlib-only. Functions return ``SharedLibResult`` so the engine can map outcomes to
its logging discipline (cached -> log_ok; published/linked -> action log; skipped ->
log; failed -> action log + failure).
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from typing import NamedTuple, Optional

from .atomic_write import write_atomic


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
        dirnames[:] = sorted(name for name in dirnames if name != "__pycache__")
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


def _read_raw(path: str) -> Optional[str]:
    """Like ``_read_text`` but without the whitespace strip.

    Used to capture a ``.pth``'s exact prior bytes before an overwrite, so a
    rollback can restore precisely what was there rather than a normalized
    approximation of it.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _rollback_pth(pth: str, prior_raw: Optional[str]) -> str:
    """Undo a ``.pth`` write whose import verification failed.

    Restores ``prior_raw`` exactly if a ``.pth`` existed before the write, or
    removes ``pth`` if there was none -- so a link that never verified is
    never left looking like one that did (the cache check earlier in
    ``link_shared_lib`` would otherwise match it forever). Returns a clause
    for the caller's failure message. A rollback failure is reported, never
    swallowed: a bare "failed" reads identically whether or not the broken
    ``.pth`` is still installed, and only this message tells a reader which.
    """
    try:
        if prior_raw is None:
            try:
                os.remove(pth)
            except FileNotFoundError:
                pass
        else:
            write_atomic(pth, prior_raw)
    except OSError as e:
        return (
            f"rollback FAILED ({e}): {pth} still holds the unverified link; "
            "remove it by hand and re-run bootstrap"
        )
    return (
        "rolled back to the prior link" if prior_raw is not None
        else "removed the unverified link"
    )


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


def sync_shared_lib(
    name: str,
    src: str,
    plugin_root: str,
    shared_root: str,
    verify_python: Optional[str] = None,
) -> SharedLibResult:
    """Publish an owner package's SOURCE to the shared location.

    Syncs ``<plugin_root>/<src>/<name>/`` -> ``<shared_root>/<name>/<name>/`` with a
    clean re-sync so renamed/deleted modules are pruned. Staging happens beside
    the live package and the completed directory is swapped into place. Skips
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

    stage_root = None
    try:
        os.makedirs(entry_dir, exist_ok=True)
        stage_root = tempfile.mkdtemp(prefix=".stage-", dir=entry_dir)
        stage_pkg = os.path.join(stage_root, name)
        shutil.copytree(
            src_pkg,
            stage_pkg,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__"),
        )

        if verify_python is not None:
            try:
                proc = subprocess.run(
                    [
                        verify_python,
                        "-c",
                        f"import sys; sys.path.insert(0, {stage_root!r}); import {name}",
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return SharedLibResult(
                    name, "failed", f"{name} failed to import from the published copy: {exc}"
                )
            if proc.returncode != 0:
                first_stderr_line = (proc.stderr or "").splitlines()[0] if proc.stderr else "import failed"
                return SharedLibResult(
                    name,
                    "failed",
                    f"{name} failed to import from the published copy: {first_stderr_line}",
                )

        _swap_directory(stage_pkg, dest_pkg)
        shutil.rmtree(stage_root, ignore_errors=True)
        stage_root = None
        with open(hash_file, "w", encoding="utf-8") as f:
            f.write(current + "\n")
    except OSError as exc:
        return SharedLibResult(name, "failed", f"failed to publish {name}: {exc}")
    finally:
        if stage_root is not None:
            shutil.rmtree(stage_root, ignore_errors=True)
    return SharedLibResult(name, "published", f"synced -> {dest_pkg}")


def _swap_directory(staged: str, destination: str) -> None:
    """Install a staged directory while retaining the old tree on failure."""
    try:
        os.replace(staged, destination)
        return
    except OSError:
        if not os.path.isdir(destination):
            raise

    backup = f"{destination}.old-{uuid.uuid4().hex}"
    os.replace(destination, backup)
    try:
        os.replace(staged, destination)
    except OSError:
        try:
            os.replace(backup, destination)
        except OSError:
            pass
        raise
    shutil.rmtree(backup, ignore_errors=True)


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

    # Capture the prior .pth exactly (or None if there was none) BEFORE the
    # write below. Verification cannot run before the write -- the .pth is
    # what makes `import <name>` resolve in the first place -- so this is the
    # only point where "prior state" can be captured for a rollback.
    prior_raw = _read_raw(pth)

    # Atomic: a .pth is read by site.py at EVERY interpreter start, so a
    # truncated write is not a transient state -- an incomplete executable line
    # is a SyntaxError that site.addpackage prints on every startup until the
    # next pass rewrites it. mkstemp + os.replace never exposes a partial file.
    try:
        write_atomic(pth, desired + "\n")
    except OSError as e:
        return SharedLibResult(name, "failed", f"failed to write {pth}: {e}")

    if _verify_import(python, name):
        return SharedLibResult(name, "linked", f"linked -> {pth}")

    # A never-verified link must not be left in place: the cache check above
    # reads exactly what was just written and would report "cached" on every
    # later pass, so a link that never worked would never be retried. Roll
    # back to the prior .pth (or remove it if there was none) so the next
    # pass's cache check misses and the link is attempted again.
    rollback_note = _rollback_pth(pth, prior_raw)
    return SharedLibResult(
        name, "failed",
        f"wrote {pth} but `import {name}` still fails; {rollback_note}",
    )
