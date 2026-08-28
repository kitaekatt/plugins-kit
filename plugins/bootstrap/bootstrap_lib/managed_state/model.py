"""Public data model for declarative managed state."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Protocol, Sequence


class Operation(str, Enum):
    CHECK = "check"
    INSTALL = "install"
    UPDATE = "update"


class State(str, Enum):
    CURRENT = "current"
    MISSING = "missing"
    DRIFTED = "drifted"
    ERROR = "error"


class Status(str, Enum):
    UNCHANGED = "unchanged"
    CHANGED = "changed"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True)
class Inspection:
    state: State
    detail: str


@dataclass(frozen=True)
class ResourceResult:
    name: str
    status: Status
    before: State
    after: State
    detail: str
    backup: str | None = None
    rollback: str | None = None

    def to_dict(self) -> dict:
        result = asdict(self)
        result["status"] = self.status.value
        result["before"] = self.before.value
        result["after"] = self.after.value
        return result


class Resource(Protocol):
    name: str

    def inspect(self) -> Inspection: ...

    def converge(
        self, before: Inspection, operation: Operation
    ) -> ResourceResult: ...


class ResourceApplyError(Exception):
    """A failed mutation with the state and recovery outcome observed afterward."""

    def __init__(
        self,
        detail: str,
        *,
        after: Inspection,
        backup: str | None = None,
        rollback: str | None = None,
    ):
        super().__init__(detail)
        self.after = after
        self.backup = backup
        self.rollback = rollback


@dataclass(frozen=True)
class PlanItem:
    resource: Resource
    inspection: Inspection
    will_change: bool
    blocked: bool


@dataclass(frozen=True)
class Plan:
    operation: Operation
    items: tuple[PlanItem, ...]


@dataclass(frozen=True)
class Report:
    operation: Operation
    results: tuple[ResourceResult, ...]

    @property
    def ok(self) -> bool:
        return all(result.status not in {Status.BLOCKED, Status.FAILED}
                   for result in self.results)

    @property
    def changed(self) -> bool:
        return any(result.status is Status.CHANGED for result in self.results)

    def to_dict(self) -> dict:
        return {
            "operation": self.operation.value,
            "ok": self.ok,
            "changed": self.changed,
            "results": [result.to_dict() for result in self.results],
        }


Resources = Sequence[Resource]


@dataclass(frozen=True)
class Declaration:
    """An ordered, immutable set of resources owned by one project CLI."""

    resources: tuple[Resource, ...]

    def __init__(self, resources: Sequence[Resource]):
        object.__setattr__(self, "resources", tuple(resources))
