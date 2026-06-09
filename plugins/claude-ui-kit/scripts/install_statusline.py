"""Bootstrap script: install claude-ui-kit's default statusLine into settings.json.

Behavior:
- If no statusLine is configured in any settings.json layer, install ours into
  the user-global `~/.claude/settings.json`. The statusLine is a user-level
  preference, not a project-level one — installing per-project meant every
  ephemeral cwd Claude was launched in (eval tmp dirs, etc.) got a stray
  `.claude/settings.local.json` written into it.
- If the existing statusLine is already claude-ui-kit's (matches our path
  prefix), refresh it to point at the current installed location. This handles
  plugin upgrades and reinstalls transparently — wherever it was found.
- If the existing statusLine is something else, leave it alone and surface a
  fix-all message asking the user to type "replace my status line" if they
  want to switch.

The script is idempotent: re-running on every SessionStart is a no-op once
installed.
"""

import json
import os
import sys
from pathlib import Path
from typing import Optional, Tuple


PLUGIN_NAME = "claude-ui-kit"
INSTALLED_SCRIPT_RELPATH = "scripts/statusline.sh"
CUSTOMIZED_FLAG = "customized.flag"


def install(ctx) -> None:
    # The /statusline skill writes this marker when the user customizes, so the
    # install script stays quiet on subsequent SessionStarts instead of nagging
    # about a "conflicting" statusLine the user intentionally chose.
    if (Path(ctx.data_dir) / CUSTOMIZED_FLAG).exists():
        ctx.log_ok("statusline: user customized (skipping)")
        return

    installed_script = _resolve_installed_script(ctx.data_dir)
    if installed_script is None:
        ctx.log("statusline: FAILED - synced script not found at "
                f"{Path(ctx.data_dir) / INSTALLED_SCRIPT_RELPATH}")
        return

    expected_command = _posix(installed_script)

    # Use the engine's canonical project_dir (Claude Code's launch CWD).
    # Never walk up looking for .claude/ — Claude Code itself does not, so any
    # parent .claude/ we'd find is a directory Claude Code never reads. Older
    # versions of this script walked up and silently wrote into the wrong file
    # (and worse, created stray .claude/ dirs that polluted sibling projects).
    project_dir_str = getattr(ctx, "project_dir", None)
    project_root = Path(project_dir_str).resolve() if project_dir_str else None

    # Search layers from highest to lowest precedence so the user-visible
    # statusLine is the one we compare against. Per-project layers are checked
    # so we can refresh an existing claude-ui-kit install in-place, but new
    # installs always land in the user-global layer.
    candidate_paths = []
    if project_root is not None:
        candidate_paths.append(project_root / ".claude" / "settings.local.json")
        candidate_paths.append(project_root / ".claude" / "settings.json")
    candidate_paths.append(Path.home() / ".claude" / "settings.local.json")
    candidate_paths.append(Path.home() / ".claude" / "settings.json")

    existing = _find_existing_statusline(candidate_paths)

    if existing is None:
        target = Path.home() / ".claude" / "settings.json"
        if _refuse_unparseable(ctx, target):
            return
        _write_statusline(target, expected_command)
        ctx.log(f"statusline: installed to {_posix(target)}")
        return

    settings_path, current_command = existing

    if current_command == expected_command:
        ctx.log_ok("statusline: already installed (no-op)")
        return

    if _is_ours(current_command):
        # Plugin path moved (upgrade, version bump, scope change). Refresh.
        if _refuse_unparseable(ctx, settings_path):
            return
        _write_statusline(settings_path, expected_command)
        ctx.log(f"statusline: refreshed path in {_posix(settings_path)}")
        return

    # User has a custom statusLine. Don't touch it without explicit consent.
    ctx.add_failure(
        "statusline_conflict",
        settings_path=_posix(settings_path),
        existing_command=current_command,
        new_command=expected_command,
        user_msg=(
            f"claude-ui-kit found an existing statusLine in "
            f"{_posix(settings_path)} and will not overwrite it. To switch "
            f"to claude-ui-kit's default, type 'replace my status line'."
        ),
        agent_msg=(
            f"The user has a custom statusLine configured in "
            f"{_posix(settings_path)} with command: {current_command}\n"
            f"DO NOT modify it. If and only if the user explicitly says "
            f"'replace my status line' (or clearly equivalent intent), "
            f"update {_posix(settings_path)} so that "
            f"statusLine.command = {expected_command} (keep "
            f"statusLine.type = 'command'). Otherwise, leave it alone and "
            f"explain that claude-ui-kit is installed but not active because "
            f"a custom statusLine takes precedence."
        ),
    )


def _resolve_installed_script(data_dir: str) -> Optional[Path]:
    p = Path(data_dir) / INSTALLED_SCRIPT_RELPATH
    return p if p.is_file() else None


def _find_existing_statusline(paths) -> Optional[Tuple[Path, str]]:
    """Return (path, command) of the highest-precedence layer with a statusLine."""
    for path in paths:
        data = _load_json(path)
        if not isinstance(data, dict):
            continue
        sl = data.get("statusLine")
        if isinstance(sl, dict) and isinstance(sl.get("command"), str):
            return path, sl["command"]
    return None


def _is_ours(command: str) -> bool:
    return f"/{PLUGIN_NAME}/" in command.replace("\\", "/")


def _refuse_unparseable(ctx, settings_path: Path) -> bool:
    """Refuse to write over a settings file that exists but cannot be parsed.

    _load_json returns None for both "missing" and "malformed"; writing in the
    malformed case would replace the user's entire settings file with just our
    statusLine block, destroying every other setting. Surface a fix-all
    failure instead and leave the file untouched.
    """
    if not settings_path.is_file() or _load_json(settings_path) is not None:
        return False
    ctx.add_failure(
        "statusline_settings_unparseable",
        settings_path=_posix(settings_path),
        user_msg=(
            f"claude-ui-kit could not parse {_posix(settings_path)} (invalid "
            f"JSON) and will not modify it. Fix the file's JSON syntax, then "
            f"re-run bootstrap to install the status line."
        ),
        agent_msg=(
            f"{_posix(settings_path)} exists but is not valid JSON. DO NOT "
            f"overwrite or truncate it -- it may contain settings the user "
            f"wants to keep. Help the user repair the JSON syntax; once it "
            f"parses, the statusLine install proceeds on the next bootstrap "
            f"run."
        ),
    )
    return True


def _write_statusline(settings_path: Path, command: str) -> None:
    data = _load_json(settings_path) or {}
    if not isinstance(data, dict):
        data = {}
    data["statusLine"] = {"type": "command", "command": command}
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _load_json(path: Path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _posix(p: Path) -> str:
    return str(p).replace("\\", "/")
