"""Load the file-backed request that starts one editor dispatch.

The request is the file seam between an editor and this package. Relative
paths resolve from the request file's directory. A corpus root contains a
``profile/`` directory and the data files named by that profile.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from yaml_data_editor_kit.schema.errors import Diagnostic

_REQUIRED_FIELDS = ("corpus_path", "comment_store_path", "run_dir")
_LEGAL_FIELDS = frozenset((*_REQUIRED_FIELDS, "selection", "driver"))
_LEGAL_SELECTION_FIELDS = frozenset(("comment_ids", "anchor_prefix"))
_DRIVERS = frozenset(("inline", "claude_bg"))


@dataclass(frozen=True)
class DispatchSelection:
    """The optional subset of open comments to plan."""

    comment_ids: tuple[str, ...] = ()
    anchor_prefix: str | None = None

    @property
    def ids(self) -> tuple[str, ...]:
        """Return the selected comment ids."""
        return self.comment_ids


@dataclass(frozen=True)
class DispatchRequest:
    """A validated dispatch request."""

    corpus_path: Path
    comment_store_path: Path
    run_dir: Path
    driver: str = "inline"
    selection: DispatchSelection = field(default_factory=DispatchSelection)

    @property
    def comments_path(self) -> Path:
        """Return the comment-store path under its file-seam alias."""
        return self.comment_store_path


@dataclass
class DispatchRequestSet:
    """A request and every diagnostic found while loading it."""

    request: DispatchRequest | None
    diagnostics: list[Diagnostic]

    @property
    def ok(self) -> bool:
        """Whether the request loaded without diagnostics."""
        return self.request is not None and not self.diagnostics


# A short alias keeps the result name parallel with ``CommentSet``.
RequestSet = DispatchRequestSet


def load_request(path: Path) -> DispatchRequestSet:
    """Load one YAML request and collect schema diagnostics."""
    request_path = Path(path)
    try:
        raw = yaml.safe_load(request_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        return DispatchRequestSet(
            request=None,
            diagnostics=[
                _diagnostic(
                    request_path,
                    "cannot read dispatch request YAML: {}".format(exc),
                )
            ],
        )

    if not isinstance(raw, Mapping):
        return DispatchRequestSet(
            request=None,
            diagnostics=[_diagnostic(request_path, "request document must be a mapping")],
        )

    diagnostics: list[Diagnostic] = []
    missing = [name for name in _REQUIRED_FIELDS if name not in raw]
    diagnostics.extend(
        _diagnostic(request_path, "missing required field {!r}".format(name), name)
        for name in missing
    )
    unknown = sorted(str(name) for name in raw if name not in _LEGAL_FIELDS)
    diagnostics.extend(
        _diagnostic(
            request_path,
            "unknown top-level key {!r}; legal keys: {}".format(
                name, _listed(_LEGAL_FIELDS)
            ),
            name,
        )
        for name in unknown
    )

    base_dir = request_path.parent
    corpus_path = _path_value(
        raw.get("corpus_path"), request_path, base_dir, "corpus_path", diagnostics
    )
    comment_store_path = _path_value(
        raw.get("comment_store_path"),
        request_path,
        base_dir,
        "comment_store_path",
        diagnostics,
    )
    run_dir = _path_value(
        raw.get("run_dir"), request_path, base_dir, "run_dir", diagnostics
    )

    driver = raw.get("driver", "inline")
    if not isinstance(driver, str) or driver not in _DRIVERS:
        diagnostics.append(
            _diagnostic(
                request_path,
                "field driver must be one of {}".format(_listed(_DRIVERS)),
                "driver",
            )
        )

    selection = _selection_value(raw.get("selection"), request_path, diagnostics)

    if diagnostics or corpus_path is None or comment_store_path is None or run_dir is None:
        return DispatchRequestSet(request=None, diagnostics=diagnostics)

    return DispatchRequestSet(
        request=DispatchRequest(
            corpus_path=corpus_path,
            comment_store_path=comment_store_path,
            run_dir=run_dir,
            driver=driver,
            selection=selection,
        ),
        diagnostics=[],
    )


def _path_value(
    value: Any,
    request_path: Path,
    base_dir: Path,
    field_name: str,
    diagnostics: list[Diagnostic],
) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        diagnostics.append(
            _diagnostic(request_path, "field {} must be non-empty text".format(field_name), field_name)
        )
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve()


def _selection_value(
    value: Any,
    request_path: Path,
    diagnostics: list[Diagnostic],
) -> DispatchSelection:
    if value is None:
        return DispatchSelection()
    if not isinstance(value, Mapping):
        diagnostics.append(
            _diagnostic(request_path, "field selection must be a mapping", "selection")
        )
        return DispatchSelection()

    unknown = sorted(str(name) for name in value if name not in _LEGAL_SELECTION_FIELDS)
    diagnostics.extend(
        _diagnostic(
            request_path,
            "unknown selection key {!r}; legal keys: {}".format(
                name, _listed(_LEGAL_SELECTION_FIELDS)
            ),
            "selection",
        )
        for name in unknown
    )

    raw_ids = value.get("comment_ids")
    comment_ids: tuple[str, ...] = ()
    if raw_ids is not None:
        if not isinstance(raw_ids, list) or any(
            not isinstance(item, str) or not item for item in raw_ids
        ):
            diagnostics.append(
                _diagnostic(
                    request_path,
                    "selection.comment_ids must be a list of non-empty text values",
                    "selection",
                )
            )
        else:
            comment_ids = tuple(raw_ids)
            if len(set(comment_ids)) != len(comment_ids):
                diagnostics.append(
                    _diagnostic(
                        request_path,
                        "selection.comment_ids must not contain duplicates",
                        "selection",
                    )
                )

    anchor_prefix = value.get("anchor_prefix")
    if anchor_prefix is not None and (
        not isinstance(anchor_prefix, str) or not anchor_prefix.strip()
    ):
        diagnostics.append(
            _diagnostic(
                request_path,
                "selection.anchor_prefix must be non-empty text",
                "selection",
            )
        )
        anchor_prefix = None

    if comment_ids and anchor_prefix is not None:
        diagnostics.append(
            _diagnostic(
                request_path,
                "selection must contain comment_ids or anchor_prefix, not both",
                "selection",
            )
        )

    return DispatchSelection(comment_ids=comment_ids, anchor_prefix=anchor_prefix)


def _diagnostic(path: Path, message: str, field: str | None = None) -> Diagnostic:
    return Diagnostic(message=message, file=str(path), field=field)


def _listed(values: set[str] | frozenset[str]) -> str:
    return "[{}]".format(", ".join(sorted(values)))


__all__ = [
    "DispatchRequest",
    "DispatchRequestSet",
    "DispatchSelection",
    "RequestSet",
    "load_request",
]
