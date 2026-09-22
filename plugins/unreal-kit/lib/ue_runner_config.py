"""Configuration loading and durable project-config writing for unreal-kit.

The effective configuration is a deep merge of the shipped defaults, the
skill defaults, the optional user config, and the optional project config. An
explicit ``config_path`` is deliberately isolated: it replaces the discovered
user/project layers while retaining only shipped and skill defaults.
"""

import ast
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = _PLUGIN_DIR / "defaults" / "config.yaml"
SKILL_CONFIG_PATH = _PLUGIN_DIR / "skills" / "ue-python-api" / "ue_runner_config.yaml"

PROJECT_CONFIG_NAME = ".local-data/plugins-kit/unreal-kit/config.yaml"
LEGACY_PROJECT_CONFIG_NAMES = (
    ".local-data/unreal-kit/config.yaml",
    ".claude/unreal-kit.yaml",
)
_GLOBAL_CONFIG_PATH = (
    Path.home() / ".claude" / "plugins" / "data" / "plugins-kit" / "unreal-kit" / "config.yaml"
)
_HOST_RUNNER = (
    '"${BOOTSTRAP_PROJECT_PYTHON:-${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}}" '
    '"${CLAUDE_PLUGIN_ROOT}/skills/ue-python-api/scripts/ue_runner.py"'
)

_DEFAULTS = {
    "remote_execution": {
        "multicast_group": "239.0.0.1",
        "multicast_port": 6766,
        "multicast_bind_address": "127.0.0.1",
    },
}


class ConfigError(ValueError):
    """A supplied config layer cannot be safely read or validated."""


@dataclass
class RemoteConfig:
    multicast_group: str = "239.0.0.1"
    multicast_port: int = 6766
    multicast_bind_address: str = "127.0.0.1"


@dataclass
class RunnerConfig:
    engine_dir: str = ""
    uproject: str = ""
    remote: RemoteConfig = field(default_factory=RemoteConfig)

    @property
    def editor_cmd_exe(self) -> str:
        if not self.engine_dir:
            return ""
        return os.path.join(self.engine_dir, "Binaries", "Win64", "UnrealEditor-Cmd.exe")

    @property
    def editor_exe(self) -> str:
        if not self.engine_dir:
            return ""
        return os.path.join(self.engine_dir, "Binaries", "Win64", "UnrealEditor.exe")

    def validate(self) -> list[str]:
        """Return list of validation errors (empty = valid)."""
        errors = []
        if not self.uproject:
            errors.append(f"uproject path not configured. Run: {_HOST_RUNNER} --setup")
        elif not os.path.isfile(self.uproject):
            errors.append(f"uproject not found: {self.uproject}")

        if not self.engine_dir:
            errors.append(f"engine_dir not configured. Run: {_HOST_RUNNER} --setup")
        elif not os.path.isdir(self.engine_dir):
            errors.append(f"engine_dir not found: {self.engine_dir}")

        exe = self.editor_cmd_exe
        if exe and not os.path.isfile(exe):
            errors.append(f"UnrealEditor-Cmd.exe not found: {exe}")

        return errors


def find_project_config(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` (default CWD) looking for project config."""
    current = (start or Path.cwd()).resolve()
    if not current.is_dir():
        current = current.parent
    for _ in range(10):
        candidate = current / PROJECT_CONFIG_NAME
        if candidate.is_file():
            return candidate
        for legacy_name in LEGACY_PROJECT_CONFIG_NAMES:
            legacy = current / legacy_name
            if legacy.is_file():
                return legacy
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def write_project_config(project_root: Path, data: dict) -> Path:
    """Replace the project config with ``data`` using an atomic same-dir swap.

    This function intentionally retains its historical replacement semantics.
    Interactive setup is responsible for loading the old mapping and merging
    its selected updates before calling it.
    """
    if not isinstance(data, Mapping):
        raise TypeError("project config data must be a mapping")
    config_path = Path(project_root) / PROJECT_CONFIG_NAME
    config_path.parent.mkdir(parents=True, exist_ok=True)
    text = _dump_yaml(_normalize_for_yaml(dict(data)))
    _atomic_write_text(config_path, text)
    return config_path


def load_config(config_path: str | Path | None = None) -> RunnerConfig:
    """Load and validate the effective runner configuration.

    Normal resolution is defaults -> skill -> global -> project. Missing
    optional layers are empty. An explicit path is required and isolated from
    discovered global/project state: defaults -> skill -> explicit.
    """
    merged = _deep_merge({}, _DEFAULTS)
    for path in (DEFAULT_CONFIG_PATH, SKILL_CONFIG_PATH):
        data = _load_yaml(path)
        _validate_layer(data, path)
        merged = _deep_merge(merged, data)

    if config_path is not None:
        explicit = Path(config_path)
        data = _load_yaml(explicit, required=True)
        _validate_layer(data, explicit)
        merged = _deep_merge(merged, data)
    else:
        global_data = _load_yaml(_GLOBAL_CONFIG_PATH)
        _validate_layer(global_data, _GLOBAL_CONFIG_PATH)
        merged = _deep_merge(merged, global_data)
        project_config = find_project_config()
        if project_config:
            project_data = _load_yaml(project_config)
            _validate_layer(project_data, project_config)
            merged = _deep_merge(merged, project_data)

    _validate_layer(merged, None)
    remote_data = merged.get("remote_execution", {})
    return RunnerConfig(
        engine_dir=merged.get("engine_dir", ""),
        uproject=merged.get("uproject", ""),
        remote=RemoteConfig(
            multicast_group=remote_data.get("multicast_group", "239.0.0.1"),
            multicast_port=remote_data.get("multicast_port", 6766),
            multicast_bind_address=remote_data.get("multicast_bind_address", "127.0.0.1"),
        ),
    )


def _load_yaml(path: str | Path, *, required: bool = False) -> dict:
    """Read one YAML layer, distinguishing absent optional files from errors."""
    path = Path(path)
    try:
        exists = path.exists()
        is_file = path.is_file()
    except OSError as exc:
        raise ConfigError(f"cannot read config layer {path}: {exc}") from exc
    if not exists:
        if required:
            raise ConfigError(f"config layer {path} is missing")
        return {}
    if not is_file:
        raise ConfigError(f"cannot read config layer {path}: path is not a file")

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read config layer {path}: {exc}") from exc

    try:
        import yaml
    except ImportError:
        try:
            data = _parse_yaml_simple(path, text=text)
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f"malformed YAML in config layer {path}: {exc}") from exc
    else:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConfigError(f"malformed YAML in config layer {path}: {exc}") from exc

    if data is None:
        if not text.strip():
            return {}
        raise ConfigError(f"config layer {path} must be a mapping, got null")
    if not isinstance(data, dict):
        raise ConfigError(
            f"config layer {path} must be a mapping at the top level, got {type(data).__name__}"
        )
    if any(not isinstance(key, str) for key in data):
        raise ConfigError(f"config layer {path} has a non-string top-level key")
    return data


def _parse_yaml_simple(path: Path, *, text: str | None = None) -> dict:
    """Parse the small mapping subset needed by the stdlib stale hook.

    Quoted scalars are decoded before type coercion, so ``"6766"`` remains a
    string and ``""`` remains an empty string. Unsupported or malformed input
    raises a named ``ConfigError`` rather than silently selecting defaults.
    """
    if text is None:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ConfigError(f"cannot read config layer {path}: {exc}") from exc

    result: dict[str, Any] = {}
    section: str | None = None
    section_indent = 0
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" \t"))
        if "\t" in raw_line[:indent]:
            raise ConfigError(f"malformed YAML in config layer {path}: tabs in indentation at line {line_number}")
        content = _strip_yaml_comment(raw_line[indent:]).strip()
        if not content:
            continue
        if ":" not in content:
            raise ConfigError(f"malformed YAML in config layer {path}: missing ':' at line {line_number}")
        key, raw_value = content.split(":", 1)
        key = key.strip()
        if not key or any(ch in key for ch in "[]{}"):
            raise ConfigError(f"malformed YAML in config layer {path}: invalid key at line {line_number}")
        raw_value = raw_value.strip()
        if indent == 0:
            if raw_value == "":
                result[key] = {}
                section = key
                section_indent = indent
            else:
                result[key] = _parse_scalar(raw_value, path, line_number)
                section = None
            continue
        if section is None or indent <= section_indent or not isinstance(result.get(section), dict):
            raise ConfigError(f"malformed YAML in config layer {path}: unexpected indentation at line {line_number}")
        if raw_value == "":
            raise ConfigError(f"malformed YAML in config layer {path}: nested mapping too deep at line {line_number}")
        result[section][key] = _parse_scalar(raw_value, path, line_number)
    return result


def _strip_yaml_comment(value: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if char in ("'", '"'):
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
            continue
        if char == "#" and quote is None and (index == 0 or value[index - 1].isspace()):
            return value[:index]
    return value


def _parse_scalar(raw: str, path: Path, line_number: int) -> Any:
    if raw[:1] in ("'", '"'):
        try:
            value = ast.literal_eval(raw)
        except (SyntaxError, ValueError) as exc:
            raise ConfigError(f"malformed YAML in config layer {path}: invalid quoted value at line {line_number}") from exc
        return value
    if raw in ("null", "Null", "NULL", "~"):
        return None
    if raw.lower() in ("true", "yes"):
        return True
    if raw.lower() in ("false", "no"):
        return False
    if raw.startswith("[") or raw.startswith("{"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"malformed YAML in config layer {path}: invalid flow value at line {line_number}") from exc
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _validate_layer(data: Mapping[str, Any], path: Path | None) -> None:
    """Validate fields whose wrong type could change the execution target."""
    label = f"config layer {path}" if path is not None else "effective config"
    if not isinstance(data, Mapping):
        raise ConfigError(f"{label} must be a mapping")
    for key in ("engine_dir", "uproject", "plugin_data_dir"):
        if key in data and not isinstance(data[key], str):
            raise ConfigError(f"{label}: {key} must be a string")
    if "remote_execution" not in data:
        return
    remote = data["remote_execution"]
    if not isinstance(remote, Mapping):
        raise ConfigError(f"{label}: remote_execution must be a mapping")
    for key in ("multicast_group", "multicast_bind_address"):
        if key in remote and (not isinstance(remote[key], str) or not remote[key]):
            raise ConfigError(f"{label}: remote_execution.{key} must be a non-empty string")
    if "multicast_port" in remote:
        port = remote["multicast_port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ConfigError(f"{label}: remote_execution.multicast_port must be an integer from 1 to 65535")


def _deep_merge(base: dict, override: Mapping[str, Any]) -> dict:
    """Merge mappings recursively; later non-empty leaves win."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, Mapping):
            result[key] = _deep_merge(result[key], value)
        elif value is not None and value != "":
            result[key] = value
    return result


def _dump_yaml(data: dict) -> str:
    try:
        import yaml
    except ImportError:
        return _dump_yaml_simple(data)
    try:
        text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    except Exception as exc:
        raise OSError(f"cannot serialize project config: {exc}") from exc
    return text if text.endswith("\n") else text + "\n"


def _normalize_for_yaml(value: Any) -> Any:
    """Keep the established forward-slash representation for path strings."""
    if isinstance(value, str):
        return value.replace("\\", "/")
    if isinstance(value, Mapping):
        return {key: _normalize_for_yaml(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_for_yaml(item) for item in value]
    return value


def _dump_yaml_simple(data: Mapping[str, Any], indent: int = 0) -> str:
    lines: list[str] = []
    prefix = " " * indent
    for key, value in data.items():
        if isinstance(value, Mapping):
            lines.append(f"{prefix}{key}:")
            lines.append(_dump_yaml_simple(value, indent + 2).rstrip("\n"))
        elif isinstance(value, str):
            lines.append(f"{prefix}{key}: {json.dumps(value)}")
        elif value is None:
            lines.append(f"{prefix}{key}: null")
        elif isinstance(value, bool):
            lines.append(f"{prefix}{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{prefix}{key}: {value}")
        else:
            lines.append(f"{prefix}{key}: {json.dumps(value)}")
    return "\n".join(lines) + "\n"


def _atomic_write_text(path: Path, text: str) -> None:
    candidate: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            candidate = stream.name
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(candidate, path)
        candidate = None
    finally:
        if candidate:
            try:
                os.unlink(candidate)
            except OSError:
                pass
