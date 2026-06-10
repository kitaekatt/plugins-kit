"""Detect Microsoft Store Python stubs shadowing the bootstrap-installed Python,
and write a self-elevating batch file to remediate by editing the System PATH.

The check reads PATH from the Windows registry directly (HKLM System Environment
+ HKCU User Environment), NOT from the in-process PATH. This is critical:
session-bootstrap.sh prepends the standalone Python directory to the in-process
PATH before the engine runs, which would mask the WindowsApps stub from
shutil.which. Only the persistent registry PATH reflects the real state that
fresh shells will see.
"""

import os
import sys
from typing import List, Optional, Tuple

from .result import Result

# Extras carried on python-stub Results:
#   bad_python      -- path to the shadowing python.exe (or None)
#   good_python_dir -- expanded absolute path to standalone python dir


def _stub_result(passed: bool, message: str, bad_python: Optional[str], good_python_dir: str) -> Result:
    return Result(
        passed=passed,
        subject="python_stub",
        message=message,
        extras={"bad_python": bad_python, "good_python_dir": good_python_dir},
    )


def _get_persistent_path_dirs() -> List[str]:
    """Read PATH from Windows registry (HKLM System + HKCU User), in resolution order.

    Returns a list of directory strings with %VAR% references expanded and empty
    entries filtered out. Returns [] if winreg is unavailable (non-Windows) or
    neither key exists.
    """
    try:
        import winreg  # type: ignore
    except ImportError:
        return []

    dirs: List[str] = []
    # System PATH (HKLM) — resolved first by Windows
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ) as key:
            val, _ = winreg.QueryValueEx(key, "Path")
            if val:
                dirs.extend(val.split(";"))
    except OSError:
        pass

    # User PATH (HKCU) — appended after System PATH by Windows
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            val, _ = winreg.QueryValueEx(key, "Path")
            if val:
                dirs.extend(val.split(";"))
    except OSError:
        pass

    return [os.path.expandvars(d.strip()) for d in dirs if d and d.strip()]


def _find_first_python_in_dirs(dirs: List[str]) -> Optional[str]:
    """Walk dirs in order and return the full path to the first python.exe found."""
    for d in dirs:
        if not d:
            continue
        for name in ("python.exe", "python3.exe"):
            candidate = os.path.join(d, name)
            try:
                if os.path.isfile(candidate):
                    return candidate
            except OSError:
                pass
    return None


def check_python_stub(good_python_dir: str, stub_markers) -> Result:
    """Detect if a Python stub (e.g. WindowsApps) shadows the standalone Python.

    On non-Windows, always passes.
    On Windows:
      - Read persistent PATH (HKLM System + HKCU User) — NOT the in-process PATH,
        which is masked by the bash session-bootstrap.sh prepend
      - Walk dirs in Windows resolution order looking for python.exe
      - If first hit lives inside good_python_dir -> pass
      - If first hit's path contains any stub_marker substring -> fail
      - Otherwise (some other Python first) -> pass (not our concern)
    """
    expanded_good = os.path.abspath(os.path.expanduser(good_python_dir))
    is_windows = sys.platform == "win32" or "MSYSTEM" in os.environ
    if not is_windows:
        return _stub_result(True, "not windows, skipped", None, expanded_good)

    persistent_dirs = _get_persistent_path_dirs()
    if not persistent_dirs:
        return _stub_result(True, "no persistent PATH found in registry", None, expanded_good)

    found = _find_first_python_in_dirs(persistent_dirs)
    if not found:
        return _stub_result(True, "no python.exe on persistent PATH", None, expanded_good)

    found_norm = os.path.normcase(os.path.normpath(found))
    good_norm = os.path.normcase(expanded_good)
    found_dir = os.path.dirname(found_norm)
    if found_dir == good_norm or found_norm.startswith(good_norm + os.sep):
        return _stub_result(
            True, f"good python first on persistent PATH ({found})",
            None, expanded_good,
        )

    for marker in stub_markers:
        if marker.lower() in found_norm.lower():
            return _stub_result(
                False,
                f"stub python ({marker}) shadows standalone python on persistent PATH: {found}",
                found, expanded_good,
            )

    return _stub_result(
        True, f"non-stub python first on persistent PATH: {found}",
        None, expanded_good,
    )


def write_fix_script(good_python_dir: str, output_dir: str) -> Tuple[bool, str, str]:
    """Write fix_python_path.bat to output_dir.

    Returns (success, message, script_path).
    Idempotent: overwrites the file if present so updates to the template land.
    """
    expanded_out = os.path.abspath(os.path.expanduser(output_dir))
    expanded_good = os.path.abspath(os.path.expanduser(good_python_dir))
    win_good = expanded_good.replace("/", "\\")
    script_path = os.path.join(expanded_out, "fix_python_path.bat")

    try:
        os.makedirs(expanded_out, exist_ok=True)
        content = _BATCH_TEMPLATE.replace("__GOOD_PYTHON_DIR__", win_good)
        with open(script_path, "w", newline="\r\n") as f:
            f.write(content)
        return True, f"wrote {script_path}", script_path
    except OSError as e:
        return False, f"failed to write {script_path}: {e}", script_path


_BATCH_TEMPLATE = r"""@echo off
REM ============================================================
REM  fix_python_path.bat
REM  Generated by plugins-kit bootstrap.
REM
REM  Prepends the bootstrap-installed standalone Python directory
REM  to the SYSTEM PATH so the Microsoft Store python.exe stub
REM  no longer shadows it.
REM
REM  Requires administrator privileges (modifies HKLM Environment).
REM  Self-elevates via UAC; if that fails, instructions are printed.
REM ============================================================

setlocal enableextensions

set "GOOD_PYTHON_DIR=__GOOD_PYTHON_DIR__"

REM --- Admin detection (fsutil requires admin; redirect noise) ---
fsutil dirty query %SystemDrive% >nul 2>&1
if %errorlevel% neq 0 goto :not_admin
goto :is_admin

:not_admin
echo.
echo This script needs administrator privileges to modify the System PATH.
echo Attempting to relaunch with elevation...
echo.
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs" 1>nul 2>nul
if %errorlevel% equ 0 exit /b
echo.
echo Could not relaunch automatically.
echo To run this script with admin privileges:
echo   1. Open File Explorer and navigate to: %~dp0
echo   2. Right-click "%~nx0"
echo   3. Choose "Run as administrator"
echo.
echo Or from an elevated command prompt:
echo   "%~f0"
echo.
pause
exit /b 1

:is_admin
echo Running with administrator privileges.
echo Good Python directory: %GOOD_PYTHON_DIR%
echo.

if not exist "%GOOD_PYTHON_DIR%\python.exe" (
    echo ERROR: Good Python not found at %GOOD_PYTHON_DIR%\python.exe
    echo Run Claude Code once to let plugins-kit install the standalone Python,
    echo then re-run this script.
    echo.
    pause
    exit /b 2
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$dir = $env:GOOD_PYTHON_DIR;" ^
  "$current = [Environment]::GetEnvironmentVariable('Path', 'Machine');" ^
  "if (-not $current) { $current = '' };" ^
  "$parts = $current -split ';' | Where-Object { $_ -ne '' };" ^
  "$norm = $dir.TrimEnd('\\');" ^
  "if ($parts.Count -gt 0 -and $parts[0].TrimEnd('\\') -ieq $norm) {" ^
  "  Write-Host 'OK: standalone Python is already first in System PATH.' -ForegroundColor Green;" ^
  "  exit 0" ^
  "};" ^
  "$parts = $parts | Where-Object { $_.TrimEnd('\\') -ine $norm };" ^
  "$newPath = (@($dir) + $parts) -join ';';" ^
  "[Environment]::SetEnvironmentVariable('Path', $newPath, 'Machine');" ^
  "Write-Host ('Prepended ' + $dir + ' to System PATH.') -ForegroundColor Green;" ^
  "Write-Host 'Open a new terminal or restart Claude Code for the change to take effect.' -ForegroundColor Cyan"

if %errorlevel% neq 0 (
    echo.
    echo ERROR: PowerShell exited with error %errorlevel%. The fix may not have applied.
    echo This script will NOT delete itself so you can try again.
    echo.
    pause
    endlocal
    exit /b 3
)

echo.
echo Open a new terminal or restart Claude Code for the change to take effect.
echo This script has done its job and will now delete itself.
echo.
pause
endlocal
REM Self-delete: (goto) leaves the current execution context, releasing the
REM file lock, then del removes this script. Standard batch self-delete idiom.
(goto) 2>nul & del "%~f0"
"""
