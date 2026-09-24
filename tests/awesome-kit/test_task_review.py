"""Regression tests for the shared task listing and HTML review projection."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from html import unescape

import pytest
import yaml

from bootstrap_guard import _REEXEC_GUARD_ENV
from task_system import discovery, listing, summary_ops

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
    summary_updated: str | None = None,
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
    if summary_updated is not None:
        block["summary_updated"] = summary_updated
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


def _install_fake_completion_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for llm_scripting_kit.completion, which is not a test-venv dep.

    summary_ops._complete does `from llm_scripting_kit.completion import
    BackendOptions` at call time; real backends are always monkeypatched over
    _complete in the other tests in this module, but the prompt-contract test
    below calls _complete itself, so the import needs somewhere to resolve.
    """
    parent = ModuleType("llm_scripting_kit")
    completion = ModuleType("llm_scripting_kit.completion")

    class BackendOptions:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    completion.BackendOptions = BackendOptions
    parent.completion = completion
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", parent)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.completion", completion)


def _link_directory(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("directory links are unavailable")
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip("directory links are unavailable")


def _configure_project_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entries: list[str]
) -> Path:
    home = tmp_path / "home"
    config = home / ".claude" / "task.local.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(yaml.safe_dump({"project_directories": entries}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(discovery, "TASK_CONFIG_PATH", config)
    return config


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
                "--scope",
                "project",
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
        assert html.count('<div class="task-card ') == 3
        assert '<button class="task-id-copy" type="button" data-task-id="tmp/newer-open"' in html
        assert "var command = 'task work ' + button.dataset.taskId;" in html
        assert "event.stopPropagation();" in html
        assert "navigator.clipboard.writeText(command)" in html
        assert "document.execCommand('copy')" in html
        assert html.count('<section class="task-section">') == 2
        assert "Needs attention" not in html
        assert '<summary class="project-row">' in html
        assert ".project-row {" in html
        assert '<div class="task-card missing" ' in html
        assert "missing_summary" in html
        assert "Should not appear" not in html
        assert "Older &lt;open&gt;" in html
        assert html.index("tmp/newer-open") < html.index("tmp/older-open")
        assert "details.task-card" not in html
        assert "aria-describedby=\"task-summary-tmp%2Fnewer-open\"" in html
        assert "role=\"tooltip\"" in html
        assert "Updates" not in html
        assert "missing task.summary" in result.stderr

    def test_invalid_task_missing_summary_is_non_actionable_but_stays_in_review(
        self, tmp_path: Path
    ) -> None:
        make_task(tmp_path, "dev/tasks/nano", title="Nano", status="invalid")
        stale_folder = make_task(
            tmp_path,
            "dev/tasks/stale-invalid",
            title="Stale invalid",
            status="invalid",
            summary="Existing summary remains visible.",
        )
        stale_block = yaml.safe_load(
            (stale_folder / "task.yaml").read_text(encoding="utf-8")
        )
        stale_block["task"]["summary_fingerprint"] = "outdated-fingerprint"
        (stale_folder / "task.yaml").write_text(
            yaml.safe_dump(stale_block), encoding="utf-8"
        )

        collected = listing.collect_listing("project", tmp_path)
        data = listing.listing_data(collected)
        views = {view.id: view for view in collected.views}

        assert views["dev/tasks/nano"].status == "invalid"
        assert views["dev/tasks/nano"].summary_status == "unavailable"
        assert views["dev/tasks/stale-invalid"].summary_status == "unavailable"
        assert all(not task["summary_missing"] for task in data["tasks"])
        assert collected.warnings == ()
        assert not any(
            item["code"] in ("missing_summary", "stale_summary")
            for item in data["diagnostics"]
        )
        assert set(data["sections"]["other"]) == {
            "dev/tasks/nano",
            "dev/tasks/stale-invalid",
        }

        output = tmp_path / "review.html"
        result = run_cli(
            [
                "review",
                "--scope",
                "project",
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
        assert "missing task.summary" not in result.stderr
        html = output.read_text(encoding="utf-8")
        assert "Nano" in html
        assert "Needs attention" in html
        assert "status-invalid" in html
        assert "Existing summary remains visible." in html

    def test_project_groups_sort_by_newest_open_task_activity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        devroot = tmp_path / "devroot"
        project_specs = {
            "alpha": {"date": "2026-09-20"},
            "beta": {"summary_updated": "2026-09-22"},
            "delta": {"date": "2026-09-22"},
            "gamma": {},
            "zeta": {"date": "2026-09-23"},
        }
        for name, dates in project_specs.items():
            make_task(
                devroot / name,
                "tmp/task",
                title=f"{name} task",
                summary="Current summary.",
                summary_updated=dates.get("summary_updated"),
                date=dates.get("date"),
            )
        make_task(
            devroot / "zeta",
            "tmp/undated",
            title="zeta undated task",
            summary="Current summary.",
        )
        # Closed-task activity does not advance a project's last update.
        make_task(
            devroot / "alpha",
            "dev/tasks/closed",
            title="alpha closed task",
            status="closed",
            summary="Closed summary.",
            date="2026-09-24",
        )
        monkeypatch.setenv("DEVROOT", str(devroot))
        _configure_project_directories(tmp_path, monkeypatch, ["${DEVROOT}"])

        collected = listing.collect_listing("all", devroot / "alpha")
        groups = listing.project_groups(collected)
        # A summary date is not activity: beta has no dated open task.
        expected_order = [
            "zeta",
            "delta",
            "alpha",
            "beta",
            "gamma",
        ]
        assert [group.name for group in groups] == expected_order
        assert [group.last_update for group in groups] == [
            "2026-09-23",
            "2026-09-22",
            "2026-09-20",
            None,
            None,
        ]
        data = listing.listing_data(collected)
        assert [project["name"] for project in data["projects"]] == expected_order
        assert data["projects"][0]["last_update"] == "2026-09-23"
        beta_view = next(view for view in collected.views if view.project_name == "beta")
        assert beta_view.last_update is None
        assert beta_view.summary_updated == "2026-09-22"
        assert "summary_updated" not in data["tasks"][0]

        output = tmp_path / "ordered-review.html"
        result = run_cli(
            [
                "review",
                "--scope",
                "all",
                "--no-generate-missing-summaries",
                "--no-open",
                "--output",
                str(output),
                "--root",
                str(devroot / "alpha"),
            ],
            devroot / "alpha",
        )
        assert result.returncode == 0
        html = output.read_text(encoding="utf-8")
        rendered_order = [
            unescape(name)
            for name in re.findall(
                r'<summary class="project-row"><span class="project-title">(.*?)</span>',
                html,
            )
        ]
        assert rendered_order == expected_order


    def test_review_without_config_uses_current_project_and_all_explains_requirement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setattr(discovery, "TASK_CONFIG_PATH", home / ".claude" / "task.local.yaml")
        devroot = tmp_path / "devroot"
        project = devroot / "current"
        make_task(project, "tmp/current", title="Current project task", summary="Current.")
        make_task(devroot / "other", "tmp/other", title="Other project task", summary="Other.")
        monkeypatch.setenv("DEVROOT", str(devroot))
        output = tmp_path / "review.html"
        args = ["review", "--no-generate-missing-summaries", "--no-open", "--output", str(output), "--root", str(project)]

        result = run_cli(args, project)
        assert result.returncode == 0, result.stderr
        html = output.read_text(encoding="utf-8")
        assert "Current project task" in html
        assert "Other project task" not in html

        explicit_all = run_cli([*args, "--scope", "all"], project)
        assert explicit_all.returncode != 0
        assert "project_directories" in explicit_all.stderr
        assert "task.local.yaml" in explicit_all.stderr


    def test_config_supports_literal_and_environment_parent_directories(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = tmp_path / "first"
        second = tmp_path / "second"
        make_task(first / "alpha", "tmp/a", title="Alpha", summary="A.")
        make_task(second / "beta", "tmp/b", title="Beta", summary="B.")
        monkeypatch.setenv("TASK_TEST_PROJECTS", str(first))
        _configure_project_directories(tmp_path, monkeypatch, ["${TASK_TEST_PROJECTS}", str(second)])

        assert discovery.configured_project_directories() == (first.resolve(), second.resolve())
        collected = listing.collect_listing("all", first / "alpha")
        assert {group.name for group in listing.project_groups(collected)} == {"alpha", "beta"}


    def test_all_scope_groups_projects_and_deduplicates_shared_dev_tasks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        devroot = tmp_path / "devroot"
        short = devroot / "plugins-kit"
        long = devroot / "plugins-kit2"
        shared = tmp_path / "shared-tasks"
        shared.mkdir(parents=True)
        make_task(
            shared,
            "shared",
            title="Shared dev task",
            summary="Shared summary.",
            date="2026-09-22",
        )
        _link_directory(short / "dev" / "tasks", shared)
        _link_directory(long / "dev" / "tasks", shared)
        make_task(short, "tmp/short-only", title="Short tmp", date="2026-09-20")
        make_task(long, "tmp/long-only", title="Long tmp", date="2026-09-21")
        monkeypatch.setenv("DEVROOT", str(devroot))
        _configure_project_directories(tmp_path, monkeypatch, ["${DEVROOT}"])

        collected = listing.collect_listing("all", short)
        groups = {group.name: group for group in listing.project_groups(collected)}
        assert set(groups) == {"plugins-kit", "plugins-kit2"}
        assert {view.id for view in groups["plugins-kit"].views} == {
            "dev/tasks/shared",
            "tmp/short-only",
        }
        assert {view.id for view in groups["plugins-kit2"].views} == {"tmp/long-only"}
        assert all(view.project_name == "plugins-kit" for view in groups["plugins-kit"].views)
        assert all(view.project_name == "plugins-kit2" for view in groups["plugins-kit2"].views)

        data = listing.listing_data(collected)
        project_data = {project["name"]: project for project in data["projects"]}
        assert project_data["plugins-kit"]["tasks"] == [
            "plugins-kit::dev/tasks/shared",
            "plugins-kit::tmp/short-only",
        ]
        assert project_data["plugins-kit2"]["tasks"] == ["plugins-kit2::tmp/long-only"]

        output = tmp_path / "review.html"
        result = run_cli(
            [
                "review",
                "--no-generate-missing-summaries",
                "--no-open",
                "--output",
                str(output),
                "--root",
                str(short),
            ],
            short,
        )
        assert result.returncode == 0
        html = output.read_text(encoding="utf-8")
        assert html.count('<details class="project-card">') == 2
        assert html.count('<div class="task-card ') == 3
        assert html.count("Shared dev task") == 1
        assert "plugins-kit2" in html
        assert "missing task.summary" in result.stderr


    def test_updates_are_reverse_chronological_by_date(self, tmp_path: Path) -> None:
        folder = make_task(
            tmp_path,
            "tmp/chronology",
            title="Chronology",
            summary="Chronology summary.",
        )
        (folder / "log.md").write_text(
            "- 2026-09-18: update: older\n"
            "- 2026-09-22: update: newest\n"
            "- 2026-09-20: update: middle\n",
            encoding="utf-8",
        )
        view = listing.collect_listing("project", tmp_path).views[0]
        assert [update.date for update in view.updates] == [
            "2026-09-22",
            "2026-09-20",
            "2026-09-18",
        ]

    def test_same_day_updates_show_latest_appended_entry_first(
        self, tmp_path: Path
    ) -> None:
        folder = make_task(
            tmp_path, "tmp/same-day", title="Same day", summary="Summary."
        )
        (folder / "log.md").write_text(
            "- 2026-09-22: update: first change\n"
            "- 2026-09-21: update: previous day\n"
            "- 2026-09-22: update: second change\n",
            encoding="utf-8",
        )
        view = listing.collect_listing("project", tmp_path).views[0]
        assert [update.detail for update in view.updates] == [
            "update: second change",
            "update: first change",
            "update: previous day",
        ]

    def test_timestamped_entries_parse_and_order_within_a_day(
        self, tmp_path: Path
    ) -> None:
        folder = make_task(
            tmp_path, "tmp/timed", title="Timed", summary="Summary."
        )
        (folder / "log.md").write_text(
            "- 2026-09-22 19:21:39: update: seconds dropped\n"
            "- 2026-09-22 08:05: update: morning\n"
            "- 2026-09-22: update: date only reads as noon\n",
            encoding="utf-8",
        )
        view = listing.collect_listing("project", tmp_path).views[0]
        assert [update.timestamp for update in view.updates] == [
            "2026-09-22 19:21",
            "2026-09-22 12:00",
            "2026-09-22 08:05",
        ]
        assert view.updates[0].detail == "update: seconds dropped"
        assert view.last_update == "2026-09-22"
        assert view.last_activity == "2026-09-22 19:21"

    def test_tasks_on_the_same_day_sort_by_time(self, tmp_path: Path) -> None:
        for stub, stamp in (("early", "2026-09-22 08:00"), ("late", "2026-09-22 18:00")):
            folder = make_task(tmp_path, f"tmp/{stub}", title=stub, summary="S.")
            (folder / "log.md").write_text(
                f"- {stamp}: update: work\n", encoding="utf-8"
            )
        views = listing.collect_listing("project", tmp_path).views
        assert [view.id for view in views] == ["tmp/late", "tmp/early"]


class TestSummaryGeneration:
    def test_all_scope_generation_uses_selected_project_roots(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        devroot = tmp_path / "devroot"
        owner = devroot / "kit"
        other = devroot / "kit-long"
        shared = tmp_path / "shared-tasks"
        shared.mkdir()
        shared_folder = make_task(shared, "shared", title="Shared task")
        _link_directory(owner / "dev" / "tasks", shared)
        _link_directory(other / "dev" / "tasks", shared)
        other_folder = make_task(other, "tmp/other", title="Other task")
        monkeypatch.setenv("DEVROOT", str(devroot))
        _configure_project_directories(tmp_path, monkeypatch, ["${DEVROOT}"])

        calls: list[tuple[str, Path]] = []

        def complete(backend: object, view: listing.TaskView, root: Path) -> str:
            calls.append((view.id, root))
            return "Generated summary."

        monkeypatch.setattr(summary_ops, "_complete", complete)
        current = listing.collect_listing("all", owner)
        report = summary_ops.generate_missing_summaries(
            current, owner, backend=SimpleNamespace()
        )

        assert report.failed == ()
        assert set(report.generated) == {"kit/dev/tasks/shared", "kit-long/tmp/other"}
        assert set(calls) == {("dev/tasks/shared", owner), ("tmp/other", other)}
        for folder in (shared_folder, other_folder):
            block = yaml.safe_load((folder / "task.yaml").read_text(encoding="utf-8"))
            assert block["task"]["summary"] == "Generated summary."

    def test_generation_does_not_recreate_disappeared_task_folder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        folder = make_task(tmp_path, "tmp/disappears", title="Disappears")
        current = listing.collect_listing("project", tmp_path)

        def disappear(backend, view, root):
            shutil.rmtree(folder)
            return "Generated summary."

        monkeypatch.setattr(summary_ops, "_complete", disappear)
        report = summary_ops.generate_missing_summaries(
            current, tmp_path, backend=SimpleNamespace()
        )
        assert report.generated == ()
        assert report.failed and report.failed[0][0] == "tmp/disappears"
        assert not folder.exists()

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


class TestSummaryPromptContract:
    def test_normalize_summary_truncates_at_new_cap_on_a_word_boundary(self) -> None:
        text = " ".join(["alpha"] * 60)  # 359 chars, past both the 240 cap and the old 320 cap
        normalized = summary_ops._normalize_summary(text)
        assert len(normalized) <= 240
        assert normalized.endswith("...")
        assert not normalized.endswith(" ...")

    def test_normalize_summary_keeps_a_short_three_section_line_intact(self) -> None:
        text = "Closet run is dead; re-terminating cat6a; re-test pending."
        assert summary_ops._normalize_summary(text) == text

    def test_complete_system_prompt_states_problem_approach_state_contract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        make_task(tmp_path, "tmp/contract", title="Contract check")
        view = listing.collect_listing("project", tmp_path).views[0]
        _install_fake_completion_module(monkeypatch)

        captured: dict[str, str] = {}

        class _StubBackend:
            def complete(self, system: str, user: str, **_kwargs: object) -> SimpleNamespace:
                captured["system"] = system
                captured["user"] = user
                return SimpleNamespace(text="Problem stated; approach stated; state stated")

        result = summary_ops._complete(_StubBackend(), view, tmp_path)
        assert result == "Problem stated; approach stated; state stated"

        system = captured["system"]
        lowered = system.lower()
        assert "problem the task solves" in lowered
        assert "how it is being solved" in lowered
        assert "stands now" in lowered
        assert str(summary_ops.SUMMARY_MAX_CHARS) in system
        assert str(summary_ops.SUMMARY_SECTION_MAX_CHARS) in system
        assert "plan.md's task_items" in system
        assert "no heading, markdown, bullets, or quotes" in lowered
        assert "```" not in system

    def test_fingerprint_bump_invalidates_a_summary_stored_under_the_old_scheme(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        folder = make_task(
            tmp_path, "tmp/legacy", title="Legacy", summary="Old summary."
        )
        block = discovery.read_task_block(folder)

        with monkeypatch.context() as patched:
            patched.setattr(listing, "SUMMARY_PROMPT_VERSION", "pre-upgrade-scheme")
            legacy_fingerprint = listing.summary_source_fingerprint(folder, block)

        task_yaml = folder / "task.yaml"
        data = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))
        data["task"]["summary_fingerprint"] = legacy_fingerprint
        task_yaml.write_text(yaml.safe_dump(data), encoding="utf-8")

        view = listing.collect_listing("project", tmp_path).views[0]
        assert view.summary_status == "stale"
