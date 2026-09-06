"""Persistent record of where bootstrap-managed tools live on disk.

Bootstrap's target architecture is "record the absolute path of every tool
we resolve, use that path directly forever after." This module is the
single source of truth for that record. See
docs/planning/bootstrap/tool-resolution-redesign.md for the full design.

Contract:
    resolve(name)       -> absolute path string, or None if not recorded.
    record(name, path)  -> persist a tool->path mapping (engine-only).
    all_paths()         -> dict[name, path] for diagnostics.

State file:
    <data_dir>/tool_paths.json. Every function takes ``data_dir`` first:
    pass ``None`` for the canonical centralized location (bootstrap's plugin
    data dir, ~/.claude/plugins/data/plugins-kit/bootstrap — what the engine
    does), or an explicit dir to read/write exactly there (tests).
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .atomic_write import write_atomic

_SCHEMA_VERSION = 1
_STATE_FILENAME = "tool_paths.json"


def canonical_data_dir():
    """Bootstrap's canonical plugin data dir.

    ~/.claude/plugins/data/plugins-kit/bootstrap. The state file
    (tool_paths.json) lives here regardless of which caller invokes
    record() / resolve(), so per-plugin engine passes write to the
    centralized location.
    """
    return os.path.join(
        os.path.expanduser("~"),
        ".claude", "plugins", "data", "plugins-kit", "bootstrap",
    )


def _resolve_data_dir(data_dir):
    # Explicit contract (no basename sniffing — B15): ``None`` means "the
    # canonical centralized location"; any explicit dir is used exactly as
    # given. The engine passes None so per-plugin passes all record to the
    # same central file; tests pass a temp dir and stay fully isolated.
    # (The old heuristic silently redirected any dir whose basename wasn't
    # "bootstrap" to the real production file — a test using a generic tmp
    # dir would have polluted the user's actual tool_paths.json.)
    if data_dir is None:
        return canonical_data_dir()
    return data_dir


def _state_path(data_dir):
    return os.path.join(_resolve_data_dir(data_dir), _STATE_FILENAME)


def _load(data_dir):
    path = _state_path(data_dir)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"_schema_version": _SCHEMA_VERSION, "tools": {}}
    except (json.JSONDecodeError, OSError):
        # Corrupt file: treat as empty rather than crash. Next record()
        # rewrites it cleanly.
        return {"_schema_version": _SCHEMA_VERSION, "tools": {}}
    if not isinstance(data, dict) or "tools" not in data or not isinstance(data["tools"], dict):
        return {"_schema_version": _SCHEMA_VERSION, "tools": {}}
    data.setdefault("_schema_version", _SCHEMA_VERSION)
    return data


def _write_atomic(data_dir, payload):
    target = _state_path(data_dir)
    write_atomic(target, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def resolve(data_dir, name):
    """Return the absolute path recorded for `name`, or None if not recorded.

    `data_dir` is bootstrap's plugin data dir. Callers that don't have it
    handy can compute it as ~/.claude/plugins/data/plugins-kit/bootstrap
    (or read $CLAUDE_PLUGIN_ROOT-relative state if applicable).
    """
    data = _load(data_dir)
    entry = data["tools"].get(name)
    if not entry:
        return None
    path = entry.get("path") if isinstance(entry, dict) else entry
    return path or None


def record(data_dir, name, path):
    """Persist `name -> path` in tool_paths.json. Idempotent.

    Engine-only. Plugins should call `resolve()`, not this.
    """
    if not name or not path:
        return
    path = str(Path(path).absolute())
    data = _load(data_dir)
    existing = data["tools"].get(name)
    existing_path = existing.get("path") if isinstance(existing, dict) else existing
    if existing_path == path:
        # No-op: already recorded with the same path.
        return
    data["tools"][name] = {
        "path": path,
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _write_atomic(data_dir, data)


def all_paths(data_dir):
    """Return {name: path} for every recorded tool. Empty dict if none."""
    data = _load(data_dir)
    out = {}
    for name, entry in data["tools"].items():
        path = entry.get("path") if isinstance(entry, dict) else entry
        if path:
            out[name] = path
    return out


def tool_env_var_name(name):
    """Compute the env var name for a recorded tool.

    Uppercases the tool name and replaces every character that is not a
    valid POSIX shell identifier character (anything other than A-Z, 0-9,
    or underscore) with an underscore. Tool names are derived from binary
    filenames, which can contain dots (e.g. ``draw.io``); a bare dot would
    produce ``BOOTSTRAP_BIN_DRAW.IO``, which is not a valid identifier and
    fails to ``export`` in the shell. Mirrors the convention used by
    bootstrap_lib.venv_check.venv_env_var_name.

    >>> tool_env_var_name("git")
    'BOOTSTRAP_BIN_GIT'
    >>> tool_env_var_name("github-cli")
    'BOOTSTRAP_BIN_GITHUB_CLI'
    >>> tool_env_var_name("draw.io")
    'BOOTSTRAP_BIN_DRAW_IO'
    """
    return "BOOTSTRAP_BIN_" + re.sub(r"[^A-Z0-9_]", "_", name.upper())


def export_tool_env_vars(data_dir):
    """Append BOOTSTRAP_BIN_<TOOL> exports to $CLAUDE_ENV_FILE.

    Mirrors export_venv_env_var: no-op when CLAUDE_ENV_FILE is unset or
    when the recorded path no longer exists on disk (consumers fail fast
    on unset vars rather than silently invoking a stale path). Returns
    the list of exported var names for diagnostics.
    """
    import shlex
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file:
        return []
    exported = []
    for name, path in all_paths(data_dir).items():
        if not os.path.isfile(path):
            continue
        var = tool_env_var_name(name)
        line = f"export {var}={shlex.quote(path)}\n"
        try:
            with open(env_file, "a", encoding="utf-8") as f:
                f.write(line)
            exported.append(var)
        except OSError:
            continue
    return exported
