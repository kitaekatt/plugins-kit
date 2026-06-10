"""End-to-end tests for the task-system ``validate`` verb (spec section 9).

Covers every error condition, every warning condition, every classification
outcome (including the absent-folder tri-state), the remote short-circuit,
stub resolution, and the CLI exit-code contract.

All fixtures build task folders under pytest tmp_path with project_root
pointed there -- the real repo's tmp/ and dev/ are never touched. Host
detection is injected (local_host=LOCAL) so nothing depends on the machine's
hostname.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from bootstrap_guard import _REEXEC_GUARD_ENV
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

LOCAL = "testhost"
OTHER = "otherhost"


def make_task(
    project_root: Path,
    rel: str,
    *,
    task_block: dict | None = None,
    scaffolding: bool = True,
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
        if task_block is not None:
            block = task_block
        (folder / "task.yaml").write_text(
            yaml.safe_dump({"task": block}), encoding="utf-8"
        )
    if scaffolding:
        for fname in ("CLAUDE.md", "plan.md", "log.md"):
            (folder / fname).write_text("placeholder\n", encoding="utf-8")
    return folder


def v(ref: str, project_root: Path, **kw):
    kw.setdefault("local_host", LOCAL)
    return validate_ref(ref, project_root, **kw)


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


def _commit_all(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "snapshot",
        ],
        check=True,
    )


class TestClassificationOutcomes:
    def test_active_tmp_task_clean(self, tmp_path):
        make_task(tmp_path, "tmp/foo")
        result = v("tmp/foo", tmp_path)
        assert result.classification == "active"
        assert result.clean
        assert result.canonical == "tmp/foo"

    def test_stored_blocked_status(self, tmp_path):
        make_task(tmp_path, "tmp/foo", status="blocked")
        assert v("tmp/foo", tmp_path).classification == "blocked"

    def test_stored_closed_status(self, tmp_path):
        make_task(tmp_path, "tmp/foo", status="closed")
        assert v("tmp/foo", tmp_path).classification == "closed"

    def test_tmp_archived_folder_kept_no_warning(self, tmp_path):
        # tmp archive keeps the folder + marks it; only NON-tmp archived
        # folders warn.
        make_task(tmp_path, "tmp/foo", status="archived")
        result = v("tmp/foo", tmp_path)
        assert result.classification == "archived"
        assert result.clean

    def test_nonempty_blocked_by_reads_as_blocked(self, tmp_path):
        # blocked_by entry is a non-tmp absent path: reads as archived per the
        # tri-state, so it is NOT dangling -- the result stays warning-free.
        make_task(
            tmp_path, "tmp/foo", status="active", blocked_by=["dev/tasks/gone"]
        )
        result = v("tmp/foo", tmp_path)
        assert result.classification == "blocked"
        assert result.clean


class TestTriState:
    def test_nontmp_absent_is_archived_no_findings(self, tmp_path):
        result = v("dev/tasks/long-done", tmp_path)
        assert result.classification == "archived"
        assert result.clean

    def test_tmp_absent_no_host_is_orphaned_with_warning(self, tmp_path):
        result = v("tmp/vanished", tmp_path)
        assert result.classification == "orphaned"
        assert result.errors == []
        assert len(result.warnings) == 1
        assert "orphaned tmp reference" in result.warnings[0]

    def test_tmp_absent_local_host_is_orphaned(self, tmp_path):
        result = v("tmp/vanished", tmp_path, ref_host=LOCAL)
        assert result.classification == "orphaned"
        assert any("orphaned tmp reference" in w for w in result.warnings)

    def test_tmp_other_host_is_remote(self, tmp_path):
        result = v("tmp/elsewhere", tmp_path, ref_host=OTHER)
        assert result.classification == "remote"
        assert result.clean


class TestRemoteShortCircuit:
    def test_garbage_task_yaml_does_not_matter(self, tmp_path):
        make_task(
            tmp_path,
            "tmp/spike",
            yaml_text="task: [unclosed\n",
            scaffolding=False,
        )
        result = v("tmp/spike", tmp_path, ref_host=OTHER)
        assert result.classification == "remote"
        assert result.clean

    def test_absent_folder_still_remote(self, tmp_path):
        result = v("tmp/spike", tmp_path, ref_host=OTHER)
        assert result.classification == "remote"
        assert result.clean

    def test_host_on_nontmp_path_is_ignored(self, tmp_path):
        # host is only meaningful for tmp paths (spec 2.3).
        make_task(tmp_path, "tmp/foo")
        # dev/tasks ref with mismatching host validates normally (tri-state).
        result = v("dev/tasks/gone", tmp_path, ref_host=OTHER)
        assert result.classification == "archived"


class TestErrors:
    def _assert_invalid(self, result, fragment):
        assert result.classification == "invalid"
        assert any(fragment in e for e in result.errors), result.errors

    def test_missing_task_yaml(self, tmp_path):
        folder = tmp_path / "tmp" / "foo"
        folder.mkdir(parents=True)
        (folder / "CLAUDE.md").write_text("x\n", encoding="utf-8")
        self._assert_invalid(v("tmp/foo", tmp_path), "missing task.yaml")

    def test_unparseable_yaml(self, tmp_path):
        make_task(tmp_path, "tmp/foo", yaml_text="task: [unclosed\n")
        self._assert_invalid(v("tmp/foo", tmp_path), "unparseable YAML")

    def test_yaml_root_not_mapping(self, tmp_path):
        make_task(tmp_path, "tmp/foo", yaml_text="- a\n- b\n")
        self._assert_invalid(v("tmp/foo", tmp_path), "schema violation")

    def test_missing_required_field(self, tmp_path):
        block = {"_schema_version": "1", "type": "hand-off", "status": "active"}
        make_task(tmp_path, "tmp/foo", task_block=block)
        self._assert_invalid(v("tmp/foo", tmp_path), "task.title")

    def test_wrong_type_field(self, tmp_path):
        make_task(tmp_path, "tmp/foo", title=42)
        self._assert_invalid(v("tmp/foo", tmp_path), "task.title")

    def test_status_outside_vocabulary(self, tmp_path):
        make_task(tmp_path, "tmp/foo", status="paused")
        self._assert_invalid(v("tmp/foo", tmp_path), "state vocabulary")

    def test_priority_outside_pattern(self, tmp_path):
        make_task(tmp_path, "tmp/foo", priority="P9")
        self._assert_invalid(v("tmp/foo", tmp_path), "task.priority")

    def test_unknown_schema_version(self, tmp_path):
        make_task(tmp_path, "tmp/foo", _schema_version="99")
        self._assert_invalid(v("tmp/foo", tmp_path), "unknown version")

    def test_missing_scaffolding_file(self, tmp_path):
        folder = make_task(tmp_path, "tmp/foo")
        (folder / "plan.md").unlink()
        self._assert_invalid(
            v("tmp/foo", tmp_path), "missing scaffolding file: plan.md"
        )

    def test_unknown_type(self, tmp_path):
        make_task(tmp_path, "tmp/foo", type="sprint")
        self._assert_invalid(v("tmp/foo", tmp_path), "unknown type")

    def test_ref_outside_known_roots(self, tmp_path):
        self._assert_invalid(
            v("docs/foo", tmp_path), "not a known task location"
        )

    def test_ref_escaping_project_root(self, tmp_path):
        self._assert_invalid(
            v("../foo/bar", tmp_path), "escapes the project root"
        )


class TestWarnings:
    def test_nontmp_archived_folder_warns(self, git_root):
        make_task(git_root, "dev/tasks/done", status="archived")
        _commit_all(git_root)
        result = v("dev/tasks/done", git_root)
        assert result.classification == "archived"
        assert result.errors == []
        assert len(result.warnings) == 1
        assert "status: archived" in result.warnings[0]

    def test_uncommitted_dev_tasks_folder_warns(self, git_root):
        make_task(git_root, "dev/tasks/wip")
        result = v("dev/tasks/wip", git_root)
        assert result.classification == "active"
        assert any("uncommitted dev/tasks folder" in w for w in result.warnings)

    def test_committed_dev_tasks_folder_clean(self, git_root):
        make_task(git_root, "dev/tasks/done")
        _commit_all(git_root)
        result = v("dev/tasks/done", git_root)
        assert result.classification == "active"
        assert result.clean

    def test_modified_committed_folder_warns(self, git_root):
        folder = make_task(git_root, "dev/tasks/done")
        _commit_all(git_root)
        (folder / "plan.md").write_text("edited\n", encoding="utf-8")
        result = v("dev/tasks/done", git_root)
        assert any("uncommitted dev/tasks folder" in w for w in result.warnings)

    def test_dev_tasks_outside_any_git_repo_counts_as_uncommitted(self, tmp_path):
        # Documented minimal reading: no enclosing repo means no git record,
        # which is the unsaved-durable-work condition the warning exists for.
        make_task(tmp_path, "dev/tasks/norepo")
        result = v("dev/tasks/norepo", tmp_path)
        assert any("uncommitted dev/tasks folder" in w for w in result.warnings)

    def test_tmp_task_never_gets_git_warnings(self, tmp_path):
        make_task(tmp_path, "tmp/foo")
        assert v("tmp/foo", tmp_path).clean

    def test_dangling_orphaned_tmp_dep_warns(self, tmp_path):
        make_task(tmp_path, "tmp/foo", depends_on=["tmp/never-there"])
        result = v("tmp/foo", tmp_path)
        assert result.classification == "active"
        assert any(
            "dangling depends_on entry 'tmp/never-there'" in w
            for w in result.warnings
        )

    def test_dangling_malformed_dep_warns(self, tmp_path):
        make_task(tmp_path, "tmp/foo", blocked_by=["elsewhere/thing"])
        result = v("tmp/foo", tmp_path)
        assert any(
            "dangling blocked_by entry" in w for w in result.warnings
        )

    def test_dangling_nonstring_dep_warns(self, tmp_path):
        make_task(tmp_path, "tmp/foo", depends_on=[123])
        result = v("tmp/foo", tmp_path)
        assert any("not a path string" in w for w in result.warnings)

    def test_nontmp_absent_dep_is_not_dangling(self, tmp_path):
        # Tri-state: a non-tmp absent path reads as archived.
        make_task(tmp_path, "tmp/foo", depends_on=["dev/tasks/shipped"])
        assert v("tmp/foo", tmp_path).clean

    def test_extant_deps_are_not_dangling(self, tmp_path):
        make_task(tmp_path, "tmp/dep")
        make_task(tmp_path, "dev/tasks/dep2", scaffolding=True)
        make_task(
            tmp_path, "tmp/foo", depends_on=["tmp/dep", "dev/tasks/dep2"]
        )
        result = v("tmp/foo", tmp_path)
        assert not any("dangling" in w for w in result.warnings)


class TestStubResolution:
    def test_unique_stub_resolves(self, tmp_path):
        make_task(tmp_path, "tmp/uniq")
        result = v("uniq", tmp_path)
        assert result.classification == "active"
        assert result.canonical == "tmp/uniq"

    def test_unique_dev_tasks_stub_resolves(self, git_root):
        make_task(git_root, "dev/tasks/durable")
        _commit_all(git_root)
        result = v("durable", git_root)
        assert result.classification == "active"
        assert result.canonical == "dev/tasks/durable"

    def test_ambiguous_stub_errors_with_candidates(self, tmp_path):
        make_task(tmp_path, "tmp/dup")
        make_task(tmp_path, "dev/tasks/dup")
        result = v("dup", tmp_path)
        assert result.classification == "invalid"
        assert len(result.errors) == 1
        assert "ambiguous stub" in result.errors[0]
        assert "tmp/dup" in result.errors[0]
        assert "dev/tasks/dup" in result.errors[0]

    def test_unmatched_stub_errors(self, tmp_path):
        result = v("nothing-here", tmp_path)
        assert result.classification == "invalid"
        assert any("matches no task folder" in e for e in result.errors)


class TestCanonicalization:
    def test_dot_segments_resolve(self, tmp_path):
        make_task(tmp_path, "tmp/foo")
        result = v("./tmp/../tmp/foo", tmp_path)
        assert result.classification == "active"
        assert result.canonical == "tmp/foo"

    def test_absolute_path_under_root_resolves(self, tmp_path):
        make_task(tmp_path, "tmp/foo")
        result = v(str(tmp_path / "tmp" / "foo"), tmp_path)
        assert result.canonical == "tmp/foo"

    def test_absolute_path_outside_root_errors(self, tmp_path):
        result = v("/definitely/elsewhere/foo", tmp_path)
        assert result.classification == "invalid"


def run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run task.py in a subprocess, hermetically.

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


class TestCLI:
    def test_clean_task_exits_zero(self, tmp_path):
        make_task(tmp_path, "tmp/foo")
        proc = run_cli(["validate", "tmp/foo", "--root", str(tmp_path)], tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "active"
        assert proc.stderr.strip() == ""

    def test_root_defaults_to_cwd(self, tmp_path):
        make_task(tmp_path, "tmp/foo")
        proc = run_cli(["validate", "tmp/foo"], tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "active"

    def test_error_exits_nonzero_findings_on_stderr(self, tmp_path):
        folder = make_task(tmp_path, "tmp/foo")
        (folder / "plan.md").unlink()
        proc = run_cli(["validate", "tmp/foo", "--root", str(tmp_path)], tmp_path)
        assert proc.returncode != 0
        assert proc.stdout.strip() == "invalid"
        assert "error: missing scaffolding file: plan.md" in proc.stderr

    def test_warning_only_exits_nonzero(self, tmp_path):
        # Orphaned tmp reference: no errors, one warning -- still blocks.
        proc = run_cli(
            ["validate", "tmp/vanished", "--root", str(tmp_path)], tmp_path
        )
        assert proc.returncode != 0
        assert proc.stdout.strip() == "orphaned"
        assert "warning: orphaned tmp reference" in proc.stderr
        assert "error:" not in proc.stderr
