"""md-domain lane-prompt helper: scripts/lane-prompt.cjs.

A background review runner is a subagent and has no Workflow tool, so it cannot
get a detect script's instantiated lane prompt by running the script. The helper
runs the REAL shipped detect script against a stub agent() and prints the prompt.
These tests prove the printed prompt is the one the Workflow path would send: an
independent harness (below, sharing no code with the helper) drives the same
script with the same args and records what agent() receives for the first
`laneModels.run` entry, which is the dispatch the Workflow tool makes first.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "plugins" / "skills-kit" / "skills" / "md-domain"
WORKFLOW_DIR = SKILL_DIR / "workflow"
HELPER = SKILL_DIR / "scripts" / "lane-prompt.cjs"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

# Records every agent() call as the Workflow tool would see it. Independent of the
# helper: it never dedupes and never touches the helper's code path.
HARNESS = r"""
const fs = require('fs')
const src = fs.readFileSync(process.argv[2], 'utf8').replace('export const meta', 'const meta')
const args = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'))
const calls = []
const agent = (prompt, opts) => {
  calls.push({ model: opts.model, prompt, schema: opts.schema })
  return Promise.resolve(null)
}
const parallel = (thunks) => Promise.all(thunks.map((t) => Promise.resolve().then(t)))
const fn = new Function('args', 'agent', 'parallel', 'phase', 'log',
  '"use strict"; return (async () => {\n' + src + '\n})()')
fn(args, agent, parallel, () => {}, () => {}).then(() => console.log(JSON.stringify(calls)))
"""

LANE_MODELS = {"run": [{"id": "sonnet", "effort": "low"}, {"id": "haiku", "effort": "low"}],
               "dropped": []}
LANES = {
    "claude-md": "claude-md-detect.js",
    "skill": "skill-detect.js",
    "project-doc": "project-doc-detect.js",
}


def _files(lane: str) -> list[dict]:
    # One plain file and one carrying every optional input a review run passes.
    plain = {"path": "/w/one.md", "preImagePath": None, "standardsPaths": []}
    rich = {
        "path": "/w/sub/two.md",
        "preImagePath": "/tmp/pre/two.md",
        "standardsPaths": ["/w/.claude/extra-standards.md"],
        "ancestorClaudeMdPaths": ["/w/sub/CLAUDE.md", "/w/CLAUDE.md"],
        "mechanicalScan": {"file": "sub/two.md", "checks_run": ["ascii"],
                           "findings": [{"line": 3, "check": "ascii", "detail": "non-ascii"}]},
    }
    if lane == "claude-md":
        plain.update(role="root", dimension="classic", parentPath=None)
        rich.update(role="child", dimension="classic", parentPath="/w/CLAUDE.md",
                    parentPreImagePath=None)
    return [plain, rich]


def _args(lane: str) -> dict:
    return {
        "files": _files(lane),
        "review": True,
        "disabledCriteria": ["some_optional_rule"],
        "mechanicalCheckPhrases": {"ascii": "non-ASCII characters"},
        "refs": {"criteria": "/s/criteria.md", "codeDirFilter": "/s/criteria.md",
                 "densityCriteria": "/s/criteria.md", "pluginRoot": "/s",
                 "venvPython": "/s/python"},
        "laneModels": LANE_MODELS,
    }


def _write(tmp_path: Path, args: dict, name: str = "args.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(args), encoding="utf-8")
    return path


def _helper(*cli: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([NODE, str(HELPER), *cli], input=stdin, capture_output=True,
                          text=True, encoding="utf-8", timeout=60)


def _workflow_dispatches(tmp_path: Path, script: str, args_path: Path) -> list[dict]:
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    done = subprocess.run([NODE, str(harness), str(WORKFLOW_DIR / script), str(args_path)],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    calls = json.loads(done.stdout)
    first = LANE_MODELS["run"][0]["id"]
    return [c for c in calls if c["model"] == first]


@pytest.mark.parametrize("lane", sorted(LANES))
def test_helper_prompt_equals_what_the_workflow_path_sends(tmp_path, lane):
    args_path = _write(tmp_path, _args(lane))
    expected = _workflow_dispatches(tmp_path, LANES[lane], args_path)
    assert len(expected) == 2

    done = _helper("--lane", lane, "--args-file", str(args_path))
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)

    assert out["script"] == LANES[lane]
    assert out["run"] == LANE_MODELS["run"]
    assert [lane_row["file"] for lane_row in out["lanes"]] == ["/w/one.md", "/w/sub/two.md"]
    for got, want in zip(out["lanes"], expected):
        text = got["dispatchPrompt"]
        # the lane prompt is byte-for-byte what agent() receives ...
        assert text[: got["lanePromptLength"]] == want["prompt"]
        # ... followed by the script's exact schema, and nothing else
        tail = text[got["lanePromptLength"]:]
        assert tail.endswith(json.dumps(want["schema"], separators=(",", ":")))
        assert "JSON object" in tail

    # the inputs the review passes reach the prompt, so equality is not vacuous
    rich = out["lanes"][1]["dispatchPrompt"]
    assert "/tmp/pre/two.md" in rich and "/w/CLAUDE.md" in rich
    assert "/w/.claude/extra-standards.md" in rich and "some_optional_rule" in rich
    assert "Target: /w/one.md" in out["lanes"][0]["dispatchPrompt"] or \
        "/w/one.md" in out["lanes"][0]["dispatchPrompt"]


def test_stdin_args_and_out_dir_carry_the_same_prompt(tmp_path):
    args = _args("project-doc")
    via_file = json.loads(_helper("--lane", "project-doc", "--args-file",
                                  str(_write(tmp_path, args))).stdout)
    via_stdin = _helper("--lane", "project-doc", stdin=json.dumps(args))
    assert via_stdin.returncode == 0, via_stdin.stderr
    assert json.loads(via_stdin.stdout) == via_file

    out_dir = tmp_path / "prompts"
    done = _helper("--lane", "project-doc", "--args-file", str(_write(tmp_path, args)),
                   "--out-dir", str(out_dir))
    assert done.returncode == 0, done.stderr
    manifest = json.loads(done.stdout)
    for got, want in zip(manifest["lanes"], via_file["lanes"]):
        assert "dispatchPrompt" not in got
        assert Path(got["promptFile"]) == (out_dir / f"{got['index']}.prompt.txt").resolve()
        assert Path(got["promptFile"]).read_text(encoding="utf-8") == want["dispatchPrompt"]


def test_failures_are_loud_and_print_no_json(tmp_path):
    args = _args("skill")
    no_route = {k: v for k, v in args.items() if k != "laneModels"}
    cases = [
        (["--lane", "nope", "--args-file", str(_write(tmp_path, args))], "unknown lane"),
        (["--args-file", str(_write(tmp_path, args))], "--lane is required"),
        (["--lane", "skill", "--bogus"], "unknown argument"),
        (["--lane", "skill", "--args-file", str(tmp_path / "missing.json")], "cannot read args"),
        (["--lane", "skill", "--args-file", str(_write(tmp_path, no_route, "no_route.json"))], "laneModels"),
        (["--lane", "skill", "--args-file", str(_write(tmp_path, {"files": []}, "no_files.json"))], "args.files"),
    ]
    for cli, reason in cases:
        done = _helper(*cli)
        assert done.returncode == 1, (cli, done.stdout, done.stderr)
        assert done.stdout == "", cli
        assert reason in done.stderr, (cli, done.stderr)
