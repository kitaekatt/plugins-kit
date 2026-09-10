"""Tests for ``move``'s relocation + span-precise reference rewrite (spec
7.1/7.2): byte-level preservation outside the rewritten path values, prose
mentions and other-task refs untouched.

Reuses the fixtures and helpers defined in ``test_task_location_ops.py``
(project layout builders, the ``git_root`` fixture) rather than duplicating
them -- the two modules describe one project's temp-directory conventions.
"""

import pytest

from task_system import reference_rewrite
from task_system.state_ops import StateOpError
from test_task_location_ops import (
    LOCAL,
    OTHER,
    SCAFFOLD_FILES,
    _commit_all,
    fenced_task_list,
    git_root,
    make_task,
    write_doc,
)

__all__ = ["git_root"]  # re-exported fixture, kept visible to linters


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
        result = reference_rewrite.move_task("tmp/spike-x", "dev/tasks", git_root)
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
        reference_rewrite.move_task("tmp/spike-x", "dev/tasks", tmp_path)
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
        result = reference_rewrite.move_task("tmp/spike-x", "dev/tasks", tmp_path)
        assert result.rewritten_docs == ()
        assert doc.read_text(encoding="utf-8") == before

    def test_demote_dev_tasks_to_tmp(self, tmp_path):
        old_folder = make_task(tmp_path, "dev/tasks/durable")
        doc = write_doc(
            tmp_path / "tmp" / "notes.md",
            fenced_task_list([{"path": "dev/tasks/durable"}]),
        )
        result = reference_rewrite.move_task("dev/tasks/durable", "tmp", tmp_path)
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
            reference_rewrite.move_task("tmp/spike-x", "dev/tasks", tmp_path)
        assert old_folder.is_dir()
        assert doc.read_text(encoding="utf-8") == before

    def test_already_at_dest_errors(self, tmp_path):
        make_task(tmp_path, "tmp/spike-x")
        with pytest.raises(StateOpError, match="already in tmp"):
            reference_rewrite.move_task("tmp/spike-x", "tmp", tmp_path)
        assert (tmp_path / "tmp" / "spike-x").is_dir()

    def test_absent_source_errors(self, tmp_path):
        with pytest.raises(StateOpError, match="no local task folder"):
            reference_rewrite.move_task("tmp/ghost", "dev/tasks", tmp_path)

    def test_remote_source_errors(self, tmp_path):
        # Even with a same-named local folder, a tmp ref tagged with a
        # non-matching host is remote (spec 7.3) -- move refuses.
        folder = make_task(tmp_path, "tmp/spike-x")
        with pytest.raises(StateOpError, match="remote"):
            reference_rewrite.move_task(
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
            reference_rewrite.move_task("tmp/spike-x", "docs", tmp_path)

