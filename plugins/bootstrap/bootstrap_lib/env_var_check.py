"""Environment-variable persistence and live export (`env_vars` manifest section).

Each `env_vars` entry is ``{"name": <NAME>, "value": <value>}``. Semantics
(bootstrap-env-refactor spec, section 4.6):

- **Live export**: the variable is set in the engine process (``os.environ``)
  so later phases in the SAME pass (e.g. tool install commands) see it, and
  an export line is appended to ``$CLAUDE_ENV_FILE`` so subsequent Bash tool
  invocations in the session see it (mirrors venv_check.export_venv_env_var).
- **Persistence**: an ``export NAME="value"`` line is written/updated
  IN PLACE in the shell rc file(s) on macOS/Ubuntu (a value change replaces
  the existing line rather than appending a stale duplicate), or the
  User-scope registry (``HKCU\\Environment``) on Windows.

Windows persistence writes the registry directly via ``winreg`` -- not
PowerShell -- for the same reason as path_check._add_path_to_windows_registry:
SessionStart hooks frequently inherit a stripped PATH where ``powershell.exe``
does not resolve. Tests (and Windows suite runs) set ``BOOTSTRAP_SKIP_REGISTRY``
to keep the real user registry untouched, same opt-out as path_check.

PATH is deliberately NOT an env_vars concern: PATH edits belong exclusively
to ``path_entries`` + tool->PATH linkage.
"""

import os
import shlex
from typing import List, Optional, Tuple

from .result import Result


def export_line(name: str, value: str) -> str:
    """The canonical rc-file export line for a variable."""
    return f'export {name}="{value}"'


def _rc_files(current_os: str) -> List[str]:
    """Shell rc files that carry env-var exports, per OS.

    Mirrors path_check.add_path_to_shell_config's target selection:
    macOS keeps ~/.zshrc (default shell) and ~/.bashrc in sync; everything
    else uses ~/.bashrc. Windows never reaches here (registry persistence).
    """
    if current_os == "macos":
        return [os.path.expanduser("~/.zshrc"), os.path.expanduser("~/.bashrc")]
    return [os.path.expanduser("~/.bashrc")]


def export_env_var(name: str, value: str) -> Optional[str]:
    """Export into the live engine process and append to ``$CLAUDE_ENV_FILE``.

    The process export always happens. The env-file append no-ops (returning
    ``None``) when ``CLAUDE_ENV_FILE`` is unset/empty or unwritable -- same
    contract as venv_check.export_venv_env_var.

    Returns:
        The exported variable name when the env-file line was written,
        else ``None``.
    """
    os.environ[name] = value

    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file:
        return None
    line = f"export {name}={shlex.quote(value)}\n"
    try:
        with open(env_file, "a") as f:
            f.write(line)
    except OSError:
        return None
    return name


def check_env_var(name: str, value: str, current_os: str) -> Result:
    """Check whether a variable is already persisted with the wanted value.

    Unix: every target rc file contains the exact canonical export line.
    Windows: the User-scope registry value equals ``value``.
    """
    if current_os == "windows":
        return _check_windows_env_var(name, value)

    line = export_line(name, value)
    missing = []
    for rc_file in _rc_files(current_os):
        try:
            with open(rc_file) as f:
                content = f.read()
        except OSError:
            missing.append(os.path.basename(rc_file))
            continue
        if not any(ln.strip() == line for ln in content.splitlines()):
            missing.append(os.path.basename(rc_file))

    if missing:
        return Result(
            passed=False,
            subject=name,
            message=f"{name} not persisted in {', '.join(missing)}",
        )
    return Result(
        passed=True,
        subject=name,
        message=f"{name} persisted in shell rc",
    )


def set_env_var(name: str, value: str, current_os: str) -> Tuple[bool, str]:
    """Persist a variable: rc in-place update (Unix) or User registry (Windows).

    Returns:
        (success, message) tuple.
    """
    if current_os == "windows":
        return _set_windows_env_var(name, value)

    line = export_line(name, value)
    pattern = f"export {name}="
    written = []
    for rc_file in _rc_files(current_os):
        try:
            if os.path.exists(rc_file):
                with open(rc_file) as f:
                    lines = f.read().splitlines(keepends=True)
                new_lines = []
                found = False
                stale = False
                for ln in lines:
                    if ln.strip().startswith(pattern):
                        found = True
                        if ln.strip() != line:
                            stale = True
                        new_lines.append(line + "\n")
                    else:
                        new_lines.append(ln)
                if found and not stale:
                    continue  # already the wanted line; don't rewrite
                if not found:
                    new_lines.append(f"\n# Added by bootstrap\n{line}\n")
                with open(rc_file, "w") as f:
                    f.write("".join(new_lines))
                written.append(
                    f"{'updated' if found else 'added to'} {os.path.basename(rc_file)}"
                )
            else:
                with open(rc_file, "w") as f:
                    f.write(f"\n# Added by bootstrap\n{line}\n")
                written.append(f"created {os.path.basename(rc_file)}")
        except OSError as e:
            return False, f"failed to write {rc_file}: {e}"

    if written:
        return True, "; ".join(written)
    return True, "already persisted"


def _check_windows_env_var(name: str, value: str) -> Result:
    """Read HKCU\\Environment and compare the stored value."""
    if os.environ.get("BOOTSTRAP_SKIP_REGISTRY"):
        return Result(
            passed=True,
            subject=name,
            message="skipped Windows registry check (BOOTSTRAP_SKIP_REGISTRY set)",
        )
    try:
        import winreg
    except ImportError:
        return Result(
            passed=False,
            subject=name,
            message="winreg unavailable (non-Windows Python build)",
        )

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ,
        ) as key:
            try:
                current, _value_type = winreg.QueryValueEx(key, name)
            except FileNotFoundError:
                return Result(
                    passed=False,
                    subject=name,
                    message=f"{name} not set in Windows User environment",
                )
    except OSError as e:
        return Result(
            passed=False,
            subject=name,
            message=f"failed to read Windows User environment: {e}",
        )

    if current == value:
        return Result(
            passed=True,
            subject=name,
            message=f"{name} set in Windows User environment (registry)",
        )
    return Result(
        passed=False,
        subject=name,
        message=(
            f"{name} is {current!r} in Windows User environment, want {value!r}"
        ),
    )


def _set_windows_env_var(name: str, value: str) -> Tuple[bool, str]:
    """Write the variable to the User-scope registry (HKCU\\Environment).

    Direct winreg write + WM_SETTINGCHANGE broadcast, matching the engine's
    PATH registry idiom (path_check._add_path_to_windows_registry).
    """
    if os.environ.get("BOOTSTRAP_SKIP_REGISTRY"):
        return True, "skipped Windows registry write (BOOTSTRAP_SKIP_REGISTRY set)"
    try:
        import winreg
    except ImportError:
        return False, "winreg unavailable (non-Windows Python build)"

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        ) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    except OSError as e:
        return False, f"failed to set {name} in Windows User environment: {e}"

    from .path_check import _broadcast_environment_change
    _broadcast_environment_change()
    return True, f"set {name} in Windows User environment (registry)"
