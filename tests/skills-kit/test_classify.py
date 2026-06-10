"""Tests for classify.py verdicts (arch-review S11 + S17).

Covers the YAML-contract path (now routed through document_walker's canonical
fence recognition -- ```yml fences and multi-block documents included), the
frontmatter-disagreement verdict, mixed-type detection, the heuristic
fallback, and the registry-derived type map.
"""

from pathlib import Path

from skills_kit_lib.classify import CONTRACT_ROOT_TO_TYPE, classify, extract_yaml_roots
from skills_kit_lib.schema_registry import SKILL_TYPE_ROOTS


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "SKILL.md"
    p.write_text(text, encoding="utf-8")
    return p


class TestTypeMapDerived:
    def test_contract_root_to_type_derived_from_registry(self):
        assert CONTRACT_ROOT_TO_TYPE == {
            r: r.replace("_", "-") for r in SKILL_TYPE_ROOTS
        }


class TestYamlContractPath:
    def test_single_root_is_deterministic(self, tmp_path):
        p = _write(tmp_path, (
            "---\nname: x\n---\n# X\n\n"
            "```yaml\ntechnique_skill:\n  identity: i\n```\n"
        ))
        report = classify(p)
        assert report["verdict"] == "single-type"
        assert report["suggested_type"] == "technique-skill"
        assert report["source"] == "yaml-contract"

    def test_yml_fence_recognized(self, tmp_path):
        p = _write(tmp_path, (
            "---\nname: x\n---\n# X\n\n"
            "```yml\nreference_skill:\n  identity: i\n```\n"
        ))
        report = classify(p)
        assert report["suggested_type"] == "reference-skill"

    def test_example_first_fence_does_not_shadow_contract(self, tmp_path):
        p = _write(tmp_path, (
            "---\nname: x\n---\n# X\n\n"
            "```yaml\nexample_config:\n  a: 1\n```\n\n"
            "```yaml\ndomain_skill:\n  identity: i\n```\n"
        ))
        report = classify(p)
        assert report["suggested_type"] == "domain-skill"
        assert report["yaml_roots"] == ["domain_skill"]

    def test_two_roots_is_mixed_type(self, tmp_path):
        p = _write(tmp_path, (
            "---\nname: x\n---\n# X\n\n"
            "```yaml\nreference_skill:\n  identity: i\ntechnique_skill:\n  identity: j\n```\n"
        ))
        report = classify(p)
        assert report["verdict"] == "mixed-type"

    def test_roots_across_separate_blocks_also_mixed(self, tmp_path):
        p = _write(tmp_path, (
            "---\nname: x\n---\n# X\n\n"
            "```yaml\nreference_skill:\n  identity: i\n```\n\n"
            "```yaml\ntechnique_skill:\n  identity: j\n```\n"
        ))
        report = classify(p)
        assert report["verdict"] == "mixed-type"

    def test_frontmatter_disagreement(self, tmp_path):
        p = _write(tmp_path, (
            "---\nname: x\nskill-type: reference-skill\n---\n# X\n\n"
            "```yaml\ntechnique_skill:\n  identity: i\n```\n"
        ))
        report = classify(p)
        assert report["verdict"] == "frontmatter-disagreement"
        assert report["suggested_type"] == "technique-skill"
        assert report["declared_type"] == "reference-skill"


class TestHeuristicFallback:
    def test_no_signals_is_indeterminate(self, tmp_path):
        p = _write(tmp_path, "---\nname: x\n---\n# X\n\nplain text only\n")
        report = classify(p)
        assert report["verdict"] == "indeterminate"
        assert report["source"] == "heuristic-fallback"

    def test_missing_file_reports_error(self, tmp_path):
        report = classify(tmp_path / "nope" / "SKILL.md")
        assert "error" in report


class TestExtractYamlRoots:
    def test_unparseable_block_skipped(self, tmp_path):
        body = "```yaml\n: : bad : yaml :\n```\n```yaml\naudit_skill:\n  a: 1\n```\n"
        assert extract_yaml_roots(body) == ["audit_skill"]

    def test_no_blocks_empty(self):
        assert extract_yaml_roots("# nothing\n") == []
