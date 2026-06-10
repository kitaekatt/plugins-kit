"""End-to-end tests for the task-system location ops (Step 5).

Covers the spec section 7.1/7.2/7.4 verbs ``archive`` / ``delete`` /
``move``: archive's closure policy (tmp -> status archived + folder kept;
non-tmp -> folder deleted, git is the record), the uncommitted-archive guard
(refuse, no auto-commit; not-in-a-git-repo counts as uncommitted), the
status-active precondition (closed -> reopen-first error), delete's
inheritance of both archive readings plus unconditional folder removal,
move's relocation + span-precise reference rewrite across the project
document set (byte-level preservation outside the rewritten path values,
prose mentions and other-task refs untouched), and the pointer
clearing/updating rules for all three verbs.

All fixtures build under pytest tmp_path with an injected pointer path -- the
real repo's tmp/, dev/, and ~/.claude are never touched.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from bootstrap_guard import _REEXEC_GUARD_ENV
from task_system import location_ops
from task_system.pointer import read_current, write_current
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
    for fname in ("CLAUDE.md", "plan.md", "log.md"):
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
def ptr(tmp_path) -> Path:
    """An injected pointer path -- never the user-global default."""
    return tmp_path / "pointer" / "current"


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


class TestArchiveLib:
    def test_tmp_active_marks_archived_keeps_folder(self, tmp_path, ptr):
        folder = make_task(tmp_path, "tmp/a")
        result = location_ops.archive_task("tmp/a", tmp_path, ptr)
        assert result.canonical == "tmp/a"
        assert result.folder_removed is False
        assert read_block(folder)["status"] == "archived"
        for fname in SCAFFOLD_FILES:
            assert (folder / fname).is_file(), fname

    def test_tmp_archived_result_validates_clean(self, tmp_path, ptr):
        # Spec section 9 interplay: the non-tmp-archived warning is for
        # NON-tmp folders only -- a tmp archived folder (archive's own
        # output) is legitimate and validates clean.
        make_task(tmp_path, "tmp/a")
        location_ops.archive_task("tmp/a", tmp_path, ptr)
        result = validate_ref("tmp/a", tmp_path)
        assert result.classification == "archived"
        assert result.clean

    def test_nontmp_committed_deletes_folder(self, git_root, ptr):
        folder = make_task(git_root, "dev/tasks/durable")
        _commit_all(git_root)
        result = location_ops.archive_task("dev/tasks/durable", git_root, ptr)
        assert result.canonical == "dev/tasks/durable"
        assert result.folder_removed is True
        assert not folder.exists()

    def test_nontmp_uncommitted_refuses_untouched(self, git_root, ptr):
        folder = make_task(git_root, "dev/tasks/durable")  # never committed
        before = snapshot(folder)
        with pytest.raises(StateOpError, match="commit first"):
            location_ops.archive_task("dev/tasks/durable", git_root, ptr)
        assert folder.is_dir()
        assert snapshot(folder) == before

    def test_nontmp_not_in_git_repo_refuses(self, tmp_path, ptr):
        # No git repo at all: there is no git record, which the documented
        # reading (matching validate.is_uncommitted) treats as uncommitted.
        folder = make_task(tmp_path, "dev/tasks/durable")
        with pytest.raises(StateOpError, match="commit first"):
            location_ops.archive_task("dev/tasks/durable", tmp_path, ptr)
        assert folder.is_dir()

    def test_closed_task_errors_reopen_first(self, tmp_path, ptr):
        folder = make_task(tmp_path, "tmp/a", status="closed")
        with pytest.raises(StateOpError, match="reopen"):
            location_ops.archive_task("tmp/a", tmp_path, ptr)
        assert read_block(folder)["status"] == "closed"

    def test_other_non_active_status_errors(self, tmp_path, ptr):
        make_task(tmp_path, "tmp/a", status="blocked")
        with pytest.raises(StateOpError, match="active"):
            location_ops.archive_task("tmp/a", tmp_path, ptr)

    def test_missing_folder_errors(self, tmp_path, ptr):
        with pytest.raises(StateOpError, match="no task folder"):
            location_ops.archive_task("tmp/ghost", tmp_path, ptr)

    def test_pointer_cleared_when_current_tmp(self, tmp_path, ptr):
        folder = make_task(tmp_path, "tmp/a")
        write_current(ptr, folder)
        location_ops.archive_task("tmp/a", tmp_path, ptr)
        assert read_current(ptr) is None

    def test_pointer_cleared_when_current_nontmp(self, git_root, ptr):
        folder = make_task(git_root, "dev/tasks/durable")
        _commit_all(git_root)
        write_current(ptr, folder)
        location_ops.archive_task("dev/tasks/durable", git_root, ptr)
        assert read_current(ptr) is None

    def test_pointer_untouched_when_it_names_another_task(self, tmp_path, ptr):
        folder_b = make_task(tmp_path, "tmp/b")
        make_task(tmp_path, "tmp/a")
        write_current(ptr, folder_b)
        location_ops.archive_task("tmp/a", tmp_path, ptr)
        assert read_current(ptr) == str(folder_b.resolve())


class TestDeleteLib:
    def test_tmp_active_folder_gone(self, tmp_path, ptr):
        folder = make_task(tmp_path, "tmp/a")
        canonical = location_ops.delete_task("tmp/a", tmp_path, ptr)
        assert canonical == "tmp/a"
        assert not folder.exists()

    def test_nontmp_committed_folder_gone(self, git_root, ptr):
        folder = make_task(git_root, "dev/tasks/durable")
        _commit_all(git_root)
        location_ops.delete_task("dev/tasks/durable", git_root, ptr)
        assert not folder.exists()

    def test_closed_task_errors_reopen_first(self, tmp_path, ptr):
        # Pins the documented Step 5 reading: delete inherits archive's
        # status-active precondition (delete = archive + unconditional
        # removal, spec 7.1).
        folder = make_task(tmp_path, "tmp/a", status="closed")
        with pytest.raises(StateOpError, match="reopen"):
            location_ops.delete_task("tmp/a", tmp_path, ptr)
        assert folder.is_dir()

    def test_nontmp_uncommitted_refuses_untouched(self, git_root, ptr):
        # Pins the documented Step 5 reading: delete inherits archive's
        # uncommitted guard too -- an uncommitted dev/tasks folder refuses.
        folder = make_task(git_root, "dev/tasks/durable")
        before = snapshot(folder)
        with pytest.raises(StateOpError, match="commit first"):
            location_ops.delete_task("dev/tasks/durable", git_root, ptr)
        assert folder.is_dir()
        assert snapshot(folder) == before

    def test_missing_folder_errors(self, tmp_path, ptr):
        with pytest.raises(StateOpError, match="no task folder"):
            location_ops.delete_task("tmp/ghost", tmp_path, ptr)

    def test_pointer_cleared_when_current(self, tmp_path, ptr):
        folder = make_task(tmp_path, "tmp/a")
        write_current(ptr, folder)
        location_ops.delete_task("tmp/a", tmp_path, ptr)
        assert read_current(ptr) is None


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
    def test_promote_relocates_and_rewrites_multiple_docs(self, git_root, ptr):
        old_folder = make_task(git_root, "tmp/spike-x")
        doc_a = write_doc(
            git_root / "notes.md", fenced_task_list([{"path": "tmp/spike-x"}])
        )
        doc_b = write_doc(
            git_root / "skills" / "foo" / "SKILL.md",
            fenced_task_list([{"path": "tmp/spike-x"}, {"path": "tmp/spike-x"}]),
        )
        _commit_all(git_root)
        result = location_ops.move_task(
            "tmp/spike-x", "dev/tasks", git_root, ptr
        )
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

    def test_byte_level_preservation_and_prose_untouched(self, tmp_path, ptr):
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
        doc = tmp_path / "notes.md"
        doc.write_text(original, encoding="utf-8")
        location_ops.move_task("tmp/spike-x", "dev/tasks", tmp_path, ptr)
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

    def test_doc_referencing_different_task_not_touched(self, tmp_path, ptr):
        make_task(tmp_path, "tmp/spike-x")
        make_task(tmp_path, "tmp/spike-y")
        doc = write_doc(
            tmp_path / "other.md", fenced_task_list([{"path": "tmp/spike-y"}])
        )
        before = doc.read_text(encoding="utf-8")
        result = location_ops.move_task(
            "tmp/spike-x", "dev/tasks", tmp_path, ptr
        )
        assert result.rewritten_docs == ()
        assert doc.read_text(encoding="utf-8") == before

    def test_pointer_updated_when_it_named_the_old_path(self, tmp_path, ptr):
        old_folder = make_task(tmp_path, "tmp/spike-x")
        write_current(ptr, old_folder)
        location_ops.move_task("tmp/spike-x", "dev/tasks", tmp_path, ptr)
        new_folder = tmp_path / "dev" / "tasks" / "spike-x"
        assert read_current(ptr) == str(new_folder.resolve())

    def test_pointer_untouched_when_it_names_another_task(self, tmp_path, ptr):
        folder_b = make_task(tmp_path, "tmp/b")
        make_task(tmp_path, "tmp/spike-x")
        write_current(ptr, folder_b)
        location_ops.move_task("tmp/spike-x", "dev/tasks", tmp_path, ptr)
        assert read_current(ptr) == str(folder_b.resolve())

    def test_demote_dev_tasks_to_tmp(self, tmp_path, ptr):
        old_folder = make_task(tmp_path, "dev/tasks/durable")
        doc = write_doc(
            tmp_path / "notes.md",
            fenced_task_list([{"path": "dev/tasks/durable"}]),
        )
        result = location_ops.move_task(
            "dev/tasks/durable", "tmp", tmp_path, ptr
        )
        assert result.new_canonical == "tmp/durable"
        assert not old_folder.exists()
        assert (tmp_path / "tmp" / "durable" / "task.yaml").is_file()
        text = doc.read_text(encoding="utf-8")
        assert "tmp/durable" in text
        assert "dev/tasks/durable" not in text

    def test_destination_exists_errors_nothing_changed(self, tmp_path, ptr):
        old_folder = make_task(tmp_path, "tmp/spike-x")
        make_task(tmp_path, "dev/tasks/spike-x")  # occupies the destination
        doc = write_doc(
            tmp_path / "notes.md", fenced_task_list([{"path": "tmp/spike-x"}])
        )
        before = doc.read_text(encoding="utf-8")
        with pytest.raises(StateOpError, match="already exists"):
            location_ops.move_task("tmp/spike-x", "dev/tasks", tmp_path, ptr)
        assert old_folder.is_dir()
        assert doc.read_text(encoding="utf-8") == before

    def test_already_at_dest_errors(self, tmp_path, ptr):
        make_task(tmp_path, "tmp/spike-x")
        with pytest.raises(StateOpError, match="already in tmp"):
            location_ops.move_task("tmp/spike-x", "tmp", tmp_path, ptr)
        assert (tmp_path / "tmp" / "spike-x").is_dir()

    def test_absent_source_errors(self, tmp_path, ptr):
        with pytest.raises(StateOpError, match="no local task folder"):
            location_ops.move_task("tmp/ghost", "dev/tasks", tmp_path, ptr)

    def test_remote_source_errors(self, tmp_path, ptr):
        # Even with a same-named local folder, a tmp ref tagged with a
        # non-matching host is remote (spec 7.3) -- move refuses.
        folder = make_task(tmp_path, "tmp/spike-x")
        with pytest.raises(StateOpError, match="remote"):
            location_ops.move_task(
                "tmp/spike-x",
                "dev/tasks",
                tmp_path,
                ptr,
                ref_host=OTHER,
                local_host=LOCAL,
            )
        assert folder.is_dir()

    def test_unknown_dest_errors(self, tmp_path, ptr):
        make_task(tmp_path, "tmp/spike-x")
        with pytest.raises(StateOpError, match="unknown dest"):
            location_ops.move_task("tmp/spike-x", "docs", tmp_path, ptr)


class TestArchiveCLI:
    def test_tmp_archive_prints_disposition(self, tmp_path, ptr):
        folder = make_task(tmp_path, "tmp/a")
        proc = run_cli(
            ["archive", "tmp/a", "--root", str(tmp_path), "--pointer", str(ptr)],
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == (
            "archived: tmp/a (tmp folder kept, status: archived)"
        )
        assert read_block(folder)["status"] == "archived"

    def test_nontmp_committed_prints_deleted_disposition(self, git_root, ptr):
        folder = make_task(git_root, "dev/tasks/durable")
        _commit_all(git_root)
        proc = run_cli(
            [
                "archive",
                "dev/tasks/durable",
                "--root",
                str(git_root),
                "--pointer",
                str(ptr),
            ],
            git_root,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == (
            "archived: dev/tasks/durable (folder deleted; git is the record)"
        )
        assert not folder.exists()

    def test_uncommitted_refusal_exits_nonzero(self, git_root, ptr):
        folder = make_task(git_root, "dev/tasks/durable")
        proc = run_cli(
            [
                "archive",
                "dev/tasks/durable",
                "--root",
                str(git_root),
                "--pointer",
                str(ptr),
            ],
            git_root,
        )
        assert proc.returncode != 0
        assert proc.stdout == ""
        assert "commit first" in proc.stderr
        assert folder.is_dir()

    def test_closed_task_exits_nonzero_with_reopen_hint(self, tmp_path, ptr):
        make_task(tmp_path, "tmp/a", status="closed")
        proc = run_cli(
            ["archive", "tmp/a", "--root", str(tmp_path), "--pointer", str(ptr)],
            tmp_path,
        )
        assert proc.returncode != 0
        assert "reopen" in proc.stderr


class TestDeleteCLI:
    def test_tmp_delete_prints_deleted_id(self, tmp_path, ptr):
        folder = make_task(tmp_path, "tmp/a")
        proc = run_cli(
            ["delete", "tmp/a", "--root", str(tmp_path), "--pointer", str(ptr)],
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "deleted: tmp/a"
        assert not folder.exists()

    def test_uncommitted_refusal_exits_nonzero(self, git_root, ptr):
        folder = make_task(git_root, "dev/tasks/durable")
        proc = run_cli(
            [
                "delete",
                "dev/tasks/durable",
                "--root",
                str(git_root),
                "--pointer",
                str(ptr),
            ],
            git_root,
        )
        assert proc.returncode != 0
        assert "commit first" in proc.stderr
        assert folder.is_dir()


class TestMoveCLI:
    def test_move_prints_new_path_and_rewrite_count(self, tmp_path, ptr):
        make_task(tmp_path, "tmp/spike-x")
        write_doc(
            tmp_path / "notes.md", fenced_task_list([{"path": "tmp/spike-x"}])
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
                "--pointer",
                str(ptr),
            ],
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == [
            "moved: tmp/spike-x -> dev/tasks/spike-x",
            "rewrote 2 document(s)",
        ]
        assert (tmp_path / "dev" / "tasks" / "spike-x").is_dir()

    def test_destination_exists_exits_nonzero(self, tmp_path, ptr):
        make_task(tmp_path, "tmp/spike-x")
        make_task(tmp_path, "dev/tasks/spike-x")
        proc = run_cli(
            [
                "move",
                "tmp/spike-x",
                "dev/tasks",
                "--root",
                str(tmp_path),
                "--pointer",
                str(ptr),
            ],
            tmp_path,
        )
        assert proc.returncode != 0
        assert "already exists" in proc.stderr
        assert (tmp_path / "tmp" / "spike-x").is_dir()

    def test_absent_source_exits_nonzero(self, tmp_path, ptr):
        proc = run_cli(
            [
                "move",
                "tmp/ghost",
                "dev/tasks",
                "--root",
                str(tmp_path),
                "--pointer",
                str(ptr),
            ],
            tmp_path,
        )
        assert proc.returncode != 0
        assert "no local task folder" in proc.stderr
