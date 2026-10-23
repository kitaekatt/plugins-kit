"""PATH entry verification and persistent remediation."""

import os
import re
import sys
from hashlib import sha256
from pathlib import Path
from typing import Tuple

from .result import Result
from .shell_rc import bash_login_candidates, effective_bash_login_file


_NATIVE_PATH_TYPE = type(Path.cwd())
_BLOCK_START = "# >>> bootstrap PATH {token} >>>"
_BLOCK_END = "# <<< bootstrap PATH {token} <<<"


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


def _shell_path(path: str) -> str:
    """Convert a native Windows path to a Git Bash path for rc files."""
    if not (sys.platform == "win32" or "MSYSTEM" in os.environ):
        return path
    normalized = path.replace("\\", "/")
    drive_match = re.match(r"^([A-Za-z]):/(.*)$", normalized)
    if drive_match:
        return f"/{drive_match.group(1).lower()}/{drive_match.group(2)}"
    return normalized


def _path_token(expanded: str) -> str:
    """Return the stable ownership token for one expanded PATH entry."""
    portable = _home_relative_path(expanded, _home()) or _shell_path(expanded)
    canonical = normalize_path_for_compare(portable)
    return sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _double_quote_literal(value: str) -> str:
    """Escape literal text for inclusion inside POSIX double quotes."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )


def _path_shell_expression(expanded: str, home: str) -> str:
    """Render a double-quote-safe entry, expanding only intentional HOME."""
    home_form = _home_relative_path(expanded, home)
    if home_form is not None:
        suffix = home_form[len("$HOME"):]
        return "${HOME}" + _double_quote_literal(suffix)
    return _double_quote_literal(_shell_path(expanded))


def render_path_guard(path_entry: str) -> str:
    """Render bootstrap's POSIX runtime-idempotent PATH guard."""
    expanded = os.path.expanduser(path_entry)
    if any(character in expanded for character in ("\x00", "\n", "\r")):
        raise ValueError("PATH entries cannot contain NUL or newline characters")
    expression = _path_shell_expression(expanded, _home())
    token = _path_token(expanded)
    return (
        f"{_BLOCK_START.format(token=token)}\n"
        'case ":${PATH-}:" in\n'
        f'    *":{expression}:"*) ;;\n'
        f'    *) export PATH="{expression}${{PATH:+:${{PATH}}}}" ;;\n'
        "esac\n"
        f"{_BLOCK_END.format(token=token)}\n"
    )


def _shell_targets(home: Path) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Return active PATH targets and inactive Bash-login candidates."""
    if sys.platform == "darwin":
        login_file = effective_bash_login_file(home)
        active = (home / ".zshrc", home / ".bashrc", login_file)
        inactive = tuple(
            candidate
            for candidate in bash_login_candidates(home)
            if candidate != login_file
        )
        return _deduplicate_targets(active), inactive
    return (home / ".bashrc",), ()


def _file_identity(path: Path) -> tuple[str, int, int] | tuple[str, str]:
    """Return inode identity for existing targets and pathname otherwise."""
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        return ("path", str(path.absolute()))
    return ("inode", stat_result.st_dev, stat_result.st_ino)


def _deduplicate_targets(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    """Deduplicate aliases without replacing or resolving their symlinks."""
    seen: set[tuple[str, int, int] | tuple[str, str]] = set()
    unique = []
    for path in paths:
        identity = _file_identity(path)
        if identity not in seen:
            seen.add(identity)
            unique.append(path)
    return tuple(unique)


def _owned_block_pattern(path_entry: str) -> re.Pattern[str]:
    """Match the bootstrap-owned block for one PATH entry."""
    token = _path_token(os.path.expanduser(path_entry))
    start = re.escape(_BLOCK_START.format(token=token))
    end = re.escape(_BLOCK_END.format(token=token))
    return re.compile(
        rf"^{start}\r?\n.*?^{end}(?:\r?\n|$)",
        flags=re.MULTILINE | re.DOTALL,
    )


def _legacy_export_lines(path_entry: str) -> set[str]:
    """Return exact unconditional exports emitted by older bootstrap builds."""
    expanded = os.path.expanduser(path_entry)
    home = _home()
    home_form = _home_relative_path(expanded, home)
    values = {
        expanded,
        expanded.replace("\\", "/"),
        _shell_path(expanded),
    }
    if home_form is not None:
        values.add(home_form)
    return {f'export PATH="{value}:$PATH"' for value in values}


def _remove_legacy_pair(content: str, path_entry: str) -> str:
    """Remove only an adjacent bootstrap comment and exact legacy export."""
    legacy_lines = _legacy_export_lines(path_entry)
    lines = content.splitlines(keepends=True)
    retained = []
    index = 0
    while index < len(lines):
        if (
            lines[index].rstrip("\r\n") == "# Added by bootstrap"
            and index + 1 < len(lines)
            and lines[index + 1].rstrip("\r\n") in legacy_lines
        ):
            index += 2
            continue
        retained.append(lines[index])
        index += 1
    return "".join(retained)


def _without_owned_path(content: str, path_entry: str) -> str:
    """Remove this entry's marked block and recognizable legacy pair."""
    without_block = _owned_block_pattern(path_entry).sub("", content)
    return _remove_legacy_pair(without_block, path_entry)


def _read_text(path: Path) -> str:
    """Read a shell file as UTF-8; a missing target is empty."""
    try:
        with open(path, encoding="utf-8") as stream:
            return stream.read()
    except FileNotFoundError:
        return ""


def _write_text(path: Path, content: str) -> None:
    """Write through a pathname so existing symlinks remain symlinks."""
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(content)


def _content_with_guard(content: str, path_entry: str) -> str:
    """Replace owned legacy/current content with one current guard."""
    guard = render_path_guard(path_entry)
    cleaned = _without_owned_path(content, path_entry)
    if cleaned and not cleaned.startswith("\n"):
        cleaned = "\n" + cleaned
    return guard + cleaned


def _shell_persistence_result(path_entry: str, targets: tuple[Path, ...]) -> Result:
    """Check that every active shell target contains the exact owned guard."""
    guard = render_path_guard(path_entry)
    missing = []
    failures = []
    for target in targets:
        try:
            content = _read_text(target)
        except OSError as exc:
            failures.append(f"{target.name}: {exc}")
            continue
        if guard not in content:
            missing.append(target.name)
    if failures or missing:
        detail = failures + [f"missing from {name}" for name in missing]
        return Result(
            passed=False,
            subject=path_entry,
            message="PATH persistence incomplete: " + "; ".join(detail),
        )
    names = ", ".join(target.name for target in targets)
    return Result(
        passed=True,
        subject=path_entry,
        message=f"guarded PATH entry is persisted in {names}",
    )


def _windows_registry_contains(path_entry: str) -> bool:
    """Return whether Windows User PATH contains the exact entry."""
    if os.environ.get("BOOTSTRAP_SKIP_REGISTRY"):
        return False
    try:
        import winreg
    except ImportError:
        return False
    expanded = os.path.expanduser(path_entry).replace("/", "\\")
    target = expanded.rstrip("\\").lower()
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ
        ) as key:
            current, _value_type = winreg.QueryValueEx(key, "Path")
    except (FileNotFoundError, OSError):
        return False
    return any(
        item.rstrip("\\").lower() == target
        for item in current.split(";")
        if item
    )


def check_path_entry_persistence(path_entry: str) -> Result:
    """Check persistent PATH coverage independently of the live process PATH."""
    if sys.platform == "win32" or "MSYSTEM" in os.environ:
        if _windows_registry_contains(path_entry):
            return Result(
                passed=True,
                subject=path_entry,
                message="PATH entry is persisted in Windows User PATH",
            )
    home = _NATIVE_PATH_TYPE(_home())
    active, _inactive = _shell_targets(home)
    return _shell_persistence_result(path_entry, active)


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
    """Persistently add a path entry to shell RC files or Windows User PATH.

    Writes a marked, runtime-idempotent POSIX guard to every active shell file.
    On Windows, writes the Windows User PATH (registry) instead, so the entry
    is visible to all new processes regardless of shell. Git Bash inherits
    that PATH, so an rc line as well would add the entry a second time in
    every shell; the rc file is written on Windows only when the registry
    write did not persist.
    On macOS the active files are .zshrc, .bashrc, and Bash's effective login
    file. Inactive Bash login files are cleaned only after every active target
    verifies persistent coverage.

    Returns:
        (success, message) tuple
    """
    # On Windows, write to the User PATH registry key (affects all new processes)
    registry_ok = True
    registry_msg = ""
    failures: list[str] = []
    if sys.platform == "win32" or "MSYSTEM" in os.environ:
        registry_ok, registry_msg = _add_path_to_windows_registry(path_entry)
        # A skipped registry write (BOOTSTRAP_SKIP_REGISTRY) persists nothing.
        if registry_ok and not os.environ.get("BOOTSTRAP_SKIP_REGISTRY"):
            return True, registry_msg

    home = _NATIVE_PATH_TYPE(_home())
    active_targets, inactive_targets = _shell_targets(home)
    changed: list[str] = []
    for target in active_targets:
        try:
            current = _read_text(target)
            replacement = _content_with_guard(current, path_entry)
            if replacement != current:
                _write_text(target, replacement)
                changed.append(target.name)
        except OSError as exc:
            failures.append(f"{target.name}: {exc}")

    coverage = _shell_persistence_result(path_entry, active_targets)
    if failures or not coverage.passed:
        detail = failures
        if not coverage.passed:
            detail.append(coverage.message)
        return False, "; ".join(detail)

    # Cleanup is migration, not revocation. It applies only to the currently
    # declared entry and only after replacement persistence is complete.
    active_identities = {_file_identity(target) for target in active_targets}
    cleaned: list[str] = []
    cleanup_failures: list[str] = []
    for target in inactive_targets:
        if _file_identity(target) in active_identities:
            continue
        try:
            current = _read_text(target)
            replacement = _without_owned_path(current, path_entry)
            if replacement != current:
                _write_text(target, replacement)
                cleaned.append(target.name)
        except OSError as exc:
            cleanup_failures.append(f"{target.name}: {exc}")

    if cleanup_failures:
        return False, "inactive login cleanup failed: " + "; ".join(cleanup_failures)

    parts = [coverage.message]
    if changed:
        parts.append(f"updated {', '.join(changed)}")
    if cleaned:
        parts.append(f"cleaned {', '.join(cleaned)}")
    if registry_msg and not registry_ok:
        parts.append(f"Windows User PATH registry: {registry_msg}")
    return True, "; ".join(parts)


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
