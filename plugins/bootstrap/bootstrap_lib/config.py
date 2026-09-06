"""Bootstrap config loading, migration, and persistence."""

import json
import os
import shutil
from copy import deepcopy
from typing import Any

from .atomic_write import write_atomic

CURRENT_SCHEMA_VERSION = 6


def _defaults_value(defaults_dir: str, *path: str, fallback: Any) -> Any:
    """Read one value from defaults/config.json, or return its fallback."""
    if not defaults_dir:
        return fallback

    defaults_path = os.path.join(defaults_dir, "config.json")
    try:
        with open(defaults_path, "r") as f:
            value = json.load(f)
        for key in path:
            if not isinstance(value, dict):
                return fallback
            value = value.get(key)
        return value
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return fallback


def load_config(data_dir: str, defaults_dir: str) -> dict:
    """Load config from data dir, copying defaults if missing.

    Args:
        data_dir: User data directory (e.g. ~/.claude/plugins/data/bootstrap/)
        defaults_dir: Plugin defaults directory containing config.json

    Returns:
        Parsed config dict
    """
    config_path = os.path.join(data_dir, "config.json")
    defaults_path = os.path.join(defaults_dir, "config.json")

    if not os.path.exists(config_path):
        os.makedirs(data_dir, exist_ok=True)
        shutil.copy2(defaults_path, config_path)

    with open(config_path, "r") as f:
        config = json.load(f)

    migrated = migrate_config(config, defaults_dir=defaults_dir)
    if migrated is not config:
        save_config(data_dir, migrated)
        return migrated

    return config


def migrate_config(config: dict, defaults_dir: str = "") -> dict:
    """Migrate config to current schema version.

    Args:
        config: Config dict to migrate.
        defaults_dir: Path to defaults directory (used for v5 migration to read self_setup).

    Returns the same dict if no migration needed, or a new dict if migrated.
    """
    version = config.get("schema_version", 0)

    if version >= CURRENT_SCHEMA_VERSION:
        return config

    # Copy to avoid mutating the original
    migrated = deepcopy(config)

    # Migration from v0 to v1: add missing fields
    if version < 1:
        migrated.setdefault("enabled_plugins", [])
        migrated["schema_version"] = 1

    # Migration from v1 to v2: add log_success settings
    if version < 2:
        migrated.setdefault("log_success_shell", True)
        migrated.setdefault("log_success_checks", True)
        migrated["schema_version"] = 2

    # Migration from v2 to v3: disable success logging by default
    if version < 3:
        migrated["log_success_shell"] = False
        migrated["log_success_checks"] = False
        migrated["schema_version"] = 3

    # Migration from v3 to v4: replace enabled_plugins with auto-discovery fields
    if version < 4:
        migrated.pop("enabled_plugins", None)
        migrated.setdefault("no_bootstrap", [])
        migrated.setdefault("bootstrap_cache", [])
        migrated["schema_version"] = 4

    # Migration from v4 to v5: add self_setup from defaults
    if version < 5:
        self_setup = _defaults_value(defaults_dir, "self_setup", fallback=None)

        # Hardcoded fallback if defaults not available
        if self_setup is None:
            self_setup = {
                "tools": [
                    {"name": "uv", "install": {"macos": "curl -LsSf https://astral.sh/uv/install.sh | sh", "windows": "curl -LsSf https://astral.sh/uv/install.sh | sh", "ubuntu": "curl -LsSf https://astral.sh/uv/install.sh | sh"}},
                    {"name": "git", "install": {"macos": "brew install git", "windows": "winget install --id Git.Git -e --source winget", "ubuntu": "sudo apt install -y git"}},
                ],
                "path_entries": ["~/.local/bin", "~/.local/share/python-standalone/python"],
                "venv": {"check_imports": ["yaml"]},
            }

        migrated["self_setup"] = self_setup

        # Clean auto-populated no_bootstrap entries from the old bootstrap: false mechanism
        no_bootstrap = migrated.get("no_bootstrap", [])
        for auto_ref in ("bootstrap@plugins-kit",):
            if auto_ref in no_bootstrap:
                no_bootstrap.remove(auto_ref)

        migrated["schema_version"] = 5

    # Migration from v5 to v6: add python_stub_check to self_setup
    if version < 6:
        stub_check = _defaults_value(
            defaults_dir, "self_setup", "python_stub_check", fallback=None
        )

        # Hardcoded fallback if defaults not available
        if stub_check is None:
            stub_check = {
                "good_python_dir": "~/.local/share/python-standalone/python",
                "stub_markers": ["WindowsApps"],
                "script_output_dir": "~/Desktop",
            }

        self_setup = migrated.setdefault("self_setup", {})
        self_setup.setdefault("python_stub_check", stub_check)

        migrated["schema_version"] = 6

    return migrated


def save_config(data_dir: str, config: dict) -> None:
    """Write config back to data dir.

    Args:
        data_dir: User data directory
        config: Config dict to save
    """
    config_path = os.path.join(data_dir, "config.json")
    os.makedirs(data_dir, exist_ok=True)
    write_atomic(config_path, json.dumps(config, indent=2) + "\n")
