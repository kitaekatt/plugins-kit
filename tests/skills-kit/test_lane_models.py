"""skills_kit_lib.lane_models: the layered md-domain `lane_models` config slot.

Covers the shipped defaults, layer precedence (a later layer's list replaces
the lower list wholesale), every loud error, effort findings collected across
all layers, the describe-ranked lane_route (and the deprecated agent_route
shim), the resolve_standards JSON block, the
`skills_kit_tool.py lane-models` CLI, and the parity between CORE_IDS and the
core-id list the generated LANE_ROUTE_CHUNK carries.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
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
    lane_route,
    lane_routes,
    load_lane_models,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = REPO_ROOT / "plugins" / "skills-kit"
TOOL = PLUGIN / "scripts" / "skills_kit_tool.py"
RESOLVE = PLUGIN / "scripts" / "resolve_standards.py"

SONNET_LOW = [{"id": "sonnet", "effort": "low"}]
HAIKU_LOW = [{"id": "haiku", "effort": "low"}]
LUNA_HIGH = {"id": "luna", "effort": "high"}
LUNA_LOW = {"id": "luna", "effort": "low"}
SHIPPED = {
    "detect": SONNET_LOW + [LUNA_HIGH],
    "classify": HAIKU_LOW + [LUNA_LOW],
    "coverage": SONNET_LOW + [LUNA_HIGH],
    "generate": SONNET_LOW + [LUNA_HIGH],
    "remediate": HAIKU_LOW + [LUNA_LOW],
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


# -- agent_route (deprecated shim) ---------------------------------------------


def test_agent_route_is_a_deprecated_shim_that_keeps_the_core_id_filter():
    # Revert that turns this RED: drop the DeprecationWarning, or change the
    # shim's behaviour (it must stay the old filter for the policy window).
    entries = [
        {"id": "luna", "effort": "high"},
        {"id": "sonnet", "effort": "low"},
        {"id": "qwen38-5090", "effort": "medium"},
        {"id": "haiku", "effort": "low"},
    ]
    with pytest.warns(DeprecationWarning, match="use lane_route"):
        route = agent_route(entries)
    assert route["run"] == [{"id": "sonnet", "effort": "low"}, {"id": "haiku", "effort": "low"}]
    assert route["dropped"] == [
        {"id": "luna", "effort": "high", "reason": "not runnable by agent()/Agent"},
        {"id": "qwen38-5090", "effort": "medium", "reason": "not runnable by agent()/Agent"},
    ]
    with pytest.warns(DeprecationWarning, match="use lane_route"):
        with pytest.raises(NoRunnableLaneModelError, match="luna \\(high\\)"):
            agent_route([{"id": "luna", "effort": "high"}])
    with pytest.warns(DeprecationWarning, match="use lane_routes"):
        block = agent_routes(load_lane_models())
    assert list(block) == list(AGENT_ONLY_FAMILIES)


# -- lane_route: describe ranking --------------------------------------------


@dataclass(frozen=True)
class _State:
    id: str
    kind: str = "harness"
    harness: str = "claude"
    drive: str = "agent"
    usable: bool = True
    via: str | None = None
    status_text: str = "n/a (unpaced)"


@dataclass(frozen=True)
class _Disp:
    id: str
    disposition: str = "usable"
    detail: str = ""


class _Ranking:
    def __init__(self, rendered, dispositions):
        self.rendered_entries = tuple(rendered)
        self.dispositions = tuple(dispositions)


def _fake_describe(rendered, dispositions=None, calls=None):
    def describe(names, **kwargs):
        if calls is not None:
            calls.append((list(names), kwargs))
        disps = dispositions if dispositions is not None else [_Disp(s.id) for s in rendered]
        return _Ranking(rendered, disps)
    return describe


CODEX = dict(harness="codex", drive="codex exec")


def test_lane_route_gives_claude_core_ids_the_agent_route_and_codex_the_codex_route():
    # Revert that turns this RED: restore the core-id filter (codex dropped).
    entries = [{"id": "sonnet", "effort": "low"}, {"id": "luna", "effort": "high"}]
    calls = []
    route = lane_route(entries, describe=_fake_describe(
        [_State("sonnet"), _State("luna", **CODEX)], calls=calls))
    assert route == {
        "run": [{"id": "sonnet", "effort": "low", "route": "agent"},
                {"id": "luna", "effort": "high", "route": "codex"}],
        "dropped": [],
    }
    (names, kwargs), = calls
    assert names == ["sonnet", "luna"]
    assert kwargs["caller"] == "session"


def test_lane_route_follows_describe_order_so_a_reset_codex_entry_leads():
    # describe puts a usable reset entry first; the run list keeps that order
    # and each entry keeps its DECLARED effort.
    # Revert that turns this RED: build run in declaration order.
    entries = [{"id": "sonnet", "effort": "low"}, {"id": "luna", "effort": "high"}]
    route = lane_route(entries, describe=_fake_describe(
        [_State("luna", **CODEX), _State("sonnet")]))
    assert [(e["id"], e["effort"], e["route"]) for e in route["run"]] == [
        ("luna", "high", "codex"), ("sonnet", "low", "agent")]


def test_lane_route_drops_unusable_unrouted_and_hidden_entries_with_reasons():
    entries = [
        {"id": "opus", "effort": "high"},
        {"id": "sol", "effort": "high"},
        {"id": "qwen", "effort": "medium"},
        {"id": "gone", "effort": "low"},
        {"id": "haiku", "effort": "low"},
    ]
    rendered = [
        _State("opus", usable=False, status_text="out of quota until 2026-11-26 03:00 UTC"),
        _State("sol", **CODEX),
        _State("qwen", kind="transport", harness=None, drive="openai-compat"),
        _State("haiku"),
    ]
    disps = [_Disp("opus", "out-of-quota"), _Disp("sol"), _Disp("qwen"),
             _Disp("gone", "disabled", "disabled by config"), _Disp("haiku")]
    route = lane_route(entries, describe=_fake_describe(rendered, disps))
    assert [e["id"] for e in route["run"]] == ["sol", "haiku"]
    reasons = {d["id"]: d["reason"] for d in route["dropped"]}
    assert [d["id"] for d in route["dropped"]] == ["opus", "qwen", "gone"]
    assert reasons["opus"] == "not usable now: out of quota until 2026-11-26 03:00 UTC"
    assert reasons["qwen"].startswith("no md-domain lane route for transport/openai-compat")
    assert reasons["gone"] == "disabled: disabled by config"
    assert all(d["effort"] for d in route["dropped"])


def test_lane_route_drops_a_claude_entry_that_is_not_a_core_id():
    route = lane_route(
        [{"id": "claude-cli-opus", "effort": "high"}, {"id": "sonnet", "effort": "low"}],
        describe=_fake_describe([_State("claude-cli-opus", drive="cli"), _State("sonnet")]))
    assert [e["id"] for e in route["run"]] == ["sonnet"]
    assert route["dropped"][0]["id"] == "claude-cli-opus"


def test_lane_route_with_nothing_routable_raises():
    # Revert that turns this RED: return the empty route instead of raising.
    with pytest.raises(NoRunnableLaneModelError, match="qwen \\(medium\\)"):
        lane_route([{"id": "qwen", "effort": "medium"}], describe=_fake_describe(
            [_State("qwen", kind="transport", harness=None, drive="openai-compat")]))
    assert issubclass(NoRunnableLaneModelError, LaneModelsError)


def test_lane_route_turns_the_describe_floor_into_no_runnable():
    from llm_kit.dispatch.declaration import NoUsableRoutingTarget

    def floor(names, **kwargs):
        raise NoUsableRoutingTarget(list(names), (), "session")

    with pytest.raises(NoRunnableLaneModelError, match="no declared entry is usable"):
        lane_route([{"id": "luna", "effort": "high"}], describe=floor)


def test_lane_route_rejects_a_malformed_entry():
    with pytest.raises(LaneModelsError):
        lane_route(["sonnet"], describe=_fake_describe([]))


def test_lane_routes_covers_only_agent_families_and_names_the_failing_family(roots):
    home, project = roots
    _write(_project(project), "lane_models:\n  remediate: [{id: qwen, effort: low}]\n")
    resolved = load_lane_models(project, home)

    def describe(names, **kwargs):
        rendered = [_State(n, kind="transport", harness=None, drive="openai-compat")
                    if n == "qwen" else (_State(n, **CODEX) if n == "luna" else _State(n))
                    for n in names]
        return _Ranking(rendered, [_Disp(n) for n in names])

    with pytest.raises(NoRunnableLaneModelError, match="lane_models.remediate"):
        lane_routes(resolved, describe=describe)
    shipped = lane_routes(load_lane_models(None, home), describe=describe)
    assert list(shipped) == list(AGENT_ONLY_FAMILIES)
    assert "audit_job" not in shipped
    assert shipped["detect"]["declared"] == SHIPPED["detect"]


def test_audit_job_keeps_luna_first_for_the_llm_kit_job_runner(roots):
    home, project = roots
    resolved = load_lane_models(project, home)
    assert resolved["audit_job"][0] == {"id": "luna", "effort": "high"}


def test_every_shipped_agent_family_declares_a_claude_entry_first_and_a_codex_entry(roots):
    # Claude first keeps today's choice when no override is set; the codex entry
    # is what a reset_mode override moves to the front.
    # Revert that turns this RED: remove the codex entry from a family in
    # defaults/lane_models.yaml.
    home, _project_root = roots
    shipped = load_lane_models(None, home)
    for family in AGENT_ONLY_FAMILIES:
        ids = [e["id"] for e in shipped[family]]
        assert model_declaration.is_core_id(ids[0]), (family, ids)
        assert "luna" in ids[1:], (family, ids)


# -- subprocess CLIs ---------------------------------------------------------


def _child_env(**extra) -> dict:
    env = dict(os.environ, _BOOTSTRAP_GUARD_SKIP_REEXEC="1", PYTHONDONTWRITEBYTECODE="1")
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
           "lane_models:\n  detect: [{id: no-such-model, effort: high}, {id: sonnet, effort: low}]\n"
           "  classify: [{id: sonnet, effort: low}]\n  coverage: [{id: sonnet, effort: low}]\n"
           "  generate: [{id: sonnet, effort: low}]\n  remediate: [{id: sonnet, effort: low}]\n")
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
    assert block["detect"]["run"] == [{"id": "sonnet", "effort": "low", "route": "agent"}]
    (dropped,) = block["detect"]["dropped"]
    assert (dropped["id"], dropped["effort"]) == ("no-such-model", "high")
    assert dropped["reason"]


@pytest.mark.parametrize(
    "body, needle",
    [
        ("lane_models:\n  detect: [{id: no-such-model, effort: high}]\n", "lane_models.detect"),
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
    LANE_CORE_IDS, and lane_route gives the agent route only to CORE_IDS. The
    two lists must be the same set, or an id lane_route keeps is one the lane rejects
    (or the reverse). This reads the shipped bytes, not the generator's chunk:
    the chunk is rendered FROM CORE_IDS, so comparing it to CORE_IDS could not
    fail.

    Revert that turns this RED: add or remove an id in a script's
    LANE_CORE_IDS, or add an id to CORE_IDS without regenerating the scripts.
    """
    lists = js_core_ids((WORKFLOW / name).read_text(encoding="utf-8"))
    assert len(lists) == 1, f"{name}: expected one LANE_CORE_IDS list, got {lists}"
    assert sorted(lists[0]) == sorted(model_declaration.CORE_IDS)


def test_lane_route_seeds_core_ids_as_agent_tool_reachable_for_describe():
    # agent() runs a Claude core id in the session; the claude CLI on PATH is
    # not what dispatches it, so describe must not rule it out for a missing CLI.
    # Revert that turns this RED: call describe without the reachability seed.
    calls = []
    lane_route([{"id": "sonnet", "effort": "low"}, {"id": "luna", "effort": "high"}],
               describe=_fake_describe([_State("sonnet"), _State("luna", **CODEX)], calls=calls))
    (_names, kwargs), = calls
    seeded = kwargs["reachability_cache"]
    assert set(seeded) == {"sonnet"}
    assert seeded["sonnet"].status == "reachable" and seeded["sonnet"].checked == "agent-tool"


def test_lane_route_keeps_sonnet_routable_when_the_claude_cli_is_not_on_path(monkeypatch):
    # The real describe, with an empty PATH: no claude and no codex CLI.
    monkeypatch.setenv("PATH", "")
    route = lane_route([{"id": "sonnet", "effort": "low"}, {"id": "luna", "effort": "high"}])
    assert route["run"] == [{"id": "sonnet", "effort": "low", "route": "agent"}]
    assert route["dropped"][0]["id"] == "luna"
