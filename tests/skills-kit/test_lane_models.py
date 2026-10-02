"""skills_kit_lib.lane_models: the layered md-domain `lane_models` config slot.

Covers the shipped defaults, layer precedence (a later layer's list replaces
the lower list wholesale), every loud error, effort findings collected across
all layers, the agent_route drop, the resolve_standards JSON block, the
`skills_kit_tool.py lane-models` CLI, and the parity between CORE_IDS and the
core-id list the generated LANE_ROUTE_CHUNK carries.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from bootstrap_lib import model_declaration
from bootstrap_lib.code_review.review_profiles import EFFORT_LEVELS
from skills_kit_lib import lane_models
from skills_kit_lib.lane_models import (
    AGENT_ONLY_FAMILIES,
    FAMILIES,
    IncompleteLaneModelsError,
    LaneModelsError,
    NoRunnableLaneModelError,
    agent_route,
    agent_routes,
    load_lane_models,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = REPO_ROOT / "plugins" / "skills-kit"
TOOL = PLUGIN / "scripts" / "skills_kit_tool.py"
RESOLVE = PLUGIN / "scripts" / "resolve_standards.py"

SONNET_LOW = [{"id": "sonnet", "effort": "low"}]
SHIPPED = {
    "detect": SONNET_LOW,
    "classify": SONNET_LOW,
    "coverage": SONNET_LOW,
    "generate": SONNET_LOW,
    "remediate": SONNET_LOW,
    "audit_job": [{"id": "luna", "effort": "high"}, {"id": "sonnet", "effort": "low"}],
}


def _user(home: Path, name: str = "config.yaml") -> Path:
    return home / ".claude" / "skills-kit" / name


def _project(root: Path, name: str = "config.yaml") -> Path:
    return root / ".claude" / "skills-kit" / name


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def roots(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    return home, project


# -- constants and shipped defaults ------------------------------------------


def test_family_constants_are_the_frozen_interface():
    assert AGENT_ONLY_FAMILIES == ("detect", "classify", "coverage", "generate", "remediate")
    assert FAMILIES == AGENT_ONLY_FAMILIES + ("audit_job",)


def test_shipped_defaults_resolve_unchanged_with_no_other_layer(roots):
    # Revert that turns this RED: change any entry in defaults/lane_models.yaml.
    home, project = roots
    assert load_lane_models(project, home) == SHIPPED
    assert list(load_lane_models(project, home)) == list(FAMILIES)


def test_shipped_file_names_no_frontier_model_for_an_agent_lane():
    # Convention (not enforced in code): astra and fable stay off review lanes.
    data = yaml.safe_load(lane_models.DEFAULTS_PATH.read_text(encoding="utf-8"))
    ids = {e["id"] for family in AGENT_ONLY_FAMILIES for e in data["lane_models"][family]}
    assert not ids & {"astra", "fable"}


# -- layering ----------------------------------------------------------------


def test_a_higher_layer_replaces_one_family_list_wholesale(roots):
    # Revert that turns this RED: merge entry lists instead of `resolved.update`.
    home, project = roots
    _write(_user(home), "lane_models:\n  detect: [{id: opus, effort: high}, {id: haiku, effort: low}]\n")
    _write(_project(project), "lane_models:\n  detect: [{id: haiku, effort: medium}]\n")
    got = load_lane_models(project, home)
    assert got["detect"] == [{"id": "haiku", "effort": "medium"}]
    assert {k: v for k, v in got.items() if k != "detect"} == {
        k: v for k, v in SHIPPED.items() if k != "detect"
    }


def test_user_layer_applies_when_the_project_is_silent(roots):
    home, project = roots
    _write(_user(home), "lane_models:\n  audit_job: [{id: opus, effort: max}]\n")
    _write(_project(project), "rules: {}\n")
    assert load_lane_models(project, home)["audit_job"] == [{"id": "opus", "effort": "max"}]


def test_config_local_overlay_wins_over_config_in_the_same_layer(roots):
    home, project = roots
    _write(_project(project), "lane_models:\n  classify: [{id: opus, effort: high}]\n")
    _write(_project(project, "config.local.yaml"), "lane_models:\n  classify: [{id: haiku, effort: low}]\n")
    assert load_lane_models(project, home)["classify"] == [{"id": "haiku", "effort": "low"}]


def test_without_home_the_user_layer_is_claude_config_dir(tmp_path, monkeypatch):
    config_dir = tmp_path / "cfg"
    _write(config_dir / "skills-kit" / "config.yaml",
           "lane_models:\n  generate: [{id: opus, effort: high}]\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    assert load_lane_models(None, None)["generate"] == [{"id": "opus", "effort": "high"}]


# -- loud errors -------------------------------------------------------------


@pytest.mark.parametrize(
    "body, needle",
    [
        ("lane_models:\n  detekt: [{id: sonnet, effort: low}]\n", "unknown family 'detekt'"),
        ("lane_models:\n  detect: {id: sonnet, effort: low}\n", "got a mapping"),
        ("lane_models:\n  detect: sonnet\n", "got str"),
        ("lane_models:\n  detect: [{id: sonnet, effort: low, model: x}]\n", "unknown field(s): 'model'"),
        ("lane_models:\n  detect: [{effort: low}]\n", "required field missing: id"),
        ("lane_models:\n  detect: [{id: sonnet, effort: lo}]\n", "unknown effort 'lo'"),
        ("lane_models:\n  detect: [{id: '  ', effort: low}]\n", "non-empty string"),
        ("lane_models:\n  detect: [{id: sonnet, effort: low}, {id: sonnet, effort: high}]\n",
         "duplicate id 'sonnet'"),
        ("lane_models:\n  detect: []\n", "must not be an empty list"),
        ("lane_models: [detect]\n", "must be a mapping of family"),
        ("lane_models:\n  detect: [42]\n", "got int"),
    ],
)
def test_malformed_layer_is_an_error_not_a_finding(roots, body, needle):
    # Revert that turns this RED: drop the matching check in _validate_family /
    # _validate_layer (the layer then resolves, or surfaces only as a finding).
    home, project = roots
    _write(_project(project), body)
    with pytest.raises(LaneModelsError) as exc:
        load_lane_models(project, home)
    assert not isinstance(exc.value, IncompleteLaneModelsError)
    assert needle in str(exc.value)
    assert str(_project(project)) in str(exc.value)


def test_every_effort_level_is_accepted(roots):
    home, project = roots
    body = "lane_models:\n" + "".join(
        f"  {fam}: [{{id: haiku, effort: {lvl}}}]\n"
        for fam, lvl in zip(FAMILIES, EFFORT_LEVELS + ("low",))
    )
    _write(_project(project), body)
    got = load_lane_models(project, home)
    assert [got[f][0]["effort"] for f in FAMILIES] == list(EFFORT_LEVELS) + ["low"]


def test_missing_shipped_file_is_an_error(roots, monkeypatch, tmp_path):
    home, project = roots
    monkeypatch.setattr(lane_models, "DEFAULTS_PATH", tmp_path / "nope.yaml")
    with pytest.raises(LaneModelsError, match="shipped lane_models file missing"):
        load_lane_models(project, home)


def test_shipped_file_missing_a_family_is_an_error(roots, monkeypatch, tmp_path):
    home, project = roots
    partial = tmp_path / "partial.yaml"
    partial.write_text("lane_models:\n  detect: [{id: sonnet, effort: low}]\n", encoding="utf-8")
    monkeypatch.setattr(lane_models, "DEFAULTS_PATH", partial)
    with pytest.raises(LaneModelsError, match="missing: classify"):
        load_lane_models(project, home)


def test_malformed_yaml_is_an_error(roots):
    home, project = roots
    _write(_user(home), "lane_models: [unclosed\n")
    with pytest.raises(LaneModelsError, match="malformed YAML"):
        load_lane_models(project, home)


# -- findings ----------------------------------------------------------------


def test_missing_effort_findings_are_collected_across_all_layers(roots):
    # The user layer's detect list is overridden by the project layer, and its
    # finding is still reported: findings cover every layer, not the winner.
    # Revert that turns this RED: collect findings only for the resolved lists,
    # or raise on the first finding.
    home, project = roots
    _write(_user(home), "lane_models:\n  detect: [{id: opus}]\n  classify: [haiku]\n")
    _write(_project(project), "lane_models:\n  detect: [{id: sonnet, effort: low}, {id: haiku}]\n")
    with pytest.raises(IncompleteLaneModelsError) as exc:
        load_lane_models(project, home)
    findings = exc.value.findings
    assert len(findings) == 3, findings
    assert any("user" in f and "lane_models.detect[0] (opus): missing effort" in f for f in findings)
    assert any("user" in f and "lane_models.classify[0] (haiku): states no effort" in f for f in findings)
    assert any("project" in f and "lane_models.detect[1] (haiku): missing effort" in f for f in findings)


def test_an_error_in_any_layer_outranks_findings(roots):
    home, project = roots
    _write(_user(home), "lane_models:\n  detect: [{id: opus}]\n")
    _write(_project(project), "lane_models:\n  bogus: [{id: opus, effort: low}]\n")
    with pytest.raises(LaneModelsError) as exc:
        load_lane_models(project, home)
    assert not isinstance(exc.value, IncompleteLaneModelsError)


# -- agent_route -------------------------------------------------------------


def test_agent_route_drops_a_non_core_id_and_keeps_order():
    # Revert that turns this RED: route every id to `run` (no CORE_IDS filter).
    entries = [
        {"id": "luna", "effort": "high"},
        {"id": "sonnet", "effort": "low"},
        {"id": "qwen38-5090", "effort": "medium"},
        {"id": "haiku", "effort": "low"},
    ]
    route = agent_route(entries)
    assert route["run"] == [{"id": "sonnet", "effort": "low"}, {"id": "haiku", "effort": "low"}]
    assert route["dropped"] == [
        {"id": "luna", "effort": "high", "reason": "not runnable by agent()/Agent"},
        {"id": "qwen38-5090", "effort": "medium", "reason": "not runnable by agent()/Agent"},
    ]


def test_agent_route_runs_every_core_id():
    entries = [{"id": i, "effort": "low"} for i in sorted(model_declaration.CORE_IDS)]
    assert agent_route(entries) == {"run": entries, "dropped": []}


def test_agent_route_with_nothing_runnable_raises():
    # Revert that turns this RED: return the empty route instead of raising.
    with pytest.raises(NoRunnableLaneModelError, match="luna \\(high\\)"):
        agent_route([{"id": "luna", "effort": "high"}])
    assert issubclass(NoRunnableLaneModelError, LaneModelsError)


def test_agent_route_rejects_a_malformed_entry():
    with pytest.raises(LaneModelsError):
        agent_route(["sonnet"])


def test_agent_routes_covers_only_agent_families(roots):
    home, project = roots
    _write(_project(project), "lane_models:\n  detect: [{id: luna, effort: high}, {id: sonnet, effort: low}]\n")
    block = agent_routes(load_lane_models(project, home))
    assert list(block) == list(AGENT_ONLY_FAMILIES)
    assert block["detect"]["declared"] == [{"id": "luna", "effort": "high"}, {"id": "sonnet", "effort": "low"}]
    assert block["detect"]["run"] == SONNET_LOW
    assert block["detect"]["dropped"][0]["id"] == "luna"
    assert block["coverage"] == {"declared": SONNET_LOW, "run": SONNET_LOW, "dropped": []}


def test_agent_routes_names_the_family_with_nothing_runnable(roots):
    home, project = roots
    _write(_project(project), "lane_models:\n  remediate: [{id: luna, effort: high}]\n")
    with pytest.raises(NoRunnableLaneModelError, match="lane_models.remediate"):
        agent_routes(load_lane_models(project, home))


def test_audit_job_keeps_luna_because_it_never_goes_through_agent_route(roots):
    home, project = roots
    resolved = load_lane_models(project, home)
    assert resolved["audit_job"][0] == {"id": "luna", "effort": "high"}
    assert "audit_job" not in agent_routes(resolved)


# -- subprocess CLIs ---------------------------------------------------------


def _child_env(**extra) -> dict:
    env = dict(os.environ, _BOOTSTRAP_GUARD_VENV_REEXEC="1", PYTHONDONTWRITEBYTECODE="1")
    env["PYTHONPATH"] = str(REPO_ROOT / "plugins" / "bootstrap")
    env.update(extra)
    return env


def _tool(cwd, *args, **env):
    return subprocess.run(
        [sys.executable, str(TOOL), "lane-models", *args],
        cwd=cwd, capture_output=True, text=True, env=_child_env(**env), timeout=120,
    )


def test_cli_check_exits_0_when_complete(roots):
    home, project = roots
    run = _tool(project, "--check", "--home", str(home), "--project-root", str(project))
    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith("lane_models: complete")


def test_cli_check_exits_1_on_findings_and_lists_them(roots):
    # Revert that turns this RED: map IncompleteLaneModelsError to exit 2.
    home, project = roots
    _write(_user(home), "lane_models:\n  detect: [{id: opus}]\n")
    run = _tool(project, "--check", "--home", str(home), "--project-root", str(project))
    assert run.returncode == 1, (run.stdout, run.stderr)
    assert "lane_models.detect[0] (opus): missing effort" in run.stderr
    assert run.stdout == ""


def test_cli_check_exits_2_on_a_malformed_layer(roots):
    home, project = roots
    _write(_project(project), "lane_models:\n  detect: []\n")
    run = _tool(project, "--check", "--home", str(home), "--project-root", str(project))
    assert run.returncode == 2, (run.stdout, run.stderr)
    assert "empty list" in run.stderr


def test_cli_without_check_prints_the_resolved_slot_as_yaml(roots):
    home, project = roots
    _write(_project(project), "lane_models:\n  detect: [{id: haiku, effort: low}]\n")
    run = _tool(project, "--home", str(home), "--project-root", str(project))
    assert run.returncode == 0, run.stderr
    expected = dict(SHIPPED, detect=[{"id": "haiku", "effort": "low"}])
    assert yaml.safe_load(run.stdout) == {"lane_models": expected}


def test_cli_home_overrides_claude_config_dir(roots, tmp_path):
    home, project = roots
    other = tmp_path / "other"
    _write(other / "skills-kit" / "config.yaml", "lane_models:\n  detect: []\n")
    run = _tool(project, "--check", "--home", str(home), "--project-root", str(project),
                CLAUDE_CONFIG_DIR=str(other))
    assert run.returncode == 0, run.stderr


def test_resolve_standards_json_carries_the_lane_models_block(tmp_path):
    # Revert that turns this RED: drop "lane_models" from the `out` dict.
    config_dir = tmp_path / "cfg"
    _write(config_dir / "skills-kit" / "config.yaml",
           "lane_models:\n  detect: [{id: luna, effort: high}, {id: sonnet, effort: low}]\n")
    project = tmp_path / "project"
    project.mkdir()
    proc = subprocess.run(
        [sys.executable, str(RESOLVE), "--project-root", str(project)],
        capture_output=True, text=True, timeout=120,
        env=_child_env(CLAUDE_CONFIG_DIR=str(config_dir)),
    )
    assert proc.returncode == 0, proc.stderr
    block = json.loads(proc.stdout)["lane_models"]
    assert list(block) == list(AGENT_ONLY_FAMILIES)
    assert block["detect"]["run"] == SONNET_LOW
    assert block["detect"]["dropped"] == [
        {"id": "luna", "effort": "high", "reason": "not runnable by agent()/Agent"}
    ]


@pytest.mark.parametrize(
    "body, needle",
    [
        ("lane_models:\n  detect: [{id: luna, effort: high}]\n", "lane_models.detect"),
        ("lane_models:\n  detect: [{id: opus}]\n", "missing effort"),
        ("lane_models:\n  detect: {id: opus, effort: low}\n", "got a mapping"),
    ],
)
def test_resolve_standards_exits_1_on_a_lane_models_problem(tmp_path, body, needle):
    config_dir = tmp_path / "cfg"
    _write(config_dir / "skills-kit" / "config.yaml", body)
    proc = subprocess.run(
        [sys.executable, str(RESOLVE), "--project-root", str(tmp_path)],
        capture_output=True, text=True, timeout=120,
        env=_child_env(CLAUDE_CONFIG_DIR=str(config_dir)),
    )
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert needle in proc.stderr


# -- JS parity ---------------------------------------------------------------

#: The ten md-domain workflow scripts that dispatch through LANE_ROUTE_CHUNK.
ROUTED_SCRIPTS = (
    "claude-md-detect.js", "skill-detect.js", "project-doc-detect.js",
    "references-classify.js", "coverage-detect.js", "claude-md-generate.js",
    "claude-md-remediate.js", "skill-remediate.js", "project-doc-remediate.js",
    "references-remediate.js",
)
WORKFLOW = PLUGIN / "skills" / "md-domain" / "workflow"
_CORE_LIST = re.compile(r"const LANE_CORE_IDS = \[([^\]]*)\]")


def js_core_ids(text: str) -> list[list[str]]:
    """Every `const LANE_CORE_IDS = [...]` id list in a script's text."""
    return [re.findall(r"""['"]([^'"]+)['"]""", body) for body in _CORE_LIST.findall(text)]


@pytest.mark.parametrize("name", ROUTED_SCRIPTS)
def test_shipped_script_core_ids_match_model_declaration(name):
    """Each SHIPPED workflow script throws on a `run` id outside its
    LANE_CORE_IDS, and agent_route drops ids outside CORE_IDS. The two lists
    must be the same set, or an id agent_route keeps is one the lane rejects
    (or the reverse). This reads the shipped bytes, not the generator's chunk:
    the chunk is rendered FROM CORE_IDS, so comparing it to CORE_IDS could not
    fail.

    Revert that turns this RED: add or remove an id in a script's
    LANE_CORE_IDS, or add an id to CORE_IDS without regenerating the scripts.
    """
    lists = js_core_ids((WORKFLOW / name).read_text(encoding="utf-8"))
    assert len(lists) == 1, f"{name}: expected one LANE_CORE_IDS list, got {lists}"
    assert sorted(lists[0]) == sorted(model_declaration.CORE_IDS)
