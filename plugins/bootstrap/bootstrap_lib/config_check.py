"""Plugin config init, validation, and autodetect lifecycle."""

import importlib.util
import os
import shutil
from typing import Any, Dict, List, Optional, Tuple


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
        if value:
            continue

        default = field_def.get("default")
        if default is not None:
            config[field_name] = default
            changed = True
            continue

        # Missing field — collect for fix-all
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
        message (never swallowed — the logging contract, B8).
    """
    _empty = (False, [], [])
    parts = autodetect_spec.split()
    if len(parts) != 2:
        return _empty

    script_rel, func_name = parts
    script_path = os.path.join(plugin_root, script_rel)

    if not os.path.isfile(script_path):
        return _empty

    try:
        spec = importlib.util.spec_from_file_location("_autodetect", script_path)
        if spec is None or spec.loader is None:
            return _empty
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        func = getattr(module, func_name, None)
        if func is None:
            return _empty

        result = func(config, config_path)

        # Support dict return with messages
        if isinstance(result, dict):
            changed = bool(result.get("changed", False))
            actions = list(result.get("actions", []))
            ok = list(result.get("ok", []))
            return (changed, actions, ok)

        # Backward compat: plain bool
        return (bool(result), [], [])
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
            message is appended here so the caller can log it (B8 — a crashed
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
    """Load a YAML config file. Returns empty dict on failure."""
    try:
        import yaml
    except ImportError:
        return _load_yaml_fallback(config_path)

    try:
        with open(config_path, "r") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def save_yaml_config(config_path: str, config: Dict[str, Any]) -> None:
    """Save config dict as YAML."""
    try:
        import yaml
    except ImportError:
        _save_yaml_fallback(config_path, config)
        return

    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


def _load_yaml_fallback(config_path: str) -> Dict[str, Any]:
    """Simple key: value YAML parser fallback when PyYAML unavailable."""
    result = {}
    try:
        with open(config_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line:
                    key, value = line.split(":", 1)
                    value = value.strip().strip('"').strip("'")
                    result[key.strip()] = value
    except OSError:
        pass
    return result


def _save_yaml_fallback(config_path: str, config: Dict[str, Any]) -> None:
    """Simple key: value YAML writer fallback when PyYAML unavailable."""
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w") as f:
        for key, value in config.items():
            if isinstance(value, str) and (not value or " " in value or ":" in value):
                f.write(f'{key}: "{value}"\n')
            else:
                f.write(f"{key}: {value}\n")
