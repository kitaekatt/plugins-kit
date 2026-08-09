"""Path resolution and explicit refresh helpers for Unreal API stubs."""

from __future__ import annotations

import json
import shutil
import stat
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

# Files that mark a Perforce workspace. Their presence at or above a path is a
# local, zero-cost signal that the tree is Perforce-managed -- checked before
# telling the user to run a p4 command, and before spawning p4 at all.
_P4_MARKERS = (".p4config.txt", ".p4config", ".p4ignore.txt", ".p4ignore")


class DestinationNotWritableError(Exception):
    """Raised when the durable stub destination exists and is read-only.

    A VCS with checkout semantics (Perforce, and similar) marks a submitted
    file read-only on disk until it is explicitly checked out; a plain
    ``shutil.copy2`` over such a file raises a raw ``PermissionError``. This
    exception carries an actionable message instead -- see
    :func:`refresh_durable_stub`. The fix is never applied automatically: only
    the user may check the file out of version control (or clear the
    read-only flag).
    """


def _is_read_only(path: Path) -> bool:
    try:
        return not (path.stat().st_mode & stat.S_IWRITE)
    except OSError:
        return False


def _in_p4_workspace(path: Path) -> bool:
    """True if a Perforce workspace marker sits at or above ``path``."""
    directory = path.parent
    while True:
        if any((directory / marker).exists() for marker in _P4_MARKERS):
            return True
        parent = directory.parent
        if parent == directory:
            return False
        directory = parent


def _not_writable_message(destination: Path) -> str:
    """Return the actionable message for a read-only durable-stub destination.

    VCS-aware when that is cheap and reliable to detect (a ``p4`` executable
    plus a Perforce workspace marker above ``destination``); a generic
    check-it-out-of-version-control message otherwise. Never guesses a VCS it
    has not confirmed.
    """
    if shutil.which("p4") is not None and _in_p4_workspace(destination):
        return (
            f"{destination} is read-only, most likely because it is checked "
            f"into Perforce. Run `p4 edit {destination}` to check it out, "
            "then re-run this refresh."
        )
    return (
        f"{destination} is read-only. Check it out of version control (or "
        "clear the read-only flag), then re-run this refresh."
    )


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

    if destination.is_file() and source.read_bytes() == destination.read_bytes():
        announce(f"Unreal API stub already up to date at {destination}")
        return destination

    if destination.exists() and _is_read_only(destination):
        raise DestinationNotWritableError(_not_writable_message(destination))

    announce(f"Writing enriched Unreal API stub: {source} -> {destination}")

    # This must remain durable consuming-project data. Bootstrap may check this
    # path, but only this explicit human-invoked action may create or update it.
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination
