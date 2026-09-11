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
    """Report the first post-image parse failure; reviewers judge introduction."""
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
        if line is None and isinstance(error, tomllib.TOMLDecodeError):
            # Python 3.12/3.13 expose location only in the exception text.
            match = re.search(r"\(at line (\d+), column \d+\)$", str(error))
            if match:
                line = int(match.group(1))
            elif str(error).endswith("(at end of document)"):
                line = snapshot.post_image_text.count("\n") + 1
        if line is None and isinstance(getattr(error, "position", None), int):
            line = snapshot.post_image_text[:error.position].count("\n") + 1
        if not isinstance(line, int) or line < 1:
            from . import MechanicalCheckUnavailable

            raise MechanicalCheckUnavailable(f"{kind} parser supplied no usable location: {error}") from error
        return ({"check": "structured_parse", "line": line, "detail": f"{kind} parser diagnostic: {error}"},)
    return ()


def scan_added_lines(snapshot: MechanicalSnapshot) -> tuple[MechanicalFinding, ...]:
    """Retain added-line result semantics for pre-contract-2 consumers."""
    added = {line for line, _ in snapshot.added_lines or ()}
    return tuple(finding for finding in scan(snapshot) if finding["line"] in added)
