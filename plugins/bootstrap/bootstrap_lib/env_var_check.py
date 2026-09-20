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
- **Removal**: ``unset_env_var`` is the inverse, used when a persisted
  variable is switched off (the ``interpreter_env.persist`` opt-out).

Windows persistence writes the registry directly via ``winreg`` -- not
PowerShell -- for the same reason as path_check._add_path_to_windows_registry:
SessionStart hooks frequently inherit a stripped PATH where ``powershell.exe``
does not resolve. Tests (and Windows suite runs) set ``BOOTSTRAP_SKIP_REGISTRY``
to keep the real user registry untouched, same opt-out as path_check.

PATH is deliberately NOT an env_vars concern: PATH edits belong exclusively
to ``path_entries`` + tool->PATH linkage.
"""

import os
import re
import shlex
from typing import List, Optional, Tuple

from . import session_env
from .result import Result


def export_line(name: str, value: str) -> str:
    """The canonical rc-file export line for a variable."""
    return f"export {name}={shlex.quote(value)}"


_ENV_VAR_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def is_valid_env_var_name(name: str) -> bool:
    """Return whether `name` is safe and valid for shell environment use."""
    return isinstance(name, str) and _ENV_VAR_NAME_RE.fullmatch(name) is not None


def _invalid_name_message(name: str) -> str:
    return f"{name!r} is not a valid shell identifier"


def plugin_root_env_var_name(plugin_name: str) -> str:
    """Compute the env var name holding a plugin's install root.

    Mirrors venv_check.venv_env_var_name and tool_paths.tool_env_var_name:
    uppercase, with every character outside ``[A-Z0-9_]`` replaced so the
    result is a valid shell identifier.

    This is the pointer that lets a consumer OUTSIDE a plugin invoke that
    plugin's shipped scripts. ``CLAUDE_PLUGIN_ROOT`` only tells a component
    where its OWN plugin lives, and a plugin's install path is version-stamped
    (``.../hue-kit/0.9.1/``), so a cross-plugin consumer has no other way to
    resolve one short of globbing the cache for a version directory.

    Exported per session and NEVER persisted to rc files or the registry: the
    value changes on every plugin upgrade, so a persisted copy would go stale
    and point at a version directory that no longer exists.

    >>> plugin_root_env_var_name("hue-kit")
    'HUE_KIT_ROOT'
    >>> plugin_root_env_var_name("bootstrap")
    'BOOTSTRAP_ROOT'
    """
    return re.sub(r"[^A-Z0-9_]", "_", plugin_name.upper()) + "_ROOT"


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
    """Export into the live engine process and into the ``$CLAUDE_ENV_FILE`` block.

    The process export always happens. The env-file part no-ops (returning
    ``None``) when ``CLAUDE_ENV_FILE`` is unset/empty -- same contract as
    venv_check.export_venv_env_var. The line is buffered by ``session_env``,
    which deduplicates the block and writes it once at the end of the pass.

    Returns:
        The exported variable name when the env-file line was written,
        else ``None``.
    """
    if not is_valid_env_var_name(name):
        return None
    os.environ[name] = value

    return session_env.record(name, value)


def check_env_var(name: str, value: str, current_os: str) -> Result:
    """Check whether a variable is already persisted with the wanted value.

    Unix: every target rc file contains the exact canonical export line.
    Windows: the User-scope registry value equals ``value``.
    """
    if not is_valid_env_var_name(name):
        return Result(
            passed=False,
            subject=str(name),
            message=_invalid_name_message(name),
        )
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
    if not is_valid_env_var_name(name):
        return False, _invalid_name_message(name)
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


_BOOTSTRAP_MARKER = "# Added by bootstrap"


def is_env_var_persisted(name: str, current_os: str) -> bool:
    """Whether ANY persisted value exists for ``name`` (whatever the value).

    Unix: some target rc file holds an ``export NAME=`` line. Windows: the
    User registry holds the value; with ``BOOTSTRAP_SKIP_REGISTRY`` set the
    registry is not read and the answer is False.
    """
    if not is_valid_env_var_name(name):
        return False
    if current_os == "windows":
        if os.environ.get("BOOTSTRAP_SKIP_REGISTRY"):
            return False
        try:
            import winreg
        except ImportError:
            return False
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ,
            ) as key:
                winreg.QueryValueEx(key, name)
        except OSError:
            return False
        return True
    pattern = f"export {name}="
    for rc_file in _rc_files(current_os):
        try:
            with open(rc_file) as f:
                if any(ln.strip().startswith(pattern) for ln in f):
                    return True
        except OSError:
            continue
    return False


def _without_export_lines(lines: List[str], name: str) -> Tuple[List[str], int]:
    """``lines`` minus every ``export NAME=`` line and bootstrap's comment above it.

    The ``# Added by bootstrap`` comment goes only when it is the previous
    non-blank line of a removed export; the one blank separator line
    ``set_env_var`` writes above that comment goes with it. Returns the
    remaining lines and the number of export lines removed.
    """
    pattern = f"export {name}="
    drop = set()
    removed = 0
    for index, line in enumerate(lines):
        if not line.strip().startswith(pattern):
            continue
        drop.add(index)
        removed += 1
        above = index - 1
        while above >= 0 and not lines[above].strip():
            above -= 1
        if above >= 0 and above not in drop and lines[above].strip() == _BOOTSTRAP_MARKER:
            drop.add(above)
            if above - 1 >= 0 and not lines[above - 1].strip():
                drop.add(above - 1)
    return [ln for i, ln in enumerate(lines) if i not in drop], removed


def unset_env_var(name: str, current_os: str) -> Tuple[bool, str]:
    """Remove a persisted variable: rc lines (Unix) or the User registry (Windows).

    The inverse of ``set_env_var``. Unix: every target rc file loses its
    ``export NAME=`` line(s) and the ``# Added by bootstrap`` comment directly
    above (see ``_without_export_lines``); a file without such a line is not
    rewritten. Windows: the ``HKCU\\Environment`` value is deleted (honouring
    ``BOOTSTRAP_SKIP_REGISTRY``) and ``WM_SETTINGCHANGE`` is broadcast. A
    variable that is not persisted is a no-op with a message saying so.

    Returns:
        (success, message) tuple.
    """
    if not is_valid_env_var_name(name):
        return False, _invalid_name_message(name)
    if current_os == "windows":
        return _unset_windows_env_var(name)

    removed_from = []
    for rc_file in _rc_files(current_os):
        try:
            with open(rc_file) as f:
                lines = f.read().splitlines(keepends=True)
        except OSError:
            continue
        new_lines, removed = _without_export_lines(lines, name)
        if not removed:
            continue
        try:
            with open(rc_file, "w") as f:
                f.write("".join(new_lines))
        except OSError as e:
            return False, f"failed to write {rc_file}: {e}"
        removed_from.append(os.path.basename(rc_file))
    if removed_from:
        return True, f"removed {name} from {', '.join(removed_from)}"
    return True, f"{name} not persisted (nothing to remove)"


def _unset_windows_env_var(name: str) -> Tuple[bool, str]:
    """Delete the User-scope registry value and broadcast the change."""
    if os.environ.get("BOOTSTRAP_SKIP_REGISTRY"):
        return True, "skipped Windows registry delete (BOOTSTRAP_SKIP_REGISTRY set)"
    try:
        import winreg
    except ImportError:
        return False, "winreg unavailable (non-Windows Python build)"
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        ) as key:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                return True, f"{name} not set in Windows User environment (nothing to remove)"
    except OSError as e:
        return False, f"failed to remove {name} from Windows User environment: {e}"

    from .path_check import _broadcast_environment_change
    _broadcast_environment_change()
    return True, f"removed {name} from Windows User environment (registry)"


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
    # Report the MISMATCH, not the values. An env var bootstrap manages is
    # routinely an API key, and both the observed and wanted value used to be
    # quoted verbatim into a message that reaches the log, the user, Claude, and
    # now a durable record. "differs from the declared value" carries the same
    # actionable information -- the variable is wrong and bootstrap will set it.
    return Result(
        passed=False,
        subject=name,
        message=(
            f"{name} differs from the declared value in the Windows User "
            f"environment"
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
