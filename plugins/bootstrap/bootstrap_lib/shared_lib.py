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

- Generations (no swap under a running process): every publish ALSO lands an
  immutable snapshot beside the stable copy and flips a pointer to it::

      <shared_root>/<pkg>/.generations/<id>/<pkg>/   # one per published content
      <shared_root>/<pkg>/.current                   # the current <id>

  The ``.pth`` is still version-independent -- it names only
  ``<shared_root>/<pkg>/`` -- but at interpreter start it reads ``.current`` and
  puts THAT generation's directory on ``sys.path``. A process therefore keeps
  resolving the generation it started with, including submodules it imports
  lazily after a later publish; only a NEW process sees the new generation.
  Missing or invalid pointer -> the stable ``<shared_root>/<pkg>/`` entry, as
  before. A superseded generation is pruned once it has been superseded for
  ``GENERATION_RETENTION_S``. The stable ``<pkg>/<pkg>/`` copy is still
  swapped in place on every publish: it is the documented location a
  foreign-interpreter consumer reads (library-consumption.md, mode 3).

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
import time
import uuid
from typing import NamedTuple, Optional

from .atomic_write import write_atomic


class SharedLibResult(NamedTuple):
    name: str
    status: str   # "cached" | "published" | "linked" | "skipped" | "failed"
    message: str


# Bump whenever the STAGING mechanism's semantics change materially enough
# that a directory installed under the old mechanism needs one guaranteed
# re-sync (not just a normal content change). Folded into the skip-cache
# stamp below: an old stamp (no "<version>:" prefix, or an older version)
# never matches the freshly computed one, so `sync_shared_lib` re-publishes
# exactly once, then converges normally on the new format. See the
# _make_stage_dir docstring for the version-2 case this exists to repair.
_CACHE_STAMP_VERSION = "2"


def _cache_stamp(content_hash: str) -> str:
    return f"{_CACHE_STAMP_VERSION}:{content_hash}"


# Generation layout inside <shared_root>/<name>/ (see the module docstring).
GENERATIONS_DIR = ".generations"
CURRENT_POINTER = ".current"
SUPERSEDED_MARKER = ".superseded"
# How long a superseded generation is kept for processes that started on it.
# Seven days covers any realistic run of a long-lived consumer process; a
# process older than that which then imports a NOT-yet-imported submodule of a
# pruned generation gets ImportError (modules it already imported are unaffected).
GENERATION_RETENTION_S = 7 * 24 * 3600
# Hex digits of the content hash used as the generation id.
_GENERATION_ID_LEN = 16


def generation_id(content_hash: str) -> str:
    """Generation id for a published content hash (a hex prefix of it)."""
    return content_hash[:_GENERATION_ID_LEN]


def pth_line(name: str, entry_dir: str) -> str:
    """The single executable ``.pth`` line linking ``name`` from ``entry_dir``.

    site.py executes a ``.pth`` line that begins with ``import``. The line
    resolves the CURRENT generation once, at interpreter start, and prepends
    it to ``sys.path``; a missing or invalid pointer (never published under the
    generation layout, or a pruned/partial generation) falls back to
    ``entry_dir`` itself, which holds the stable ``<name>/`` copy. Prepending
    (rather than a plain-path line, which only appends) makes the shared copy
    win over a stale installed shadow of ``name`` in site-packages. Every error
    is contained: a ``.pth`` that raises would print a traceback on every
    interpreter start.
    """
    code = (
        "import os\n"
        f"_bsl_e = {entry_dir!r}\n"
        "_bsl_p = _bsl_e\n"
        "try:\n"
        f"    with open(os.path.join(_bsl_e, {CURRENT_POINTER!r}), encoding='utf-8') as _bsl_f:\n"
        "        _bsl_g = _bsl_f.read().strip()\n"
        f"    _bsl_c = os.path.join(_bsl_e, {GENERATIONS_DIR!r}, _bsl_g)\n"
        f"    if _bsl_g.isalnum() and os.path.isdir(os.path.join(_bsl_c, {name!r})):\n"
        "        _bsl_p = _bsl_c\n"
        "except Exception:\n"
        "    pass\n"
        "sys.path.insert(0, _bsl_p)\n"
    )
    return "import sys; exec(%r)" % code


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


def _make_stage_dir(entry_dir: str) -> str:
    """Create a uniquely named staging directory beside the published package,
    inheriting ``entry_dir``'s ACL on Windows instead of ``tempfile.mkdtemp``'s
    protected one.

    ``tempfile.mkdtemp`` creates its directory via ``os.mkdir(path, 0o700)``.
    On Windows, CPython >= 3.12.4 (the CVE-2024-4030 fix) maps that explicit
    owner-only mode to a PROTECTED ACL -- only SYSTEM, Administrators, and
    OWNER RIGHTS, with inheritance from the parent disabled -- instead of the
    normal behavior of inheriting the parent directory's ACL. Verified on this
    machine: ``os.mkdir(path)`` (default mode) inherits the parent's ACL;
    ``os.mkdir(path, 0o700)`` reproduces the protected ACL exactly like
    ``mkdtemp``. ``shutil.copytree`` creates the copied subtree under this
    directory with plain ``os.makedirs`` (default mode), so everything copied
    into the stage inherits from THIS directory -- and ``os.replace`` (the
    atomic swap below) preserves a directory's ACL across the rename, so a
    protected stage's ACL would otherwise survive into the durable
    destination. That is exactly the bug this function avoids: a shared-lib
    directory installed with the protected ACL is unreadable to any principal
    that only has access via an inherited ACE on the parent (e.g. a sandboxed
    process running as a different identity), even though the parent's own
    ACL is fine.

    Mirrors ``tempfile.mkdtemp``'s collision-retry loop, but never passes an
    explicit mode -- POSIX gets the ordinary umask-filtered default (0755
    under a typical 022 umask), matching what the published package tree
    already has. No explicit mode is passed because an explicit owner-only
    mode is exactly what triggers the Windows hardening this function exists
    to sidestep.
    """
    for _ in range(100):
        candidate = os.path.join(entry_dir, f".stage-{uuid.uuid4().hex}")
        try:
            os.mkdir(candidate)
        except FileExistsError:
            continue
        return candidate
    raise OSError(f"could not create a unique staging directory under {entry_dir}")


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
    pointer = os.path.join(entry_dir, CURRENT_POINTER)

    current = _hash_tree(src_pkg)
    stamp = _cache_stamp(current)
    gen_id = generation_id(current)
    gen_dir = os.path.join(entry_dir, GENERATIONS_DIR, gen_id)
    # Cached only when the stable copy, the stamp, AND the current generation
    # all agree. A tree published before generations existed has no pointer,
    # so it re-publishes exactly once and gains one.
    if (
        os.path.isdir(dest_pkg)
        and _read_text(hash_file) == stamp
        and _read_text(pointer) == gen_id
        and os.path.isdir(os.path.join(gen_dir, name))
    ):
        return SharedLibResult(name, "cached", f"synced (cached, {dest_pkg})")

    stage_root = None
    try:
        os.makedirs(entry_dir, exist_ok=True)
        stage_root = _make_stage_dir(entry_dir)
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

        # The generation lands BEFORE anything a process resolves through
        # changes: once the pointer below flips, every new interpreter start
        # resolves this directory, and it is never modified again.
        _install_generation(entry_dir, name, stage_pkg, gen_dir)
        _swap_directory(stage_pkg, dest_pkg)
        shutil.rmtree(stage_root, ignore_errors=True)
        stage_root = None
        previous_gen = _read_text(pointer)
        write_atomic(pointer, gen_id + "\n")
        if previous_gen and previous_gen != gen_id:
            _mark_superseded(os.path.join(entry_dir, GENERATIONS_DIR, previous_gen))
        with open(hash_file, "w", encoding="utf-8") as f:
            f.write(stamp + "\n")
    except OSError as exc:
        return SharedLibResult(name, "failed", f"failed to publish {name}: {exc}")
    finally:
        if stage_root is not None:
            shutil.rmtree(stage_root, ignore_errors=True)
    pruned, prune_errors = prune_generations(entry_dir, keep=gen_id)
    message = f"synced -> {dest_pkg} (generation {gen_id})"
    if pruned:
        message += f"; pruned {len(pruned)} superseded generation(s): {', '.join(pruned)}"
    if prune_errors:
        message += f"; could not prune: {'; '.join(prune_errors)} (retried next publish)"
    return SharedLibResult(name, "published", message)


def _install_generation(entry_dir: str, name: str, stage_pkg: str, gen_dir: str) -> None:
    """Place an immutable copy of ``stage_pkg`` at ``<gen_dir>/<name>/``.

    Built in its own stage beside the entry (inheriting its ACL, see
    ``_make_stage_dir``) and renamed into place, so a generation directory is
    either complete or absent. An existing complete generation (the same
    content published before, e.g. a revert) is reused and becomes current
    again, so its superseded marker is cleared.
    """
    if os.path.isdir(os.path.join(gen_dir, name)):
        try:
            os.remove(os.path.join(gen_dir, SUPERSEDED_MARKER))
        except FileNotFoundError:
            pass
        return
    if os.path.exists(gen_dir):
        # A directory without the package cannot be resolved by the .pth;
        # discard it rather than renaming over it.
        _discard_dir(entry_dir, gen_dir)
    os.makedirs(os.path.dirname(gen_dir), exist_ok=True)
    gen_stage = _make_stage_dir(entry_dir)
    try:
        shutil.copytree(stage_pkg, os.path.join(gen_stage, name))
        try:
            os.replace(gen_stage, gen_dir)
        except OSError:
            # A concurrent publisher may have installed the same content first.
            if not os.path.isdir(os.path.join(gen_dir, name)):
                raise
        else:
            gen_stage = None
    finally:
        if gen_stage is not None:
            shutil.rmtree(gen_stage, ignore_errors=True)


def _mark_superseded(gen_dir: str) -> None:
    """Start a superseded generation's retention clock (marker mtime)."""
    if os.path.isdir(gen_dir):
        write_atomic(os.path.join(gen_dir, SUPERSEDED_MARKER), "superseded\n")


def _discard_dir(entry_dir: str, path: str) -> None:
    """Rename ``path`` out of its resolvable name, then delete it.

    The rename is atomic, so ``path`` is either intact or gone; a delete
    that fails part-way (a file held open on Windows) only leaves a
    ``.trash-*`` directory that ``prune_generations`` retries.
    """
    trash = os.path.join(entry_dir, f".trash-{uuid.uuid4().hex}")
    os.replace(path, trash)
    shutil.rmtree(trash, ignore_errors=True)


def prune_generations(entry_dir: str, keep: str, now: Optional[float] = None):
    """Delete superseded generations older than ``GENERATION_RETENTION_S``.

    Never touches ``keep`` (the current generation). A non-current generation
    without a superseded marker gets one now, which starts its clock. Returns
    ``(pruned_ids, errors)``; an error leaves that generation in place for the
    next publish to retry and is reported, never swallowed.
    """
    now = time.time() if now is None else now
    pruned, errors = [], []
    gen_root = os.path.join(entry_dir, GENERATIONS_DIR)
    try:
        names = sorted(os.listdir(gen_root))
    except OSError:
        names = []
    for gid in names:
        if gid == keep:
            continue
        gen_dir = os.path.join(gen_root, gid)
        if not os.path.isdir(gen_dir):
            continue
        marker = os.path.join(gen_dir, SUPERSEDED_MARKER)
        try:
            if not os.path.exists(marker):
                _mark_superseded(gen_dir)
                continue
            if now - os.path.getmtime(marker) < GENERATION_RETENTION_S:
                continue
            _discard_dir(entry_dir, gen_dir)
            pruned.append(gid)
        except OSError as exc:
            errors.append(f"{gid}: {exc}")
    # Leftovers of a delete that failed part-way.
    try:
        leftovers = [n for n in os.listdir(entry_dir) if n.startswith(".trash-")]
    except OSError:
        leftovers = []
    for leftover in leftovers:
        shutil.rmtree(os.path.join(entry_dir, leftover), ignore_errors=True)
    return pruned, errors


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
    # Executable .pth that PREPENDS the current generation (or the stable entry
    # dir) to sys.path at interpreter start -- see pth_line. A plain-path .pth
    # only APPENDS (after this interpreter's own site-packages), so a stale
    # pip-installed copy of <name> sitting in site-packages -- e.g. left over from
    # a former `bootstrap @ git+` dependency that uv sync didn't prune -- would
    # shadow the shared copy. Prepending makes the shared copy authoritative (the
    # single source of truth) regardless of any such leftover. The line names
    # only entry_dir, so an owner publish never rewrites it.
    desired = pth_line(name, entry_dir)
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
