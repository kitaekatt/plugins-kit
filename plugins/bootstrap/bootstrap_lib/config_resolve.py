"""Runtime layered config resolution for bootstrap-managed plugins.

This is the read-time counterpart to config_check.py (which provisions/seeds
config at session start). Where config_check copies a default file once and
write-mirrors declared scalar fields, this module RESOLVES a plugin's effective
config every time it is read, by deep-merging an ordered stack of layers:

    shipped defaults  (lowest precedence)
      -> user config   (~/.claude/plugins/data/<marketplace>/<plugin>/<file>)
        -> project override (<project_root>/.local-data/<marketplace>/<plugin>/<file>)  (highest)

The standard config filename is ``config.yaml`` (matching the existing config /
project_config manifest sections); a plugin's OpenRouter or other settings are
just keys in that one file. User and project dirs use the same
``<marketplace>/<plugin>`` layout so a file seeded at one is found at the other.

Later layers win. Nested mappings are deep-merged (so a project file can add a
single key under `models:` without dropping the user's other models); scalars
from a higher layer replace lower ones. The merge reuses the same
``_deep_merge_dicts`` the manifest layering uses, so semantics stay consistent.

Design rules:
- Never silently swallow a broken layer. A malformed YAML file, an unreadable
  file, or a non-mapping top level raises ConfigError -- a silent ``{}`` would
  hide a typo'd override and is exactly the failure mode this module replaces.
- An ABSENT layer is not an error -- it is simply skipped (that is how the
  precedence stack degrades when a user or project has not written an override).
- PyYAML is required. The flat ``key: value`` fallback in config_check cannot
  represent a nested registry, so rather than mis-parse one we fail loudly with
  an actionable message. PyYAML is a declared bootstrap dependency.
"""

from pathlib import Path
from pathlib import PureWindowsPath
from typing import Iterable, List, Mapping, Optional, Union

from .manifest_merge import _deep_merge_dicts

PathLike = Union[str, Path]


class ConfigError(Exception):
    """A config layer could not be read or parsed. Raised, never swallowed."""


def _require_yaml():
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch in tests
        raise ConfigError(
            "PyYAML is required to read layered config files but is not importable. "
            "It is a declared bootstrap dependency; install it into the bootstrap venv."
        ) from exc
    return yaml


def load_config_layer(path: PathLike) -> Optional[dict]:
    """Read one YAML config layer.

    Returns:
        - ``None`` if the file does not exist (an absent layer is skipped, not an error).
        - ``{}`` if the file exists but is empty.
        - the parsed mapping otherwise.

    Raises:
        ConfigError: the file is unreadable, contains malformed YAML, or its top
            level is not a mapping.
    """
    p = Path(path)
    if not p.exists():
        return None

    yaml = _require_yaml()
    try:
        text = p.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except OSError as exc:
        raise ConfigError(f"cannot read config layer {p}: {exc}") from exc
    except UnicodeError as exc:
        raise ConfigError(f"malformed YAML in config layer {p}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"malformed YAML in config layer {p}: {exc}") from exc

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"config layer {p} must be a mapping at the top level, got {type(data).__name__}"
        )
    return data


def resolve_config(layers: Iterable[PathLike]) -> dict:
    """Deep-merge config layers in INCREASING precedence order (later wins).

    Absent layers are skipped; malformed layers raise ConfigError. Returns an
    empty dict if no layer is present.
    """
    result: dict = {}
    for layer in layers:
        data = load_config_layer(layer)
        if data:
            result = _deep_merge_dicts(result, data)
    return result


def default_data_root() -> Path:
    """The canonical bootstrap plugin-data root (``~/.claude/plugins/data``)."""
    return Path.home() / ".claude" / "plugins" / "data"


def standard_config_layers(
    filename: str = "config.yaml",
    *,
    plugin: str,
    marketplace: str = "plugins-kit",
    shipped_default: Optional[PathLike] = None,
    project_root: Optional[PathLike] = None,
    data_root: Optional[PathLike] = None,
) -> List[Path]:
    """Build the standard precedence-ordered layer paths for a plugin's config.

    ``filename`` defaults to ``config.yaml`` -- the established per-plugin config
    name (plugins put their settings as keys in that one file).

    Order (lowest -> highest precedence):
        1. ``shipped_default`` (if given) -- the plugin's checked-in defaults file.
        2. user config -- ``<data_root>/<marketplace>/<plugin>/<filename>``.
        3. project override -- ``<project_root>/.local-data/<marketplace>/<plugin>/<filename>``
           (only if ``project_root`` is given).

    User and project use the same ``<marketplace>/<plugin>`` layout so a file
    seeded at one location is the same file resolved at the other. Pass the
    result straight to ``resolve_config``.
    """
    layers: List[Path] = []
    if shipped_default is not None:
        layers.append(Path(shipped_default))
    root = Path(data_root) if data_root is not None else default_data_root()
    layers.append(root / marketplace / plugin / filename)
    if project_root is not None:
        layers.append(Path(project_root) / ".local-data" / marketplace / plugin / filename)
    return layers


def resolve_plugin_data_dir(
    project_root: PathLike,
    *,
    marketplace: str,
    plugin: str,
    config: Optional[Mapping[str, object]] = None,
) -> Path:
    """Resolve a plugin's durable, project-versioned data directory.

    The default is
    ``<project_root>/.plugin-data/<marketplace>/<plugin>``. A project config
    may relocate it with ``plugin_data_dir``; the override must be a relative
    path and is resolved from ``project_root``.

    This function only resolves a path. It never creates the directory or
    writes durable data.
    """
    root = Path(project_root).resolve()
    override = config.get("plugin_data_dir") if config else None
    if override in (None, ""):
        return root / ".plugin-data" / marketplace / plugin
    if not isinstance(override, str):
        raise ConfigError("plugin_data_dir must be a relative path string")

    override_path = Path(override)
    windows_override = PureWindowsPath(override)
    if override_path.is_absolute() or windows_override.drive or windows_override.root:
        raise ConfigError(
            f"plugin_data_dir override {override!r} must be relative to the project root {root}"
        )

    resolved = (root / override_path).resolve()
    if not resolved.is_relative_to(root):
        raise ConfigError(
            f"plugin_data_dir override {override!r} resolves outside project root {root}"
        )
    return resolved
