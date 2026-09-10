"""Registry and dispatcher for file-local mechanical review checks.

This package is Seam A: every check receives one file and immutable text from
the review snapshot. Checks that require repository resolution, the complete
changed-file set, or cross-file state belong in the future Seam B and must not
be registered here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TypedDict

from ._diff import (
    _DiffMismatchError,
    _HunkParseError,
    _added_lines_with_numbers,
    _delta_lines,
    _parse_hunks,
    _reconstruct,
)


class MechanicalFinding(TypedDict):
    """One deterministic, located detection."""

    check: str
    line: int
    detail: str


@dataclass(frozen=True)
class MechanicalSnapshot:
    """Immutable inputs derived from one file in the reviewed snapshot."""

    file: str
    diff_section_text: str
    pre_image_text: str | None
    added_lines: tuple[tuple[int, str], ...] | None
    changed_lines: tuple[str, ...] | None
    post_image_text: str | None


@dataclass(frozen=True)
class MechanicalCheck:
    """One registered Seam A check."""

    check_id: str
    phrase: str
    required_inputs: frozenset[str]
    precondition: Callable[[MechanicalSnapshot], bool]
    scan: Callable[[MechanicalSnapshot], tuple[MechanicalFinding, ...]]


def build_snapshot(
    file: str,
    diff_section_text: str,
    *,
    pre_image_text: str | None = None,
) -> MechanicalSnapshot:
    """Build immutable per-file check inputs without reading the live tree."""
    try:
        hunks = _parse_hunks(diff_section_text)
    except _HunkParseError:
        hunks = []
        parsed = False
    else:
        parsed = bool(hunks)

    added_lines: tuple[tuple[int, str], ...] | None = None
    changed_lines: tuple[str, ...] | None = None
    post_image_text: str | None = None
    if parsed:
        added_lines = tuple(_added_lines_with_numbers(hunks))
        added, removed = _delta_lines(hunks)
        changed_lines = tuple(added + removed)
        if pre_image_text is not None:
            try:
                post_lines, _, _ = _reconstruct(pre_image_text.splitlines(), hunks)
            except (_DiffMismatchError, _HunkParseError):
                pass
            else:
                post_image_text = "\n".join(post_lines)

    return MechanicalSnapshot(
        file=file,
        diff_section_text=diff_section_text,
        pre_image_text=pre_image_text,
        added_lines=added_lines,
        changed_lines=changed_lines,
        post_image_text=post_image_text,
    )


def _has_required_input(snapshot: MechanicalSnapshot, name: str) -> bool:
    """Return whether one declared input is available in the snapshot."""
    if name == "file":
        return bool(snapshot.file)
    if name == "diff_section_text":
        return bool(snapshot.diff_section_text)
    if name in {"pre_image_text", "added_lines", "changed_lines", "post_image_text"}:
        return getattr(snapshot, name) is not None
    raise ValueError(f"unknown mechanical-check input {name!r}")


def _run_checks(
    snapshot: MechanicalSnapshot,
    checks: tuple[MechanicalCheck, ...],
) -> tuple[list[str], list[MechanicalFinding]]:
    """Run eligible checks and return coverage plus located findings."""
    checks_run: list[str] = []
    findings: list[MechanicalFinding] = []
    order = {check.check_id: index for index, check in enumerate(checks)}
    for check in checks:
        if not all(
            _has_required_input(snapshot, required)
            for required in check.required_inputs
        ):
            continue
        if not check.precondition(snapshot):
            continue
        checks_run.append(check.check_id)
        findings.extend(check.scan(snapshot))
    findings.sort(key=lambda finding: (finding["line"], order[finding["check"]]))
    return checks_run, findings


# One module plus one entry here is the complete registration surface for a
# later Seam A check. Registration order is stable output order.
from . import abs_path, non_ascii  # noqa: E402

REGISTRY: tuple[MechanicalCheck, ...] = (
    MechanicalCheck(
        check_id="non_ascii",
        phrase="non-ASCII characters",
        required_inputs=frozenset({"added_lines"}),
        precondition=non_ascii.precondition,
        scan=non_ascii.scan,
    ),
    MechanicalCheck(
        check_id="abs_path",
        phrase="absolute paths",
        required_inputs=frozenset({"added_lines"}),
        precondition=abs_path.precondition,
        scan=abs_path.scan,
    ),
)

if len({check.check_id for check in REGISTRY}) != len(REGISTRY):
    raise ValueError("mechanical check ids must be unique")

_CHECKS_BY_ID = {check.check_id: check for check in REGISTRY}
LEGACY_CHECK_IDS = ("non_ascii", "abs_path")


def scan_file(
    file: str,
    diff_section_text: str,
    *,
    pre_image_text: str | None = None,
) -> dict[str, object]:
    """Dispatch every eligible Seam A check for one reviewed file."""
    snapshot = build_snapshot(
        file,
        diff_section_text,
        pre_image_text=pre_image_text,
    )
    checks_run, findings = _run_checks(snapshot, REGISTRY)
    return {"file": file, "checks_run": checks_run, "findings": findings}


def mechanical_findings(diff_section_text: str) -> list[MechanicalFinding]:
    """Return only the two permanently legacy-compatible finding types.

    This facade preserves the original contract: scan added lines only, locate
    every detection instead of aggregating it, and leave adjudication to the
    reviewer. New registry checks never enter this result.
    """
    snapshot = build_snapshot("", diff_section_text)
    legacy_checks = tuple(_CHECKS_BY_ID[check_id] for check_id in LEGACY_CHECK_IDS)
    _, findings = _run_checks(snapshot, legacy_checks)
    return findings


# Inputs a check can only get from a materialized pre-image. A front-half pays
# one VCS round-trip PER CHANGED FILE to produce one, so it asks this first and
# skips the work when no registered check would use the result. The predicate
# lives here rather than in each kit because the registry is the only thing that
# knows the answer, and it changes the moment a post-image check is registered.
_SNAPSHOT_INPUTS = frozenset({"pre_image_text", "post_image_text"})


def requires_pre_image() -> bool:
    """Whether any registered check needs a materialized pre-image.

    False while the registry holds only added-line checks, which read the diff
    alone. Registering the first structured-parse, duplicate-key, CSV or schema
    check flips it to True with no change at either call site.
    """
    return any(
        check.required_inputs & _SNAPSHOT_INPUTS for check in REGISTRY
    )


def check_phrase(check_id: str) -> str:
    """Return the registered human phrase, falling back for newer producers."""
    check = _CHECKS_BY_ID.get(check_id)
    return check.phrase if check is not None else check_id


__all__ = [
    "LEGACY_CHECK_IDS",
    "MechanicalCheck",
    "MechanicalFinding",
    "MechanicalSnapshot",
    "REGISTRY",
    "build_snapshot",
    "check_phrase",
    "mechanical_findings",
    "requires_pre_image",
    "scan_file",
]
