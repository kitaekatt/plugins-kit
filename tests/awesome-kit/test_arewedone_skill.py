"""Static invariants for awesome-kit's arewedone skill (impl-plan row K6).

The skill has two modes: active (owned running work is all that remains, the
turn ends "Waiting on <what>") and finished (the audit, ending "All done" when
nothing owned runs). The plugin ships standalone, so the skill states the
stopping rule inline and names no file or plugin outside awesome-kit.
"""

import re
from pathlib import Path

import pytest
import yaml

_SKILL_DIR = (
    Path(__file__).resolve().parents[2] / "plugins" / "awesome-kit" / "skills" / "arewedone"
)
_SKILL_MD = _SKILL_DIR / "SKILL.md"
_OPENAI_YAML = _SKILL_DIR / "agents" / "openai.yaml"


def _frontmatter(text: str) -> dict:
    return yaml.safe_load(text.split("---", 2)[1])


def _gate_section(text: str) -> str:
    """The mode-selection section (step 0), up to step 1."""
    start = text.index("## 0. Choose the mode")
    return text[start : text.index("## 1.", start)]


def _active_gate_conditions(text: str) -> str:
    """The condition list of the active-mode gate."""
    gate = _gate_section(text)
    return gate[gate.index("Active mode applies only when") : gate.index("**Active mode.**")]


def test_description_is_directive_and_short() -> None:
    description = _frontmatter(_SKILL_MD.read_text(encoding="utf-8"))["description"]
    assert description.startswith("Use when")
    assert "Do NOT use for" in description
    assert len(description) <= 160


def test_name_matches_directory() -> None:
    assert _frontmatter(_SKILL_MD.read_text(encoding="utf-8"))["name"] == "arewedone"


@pytest.mark.parametrize("path", [_SKILL_MD, _OPENAI_YAML])
def test_shipped_files_are_ascii(path: Path) -> None:
    path.read_bytes().decode("ascii")


@pytest.mark.parametrize("path", [_SKILL_MD, _OPENAI_YAML])
def test_no_reference_outside_the_plugin(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for needle in (
        "USER-PREFERENCES",
        "stop-turn-guard",
        "presence-kit",
        "presence_kit",
        "claude-settings",
        "/Users/",
        "TaskOutput",
    ):
        assert needle not in text, f"{path.name} names {needle!r}"
    # Every markdown link target stays inside the skill directory.
    for target in re.findall(r"\]\(([^)]+)\)", text):
        assert not target.startswith(("..", "/", "http")), target
        assert (_SKILL_DIR / target.split("#")[0]).exists(), target


def test_both_modes_and_both_endings_present() -> None:
    text = _SKILL_MD.read_text(encoding="utf-8")
    gate = _gate_section(text)
    assert "**Active mode.**" in gate
    assert "**Finished mode.**" in gate
    assert 'last line\n"Waiting on <what is running>"' in gate
    assert '"All done"' in gate
    for step in ("## 1.", "## 2.", "## 3.", "## 4."):
        assert step in text


def test_active_mode_keyed_on_owned_running_work_only() -> None:
    text = _SKILL_MD.read_text(encoding="utf-8")
    conditions = _active_gate_conditions(text)
    assert "launched and owns" in conditions
    assert "still running" in conditions
    for kind in ("background job", "delegated agent", "workflow", "session cron"):
        assert kind in conditions
    assert "No other actionable work remains" in conditions
    # Mid-work with nothing owned running is not active mode.
    assert "Ending a turn is not waiting on yourself" in _gate_section(text)
    assert "mid-work" not in conditions.lower()


def test_stopping_rule_is_stated_inline() -> None:
    text = _SKILL_MD.read_text(encoding="utf-8")
    assert "The stopping rule is completion" in text
    assert "A check is not a boundary" in text
    assert "An incidental problem is not scope" in text


def test_finished_mode_reports_only_what_is_new() -> None:
    text = _SKILL_MD.read_text(encoding="utf-8")
    assert "Report only what the last message did not\nalready say" in _gate_section(text)
    assert "the report is the verdict line alone" in _gate_section(text)


def test_openai_yaml_prompt_is_aligned() -> None:
    interface = yaml.safe_load(_OPENAI_YAML.read_text(encoding="utf-8"))["interface"]
    prompt = interface["default_prompt"]
    assert "Waiting on <what is running>" in prompt
    assert "All done" in prompt
    assert "Active mode" in prompt and "Finished mode" in prompt
    for kind in ("background job", "delegated agent", "workflow", "session cron"):
        assert kind in prompt
    assert "Ending a turn is not waiting on myself" in prompt
    assert "Completion is the stopping rule" in prompt
    assert "Cleanup I took care of" in prompt
    assert "Omit empty buckets" in prompt
    assert "did not already say" in prompt
    assert "verdict line alone" in prompt
    # The skill's descriptor section names the same file.
    assert "agents/openai.yaml" in _SKILL_MD.read_text(encoding="utf-8")
