"""The audit auto-edit seam: `audit.fix_mode: propose` makes the audit report FIX
findings as proposals and apply nothing.

Two layers, kept distinct:

- Config layer: the `audit:` block resolves, layers later-wins, and rejects an
  unknown key or value (resolve_standards JSON key `audit`).
- Behaviour layer, FAN-OUT path: the generated `workflow/<artifact>-remediate.js`
  scripts are executed under node with a stub `agent`. With `fixMode: "propose"`
  the stub must see ZERO dispatches and the target file's bytes must be
  unchanged; with `fixMode: "apply"` the stub is dispatched with the remediation
  list. `fixMode` is a REQUIRED input: an absent or unknown value throws before
  any dispatch, in every remediate lane, rather than reading as "apply".

The SINGLE-FILE path is an agent doing an inline Edit, with no code to execute;
it is guarded only by an instruction in audit-lane.md. The last test pins that
the instruction text exists -- it is NOT behavioural verification of that path.
"""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from skills_kit_lib import audit as audit_mod
from skills_kit_lib import standards_resolve as sr

REPO_ROOT = Path(__file__).resolve().parents[2]
MD_DOMAIN = REPO_ROOT / "plugins" / "skills-kit" / "skills" / "md-domain"
AUDIT_LANE_MD = MD_DOMAIN / "references" / "lanes" / "audit-lane.md"
SKILL_REMEDIATE = MD_DOMAIN / "workflow" / "skill-remediate.js"

NODE = shutil.which("node")

LONG_DESC = "Use when " + "auditing a thing that has a very long description " * 5 + "Do NOT use otherwise."

FIXTURE = f"""---
name: demo-skill
description: {LONG_DESC}
---

# Demo

Body.
"""

# Runs a workflow script the way the Workflow tool does: top-level await, the
# injected `args`, `agent`, `parallel`, `phase`, `log`, and a top-level return.
HARNESS = r"""
const fs = require('fs')
const [scriptPath, argsJson] = process.argv.slice(2)
const src = fs.readFileSync(scriptPath, 'utf8').replace(/^export const /gm, 'const ')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const calls = []
const run = new AsyncFunction('args', 'agent', 'parallel', 'phase', 'log', src)
run(
  JSON.parse(argsJson),
  async (prompt, opts) => {
    calls.push({ prompt, opts })
    return { path: 'x', applied: 1, skipped: 0, failed: 0, actions: [] }
  },
  (thunks) => Promise.all(thunks.map((t) => t())),
  () => {},
  () => {},
).then((result) => {
  console.log(JSON.stringify({ calls: calls.length, prompts: calls.map((c) => c.prompt), result }))
}).catch((e) => { console.log(JSON.stringify({ error: String(e) })); process.exit(1) })
"""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_lane(tmp_path: Path, args: dict, script: Path = SKILL_REMEDIATE,
              expect_ok: bool = True) -> dict:
    harness = tmp_path / "harness.cjs"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(harness), str(script), json.dumps(args)],
        capture_output=True, text=True, timeout=60,
    )
    if expect_ok:
        assert proc.returncode == 0, proc.stdout + proc.stderr
    else:
        assert proc.returncode != 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture
def fixture_skill(tmp_path):
    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    path = skill_dir / "SKILL.md"
    path.write_text(FIXTURE, encoding="utf-8", newline="\n")
    return path


def _args(path: Path, **extra) -> dict:
    item = {
        "criterion": "desc-160-char",
        "taxonomy": "A",
        "bucket": "FIX",
        "line": 3,
        "instruction": "Shorten the description to 160 characters or fewer.",
        "decision": "apply",
    }
    # laneModels is a required lane input: the resolved lane_models.remediate route.
    lane_models = {"run": [{"id": "sonnet", "effort": "low"}], "dropped": []}
    return {"perFile": [{"path": str(path), "remediations": [item]}],
            "laneModels": lane_models, **extra}


def _rows(node):
    """Every dict carrying a `rule` key anywhere in the audit report."""
    if isinstance(node, dict):
        if "rule" in node:
            yield node
        for v in node.values():
            yield from _rows(v)
    elif isinstance(node, list):
        for v in node:
            yield from _rows(v)


def test_fixture_really_carries_the_mechanical_fix_finding(fixture_skill):
    report = audit_mod.audit(fixture_skill)
    rows = [r for r in _rows(report) if r["rule"] == "desc-160-char"]
    assert rows, "fixture does not exercise desc-160-char; the seam test would be vacuous"
    assert any(str(r.get("status") or r.get("verdict")).lower() == "fail" for r in rows), rows


@pytest.mark.skipif(NODE is None, reason="node is required to execute the workflow lane")
class TestFanOutPathIsEnforcedByCode:
    def test_propose_mode_dispatches_nothing_and_leaves_the_file_alone(self, tmp_path, fixture_skill):
        before = _sha(fixture_skill)
        out = _run_lane(tmp_path, _args(fixture_skill, fixMode="propose"))
        assert out["calls"] == 0, "propose mode dispatched an agent"
        assert _sha(fixture_skill) == before
        assert out["result"]["summary"]["applied"] == 0
        assert out["result"]["summary"]["proposed"] == 1
        proposed = out["result"]["perFile"][0]["proposed"]
        assert proposed[0]["criterion"] == "desc-160-char"

    def test_apply_mode_dispatches_with_the_remediation_list(self, tmp_path, fixture_skill):
        out = _run_lane(tmp_path, _args(fixture_skill, fixMode="apply"))
        assert out["calls"] == 1
        assert "desc-160-char" in out["prompts"][0]
        assert str(fixture_skill) in out["prompts"][0]

    @pytest.mark.parametrize("lane", ["skill", "claude-md", "project-doc", "references"])
    @pytest.mark.parametrize("fix_mode", [None, "", "Apply", "never", True])
    def test_absent_or_unknown_fix_mode_throws_before_dispatch(
        self, tmp_path, fixture_skill, lane, fix_mode
    ):
        """An absent fixMode used to read as "apply" and edit the file; it must
        refuse instead, in every generated remediate lane."""
        before = _sha(fixture_skill)
        extra = {} if fix_mode is None else {"fixMode": fix_mode}
        args = _args(fixture_skill, **extra)
        if lane == "references":
            item = args["perFile"][0].pop("remediations")[0]
            args["perFile"][0] = {"file": str(fixture_skill), "edits": [item]}
        out = _run_lane(
            tmp_path, args,
            script=MD_DOMAIN / "workflow" / f"{lane}-remediate.js",
            expect_ok=False,
        )
        assert "requires args.fixMode" in out["error"], out
        assert "resolve_standards.py" in out["error"], out
        assert "calls" not in out
        assert _sha(fixture_skill) == before

    @pytest.mark.parametrize("lane", ["claude-md", "project-doc", "references"])
    def test_every_remediate_lane_carries_the_guard(self, lane):
        text = (MD_DOMAIN / "workflow" / f"{lane}-remediate.js").read_text(encoding="utf-8")
        guard = text.index("input.fixMode === 'propose'")
        assert guard < text.index("laneAgent(f.")


class TestConfigBlock:
    def _resolve(self, tmp_path, monkeypatch, user_cfg=None, proj_cfg=None):
        user = tmp_path / "user"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(user))
        proj = tmp_path / "proj"
        for base, cfg in ((user / "skills-kit", user_cfg), (proj / ".claude" / "skills-kit", proj_cfg)):
            if cfg is not None:
                base.mkdir(parents=True)
                (base / "config.yaml").write_text(cfg, encoding="utf-8")
        proj.mkdir(exist_ok=True)
        return sr.resolve(proj)

    def test_default_is_apply(self, tmp_path, monkeypatch):
        assert self._resolve(tmp_path, monkeypatch).audit == {"fix_mode": "apply"}

    def test_project_layer_wins_over_user_layer(self, tmp_path, monkeypatch):
        r = self._resolve(
            tmp_path, monkeypatch,
            user_cfg="audit:\n  fix_mode: propose\n",
            proj_cfg="audit:\n  fix_mode: apply\n",
        )
        assert r.audit["fix_mode"] == "apply"
        r = self._resolve(tmp_path / "b", monkeypatch, user_cfg="audit:\n  fix_mode: propose\n")
        assert r.audit["fix_mode"] == "propose"

    @pytest.mark.parametrize("cfg", [
        "audit:\n  fix_modee: propose\n",
        "audit:\n  fix_mode: sometimes\n",
        "audit:\n  fix_mode: true\n",
        "audit: propose\n",
    ])
    def test_unknown_key_or_value_is_rejected_loudly(self, tmp_path, monkeypatch, cfg):
        with pytest.raises(sr.StandardsConfigError):
            self._resolve(tmp_path, monkeypatch, proj_cfg=cfg)


def test_single_file_path_instruction_exists():
    """Pins that the instruction TEXT exists. This is not verification that an
    agent obeys it on the single-file path -- that path has no code to run."""
    text = AUDIT_LANE_MD.read_text(encoding="utf-8")
    assert "Propose-only" in text
    assert "ONE file: no script runs" in text
    assert "never when `fixMode` is `propose`" in text
