"""Tests for the asset_dependencies portable unit and its audit resolution.

asset_dependencies declares repo files a skill consumes at RUNTIME (bundled
scripts, tool-argument examples) -- an edge invisible to md-citation scanning.
The schema validates the record shape; audit.check_asset_dependencies_resolve
resolves every declared path (and every domain_skill tools[].tests path)
against the skill dir, then the nearest project root, FAILing per unresolved
path (steam-analysis stress-test gaps 5 + 7, Dec-18).
"""

import copy

import yaml

from skills_kit_lib.audit import (
    FAIL,
    PASS,
    audit,
    check_asset_dependencies_resolve,
)
from skills_kit_lib.schema_engine import validate
from skills_kit_lib.schema_registry import PORTABLE_UNIT_ROOTS, SCHEMAS_BY_ROOT
from skills_kit_lib.schemas.portable import ASSET_DEPENDENCIES_SCHEMA


MINIMAL_DEP = {
    "asset_dependencies": [
        {
            "path": ".claude/skills/other-skill/references/funnel.md",
            "consumer": "workflow.js",
            "purpose": "runtime input the workflow lanes read",
            "invariant": "output records mirror the funnel's output-schema section",
        },
    ]
}


def _body(data: dict) -> str:
    return "```yaml\n" + yaml.safe_dump(data, sort_keys=False) + "```\n"


def _fails(results):
    return [r for r in results if r.verdict == FAIL]


class TestSchema:
    def test_registered_as_portable_unit(self):
        assert "asset_dependencies" in PORTABLE_UNIT_ROOTS
        assert SCHEMAS_BY_ROOT["asset_dependencies"] is ASSET_DEPENDENCIES_SCHEMA

    def test_minimal_record_validates(self):
        fails, _ = validate(MINIMAL_DEP, ASSET_DEPENDENCIES_SCHEMA)
        assert fails == []

    def test_consumer_and_invariant_are_optional(self):
        data = copy.deepcopy(MINIMAL_DEP)
        del data["asset_dependencies"][0]["consumer"]
        del data["asset_dependencies"][0]["invariant"]
        fails, _ = validate(data, ASSET_DEPENDENCIES_SCHEMA)
        assert fails == []

    def test_path_and_purpose_are_required(self):
        for key in ("path", "purpose"):
            data = copy.deepcopy(MINIMAL_DEP)
            del data["asset_dependencies"][0][key]
            fails, _ = validate(data, ASSET_DEPENDENCIES_SCHEMA)
            assert fails, f"dropping {key} should fail validation"

    def test_empty_list_fails_min_len(self):
        fails, _ = validate({"asset_dependencies": []}, ASSET_DEPENDENCIES_SCHEMA)
        assert fails


class TestResolution:
    def _skill(self, tmp_path, deps_yaml: dict, project_marker: bool = True):
        """Create <root>/.claude/skills/consumer/SKILL.md declaring deps."""
        if project_marker:
            (tmp_path / ".git").mkdir()
        skill_dir = tmp_path / ".claude" / "skills" / "consumer"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: consumer\n---\n\n# consumer\n\n" + _body(deps_yaml),
            encoding="utf-8",
        )
        return skill_dir

    def test_project_root_relative_path_resolves(self, tmp_path):
        asset = tmp_path / ".claude" / "skills" / "other" / "references" / "funnel.md"
        asset.parent.mkdir(parents=True)
        asset.write_text("# funnel\n", encoding="utf-8")
        deps = {"asset_dependencies": [{
            "path": ".claude/skills/other/references/funnel.md",
            "purpose": "runtime input",
        }]}
        skill_dir = self._skill(tmp_path, deps)
        results = check_asset_dependencies_resolve(
            (skill_dir / "SKILL.md").read_text(encoding="utf-8"), skill_dir)
        assert not _fails(results)
        assert any(r.verdict == PASS for r in results)

    def test_skill_dir_relative_path_resolves(self, tmp_path):
        deps = {"asset_dependencies": [{
            "path": "scripts/helper.py",
            "purpose": "bundled helper",
        }]}
        skill_dir = self._skill(tmp_path, deps)
        (skill_dir / "scripts").mkdir()
        (skill_dir / "scripts" / "helper.py").write_text("", encoding="utf-8")
        results = check_asset_dependencies_resolve(
            (skill_dir / "SKILL.md").read_text(encoding="utf-8"), skill_dir)
        assert not _fails(results)

    def test_missing_asset_fails(self, tmp_path):
        deps = {"asset_dependencies": [{
            "path": ".claude/skills/other/references/renamed-away.md",
            "purpose": "runtime input",
        }]}
        skill_dir = self._skill(tmp_path, deps)
        results = check_asset_dependencies_resolve(
            (skill_dir / "SKILL.md").read_text(encoding="utf-8"), skill_dir)
        fails = _fails(results)
        assert len(fails) == 1
        assert "does not resolve" in fails[0].note

    def test_nested_inside_domain_skill_unit_is_collected(self, tmp_path):
        nested = {
            "domain_skill": {
                "identity": "Domain X.",
                "companions": {"siblings": []},
                "scope": {"covers": ["x"], "excludes": ["y"]},
                "orientation": {"summary": "X."},
                "index": {"references": [{
                    "id": "r1", "path": "references/x.md",
                    "keywords": ["a", "b", "c"], "summary": "X ref.",
                }]},
                "asset_dependencies": [{
                    "path": "missing/asset.md",
                    "purpose": "runtime input",
                }],
            }
        }
        skill_dir = self._skill(tmp_path, nested)
        results = check_asset_dependencies_resolve(
            (skill_dir / "SKILL.md").read_text(encoding="utf-8"), skill_dir)
        assert _fails(results)

    def test_tools_tests_path_is_resolved(self, tmp_path):
        data = {
            "domain_skill": {
                "identity": "Domain X.",
                "companions": {"siblings": []},
                "scope": {"covers": ["x"], "excludes": ["y"]},
                "orientation": {"summary": "X."},
                "index": {"references": [{
                    "id": "r1", "path": "references/x.md",
                    "keywords": ["a", "b", "c"], "summary": "X ref.",
                }]},
                "tools": [{
                    "name": "workflow",
                    "command": "node workflow.js",
                    "description": "the engine",
                    "tests": "scripts/test_workflow.py",
                }],
            }
        }
        skill_dir = self._skill(tmp_path, data)
        body = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        # Missing test file -> FAIL
        results = check_asset_dependencies_resolve(body, skill_dir)
        assert _fails(results)
        # Present test file -> PASS
        (skill_dir / "scripts").mkdir()
        (skill_dir / "scripts" / "test_workflow.py").write_text("", encoding="utf-8")
        results = check_asset_dependencies_resolve(body, skill_dir)
        assert not _fails(results)

    def test_plugin_root_placeholder_is_stripped(self, tmp_path):
        deps = {"asset_dependencies": [{
            "path": "${CLAUDE_PLUGIN_ROOT}/scripts/helper.py",
            "purpose": "bundled helper",
        }]}
        skill_dir = self._skill(tmp_path, deps)
        (skill_dir / "scripts").mkdir()
        (skill_dir / "scripts" / "helper.py").write_text("", encoding="utf-8")
        results = check_asset_dependencies_resolve(
            (skill_dir / "SKILL.md").read_text(encoding="utf-8"), skill_dir)
        assert not _fails(results)

    def test_no_declarations_emits_nothing(self, tmp_path):
        skill_dir = self._skill(tmp_path, {"references": [{
            "id": "r1", "path": "references/x.md",
            "keywords": ["a", "b", "c"], "summary": "X ref.",
        }]})
        results = check_asset_dependencies_resolve(
            (skill_dir / "SKILL.md").read_text(encoding="utf-8"), skill_dir)
        assert results == []


class TestAuditIntegration:
    def test_full_audit_reports_unresolved_asset(self, tmp_path):
        (tmp_path / ".git").mkdir()
        skill_dir = tmp_path / ".claude" / "skills" / "consumer"
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(
            "---\nname: consumer\ndescription: Use when testing. Do NOT use otherwise.\n---\n\n"
            "# consumer\n\n" + _body(MINIMAL_DEP),
            encoding="utf-8",
        )
        report = audit(skill_md)
        rows = report["yaml_contract"]
        assert any(
            r["verdict"] == FAIL and "does not resolve" in r["note"] for r in rows
        )
