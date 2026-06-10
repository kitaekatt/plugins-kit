#!/usr/bin/env python3
"""Background detector for unreal-kit editor staleness.

Reads the PreToolUse hook JSON from stdin (for cwd), resolves the per-project
config through the canonical resolver in lib/ue_runner_config.py (current path
plus legacy fallbacks, walking up from cwd -- the same resolution ue_runner.py
uses), compares UnrealEditor-BuildSettings.dll mtime vs
Engine/Build/Build.version mtime, and writes or removes the per-project marker
plus the claude-ui-kit system message.

Source-build gate: the dll-vs-Build.version mtime comparison is only
meaningful for SOURCE builds, where a sync updates Build.version and the
developer must rebuild. Launcher/binary engine installs ship
Engine/Build/InstalledBuild.txt; for those the staleness check is skipped
(and any leftover marker cleared) -- otherwise an installed engine could
warn on every MCP call forever.

Marker path:  <cwd>/.local-data/plugins-kit/unreal-kit/editor-stale.flag
System msg:   <cwd>/.local-data/claude-ui-kit/systemmessage.unreal-kit.txt

Cleans up after the path move: any stale marker at the old
<cwd>/.local-data/unreal-kit/ location (and its empty directory) is removed.

Runs under `uv run --no-project python` (see check-editor-build-fresh.sh), a
bare interpreter with no pyyaml -- ue_runner_config falls back to its simple
line parser in that case.

Latency is not foreground-critical: this runs detached after the PreToolUse
hook has already returned. The marker it writes is consumed by subsequent
PreToolUse invocations.

Defensive: any failure to locate the config or referenced files results in a
no-op (preserves prior marker state). The hook is advisory, not safety-critical.
"""
import json
import os
import sys
from pathlib import Path

# hooks/pretooluse/ -> unreal-kit/lib (the plugin's shared config resolver)
_LIB_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lib")
)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

SYSMSG_TEXT = "Editor needs rebuild"


def read_engine_dir(cwd: str) -> str | None:
    """Resolve engine_dir via the canonical per-project config resolver.

    Delegates path resolution (current config name + legacy fallbacks +
    walk-up) and YAML parsing to lib/ue_runner_config.py so the hook can
    never drift from the runner's resolution order.
    """
    try:
        from ue_runner_config import _load_yaml, find_project_config

        config_path = find_project_config(Path(cwd))
        if not config_path:
            return None
        value = _load_yaml(config_path).get("engine_dir")
        return str(value) if value else None
    except Exception:
        return None


def _touch(path: str, content: str | None = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if content is None:
        with open(path, "a"):
            pass
        os.utime(path, None)
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)


def _remove(path: str) -> None:
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _rmdir_if_empty(path: str) -> None:
    """Best-effort: drop a directory if it is now empty. Never raises."""
    try:
        os.rmdir(path)
    except OSError:
        pass


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    cwd = payload.get("cwd")
    if not cwd:
        return 0

    marker = os.path.join(cwd, ".local-data", "plugins-kit", "unreal-kit", "editor-stale.flag")
    old_marker = os.path.join(cwd, ".local-data", "unreal-kit", "editor-stale.flag")
    sysmsg = os.path.join(cwd, ".local-data", "claude-ui-kit", "systemmessage.unreal-kit.txt")

    engine_dir = read_engine_dir(cwd)

    # Clean up after the path move: drop any stale marker at the old location
    # (and its now-empty directory) regardless of the staleness outcome.
    _remove(old_marker)
    _rmdir_if_empty(os.path.dirname(old_marker))

    if not engine_dir:
        return 0

    # Launcher/binary engine installs (Engine/Build/InstalledBuild.txt) are
    # never rebuilt locally; the mtime heuristic below would flag them stale
    # forever. Skip the check and clear any leftover marker.
    if os.path.isfile(os.path.join(engine_dir, "Build", "InstalledBuild.txt")):
        _remove(marker)
        _remove(sysmsg)
        return 0

    dll = os.path.join(engine_dir, "Binaries", "Win64", "UnrealEditor-BuildSettings.dll")
    version_file = os.path.join(engine_dir, "Build", "Build.version")
    if not os.path.isfile(dll) or not os.path.isfile(version_file):
        return 0

    is_stale = os.path.getmtime(dll) < os.path.getmtime(version_file)

    if is_stale:
        _touch(marker)
        _touch(sysmsg, SYSMSG_TEXT)
    else:
        _remove(marker)
        _remove(sysmsg)

    return 0


if __name__ == "__main__":
    sys.exit(main())
