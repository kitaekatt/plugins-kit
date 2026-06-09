"""Python venv validation and remediation."""

import os
import re
import shlex
import subprocess
from typing import List, NamedTuple, Optional


class VenvCheckResult(NamedTuple):
    passed: bool
    message: str
    venv_path: str
    remediation_cmd: Optional[str] = None


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


def check_venv(plugin_data_dir: str, plugin_root: str, check_imports: List[str]) -> VenvCheckResult:
    """Check if a Python venv exists and required imports are available.

    Args:
        plugin_data_dir: Plugin data directory (venv lives at <data_dir>/.venv)
        plugin_root: Plugin root directory (for uv sync --project)
        check_imports: List of module names to try importing

    Returns:
        VenvCheckResult with pass/fail and optional remediation command
    """
    venv_path = os.path.join(plugin_data_dir, ".venv")
    remediation = f"uv sync --project {plugin_root}"

    # Check venv directory exists
    if not os.path.isdir(venv_path):
        return VenvCheckResult(
            passed=False,
            message=f"venv not found at {venv_path}",
            venv_path=venv_path,
            remediation_cmd=remediation,
        )

    # Find python binary
    python_bin = _find_python(venv_path)
    if not python_bin:
        return VenvCheckResult(
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
        return VenvCheckResult(
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
                return VenvCheckResult(
                    passed=False,
                    message=f"import {module} failed in venv",
                    venv_path=venv_path,
                    remediation_cmd=remediation,
                )
        except (subprocess.SubprocessError, OSError):
            return VenvCheckResult(
                passed=False,
                message=f"failed to check import {module}",
                venv_path=venv_path,
                remediation_cmd=remediation,
            )

    return VenvCheckResult(
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
