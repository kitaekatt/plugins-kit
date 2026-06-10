"""Tests for tag.py -- the frontmatter skill-type writer (arch-review S17:
tag writes files and shipped untested).

Pins the documented behaviors: add, no-op, refuse-without-force, force,
--check dry-run, never-invent-frontmatter, invalid type rejection.
"""

from pathlib import Path

from skills_kit_lib.tag import tag


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "SKILL.md"
    p.write_text(text, encoding="utf-8")
    return p


class TestTag:
    def test_adds_missing_skill_type(self, tmp_path):
        p = _write(tmp_path, "---\nname: x\ndescription: d\n---\n# X\n")
        result = tag(p, "technique-skill", force=False, check_only=False)
        assert result["ok"] and result["action"] == "added"
        text = p.read_text(encoding="utf-8")
        assert "skill-type: technique-skill" in text
        assert text.endswith("# X\n")  # body untouched

    def test_no_op_when_value_matches(self, tmp_path):
        p = _write(tmp_path, "---\nname: x\nskill-type: technique-skill\n---\n# X\n")
        before = p.read_text(encoding="utf-8")
        result = tag(p, "technique-skill", force=False, check_only=False)
        assert result["ok"] and result["action"] == "no-op"
        assert p.read_text(encoding="utf-8") == before

    def test_refuses_overwrite_without_force(self, tmp_path):
        p = _write(tmp_path, "---\nname: x\nskill-type: reference-skill\n---\n# X\n")
        result = tag(p, "technique-skill", force=False, check_only=False)
        assert not result["ok"] and result["action"] == "refused"
        assert "skill-type: reference-skill" in p.read_text(encoding="utf-8")

    def test_force_overwrites(self, tmp_path):
        p = _write(tmp_path, "---\nname: x\nskill-type: reference-skill\n---\n# X\n")
        result = tag(p, "technique-skill", force=True, check_only=False)
        assert result["ok"] and result["action"] == "replaced"
        assert "skill-type: technique-skill" in p.read_text(encoding="utf-8")

    def test_check_mode_writes_nothing(self, tmp_path):
        p = _write(tmp_path, "---\nname: x\n---\n# X\n")
        before = p.read_text(encoding="utf-8")
        result = tag(p, "technique-skill", force=False, check_only=True)
        assert result["ok"] and result["action"] == "would-add"
        assert p.read_text(encoding="utf-8") == before

    def test_never_invents_frontmatter(self, tmp_path):
        p = _write(tmp_path, "# X\nno frontmatter here\n")
        before = p.read_text(encoding="utf-8")
        result = tag(p, "technique-skill", force=False, check_only=False)
        assert not result["ok"] and result.get("action") == "flag"
        assert p.read_text(encoding="utf-8") == before

    def test_invalid_type_rejected_before_touching_file(self, tmp_path):
        p = _write(tmp_path, "---\nname: x\n---\n# X\n")
        before = p.read_text(encoding="utf-8")
        result = tag(p, "not-a-type", force=False, check_only=False)
        assert not result["ok"]
        assert p.read_text(encoding="utf-8") == before

    def test_missing_file(self, tmp_path):
        result = tag(tmp_path / "missing.md", "technique-skill", False, False)
        assert not result["ok"]
