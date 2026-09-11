"""Repository-snapshot mechanical checks (Seam B).

Collectors declare every repository query before any check is evaluated.  The
dispatcher resolves those queries through a short-lived VCS reader, freezes an
overlay-first view, and returns the same per-file version-2 records as Seam A.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping, Protocol

from bootstrap_lib.code_review.mechanical import MechanicalFinding, MechanicalSnapshot

MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_TARGETS = 10_000
MAX_TARGET_CONTENT_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class PathEffect:
    path: str
    effect: str
    review_id: str
    post_image: bytes | None = None
    post_size: int = 0
    post_identity: str | None = None
    post_image_error: str | None = None


@dataclass(frozen=True)
class StatResult:
    kind: str
    size: int = 0
    identity: str | None = None
    diagnostic: str | None = None


@dataclass(frozen=True)
class ReadResult:
    kind: str
    data: bytes | None = None
    identity: str | None = None
    diagnostic: str | None = None


class SnapshotReader(Protocol):
    def stat(self, paths: tuple[str, ...]) -> Mapping[str, StatResult]: ...
    def read(self, paths: tuple[str, ...]) -> Mapping[str, ReadResult]: ...


@dataclass(frozen=True)
class RepositoryRequest:
    target: str
    need_content: bool = False


@dataclass(frozen=True)
class SourceRequests:
    requests: tuple[RepositoryRequest, ...] = ()
    diagnostic: str | None = None


@dataclass(frozen=True)
class CheckOutcome:
    ran: bool
    findings: tuple[MechanicalFinding, ...] = ()
    diagnostic: str | None = None


class RepositoryCheck(Protocol):
    check_id: str
    phrase: str

    def collect(
        self, sources: Mapping[str, MechanicalSnapshot]
    ) -> Mapping[str, SourceRequests]: ...

    def evaluate(
        self,
        sources: Mapping[str, MechanicalSnapshot],
        view: "FrozenRepositoryView",
        requests: Mapping[str, SourceRequests],
    ) -> Mapping[str, CheckOutcome]: ...


class FrozenRepositoryView:
    """Immutable overlay-first answers for the requests declared in collect."""

    def __init__(
        self,
        stats: Mapping[str, StatResult],
        reads: Mapping[str, ReadResult],
        declared: frozenset[str],
        content_declared: frozenset[str],
    ) -> None:
        self._stats = dict(stats)
        self._reads = dict(reads)
        self._declared = declared
        self._content_declared = content_declared

    def stat_many(self, paths: tuple[str, ...]) -> Mapping[str, StatResult]:
        undeclared = set(paths) - self._declared
        if undeclared:
            raise RuntimeError(f"repository query was not collected: {sorted(undeclared)!r}")
        return {path: self._stats[path] for path in paths}

    def read_many(self, paths: tuple[str, ...]) -> Mapping[str, ReadResult]:
        undeclared = set(paths) - self._content_declared
        if undeclared:
            raise RuntimeError(f"repository content query was not collected: {sorted(undeclared)!r}")
        return {path: self._reads[path] for path in paths}


def _add_diagnostic(
    records: dict[str, dict[str, object]], sources: set[str], message: str
) -> None:
    for source in sources:
        records[source].setdefault("diagnostics", []).append(message)  # type: ignore[union-attr]


def _overlay(
    effects: tuple[PathEffect, ...],
) -> tuple[dict[str, StatResult], dict[str, ReadResult]]:
    stats: dict[str, StatResult] = {}
    reads: dict[str, ReadResult] = {}
    for effect in effects:
        if effect.effect == "delete":
            stats[effect.path] = StatResult("missing")
            reads[effect.path] = ReadResult("missing")
        elif effect.effect in {"add", "edit"}:
            if effect.post_image_error is not None:
                message = effect.post_image_error
                stats[effect.path] = StatResult(
                    "file", effect.post_size, effect.post_identity
                )
                reads[effect.path] = ReadResult("error", diagnostic=message)
            elif effect.post_image is None:
                stats[effect.path] = StatResult(
                    "file", effect.post_size, effect.post_identity
                )
                reads[effect.path] = ReadResult(
                    "error", diagnostic="changed target content was not captured"
                )
            else:
                identity = effect.post_identity or hashlib.sha256(effect.post_image).hexdigest()
                stats[effect.path] = StatResult("file", len(effect.post_image), identity)
                reads[effect.path] = ReadResult("file", effect.post_image, identity)
        else:
            raise ValueError(f"unknown path effect {effect.effect!r}")
    return stats, reads


def scan_repository(
    sources: Mapping[str, MechanicalSnapshot],
    *,
    snapshot_seed: str | None,
    path_effects: tuple[PathEffect, ...],
    reader: SnapshotReader | None,
    checks: tuple[RepositoryCheck, ...] | None = None,
) -> tuple[dict[str, dict[str, object]], str | None]:
    """Collect, freeze, and evaluate repository checks in registry order."""
    ordered_sources = dict(sorted(sources.items(), key=lambda item: item[1].file))
    if checks is None:
        checks = REGISTRY
    records = {
        source: {"file": source, "checks_run": [], "findings": []}
        for source in ordered_sources
    }
    if not checks:
        return records, snapshot_seed
    collections: list[tuple[RepositoryCheck, Mapping[str, SourceRequests]]] = []
    union: dict[str, bool] = {}
    try:
        for check in checks:
            requested = check.collect(ordered_sources)
            unknown = set(requested) - set(ordered_sources)
            if unknown:
                raise RuntimeError(f"collector returned unknown sources: {sorted(unknown)!r}")
            collections.append((check, requested))
            for source_request in requested.values():
                if source_request.diagnostic is not None:
                    continue
                for request in source_request.requests:
                    union[request.target] = union.get(request.target, False) or request.need_content
    except Exception as exc:
        _add_diagnostic(records, set(ordered_sources), f"repository collection failed: {exc}")
        return records, None

    def fail_before_evaluation(message: str) -> None:
        for _, requested in collections:
            for source, source_requests in requested.items():
                diagnostic = source_requests.diagnostic or message
                existing = records[source].setdefault("diagnostics", [])
                if diagnostic not in existing:
                    existing.append(diagnostic)

    if snapshot_seed is None or reader is None:
        fail_before_evaluation("repository snapshot unavailable")
        return records, None

    if len(union) > MAX_TARGETS:
        message = f"repository target limit exceeded ({len(union)} > {MAX_TARGETS})"
        fail_before_evaluation(message)
        return records, None

    overlay_stats, overlay_reads = _overlay(path_effects)
    query_paths = tuple(sorted(path for path in union if path not in overlay_stats))
    try:
        base_stats = dict(reader.stat(query_paths)) if query_paths else {}
    except Exception as exc:
        base_stats = {path: StatResult("error", diagnostic=str(exc)) for path in query_paths}
    stats = {**base_stats, **overlay_stats}
    for path in union:
        stats.setdefault(path, StatResult("error", diagnostic="snapshot reader omitted result"))

    content_paths = tuple(
        sorted(path for path, needed in union.items() if needed and stats[path].kind == "file")
    )
    content_size = sum(stats[path].size for path in content_paths)
    if content_size > MAX_TARGET_CONTENT_BYTES:
        message = (
            f"repository target content limit exceeded ({content_size} > "
            f"{MAX_TARGET_CONTENT_BYTES})"
        )
        fail_before_evaluation(message)
        return records, None
    base_read_paths = tuple(path for path in content_paths if path not in overlay_reads)
    try:
        base_reads = dict(reader.read(base_read_paths)) if base_read_paths else {}
    except Exception as exc:
        base_reads = {path: ReadResult("error", diagnostic=str(exc)) for path in base_read_paths}
    reads = {**base_reads, **overlay_reads}
    for path in content_paths:
        reads.setdefault(path, ReadResult("error", diagnostic="snapshot reader omitted content"))
    view = FrozenRepositoryView(
        stats, reads, frozenset(union), frozenset(path for path, needed in union.items() if needed)
    )

    for check, requested in collections:
        try:
            outcomes = check.evaluate(ordered_sources, view, requested)
        except Exception as exc:
            outcomes = {
                source: CheckOutcome(False, diagnostic=f"{check.check_id} failed: {exc}")
                for source in requested
            }
        if set(outcomes) - set(ordered_sources):
            raise RuntimeError(f"check {check.check_id!r} returned an unknown source")
        for source, source_requests in requested.items():
            outcome = outcomes.get(source)
            if outcome is None:
                outcome = CheckOutcome(False, diagnostic=f"{check.check_id} returned no outcome")
            if source_requests.diagnostic is not None:
                outcome = CheckOutcome(False, diagnostic=source_requests.diagnostic)
            record = records[source]
            if outcome.ran:
                for finding in outcome.findings:
                    if finding.get("check") != check.check_id:
                        raise RuntimeError(f"check {check.check_id!r} returned a foreign finding")
                record["checks_run"].append(check.check_id)  # type: ignore[union-attr]
                record["findings"].extend(outcome.findings)  # type: ignore[union-attr]
            elif outcome.diagnostic:
                record.setdefault("diagnostics", []).append(outcome.diagnostic)  # type: ignore[union-attr]

    finalize_identity = getattr(reader, "finalize_identity", None)
    snapshot_identity = (
        finalize_identity(snapshot_seed, path_effects)
        if callable(finalize_identity)
        else snapshot_seed
    )
    return records, snapshot_identity


from .local_link_targets import CHECK as LOCAL_LINK_TARGETS_CHECK  # noqa: E402
from .python_syntax import CHECK as PYTHON_SYNTAX_CHECK  # noqa: E402

REGISTRY: tuple[RepositoryCheck, ...] = (LOCAL_LINK_TARGETS_CHECK, PYTHON_SYNTAX_CHECK)

__all__ = [
    "CheckOutcome",
    "FrozenRepositoryView",
    "LOCAL_LINK_TARGETS_CHECK",
    "MAX_SOURCE_BYTES",
    "MAX_TARGETS",
    "MAX_TARGET_CONTENT_BYTES",
    "PathEffect",
    "PYTHON_SYNTAX_CHECK",
    "REGISTRY",
    "ReadResult",
    "RepositoryCheck",
    "RepositoryRequest",
    "SnapshotReader",
    "SourceRequests",
    "StatResult",
    "scan_repository",
]
