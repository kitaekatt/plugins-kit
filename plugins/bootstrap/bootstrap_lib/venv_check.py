"""Python venv validation and remediation."""

import ast
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .result import Result


def _venv_result(passed: bool, message: str, venv_path: str, remediation_cmd: Optional[str] = None) -> Result:
    """Result for venv checks: subject is the venv path."""
    return Result(
        passed=passed,
        subject=venv_path,
        message=message,
        remediation_cmd=remediation_cmd,
    )


def venv_env_var_name(plugin_name: str) -> str:
    """Compute the env var name exposing a plugin's venv python.

    Uppercases the name and replaces every character that is not a valid
    POSIX shell identifier character (anything other than A-Z, 0-9, or
    underscore) with an underscore, then suffixes ``_VENV``. Consumers
    re-exec themselves under this interpreter so they don't have to
    reconstruct bootstrap's data-dir path layout.

    >>> venv_env_var_name("unreal-kit")
    'UNREAL_KIT_VENV'
    >>> venv_env_var_name("bootstrap")
    'BOOTSTRAP_VENV'
    """
    return re.sub(r"[^A-Z0-9_]", "_", plugin_name.upper()) + "_VENV"


def export_venv_env_var(plugin_name: str, plugin_data_dir: str) -> Optional[str]:
    """Append an export line to ``$CLAUDE_ENV_FILE`` for this plugin's venv.

    No-ops (returning ``None``) when any of these hold:
        - ``CLAUDE_ENV_FILE`` is unset or empty
        - the venv python binary does not exist

    The no-op-on-missing-binary behavior is deliberate: consumer scripts
    fail fast on unset env vars rather than silently re-exec'ing a broken
    interpreter path.

    Args:
        plugin_name: Plugin manifest name (e.g. ``"unreal-kit"``).
        plugin_data_dir: Plugin data dir; the venv lives at
            ``<plugin_data_dir>/.venv``.

    Returns:
        The exported env var name, or ``None`` if nothing was written.
    """
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file:
        return None

    venv_path = os.path.join(plugin_data_dir, ".venv")
    python_bin = _find_python(venv_path)
    if not python_bin:
        return None

    var_name = venv_env_var_name(plugin_name)
    line = f"export {var_name}={shlex.quote(python_bin)}\n"
    try:
        with open(env_file, "a") as f:
            f.write(line)
    except OSError:
        return None
    return var_name


_FINDER_IMPORT_RE = re.compile(r"\bimport\s+(__editable___[A-Za-z0-9_]+_finder)\b")


def site_packages_dirs(venv_path: str) -> List[Path]:
    """Every site-packages directory inside a venv (Windows and POSIX layouts)."""
    venv = Path(venv_path)
    dirs: List[Path] = []
    win = venv / "Lib" / "site-packages"
    if win.is_dir():
        dirs.append(win)
    posix_lib = venv / "lib"
    if posix_lib.is_dir():
        for entry in sorted(posix_lib.iterdir()):
            candidate = entry / "site-packages"
            if candidate.is_dir():
                dirs.append(candidate)
    return dirs


def _normalize(path: Path) -> str:
    """Resolve links and case so two spellings of one directory compare equal.

    Load-bearing: ``~/.claude`` is a symlink or junction on many machines, so
    the same cache directory is recorded under several spellings. Comparing the
    raw strings would report every plugin on such a machine as stale.
    """
    return os.path.normcase(os.path.realpath(str(path)))


def _is_inside(child: Path, parent: Path) -> bool:
    """True when ``child`` is ``parent`` or lives beneath it."""
    c = _normalize(child)
    p = _normalize(parent)
    return c == p or c.startswith(p + os.sep)


def _read_editable_paths(pth: Path) -> Tuple[List[Path], Optional[str]]:
    """Extract the source paths an editable-install ``.pth`` resolves to.

    Two shapes are produced by the build backends in use:

    - direct: the file's lines are bare filesystem paths appended to sys.path.
    - finder: the file is an ``import __editable___<name>_finder; ...install()``
      one-liner, and the sibling finder module holds a ``MAPPING`` dict of
      package name -> source directory.

    Returns ``(paths, error)``. A non-None ``error`` means the file could not be
    understood and must be left alone.
    """
    try:
        text = pth.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], f"unreadable: {exc}"

    finder_names = _FINDER_IMPORT_RE.findall(text)
    if finder_names:
        paths: List[Path] = []
        for finder_name in finder_names:
            finder_py = pth.parent / f"{finder_name}.py"
            if not finder_py.is_file():
                return [], f"finder module {finder_name}.py not found"
            mapping, error = _read_finder_mapping(finder_py)
            if error:
                return [], error
            paths.extend(mapping)
        if not paths:
            return [], f"finder module for {finder_names[0]} records no paths"
        return paths, None

    paths = []
    for line in text.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.startswith("import ") or entry.startswith("from "):
            # An executable line that is not the known finder shape.
            return [], "unrecognized executable .pth line"
        paths.append(Path(entry))
    if not paths:
        return [], "no paths recorded"
    return paths, None


def _read_finder_mapping(finder_py: Path) -> Tuple[List[Path], Optional[str]]:
    """Read the ``MAPPING`` literal out of an editable finder module.

    Parsed with ``ast`` rather than imported: the module is third-party
    generated code and a check must never execute it.
    """
    try:
        tree = ast.parse(finder_py.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError) as exc:
        return [], f"{finder_py.name} could not be parsed: {exc}"

    for node in tree.body:
        targets = []
        value = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
            value = node.value
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        if "MAPPING" not in targets or value is None:
            continue
        try:
            mapping = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return [], f"{finder_py.name} MAPPING is not a literal"
        if not isinstance(mapping, dict):
            return [], f"{finder_py.name} MAPPING is not a dict"
        return [Path(str(v)) for v in mapping.values()], None
    return [], f"{finder_py.name} declares no MAPPING"


def scan_editable_installs(
    venv_path: str, project_dir: str
) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """Find editable installs in a venv that no longer point at ``project_dir``.

    A plugin's venv lives at a version-independent path while its source lives
    in a version-keyed cache directory, so an editable install written for one
    version keeps resolving to that version's directory after the plugin
    updates. The old directory is still on disk, so the import succeeds and the
    venv silently serves the previous release's code.

    Detection is a containment test, not an equality test: build backends record
    a package subdirectory (``<project_dir>/lib``) rather than the project root,
    and both are legitimate.

    Fails closed. A ``.pth`` whose recorded paths cannot be determined is
    reported as unreadable and never treated as stale, so an unfamiliar shape
    produces a note rather than a rewritten venv.

    Returns:
        ``(stale, unreadable)``, each a list of ``(pth_name, detail)``.
    """
    stale: List[Tuple[str, str]] = []
    unreadable: List[Tuple[str, str]] = []
    project = Path(project_dir)
    for site_dir in site_packages_dirs(venv_path):
        for pth in sorted(site_dir.glob("__editable__*.pth")):
            paths, error = _read_editable_paths(pth)
            if error:
                unreadable.append((pth.name, error))
                continue
            outside = [p for p in paths if not _is_inside(p, project)]
            if outside:
                stale.append((
                    pth.name,
                    f"points at {outside[0]}, expected under {project_dir}",
                ))
    return stale, unreadable


def check_venv(plugin_data_dir: str, plugin_root: str, check_imports: List[str]) -> Result:
    """Check if a Python venv exists and required imports are available.

    Also fails when an editable install still points outside ``plugin_root``
    (see ``scan_editable_installs``): such a venv passes every import while
    serving a superseded source directory, so only the recorded path reveals it.

    Args:
        plugin_data_dir: Plugin data directory (venv lives at <data_dir>/.venv)
        plugin_root: Plugin root directory (for uv sync --project)
        check_imports: List of module names to try importing

    Returns:
        Result with pass/fail and optional remediation command
    """
    venv_path = os.path.join(plugin_data_dir, ".venv")
    remediation = f"uv sync --project {plugin_root}"

    # Check venv directory exists
    if not os.path.isdir(venv_path):
        return _venv_result(
            passed=False,
            message=f"venv not found at {venv_path}",
            venv_path=venv_path,
            remediation_cmd=remediation,
        )

    # Find python binary
    python_bin = _find_python(venv_path)
    if not python_bin:
        return _venv_result(
            passed=False,
            message=f"no python binary in {venv_path}",
            venv_path=venv_path,
            remediation_cmd=remediation,
        )

    # Check python works
    try:
        proc = subprocess.run(
            [python_bin, "-c", "import sys; sys.exit(0)"],
            capture_output=True, timeout=10,
        )
        if proc.returncode != 0:
            raise subprocess.SubprocessError(f"exit code {proc.returncode}")
    except (subprocess.SubprocessError, OSError):
        return _venv_result(
            passed=False,
            message=f"python binary not functional at {python_bin}",
            venv_path=venv_path,
            remediation_cmd=remediation,
        )

    # Check each import
    for module in check_imports:
        try:
            result = subprocess.run(
                [python_bin, "-c", f"import {module}"],
                capture_output=True, timeout=10,
            )
            if result.returncode != 0:
                return _venv_result(
                    passed=False,
                    message=f"import {module} failed in venv",
                    venv_path=venv_path,
                    remediation_cmd=remediation,
                )
        except (subprocess.SubprocessError, OSError):
            return _venv_result(
                passed=False,
                message=f"failed to check import {module}",
                venv_path=venv_path,
                remediation_cmd=remediation,
            )

    # An editable install pinned to a superseded source directory imports
    # cleanly, so it survives every check above; it has to be detected on the
    # recorded path rather than on behavior.
    stale, unreadable = scan_editable_installs(venv_path, plugin_root)
    if stale:
        detail = "; ".join(f"{name} {why}" for name, why in stale)
        return _venv_result(
            passed=False,
            message=f"stale editable install: {detail}",
            venv_path=venv_path,
            remediation_cmd=remediation,
        )

    message = f"venv ok ({len(check_imports)} imports verified)"
    if unreadable:
        # Verbose-only, and deliberately not a failure: an unparsed .pth is left
        # exactly as found.
        notes = "; ".join(f"{name}: {why}" for name, why in unreadable)
        message += f" [editable install left alone - {notes}]"
    return _venv_result(
        passed=True,
        message=message,
        venv_path=venv_path,
    )


def _find_python(venv_path: str) -> Optional[str]:
    """Find the python binary in a venv."""
    candidates = [
        os.path.join(venv_path, "bin", "python"),
        os.path.join(venv_path, "Scripts", "python.exe"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def find_uv() -> Optional[str]:
    """Locate the uv binary: PATH first, then ~/.local/bin directly.

    The direct ~/.local/bin probe covers the same-session case where the tools
    phase just installed uv but the inherited PATH doesn't include it yet.
    """
    uv_bin = shutil.which("uv")
    if uv_bin:
        return uv_bin
    local_bin = os.path.expanduser("~/.local/bin")
    for name in ("uv", "uv.exe", "uv.EXE"):
        candidate = os.path.join(local_bin, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def ensure_venv(
    project_dir: str,
    venv_path: str,
    extras: Sequence[str] = (),
    check_imports: Sequence[str] = (),
    always_sync: bool = False,
) -> Tuple[Result, List[str]]:
    """Check a venv and remediate via ``uv sync``: check -> sync -> re-check.

    The single venv-remediation path shared by the engine's self-setup,
    per-plugin manifest, and project_venv phases (previously three diverging
    copies — B9).

    Args:
        project_dir: Directory holding the pyproject.toml (``uv sync --project``).
        venv_path: Target venv directory (``<dir>/.venv``); also exported as
            UV_PROJECT_ENVIRONMENT so the sync lands here rather than in
            project_dir/.venv.
        extras: Optional dependency extras (``--extra <name>`` each).
        check_imports: Module names that must import inside the venv.
        always_sync: Run ``uv sync`` even when the check already passes
            (self-setup keeps the engine venv current every session; a no-op
            sync is ~100ms).

    Returns:
        (result, entries): the final check Result, plus unprefixed action
        messages describing every remediation step or failure. Per the
        logging contract, any attempted work or error produces an entry —
        nothing is swallowed. Callers prefix entries with their phase label
        (e.g. ``"venv: "``) and route the final result to ok/action entries.
    """
    data_dir = os.path.dirname(venv_path)
    entries: List[str] = []
    existed = os.path.isdir(venv_path)
    result = check_venv(data_dir, project_dir, list(check_imports))

    if result.passed and not always_sync:
        return result, entries

    extra_flags = " ".join(f"--extra {e}" for e in extras)
    uv_cmd = f"uv sync --project {project_dir}" + (f" {extra_flags}" if extra_flags else "")
    if not result.passed:
        # The reason is part of the action entry: a re-sync triggered by a stale
        # editable install is otherwise indistinguishable from any other repair.
        entries.append(f"not ready, running `{uv_cmd}` - {result.message}")

    uv_bin = find_uv()
    if not uv_bin:
        entries.append("uv not found on PATH or in ~/.local/bin")
        return result, entries

    cmd = [uv_bin, "sync", "--project", project_dir]
    for e in extras:
        cmd.extend(["--extra", e])
    env = dict(os.environ, UV_PROJECT_ENVIRONMENT=venv_path)
    # Ensure ~/.local/bin is on PATH for uv's own child processes.
    local_bin = os.path.expanduser("~/.local/bin")
    if local_bin not in env.get("PATH", ""):
        env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, timeout=120)
    except (subprocess.SubprocessError, OSError) as exc:
        entries.append(f"uv sync error: {exc}")
        return result, entries

    was_passing = result.passed
    result = check_venv(data_dir, project_dir, list(check_imports))
    if result.passed:
        if not was_passing:
            entries.append("created" if not existed else "re-synced")
    elif proc.returncode != 0:
        stderr_text = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        # The FULL stderr, not the first 200 chars. uv reports the resolution
        # conflict that actually explains the failure well past that cut, and
        # the tail was unrecoverable -- the entry was the only copy. Length is
        # now a rendering concern: the record and the log keep everything, and
        # the collated display line shortens it (see messages.py).
        entries.append(f"uv sync failed (exit {proc.returncode}): {stderr_text}")
    else:
        entries.append(f"uv sync completed but re-check failed: {result.message}")
    return result, entries
