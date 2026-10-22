"""Guard: every SHIPPED model declaration lists a Claude-harness entry and a
Codex-harness entry, so an action survives either usage pool running out.

Every declaration is read from the real shipped file; the family of each id
comes from llm-scripting-kit's default_config.yaml (`harness: claude|codex`).
An id with no harness there fails loudly.

Named exemptions (a NEW Claude-only declaration is not exempt and fails):
  - review `validator_models`: a validator runs on an Agent entry only
    (plugins/CLAUDE.md register, "A validator runs on an Agent entry only").
  - skills-kit lane_models families detect, classify, coverage, generate,
    remediate: Workflow agent() runs Claude models only.
  - workflow-kit EXECUTOR_MODELS: a Claude subagent running one shell command.
"""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PLUGINS = ROOT / "plugins"

DEFAULT_CONFIG = PLUGINS / "llm-scripting-kit/lib/llm_scripting_kit/default_config.yaml"
ORCHESTRATION = PLUGINS / "awesome-kit/skills/orchestrate/defaults/orchestration.yaml"
REVIEW_PROFILES = PLUGINS / "bootstrap/bootstrap_lib/code_review/defaults/review_profiles.yaml"
LANE_MODELS = PLUGINS / "skills-kit/skills_kit_lib/defaults/lane_models.yaml"
TASK_ANSWERS = PLUGINS / "task-kit/skills/task/defaults/answers.yaml"

# Exempt lane_models families: Workflow agent() runs Claude models only.
AGENT_ONLY_LANE_FAMILIES = {"detect", "classify", "coverage", "generate", "remediate"}


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _harness_of(model_id: str) -> str:
    endpoints = _load(DEFAULT_CONFIG)["endpoints"]
    entry = endpoints.get(model_id)
    if not isinstance(entry, dict) or entry.get("harness") not in ("claude", "codex"):
        raise AssertionError(
            f"model id {model_id!r} has no claude/codex harness in {DEFAULT_CONFIG}"
        )
    return entry["harness"]


def _entry_id(entry) -> str:
    return entry["id"] if isinstance(entry, dict) else entry


def _declarations():
    """Yield (label, [ids]) for every non-exempt shipped declaration."""
    for row in _load(ORCHESTRATION)["routing"]:
        yield f"orchestration routing {row['shape']}", [_entry_id(m) for m in row["models"]]
    for profile in _load(REVIEW_PROFILES)["profiles"]:
        for reviewer in profile["reviewers"]:
            yield (
                f"review_profiles {profile['id']}.{reviewer['name']}",
                [_entry_id(m) for m in reviewer["model"]],
            )
    yield "task-kit answers.models", [_entry_id(m) for m in _load(TASK_ANSWERS)["answers"]["models"]]
    lanes = _load(LANE_MODELS)["lane_models"]
    for family, entries in lanes.items():
        if family in AGENT_ONLY_LANE_FAMILIES:
            continue
        yield f"lane_models {family}", [_entry_id(m) for m in entries]


def test_exempt_lane_families_are_the_only_ones_skipped():
    families = set(_load(LANE_MODELS)["lane_models"])
    assert families - AGENT_ONLY_LANE_FAMILIES == {"audit_job"}, (
        "a lane_models family was added: list both a claude and a codex entry, "
        "or name it as Claude-only in AGENT_ONLY_LANE_FAMILIES with a reason"
    )


@pytest.mark.parametrize("label,ids", list(_declarations()), ids=lambda v: v if isinstance(v, str) else "")
def test_declaration_lists_both_families(label, ids):
    families = {_harness_of(i) for i in ids}
    missing = {"claude", "codex"} - families
    assert not missing, (
        f"{label} lists {ids} with no {'/'.join(sorted(missing))}-harness entry; "
        "a shipped declaration must survive either pool running out"
    )
