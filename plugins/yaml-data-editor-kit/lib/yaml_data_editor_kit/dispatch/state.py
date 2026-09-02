"""Durable, authenticated state for staged dispatch runs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from .units import plain_value

PLAN_SCHEMA_VERSION = "1"
ADAPTER_PREFIX = "yaml-data-editor-dispatch-bg-1:"


class PlanError(ValueError):
    """Base error for an invalid durable plan."""


class PlanDigestError(PlanError):
    """The plan digest does not authenticate its immutable fields."""


class PlanUnitSetError(PlanError):
    """The durable plan and execution store contain different unit ids."""


@dataclass(frozen=True)
class DispatchPlan:
    """The immutable inputs needed to reconstruct one staged run."""

    schema_version: str
    run_id: str
    driver: str
    corpus_path: Path
    comment_store_path: Path
    execution_store: str
    attributed_store: str
    units: tuple[Mapping[str, Any], ...]
    digest: str

    @property
    def adapter_version(self) -> str:
        return ADAPTER_PREFIX + self.digest

    @property
    def unit_ids(self) -> tuple[str, ...]:
        return tuple(str(unit["id"]) for unit in self.units)

    def unit_for(self, unit_id: str) -> Mapping[str, Any]:
        for unit in self.units:
            if unit.get("id") == unit_id:
                return unit
        raise KeyError(unit_id)


def _immutable(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {key: plan[key] for key in (
        "schema_version", "run_id", "driver", "corpus_path", "comment_store_path",
        "execution_store", "attributed_store", "units",
    )}


def plan_digest(fields: Mapping[str, Any]) -> str:
    """Return the canonical SHA-256 digest for immutable plan fields."""
    encoded = json.dumps(plain_value(fields), ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_dump(path: Path, value: Mapping[str, Any]) -> None:
    # Keep the implementation shared with the established attributed-store writer.
    from .run import _atomic_dump as shared_atomic_dump

    shared_atomic_dump(path, value)


def write_plan(
    path: Path,
    *,
    run_id: str | None = None,
    driver: str = "claude_bg",
    corpus_path: Path,
    comment_store_path: Path,
    execution_store: str = "execution.sqlite3",
    attributed_store: str = "attributed.yaml",
    units: Sequence[Mapping[str, Any]],
) -> DispatchPlan:
    """Atomically write and return one immutable dispatch plan."""
    raw: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "run_id": run_id or "dispatch-{}".format(uuid4().hex),
        "driver": driver,
        "corpus_path": str(Path(corpus_path).resolve()),
        "comment_store_path": str(Path(comment_store_path).resolve()),
        "execution_store": execution_store,
        "attributed_store": attributed_store,
        # plain_value here, not at each consumer: an anchored slice may hold the
        # ABSENT sentinel, which neither the digest's json.dumps nor the plan's
        # yaml.safe_dump can represent, and an absent slice is a supported anchor.
        "units": [plain_value(dict(unit)) for unit in units],
    }
    raw["digest"] = plan_digest(raw)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_dump(target, raw)
    return _plan_from_raw(raw, target)


def load_plan(path: Path, *, execution_unit_ids: Sequence[str] | None = None) -> DispatchPlan:
    """Load a plan without mutation and reject digest or unit-set tampering."""
    target = Path(path)
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise PlanError("cannot read dispatch plan: {}".format(exc)) from exc
    if not isinstance(raw, Mapping):
        raise PlanError("dispatch plan must be a mapping")
    missing = [key for key in (*_immutable_keys(), "digest") if key not in raw]
    if missing:
        raise PlanError("dispatch plan is missing field {!r}".format(missing[0]))
    expected = raw.get("digest")
    if not isinstance(expected, str) or expected != plan_digest(_immutable(raw)):
        raise PlanDigestError("dispatch plan digest is invalid")
    plan = _plan_from_raw(raw, target)
    if execution_unit_ids is not None and tuple(execution_unit_ids) != plan.unit_ids:
        raise PlanUnitSetError("dispatch plan unit set does not match execution store")
    return plan


def _plan_from_raw(raw: Mapping[str, Any], path: Path) -> DispatchPlan:
    required = (*_immutable_keys(), "digest")
    missing = [key for key in required if key not in raw]
    if missing:
        raise PlanError("dispatch plan is missing field {!r}".format(missing[0]))
    units = raw["units"]
    if not isinstance(units, list) or any(not isinstance(unit, Mapping) or not isinstance(unit.get("id"), str) for unit in units):
        raise PlanError("dispatch plan units must be mappings with text ids")
    return DispatchPlan(
        schema_version=str(raw["schema_version"]), run_id=str(raw["run_id"]), driver=str(raw["driver"]),
        corpus_path=Path(str(raw["corpus_path"])), comment_store_path=Path(str(raw["comment_store_path"])),
        execution_store=str(raw["execution_store"]), attributed_store=str(raw["attributed_store"]),
        units=tuple(dict(unit) for unit in units), digest=str(raw["digest"]),
    )


def _immutable_keys() -> tuple[str, ...]:
    return ("schema_version", "run_id", "driver", "corpus_path", "comment_store_path", "execution_store", "attributed_store", "units")


__all__ = ["ADAPTER_PREFIX", "DispatchPlan", "PLAN_SCHEMA_VERSION", "PlanDigestError", "PlanError", "PlanUnitSetError", "load_plan", "plan_digest", "write_plan"]
