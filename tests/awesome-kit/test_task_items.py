"""End-to-end tests for the task-items surface (design/task-items-design.md).

Covers the TASK_ITEMS_SCHEMA acceptance/rejection floor, read_task_items
(extraction, singularity, plan.md placement, post-walker vocabulary/id
checks, lenient projection), sort_items ordering, the CLAUDE.md
stale-priority-reference heuristic, the validate.py integration (missing
block = pre-contract warning; block findings = errors; both gate work), the
``items`` CLI verb (line format, filters, current-task default, exit codes),
the ``status`` substrate's items section, and the init scaffold's empty
block. The homeassistant-shaped fixture is the design doc's section 10 test
case reduced to three items.

All fixtures build under pytest tmp_path with injected pointer paths -- the
real repo's tmp/, dev/, and ~/.claude are never touched.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from bootstrap_guard import _REEXEC_GUARD_ENV
from skills_kit_lib import schema_engine
from task_system import state_ops
from task_system.init import init_task
from task_system.schemas import TASK_ITEMS_SCHEMA
from task_system.state_ops import StateOpError
from task_system.task_items import (
    ItemRecord,
    read_task_items,
    sort_items,
    stale_priority_refs,
)
from task_system.types import DEFAULT_TYPE_NAME, get_type
from task_system.validate import validate_ref

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

HAND_OFF = get_type(DEFAULT_TYPE_NAME)

ITEMS_BLOCK = """\
```yaml
task_items:
  items:
    - id: hue-scene-automation
      title: "Hue scene automation"
      state: available
    - id: nano-swipe-controls
      title: "Nano swipe-gesture controls"
      state: in-flight
      priority: P1
      note: "resume point per the 2026-07-09 banner"
    - id: bravia-google-home
      title: "Move the Bravia into the right Google Home"
      state: blocked-user
      priority: P2
```
"""

EMPTY_BLOCK = "```yaml\ntask_items:\n  items: []\n```\n"


def run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run task.py in a subprocess, hermetically (same pattern as Steps 2-5)."""
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
    project_root: Path,
    rel: str,
    *,
    plan_md: str = f"# Plan\n\n## Forward overview\n\n{EMPTY_BLOCK}",
    claude_md: str = "placeholder\n",
) -> Path:
    """A structurally-valid hand-off folder with a controllable plan/CLAUDE."""
    folder = project_root / rel
    folder.mkdir(parents=True)
    (folder / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "task": {
                    "_schema_version": "1",
                    "type": "hand-off",
                    "title": "A task",
                    "status": "active",
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (folder / "plan.md").write_text(plan_md, encoding="utf-8")
    (folder / "CLAUDE.md").write_text(claude_md, encoding="utf-8")
    (folder / "log.md").write_text("placeholder\n", encoding="utf-8")
    return folder


def _items_data(items: list[dict]) -> dict:
    return {"task_items": {"items": items}}


def _minimal_item(**over) -> dict:
    item = {"id": "fix-the-run", "title": "Fix the run", "state": "available"}
    item.update(over)
    return item


class TestTaskItemsSchema:
    def _fails(self, data):
        fails, _ = schema_engine.validate(data, TASK_ITEMS_SCHEMA)
        return fails

    def test_empty_items_valid(self):
        assert self._fails(_items_data([])) == []

    def test_minimal_and_full_item_valid(self):
        full = _minimal_item(priority="P1", note="after the spike")
        assert self._fails(_items_data([_minimal_item(), full])) == []

    @pytest.mark.parametrize("missing", ["id", "title", "state"])
    def test_required_field_omission(self, missing):
        item = _minimal_item()
        del item[missing]
        fails = self._fails(_items_data([item]))
        assert any(missing in path for path, _ in fails), fails

    def test_empty_title_rejected(self):
        fails = self._fails(_items_data([_minimal_item(title="  ")]))
        assert any("title must be non-empty" in msg for _, msg in fails), fails

    def test_missing_items_key_rejected(self):
        assert self._fails({"task_items": {}}), "items key is required"

    def test_unknown_extra_key_passes(self):
        # Schemas are floors, not ceilings.
        item = _minimal_item(custom="anything")
        assert self._fails(_items_data([item])) == []


class TestReadTaskItems:
    def test_absent_block(self, tmp_path):
        folder = make_task(tmp_path, "tmp/a", plan_md="# Plan\n")
        result = read_task_items(folder, HAND_OFF)
        assert result.block_found is False
        assert result.errors == []
        assert result.items == []

    def test_parses_records_in_block_order(self, tmp_path):
        folder = make_task(
            tmp_path, "tmp/a", plan_md=f"# Plan\n\n{ITEMS_BLOCK}"
        )
        result = read_task_items(folder, HAND_OFF)
        assert result.block_found is True
        assert result.errors == []
        assert [it.id for it in result.items] == [
            "hue-scene-automation",
            "nano-swipe-controls",
            "bravia-google-home",
        ]
        nano = result.items[1]
        assert nano.state == "in-flight"
        assert nano.priority == "P1"
        assert nano.note == "resume point per the 2026-07-09 banner"
        assert result.items[0].priority is None
        assert result.items[0].note is None

    def test_duplicate_id_is_error(self, tmp_path):
        block = (
            "```yaml\ntask_items:\n  items:\n"
            "    - {id: a-b, title: T, state: available}\n"
            "    - {id: a-b, title: U, state: deferred}\n```\n"
        )
        folder = make_task(tmp_path, "tmp/a", plan_md=block)
        result = read_task_items(folder, HAND_OFF)
        assert any("duplicate id 'a-b'" in e for e in result.errors)
        assert len(result.items) == 2  # lenient projection

    def test_bad_state_and_priority_and_id_are_errors(self, tmp_path):
        block = (
            "```yaml\ntask_items:\n  items:\n"
            "    - {id: Bad_Id, title: T, state: doing, priority: high}\n```\n"
        )
        folder = make_task(tmp_path, "tmp/a", plan_md=block)
        result = read_task_items(folder, HAND_OFF)
        joined = "\n".join(result.errors)
        assert "not kebab-case" in joined
        assert "state 'doing' not in" in joined
        assert "priority 'high' does not match" in joined

    def test_multiple_blocks_is_error(self, tmp_path):
        folder = make_task(
            tmp_path, "tmp/a", plan_md=f"# Plan\n\n{EMPTY_BLOCK}"
        )
        (folder / "notes.md").write_text(EMPTY_BLOCK, encoding="utf-8")
        result = read_task_items(folder, HAND_OFF)
        assert any("multiple task_items blocks" in e for e in result.errors)
        assert result.items == []

    def test_block_outside_plan_md_is_error(self, tmp_path):
        folder = make_task(tmp_path, "tmp/a", plan_md="# Plan\n")
        (folder / "notes.md").write_text(EMPTY_BLOCK, encoding="utf-8")
        result = read_task_items(folder, HAND_OFF)
        assert any(
            "found in notes.md" in e and "lives in plan.md" in e
            for e in result.errors
        )

    def test_unparseable_block_naming_the_key_is_error(self, tmp_path):
        bad = "```yaml\ntask_items: [unclosed\n```\n"
        folder = make_task(tmp_path, "tmp/a", plan_md=bad)
        result = read_task_items(folder, HAND_OFF)
        assert result.block_found is True
        assert any("unparseable YAML block" in e for e in result.errors)

    def test_schema_violation_is_error_no_items(self, tmp_path):
        block = (
            "```yaml\ntask_items:\n  items:\n"
            "    - {id: a-b, title: T}\n```\n"  # missing state
        )
        folder = make_task(tmp_path, "tmp/a", plan_md=block)
        result = read_task_items(folder, HAND_OFF)
        assert any("schema violation" in e for e in result.errors)
        assert result.items == []


class TestSortItems:
    def test_priority_then_block_order(self):
        items = [
            ItemRecord(id="c", title="C", state="available"),
            ItemRecord(id="a", title="A", state="deferred", priority="P2"),
            ItemRecord(id="b", title="B", state="in-flight", priority="P1"),
            ItemRecord(id="d", title="D", state="available", priority="P2"),
        ]
        assert [it.id for it in sort_items(items)] == ["b", "a", "d", "c"]


class TestStalePriorityRefs:
    def test_unknown_hyphenated_token_is_stale(self):
        text = (
            "# Project Overview\n\n## Immediate Priorities\n\n"
            "- Resume `nano-swipe-controls` first.\n"
            "- Then look at `gone-item`.\n\n## Project vocabulary\n\n"
            "- `other-unknown` outside the section is ignored.\n"
        )
        stale = stale_priority_refs(text, {"nano-swipe-controls"})
        assert stale == ["gone-item"]

    def test_hyphenless_and_dotted_tokens_ignored(self):
        text = (
            "## Immediate Priorities\n\n"
            "- Run `task items`; see `plan.md`; state is `active`.\n"
        )
        assert stale_priority_refs(text, set()) == []

    def test_absent_section_is_empty(self):
        assert stale_priority_refs("# Nothing here\n", set()) == []


class TestValidateIntegration:
    def test_empty_block_is_clean(self, tmp_path):
        make_task(tmp_path, "tmp/a")
        result = validate_ref("tmp/a", tmp_path)
        assert result.classification == "active"
        assert result.clean

    def test_missing_block_is_precontract_warning(self, tmp_path):
        make_task(tmp_path, "tmp/a", plan_md="# Plan\n")
        result = validate_ref("tmp/a", tmp_path)
        assert result.classification == "active"
        assert result.errors == []
        assert any("no task_items block" in w for w in result.warnings)

    def test_block_findings_are_errors(self, tmp_path):
        block = (
            "```yaml\ntask_items:\n  items:\n"
            "    - {id: a-b, title: T, state: doing}\n```\n"
        )
        make_task(tmp_path, "tmp/a", plan_md=block)
        result = validate_ref("tmp/a", tmp_path)
        assert result.classification == "invalid"
        assert any("state 'doing' not in" in e for e in result.errors)

    def test_stale_claude_md_reference_is_warning(self, tmp_path):
        claude = (
            "# Project Overview\n\n## Immediate Priorities\n\n"
            "- Resume `gone-item`.\n"
        )
        make_task(tmp_path, "tmp/a", claude_md=claude)
        result = validate_ref("tmp/a", tmp_path)
        assert any(
            "stale item reference" in w and "`gone-item`" in w
            for w in result.warnings
        )

    def test_missing_block_warning_gates_work(self, tmp_path):
        make_task(tmp_path, "tmp/a", plan_md="# Plan\n")
        ptr = tmp_path / "pointer" / "current"
        with pytest.raises(StateOpError) as exc_info:
            state_ops.work("tmp/a", tmp_path, ptr)
        assert any("no task_items block" in w for w in exc_info.value.warnings)


class TestInitScaffold:
    def test_scaffold_has_empty_block_and_validates_clean(self, tmp_path):
        folder = init_task("fresh-spike", tmp_path)
        plan = (folder / "plan.md").read_text(encoding="utf-8")
        assert "task_items:" in plan
        result = read_task_items(folder, HAND_OFF)
        assert result.block_found is True
        assert result.errors == []
        assert result.items == []
        check = validate_ref("tmp/fresh-spike", tmp_path)
        assert check.clean


class TestItemsCli:
    def test_lines_sorted_by_priority_then_order(self, tmp_path):
        make_task(tmp_path, "tmp/a", plan_md=f"# Plan\n\n{ITEMS_BLOCK}")
        proc = run_cli(["items", "tmp/a", "--root", str(tmp_path)], tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == [
            "nano-swipe-controls  in-flight  P1  Nano swipe-gesture controls",
            (
                "bravia-google-home  blocked-user  P2  "
                "Move the Bravia into the right Google Home"
            ),
            "hue-scene-automation  available  -  Hue scene automation",
        ]

    def test_state_and_priority_filters(self, tmp_path):
        make_task(tmp_path, "tmp/a", plan_md=f"# Plan\n\n{ITEMS_BLOCK}")
        proc = run_cli(
            ["items", "tmp/a", "--state", "available", "--root", str(tmp_path)],
            tmp_path,
        )
        assert proc.returncode == 0
        assert proc.stdout.splitlines() == [
            "hue-scene-automation  available  -  Hue scene automation"
        ]
        proc = run_cli(
            ["items", "tmp/a", "--priority", "P1", "--root", str(tmp_path)],
            tmp_path,
        )
        assert [l.split("  ")[0] for l in proc.stdout.splitlines()] == [
            "nano-swipe-controls"
        ]

    def test_empty_block_prints_nothing_exit_zero(self, tmp_path):
        make_task(tmp_path, "tmp/a")
        proc = run_cli(["items", "tmp/a", "--root", str(tmp_path)], tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""

    def test_precontract_folder_notes_to_stderr_exit_zero(self, tmp_path):
        make_task(tmp_path, "tmp/a", plan_md="# Plan\n")
        proc = run_cli(["items", "tmp/a", "--root", str(tmp_path)], tmp_path)
        assert proc.returncode == 0
        assert proc.stdout == ""
        assert "note: no task_items block" in proc.stderr

    def test_ref_defaults_to_current(self, tmp_path):
        make_task(tmp_path, "tmp/a", plan_md=f"# Plan\n\n{ITEMS_BLOCK}")
        ptr = tmp_path / "pointer" / "current"
        state_ops.work("tmp/a", tmp_path, ptr)
        proc = run_cli(["items", "--pointer", str(ptr)], tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert len(proc.stdout.splitlines()) == 3

    def test_nothing_current_errors(self, tmp_path):
        ptr = tmp_path / "pointer" / "current"
        proc = run_cli(["items", "--pointer", str(ptr)], tmp_path)
        assert proc.returncode == 1
        assert "nothing is current" in proc.stderr

    def test_unreadable_folder_errors(self, tmp_path):
        proc = run_cli(
            ["items", "dev/tasks/long-gone", "--root", str(tmp_path)], tmp_path
        )
        assert proc.returncode == 1
        assert "no task folder readable locally" in proc.stderr


class TestStatusSubstrate:
    def test_items_in_substrate(self, tmp_path):
        make_task(tmp_path, "tmp/a", plan_md=f"# Plan\n\n{ITEMS_BLOCK}")
        proc = run_cli(["status", "tmp/a", "--root", str(tmp_path)], tmp_path)
        assert proc.returncode == 0, proc.stderr
        lines = proc.stdout.splitlines()
        assert "items:" in lines
        assert (
            "  nano-swipe-controls  in-flight  P1  Nano swipe-gesture controls"
            in lines
        )

    def test_empty_and_precontract_renderings(self, tmp_path):
        make_task(tmp_path, "tmp/empty")
        proc = run_cli(["status", "tmp/empty", "--root", str(tmp_path)], tmp_path)
        assert "items: none (empty task_items block)" in proc.stdout
        make_task(tmp_path, "tmp/pre", plan_md="# Plan\n")
        proc = run_cli(["status", "tmp/pre", "--root", str(tmp_path)], tmp_path)
        assert "items: no task_items block in plan.md (pre-contract)" in proc.stdout
