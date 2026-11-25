"""Guard: every SHIPPED model declaration lists a Claude-harness entry and a
Codex-harness entry, so an action survives either usage pool running out.

Every declaration is read from the real shipped file; the family of each id
comes from llm_kit.registry.models.DEFAULT_MODEL_CONFIG (`harness: claude|codex`).
An id with no harness there fails loudly.

Named exemptions (a NEW Claude-only declaration is not exempt and fails):
  - workflow-kit EXECUTOR_MODELS: a Claude subagent running one shell command.
"""

from pathlib import Path

import pytest
import yaml
from llm_kit.registry.models import DEFAULT_MODEL_CONFIG

ROOT = Path(__file__).resolve().parents[2]
PLUGINS = ROOT / "plugins"

ORCHESTRATION = PLUGINS / "awesome-kit/skills/orchestrate/defaults/orchestration.yaml"
REVIEW_PROFILES = PLUGINS / "bootstrap/bootstrap_lib/code_review/defaults/review_profiles.yaml"
LANE_MODELS = PLUGINS / "skills-kit/skills_kit_lib/defaults/lane_models.yaml"
TASK_ANSWERS = PLUGINS / "task-kit/skills/task/defaults/answers.yaml"


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _harness_of(model_id: str) -> str:
    endpoints = DEFAULT_MODEL_CONFIG["endpoints"]
    entry = endpoints.get(model_id)
    if not isinstance(entry, dict) or entry.get("harness") not in ("claude", "codex"):
        raise AssertionError(
            f"model id {model_id!r} has no claude/codex harness in llm_kit DEFAULT_MODEL_CONFIG"
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
        for reason, entries in profile["validator_models"].items():
            yield (
                f"review_profiles {profile['id']}.validator_models.{reason}",
                [_entry_id(m) for m in entries],
            )
    yield "task-kit answers.models", [_entry_id(m) for m in _load(TASK_ANSWERS)["answers"]["models"]]
    lanes = _load(LANE_MODELS)["lane_models"]
    for family, entries in lanes.items():
        yield f"lane_models {family}", [_entry_id(m) for m in entries]


@pytest.mark.parametrize("label,ids", list(_declarations()), ids=lambda v: v if isinstance(v, str) else "")
def test_declaration_lists_both_families(label, ids):
    families = {_harness_of(i) for i in ids}
    missing = {"claude", "codex"} - families
    assert not missing, (
        f"{label} lists {ids} with no {'/'.join(sorted(missing))}-harness entry; "
        "a shipped declaration must survive either pool running out"
    )
