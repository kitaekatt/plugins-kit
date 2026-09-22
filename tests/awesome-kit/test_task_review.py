"""Regression tests for the shared task listing and HTML review projection."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from bootstrap_guard import _REEXEC_GUARD_ENV
from task_system import listing, summary_ops

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TASK_CLI = (
    _REPO_ROOT
    / "plugins"
    / "awesome-kit"
    / "skills"
    / "task"
    / "scripts"
    / "task.py"
)


def run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env[_REEXEC_GUARD_ENV] = "1"
    env["PYTHONPATH"] = str(_REPO_ROOT / "plugins" / "skills-kit")
    return subprocess.run(
        [sys.executable, str(_TASK_CLI), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )


def make_task(
    root: Path,
    rel: str,
    *,
    title: str,
    status: str = "active",
    summary: str | None = None,
    date: str | None = None,
) -> Path:
    folder = root / rel
    folder.mkdir(parents=True)
    block = {
        "_schema_version": "1",
        "type": "hand-off",
        "title": title,
        "status": status,
    }
    if summary is not None:
        block["summary"] = summary
    (folder / "task.yaml").write_text(
        yaml.safe_dump({"task": block}), encoding="utf-8"
    )
    (folder / "CLAUDE.md").write_text("Task context.\n", encoding="utf-8")
    (folder / "plan.md").write_text(
        "# Plan\n\n```yaml\ntask_items:\n  items: []\n```\n",
        encoding="utf-8",
    )
    log = f"- {date}: update: task changed\n" if date else "placeholder\n"
    (folder / "log.md").write_text(log, encoding="utf-8")
    return folder


class TestTaskListingAndReview:
    def test_list_text_remains_parseable_and_structured_formats_report_missing_summary(
        self, tmp_path: Path
    ) -> None:
        make_task(tmp_path, "tmp/missing", title="Needs summary", date="2026-09-20")
        make_task(
            tmp_path,
            "dev/tasks/closed",
            title="Finished",
            status="closed",
            summary="Finished task.",
            date="2026-09-21",
        )

        text_result = run_cli(["list", "--root", str(tmp_path)], tmp_path)
        assert text_result.returncode == 0
        assert "Open tasks:" in text_result.stdout
        assert "tmp/missing  active  -  2026-09-20  Needs summary" in text_result.stdout
        assert "missing task.summary" in text_result.stderr

        json_result = run_cli(
            ["list", "--format", "json", "--root", str(tmp_path)], tmp_path
        )
        assert json_result.returncode == 0
        payload = json.loads(json_result.stdout)
        missing = next(item for item in payload["tasks"] if item["id"] == "tmp/missing")
        assert missing["summary_missing"] is True
        assert any(
            item["code"] == "missing_summary" for item in payload["diagnostics"]
        )
        assert "missing task.summary" in json_result.stderr

        yaml_result = run_cli(
            ["list", "--format", "yaml", "--root", str(tmp_path)], tmp_path
        )
        assert yaml.safe_load(yaml_result.stdout)["schema_version"] == "1"
        assert "missing task.summary" in yaml_result.stderr

    def test_review_is_collapsible_sorted_escaped_and_excludes_archived(
        self, tmp_path: Path
    ) -> None:
        make_task(
            tmp_path,
            "tmp/older-open",
            title="Older <open>",
            date="2026-09-18",
        )
        make_task(
            tmp_path,
            "tmp/newer-open",
            title="Newer open",
            date="2026-09-21",
        )
        make_task(
            tmp_path,
            "dev/tasks/closed",
            title="Closed task",
            status="closed",
            summary="Closed summary.",
            date="2026-09-20",
        )
        make_task(
            tmp_path,
            "tmp/archived",
            title="Should not appear",
            status="archived",
            summary="Archived summary.",
            date="2026-09-22",
        )
        output = tmp_path / "review.html"
        result = run_cli(
            [
                "review",
                "--no-generate-missing-summaries",
                "--no-open",
                "--output",
                str(output),
                "--root",
                str(tmp_path),
            ],
            tmp_path,
        )
        assert result.returncode == 0
        html = output.read_text(encoding="utf-8")
        assert html.count("<details class=\"task-card") == 3
        assert "summary-line missing" in html
        assert "missing_summary" in html
        assert "Should not appear" not in html
        assert "Older &lt;open&gt;" in html
        assert html.index("tmp/newer-open") < html.index("tmp/older-open")
        assert "document.querySelectorAll('details.task-card[open]')" in html
        assert "missing task.summary" in result.stderr


class TestSummaryGeneration:
    def test_generation_persists_summary_without_advancing_activity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        make_task(tmp_path, "tmp/generate", title="Generate me", date="2026-09-19")
        first = listing.collect_listing("project", tmp_path)
        assert first.views[0].summary_status == "missing"

        monkeypatch.setattr(
            summary_ops,
            "_complete",
            lambda backend, view, root: "Generated summary.",
        )
        report = summary_ops.generate_missing_summaries(
            first, tmp_path, backend=SimpleNamespace()
        )
        assert report.generated == ("tmp/generate",)
        assert report.failed == ()

        block = yaml.safe_load(
            (tmp_path / "tmp" / "generate" / "task.yaml").read_text(encoding="utf-8")
        )["task"]
        assert block["summary"] == "Generated summary."
        log = (tmp_path / "tmp" / "generate" / "log.md").read_text(encoding="utf-8")
        assert "summary: refreshed" in log
        refreshed = listing.collect_listing("project", tmp_path)
        view = refreshed.views[0]
        assert view.summary_status == "present"
        assert view.last_update == "2026-09-19"
        assert view.updates[0].detail == "update: task changed"