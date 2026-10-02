"""Runtime tests for the md-domain lane route (LANE_ROUTE_CHUNK).

Every md-domain workflow script that dispatches an agent takes a REQUIRED
args.laneModels = {run, dropped} and dispatches through laneAgent, which tries
the run entries in order. These tests execute the REAL shipped scripts under
node with a stub agent() that records the model and effort it was handed, so
they observe what the Workflow tool would receive -- not the source text.

Harness pattern: tests/bootstrap/code_review/test_md_domain_standards_dispatch.py.
The stub parallel() follows the Workflow contract: a thunk that throws resolves
to null and the call itself never rejects.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / "plugins" / "skills-kit" / "skills" / "md-domain" / "workflow"
GEN_PATH = REPO_ROOT / "plugins" / "skills-kit" / "scripts" / "gen_workflow_js.py"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

_spec = importlib.util.spec_from_file_location("gen_workflow_js_route", GEN_PATH)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

# behaviour: {model id: "ok" | "throw" | "null"}; an id absent from the map is "ok".
HARNESS = r"""
const fs = require('fs')
const src = fs.readFileSync(process.argv[2], 'utf8').replace('export const meta', 'const meta')
const spec = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'))
const calls = []
const logs = []
const agent = (prompt, opts) => {
  calls.push({ model: opts && opts.model, effort: opts && opts.effort, label: opts && opts.label })
  const how = (spec.behaviour || {})[opts && opts.model] || 'ok'
  if (how === 'throw') return Promise.reject(new Error('stub failure for ' + opts.model))
  if (how === 'null') return Promise.resolve(null)
  return Promise.resolve(spec.reply)
}
const parallel = (thunks) => Promise.all(thunks.map((t) => Promise.resolve().then(t).catch(() => null)))
const noop = () => {}
const log = (m) => { logs.push(String(m)) }
const fn = new Function('args', 'agent', 'parallel', 'phase', 'log',
  '"use strict"; return (async () => {\n' + src + '\n})()')
Promise.resolve()
  .then(() => fn(spec.args, agent, parallel, noop, log))
  .then((result) => console.log(JSON.stringify({ calls, logs, result, error: null })))
  .catch((e) => console.log(JSON.stringify({ calls, logs, result: null, error: String((e && e.message) || e) })))
"""

SONNET_LOW = {"run": [{"id": "sonnet", "effort": "low"}], "dropped": []}

DETECT_REPLY = {"findings": [], "verdict": "COMPLIANT"}
REMEDIATE_REPLY = {"applied": 1, "skipped": 0, "failed": 0, "actions": []}


def _detect_args(tmp_path: Path, lane: str) -> dict:
    subject = tmp_path / ("CLAUDE.md" if lane == "claude-md-detect.js" else "doc.md")
    subject.write_text("# Title\n\nBody.\n", encoding="utf-8")
    entry = {"path": str(subject), "preImagePath": None, "standardsPaths": []}
    if lane == "claude-md-detect.js":
        entry.update(role="root", dimension="classic", parentPath=None)
    return {
        "files": [entry],
        "disabledCriteria": [],
        "mechanicalCheckPhrases": {},
        "review": False,
        "refs": {"pluginRoot": str(tmp_path), "venvPython": str(tmp_path / "python")},
    }


def _remediate_args(lane: str) -> dict:
    if lane == "references-remediate.js":
        item = {"category": "A_renamed", "bucket": "FIX", "line": 1, "before": "a",
                "after": "b", "instruction": "", "decision": "apply"}
        per_file = [{"file": "/x/doc.md", "edits": [item]}]
    else:
        item = {"criterion": "A-1", "taxonomy": "A", "bucket": "FIX", "line": 1,
                "instruction": "fix it", "decision": "apply"}
        per_file = [{"path": "/x/doc.md", "role": "root", "remediations": [item]}]
    return {"perFile": per_file, "fixMode": "apply"}


def _classify_args() -> dict:
    return {
        "files": [{"file": "/x/doc.md",
                   "findings": [{"severity": "ERROR", "line": 1, "ref": "gone-skill"}]}],
        "refs": {"standardsDoc": "/x/references-standards.md"},
    }


DETECT_LANES = ["claude-md-detect.js", "skill-detect.js", "project-doc-detect.js"]
REMEDIATE_LANES = ["claude-md-remediate.js", "skill-remediate.js",
                   "project-doc-remediate.js", "references-remediate.js"]
# Every md-domain workflow script that dispatches an agent.
ALL_LANES = DETECT_LANES + REMEDIATE_LANES + [
    "references-classify.js", "coverage-detect.js", "claude-md-generate.js"]


def _args_for(tmp_path: Path, lane: str) -> tuple[dict, object]:
    if lane in DETECT_LANES:
        return _detect_args(tmp_path, lane), DETECT_REPLY
    if lane in REMEDIATE_LANES:
        return _remediate_args(lane), REMEDIATE_REPLY
    if lane == "references-classify.js":
        return _classify_args(), {"findings": []}
    if lane == "coverage-detect.js":
        subject = {"root": "src", "codeFiles": ["src/a.py"], "ambientClaudeMdPaths": [],
                   "rootExclusion": None, "skipped": [], "unknownExtensions": {}}
        return {"subjects": [subject], "depth": "basic", "disabledCriteria": [],
                "refs": {"criteria": "/x/c.md", "observationKinds": "/x/o.md",
                         "pluginRoot": "/x"}}, {"subjects": []}
    if lane == "claude-md-generate.js":
        # A composition-only subject: no coverage report by design, so one
        # compose dispatch runs and takes the null branch.
        reply = {"written": False, "writtenFalseReason": "null-branch", "path": "",
                 "sections": [], "candidatesRead": 0, "candidateDispositions": [],
                 "droppedCandidates": [], "verifications": [], "hoists": [],
                 "candidateHoists": [], "notProposed": [], "potentialDefects": [],
                 "notes": []}
        return {"subjects": [{"root": "src", "compositionOnly": True}],
                "refs": {"standards": "/x/s.md", "lane": "/x/l.md",
                         "placement": "/x/p.md"}}, reply
    raise AssertionError(lane)


def _run(tmp_path: Path, lane: str, args: dict, reply: object, behaviour: dict | None = None) -> dict:
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"args": args, "reply": reply, "behaviour": behaviour or {}}),
                    encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(harness), str(WORKFLOW_DIR / lane), str(spec)],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("lane", ALL_LANES)
def test_shipped_route_dispatches_sonnet_low(tmp_path: Path, lane: str) -> None:
    args, reply = _args_for(tmp_path, lane)
    args["laneModels"] = SONNET_LOW
    out = _run(tmp_path, lane, args, reply)
    assert out["error"] is None, out
    assert out["calls"], out
    assert {(c["model"], c["effort"]) for c in out["calls"]} == {("sonnet", "low")}
    routes = out["result"]["routes"]
    assert routes["dropped"] == []
    assert [r["used"] for r in routes["perFile"]] == [{"id": "sonnet", "effort": "low"}]
    assert routes["perFile"][0]["failed"] == []


@pytest.mark.parametrize("behaviour", ["throw", "null"])
@pytest.mark.parametrize("lane", ["skill-detect.js", "skill-remediate.js", "references-classify.js"])
def test_failover_moves_to_the_next_run_entry(tmp_path: Path, lane: str, behaviour: str) -> None:
    args, reply = _args_for(tmp_path, lane)
    args["laneModels"] = {"run": [{"id": "opus", "effort": "high"},
                                  {"id": "sonnet", "effort": "low"}], "dropped": []}
    out = _run(tmp_path, lane, args, reply, behaviour={"opus": behaviour})
    assert out["error"] is None, out
    assert [(c["model"], c["effort"]) for c in out["calls"]] == [("opus", "high"), ("sonnet", "low")]
    (route,) = out["result"]["routes"]["perFile"]
    assert route["used"] == {"id": "sonnet", "effort": "low"}
    assert [(f["id"], f["effort"]) for f in route["failed"]] == [("opus", "high")]


def test_every_entry_failing_yields_a_null_lane_and_a_recorded_route(tmp_path: Path) -> None:
    args, reply = _args_for(tmp_path, "skill-detect.js")
    args["laneModels"] = {"run": [{"id": "opus", "effort": "high"},
                                  {"id": "sonnet", "effort": "low"}], "dropped": []}
    out = _run(tmp_path, "skill-detect.js", args, reply,
               behaviour={"opus": "throw", "sonnet": "null"})
    assert out["error"] is None, out
    assert out["result"]["perFile"] == []
    (route,) = out["result"]["routes"]["perFile"]
    assert route["used"] is None
    assert [f["reason"] for f in route["failed"]] == [
        "stub failure for opus", "agent() returned nothing"]
    assert any("route exhausted" in line for line in out["logs"]), out["logs"]


@pytest.mark.parametrize("lane", ALL_LANES)
def test_missing_lane_models_throws_before_any_dispatch(tmp_path: Path, lane: str) -> None:
    args, reply = _args_for(tmp_path, lane)
    out = _run(tmp_path, lane, args, reply)
    assert out["calls"] == [], out
    assert "requires args.laneModels" in (out["error"] or ""), out


@pytest.mark.parametrize("lane", ALL_LANES)
def test_a_non_core_run_id_throws_before_any_dispatch(tmp_path: Path, lane: str) -> None:
    args, reply = _args_for(tmp_path, lane)
    args["laneModels"] = {"run": [{"id": "luna", "effort": "high"}], "dropped": []}
    out = _run(tmp_path, lane, args, reply)
    assert out["calls"] == [], out
    assert "agent() cannot run" in (out["error"] or ""), out


@pytest.mark.parametrize("lane", ["claude-md-detect.js", "project-doc-remediate.js"])
def test_a_luna_first_declaration_reports_the_drop(tmp_path: Path, lane: str) -> None:
    # What agent_route makes of [{luna, high}, {sonnet, low}]: luna dropped,
    # sonnet run. The lane runs sonnet only and carries the drop to its caller.
    dropped = [{"id": "luna", "effort": "high", "reason": "not runnable by agent()/Agent"}]
    args, reply = _args_for(tmp_path, lane)
    args["laneModels"] = {"run": [{"id": "sonnet", "effort": "low"}], "dropped": dropped}
    out = _run(tmp_path, lane, args, reply)
    assert out["error"] is None, out
    assert {c["model"] for c in out["calls"]} == {"sonnet"}
    assert out["result"]["routes"]["dropped"] == dropped
    assert any("dropped luna (high)" in line and "running sonnet (low)" in line
               for line in out["logs"]), out["logs"]


def test_propose_mode_still_requires_and_reports_the_route(tmp_path: Path) -> None:
    args, reply = _args_for(tmp_path, "skill-remediate.js")
    args["fixMode"] = "propose"
    args["laneModels"] = SONNET_LOW
    out = _run(tmp_path, "skill-remediate.js", args, reply)
    assert out["error"] is None and out["calls"] == [], out
    assert out["result"]["routes"] == {"dropped": [], "perFile": []}


@pytest.mark.parametrize("lane", ALL_LANES)
def test_every_dispatch_goes_through_lane_agent(lane: str) -> None:
    """agent( appears only inside the shared chunk; every call site is laneAgent."""
    text = (WORKFLOW_DIR / lane).read_text(encoding="utf-8")
    assert gen.LANE_ROUTE_CHUNK in text
    outside = text.replace(gen.LANE_ROUTE_CHUNK, "")
    code = "\n".join(line for line in outside.splitlines()
                     if not line.lstrip().startswith("//"))
    # Prompt prose mentions agent() inside template strings; count only call
    # sites shaped like a dispatch: agent( followed by a prompt builder or name.
    offenders = [m.group(0) for m in re.finditer(r"(?<![A-Za-z0-9_$.])agent\(\s*[A-Za-z_]", code)]
    assert offenders == [], offenders
    assert "laneAgent(" in code
