"""The failure vocabulary shared by every execution probe.

A probe imports a bootstrap_lib (or validator) module and checks that it is
usable. Each refusal is a :class:`ProbeFailure`: a classification, never a
sentence. The product's spec holds the error class and the ``render``
function that turns a failure into that product's message, so the shared code
decides what is wrong and the product words it.

Kinds:

- ``ABSENT``: the package does not import (``ModuleNotFoundError`` naming the
  package, or naming nothing).
- ``BROKEN_IMPORT``: the package exists but failed to import a module of its
  own (``ModuleNotFoundError`` naming another module). ``detail`` holds the
  exception text. A product that never separated this case words it as
  ``ABSENT``.
- ``MODULE_UNIMPORTABLE``: the package imports, the probed submodule does not.
- ``UNSUPPORTED``: a capability literal is not advertised; ``subject`` names it.
- ``MISSING_NAME``: a callable or error class is missing; ``subject`` names it.
- ``BAD_SHAPE``: a callable exists but does not accept the call the product
  makes; ``subject`` names it.

Import seam: a product that fakes or logs its imports passes an ``importer``
(a callable taking a dotted name) on its spec; ``None`` means
``importlib.import_module``, looked up when the probe runs.

Stages: ``base`` is the main probe; ``v2`` is the second stage the event probe
runs for a run that needs schema v2. ``floor`` is the bootstrap (or validator)
release the product's wording names for the failing stage.
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass
from typing import Any, Callable, NoReturn, Optional

ABSENT = "absent"
BROKEN_IMPORT = "broken-import"
MODULE_UNIMPORTABLE = "module-unimportable"
UNSUPPORTED = "unsupported"
MISSING_NAME = "missing-name"
BAD_SHAPE = "bad-shape"
KINDS = frozenset(
    {ABSENT, BROKEN_IMPORT, MODULE_UNIMPORTABLE, UNSUPPORTED, MISSING_NAME, BAD_SHAPE}
)

STAGE_BASE = "base"
STAGE_V2 = "v2"


@dataclass(frozen=True)
class ProbeFailure:
    """What a probe found wrong; the product's ``render`` words it."""

    kind: str
    subject: str
    floor: str
    detail: str = ""
    stage: str = STAGE_BASE

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown probe failure kind {self.kind!r}")


@dataclass(frozen=True)
class CallShape:
    """The exact call a product makes: positional count, then keyword names."""

    name: str
    positional: int = 0
    keywords: tuple[str, ...] = ()

    def bind_error(self, function: Any) -> Optional[Exception]:
        """Why ``function`` does not accept that call, or ``None`` when it does.

        Placeholders are ``None``. A callable with no introspectable signature
        raises ``ValueError``; a mismatched signature raises ``TypeError``.
        """
        try:
            inspect.signature(function).bind(
                *([None] * self.positional), **{k: None for k in self.keywords}
            )
        except (TypeError, ValueError) as exc:
            return exc
        return None

    def binds(self, function: Any) -> bool:
        """Whether ``function`` accepts that call (placeholders are ``None``)."""
        return self.bind_error(function) is None


Render = Callable[[ProbeFailure], str]

#: The import seam a product may supply: called with a dotted module name,
#: it returns the module or raises ``ImportError``. ``None`` means
#: ``importlib.import_module``, looked up at call time.
Importer = Callable[[str], Any]


def _import(importer: Optional[Importer], name: str) -> Any:
    return (importer if importer is not None else importlib.import_module)(name)


def require_text(owner: str, field_name: str, value: object) -> None:
    """Refuse a spec field that must be a non-empty string."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{owner}.{field_name} must be a non-empty string")


def refuse(
    error: type[ImportError],
    render: Render,
    failure: ProbeFailure,
    cause: Optional[BaseException] = None,
) -> NoReturn:
    """Raise the product's error, worded by the product's ``render``."""
    if cause is not None:
        raise error(render(failure)) from cause
    raise error(render(failure))


def import_package(
    package: str,
    error: type[ImportError],
    render: Render,
    floor: str,
    importer: Optional[Importer] = None,
) -> None:
    """Import ``package``; refuse as ``ABSENT`` or ``BROKEN_IMPORT``.

    Only ``ModuleNotFoundError`` is classified; any other import-time
    exception propagates.
    """
    try:
        _import(importer, package)
    except ModuleNotFoundError as exc:
        kind = ABSENT if exc.name in (None, package) else BROKEN_IMPORT
        refuse(error, render, ProbeFailure(kind, package, floor, str(exc)), exc)


def import_module(
    module: str,
    error: type[ImportError],
    render: Render,
    floor: str,
    importer: Optional[Importer] = None,
) -> Any:
    """Import ``module``; refuse as ``MODULE_UNIMPORTABLE`` on any ImportError."""
    try:
        return _import(importer, module)
    except ImportError as exc:
        refuse(error, render, ProbeFailure(MODULE_UNIMPORTABLE, module, floor), exc)


def advertises(container: object, literal: str) -> bool:
    """Whether ``literal`` is in ``container``; a non-container advertises nothing."""
    try:
        return literal in container  # type: ignore[operator]
    except TypeError:
        return False


def require_callable(
    module: Any,
    name: str,
    error: type[ImportError],
    render: Render,
    floor: str,
    stage: str = STAGE_BASE,
) -> Any:
    """Return ``module.name`` when callable; refuse as ``MISSING_NAME``."""
    function = getattr(module, name, None)
    if not callable(function):
        refuse(error, render, ProbeFailure(MISSING_NAME, name, floor, stage=stage))
    return function


def require_binding(
    function: Any,
    shape: CallShape,
    error: type[ImportError],
    render: Render,
    floor: str,
    stage: str = STAGE_BASE,
) -> None:
    """Refuse as ``BAD_SHAPE`` when ``function`` does not bind ``shape``."""
    problem = shape.bind_error(function)
    if problem is not None:
        refuse(
            error,
            render,
            ProbeFailure(BAD_SHAPE, shape.name, floor, stage=stage),
            problem,
        )
