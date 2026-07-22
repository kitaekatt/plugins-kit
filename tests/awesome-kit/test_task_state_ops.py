"""End-to-end tests for the task-system state ops (Step 4).

Covers the spec section 7.1 verbs ``work`` / ``switch`` / ``update`` /
``close`` / ``reopen`` plus the spec 2.6 ``current`` pointer semantics they
ride on: the validate gate on work (errors AND warnings both block, spec 9),
the remote-ref error, auto-init promotion, the absolute pointer write, the
Skill(...)/agent_hint emission, switch's update-previous-then-work flow and
stale-pointer clearing, update's upsert + field-edit persistence (lists
REPLACE; unknown extra fields round-trip; the write persists even with
findings), the minimal log.md rotation reading (one dated entry appended,
plan.md untouched), close/reopen preconditions and pointer clearing, and the
``current`` verb's stale handling.

All fixtures build under pytest tmp_path with an injected pointer path -- the
real repo's tmp/, dev/, and ~/.claude are never touched.
"""

import datetime
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from bootstrap_guard import _REEXEC_GUARD_ENV
from task_system import state_ops
from task_system.pointer import read_current, write_current
from task_system.state_ops import StateOpError, derive_root_and_canonical
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

SCAFFOLD_FILES = ("CLAUDE.md", "plan.md", "log.md", "task.yaml")
LOCAL = "testhost"
OTHER = "definitely-not-this-host"


def run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run task.py in a subprocess, hermetically (same pattern as Steps 2-3)."""
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


PLAN_MD_SCAFFOLD = "# Plan\n\n```yaml\ntask_items:\n  items: []\n```\n"


def make_task(
    project_root: Path,
    rel: str,
    *,
    yaml_text: str | None = None,
    **fields,
) -> Path:
    """Create a task folder at <project_root>/<rel> with a hand-off layout."""
    folder = project_root / rel
    folder.mkdir(parents=True)
    if yaml_text is not None:
        (folder / "task.yaml").write_text(yaml_text, encoding="utf-8")
    else:
        block = {
            "_schema_version": "1",
            "type": "hand-off",
            "title": "A task",
            "status": "active",
        }
        block.update(fields)
        (folder / "task.yaml").write_text(
            yaml.safe_dump({"task": block}, sort_keys=False), encoding="utf-8"
        )
    (folder / "plan.md").write_text(PLAN_MD_SCAFFOLD, encoding="utf-8")
    for fname in ("CLAUDE.md", "log.md"):
        (folder / fname).write_text("placeholder\n", encoding="utf-8")
    return folder


def read_block(folder: Path) -> dict:
    data = yaml.safe_load((folder / "task.yaml").read_text(encoding="utf-8"))
    return data["task"]


@pytest.fixture
def ptr(tmp_path) -> Path:
    """An injected pointer path -- never the user-global default."""
    return tmp_path / "pointer" / "current"


class TestDeriveRootAndCanonical:
    def test_tmp_shape(self, tmp_path):
        folder = tmp_path / "tmp" / "spike"
        assert derive_root_and_canonical(folder) == (tmp_path, "tmp/spike")

    def test_dev_tasks_shape(self, tmp_path):
        folder = tmp_path / "dev" / "tasks" / "durable"
        assert derive_root_and_canonical(folder) == (
            tmp_path,
            "dev/tasks/durable",
        )

    def test_relative_path_is_none(self):
        assert derive_root_and_canonical(Path("tmp/spike")) is None

    def test_unknown_shape_is_none(self, tmp_path):
        assert derive_root_and_canonical(tmp_path / "docs" / "spike") is None


class TestWorkLib:
    def test_pass_writes_absolute_pointer(self, tmp_path, ptr):
        folder = make_task(tmp_path, "tmp/a")
        result = state_ops.work("tmp/a", tmp_path, ptr)
        assert result.canonical == "tmp/a"
        assert result.folder == folder.resolve()
        stored = ptr.read_text(encoding="utf-8")
        assert stored == f"{folder.resolve()}\n"
        assert Path(stored.strip()).is_absolute()

    def test_pass_surfaces_skills_and_agent_hint(self, tmp_path, ptr):
        make_task(
            tmp_path,
            "tmp/a",
            skills_to_invoke=["home-domain", "md-read"],
            agent_hint="backend-developer",
        )
        result = state_ops.work("tmp/a", tmp_path, ptr)
        assert result.skills_to_invoke == ("home-domain", "md-read")
        assert result.agent_hint == "backend-developer"

    def test_error_finding_blocks_and_pointer_unwritten(self, tmp_path, ptr):
        make_task(tmp_path, "tmp/a", status="bogus")  # out of vocabulary
        with pytest.raises(StateOpError) as exc_info:
            state_ops.work("tmp/a", tmp_path, ptr)
        assert exc_info.value.errors
        assert read_current(ptr) is None

    def test_warning_only_finding_blocks(self, tmp_path, ptr):
        # A dangling depends_on entry is a warning -- warnings gate work too
        # (spec 9: errors AND warnings both block).
        make_task(tmp_path, "tmp/a", depends_on=["tmp/gone"])
        with pytest.raises(StateOpError) as exc_info:
            state_ops.work("tmp/a", tmp_path, ptr)
        assert exc_info.value.errors == []
        assert exc_info.value.warnings
        assert read_current(ptr) is None

    def test_remote_ref_errors(self, tmp_path, ptr):
        with pytest.raises(StateOpError, match="remote"):
            state_ops.work(
                "tmp/elsewhere",
                tmp_path,
                ptr,
                ref_host=OTHER,
                local_host=LOCAL,
            )
        assert read_current(ptr) is None

    def test_auto_init_promotion_at_tmp_path(self, tmp_path, ptr):
        result = state_ops.work("tmp/fresh-spike", tmp_path, ptr)
        assert result.initialized is True
        folder = tmp_path / "tmp" / "fresh-spike"
        for fname in SCAFFOLD_FILES:
            assert (folder / fname).is_file(), fname
        # The promoted task validates active (init's invariant).
        check = validate_ref("tmp/fresh-spike", tmp_path)
        assert check.classification == "active"
        assert check.clean
        assert read_current(ptr) == str(folder.resolve())

    def test_dev_tasks_promotion_inits_then_blocks_on_warning(
        self, tmp_path, ptr
    ):
        # Inside a git repo, the freshly-initialized dev/tasks folder is
        # dirty, so it carries the uncommitted warning -- work blocks, but
        # the promotion ran a real init and the folder is KEPT (the
        # documented Step 4 reading).
        subprocess.run(
            ["git", "init", "-q", str(tmp_path)],
            check=True,
            capture_output=True,
        )
        with pytest.raises(StateOpError) as exc_info:
            state_ops.work("dev/tasks/durable", tmp_path, ptr)
        assert any("uncommitted" in w for w in exc_info.value.warnings)
        folder = tmp_path / "dev" / "tasks" / "durable"
        for fname in SCAFFOLD_FILES:
            assert (folder / fname).is_file(), fname
        assert read_current(ptr) is None

    def test_dev_tasks_promotion_outside_git_succeeds(self, tmp_path, ptr):
        # Outside any git repo the script cannot verify VCS state (no git
        # dependency): validate emits only an advisory note, so promotion
        # completes and the pointer is written.
        result = state_ops.work("dev/tasks/durable", tmp_path, ptr)
        assert result.initialized is True
        folder = tmp_path / "dev" / "tasks" / "durable"
        assert read_current(ptr) == str(folder.resolve())

    def test_unsafe_stub_cannot_auto_init(self, tmp_path, ptr):
        # init would rewrite "UPPER" to "upper" -- promotion refuses rather
        # than creating a folder at a different path than the ref named.
        with pytest.raises(StateOpError, match="auto-init"):
            state_ops.work("tmp/UPPER", tmp_path, ptr)
        assert not (tmp_path / "tmp" / "UPPER").exists()
        assert not (tmp_path / "tmp" / "upper").exists()

    def test_bare_stub_matching_nothing_errors(self, tmp_path, ptr):
        with pytest.raises(StateOpError, match="matches no task folder"):
            state_ops.work("no-such-stub", tmp_path, ptr)
        assert read_current(ptr) is None


class TestSwitchLib:
    def test_live_current_is_updated_and_stays_active(self, tmp_path, ptr):
        folder_a = make_task(tmp_path, "tmp/a")
        make_task(tmp_path, "tmp/b")
        state_ops.work("tmp/a", tmp_path, ptr)
        log_before = (folder_a / "log.md").read_text(encoding="utf-8")
        result = state_ops.switch("tmp/b", tmp_path, ptr)
        assert result.previous is not None
        assert result.previous.canonical == "tmp/a"
        assert result.previous.validation.classification == "active"
        assert read_block(folder_a)["status"] == "active"
        # The previous task was updated (the dated log.md entry landed).
        log_after = (folder_a / "log.md").read_text(encoding="utf-8")
        assert log_after.startswith(log_before)
        assert len(log_after) > len(log_before)
        # The pointer moved to the new task.
        assert read_current(ptr) == str(
            (tmp_path / "tmp" / "b").resolve()
        )

    def test_nothing_current_behaves_like_work(self, tmp_path, ptr):
        folder = make_task(tmp_path, "tmp/b")
        result = state_ops.switch("tmp/b", tmp_path, ptr)
        assert result.previous is None
        assert read_current(ptr) == str(folder.resolve())

    def test_stale_pointer_cleared_and_behaves_like_work(self, tmp_path, ptr):
        write_current(ptr, tmp_path / "tmp" / "ghost")  # folder never existed
        folder = make_task(tmp_path, "tmp/b")
        result = state_ops.switch("tmp/b", tmp_path, ptr)
        assert result.previous is None
        assert read_current(ptr) == str(folder.resolve())

    def test_stale_pointer_cleared_even_when_work_blocks(self, tmp_path, ptr):
        write_current(ptr, tmp_path / "tmp" / "ghost")
        make_task(tmp_path, "tmp/b", status="bogus")
        with pytest.raises(StateOpError):
            state_ops.switch("tmp/b", tmp_path, ptr)
        # The stale pointer was cleared before work raised.
        assert read_current(ptr) is None


class TestUpdateLib:
    def test_defaults_to_current_and_derives_root(self, tmp_path, ptr):
        folder = make_task(tmp_path, "tmp/a")
        state_ops.work("tmp/a", tmp_path, ptr)
        # Pass a DIFFERENT project_root: the pointer's absolute path derives
        # the real root, superseding the argument.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        result = state_ops.update(None, elsewhere, ptr, priority="P1")
        assert result.canonical == "tmp/a"
        assert read_block(folder)["priority"] == "P1"

    def test_no_ref_and_nothing_current_errors(self, tmp_path, ptr):
        with pytest.raises(StateOpError, match="nothing is current"):
            state_ops.update(None, tmp_path, ptr, priority="P1")

    def test_upsert_inits_when_folder_absent(self, tmp_path, ptr):
        result = state_ops.update("tmp/fresh", tmp_path, ptr)
        assert result.initialized is True
        folder = tmp_path / "tmp" / "fresh"
        for fname in SCAFFOLD_FILES:
            assert (folder / fname).is_file(), fname
        assert result.validation.classification == "active"
        assert result.validation.clean

    def test_each_field_edit_persists(self, tmp_path, ptr):
        make_task(tmp_path, "tmp/dep")  # keep depends_on non-dangling
        folder = make_task(tmp_path, "tmp/a")
        state_ops.update(
            "tmp/a",
            tmp_path,
            ptr,
            status="closed",
            priority="P1",
            description="new words",
            depends_on=["tmp/dep"],
            blocked_by=["tmp/dep"],
            agent_hint="backend-developer",
            skills_to_invoke=["home-domain"],
        )
        block = read_block(folder)
        assert block["status"] == "closed"
        assert block["priority"] == "P1"
        assert block["description"] == "new words"
        assert block["depends_on"] == ["tmp/dep"]
        assert block["blocked_by"] == ["tmp/dep"]
        assert block["agent_hint"] == "backend-developer"
        assert block["skills_to_invoke"] == ["home-domain"]

    def test_list_valued_edits_replace_stored_list(self, tmp_path, ptr):
        make_task(tmp_path, "tmp/c")
        folder = make_task(
            tmp_path, "tmp/a", skills_to_invoke=["one", "two"]
        )
        state_ops.update(
            "tmp/a", tmp_path, ptr, depends_on=["tmp/c"], skills_to_invoke=["three"]
        )
        block = read_block(folder)
        assert block["depends_on"] == ["tmp/c"]  # not appended to anything
        assert block["skills_to_invoke"] == ["three"]  # REPLACED, not merged

    def test_unknown_extra_fields_survive_round_trip(self, tmp_path, ptr):
        folder = make_task(tmp_path, "tmp/a", custom_field="keepme")
        # A root-level sibling of the task block also round-trips.
        data = yaml.safe_load(
            (folder / "task.yaml").read_text(encoding="utf-8")
        )
        data["extra_root"] = {"nested": 1}
        (folder / "task.yaml").write_text(
            yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
        )
        state_ops.update("tmp/a", tmp_path, ptr, priority="P2")
        after = yaml.safe_load(
            (folder / "task.yaml").read_text(encoding="utf-8")
        )
        assert after["task"]["custom_field"] == "keepme"
        assert after["task"]["priority"] == "P2"
        assert after["extra_root"] == {"nested": 1}

    def test_log_md_gets_one_dated_entry_plan_md_untouched(self, tmp_path, ptr):
        # Pins the Step 4 rotation reading: the script's mechanical share is
        # ONE dated log.md entry per update recording the edits; plan.md is
        # left to the skill layer (untouched by the script).
        folder = make_task(tmp_path, "tmp/a")
        plan_before = (folder / "plan.md").read_text(encoding="utf-8")
        log_before = (folder / "log.md").read_text(encoding="utf-8")
        state_ops.update("tmp/a", tmp_path, ptr, priority="P1")
        assert (folder / "plan.md").read_text(encoding="utf-8") == plan_before
        log_after = (folder / "log.md").read_text(encoding="utf-8")
        assert log_after.startswith(log_before)
        added = log_after[len(log_before):]
        today = datetime.date.today().isoformat()
        assert added == f"- {today}: update: priority = 'P1'\n"

    def test_update_without_edits_logs_a_refresh_entry(self, tmp_path, ptr):
        folder = make_task(tmp_path, "tmp/a")
        state_ops.update("tmp/a", tmp_path, ptr)
        log = (folder / "log.md").read_text(encoding="utf-8")
        today = datetime.date.today().isoformat()
        assert log.endswith(f"- {today}: update: refresh (no field edits)\n")

    def test_warning_case_still_persists_the_edit(self, tmp_path, ptr):
        folder = make_task(tmp_path, "tmp/a")
        result = state_ops.update(
            "tmp/a", tmp_path, ptr, depends_on=["tmp/gone"]
        )
        assert result.validation.warnings  # dangling depends_on
        assert result.validation.classification == "active"
        assert read_block(folder)["depends_on"] == ["tmp/gone"]

    def test_invalid_value_persists_fix_forward(self, tmp_path, ptr):
        # update is a write op; validate reports. A bad value persists and
        # surfaces as a finding (the fix-forward posture).
        folder = make_task(tmp_path, "tmp/a")
        result = state_ops.update("tmp/a", tmp_path, ptr, status="bogus")
        assert result.validation.classification == "invalid"
        assert result.validation.errors
        assert read_block(folder)["status"] == "bogus"

    def test_remote_ref_errors(self, tmp_path, ptr):
        with pytest.raises(StateOpError, match="remote"):
            state_ops.update(
                "tmp/elsewhere",
                tmp_path,
                ptr,
                ref_host=OTHER,
                local_host=LOCAL,
            )
        assert not (tmp_path / "tmp" / "elsewhere").exists()


class TestCloseLib:
    def test_active_to_closed_folder_kept(self, tmp_path, ptr):
        folder = make_task(tmp_path, "tmp/a")
        canonical = state_ops.close("tmp/a", tmp_path, ptr)
        assert canonical == "tmp/a"
        assert read_block(folder)["status"] == "closed"
        for fname in SCAFFOLD_FILES:
            assert (folder / fname).is_file(), fname

    def test_pointer_cleared_when_it_named_this_task(self, tmp_path, ptr):
        make_task(tmp_path, "tmp/a")
        state_ops.work("tmp/a", tmp_path, ptr)
        assert read_current(ptr) is not None
        state_ops.close("tmp/a", tmp_path, ptr)
        assert read_current(ptr) is None

    def test_pointer_untouched_when_it_names_another_task(self, tmp_path, ptr):
        folder_a = make_task(tmp_path, "tmp/a")
        make_task(tmp_path, "tmp/b")
        state_ops.work("tmp/a", tmp_path, ptr)
        state_ops.close("tmp/b", tmp_path, ptr)
        assert read_current(ptr) == str(folder_a.resolve())

    def test_non_active_status_errors(self, tmp_path, ptr):
        folder = make_task(tmp_path, "tmp/a", status="closed")
        with pytest.raises(StateOpError, match="active"):
            state_ops.close("tmp/a", tmp_path, ptr)
        assert read_block(folder)["status"] == "closed"

    def test_missing_folder_errors(self, tmp_path, ptr):
        with pytest.raises(StateOpError, match="no task folder"):
            state_ops.close("tmp/ghost", tmp_path, ptr)


class TestReopenLib:
    def test_closed_to_active(self, tmp_path, ptr):
        folder = make_task(tmp_path, "tmp/a", status="closed")
        result = state_ops.reopen("tmp/a", tmp_path, ptr)
        assert read_block(folder)["status"] == "active"
        assert result.validation.classification == "active"
        assert result.validation.clean
        assert result.initialized is False

    def test_tmp_archived_to_active(self, tmp_path, ptr):
        folder = make_task(tmp_path, "tmp/a", status="archived")
        result = state_ops.reopen("tmp/a", tmp_path, ptr)
        assert read_block(folder)["status"] == "active"
        assert result.validation.classification == "active"

    def test_missing_folder_errors(self, tmp_path, ptr):
        with pytest.raises(StateOpError, match="cannot be reopened"):
            state_ops.reopen("tmp/ghost", tmp_path, ptr)


class TestWorkCLI:
    def test_pass_emits_skill_lines_and_agent_hint(self, tmp_path, ptr):
        folder = make_task(
            tmp_path,
            "tmp/a",
            skills_to_invoke=["home-domain", "md-read"],
            agent_hint="backend-developer",
        )
        proc = run_cli(
            ["work", "tmp/a", "--root", str(tmp_path), "--pointer", str(ptr)],
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == [
            'Skill(skill: "home-domain")',
            'Skill(skill: "md-read")',
            "agent_hint: backend-developer",
        ]
        assert read_current(ptr) == str(folder.resolve())

    def test_error_finding_exits_nonzero_findings_on_stderr(self, tmp_path, ptr):
        make_task(tmp_path, "tmp/a", status="bogus")
        proc = run_cli(
            ["work", "tmp/a", "--root", str(tmp_path), "--pointer", str(ptr)],
            tmp_path,
        )
        assert proc.returncode != 0
        assert proc.stdout == ""
        assert "error:" in proc.stderr
        assert read_current(ptr) is None

    def test_warning_only_finding_exits_nonzero(self, tmp_path, ptr):
        make_task(tmp_path, "tmp/a", depends_on=["tmp/gone"])
        proc = run_cli(
            ["work", "tmp/a", "--root", str(tmp_path), "--pointer", str(ptr)],
            tmp_path,
        )
        assert proc.returncode != 0
        assert "warning:" in proc.stderr
        assert read_current(ptr) is None

    def test_auto_init_promotion(self, tmp_path, ptr):
        proc = run_cli(
            [
                "work",
                "tmp/fresh-spike",
                "--root",
                str(tmp_path),
                "--pointer",
                str(ptr),
            ],
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        folder = tmp_path / "tmp" / "fresh-spike"
        for fname in SCAFFOLD_FILES:
            assert (folder / fname).is_file(), fname
        assert read_current(ptr) == str(folder.resolve())

    def test_bare_stub_matching_nothing_exits_nonzero(self, tmp_path, ptr):
        proc = run_cli(
            [
                "work",
                "no-such-stub",
                "--root",
                str(tmp_path),
                "--pointer",
                str(ptr),
            ],
            tmp_path,
        )
        assert proc.returncode != 0
        assert "matches no task folder" in proc.stderr


class TestSwitchCLI:
    def test_live_current_noted_on_stderr_then_work_output(self, tmp_path, ptr):
        folder_a = make_task(tmp_path, "tmp/a")
        make_task(tmp_path, "tmp/b", skills_to_invoke=["home-domain"])
        state_ops.work("tmp/a", tmp_path, ptr)
        proc = run_cli(
            ["switch", "tmp/b", "--root", str(tmp_path), "--pointer", str(ptr)],
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        assert "note: updated previous current tmp/a (active)" in proc.stderr
        assert 'Skill(skill: "home-domain")' in proc.stdout
        assert read_block(folder_a)["status"] == "active"
        assert read_current(ptr) == str((tmp_path / "tmp" / "b").resolve())

    def test_nothing_current_behaves_like_work(self, tmp_path, ptr):
        folder = make_task(tmp_path, "tmp/b")
        proc = run_cli(
            ["switch", "tmp/b", "--root", str(tmp_path), "--pointer", str(ptr)],
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        assert "note:" not in proc.stderr
        assert read_current(ptr) == str(folder.resolve())


class TestUpdateCLI:
    def test_defaults_to_current_and_prints_classification(self, tmp_path, ptr):
        folder = make_task(tmp_path, "tmp/a")
        state_ops.work("tmp/a", tmp_path, ptr)
        proc = run_cli(
            ["update", "--priority", "P1", "--pointer", str(ptr)], tmp_path
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "active"
        assert read_block(folder)["priority"] == "P1"

    def test_no_ref_nothing_current_exits_nonzero(self, tmp_path, ptr):
        proc = run_cli(
            ["update", "--priority", "P1", "--root", str(tmp_path), "--pointer", str(ptr)],
            tmp_path,
        )
        assert proc.returncode != 0
        assert "nothing is current" in proc.stderr

    def test_repeatable_flags_replace_stored_lists(self, tmp_path, ptr):
        make_task(tmp_path, "tmp/c")
        make_task(tmp_path, "tmp/d")
        folder = make_task(tmp_path, "tmp/a", depends_on=["tmp/old"])
        proc = run_cli(
            [
                "update",
                "tmp/a",
                "--depends-on",
                "tmp/c",
                "--depends-on",
                "tmp/d",
                "--skill-to-invoke",
                "home-domain",
                "--root",
                str(tmp_path),
                "--pointer",
                str(ptr),
            ],
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        block = read_block(folder)
        assert block["depends_on"] == ["tmp/c", "tmp/d"]
        assert block["skills_to_invoke"] == ["home-domain"]

    def test_warning_case_exits_nonzero_but_edit_persists(self, tmp_path, ptr):
        folder = make_task(tmp_path, "tmp/a")
        proc = run_cli(
            [
                "update",
                "tmp/a",
                "--depends-on",
                "tmp/gone",
                "--root",
                str(tmp_path),
                "--pointer",
                str(ptr),
            ],
            tmp_path,
        )
        assert proc.returncode != 0
        assert proc.stdout.strip() == "active"
        assert "warning:" in proc.stderr
        assert read_block(folder)["depends_on"] == ["tmp/gone"]

    def test_upsert_init_via_cli(self, tmp_path, ptr):
        proc = run_cli(
            [
                "update",
                "tmp/fresh",
                "--priority",
                "P3",
                "--root",
                str(tmp_path),
                "--pointer",
                str(ptr),
            ],
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        folder = tmp_path / "tmp" / "fresh"
        assert folder.is_dir()
        assert read_block(folder)["priority"] == "P3"


class TestCloseReopenCLI:
    def test_close_prints_closed_id(self, tmp_path, ptr):
        folder = make_task(tmp_path, "tmp/a")
        proc = run_cli(
            ["close", "tmp/a", "--root", str(tmp_path), "--pointer", str(ptr)],
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "closed: tmp/a"
        assert read_block(folder)["status"] == "closed"

    def test_close_non_active_exits_nonzero(self, tmp_path, ptr):
        make_task(tmp_path, "tmp/a", status="archived")
        proc = run_cli(
            ["close", "tmp/a", "--root", str(tmp_path), "--pointer", str(ptr)],
            tmp_path,
        )
        assert proc.returncode != 0
        assert "active" in proc.stderr

    def test_reopen_prints_classification(self, tmp_path, ptr):
        folder = make_task(tmp_path, "tmp/a", status="closed")
        proc = run_cli(
            ["reopen", "tmp/a", "--root", str(tmp_path), "--pointer", str(ptr)],
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "active"
        assert read_block(folder)["status"] == "active"

    def test_reopen_missing_folder_exits_nonzero(self, tmp_path, ptr):
        proc = run_cli(
            ["reopen", "tmp/ghost", "--root", str(tmp_path), "--pointer", str(ptr)],
            tmp_path,
        )
        assert proc.returncode != 0
        assert "cannot be reopened" in proc.stderr


class TestCurrentVerb:
    def test_stale_absolute_pointer_cleared_reports_none(self, tmp_path, ptr):
        write_current(ptr, tmp_path / "tmp" / "ghost")  # absolute, no folder
        proc = run_cli(["current", "--pointer", str(ptr)], tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "none"
        assert read_current(ptr) is None

    def test_live_pointer_prints_path_and_summary(self, tmp_path, ptr):
        folder = make_task(tmp_path, "tmp/a", title="The live one")
        write_current(ptr, folder)
        proc = run_cli(["current", "--pointer", str(ptr)], tmp_path)
        assert proc.returncode == 0, proc.stderr
        line = proc.stdout.strip()
        assert line.startswith("tmp/a")
        assert "active" in line
        assert "The live one" in line
        assert read_current(ptr) == str(folder.resolve())
