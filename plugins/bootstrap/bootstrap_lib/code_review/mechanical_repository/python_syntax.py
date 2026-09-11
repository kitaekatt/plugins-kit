"""Compile mapped CPython source in memory, without executing reviewed code.

The nearest snapshot .python-version selects the grammar. Only a single
numeric CPython version matching the running parser's minor version is covered.
The answer is successful compilation or its first SyntaxError, not an inventory
of every possible error. Introduction and reportability belong to the reviewer.
"""

from __future__ import annotations

import re
import sys
import warnings
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Mapping

from bootstrap_lib.code_review.mechanical import MechanicalFinding, MechanicalSnapshot

from . import (
    CheckOutcome,
    FrozenRepositoryView,
    MAX_SOURCE_BYTES,
    RepositoryRequest,
    SourceRequests,
)


def _version_paths(file: str) -> tuple[str, ...]:
    """Return nearest-first mapping paths inside the repository."""
    path = PurePosixPath(file)
    if path.is_absolute() or ".." in path.parts or "\\" in file or "\x00" in file:
        raise ValueError("Python source path is not repository-relative")
    return tuple(str(parent / ".python-version") for parent in path.parents)


def _mapped_version(
    view: FrozenRepositoryView, paths: tuple[str, ...]
) -> tuple[str | None, str]:
    """Resolve one authoritative version without falling past an unreadable file."""
    for path, stat in view.stat_many(paths).items():
        if stat.kind == "missing":
            continue
        if stat.kind != "file":
            return None, f"{path}: Python version mapping is {stat.kind}"
        result = view.read_many((path,))[path]
        if result.kind != "file" or result.data is None:
            return None, f"{path}: Python version mapping content unavailable"
        try:
            value = result.data.decode("utf-8").strip()
        except UnicodeError:
            return None, f"{path}: Python version mapping is not UTF-8"
        if re.fullmatch(r"3\.[0-9]+(?:\.[0-9]+)?", value) is None:
            return None, f"{path}: expected one numeric CPython version"
        grammar = ".".join(value.split(".")[:2])
        available = f"{sys.version_info.major}.{sys.version_info.minor}"
        if sys.implementation.name != "cpython" or grammar != available:
            return None, f"{path}: CPython {grammar} grammar unavailable (parser: {available})"
        return grammar, path
    return None, "no snapshot .python-version mapping for Python source"


@dataclass(frozen=True)
class PythonSyntaxCheck:
    check_id: str = "python_syntax"
    phrase: str = (
        "CPython syntax under nearest snapshot .python-version "
        "(whole post-image; first compiler diagnostic)"
    )

    def collect(
        self, sources: Mapping[str, MechanicalSnapshot]
    ) -> Mapping[str, SourceRequests]:
        """Declare all possible ancestor mappings before freezing the view."""
        requests: dict[str, SourceRequests] = {}
        for source, snapshot in sources.items():
            if not snapshot.file.endswith(".py"):
                continue
            if snapshot.post_image_text is None:
                requests[source] = SourceRequests(diagnostic="Python post-image unavailable")
                continue
            if len(snapshot.post_image_text.encode("utf-8")) > MAX_SOURCE_BYTES:
                requests[source] = SourceRequests(diagnostic="Python source exceeds snapshot byte limit")
                continue
            try:
                paths = _version_paths(snapshot.file)
            except ValueError as exc:
                requests[source] = SourceRequests(diagnostic=str(exc))
                continue
            requests[source] = SourceRequests(tuple(RepositoryRequest(path, True) for path in paths))
        return requests

    def evaluate(
        self,
        sources: Mapping[str, MechanicalSnapshot],
        view: FrozenRepositoryView,
        requests: Mapping[str, SourceRequests],
    ) -> Mapping[str, CheckOutcome]:
        """Return the first compiler diagnostic, including on unchanged lines."""
        outcomes: dict[str, CheckOutcome] = {}
        for source, request in requests.items():
            if request.diagnostic:
                outcomes[source] = CheckOutcome(False, diagnostic=request.diagnostic)
                continue
            grammar, mapping = _mapped_version(view, tuple(item.target for item in request.requests))
            if grammar is None:
                outcomes[source] = CheckOutcome(False, diagnostic=mapping)
                continue
            snapshot = sources[source]
            assert snapshot.post_image_text is not None
            findings: tuple[MechanicalFinding, ...] = ()
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", SyntaxWarning)
                    compile(snapshot.post_image_text.encode("utf-8"), snapshot.file, "exec", dont_inherit=True, optimize=0)
            except SyntaxError as exc:
                if exc.lineno is None or exc.lineno < 1:
                    outcomes[source] = CheckOutcome(False, diagnostic=(
                        f"CPython {grammar} ({mapping}) first compiler diagnostic: {exc.msg}; "
                        f"compiler supplied line {exc.lineno!r}, not a source location"
                    ))
                    continue
                findings = ({
                    "check": self.check_id,
                    "line": exc.lineno,
                    "detail": (
                        f"CPython {grammar} ({mapping}) first compiler diagnostic: "
                        f"{exc.msg} (column {exc.offset}); introduction requires review"
                    ),
                },)
            except (ValueError, RecursionError, MemoryError, OverflowError) as exc:
                outcomes[source] = CheckOutcome(False, diagnostic=f"Python compilation unavailable: {exc}")
                continue
            outcomes[source] = CheckOutcome(True, findings)
        return outcomes


CHECK = PythonSyntaxCheck()
