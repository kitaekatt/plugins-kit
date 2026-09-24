"""Summary maintenance for the task review projection.

The task CLI remains the owner of persistence. This module only asks the
configured Codex Luna completion for a one-line summary and writes it through
state_ops.update so the normal YAML and log discipline is preserved.
"""

from __future__ import annotations

import datetime
import json
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import state_ops
from .discovery import read_task_block
from .listing import TaskListing, TaskView, summary_source_fingerprint

SUMMARY_MODEL = "gpt-5.6-luna"
SUMMARY_EFFORT = "medium"
SUMMARY_TIMEOUT_S = 180.0
SUMMARY_MAX_CHARS = 240
SUMMARY_SECTION_MAX_CHARS = 80
ELIGIBLE_STATUSES = frozenset(("active", "blocked", "closed", "deferred"))
SUMMARY_WORKERS = 4


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


def _normalize_summary(text: str) -> str:
    value = text.strip()
    if value.startswith(chr(96) * 3) and value.endswith(chr(96) * 3):
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


def _complete(backend: Any, view: TaskView, root: Path) -> str:
    from llm_scripting_kit.completion import BackendOptions

    system = (
        "Write task.summary for a task review dashboard as ONE line with "
        "three semicolon-separated sections, in this exact order: (1) the "
        "problem the task solves, (2) how it is being solved, (3) where it "
        f"stands now. Each section is at most {SUMMARY_SECTION_MAX_CHARS} "
        f"characters, and the full line is at most {SUMMARY_MAX_CHARS} "
        "characters. Prefer the current state from plan.md's task_items and "
        "the latest log entries. Telegraphic style is fine. Return only the "
        "summary line: no heading, markdown, bullets, or quotes."
    )
    user = (
        "Task id: "
        + view.id
        + "\nProject: "
        + view.project_name
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


def _view_root(view: TaskView, fallback: Path) -> Path:
    return view.project_root or fallback


def _view_label(listing: TaskListing, view: TaskView) -> str:
    return f"{view.project_name}/{view.id}" if listing.scope == "all" else view.id


def _existing_folder(view: TaskView, root: Path) -> Path:
    folder = root / view.id
    if not folder.is_dir():
        raise FileNotFoundError(f"{view.id}: task folder is no longer present")
    return folder.resolve()


def _assert_current_folder(view: TaskView, root: Path, expected: Path) -> None:
    actual = _existing_folder(view, root)
    if actual != expected:
        raise RuntimeError(
            f"{view.id}: task folder changed from {expected} to {actual}"
        )
    block = read_task_block(actual)
    if block is None:
        raise RuntimeError(f"{view.id}: task.yaml is no longer readable")
    fingerprint = summary_source_fingerprint(actual, block)
    if fingerprint != view.current_fingerprint:
        raise RuntimeError(f"{view.id}: task changed during summary generation")


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
    candidate_views = [
        view
        for view in listing.views
        if view.summary_status in ("missing", "stale")
        and view.status in ELIGIBLE_STATUSES
        and (_view_root(view, project_root) / view.id).is_dir()
        and view.current_fingerprint is not None
    ]
    if not candidate_views:
        return SummaryGenerationReport()

    active_backend = backend if backend is not None else _default_backend()
    stamp = datetime.date.today().isoformat()
    # Refill each worker slot as soon as a completion finishes. Persistence
    # stays in the caller thread so updates cannot race, and is applied in
    # listing order after every model completion has finished.
    prepared: list[tuple[TaskView, Path, str, Path | None, Exception | None]] = []
    for view in candidate_views:
        root = _view_root(view, project_root)
        label = _view_label(listing, view)
        try:
            expected_folder = _existing_folder(view, root)
            prepared.append((view, root, label, expected_folder, None))
        except Exception as exc:
            prepared.append((view, root, label, None, exc))

    results: dict[int, str | Exception] = {
        index: error
        for index, (_view, _root, _label, _folder, error) in enumerate(prepared)
        if error is not None
    }
    pending: dict[Future[str], int] = {}
    next_index = 0
    with ThreadPoolExecutor(max_workers=SUMMARY_WORKERS) as pool:
        while next_index < len(prepared) or pending:
            while next_index < len(prepared) and len(pending) < SUMMARY_WORKERS:
                view, root, _label, expected_folder, error = prepared[next_index]
                if error is None and expected_folder is not None:
                    pending[pool.submit(_complete, active_backend, view, root)] = next_index
                next_index += 1
            if not pending:
                continue
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                index = pending.pop(future)
                try:
                    results[index] = future.result()
                except Exception as exc:
                    results[index] = exc

    for index, (view, root, label, expected_folder, error) in enumerate(prepared):
        try:
            result = results[index] if error is None else error
            if isinstance(result, Exception):
                raise result
            if expected_folder is None:
                raise FileNotFoundError(f"{view.id}: task folder is no longer present")
            _assert_current_folder(view, root, expected_folder)
            state_ops.update(
                view.id,
                root,
                summary=result,
                summary_fingerprint=view.current_fingerprint,
                summary_updated=stamp,
                allow_init=False,
                expected_folder=expected_folder,
            )
            generated.append(label)
        except Exception as exc:  # one task must not suppress the rest
            failed.append((label, str(exc)))
    return SummaryGenerationReport(tuple(generated), tuple(failed))
