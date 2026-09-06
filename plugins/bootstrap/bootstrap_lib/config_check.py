"""Plugin config init, validation, and autodetect lifecycle."""

import importlib.util
import os
import shutil
from typing import Any, Dict, List, Optional, Tuple

from .atomic_write import write_atomic
from .config_resolve import ConfigError, _require_yaml, load_config_layer


def config_init(plugin_data_dir: str, plugin_root: str, defaults_source: str, config_file: str) -> str:
    """Copy default config to data dir if it doesn't exist.

    Args:
        plugin_data_dir: Plugin's data directory
        plugin_root: Plugin's root directory (where defaults_source lives)
        defaults_source: Relative path from plugin_root to defaults file
        config_file: Config filename in data dir

    Returns:
        Absolute path to the config file
    """
    config_path = os.path.join(plugin_data_dir, config_file)
    if not os.path.exists(config_path):
        source = os.path.join(plugin_root, defaults_source)
        os.makedirs(plugin_data_dir, exist_ok=True)
        shutil.copy2(source, config_path)
    return config_path


def config_validate(
    config: Dict[str, Any],
    required_fields: Dict[str, Dict[str, str]],
    config_path: str,
) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """Validate config fields, applying defaults where declared.

    A field is missing when its key is absent, or its value is ``None`` or an
    empty string. Explicit ``False`` and ``0`` values are valid configuration.

    Args:
        config: Parsed config dict
        required_fields: Field definitions from manifest
        config_path: Path to config file (for message expansion)

    Returns:
        Tuple of (updated config dict, list of missing field dicts with user_msg/agent_msg)
    """
    changed = False
    missing = []

    for field_name, field_def in required_fields.items():
        value = config.get(field_name)
        if field_name in config and value is not None and value != "":
            continue

        default = field_def.get("default")
        if default is not None:
            config[field_name] = default
            changed = True
            continue

        # Missing field -- collect for fix-all
        missing.append({
            "field": field_name,
            "user_msg": field_def.get("user_msg", field_name),
            "agent_msg": field_def.get("agent_msg", f"Set {field_name} in {config_path}").replace(
                "{config_path}", config_path
            ),
        })

    return config, missing


def run_autodetect(
    plugin_root: str,
    autodetect_spec: str,
    config: Dict[str, Any],
    config_path: str,
) -> Tuple[bool, List[str], List[str]]:
    """Run a plugin's autodetect script.

    Args:
        plugin_root: Plugin root directory
        autodetect_spec: "<script_path> <function_name>" from manifest
        config: Config dict to pass to the autodetect function
        config_path: Path to config file

    Returns:
        Tuple of (changed, action_messages, ok_messages).
        Autodetect functions may return bool (backward compat) or a dict with
        keys: changed (bool), actions (list[str]), ok (list[str]).
        If the autodetect script raises, the error is surfaced as an action
        message (never swallowed -- the logging contract, B8).
    """
    parts = autodetect_spec.split()
    if len(parts) != 2:
        return (False, [f"config autodetect FAILED - invalid spec {autodetect_spec!r}"], [])

    script_rel, func_name = parts
    script_path = os.path.join(plugin_root, script_rel)

    if not os.path.isfile(script_path):
        return (False, [f"config autodetect FAILED - missing script {script_path}"], [])

    try:
        spec = importlib.util.spec_from_file_location("_autodetect", script_path)
        if spec is None or spec.loader is None:
            return (False, [f"config autodetect FAILED - cannot load script {script_path}"], [])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        func = getattr(module, func_name, None)
        if func is None:
            return (
                False,
                [f"config autodetect FAILED - missing function {func_name} in {script_path}"],
                [],
            )

        result = func(config, config_path)

        # Support dict return with messages
        if isinstance(result, dict):
            changed = bool(result.get("changed", False))
            actions = list(result.get("actions", []))
            ok = list(result.get("ok", []))
            # Never silent: a script that reports nothing still yields one entry.
            if changed and not actions:
                actions = ["config autodetect: changed"]
            if not changed and not actions and not ok:
                ok = ["config autodetect: ok - unchanged"]
            return (changed, actions, ok)

        # Backward compat: plain bool
        if result:
            return (True, ["config autodetect: changed"], [])
        return (False, [], ["config autodetect: ok - unchanged"])
    except Exception as e:
        # Non-fatal but never silent: route the error to the action entries
        # via the actions channel so the user sees the autodetect broke.
        return (False, [f"config autodetect FAILED - {e}"], [])


def run_project_autodetect(
    plugin_root: str,
    autodetect_spec: str,
    errors: Optional[List[str]] = None,
) -> Optional[Dict[str, str]]:
    """Run a project_config autodetect. Returns dict of discovered field values, or None.

    Unlike run_autodetect, this calls the function with no arguments and expects
    a dict return (field name -> value) or None.

    Args:
        plugin_root: Plugin root directory
        autodetect_spec: "<script_path> <function_name>" from manifest
        errors: Optional list; when the autodetect script raises, an error
            message is appended here so the caller can log it (B8 -- a crashed
            autodetect must not silently read as "no project detected").

    Returns:
        Dict of discovered field values, or None if nothing detected.
    """
    parts = autodetect_spec.split()
    if len(parts) != 2:
        return None

    script_rel, func_name = parts
    script_path = os.path.join(plugin_root, script_rel)

    if not os.path.isfile(script_path):
        return None

    try:
        spec = importlib.util.spec_from_file_location("_project_autodetect", script_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        func = getattr(module, func_name, None)
        if func is None:
            return None

        result = func()

        if isinstance(result, dict):
            return result
        return None
    except Exception as e:
        if errors is not None:
            errors.append(f"project autodetect FAILED - {e}")
        return None


def load_yaml_config(config_path: str) -> Dict[str, Any]:
    """Load a YAML config file, returning an empty dict only when absent/empty."""
    data = load_config_layer(config_path)
    return {} if data is None else data


def save_yaml_config(config_path: str, config: Dict[str, Any]) -> None:
    """Save config dict as YAML."""
    yaml = _require_yaml()
    text = yaml.safe_dump(config, default_flow_style=False, sort_keys=False)
    write_atomic(config_path, text)
