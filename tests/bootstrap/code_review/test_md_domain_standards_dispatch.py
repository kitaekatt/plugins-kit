"""The review's md-domain pass resolves the run's standards and threads them.

Every md-domain detect lane (skills-kit `workflow/*-detect.js`) throws before
dispatching any agent unless `args.disabledCriteria` is a string[] -- the
`disabled` list from skills-kit's `scripts/resolve_standards.py`. The code-review
kits dispatch three of those lanes from references/md-domain-review.md, so the
rendered reference must (a) run the resolver once, under the skills-kit venv
interpreter, (b) pass `disabledCriteria` in EVERY lane's args, and (c) report the
claimed files incomplete -- never substitute `[]` -- when the resolver fails.

These assertions read the RENDERED files, not the generator: a property that a
regeneration could remove from both sides is invisible to the byte-identity
drift guard (root CLAUDE.md insight guard_cannot_see_its_own_subject). The
phrases are typed here on purpose.

The node test then drives each real detect lane with args built from the keys
the rendered reference documents, showing the documented shape passes the
lane's guard and reaches agent dispatch, and that dropping the key does not.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
REFERENCES = (
    REPO_ROOT / "plugins/git-kit/skills/git-code-review/references/md-domain-review.md",
    REPO_ROOT / "plugins/p4-kit/skills/p4-code-review/references/md-domain-review.md",
)
SKILLS = (
    REPO_ROOT / "plugins/git-kit/skills/git-code-review/SKILL.md",
    REPO_ROOT / "plugins/p4-kit/skills/p4-code-review/SKILL.md",
)
WORKFLOW_DIR = REPO_ROOT / "plugins/skills-kit/skills/md-domain/workflow"

RESOLVER_COMMAND = (
    '"<venvPython>" "<root>/scripts/resolve_standards.py" --project-root "<project root>"'
)

# `script` = the text of `<root>/.../<lane>-detect.js`, `args` = `{ ... }`
LANE_ARGS = re.compile(
    r"workflow/(?P<lane>[\w-]+-detect\.js)`, `args` = `(?P<args>\{.*?\} \})`"
)


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def _lane_args(path: Path) -> dict[str, str]:
    return {m["lane"]: m["args"] for m in LANE_ARGS.finditer(_flat(path))}


def _top_level_keys(args_text: str) -> list[str]:
    """Top-level keys of a documented `{ k: v, ... }` args template."""
    inner = args_text.strip()[1:-1]
    keys, depth, token = [], 0, ""
    for ch in inner:
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        if ch == "," and depth == 0:
            keys.append(token)
            token = ""
        else:
            token += ch
    keys.append(token)
    return [k.split(":", 1)[0].strip() for k in keys if k.strip()]


@pytest.mark.parametrize("path", REFERENCES, ids=("git", "p4"))
def test_every_detect_lane_receives_disabled_criteria(path: Path) -> None:
    lanes = _lane_args(path)
    assert set(lanes) == {"claude-md-detect.js", "skill-detect.js", "project-doc-detect.js"}
    for lane, args in lanes.items():
        assert "disabledCriteria" in _top_level_keys(args), lane


@pytest.mark.parametrize("path", REFERENCES, ids=("git", "p4"))
def test_reference_runs_the_resolver_once_under_the_skills_kit_venv(path: Path) -> None:
    body = _flat(path)
    assert RESOLVER_COMMAND in body
    assert "Resolve it ONCE per review" in body
    assert "`disabledCriteria` = `disabled`" in body
    # The interpreter is the skills-kit venv, never a substitute.
    command_line = next(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if "resolve_standards.py" in line and "--project-root" in line
    )
    for forbidden in ("BOOTSTRAP_PYTHON", "uv run", "CLAUDE_PLUGIN_ROOT"):
        assert forbidden not in command_line


@pytest.mark.parametrize("path", REFERENCES, ids=("git", "p4"))
def test_resolver_failure_reports_incomplete_and_never_defaults(path: Path) -> None:
    body = _flat(path)
    assert "Do NOT substitute `[]`" in body
    assert (
        "`REVIEW INCOMPLETE: <file> - resolve_standards.py exited <code>: <stderr line>`"
        in body
    )
    assert "Run no detect lane, keep the files claimed" in body


@pytest.mark.parametrize("path", REFERENCES, ids=("git", "p4"))
def test_manual_invocation_carries_disabled_criteria(path: Path) -> None:
    body = _flat(path)
    assert "the top-level `disabledCriteria` and `mechanicalCheckPhrases`" in body


@pytest.mark.parametrize("path", SKILLS, ids=("git", "p4"))
def test_skill_body_requires_the_resolver_and_disabled_criteria(path: Path) -> None:
    body = _flat(path)
    assert "`scripts/resolve_standards.py` ONCE per review" in body
    assert "pass its `disabled` list as `disabledCriteria` in EVERY lane args object" in body
    assert "A non-zero exit is never replaced by `[]`" in body


# ---------------------------------------------------------------------------
# Integration: the documented args shape passes each real lane's guard.

NODE = shutil.which("node")

HARNESS = r"""
const fs = require('fs')
const src = fs.readFileSync(process.argv[2], 'utf8').replace('export const meta', 'const meta')
const args = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'))
let calls = 0
const agent = () => { calls += 1; return Promise.resolve({}) }
const parallel = (thunks) => Promise.all(thunks.map((t) => t()))
const noop = () => {}
const fn = new Function('args', 'agent', 'parallel', 'phase', 'log',
  '"use strict"; return (async () => {\n' + src + '\n})()')
Promise.resolve()
  .then(() => fn(args, agent, parallel, noop, noop))
  .then(() => console.log(JSON.stringify({ calls, error: null })))
  .catch((e) => console.log(JSON.stringify({ calls, error: String((e && e.message) || e) })))
"""

GUARD_ERROR = "requires args.disabledCriteria"


def _run_lane(tmp_path: Path, lane: str, args: dict) -> dict:
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    spec = tmp_path / f"{lane}.args.json"
    spec.write_text(json.dumps(args), encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(harness), str(WORKFLOW_DIR / lane), str(spec)],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _documented_args(tmp_path: Path, lane: str, keys: list[str]) -> dict:
    subject = tmp_path / ("CLAUDE.md" if lane == "claude-md-detect.js" else "doc.md")
    subject.write_text("# Title\n\nBody.\n", encoding="utf-8")
    file_entry = {"path": str(subject), "preImagePath": None, "standardsPaths": []}
    if lane == "claude-md-detect.js":
        file_entry.update(role="root", dimension="classic", parentPath=None)
    values = {
        "files": [file_entry],
        "disabledCriteria": [],
        "mechanicalCheckPhrases": {},
        "review": True,
        "refs": {"pluginRoot": str(tmp_path), "venvPython": str(tmp_path / "python")},
    }
    assert set(keys) <= set(values), keys
    return {k: values[k] for k in keys}


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("lane", ["claude-md-detect.js", "skill-detect.js", "project-doc-detect.js"])
def test_documented_args_pass_the_real_lane_guard(tmp_path: Path, lane: str) -> None:
    keys = _top_level_keys(_lane_args(REFERENCES[0])[lane])
    args = _documented_args(tmp_path, lane, keys)

    out = _run_lane(tmp_path, lane, args)
    assert out["error"] is None or GUARD_ERROR not in out["error"], out
    assert out["calls"] >= 1, out

    # Counterfactual: the same args without the key never reach an agent.
    del args["disabledCriteria"]
    out = _run_lane(tmp_path, lane, args)
    assert out["calls"] == 0 and GUARD_ERROR in (out["error"] or ""), out
