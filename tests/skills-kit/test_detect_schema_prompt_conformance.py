"""Schema/prompt conformance for the md-domain audit detect lanes.

A StructuredOutput schema field marked `required` forces the model to emit a
value for it on every call. If the lane's own prompt text never tells the
model what that field means or how to derive it, the model has nothing to go
on but guessing -- and a guessed value can read as evidence downstream (an
orphan sentinel, a routing flag) when it is really noise.

This test parses each detect lane's FILE_FINDINGS_SCHEMA `required` list (the
file-level one, not the per-finding one nested under `findings.items`) and
asserts every name in it appears as a literal substring somewhere in the
`function lanePrompt(f) { ... }` body -- the text actually sent to the model.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = (
    REPO_ROOT / "plugins" / "skills-kit" / "skills" / "md-domain" / "workflow"
)

LANES = ["claude-md-detect.js", "skill-detect.js", "project-doc-detect.js"]


def _lane_prompt_body(text: str) -> str:
    start = text.index("function lanePrompt(f) {")
    end = text.index("\nphase(", start)
    return text[start:end]


def _file_level_required(text: str) -> list[str]:
    """The LAST `required: [...]` in the file is the file-level schema's --
    the per-finding one is nested earlier under `findings.items.properties`."""
    matches = re.findall(r"required:\s*\[([^\]]*)\]", text)
    assert matches, "no required: [...] list found"
    last = matches[-1]
    return [name.strip().strip("'\"") for name in last.split(",") if name.strip()]


class TestEveryRequiredFieldIsNamedInThePrompt:
    def test_lanes(self):
        problems = []
        for lane in LANES:
            path = WORKFLOW_DIR / lane
            text = path.read_text(encoding="utf-8")
            required = _file_level_required(text)
            prompt_body = _lane_prompt_body(text)
            for field in required:
                if field not in prompt_body:
                    problems.append(f"{lane}: required field {field!r} never named in lanePrompt()")
        assert problems == [], "\n".join(problems)


class TestInboundCitationsIsNullableNotFabricated:
    """project-doc-detect.js's inbound_citations OUTPUT field used to be a
    non-nullable required integer even though review mode's subject-lens call
    supplies no citer-scan signal at all -- forcing the model to fabricate a
    number, and a fabricated 0 reads as the orphan sentinel downstream."""

    PATH = WORKFLOW_DIR / "project-doc-detect.js"

    def test_schema_type_accepts_null(self):
        text = self.PATH.read_text(encoding="utf-8")
        m = re.search(r"inbound_citations:\s*\{[^}]*\}", text, re.S)
        assert m, "inbound_citations schema entry not found"
        assert "'integer', 'null'" in m.group(0) or '"integer", "null"' in m.group(0)

    def test_prompt_instructs_null_when_no_signal(self):
        text = self.PATH.read_text(encoding="utf-8")
        assert "inbound_citations: null" in text
        assert "do NOT fabricate a number" in text

    def test_never_guess_zero_is_stated(self):
        text = self.PATH.read_text(encoding="utf-8")
        assert "never guess 0" in text
