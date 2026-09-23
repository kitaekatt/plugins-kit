"""Concurrency regression tests for task review summary generation."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import yaml
import pytest

from task_system import listing, summary_ops


def _make_task(root: Path, task_id: str) -> None:
    folder = root / task_id
    folder.mkdir(parents=True)
    (folder / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "task": {
                    "_schema_version": "1",
                    "type": "hand-off",
                    "title": task_id,
                    "status": "active",
                }
            }
        ),
        encoding="utf-8",
    )
    (folder / "CLAUDE.md").write_text("Context.\n", encoding="utf-8")
    (folder / "plan.md").write_text("Plan.\n", encoding="utf-8")
    (folder / "log.md").write_text("Log.\n", encoding="utf-8")


def test_completion_pool_refills_slots_and_reports_in_listing_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_ids = ["tmp/alpha", "tmp/beta", "tmp/gamma", "tmp/delta", "tmp/epsilon"]
    for task_id in task_ids:
        _make_task(tmp_path, task_id)
    collected = listing.collect_listing("project", tmp_path)
    fifth_started = threading.Event()
    completed: list[str] = []

    def complete(_backend, view, _root) -> str:
        if view.id == "tmp/alpha":
            assert fifth_started.wait(timeout=5), "pool did not refill a free worker slot"
        elif view.id == "tmp/epsilon":
            fifth_started.set()
        completed.append(view.id)
        return f"Summary for {view.id}."

    monkeypatch.setattr(summary_ops, "_complete", complete)
    report = summary_ops.generate_missing_summaries(
        collected, tmp_path, backend=SimpleNamespace()
    )

    assert set(completed) == set(task_ids)
    assert report.generated == tuple(view.id for view in collected.views)
    assert report.failed == ()
