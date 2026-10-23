"""Which deployment this ``llm_kit`` runs in, and the seams that depend on it.

Two deployments exist and they must never be mixed in one environment:

``legacy``
    The source tree as a path-linked copy: a ``.pth`` file or ``sys.path`` entry
    puts ``src/llm_kit`` on the path, ``bootstrap_lib`` is linked the same way, and the
    shared contracts (``execution_event``, ``interrupt_contract``,
    ``model_declaration``) are read from ``bootstrap_lib``. The committed
    ``llm_kit/_deployment.py`` says ``MODE = "legacy"``.

``package``
    The ``katsoft-llm-kit`` wheel. It carries a build-hook-written
    ``llm_kit/_deployment.py`` (``MODE = "package"``, ``DIST``, ``VERSION``,
    ``TAG``, ``COMMIT``, ``TAG_URL``), depends on ``katsoft-contracts`` and
    never meets ``bootstrap_lib``.

``mode()`` decides once per process and refuses an environment whose identity
cannot be established. Nothing here reads the network or the clock, and nothing
is repaired: an ambiguous environment raises and says what to separate.

``fleet()`` answers a different question: whether ``bootstrap_lib`` imports in
this environment (a plugin venv linked to the marketplace's shared libraries).
It is independent of ``mode()``: the package deployment may run with or without
the fleet. Features that need ``bootstrap_lib`` call ``require_fleet``; whether
the linked ``bootstrap_lib`` is new enough stays with each feature's own probe.

Standard library only; imports no other ``llm_kit`` module besides the marker.
"""
from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.metadata as metadata
import json
import os
import re
import sys
from importlib.machinery import SourceFileLoader
from typing import Any, Optional

#: The distribution that ships the package deployment.
DIST = "katsoft-llm-kit"
#: The distribution and import package that carry the shared contracts.
CONTRACTS_DIST = "katsoft-contracts"
CONTRACTS_PACKAGE = "katsoft_contracts"
#: The only contracts the library routes; everything else from ``bootstrap_lib``
#: is a fleet feature (``require_fleet``).
ROUTED_CONTRACTS = ("execution_event", "interrupt_contract", "model_declaration")

LEGACY = "legacy"
PACKAGE = "package"
_MODES = (LEGACY, PACKAGE)
_MARKER_MEMBER = "llm_kit/_deployment.py"


class RuntimeIdentityError(RuntimeError):
    """The deployment of this ``llm_kit`` cannot be established or is ambiguous."""


class UnsupportedLayout(RuntimeError):
    """``llm_kit`` is loaded in a layout the runtime-mode check cannot verify.

    Frozen interpreters, zip imports, bytecode-only files and editable
    installs have no on-disk file the installed metadata can be checked
    against, so they are refused rather than guessed at.
    """


class ContractsMissing(ImportError):
    """Package mode needs ``katsoft-contracts``, which is not importable."""


class UnsupportedInPackageMode(RuntimeError):
    """A fleet feature was used in package mode where ``bootstrap_lib`` does not import."""

    def __init__(self, feature: str) -> None:
        self.feature = feature
        super().__init__(
            f"{feature} needs the plugin marketplace's bootstrap_lib, which is not "
            f"importable in this environment and which the {DIST} package does not "
            "provide; use the llm-kit plugin, or install the package in a plugin "
            "venv that bootstrap links to bootstrap_lib"
        )


_MODE: Optional[str] = None
_FLEET: Optional[bool] = None


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _real(path: Any) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def mode() -> str:
    """``"legacy"`` or ``"package"``; decided once per process, cached on success."""
    global _MODE
    if _MODE is None:
        _MODE = _identify()
    return _MODE


def _identify() -> str:
    # (1) The marker.
    try:
        from llm_kit import _deployment  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeIdentityError(
            "llm_kit/_deployment.py is missing, so the deployment of this llm_kit "
            "cannot be identified"
        ) from exc
    declared = getattr(_deployment, "MODE", None)
    if declared not in _MODES:
        raise RuntimeIdentityError(
            f"llm_kit/_deployment.py states MODE = {declared!r}; expected one of {_MODES}"
        )
    # (2) The layout.
    _check_layout(_deployment)
    named = _named_distributions()
    if declared == LEGACY:
        # (3) A linked plugin copy beside an installed package: two llm_kit.
        if named:
            raise RuntimeIdentityError(
                "this environment holds both the plugin's llm_kit (bootstrap "
                f"shared-lib .pth) and the {DIST} package; use separate "
                "environments -- the plugin venv for the plugin, a consumer venv "
                "without the bootstrap llm_kit link for the package"
            )
        return LEGACY
    # (4) The package.
    _check_package(_deployment, named)
    return PACKAGE


def _check_layout(deployment: Any) -> None:
    if getattr(sys, "frozen", False):
        raise UnsupportedLayout("llm_kit does not support a frozen interpreter")
    spec = getattr(deployment, "__spec__", None)
    if spec is None or type(spec.loader) is not SourceFileLoader:
        raise UnsupportedLayout(
            "llm_kit must be loaded from source files on disk; this layout "
            f"(loader {type(getattr(spec, 'loader', None)).__name__}) is not supported"
        )
    path = getattr(deployment, "__file__", None)
    if not path or not os.path.isfile(path):
        raise UnsupportedLayout("llm_kit/_deployment.py is not a file on disk")


def _named_distributions() -> list:
    """Every distinct installed distribution named ``DIST``.

    One physical location is one distribution, however many ``sys.path``
    entries lead to it; two locations are a duplicate.
    """
    wanted = _normalize(DIST)
    found: dict[str, Any] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name and _normalize(name) == wanted:
            found.setdefault(_real(dist.locate_file("")), dist)
    return list(found.values())


def _check_package(deployment: Any, named: list) -> None:
    if not named:
        raise RuntimeIdentityError(
            f"llm_kit/_deployment.py states MODE = 'package' but no {DIST} "
            "distribution is installed"
        )
    if len(named) > 1:
        where = ", ".join(sorted(_real(d.locate_file("")) for d in named))
        raise RuntimeIdentityError(f"{DIST} is installed more than once ({where})")
    dist = named[0]
    direct = dist.read_text("direct_url.json")
    if direct:
        try:
            editable = bool(json.loads(direct).get("dir_info", {}).get("editable"))
        except (ValueError, AttributeError) as exc:
            raise RuntimeIdentityError(f"{DIST} has an unreadable direct_url.json") from exc
        if editable:
            raise UnsupportedLayout(f"{DIST} is an editable install; install the built wheel")
    loaded = os.fspath(deployment.__file__)
    entries = [f for f in (dist.files or ()) if str(f).replace("\\", "/") == _MARKER_MEMBER]
    if len(entries) != 1 or entries[0].hash is None or entries[0].hash.mode != "sha256":
        raise RuntimeIdentityError(
            f"the RECORD of {DIST} does not list {_MARKER_MEMBER} with a sha256"
        )
    with open(loaded, "rb") as handle:
        digest = base64.urlsafe_b64encode(hashlib.sha256(handle.read()).digest()).rstrip(b"=").decode()
    if digest != entries[0].hash.value:
        raise RuntimeIdentityError(
            f"{_MARKER_MEMBER} does not match the RECORD of {DIST}; the package was modified"
        )
    if _real(dist.locate_file(entries[0])) != _real(loaded):
        raise RuntimeIdentityError(
            f"the llm_kit loaded from {loaded} is not the copy {DIST} installed"
        )
    stated = (getattr(deployment, "DIST", None), getattr(deployment, "VERSION", None))
    if stated != (DIST, dist.version):
        raise RuntimeIdentityError(
            f"llm_kit/_deployment.py states {stated[0]!r} {stated[1]!r} but the "
            f"installed distribution is {DIST!r} {dist.version!r}"
        )
    for other in metadata.distributions():
        if any(other is d or _real(other.locate_file("")) == _real(d.locate_file("")) for d in named):
            continue
        if any(str(f).replace("\\", "/").startswith("llm_kit/") for f in (other.files or ())):
            raise RuntimeIdentityError(
                f"{other.metadata['Name']!r} also provides llm_kit files; "
                f"{DIST} must be the only provider of the llm_kit package"
            )


def load(name: str, error: Optional[type] = None) -> Any:
    """Import the routed contract ``name`` from ``katsoft_contracts`` (package mode).

    Only a ``ModuleNotFoundError`` that names ``katsoft_contracts`` or the
    routed submodule means the contracts distribution is missing; it becomes
    :class:`ContractsMissing`, or ``error`` (the calling product's own
    ``ImportError`` subclass) worded the same. Absent (``katsoft_contracts``
    does not import: install) and too old (it imports but lacks the routed
    module: upgrade) read differently. Every other ``ImportError``
    propagates unchanged: a broken contracts install must not read as an absent
    one.
    """
    if name not in ROUTED_CONTRACTS:
        raise ValueError(f"{name!r} is not a routed contract; routed: {ROUTED_CONTRACTS}")
    if mode() != PACKAGE:
        raise RuntimeIdentityError(f"load({name!r}) is a package-mode seam; this llm_kit is {mode()}")
    qualified = f"{CONTRACTS_PACKAGE}.{name}"
    try:
        return importlib.import_module(qualified)
    except ModuleNotFoundError as exc:
        if exc.name not in (CONTRACTS_PACKAGE, qualified):
            raise
        if exc.name == qualified:
            # The package imports but lacks the routed module: too old (or
            # stale), a different remedy from an absent install.
            missing = ContractsMissing(
                f"{CONTRACTS_DIST} is installed but has no {name}; upgrade "
                f"{CONTRACTS_DIST} ({DIST} requires >=1.0,<2)"
            )
        else:
            missing = ContractsMissing(
                f"{qualified} is not importable: install {CONTRACTS_DIST} "
                f"(the {DIST} package depends on it)"
            )
        if error is None:
            raise missing from exc
        raise error(str(missing)) from exc


def fleet() -> bool:
    """Whether ``bootstrap_lib`` imports in this environment; decided once per process.

    Only a ``ModuleNotFoundError`` for ``bootstrap_lib`` itself means absent.
    Any other import failure (a broken or half-synced ``bootstrap_lib``)
    propagates unchanged: a broken fleet install must not read as no fleet.
    Whether the linked ``bootstrap_lib`` is new enough for a feature is that
    feature's own probe, never this gate.
    """
    global _FLEET
    if _FLEET is None:
        try:
            importlib.import_module("bootstrap_lib")
        except ModuleNotFoundError as exc:
            if exc.name != "bootstrap_lib":
                raise
            _FLEET = False
        else:
            _FLEET = True
    return _FLEET


def require_fleet(feature: str) -> None:
    """Refuse ``feature`` in package mode when ``bootstrap_lib`` does not import.

    Legacy mode links ``bootstrap_lib`` by construction, so nothing is checked
    there and each caller keeps its own diagnostics.
    """
    if mode() == PACKAGE and not fleet():
        raise UnsupportedInPackageMode(feature)
