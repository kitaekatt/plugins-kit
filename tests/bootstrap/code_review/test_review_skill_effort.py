"""Per-entry effort and the profile-resolution-first ordering in the generated review skills.

The resolved review-profile table gives every model entry -- reviewer and
validator alike -- its own `effort`, and the renderer refuses a table where any
entry lacks one. These tests pin what the two generated code-review skills (and
their configuration references and effort agents) tell an agent to do with that:

- profile resolution is the FIRST step, before prepare_review.py and before any
  question to the user, and a non-zero render exit stops the review;
- each entry is dispatched at its OWN effort -- an Agent entry to
  `<kit>:review-lane-<effort>`, a lane-tool entry with `--effort`;
- validators go to the same effort agents;
- no prose survives that lets a lane inherit the session's effort or tells the
  agent an endpoint ignores effort.

Every assertion reads text rendered by scripts/gen_code_review_skills.py, so a
template edit that drops one of these rules fails here even after regenerating
(which keeps the byte-identity drift guard green).
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
GEN_PATH = REPO_ROOT / "scripts" / "gen_code_review_skills.py"

_spec = importlib.util.spec_from_file_location("gen_code_review_skills_effort", GEN_PATH)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

VCS = ("git", "p4")
KIT = {"git": "git-kit", "p4": "p4-kit"}


def _flat(text: str) -> str:
    return " ".join(text.split())


def _steps(vcs: str) -> list[dict]:
    """The ordered steps of the rendered skill's full_review technique."""
    body = gen.render_skill(vcs)
    block = body.split("```yaml\n", 1)[1].split("\n```", 1)[0]
    data = yaml.safe_load(block)
    (technique,) = [
        t for t in data["technique_skill"]["techniques"] if t["id"] == "full_review"
    ]
    return technique["steps"]


def _step_text(step: dict) -> str:
    return _flat(" ".join(str(v) for v in step.values()))


# ---------------------------------------------------------------------------
# Resolution runs first and a failure stops the review.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vcs", VCS)
def test_profile_resolution_is_the_first_step_before_prepare(vcs: str) -> None:
    steps = _steps(vcs)
    numbers = [step["n"] for step in steps]
    assert numbers == sorted(numbers), "steps must be listed in execution order"

    render_steps = [
        i for i, step in enumerate(steps) if "render_review_profiles.py" in str(step.get("tool", ""))
    ]
    prepare_steps = [
        i for i, step in enumerate(steps) if "prepare_review.py" in str(step.get("tool", ""))
    ]
    assert render_steps == [0], (
        f"{vcs}: render_review_profiles.py must be the tool of the FIRST step and of no "
        f"other step; found at step indexes {render_steps}"
    )
    assert prepare_steps, f"{vcs}: no step runs prepare_review.py"
    assert render_steps[0] < min(prepare_steps)
    # No user interaction may precede it either: the first step is the render.
    assert "AskUserQuestion" not in _step_text(steps[0])
    assert "prompt the user" not in _step_text(steps[0])


@pytest.mark.parametrize("vcs", VCS)
def test_a_render_failure_stops_the_review(vcs: str) -> None:
    first = _steps(vcs)[0]
    on_failure = _flat(first["on_failure"])
    assert "A non-zero exit STOPS the review." in on_failure
    assert "Print the renderer's stderr verbatim" in on_failure
    assert "do not run step 1 or prepare_review.py" in on_failure
    assert "do not launch any reviewer or validator" in on_failure
    assert "Never guess, default, or fill in a missing model or effort." in on_failure


@pytest.mark.parametrize("vcs", VCS)
def test_resolution_does_not_wait_for_the_prepare_bundle(vcs: str) -> None:
    """Step 0 runs before prepare, so it cannot read `bundle.project_root`."""
    first = _step_text(_steps(vcs)[0])
    assert "<bundle.project_root>" not in first
    assert "the same root prepare_review.py later reports as `bundle.project_root`" in first


# ---------------------------------------------------------------------------
# Each entry runs at its own effort.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vcs", VCS)
def test_agent_entries_dispatch_to_their_own_effort_agent(vcs: str) -> None:
    body = _flat(gen.render_skill(vcs))
    kit = KIT[vcs]
    assert (
        f"`subagent_type: {kit}:review-lane-<entry effort>` and `model: <entry id>`" in body
    )
    assert "An entry's EFFORT is the `effort` stated beside its `id` in that list." in body
    assert "Dispatch the chosen entry by the entry-harness rule, at the chosen entry's own effort." in body
    assert "A re-selected entry runs at ITS OWN stated effort, never at the failed entry's." in body


@pytest.mark.parametrize("vcs", VCS)
def test_lane_tool_entries_are_passed_their_effort(vcs: str) -> None:
    body = _flat(gen.render_skill(vcs))
    assert "`--model <entry id>`, `--effort <entry effort>`, `--bundle" in body
    ref = _flat(gen.render_configuration(vcs))
    assert "with `--model <id> --effort <effort>`" in ref


@pytest.mark.parametrize("vcs", VCS)
def test_a_runner_refusal_is_not_re_selected_past(vcs: str) -> None:
    body = _flat(gen.render_skill(vcs))
    assert "Exit 2 from the runner is a REFUSAL" in body
    assert "older than 0.60.0, the first release that accepts `--effort`" in body
    assert "Do not re-select past it, and do not re-run the entry at another effort." in body


@pytest.mark.parametrize("vcs", VCS)
def test_validators_dispatch_to_the_effort_agent(vcs: str) -> None:
    steps = _steps(vcs)
    (validate,) = [step for step in steps if step["n"] == 7]
    action = _flat(validate["action"])
    kit = KIT[vcs]
    assert "is a list of exactly one `{id, effort}` entry" in action
    assert f"`subagent_type: {kit}:review-lane-<effort>` and `model: <id>`" in action


@pytest.mark.parametrize("vcs", VCS)
def test_no_subagent_is_declared_general_purpose(vcs: str) -> None:
    """Every reviewer and the validator now runs on an effort agent."""
    body = gen.render_skill(vcs)
    block = body.split("```yaml\n", 1)[1].split("\n```", 1)[0]
    subagents = yaml.safe_load(block)["technique_skill"]["subagents"]
    for agent in subagents:
        assert agent["subagent_type"].startswith(f"{KIT[vcs]}:review-lane-<entry effort>"), agent


# ---------------------------------------------------------------------------
# Retired prose stays retired.
# ---------------------------------------------------------------------------

_RETIRED = (
    "inherits this session's effort",
    "session-inherited effort",
    "INHERITS the invoking session's effort",
    "does NOT reach an ENDPOINT lane",
    "An ENDPOINT lane ignores `effort`",
    "runs at the endpoint's configured effort",
    "model_fallbacks",
    "keeps `general-purpose`",
)


def test_no_session_inherit_or_endpoint_ignores_effort_prose() -> None:
    for path, text in gen.targets().items():
        flat = _flat(text)
        for phrase in _RETIRED:
            assert phrase not in flat, f"{path}: retired prose survives: {phrase!r}"


def test_no_session_effort_value_anywhere() -> None:
    for path, text in gen.targets().items():
        assert "review-lane-session" not in text, path
        assert not re.search(r"effort:\s*session\b", text), path


# ---------------------------------------------------------------------------
# configuration.md documents the frozen interface.
# ---------------------------------------------------------------------------


def _example_blocks(ref: str) -> list[dict]:
    """Every ```yaml block in configuration.md except the shipped-defaults table."""
    head, tail = ref.split("## Shipped defaults", 1)
    tail = tail.split("\n## ", 1)[1]
    blocks = re.findall(r"```yaml\n(.*?)```", head + tail, flags=re.S)
    return [yaml.safe_load(block) for block in blocks]


@pytest.mark.parametrize("vcs", VCS)
def test_worked_overrides_use_the_entry_shape_with_a_level_placeholder(vcs: str) -> None:
    examples = _example_blocks(gen.render_configuration(vcs))
    assert len(examples) >= 3
    for example in examples:
        for profile in example["profiles"]:
            for reviewer in profile["reviewers"]:
                assert "effort" not in reviewer, "lane-level effort was removed"
                assert isinstance(reviewer["model"], list) and reviewer["model"]
                for entry in reviewer["model"]:
                    assert set(entry) == {"id", "effort"}, entry
                    assert entry["effort"] == "<level>", (
                        "worked examples choose no effort value; the user picks it"
                    )


@pytest.mark.parametrize("vcs", VCS)
def test_configuration_documents_check_and_completeness(vcs: str) -> None:
    ref = _flat(gen.render_configuration(vcs))
    assert "/scripts/render_review_profiles.py\" --check --project-root <project root>" in ref
    assert (
        "exits 0 when every layer is complete, 1 when there are findings, and 2 when a layer "
        "is malformed or invalid"
    ) in ref
    assert (
        "<layer> <path>: profiles[<id>].reviewers[<name>].model[<i>] (<model id>): missing effort"
    ) in ref
    assert "A lane-level `effort` is one of those errors" in ref
    assert "There is no default and no inherited effort" in ref


# ---------------------------------------------------------------------------
# The effort agents serve validators too.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vcs", VCS)
def test_effort_agents_name_both_lane_kinds(vcs: str) -> None:
    for effort in gen.EFFORT_LEVELS:
        agent = _flat(gen.render_agent(vcs, effort))
        assert f"executor for one {KIT[vcs]} reviewer or validator lane" in agent
        assert "steps 6 and 7 select this agent" in agent
        assert "You run ONE reviewer or validator lane" in agent
