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


class TestNoAccidentalTaggedTemplates:
    """A backtick inside a prompt template literal makes the lane UNRUNNABLE.

    The prose in these lanes quotes field names, and a writer reaching for
    markdown backticks (``the `why` field``) inside a template literal ends the
    template early. The result parses in node as a TAGGED TEMPLATE on an
    undefined identifier, so `node --check` passes and review-by-reading passes,
    while the Workflow tool's parser rejects the script outright and the lane
    dispatches zero agents. coverage-detect.js shipped that way in skills-kit
    0.47.0 and was caught only by a real dispatch (fixed 0.47.1).

    The detectable shape is the WRITER'S mistake, not the parse result: a
    markdown-style backtick-quoted bare word outside a comment. Ordinary
    template code never contains one -- an interpolation starts `${`, and a
    template's own closing backtick is not preceded by one on the same side.
    Quoting a field name in prompt prose is the only way this appears, and it
    is always wrong there: use plain quotes instead.
    """

    WORKFLOW_DIR = (
        REPO_ROOT / "plugins" / "skills-kit" / "skills" / "md-domain" / "workflow"
    )
    TAGGED = __import__("re").compile(r"`[A-Za-z_$][A-Za-z0-9_$.]*`")

    def _strip_comments(self, text: str) -> str:
        out, in_block = [], False
        for line in text.splitlines():
            stripped = line.lstrip()
            if in_block:
                if "*/" in line:
                    in_block = False
                out.append("")
                continue
            if stripped.startswith("//"):
                out.append("")
                continue
            if stripped.startswith("/*"):
                in_block = "*/" not in line
                out.append("")
                continue
            out.append(line)
        return "\n".join(out)

    def test_no_workflow_script_has_a_tagged_template(self):
        offenders = []
        for path in sorted(self.WORKFLOW_DIR.glob("*.js")):
            body = self._strip_comments(path.read_text(encoding="utf-8"))
            for n, line in enumerate(body.splitlines(), start=1):
                if self.TAGGED.search(line):
                    offenders.append(f"{path.name}:{n}: {line.strip()}")
        assert offenders == [], (
            "unescaped backtick inside a template literal -- the lane will not "
            "parse in the Workflow tool even though node accepts it:\n"
            + "\n".join(offenders)
        )
