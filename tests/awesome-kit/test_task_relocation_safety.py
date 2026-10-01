"""A failed folder relocation or removal never leaves a task split.

Observed failure: on Windows/Perforce, ``os.rename`` of a task folder raised
PermissionError, ``shutil.move`` fell back to copy-then-delete, and the
delete failed on a read-only file after the copy completed -- two folders
that both said ``archived``. Every site that relocates or removes a task
folder now goes through ``task_system.relocate``. These tests cover, per
site: (a) rename fails, copy fallback succeeds; (b) a read-only file does
not block the removal; (c) the source cannot be removed at all -> one
authoritative folder and a plain error.

POSIX does not honour read-only on unlink, so read-only files are simulated
by wrapping ``os.unlink``: a "locked" file refuses to unlink until the
handler clears the bit (``os.chmod``); a "stuck" path never unlinks.
"""

import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

from task_system import init as init_mod
from task_system import location_ops, reference_rewrite, state_ops
from task_system.init import init_task
from task_system.relocate import RelocationError, relocate_tree, remove_tree
from task_system.state_ops import StateOpError
from test_task_location_ops import (
    _commit_all,
    git_root,
    make_task,
    read_block,
    run_cli,
)

__all__ = ["git_root"]


class FileGuard:
    """Wraps os.unlink / os.chmod to simulate read-only and stuck files."""

    def __init__(self, monkeypatch) -> None:
        self.locked: set[str] = set()  # unlink fails until chmod'd
        self.stuck_under: list[str] = []  # unlink always fails under these
        self.cleared: set[str] = set()
        self.clear_calls = 0
        real_unlink, real_chmod = os.unlink, os.chmod

        def unlink(path, *a, **k):
            full = os.fspath(path)
            name = os.path.basename(full)
            if any(s in full for s in self.stuck_under):
                raise PermissionError(13, "stuck", full)
            if name in self.locked and full not in self.cleared:
                raise PermissionError(13, "read-only", full)
            return real_unlink(path, *a, **k)

        def chmod(path, mode, *a, **k):
            self.clear_calls += 1
            self.cleared.add(os.fspath(path))
            return real_chmod(path, mode, *a, **k)

        monkeypatch.setattr(os, "unlink", unlink)
        monkeypatch.setattr(os, "chmod", chmod)


@pytest.fixture
def guard(monkeypatch) -> FileGuard:
    return FileGuard(monkeypatch)


@pytest.fixture
def no_rename(monkeypatch) -> None:
    def boom(*a, **k):
        raise PermissionError(13, "Access is denied (simulated WinError 5)")

    monkeypatch.setattr(os, "rename", boom)


def _complete(folder: Path) -> bool:
    return all(
        (folder / f).is_file()
        for f in ("CLAUDE.md", "plan.md", "log.md", "task.yaml")
    )


class TestHelper:
    def test_rename_failure_copies_and_removes_source(self, tmp_path, no_rename):
        src = make_task(tmp_path, "src")
        relocate_tree(src, tmp_path / "dst")
        assert not src.exists() and _complete(tmp_path / "dst")

    def test_readonly_file_cleared_and_retried(self, tmp_path, guard, no_rename):
        src = make_task(tmp_path, "src")
        guard.locked.add("log.md")
        relocate_tree(src, tmp_path / "dst")
        assert not src.exists() and _complete(tmp_path / "dst")
        assert guard.clear_calls >= 1

    @pytest.mark.skipif(sys.platform != "win32", reason="real read-only bit")
    def test_real_readonly_attribute_windows(self, tmp_path):
        src = make_task(tmp_path, "src")
        os.chmod(src / "log.md", stat.S_IREAD)
        remove_tree(src)
        assert not src.exists()

    def test_stuck_source_leaves_copy_authoritative(
        self, tmp_path, guard, no_rename
    ):
        src = make_task(tmp_path, "src")
        guard.stuck_under.append(str(src))
        with pytest.raises(RelocationError) as ei:
            relocate_tree(src, tmp_path / "dst")
        assert ei.value.authoritative == tmp_path / "dst"
        assert _complete(tmp_path / "dst")

    def test_incomplete_copy_is_rolled_back(self, tmp_path, no_rename, monkeypatch):
        src = make_task(tmp_path, "src")

        real = shutil.copytree

        def bad_copytree(s, d, *a, **k):
            real(s, d)
            (Path(d) / "log.md").write_text("truncated", encoding="utf-8")

        monkeypatch.setattr("task_system.relocate.shutil.copytree", bad_copytree)
        with pytest.raises(RelocationError) as ei:
            relocate_tree(src, tmp_path / "dst")
        assert ei.value.authoritative == src
        assert _complete(src) and not (tmp_path / "dst").exists()


class TestArchiveTmp:
    def test_rename_fails_still_one_archived_folder(self, tmp_path, no_rename):
        folder = make_task(tmp_path, "tmp/a")
        location_ops.archive_task("tmp/a", tmp_path)
        parked = tmp_path / "tmp" / "archived-tasks" / "a"
        assert not folder.exists()
        assert read_block(parked)["status"] == "archived"

    def test_readonly_file_does_not_block(self, tmp_path, guard, no_rename):
        folder = make_task(tmp_path, "tmp/a")
        guard.locked.add("plan.md")
        location_ops.archive_task("tmp/a", tmp_path)
        assert not folder.exists()
        assert _complete(tmp_path / "tmp" / "archived-tasks" / "a")

    def test_unremovable_source_names_one_authoritative_folder(
        self, tmp_path, guard, no_rename
    ):
        folder = make_task(tmp_path, "tmp/a")
        guard.stuck_under.append(str(folder))
        with pytest.raises(StateOpError, match="authoritative folder is"):
            location_ops.archive_task("tmp/a", tmp_path)
        parked = tmp_path / "tmp" / "archived-tasks" / "a"
        assert read_block(parked)["status"] == "archived"

    def test_failed_move_restores_live_status(self, tmp_path, monkeypatch):
        folder = make_task(tmp_path, "tmp/a")

        def fail(*a, **k):
            raise RelocationError("x", authoritative=folder)

        monkeypatch.setattr(location_ops, "relocate_tree", fail)
        with pytest.raises(StateOpError):
            location_ops.archive_task("tmp/a", tmp_path)
        assert read_block(folder)["status"] == "active"
        assert not (tmp_path / "tmp" / "archived-tasks" / "a").exists()

    def test_cli_failure_is_plain_message_nonzero(self, tmp_path):
        # A subprocess cannot share monkeypatches; make the parking parent
        # unusable so the move cannot proceed, then check the CLI contract.
        make_task(tmp_path, "tmp/a")
        (tmp_path / "tmp" / "archived-tasks").write_text("f", encoding="utf-8")
        proc = run_cli(["archive", "tmp/a"], tmp_path)
        assert proc.returncode != 0
        assert "Traceback" not in proc.stderr


class TestArchiveIgnoredParking:
    def test_rename_fails_readonly_ok(self, git_root, guard, no_rename):
        (git_root / ".gitignore").write_text("dev/\n", encoding="utf-8")
        _commit_all(git_root)
        folder = make_task(git_root, "dev/tasks/scratch")
        guard.locked.add("log.md")
        location_ops.archive_task("dev/tasks/scratch", git_root)
        parked = git_root / "dev" / "tasks" / "archived-tasks" / "scratch"
        assert not folder.exists()
        assert read_block(parked)["status"] == "archived"

    def test_failed_move_restores_live_docs(self, git_root, monkeypatch):
        (git_root / ".gitignore").write_text("dev/\n", encoding="utf-8")
        _commit_all(git_root)
        folder = make_task(git_root, "dev/tasks/scratch")
        log_before = (folder / "log.md").read_bytes()

        def fail(*a, **k):
            raise RelocationError("x", authoritative=folder)

        monkeypatch.setattr(location_ops, "relocate_tree", fail)
        with pytest.raises(StateOpError):
            location_ops.archive_task("dev/tasks/scratch", git_root)
        assert read_block(folder)["status"] == "active"
        assert (folder / "log.md").read_bytes() == log_before


class TestArchiveCommittedRemoval:
    def test_readonly_file_does_not_block_removal(self, git_root, guard):
        folder = make_task(git_root, "dev/tasks/t")
        _commit_all(git_root)
        guard.locked.add("log.md")
        result = location_ops.archive_task("dev/tasks/t", git_root)
        assert result.folder_removed and not folder.exists()

    def test_unremovable_folder_is_plain_error(self, git_root, guard):
        folder = make_task(git_root, "dev/tasks/t")
        _commit_all(git_root)
        guard.stuck_under.append(str(folder))
        with pytest.raises(StateOpError, match="could not remove"):
            location_ops.archive_task("dev/tasks/t", git_root)


class TestDelete:
    def test_readonly_file_does_not_block(self, tmp_path, guard):
        folder = make_task(tmp_path, "tmp/a")
        guard.locked.add("task.yaml")
        location_ops.delete_task("tmp/a", tmp_path)
        assert not folder.exists()

    def test_stuck_file_is_plain_error(self, tmp_path, guard):
        folder = make_task(tmp_path, "tmp/a")
        guard.stuck_under.append(str(folder))
        with pytest.raises(StateOpError, match="delete failed"):
            location_ops.delete_task("tmp/a", tmp_path)


class TestReopen:
    def _archived(self, tmp_path) -> Path:
        make_task(tmp_path, "tmp/a")
        location_ops.archive_task("tmp/a", tmp_path)
        return tmp_path / "tmp" / "archived-tasks" / "a"

    def test_rename_fails_readonly_ok(self, tmp_path, guard, no_rename):
        parked = self._archived(tmp_path)
        guard.locked.add("log.md")
        state_ops.reopen("tmp/a", tmp_path)
        assert not parked.exists()
        assert read_block(tmp_path / "tmp" / "a")["status"] == "active"

    def test_stuck_parked_source_is_plain_error(self, tmp_path, guard, no_rename):
        parked = self._archived(tmp_path)
        guard.stuck_under.append(str(parked))
        with pytest.raises(StateOpError, match="authoritative folder is"):
            state_ops.reopen("tmp/a", tmp_path)


class TestMove:
    def test_rename_fails_readonly_ok(self, tmp_path, guard, no_rename):
        old = make_task(tmp_path, "tmp/a")
        guard.locked.add("log.md")
        reference_rewrite.move_task("tmp/a", "dev/tasks", tmp_path)
        assert not old.exists()
        assert _complete(tmp_path / "dev" / "tasks" / "a")

    def test_stuck_source_is_plain_error(self, tmp_path, guard, no_rename):
        old = make_task(tmp_path, "tmp/a")
        guard.stuck_under.append(str(old))
        with pytest.raises(StateOpError, match="authoritative folder is"):
            reference_rewrite.move_task("tmp/a", "dev/tasks", tmp_path)


class TestInitCleanup:
    def _fail_validation(self, monkeypatch) -> None:
        def boom(*a, **k):
            raise RuntimeError("validation exploded")

        monkeypatch.setattr(init_mod, "validate_ref", boom)

    def test_readonly_file_does_not_block_cleanup(
        self, tmp_path, guard, monkeypatch
    ):
        self._fail_validation(monkeypatch)
        guard.locked.add("task.yaml")
        with pytest.raises(RuntimeError, match="exploded"):
            init_task("spike-ipv6-diag", tmp_path)
        assert not any(tmp_path.rglob("task.yaml"))

    def test_stuck_cleanup_is_reported_not_silent(
        self, tmp_path, guard, monkeypatch, capsys
    ):
        self._fail_validation(monkeypatch)
        guard.stuck_under.append(str(tmp_path))
        with pytest.raises(RuntimeError, match="exploded"):
            init_task("spike-ipv6-diag", tmp_path)
        assert "could not remove" in capsys.readouterr().err


def _split(root: Path, rel: str, *, status_in_parked: str = "archived"):
    """Build the observed split: a complete parked copy plus a partial
    source holding a subset of the same files."""
    import yaml

    src = make_task(root, rel)
    parked = root / "tmp" / "archived-tasks" / "a"
    shutil.copytree(src, parked)
    data = read_block(parked)
    data["status"] = status_in_parked
    (parked / "task.yaml").write_text(
        yaml.safe_dump({"task": data}, sort_keys=False), encoding="utf-8"
    )
    shutil.copy2(parked / "task.yaml", src / "task.yaml")
    (src / "plan.md").unlink()
    (src / "CLAUDE.md").unlink()
    return src, parked


def _bytes(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in folder.iterdir()}


class TestSplitStateRepair:
    def test_subset_source_is_repaired_incl_readonly(self, tmp_path, guard):
        src, parked = _split(tmp_path, "tmp/a", status_in_parked="active")
        guard.locked.add("log.md")
        result = location_ops.archive_task("tmp/a", tmp_path)
        assert result.repaired_split is True
        assert not src.exists()
        assert _complete(parked)
        assert read_block(parked)["status"] == "archived"

    def test_differing_source_file_refuses_and_changes_nothing(self, tmp_path):
        src, parked = _split(tmp_path, "tmp/a")
        (src / "log.md").write_text("edited since the failed archive\n")
        before_src, before_parked = _bytes(src), _bytes(parked)
        with pytest.raises(StateOpError, match="log.md"):
            location_ops.archive_task("tmp/a", tmp_path)
        assert _bytes(src) == before_src
        assert _bytes(parked) == before_parked

    def test_source_only_file_refuses(self, tmp_path):
        src, parked = _split(tmp_path, "tmp/a")
        (src / "notes-only-here.md").write_text("unique\n")
        with pytest.raises(StateOpError, match="notes-only-here.md"):
            location_ops.archive_task("tmp/a", tmp_path)
        assert (src / "notes-only-here.md").is_file()

    def test_cli_repairs_with_plain_line_and_zero_exit(self, tmp_path):
        src, parked = _split(tmp_path, "tmp/a")
        proc = run_cli(["archive", "tmp/a"], tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert "repaired split state" in proc.stdout
        assert not src.exists()

    def test_cli_refusal_nonzero_names_paths(self, tmp_path):
        src, parked = _split(tmp_path, "tmp/a")
        (src / "extra.md").write_text("x\n")
        proc = run_cli(["archive", "tmp/a"], tmp_path)
        assert proc.returncode != 0
        assert "extra.md" in proc.stderr and "Traceback" not in proc.stderr

    def test_reopen_repairs_split_and_restores_parked(self, tmp_path):
        src, parked = _split(tmp_path, "tmp/a")
        state_ops.reopen("tmp/a", tmp_path)
        assert not parked.exists() and _complete(src)
        assert read_block(src)["status"] == "active"

    def test_ignored_parking_repairs_split(self, git_root):
        (git_root / ".gitignore").write_text("dev/\n", encoding="utf-8")
        _commit_all(git_root)
        src = make_task(git_root, "dev/tasks/scratch")
        parked = git_root / "dev" / "tasks" / "archived-tasks" / "scratch"
        shutil.copytree(src, parked)
        (src / "plan.md").unlink()
        result = location_ops.archive_task("dev/tasks/scratch", git_root)
        assert result.repaired_split and not src.exists()
        assert read_block(parked)["status"] == "archived"
