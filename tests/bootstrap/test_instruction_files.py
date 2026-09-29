"""Tests for bootstrap_lib.instruction_files (CLAUDE.md wins, AGENTS.md falls back)."""

from bootstrap_lib import instruction_files as inf


def test_claude_only(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("c\n")
    assert inf.resolve_instruction_file(tmp_path) == tmp_path / "CLAUDE.md"


def test_agents_only(tmp_path):
    (tmp_path / "AGENTS.md").write_text("a\n")
    assert inf.resolve_instruction_file(tmp_path) == tmp_path / "AGENTS.md"


def test_both_prefers_claude(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("c\n")
    (tmp_path / "AGENTS.md").write_text("a\n")
    assert inf.resolve_instruction_file(tmp_path) == tmp_path / "CLAUDE.md"


def test_neither(tmp_path):
    assert inf.resolve_instruction_file(tmp_path) is None


def test_active_shadowing(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("c\n")
    (tmp_path / "AGENTS.md").write_text("a\n")
    assert inf.is_active_instruction_file(tmp_path / "CLAUDE.md")
    assert not inf.is_active_instruction_file(tmp_path / "AGENTS.md")


def test_active_agents_when_alone(tmp_path):
    (tmp_path / "AGENTS.md").write_text("a\n")
    assert inf.is_active_instruction_file(tmp_path / "AGENTS.md")


def test_active_rejects_other_names(tmp_path):
    (tmp_path / "README.md").write_text("r\n")
    assert not inf.is_active_instruction_file(tmp_path / "README.md")


def test_defects_file_name_is_literal():
    assert inf.DEFECTS_FILE_NAME == "CLAUDE-potential-defects.md"
