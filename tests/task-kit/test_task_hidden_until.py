"""``hidden_until``: a task hidden until a calendar date (YYYY-MM-DD, local time).

Behaviors: one rule (``hidden.is_hidden``) decides; every view that offers tasks to pick (list, review, the
dashboard export, answers refresh and status) leaves a hidden task out by default; a by-name verb still works on
it; ``update --hidden-until`` sets and clears the field; ``validate`` reports a malformed value.

Revert checks: ``is_hidden`` returning False turns the rule and every view test red; dropping the hidden filter
from ``discovery._discover_local`` turns the list and review tests red; dropping it from ``export._owners``
turns the export and answers tests red; dropping the ``--hidden-until`` check in ``state_ops.update`` turns the
bad-date test red.

CLI tests read the real clock, so they use a date far in the future (hidden) or far in the past (visible).
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from bootstrap_guard import _REEXEC_SKIP_ENV
from task_export_support import inject
from task_system import export, export_cli, hidden
from task_system.discovery import discover
from task_system.listing import collect_listing, listing_data

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TASK_CLI = _REPO_ROOT / "plugins" / "task-kit" / "skills" / "task" / "scripts" / "task.py"

TODAY = datetime.date(2026, 11, 23)
FAR = "2999-01-01"
PAST = "2000-01-01"
PLAN_MD = "# Plan\n\n```yaml\ntask_items:\n  items: []\n```\n"


def make_task(root: Path, rel: str, **fields) -> Path:
    folder = root / rel
    folder.mkdir(parents=True)
    block = {"_schema_version": "1", "type": "hand-off", "title": f"Task {rel}", "status": "active"}
    block.update(fields)
    (folder / "task.yaml").write_text(yaml.safe_dump({"task": block}), encoding="utf-8")
    (folder / "plan.md").write_text(PLAN_MD, encoding="utf-8")
    for name in ("CLAUDE.md", "log.md"):
        (folder / name).write_text("placeholder\n", encoding="utf-8")
    return folder


def run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env[_REEXEC_SKIP_ENV] = "1"
    env["PYTHONPATH"] = str(_REPO_ROOT / "plugins" / "skills-kit")
    return subprocess.run(
        [sys.executable, str(_TASK_CLI), *args], cwd=cwd, capture_output=True, text=True, env=env, timeout=120
    )


def stored(folder: Path) -> dict:
    return yaml.safe_load((folder / "task.yaml").read_text(encoding="utf-8"))["task"]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    make_task(tmp_path, "tmp/shown")
    make_task(tmp_path, "tmp/later", hidden_until=FAR)
    make_task(tmp_path, "tmp/was-hidden", hidden_until=PAST)
    return tmp_path


# --- the rule -----------------------------------------------------------------------------------------------------


class TestIsHidden:
    def test_hidden_before_the_date(self):
        assert hidden.is_hidden({"hidden_until": "2026-11-30"}, TODAY) is True

    def test_visible_on_the_date_and_after(self):
        assert hidden.is_hidden({"hidden_until": "2026-11-23"}, TODAY) is False
        assert hidden.is_hidden({"hidden_until": "2026-11-22"}, TODAY) is False

    def test_the_day_before_is_still_hidden(self):
        assert hidden.is_hidden({"hidden_until": "2026-11-24"}, TODAY) is True

    def test_missing_or_null_is_visible(self):
        assert hidden.is_hidden({}, TODAY) is False
        assert hidden.is_hidden({"hidden_until": None}, TODAY) is False
        assert hidden.is_hidden(None, TODAY) is False

    def test_a_yaml_date_object_counts(self):
        assert hidden.is_hidden({"hidden_until": datetime.date(2026, 11, 30)}, TODAY) is True

    def test_a_malformed_value_is_not_hidden(self):
        # validate reports it; the task must stay findable meanwhile
        for bad in ("soon", "2026-13-01", "11/30/2026", 20261130, ["2026-11-30"], datetime.datetime(2999, 1, 1)):
            assert hidden.is_hidden({"hidden_until": bad}, TODAY) is False, bad

    def test_parse_accepts_only_iso_calendar_dates(self):
        assert hidden.parse_hidden_until("2026-11-30") == datetime.date(2026, 11, 30)
        for bad in ("2026-11-30T00:00", "2026-1-5", "tomorrow", "2026-02-30", ""):
            with pytest.raises(hidden.HiddenUntilError):
                hidden.parse_hidden_until(bad)


# --- list ---------------------------------------------------------------------------------------------------------


class TestDiscovery:
    def test_default_leaves_a_hidden_task_out(self, project):
        records = discover("project", project, local_host="h", today=TODAY)
        assert {r.id for r in records} == {"tmp/shown", "tmp/was-hidden"}

    def test_the_date_arrives(self, tmp_path):
        make_task(tmp_path, "tmp/t", hidden_until="2026-11-30")
        assert discover("project", tmp_path, local_host="h", today=datetime.date(2026, 11, 29)) == []
        on = discover("project", tmp_path, local_host="h", today=datetime.date(2026, 11, 30))
        assert [r.id for r in on] == ["tmp/t"]

    def test_include_hidden_returns_it_with_its_date(self, project):
        records = discover("project", project, local_host="h", today=TODAY, include_hidden=True)
        by_id = {r.id: r for r in records}
        assert set(by_id) == {"tmp/shown", "tmp/later", "tmp/was-hidden"}
        assert by_id["tmp/later"].hidden_until == FAR
        assert by_id["tmp/shown"].hidden_until is None

    def test_hidden_stays_active(self, project):
        records = discover("project", project, local_host="h", today=TODAY, include_hidden=True)
        [record] = [r for r in records if r.id == "tmp/later"]
        assert record.classification == "active"


class TestListing:
    def test_collect_listing_default_and_include(self, project):
        now = datetime.datetime(2026, 11, 23, 9, 0)
        shown = collect_listing("project", project, now=now)
        assert {v.id for v in shown.views} == {"tmp/shown", "tmp/was-hidden"}
        every = collect_listing("project", project, now=now, include_hidden=True)
        assert {v.id for v in every.views} == {"tmp/shown", "tmp/later", "tmp/was-hidden"}

    def test_the_json_projection_carries_the_field(self, project):
        every = collect_listing("project", project, include_hidden=True)
        tasks = {t["id"]: t for t in listing_data(every)["tasks"]}
        assert tasks["tmp/later"]["hidden_until"] == FAR
        assert tasks["tmp/shown"]["hidden_until"] is None


class TestListCli:
    def test_default_omits_and_flag_marks(self, project):
        plain = run_cli(["list", "--root", str(project)], project)
        assert plain.returncode == 0, plain.stderr
        assert "tmp/shown" in plain.stdout and "tmp/later" not in plain.stdout
        flagged = run_cli(["list", "--include-hidden", "--root", str(project)], project)
        line = next(line for line in flagged.stdout.splitlines() if "tmp/later" in line)
        assert f"hidden until {FAR}" in line
        past = next(line for line in flagged.stdout.splitlines() if "tmp/was-hidden" in line)
        assert "hidden until" not in past

    def test_json_format_includes_the_field(self, project):
        done = run_cli(["list", "--include-hidden", "--format", "json", "--root", str(project)], project)
        tasks = {t["id"]: t for t in json.loads(done.stdout)["tasks"]}
        assert tasks["tmp/later"]["hidden_until"] == FAR
        default = run_cli(["list", "--format", "json", "--root", str(project)], project)
        assert "tmp/later" not in {t["id"] for t in json.loads(default.stdout)["tasks"]}

    def test_a_status_filter_does_not_unhide(self, project):
        done = run_cli(["list", "--status", "active", "--root", str(project)], project)
        assert "tmp/later" not in done.stdout and "tmp/shown" in done.stdout


class TestReviewCli:
    def test_review_html_omits_a_hidden_task_by_default(self, project):
        base = ["review", "--scope", "project", "--output", "-", "--no-open",
                "--no-generate-missing-summaries", "--root", str(project)]
        done = run_cli(base, project)
        assert done.returncode == 0, done.stderr
        assert "tmp/shown" in done.stdout and "tmp/later" not in done.stdout
        every = run_cli([*base, "--include-hidden"], project)
        assert "tmp/later" in every.stdout and f"hidden until {FAR}" in every.stdout


# --- by-name verbs ------------------------------------------------------------------------------------------------


class TestByName:
    def test_show_prints_the_date_for_a_hidden_task(self, project):
        done = run_cli(["show", "tmp/later", "--root", str(project)], project)
        assert done.returncode == 0, done.stderr
        assert f"hidden_until: {FAR}" in done.stdout
        assert "hidden_until: -" in run_cli(["show", "tmp/shown", "--root", str(project)], project).stdout

    def test_validate_and_close_work_on_a_hidden_task(self, project):
        assert run_cli(["validate", "tmp/later", "--root", str(project)], project).returncode == 0
        assert run_cli(["close", "tmp/later", "--root", str(project)], project).returncode == 0
        assert stored(project / "tmp" / "later")["status"] == "closed"


# --- update -------------------------------------------------------------------------------------------------------


class TestUpdate:
    def test_sets_and_clears(self, project):
        folder = project / "tmp" / "shown"
        done = run_cli(["update", "tmp/shown", "--hidden-until", "2026-11-30", "--root", str(project)], project)
        assert done.returncode == 0, done.stderr
        assert str(stored(folder)["hidden_until"]) == "2026-11-30"
        assert "hidden_until: 2026-11-30" in (folder / "task.yaml").read_text(encoding="utf-8")  # an unquoted date
        assert "hidden_until: 2026-11-30" in run_cli(["show", "tmp/shown", "--root", str(project)], project).stdout
        assert "hidden_until = 2026-11-30" in (folder / "log.md").read_text(encoding="utf-8")
        done = run_cli(["update", "tmp/shown", "--hidden-until", "none", "--root", str(project)], project)
        assert done.returncode == 0, done.stderr
        assert "hidden_until" not in stored(folder)

    def test_a_bad_date_is_a_clear_error_and_writes_nothing(self, project):
        folder = project / "tmp" / "shown"
        before = (folder / "task.yaml").read_text(encoding="utf-8")
        done = run_cli(["update", "tmp/shown", "--hidden-until", "next-week", "--root", str(project)], project)
        assert done.returncode != 0
        assert "hidden-until" in done.stderr and "YYYY-MM-DD" in done.stderr
        assert (folder / "task.yaml").read_text(encoding="utf-8") == before

    def test_a_bad_date_does_not_scaffold_a_missing_task(self, tmp_path):
        done = run_cli(["update", "tmp/new-one", "--hidden-until", "bogus", "--root", str(tmp_path)], tmp_path)
        assert done.returncode != 0
        assert not (tmp_path / "tmp" / "new-one").exists()

    def test_clearing_an_absent_field_is_fine(self, project):
        done = run_cli(["update", "tmp/shown", "--hidden-until", "none", "--root", str(project)], project)
        assert done.returncode == 0, done.stderr


# --- validate -----------------------------------------------------------------------------------------------------


class TestValidate:
    def test_a_malformed_date_is_an_error(self, tmp_path):
        make_task(tmp_path, "tmp/t", hidden_until="soon")
        done = run_cli(["validate", "tmp/t", "--root", str(tmp_path)], tmp_path)
        assert done.returncode != 0
        assert "hidden_until" in done.stderr + done.stdout

    def test_a_non_string_value_is_an_error(self, tmp_path):
        make_task(tmp_path, "tmp/t", hidden_until=[1])
        assert run_cli(["validate", "tmp/t", "--root", str(tmp_path)], tmp_path).returncode != 0

    def test_a_past_date_is_fine(self, project):
        assert run_cli(["validate", "tmp/was-hidden", "--root", str(project)], project).returncode == 0

    def test_a_malformed_task_still_lists(self, tmp_path):
        make_task(tmp_path, "tmp/t", hidden_until="soon")
        assert "tmp/t" in run_cli(["list", "--root", str(tmp_path)], tmp_path).stdout


# --- the dashboard export ---------------------------------------------------------------------------------------------


@pytest.fixture
def parent(tmp_path: Path) -> Path:
    root = tmp_path / "parent"
    project = root / "alpha"
    make_task(project, "dev/tasks/shown")
    make_task(project, "dev/tasks/later", hidden_until="2026-11-30")
    return root


class TestExport:
    def payload(self, parent: Path, day: datetime.date, **kw):
        return export.tasks_payload(
            (parent,),
            resolve_machine=lambda: None,
            local_now=lambda: datetime.datetime.combine(day, datetime.time(9)),
            **kw,
        )

    def test_the_dashboard_payload_omits_a_hidden_task_until_its_date(self, parent):
        before = {t["path"] for t in self.payload(parent, datetime.date(2026, 11, 29))["tasks"]}
        assert before == {"dev/tasks/shown"}
        on = {t["path"] for t in self.payload(parent, datetime.date(2026, 11, 30))["tasks"]}
        assert on == {"dev/tasks/shown", "dev/tasks/later"}

    def test_include_hidden_keeps_it(self, parent):
        got = {t["path"] for t in self.payload(parent, datetime.date(2026, 11, 29), include_hidden=True)["tasks"]}
        assert got == {"dev/tasks/shown", "dev/tasks/later"}

    def test_export_item_by_name_still_reaches_a_hidden_task(self, parent):
        folder = parent / "alpha" / "dev" / "tasks" / "later"
        (folder / "plan.md").write_text(
            "# Plan\n\n```yaml\ntask_items:\n  items:\n    - {id: one, title: One, state: available}\n```\n",
            encoding="utf-8",
        )
        found = export.item_payload(export.checkout_id(parent / "alpha"), "dev/tasks/later", "one", (parent,))
        assert found["item"]["id"] == "one"


class TestExportCli:
    """``task-kit export tasks --include-hidden``: the flag reaches ``tasks_payload``; the payload dates the task.

    Revert checks: dropping ``include_hidden`` from ``export_cli._payload`` turns the flag and env tests red;
    dropping ``hidden_until`` from ``export.task_facts`` turns the marker tests red.
    """

    ARGV = ["tasks", "--format", "json", "--protocol", "1"]

    def tasks(self, parent: Path, *extra: str) -> dict:
        code, doc = export_cli.document([*self.ARGV, "--parent", str(parent), *extra], **inject())
        assert code == 0, doc
        return {t["path"]: t for t in doc["payload"]["tasks"]}

    def test_default_leaves_a_hidden_task_out(self, parent):
        assert set(self.tasks(parent)) == {"dev/tasks/shown"}

    def test_the_flag_brings_it_back_with_its_date(self, parent):
        found = self.tasks(parent, "--include-hidden")
        assert set(found) == {"dev/tasks/shown", "dev/tasks/later"}
        assert found["dev/tasks/later"]["hidden_until"] == "2026-11-30"

    def test_a_visible_task_carries_a_null_hidden_until(self, parent):
        assert self.tasks(parent, "--include-hidden")["dev/tasks/shown"]["hidden_until"] is None

    def test_the_flag_is_refused_for_the_other_kinds(self, parent):
        for argv in (["protocols", "--format", "json", "--include-hidden"],
                     ["item", "--format", "json", "--protocol", "1", "--checkout", "c", "--task", "t", "--item", "i",
                      "--include-hidden"]):
            code, doc = export_cli.document(argv, **inject())
            assert code == 2 and doc["error"]["code"] == "usage", doc
            assert "--include-hidden" in doc["error"]["message"]

    def test_the_real_command_line_takes_the_flag(self, parent, tmp_path):
        env = dict(os.environ)
        env.update({_REEXEC_SKIP_ENV: "1", "PYTHONPATH": str(_REPO_ROOT / "plugins" / "skills-kit"),
                    "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)})
        entry = _REPO_ROOT / "plugins" / "task-kit" / "bin" / "task_kit_entrypoint.py"
        done = subprocess.run(
            [sys.executable, str(entry), "export", *self.ARGV, "--parent", str(parent), "--include-hidden"],
            capture_output=True, text=True, env=env, timeout=120,
        )
        assert done.returncode == 0, done.stderr
        paths = {t["path"] for t in json.loads(done.stdout)["payload"]["tasks"]}
        assert paths == {"dev/tasks/shown", "dev/tasks/later"}
