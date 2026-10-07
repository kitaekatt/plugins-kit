"""Manage bootstrap's user-level Codex SessionStart hook.

Bootstrap installs one guarded hook in ``$CODEX_HOME/hooks.json`` (default
``~/.codex/hooks.json``). The command contains no user-specific absolute path
and quietly exits when bootstrap is not installed. During migration, each pass
also removes bootstrap-owned entries from a project's legacy hook file.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
import json
import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass


CODEX_DIRNAME = ".codex"
HOOKS_FILENAME = "hooks.json"
HOOK_MARKER = "bootstrap codex-hook"
SESSION_START_MATCHER = "^(startup|resume)$"
ADDITIONAL_CONTEXT_LIMIT = 5000
POSIX_HOOK_COMMAND = (
    "if command -v bootstrap >/dev/null 2>&1; then exec bootstrap codex-hook; "
    'elif [ -x "$HOME/.local/bin/bootstrap" ]; then exec "$HOME/.local/bin/bootstrap" '
    "codex-hook; fi; exit 0"
)
# No embedded double quotes: Windows PowerShell 5.1 re-parses the string and
# mangles them, so the profile launcher directory is appended to PATH (set takes
# the rest of the line, spaces included) and found by the same PATH lookup.
WINDOWS_HOOK_COMMAND = (
    'cmd.exe /d /c "set PATH=%PATH%;%USERPROFILE%\\.local\\bin& '
    'where bootstrap.cmd >nul 2>&1 & '
    'if errorlevel 1 (exit /b 0) else call bootstrap.cmd codex-hook"'
)
_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class CodexHookInstallResult:
    changed: bool
    path: str


@dataclass(frozen=True)
class CodexHookStripResult:
    changed: bool
    path: str


class CodexHookError(Exception):
    """The user Codex hook cannot be safely installed."""


def _hook_entry() -> dict:
    return {
        "type": "command",
        "command": POSIX_HOOK_COMMAND,
        "commandWindows": WINDOWS_HOOK_COMMAND,
        "statusMessage": "Running bootstrap",
        "timeout": 300,
        "additionalContextLimit": ADDITIONAL_CONTEXT_LIMIT,
    }


def _is_legacy_bootstrap_command(value) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().replace("\\", "/").replace('"', "").lower()
    return ("bootstrap codex-hook" in normalized or "bootstrap.cmd codex-hook" in normalized) and (
        "/" in normalized or "cmd.exe" in normalized
    )


def _is_our_hook(value) -> bool:
    if not isinstance(value, dict):
        return False
    return any((
        value.get("command") == POSIX_HOOK_COMMAND,
        value.get("commandWindows") == WINDOWS_HOOK_COMMAND,
        _is_legacy_bootstrap_command(value.get("command")),
        _is_legacy_bootstrap_command(value.get("commandWindows")),
    ))


def _merge_hooks(document: dict) -> dict:
    if not isinstance(document, dict):
        raise CodexHookError("hooks.json must contain a JSON object")
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise CodexHookError("hooks.json 'hooks' must be an object")
    for event_name, groups in list(hooks.items()):
        if not isinstance(groups, list):
            continue
        kept = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept.append(group)
                continue
            remaining = [hook for hook in group["hooks"] if not _is_our_hook(hook)]
            if remaining:
                updated = dict(group)
                updated["hooks"] = remaining
                kept.append(updated)
        hooks[event_name] = kept
    session_groups = hooks.get("SessionStart")
    if not isinstance(session_groups, list):
        session_groups = []
    hooks["SessionStart"] = session_groups
    hooks["SessionStart"].append({
        "matcher": SESSION_START_MATCHER,
        "hooks": [_hook_entry()],
    })
    return document


def _strip_hooks(document: dict) -> tuple[dict, bool]:
    if not isinstance(document, dict):
        raise ValueError("hooks.json must contain a JSON object")
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return document, False
    changed = False
    for event_name, groups in list(hooks.items()):
        if not isinstance(groups, list):
            continue
        kept = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept.append(group)
                continue
            remaining = [hook for hook in group["hooks"] if not _is_our_hook(hook)]
            if len(remaining) != len(group["hooks"]):
                changed = True
            if remaining:
                updated = dict(group)
                updated["hooks"] = remaining
                kept.append(updated)
            else:
                changed = True
        if len(kept) != len(groups):
            changed = True
        if kept:
            hooks[event_name] = kept
        else:
            del hooks[event_name]
            changed = True
    return document, changed


def _atomic_write(path: str, content: str, mode: int | None = None) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".codex-hooks.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


@contextmanager
def _hooks_lock(path: str):
    """Serialize read/merge/write cycles across concurrent hook passes."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    created = False
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        created = True
    except FileExistsError:
        fd = os.open(path, os.O_RDWR)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield created
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _user_codex_home() -> str:
    configured = os.environ.get("CODEX_HOME")
    path = configured if configured else os.path.expanduser(os.path.join("~", ".codex"))
    return os.path.realpath(os.path.abspath(path))


def _read_document(path: str) -> tuple[str | None, dict, int | None]:
    if not os.path.lexists(path):
        return None, {}, None
    if os.path.islink(path):
        raise CodexHookError("refusing to replace symlinked %s" % path)
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        with open(path, "r", encoding="utf-8") as stream:
            content = stream.read()
        return content, json.loads(content), mode
    except (OSError, ValueError) as exc:
        raise CodexHookError("cannot read %s: %s" % (path, exc)) from exc


def ensure_user_codex_hook() -> CodexHookInstallResult:
    """Install or refresh the guarded user-level Codex SessionStart hook."""
    hooks_path = os.path.join(_user_codex_home(), HOOKS_FILENAME)
    if os.path.islink(hooks_path):
        raise CodexHookError("refusing to replace symlinked %s" % hooks_path)
    try:
        with _hooks_lock(hooks_path + ".lock"):
            old_content, document, mode = _read_document(hooks_path)
            new_document = _merge_hooks(document)
            new_content = json.dumps(new_document, indent=2, ensure_ascii=False) + "\n"
            if old_content == new_content:
                return CodexHookInstallResult(False, hooks_path)
            _atomic_write(hooks_path, new_content, mode)
    except CodexHookError:
        raise
    except OSError as exc:
        raise CodexHookError("cannot write %s: %s" % (hooks_path, exc)) from exc
    return CodexHookInstallResult(True, hooks_path)


def _git_tracked(project_dir: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", project_dir, "ls-files", "--error-unmatch", "--",
             os.path.join(CODEX_DIRNAME, HOOKS_FILENAME)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            timeout=_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _semantically_empty(document: dict) -> bool:
    return not document or (set(document) == {"hooks"} and not document["hooks"])


def strip_project_codex_hook(project_dir: str) -> CodexHookStripResult:
    """Best-effort removal of bootstrap-owned legacy project hooks."""
    project_dir = os.path.abspath(project_dir)
    hooks_path = os.path.join(project_dir, CODEX_DIRNAME, HOOKS_FILENAME)
    lock_path = hooks_path + ".lock"
    if not os.path.isfile(hooks_path) or os.path.islink(hooks_path):
        return CodexHookStripResult(False, hooks_path)

    try:
        old_content, document, mode = _read_document(hooks_path)
        reduced, changed = _strip_hooks(document)
        empty_untracked = _semantically_empty(reduced) and not _git_tracked(project_dir)
        if not changed and not empty_untracked:
            return CodexHookStripResult(False, hooks_path)
    except (CodexHookError, OSError, ValueError, subprocess.SubprocessError):
        return CodexHookStripResult(False, hooks_path)

    delete_file = False
    lock_created = False
    try:
        with _hooks_lock(lock_path) as lock_created:
            old_content, document, mode = _read_document(hooks_path)
            reduced, changed = _strip_hooks(document)
            empty_untracked = _semantically_empty(reduced) and not _git_tracked(project_dir)
            if not changed and not empty_untracked:
                return CodexHookStripResult(False, hooks_path)
            if empty_untracked:
                delete_file = True
            else:
                new_content = json.dumps(reduced, indent=2, ensure_ascii=False) + "\n"
                if old_content != new_content:
                    _atomic_write(hooks_path, new_content, mode)
                return CodexHookStripResult(True, hooks_path)
    except (CodexHookError, OSError, ValueError, subprocess.SubprocessError):
        return CodexHookStripResult(False, hooks_path)
    finally:
        if lock_created:
            try:
                os.unlink(lock_path)
            except OSError:
                pass

    if delete_file:
        try:
            os.unlink(hooks_path)
            if os.path.isfile(lock_path) or os.path.islink(lock_path):
                os.unlink(lock_path)
            try:
                os.rmdir(os.path.dirname(hooks_path))
            except OSError:
                pass
        except OSError:
            pass
    return CodexHookStripResult(True, hooks_path)


def add_additional_context(response: dict, context: str) -> dict:
    """Append fixed context to a Codex SessionStart response."""
    result = copy.deepcopy(response)
    if not context:
        return result
    output = result.setdefault("hookSpecificOutput", {})
    output["hookEventName"] = "SessionStart"
    existing = output.get("additionalContext", "")
    output["additionalContext"] = (
        "%s\n\n%s" % (existing, context) if existing else context
    )
    return result
