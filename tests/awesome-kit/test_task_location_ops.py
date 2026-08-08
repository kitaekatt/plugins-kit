"""End-to-end tests for the task-system location ops (Step 5).

Covers the spec section 7.1/7.2/7.4 verbs ``archive`` / ``delete`` /
``move``: archive's closure policy (tmp -> status archived + folder parked
at tmp/archived-tasks/<stub>; non-tmp in git -> final state committed,
folder deleted, removal committed; non-tmp outside git -> final state
recorded, folder kept, VCS submission left to the agent -- version control
is the record, git is just the automated case), the status preconditions
(closed -> reopen-first error), delete's git-dirty guard (delete never
auto-commits; accepts active or archived) plus unconditional folder
removal, reopen's restore of a parked tmp folder,
move's relocation + span-precise reference rewrite across the project
document set (byte-level preservation outside the rewritten path values,
prose mentions and other-task refs untouched).

All fixtures build under pytest tmp_path -- the real repo's tmp/, dev/, and
~/.claude are never touched.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from bootstrap_guard import _REEXEC_GUARD_ENV
from task_system import location_ops
from task_system.state_ops import StateOpError
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
    """Run task.py in a subprocess, hermetically (same pattern as Steps 2-4)."""
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


def snapshot(folder: Path) -> dict[str, str]:
    """Relative path -> content for every file under a folder."""
    return {
        str(p.relative_to(folder)): p.read_text(encoding="utf-8")
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


@pytest.fixture
def git_root(tmp_path) -> Path:
    """A temp project root that is a real git repo (same pattern as the
    validate tests)."""
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
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "commit.gpgsign", "false"],
        check=True,
    )
    return tmp_path


def _log_subjects(root: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(root), "log", "--format=%s"],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.splitlines()


def _show_file(root: Path, rev: str, rel: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), "show", f"{rev}:{rel}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


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


class TestArchiveLib:
    def test_tmp_active_marks_archived_parks_folder(self, tmp_path):
        folder = make_task(tmp_path, "tmp/a")
        result = location_ops.archive_task("tmp/a", tmp_path)
        assert result.canonical == "tmp/a"
        assert result.folder_removed is False
        assert result.archived_to == "tmp/archived-tasks/a"
        assert not folder.exists()
        parked = tmp_path / "tmp" / "archived-tasks" / "a"
        assert read_block(parked)["status"] == "archived"
        for fname in SCAFFOLD_FILES:
            assert (parked / fname).is_file(), fname

    def test_tmp_archived_result_validates_clean(self, tmp_path):
        # Spec section 9 interplay: a tmp task parked at
        # tmp/archived-tasks/<stub> (archive's own output) is a PROPER
        # archive -- the ref reads as archived, not orphaned, no findings.
        make_task(tmp_path, "tmp/a")
        location_ops.archive_task("tmp/a", tmp_path)
        result = validate_ref("tmp/a", tmp_path)
        assert result.classification == "archived"
        assert result.clean

    def test_tmp_occupied_parking_spot_refuses_untouched(self, tmp_path):
        folder = make_task(tmp_path, "tmp/a")
        parked = tmp_path / "tmp" / "archived-tasks" / "a"
        parked.mkdir(parents=True)
        before = snapshot(folder)
        with pytest.raises(StateOpError, match="parking spot"):
            location_ops.archive_task("tmp/a", tmp_path)
        assert snapshot(folder) == before
        assert read_block(folder)["status"] == "active"

    def test_nontmp_committed_commits_final_state_and_removal(
        self, git_root
    ):
        folder = make_task(git_root, "dev/tasks/durable")
        _commit_all(git_root)
        result = location_ops.archive_task("dev/tasks/durable", git_root)
        assert result.canonical == "dev/tasks/durable"
        assert result.folder_removed is True
        assert result.archived_to is None
        assert not folder.exists()
        subjects = _log_subjects(git_root)
        assert subjects[0] == (
            "task archive: dev/tasks/durable (remove folder; version "
            "control is the record)"
        )
        assert subjects[1] == "task archive: dev/tasks/durable (final state)"
        # The final-state commit holds the archived record + the log entry.
        final_yaml = _show_file(
            git_root, "HEAD~1", "dev/tasks/durable/task.yaml"
        )
        assert "archived" in final_yaml
        final_log = _show_file(git_root, "HEAD~1", "dev/tasks/durable/log.md")
        assert "archive: final state committed" in final_log
        # Nothing uncommitted left behind for the task folder.
        status = subprocess.run(
            ["git", "-C", str(git_root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert "durable" not in status

    def test_nontmp_uncommitted_is_committed_then_removed(self, git_root):
        # The 2026-07-22 revision: archive no longer refuses uncommitted
        # durable work -- it records + submits the final state itself, then
        # removes the folder.
        folder = make_task(git_root, "dev/tasks/durable")  # never committed
        result = location_ops.archive_task("dev/tasks/durable", git_root)
        assert result.folder_removed is True
        assert not folder.exists()
        final_yaml = _show_file(
            git_root, "HEAD~1", "dev/tasks/durable/task.yaml"
        )
        assert "archived" in final_yaml

    def test_nontmp_commit_scoped_to_folder_only(self, git_root):
        # Pathspec-limited commits: pre-staged UNRELATED index content must
        # not be swept into the archive commits.
        make_task(git_root, "dev/tasks/durable")
        _commit_all(git_root)
        unrelated = git_root / "unrelated.txt"
        unrelated.write_text("wip\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(git_root), "add", str(unrelated)], check=True
        )
        location_ops.archive_task("dev/tasks/durable", git_root)
        status = subprocess.run(
            ["git", "-C", str(git_root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert "unrelated.txt" in status  # still pending, not committed
        for rev in ("HEAD", "HEAD~1"):
            files = subprocess.run(
                ["git", "-C", str(git_root), "show", "--name-only",
                 "--format=", rev],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            assert "unrelated.txt" not in files

    def test_nontmp_not_in_git_repo_records_and_keeps_folder(
        self, tmp_path
    ):
        # No git repo: the task system has no dependency on git -- the
        # workspace may use another VCS (e.g. Perforce). No git command
        # runs; the final state is recorded and the folder KEPT for the
        # agent to submit with the workspace's VCS, then delete.
        folder = make_task(tmp_path, "dev/tasks/durable")
        result = location_ops.archive_task("dev/tasks/durable", tmp_path)
        assert result.folder_removed is False
        assert result.vcs_pending is True
        assert folder.is_dir()
        assert read_block(folder)["status"] == "archived"
        log = (folder / "log.md").read_text(encoding="utf-8")
        assert "submit to version control" in log

    def test_nontmp_vcs_pending_then_delete_finishes(self, tmp_path):
        # The second half of the non-git flow: after the agent submits with
        # the workspace's VCS, delete removes the archived folder (delete
        # accepts stored status archived, and outside a git repo no git
        # guard applies).
        folder = make_task(tmp_path, "dev/tasks/durable")
        location_ops.archive_task("dev/tasks/durable", tmp_path)
        canonical = location_ops.delete_task(
            "dev/tasks/durable", tmp_path
        )
        assert canonical == "dev/tasks/durable"
        assert not folder.exists()

    def test_closed_task_errors_reopen_first(self, tmp_path):
        folder = make_task(tmp_path, "tmp/a", status="closed")
        with pytest.raises(StateOpError, match="reopen"):
            location_ops.archive_task("tmp/a", tmp_path)
        assert read_block(folder)["status"] == "closed"

    def test_git_ignored_folder_records_and_keeps_folder(self, git_root):
        # A project may deliberately gitignore its task root, keeping task
        # folders as local scratch. Git is present and CAN see the folder,
        # but will never carry it -- so the commits cannot succeed and the
        # "version control is the record, therefore the folder may go"
        # justification does not hold. Before the fix this crashed at
        # `git add -A` AFTER the final-state writes had landed.
        (git_root / ".gitignore").write_text("dev/\n", encoding="utf-8")
        _commit_all(git_root)
        folder = make_task(git_root, "dev/tasks/scratch")
        result = location_ops.archive_task("dev/tasks/scratch", git_root)
        assert result.vcs_ignored is True
        assert result.vcs_pending is False
        assert result.folder_removed is False
        assert folder.is_dir()
        assert read_block(folder)["status"] == "archived"
        log = (folder / "log.md").read_text(encoding="utf-8")
        assert "git-ignored" in log
        # The log must NOT claim a commit or a removal that did not happen.
        assert "final state committed" not in log
        # No archive commit was invented for an unreachable folder.
        assert not any("task archive" in s for s in _log_subjects(git_root))

    def test_git_ignored_delete_finishes(self, git_root):
        # The second half: delete removes an ignored archived folder. Its
        # commit-first guard cannot apply (git will never hold this content),
        # so it must not block -- the CLI disposition is what warns that the
        # removal is permanent.
        (git_root / ".gitignore").write_text("dev/\n", encoding="utf-8")
        _commit_all(git_root)
        folder = make_task(git_root, "dev/tasks/scratch")
        location_ops.archive_task("dev/tasks/scratch", git_root)
        location_ops.delete_task("dev/tasks/scratch", git_root)
        assert not folder.exists()

    def test_force_added_file_is_tracked_not_ignored(self, git_root):
        # `check-ignore` alone would call this ignored, but a force-added
        # file inside an ignored directory IS tracked and git IS the record
        # for it -- so the normal commit-and-remove path must still run.
        (git_root / ".gitignore").write_text("dev/\n", encoding="utf-8")
        folder = make_task(git_root, "dev/tasks/durable")
        subprocess.run(
            ["git", "-C", str(git_root), "add", "-f", "--", str(folder)],
            check=True,
            capture_output=True,
        )
        _commit_all(git_root)
        result = location_ops.archive_task("dev/tasks/durable", git_root)
        assert result.vcs_ignored is False
        assert result.folder_removed is True
        assert not folder.exists()

    def test_failed_commit_rolls_back_the_final_state_writes(
        self, git_root, monkeypatch
    ):
        # The final state must be ON DISK to be committed, so the writes
        # precede the commit. When the commit then fails, those writes are a
        # lie the failure leaves behind -- a log line asserting the folder was
        # committed and removed, in a folder that is still present. A failed
        # archive must be a no-op.
        folder = make_task(git_root, "dev/tasks/durable")
        _commit_all(git_root)
        before = snapshot(folder)

        def boom(*args, **kwargs):
            raise StateOpError("git failed during archive (simulated)")

        monkeypatch.setattr(location_ops, "_git_commit_folder", boom)
        with pytest.raises(StateOpError, match="simulated"):
            location_ops.archive_task("dev/tasks/durable", git_root)
        assert folder.is_dir()
        assert snapshot(folder) == before
        assert read_block(folder)["status"] == "active"

    def test_other_non_active_status_errors(self, tmp_path):
        make_task(tmp_path, "tmp/a", status="blocked")
        with pytest.raises(StateOpError, match="active"):
            location_ops.archive_task("tmp/a", tmp_path)

    def test_missing_folder_errors(self, tmp_path):
        with pytest.raises(StateOpError, match="no task folder"):
            location_ops.archive_task("tmp/ghost", tmp_path)


class TestDeleteLib:
    def test_tmp_active_folder_gone(self, tmp_path):
        folder = make_task(tmp_path, "tmp/a")
        canonical = location_ops.delete_task("tmp/a", tmp_path)
        assert canonical == "tmp/a"
        assert not folder.exists()

    def test_nontmp_committed_folder_gone(self, git_root):
        folder = make_task(git_root, "dev/tasks/durable")
        _commit_all(git_root)
        location_ops.delete_task("dev/tasks/durable", git_root)
        assert not folder.exists()

    def test_closed_task_errors_reopen_first(self, tmp_path):
        # Pins the documented Step 5 reading: delete inherits archive's
        # status-active precondition (delete = archive + unconditional
        # removal, spec 7.1).
        folder = make_task(tmp_path, "tmp/a", status="closed")
        with pytest.raises(StateOpError, match="reopen"):
            location_ops.delete_task("tmp/a", tmp_path)
        assert folder.is_dir()

    def test_nontmp_uncommitted_refuses_untouched(self, git_root):
        # Pins the documented Step 5 reading: delete inherits archive's
        # uncommitted guard too -- an uncommitted dev/tasks folder refuses.
        folder = make_task(git_root, "dev/tasks/durable")
        before = snapshot(folder)
        with pytest.raises(StateOpError, match="commit first"):
            location_ops.delete_task("dev/tasks/durable", git_root)
        assert folder.is_dir()
        assert snapshot(folder) == before

    def test_missing_folder_errors(self, tmp_path):
        with pytest.raises(StateOpError, match="no task folder"):
            location_ops.delete_task("tmp/ghost", tmp_path)


class TestParkedTmpArchive:
    """The tmp/archived-tasks parking lifecycle around archive (2026-07-22)."""

    def test_reopen_restores_parked_folder(self, tmp_path):
        from task_system import state_ops

        make_task(tmp_path, "tmp/a")
        location_ops.archive_task("tmp/a", tmp_path)
        result = state_ops.reopen("tmp/a", tmp_path)
        folder = tmp_path / "tmp" / "a"
        assert folder.is_dir()
        assert not (tmp_path / "tmp" / "archived-tasks" / "a").exists()
        assert read_block(folder)["status"] == "active"
        assert result.validation.classification == "active"

    def test_reopen_still_errors_when_nothing_parked(self, tmp_path):
        from task_system import state_ops

        with pytest.raises(StateOpError, match="cannot be reopened"):
            state_ops.reopen("tmp/ghost", tmp_path)

    def test_discovery_skips_parked_folders(self, tmp_path):
        from task_system.discovery import discover

        make_task(tmp_path, "tmp/live")
        make_task(tmp_path, "tmp/done")
        location_ops.archive_task("tmp/done", tmp_path)
        notes: list[str] = []
        records = discover("project", tmp_path, notes=notes)
        assert {r.id for r in records} == {"tmp/live"}
        assert notes == []  # the parked folder is skipped silently

    def test_referenced_parked_task_lists_as_archived(self, tmp_path):
        # A surviving task_list ref to the archived task resolves through
        # the parking directory: archived, not orphaned.
        from task_system.discovery import discover

        make_task(tmp_path, "tmp/done")
        write_doc(
            tmp_path / "tmp" / "notes.md", fenced_task_list([{"path": "tmp/done"}])
        )
        location_ops.archive_task("tmp/done", tmp_path)
        records = discover("project", tmp_path)
        by_id = {r.id: r for r in records}
        assert by_id["tmp/done"].classification == "archived"

    def test_reserved_ref_errors(self, tmp_path):
        with pytest.raises(StateOpError, match="reserved"):
            location_ops.archive_task("tmp/archived-tasks", tmp_path)

    def test_init_reserved_stub_errors(self, tmp_path):
        from task_system.init import InitError, init_task

        with pytest.raises(InitError, match="reserved"):
            init_task("archived-tasks", tmp_path, dest="tmp")


def fenced_task_list(refs: list[dict]) -> str:
    """A markdown fragment embedding a task_list typed-unit block."""
    return (
        "```yaml\n" + yaml.safe_dump({"task_list": {"refs": refs}}) + "```\n"
    )


def write_doc(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Doc\n\n" + body, encoding="utf-8")
    return path


class TestMoveLib:
    def test_promote_relocates_and_rewrites_multiple_docs(self, git_root):
        old_folder = make_task(git_root, "tmp/spike-x")
        doc_a = write_doc(
            git_root / "notes.md", fenced_task_list([{"path": "tmp/spike-x"}])
        )
        doc_b = write_doc(
            git_root / "skills" / "foo" / "SKILL.md",
            fenced_task_list([{"path": "tmp/spike-x"}, {"path": "tmp/spike-x"}]),
        )
        _commit_all(git_root)
        result = location_ops.move_task("tmp/spike-x", "dev/tasks", git_root)
        assert result.old_canonical == "tmp/spike-x"
        assert result.new_canonical == "dev/tasks/spike-x"
        new_folder = git_root / "dev" / "tasks" / "spike-x"
        assert result.folder == new_folder.resolve()
        assert not old_folder.exists()
        for fname in SCAFFOLD_FILES:
            assert (new_folder / fname).is_file(), fname
        assert set(result.rewritten_docs) == {doc_a, doc_b}
        assert "dev/tasks/spike-x" in doc_a.read_text(encoding="utf-8")
        assert "tmp/spike-x" not in doc_a.read_text(encoding="utf-8")
        assert doc_b.read_text(encoding="utf-8").count("dev/tasks/spike-x") == 2

    def test_byte_level_preservation_and_prose_untouched(self, tmp_path):
        # The rewrite is span-precise: only the matching task_list path
        # values change. Prose mentions of the old path, comments, flow
        # style, indentation, and refs to a DIFFERENT task are preserved
        # byte-for-byte. A quoted matching scalar is replaced (quotes and
        # all) with the bare canonical path.
        make_task(tmp_path, "tmp/spike-x")
        original = (
            "# Notes\n"
            "\n"
            "Prose mention of tmp/spike-x stays as prose.\n"
            "\n"
            "```yaml\n"
            "# leading comment\n"
            "task_list:\n"
            "  refs:\n"
            "    - { path: tmp/spike-x }  # inline comment\n"
            '    - path: "tmp/spike-x"\n'
            "    - path: dev/tasks/other-task\n"
            "```\n"
            "\n"
            "Trailing prose tmp/spike-x.\n"
        )
        doc = tmp_path / "tmp" / "notes.md"
        doc.write_text(original, encoding="utf-8")
        location_ops.move_task("tmp/spike-x", "dev/tasks", tmp_path)
        expected = (
            "# Notes\n"
            "\n"
            "Prose mention of tmp/spike-x stays as prose.\n"
            "\n"
            "```yaml\n"
            "# leading comment\n"
            "task_list:\n"
            "  refs:\n"
            "    - { path: dev/tasks/spike-x }  # inline comment\n"
            "    - path: dev/tasks/spike-x\n"
            "    - path: dev/tasks/other-task\n"
            "```\n"
            "\n"
            "Trailing prose tmp/spike-x.\n"
        )
        assert doc.read_text(encoding="utf-8") == expected

    def test_doc_referencing_different_task_not_touched(self, tmp_path):
        make_task(tmp_path, "tmp/spike-x")
        make_task(tmp_path, "tmp/spike-y")
        doc = write_doc(
            tmp_path / "other.md", fenced_task_list([{"path": "tmp/spike-y"}])
        )
        before = doc.read_text(encoding="utf-8")
        result = location_ops.move_task("tmp/spike-x", "dev/tasks", tmp_path)
        assert result.rewritten_docs == ()
        assert doc.read_text(encoding="utf-8") == before

    def test_demote_dev_tasks_to_tmp(self, tmp_path):
        old_folder = make_task(tmp_path, "dev/tasks/durable")
        doc = write_doc(
            tmp_path / "tmp" / "notes.md",
            fenced_task_list([{"path": "dev/tasks/durable"}]),
        )
        result = location_ops.move_task("dev/tasks/durable", "tmp", tmp_path)
        assert result.new_canonical == "tmp/durable"
        assert not old_folder.exists()
        assert (tmp_path / "tmp" / "durable" / "task.yaml").is_file()
        text = doc.read_text(encoding="utf-8")
        assert "tmp/durable" in text
        assert "dev/tasks/durable" not in text

    def test_destination_exists_errors_nothing_changed(self, tmp_path):
        old_folder = make_task(tmp_path, "tmp/spike-x")
        make_task(tmp_path, "dev/tasks/spike-x")  # occupies the destination
        doc = write_doc(
            tmp_path / "tmp" / "notes.md", fenced_task_list([{"path": "tmp/spike-x"}])
        )
        before = doc.read_text(encoding="utf-8")
        with pytest.raises(StateOpError, match="already exists"):
            location_ops.move_task("tmp/spike-x", "dev/tasks", tmp_path)
        assert old_folder.is_dir()
        assert doc.read_text(encoding="utf-8") == before

    def test_already_at_dest_errors(self, tmp_path):
        make_task(tmp_path, "tmp/spike-x")
        with pytest.raises(StateOpError, match="already in tmp"):
            location_ops.move_task("tmp/spike-x", "tmp", tmp_path)
        assert (tmp_path / "tmp" / "spike-x").is_dir()

    def test_absent_source_errors(self, tmp_path):
        with pytest.raises(StateOpError, match="no local task folder"):
            location_ops.move_task("tmp/ghost", "dev/tasks", tmp_path)

    def test_remote_source_errors(self, tmp_path):
        # Even with a same-named local folder, a tmp ref tagged with a
        # non-matching host is remote (spec 7.3) -- move refuses.
        folder = make_task(tmp_path, "tmp/spike-x")
        with pytest.raises(StateOpError, match="remote"):
            location_ops.move_task(
                "tmp/spike-x",
                "dev/tasks",
                tmp_path,
                ref_host=OTHER,
                local_host=LOCAL,
            )
        assert folder.is_dir()

    def test_unknown_dest_errors(self, tmp_path):
        make_task(tmp_path, "tmp/spike-x")
        with pytest.raises(StateOpError, match="unknown dest"):
            location_ops.move_task("tmp/spike-x", "docs", tmp_path)


class TestArchiveCLI:
    def test_tmp_archive_prints_disposition(self, tmp_path):
        folder = make_task(tmp_path, "tmp/a")
        proc = run_cli(
            ["archive", "tmp/a", "--root", str(tmp_path)],
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == (
            "archived: tmp/a (moved to tmp/archived-tasks/a, "
            "status: archived)"
        )
        assert not folder.exists()
        parked = tmp_path / "tmp" / "archived-tasks" / "a"
        assert read_block(parked)["status"] == "archived"

    def test_nontmp_committed_prints_deleted_disposition(self, git_root):
        folder = make_task(git_root, "dev/tasks/durable")
        _commit_all(git_root)
        proc = run_cli(
            [
                "archive",
                "dev/tasks/durable",
                "--root",
                str(git_root),
            ],
            git_root,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == (
            "archived: dev/tasks/durable (final state committed; folder "
            "deleted; version control is the record)"
        )
        assert not folder.exists()

    def test_not_in_git_repo_prints_vcs_pending_disposition(
        self, tmp_path
    ):
        folder = make_task(tmp_path, "dev/tasks/durable")
        proc = run_cli(
            [
                "archive",
                "dev/tasks/durable",
                "--root",
                str(tmp_path),
            ],
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        out = proc.stdout.strip()
        assert out.startswith("archived: dev/tasks/durable (")
        assert "final state recorded; folder kept" in out
        assert "then run delete" in out
        assert folder.is_dir()

    def test_git_ignored_prints_permanent_delete_warning(self, git_root):
        (git_root / ".gitignore").write_text("dev/\n", encoding="utf-8")
        _commit_all(git_root)
        folder = make_task(git_root, "dev/tasks/scratch")
        proc = run_cli(
            ["archive", "dev/tasks/scratch", "--root", str(git_root)],
            git_root,
        )
        assert proc.returncode == 0, proc.stderr
        out = proc.stdout.strip()
        assert "git-ignored" in out
        # The recovery path must be stated, and stated as destructive -- the
        # old behavior was a bare git error naming no next step at all.
        assert "delete" in out
        assert "PERMANENTLY" in out
        assert folder.is_dir()

    def test_closed_task_exits_nonzero_with_reopen_hint(self, tmp_path):
        make_task(tmp_path, "tmp/a", status="closed")
        proc = run_cli(
            ["archive", "tmp/a", "--root", str(tmp_path)],
            tmp_path,
        )
        assert proc.returncode != 0
        assert "reopen" in proc.stderr


class TestDeleteCLI:
    def test_tmp_delete_prints_deleted_id(self, tmp_path):
        folder = make_task(tmp_path, "tmp/a")
        proc = run_cli(
            ["delete", "tmp/a", "--root", str(tmp_path)],
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "deleted: tmp/a"
        assert not folder.exists()

    def test_uncommitted_refusal_exits_nonzero(self, git_root):
        folder = make_task(git_root, "dev/tasks/durable")
        proc = run_cli(
            [
                "delete",
                "dev/tasks/durable",
                "--root",
                str(git_root),
            ],
            git_root,
        )
        assert proc.returncode != 0
        assert "commit first" in proc.stderr
        assert folder.is_dir()


class TestMoveCLI:
    def test_move_prints_new_path_and_rewrite_count(self, tmp_path):
        make_task(tmp_path, "tmp/spike-x")
        write_doc(
            tmp_path / "tmp" / "notes.md", fenced_task_list([{"path": "tmp/spike-x"}])
        )
        write_doc(
            tmp_path / "more.md", fenced_task_list([{"path": "tmp/spike-x"}])
        )
        proc = run_cli(
            [
                "move",
                "tmp/spike-x",
                "dev/tasks",
                "--root",
                str(tmp_path),
            ],
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == [
            "moved: tmp/spike-x -> dev/tasks/spike-x",
            "rewrote 2 document(s)",
        ]
        assert (tmp_path / "dev" / "tasks" / "spike-x").is_dir()

    def test_destination_exists_exits_nonzero(self, tmp_path):
        make_task(tmp_path, "tmp/spike-x")
        make_task(tmp_path, "dev/tasks/spike-x")
        proc = run_cli(
            [
                "move",
                "tmp/spike-x",
                "dev/tasks",
                "--root",
                str(tmp_path),
            ],
            tmp_path,
        )
        assert proc.returncode != 0
        assert "already exists" in proc.stderr
        assert (tmp_path / "tmp" / "spike-x").is_dir()

    def test_absent_source_exits_nonzero(self, tmp_path):
        proc = run_cli(
            [
                "move",
                "tmp/ghost",
                "dev/tasks",
                "--root",
                str(tmp_path),
            ],
            tmp_path,
        )
        assert proc.returncode != 0
        assert "no local task folder" in proc.stderr


class TestDurableOutputs:
    """archive's durable-outputs verification (spec 2.8).

    The check is mechanical -- existence plus outside-the-folder -- because
    archive is forbidden from asking the user to assess anything. The
    judgment lives at authoring time (the declaration).
    """

    def test_absent_field_notes_and_archives(self, tmp_path):
        # Every folder predating the rule: degrade to a note, never refuse.
        make_task(tmp_path, "tmp/legacy")
        result = location_ops.archive_task("tmp/legacy", tmp_path)
        assert result.durable_note is not None
        assert "declares no durable_outputs" in result.durable_note
        assert not (tmp_path / "tmp" / "legacy").exists()
        assert (tmp_path / "tmp" / "archived-tasks" / "legacy").is_dir()

    def test_empty_list_is_an_explicit_nothing_durable(self, tmp_path):
        # Declaring nothing IS a valid result (content already absorbed
        # elsewhere); an explicit [] is not the same as an absent field.
        make_task(tmp_path, "tmp/nothing", durable_outputs=[])
        result = location_ops.archive_task("tmp/nothing", tmp_path)
        assert result.durable_note is None

    def test_declared_path_outside_the_folder_archives(self, tmp_path):
        home = tmp_path / "docs" / "architecture"
        home.mkdir(parents=True)
        (home / "env-json.md").write_text("# Spec\n", encoding="utf-8")
        make_task(
            tmp_path,
            "tmp/shipped",
            durable_outputs=["docs/architecture/env-json.md"],
        )
        result = location_ops.archive_task("tmp/shipped", tmp_path)
        assert result.durable_note is None
        assert (home / "env-json.md").is_file()

    def test_declared_path_inside_the_folder_refuses(self, tmp_path):
        # The load-bearing case: the folder is about to be parked/deleted,
        # so a document inside it has no durable home at all.
        folder = make_task(
            tmp_path,
            "tmp/spec",
            durable_outputs=["tmp/spec/analysis.md"],
        )
        (folder / "analysis.md").write_text("# THE SPEC\n", encoding="utf-8")
        with pytest.raises(StateOpError, match="lives INSIDE the task folder"):
            location_ops.archive_task("tmp/spec", tmp_path)
        # Refused BEFORE anything moved -- the document is still recoverable.
        assert folder.is_dir()
        assert (folder / "analysis.md").is_file()

    def test_declared_path_that_does_not_exist_refuses(self, tmp_path):
        make_task(tmp_path, "tmp/ghostdoc", durable_outputs=["docs/never.md"])
        with pytest.raises(StateOpError, match="no such path"):
            location_ops.archive_task("tmp/ghostdoc", tmp_path)
        assert (tmp_path / "tmp" / "ghostdoc").is_dir()

    def test_every_offender_is_named(self, tmp_path):
        folder = make_task(
            tmp_path,
            "tmp/multi",
            durable_outputs=["docs/gone-a.md", "tmp/multi/inside.md", ""],
        )
        (folder / "inside.md").write_text("x\n", encoding="utf-8")
        with pytest.raises(StateOpError) as exc:
            location_ops.archive_task("tmp/multi", tmp_path)
        msg = str(exc.value)
        assert "docs/gone-a.md" in msg
        assert "tmp/multi/inside.md" in msg
        assert "not a non-empty path string" in msg

    def test_absolute_path_refuses(self, tmp_path):
        # `project_root / entry` DISCARDS project_root for an absolute entry,
        # so without an explicit guard an out-of-repo path would pass -- and
        # a home version control does not carry is not durable.
        outside = tmp_path.parent / "outside-the-repo.md"
        outside.write_text("# Spec\n", encoding="utf-8")
        make_task(tmp_path, "tmp/abs", durable_outputs=[str(outside)])
        with pytest.raises(StateOpError, match="must be RELATIVE"):
            location_ops.archive_task("tmp/abs", tmp_path)
        assert (tmp_path / "tmp" / "abs").is_dir()

    def test_parent_escape_refuses(self, tmp_path):
        outside = tmp_path.parent / "escaped.md"
        outside.write_text("# Spec\n", encoding="utf-8")
        make_task(tmp_path, "tmp/esc", durable_outputs=["../escaped.md"])
        with pytest.raises(StateOpError, match="resolves OUTSIDE"):
            location_ops.archive_task("tmp/esc", tmp_path)
        assert (tmp_path / "tmp" / "esc").is_dir()

    def test_non_list_field_refuses(self, tmp_path):
        make_task(tmp_path, "tmp/bad", durable_outputs="docs/one.md")
        with pytest.raises(StateOpError, match="must be a list"):
            location_ops.archive_task("tmp/bad", tmp_path)

    def test_dev_tasks_folder_survives_a_refusal(self, git_root):
        # The whole point: refuse while the document can still be moved,
        # never after version control became the record.
        root = git_root
        folder = make_task(
            root, "dev/tasks/spec", durable_outputs=["dev/tasks/spec/spec.md"]
        )
        (folder / "spec.md").write_text("# THE SPEC\n", encoding="utf-8")
        _commit_all(root)
        with pytest.raises(StateOpError, match="lives INSIDE the task folder"):
            location_ops.archive_task("dev/tasks/spec", root)
        assert folder.is_dir()
        assert (folder / "spec.md").is_file()


class TestDurableOutputsCLI:
    def test_update_flag_persists_and_replaces(self, tmp_path):
        make_task(tmp_path, "tmp/decl")
        rounds = (
            ["--durable-output", "docs/a.md"],
            ["--durable-output", "docs/b.md", "--durable-output", "docs/c.md"],
        )
        for flags in rounds:
            proc = run_cli(
                [
                    "update",
                    "tmp/decl",
                    *flags,
                    "--root",
                    str(tmp_path),
                ],
                tmp_path,
            )
            assert proc.returncode == 0, proc.stderr
        data = yaml.safe_load(
            (tmp_path / "tmp" / "decl" / "task.yaml").read_text(encoding="utf-8")
        )
        # REPLACES, never appends (same convention as --skill-to-invoke).
        assert data["task"]["durable_outputs"] == ["docs/b.md", "docs/c.md"]

    def test_archive_note_goes_to_stderr_exit_zero(self, tmp_path):
        make_task(tmp_path, "tmp/legacy2")
        proc = run_cli(
            [
                "archive",
                "tmp/legacy2",
                "--root",
                str(tmp_path),
            ],
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.startswith("archived: tmp/legacy2")
        assert "declares no durable_outputs" in proc.stderr

    def test_archive_refusal_exits_nonzero(self, tmp_path):
        folder = make_task(
            tmp_path, "tmp/ref", durable_outputs=["tmp/ref/keep.md"]
        )
        (folder / "keep.md").write_text("x\n", encoding="utf-8")
        proc = run_cli(
            [
                "archive",
                "tmp/ref",
                "--root",
                str(tmp_path),
            ],
            tmp_path,
        )
        assert proc.returncode == 1
        assert "lives INSIDE the task folder" in proc.stderr
        assert folder.is_dir()
