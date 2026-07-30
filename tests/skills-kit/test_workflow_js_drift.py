"""Drift test for the workflow JS canonical template (arch-review S4).

The md-domain remediate lanes (skill-remediate.js / claude-md-remediate.js /
project-doc-remediate.js / references-remediate.js, all under
plugins/skills-kit/skills/md-domain/workflow/) are generated from the canonical
template + per-lane fragments in plugins/skills-kit/scripts/gen_workflow_js.py.
This test asserts the shipped files are byte-identical to the rendered template,
and that the detect/classify lanes still carry the shared skeleton chunks
verbatim -- the bootstrap_guard-style guard against copy-paste drift.

To change a remediate lane: edit the template/fragments in gen_workflow_js.py,
run `uv run python plugins/skills-kit/scripts/gen_workflow_js.py`, commit both.
"""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GEN_PATH = REPO_ROOT / "plugins" / "skills-kit" / "scripts" / "gen_workflow_js.py"

_spec = importlib.util.spec_from_file_location("gen_workflow_js", GEN_PATH)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


class TestRemediateTemplateDrift:
    def test_every_remediate_js_matches_rendered_template(self):
        for lane, path in gen.remediate_targets().items():
            rendered = gen.render_remediate(lane)
            on_disk = path.read_text(encoding="utf-8")
            assert on_disk == rendered, (
                f"{path} drifted from the canonical template in "
                f"gen_workflow_js.py -- edit the template/fragments and "
                f"regenerate (do not hand-edit the .js)"
            )

    def test_lane_roster_is_the_four_md_domain_lanes(self):
        assert set(gen.remediate_targets()) == {
            "audit_skill",
            "audit_claude_md",
            "audit_project_doc",
            "audit_references",
        }

    def test_targets_exist(self):
        for path in gen.remediate_targets().values():
            assert path.is_file(), f"missing workflow script: {path}"


class TestDetectClassifySharedSkeleton:
    def test_shared_chunks_present_verbatim(self):
        problems = gen.check_shared_chunks()
        assert problems == [], "\n".join(problems)


class TestCheckMode:
    def test_check_mode_passes_on_clean_tree(self):
        assert gen.check_remediate() == []
