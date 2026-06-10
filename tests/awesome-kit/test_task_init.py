"""End-to-end tests for the task-system ``init`` verb (spec section 7.1).

Covers scaffolding into tmp and dev/tasks, the always-valid-active invariant,
the uncommitted-dev/tasks reading (init succeeds when the only warnings are
the expected uncommitted-dev/tasks warning), the refuse-to-overwrite
precondition, stub/title derivation, failure cleanup (no partial folder), the
unknown-type/dest errors, and the CLI exit-code/stdout contract.

All fixtures build under pytest tmp_path with project_root pointed there --
the real repo's tmp/ and dev/ are never touched.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from bootstrap_guard import _REEXEC_GUARD_ENV
from task_system import init as init_mod
from task_system.init import InitError, derive_stub_and_title, init_task
from task_system.validate import ValidationResult, validate_ref

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


@pytest.fixture
def git_root(tmp_path):
    """A temp project root that is a real git repo."""
    subprocess.run(
        ["git", "init", "-q", str(tmp_path)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True
    )
    return tmp_path


def run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run task.py in a subprocess, hermetically (same pattern as Step 1).

    The re-exec guard env var is pre-set so the CLI never re-execs into a
    machine-provisioned venv; PYTHONPATH supplies skills_kit_lib from the
    working tree (pyyaml comes from the dev venv running pytest).
    """
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


class TestDerivation:
    def test_stub_arg_used_verbatim_title_derived(self):
        stub, title = derive_stub_and_title("fix-the-closet_run")
        assert stub == "fix-the-closet_run"
        assert title == "Fix the closet run"

    def test_freeform_description_derives_kebab_stub(self):
        stub, title = derive_stub_and_title(
            "Re-terminate the My Office closet cat6a run!"
        )
        assert stub == "re-terminate-the-my-office-closet-cat6a-run"
        assert title == "Re-terminate the My Office closet cat6a run!"

    def test_uppercase_kebab_is_a_description(self):
        # Uppercase chars fail the stub shape -> description path.
        stub, title = derive_stub_and_title("Fix-The-Thing")
        assert stub == "fix-the-thing"
        assert title == "Fix-The-Thing"

    def test_path_shaped_arg_is_sanitized_to_one_segment(self):
        stub, _ = derive_stub_and_title("tmp/evil/../escape")
        assert "/" not in stub
        assert ".." not in stub

    def test_long_description_truncates_to_60(self):
        stub, _ = derive_stub_and_title("word " * 40)
        assert len(stub) <= 60
        assert not stub.endswith("-")

    def test_underivable_description_errors(self):
        with pytest.raises(InitError, match="cannot derive"):
            derive_stub_and_title("!!! ???")

    def test_empty_arg_errors(self):
        with pytest.raises(InitError, match="empty"):
            derive_stub_and_title("   ")


class TestInitTmp:
    def test_creates_all_scaffolding_files(self, tmp_path):
        folder = init_task("spike-ipv6-diag", tmp_path)
        assert folder == (tmp_path / "tmp" / "spike-ipv6-diag").absolute()
        assert folder.is_dir()
        for fname in SCAFFOLD_FILES:
            assert (folder / fname).is_file(), fname

    def test_task_yaml_seeded_per_spec(self, tmp_path):
        folder = init_task("spike-ipv6-diag", tmp_path)
        data = yaml.safe_load((folder / "task.yaml").read_text(encoding="utf-8"))
        assert data == {
            "task": {
                "_schema_version": "1",
                "type": "hand-off",
                "title": "Spike ipv6 diag",
                "status": "active",
            }
        }

    def test_result_validates_active_with_zero_findings(self, tmp_path):
        init_task("spike-ipv6-diag", tmp_path)
        result = validate_ref("tmp/spike-ipv6-diag", tmp_path)
        assert result.classification == "active"
        assert result.clean

    def test_claude_md_carries_the_eight_section_template(self, tmp_path):
        folder = init_task("spike-ipv6-diag", tmp_path)
        text = (folder / "CLAUDE.md").read_text(encoding="utf-8")
        assert text.startswith("# Project Overview")
        for section in (
            "## Where we are today",
            "### Environment",
            "## Where we want to get to",
            "## Immediate Priorities",
            "## Project vocabulary",
            "## Protocols",
            "## Behaviors",
            "## Relevant files",
        ):
            assert section in text, section
        assert text.isascii()

    def test_freeform_description_title_carries_description(self, tmp_path):
        desc = "Re-terminate the My Office closet cat6a run!"
        folder = init_task(desc, tmp_path)
        assert folder.name == "re-terminate-the-my-office-closet-cat6a-run"
        data = yaml.safe_load((folder / "task.yaml").read_text(encoding="utf-8"))
        assert data["task"]["title"] == desc
        assert validate_ref(f"tmp/{folder.name}", tmp_path).clean


class TestInitDevTasks:
    def test_succeeds_with_only_the_uncommitted_warning(self, git_root):
        folder = init_task("durable-work", git_root, dest="dev/tasks")
        assert folder == (git_root / "dev" / "tasks" / "durable-work").absolute()
        for fname in SCAFFOLD_FILES:
            assert (folder / fname).is_file(), fname
        # Expected warnings: exactly the uncommitted-dev/tasks warning --
        # unavoidable at creation time (the user owns the commit).
        result = validate_ref("dev/tasks/durable-work", git_root)
        assert result.classification == "active"
        assert result.errors == []
        assert len(result.warnings) == 1
        assert "uncommitted dev/tasks folder" in result.warnings[0]


class TestRefuseExisting:
    def test_existing_folder_errors_and_is_untouched(self, tmp_path):
        folder = init_task("taken", tmp_path)
        marker = folder / "marker.txt"
        marker.write_text("precious\n", encoding="utf-8")
        with pytest.raises(InitError, match="already exists .* use update"):
            init_task("taken", tmp_path)
        assert marker.read_text(encoding="utf-8") == "precious\n"
        for fname in SCAFFOLD_FILES:
            assert (folder / fname).is_file(), fname

    def test_existing_non_dir_path_also_refused(self, tmp_path):
        (tmp_path / "tmp").mkdir()
        (tmp_path / "tmp" / "taken").write_text("a file\n", encoding="utf-8")
        with pytest.raises(InitError, match="already exists"):
            init_task("taken", tmp_path)


class TestFailureCleanup:
    def test_dirty_validation_fails_and_removes_folder(self, tmp_path, monkeypatch):
        def fake_validate(ref, project_root, **kw):
            return ValidationResult(
                ref=ref, classification="invalid", errors=["boom"]
            )

        monkeypatch.setattr(init_mod, "validate_ref", fake_validate)
        with pytest.raises(InitError, match="does not validate clean"):
            init_task("doomed", tmp_path)
        assert not (tmp_path / "tmp" / "doomed").exists()

    def test_unexpected_warning_fails_and_removes_folder(self, tmp_path, monkeypatch):
        # A warning that is NOT the expected uncommitted-dev/tasks warning
        # fails init even with zero errors (the chosen reading).
        def fake_validate(ref, project_root, **kw):
            return ValidationResult(
                ref=ref,
                classification="active",
                warnings=["dangling depends_on entry 'tmp/x': gone"],
            )

        monkeypatch.setattr(init_mod, "validate_ref", fake_validate)
        with pytest.raises(InitError, match="dangling depends_on"):
            init_task("doomed", tmp_path)
        assert not (tmp_path / "tmp" / "doomed").exists()

    def test_scaffold_exception_propagates_and_removes_folder(
        self, tmp_path, monkeypatch
    ):
        def exploding_validate(ref, project_root, **kw):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(init_mod, "validate_ref", exploding_validate)
        with pytest.raises(RuntimeError, match="disk on fire"):
            init_task("doomed", tmp_path)
        assert not (tmp_path / "tmp" / "doomed").exists()


class TestBadArguments:
    def test_unknown_type_errors_before_creating_anything(self, tmp_path):
        with pytest.raises(InitError, match="unknown type"):
            init_task("foo", tmp_path, task_type="sprint")
        assert not (tmp_path / "tmp" / "foo").exists()

    def test_unknown_dest_errors(self, tmp_path):
        with pytest.raises(InitError, match="unknown dest"):
            init_task("foo", tmp_path, dest="docs")


class TestCLI:
    def test_init_into_tmp_exits_zero_prints_path(self, tmp_path):
        proc = run_cli(["init", "spike-x", "--root", str(tmp_path)], tmp_path)
        assert proc.returncode == 0, proc.stderr
        printed = Path(proc.stdout.strip())
        assert printed == (tmp_path / "tmp" / "spike-x").resolve()
        assert printed.is_dir()
        for fname in SCAFFOLD_FILES:
            assert (printed / fname).is_file(), fname
        # The created task validates clean via the CLI too.
        check = run_cli(["validate", "tmp/spike-x", "--root", str(tmp_path)], tmp_path)
        assert check.returncode == 0, check.stderr
        assert check.stdout.strip() == "active"

    def test_init_into_dev_tasks_exits_zero(self, git_root):
        proc = run_cli(
            ["init", "durable", "--dest", "dev/tasks", "--root", str(git_root)],
            git_root,
        )
        assert proc.returncode == 0, proc.stderr
        printed = Path(proc.stdout.strip())
        assert printed == (git_root / "dev" / "tasks" / "durable").resolve()
        assert printed.is_dir()

    def test_existing_folder_exits_nonzero_folder_untouched(self, tmp_path):
        first = run_cli(["init", "taken", "--root", str(tmp_path)], tmp_path)
        assert first.returncode == 0, first.stderr
        folder = tmp_path / "tmp" / "taken"
        before = (folder / "task.yaml").read_text(encoding="utf-8")
        second = run_cli(["init", "taken", "--root", str(tmp_path)], tmp_path)
        assert second.returncode != 0
        assert second.stdout.strip() == ""
        assert "already exists" in second.stderr
        assert "update" in second.stderr
        assert (folder / "task.yaml").read_text(encoding="utf-8") == before

    def test_unknown_type_exits_nonzero(self, tmp_path):
        proc = run_cli(
            ["init", "foo", "--type", "sprint", "--root", str(tmp_path)],
            tmp_path,
        )
        assert proc.returncode != 0
        assert "unknown type" in proc.stderr
        assert not (tmp_path / "tmp" / "foo").exists()

    def test_freeform_description_via_cli(self, tmp_path):
        proc = run_cli(
            ["init", "Fix the frobnicator: phase 2", "--root", str(tmp_path)],
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        printed = Path(proc.stdout.strip())
        assert printed.name == "fix-the-frobnicator-phase-2"
        data = yaml.safe_load(
            (printed / "task.yaml").read_text(encoding="utf-8")
        )
        assert data["task"]["title"] == "Fix the frobnicator: phase 2"
