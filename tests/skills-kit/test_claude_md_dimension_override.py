"""Lane contract test for the claude-md-detect.js dimension override (I1 of
the audit/generation-pipeline slice).

f.dimension is a caller-supplied HINT, not a verified fact -- three
independent implementations of "is this CLAUDE.md code-directory" used to
disagree (this lane trusting f.dimension unvalidated, discover_claude_md.py's
classify_dimension, and evidence_pack's own now-retired regex). This test
extracts the module-level slice of claude-md-detect.js (`let input = args`
through the end of `function lanePrompt`) and evaluates it under Node with a
stub `args`, then calls `lanePrompt(f)` directly and inspects the rendered
prompt text -- the same approach test_workflow_js_drift.py and
test_not_audited_verdict.py use to read these files, extended to actually
RUN the pure, side-effect-free prompt builder (no top-level await / agent /
parallel needed for this slice).
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LANE_PATH = (
    REPO_ROOT / "plugins" / "skills-kit" / "skills" / "md-domain"
    / "workflow" / "claude-md-detect.js"
)

NODE = shutil.which("node")


def _lane_prompt_builder_slice() -> str:
    text = LANE_PATH.read_text(encoding="utf-8")
    start = text.index("let input = args")
    end = text.index("\nphase('Audit')", start)
    return text[start:end]


def _render_prompt(file_record: dict) -> str:
    """Run lanePrompt(file_record) under real Node and return the rendered
    prompt string."""
    assert NODE, "node is required for this lane contract test"
    slice_src = _lane_prompt_builder_slice()
    harness = (
        f"const args = {json.dumps({'files': [file_record]})};\n"
        + slice_src
        + "\nprocess.stdout.write(lanePrompt(args.files[0]))\n"
    )
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", harness],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


class TestDimensionIsOverriddenByTheContractBlock:
    def test_code_directory_hint_with_contract_block_body_reads_classic(self):
        body = "# Contract\n\nNever call `frobnicate()`.\n\nclaude_md:\n  scope: {}\n"
        prompt = _render_prompt({
            "path": "a/CLAUDE.md", "role": "child", "dimension": "code-directory",
            "body": body,
        })
        assert "Dimension: classic" in prompt
        # The code-directory CD-* instructions must not be the active branch.
        assert "This file is flagged `code-directory`" not in prompt
        assert "run the classic CCP/CRP/ADP/Hygiene/Schema criteria only" in prompt

    def test_code_directory_hint_without_body_still_carries_the_override_check(self):
        # The common real-world case: no f.body supplied. The lane cannot
        # resolve the override itself, so it must instruct the MODEL to.
        prompt = _render_prompt({
            "path": "a/CLAUDE.md", "role": "child", "dimension": "code-directory",
        })
        assert "DIMENSION CHECK" in prompt
        assert "claude_md:" in prompt
        assert "dimension override" in prompt

    def test_classic_hint_with_no_block_stays_classic(self):
        prompt = _render_prompt({
            "path": "a/CLAUDE.md", "role": "child", "dimension": "classic",
            "body": "# Plain\n\nSome prose.\n",
        })
        assert "Dimension: classic" in prompt
