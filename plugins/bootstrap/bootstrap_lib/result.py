"""The shared check-result type for bootstrap check modules.

Every check module used by the engine's check -> remediate -> re-check
pipeline returns this one shape instead of a module-private NamedTuple
(``CheckResult`` used to be defined three times with different fields:
tool_check, path_check, python_stub_check).

Fields:
    passed          -- did the check pass?
    subject         -- what was checked (tool name, path entry, repo name,
                       venv path, ...). The identity used in log entries.
    message         -- human-readable status/detail.
    remediation_cmd -- optional command that would remediate a failure.
    extras          -- per-check-type detail (e.g. tool_check sets
                       ``path`` / ``on_path`` / ``install_cmd``). Extras are
                       readable as attributes: ``result.on_path`` is
                       ``result.extras["on_path"]``.

Converted so far: tool_check, path_check, python_stub_check, venv_check,
git_dep_check. Modules with distinctly-named result types (LifecycleResult,
PypiCheckResult, IniCheckResult, ...) keep theirs for now; new check modules
should return Result.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class Result:
    passed: bool
    subject: str
    message: str
    remediation_cmd: Optional[str] = None
    extras: Dict[str, Any] = field(default_factory=dict)

    def __getattr__(self, name):
        # Only called for attributes NOT found normally — fall through to
        # extras so per-check fields read like plain attributes.
        try:
            return self.__dict__["extras"][name]
        except KeyError:
            raise AttributeError(
                f"Result has no field or extra {name!r} (extras: "
                f"{sorted(self.__dict__.get('extras', {}))})"
            ) from None
