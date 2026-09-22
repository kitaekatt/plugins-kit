"""Summary maintenance for the task review projection.

The task CLI remains the owner of persistence. This module only asks the
configured Codex Luna completion for a one-line summary and writes it through
``state_ops.update`` so the normal YAML and log discipline is preserved.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import state_ops
from .listing import TaskListing, TaskView

SUMMARY_MODEL = "gpt-5.6-luna"
SUMMARY_EFFORT = "medium"
SUMMARY_TIMEOUT_S = 180.0
SUMMARY_MAX_CHARS = 320
ELIGIBLE_STATUSES = frozenset(("active", "blocked", "closed"))


@dataclass(frozen=True)
class SummaryGenerationReport:
    generated: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()

    @property
    def attempted(self) -> int:
        return len(self.generated) + len(self.failed)


def _read_excerpt(path: Path, limit: int = 12000) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "(unavailable)"
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[truncated]"


def _task_context(view: TaskView, root: Path) -> str:
    folder = root / view.id
    task_yaml = _read_excerpt(folder / "task.yaml")
    claude = _read_excerpt(folder / "CLAUDE.md")
    plan = _read_excerpt(folder / "plan.md")
    log = _read_excerpt(folder / "log.md", limit=8000)
    return "\n\n".join(
        (
            "TASK YAML:\n" + task_yaml,
            "CLAUDE.md:\n" + claude,
            "PLAN:\n" + plan,
            "ACTIVITY LOG:\n" + log,
        )
    )


def _normalize_summary(text: str) -> str:
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        value = value[3:-3].strip()
    if value.lower().startswith("summary:"):
        value = value.split(":", 1)[1].strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("summary"), str):
        value = parsed["summary"].strip()
    value = " ".join(value.split())
    if len(value) > SUMMARY_MAX_CHARS:
        value = value[: SUMMARY_MAX_CHARS - 3].rsplit(" ", 1)[0] + "..."
    if not value:
        raise ValueError("model returned an empty summary")
    return value


def _default_backend():
    from llm_scripting_kit.completion import CodexCliBackend

    return CodexCliBackend()


def _complete(backend: Any, view: TaskView, root: Path) -> str:
    from llm_scripting_kit.completion import BackendOptions

    system = (
        "Write a concise one-line task summary for a task review dashboard. "
        "State the task's purpose and current outcome or next focus. Return "
        "only the summary sentence, with no heading, markdown, bullets, or quotes."
    )
    user = (
        "Task id: "
        + view.id
        + "\nCurrent status: "
        + view.status
        + "\n\n"
        + _task_context(view, root)
    )
    response = backend.complete(
        system,
        user,
        model=SUMMARY_MODEL,
        options=BackendOptions(
            effort=SUMMARY_EFFORT,
            timeout_s=SUMMARY_TIMEOUT_S,
            cwd=root.resolve(),
            extras={"sandbox": "read-only", "network": False},
        ),
    )
    return _normalize_summary(response.text)


def generate_missing_summaries(
    listing: TaskListing,
    project_root: Path,
    *,
    backend: Any | None = None,
) -> SummaryGenerationReport:
    """Generate missing/stale summaries for valid local task folders.

    Individual failures do not prevent the remaining eligible tasks from being
    attempted. The caller decides whether failed attempts should affect the
    command exit code.
    """
    generated: list[str] = []
    failed: list[tuple[str, str]] = []
    effective_root = listing.effective_root
    candidate_views = [
        view
        for view in listing.views
        if view.summary_status in ("missing", "stale")
        and view.status in ELIGIBLE_STATUSES
        and (effective_root / view.id).is_dir()
        and view.current_fingerprint is not None
    ]
    if not candidate_views:
        return SummaryGenerationReport()
    active_backend = backend if backend is not None else _default_backend()
    stamp = datetime.date.today().isoformat()
    for view in candidate_views:
        try:
            summary = _complete(active_backend, view, effective_root)
            state_ops.update(
                view.id,
                effective_root,
                summary=summary,
                summary_fingerprint=view.current_fingerprint,
                summary_updated=stamp,
            )
            generated.append(view.id)
        except Exception as exc:  # one task must not suppress the rest
            failed.append((view.id, str(exc)))
    return SummaryGenerationReport(tuple(generated), tuple(failed))