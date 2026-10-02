"""refs-one-hop-deep: a nested references/<dir>/<file>.md is one hop when the
SKILL.md index.references[] declares the exact file path or its containing
directory; a nested file declared by neither form still FAILs."""

import yaml

from skills_kit_lib.audit import FAIL, PASS, check_universal
from skills_kit_lib.markdown_heuristics import parse_body, parse_frontmatter

RULE = "refs-one-hop-deep"


def _skill(tmp_path, index_paths, files):
    skill_dir = tmp_path / "subject"
    skill_dir.mkdir()
    block = {"domain_skill": {"index": {"references": [
        {"path": p, "purpose": "x"} for p in index_paths]}}}
    content = (
        "---\nname: subject\ndescription: Use when x. Do NOT use for y.\n---\n\n"
        "# subject\n\nIdentity sentence.\n\n```yaml\n"
        + yaml.safe_dump(block, sort_keys=False) + "```\n"
    )
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    for rel in files:
        p = skill_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# ref\n", encoding="utf-8")
    return skill_dir, content


def _verdict(skill_dir, content):
    fm = parse_frontmatter(content, mode="full")
    rows = [r for r in check_universal(fm, parse_body(content), skill_dir) if r.rule == RULE]
    assert len(rows) == 1
    return rows[0]


def test_nested_file_declared_by_exact_path_passes(tmp_path):
    d, c = _skill(tmp_path, ["references/lanes/audit-lane.md"], ["references/lanes/audit-lane.md"])
    assert _verdict(d, c).verdict == PASS


def test_nested_file_declared_by_containing_directory_passes(tmp_path):
    d, c = _skill(
        tmp_path,
        ["references/provenance/"],
        ["references/provenance/a.md", "references/provenance/b.md"],
    )
    assert _verdict(d, c).verdict == PASS


def test_nested_file_declared_by_neither_form_fails(tmp_path):
    d, c = _skill(
        tmp_path,
        ["references/lanes/audit-lane.md", "references/other/"],
        ["references/lanes/audit-lane.md", "references/lanes/unlisted.md", "references/stray/x.md"],
    )
    row = _verdict(d, c)
    assert row.verdict == FAIL
    assert "unlisted.md" in row.note and "x.md" in row.note and "audit-lane.md" not in row.note


def test_flat_reference_passes(tmp_path):
    d, c = _skill(tmp_path, ["references/flat.md"], ["references/flat.md"])
    assert _verdict(d, c).verdict == PASS
