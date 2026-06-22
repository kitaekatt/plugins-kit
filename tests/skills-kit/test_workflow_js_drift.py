"""Drift test for the workflow JS canonical template (arch-review S4).

The remediate.js files (claude-md-audit / skill-audit / project-doc-audit /
references-audit) are generated from the canonical template + per-skill fragments in
plugins/skills-kit/scripts/gen_workflow_js.py. This test asserts the shipped
files are byte-identical to the rendered template, and that the detect/classify
scripts still carry the shared skeleton chunks verbatim -- the
bootstrap_guard-style guard against copy-paste drift.

To change a remediate.js: edit the template/fragments in gen_workflow_js.py,
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
        for skill, path in gen.remediate_targets().items():
            rendered = gen.render_remediate(skill)
            on_disk = path.read_text(encoding="utf-8")
            assert on_disk == rendered, (
                f"{path} drifted from the canonical template in "
                f"gen_workflow_js.py -- edit the template/fragments and "
                f"regenerate (do not hand-edit the .js)"
            )

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
