"""Shared-library publishing + linking for the bootstrap engine.

The ``shared_libs`` capability lets an owner plugin publish a first-party Python
package to a stable, version-independent location, and lets consuming plugins
import it WITHOUT declaring a dependency on the owner plugin (reuse-by-availability).

Mechanism (a generalization of llm-kit's B1 prototype):

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
  foreign-interpreter consumer reads (library-consumption.md, mode 3b).

- Revocation: a link is removed when its declaration is. Bootstrap removes only
  the ``.pth`` files IT wrote (content exactly equal to ``pth_line``'s output
  for the file's own name), from a venv whose owner no longer declares that
  name, and from the standalone interpreter when the recorded owner
  (``<shared_root>/<pkg>/.owner``) is installed and no longer publishes the
  library. A published tree under ``_shared_libs`` is never deleted.

This module shares first-party SOURCE only. Third-party deps the package needs
(e.g. ``openai`` for ``llm_kit``) are the importing plugin's own concern,
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

import ast
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from typing import NamedTuple, Optional

from .atomic_write import write_atomic


class SharedLibResult(NamedTuple):
    name: str
    status: str   # "cached" | "published" | "linked" | "revoked" | "skipped" | "failed"
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
    owner_marketplace: str = "",
    owner_plugin: str = "",
) -> SharedLibResult:
    """Publish an owner package's SOURCE to the shared location.

    Syncs ``<plugin_root>/<src>/<name>/`` -> ``<shared_root>/<name>/<name>/`` with a
    clean re-sync so renamed/deleted modules are pruned. Staging happens beside
    the live package and the completed directory is swapped into place. Skips
    when the source tree hash is unchanged.

    Returns "published" (synced), "cached" (unchanged), or "failed" (no source).

    When ``owner_plugin`` is given, the owner's identity is recorded in
    ``<shared_root>/<name>/.owner`` on every outcome that leaves a published
    tree (including "cached", so a tree published before the record existed
    gains one). The record is what lets a later pass revoke the standalone
    ``<name>.pth`` once its owner stops publishing the library
    (``read_owner_record``); it is not part of the content hash.
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
        try:
            _record_owner(entry_dir, owner_marketplace, owner_plugin)
        except OSError as exc:
            return SharedLibResult(
                name, "failed", f"could not record the owner of {name}: {exc}")
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
        _record_owner(entry_dir, owner_marketplace, owner_plugin)
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


# ---------------------------------------------------------------------------
# Owner records and targeted revocation
# ---------------------------------------------------------------------------

OWNER_RECORD = ".owner"
# A bootstrap-written .pth is under 1 KiB; anything much larger is not ours,
# so it is never read.
_MAX_PTH_BYTES = 8192
_PTH_EXEC_PREFIX = "import sys; exec("
_PTH_ENTRY_RE = re.compile(r"^_bsl_e = (.+)$", re.MULTILINE)


class BootstrapLink(NamedTuple):
    name: str
    path: str
    entry_dir: str


def parse_pth_entry_dir(name: str, content: str) -> Optional[str]:
    """The ``entry_dir`` of a ``.pth`` bootstrap wrote for ``name``, else None.

    "Bootstrap wrote it" means the stripped content EQUALS
    ``pth_line(name, entry_dir)`` for the ``entry_dir`` the content itself
    names. The literals are read with ``ast.literal_eval`` (never executed),
    and the final equality check rejects anything that merely resembles the
    format, so a hand-written, edited or foreign ``.pth`` is never claimed.
    None means "not ours", not an error.
    """
    text = content.strip()
    if not (text.startswith(_PTH_EXEC_PREFIX) and text.endswith(")")):
        return None
    try:
        code = ast.literal_eval(text[len(_PTH_EXEC_PREFIX):-1])
        match = _PTH_ENTRY_RE.search(code) if isinstance(code, str) else None
        entry_dir = ast.literal_eval(match.group(1)) if match else None
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None
    if not isinstance(entry_dir, str):
        return None
    return entry_dir if pth_line(name, entry_dir) == text else None


def iter_bootstrap_links(site_dir: str):
    """Yield a ``BootstrapLink`` for every bootstrap-written ``.pth`` in ``site_dir``."""
    try:
        names = sorted(os.listdir(site_dir))
    except OSError:
        return
    for fname in names:
        if not fname.endswith(".pth") or len(fname) == len(".pth"):
            continue
        path = os.path.join(site_dir, fname)
        try:
            if not os.path.isfile(path) or os.path.getsize(path) > _MAX_PTH_BYTES:
                continue
        except OSError:
            continue
        name = fname[: -len(".pth")]
        raw = _read_raw(path)
        entry_dir = parse_pth_entry_dir(name, raw) if raw is not None else None
        if entry_dir is not None:
            yield BootstrapLink(name, path, entry_dir)


def venv_site_packages(venv_dir: str) -> Optional[str]:
    """The site-packages directory of the venv at ``venv_dir``, or None.

    Resolved from the venv layout without running its interpreter: revocation
    runs BEFORE the venv check, when the interpreter may be missing or broken.
    """
    if sys.platform == "win32":
        candidates = [os.path.join(venv_dir, "Lib", "site-packages")]
    else:
        candidates = sorted(glob.glob(os.path.join(venv_dir, "lib", "python*", "site-packages")))
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return None


def standalone_site_packages() -> Optional[str]:
    """site-packages of the bootstrap-managed standalone Python, or None."""
    python = find_standalone_python()
    if python is None:
        return None
    # Windows: <prefix>/python.exe. POSIX: <prefix>/bin/python3.
    prefix = os.path.dirname(python)
    if sys.platform != "win32":
        prefix = os.path.dirname(prefix)
    return venv_site_packages(prefix)


def revoke_link(link: BootstrapLink) -> SharedLibResult:
    """Remove one bootstrap-written ``.pth``; returns "revoked" or "failed"."""
    try:
        os.remove(link.path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        return SharedLibResult(
            link.name, "failed",
            f"could not revoke the undeclared link {link.path}: {exc}",
        )
    return SharedLibResult(link.name, "revoked", f"revoked undeclared link -> {link.path}")


def revoke_undeclared_links(site_dir: str, declared) -> list:
    """Revoke every bootstrap-written ``.pth`` in ``site_dir`` not in ``declared``.

    ``declared`` is the set of library names the venv's owner still declares in
    ``shared_lib_imports``. A ``.pth`` whose content is not exactly what
    bootstrap writes is never touched. Returns one ``SharedLibResult`` per
    removal attempt ("revoked" or "failed"); nothing for a kept or foreign file.
    """
    return [
        revoke_link(link)
        for link in iter_bootstrap_links(site_dir)
        if link.name not in declared
    ]


def _record_owner(entry_dir: str, marketplace: str, plugin: str) -> None:
    """Write ``<entry_dir>/.owner`` when it differs from the desired record."""
    if not plugin:
        return
    desired = json.dumps({"marketplace": marketplace, "plugin": plugin}, sort_keys=True) + "\n"
    if _read_raw(os.path.join(entry_dir, OWNER_RECORD)) == desired:
        return
    write_atomic(os.path.join(entry_dir, OWNER_RECORD), desired)


def read_owner_record(entry_dir: str) -> Optional[dict]:
    """The ``{"marketplace", "plugin"}`` record beside a published tree, or None.

    None covers a tree published before records existed and a record that is
    not the expected shape; the caller treats both as "ownership unknown" and
    revokes nothing.
    """
    raw = _read_raw(os.path.join(entry_dir, OWNER_RECORD))
    if raw is None:
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    if (
        isinstance(record, dict)
        and isinstance(record.get("plugin"), str) and record["plugin"]
        and isinstance(record.get("marketplace"), str)
    ):
        return record
    return None


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


class SharedRootResolution(NamedTuple):
    name: str
    status: str   # "resolved" | "absent" | "ambiguous" | "misqualified"
    root: Optional[str]   # the shared_root to pass to link_shared_lib; set only when "resolved"
    message: str


def resolve_project_shared_root(
    data_root: str, name: str, marketplace: Optional[str] = None
) -> SharedRootResolution:
    """Find the ``shared_root`` that holds a published shared lib, for a consumer
    that has no marketplace of its own (a project venv).

    Scans ``<data_root>/*/_shared_libs/<name>/<name>/`` and reports the one
    marketplace that holds it. ``root`` is ``<data_root>/<marketplace>/_shared_libs``,
    the same value a plugin consumer derives, so it feeds ``link_shared_lib``
    unchanged; generation pinning stays in ``pth_line``.

    Never raises (like the other functions here, outcomes are a status for the
    engine to route): "resolved" (exactly one match, or the named marketplace
    holds it), "absent" (no match, or the named marketplace does not hold it --
    the same eventual-consistency soft-skip as an unpublished lib), or
    "ambiguous" (two or more marketplaces hold it and none was named; the message
    names each and asks for the qualified form), or "misqualified" (a named
    marketplace does not hold it but others do -- a likely typo, reported with the
    marketplaces that do publish it; not a failure). ``marketplace`` restricts the
    search to that one directory name, matched exactly.
    """
    try:
        candidates = sorted(os.listdir(data_root))
    except OSError:
        candidates = []
    found = [
        mkt for mkt in candidates
        if (marketplace is None or mkt == marketplace)
        and os.path.isdir(os.path.join(data_root, mkt, "_shared_libs", name, name))
    ]
    if not found and marketplace is not None:
        elsewhere = [
            mkt for mkt in candidates
            if os.path.isdir(os.path.join(data_root, mkt, "_shared_libs", name, name))
        ]
        if elsewhere:
            # Marketplace names match exactly: case and whitespace included. A
            # case-insensitive match would resolve on Windows and fail on Linux
            # for the same manifest, so a near-miss is reported, never resolved.
            return SharedRootResolution(
                name, "misqualified", None,
                f"shared lib {name} is not published by marketplace "
                f"{marketplace!r}, but is published by {', '.join(elsewhere)}; "
                f"check the marketplace spelling (names match exactly, "
                f"including case and surrounding whitespace)",
            )
    if not found:
        where = (
            f"marketplace {marketplace}" if marketplace is not None
            else f"any marketplace under {data_root}"
        )
        return SharedRootResolution(
            name, "absent", None,
            f"shared lib {name} not published by {where}; will retry next session",
        )
    if len(found) > 1:
        return SharedRootResolution(
            name, "ambiguous", None,
            f"shared lib {name} is published by more than one marketplace "
            f"({', '.join(found)}); qualify it as "
            f'{{"name": "{name}", "marketplace": "<one of them>"}}',
        )
    return SharedRootResolution(
        name, "resolved", os.path.join(data_root, found[0], "_shared_libs"),
        f"{name} resolved from marketplace {found[0]}",
    )
