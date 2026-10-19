"""The structured result an autonomous worker must end its answer with.

The worker's final answer must contain exactly one fenced block whose info
string is ``task_result`` holding one JSON object. Acceptance judges that
object and nothing else: a missing, duplicated or malformed result is a
rejection, never inferred from the surrounding prose.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

SCHEMA = "task_result/v1"
INFO_STRING = "task_result"
STATUSES = ("done", "blocked")
FIELDS = ("schema", "status", "summary", "needs", "reason", "resume_hint", "checks", "changed_paths")

_BLOCK = re.compile(r"^[ \t]{0,3}```[ \t]*task_result[ \t]*\r?\n(.*?)^[ \t]{0,3}```[ \t]*$", re.MULTILINE | re.DOTALL)

DESCRIPTION = f"""End your final answer with exactly ONE fenced block whose info string is {INFO_STRING}, holding one JSON object with exactly these keys:
  schema        the string "{SCHEMA}"
  status        "done" or "blocked"
  summary       one or two sentences on what you did
  needs         when blocked: the kind of thing you need (decision, access, clarification, fix); otherwise ""
  reason        when blocked: why you cannot continue; otherwise ""
  resume_hint   when blocked: what the person should do, in the harness, so a fresh run can finish; otherwise ""
  checks        list of the checks you ran, as short strings
  changed_paths list of repository-relative paths you changed
Example:
```{INFO_STRING}
{{"schema": "{SCHEMA}", "status": "done", "summary": "...", "needs": "", "reason": "", "resume_hint": "", "checks": [], "changed_paths": []}}
```
Report "blocked" instead of guessing. Anything outside the block is ignored by acceptance."""


class ResultError(ValueError):
    """The answer carries no acceptable result; ``code`` is stable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TaskResult:
    status: str
    summary: str
    needs: str
    reason: str
    resume_hint: str
    checks: tuple[str, ...]
    changed_paths: tuple[str, ...]


def extract(answer: str) -> TaskResult:
    blocks = _BLOCK.findall(answer.replace("\r\n", "\n"))
    if not blocks:
        raise ResultError("result-missing", f"the answer has no fenced {INFO_STRING} block")
    if len(blocks) > 1:
        raise ResultError("result-duplicate", f"the answer has {len(blocks)} {INFO_STRING} blocks; exactly one is required")
    try:
        data = json.loads(blocks[0])
    except ValueError as exc:
        raise ResultError("result-malformed", f"the {INFO_STRING} block is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ResultError("result-malformed", f"the {INFO_STRING} block is not a JSON object")
    missing = [k for k in FIELDS if k not in data]
    extra = sorted(set(data) - set(FIELDS))
    if missing or extra:
        raise ResultError("result-malformed", f"{INFO_STRING} keys are wrong: missing {missing}, unknown {extra}")
    if data["schema"] != SCHEMA:
        raise ResultError("result-malformed", f"schema must be {SCHEMA!r}, got {data['schema']!r}")
    if data["status"] not in STATUSES:
        raise ResultError("result-malformed", f"status must be one of {STATUSES}, got {data['status']!r}")
    for key in ("summary", "needs", "reason", "resume_hint"):
        if not isinstance(data[key], str):
            raise ResultError("result-malformed", f"{key} must be a string")
    for key in ("checks", "changed_paths"):
        value = data[key]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ResultError("result-malformed", f"{key} must be a list of strings")
    if not data["summary"].strip():
        raise ResultError("result-malformed", "summary must not be empty")
    if data["status"] == "blocked":
        for key in ("needs", "reason", "resume_hint"):
            if not data[key].strip():
                raise ResultError("result-malformed", f"a blocked result needs a non-empty {key}")
    return TaskResult(
        status=data["status"],
        summary=data["summary"].strip(),
        needs=data["needs"].strip(),
        reason=data["reason"].strip(),
        resume_hint=data["resume_hint"].strip(),
        checks=tuple(data["checks"]),
        changed_paths=tuple(data["changed_paths"]),
    )
