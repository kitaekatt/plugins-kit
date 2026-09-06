"""PATH entry verification and persistent remediation."""

import os
import sys
from typing import Tuple

from .result import Result


def normalize_path_for_compare(path: str) -> str:
    """Canonical form for PATH-entry equality: normcase + normpath.

    normcase matters on Windows, where case-differing spellings of the same
    directory must compare equal (comparing with normpath alone re-prepended
    entries every phase).
    """
    return os.path.normcase(os.path.normpath(path))


def _home() -> str:
    """Home directory for rc-file writes, honoring ``$HOME`` on Windows too.

    On POSIX ``expanduser("~")`` already prefers ``$HOME``, so a HOME-isolated
    subprocess lands where it intends. On Windows it does NOT -- ``ntpath``
    consults ``USERPROFILE`` and ignores ``HOME`` entirely -- so every engine
    test that redirects HOME to a tmp dir still resolved the developer's REAL
    home and appended its fixture ``path_entries`` to the real ``~/.bashrc``
    (three dead ``/from/*`` exports on one machine before anyone noticed). Same
    class of leak as the registry one guarded in tests/conftest.py, one layer
    over: rc files honor redirection, but only if the code asks for it.

    ``$HOME`` wins only when it names an existing directory: under Git Bash it
    is an MSYS path (``/c/Users/you``) that native Python cannot stat, and
    falling back to ``expanduser`` there keeps production behavior unchanged.
    """
    if os.name == "nt":
        env_home = os.environ.get("HOME")
        if env_home and os.path.isdir(env_home):
            return env_home
    return os.path.expanduser("~")


def _home_relative_path(path: str, home: str) -> str | None:
    """Return the portable $HOME spelling when `path` is under `home`."""
    path_fwd = path.replace("\\", "/")
    home_fwd = home.replace("\\", "/").rstrip("/")
    if not home_fwd:
        return "$HOME" + path_fwd
    if path_fwd == home_fwd or path_fwd.startswith(home_fwd + "/"):
        return "$HOME" + path_fwd[len(home_fwd):]
    return None


def check_path_entry(path_entry: str) -> Result:
    """Check if a directory is present in PATH.

    Args:
        path_entry: Directory path to check (supports ~ expansion)

    Returns:
        Result with pass/fail; subject is the (unexpanded) path entry.
    """
    expanded = os.path.expanduser(path_entry)
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)

    expanded_norm = normalize_path_for_compare(expanded)
    for d in path_dirs:
        if normalize_path_for_compare(d) == expanded_norm:
            return Result(
                passed=True,
                subject=path_entry,
                message=f"{path_entry} is in PATH",
            )

    return Result(
        passed=False,
        subject=path_entry,
        message=f"{path_entry} ({expanded}) is not in PATH",
    )


def add_path_to_shell_config(path_entry: str) -> Tuple[bool, str]:
    """Persistently add a path entry to shell RC files and Windows User PATH.

    Appends `export PATH="<path>:$PATH"` to the appropriate RC file(s).
    On Windows, also writes to the Windows User PATH (registry) so the entry
    is visible to all new processes regardless of shell.
    Idempotent: skips files/registry where the path is already declared.

    Returns:
        (success, message) tuple
    """
    expanded = os.path.expanduser(path_entry)

    # On Windows, write to the User PATH registry key (affects all new processes)
    registry_ok = True
    registry_msg = ""
    failures = []
    durable = False
    if sys.platform == "win32" or "MSYSTEM" in os.environ:
        registry_ok, registry_msg = _add_path_to_windows_registry(path_entry)
        # A skipped registry write (BOOTSTRAP_SKIP_REGISTRY) persists nothing.
        durable = registry_ok and not os.environ.get("BOOTSTRAP_SKIP_REGISTRY")
        if not registry_ok:
            failures.append(f"Windows User PATH registry: {registry_msg}")

    # Build portable export line using $HOME where possible
    home = _home()
    home_form = _home_relative_path(expanded, home)
    if home_form is not None:
        path_expr = f'"{home_form}:$PATH"'
    else:
        path_expr = f'"{expanded}:$PATH"'
    export_line = f'export PATH={path_expr}'

    # Determine RC files by platform
    if sys.platform == "darwin":
        rc_files = [os.path.join(home, ".zshrc"), os.path.join(home, ".bashrc")]
    else:
        # Linux and Windows (Git Bash)
        rc_files = [os.path.join(home, ".bashrc")]

    # Build a slash-normalized form of the $HOME-relative path we'd write,
    # so the idempotency check matches regardless of whether a previous run
    # wrote backslashes (native Windows Python) or forward slashes (MSYS/Cygwin
    # Python). Without this, every run appends a fresh duplicate line.
    expanded_fwd = expanded.replace("\\", "/")
    home_form = _home_relative_path(expanded, home) or expanded_fwd

    written = []
    for rc_file in rc_files:
        try:
            if os.path.exists(rc_file):
                content_fwd = open(rc_file).read().replace("\\", "/")
                if home_form in content_fwd or expanded_fwd in content_fwd:
                    durable = True
                    continue
            with open(rc_file, "a") as f:
                f.write(f'\n# Added by bootstrap\n{export_line}\n')
            written.append(os.path.basename(rc_file))
        except OSError as exc:
            failures.append(f"{os.path.basename(rc_file)}: {exc}")
    if written:
        durable = True

    parts = []
    if written:
        parts.append(f"added to {', '.join(written)}")
    if registry_msg and registry_ok:
        parts.append(registry_msg)
    if parts:
        if failures:
            parts.extend(failures)
        return True, "; ".join(parts)
    if durable:
        return True, "already declared in shell config"
    if failures:
        return False, "; ".join(failures)
    return False, "could not persist PATH in shell config"


def _add_path_to_windows_registry(path_entry: str) -> Tuple[bool, str]:
    """Add a path entry to the Windows User PATH (HKCU\\Environment).

    Writes the registry directly via winreg — no subprocess, so the call does
    not depend on powershell.exe being resolvable on the inherited PATH.
    SessionStart hooks frequently inherit a stripped PATH (e.g. when launched
    from a parent that lacks System32\\WindowsPowerShell\\v1.0), which made
    the previous PowerShell-based implementation fail with WinError 2.

    Reads from User scope only (not the merged Machine+User PATH). Preserves
    the existing Path value type (REG_EXPAND_SZ vs REG_SZ). Broadcasts
    WM_SETTINGCHANGE so other top-level windows pick up the change, matching
    the behavior of .NET's [Environment]::SetEnvironmentVariable.

    Returns:
        (success, message) tuple
    """
    # The registry is global state — it ignores HOME/USERPROFILE redirection,
    # so tests that point HOME at a tmp dir would otherwise leak permanent
    # entries into the real user's PATH. Tests set this var to opt out.
    if os.environ.get("BOOTSTRAP_SKIP_REGISTRY"):
        return True, "skipped Windows registry write (BOOTSTRAP_SKIP_REGISTRY set)"

    try:
        import winreg
    except ImportError:
        return False, "winreg unavailable (non-Windows Python build)"

    expanded = os.path.expanduser(path_entry)
    win_path = expanded.replace("/", "\\")
    norm_target = win_path.rstrip("\\").lower()

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        ) as key:
            try:
                current, value_type = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                current, value_type = "", winreg.REG_EXPAND_SZ

            parts = [p for p in current.split(";") if p]
            if any(p.rstrip("\\").lower() == norm_target for p in parts):
                return True, f"{win_path} already in Windows User PATH"

            new_value = (win_path + ";" + current).rstrip(";") if current else win_path
            winreg.SetValueEx(key, "Path", 0, value_type, new_value)
    except OSError as e:
        return False, (
            f"failed to write Windows User PATH: {e} "
            f"[diag: {_path_diagnostic()}]"
        )

    _broadcast_environment_change()
    return True, f"added {win_path} to Windows User PATH (registry)"


def _path_diagnostic() -> str:
    """Snapshot of PATH state for failure messages.

    Captures length, entry count, and whether the canonical Windows binary
    directories are visible — enough to distinguish "stripped PATH" from
    "registry permission" failures the next time something goes wrong.
    """
    p = os.environ.get("PATH", "")
    entries = [d for d in p.split(os.pathsep) if d]
    has_system32 = any("system32" in d.lower() for d in entries)
    has_powershell = any("windowspowershell" in d.lower() for d in entries)
    return (
        f"PATH={len(p)} chars / {len(entries)} entries; "
        f"System32={has_system32}; PowerShell={has_powershell}"
    )


def _broadcast_environment_change() -> None:
    """Notify top-level windows that environment variables changed.

    Best-effort: a failure here does not roll back the registry write.
    Matches the broadcast behavior of .NET's SetEnvironmentVariable, which
    is what the previous PowerShell implementation relied on implicitly.
    """
    try:
        import ctypes
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        result = ctypes.c_long()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0,
            ctypes.c_wchar_p("Environment"),
            SMTO_ABORTIFHUNG, 5000, ctypes.byref(result),
        )
    except (OSError, AttributeError, ImportError):
        pass
