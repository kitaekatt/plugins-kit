"""Path resolution and explicit refresh helpers for Unreal API stubs."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

from bootstrap_lib.config_resolve import (
    default_data_root,
    resolve_config,
    resolve_plugin_data_dir,
    standard_config_layers,
)

MARKETPLACE = "plugins-kit"
PLUGIN = "unreal-kit"


def load_effective_config(project_root: Path) -> dict:
    """Load the user and consuming-project config layers."""
    return resolve_config(
        standard_config_layers(
            plugin=PLUGIN,
            marketplace=MARKETPLACE,
            project_root=project_root,
        )
    )


def generated_stub_path(config: Mapping[str, object]) -> Path | None:
    """Return the editor-generated stub path, if a project is configured."""
    uproject = config.get("uproject")
    if not isinstance(uproject, str) or not uproject:
        return None
    return Path(uproject).parent / "Intermediate" / "PythonStub" / "unreal.py"


def durable_stub_path(
    project_root: Path,
    config: Mapping[str, object],
) -> Path:
    """Resolve the consuming project's durable enriched-stub path."""
    return (
        resolve_plugin_data_dir(
            project_root,
            marketplace=MARKETPLACE,
            plugin=PLUGIN,
            config=config,
        )
        / "unreal.py"
    )


def stock_stub_path() -> Path:
    """Return the machine-local stock-stub path provisioned by bootstrap."""
    return default_data_root() / MARKETPLACE / PLUGIN / "stubs" / "unreal.py"


def deferred_requirement_message(name: str) -> str | None:
    """Read bootstrap's prepared point-of-need statement for one requirement."""
    path = (
        default_data_root()
        / MARKETPLACE
        / PLUGIN
        / "deferred_requirements.json"
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for requirement in payload.get("requirements", []):
        if requirement.get("name") == name:
            message = requirement.get("agent_msg")
            return message if isinstance(message, str) else None
    return None


def select_search_stub(
    project_root: Path,
    config: Mapping[str, object],
) -> Path | None:
    """Prefer the durable enriched stub, then the machine-local stock stub."""
    enriched = durable_stub_path(project_root, config)
    if enriched.is_file():
        return enriched
    stock = stock_stub_path()
    return stock if stock.is_file() else None


def refresh_durable_stub(
    project_root: Path,
    config: Mapping[str, object],
    announce: Callable[[str], None],
) -> Path:
    """Explicitly copy the editor-generated stub into durable project data."""
    source = generated_stub_path(config)
    if source is None:
        raise ValueError("uproject path is not configured")
    if not source.is_file():
        raise FileNotFoundError(source)

    destination = durable_stub_path(project_root, config)
    announce(f"Writing enriched Unreal API stub: {source} -> {destination}")

    # This must remain durable consuming-project data. Bootstrap may check this
    # path, but only this explicit human-invoked action may create or update it.
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination
