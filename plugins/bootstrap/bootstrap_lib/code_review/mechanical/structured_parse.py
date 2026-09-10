"""Detect parse failures in strict structured-data files."""

from __future__ import annotations

import json
import re
import tomllib
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from . import MechanicalFinding, MechanicalSnapshot


_TEMPLATE_RE = re.compile(r"\{\{|\}\}|\{%|%\}|\$\{")


def _kind(file: str) -> str | None:
    suffix = file.lower().rsplit(".", 1)[-1] if "." in file else ""
    return {"json": "json", "yaml": "yaml", "yml": "yaml", "toml": "toml"}.get(suffix)


def precondition(snapshot: MechanicalSnapshot) -> bool:
    """Run only for recognized, non-templated strict structured data."""
    return (
        snapshot.post_image_text is not None
        and _kind(snapshot.file) is not None
        and not (snapshot.file.lower().endswith(".jsonc") or _TEMPLATE_RE.search(snapshot.post_image_text))
    )


def scan(snapshot: MechanicalSnapshot) -> tuple[MechanicalFinding, ...]:
    """Return a parse diagnostic only when its line was added."""
    assert snapshot.post_image_text is not None
    kind = _kind(snapshot.file)
    try:
        if kind == "json":
            json.loads(snapshot.post_image_text)
        elif kind == "yaml":
            yaml.safe_load(snapshot.post_image_text)
        else:
            tomllib.loads(snapshot.post_image_text)
    except (json.JSONDecodeError, yaml.YAMLError, tomllib.TOMLDecodeError) as error:
        line = getattr(error, "lineno", None)
        if line is None and getattr(error, "problem_mark", None) is not None:
            line = error.problem_mark.line + 1
        if snapshot.added_lines is None or line not in {n for n, _ in snapshot.added_lines}:
            return ()
        return ({"check": "structured_parse", "line": line, "detail": f"{kind} parser diagnostic: {error}"},)
    return ()
