"""Path resolution and explicit refresh helpers for Unreal API stubs."""

from __future__ import annotations

import ast
import json
import os
import shutil
import stat
import tempfile
import tokenize
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


class StubValidationError(ValueError):
    """Raised when a stub is not readable, nonempty, and valid Python text."""


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


def _validate_stub(path: Path) -> bytes:
    """Validate a stub as Python source without importing or executing it."""
    try:
        if not path.is_file():
            raise StubValidationError(f"stub source is not a regular file: {path}")
        if path.stat().st_size == 0:
            raise StubValidationError(f"stub source is empty: {path}")
        with tokenize.open(str(path)) as handle:
            source = handle.read()
        if not source.strip():
            raise StubValidationError(f"stub source is empty: {path}")
        ast.parse(source, filename=str(path), mode="exec")
        return path.read_bytes()
    except StubValidationError:
        raise
    except (OSError, SyntaxError, UnicodeError, LookupError) as exc:
        raise StubValidationError(
            f"stub source is not readable valid Python: {path}: {exc}"
        ) from exc


def _remove_candidate(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # Preserve the original copy/replace error if a platform denies cleanup.
        pass


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
    announce: Callable[[str], None] | None = None,
) -> Path | None:
    """Prefer usable enriched data, then usable stock data.

    A malformed or empty enriched file is not treated as project-specific
    coverage. When stock data is usable, the optional callback receives an
    explicit fallback diagnostic so callers can disclose the reduced source.
    """
    enriched = durable_stub_path(project_root, config)
    enriched_error: StubValidationError | None = None
    if enriched.exists():
        try:
            _validate_stub(enriched)
        except StubValidationError as exc:
            enriched_error = exc
        else:
            return enriched
    stock = stock_stub_path()
    if stock.exists():
        try:
            _validate_stub(stock)
        except StubValidationError as exc:
            if announce is not None:
                announce(f"Stock Unreal API stub is unusable: {exc}")
            return None
        if enriched_error is not None and announce is not None:
            announce(
                "Enriched Unreal API stub is unusable; falling back to the "
                f"machine-local stock stub: {enriched_error}"
            )
        return stock
    if enriched_error is not None and announce is not None:
        announce(f"Enriched Unreal API stub is unusable: {enriched_error}")
    return None


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

    source_bytes = _validate_stub(source)

    destination = durable_stub_path(project_root, config)

    if destination.is_file() and source_bytes == destination.read_bytes():
        announce(f"Unreal API stub already up to date at {destination}")
        return destination

    if destination.exists() and _is_read_only(destination):
        raise DestinationNotWritableError(_not_writable_message(destination))

    announce(f"Writing enriched Unreal API stub: {source} -> {destination}")

    # This must remain durable consuming-project data. Bootstrap may check this
    # path, but only this explicit human-invoked action may create or update it.
    destination.parent.mkdir(parents=True, exist_ok=True)
    candidate: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            candidate = Path(stream.name)
        # Copy into a same-directory candidate. A failed copy can therefore
        # never truncate the old destination, and the finally block removes
        # the partial candidate.
        shutil.copy2(source, candidate)
        _validate_stub(candidate)
        os.replace(candidate, destination)
        candidate = None
    finally:
        _remove_candidate(candidate)
    return destination
