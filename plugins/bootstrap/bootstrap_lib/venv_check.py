"""Python venv validation and remediation."""

import os
import re
import shlex
import shutil
import subprocess
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


def check_venv(plugin_data_dir: str, plugin_root: str, check_imports: List[str]) -> Result:
    """Check if a Python venv exists and required imports are available.

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

    return _venv_result(
        passed=True,
        message=f"venv ok ({len(check_imports)} imports verified)",
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
    result = check_venv(data_dir, project_dir, list(check_imports))

    if result.passed and not always_sync:
        return result, entries

    extra_flags = " ".join(f"--extra {e}" for e in extras)
    uv_cmd = f"uv sync --project {project_dir}" + (f" {extra_flags}" if extra_flags else "")
    if not result.passed:
        entries.append(f"not ready, running `{uv_cmd}`")

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
            entries.append("created")
    elif proc.returncode != 0:
        stderr_text = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        entries.append(f"uv sync failed (exit {proc.returncode}): {stderr_text[:200]}")
    else:
        entries.append(f"uv sync completed but re-check failed: {result.message}")
    return result, entries
